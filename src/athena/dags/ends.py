"""The tasks every DAG ends with: Athena's own, in place of agentic-airflow's DagEvaluator.

  review_workflow  looks back at how well the run did what it's for, and can propose a new DAG file
  review_tools     looks at the tools the run used, and can propose which to hide, and which may run unasked
  review_skills    looks at the skills it read, and can suggest improvements, for you to approve
  save_context     keeps what the next run should know: the agent's context
  summary          tells you what the run did and found, and what it recommends: you're notified only when
                   there's news, or something to approve

They're the task group "end", after the DAG's last tasks, one after another, whatever happened before them, each
given the run's log. A run that found nothing skips them all (agentic_airflow.found_nothing), so checking for
something often costs little; the tool review is skipped when no tools were used, and the skill review when no
skills were read. Their prompts are written as each starts, from how the run went, and what each answers is
checked: a DAG file that won't read, or tools that won't do, are sent back, once, saying why.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentic_airflow import AgenticTask, DagRun, OutputValidationError, TaskGroup, TaskStatus, found_nothing

from athena.dags.reader import END_GROUP, REFERENCE, DagError, read
from athena.dags.store import AgentStore
from athena.skills import READ_SKILL, REVIEW, SAVE_SKILL

NO_CHANGES = "NO CHANGES"
UNCHANGED = "UNCHANGED"
QUIET = "[quiet]"
CONTEXT_WORDS = 1200  # the most save_context keeps
SKILL_STEPS = 4  # tool calls the skill review may make
_FENCE = re.compile(r"```(\w*)[ \t]*\n(.*?)```", re.S)


@dataclass
class RunNotes:
    """What a run's tasks noted as they went, and what its reviews recommend, for the reviews after them."""

    skills: list[str] = field(default_factory=list)  # read in the run
    suggested: list[str] = field(default_factory=list)  # "memory" or "skill", for each its tasks suggested
    recommendations: list[str] = field(default_factory=list)  # what the reviews proposed, for the summary


class Ends(Protocol):
    """What the end tasks need of the service that runs agents' DAGs (service.Dags)."""

    store: AgentStore
    timezone: str

    @property
    def user(self) -> str: ...

    def agent_of(self, run: DagRun) -> int: ...

    def notes(self, run: DagRun) -> RunNotes: ...

    def tool_names(self) -> tuple[list[str], list[str], list[str]]: ...  # (all, ask first, always ask)

    async def told(self, run: DagRun, text: str, quiet: bool) -> None: ...


def group(source: str, task_ids: Sequence[str], ends: Ends) -> TaskGroup:
    """The end tasks for a DAG with this file and these tasks of its own, as the engine attaches them
    (Engine.register's evaluator)."""
    tasks = TaskGroup(END_GROUP, allowed_tools=[], max_tool_steps=0, dag=None, parent=None)
    tasks.dag = None

    def skip(why_else: Any = None) -> Any:
        def check(engine: Any, run: DagRun) -> str | None:
            return found_nothing(engine, run) or (why_else(run) if why_else else None)

        return check

    def no_tools(run: DagRun) -> str | None:
        return None if _uses(run, ends) else "No tools were used."

    def no_skills(run: DagRun) -> str | None:
        return None if ends.notes(run).skills else "No skills were read."

    common: dict[str, Any] = {
        "context": [],
        "trigger_rule": "all_done",
        "include_run_log": True,
        "group": tasks,
    }
    made = [
        AgenticTask(
            f"{END_GROUP}.review_workflow",
            prompt=lambda engine, run: workflow(source),
            validator=lambda answer: _check_dag(answer, ends.timezone),
            on_complete=lambda engine, run, inst: _proposed_dag(ends, run, source, inst.output),
            skip_if=skip(),
            **common,
        ),
        AgenticTask(
            f"{END_GROUP}.review_tools",
            prompt=lambda engine, run: tools(_uses(run, ends), engine.dag_for(run), ends),
            validator=lambda answer: _check_tools(answer, ends, task_ids),
            on_complete=lambda engine, run, inst: _proposed_tools(ends, run, source, task_ids, inst.output),
            skip_if=skip(no_tools),
            **common,
        ),
        AgenticTask(
            f"{END_GROUP}.review_skills",
            prompt=lambda engine, run: _skills(ends, run),
            allowed_tools=[READ_SKILL, SAVE_SKILL],
            max_tool_steps=SKILL_STEPS,
            skip_if=skip(no_skills),
            **common,
        ),
        AgenticTask(
            f"{END_GROUP}.save_context",
            prompt=lambda engine, run: save_context(ends.store.get(ends.agent_of(run)).context, ends.user),
            on_complete=lambda engine, run, inst: _keep(ends, run, inst.output),
            skip_if=skip(),
            **common,
        ),
        AgenticTask(
            f"{END_GROUP}.summary",
            prompt=lambda engine, run: summary(ends.user, _recommended(ends, run), failures(engine, run)),
            on_complete=lambda engine, run, inst: _summarised(ends, run, inst.output),
            skip_if=skip(),
            **common,
        ),
    ]
    for before, after in zip(made, made[1:], strict=False):
        before >> after
    return tasks


