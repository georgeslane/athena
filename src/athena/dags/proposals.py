"""What's proposed for agents' DAGs, for you to approve or reject: kept in agentic-airflow's queue of proposals
(engine.list_recommendations), so whatever proposes a change, a run's reviews or anything else, it waits in one
place, on the dashboard.

Each is a whole DAG file (agentic-airflow's "definition" kind), read when it's proposed, so one that won't read
never waits for you:

  workflow  the file the workflow review proposes (ends.py), against the file as it is
  tools     the file with the tool lists the tool review proposes; the lists are kept too ("changes"), to apply
            again to the file as it is when you approve them

Approving one saves its file (Dags.save). One proposed against a file that's changed since is out of date
("stale"), unless it only changes tool lists; and when one's approved, those still waiting are brought up to date
where they can be. Athena takes no other kind: the engine's overrides would make what runs differ from the file,
and Athena's skills are its own (skills.py).

Each says which tools that ask first it would let run without asking, so a change to allowed_tools is plain to
see, not only in the diff.
"""

from __future__ import annotations

import difflib
import logging
import time
from typing import TYPE_CHECKING, Any

from agentic_airflow import Applier, ApprovalError, StaleProposalError, resolve_config, resolve_permissions
from agentic_airflow import proposals as queue
from agentic_airflow.errors import ConfigurationError

from athena.dags.reader import DagError, read, with_tools

if TYPE_CHECKING:
    from athena.dags.service import Dags

log = logging.getLogger(__name__)

KIND = "definition"  # agentic-airflow's kind of proposal: a whole new file
WORKFLOW, TOOLS = "workflow", "tools"  # what one changes: its payload's "what"
SOURCE = "review"  # what proposed it, as agentic-airflow keeps it: the reviews at the end of a run
WHY = {WORKFLOW: "The workflow review recommends it.", TOOLS: "The tool review recommends it."}


