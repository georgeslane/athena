"""The DAG language: Airflow-style Python that says what an agent does by itself, which Athena reads but never runs.

    from athena import DAG, Task, TaskGroup, trigger

    with DAG(
        schedule="0 8 * * *",              # cron, in your time zone; or None
        triggers=["new-email"],            # events that start it too: from /hooks/<name>, Claude, or outlets
        hidden_tools=["news_*"],           # tools it finds when it needs them, rather than in every prompt
        allowed_tools=["read_email"],      # tools that ask first which every task may use without asking
    ):
        emails = Task(
            prompt="Which of my emails since the last run need a reply? One item for each.",
            return_type=list,              # list, for for_each; bool, for run_if; or str, the default
        )
        with TaskGroup("each_email", for_each=emails, allowed_tools=["draft_email"]):
            urgent = Task(prompt="Is this one urgent? {item}", return_type=bool)
            draft = Task(prompt="Draft a reply to: {item}", run_if=urgent)
            later = Task(prompt="Say why it can wait: {item}", run_if=not urgent)
            urgent >> [draft, later]
        report = Task(prompt="Say what was drafted.", alert=True, outlets=["drafted"])
        [draft, later] >> report

The file is parsed (ast), never executed, so a change Athena proposes can't run code on the Pi: anything but
imports, one `with DAG(...)` block, tasks assigned to names, task groups, and `>>` or `<<` between them is
refused. Every argument is written out as it is (strings, lists of strings, True, False or None), except those
that name a task (for_each, run_if) or a type (return_type).

A task with return_type=list or bool gives its answer with the task_result tool, so it's always a list of strings
or a yes or no. for_each runs a task once for each item of a list, and run_if only when a yes or no says so.
Tasks that run for the same items run for each in turn: all of a group's tasks for the first item, then the next.

Every DAG ends with the same tasks, which aren't written in the file (END_TASKS): they look back at the run and
propose changes, keep what's worth keeping for next time, and tell you how it went.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from athena.cron import Cron, CronError

EVENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # an event's name, such as arrive-home: what /hooks/<name> fires
CLAUDE_DONE = "claude-done"  # the event when Claude finishes a project handed to it (claude_projects.py)
TRIGGER = "trigger"  # for_each=trigger: each item that came with what started the run
TASK_ID = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
TEXT, LIST, BOOL = "text", "list", "bool"  # what a task returns
MAX_TASKS = 30
MAX_GROUPS = 10
MAX_ITEMS = 20  # in a list a task returns, or a task runs for, in one run
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

_DAG_ARGS = {"schedule", "triggers", "hidden_tools", "allowed_tools", "dag_id", "description"}
_TASK_ARGS = {"task_id", "prompt", "return_type", "for_each", "run_if", "allowed_tools", "alert", "outlets"}
_GROUP_ARGS = {"group_id", "for_each", "allowed_tools"}
_TYPES = {"str": TEXT, "text": TEXT, "list": LIST, "bool": BOOL}

# How DAG files are written, in brief: for the model, when it changes one.
REFERENCE = """DAG files are Airflow-style Python, read and never run:
from athena import DAG, Task, TaskGroup, trigger
with DAG(schedule="30 7 * * 1-5", triggers=["event-name"], hidden_tools=[...], allowed_tools=[...]):  # or None
    jobs = Task(prompt="...", return_type=list)  # or bool, given with task_result; str is the default
    apply = Task(prompt="Apply to {item}", for_each=jobs)  # once for each item. for_each=trigger: for each item
                                                           # that came with what started the run
    good = Task(prompt="...", return_type=bool)
    tell = Task(prompt="...", run_if=good, alert=True)  # only if good said yes (not good: no). alert tells the user
    with TaskGroup("each_job", for_each=jobs, allowed_tools=[...]):  # its tasks run for each item in turn
        ...
    note = Task(prompt="...", outlets=["event-name"], allowed_tools=["fetch"])  # outlets start DAGs with that
    jobs >> apply >> note                                                      # trigger, with what it found
Triggers are events: /hooks/<name>, "claude-done", or other DAGs' outlets. allowed_tools may run without asking."""

STARTER = """from athena import DAG, Task

# What this agent does by itself. Give it a schedule or triggers to run on its own, and press Run now to try it.
with DAG(schedule=None):
    check = Task(prompt="Check on what this agent is for, and say what's new.")
"""

