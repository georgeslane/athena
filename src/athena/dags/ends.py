"""The tasks every DAG ends with (spec.END_TASKS): what each is asked, and how its answer's read.

They run after the DAG's own tasks, in the same conversation, so the model has the whole run in front of it and
the server's cache of it holds. None of them needs a tool but the skill review: the workflow review answers with
a whole new DAG file, the tool review with a little JSON, saving context with what to keep, and the summary with
the summary. A review with nothing to look at, such as the tool review of a run that used no tools, is skipped
without asking the model anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from athena.dags.spec import DagError, DagSpec, parse

NO_CHANGES = "NO CHANGES"
UNCHANGED = "UNCHANGED"
QUIET = "[quiet]"
CONTEXT_WORDS = 1200  # the most save_context keeps
_FENCE = re.compile(r"```(\w*)[ \t]*\n(.*?)```", re.S)


@dataclass
class ToolUse:
    """How a run used one tool."""

    calls: int = 0
    failed: int = 0
    declined: int = 0
    asked: int = 0  # times you were asked
    allowed: int = 0  # times it ran without asking, as it may here


@dataclass
class Uses:
    """How a run used its tools, from its tasks' steps."""

    tools: dict[str, ToolUse] = field(default_factory=dict)

    @classmethod
    def of(cls, steps: Sequence[dict[str, str]]) -> Uses:
        uses = cls()
        for step in steps:
            use = uses.tools.setdefault(step["tool"], ToolUse())
            use.calls += 1
            use.failed += step.get("outcome") == "failed"
            use.declined += step.get("outcome") == "declined"
            use.asked += step.get("approval") == "you"
            use.allowed += step.get("approval") == "allowed"
        return uses


# -- the workflow review -------------------------------------------------------------------------------


def workflow(source: str) -> str:
    return (
        "(From the system: the run's own tasks have finished.) Look back at how well this DAG did what it's for: "
        "a task that's missing, unnecessary or in the wrong place, prompts "
        "that were unclear, work a task should hand on to a triggered task, or anything that made the run "
        "slower or worse than it needed to be. This is the DAG file now:\n"
        f"```python\n{source.strip()}\n```\n"
        "If changing it would make it clearly more effective, reply with what you'd change and why, in a few "
        "sentences, then the whole new file in a ```python block. Keep what works, and the same file's layout. "
        f"Its tasks may use any of your tools. If it's fine as it is, reply with just: {NO_CHANGES}"
    )


def proposed_dag(answer: str) -> tuple[str, str] | None:
    """The DAG file the workflow review proposes, and why: None if it proposes none. DagError if it's one that
    won't do."""
    if NO_CHANGES in answer.upper() and "```" not in answer:
        return None
    blocks = [(lang.lower(), body) for lang, body in _FENCE.findall(answer)]
    code = [body for lang, body in blocks if lang in ("python", "py", "")]
    if not code:
        return None
    source = code[-1].strip() + "\n"
    parse(source)
    why = _FENCE.sub("", answer).strip()
    return source, why


def fix(error: DagError) -> str:
    return (
        f"That DAG file won't do: {error}. Reply with the whole corrected file in a ```python block, or just "
        f"{NO_CHANGES}."
    )


def same(a: str, b: str) -> bool:
    """Whether two DAG files say the same, whatever their spacing."""
    return " ".join(a.split()) == " ".join(b.split())


# -- the tool review -----------------------------------------------------------------------------------


def tools(uses: Uses, spec: DagSpec, in_prompt: Sequence[str], asks_first: Sequence[str]) -> str:
    """What the tool review is asked: how each tool was used, and which tools are where now."""
    lines = []
    for name, use in sorted(uses.tools.items(), key=lambda kv: -kv[1].calls):
        parts = [f"{use.calls} call{'s' if use.calls != 1 else ''}"]
        for count, said in ((use.failed, "failed"), (use.declined, "declined by the user"),
                            (use.asked, "asked the user first"), (use.allowed, "ran without asking")):  # fmt: skip
            if count:
                parts.append(f"{count} {said}")
        lines.append(f"- {name}: {', '.join(parts)}")
    unused = [name for name in in_prompt if name not in uses.tools]
    return (
        "(From the system.) Look at the tools this run used, to make the next run quicker and smoother:\n"
        + "\n".join(lines)
        + "\n\nSome tools are in every prompt, which makes every request longer, and some are hidden: found with "
        "find_tools when they're needed. Some ask the user first, every time, unless the DAG allows them to run "
        "without asking.\n"
        f"In the prompt now, and not used in this run: {', '.join(unused) or 'none'}.\n"
        f"Hidden by this DAG now: {', '.join(spec.hidden_tools) or 'none'}.\n"
        f"Tools that ask first: {', '.join(asks_first) or 'none'}. The DAG lets these run without asking: "
        f"{', '.join(spec.allow) or 'none'}.\n\n"
        "Recommend what this DAG should hide (tools it's unlikely to need), show again (hidden ones it needed), "
        "let run without asking (ones the user allowed and would want to again, that only read or are low-risk) "
        "and ask about again. Reply with a ```json block like "
        '{"hide": [], "show": [], "allow": [], "ask": [], "why": "..."}, '
        f"or just {NO_CHANGES}."
    )


