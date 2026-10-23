"""Reading an agent's DAG file: agentic-airflow's Python (docs/dags.md), which Athena reads but never runs.

    from agentic_airflow import AgenticBranch, AgenticTask, Asset, Dag, Task, trigger_items
    from athena.dags import alert

    with Dag(
        "job_hunting",
        prompt="Apply for data engineering jobs that fit my CV.",
        schedule=["0 9 * * 1-5", Asset("job-emails")],  # cron, in your time zone, and what starts it
        allowed_tools=["read_file", "fetch"],
    ):
        jobs = Task("jobs", fn=trigger_items, fan_out=True)  # one for each job that came with the run
        fits = AgenticBranch("fits", prompt="Does this job fit my CV?", allow_none=True)
        apply = AgenticTask("apply", prompt="Apply with my CV.", follow_mapping=True, on_complete=alert)
        report = AgenticTask("report", prompt="Say what was applied for, with links.")
        jobs >> fits >> apply >> report

The file is interpreted a statement at a time by a reader that knows only these: imports (passed over), one
`with Dag(...)` block, task groups in it, operators assigned to names or not, and `>>` or `<<` between them; values
written out as they are (strings, numbers, True, False, None, and lists and dicts of them); the package's
operators and triggers; and the few functions in FUNCTIONS. It builds the package's objects as it goes, so they
check themselves as they would in code, and any error says on which line. Nothing in the file is run, so a DAG,
or a change the model proposes to one, can't run code on the Pi.
"""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import operator
import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from agentic_airflow import (
    AgenticAirflowError,
    AgenticBranch,
    AgenticEvaluator,
    AgenticTask,
    Asset,
    CronTrigger,
    Dag,
    DagTrigger,
    Task,
    TaskGroup,
    Webhook,
    found_nothing,
    trigger_items,
)
from agentic_airflow.operators import BaseOperator
from agentic_airflow.triggers import AssetTrigger, WebhookTrigger

from athena.cron import Cron, CronError

EVENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # a webhook's or asset's name, such as arrive-home
CLAUDE_DONE = "claude-done"  # the asset Claude finishing a project updates (claude_projects.py)
END_GROUP = "end"  # the id of the tasks every DAG ends with (ends.py), which a file's ids can't start with
# Those tasks, in order: (id, what it does).
END_TASKS = (
    ("review", "Looks back at the run: proposes changes to its tasks and their tools, and improves skills it used"),
    ("summary", "Tells you what's new, if anything, which the next run starts with"),
)
MAX_SOURCE = 40_000  # characters
MAX_TASKS = 30
MAX_GROUPS = 10
MAX_PROMPT = 4000  # characters
MAX_ITEMS = 20  # a list mapped over: the most tasks it starts
MAX_RETRIES = 3  # a task's retries
MAX_RETRY_DELAY = 3600  # seconds

# Athena's own functions for a DAG: what `on_complete=alert` and the like name. The service gives the real ones
# (Dags.functions); these stand in when a file's only being checked.
HOOKS = ("alert",)

# What a DAG file can call, and the arguments each takes: positional, then by name.
_TASK_ARGS = {"context", "fan_out", "follow_mapping", "trigger_rule", "on_complete", "outlets", "skip_if", "retries",
              "retry_delay"}  # fmt: skip
_SCOPE_ARGS = {"model", "allowed_tools", "denied_tools", "hidden_tools", "skills", "inherit_skills", "thinking",
               "max_tool_steps", "retries", "retry_delay", "ask_user"}  # fmt: skip
_AGENTIC_ARGS = _TASK_ARGS | _SCOPE_ARGS | {"return_type", "json_schema", "verbosity", "include_run_log",
                                            "include_dag_definition", "system_prompt"}  # fmt: skip
