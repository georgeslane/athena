"""When agents' DAGs run: on their schedules, when an event starts them, or when you ask, one at a time; and
looking back at skills used in a chat.

Runs wait their turn in a queue. The one to run next is one you asked for, or an event started, if there is one,
then the DAG with the fewest tasks, then the one that's waited longest. One you asked for, or an event started,
starts as soon as Athena's free; one that's due on its schedule once Athena has been inactive, neither answering
you nor running anything, for [tasks] quiet_minutes (5). So when two DAGs are due at the same time, the shorter
runs first, and the other once it's finished and things have been quiet for five minutes.

Events come from outside, through /hooks/<event> (triggers.py), from Claude finishing a project, and from the
outlets of agents' DAGs: what a task with outlets finds starts the DAGs with that event in their triggers, such as
an agent that applies for jobs starting from what the master agent's DAG found in your emails. A DAG isn't
started by an event its own run led to, however many agents that went through, so they can't go round in a
circle.

It's also what the agent loop asks about each agent's conversation (agent.Directory): its model, what it starts
with (its guidance and files, what its last run kept, its DAG, and for the master agent, your other agents), and
what its DAG's doing now.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from athena.agent import AgentResult, Background
from athena.clock import Clock
from athena.dags import ends
from athena.dags import files as agent_files
from athena.dags.runner import RunContext, Runner, apply_tools
from athena.dags.spec import MASTER_STARTER, STARTER, DagError, DagSpec, parse
from athena.dags.store import APPROVED, PENDING, REJECTED, STALE, AgentStore, Proposal
from athena.history import session_of
from athena.skills import READ_SKILL, REVIEW, SAVE_SKILL
from athena.status import describe_error
from athena.tools import Tool, ToolRegistry, current_turn
from athena.workspace import show_size

if TYPE_CHECKING:
    from athena.agent import Agent
    from athena.channel import Channel
    from athena.config import TasksConfig

log = logging.getLogger(__name__)

MISSED_GRACE = timedelta(hours=6)  # a scheduled run missed by more than this (Athena was off) is skipped
REVIEW_MINUTES, REVIEW_ROUNDS = 10, 4  # looking back at how a skill went in a chat
MAX_REVIEWS = 5  # waiting to be done; the oldest is dropped for a new one past this
MAX_QUEUED = 5  # runs of one agent waiting their turn
EVENT_GAP = 60.0  # seconds: the same event again sooner doesn't start a DAG again
ABOUT_CHARS = 160  # of an agent's guidance, as the master agent's told of it


@dataclass
class Pending:
    """A run waiting its turn."""

    agent: int
    trigger: str  # "schedule", "manual" or "event:<name>"
    input: str  # what came with it
    at: float  # when it was due
    now: bool  # as soon as Athena's free, rather than once it's been inactive for a while
    items: list[str] = field(default_factory=list)  # what came with it, as items, for for_each=trigger
    chain: tuple[int, ...] = ()  # the agents whose runs led to it, through outlets


@dataclass
class Review:
    """Looking back at how skills went, after Athena used them in a chat."""

    skills: list[str]
    transcript: list[dict[str, Any]] = field(repr=False)  # what it did, from AgentResult.transcript
    what: str  # what it used them for: "our chat"


class Dags:
    def __init__(
        self,
        cfg: TasksConfig,
        store: AgentStore,
        agent: Agent,
        *,
        timezone: str,
        channel: Callable[[], Channel | None],
        tools: ToolRegistry,
        workspace: Path | None = None,
        clock: Clock | None = None,
        on_ran: Callable[[], None] = lambda: None,
        poll_seconds: float = 15.0,
    ):
        self.cfg = cfg
        self.store = store
        self.agent = agent
        self.history = agent.history
        self.clock = clock or Clock()
        try:
            self.tz: tzinfo = ZoneInfo(timezone)
        except Exception:  # unknown timezone name
            self.tz = UTC
        self.channel = channel
        self.registry = tools
        self.workspace = workspace  # where agents' files are, if Athena has files (files.py)
        self.on_ran = on_ran  # e.g. so the model server reads your conversation again
        self.poll = poll_seconds
        self.runner = Runner(
            cfg,
            store,
            agent,
            tools,
            channel=channel,
            intro=self.intro_for,
            tz=self.tz,
            master=lambda: self.master,
            emit=self.emitted,
        )
        self.queue: list[Pending] = []
        self.reviews: list[Review] = []  # skills to look back on, once you've finished chatting
        self._fired: dict[tuple[int, str], float] = {}  # (agent, trigger) -> when it last started a run
        self._finished = float("-inf")  # time.monotonic() when Athena last finished something by itself
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._run: asyncio.Task[Any] | None = None

    @property
    def user(self) -> str:
        return self.agent.cfg.user_name

    @property
    def master(self) -> int:
        """The master agent: where Telegram, Siri and the terminal talk to you, and news from the others is told."""
        return self.history.master.id

    @property
    def current(self) -> RunContext | None:
        return self.runner.current

    # -- each agent's conversation (agent.Directory) ---------------------------------------------------

    def model(self, chat_id: str) -> str | None:
        agent_id = session_of(chat_id)
        return (self.store.get(agent_id).model or None) if agent_id is not None else None

    def intro(self, chat_id: str) -> str:
        agent_id = session_of(chat_id)
        return self.intro_for(agent_id) if agent_id is not None else ""

    def note(self, chat_id: str) -> str:
        current = self.current
        if current is None or session_of(chat_id) != current.run.agent:
            return ""
        return current.note(self.user)

    def intro_for(self, agent_id: int) -> str:
        """What an agent's conversations start with, its chat's and its runs': what it is, its guidance and its
        files, what its last run kept, and its DAG; and for the master agent, your other agents. Nothing for an
        agent that's none of those."""
        config = self.store.get(agent_id)
        session = self.history.get_session(agent_id)
        name = session.name if session else ""
        kept = agent_files.listed(self.workspace, agent_id)
        if agent_id == self.master:
            lines = [
                "<agent>",
                f"This is the master agent, where {self.user} talks to you from Telegram, Siri, the terminal and the "
                f"dashboard. Each of their other agents does something for them by itself: you answer {self.user}'s "
                "questions about those agents, and help with anything else.",
                *self._roster(),
            ]
        elif name or config.guidance or config.context or config.dag or kept:
            who = f"This is {self.user}'s agent “{name}”." if name else f"This is one of {self.user}'s agents."
            lines = ["<agent>", who]
        else:
            return ""
        if config.guidance:
            lines += [f"How {self.user} wants you to act here:", config.guidance.strip()]
        if kept:
            listed = ", ".join(f"{agent_files.folder(agent_id)}/{n} ({show_size(size)})" for n, size in kept)
            lines.append(f"Files {self.user} gave this agent, to read with read_file when they'd help: {listed}.")
        if config.context:
            lines += ["What its DAG's last run kept for the next:", config.context.strip()]
        if config.dag:
            lines += [
                "Its DAG, which says what it does by itself (change_dag changes it, asking first):",
                f"```python\n{config.dag.strip()}\n```",
            ]
        lines.append("</agent>")
        return "\n".join(lines)

    def _roster(self) -> list[str]:
        """Your other agents, as the master agent's told of them: what each is for, and when its DAG runs. In the
        order they were made, so it only changes when they do."""
        configs = {c.id: c for c in self.store.all()}
        lines = []
        for agent_id, name in self.agents():
            if agent_id == self.master:
                continue
            config = configs.get(agent_id)
            spec = self.spec(config.dag) if config else None
            runs = f" Its DAG runs {spec.describe()}." if spec else " It has no DAG."
            about = " ".join(config.guidance.split()) if config else ""
            about = about if len(about) <= ABOUT_CHARS else about[: ABOUT_CHARS - 1] + "…"
            lines.append(f"- “{name}” (#{agent_id}).{runs}{f' {about}' if about else ''}")
        if not lines:
            return [f"{self.user} has no other agents yet: they make them on the dashboard's Agents tab."]
        return [
            "Their other agents:",
            *lines,
            "When a run of one of their DAGs has news, it's told to you here too. agent_runs reads what any of them "
            "did; change_dag and run_dag act on one of them by name.",
        ]

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
        named = [(agent_id, name) for agent_id, name in self.agents()]
        exact = [agent_id for agent_id, name in named if name.casefold() == which.casefold()]
        partly = [agent_id for agent_id, name in named if which.casefold() in name.casefold()]
        found = exact or partly
        return found[0] if len(found) == 1 else None

    # -- starting and stopping -------------------------------------------------------------------------

    def start(self) -> None:
        if self._loop is None:
            if stopped := self.store.interrupted():
                log.info("%d DAG run%s stopped when Athena did", stopped, "" if stopped == 1 else "s")
            self._skip_missed()
            self._loop = asyncio.create_task(self._forever(), name="dags")

    async def stop(self) -> None:
        if self._loop:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop
        self._loop = None

    def wake(self) -> None:
        self._wake.set()

    def _skip_missed(self) -> None:
        """Scheduled runs missed by a long way, say while the Pi was off, are skipped to the next one."""
        now = self.clock.now(self.tz)
        for config in self.store.all():
            spec = self.spec(config.dag)
            if spec is None or spec.schedule is None:
                if config.next_run is not None:
                    self.store.update(config.id, next_run=None)
                continue
            if config.next_run is None or config.next_run < (now - MISSED_GRACE).timestamp():
                self.store.update(config.id, next_run=spec.schedule.after(now).timestamp())

    async def _forever(self) -> None:
        while True:
            try:
                self._due()
                pending = self._next()
                if pending and self.agent.answering_you == 0 and (pending.now or self.quiet()):
                    await self._start(pending)
                    continue
                if self.reviews and self.quiet():
                    await self.review(self.reviews.pop(0))
                    continue
                await self._sleep(self.poll if pending or self.reviews else self._until_due())
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug in one run shouldn't stop the others
                log.exception("The DAG scheduler hit a problem")
                await self._sleep(60)

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        await self.clock.sleep(seconds, self._wake)

    def quiet(self) -> bool:
        """Athena's been inactive for [tasks] quiet_minutes: neither answering you nor running anything."""
        idle = time.monotonic() - max(self.agent.last_answered_you, self._finished)
        return self.agent.answering_you == 0 and idle >= self.cfg.quiet_minutes * 60

    def _until_due(self) -> float:
        dues = [c.next_run for c in self.store.all() if c.next_run is not None and not c.paused]
        return 60.0 if not dues else min(60.0, max(1.0, min(dues) - self.clock.time()))

    # -- the queue -----------------------------------------------------------------------------------

    @staticmethod
    @functools.lru_cache(maxsize=64)
    def _parse(source: str) -> DagSpec | None:
        try:
            return parse(source) if source.strip() else None
        except DagError:
            return None

    def spec(self, source: str) -> DagSpec | None:
        """A DAG file's DAG, or None if it hasn't one or it won't do: read once for each file there's been."""
        return self._parse(source)

    def _due(self) -> None:
        """Queue the DAGs due on their schedules, and work out when each is next due."""
        now = self.clock.time()
        for config in self.store.all():
            if config.paused or config.next_run is None or config.next_run > now:
                continue
            spec = self.spec(config.dag)
            if spec is None or spec.schedule is None:
                self.store.update(config.id, next_run=None)
                continue
            self._queue(config.id, "schedule", at=config.next_run)
            self.store.update(config.id, next_run=spec.schedule.after(self.clock.now(self.tz)).timestamp())

    def _queue(
        self,
        agent_id: int,
        trigger: str,
        input: str = "",
        items: list[str] | None = None,
        *,
        now: bool = False,
        at: float | None = None,
        chain: tuple[int, ...] = (),
    ) -> bool:
        items = list(items or [])
        waiting = [p for p in self.queue if p.agent == agent_id]
        same = any(p.trigger == trigger and p.input == input and p.items == items for p in waiting)
        if same or len(waiting) >= MAX_QUEUED:
            return False
        when = at if at is not None else self.clock.time()
        self.queue.append(Pending(agent_id, trigger, input, when, now, items, chain))
        self.wake()
        return True

    def _next(self) -> Pending | None:
        """The run to start next: one you asked for, or an event started, then the DAG with the fewest tasks, then
        the one that's waited longest."""
        if not self.queue:
            return None

        def size(p: Pending) -> int:
            spec = self.spec(self.store.get(p.agent).dag)
            return len(spec.tasks) if spec else 0

        return min(self.queue, key=lambda p: (not p.now, size(p), p.at))

    async def _start(self, pending: Pending) -> None:
        self.queue.remove(pending)
        self._fired[(pending.agent, pending.trigger)] = self.clock.time()
        self._run = asyncio.create_task(
            self.runner.run(pending.agent, pending.trigger, pending.input, pending.items, pending.chain),
            name=f"dag-{pending.agent}",
        )
        try:
            await asyncio.wait({self._run})
        except asyncio.CancelledError:  # Athena's stopping
            self._run.cancel()
            raise
        finally:
            self._finished = time.monotonic()
        if not self._run.cancelled() and (exc := self._run.exception()):
            log.error("Agent %d's DAG run failed", pending.agent, exc_info=exc)
        self.on_ran()

    # -- what you, and events, do to them -------------------------------------------------------------

    def run_now(self, agent_id: int) -> None:
        """Run an agent's DAG as soon as Athena's free. ValueError if it can't run."""
        config = self.store.get(agent_id)
        if not config.dag.strip():
            raise ValueError("This agent has no DAG yet: write one first.")
        if self.spec(config.dag) is None:
            raise ValueError("Its DAG file has a problem: fix it first.")
        if not self._queue(agent_id, "manual", now=True):
            raise ValueError("It's already waiting to run.")

    def fire(self, event: str, text: str = "", items: list[str] | None = None) -> list[str]:
        """An event came in, from outside or from Claude: queue the DAGs it starts, unless one started on it within
        EVENT_GAP seconds. Returns the names of the agents whose DAGs it started."""
        return self._trigger(event, text, items)

    def emitted(self, ctx: RunContext, event: str, text: str, items: list[str]) -> list[str]:
        """A task with outlets found something: start the DAGs with the event in their triggers, but not those of
        the agents whose runs led here."""
        return self._trigger(event, text, items, chain=(*ctx.chain, ctx.run.agent))

    def _trigger(self, event: str, text: str, items: list[str] | None, chain: tuple[int, ...] = ()) -> list[str]:
        trigger, now, started = f"event:{event}", self.clock.time(), []
        for config in self.store.all():
            spec = self.spec(config.dag)
            if config.paused or spec is None or event not in spec.triggers:
                continue
            if config.id in chain:
                log.info("%s would start agent %d's DAG again, which led to it: it doesn't", trigger, config.id)
                continue
            if now - self._fired.get((config.id, trigger), float("-inf")) < EVENT_GAP:
                continue  # it's only just started on it
            if self._queue(config.id, trigger, text, items, now=True, chain=chain):
                self._fired[(config.id, trigger)] = now
                started.append(self.history.title(config.id) or f"Agent {config.id}")
        if started:
            log.info("%s started %d DAG%s", trigger, len(started), "" if len(started) == 1 else "s")
        return started

    def waiting_for(self) -> dict[str, list[str]]:
        """The events DAGs that aren't paused start on, with the names of their agents."""
        events: dict[str, list[str]] = {}
        for config in self.store.all():
            spec = self.spec(config.dag)
            for event in spec.triggers if spec and not config.paused else ():
                events.setdefault(event, []).append(self.history.title(config.id) or f"Agent {config.id}")
        return events

    def sources(self) -> dict[str, list[dict[str, Any]]]:
        """The events DAGs' outlets start, with where each comes from: {event: [{"agent", "name", "task"}]}."""
        found: dict[str, list[dict[str, Any]]] = {}
        for config in self.store.all():
            spec = self.spec(config.dag)
            for task in spec.tasks if spec else ():
                for event in task.outlets:
                    name = self.history.title(config.id) or f"Agent {config.id}"
                    found.setdefault(event, []).append({"agent": config.id, "name": name, "task": task.id})
        return found

    def save(self, agent_id: int, source: str) -> DagSpec:
        """Make ``source`` an agent's DAG, and work out when it's next due. DagError if it won't do."""
        spec = parse(source)
        nxt = spec.schedule.after(self.clock.now(self.tz)).timestamp() if spec.schedule else None
        self.store.update(agent_id, dag=source, next_run=nxt)
        self.wake()
        return spec

    def pause(self, agent_id: int) -> None:
        self.store.update(agent_id, paused=True)
        self.queue = [p for p in self.queue if p.agent != agent_id or p.trigger == "manual"]

    def resume(self, agent_id: int) -> None:
        spec = self.spec(self.store.get(agent_id).dag)
        nxt = spec.schedule.after(self.clock.now(self.tz)).timestamp() if spec and spec.schedule else None
        self.store.update(agent_id, paused=False, next_run=nxt)
        self.wake()

    def stop_running(self, agent_id: int | None = None) -> bool:
        """Stop the run that's going, if it's ``agent_id``'s, or anyone's. False if none is."""
        current = self.current
        if self._run and not self._run.done() and (agent_id is None or (current and current.run.agent == agent_id)):
            self._run.cancel()
            return True
        return False

    async def halt(self, agent_id: int | None = None) -> None:
        """Stop the run that's going, if it's ``agent_id``'s, or anyone's, and wait for it to have stopped."""
        run = self._run
        if self.stop_running(agent_id) and run is not None:
            await asyncio.wait({run}, timeout=10)

    async def delete(self, agent_id: int) -> None:
        """Forget an agent's DAG, its runs, what they proposed and its files, as when the agent's deleted: stopping
        a run of it first, if one's going."""
        await self.halt(agent_id)
        self.queue = [p for p in self.queue if p.agent != agent_id]
        self.store.delete(agent_id)
        agent_files.forget(self.workspace, agent_id)

    def forget(self) -> None:
        """Forget what runs found, as forgetting everything does. The agents and their DAGs stay."""
        self.store.forget()

    def decide(self, proposal_id: int, approve: bool) -> Proposal:
        """Approve or reject a proposal. ValueError if it can't be, as when the DAG's changed since."""
        proposal = self.store.proposal(proposal_id)
        if proposal is None or proposal.state != PENDING:
            raise ValueError("That proposal isn't waiting for an answer.")
        if not approve:
            self.store.decide(proposal, REJECTED)
            return proposal
        before = self.store.get(proposal.agent).dag
        if proposal.kind == "workflow":
            if not ends.same(proposal.base, before):
                self.store.decide(proposal, STALE)
                raise ValueError("The DAG has changed since this was proposed, so it's out of date.")
            after = proposal.source
        else:
            after = apply_tools(before, proposal.changes)
        self.save(proposal.agent, after)
        self.store.decide(proposal, APPROVED)
        for waiting in self.store.proposals(proposal.agent):  # one still waiting, as the DAG is now
            if waiting.kind == "workflow" and ends.same(waiting.base, before) and proposal.kind == "tools":
                with contextlib.suppress(DagError, ValueError):
                    self.store.rebase(waiting, apply_tools(waiting.source, proposal.changes), after)
        return proposal

    def describe(self, agent_id: int) -> dict[str, Any]:
        """An agent's DAG, for the dashboard: its file and tasks, or what's wrong with it, when it runs, whether
        it's running, and what's waiting for you."""
        config = self.store.get(agent_id)
        master = agent_id == self.master
        source = config.dag or (MASTER_STARTER if master else STARTER)
        try:
            spec, error = parse(source), None
        except DagError as exc:
            spec, error = None, exc.as_dict()
        current = self.current
        running = current is not None and current.run.agent == agent_id
        last = self.store.runs(agent_id, 1)
        return {
            "source": source,
            "written": bool(config.dag),
            "master": master,
            "dag": spec.as_dict() if spec else None,
            "error": error,
            "paused": config.paused,
            "next_run": config.next_run if not config.paused else None,
            "running": {"run": current.run.id, "number": current.run.number, "doing": current.doing}
            if running and current
            else None,
            "queued": [p.trigger for p in self.queue if p.agent == agent_id],
            "last": last[0].as_dict(tasks=False) if last else None,
            "proposals": [self.show(p) for p in self.store.proposals(agent_id)],
        }

    def show(self, proposal: Proposal) -> dict[str, Any]:
        """A proposal, for the dashboard: what it changes, as a diff or in words."""
        out = proposal.as_dict()
        if proposal.kind == "workflow":
            out["diff"] = diff(proposal.base, proposal.source)
        else:
            out["summary"] = ends.describe_tools(proposal.changes)
        return out

    async def _tell(self, text: str) -> None:
        channel = self.channel()
        if channel is None:
            log.warning("Nowhere to send a message of %d characters", len(text))  # not what it says
            return
        try:
            await channel.tell(text)
        except Exception as exc:
            log.warning("Couldn't send a message: %s", exc)

    # -- looking back at how skills went in a chat ---------------------------------------------------

    def after_answer(self, result: AgentResult, background: Background | None) -> None:
        """Agent.on_answered: if a skill was used answering you, look back at how it went once you've finished
        chatting. A DAG run looks back at its own, at its end."""
        skills = list(dict.fromkeys(result.notes.get("skills", [])))
        if not skills or not result.transcript or background is not None:
            return
        self.reviews.append(Review(skills, result.transcript, "our chat"))
        del self.reviews[:-MAX_REVIEWS]
        self.wake()

    async def review(self, review: Review) -> None:
        """Ask the model, with what it did in front of it, whether a change to the skills would help."""
        names = " and ".join(f"“{name}”" for name in review.skills)
        used = f"your skill{'s' if len(review.skills) > 1 else ''} {names}"
        log.info("Looking back at %s", used)
        try:
            async with self.agent.gate.task(f"looking back at {used}"):
                background = Background(
                    label=f"Looking back at {used}",
                    step=_budget(REVIEW_MINUTES),
                    thinking=self.cfg.thinking,
                    max_rounds=REVIEW_ROUNDS,
                    source=f"looking back at {used}, after {review.what}",
                    only_tools=frozenset({READ_SKILL, SAVE_SKILL}),
                    review=True,
                )
                text = REVIEW.format(user=self.user, skills=used)
                result = await self.agent.follow_up(review.transcript, text, background)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # it's only a suggestion: not worth trying again
            log.warning("Couldn't look back at %s: %s", used, describe_error(exc))
            return
        finally:
            self._finished = time.monotonic()
        if "skill" in result.notes.get("suggested", []):
            await self._tell(
                f"✏️ After {review.what}, I've suggested a change to {used}. "
                "Approve it, or not, on the dashboard's Skills tab."
            )

    # -- the model's tools ---------------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        from athena.dags.tools import dag_tools

        return dag_tools(self)

    def agent_here(self) -> int:
        """The agent of the conversation a tool's called in: the master agent, if it's not an agent's."""
        chat = current_turn().chat
        agent_id = session_of(chat) if chat else None
        return agent_id if agent_id is not None else self.master


class _OutOfTime(Exception):
    pass


def _budget(minutes: float) -> Callable[[Any, Callable[[], Awaitable[Any]]], Awaitable[Any]]:
    """How background work takes each step: _OutOfTime once it's worked for ``minutes``."""
    started = time.monotonic()

    async def step(status: Any, call: Callable[[], Awaitable[Any]]) -> Any:
        if time.monotonic() - started > minutes * 60:
            raise _OutOfTime
        return await call()

    return step


def diff(before: str, after: str) -> str:
    """What a change to a DAG file changes, as a unified diff."""
    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(), "dag.py (now)", "dag.py (proposed)", lineterm="", n=2
    )
    return "\n".join(lines)
