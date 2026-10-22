"""The DAG language: Airflow-style Python that says what an agent does, which Athena reads but never runs.

    from athena import DAG, Task

    with DAG(
        schedule="0 9 * * 1-5",          # cron, in your time zone; or None
        triggers=["watch:job-emails"],   # events that start it too, such as "arrive-home" or "claude-done"
        hidden_tools=["news_*"],         # tools it finds when it needs them, rather than in every prompt
        allow=["fetch"],                 # tools that ask first which every task may use without asking
    ):
        find = Task(
            task_id="find_jobs",         # the variable's name if it's not given
            prompt="Find new jobs that fit my CV, and trigger apply once for each good one.",
            allow=["browser_*"],
        )
        apply = Task(
            task_id="apply",
            prompt="Apply to this job: {input}",
            triggered=True,              # runs once each time a task upstream triggers it, with what it gives
            alert=True,                  # what it says goes to the agent's chat, and to you
        )
        find >> apply                    # apply comes after find; [a, b] >> c, a << b and chains work too

The file is parsed (ast), never executed, so a change Athena proposes can't run code on the Pi: anything but
imports, one `with DAG(...)` block, tasks assigned to names, and `>>` or `<<` between them is refused, and every
argument must be written out as it is (strings, lists of strings, True, False or None). There are no loops or
conditions in the file: what happens differently each time is up to the tasks, which can trigger others.

Every DAG ends with the same tasks, which aren't written in the file (END_TASKS): they look back at the run and
propose changes, keep what's worth keeping for next time, and tell you how it went.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from athena.cron import Cron, CronError

EVENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # an event's name, such as arrive-home: what /hooks/<name> fires
WATCH = "watch:"  # a trigger such as "watch:job-emails": when what's watched as job-emails happens (watching.py)
CLAUDE_DONE = "claude-done"  # the event when Claude finishes a project handed to it (claude_projects.py)
TASK_ID = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
MAX_TASKS = 30
MAX_PROMPT = 4000  # characters
MAX_SOURCE = 40_000

# The tasks every DAG ends with, in order: (id, what it does). They run after all the others, however those went.
END_TASKS = (
    ("review_workflow", "Looks back at how well the run did what it's for, and proposes changes to its tasks"),
    ("review_tools", "Proposes which tools to keep out of the prompt, and which may run without asking"),
    ("review_skills", "Proposes improvements to the skills the run used"),
    ("save_context", "Keeps what the next run should know"),
    ("summary", "Tells you what the run did and found, and what it recommends"),
)
END_IDS = frozenset(task_id for task_id, _ in END_TASKS)

_DAG_ARGS = {"schedule", "triggers", "hidden_tools", "allow", "dag_id", "description"}
_TASK_ARGS = {"task_id", "prompt", "allow", "alert", "triggered"}

STARTER = '''from athena import DAG, Task

# What this agent does by itself. Give it a schedule, or triggers, to run on its own, and press Run to try it.
with DAG(schedule=None):
    check = Task(
        task_id="check",
        prompt="""Check on what this agent is for, and say what's new.""",
    )