CALLS: dict[str, tuple[Any, tuple[str, ...], frozenset[str]]] = {
    "Dag": (
        Dag,
        ("id", "prompt"),
        frozenset(
            _SCOPE_ARGS
            | {"schedule", "context", "auto_evaluate", "description", "tags", "verbosity", "max_active_runs", "catchup"}
        ),
    ),  # fmt: skip
    "TaskGroup": (TaskGroup, ("id",), frozenset(_SCOPE_ARGS)),
    "Task": (Task, ("id", "fn"), frozenset(_TASK_ARGS)),
    "AgenticTask": (AgenticTask, ("id", "prompt"), frozenset(_AGENTIC_ARGS)),
    "AgenticBranch": (AgenticBranch, ("id", "prompt"), frozenset(_AGENTIC_ARGS | {"multi_select", "allow_none"})),
    "AgenticEvaluator": (
        AgenticEvaluator,
        ("id", "prompt"),
        frozenset(_AGENTIC_ARGS | {"target", "max_retries", "on_exhausted"}),
    ),
    "Asset": (Asset, ("name",), frozenset()),
    "Webhook": (Webhook, ("name",), frozenset()),
    "DagTrigger": (DagTrigger, ("dag_id",), frozenset({"on"})),
    "Cron": (CronTrigger, ("expression",), frozenset({"timezone"})),
}
ALIASES = {"AssetTrigger": "Asset", "WebhookTrigger": "Webhook", "CronTrigger": "Cron"}
_OPERATORS = {"Task", "AgenticTask", "AgenticBranch", "AgenticEvaluator"}

# How DAG files are written, in brief: for the model, when it writes or changes one.
REFERENCE = """DAG files are agentic-airflow Python, read and never run (docs/dags.md):
from agentic_airflow import AgenticBranch, AgenticTask, Asset, Dag, Task, TaskGroup, Webhook, trigger_items
from athena.dags import alert
with Dag("dag_id", prompt="What it's for", schedule=["30 7 * * 1-5", Asset("job-emails"), Webhook("arrive-home")],
         allowed_tools=["fetch"]):  # the tools its tasks are given: a task's own list replaces it
    jobs = AgenticTask("jobs", prompt="List new jobs: one item each.", return_type="list")  # fan_out: one each
    fits = AgenticBranch("fits", prompt="Does it fit my CV?", allow_none=True, allowed_tools=["read_file"])
    apply = AgenticTask("apply", prompt="Apply.", follow_mapping=True, on_complete=alert)  # per item; tells user
    report = AgenticTask("report", prompt="Say what was applied for.", outlets=[Asset("applied")])
    jobs >> fits >> apply >> report  # report joins: it gets every item's output
    # Task("items", fn=trigger_items, fan_out=True): one lane for each item that came with what started the run.
    # with TaskGroup("each", allowed_tools=[...]) as each: ... tasks ...  then a >> each >> b
Schedule: cron strings (your time zone, at most every 15 min), Asset(name): when a task with it in outlets
produces something, Webhook(name): POST /hooks/<name>, DagTrigger("dag_id"): when that DAG's run ends. Asset
"claude-done": when Claude finishes a project. A run missed while Athena was off runs when it's back
(catchup=False on the Dag to skip it).
On the Dag, a TaskGroup or a task: retries=1 tries a failed task again after retry_delay seconds (default 60);
ask_user=True lets its tasks ask the user something only they can decide, and wait for the answer.
Tools: a task is given only those its allowed_tools lists (patterns such as "news_*" work), and they run without
asking, apart from those that always ask. Others that ask first it can find when it needs one, and they ask; others
it never has. So list each task's own, and only what it needs.
Give each task one job, and a prompt that says exactly what to look at and with what, as "Read today's calendar
events.", not "Look for events.": a task sees only its prompt and what came before it, so a vague one does other
tasks' work again."""

STARTER = """from agentic_airflow import AgenticTask, Dag

# What this agent does by itself. Give it a schedule, such as ["0 9 * * *"], to run on its own, and press Run
# now to try it. allowed_tools are the tools its tasks are given, such as ["fetch", "read_file"].
with Dag("{dag_id}", prompt="{purpose}", schedule=[], allowed_tools=[]):
    check = AgenticTask("check", prompt="Check on what this agent is for, and say what's new.")
"""

