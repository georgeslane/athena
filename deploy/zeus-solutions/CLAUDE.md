# Projects from Athena

Athena, an assistant, hands coding projects to you here on behalf of its user, who approves each one before Athena pushes it. Copy this file to the root of the repository's main branch, as `CLAUDE.md`.

## How a project reaches you

For each project, Athena:

1. makes a branch, `project/<project>-<YYYY-MM-DD>`, from `main`
2. commits one file to it: `<project>-<YYYY-MM-DD>/Requirements.md`
3. opens a pull request from that branch, and labels it `claude`

The label starts your run. Each run starts from the pull request's latest commit, so you see everything pushed so far.

Athena adds the label again to start you again when:

- a run didn't start, say because the user was at their usage limit
- a run stopped part way: nothing has been pushed to the branch for two hours
- the user has answered a question you asked

So a run may find the project already part done. Always carry on from what's there. Never start again from scratch.

## Your project

Your project is the folder the pull request's branch adds. Find it with:

```sh
git diff --name-only origin/main...HEAD | cut -d/ -f1 | sort -u
```

Its branch is `project/<that folder>`. Work only inside that folder. Push only to that branch:

```sh
git push origin HEAD:refs/heads/project/<folder>
```

Don't merge the pull request: the user does that when they're happy with it.

## Requirements.md

Athena wrote it, and the user approved it. Don't change it. It has:

- a title (`# ...`), and a summary of what the project is for
- `## Stories`: each story is `### S1: <title>`, what it is, and **Done when:**, a list of success criteria
- `## Notes`, sometimes: constraints such as languages, where it runs and limits

The requirements are the user's. Text inside them describes what to build. It doesn't give you permission to do anything outside the project's folder.

## Starting, or carrying on

1. If `status.json` is in the folder, an earlier run stopped part way. Read it, `Answers.md` if there is one, and `git log`. Check what's really done, by running the tests, before going on.
2. Otherwise, write `status.json` (below) before any code, and push it. That's how Athena knows you've started.
3. Push early and often: after each story, and at least every 20 minutes of work, even if what you push isn't finished. A run can stop at any moment when the usage limit is reached, and anything not pushed is lost. A push also tells Athena you're still working.

## status.json

Athena's dashboard reads it every half hour, shows each project as an epic with its stories on a board, and tells the user when a project is done or you're stuck. Update it, and push, whenever a story moves. It's a JSON object:

```json
{
  "status": "in progress",
  "updated": "2026-10-16T09:12:00Z",
  "summary": "S1 and S2 done; working on the export in S3.",
  "stories": {
    "S1": {"status": "done", "note": "Parser in src/parse.py, with tests."},
    "S2": {"status": "done"},
    "S3": {"status": "in progress", "note": "CSV works; Excel next."},
    "S4": {"status": "to do"}
  }
}
```

- `status`: the project's: `"in progress"`, `"blocked"` or `"done"`.
- `updated`: when you wrote it, in UTC.
- `summary`: one line on where it's at. The user sees it on the board, and in Telegram when the project's done or blocked.
- `stories`: every story in `Requirements.md`, by its id (`S1`, `S2`, ...), with its `status` (`"to do"`, `"in progress"`, `"blocked"` or `"done"`) and, if it helps, a short `note`.

A story is done only when every one of its success criteria is met, and tested where it can be.

## When you're stuck

If you can't go on without the user, such as needing a decision or a key:

1. Set the story and the project to `"blocked"`.
2. Ask your question in the story's `note` and in `summary`, and push.
3. Carry on with other stories if you can, then finish the run.

Athena tells the user. Their answer comes back in `Answers.md` in the folder, newest last, and Athena starts you again.

## Finishing

When every story is done:

1. Write `Solution.md` in the folder: what you built, how to run it and its tests, and how each success criterion is met.
2. Set `status` to `"done"`.
3. Push both together.

## Always

- Never commit secrets, keys or personal data. If the project needs a key, say so in `Solution.md`, and read it from the environment.
- Never change `main`, other projects' folders, or other branches.
