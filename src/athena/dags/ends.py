"""The tasks every DAG ends with (spec.END_TASKS): what each is asked, and how its answer's read.

They run after the DAG's own tasks, in the same conversation, so the model has the whole run in front of it and
the server's cache of it holds. None of them needs a tool but the skill review: the workflow review answers with
a whole new DAG file, the tool review with a little JSON, saving context with what to keep, and the summary with
the summary. A review with nothing to look at, such as the tool review of a run that used no tools, is skipped
without asking the model anything.

Also here: reading a task's result from a ```json block in its answer, when it gave it there rather than with
task_result.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from athena.dags.spec import BOOL, LIST, MAX_ITEMS, REFERENCE, DagError, DagSpec, parse

NO_CHANGES = "NO CHANGES"
UNCHANGED = "UNCHANGED"
QUIET = "[quiet]"
CONTEXT_WORDS = 1200  # the most save_context keeps
ITEM_CHARS = 4000  # of an item in a list a task returns
_FENCE = re.compile(r"```(\w*)[ \t]*\n(.*?)```", re.S)


@dataclass
class ToolUse:
    """How a run used one tool."""

    calls: int = 0
    failed: int = 0
    declined: int = 0
    asked: int = 0  # times you were asked
    allowed: int = 0  # times it ran without asking, as it may here
    tasks: dict[str, int] = field(default_factory=dict)  # calls by task


@dataclass
class Uses:
    """How a run used its tools, from its tasks' steps."""

    tools: dict[str, ToolUse] = field(default_factory=dict)

    @classmethod
    def of(cls, steps: Sequence[tuple[str, dict[str, str]]]) -> Uses:
        """From (task, step) for each step its tasks took."""
        uses = cls()
        for task, step in steps:
            use = uses.tools.setdefault(step["tool"], ToolUse())
            use.calls += 1
            use.failed += step.get("outcome") == "failed"
            use.declined += step.get("outcome") == "declined"
            use.asked += step.get("approval") == "you"
            use.allowed += step.get("approval") == "allowed"
            use.tasks[task] = use.tasks.get(task, 0) + 1
        return uses


# -- the workflow review -------------------------------------------------------------------------------