MASTER_STARTER = """from agentic_airflow import AgenticTask, Asset, Dag

# What the master agent keeps an eye on for you. What a task with outlets finds starts the DAGs of your other
# agents with that asset in their schedule. Give it a schedule, such as ["@hourly"], to check by itself, and each
# task the tools it uses, in allowed_tools.
with Dag("master", prompt="Keep an eye on things for the user's other agents.", schedule=[]):
    job_emails = AgenticTask(
        "job_emails",
        prompt="Look through my emails since the last run for new ones about jobs: one item for each.",
        allowed_tools=[],  # the email server's tools that read mail
        return_type="list",
        fan_out=False,
        outlets=[Asset("job-emails")],  # starts the DAGs with Asset("job-emails") in their schedule
    )
"""


class DagError(ValueError):
    """A DAG file that can't be used: what's wrong, and where."""

    def __init__(self, message: str, line: int = 0, column: int = 0):
        super().__init__(f"Line {line}: {message}" if line else message)
        self.message, self.line, self.column = message, line, column

    def as_dict(self) -> dict[str, Any]:
        return {"message": self.message, "line": self.line, "column": self.column}


class Hook:
    """One of Athena's functions a DAG names, such as alert: called as the package calls it, and known by name."""

    def __init__(self, name: str, fn: Callable[..., Any] | None = None):
        self.name, self.fn = name, fn

    def __call__(self, *args: Any) -> Any:
        return self.fn(*args) if self.fn else None  # nothing, while a file's only being checked

    def __repr__(self) -> str:
        return self.name


def read(
    source: str,
    *,
    timezone: str = "UTC",
    hooks: Mapping[str, Callable[..., Any]] | None = None,
    as_written: bool = False,
) -> Dag:
    """The DAG a file describes, validated. ``hooks`` are Athena's functions (HOOKS) its operators can name.
    ``as_written``: without what Athena gives a DAG that doesn't say, such as an empty list of tools. DagError,
    saying what and where, if it won't do."""
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
        if isinstance(statement, ast.With) and block is None and _with_call(statement, "Dag"):
            block = statement
            continue
        if isinstance(statement, ast.With) and _with_call(statement, "DAG", exact=True):
            raise _error(
                statement,
                "This is written as Athena's DAGs were before: write it again with agentic-airflow's Dag, as "
                "docs/dags.md says.",
            )
        raise _error(statement, "Outside the DAG, only imports go here: put everything in one `with Dag(...):` block.")
    if block is None:
        raise DagError('There\'s no `with Dag("dag_id", prompt="..."):` block.', 1)
    reader = _Reader(timezone, {name: Hook(name, (hooks or {}).get(name)) for name in HOOKS}, as_written)
    dag = reader.dag(block)
    dag.definition = source  # what a proposal for it replaces (proposals.py)
    return dag


def check(source: str, timezone: str = "UTC") -> Dag | None:
    """The DAG a file describes, or None if it hasn't one or it won't do."""
    try:
        return read(source, timezone=timezone) if source.strip() else None
    except DagError:
        return None


