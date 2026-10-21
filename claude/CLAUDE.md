# Cloud session rules (claude-cloud-kit)

You work for Devin, a solo engineer, in a Claude Code cloud session. He often drives from
his phone. A repository's own CLAUDE.md / AGENTS.md wins over this file.

## How to work
- Get things done. Make the call yourself, say what you chose in one line, keep it
  reversible, continue. Ask only for credentials, new spending, contacting people, or
  destroying data without a backup.
- Write to Devin in plain, short sentences, active voice. Report what you did and what
  you decided, not options. Lead with the result.
- Run the smallest existing tests that cover the change. CI runs the full suite.
- Verify before you say done: name the command you ran and its result.

## Session setup
- `cloud-prep` ran at session start: it picked the repo's Node version and is installing
  dependencies in the background. Run `cloud-prep --wait` before the first test, build
  or lint. If it reports a failure, read its log and fix the install.
- Node 22, 24 and 26 are in `/opt/node<major>`; Python uses `uv`.
- `gh pr` and `gh issue` do not work here (GraphQL is blocked). Use `gh api repos/{owner}/{repo}/...`.

## Git
- Work on a feature branch (`feat/…`, `fix/…`, `chore/…`), never on main.
- Conventional commits: `type(scope): subject`. No AI attribution lines, no
  `Co-Authored-By`, no "Generated with" footers in commits, PR bodies or comments.
- Push with `git push -u origin HEAD`. Force-push only your feature branch, with `--force-with-lease`.

## Landing — `land`
When Devin says "land it", "ship it" or "merge it":
1. Commit everything on the feature branch.
2. For a non-trivial change, first have a reviewer subagent check the full diff against
   origin's default branch. Fix verified bugs, then commit.
3. Run `land` with a long timeout (it waits for CI; up to 45 min). It pushes, opens or
   reuses the PR, waits for green CI, squash-merges, then switches to the synced default branch.
   - `land --pr` only opens the PR. `land --dry-run` shows the plan.
   - `land --title "…" --body-file pr.md` sets the PR text (write a real summary and test plan for big changes).
4. Read the exit code: 0 landed (or stopped at the PR by policy), 1 nothing merged
   (read the reason, fix, run again), 3 merged but the follow-up failed (finish by hand, never re-run).
- Client repos and repos Devin does not own: `land` opens the PR and stops. Tell Devin the PR link.
- Never merge any other way: no `gh api …/merge`, no pushing to main. A hook blocks those.
- If CI fails, read the failing job's log (`gh api repos/{o}/{r}/actions/runs/{id}/jobs`, then
  `…/actions/jobs/{job_id}/logs`), fix the cause, push, run `land` again.

## Data and secrets
- No production database work from here. Database changes ship as committed migrations;
  Devin's machine applies them with a backup first.
- There are no secrets in this VM. Do not ask for them or print environment tokens.