MASTER_STARTER = """from athena import DAG, Task

# What the master agent keeps an eye on for you. A task's outlets start the DAGs of your other agents that have
# the same name in their triggers, with what it found. Give it a schedule, such as "@hourly", to check by itself.
with DAG(schedule=None):
    job_emails = Task(
        prompt="Look through my emails since the last run for new ones about jobs: one item for each.",
        return_type=list,
        outlets=["job-emails"],  # starts the DAGs with triggers=["job-emails"], with the emails found
    )
"""


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
    returns: str = TEXT  # TEXT, LIST or BOOL
    for_each: str | None = None  # a task's id, or TRIGGER: it runs once for each item of that
    run_if: str | None = None  # a task that returns BOOL: it runs only if that said run_when
    run_when: bool = True
    allowed_tools: tuple[str, ...] = ()  # its own, besides its group's and the DAG's
    alert: bool = False
    outlets: tuple[str, ...] = ()  # events it starts DAGs with, with what it found
    group: str | None = None
    line: int = 0
    upstream: tuple[str, ...] = ()  # the tasks it comes after, with its for_each's and run_if's

    @property
    def mapped(self) -> bool:
        return self.for_each is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "returns": self.returns,
            "for_each": self.for_each,
            "run_if": self.run_if,
            "run_when": self.run_when,
            "allowed_tools": list(self.allowed_tools),
            "alert": self.alert,
            "outlets": list(self.outlets),
            "group": self.group,
            "line": self.line,
            "upstream": list(self.upstream),
        }


@dataclass(frozen=True)
class GroupSpec:
    id: str
    for_each: str | None = None
    allowed_tools: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    line: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "for_each": self.for_each, "allowed_tools": list(self.allowed_tools),
                "tasks": list(self.tasks), "line": self.line}  # fmt: skip