class _Reader:
    """Interprets the DAG block, building the package's objects. Names are bound to tasks, groups and the DAG as
    they're assigned; the package's operators and triggers, and the functions a DAG can use, are known from the
    start."""

    def __init__(self, timezone: str, hooks: dict[str, Callable[..., Any]], as_written: bool = False):
        self.timezone = timezone
        self.as_written = as_written
        self.functions: dict[str, Callable[..., Any]] = {
            "trigger_items": trigger_items,
            "found_nothing": found_nothing,
            **hooks,
        }
        self.names: dict[str, Any] = {}  # tasks, groups and the DAG, by the names they're assigned to
        self.lines: dict[str, int] = {}  # where each task's written, by its id
        self.groups = 0

    def dag(self, block: ast.With) -> Dag:
        dag = self.make(block.items[0].context_expr)  # type: ignore[arg-type]
        self.bind(block.items[0].optional_vars, dag, block)
        with self.entered(dag, block):
            self.body(block.body)
        if not dag.tasks:
            raise _error(block, 'The DAG has no tasks: add one, such as AgenticTask("check", prompt="...").')
        if len(dag.tasks) > MAX_TASKS:
            raise _error(block, f"That's {len(dag.tasks)} tasks: a DAG has at most {MAX_TASKS}.")
        try:
            dag.validate()
        except AgenticAirflowError as exc:
            # On the line of the first task it's about, if it names one.
            named = [self.lines[i] for i in re.findall(r"'([^']+)'", str(exc)) if i in self.lines]
            raise DagError(_said(exc), named[0] if named else block.lineno) from None
        for trigger in dag.schedule:  # as the engine checks when it's registered
            if isinstance(trigger, AssetTrigger) and trigger.name in dag.outlets():
                task = next(t for t in dag.tasks.values() if trigger.name in t.outlets)
                raise DagError(
                    f"A DAG can't start itself: {trigger.name} is in its schedule and its task {task.id}'s outlets.",
                    self.lines.get(task.id, block.lineno),
                )
            if isinstance(trigger, DagTrigger) and trigger.dag_id == dag.id:
                raise DagError("A DAG can't start itself: its schedule has DagTrigger of its own id.", block.lineno)
        return dag

    @contextlib.contextmanager
    def entered(self, scope: Any, node: ast.AST) -> Iterator[None]:
        scope.__enter__()
        try:
            yield
        finally:
            scope.__exit__(None, None, None)

    def body(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if _is_docstring(statement) or isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.With) and _with_call(statement, "TaskGroup"):
                self.groups += 1
                if self.groups > MAX_GROUPS:
                    raise _error(statement, f"A DAG has at most {MAX_GROUPS} task groups.")
                group = self.make(statement.items[0].context_expr)  # type: ignore[arg-type]
                self.bind(statement.items[0].optional_vars, group, statement)
                with self.entered(group, statement):
                    self.body(statement.body)
                if not group.all_tasks():
                    raise _error(statement, "This TaskGroup has no tasks.")
            elif isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    raise _error(statement, 'Assign each task to one name, like `find = AgenticTask("find", ...)`.')
                value = self.value(statement.value)
                if not _is_node(value):
                    raise _error(statement, "Only tasks are assigned to names in a DAG.")
                self.bind(statement.targets[0], value, statement)
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call | ast.BinOp):
                self.value(statement.value)
            else:
                raise _error(
                    statement,
                    'Inside the DAG, write tasks (`find = AgenticTask("find", prompt="...")`), task groups '
                    '(`with TaskGroup("each"):`) and what comes after what (`a >> b`).',
                )

    def bind(self, target: ast.expr | None, value: Any, statement: ast.stmt) -> None:
        if target is None:
            return
        if not isinstance(target, ast.Name):
            raise _error(statement, "Give it one name, like `as each`.")
        name = target.id
        if name in self.names or name in self.functions or name in CALLS or name in ALIASES:
            raise _error(statement, f"“{name}” is already used: give each task and group its own name.")
        self.names[name] = value

    # -- values ---------------------------------------------------------------------------------------

    def value(self, node: ast.expr) -> Any:
        """What an expression stands for: a value written out, a name, a call the reader knows, or `>>`."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str | int | float | bool | type(None)):
            return node.value
        if isinstance(node, ast.List | ast.Tuple):
            return [self.value(item) for item in node.elts]
        if isinstance(node, ast.Dict):
            if not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
                raise _error(node, "A dict's keys are strings, written out.")
            return {k.value: self.value(v) for k, v in zip(node.keys, node.values, strict=True)}  # type: ignore[union-attr]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
            value = node.operand.value
            if isinstance(value, int | float) and not isinstance(value, bool):
                return -value
        if isinstance(node, ast.Name):
            if node.id in self.names:
                return self.names[node.id]
            if node.id in self.functions:
                return self.functions[node.id]
            raise _error(node, f"There's no task called “{node.id}” before this: assign it before using it.")
        if isinstance(node, ast.Call):
            return self.make(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.RShift | ast.LShift):
            left, right = self.value(node.left), self.value(node.right)
            if not (_is_nodes(left) and _is_nodes(right)):
                raise _error(node, "Only tasks, task groups and lists of them go either side of >> and <<.")
            try:
                return (operator.rshift if isinstance(node.op, ast.RShift) else operator.lshift)(left, right)
            except AgenticAirflowError as exc:
                raise _error(node, _said(exc)) from None
        raise _error(node, "Write values out as they are: no f-strings, variables, loops or other calls.")

    def make(self, call: ast.Call) -> Any:
        """Call one of the package's operators, task groups, triggers or the DAG, as the file says."""
        if not isinstance(call.func, ast.Name):
            raise _error(call, "Write values out as they are: no f-strings, variables, loops or other calls.")
        written = call.func.id
        name = ALIASES.get(written, written)
        if name == "Branch":
            raise _error(call, "A Branch runs Python, which a DAG here can't: use AgenticBranch.")
        if name not in CALLS:
            known = ", ".join(sorted(CALLS))
            raise _error(call, f"A DAG can call {known}, not {written}.")
        made, positional, by_name = CALLS[name]
        if len(call.args) > len(positional):
            raise _error(call, f"{written} takes {', '.join(positional)} in order; give the rest by name.")
        args: dict[str, Any] = {}
        for param, node in zip(positional, call.args, strict=False):
            args[param] = self.argument(name, param, node)
        for keyword in call.keywords:
            if keyword.arg is None or keyword.arg not in by_name | set(positional):
                known = ", ".join(sorted(by_name | set(positional)))
                raise _error(keyword.value, f"{written} takes {known}, not {keyword.arg or '**'}.")
            if keyword.arg in args:
                raise _error(keyword.value, f"{keyword.arg} is given twice.")
            args[keyword.arg] = self.argument(name, keyword.arg, keyword.value)
        if name == "Dag":  # a run missed while the Pi was off runs when it's back
            args.setdefault("catchup", True)
            if not self.as_written:
                args.setdefault("allowed_tools", [])  # its tasks are given only the tools their lists name
        if name == "Cron":
            args.setdefault("timezone", self.timezone)
            self.cron(args.get("expression"), call)
        if name == "AgenticEvaluator":  # nobody's asked what to do once it's out of tries: it passes on what it has
            args.setdefault("on_exhausted", "pass_latest")
            if args["on_exhausted"] not in ("pass_latest", "fail_dag"):
                raise _error(call, 'on_exhausted is "pass_latest" or "fail_dag" here.')
        if name in ("Asset", "Webhook") and not EVENT.match(str(args.get("name", ""))):
            raise _error(call, f'An {name.lower()}\'s name is lower case with hyphens, like "job-emails".')
        try:
            made_ = made(**args)
        except (AgenticAirflowError, ValueError) as exc:
            raise _error(call, _said(exc)) from None
        except TypeError as exc:
            raise _error(call, f"{written} needs {', '.join(positional)}: {exc}") from None
        if isinstance(made_, BaseOperator):
            self.lines[made_.id] = call.lineno
        return made_

    def argument(self, call: str, param: str, node: ast.expr) -> Any:
        value = self.value(node)
        if param == "id" and isinstance(value, str) and value.split(".")[0] == END_GROUP and call != "Dag":
            raise _error(node, f"Ids starting “{END_GROUP}.” are the tasks every DAG ends with: choose another.")
        if param in ("prompt", "system_prompt") and isinstance(value, str) and len(value) > MAX_PROMPT:
            raise _error(node, f"That's {len(value):,} characters: keep it under {MAX_PROMPT:,}.")
        if param == "fn" and value is not trigger_items:
            raise _error(node, 'A Task here runs trigger_items, and nothing else: Task("items", fn=trigger_items).')
        if param == "on_complete" and value not in [self.functions[h] for h in HOOKS]:
            raise _error(node, f"on_complete is one of {', '.join(HOOKS)}.")
        if param == "skip_if" and value is not found_nothing:
            raise _error(node, "skip_if is found_nothing.")
        if param == "retries" and not (
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_RETRIES
        ):
            raise _error(node, f"retries is a whole number from 0 to {MAX_RETRIES}.")
        if param == "retry_delay" and not (
            isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= MAX_RETRY_DELAY
        ):
            raise _error(node, f"retry_delay is a number of seconds, up to {MAX_RETRY_DELAY}.")
        if param in ("ask_user", "catchup") and not isinstance(value, bool):
            raise _error(node, f"{param} is True or False.")
        if param in ("context", "target") and not _is_nodes(value) and value is not None:
            raise _error(node, f"{param} names tasks before this one.")
        if param == "schedule":
            entries = value if isinstance(value, list) else [value] if value is not None else []
            written = node.elts if isinstance(node, ast.List | ast.Tuple) else [node]
            for entry, where in zip(entries, written, strict=False):
                if isinstance(entry, str):
                    self.cron(entry, where)
            value = [CronTrigger(e, timezone=self.timezone) if isinstance(e, str) else e for e in entries]
        if callable(value) and param not in ("fn", "on_complete", "skip_if") and not _is_nodes(value):
            raise _error(node, f"{param} can't be a function.")
        return value

    def cron(self, expression: Any, node: ast.AST) -> None:
        if not isinstance(expression, str):
            raise _error(node, 'A schedule is cron, like "30 7 * * 1-5".')
        try:
            Cron.parse(expression)
        except CronError as exc:
            raise _error(node, f"{exc}") from None