# -- the workflow review -------------------------------------------------------------------------------


def workflow(source: str) -> str:
    return (
        "The run's own tasks have finished: its log is above. Look back at how well this DAG did what it's for: a "
        "task that's missing, unnecessary or in the wrong place, prompts that were unclear, work that should run "
        "once for each item of a list, or only when a branch says so, or anything that made the run slower or "
        f"worse than it needed to be. This is the DAG file now:\n```python\n{source.strip()}\n```\n{REFERENCE}\n\n"
        "If changing it would make it clearly more effective, reply with what you'd change and why, in a few "
        "sentences, then the whole new file in a ```python block. Keep what works, and the same file's layout. If "
        f"it's fine as it is, reply with just: {NO_CHANGES}"
    )


def proposed_dag(answer: str, timezone: str = "UTC") -> tuple[str, str] | None:
    """The DAG file the workflow review proposes, and why: None if it proposes none. DagError if it's one that
    won't do."""
    if NO_CHANGES in answer.upper() and "```" not in answer:
        return None
    code = [body for lang, body in _FENCE.findall(answer) if lang.lower() in ("python", "py", "")]
    if not code:
        return None
    source = code[-1].strip() + "\n"
    read(source, timezone=timezone)
    return source, _FENCE.sub("", answer).strip()


def _check_dag(answer: str, timezone: str) -> None:
    try:
        proposed_dag(answer, timezone)
    except DagError as exc:
        raise OutputValidationError(
            f"That DAG file won't do: {exc}. Reply with the whole corrected file in a ```python block, or just "
            f"{NO_CHANGES}."
        ) from None


def _proposed_dag(ends: Ends, run: DagRun, source: str, answer: str) -> None:
    try:
        proposed = proposed_dag(answer, ends.timezone)
    except DagError:
        return
    if proposed and not same(proposed[0], source):
        new, why = proposed
        agent_id = ends.agent_of(run)
        p = ends.store.propose(agent_id, run.run_id, "workflow", why, source, source=new)
        ends.notes(run).recommendations.append(f"A change to the DAG (proposal #{p.id}): {_short(why, 300)}")


def same(a: str, b: str) -> bool:
    """Whether two DAG files say the same, whatever their spacing."""
    return " ".join(a.split()) == " ".join(b.split())


# -- the tool review -----------------------------------------------------------------------------------


@dataclass
class ToolUse:
    """How a run used one tool."""

    calls: int = 0
    failed: int = 0
    declined: int = 0  # by you
    asked: int = 0  # times you were asked, and allowed it
    tasks: dict[str, int] = field(default_factory=dict)  # calls by task


def _uses(run: DagRun, ends: Ends) -> dict[str, ToolUse]:
    """How the run's own tasks used each tool, from what the engine kept of each call."""
    uses: dict[str, ToolUse] = {}
    for inst in run.instances.values():
        if inst.task_id.startswith(f"{END_GROUP}."):
            continue
        for call in inst.tool_calls:
            if call.get("name") == "search_tools":
                continue
            use = uses.setdefault(str(call.get("name")), ToolUse())
            use.calls += 1
            status = call.get("status")
            use.failed += status == "error"
            use.declined += status == "rejected"
            use.asked += status == "approved"
            use.tasks[inst.task_id] = use.tasks.get(inst.task_id, 0) + 1
    return uses