@dataclass(frozen=True)
class DagSpec:
    schedule: Cron | None = None
    triggers: tuple[str, ...] = ()
    hidden_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    tasks: tuple[TaskSpec, ...] = ()  # in an order where each comes after those it depends on
    groups: tuple[GroupSpec, ...] = ()
    _by_id: dict[str, TaskSpec] = field(default_factory=dict, repr=False, compare=False)
    _spaces: dict[str, str | None] = field(default_factory=dict, repr=False, compare=False)

    def task(self, task_id: str) -> TaskSpec | None:
        return self._by_id.get(task_id)

    def group(self, group_id: str | None) -> GroupSpec | None:
        return next((g for g in self.groups if g.id == group_id), None)

    def downstream(self, task_id: str) -> list[TaskSpec]:
        return [t for t in self.tasks if task_id in t.upstream]

    def space(self, task_id: str) -> str | None:
        """The items a task runs for, by what gives them: the list task's id, TRIGGER, or None if it runs once.
        Tasks with the same space run for the same items, and each item's run of one comes after its runs of the
        others it comes after."""
        return self._spaces.get(task_id)

    def allowed(self, task: TaskSpec) -> tuple[str, ...]:
        """The tools, and patterns of them, that may run without asking in a task: the DAG's, its group's and its
        own."""
        group = self.group(task.group)
        return tuple(dict.fromkeys([*self.allowed_tools, *(group.allowed_tools if group else ()), *task.allowed_tools]))

    @property
    def edges(self) -> list[tuple[str, str]]:
        return [(up, t.id) for t in self.tasks for up in t.upstream]

    @property
    def outlets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(event for t in self.tasks for event in t.outlets))

    def describe(self) -> str:
        """When it runs, in words."""
        parts = [self.schedule.describe()] if self.schedule else []
        parts += [f"when “{t}” comes in" for t in self.triggers]
        if not parts:
            return "only when you run it"
        return ", and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + ", and " + parts[-1]

    def outline(self) -> str:
        """The tasks, and how each runs, in a few lines for the model."""
        lines = []
        for t in self.tasks:
            how = []
            if t.upstream:
                how.append(f"after {', '.join(t.upstream)}")
            if t.for_each == TRIGGER:
                how.append("once for each item that came with what started the run")
            elif t.for_each:
                source = self._by_id[t.for_each]
                how.append(f"once for each item {t.for_each} {'returns' if source.returns == LIST else 'ran for'}")
            if t.run_if:
                how.append(f"only if {t.run_if} said {'yes' if t.run_when else 'no'}")
            said = {LIST: " It returns a list.", BOOL: " It returns yes or no."}.get(t.returns, "")
            if t.alert:
                said += " What it finds is sent to the user."
            if t.outlets:
                said += f" What it finds starts {', '.join(t.outlets)}."
            when = f" ({'; '.join(how)})" if how else ""
            lines.append(f"- {t.id}{when}: {_first_line(t.prompt)}{said}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule.text if self.schedule else None,
            "when": self.describe(),
            "triggers": list(self.triggers),
            "hidden_tools": list(self.hidden_tools),
            "allowed_tools": list(self.allowed_tools),
            "tasks": [{**t.as_dict(), "space": self.space(t.id)} for t in self.tasks],
            "groups": [g.as_dict() for g in self.groups],
            "edges": self.edges,
            "outlets": list(self.outlets),
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
    reader = _Reader()
    dag_args = reader.arguments(_dag_call(block), _DAG_ARGS, "DAG")  # type: ignore[arg-type]
    reader.body(block.body, None)
    return _build(dag_args, reader, block.lineno)


@dataclass
class _Reader:
    """What's written in the DAG block: tasks and groups by the names they're assigned to, and edges between those
    names, as they're read."""

    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)  # by name: its `as` name, or its id
    edges: list[tuple[str, str, int]] = field(default_factory=list)  # (name, name, line)

    def body(self, statements: list[ast.stmt], group: str | None) -> None:
        for statement in statements:
            if _is_docstring(statement) or isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.Assign) and _called(statement.value) == "Task":
                self.task(statement, group)
            elif isinstance(statement, ast.With) and _group_call(statement):
                if group is not None:
                    raise _error(statement, "A TaskGroup can't be inside another: put it beside it.")
                self.group(statement)
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.BinOp):
                self.chain(statement.value, statement.lineno)
            else:
                raise _error(
                    statement,
                    "Inside the DAG, write tasks (`name = Task(...)`), task groups (`with TaskGroup(...):`) and what "
                    "comes after what (`a >> b`).",
                )

    def task(self, statement: ast.Assign, group: str | None) -> None:
        if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            raise _error(statement, "Assign each task to one name, like `find = Task(...)`.")
        name = statement.targets[0].id
        if name in self.tasks or name in self.groups:
            raise _error(statement, f"“{name}” is already used: give each task its own name.")
        if name == TRIGGER:
            raise _error(statement, f"“{TRIGGER}” stands for what started the run: call this task something else.")
        args = self.arguments(statement.value, _TASK_ARGS, "Task")  # type: ignore[arg-type]
        args["line"], args["group"] = statement.lineno, group
        self.tasks[name] = args
        if group is not None:
            self.groups[group]["members"].append(name)

    def group(self, statement: ast.With) -> None:
        call = _group_call(statement)
        assert call is not None
        alias = statement.items[0].optional_vars
        if alias is not None and not isinstance(alias, ast.Name):
            raise _error(statement, 'Name the group with one name, like `with TaskGroup("apply") as apply:`.')
        positional = call.args
        if len(positional) > 1 or (positional and not _is_string(positional[0])):
            raise _error(call, 'Give a TaskGroup its id as a string, like TaskGroup("each_job", for_each=jobs).')
        args = self.arguments(ast.Call(call.func, [], call.keywords), _GROUP_ARGS, "TaskGroup")
        group_id = positional[0].value if positional else args.get("group_id", alias.id if alias else None)  # type: ignore[attr-defined]
        if group_id is None:
            raise _error(call, 'Give the TaskGroup an id, like TaskGroup("each_job"), or a name with `as`.')
        name = alias.id if alias else group_id
        if name in self.tasks or name in self.groups or name == TRIGGER:
            raise _error(statement, f"“{name}” is already used: give the group a name of its own.")
        self.groups[name] = {**args, "group_id": group_id, "line": statement.lineno, "members": []}
        self.body(statement.body, name)
        if not self.groups[name]["members"]:
            raise _error(statement, "This TaskGroup has no tasks.")

    def arguments(self, call: ast.Call, allowed: set[str], what: str) -> dict[str, Any]:
        """A call's keyword arguments: each written out as it is, or naming a task or a type."""
        if call.args:
            raise _error(call, f"Give {what}'s arguments by name, like {what}(prompt=...).")
        args: dict[str, Any] = {}
        for keyword in call.keywords:
            if keyword.arg is None or keyword.arg not in allowed:
                known = ", ".join(sorted(allowed - {"dag_id", "description", "group_id"}))
                raise _error(keyword.value, f"{what} takes {known}, not {keyword.arg or '**'}.")
            value = keyword.value
            if keyword.arg == "for_each":
                args["for_each"] = self.named(value, "for_each", trigger=True)
            elif keyword.arg == "run_if":
                negated = isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not)
                args["run_if"] = self.named(value.operand if negated else value, "run_if")  # type: ignore[attr-defined]
                args["run_when"] = not negated
            elif keyword.arg == "return_type":
                written = value.id if isinstance(value, ast.Name) else value.value if _is_string(value) else None  # type: ignore[attr-defined]
                if written not in _TYPES:
                    raise _error(value, "return_type is str, list or bool.")
                args["return_type"] = _TYPES[written]
            else:
                try:
                    args[keyword.arg] = ast.literal_eval(value)
                except ValueError:
                    raise _error(
                        value, f"Write {keyword.arg} out as it is: a string, a list of strings, True, False or None."
                    ) from None
            args.setdefault("lines", {})[keyword.arg] = value.lineno
        return args

    def named(self, node: ast.expr, what: str, trigger: bool = False) -> str:
        """The task a for_each or run_if names, by its name; or TRIGGER."""
        if not isinstance(node, ast.Name):
            also = ", or trigger" if trigger else ""
            raise _error(node, f"{what} names a task, like {what}=find_jobs{also}.")
        if trigger and node.id == TRIGGER:
            return TRIGGER
        if node.id not in self.tasks:
            raise _error(node, f"There's no task called “{node.id}” before this: assign it before using it.")
        return node.id

    def chain(self, node: ast.expr, line: int) -> list[str]:
        """The names at the right of `a >> b`, `a << b` or a chain of them, noting what comes after what."""
        if isinstance(node, ast.Name):
            if node.id not in self.tasks and node.id not in self.groups:
                raise _error(node, f"There's no task called “{node.id}”: assign it before using it.")
            return [node.id]
        if isinstance(node, ast.List | ast.Tuple):
            return [name for item in node.elts for name in self.chain(item, line)]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.RShift | ast.LShift):
            left, right = self.chain(node.left, line), self.chain(node.right, line)
            before, after = (left, right) if isinstance(node.op, ast.RShift) else (right, left)
            self.edges.extend((a, b, line) for a in before for b in after)
            return right
        raise _error(node, "Only tasks, task groups, lists of them, >> and << go here, like `[a, b] >> c`.")