# -- describing a DAG ------------------------------------------------------------------------------------


def when(dag: Dag) -> str:
    """When a DAG runs, in words: "at 07:30 on weekdays, or when job-emails is updated"."""
    said = []
    for trigger in dag.schedule:
        if isinstance(trigger, CronTrigger):
            with contextlib.suppress(CronError):
                said.append(Cron.parse(trigger.expression).describe())
        elif isinstance(trigger, AssetTrigger):
            said.append(f"when {trigger.name} is updated")
        elif isinstance(trigger, WebhookTrigger):
            said.append(f"when {trigger.name} comes in")
        elif isinstance(trigger, DagTrigger):
            said.append(f"when the DAG {trigger.dag_id} finishes")
    if not said:
        return "only when it's run by hand"
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + ", or " + said[-1]


def assets(dag: Dag) -> list[str]:
    return [t.name for t in dag.schedule if isinstance(t, AssetTrigger)]


def webhooks(dag: Dag) -> list[str]:
    return [t.name for t in dag.schedule if isinstance(t, WebhookTrigger)]


def as_dict(dag: Dag) -> dict[str, Any]:
    """A DAG for the dashboard: its schedule, tools, task groups and tasks, as it draws them."""
    own = dag.own_tasks()
    groups = [g for g in dag.groups if g is not dag.evaluator_group]
    return {
        "id": dag.id,
        "prompt": dag.prompt,
        "when": when(dag),
        "schedule": [t.to_dict() for t in dag.schedule],
        "tools": {k: dag.scope.to_dict()[k] for k in ("allowed_tools", "hidden_tools", "denied_tools")},
        "groups": [
            {"id": g.id, "parent": g.parent.id if g.parent else None, "tasks": [t.id for t in g.all_tasks()]}
            for g in groups
        ],
        "tasks": [_task(t) for t in dag.topological_order() if t in own],
        "outlets": dag.outlets(),
        "end": [{"id": f"{END_GROUP}.{i}", "about": about} for i, about in END_TASKS] if dag.auto_evaluate else [],
    }


