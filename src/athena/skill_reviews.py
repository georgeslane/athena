"""Looking back at how skills went, after Athena used them in a chat.

Once you've likely finished chatting (REVIEW_AFTER since the answer, and not while it's answering you), the model
is asked, with what it did in front of it, whether a change to a skill would help next time; one it suggests waits
for you on the dashboard's Skills tab, and you're told. A DAG run looks back at the skills it read at its own end
(dags/ends.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from athena.agent import AgentResult, Background
from athena.skills import READ_SKILL, REVIEW, SAVE_SKILL
from athena.status import describe_error

if TYPE_CHECKING:
    from athena.agent import Agent
    from athena.channel import Channel
    from athena.config import TasksConfig

log = logging.getLogger(__name__)

REVIEW_MINUTES, REVIEW_ROUNDS = 10, 4  # for each look back
REVIEW_AFTER = 60.0  # seconds since a skill was used: once you've likely finished chatting
MAX_REVIEWS = 5  # waiting to be done; the oldest is dropped for a new one past this


@dataclass
class Review:
    """Looking back at how skills went, after Athena used them in a chat."""

    skills: list[str]
    transcript: list[dict[str, Any]] = field(repr=False)  # what it did, from AgentResult.transcript
    what: str  # what it used them for: "our chat"
    at: float = field(default_factory=time.monotonic)


class SkillReviews:
    def __init__(self, cfg: TasksConfig, agent: Agent, channel: Callable[[], Channel | None]):
        self.cfg = cfg  # [tasks] thinking, as it is when each is done
        self.agent = agent
        self.channel = channel
        self.reviews: list[Review] = []  # to do, once you've finished chatting: they hold what was said
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._forever(), name="skill-reviews")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def after_answer(self, result: AgentResult, background: Background | None) -> None:
        """Agent.on_answered: if a skill was used answering you, look back at how it went once you've likely
        finished chatting."""
        skills = list(dict.fromkeys(result.notes.get("skills", [])))
        if not skills or not result.transcript or background is not None:
            return
        self.reviews.append(Review(skills, result.transcript, "our chat"))
        del self.reviews[:-MAX_REVIEWS]
        self._wake.set()

    async def _forever(self) -> None:
        while True:
            self._wake.clear()
            if not self.reviews:
                await self._wake.wait()
                continue
            wait = REVIEW_AFTER - (time.monotonic() - self.reviews[0].at)
            if wait > 0 or self.agent.answering_you:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), max(wait, 5.0))
                continue
            try:
                await self.review(self.reviews.pop(0))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Couldn't look back at a skill")

    async def review(self, review: Review) -> None:
        """Ask the model, with what it did in front of it, whether a change to the skills would help."""
        names = " and ".join(f"“{name}”" for name in review.skills)
        used = f"your skill{'s' if len(review.skills) > 1 else ''} {names}"
        log.info("Looking back at %s", used)
        started = time.monotonic()

        async def step(status: Any, call: Callable[[], Any]) -> Any:
            if time.monotonic() - started > REVIEW_MINUTES * 60:
                raise TimeoutError("ran out of time")
            return await call()

        background = Background(
            label=f"Looking back at {used}",
            step=step,
            thinking=self.cfg.thinking,
            max_rounds=REVIEW_ROUNDS,
            source=f"looking back at {used}, after {review.what}",
            only_tools=frozenset({READ_SKILL, SAVE_SKILL}),
            review=True,
        )
        text = REVIEW.format(user=self.agent.cfg.user_name, skills=used)
        try:
            result = await self.agent.follow_up(review.transcript, text, background)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # it's only a suggestion: not worth trying again
            log.warning("Couldn't look back at %s: %s", used, describe_error(exc))
            return
        if "skill" in result.notes.get("suggested", []):
            channel = self.channel()
            with contextlib.suppress(Exception):
                if channel:
                    await channel.tell(
                        f"✏️ After {review.what}, I've suggested a change to {used}. "
                        "Approve it, or not, on the dashboard's Skills tab."
                    )
