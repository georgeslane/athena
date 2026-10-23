# Agents

Athena revolves around agents. Each agent is something you want Athena to keep an eye on, or do for you: specific items on eBay, new job postings, comments on your blog to reply to. You talk to it, and it works by itself, through its DAG.

- **Its chat:** talk to it on the dashboard's Agents tab.
- **Its DAG:** the tasks it does by itself, on a schedule or when something happens, written in [agentic-airflow](https://github.com/georgeslane/agentic-airflow)'s Python and run by its engine. See [Writing DAGs](dags.md).
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
- **Its DAG keeps an eye on things**: say, your inbox every hour, for new emails about jobs, with an asset in its outlets that starts the DAG of the agent that applies for them. See [Assets](dags.md#other-agents-dags-assets).

## DAGs

An agent's DAG is a file of Python for [agentic-airflow](https://github.com/georgeslane/agentic-airflow), a library in the style of [Apache Airflow](https://airflow.apache.org/) whose engine runs it. Athena reads the file but never runs it, so a change it proposes can never run code on the Pi. [Writing DAGs](dags.md) has all of it: tasks that return a list, that run for each item of one, or only when a branch picks them, task groups, the tools each may use without asking, and what starts a DAG.

```python
from agentic_airflow import AgenticBranch, AgenticTask, Asset, Dag, Task, trigger_items
from athena.dags import alert

with Dag("job_hunting", prompt="Apply for jobs that fit my CV.", schedule=[Asset("job-emails")]):
    jobs = Task("jobs", fn=trigger_items, fan_out=True)  # each job the master agent's DAG found
    fits = AgenticBranch("fits", prompt="Does this job fit my CV?", allow_none=True)
    apply = AgenticTask("apply", prompt="Apply to it with my CV.", follow_mapping=True, on_complete=alert)
    report = AgenticTask("report", prompt="Say what was applied for, with links.")
    jobs >> fits >> apply >> report
```

Its DAG view shows when it runs and what starts it: its schedule; for each webhook, the address to send it to from an iPhone Shortcut or anything else; for each asset, which agent's task updates it. Below that are what its runs propose, the DAG drawn, and its file, which is checked as you write it.

### How runs take turns

Runs wait their turn, first come first served, and only one at a time runs on each model: two agents on the same model never run at once. A task holds the model from its first step to its answer, so it's never interrupted, then the next task, or the next run, takes its turn. A run that's due on its schedule while the last one's still waiting isn't queued again, and one missed while the Pi was off isn't run late.

### You come first

You can ask the agent anything while its DAG runs: your message waits for the task that's running to finish, then goes before the next one, and the agent knows what the run's found so far. Your messages go in the order you sent them, before anything Athena's waiting to do by itself. While a task waits for your OK to use a tool, it doesn't hold the model, so your messages go ahead.

### The end of each run

Every DAG ends with five tasks of its own, which aren't in its file, each given the run's log:

1. **review_workflow** looks back at how well the run did what it's for, and can propose a new version of the DAG file: new tasks, changed prompts, work that should run for each item of a list, or only when a yes or no says so.
2. **review_tools** looks at the tools the run used, and which task used each, and can propose which to hide from the DAG's prompts, to be found when needed, and the tools to let run without asking next time, in one task or in every task. It's skipped if no tools were used.
3. **review_skills** looks at the skills the run used, and can suggest improvements, for you to approve on the Skills tab. It's skipped if no skills were used.
4. **save_context** decides what the next run should know, and keeps it as the agent's context.
5. **summary** tells you what the run did and found, and what it recommends. It always goes into the agent's chat, but only notifies you (in Telegram and on your devices), and tells the master agent, when there's news, or something to approve.

A run that finds nothing, with every list its tasks returned empty and every branch picking none, ends without them: it has nothing to look back on, and checking for something often should cost little. One of them failing doesn't make the run a failure.

What the reviews propose waits for you to approve or reject, on the agent's DAG view or the run's History, showing exactly what would change. A change proposed before the DAG changed in another way is out of date.

### History

The History view shows each agent's last 10 runs: what started each, and what came with it, how long it took, its summary, and its DAG, drawn with how each task went. Choose a task for what it was given, what it said and, for a branch, what it picked, and each tool it used: what that was given and gave back, whether it worked, and whether you allowed it. A task that ran for each item has a run for each, with its item.

## What it may do

- **Tools that ask first** still ask you, in Telegram and in the agent's chat, saying which task wants it, unless the DAG allows it (`allowed_tools`, on the DAG, a task group or a task: the whole list of tools that may run without asking). Unanswered after `[tasks] confirm_minutes` (60), it's declined. What you've allowed when talking to the agent doesn't count for its DAG.
- **Some always ask**, whatever the DAG says: changing a DAG (`change_dag`), and handing work to Claude.
- **Athena changes an agent's DAG** with `change_dag`, which shows you exactly what changes, and asks first. A run can't change its own DAG: its reviews propose changes instead.
- **What a task remembers, and skills it writes,** are suggestions for you to approve on the Memory and Skills tabs, and the summary says so.
- **Longer work:** a task gets more steps than a reply (`max_tool_rounds`, 25), up to `max_minutes` (30) of work, not counting time it waits for your OK, and thinks before answering. If the model server can't be reached, a task tries again after 30 seconds, then 2 minutes, and if it still can't, the rest of the run doesn't try.

Allowing a task to fetch or search without asking lets it send what it's read to those sites unseen, so allow only what it needs.

## Projects for Claude

A task can hand a coding project to Claude with `hand_to_claude` (see [Projects for Claude](claude-projects.md)), which asks you first. When Claude finishes one, the asset `claude-done` is updated, with the project and its pull request, so a DAG with `Asset("claude-done")` in its schedule can review it, tell you, or carry on.