def _task(t: BaseOperator) -> dict[str, Any]:
    group = t.group if t.group is not None and not getattr(t.group, "is_dag", False) else None
    out = {
        "id": t.id,
        "kind": t.kind,
        "prompt": getattr(t, "prompt_text", None),
        "fn": getattr(getattr(t, "fn", None), "__name__", None),
        "return_type": getattr(t, "return_type", None),
        "fan_out": t.fan_out,
        "follow_mapping": t.follow_mapping,
        "allow_none": getattr(t, "allow_none", False),
        "group": group.id if group else None,
        "upstream": t.upstream_ids,
        "outlets": list(t.outlets),
        "alert": getattr(t.on_complete, "name", "") == "alert",
        "tools": {k: v for k, v in t.scope.to_dict().items() if k.endswith("_tools") and v is not None},
    }
    return out


def unknown_tools(dag: Dag, known: list[str]) -> list[str]:
    """Tools a DAG names, at any level, that no tool is called now: say because its MCP server is off."""
    scopes = [dag.scope, *(g.scope for g in dag.groups), *(t.scope for t in dag.tasks.values())]
    lists = ("allowed_tools", "denied_tools", "hidden_tools")
    named = {n for s in scopes for key in lists for n in (getattr(s, key) or [])}
    return sorted(n for n in named if not fnmatch.filter(known, n))