def proposed_tools(answer: str, known: Sequence[str], asks_first: Sequence[str], never: Sequence[str]) -> tuple[
    dict[str, list[str]], str
] | None:  # fmt: skip
    """The changes the tool review proposes, and why, keeping only those that can be made: None if there are
    none."""
    if NO_CHANGES in answer.upper() and "```" not in answer:
        return None
    blocks = [body for lang, body in _FENCE.findall(answer) if lang.lower() in ("json", "")]
    try:
        said = json.loads(blocks[-1]) if blocks else None
    except ValueError:
        said = None
    if not isinstance(said, dict):
        return None
    changes: dict[str, list[str]] = {}
    for key in ("hide", "show", "allow", "ask"):
        names = said.get(key) or []
        if not isinstance(names, list):
            continue
        valid = [n for n in dict.fromkeys(str(n) for n in names) if n in known]
        if key == "allow":  # only tools that ask first, and never those that always must
            valid = [n for n in valid if n in asks_first and n not in never]
        if valid:
            changes[key] = valid
    if not changes:
        return None
    return changes, str(said.get("why") or "").strip()


def tool_lists(spec: DagSpec, changes: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """The DAG's hidden_tools and allow once ``changes`` are made."""
    hidden = [t for t in spec.hidden_tools if t not in changes.get("show", [])]
    hidden += [t for t in changes.get("hide", []) if t not in hidden]
    allow = [t for t in spec.allow if t not in changes.get("ask", [])]
    allow += [t for t in changes.get("allow", []) if t not in allow]
    return hidden, allow


def describe_tools(changes: dict[str, list[str]]) -> str:
    said = {"hide": "Hide", "show": "Show again", "allow": "Run without asking", "ask": "Ask first again"}
    return "; ".join(f"{said[k]}: {', '.join(v)}" for k, v in changes.items() if v)


# -- saving context, and the summary ---------------------------------------------------------------------


def save_context(kept: str, user: str) -> str:
    return (
        "(From the system.) Decide what this DAG's next run should know, which starts afresh with only what you "
        "keep now and the agent's guidance. Keep what will matter again, such as what "
        f"{user} wants and has told you, what's been found or done already so it isn't done twice, and links "
        "or details the next run will need; not what was only needed on the way, such as searches and pages read. "
        f"This is what's kept now:\n<<<\n{kept.strip() or '(nothing yet)'}\n>>>\n"
        f"Reply with everything to keep, as the next run should read it (it replaces what's kept now), in at most "
        f"{CONTEXT_WORDS} words. If nothing should change, reply with just: {UNCHANGED}"
    )


def kept(answer: str) -> str | None:
    """What save_context keeps: None to keep what's kept already."""
    text = answer.strip()
    if not text or text.upper().rstrip(".") == UNCHANGED:
        return None
    words = text.split()
    if len(words) > CONTEXT_WORDS * 1.25:  # well over what it was asked for
        text = " ".join(words[: int(CONTEXT_WORDS * 1.25)]) + " …"
    return text


def summary(user: str, recommendations: Sequence[str], failed: Sequence[str]) -> str:
    recommended = (
        "These recommendations are waiting for them to approve or reject, so mention each briefly:\n"
        + "\n".join(f"- {r}" for r in recommendations)
        if recommendations
        else "There's nothing to recommend this time."
    )
    went_wrong = f" These tasks didn't finish: {', '.join(failed)}." if failed else ""
    return (
        f"(From the system.) Write {user} a short summary of this run, for the agent's chat: what it did, what it "
        f"found that's new, and what went wrong if anything.{went_wrong} {recommended} Lead with what matters most. "
        f"If the run found nothing new{', and there are no recommendations,' if not recommendations else ''} start "
        f"your reply with {QUIET}, so {user} isn't notified."
    )


def is_quiet(answer: str) -> tuple[bool, str]:
    """Whether the summary says there's nothing new, and the summary without saying so."""
    text = answer.strip()
    if text.lower().startswith(QUIET):
        return True, text[len(QUIET) :].strip()
    return False, text
