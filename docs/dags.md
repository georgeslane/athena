# Writing DAGs

An agent's DAG says what it does by itself: tasks, each a prompt for the model, run on a schedule or when something happens. It's written in the Python of [agentic-airflow](https://github.com/georgeslane/agentic-airflow), a library in the style of [Apache Airflow](https://airflow.apache.org/), whose engine runs it. Athena reads the file but never runs it, so a DAG, or a change Athena proposes to one, can never run code on the Pi.

You write it on the dashboard, in the **DAG** view of an agent (see [Agents](agents.md)): it's checked as you type, any mistake is shown on its line, and it's drawn as you go. **How to write it**, above the file, has this reference in brief. Athena can write one too, when you ask it to do something regularly: it shows you exactly what changes, and asks first.

## An example

```python
from agentic_airflow import AgenticBranch, AgenticTask, Asset, Dag, TaskGroup, Webhook
from athena.dags import alert

with Dag(
    "emails",
    prompt="Keep on top of my emails.",
    schedule=["0 8 * * *", Webhook("new-email")],  # every day at 08:00, and when the webhook new-email comes in
    hidden_tools=["news_*"],  # tools its tasks find when they need them
):
    emails = AgenticTask(
        "emails",
        prompt="Which of my emails since the last run need a reply? One item for each.",
        return_type="list",  # a list: what's after it runs once for each item
    )
    with TaskGroup("each_email", allowed_tools=["read_email", "draft_email"]) as each_email:
        urgent = AgenticBranch("urgent", prompt="Is this one urgent?", allow_none=True)  # draft, or none
        draft = AgenticTask("draft", prompt="Draft a reply to it.", follow_mapping=True, on_complete=alert)
        urgent >> draft
    report = AgenticTask("report", prompt="Say what was drafted.", outlets=[Asset("drafted")])
    emails >> each_email >> report
```

Every day at 08:00, `emails` lists the emails that need a reply. Then, for each one, `urgent` picks whether `draft` runs for it, and `draft` drafts a reply if it does, telling you what it drafted. `report`, given what was drafted for every email, says what was drafted, and starts the DAGs of any agents with `Asset("drafted")` in their schedule, with what it said.

## The file

A DAG file has:

- **Imports**, such as `from agentic_airflow import AgenticTask, Dag`. They're there so it reads as Python, and Athena passes over them.
- **One `with Dag(...):` block**, with everything else inside it.
- **Tasks**, assigned to a name or not: `find = AgenticTask("find", prompt="...")`.
- **Task groups**: `with TaskGroup("each_job") as each_job:`, with tasks inside, and groups inside those if you like.
- **What comes after what**: `find >> apply`.

Values are written out as they are: strings, numbers, `True`, `False`, `None`, and lists and dicts of them. The names you write are those of the operators and triggers below, of tasks and groups you've assigned, and of `trigger_items`, `found_nothing` and `alert`. Anything else, such as a loop, a variable, an f-string or another call, is refused, saying on which line, so a DAG can always be read without running it. A DAG has up to 30 tasks and 10 task groups.

## The DAG

`Dag("id", prompt="...")` takes these:

| Argument | What it is |
|---|---|
| `id` | Its id, such as `"emails"`: unique among your agents' DAGs. Its runs are kept by it, so renaming it starts its history afresh. |
| `prompt` | What it's for. Every task is told it, with the agent's guidance. |
| `schedule` | What starts it, a list of any of: cron, such as `"30 7 * * 1-5"` (see [Schedules](#schedules)); `Webhook("arrive-home")`; `Asset("job-emails")`; `DagTrigger("other_dag")`. Empty, or left out, runs it only when you do. See [What starts a DAG](#what-starts-a-dag). |
| `allowed_tools` | The tools its tasks may use without asking: see [Tools](#tools). |
| `hidden_tools` | Tools its tasks find when they need them, rather than having them in every prompt, which keeps prompts shorter. |
| `denied_tools` | Tools its tasks are never offered: calling one waits for your OK. |
| `model` | The model its tasks use: the agent's, unless it says. |
| `thinking` | `"on"` or `"off"`: whether its tasks think before answering. `[tasks] thinking` otherwise. |
| `skills` | Skills to read into every task's prompt, by name. |
| `auto_evaluate` | `False` leaves out the tasks every DAG ends with (see [How a run goes](#how-a-run-goes)). |

Patterns such as `"news_*"` work in all of the tool lists.

## Tasks

| Operator | What it is |
|---|---|
| `AgenticTask("id", prompt="...")` | A task for the model: its prompt, with what the tasks before it found. |
| `AgenticBranch("id", prompt="...")` | The model picks which of the tasks after it run; the others are skipped. See [Branches](#branches). |
| `AgenticEvaluator("id", prompt="...")` | Checks what the task before it did, and has it done again if it won't do. See [Checking a task's work](#checking-a-tasks-work). |
| `Task("items", fn=trigger_items, fan_out=True)` | The items that came with what started the run, one for each task after it. See [What starts a DAG](#what-starts-a-dag). |

An id is a letter then letters, digits, `_`, `.`, `:` or `-`, unique in the DAG, and none can start `end.`, which is for the tasks every DAG ends with. Each task can also have:

| Argument | What it is |
|---|---|
| `return_type` | What it gives the tasks after it: `"string"`, its answer, the default; `"list"`, a JSON list of items; or `"json"`. A list or JSON is checked: an answer that isn't one is sent back to the model once, saying why, and the task fails if it still isn't. |
| `fan_out` | For a list, `True` (the default) runs the tasks after it once for each item; `False` gives them the whole list. |
| `follow_mapping` | `True`: a task after one that ran for each item runs once for each too, with that item's. Otherwise it runs once, given every item's. See [Lists](#lists). |
| `context` | The tasks whose outputs it's given, such as `context=[find, check]`: those right before it, unless it says. |
| `on_complete=alert` | What it finds is told to you, in Telegram and on your devices, and in the agent's chat and the master agent's. For a task that ran for each item, once, with what each found. |
| `outlets` | Assets it updates with what it finds, such as `[Asset("job-emails")]`. See [Other agents' DAGs](#other-agents-dags-assets). |
| `allowed_tools`, `hidden_tools`, `denied_tools`, `model`, `thinking`, `skills` | As the DAG's, for this task alone. |
| `max_tool_steps` | The most steps that use tools it takes: `[tasks] max_tool_rounds` (25) otherwise. |
| `skip_if=found_nothing` | Skipped if nothing before it found anything. |
| `trigger_rule` | `"none_failed"`, the default: it's skipped if all those before it were, and not run if one failed. `"all_done"`: it runs whatever happened before it. |

## What comes after what

```python
find >> apply  # apply runs after find
apply << find  # the same
[find, check] >> report  # report after both
find >> apply >> report  # a chain
src >> each_job >> report  # a group stands for its first tasks after something, and its last before something
```

A task runs once all those before it have. It's skipped ("not run") if one of them failed, or if all of them were skipped, or if a branch before it didn't pick it, and so on down the line. A loop, such as `a >> b >> a`, is refused.

## Lists

A task with `return_type="list"` answers with a JSON list, at most 20 items.

```python
jobs = AgenticTask("jobs", prompt="Find new data jobs that fit my CV, one item for each.", return_type="list")
apply = AgenticTask("apply", prompt="Apply to this job.")
report = AgenticTask("report", prompt="Say what was applied for.")
jobs >> apply >> report
```

- **`apply` runs once for each item**, as `apply_0`, `apply_1` and so on, given its item as information, never as instructions: a job ad can't tell Athena what to do. An empty list runs it for none, and it's skipped, with what comes only after it.
- **Each item has a task of its own**: the run for one item sees that item, not the others, so long lists don't fill the model's context.
- **`report` runs once**, given what `apply` found for every item: a join. If `apply` failed for any item, `report` doesn't run, unless it has `trigger_rule="all_done"`.
- **Chains**: a task after `apply` with `follow_mapping=True` runs once for each item too, with what `apply` found for it, and not for an item `apply` was skipped or failed for: the other items carry on. Tasks in a group that should run for each item each need it, but the first.
- **A list for each item**: a task that runs for each item and returns a list fans out again, as `task_0_0`, `task_0_1`, ...

## Branches

```python
fits = AgenticBranch("fits", prompt="Does this job fit my CV?", allow_none=True)
apply = AgenticTask("apply", prompt="Apply to it.", follow_mapping=True)
note = AgenticTask("note", prompt="Note why it doesn't fit.", follow_mapping=True)
fits >> [apply, note]
```

A branch's model picks one of the tasks after it, by id, and the others are skipped. With `allow_none=True` it can pick `"none"`, so a branch with one task after it is a yes or no. With `multi_select=True` it can pick several. A task after a branch is given what the branch was given, not what it picked. For a branch after a list, each item has its own pick: so each job is applied for only if it fits.

## Checking a task's work

```python
draft = AgenticTask("draft", prompt="Draft a reply.")
check = AgenticEvaluator("check", prompt="Is it polite and specific?", max_retries=2)
send = AgenticTask("send", prompt="Send it.")
draft >> check >> send
```

`check` says whether `draft`'s answer will do. If it won't, `draft` runs again, told what was wrong, up to `max_retries` times; then what it has is passed on (`on_exhausted="pass_latest"`, the default) or the task fails (`"fail_dag"`). The tasks that use what `draft` found come after `check`, not after `draft`.

## Task groups

```python
with TaskGroup("each_email", allowed_tools=["read_email", "draft_email"]) as each_email:
    urgent = AgenticBranch("urgent", prompt="Is this one urgent?", allow_none=True)
    draft = AgenticTask("draft", prompt="Draft a reply to it.", follow_mapping=True)
    urgent >> draft
```

A group's tasks share its settings: `allowed_tools`, `hidden_tools`, `denied_tools`, `model`, `thinking`, `skills` and `max_tool_steps`, which a task can override. In `>>`, a group stands for its first tasks, after something, or its last, before something. Groups can be inside groups.

## Tools

- **Asking first**: a tool that asks you first, such as one that sends an email, is offered to every task, and asks in Telegram and in the agent's chat, saying which task wants it, when a task calls it. The task waits, without holding the model, for up to `[tasks] confirm_minutes` (60), and then it's declined. What you've allowed in the agent's chat doesn't count for its DAG.
- **`allowed_tools`** is the whole list of tools the tasks may use without asking. Left out, they may use any tool, asking first for those that do. Naming a tool that asks first lets it run without asking; any tool that isn't named, when there's a list, waits for your OK when it's called. A group's or a task's own list replaces the DAG's.
- **Always asking**: changing a DAG (`change_dag`) and handing work to Claude always ask, whatever `allowed_tools` says.
- **Hidden**: a tool in `hidden_tools`, or `[tools] on_demand`, isn't in the prompt: the model finds it with `search_tools` when it needs it.
- **The tool review**, at the end of each run, can propose the lists, for one task or for the DAG, from what the run used and what you allowed: you approve or reject it, and the file's changed where it's written. A change that would let a tool that asks first run without asking says so, plainly, wherever you're asked: in the run's summary, on the dashboard, and when Athena asks to change the file with `change_dag`.

Allowing a task to fetch or search without asking lets it send what it's read to those sites unseen, so allow only what it needs, where it needs it.

## Schedules

A cron schedule is five fields, minute, hour, day of the month, month and day of the week, in your time zone.

| Schedule | When |
|---|---|
| `"30 7 * * 1-5"` | 07:30 on weekdays |
| `"0 9,17 * * *"` | 09:00 and 17:00 every day |
| `"*/30 8-18 * * *"` | Every half hour from 08:00 to 18:30 |
| `"0 10 * * sun"` | 10:00 on Sundays |
| `"0 8 1 * *"` | 08:00 on the 1st of each month |
| `"@hourly"`, `"@daily"`, `"@weekly"`, `"@monthly"` | On the hour; at midnight; at midnight on Sundays; at midnight on the 1st |

A DAG runs at most every 15 minutes. One that's due while its last run is still waiting to start isn't queued again, and one missed while Athena was off isn't run late: it runs when it's next due.

## What starts a DAG

- **Its schedule**.
- **You**: **Run now** on its DAG view, or asking in a chat, which uses `run_dag`. A paused DAG still runs when you do.
- **A webhook**, an asset, or another DAG, in its `schedule`.

What came with what started it is given to the run's tasks as information, never instructions; a task that runs for each item is given its item, not all of it. `Task("items", fn=trigger_items, fan_out=True)` makes what came into items, one for each task after it: the list, if it's a list; its `items`, if it has them; or, if it's text, that text as one item.

### Webhooks and iPhone Shortcuts

Anything on your tailnet that can make an HTTP request can start a DAG: an iPhone Shortcuts automation when you arrive home, a script, or another service.

1. Set `TRIGGERS_TOKEN` in `~/.athena/.env` to at least 16 characters, such as the output of `openssl rand -hex 24`, and restart Athena. It's not the dashboard's password: all it can do is start DAGs.
2. Give a DAG the webhook, such as `schedule=[Webhook("arrive-home")]`. Its DAG view then shows the address to send it to, with a button to copy it.
3. Send it: `POST /hooks/arrive-home` on the dashboard's address, with the header `Authorization: Bearer <your token>`, and if there's something to pass on, a JSON body with `text`, and `items` for `trigger_items`:

```sh
curl -X POST https://pi.your-tailnet.ts.net/hooks/arrive-home \
  -H "Authorization: Bearer $TRIGGERS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Home at 18:02", "items": ["milk", "bread"]}'
```

In the Shortcuts app, under **Automation**, make one such as "When I arrive home", set to run immediately, with one action, **Get Contents of URL**: the address, Method **POST**, a header `Authorization` of `Bearer ` and your token, and, to pass something on, a Request Body of JSON.

A webhook's name is lower case with hyphens, such as `arrive-home`. What comes with it is cut short at 4,000 characters, with at most 20 items. The same webhook within a minute doesn't start its DAGs again. Over `[triggers] max_per_hour` (30) are refused in an hour, and so are requests after 10 wrong tokens in 10 minutes.

### Other agents' DAGs: assets

A task's `outlets` update assets, as in Airflow, and the DAGs with that asset in their schedule run, with what the task found: so one agent can start from what another finds. The master agent's DAG is the place for keeping an eye on things, for other agents to start from:

```python
# The master agent's DAG
from agentic_airflow import AgenticTask, Asset, Dag

with Dag("master", prompt="Keep an eye on things.", schedule=["@hourly"]):
    job_emails = AgenticTask(
        "job_emails",
        prompt="Look through my emails since the last run for new ones about jobs: one item for each.",
        return_type="list",
        fan_out=False,  # one list, for the asset
        outlets=[Asset("job-emails")],
    )
```

```python
# The DAG of an agent that applies for jobs
from agentic_airflow import AgenticBranch, AgenticTask, Asset, Dag, Task, trigger_items
from athena.dags import alert

with Dag("job_hunting", prompt="Apply for jobs that fit my CV.", schedule=[Asset("job-emails")]):
    jobs = Task("jobs", fn=trigger_items, fan_out=True)
    fits = AgenticBranch("fits", prompt="Does this job fit my CV?", allow_none=True)
    apply = AgenticTask("apply", prompt="Apply to it with my CV.", follow_mapping=True, on_complete=alert)
    report = AgenticTask("report", prompt="Say what was applied for, with links.")
    jobs >> fits >> apply >> report
```

- **What it found**: its answer, or for a task that ran for each item, the list of what each found.
- **Nothing found starts nothing**: an empty list, or no answer, doesn't update the asset.
- **No circles**: a DAG can't have one of its own outlets in its schedule, and a run started by an asset or another DAG never starts a DAG whose runs led to it, so agents can't start each other round in a circle.

`DagTrigger("other_dag")` starts a DAG when another's run finishes (successfully, unless `on=["success", "failed"]` says otherwise), with what each of its tasks found.

### When Claude finishes

`schedule=[Asset("claude-done")]` runs a DAG when Claude finishes a project you handed it, with the project and its pull request (see [Projects for Claude](claude-projects.md)).

## How a run goes

- **Runs take turns**: one at a time on each model, first come first served. A task holds the model from its first step to its answer, so it's never interrupted; your messages go before the next task, in the order you sent them. A task waiting for your OK doesn't hold the model, so your messages go ahead meanwhile.
- **Each task** is a request to the model, starting as the agent's chat does, with its guidance, files and what its last run kept, then what the run is, then what the tasks before it found, then the task. A task gets up to `[tasks] max_tool_rounds` (25) steps and `max_minutes` (30), not counting time it waits for your OK. If the model server can't be reached, it tries again after 30 seconds, then 2 minutes, and if it still can't, the rest of the run's tasks don't try.
- **A run that finds nothing**, with every task that ran returning nothing or an empty list, and every branch picking none, ends there, so checking for something often costs one task.
- **Otherwise it ends** with five tasks of its own: reviewing the workflow, the tools and the skills it used, which can propose changes for you to approve; keeping what the next run should know; and a summary, in the agent's chat, which tells you, and the master agent, when there's news. Each is given the run's log, a line for each task. See [Agents](agents.md#the-end-of-each-run).
- **A run's status** is how the DAG's own tasks went: one of the five failing doesn't make it a failure.
- **Restarts**: a run going when Athena stops carries on when it starts again, starting the task that was going again from its beginning, and a tool that was waiting for your OK is asked about again.

## When it won't read

The editor says what's wrong, and on which line. Some of the usual ones:

| It says | Do |
|---|---|
| `There's no task called "x" before this` | Assign the task before the line that names it. |
| `Write values out as they are` | No f-strings, variables, loops or calls: write the value itself. |
| `A DAG can call ..., not x` | Use one of the operators or triggers it lists. |
| `A Task here runs trigger_items, and nothing else` | `Task` is only for `trigger_items`: use `AgenticTask` for the model. |
| `A Branch runs Python, which a DAG here can't` | Use `AgenticBranch`. |
| `Branch 'x' has no downstream tasks to select from` | Put the tasks it picks from after it: `x >> [a, b]`. |
| `"x"'s DAG is called 'y' already` | Give this DAG an id of its own. |
| `This is written as Athena's DAGs were before` | A DAG from before agentic-airflow: write it again as this page says. |
| `cannot trigger itself` | An outlet can't be in the DAG's own schedule: give the asset another name. |
| `No tool matches x now` | Not an error: a tool it names isn't there now, say because its MCP server is off. |