# -- changing a file ------------------------------------------------------------------------------------


def with_tools(source: str, changes: dict[str, Any]) -> str:
    """A DAG file with the tool lists ``changes`` sets: {"hidden_tools": [...], "allowed_tools": {where: [...]}},
    where is "dag" or a task's id. Each list's written where the file says it now, or added to the call; the rest
    of the file stays as it's written. DagError if the file won't read, or a task it names isn't in it."""
    module = ast.parse(source)
    calls: dict[str, ast.Call] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = ALIASES.get(node.func.id, node.func.id)
            if name == "Dag":
                calls["dag"] = node
            elif name in _OPERATORS and (task_id := _written_id(node)):
                calls[task_id] = node
    edits: list[tuple[int, int, str]] = []  # (start, end, text) in the source, as offsets
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def at(line: int, column: int) -> int:
        return starts[line - 1] + len(lines[line - 1].encode()[:column].decode(errors="ignore"))

    def put(where: str, key: str, value: list[str]) -> None:
        call = calls.get(where)
        if call is None:
            raise DagError(f"There's no task called “{where}” in the DAG now.")
        written = repr(list(value)).replace("'", '"')
        for keyword in call.keywords:
            if keyword.arg == key:
                v = keyword.value
                edits.append((at(v.lineno, v.col_offset), at(v.end_lineno or v.lineno, v.end_col_offset or 0), written))
                return
        last = [*call.args, *call.keywords][-1] if call.args or call.keywords else None
        if last is None:
            end = at(call.end_lineno or call.lineno, (call.end_col_offset or 1) - 1)
            edits.append((end, end, f"{key}={written}"))
        else:
            node = last.value if isinstance(last, ast.keyword) else last
            end = at(node.end_lineno or node.lineno, node.end_col_offset or 0)
            start = last if isinstance(last, ast.keyword) else node
            if start.lineno != call.lineno:  # one argument to a line: so is this one
                edits.append((end, end, f",\n{' ' * start.col_offset}{key}={written}"))
            else:
                edits.append((end, end, f", {key}={written}"))

    if "hidden_tools" in changes:
        put("dag", "hidden_tools", changes["hidden_tools"])
    for where, names in (changes.get("allowed_tools") or {}).items():
        put(where, "allowed_tools", names)
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    return source


def _written_id(call: ast.Call) -> str | None:
    for node in [*call.args[:1], *(k.value for k in call.keywords if k.arg == "id")]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
    return None


# -- helpers ----------------------------------------------------------------------------------------------


def _with_call(statement: ast.With, name: str, exact: bool = False) -> ast.Call | None:
    if len(statement.items) != 1:
        return None
    call = statement.items[0].context_expr
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None
    written = call.func.id if exact else ALIASES.get(call.func.id, call.func.id)
    return call if written == name else None


def _is_docstring(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)


def _is_node(value: Any) -> bool:
    return isinstance(value, BaseOperator | TaskGroup)


def _is_nodes(value: Any) -> bool:
    return _is_node(value) or (isinstance(value, list) and bool(value) and all(_is_node(v) for v in value))


def _said(exc: Exception) -> str:
    text = str(exc).strip()
    return text if text.endswith(".") else text + "."


def _error(node: ast.AST, message: str) -> DagError:
    return DagError(message, getattr(node, "lineno", 0), getattr(node, "col_offset", 0) + 1)


def unlisted(source: str) -> list[str]:
    """The DAG's own tasks that no list of tools covers, as it's written: not their own, a group's or the DAG's."""
    dag = read(source, as_written=True)
    found = []
    for task in dag.own_tasks():
        scopes = [task.scope, dag.scope]
        group = task.group
        while group is not None and not getattr(group, "is_dag", False):
            scopes.append(group.scope)
            group = group.parent
        if all(scope.allowed_tools is None for scope in scopes):
            found.append(task.id)
    return found