'''


class DagError(ValueError):
    """A DAG file that can't be used: what's wrong, and where."""

    def __init__(self, message: str, line: int = 0, column: int = 0):
        super().__init__(f"Line {line}: {message}" if line else message)
        self.message, self.line, self.column = message, line, column

    def as_dict(self) -> dict[str, Any]:
        return {"message": self.message, "line": self.line, "column": self.column}


@dataclass(frozen=True)
class TaskSpec:
    id: str
    prompt: str
    allow: tuple[str, ...] = ()  # tools that ask first which this task may use without asking
    alert: bool = False
    triggered: bool = False
    line: int = 0
    upstream: tuple[str, ...] = ()  # the tasks it comes after, in the order they're written

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "allow": list(self.allow),
            "alert": self.alert,
            "triggered": self.triggered,
            "line": self.line,
            "upstream": list(self.upstream),
        }


@dataclass(frozen=True)
class DagSpec:
    schedule: Cron | None = None
    triggers: tuple[str, ...] = ()
    hidden_tools: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    tasks: tuple[TaskSpec, ...] = ()  # in an order where each comes after those it depends on
    _by_id: dict[str, TaskSpec] = field(default_factory=dict, repr=False, compare=False)

    def task(self, task_id: str) -> TaskSpec | None:
        return self._by_id.get(task_id)

    def downstream(self, task_id: str) -> list[TaskSpec]:
        return [t for t in self.tasks if task_id in t.upstream]

    @property
    def edges(self) -> list[tuple[str, str]]:
        return [(up, t.id) for t in self.tasks for up in t.upstream]

    @property
    def events(self) -> tuple[str, ...]:
        """The events that start it, besides its schedule: not what's watched."""
        return tuple(t for t in self.triggers if not t.startswith(WATCH))

    @property
    def watches(self) -> tuple[str, ...]:
        """The keys of what's watched that starts it when it happens."""
        return tuple(t.removeprefix(WATCH) for t in self.triggers if t.startswith(WATCH))

    def describe(self) -> str:
        """When it runs, in words."""
        parts = [self.schedule.describe()] if self.schedule else []
        parts += [f"when “{t.removeprefix(WATCH)}” happens" if t.startswith(WATCH) else f"when “{t}” comes in"
                  for t in self.triggers]  # fmt: skip
        if not parts:
            return "only when you run it"
        return ", and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + ", and " + parts[-1]

    def outline(self) -> str:
        """The tasks, and what each comes after, in a few lines for the model."""
        lines = []
        for t in self.tasks:
            after = f" (after {', '.join(t.upstream)})" if t.upstream else ""
            how = " Runs once for each time a task before it triggers it." if t.triggered else ""
            lines.append(f"- {t.id}{after}:{how} {_first_line(t.prompt)}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule.text if self.schedule else None,
            "when": self.describe(),
            "triggers": list(self.triggers),
            "hidden_tools": list(self.hidden_tools),
            "allow": list(self.allow),
            "tasks": [t.as_dict() for t in self.tasks],
            "edges": self.edges,
            "end": [{"id": task_id, "about": about} for task_id, about in END_TASKS],
        }


# -- reading it ------------------------------------------------------------------------------------


def parse(source: str) -> DagSpec:
    """The DAG a file describes. DagError, saying what and where, if it won't do."""
    if len(source) > MAX_SOURCE:
        raise DagError(f"That's over {MAX_SOURCE:,} characters: a DAG should be much shorter.")
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise DagError(f"That isn't valid Python: {exc.msg}.", exc.lineno or 0, exc.offset or 0) from None
    block = None
    for statement in module.body:
        if isinstance(statement, ast.Import | ast.ImportFrom) or _is_docstring(statement):
            continue
        if isinstance(statement, ast.With) and block is None and _dag_call(statement):
            block = statement
            continue
        raise _error(statement, "Outside the DAG, only imports go here: put everything in one `with DAG(...):` block.")
    if block is None:
        raise DagError("There's no `with DAG(...):` block.", 1)
    dag_args = _arguments(_dag_call(block), _DAG_ARGS, "DAG")  # type: ignore[arg-type]
    tasks: dict[str, dict[str, Any]] = {}  # by the name they're assigned to
    ids: dict[str, str] = {}  # variable name -> task_id
    edges: list[tuple[str, str]] = []
    for statement in block.body:
        if _is_docstring(statement) or isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Assign) and _task_call(statement.value):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                raise _error(statement, "Assign each task to one name, like `find = Task(...)`.")
            name = statement.targets[0].id
            if name in tasks:
                raise _error(statement, f"“{name}” is already a task: give each its own name.")
            args = _arguments(statement.value, _TASK_ARGS, "Task")  # type: ignore[arg-type]
            args["line"] = statement.lineno
            tasks[name] = args
            ids[name] = args.get("task_id", name)
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.BinOp):
            _chain(statement.value, tasks, edges)
            continue
        raise _error(
            statement, "Inside the DAG, write tasks (`name = Task(...)`) and what comes after what (`a >> b`)."
        )
    return _build(dag_args, tasks, ids, edges, block.lineno)


def _dag_call(statement: ast.With) -> ast.Call | None:
    if len(statement.items) != 1:
        return None
    call = statement.items[0].context_expr
    return call if isinstance(call, ast.Call) and _called(call) == "DAG" else None


def _task_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _called(node) == "Task"


def _called(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _is_docstring(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)


def _error(node: ast.AST, message: str) -> DagError:
    return DagError(message, getattr(node, "lineno", 0), getattr(node, "col_offset", 0) + 1)


def _arguments(call: ast.Call, allowed: set[str], what: str) -> dict[str, Any]:
    """A call's keyword arguments, each written out as it is."""
    if call.args:
        raise _error(call, f"Give {what}'s arguments by name, like {what}(prompt=...).")
    args: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg not in allowed:
            known = ", ".join(sorted(allowed - {"dag_id", "description"}))
            raise _error(keyword.value, f"{what} takes {known}, not {keyword.arg or '**'}.")
        try:
            args[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError:
            raise _error(
                keyword.value, f"Write {keyword.arg} out as it is: a string, a list of strings, True, False or None."
            ) from None
    return args


def _chain(node: ast.expr, tasks: dict[str, Any], edges: list[tuple[str, str]]) -> list[str]:
    """The tasks at the right of `a >> b`, `a << b` or a chain of them, noting what comes after what."""
    if isinstance(node, ast.Name):
        if node.id not in tasks:
            raise _error(node, f"There's no task called “{node.id}”: assign it before using it.")
        return [node.id]
    if isinstance(node, ast.List | ast.Tuple):
        return [name for item in node.elts for name in _chain(item, tasks, edges)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.RShift | ast.LShift):
        left, right = _chain(node.left, tasks, edges), _chain(node.right, tasks, edges)
        before, after = (left, right) if isinstance(node.op, ast.RShift) else (right, left)
        edges.extend((a, b) for a in before for b in after)
        return right
    raise _error(node, "Only tasks, lists of them, >> and << go here, like `[a, b] >> c`.")


def _strings(value: Any, what: str, line: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) and v.strip() for v in value):
        raise DagError(f'{what} should be a list of names, like ["fetch"].', line)
    return tuple(dict.fromkeys(v.strip() for v in value))


def _build(
    dag: dict[str, Any], tasks: dict[str, dict[str, Any]], ids: dict[str, str], edges: list[tuple[str, str]], line: int
) -> DagSpec:
    schedule = dag.get("schedule")
    cron = None
    if schedule is not None:
        if not isinstance(schedule, str):
            raise DagError('The schedule is cron, like "30 7 * * 1-5", or None.', line)
        try:
            cron = Cron.parse(schedule)
        except CronError as exc:
            raise DagError(f"The schedule {schedule!r} won't do: {exc}", line) from None
    triggers = _strings(dag.get("triggers"), "triggers", line)
    for trigger in triggers:
        if not EVENT.match(trigger.removeprefix(WATCH)):
            raise DagError(
                f'“{trigger}” isn\'t a trigger: name an event in lower case with hyphens, like "arrive-home", '
                'or what\'s watched, like "watch:job-emails".',
                line,
            )
    if not tasks:
        raise DagError('The DAG has no tasks: add one, like `check = Task(prompt="...")`.', line)
    if len(tasks) > MAX_TASKS:
        raise DagError(f"That's {len(tasks)} tasks: a DAG can have up to {MAX_TASKS}.", line)
    seen: dict[str, str] = {}
    for name, args in tasks.items():
        task_id, at = ids[name], args["line"]
        if not isinstance(task_id, str) or not TASK_ID.match(task_id):
            raise DagError(f"task_id {task_id!r} should be lower case letters, digits and _, like find_jobs.", at)
        if task_id in END_IDS:
            raise DagError(f"“{task_id}” is one of the tasks every DAG ends with: call yours something else.", at)
        if task_id in seen:
            raise DagError(f"Two tasks are called “{task_id}”.", at)
        seen[task_id] = name
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DagError(f"“{task_id}” needs a prompt: what to do.", at)
        if len(prompt) > MAX_PROMPT:
            raise DagError(f"“{task_id}”'s prompt is over {MAX_PROMPT:,} characters.", at)
        for flag in ("alert", "triggered"):
            if not isinstance(args.get(flag, False), bool):
                raise DagError(f"{flag} is True or False.", at)
    upstream: dict[str, list[str]] = {ids[name]: [] for name in tasks}
    for before, after in edges:
        a, b = ids[before], ids[after]
        if a == b:
            raise DagError(f"“{a}” can't come after itself.", tasks[after]["line"])
        if a not in upstream[b]:
            upstream[b].append(a)
    specs = {
        ids[name]: TaskSpec(
            id=ids[name],
            prompt=inspect.cleandoc(args["prompt"]),
            allow=_strings(args.get("allow"), "allow", args["line"]),
            alert=args.get("alert", False),
            triggered=args.get("triggered", False),
            line=args["line"],
            upstream=tuple(upstream[ids[name]]),
        )
        for name, args in tasks.items()
    }
    for task in specs.values():
        if task.triggered and not task.upstream:
            raise DagError(f"“{task.id}” is triggered, so it needs a task before it to trigger it.", task.line)
    ordered = _in_order(specs)
    return DagSpec(
        schedule=cron,
        triggers=triggers,
        hidden_tools=_strings(dag.get("hidden_tools"), "hidden_tools", line),
        allow=_strings(dag.get("allow"), "allow", line),
        tasks=ordered,
        _by_id={t.id: t for t in ordered},
    )


def _in_order(specs: dict[str, TaskSpec]) -> tuple[TaskSpec, ...]:
    """The tasks so that each comes after those it depends on, otherwise in the order they're written (Kahn's
    algorithm). DagError if they go round in a circle."""
    waiting = {task_id: len(task.upstream) for task_id, task in specs.items()}
    after: dict[str, list[str]] = {task_id: [] for task_id in specs}
    for task in specs.values():
        for up in task.upstream:
            after[up].append(task.id)
    position = {task_id: i for i, task_id in enumerate(specs)}
    ready = deque(sorted((t for t, n in waiting.items() if n == 0), key=position.__getitem__))
    ordered: list[TaskSpec] = []
    while ready:
        task_id = ready.popleft()
        ordered.append(specs[task_id])
        freed = []
        for down in after[task_id]:
            waiting[down] -= 1
            if waiting[down] == 0:
                freed.append(down)
        # Keep the order they're written in, among those that are ready.
        ready = deque(sorted([*ready, *freed], key=position.__getitem__))
    if len(ordered) < len(specs):
        stuck = sorted((t for t, n in waiting.items() if n), key=position.__getitem__)
        raise DagError(f"These go round in a circle: {', '.join(stuck)}.", specs[stuck[0]].line)
    return tuple(ordered)


def _first_line(text: str, limit: int = 160) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


# -- changing it -----------------------------------------------------------------------------------


def with_tool_lists(source: str, hidden_tools: list[str], allow: list[str]) -> str:
    """The file with the DAG's hidden_tools and allow set to these, and everything else as it was written."""
    module = ast.parse(source)
    block = next(s for s in module.body if isinstance(s, ast.With) and _dag_call(s))
    call = _dag_call(block)
    assert call is not None
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def at(line: int, column: int) -> int:
        """Where a node starts or ends in the text: ast counts columns in UTF-8 bytes."""
        return starts[line - 1] + len(lines[line - 1].encode()[:column].decode(errors="ignore"))

    changes: list[tuple[int, int, str]] = []
    written = {k.arg: k.value for k in call.keywords}
    adding = []
    for name, value in (("hidden_tools", hidden_tools), ("allow", allow)):
        literal = _literal(sorted(dict.fromkeys(value)))
        if name in written:
            node = written[name]
            changes.append((at(node.lineno, node.col_offset), at(node.end_lineno, node.end_col_offset), literal))  # type: ignore[arg-type]
        elif value:
            adding.append(f"{name}={literal}")
    if adding:
        if call.keywords:
            last = call.keywords[-1].value
            where = at(last.end_lineno, last.end_col_offset)  # type: ignore[arg-type]
            if call.lineno == call.end_lineno:
                text = "".join(f", {arg}" for arg in adding)
            else:  # one argument to a line, as they're written
                line = lines[call.keywords[-1].value.lineno - 1]
                indent = line[: len(line) - len(line.lstrip())]
                text = "".join(f",\n{indent}{arg}" for arg in adding)
        else:
            where = at(call.end_lineno, call.end_col_offset) - 1  # type: ignore[arg-type]  # before the )
            text = ", ".join(adding)
        changes.append((where, where, text))
    for begin, finish, text in sorted(changes, reverse=True):
        source = source[:begin] + text + source[finish:]
    parse(source)  # it still reads
    return source


def unknown_tools(spec: DagSpec, known: list[str]) -> list[str]:
    """The tool names and patterns in a DAG that match no tool Athena has now, such as one from an MCP server
    that's switched off."""
    from athena.tools import matches_any

    named = [*spec.hidden_tools, *spec.allow, *(name for t in spec.tasks for name in t.allow)]
    return [p for p in dict.fromkeys(named) if not any(matches_any(tool, [p]) for tool in known)]


def render(spec_args: dict[str, Any], tasks: list[dict[str, Any]], comments: Sequence[str] = ()) -> str:
    """A DAG file from its parts: {"schedule", "triggers", "allow"} and tasks as {"id", "prompt", "allow",
    "after": [ids]}, for a DAG made rather than written, such as from tasks of before."""
    out = ["from athena import DAG, Task", ""]
    out += [f"# {line}" for line in comments]
    args = [f"schedule={_literal(spec_args.get('schedule'))}"]
    for key in ("triggers", "allow"):
        if spec_args.get(key):
            args.append(f"{key}={_literal(list(spec_args[key]))}")
    out.append(f"with DAG({', '.join(args)}):")
    names = {}
    for task in tasks:
        names[task["id"]] = task["id"]
        out.append(f"    {task['id']} = Task(")
        out.append(f"        task_id={_literal(task['id'])},")
        out.append(f"        prompt={_prompt(task['prompt'])},")
        if task.get("allow"):
            out.append(f"        allow={_literal(list(task['allow']))},")
        out.append("    )")
    for task in tasks:
        for before in task.get("after", []):
            out.append(f"    {names[before]} >> {names[task['id']]}")
    source = "\n".join(out) + "\n"
    parse(source)
    return source


def _literal(value: Any) -> str:
    return repr(value).replace("'", '"') if not isinstance(value, str) else _string(value)


def _string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _prompt(text: str) -> str:
    """A prompt as it reads best: on its own lines, if it has more than one and nothing that would trouble that."""
    if "\n" in text and '"""' not in text and "\\" not in text and not text.endswith('"'):
        indented = "\n".join(f"            {line}" if line else "" for line in text.splitlines())
        return f'"""\n{indented}\n        """'
    return _string(text)
