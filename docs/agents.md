# Agents

Athena revolves around agents. Each agent is something you want Athena to keep an eye on, or do for you: specific items on eBay, new job postings, comments on your blog to reply to. You talk to it, and it works by itself, through its DAG.

- **Its chat:** talk to it on the dashboard's Agents tab, or in Telegram, Siri or the terminal, which talk to the main agent. The main agent is the one with the latest news, from you or from Athena.
- **Its DAG:** the tasks it does by itself, on a schedule or when something happens, written as Airflow-style Python. See [DAGs](#dags).
- **Its guidance:** how Athena acts in it, at a high level: what it's for, what matters, what to avoid. It's at the start of the agent's chat and of each run of its DAG.
- **Its context:** what its DAG's last run kept for the next: say, your CV and the jobs it's applied for, rather than the searches it made on the way. Each run's `save_context` task writes it, and you can read and change it.
- **Its model:** any of the model server's models, chosen in **⋯**. Otherwise it uses `[llm] model`.

Make one with **New agent**, and name, mute or delete it from the top of it: deleting it deletes what was said to it, here and in memory, its DAG and the history of its runs.

## DAGs

An agent's DAG is a file of Python in the style of [Apache Airflow](https://airflow.apache.org/), which Athena reads but never runs, so a change it proposes can never run code on the Pi.

```python
from athena import DAG, Task

with DAG(
    schedule="0 9 * * 1-5",          # cron, in your time zone; or None
    triggers=["watch:job-emails"],   # what else starts it: events, and what's watched
    hidden_tools=["news_*"],         # tools it finds when it needs them, rather than in every prompt
    allow=["fetch"],                 # tools that ask first which every task may use without asking
):
    find = Task(
        task_id="find_jobs",         # the name it's assigned to, if it's not given
        prompt="Find new data jobs that fit my CV, and trigger apply once for each good one.",
        allow=["browser_*"],         # tools this task may use without asking
    )
    apply = Task(
        task_id="apply",
        prompt="Apply to this job: {input}",
        triggered=True,              # once each time a task before it triggers it
        alert=True,                  # what it says goes to the agent's chat, and to you
    )
    report = Task(prompt="Say what was applied for, with links.")
    find >> apply >> report          # [a, b] >> c and a << b work too
```

The file holds imports, one `with DAG(...)` block, tasks assigned to names, and `>>` or `<<` between them. Every argument is written out as it is: a string, a list of strings, `True`, `False` or `None`. Anything else, such as a loop, a call or an f-string, is refused, with the line it's on. There are at most 30 tasks.

- **Each task** is a prompt, run as a message to the agent with everything it needs to go on: its guidance, what its last run kept, what's been said in its chat since, and what the tasks before it found. Its answer is what the tasks after it read.
- **A task runs** once those before it have finished. One is skipped if a task before it failed, or if all those before it were skipped. The run itself always goes on to the end.
- **A triggered task** (`triggered=True`) runs once each time a task before it calls `trigger_task` with something for it to do, such as a job's link: that's its `{input}`. It runs after all the tasks before it have finished, and not at all if nothing triggers it. A run triggers a task at most 20 times.
- **What starts it:** its `schedule`, in cron (minute, hour, day of the month, month, day of the week, at most every 15 minutes; `@daily` and the like work too), and its `triggers`: events, such as `arrive-home` from an iPhone Shortcut or `claude-done` when Claude finishes a project you handed it, and `watch:<key>` when something on the [Watching](#watching) tab happens. **Run now** runs it whenever you like.

### How runs are scheduled

DAGs run one at a time. When more than one is waiting, one you ran, or an event started, goes first; then the one with the fewest tasks; then the one that's waited longest. One you ran, or an event started, starts as soon as Athena's free. One that's due on its schedule, or was started by something watched, waits until Athena has been inactive (neither answering you nor running anything) for `[tasks] quiet_minutes` (5). So when two DAGs are due at the same time, the shorter runs first, and the other once it's finished and things have been quiet for five minutes. A scheduled run missed by more than six hours, say while the Pi was off, is skipped to the next.

### You come first

You can ask the agent anything while its DAG runs: your message is answered once the task that's running has finished, before the next one starts, and the agent knows what the run's found so far. The next task waits until you've been quiet for a minute, unless you started the run. While a task waits for your OK to use a tool, your messages go ahead.

### The end of each run

Every DAG ends with five tasks of its own, which aren't in its file:

1. **review_workflow** looks back at how well the run did what it's for, and can propose a new version of the DAG file: new tasks, changed prompts, tasks that should trigger others.
2. **review_tools** looks at the tools the run used, and can propose which to hide from the DAG's prompts, to be found when needed, and which to let run without asking next time. It's skipped if no tools were used.
3. **review_skills** looks at the skills the run used, and can suggest improvements, for you to approve on the Skills tab. It's skipped if no skills were used.
4. **save_context** decides what the next run should know, and keeps it as the agent's context.
5. **summary** tells you what the run did and found, and what it recommends. It always goes into the agent's chat, but only notifies you (in Telegram and on your devices) and makes the agent the main one when there's news, or something to approve.

What the reviews propose waits for you to approve or reject, on the agent's DAG view or the run's History, showing exactly what would change. A change proposed before the DAG changed in another way is out of date.

### History

The History view shows each agent's last 10 runs: what started each, how long it took, its summary, and its DAG as it was, drawn with how each task went. Choose a task for what it was given, what it said, and each tool it used: what that was given and gave back, whether it worked, and whether you allowed it.

## What it may do

- **Tools that ask first** still ask you, in Telegram and in the agent's chat, saying which task wants it, unless the DAG allows it (`allow`, on the DAG or the task). Unanswered after `[tasks] confirm_minutes` (60), it's denied. What you've allowed when talking to the agent doesn't count for its DAG.
- **Some always ask**, whatever the DAG says: changing a DAG (`change_dag`), and handing work to Claude.
- **Athena changes an agent's DAG** with `change_dag`, which shows you exactly what changes, and asks first. A run can't change its own DAG: its reviews propose changes instead.
- **What a task remembers, and skills it writes,** are suggestions for you to approve on the Memory and Skills tabs, and the summary says so.
- **Longer work:** a task gets more steps than a reply (`max_tool_rounds`, 25), up to `max_minutes` (30) of work, not counting time it waits for your OK, and thinks before answering. If the model server can't be reached, a task tries again after 10 minutes, then 30.

Allowing a task to fetch or search without asking lets it send what it's read to those sites unseen, so allow only what it needs.

## Triggers

Anything on your tailnet that can make an HTTP request can start a DAG with an event, such as an iPhone Shortcuts automation when you arrive home.

1. Set `TRIGGERS_TOKEN` in `~/.athena/.env` to at least 16 characters, such as the output of `openssl rand -hex 24`, and restart Athena. It's not the dashboard's password: all it can do is start DAGs you've made.
2. Give a DAG the trigger, such as `triggers=["arrive-home"]`.
3. In the Shortcuts app, under Automation, make one such as "When I arrive home", set to run immediately, with one action, **Get Contents of URL**:
   - **URL:** your dashboard's address, then `/hooks/arrive-home`
   - **Method:** POST
   - **Header:** `Authorization` = `Bearer ` and your token
   - **Request body (optional):** JSON, with `text`, to pass on anything that helps

What comes with an event is given to the run as information, never instructions, cut short at 4,000 characters. The same event within a minute doesn't start a DAG again. Over 30 events in an hour (`[triggers] max_per_hour`) are refused, and so are requests after 10 wrong tokens in 10 minutes.

## Watching

Ask Athena to keep an eye on something, such as "tell me about new emails about jobs" or "tell me if gold drops below 4,000", or add it on the Watching tab. Every hour, if anything's on the list and Athena's free, it checks all of it in one go:

- **With its tools,** such as email or Markets.
- **Each has a key,** such as `job-emails`. When it happens, the DAGs with `watch:job-emails` in their triggers run, with what was found. If none do, Athena tells you, in a message that starts 👀.
- **Between checks,** it keeps a short note on each, such as the last price it saw, or what it's passed on already.
- **It takes things off the list** once they no longer matter. Each is watched for 14 days unless you say otherwise, or with no end, such as "check my inbox every time".

The list costs nothing while it's empty. Checking uses tools that don't ask first; tools that do still ask you, unless they're in `heartbeat_tools`. Athena only adds to the list while answering you, so a web page it reads in a run can't put things on it. `heartbeat_minutes = 0` turns checking off.

## Projects for Claude

A task can hand a coding project to Claude with `hand_to_claude` (see [Projects for Claude](claude-projects.md)), which asks you first. When Claude finishes one, the event `claude-done` comes in, with the project and its pull request, so a DAG with `triggers=["claude-done"]` can review it, tell you, or carry on.