def _dag_call(statement: ast.With) -> ast.Call | None:
    return _with_call(statement, "DAG")


def _group_call(statement: ast.With) -> ast.Call | None:
    return _with_call(statement, "TaskGroup")


def _with_call(statement: ast.With, name: str) -> ast.Call | None:
    if len(statement.items) != 1:
        return None
    call = statement.items[0].context_expr
    return call if isinstance(call, ast.Call) and _called(call) == name else None


def _called(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        return ""
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _is_string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_docstring(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)


def _error(node: ast.AST, message: str) -> DagError:
    return DagError(message, getattr(node, "lineno", 0), getattr(node, "col_offset", 0) + 1)


def _strings(value: Any, what: str, line: int, names: re.Pattern[str] | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) and v.strip() for v in value):
        raise DagError(f'{what} should be a list of names, like ["{"new-email" if names else "fetch"}"].', line)
    strings = tuple(dict.fromkeys(v.strip() for v in value))
    for name in strings if names else ():
        if not names.match(name):  # type: ignore[union-attr]
            raise DagError(f"“{name}” in {what} isn't an event's name: lower case with hyphens, like new-email.", line)
    return strings


def _line(args: dict[str, Any], name: str) -> int:
    return args.get("lines", {}).get(name) or args["line"]


def _build(dag: dict[str, Any], reader: _Reader, line: int) -> DagSpec:
    schedule = dag.get("schedule")
    cron = None
    if schedule is not None:
        at = _line({**dag, "line": line}, "schedule")
        if not isinstance(schedule, str):
            raise DagError('The schedule is cron, like "30 7 * * 1-5", or None.', at)
        try:
            cron = Cron.parse(schedule)
        except CronError as exc:
            raise DagError(f"The schedule {schedule!r} won't do: {exc}", at) from None
    triggers = _strings(dag.get("triggers"), "triggers", line, EVENT)
    tasks, groups = reader.tasks, reader.groups
    if not tasks:
        raise DagError('The DAG has no tasks: add one, like `check = Task(prompt="...")`.', line)
    if len(tasks) > MAX_TASKS:
        raise DagError(f"That's {len(tasks)} tasks: a DAG can have up to {MAX_TASKS}.", line)
    if len(groups) > MAX_GROUPS:
        raise DagError(f"That's {len(groups)} task groups: a DAG can have up to {MAX_GROUPS}.", line)
    ids = {name: args.get("task_id", name) for name, args in tasks.items()}
    seen: set[str] = set()
    for name, args in tasks.items():
        task_id, at = ids[name], args["line"]
        if not isinstance(task_id, str) or not TASK_ID.match(task_id):
            raise DagError(f"task_id {task_id!r} should be lower case letters, digits and _, like find_jobs.", at)
        if task_id in END_IDS:
            raise DagError(f"“{task_id}” is one of the tasks every DAG ends with: call yours something else.", at)
        if task_id == TRIGGER:
            raise DagError(f"“{TRIGGER}” stands for what started the run: call this task something else.", at)
        if task_id in seen:
            raise DagError(f"Two tasks are called “{task_id}”.", at)
        seen.add(task_id)
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DagError(f"“{task_id}” needs a prompt: what to do.", at)
        if len(prompt) > MAX_PROMPT:
            raise DagError(f"“{task_id}”'s prompt is over {MAX_PROMPT:,} characters.", at)
        if not isinstance(args.get("alert", False), bool):
            raise DagError("alert is True or False.", _line(args, "alert"))
    group_ids: dict[str, str] = {}
    for name, args in groups.items():
        group_id = args["group_id"]
        if not isinstance(group_id, str) or not TASK_ID.match(group_id):
            raise DagError(f"The group id {group_id!r} should be lower case letters, digits and _.", args["line"])
        if group_id in group_ids.values():
            raise DagError(f"Two task groups are called “{group_id}”.", args["line"])
        group_ids[name] = group_id
        for member in args["members"]:
            if args.get("for_each") is not None and tasks[member].get("for_each") is not None:
                raise DagError(
                    f"The tasks in “{group_id}” run once for each item it's for: “{ids[member]}” can't have a "
                    "for_each of its own.",
                    _line(tasks[member], "for_each"),
                )
    # What comes after what: as written, with groups standing for their first tasks or their last, and each task
    # after the task its for_each and run_if name.
    pairs: list[tuple[str, str, int]] = []
    for name, args in tasks.items():
        source = args.get("for_each")
        if source is None and args["group"] is not None and groups[args["group"]].get("for_each") is not None:
            source = groups[args["group"]]["for_each"]
            args.setdefault("lines", {})["for_each"] = groups[args["group"]]["line"]  # what's wrong with it is there
        args["source"] = source
        if source is not None and source != TRIGGER:
            pairs.append((source, name, _line(args, "for_each")))
        if args.get("run_if") is not None:
            pairs.append((args["run_if"], name, _line(args, "run_if")))
    written = [(a, b, at) for a, b, at in reader.edges if a in tasks and b in tasks]
    inner = [*pairs, *written]
    for a, b, at in reader.edges:
        befores = _ends(a, groups, inner, last=True) if a in groups else [a]
        afters = _ends(b, groups, inner, last=False) if b in groups else [b]
        if a in groups or b in groups:
            pairs += [(x, y, at) for x in befores for y in afters]
    pairs += written
    upstream: dict[str, list[str]] = {name: [] for name in tasks}
    for before, after, at in pairs:
        if before == after:
            raise DagError(f"“{ids[after]}” can't come after itself.", at)
        if before not in upstream[after]:
            upstream[after].append(before)
    by_id: dict[str, TaskSpec] = {}
    for name, args in tasks.items():
        task_id = ids[name]
        by_id[task_id] = TaskSpec(
            id=task_id,
            prompt=inspect.cleandoc(args["prompt"]),
            returns=args.get("return_type", TEXT),
            for_each=ids.get(args["source"], args["source"]),
            run_if=ids[args["run_if"]] if args.get("run_if") else None,
            run_when=args.get("run_when", True),
            allowed_tools=_strings(args.get("allowed_tools"), "allowed_tools", _line(args, "allowed_tools")),
            alert=args.get("alert", False),
            outlets=_strings(args.get("outlets"), "outlets", _line(args, "outlets"), EVENT),
            group=group_ids[args["group"]] if args["group"] else None,
            line=args["line"],
            upstream=tuple(ids[u] for u in upstream[name]),
        )
    ordered = _in_order(by_id)
    written_as = {ids[name]: args for name, args in tasks.items()}
    spaces = _spaces(ordered, by_id, written_as)
    for task in ordered:
        _check(task, by_id, spaces, triggers, written_as[task.id])
    group_specs = tuple(
        GroupSpec(
            id=group_ids[name],
            for_each=ids.get(args.get("for_each"), args.get("for_each")),  # type: ignore[arg-type]
            allowed_tools=_strings(args.get("allowed_tools"), "allowed_tools", _line(args, "allowed_tools")),
            tasks=tuple(ids[m] for m in args["members"]),
            line=args["line"],
        )
        for name, args in groups.items()
    )
    return DagSpec(
        schedule=cron,
        triggers=triggers,
        hidden_tools=_strings(dag.get("hidden_tools"), "hidden_tools", line),
        allowed_tools=_strings(dag.get("allowed_tools"), "allowed_tools", line),
        tasks=ordered,
        groups=group_specs,
        _by_id={t.id: t for t in ordered},
        _spaces=spaces,
    )