class Proposals:
    def __init__(self, dags: Dags):
        self.dags = dags
        self.engine = dags.engine
        self.engine.appliers.clear()
        self.engine.appliers[KIND] = Applier(self._apply, self._check)

    # -- proposing -------------------------------------------------------------------------------------

    def propose(
        self, agent_id: int, run_id: str | None, what: str, why: str, file: str, changes: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """A new file for an agent's DAG, for you to approve: the proposal, or None if it changes nothing. One of the
        same kind still waiting is withdrawn: this one's newer."""
        dag = self.dags.dag(agent_id)
        if dag is None:
            return None
        older = [r for r in self.waiting(agent_id) if r["payload"].get("what") == what]
        details = {"agent": agent_id, "what": what, "changes": changes or {}}
        try:
            made = queue.propose_definition(
                self.engine,
                dag.id,
                file,
                why.strip() or WHY[what],
                title=what,
                run_id=run_id,
                details=details,
                source=SOURCE,
            )
        except ConfigurationError as exc:  # it's the file already, or it won't do
            log.info("Not proposing a change to agent %d's DAG: %s", agent_id, exc)
            return None
        for rec in older:
            queue.withdraw_proposal(self.engine, rec["id"], reason="A newer run proposed another.")
        return made

    def carry_over(self, old: list[tuple[int, str, str, str, dict[str, Any], str]]) -> None:
        """Proposals waiting from before they were kept in the engine's queue (store.take_old_proposals): proposed
        again, against the DAGs as they are now, or dropped if they no longer fit."""
        for agent_id, what, why, file, changes, base in old:
            current = self.dags.store.get(agent_id).dag
            try:
                if what == TOOLS:
                    self.propose(agent_id, None, TOOLS, why, with_tools(current, changes), changes)
                elif same(base, current):
                    self.propose(agent_id, None, WORKFLOW, why, file)
            except (DagError, SyntaxError):
                log.info("Dropping a proposal for agent %d's DAG from before: it no longer fits", agent_id)

    def _check(self, engine: Any, rec: dict[str, Any]) -> None:
        """As a proposal's made: the file must be one this agent's DAG could be."""
        agent_id = self.agent(rec)
        if agent_id is None:
            raise ConfigurationError("None of the agents has that DAG.")
        try:
            self.dags.check(agent_id, rec["payload"]["definition"])
        except DagError as exc:
            raise ConfigurationError(f"That DAG file won't do: {exc}") from None

    # -- deciding --------------------------------------------------------------------------------------

    async def decide(self, rec_id: str, approve: bool) -> int:
        """Approve or reject a proposal: the agent it's for. ValueError if it can't be, as when the DAG's changed
        since it was proposed."""
        rec = self.engine.state.get_recommendation(rec_id)
        agent_id = self.agent(rec) if rec else None
        if rec is None or rec["kind"] != KIND or rec["status"] != "pending" or agent_id is None:
            raise ValueError("That proposal isn't waiting for an answer.")
        try:
            if approve:
                await self.engine.approve_recommendation(rec_id)
            else:
                await self.engine.reject_recommendation(rec_id)
        except StaleProposalError as exc:
            raise ValueError(str(exc)) from None
        except ApprovalError:  # decided meanwhile
            raise ValueError("That proposal isn't waiting for an answer.") from None
        return agent_id

    def _apply(self, engine: Any, rec: dict[str, Any]) -> dict[str, Any]:
        """You approved it: save its file, and bring those still waiting up to date."""
        payload = rec["payload"]
        agent_id = self.agent(rec)
        before = self.dags.store.get(agent_id).dag  # type: ignore[arg-type]
        if payload.get("what") == TOOLS:
            after = self._fitted(before, payload.get("changes") or {})
        elif same(payload["base"], before):
            after = payload["definition"]
        else:
            raise StaleProposalError("The DAG has changed since this was proposed, so it's out of date.")
        try:
            self.dags.save(agent_id, after)  # type: ignore[arg-type]
        except DagError as exc:
            raise StaleProposalError(f"It no longer fits the DAG: {exc}") from None
        self._catch_up(agent_id, rec, before, after)  # type: ignore[arg-type]
        return {"saved": True}

    def _catch_up(self, agent_id: int, approved: dict[str, Any], before: str, after: str) -> None:
        """Those still waiting, against the file as it is now: tool lists apply again, and a workflow change
        proposed against the file as it was gets the tool lists just approved."""
        changes = approved["payload"].get("changes") or {}
        for rec in self.waiting(agent_id):
            payload = rec["payload"]
            try:
                if payload.get("what") == TOOLS:
                    file = self._fitted(after, payload.get("changes") or {})
                elif approved["payload"].get("what") == TOOLS and same(payload["base"], before):
                    file = self._fitted(payload["definition"], changes)
                else:
                    continue
            except StaleProposalError as exc:
                self._stale(rec, str(exc))
                continue
            if same(file, after):
                queue.withdraw_proposal(self.engine, rec["id"], reason="The DAG does this already.")
                continue
            payload["definition"], payload["base"] = file, after
            self.engine.state.save_recommendation(rec)

    @staticmethod
    def _fitted(file: str, changes: dict[str, Any]) -> str:
        try:
            return with_tools(file, changes)
        except (DagError, SyntaxError) as exc:
            raise StaleProposalError(f"It no longer fits the DAG: {exc}") from None

    def _stale(self, rec: dict[str, Any], why: str) -> None:
        rec.update(status="stale", decided_at=time.time(), reason=why)
        self.engine.state.save_recommendation(rec)

    # -- what's proposed -------------------------------------------------------------------------------

    def agent(self, rec: dict[str, Any]) -> int | None:
        """The agent a proposal's for: as it says, or, from elsewhere, the agent whose DAG it's for."""
        agent_id = rec["payload"].get("agent")
        return agent_id if isinstance(agent_id, int) else self.dags.owner(rec["dag_id"])

    def waiting(self, agent_id: int | None = None) -> list[dict[str, Any]]:
        """Proposals waiting for you: an agent's, or everyone's, oldest first."""
        found = self.engine.list_recommendations(status="pending")
        return [r for r in found if r["kind"] == KIND and agent_id in (None, self.agent(r))]

    def of_run(self, run_id: str) -> list[dict[str, Any]]:
        """What a run proposed, whatever became of it."""
        return [r for r in self.engine.list_recommendations(run_id=run_id) if r["kind"] == KIND]

    def show(self, rec: dict[str, Any]) -> dict[str, Any]:
        """A proposal, for the dashboard: what it changes, as a diff, and which tools it would let run unasked."""
        payload = rec["payload"]
        what = payload.get("what") or WORKFLOW
        return {
            "id": rec["id"],
            "agent": self.agent(rec),
            "run": rec.get("run_id"),
            "kind": what,
            "why": rec.get("rationale") or "",
            "source": payload["definition"],
            "diff": diff(payload["base"], payload["definition"]),
            "summary": describe_tools(payload.get("changes") or {}) if what == TOOLS else "",
            "unasked": self.unasked(payload["base"], payload["definition"]),
            "by": rec.get("proposed_by") or ("a run" if rec.get("source") == SOURCE else rec.get("source")),
            "state": rec["status"],
            "created": rec.get("created_at"),
        }

    def unasked(self, before: str, after: str) -> list[str]:
        """The tools that ask first which ``after`` would let run without asking, where ``before`` didn't, each with
        the tasks it would: ["send_email (in apply)"]. As agentic-airflow decides it, with the tools there are now."""
        try:
            was, will = self._unasked(before) if before.strip() else {}, self._unasked(after)
        except DagError:
            return []
        found: dict[str, list[str]] = {}
        for task, tools in will.items():
            for tool in sorted(tools - was.get(task, set())):
                found.setdefault(tool, []).append(task)
        return [f"{tool} (in {', '.join(tasks)})" for tool, tasks in sorted(found.items())]

    def _unasked(self, file: str) -> dict[str, set[str]]:
        dag = read(file, timezone=self.dags.timezone, hooks=self.dags.hooks)
        asks = {t.name for t in self.engine.tools if t.approval == "ask"}
        found = {}
        for task in dag.own_tasks():
            if task.is_agentic:
                config = resolve_config([self.engine.config, *dag.scopes_for(task)])
                found[task.id] = set(resolve_permissions(config, self.engine.tools).allowed) & asks
        return found

    # -- forgetting ------------------------------------------------------------------------------------

    def forget(self, agent_id: int | None = None) -> None:
        """Forget what's been proposed, for an agent or everyone: why each was proposed can say what was found."""
        for rec in self.engine.list_recommendations():
            if agent_id is None or self.agent(rec) == agent_id:
                self.engine.state.delete_recommendation(rec["id"])

    def prune(self) -> None:
        """Forget decided proposals whose runs aren't kept any more, as runs are forgotten (KEEP_RUNS)."""
        for rec in self.engine.list_recommendations():
            run_id = rec.get("run_id")
            if rec["status"] != "pending" and run_id and self.engine.state.get_run(run_id) is None:
                self.engine.state.delete_recommendation(rec["id"])


def same(a: str, b: str) -> bool:
    """Whether two DAG files say the same, whatever their spacing."""
    return " ".join(a.split()) == " ".join(b.split())


def diff(before: str, after: str) -> str:
    """What a change to a DAG file changes, as a unified diff."""
    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(), "dag.py (now)", "dag.py (proposed)", lineterm="", n=2
    )
    return "\n".join(lines)


def describe_tools(changes: dict[str, Any]) -> str:
    """The tool lists a proposal sets, in words."""
    parts = []
    if "hidden_tools" in changes:
        parts.append(f"Find when needed: {listed(changes['hidden_tools'])}")
    for where, names in (changes.get("allowed_tools") or {}).items():
        parts.append(f"Use without asking {'in every task' if where == 'dag' else f'in {where}'}: {listed(names)}")
    return "; ".join(parts)


def listed(names: Any) -> str:
    return "None" if names is None else ", ".join(names) or "[] (none)"