def workflow(source: str) -> str:
    return (
        "(From the system: the run's own tasks have finished.) Look back at how well this DAG did what it's for: "
        "a task that's missing, unnecessary or in the wrong place, prompts that were unclear, work that should run "
        "once for each item of a list, or only when a yes or no says so, tools a task should be allowed to use "
        "without asking, or anything that made the run slower or worse than it needed to be. This is the DAG file "
        f"now:\n```python\n{source.strip()}\n```\n{REFERENCE}\n\n"
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
    """What the tool review is asked: how each tool was used, by which task, and which tools are where now."""
    lines = []
    for name, use in sorted(uses.tools.items(), key=lambda kv: -kv[1].calls):
        by = ", ".join(f"{task} {n}" for task, n in use.tasks.items())
        parts = [f"{use.calls} call{'s' if use.calls != 1 else ''} ({by})"]
        for count, said in ((use.failed, "failed"), (use.declined, "declined by the user"),
                            (use.asked, "asked the user first"), (use.allowed, "ran without asking")):  # fmt: skip
            if count:
                parts.append(f"{count} {said}")
        lines.append(f"- {name}: {', '.join(parts)}")
    unused = [name for name in in_prompt if name not in uses.tools]
    allowed = [f"in every task: {', '.join(spec.allowed_tools)}"] if spec.allowed_tools else []
    allowed += [f"in the group {g.id}: {', '.join(g.allowed_tools)}" for g in spec.groups if g.allowed_tools]
    allowed += [f"in {t.id}: {', '.join(t.allowed_tools)}" for t in spec.tasks if t.allowed_tools]
    task = spec.tasks[0].id if spec.tasks else "find_jobs"
    return (
        "(From the system.) Look at the tools this run used, to make the next run quicker and smoother:\n"
        + "\n".join(lines)
        + "\n\nSome tools are in every prompt, which makes every request longer, and some are hidden: found with "
        "find_tools when they're needed. Some ask the user first, every time, unless the DAG's allowed_tools let "
        "them run without asking: in every task, or in one.\n"
        f"In the prompt now, and not used in this run: {', '.join(unused) or 'none'}.\n"
        f"Hidden by this DAG now: {', '.join(spec.hidden_tools) or 'none'}.\n"
        f"Tools that ask first: {', '.join(asks_first) or 'none'}. Allowed without asking now: "
        f"{'; '.join(allowed) or 'none'}.\n\n"
        "Recommend what this DAG should hide (tools it's unlikely to need), show again (hidden ones it needed), "
        "let run without asking (ones the user allowed and would want to again, that only read or are low-risk), "
        'in every task ("dag") or only in the task that needs it, and ask about again. Reply with a ```json '
        f'block like {{"hide": [], "show": [], "allow": {{"{task}": ["tool_name"]}}, "ask": {{"dag": []}}, '
        f'"why": "..."}}, or just {NO_CHANGES}.'
    )


def proposed_tools(
    answer: str, known: Sequence[str], asks_first: Sequence[str], never: Sequence[str], tasks: Sequence[str]
) -> tuple[dict[str, Any], str] | None:
    """The changes the tool review proposes, and why, keeping only those that can be made: None if there are
    none. {"hide": [tools], "show": [tools], "allow": {where: [tools]}, "ask": {where: [tools]}}, where is "dag"
    for every task, or one of ``tasks``."""
    if NO_CHANGES in answer.upper() and "```" not in answer:
        return None
    blocks = [body for lang, body in _FENCE.findall(answer) if lang.lower() in ("json", "")]
    try:
        said = json.loads(blocks[-1]) if blocks else None
    except ValueError:
        said = None
    if not isinstance(said, dict):
        return None
    changes: dict[str, Any] = {}
    for key in ("hide", "show"):
        names = said.get(key) or []
        if isinstance(names, list) and (valid := [n for n in dict.fromkeys(str(n) for n in names) if n in known]):
            changes[key] = valid
    for key in ("allow", "ask"):
        places = said.get(key) or {}
        if isinstance(places, list):  # just names: the DAG's own
            places = {"dag": places}
        if not isinstance(places, dict):
            continue
        where: dict[str, list[str]] = {}
        for place, names in places.items():
            if str(place) not in ("dag", *tasks) or not isinstance(names, list):
                continue
            valid = [n for n in dict.fromkeys(str(n) for n in names) if n in known]
            if key == "allow":  # only tools that ask first, and never those that always must
                valid = [n for n in valid if n in asks_first and n not in never]
            if valid:
                where[str(place)] = valid
        if where:
            changes[key] = where
    if not changes:
        return None
    return changes, str(said.get("why") or "").strip()


def tool_lists(spec: DagSpec, changes: dict[str, Any]) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """The DAG's hidden_tools and allowed_tools once ``changes`` are made, and those of each task they change."""
    hidden = [t for t in spec.hidden_tools if t not in changes.get("show", [])]
    hidden += [t for t in changes.get("hide", []) if t not in hidden]
    allow, ask = changes.get("allow") or {}, changes.get("ask") or {}

    def changed(now: tuple[str, ...], where: str) -> list[str]:
        kept = [t for t in now if t not in ask.get(where, [])]
        return kept + [t for t in allow.get(where, []) if t not in kept]

    tasks = {where: changed(task.allowed_tools, where) for where in {*allow, *ask} if (task := spec.task(where))}
    return hidden, changed(spec.allowed_tools, "dag"), tasks


def describe_tools(changes: dict[str, Any]) -> str:
    parts = [f"{said}: {', '.join(changes[key])}" for key, said in (("hide", "Hide"), ("show", "Show again"))
             if changes.get(key)]  # fmt: skip
    for key, said in (("allow", "Run without asking"), ("ask", "Ask first again")):
        for where, names in (changes.get(key) or {}).items():
            parts.append(f"{said} {'in every task' if where == 'dag' else f'in {where}'}: {', '.join(names)}")
    return "; ".join(parts)


# -- a task's result, given in its answer --------------------------------------------------------------


def json_value(answer: str, returns: str) -> Any:
    """The list, or true or false, in a ```json block of a task's answer: None if there isn't one that will do."""
    for lang, body in reversed(_FENCE.findall(answer)):
        if lang.lower() not in ("json", ""):
            continue
        try:
            value = json.loads(body)
        except ValueError:
            continue
        if isinstance(value, dict):
            value = value.get("items" if returns == LIST else "answer")
        if returns == LIST and isinstance(value, list) and len(value) <= MAX_ITEMS:
            return [text for text in (item_text(v) for v in value) if text]
        if returns == BOOL and isinstance(value, bool):
            return value
    return None


def without_json(answer: str) -> str:
    """An answer, without its ```json blocks."""
    return " ".join(_FENCE.sub(" ", answer).split())


def item_text(value: Any) -> str:
    """An item of a list a task returned, as text."""
    text = value.strip() if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= ITEM_CHARS else text[:ITEM_CHARS] + " [cut short]"


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