def _ends(group: str, groups: dict[str, dict[str, Any]], pairs: list[tuple[str, str, int]], last: bool) -> list[str]:
    """A group's first tasks (none of its others before them), or its last (none after them)."""
    members = groups[group]["members"]
    inside = [(a, b) for a, b, _ in pairs if a in members and b in members]
    return [m for m in members if not any((a if last else b) == m for a, b in inside)]


def _spaces(
    ordered: tuple[TaskSpec, ...], by_id: dict[str, TaskSpec], args: dict[str, dict[str, Any]]
) -> dict[str, str | None]:
    """What each task runs for (DagSpec.space), in order, as each follows from the task its for_each names."""
    spaces: dict[str, str | None] = {}
    for task in ordered:
        source = task.for_each
        if source is None or source == TRIGGER:
            spaces[task.id] = source
            continue
        given = by_id[source]
        at = _line(args[task.id], "for_each")
        if given.returns == BOOL:
            raise DagError(f"for_each needs a list: “{source}” returns yes or no.", at)
        if given.returns == TEXT and not given.mapped:
            raise DagError(
                f"for_each needs a list: “{source}” returns text. Give it return_type=list, or have it run for each "
                "item of a list itself.",
                at,
            )
        spaces[task.id] = source if given.returns == LIST else spaces[source]
    return spaces


