"""The dashboard's API for agents, their DAGs and their files (dashboard.py has the rest, and lists it all).

A mixin of DashboardServer's: each handler is given the request, its method and the parts of its path, as
dashboard.ROUTES says.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athena import files, workspace
from athena.dags import files as agent_files
from athena.dags import runs
from athena.dags.reader import CLAUDE_DONE, DagError, as_dict, unknown_tools
from athena.history import Busy
from athena.tools import ToolError
from athena.webserver import HTTPError, Request, Response
from athena.webserver import json_body as _body
from athena.webserver import json_response as _json

if TYPE_CHECKING:
    from athena.app import Services

log = logging.getLogger(__name__)

MODELS_SECONDS = 60.0  # how long the model server's list of models is taken to stay the same
MAX_FILE_MB = 20  # each of an agent's files, as a file sent in a chat
TEXT_TYPES = {".md": "text/markdown", ".csv": "text/csv", ".json": "application/json", ".html": "text/plain"}


class AgentsAPI:
    s: Services
    _models: tuple[float, list[str]] | None = None

    # -- agents, and talking to them -------------------------------------------------------------------

    def _get_agents(self, request: Request, method: str) -> Response:
        return _json(self.s.chat.state())

    def _new_agent(self, request: Request, method: str) -> Response:
        """{"name"}: a new agent."""
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
                if "model" in settings:
                    self.s.dags.changed(agent_id)  # its runs use it too
                self.s.rewarm.set()  # what the agent's conversation starts with has changed
            return self.s.chat.session(agent_id)

        return self._agent(agent, change)

    async def _delete_agent(self, request: Request, method: str, agent: str) -> Response:
        try:
            await self.s.chat.delete(int(agent))
        except (Busy, ValueError) as exc:  # answering you in it, or it's the master agent
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
        """An agent's DAG, and what can start it, for the DAG tab: the assets and webhooks DAGs wait for, which
        tasks' outlets update each asset, whether /hooks/<name> takes webhooks, and the tools a DAG can name."""
        dags = self.s.dags
        return {
            **dags.describe(agent_id),
            "events": {
                "assets": dags.assets(),  # asset: [agents]
                "webhooks": dags.webhooks(),  # webhook: [agents]
                "sources": dags.sources(),  # asset: [{"agent", "name", "task"}], from outlets
                "claude": CLAUDE_DONE,
                "hooks": self.hooks.on,  # type: ignore[attr-defined]
            },
            "tools": sorted(t.name for t in self.s.tools.all()),
            "enabled": self.s.config.tasks.enabled,
        }

    def _check_dag(self, request: Request, method: str, agent: str) -> Response:
        """{"source"}: whether a DAG file would do, and its tasks if it would, as it's being written."""
        agent_id = self._known(agent)
        try:
            dag = self.s.dags.check(agent_id, _source(request))
        except DagError as exc:
            return _json({"dag": None, "error": exc.as_dict(), "unknown": []})
        known = [t.name for t in self.s.tools.all()]
        return _json({"dag": as_dict(dag), "error": None, "unknown": unknown_tools(dag, known)})

    def _save_dag(self, request: Request, method: str, agent: str) -> Response:
        agent_id = self._known(agent)
        try:
            self.s.dags.save(agent_id, _source(request))
        except DagError as exc:
            raise HTTPError(400, str(exc)) from None
        self.s.rewarm.set()  # the agent's conversation starts with its DAG
        log.info("Dashboard: changed agent %d's DAG", agent_id)
        return _json(self.dag(agent_id))

    async def _dag_action(self, request: Request, method: str, agent: str, action: str) -> Response:
        agent_id, dags = self._known(agent), self.s.dags
        try:
            if action == "run":
                if not self.s.config.tasks.enabled:
                    raise ValueError("Agents' DAGs are switched off ([tasks] enabled = false in config.toml).")
                await dags.run_now(agent_id)
            elif action == "pause":
                dags.pause(agent_id)
            elif action == "resume":
                dags.resume(agent_id)
            elif not await dags.stop_running(agent_id):
                raise ValueError("It isn't running.")
        except ValueError as exc:
            raise HTTPError(409, str(exc)) from None
        return _json(self.dag(agent_id))

    def _get_runs(self, request: Request, method: str, agent: str) -> Response:
        """The agent's last runs, newest first."""
        agent_id = self._known(agent)
        return _json({"runs": [runs.brief(run) for run in self.s.dags.runs(agent_id)]})

    def _get_run(self, request: Request, method: str, run: str) -> Response:
        """A run: what each of its tasks was given, said and did, its DAG as it was, and what it proposed."""
        dags = self.s.dags
        found = dags.run(run)
        if found is None:
            raise HTTPError(404, "That run isn't kept any more: only each agent's last 10 are.")
        try:
            dag = as_dict(dags.engine.dag_for(found))  # as it was for the run, if it's going; else as it is now
        except Exception:  # its DAG's been deleted, or won't read now
            dag = None
        return _json(
            {
                **runs.detail(found),
                "agent": dags.agent_of(found),
                "dag": dag,
                "doing": dags._doing(found) if not found.is_terminal else "",
                "proposals": [dags.show(p) for p in dags.store.proposals(run_id=found.run_id)],
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

    # -- an agent's files ----------------------------------------------------------------------------

    def _space(self) -> Path:
        """The workspace, where agents' files are. 409 if Athena has no files."""
        if not self.s.config.workspace.enabled:
            raise HTTPError(409, "Files are switched off: switch them on in the Tools tab to give an agent files.")
        return workspace.workspace_of(self.s.config)

    def files(self, agent_id: int) -> list[dict[str, Any]]:
        if not self.s.config.workspace.enabled:
            return []
        return [
            {"name": name, "size": size, "path": f"{agent_files.folder(agent_id)}/{name}"}
            for name, size in agent_files.listed(self._space(), agent_id)
        ]

    def _get_files(self, request: Request, method: str, agent: str) -> Response:
        """The files that come with an agent's guidance."""
        agent_id = self._known(agent)
        return _json(
            {"files": self.files(agent_id), "on": self.s.config.workspace.enabled, "max": agent_files.MAX_FILES}
        )

    def _add_files(self, request: Request, method: str, agent: str) -> Response:
        """{"files": [{"name", "data": base64}]}: files for an agent's guidance, which its chat and runs can read."""
        agent_id, space = self._known(agent), self._space()
        sent = _body(request).get("files")
        if (
            not isinstance(sent, list)
            or not sent
            or not all(
                isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("data"), str) for f in sent
            )
        ):
            raise HTTPError(400, 'Send the files as {"files": [{"name", "data": base64}]}.')
        given = []
        for each in sent:
            name = workspace.safe_name(each["name"])
            try:
                data = base64.b64decode(each["data"], validate=True)
            except (binascii.Error, ValueError):
                raise HTTPError(400, f"{name} didn't arrive whole: try again.") from None
            if problem := files.why_not(self.s.config, name, len(data), MAX_FILE_MB):
                raise HTTPError(400, f"{name}: {problem}")
            given.append((name, data))
        for name, data in given:
            try:
                agent_files.keep(space, agent_id, name, data)
            except ToolError as exc:
                raise HTTPError(409, str(exc)) from None
        self.s.rewarm.set()  # its conversations say what its files are
        log.info("Dashboard: gave agent %d %d file%s", agent_id, len(given), "" if len(given) == 1 else "s")
        return _json({"files": self.files(agent_id)})

    def _get_file(self, request: Request, method: str, agent: str, name: str) -> Response:
        """One of an agent's files, to download."""
        agent_id, space = self._known(agent), self._space()
        name = urllib.parse.unquote(name)
        try:
            data = workspace.read(space, agent_files.path_of(space, agent_id, name), MAX_FILE_MB * 1024 * 1024)
        except ToolError as exc:
            raise HTTPError(404, str(exc)) from None
        suffix = Path(name).suffix.lower()
        kind = "application/pdf" if suffix == ".pdf" else TEXT_TYPES.get(suffix, "text/plain")
        quoted = urllib.parse.quote(name)
        return Response(data, kind, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})

    def _delete_file(self, request: Request, method: str, agent: str, name: str) -> Response:
        agent_id, space = self._known(agent), self._space()
        try:
            agent_files.remove(space, agent_id, urllib.parse.unquote(name))
        except ToolError as exc:
            raise HTTPError(404, str(exc)) from None
        self.s.rewarm.set()
        return _json({"files": self.files(agent_id)})


def _source(request: Request) -> str:
    source = _body(request).get("source")
    if not isinstance(source, str):
        raise HTTPError(400, 'Send the DAG file: {"source": "..."}.')
    return source
