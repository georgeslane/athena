# Writing DAGs

An agent's DAG says what it does by itself: tasks, each a prompt for the model, run on a schedule or when something happens, one after another. It's written in Python, in the style of [Apache Airflow](https://airflow.apache.org/), which Athena reads but never runs, so a DAG, or a change Athena proposes to one, can never run code on the Pi.

You write it on the dashboard, in the **DAG** view of an agent (see [Agents](agents.md)): it's checked as you type, any mistake is shown on its line, and it's drawn as you go. **How to write it**, above the file, has this reference in brief. Athena can write one too, when you ask it to do something regularly: it shows you exactly what changes, and asks first.

## An example

```python
from athena import DAG, Task, TaskGroup, trigger

with DAG(
    schedule="0 8 * * *",  # every day at 08:00, in your time zone
    triggers=["new-email"],  # and when the event new-email comes in
    hidden_tools=["news_*"],  # tools its tasks find when they need them
    allowed_tools=["read_email"],  # tools that may run without asking you, in every task
):
    emails = Task(
        prompt="Which of my emails since the last run need a reply? One item for each.",
        return_type=list,  # a list: what for_each runs for
    )
    with TaskGroup("each_email", for_each=emails, allowed_tools=["draft_email"]) as each_email:
        urgent = Task(prompt="Is this one urgent? {item}", return_type=bool)  # a yes or no
        draft = Task(prompt="Draft a reply to {item}", run_if=urgent, alert=True)  # only if urgent said yes
        later = Task(prompt="Say why {item} can wait.", run_if=not urgent)  # only if it said no
        urgent >> [draft, later]
    report = Task(prompt="Say what was drafted.", outlets=["drafted"])
    each_email >> report
```

Every day at 08:00, `emails` lists the emails that need a reply. Then, for each one in turn, `urgent` says whether it's urgent, and `draft` drafts a reply if it is, or `later` says why it can wait if it isn't. `report` then says what was drafted, and starts the DAGs of any agents waiting for `drafted`, with what it said.

## The file

A DAG file has:

- **Imports**, such as `from athena import DAG, Task, TaskGroup, trigger`. They're there so it reads as Python: Athena doesn't need them.
- **One `with DAG(...):` block**, with everything else inside it.
- **Tasks**, each assigned to a name: `find = Task(prompt="...")`.
- **Task groups**: `with TaskGroup("each_job", for_each=jobs) as each_job:`, with tasks inside.
- **What comes after what**: `find >> apply`.

Every argument is written out as it is: a string, a list of strings, `True`, `False` or `None`. The only names you write are a task's, for `for_each` and `run_if`, `trigger`, for `for_each`, and a type, for `return_type`. Anything else, such as a loop, a call, an f-string or a variable, is refused, saying on which line, so a DAG can always be read without running it. A DAG has up to 30 tasks and 10 task groups.

## The DAG

`with DAG(...):` takes these, all of them optional:

| Argument | What it is |
|---|---|
| `schedule` | When it runs, in cron, in your time zone (see [Schedules](#schedules)). `None`, the default, runs it only when something starts it. |
| `triggers` | The events that start it, such as `["new-email", "claude-done"]`: from a webhook or an iPhone Shortcut, from Claude, or from another DAG's outlets (see [What starts a DAG](#what-starts-a-dag)). |
| `hidden_tools` | Tools its tasks find when they need them, rather than having them in every prompt, which keeps prompts shorter. Patterns such as `"news_*"` work. |
| `allowed_tools` | Tools that ask you first which every task may use without asking. Patterns work here too. |

## A task

`Task(...)` takes these, of which only `prompt` is needed:

| Argument | What it is |
|---|---|
| `prompt` | What to do. It's the message the model's given, with the agent's guidance, what its last run kept, and what the tasks before it found. |
| `task_id` | Its name: lower case letters, digits and `_`, unique in the DAG. It's the name it's assigned to, if it's not given. |
| `return_type` | What it gives the tasks after it: `str`, its answer, the default; `list`, a list of items; or `bool`, a yes or no. See [Lists](#lists-and-for_each) and [Yes or no](#yes-or-no-and-run_if). |
| `for_each` | Run it once for each item of a list: `for_each=emails`, for each item the task `emails` returns, or `for_each=trigger`, for each item that came with what started the run. Write `{item}` in its prompt where the item goes. |
| `run_if` | Run it only if a task with `return_type=bool` said yes, `run_if=urgent`, or no, `run_if=not urgent`. |
| `allowed_tools` | Tools that ask you first which this task may use without asking, as well as its group's and the DAG's. |
| `alert` | `True`: what it finds goes to the agent's chat, the master agent's, and to you, in Telegram and on your devices. |
| `outlets` | Events it starts when it finds something, such as `["job-emails"]`: the DAGs with that in their `triggers` run, with what it found. |

## What comes after what

```python
find >> apply  # apply runs after find
apply << find  # the same
[find, check] >> report  # report after both
find >> apply >> report  # a chain
each_job >> report  # after a group's last tasks; report >> each_job would be before its first
```

A task with `for_each` or `run_if` comes after the task it names without saying so. Tasks with nothing between them run in the order they're written. A loop, such as `a >> b >> a`, is refused.

A task runs once all those before it have. It's skipped if one of them failed (as "not run"), or if all of them were skipped, and so on down the line. A task after one that ran for each item, such as `report` above, counts it as done if it was done for any item.

## Lists and for_each

A task with `return_type=list` gives its list with the `task_result` tool, so it's always a list of strings: one for each item, saying all a task working on it needs to know, such as an email's sender, subject and what it asks. If the model answers without using it, it's asked once more, and the task fails if it still doesn't. A list has at most 20 items.

```python
jobs = Task(prompt="Find new data jobs that fit my CV, one item for each.", return_type=list)
apply = Task(prompt="Apply to this job: {item}", for_each=jobs)
```

- **`apply` runs once for each item**, given the item as information, never as instructions: a job ad can't tell Athena what to do. An empty list runs it for none, and it's skipped.
- **Each item has a thread of its own**: the run for one item sees what was found for it, not for the others, so long lists don't fill the model's context.
- **Once it's run for every item**, the next task is told what each run found.
- **Chains**: `send = Task(prompt="Send {item}", for_each=draft)`, where `draft` ran for each item, runs once for each of `draft`'s runs, with what it said as its item, and not for an item `draft` was skipped for.
- **A list for each item** is one list: `for_each=found`, where `found` returned a list for each search, runs for all of them.
- **What started the run**: `for_each=trigger` runs it once for each item that came with what started the run, such as the emails the master agent found, or the `items` a webhook sent. Text that came without items is one item.

## Yes or no, and run_if

A task with `return_type=bool` says yes or no with `task_result`, and a sentence on why: its tasks after it read "Yes. ..." or "No. ...".

```python
cheaper = Task(prompt="Is gold below £3,000 an ounce?", return_type=bool)
buy = Task(prompt="Say how much I could buy with £500.", run_if=cheaper, alert=True)
wait = Task(prompt="Note today's price.", run_if=not cheaper)
```

A task whose `run_if` isn't met is skipped, and so are the tasks after it, unless another task they come after ran: `report` after `[buy, wait]` always runs, after whichever did. In a group or with the same `for_each`, the yes or no is the one for the same item: so each email is drafted only if it's urgent.

## Task groups

```python
with TaskGroup("each_email", for_each=emails, allowed_tools=["draft_email"]) as each_email:
    urgent = Task(prompt="Is this one urgent? {item}", return_type=bool)
    draft = Task(prompt="Draft a reply to {item}", run_if=urgent)
    urgent >> draft
```

A group's tasks run once for each item of its `for_each`, in turn: all of them for the first item, then all for the next. Its tasks can't have a `for_each` of their own. Its `allowed_tools` apply to all of its tasks. In `>>`, a group stands for its first tasks, after something, or its last, before something. Groups can't be inside other groups. A group's id is lower case with `_`, and so is the name after `as`, which `>>` uses.

## Tools

- **In the prompt, or hidden**: every tool Athena has is in each task's prompt, unless the DAG's `hidden_tools` names it: then the model finds it with `find_tools` when it needs it.
- **Asking first**: a tool that asks you first, such as one that sends an email, asks in Telegram and in the agent's chat, saying which task wants it, and waits up to `[tasks] confirm_minutes` (60) for your answer. `allowed_tools` on the DAG, a group or a task lets it run there without asking. What you've allowed in the agent's chat doesn't count for its DAG.
- **Always asking**: changing a DAG (`change_dag`) and handing work to Claude always ask, whatever `allowed_tools` says.
- **The tool review**, at the end of each run, can propose `allowed_tools` for one task, or for every task, from what the run used and what you allowed: you approve or reject it, and the file's changed where it's written.

Allowing a task to fetch or search without asking lets it send what it's read to those sites unseen, so allow only what it needs, where it needs it.

## Schedules

`schedule` is cron, in your time zone: five fields, minute, hour, day of the month, month and day of the week.

| Schedule | When |
|---|---|
| `"30 7 * * 1-5"` | 07:30 on weekdays |
| `"0 9,17 * * *"` | 09:00 and 17:00 every day |
| `"*/30 8-18 * * *"` | Every half hour from 08:00 to 18:30 |
| `"0 10 * * sun"` | 10:00 on Sundays |
| `"0 8 1 * *"` | 08:00 on the 1st of each month |
| `"@hourly"`, `"@daily"`, `"@weekly"`, `"@monthly"` | On the hour; at midnight; at midnight on Sundays; at midnight on the 1st |

A DAG runs at most every 15 minutes. A scheduled run waits until Athena's been inactive for `[tasks] quiet_minutes` (5), and one missed by more than six hours, say while the Pi was off, is skipped to the next.

## What starts a DAG

- **Its schedule**.
- **You**: **Run now** on its DAG view, or asking in a chat, which uses `run_dag`.
- **An event in its `triggers`**, from any of these. It runs as soon as Athena's free, with what came with the event.

### Webhooks and iPhone Shortcuts

Anything on your tailnet that can make an HTTP request can send an event: an iPhone Shortcuts automation when you arrive home, a script, or another service.

1. Set `TRIGGERS_TOKEN` in `~/.athena/.env` to at least 16 characters, such as the output of `openssl rand -hex 24`, and restart Athena. It's not the dashboard's password: all it can do is start DAGs.
2. Give a DAG the trigger, such as `triggers=["arrive-home"]`. Its DAG view then shows the address to send it to, with a button to copy it.
3. Send it: `POST /hooks/arrive-home` on the dashboard's address, with the header `Authorization: Bearer <your token>`, and if there's something to pass on, a JSON body with `text`, and `items` for a task with `for_each=trigger`:

```sh
curl -X POST https://pi.your-tailnet.ts.net/hooks/arrive-home \
  -H "Authorization: Bearer $TRIGGERS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Home at 18:02", "items": ["milk", "bread"]}'
```

In the Shortcuts app, under **Automation**, make one such as "When I arrive home", set to run immediately, with one action, **Get Contents of URL**: the address, Method **POST**, a header `Authorization` of `Bearer ` and your token, and, to pass something on, a Request Body of JSON.

An event's name is lower case with hyphens, such as `arrive-home`. What comes with it is given to the run as information, never instructions, cut short at 4,000 characters, with at most 20 items. The same event within a minute doesn't start a DAG again. Over `[triggers] max_per_hour` (30) events in an hour are refused, and so are requests after 10 wrong tokens in 10 minutes.

### Other agents' DAGs: outlets

A task's `outlets` start the DAGs with the same event in their `triggers`, with what it found: so one agent can start from what another finds. The master agent's DAG is the place for keeping an eye on things, for other agents to start from:

```python
# The master agent's DAG
with DAG(schedule="@hourly"):
    job_emails = Task(
        prompt="Look through my emails since the last run for new ones about jobs: one item for each.",
        return_type=list,
        outlets=["job-emails"],
    )
```

```python
# The DAG of an agent that applies for jobs
from athena import DAG, Task, TaskGroup, trigger

with DAG(triggers=["job-emails"]):
    with TaskGroup("each_job", for_each=trigger) as each_job:
        fits = Task(prompt="Does this job fit my CV? {item}", return_type=bool)
        apply = Task(prompt="Apply to {item} with my CV.", run_if=fits, alert=True)
        fits >> apply
    report = Task(prompt="Say what was applied for, with links.")
    each_job >> report
```

- **A list** starts them with its items, and only if it has any.
- **A yes or no** starts them only on a yes; for one that ran for each item, with the items it said yes to.
- **Text** starts them with what it said; for one that ran for each item, with what it said for each, as items.

A DAG can't have one of its own outlets in its triggers, and a run started by an outlet never starts the DAG of an agent that led to it, so agents can't start each other round in a circle.

### When Claude finishes

`triggers=["claude-done"]` runs a DAG when Claude finishes a project you handed it, with the project and its pull request (see [Projects for Claude](claude-projects.md)).

## How a run goes

- **One task at a time**, each once those before it have finished, in the order they're written, and item by item for tasks that run for each item.
- **Each task** is a message, with what the tasks before it found; only the answers stay in the run's conversation, not the tools used on the way, so it stays short and the model server's cache of it holds. A task gets up to `[tasks] max_tool_rounds` (25) steps and `max_minutes` (30), not counting time it waits for your OK.
- **Your messages come first**: one you send while a task's running is answered once that task's finished, before the next starts.
- **A run that finds nothing**, with every list its tasks returned empty and every answer no, ends there, so checking for something often costs one task.
- **Otherwise it ends** with five tasks of its own: reviewing the workflow, the tools and the skills it used, which can propose changes for you to approve; keeping what the next run should know; and a summary, in the agent's chat, which tells you, and the master agent, when there's news. See [Agents](agents.md#the-end-of-each-run).

## When it won't read

The editor says what's wrong, and on which line. Some of the usual ones:

| It says | Do |
|---|---|
| `for_each needs a list` | Give the task it names `return_type=list`. |
| `run_if needs a yes or no` | Give the task it names `return_type=bool`. |
| `run_if needs a yes or no for the same item` | A task that runs once can't depend on one that ran for each item's yes or no: put both in the same group, or use the same `for_each`. |
| `{item} is the item a task runs for` | Give the task a `for_each`, or put it in a group with one. |
| `There's no task called "x" before this` | Assign the task before the one that names it. |
| `a DAG can't start itself` | An outlet can't be one of the DAG's own triggers: give it another name. |
| `Write prompt out as it is` | Values are written out, not worked out: no f-strings, calls or variables. |
| `No tool matches x now` | Not an error: a tool it names isn't there now, say because its MCP server is off. |
