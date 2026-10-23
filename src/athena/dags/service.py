"""Agents' DAGs, run by agentic-airflow's engine (docs/dags.md).

Each agent's DAG file is read (reader.py) and registered with the engine, with the tasks every DAG ends with
(ends.py), under the id its file gives it. The engine runs them: on their schedules, when a webhook or an asset
starts them, or when you ask. Runs wait their turn, first come first served, one at a time on each model; a task
holds the model until it's finished, and your messages go before the next (gate.py). The engine's state, runs
and approvals among it, is kept in dags.db, beside Athena's database.

Here is what's Athena's about them: the model and tools the engine works with (backend.py), asking you before a
tool that asks first runs, telling you what a task with on_complete=alert found and what a run's summary says, in
the agent's chat and, unless it's muted, in the master agent's, where Telegram talks to you; the webhooks
/hooks/<name> sends in (triggers.py) and Claude finishing a project; each run's number; and what the dashboard
and the model are shown.

It's also what the agent loop asks about each agent's conversation (agent.Directory): its model, what it starts
with, and what its DAG's doing now (intros.py). What's proposed for agents' DAGs is in proposals.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from agentic_airflow import (
    ApprovalError,
    Dag,
    DagRun,
    Engine,
    EngineConfig,
    Event,
    RunError,
    RunStatus,
    SqliteStateStore,
    TaskInstance,
    TriggerError,
)
from agentic_airflow import ToolRegistry as EngineTools

from athena.dags import ends, intros, runs
from athena.dags import files as agent_files
from athena.dags.backend import Backend, Doing, Toolbox, Working
from athena.dags.proposals import Proposals
from athena.dags.reader import (
    CLAUDE_DONE,
    END_GROUP,
    MASTER_STARTER,
    MAX_ITEMS,
    STARTER,
    DagError,
    as_dict,
    assets,
    read,
    unlisted,
    webhooks,
    with_tools,
)
from athena.dags.store import AgentStore
from athena.files import SENT
from athena.history import session_of
from athena.privacy import refusal, warning
from athena.tools import NEVER_UNASKED, Tool, current_turn

if TYPE_CHECKING:
    from athena.agent import Agent
    from athena.channel import Channel
    from athena.config import TasksConfig

log = logging.getLogger(__name__)

KEEP_RUNS = 10  # of each DAG's
LISTED_TOOLS = "dags-list-tools"  # the upgrade that gives each task of a DAG from before the tools it used
CONTEXT_CHARS = 12_000  # of a task's data, as the engine renders it: each upstream output, the run log
RETRY_SECONDS = 60.0  # before a failed task's tried again, unless its DAG says otherwise
EVENT_GAP = 60.0  # seconds: the same webhook again sooner doesn't start its DAGs again
DECLINED = "The user declined this action. Don't retry it unless they ask."


class Dags:
    def __init__(
        self,
        cfg: TasksConfig,
        store: AgentStore,
        agent: Agent,
        *,
        timezone: str,
        channel: Callable[[], Channel | None],
        state: Path,
        workspace: Path | None = None,
        on_ran: Callable[[], None] = lambda: None,
    ):
        self.cfg = cfg
        self.store = store
        self.agent = agent
        self.history = agent.history
        self.timezone = timezone
        try:
            self.tz: tzinfo = ZoneInfo(timezone)
        except Exception:  # unknown timezone name
            self.tz = UTC
        self.channel = channel
        self.workspace = workspace  # where agents' files are, if Athena has files (files.py)
        self.on_ran = on_ran  # e.g. so the model server reads your conversation again
        self.working = Working(agent, self._label)
        self.backend = Backend(agent, self.working, self.opening, cfg.thinking)
        tools = EngineTools()
        tools.add_provider(Toolbox(agent, self.working, self._source, self._noted))
        for tool in ends.review_tools(self):  # the end of each run's: proposing changes to its DAG
            tools.register(tool)
        self.engine = Engine(
            self.backend,
            config=EngineConfig(
                model=agent.llm.cfg.model,
                thinking="off" if cfg.thinking == "off" else "on",
                max_tool_steps=cfg.max_tool_rounds,
                task_timeout=cfg.max_minutes * 60,
                retries=cfg.retries,
                retry_delay=RETRY_SECONDS,
                # Athena asks for less: this is a backstop.
                approval_timeout=max(cfg.confirm_minutes, cfg.ask_minutes) * 60 + 300,
                max_mapped=MAX_ITEMS,
                keep_runs=KEEP_RUNS,
                max_context_chars=CONTEXT_CHARS,
                persist_every_step=False,  # a task that's interrupted starts again: the Pi's card is spared
                cpu_workers=2,
            ),
            state=SqliteStateStore(state),
            tools=tools,
            queues=agent.gate.queues,  # your messages and its tasks take turns with the model (gate.py)
        )
        self.engine.subscribe(self._event)
        self.proposals = Proposals(self)  # what's proposed for agents' DAGs, in the engine's queue
        self.hooks = {"alert": self._alerted}  # what a DAG's on_complete can name (reader.HOOKS)
        self._dags: dict[int, Dag] = {}  # each agent's DAG, as registered
        self._errors: dict[int, DagError] = {}  # why an agent's file won't do, if it won't
        self._notes: dict[str, ends.RunNotes] = {}  # by run
        self._openings: dict[str, str] = {}  # what each run's requests start with, by run
        self._alerts: dict[tuple[str, str], list[str]] = {}  # what tasks with alert found, by (run, task)
        self._asking: set[str] = set()  # approvals you're being asked for
        self._fired: dict[str, float] = {}  # webhook -> when it last started DAGs
        self._background: set[asyncio.Task[Any]] = set()

    @property
    def user(self) -> str:
        return self.agent.cfg.user_name

    @property
    def master(self) -> int:
        """The master agent: where Telegram, Siri and the terminal talk to you, and news from the others is told."""
        return self.history.master.id

    # -- starting and stopping -------------------------------------------------------------------------

    async def start(self) -> None:
        """Register each agent's DAG, start the engine, carry on with runs Athena stopped during, and ask again
        about tools that were waiting for your OK."""
        if self.store.once(LISTED_TOOLS):
            self._list_tools()
        for config in self.store.all():
            if config.dag.strip():
                try:
                    self._register(config.id, config.dag)
                except DagError as exc:
                    self._errors[config.id] = exc
                    log.warning("Agent %d's DAG won't do, so it won't run: %s", config.id, exc)
        self.proposals.carry_over(self.store.take_old_proposals())
        await self.engine.start()
        resumed = await self.engine.resume_interrupted()
        if resumed:
            log.info(
                "Carrying on with %d DAG run%s Athena stopped during", len(resumed), "" if len(resumed) == 1 else "s"
            )
        for approval in self.engine.list_approvals():
            self._spawn(self._ask(approval.to_dict()))

    def _list_tools(self) -> None:
        """Once, for DAG files written before a task was given only the tools a list names: each task no list
        covers is given the tools it used in its kept runs that don't ask first, so it carries on as it did. Those
        that ask, it can find when it needs one, and they ask."""
        asks = {t.name for t in self.agent.tools.all() if t.needs_confirmation}
        for config in self.store.all():
            try:
                tasks = unlisted(config.dag) if config.dag.strip() else []
                if not tasks:
                    continue
                dag_id = read(config.dag, timezone=self.timezone, hooks=self.hooks).id
                used: dict[str, list[str]] = {task: [] for task in tasks}
                for run in self.engine.list_runs(dag_id, limit=KEEP_RUNS):
                    for inst in run.instances.values():
                        for call in inst.tool_calls if inst.task_id in used else []:
                            name = str(call.get("name"))
                            if call.get("status") == "ok" and name not in asks and name not in used[inst.task_id]:
                                used[inst.task_id].append(name)
                file = with_tools(config.dag, {"allowed_tools": {"dag": [], **{t: sorted(n) for t, n in used.items()}}})
                self.check(config.id, file)
            except (DagError, SyntaxError) as exc:
                log.warning("Couldn't list the tools of agent %d's DAG: %s", config.id, exc)
                continue
            self.store.update(config.id, dag=file)
            log.info("Listed the tools each task of agent %d's DAG uses: %s", config.id, used)

    async def stop(self) -> None:
        for task in list(self._background):
            task.cancel()
        await self.engine.stop()  # runs going carry on when Athena starts again
        self.engine.close()
        self.engine.state.close()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- agents' DAGs --------------------------------------------------------------------------------

    def dag(self, agent_id: int) -> Dag | None:
        """An agent's DAG, as registered: None if it hasn't one, or its file won't do."""
        return self._dags.get(agent_id)

    def agent_of(self, run: DagRun) -> int:
        agent_id = run.trigger.get("agent")
        return agent_id if isinstance(agent_id, int) else self.owner(run.dag_id) or self.master

    def owner(self, dag_id: str) -> int | None:
        return next((agent_id for agent_id, dag in self._dags.items() if dag.id == dag_id), None)

    def check(self, agent_id: int, source: str) -> Dag:
        """The DAG a file describes, as it would be this agent's. DagError if it won't do."""
        dag = read(source, timezone=self.timezone, hooks=self.hooks)
        owner = self.owner(dag.id)
        if owner not in (None, agent_id):
            name = self.history.title(owner) or f"Agent {owner}"
            raise DagError(f"“{name}”'s DAG is called {dag.id!r} already: give this one another id.", 1)
        return dag

    def _register(self, agent_id: int, source: str) -> Dag:
        dag = self.check(agent_id, source)
        if dag.scope.model is None:  # the agent's model, as its chat uses
            dag.scope.model = self.store.get(agent_id).model or self.agent.llm.cfg.model
        before = self._dags.get(agent_id)
        own = [t.id for t in dag.tasks.values()]
        group = ends.group(source, own, self) if dag.auto_evaluate else None
        self.engine.register(dag, evaluator=group, replace=True)
        if before is not None and before.id != dag.id:  # renamed: its runs and pausing go with it
            if self.engine.is_paused(before.id):
                self.engine.pause(dag.id)
            self._forget_dag(before.id)
        self._dags[agent_id] = dag
        self._errors.pop(agent_id, None)
        return dag

    def _forget_dag(self, dag_id: str) -> None:
        with contextlib.suppress(KeyError, RunError):
            self.engine.unpause(dag_id)
        self.engine.unregister(dag_id)
        for run in self.engine.list_runs(dag_id, limit=1000):
            if run.is_terminal:
                self.engine.state.delete_run(run.run_id)

    def save(self, agent_id: int, source: str) -> Dag:
        """Make ``source`` an agent's DAG. DagError if it won't do."""
        dag = self._register(agent_id, source)
        self.store.update(agent_id, dag=source)
        return dag

    def changed(self, agent_id: int) -> None:
        """An agent's settings changed, such as its model: its DAG's registered again, to use them."""
        source = self.store.get(agent_id).dag
        if agent_id in self._dags and source.strip():
            with contextlib.suppress(DagError):
                self._register(agent_id, source)

    def active(self, agent_id: int) -> list[DagRun]:
        dag = self._dags.get(agent_id)
        return [r for r in self.engine.runs.values() if dag is not None and r.dag_id == dag.id and not r.is_terminal]

    async def run_now(self, agent_id: int) -> DagRun:
        """Run an agent's DAG as soon as it's its turn. ValueError if it can't run."""
        if not self.cfg.enabled:
            raise ValueError("Agents' DAGs are switched off ([tasks] enabled = false in config.toml).")
        dag = self._dags.get(agent_id)
        if dag is None:
            if agent_id in self._errors:
                raise ValueError("Its DAG file has a problem: fix it first.")
            raise ValueError("This agent has no DAG yet: write one first.")
        if any(r.status == RunStatus.PENDING and r.trigger.get("source") == "manual" for r in self.active(agent_id)):
            raise ValueError("It's already waiting to run.")
        return await self.engine.trigger(dag.id, source="manual")

    def pause(self, agent_id: int) -> None:
        if dag := self._dags.get(agent_id):
            self.engine.pause(dag.id)

    def resume(self, agent_id: int) -> None:
        if dag := self._dags.get(agent_id):
            self.engine.unpause(dag.id)

    def paused(self, agent_id: int) -> bool:
        dag = self._dags.get(agent_id)
        return dag is not None and self.engine.is_paused(dag.id)

    async def stop_running(self, agent_id: int | None = None) -> bool:
        """Stop the runs of ``agent_id``'s DAG that are going or waiting, or everyone's. False if none were."""
        stopped = False
        for agent in [agent_id] if agent_id is not None else list(self._dags):
            for run in self.active(agent):  # type: ignore[arg-type]
                await self.engine.cancel(run.run_id)
                stopped = True
        return stopped

    async def delete(self, agent_id: int) -> None:
        """Forget an agent's DAG, its runs, what they proposed and its files, as when the agent's deleted: stopping
        a run of it first, if one's going."""
        await self.stop_running(agent_id)
        self.proposals.forget(agent_id)
        if dag := self._dags.pop(agent_id, None):
            self._forget_dag(dag.id)
        for run in self.engine.list_runs(limit=10_000):  # those of a DAG it had before, too
            if run.is_terminal and run.trigger.get("agent") == agent_id:
                self.engine.state.delete_run(run.run_id)
        self._errors.pop(agent_id, None)
        self.store.delete(agent_id)
        agent_files.forget(self.workspace, agent_id)

    def forget(self) -> None:
        """Forget what runs found and proposed, as forgetting everything does. The agents and their DAGs stay."""
        for run in self.engine.list_runs(limit=10_000):
            if run.is_terminal:
                self.engine.state.delete_run(run.run_id)
        self.proposals.forget()
        self.store.forget()

    # -- what starts them --------------------------------------------------------------------------

    async def fire(self, name: str, text: str = "", items: list[str] | None = None) -> list[str]:
        """A webhook came in (triggers.py): start the DAGs with Webhook(name) in their schedule, unless it started
        them in the last EVENT_GAP seconds. Returns the names of the agents whose DAGs it started."""
        now = time.monotonic()
        if now - self._fired.get(name, float("-inf")) < EVENT_GAP:
            return []
        payload: Any = {"text": text, "items": list(items)} if items else text or None
        try:
            started = await self.engine.deliver_webhook(name, payload)
        except TriggerError:
            return []
        if started:
            self._fired[name] = now
        return [self._name(self.agent_of(run)) for run in started]

    async def claude_done(self, said: str) -> None:
        """Claude finished a project: the DAGs with Asset("claude-done") in their schedule run, with what it said."""
        await self.engine.update_asset(CLAUDE_DONE, said, extra={"from": "Claude"})

    def webhooks(self) -> dict[str, list[str]]:
        """The webhooks DAGs that aren't paused start on, with the names of their agents."""
        return self._starts(webhooks)

    def assets(self) -> dict[str, list[str]]:
        """The assets DAGs that aren't paused start on, with the names of their agents."""
        return self._starts(assets)

    def _starts(self, names: Callable[[Dag], list[str]]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for agent_id, dag in self._dags.items():
            if not self.engine.is_paused(dag.id):
                for name in names(dag):
                    found.setdefault(name, []).append(self._name(agent_id))
        return found

    def sources(self) -> dict[str, list[dict[str, Any]]]:
        """The assets DAGs' tasks update, with where each comes from: {asset: [{"agent", "name", "task"}]}."""
        found: dict[str, list[dict[str, Any]]] = {}
        for agent_id, dag in self._dags.items():
            for task in dag.own_tasks():
                for name in task.outlets:
                    found.setdefault(name, []).append(
                        {"agent": agent_id, "name": self._name(agent_id), "task": task.id}
                    )
        return found

    # -- what the engine says ----------------------------------------------------------------------------

    def _event(self, event: Event) -> None:
        """What happens in runs, as the engine tells it."""
        kind = event.type
        if kind == "run.created" and event.run_id in self.engine.runs:
            run = self.engine.runs[event.run_id]
            agent_id = self.agent_of(run)
            run.trigger.setdefault("agent", agent_id)  # whose it is, whatever its DAG's called later
            run.trigger.setdefault("number", self.store.next_number(agent_id))
        elif kind in (
            "task.finished",
            "task.failed",
            "task.retrying",
            "task.awaiting_tool_approval",
            "task.awaiting_user",
        ):
            self._stopped(event)
        elif kind == "approval.requested":
            self._spawn(self._ask(event.payload))
        elif kind == "node.finished" and event.run_id and event.task_id:
            if texts := self._alerts.pop((event.run_id, event.task_id), None):
                self._spawn(self._alert(event.run_id, event.task_id, texts))
        elif kind == "run.finished" and event.run_id:
            self._spawn(self._finished(event.run_id))

    def _stopped(self, event: Event) -> None:
        error = str((event.payload or {}).get("error") or "") if event.type in ("task.failed", "task.retrying") else ""
        self.working.finish(event.run_id or "", event.instance_id or "", error)

    def _label(self, run_id: str, instance_id: str) -> tuple[int, str, str]:
        """For Working: whose a task is, what the status board says while it runs, and what a message of yours
        waiting for it is told."""
        run = self.engine.runs.get(run_id)
        agent_id = self.agent_of(run) if run else self.master
        name = self._name(agent_id)
        return agent_id, f"{name}: {instance_id}", f"“{instance_id}” in {name}"

    def _source(self, run_id: str) -> str:
        """What a run's doing, as what its tools save says it came from."""
        run = self.engine.runs.get(run_id)
        return f"the DAG of your agent “{self._name(self.agent_of(run))}”" if run else "a DAG"

    def notes(self, run: DagRun) -> ends.RunNotes:
        return self._notes.setdefault(run.run_id, ends.RunNotes())

    async def _noted(self, run_id: str, doing: Doing, notes: dict[str, list[str]]) -> None:
        """What a task's tool noted: files to send you, skills it read and what it suggested."""
        run = self.engine.runs.get(run_id)
        if run is None:
            return
        kept = self.notes(run)
        kept.skills += [s for s in notes.get("skills", []) if s not in kept.skills]
        kept.suggested += notes.get("suggested", [])
        channel = self.channel()
        for file in notes.get(SENT, []) if channel else []:
            try:
                await channel.send_file(file)  # type: ignore[union-attr]
            except Exception as exc:
                log.warning("Couldn't send a file a DAG task sent: %s", exc)

    def tool_names(self) -> tuple[list[str], list[str], list[str]]:
        """For the tool review: every tool, those that ask first, and those that always do."""
        tools = self.agent.tools.all()
        asks = [t.name for t in tools if t.needs_confirmation and t.name not in NEVER_UNASKED]
        return [t.name for t in tools], asks, [t.name for t in tools if t.name in NEVER_UNASKED]

    # -- asking you -----------------------------------------------------------------------------------

    async def _ask(self, approval: dict[str, Any]) -> None:
        """A task wants a tool that asks first: ask you, in Telegram and the agent's chat, and tell the engine."""
        approval_id = approval["approval_id"]
        if approval_id in self._asking:
            return
        self._asking.add(approval_id)
        try:
            if approval.get("kind") == "question":
                await self._question(approval)
                return
            if approval.get("kind") != "tool_call":  # an evaluator that ran out of tries: what it has will do
                await self.engine.decide(approval_id, "pass")
                return
            call = approval["payload"]["tool_call"]
            name, args = call["name"], call.get("arguments") or {}
            said = await self._preview(name, args)
            if isinstance(said, Exception):
                await self.engine.reject(approval_id, reason=f"Error: {said}")
                return
            run = self.engine.get_run(approval["run_id"])
            agent_id = self.agent_of(run)
            channel = self.channel()
            allowed = channel is not None and await channel.ask(
                name,
                args,
                said,
                why=f"For “{approval['instance_id']}”, in {self._name(agent_id)}",
                timeout=self.cfg.confirm_minutes * 60,
                session=agent_id,
            )
            if allowed:
                await self.engine.approve(approval_id)
            else:
                await self.engine.reject(approval_id, reason=DECLINED)
        except (ApprovalError, RunError):
            pass  # it was answered, or its run stopped, meanwhile
        except Exception:
            log.exception("Couldn't ask about a DAG task's tool")
        finally:
            self._asking.discard(approval_id)

    async def _question(self, approval: dict[str, Any]) -> None:
        """A task with ask_user asks you something: in Telegram and the agent's chat. Unanswered in ask_minutes, it
        decides for itself."""
        payload = approval["payload"]
        run = self.engine.get_run(approval["run_id"])
        agent_id = self.agent_of(run)
        channel = self.channel()
        answer = channel is not None and await channel.question(
            str(payload.get("question") or ""),
            list(payload.get("choices") or []),
            why=f"“{approval['instance_id']}”, in {self._name(agent_id)}, asks",
            timeout=self.cfg.ask_minutes * 60,
            session=agent_id,
        )
        if answer:
            await self.engine.answer(approval["approval_id"], answer)
        else:
            minutes = f"{self.cfg.ask_minutes:g} minutes"
            await self.engine.decide(
                approval["approval_id"], "dismiss", reason=f"{self.user} didn't answer within {minutes}."
            )

    async def _preview(self, name: str, args: dict[str, Any]) -> str | None | Exception:
        """What a tool call will do, for the request for your OK, with what the privacy check found in it; or why it
        mustn't run at all."""
        tool: Tool | None = self.agent.tools.get(name)
        verdict = self.agent.privacy.check(name, args, asks=True) if self.agent.privacy else None
        if verdict and verdict.refused:
            return ValueError(refusal(verdict))
        summary = None
        if tool is not None and tool.preview:
            try:
                summary = await asyncio.wait_for(tool.preview(args), self.agent.cfg.tool_timeout_seconds)
            except Exception as exc:  # nothing's been done, and you haven't been asked
                return exc
        if verdict and verdict.findings:
            summary = warning(verdict) + (f"\n{summary}" if summary else "")
        return summary

    # -- telling you ----------------------------------------------------------------------------------

    def _alerted(self, engine: Engine, run: DagRun, inst: TaskInstance) -> None:
        """on_complete=alert: keep what it found, to tell you once the task's done for every item."""
        self._alerts.setdefault((run.run_id, inst.task_id), []).append(runs.text_of(inst.output))

    async def _alert(self, run_id: str, task_id: str, texts: list[str]) -> None:
        text = texts[0] if len(texts) == 1 else "\n\n".join(f"**{n}.** {t}" for n, t in enumerate(texts, 1))
        await self.tell(self.engine.get_run(run_id), f"task “{task_id}”", text)

    async def told(self, run: DagRun, text: str, quiet: bool) -> None:
        """The summary's written (ends.py)."""
        await self.tell(run, "summary", text, quiet)

    async def tell(self, run: DagRun, what: str, text: str, quiet: bool = False) -> None:
        """Put what a run said in the agent's chat, and, unless it's ``quiet`` or you've muted the agent, in the
        master agent's chat, where Telegram talks to you, and tell you, in Telegram and on your devices."""
        agent_id = self.agent_of(run)
        number = runs.number(run)
        self.history.append_exchange(f"session:{agent_id}", f"(DAG run #{number}: {what})", text, channel="Task")
        session = self.history.get_session(agent_id)
        if quiet or session is None or session.muted:
            return
        name = self._name(agent_id)
        if agent_id != self.master:  # so you can ask about it there
            told = f"(From your agent “{name}”, its DAG run #{number}: {what})"
            self.history.append_exchange(f"session:{self.master}", told, text, channel="Task")
        channel = self.channel()
        if channel is None:
            return
        said = text if agent_id == self.master else f"**{name}:** {text}"  # as a message, not a report
        try:
            await channel.tell(said, session=agent_id)
        except Exception as exc:
            log.warning("Couldn't tell you what a DAG run found: %s", exc)

    async def _finished(self, run_id: str) -> None:
        """A run's finished: tell you if it couldn't reach the model, and forget what it noted on the way."""
        try:
            run = self.engine.get_run(run_id)
        except RunError:
            return
        if run_id in self.backend.unreachable:
            said = "This run stopped early: the model server couldn't be reached, even after trying again."
            await self.tell(run, "summary", said, quiet=True)  # alerts tell you the server's down (alerts.py)
        elif run.summary is None and (summary := run.instances.get(f"{END_GROUP}.summary")) is not None:
            if summary.status.value in ("failed", "upstream_failed"):  # it was meant to be written, and wasn't
                recommended = self.notes(run).recommendations
                said = "The run finished, but its summary couldn't be written." + (
                    f" It recommends: {'; '.join(recommended)}" if recommended else ""
                )
                run.summary = said
                self.engine.state.save_run(run.to_dict())
                await self.tell(run, "summary", said)
        for key in [k for k in self._alerts if k[0] == run_id]:
            self._alerts.pop(key)
        self._notes.pop(run_id, None)
        self._openings.pop(run_id, None)
        self.backend.forget(run_id)
        self.working.finish_run(run_id)
        self.proposals.prune()
        self.on_ran()

    # -- each agent's conversation (agent.Directory) ---------------------------------------------------

    def model(self, chat_id: str) -> str | None:
        agent_id = session_of(chat_id)
        return (self.store.get(agent_id).model or None) if agent_id is not None else None

    def intro(self, chat_id: str) -> str:
        agent_id = session_of(chat_id)
        return self.intro_for(agent_id) if agent_id is not None else ""

    def note(self, chat_id: str) -> str:
        """What the agent's DAG is doing now, if it's running, for a message of yours meanwhile."""
        agent_id = session_of(chat_id)
        return intros.note(self, agent_id) if agent_id is not None else ""

    def intro_for(self, agent_id: int) -> str:
        """What an agent's conversations start with, its chat's and its runs' (intros.py)."""
        return intros.intro_for(self, agent_id)

    def agents(self) -> list[tuple[int, str]]:
        """Every agent, by id: (id, name), the master agent first."""
        listed = self.history.sessions(limit=500, also={c.id for c in self.store.all()})
        found = sorted((s.id, s.title or f"Agent {s.id}") for s in listed)
        return sorted(found, key=lambda a: a[0] != self.master)

    def find(self, which: str) -> int | None:
        """An agent, by its name or #id: None if there's no such agent, or the name could be more than one."""
        which = which.strip()
        if which.lstrip("#").isdigit():
            agent_id = int(which.lstrip("#"))
            return agent_id if self.history.get_session(agent_id) else None
        named = self.agents()
        exact = [agent_id for agent_id, name in named if name.casefold() == which.casefold()]
        partly = [agent_id for agent_id, name in named if which.casefold() in name.casefold()]
        found = exact or partly
        return found[0] if len(found) == 1 else None

    def agent_here(self) -> int:
        """The agent of the conversation a tool's called in: the master agent, if it's not an agent's."""
        chat = current_turn().chat
        agent_id = session_of(chat) if chat else None
        return agent_id if agent_id is not None else self.master

    def _name(self, agent_id: int) -> str:
        return self.history.title(agent_id) or f"Agent {agent_id}"

    def opening(self, run_id: str) -> str:
        """What each request of a run starts with, after Athena's system prompt (intros.py). The same for all of a
        run's tasks, so the model server's cache of it holds."""
        if run_id not in self._openings and (run := self.engine.runs.get(run_id)) is not None:
            self._openings[run_id] = intros.opening(self, run)
        return self._openings.get(run_id, "")

    # -- the dashboard -----------------------------------------------------------------------------------

    def runs(self, agent_id: int) -> list[DagRun]:
        """An agent's last runs, newest first."""
        dag = self._dags.get(agent_id)
        return self.engine.list_runs(dag.id, limit=KEEP_RUNS) if dag else []

    def run(self, run_id: str) -> DagRun | None:
        try:
            return self.engine.get_run(run_id)
        except RunError:
            return None

    def describe(self, agent_id: int) -> dict[str, Any]:
        """An agent's DAG, for the dashboard: its file and tasks, or what's wrong with it, when it runs, whether
        it's running, and what's waiting for you."""
        config = self.store.get(agent_id)
        master = agent_id == self.master
        source = config.dag or self.starter(agent_id)
        try:
            dag, error = self.check(agent_id, source), None
        except DagError as exc:
            dag, error = None, exc.as_dict()
        registered = self._dags.get(agent_id)
        active = self.active(agent_id)
        going = next((r for r in active if r.status != RunStatus.PENDING), None)
        fires = self.engine.next_cron_fires().get(registered.id, []) if registered else []
        last = self.runs(agent_id)[:1]
        return {
            "source": source,
            "written": bool(config.dag),
            "master": master,
            "dag": as_dict(dag) if dag else None,
            "error": error,
            "paused": self.paused(agent_id),
            "next_run": datetime.fromisoformat(min(fires)).timestamp() if fires and not self.paused(agent_id) else None,
            "running": {"run": going.run_id, "number": runs.number(going), "doing": self.doing(going)}
            if going
            else None,
            "queued": [r.trigger.get("source") for r in active if r.status == RunStatus.PENDING],
            "last": runs.brief(last[0]) if last else None,
            "proposals": [self.proposals.show(p) for p in self.proposals.waiting(agent_id)],
        }

    @staticmethod
    def doing(run: DagRun) -> str:
        doing = [i.instance_id for i in run.instances.values() if i.status.value in ("running", "queued")]
        waiting = [i.instance_id for i in run.instances.values() if i.is_waiting]
        if doing:
            return ", ".join(f"“{i}”" for i in doing)
        return f"waiting for your OK for “{waiting[0]}”" if waiting else ""

    def starter(self, agent_id: int) -> str:
        """What a new agent's DAG file starts as."""
        if agent_id == self.master:
            return MASTER_STARTER
        name = self.history.title(agent_id) or f"agent {agent_id}"
        dag_id = "_".join("".join(c if c.isalnum() else " " for c in name.lower()).split())[:40] or f"agent_{agent_id}"
        dag_id = dag_id if dag_id[0].isalpha() else f"agent_{dag_id}"
        while self.owner(dag_id) not in (None, agent_id):
            dag_id += "_2"
        return STARTER.format(dag_id=dag_id, purpose=f"What {name} is for.".replace('"', "'"))

    # -- the model's tools ---------------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        from athena.dags.tools import dag_tools

        return dag_tools(self)
