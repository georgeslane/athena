# Tasks

Athena can work on things by itself, then message you what it found: a briefing every weekday at 7:30, research while you get on with your day, or a weekly look for MCP servers that would help. Ask it in Telegram, such as "every Sunday, look for MCP servers I'd find useful", or add one on the dashboard's Tasks tab, which has examples to start from.

- **You come first.** A task doesn't start while you're chatting, or within `quiet_minutes` (5) of your last message. Once it's running, a message from you stops what it's doing at once, so the model's yours, and it carries on once you've finished.
- **What it tells you:** its findings, as a message in Telegram, which goes into your chat too, so you can reply "tell me more about the second one". If there's nothing new, it stays quiet. The dashboard shows what each task said last time, and each tool it used: what it was given, what came back, and whether it failed or you declined it.
- **Longer work:** a task gets more steps than a reply (`max_tool_rounds`, 25), up to `max_minutes` (30) of work, and thinks before answering.
- **If it fails**, such as when the Mac's asleep, it tries again after 10 minutes, then 30, then tells you and waits for its next run.
- **Changing one:** on the Tasks tab, change its title, instructions, schedule and the tools it may use without asking. A new schedule counts from then, and gives a one-off that's done another run. A task can't be changed while it's running.

## When

- **Once:** `now`, `in 2h`, `once 09:00` (the next 09:00) or `once 2026-10-16 09:00`.
- **Regularly:** a cron schedule, with five fields for the minute, hour, day of the month, month and day of the week:
  - `30 7 * * 1-5`: 07:30 on weekdays
  - `0 18 * * mon,thu`: 18:00 on Mondays and Thursdays
  - `0 */6 * * *`: every six hours, on the hour
  - `0 9 1 * *`: 09:00 on the 1st of each month
  - `@daily`, `@weekly` and the like work too.

  Each field is `*`, a number, a range (`1-5`), a list (`1,15`) or a step (`*/15`), and months and days can be names. When both the day of the month and the day of the week are given, either will do, as in cron. A task runs at most every 15 minutes. Times are in your time zone: one skipped as the clocks go forward doesn't run, and one that happens twice as they go back runs once. A run missed by more than six hours, say while the Pi was off, is skipped.
- **When an event comes in:** `on arrive-home`. See [Triggers](#triggers).

## What it may do

- A task Athena suggests needs your OK: scheduling one asks first, showing what it'll do, when, and the tools it may use without asking. Tasks you add on the dashboard are yours already.
- A tool that asks first still asks you for a task, in Telegram, saying which task wants it, unless you've allowed that task to use it. Unanswered after `confirm_minutes` (60), it's denied.
- Scheduling another task always asks, so a task can't start others by itself.

Allowing a task to fetch or search without asking lets it send what's in its instructions and what it's read to those sites unseen, so allow only what it needs.

## Triggers

A task whose schedule is `on <event>`, such as `on arrive-home`, runs each time that event comes in. Anything on your tailnet that can make an HTTP request can send one, such as an iPhone Shortcuts automation when you arrive home.

1. Set `TRIGGERS_TOKEN` in `~/.athena/.env` to at least 16 characters, such as the output of `openssl rand -hex 24`, and restart Athena. It's not the dashboard's password: all it can do is start tasks you've made.
2. Give a task the schedule `on arrive-home`. The task shows the address to call.
3. In the Shortcuts app, under Automation, make one such as "When I arrive home", set to run immediately, with one action, **Get Contents of URL**:
   - **URL:** your dashboard's address, then `/hooks/arrive-home`
   - **Method:** POST
   - **Header:** `Authorization` = `Bearer ` and your token
   - **Request body (optional):** JSON, with `text`, to pass on anything that helps

What comes with an event is given to the task as information, never instructions, cut short at 4,000 characters. The same event within a minute doesn't run its tasks again. Over 30 events in an hour (`[triggers] max_per_hour`) are refused, and so are requests after 10 wrong tokens in 10 minutes.

## Watching

Ask Athena to keep an eye on something, such as "let me know when Claude finishes the bank project" or "tell me if gold drops below 4,000". Every `heartbeat_minutes` (30), if anything's on the list and you're not chatting, it checks all of it in one go:

- **With its tools,** such as Claude's board, Markets or email.
- **It tells you only what needs your attention,** as a message that starts 👀.
- **Between checks,** it keeps a short note on each, such as the last price it saw, or that it's told you.
- **It takes things off the list** once they've happened or stopped mattering. Each is watched for 14 days unless you say otherwise, or with no end, such as "check my inbox every time".

The Tasks tab shows the list, with each note, and what the last check found and did. You can add something to watch, change it, take it off, or check now.

The list costs nothing while it's empty. Checking uses tools that don't ask first; tools that do still ask you, unless they're in `heartbeat_tools`. Athena only adds to the list while answering you, so a web page it reads on a task can't put things on it. `heartbeat_minutes = 0` turns checking off.
