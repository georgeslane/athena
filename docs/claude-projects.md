# Projects for Claude

Athena can hand coding projects to Claude, which works on them by itself and reports back. A skill of yours can say when, such as "give coding tasks of moderate complexity or more to Claude".

1. **Athena hands a project over** with `hand_to_claude`: a title, a summary, and stories, each with what must be true for it to be done. It always asks you first, in Telegram, showing everything it will push, even in a task allowed to use other tools without asking. Once you allow it, Athena makes a branch, `project/<project>-<YYYY-MM-DD>`, commits the project's `Requirements.md` to it, opens a pull request and labels it `claude`.
2. **The label starts Claude**: a [routine](https://code.claude.com/docs/en/routines) with a GitHub trigger on pull requests being labelled, which follows `CLAUDE.md` at the root of the repo.
3. **Claude pushes its work to the branch** as it goes. `status.json` says where the project and each story are, and `Solution.md`, when it's done, says how each success criterion is met. You merge the pull request when you're happy with it, or close it to call the project off.
4. **Athena reads the pull requests** every `poll_minutes` (30), and tells you in Telegram when a project's done or Claude's stuck. When one's done, the asset `claude-done` is updated, with the project and its pull request, so an agent's DAG with `Asset("claude-done")` in its schedule can carry on from there (see [Agents](agents.md#projects-for-claude)).
5. **When Claude's stuck**, it asks you in its note. Tell Athena the answer: `answer_claude` adds it to `Answers.md` on the branch, after asking you, and starts Claude again.
6. **For more work on a project**, name it to Athena, such as "add a CSV export to the bank statements project". `extend_claude_project` adds new stories after asking you, and starts Claude on them: on the same branch if its pull request is still open, or as a new round, with a pull request of its own, if it's merged.

**When you're at your usage limit**, a run that would start is rejected, and one that reaches it stops part way. Neither is tried again, so Athena watches for both: no `status.json` 30 minutes (`start_minutes`) after the label, or nothing pushed for 2 hours (`stalled_minutes`). It starts Claude again by taking the label off and putting it back, waiting twice as long before each next try, up to `max_restarts` (6) times. Claude carries on from what it pushed.

Athena reads the repo rather than receiving webhooks: the Pi has no address the internet can reach, and that stays so.

## Set it up

1. Make a **private** repository on GitHub. Athena refuses to push to a public one, since requirements can be personal.
2. Put [`claude-projects/CLAUDE.md`](claude-projects/CLAUDE.md) at its root, on `main`, as `CLAUDE.md`.
3. Make a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to **only that repository**, and the permissions **Contents** and **Pull requests**, both read and write.
4. On the dashboard's Tools tab, open **Projects for Claude**: set the repository (`owner/name`), paste the token, and switch it on. Then run `athena doctor`.
5. Install the [Claude GitHub App](https://github.com/apps/claude) on the repository, and make a routine at [claude.ai/code/routines](https://claude.ai/code/routines):
   - **Repository:** this one.
   - **Prompt:** "A pull request labelled `claude` has asked you to work on a project in this repository. Follow CLAUDE.md at its root."
   - **Trigger:** GitHub event, **Pull request**, action **labeled**, with the filters **Labels** contains `claude`, **Head branch** starts with `project/`, and **Is merged** is false.
   - **Environment:** network access for whatever the projects need, such as package registries.

What Athena pushes is [checked for personal details](privacy-and-security.md#privacy) first, including what you type into **Add stories…**, so Claude only ever gets tasks anyone could ask.