def _check(
    task: TaskSpec, by_id: dict[str, TaskSpec], spaces: dict[str, str | None], triggers: tuple[str, ...], args: dict
) -> None:
    """What can only be checked once the tasks are known: run_if, {item} and outlets."""
    if task.run_if:
        condition = by_id[task.run_if]
        at = _line(args, "run_if")
        if condition.returns != BOOL:
            raise DagError(f"run_if needs a yes or no: give “{condition.id}” return_type=bool.", at)
        if spaces[condition.id] is not None and spaces[condition.id] != spaces[task.id]:
            raise DagError(
                f"run_if needs a yes or no for the same item: “{condition.id}” runs for each item, and “{task.id}” "
                "doesn't run for those items.",
                at,
            )
    if "{item}" in task.prompt and not task.mapped:
        raise DagError(f"{{item}} is the item a task runs for, with for_each: “{task.id}” runs once.", args["line"])
    for event in task.outlets:
        at = _line(args, "outlets")
        if event == CLAUDE_DONE:
            raise DagError(f"“{CLAUDE_DONE}” comes from Claude: give the outlet a name of its own.", at)
        if event in triggers:
            raise DagError(f"“{event}” is one of this DAG's triggers: a DAG can't start itself.", at)


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


def with_tool_lists(
    source: str, hidden_tools: list[str], allowed_tools: list[str], tasks: dict[str, list[str]] | None = None
) -> str:
    """The file with the DAG's hidden_tools and allowed_tools set to these, and the allowed_tools of the ``tasks``
    named, and everything else as it was written."""
    module = ast.parse(source)
    block = next(s for s in module.body if isinstance(s, ast.With) and _dag_call(s))
    call = _dag_call(block)
    assert call is not None
    edits = _Edits(source)
    edits.set(call, {"hidden_tools": hidden_tools, "allowed_tools": allowed_tools})
    for task_call, task_id in _task_calls(block.body):
        if tasks and task_id in tasks:
            edits.set(task_call, {"allowed_tools": tasks[task_id]})
    changed = edits.apply()
    parse(changed)  # it still reads
    return changed