def tools(uses: dict[str, ToolUse], dag: Any, ends: Ends) -> str:
    """What the tool review is asked: how each tool was used, by which task, and the DAG's tool lists now."""
    _, asks, always = ends.tool_names()
    lines = []
    for name, use in sorted(uses.items(), key=lambda kv: -kv[1].calls):
        by = ", ".join(f"{task} {n}" for task, n in use.tasks.items())
        parts = [f"{use.calls} call{'s' if use.calls != 1 else ''} ({by})"]
        for count, said in ((use.failed, "failed"), (use.declined, "declined by the user"),
                            (use.asked, "asked the user first, who allowed it")):  # fmt: skip
            if count:
                parts.append(f"{count} {said}")
        lines.append(f"- {name}: {', '.join(parts)}")
    lists = [f"the DAG's: {_list(dag.scope.allowed_tools)}"]
    lists += [f"{t.id}'s: {_list(t.scope.allowed_tools)}" for t in dag.own_tasks() if t.scope.allowed_tools is not None]
    first = dag.own_tasks()[0].id if dag.own_tasks() else "find_jobs"
    return (
        "Look at the tools this run used, to make the next run quicker and smoother:\n"
        + "\n".join(lines)
        + "\n\nallowed_tools is the whole list of tools the DAG's tasks may use without asking (patterns such as "
        '"news_*" work): None lets them use any tool, asking the user first for those that ask; a list lets them '
        "use only those, and each other tool they call waits for the user's OK. A task's own list replaces the "
        f"DAG's. allowed_tools now: {'; '.join(lists)}. Tools that ask first: {', '.join(asks) or 'none'}; these "
        f"always ask, whatever allowed_tools says: {', '.join(always) or 'none'}. hidden_tools are found when "
        f"they're needed, rather than in every prompt: {_list(dag.scope.hidden_tools)} now.\n\n"
        "Recommend lists that would make the next run smoother: hide tools it's unlikely to need, and let a task "
        "use without asking the tools the user allowed and would want to again, that only read or are low-risk. "
        'Reply with a ```json block of only the lists to change, whole, like {"hidden_tools": [...], '
        f'"allowed_tools": {{"{first}": ["read_file", "fetch"]}}, "why": "..."}} ("dag" for the DAG\'s own '
        f"allowed_tools), or just {NO_CHANGES}."
    )


