# Agents

Athena revolves around agents. Each agent is something you want Athena to keep an eye on, or do for you: specific items on eBay, new job postings, comments on your blog to reply to. You talk to it, and it works by itself, through its DAG.

- **Its chat:** talk to it on the dashboard's Agents tab.
- **Its DAG:** the tasks it does by itself, on a schedule or when something happens, written as Airflow-style Python. See [Writing DAGs](dags.md).
- **Its guidance:** how Athena acts in it, at a high level: what it's for, what matters, what to avoid. It's at the start of the agent's chat and of each run of its DAG.
- **Its files:** your CV, notes, or anything else it should know, added with its guidance: PDFs and text files. Its chat and its runs are told they're there, and read them when they'd help.
- **Its context:** what its DAG's last run kept for the next: say, the jobs it's applied for, rather than the searches it made on the way. Each run's `save_context` task writes it, and you can read and change it.
- **Its model:** any of the model server's models, chosen in **⋯**. Otherwise it uses `[llm] model`.

Make one with **New agent**, and name, mute or delete it from the top of it: deleting it deletes what was said to it, here and in memory, its DAG, the history of its runs and its files.

## The master agent

The master agent is always there, first on the Agents tab, and can't be deleted.

- **Telegram, Siri and the terminal talk to it**, and only to it: it's your conversation with Athena, wherever you are. `/new` starts it afresh.
- **It hears your other agents' news**: when a run of another agent's DAG has news, or a task with `alert=True` finds something, it's told to the master agent as well as to you, so you can ask about it there. It knows what each agent is for and when it runs, and its `agent_runs` tool reads what any of their runs did, task by task.
- **It can act on them**: ask it to change another agent's DAG, or run it now, by name. A change still shows you exactly what changes, and asks first.
- **Its DAG keeps an eye on things**: say, your inbox every hour, for new emails about jobs, with an outlet that starts the DAG of the agent that applies for them. See [Outlets](dags.md#other-agents-dags-outlets).

## DAGs

An agent's DAG is a file of Python in the style of [Apache Airflow](https://airflow.apache.org/), which Athena reads but never runs, so a change it proposes can never run code on the Pi. [Writing DAGs](dags.md) has all of it: tasks that return a list or a yes or no, tasks that run for each item of a list, or only when a yes or no says so, task groups, the tools each may use without asking, and what starts a DAG.

```python
from athena import DAG, Task, TaskGroup, trigger

with DAG(triggers=["job-emails"]):  # what the master agent's DAG finds in your emails
    with TaskGroup("each_job", for_each=trigger, allowed_tools=["fetch"]) as each_job:
        fits = Task(prompt="Does this job fit my CV? {item}", return_type=bool)
        apply = Task(prompt="Apply to {item} with my CV.", run_if=fits, alert=True)
        fits >> apply
    report = Task(prompt="Say what was applied for, with links.")
    each_job >> report
```

Its DAG view shows when it runs and what starts it: its schedule, and for each trigger, which agent's task sends it, and the address to send it to from an iPhone Shortcut or a webhook. Below that are what its runs propose, the DAG drawn, and its file, which is checked as you write it.

### How runs are scheduled

DAGs run one at a time. When more than one is waiting, one you ran, or an event started, goes first; then the one with the fewest tasks; then the one that's waited longest. One you ran, or an event started, starts as soon as Athena's free. One that's due on its schedule waits until Athena has been inactive (neither answering you nor running anything) for `[tasks] quiet_minutes` (5). So when two DAGs are due at the same time, the shorter runs first, and the other once it's finished and things have been quiet for five minutes. A scheduled run missed by more than six hours, say while the Pi was off, is skipped to the next.

### You come first

You can ask the agent anything while its DAG runs: your message is answered once the task that's running has finished, before the next one starts, and the agent knows what the run's found so far. The next task waits until you've been quiet for a minute, unless you started the run. While a task waits for your OK to use a tool, your messages go ahead.

### The end of each run

Every DAG ends with five tasks of its own, which aren't in its file:

1. **review_workflow** looks back at how well the run did what it's for, and can propose a new version of the DAG file: new tasks, changed prompts, work that should run for each item of a list, or only when a yes or no says so.
2. **review_tools** looks at the tools the run used, and which task used each, and can propose which to hide from the DAG's prompts, to be found when needed, and which to let run without asking next time, in one task or in every task. It's skipped if no tools were used.
3. **review_skills** looks at the skills the run used, and can suggest improvements, for you to approve on the Skills tab. It's skipped if no skills were used.
4. **save_context** decides what the next run should know, and keeps it as the agent's context.
5. **summary** tells you what the run did and found, and what it recommends. It always goes into the agent's chat, but only notifies you (in Telegram and on your devices), and tells the master agent, when there's news, or something to approve.

A run that finds nothing, with every list its tasks returned empty and every answer no, ends without them: it has nothing to look back on, and checking for something often should cost little.

What the reviews propose waits for you to approve or reject, on the agent's DAG view or the run's History, showing exactly what would change. A change proposed before the DAG changed in another way is out of date.

### History

The History view shows each agent's last 10 runs: what started each, and what came with it, how long it took, its summary, and its DAG as it was, drawn with how each task went. Choose a task for what it was given, what it said and returned, and each tool it used: what that was given and gave back, whether it worked, and whether you allowed it. A task that ran for each item has a run for each, with its item.

## What it may do

- **Tools that ask first** still ask you, in Telegram and in the agent's chat, saying which task wants it, unless the DAG allows it (`allowed_tools`, on the DAG, a task group or a task). Unanswered after `[tasks] confirm_minutes` (60), it's denied. What you've allowed when talking to the agent doesn't count for its DAG.
- **Some always ask**, whatever the DAG says: changing a DAG (`change_dag`), and handing work to Claude.
- **Athena changes an agent's DAG** with `change_dag`, which shows you exactly what changes, and asks first. A run can't change its own DAG: its reviews propose changes instead.
- **What a task remembers, and skills it writes,** are suggestions for you to approve on the Memory and Skills tabs, and the summary says so.
- **Longer work:** a task gets more steps than a reply (`max_tool_rounds`, 25), up to `max_minutes` (30) of work, not counting time it waits for your OK, and thinks before answering. If the model server can't be reached, a task tries again after 10 minutes, then 30.

Allowing a task to fetch or search without asking lets it send what it's read to those sites unseen, so allow only what it needs.

## Projects for Claude

A task can hand a coding project to Claude with `hand_to_claude` (see [Projects for Claude](claude-projects.md)), which asks you first. When Claude finishes one, the event `claude-done` comes in, with the project and its pull request, so a DAG with `triggers=["claude-done"]` can review it, tell you, or carry on.
