"""When agents' DAGs run: on their schedules, when an event or something watched starts them, or when you ask,
one at a time; and the checks on what's watched, every hour, and looking back at skills used in a chat.

Runs wait their turn in a queue. The one to run next is one you asked for, or an event started, if there is one,
then the DAG with the fewest tasks, then the one that's waited longest. One you asked for, or an event started,
starts as soon as Athena's free; one that's due on its schedule, or was started by something watched, once Athena
has been inactive, neither answering you nor running anything, for [tasks] quiet_minutes (5). So when two DAGs
are due at the same time, the shorter runs first, and the other once it's finished and things have been quiet
for five minutes.

It's also what the agent loop asks about each agent's conversation (agent.Directory): its model, what it starts
with (its guidance, what its last run kept, its DAG), and what its DAG's doing now.
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
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from athena.agent import AgentResult, Background
from athena.clock import Clock
from athena.dags import ends
from athena.dags.runner import RunContext, Runner, apply_tools
from athena.dags.spec import STARTER, WATCH, DagError, DagSpec, parse
from athena.dags.store import APPROVED, PENDING, REJECTED, STALE, AgentStore, Proposal
from athena.history import session_of
from athena.skills import READ_SKILL, REVIEW, SAVE_SKILL
from athena.status import describe_error
from athena.steps import steps
from athena.tools import Tool, ToolRegistry, current_turn
from athena.watching import WatchStore, watch_tool
from athena.watching import describe as describe_watch

if TYPE_CHECKING:
    from athena.agent import Agent
    from athena.channel import Channel
    from athena.config import TasksConfig

log = logging.getLogger(__name__)

NOTHING = "NOTHING TO REPORT"  # what a check on what's watched replies when nothing needs you
MISSED_GRACE = timedelta(hours=6)  # a scheduled run missed by more than this (Athena was off) is skipped
HEARTBEAT_MINUTES = 10  # the most a check on what's watched works for
MIN_HEARTBEAT = 60.0  # minutes between checks, whatever heartbeat_minutes says
REVIEW_MINUTES, REVIEW_ROUNDS = 10, 4  # looking back at how a skill went in a chat
MAX_REVIEWS = 5  # waiting to be done; the oldest is dropped for a new one past this
MAX_QUEUED = 5  # runs of one agent waiting their turn
EVENT_GAP = 60.0  # seconds: the same event again sooner doesn't start a DAG again


@dataclass
class Pending:
    """A run waiting its turn."""

    agent: int
    trigger: str  # "schedule", "manual", "event:<name>" or "watch:<key>"
    input: str  # what came with it
    at: float  # when it was due
    now: bool  # as soon as Athena's free, rather than once it's been inactive for a while


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
        watches: WatchStore | None = None,
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
        self.watches = watches  # what Athena keeps an eye on for you (watching.py)
        self.on_ran = on_ran  # e.g. so the model server reads your conversation again
        self.poll = poll_seconds
        self.runner = Runner(
            cfg, store, agent, tools, channel=channel, intro=self.intro_for, tz=self.tz, main=self._make_main
        )
        self.queue: list[Pending] = []
        self.reviews: list[Review] = []  # skills to look back on, once you've finished chatting
        self.last_check = self.clock.time()  # the first check is an hour after starting
        self.check_result: dict[str, Any] | None = None  # how the last check went, for the dashboard
        self.checking = False
        self._check_now = False  # you asked for a check, on the dashboard
        self._fired: dict[tuple[int, str], float] = {}  # (agent, trigger) -> when it last started a run
        self._finished = float("-inf")  # time.monotonic() when Athena last finished something by itself
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._run: asyncio.Task[Any] | None = None

    @property
    def user(self) -> str:
        return self.agent.cfg.user_name

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
        """What an agent's conversations start with, its chat's and its runs': what it is, its guidance, what its
        last run kept, and its DAG. Nothing for an agent that's none of those."""
        config = self.store.get(agent_id)
        session = self.history.get_session(agent_id)
        name = session.name if session else ""
        if not (name or config.guidance or config.context or config.dag):
            return ""
        who = f"This is {self.user}'s agent “{name}”." if name else f"This is one of {self.user}'s agents."
        lines = ["<agent>", who]
        if config.guidance:
            lines += [f"How {self.user} wants you to act here:", config.guidance.strip()]
        if config.context:
            lines += ["What its DAG's last run kept for the next:", config.context.strip()]
        if config.dag:
            lines += [
                "Its DAG, which says what it does by itself (change_dag changes it, asking first):",
                f"```python\n{config.dag.strip()}\n```",
            ]
        lines.append("</agent>")
        return "\n".join(lines)

    def _make_main(self, agent_id: int) -> None:
        """Make an agent the main one, as it's the one with the latest news: unless you're talking to Athena."""
        if self.agent.answering_you == 0 and self.history.session.id != agent_id:
            with contextlib.suppress(KeyError):
                self.history.use_session(agent_id)

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
                if self._heartbeat_due() and (self.agent.answering_you == 0 if self._check_now else self.quiet()):
                    await self.heartbeat()
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

    def _queue(self, agent_id: int, trigger: str, input: str = "", now: bool = False, at: float | None = None) -> bool:
        waiting = [p for p in self.queue if p.agent == agent_id]
        if any(p.trigger == trigger and p.input == input for p in waiting) or len(waiting) >= MAX_QUEUED:
            return False
        self.queue.append(Pending(agent_id, trigger, input, at if at is not None else self.clock.time(), now))
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
            self.runner.run(pending.agent, pending.trigger, pending.input), name=f"dag-{pending.agent}"
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

    def fire(self, event: str, text: str = "") -> list[str]:
        """An event came in: queue the DAGs it starts, unless one started on it within EVENT_GAP seconds. Returns
        the names of the agents whose DAGs it started."""
        return self._trigger(f"event:{event}", lambda spec: event in spec.events, text)

    def happened(self, key: str, text: str = "") -> list[str]:
        """Something watched as ``key`` has happened: queue the DAGs it starts."""
        return self._trigger(f"{WATCH}{key}", lambda spec: key in spec.watches, text)

    def _trigger(self, trigger: str, starts: Callable[[DagSpec], bool], text: str) -> list[str]:
        now, started = self.clock.time(), []
        for config in self.store.all():
            spec = self.spec(config.dag)
            if config.paused or spec is None or not starts(spec):
                continue
            if now - self._fired.get((config.id, trigger), float("-inf")) < EVENT_GAP:
                continue  # it's only just started on it
            if self._queue(config.id, trigger, text, now=not trigger.startswith(WATCH)):
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
            for event in spec.events if spec and not config.paused else ():
                events.setdefault(event, []).append(self.history.title(config.id) or f"Agent {config.id}")
        return events

    def watched_by(self) -> dict[str, list[str]]:
        """The keys of what's watched that start DAGs, with the names of their agents."""
        keys: dict[str, list[str]] = {}
        for config in self.store.all():
            spec = self.spec(config.dag)
            for key in spec.watches if spec and not config.paused else ():
                keys.setdefault(key, []).append(self.history.title(config.id) or f"Agent {config.id}")
        return keys

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
        """Forget an agent's DAG, its runs and what they proposed, as when the agent's deleted: stopping a run of it
        first, if one's going."""
        await self.halt(agent_id)
        self.queue = [p for p in self.queue if p.agent != agent_id]
        self.store.delete(agent_id)

    def forget(self) -> None:
        """Forget what runs and checks found, as forgetting everything does. The DAGs, and what's watched, stay."""
        self.store.forget()
        self.check_result = None
        if self.watches:
            self.watches.forget_notes()

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
        source = config.dag or STARTER
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

    # -- checking on what's watched ----------------------------------------------------------------------

    def _heartbeat_minutes(self) -> float:
        return max(self.cfg.heartbeat_minutes, MIN_HEARTBEAT) if self.cfg.heartbeat_minutes else 0.0

    def _heartbeat_due(self) -> bool:
        if self.watches is None or not self._heartbeat_minutes() or not self.watches.all():
            return False
        return self._check_now or self.clock.time() - self.last_check >= self._heartbeat_minutes() * 60

    def check_now(self) -> None:
        """Check on what's watched as soon as Athena's free, as asked on the dashboard."""
        self._check_now = True
        self.wake()

    async def heartbeat(self) -> None:
        """Check on everything that's watched, in one turn: start the DAGs waiting for what's happened, and tell
        you only what needs your attention."""
        assert self.watches is not None
        asked = self._check_now  # you asked for it: it doesn't wait for you to have been quiet a moment
        self._check_now, self.last_check = False, self.clock.time()
        for gone in self.watches.expire(self.clock.time()):
            await self._tell(f"👀 I've stopped watching for this, as it was only until today: {gone.what}")
        watching = self.watches.all()
        if not watching:
            return
        starts = self.watched_by()
        listed = "\n".join(describe_watch(w, self.tz, starts.get(w.key, [])) for w in watching)
        text = (
            f"Check on what {self.user} asked you to keep an eye on:\n{listed}\n\n"
            "Use your tools to see where each one stands. When one that starts an agent's DAG has happened, call "
            "watch with action happened, and what you found, rather than telling them yourself. Use action note to "
            "keep what you'll want next time, such as the last figure you saw or what you've passed on already, "
            "and action done once one no longer matters. Then reply with only what needs their attention now, "
            f"short. If nothing does, reply with just: {NOTHING}"
        )
        note = (
            f"This is your regular check, every {self._heartbeat_minutes():g} minutes, not a message from "
            f"{self.user}. They aren't watching, so don't ask them anything. Your earlier checks are above."
        )

        async def confirm(tool: str, args: dict[str, Any], summary: str | None = None) -> bool:
            channel = self.channel()
            if channel is None:
                return False
            why = "While checking what you asked me to watch"
            async with self.agent.gate.aside():
                return await channel.ask(tool, args, summary, why=why, timeout=self.cfg.confirm_minutes * 60)

        log.info("Checking on %d watched thing%s", len(watching), "" if len(watching) == 1 else "s")
        self.checking = True
        taken, on_tool = steps()
        outcome, said = "nothing new", ""
        try:
            async with self.agent.gate.task("checking what you're watching", grace=not asked):
                background = Background(  # its time counts from now, when it has the model
                    label="Checking what you're watching",
                    step=_budget(HEARTBEAT_MINUTES),
                    thinking=self.cfg.thinking,
                    max_rounds=self.cfg.max_tool_rounds,
                    source="checking what you asked me to watch",
                )
                result = await self.agent.respond(
                    "heartbeat",
                    text,
                    confirm=confirm,
                    allowed=self.cfg.heartbeat_tools,
                    on_tool=on_tool,
                    note=note,
                    channel="Heartbeat",
                    background=background,
                )
        except _OutOfTime:
            outcome = f"ran out of time ({HEARTBEAT_MINUTES} minutes)"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = f"failed: {describe_error(exc)}"
            log.warning("The check on what's watched failed: %s", exc)
        else:
            said = result.text.strip()
            if result.saved and NOTHING not in said.upper():
                outcome = "told you"
                await self._tell(f"👀 {said}", keep=("(You checked what I asked you to watch.)", said))
            else:
                said = ""
        finally:
            self.checking = False
            self._finished = time.monotonic()
        self.watches.checked(self.clock.time())
        self.check_result = {"at": self.last_check, "outcome": outcome, "said": said, "steps": taken}
        log.info("The check on what's watched: %s", outcome)
        self.on_ran()

    def watching(self) -> dict[str, Any]:
        """What's watched, and the checks, for the dashboard."""
        minutes = self._heartbeat_minutes()
        items = self.watches.all() if self.watches else []
        starts = self.watched_by()
        return {
            "items": [
                {
                    "id": w.id,
                    "key": w.key,
                    "what": w.what,
                    "created": w.created,
                    "until": w.until,
                    "by": w.by,
                    "note": w.note,
                    "starts": starts.get(w.key, []),
                }
                for w in items
            ],
            "every_minutes": minutes,
            "next": self.last_check + minutes * 60 if minutes and items else None,
            "last": self.check_result,
            "checking": self.checking,
            "asked": self._check_now,  # you asked for a check, which hasn't started yet
        }

    async def _tell(self, text: str, keep: tuple[str, str] | None = None) -> None:
        """Message you. ``keep``: (a line for your side, what was said), to keep in the main agent's chat too."""
        channel = self.channel()
        if channel is None:
            log.warning("Nowhere to send a message of %d characters", len(text))  # not what it says
            return
        try:
            await channel.tell(text)
        except Exception as exc:
            log.warning("Couldn't send a message: %s", exc)
        if keep:  # so you can ask about it
            yours, said = keep
            self.history.append_exchange(self.history.current_chat, yours, said, channel="Athena")

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

        watching = (
            [watch_tool(self.watches, self.tz, self._heartbeat_minutes(), self.wake, self.happened)]
            if self.watches
            else []
        )
        return [*watching, *dag_tools(self)]

    def agent_here(self) -> int:
        """The agent of the conversation a tool's called in: the main one, if it's not an agent's."""
        chat = current_turn().chat
        agent_id = session_of(chat) if chat else None
        return agent_id if agent_id is not None else self.history.session.id


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