def proposed_tools(
    answer: str, known: Sequence[str], always: Sequence[str], tasks: Sequence[str]
) -> tuple[dict[str, Any], str] | None:
    """The tool lists the tool review proposes, and why: None if it proposes none. ValueError, saying why, if they
    won't do. {"hidden_tools": [...], "allowed_tools": {where: [...]}}, where "dag" or one of ``tasks``."""
    if NO_CHANGES in answer.upper() and "```" not in answer:
        return None
    blocks = [body for lang, body in _FENCE.findall(answer) if lang.lower() in ("json", "")]
    try:
        said = json.loads(blocks[-1]) if blocks else None
    except ValueError:
        raise ValueError("The ```json block isn't valid JSON.") from None
    if not isinstance(said, dict):
        raise ValueError('Reply with a ```json block like {"hidden_tools": [...], "why": "..."}.')
    changes: dict[str, Any] = {}

    def names(value: Any, what: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{what} is a list of tool names.")
        if unknown := [v for v in value if not any(_fits(v, k) for k in known)]:
            raise ValueError(f"No tool is called {', '.join(unknown)}.")
        return list(dict.fromkeys(value))

    if "hidden_tools" in said:
        changes["hidden_tools"] = names(said["hidden_tools"], "hidden_tools")
    places = said.get("allowed_tools") or {}
    if not isinstance(places, dict):
        raise ValueError('allowed_tools is {"dag" or a task\'s id: [tools]}.')
    allowed = {}
    for where, value in places.items():
        if where not in ("dag", *tasks):
            raise ValueError(f'There\'s no task called {where!r}: give "dag" or one of {", ".join(tasks)}.')
        allowed[where] = [n for n in names(value, f"allowed_tools for {where}") if n not in always]
    if allowed:
        changes["allowed_tools"] = allowed
    return (changes, str(said.get("why") or "").strip()) if changes else None


def _check_tools(answer: str, ends: Ends, task_ids: Sequence[str]) -> None:
    known, _, always = ends.tool_names()
    try:
        proposed_tools(answer, known, always, task_ids)
    except ValueError as exc:
        raise OutputValidationError(f"{exc} Reply with a corrected ```json block, or just {NO_CHANGES}.") from None


def _proposed_tools(ends: Ends, run: DagRun, source: str, task_ids: Sequence[str], answer: str) -> None:
    known, _, always = ends.tool_names()
    try:
        proposed = proposed_tools(answer, known, always, task_ids)
    except ValueError:  # the tools changed since it was checked
        return
    if proposed:
        changes, why = proposed
        p = ends.store.propose(ends.agent_of(run), run.run_id, "tools", why, source, changes=changes)
        ends.notes(run).recommendations.append(f"Tools (proposal #{p.id}): {describe_tools(changes)}")


def describe_tools(changes: dict[str, Any]) -> str:
    parts = []
    if "hidden_tools" in changes:
        parts.append(f"Find when needed: {_list(changes['hidden_tools'])}")
    for where, names in (changes.get("allowed_tools") or {}).items():
        parts.append(f"Use without asking {'in every task' if where == 'dag' else f'in {where}'}: {_list(names)}")
    return "; ".join(parts)


def _fits(name: str, tool: str) -> bool:
    return fnmatch.fnmatchcase(tool, name)


def _list(names: Sequence[str] | None) -> str:
    return "None" if names is None else ", ".join(names) or "[] (none)"


# -- the skill review -------------------------------------------------------------------------------------


def _skills(ends: Ends, run: DagRun) -> str:
    used = ends.notes(run).skills
    names = " and ".join(f"“{name}”" for name in used)
    return REVIEW.format(user=ends.user, skills=f"your skill{'s' if len(used) > 1 else ''} {names}")


# -- saving context, and the summary ---------------------------------------------------------------------


def save_context(kept: str, user: str) -> str:
    return (
        "Decide what this DAG's next run should know, which starts afresh with only what you keep now and the "
        f"agent's guidance. Keep what will matter again, such as what {user} wants and has told you, what's been "
        "found or done already so it isn't done twice, and links or details the next run will need; not what was "
        "only needed on the way, such as searches and pages read. This is what's kept now:\n"
        f"<<<\n{kept.strip() or '(nothing yet)'}\n>>>\n"
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


def _keep(ends: Ends, run: DagRun, answer: str) -> None:
    if (keep := kept(answer)) is not None:
        ends.store.update(ends.agent_of(run), context=keep)


def summary(user: str, recommendations: Sequence[str], failed: Sequence[str]) -> str:
    recommended = (
        "These recommendations are waiting for them to approve or reject, so mention each briefly:\n"
        + "\n".join(f"- {r}" for r in recommendations)
        if recommendations
        else "There's nothing to recommend this time."
    )
    went_wrong = f" These tasks didn't finish: {', '.join(failed)}." if failed else ""
    return (
        f"Write {user} a short summary of this run, for the agent's chat: what it did, what it found that's new, "
        f"and what went wrong if anything.{went_wrong} {recommended} Lead with what matters most. If the run found "
        f"nothing new{', and there are no recommendations,' if not recommendations else ''} start your reply with "
        f"{QUIET}, so {user} isn't notified."
    )


def is_quiet(answer: str) -> tuple[bool, str]:
    """Whether the summary says there's nothing new, and the summary without saying so."""
    text = answer.strip()
    if text.lower().startswith(QUIET):
        return True, text[len(QUIET) :].strip()
    return False, text


def _recommended(ends: Ends, run: DagRun) -> list[str]:
    notes = ends.notes(run)
    said = suggested(notes.suggested)
    extra = [f"It suggested {said}, to approve or not on the dashboard's Memory and Skills tabs"] if said else []
    return [*notes.recommendations, *extra]


async def _summarised(ends: Ends, run: DagRun, answer: str) -> None:
    quiet, said = is_quiet(answer)
    run.summary = said
    await ends.told(run, said, quiet and not _recommended(ends, run))


def suggested(kinds: list[str]) -> str:
    """What a run suggested for you to approve, such as "2 memories and a skill". Empty if nothing."""
    parts = []
    for kind, plural in (("memory", "memories"), ("skill", "skills")):
        if n := kinds.count(kind):
            parts.append(f"a {kind}" if n == 1 else f"{n} {plural}")
    return " and ".join(parts)


def failures(engine: Any, run: DagRun) -> list[str]:
    """The DAG's own tasks that failed, or weren't run for a failure before them, such as "apply (for 2 of its 5
    items)" for one that failed for some of the items it ran for."""
    failed = []
    for task in engine.dag_for(run).own_tasks():
        insts = run.instances_for(task.id)
        broken = [i for i in insts if i.status in (TaskStatus.FAILED, TaskStatus.UPSTREAM_FAILED)]
        if broken and len(insts) > 1 and len(broken) < len(insts):
            failed.append(f"{task.id} (for {len(broken)} of its {len(insts)} items)")
        elif broken:
            failed.append(task.id)
    return failed


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