def _task_calls(statements: list[ast.stmt]) -> list[tuple[ast.Call, str]]:
    """Each Task(...) in a DAG block, and its task_id."""
    found = []
    for statement in statements:
        if isinstance(statement, ast.Assign) and _called(statement.value) == "Task":
            call: ast.Call = statement.value  # type: ignore[assignment]
            written = {k.arg: k.value for k in call.keywords}
            given = written.get("task_id")
            name = statement.targets[0].id if isinstance(statement.targets[0], ast.Name) else ""
            found.append((call, given.value if given is not None and _is_string(given) else name))  # type: ignore[attr-defined]
        elif isinstance(statement, ast.With) and _group_call(statement):
            found += _task_calls(statement.body)
    return found


class _Edits:
    """Changes to the keyword arguments of calls in a file, made all at once, keeping everything else as written."""

    def __init__(self, source: str):
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))
        self.changes: list[tuple[int, int, str]] = []

    def at(self, line: int, column: int) -> int:
        """Where a node starts or ends in the text: ast counts columns in UTF-8 bytes."""
        return self.starts[line - 1] + len(self.lines[line - 1].encode()[:column].decode(errors="ignore"))

    def set(self, call: ast.Call, values: dict[str, list[str]]) -> None:
        written = {k.arg: k.value for k in call.keywords}
        adding = []
        for name, value in values.items():
            literal = _literal(sorted(dict.fromkeys(value)))
            if name in written:
                node = written[name]
                self.changes.append(
                    (self.at(node.lineno, node.col_offset), self.at(node.end_lineno, node.end_col_offset), literal)  # type: ignore[arg-type]
                )
            elif value:
                adding.append(f"{name}={literal}")
        if not adding:
            return
        if call.keywords:
            last = call.keywords[-1].value
            where = self.at(last.end_lineno, last.end_col_offset)  # type: ignore[arg-type]
            if call.lineno == call.end_lineno:
                text = "".join(f", {arg}" for arg in adding)
            else:  # one argument to a line, as they're written
                line = self.lines[last.lineno - 1]
                indent = line[: len(line) - len(line.lstrip())]
                text = "".join(f",\n{indent}{arg}" for arg in adding)
        else:
            where = self.at(call.end_lineno, call.end_col_offset) - 1  # type: ignore[arg-type]  # before the )
            text = ", ".join(adding)
        self.changes.append((where, where, text))

    def apply(self) -> str:
        source = self.source
        for begin, finish, text in sorted(self.changes, reverse=True):
            source = source[:begin] + text + source[finish:]
        return source


def unknown_tools(spec: DagSpec, known: list[str]) -> list[str]:
    """The tool names and patterns in a DAG that match no tool Athena has now, such as one from an MCP server
    that's switched off."""
    from athena.tools import matches_any

    named = [
        *spec.hidden_tools,
        *spec.allowed_tools,
        *(name for g in spec.groups for name in g.allowed_tools),
        *(name for t in spec.tasks for name in t.allowed_tools),
    ]
    return [p for p in dict.fromkeys(named) if not any(matches_any(tool, [p]) for tool in known)]


def _literal(value: Any) -> str:
    return repr(value).replace("'", '"') if not isinstance(value, str) else _string(value)


def _string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
