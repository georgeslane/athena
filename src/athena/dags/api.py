"""The dashboard's API for agents, their DAGs and what's watched (dashboard.py has the rest, and lists it all).

A mixin of DashboardServer's: each handler is given the request, its method and the parts of its path, as
dashboard.ROUTES says.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from athena.dags.spec import DagError, parse, unknown_tools
from athena.history import Busy
from athena.watching import end_of
from athena.webserver import HTTPError, Request, Response
from athena.webserver import json_body as _body
from athena.webserver import json_response as _json

if TYPE_CHECKING:
    from athena.app import Services

log = logging.getLogger(__name__)

MODELS_SECONDS = 60.0  # how long the model server's list of models is taken to stay the same


class AgentsAPI:
    s: Services
    _models: tuple[float, list[str]] | None = None

    # -- agents, and talking to them -------------------------------------------------------------------

    def _get_agents(self, request: Request, method: str) -> Response:
        return _json(self.s.chat.state())

    def _new_agent(self, request: Request, method: str) -> Response:
        """{"name"}: a new agent, as the main one."""
        try:
            session = self.s.chat.new_session(_body(request).get("name") or "")
        except Busy as exc:
            raise HTTPError(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        return _json({**self.s.chat.state(), "agent": session.id})

    async def _stop_chat(self, request: Request, method: str) -> Response:
        return _json({"stopped": await self.s.chat.stop()})

    def _get_agent(self, request: Request, method: str, agent: str) -> Response:
        """An agent's messages, the last page or ?before=<message id>, and its settings."""
        before = request.query.get("before")
        if before is not None and not before.isdigit():
            raise HTTPError(400, "Ask with ?before=<a message's id>.")
        return self._agent(agent, lambda: self.s.chat.session(int(agent), int(before) if before else None))

    def _change_agent(self, request: Request, method: str, agent: str) -> Response:
        """{"name", "model", "muted", "guidance", "context"}, any of them: change an agent."""
        body = _body(request)
        known = {"name", "model", "muted", "guidance", "context"}
        if not body or set(body) - known:
            raise HTTPError(400, f"Send any of {', '.join(sorted(known))}.")

        def change() -> dict[str, Any]:
            agent_id = int(agent)
            if self.s.history.get_session(agent_id) is None:
                raise KeyError(agent_id)
            if "name" in body:
                self.s.chat.rename(agent_id, body["name"])
            if "muted" in body:
                self.s.chat.mute(agent_id, body["muted"])
            settings = {k: body[k] for k in ("model", "guidance", "context") if k in body}
            for key, value in settings.items():
                if not isinstance(value, str):
                    raise ValueError(f"Send {key} as text.")
            if settings:
                self.s.dags.store.update(agent_id, **{k: v.strip() for k, v in settings.items()})
                self.s.rewarm.set()  # what the agent's conversation starts with has changed
            return self.s.chat.session(agent_id)

        return self._agent(agent, change)

    async def _delete_agent(self, request: Request, method: str, agent: str) -> Response:
        try:
            await self.s.chat.delete(int(agent))
        except Busy as exc:
            raise HTTPError(409, str(exc)) from None
        except KeyError:
            raise HTTPError(404, "There's no such agent.") from None
        return _json(self.s.chat.state())

    async def _send(self, request: Request, method: str, agent: str) -> Response:
        """{"text", "allow": [tools allowed without asking for this message], "attachments": [photos and files]}: a
        command says what it did."""
        body = _body(request)
        try:
            said = await self.s.chat.send(
                int(agent), body.get("text"), body.get("allow") or [], body.get("attachments") or []
            )
        except Busy as exc:
            raise HTTPError(409, str(exc)) from None
        except KeyError:
            raise HTTPError(404, "There's no such agent.") from None
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        return _json({**self.s.chat.state(), "said": said})

    def _allow_in_session(self, request: Request, method: str, agent: str) -> Response:
        """{"allowed": [tools]}: what may run without asking when you're talking to an agent."""

        def allow() -> dict[str, Any]:
            self.s.chat.allow_in_session(int(agent), _body(request).get("allowed"))
            return self.s.chat.session(int(agent))

        return self._agent(agent, allow)

    def _agent(self, agent: str, do: Any) -> Response:
        try:
            return _json(do())
        except KeyError:
            raise HTTPError(404, "There's no such agent.") from None
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None

    async def _get_models(self, request: Request, method: str) -> Response:
        """The models the model server has, for an agent to use, and the default."""
        llm = self.s.config.llm
        if self._models is None or time.monotonic() - self._models[0] > MODELS_SECONDS:
            try:
                found = await self.s.llm.list_models()
            except Exception as exc:  # it's asleep: the default's all that's known
                log.info("Couldn't list the model server's models: %s", exc)
                found = []
            embedding = self.s.config.embeddings.model
            self._models = (time.monotonic(), sorted(m for m in found if m != embedding))
        models = self._models[1]
        return _json({"models": models if llm.model in models else [llm.model, *models], "default": llm.model})

    # -- DAGs ------------------------------------------------------------------------------------------

    def _known(self, agent: str) -> int:
        agent_id = int(agent)
        if self.s.history.get_session(agent_id) is None:
            raise HTTPError(404, "There's no such agent.")
        return agent_id

    def _get_dag(self, request: Request, method: str, agent: str) -> Response:
        return _json(self.dag(self._known(agent)))

    def dag(self, agent_id: int) -> dict[str, Any]:
        """An agent's DAG, and what it can be triggered by, for the DAG tab."""
        dags = self.s.dags
        watches = dags.watches.all() if dags.watches else []
        return {
            **dags.describe(agent_id),
            "triggers": {
                "events": sorted({*dags.waiting_for(), "claude-done"}),
                "watches": [{"key": w.key, "what": w.what} for w in watches],
                "hooks": self.hooks.on,  # type: ignore[attr-defined]
            },
            "tools": sorted(t.name for t in self.s.tools.all()),
            "enabled": self.s.config.tasks.enabled,
        }

    def _check_dag(self, request: Request, method: str, agent: str) -> Response:
        """{"source"}: whether a DAG file would do, and its tasks if it would, as it's being written."""
        self._known(agent)
        source = _source(request)
        try:
            spec = parse(source)
        except DagError as exc:
            return _json({"dag": None, "error": exc.as_dict(), "unknown": []})
        known = [t.name for t in self.s.tools.all()]
        return _json({"dag": spec.as_dict(), "error": None, "unknown": unknown_tools(spec, known)})

    def _save_dag(self, request: Request, method: str, agent: str) -> Response:
        agent_id = self._known(agent)
        try:
            self.s.dags.save(agent_id, _source(request))
        except DagError as exc:
            raise HTTPError(400, str(exc)) from None
        self.s.rewarm.set()  # the agent's conversation starts with its DAG
        log.info("Dashboard: changed agent %d's DAG", agent_id)
        return _json(self.dag(agent_id))

    def _dag_action(self, request: Request, method: str, agent: str, action: str) -> Response:
        agent_id, dags = self._known(agent), self.s.dags
        try:
            if action == "run":
                dags.run_now(agent_id)
            elif action == "pause":
                dags.pause(agent_id)
            elif action == "resume":
                dags.resume(agent_id)
            elif not dags.stop_running(agent_id):
                raise ValueError("It isn't running.")
        except ValueError as exc:
            raise HTTPError(409, str(exc)) from None
        return _json(self.dag(agent_id))

    def _get_runs(self, request: Request, method: str, agent: str) -> Response:
        """The agent's last runs, newest first."""
        agent_id = self._known(agent)
        return _json({"runs": [run.as_dict(tasks=False) for run in self.s.dags.store.runs(agent_id)]})

    def _get_run(self, request: Request, method: str, run: str) -> Response:
        """A run: what each of its tasks was given, said and did, and what it proposed."""
        dags = self.s.dags
        found = dags.store.run(int(run))
        if found is None:
            raise HTTPError(404, "That run isn't kept any more: only each agent's last 10 are.")
        current = dags.current
        spec = dags.spec(found.source)
        return _json(
            {
                **found.as_dict(),
                "dag": spec.as_dict() if spec else None,  # as it was for the run
                "doing": current.doing if current and current.run.id == found.id else "",
                "proposals": [dags.show(p) for p in dags.store.proposals(run_id=found.id)],
            }
        )

    def _decide(self, request: Request, method: str, proposal: str) -> Response:
        """{"approve": true or false}: what to do with a run's proposal."""
        approve = _body(request).get("approve")
        if not isinstance(approve, bool):
            raise HTTPError(400, 'Send {"approve": true} or {"approve": false}.')
        try:
            decided = self.s.dags.decide(int(proposal), approve)
        except ValueError as exc:
            raise HTTPError(409, str(exc)) from None
        if approve:
            self.s.rewarm.set()
        return _json(self.dag(decided.agent))

    # -- what's watched --------------------------------------------------------------------------------

    def _get_watches(self, request: Request, method: str) -> Response:
        return _json(self.s.dags.watching())

    def _add_watch(self, request: Request, method: str) -> Response:
        dags, body = self._watching(), _body(request)
        try:
            dags.watches.add(str(body.get("what") or ""), self._until(body), by="you", key=_key(body))  # type: ignore[union-attr]
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        dags.wake()
        return _json(dags.watching())

    def _change_watch(self, request: Request, method: str, which: str) -> Response:
        dags, body = self._watching(), _body(request)
        assert dags.watches is not None
        if dags.watches.get(int(which)) is None:
            raise HTTPError(404, f"Nothing's watched as #{which}.")
        try:
            dags.watches.change(int(which), str(body.get("what") or ""), self._until(body), key=_key(body))
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        return _json(dags.watching())

    def _check_watches(self, request: Request, method: str) -> Response:
        dags = self._watching()
        dags.check_now()
        return _json(dags.watching())

    def _stop_watching(self, request: Request, method: str, which: str) -> Response:
        dags = self._watching()
        assert dags.watches is not None
        try:
            dags.watches.remove(int(which))
        except ValueError as exc:
            raise HTTPError(404, str(exc)) from None
        return _json(dags.watching())

    def _watching(self) -> Any:
        if self.s.dags.watches is None:
            raise HTTPError(409, "Watching is switched off.")
        return self.s.dags

    def _until(self, body: dict[str, Any]) -> float | None:
        """When something's watched until, from {"until": "YYYY-MM-DD"}, the end of that day; null for no end."""
        until = body.get("until")
        if until is None:
            return None
        if not isinstance(until, str):
            raise ValueError("Give the day as YYYY-MM-DD, or null for no end.")
        return end_of(until, self.s.dags.tz)


def _source(request: Request) -> str:
    source = _body(request).get("source")
    if not isinstance(source, str):
        raise HTTPError(400, 'Send the DAG file: {"source": "..."}.')
    return source


def _key(body: dict[str, Any]) -> str:
    key = body.get("key") or ""
    if not isinstance(key, str):
        raise HTTPError(400, "Send the key as text, like job-emails.")
    return key
