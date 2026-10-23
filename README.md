# claude-cloud-kit

Makes Claude Code cloud sessions (claude.ai/code, the Claude app, `claude --cloud`) ready to
work and easy to land, without giving up the safety rules of the local setup.

## Install (once per Claude account)

claude.ai/code → environment selector → **Default** → edit → **Setup script**:

```bash
curl -fsSL https://raw.githubusercontent.com/ZeroSum-Solutions/claude-cloud-kit/main/setup.sh | bash || true
```

Network access: **Limited** with **Allow package managers** checked (the kit needs nodejs.org,
npm, PyPI and raw.githubusercontent.com, which are all on that default list), plus these
**Allowed domains** so `docker pull` works for a local Supabase stack:

```text
registry-1.docker.io
auth.docker.io
production.cloudflare.docker.com
production.cloudfront.docker.com
ghcr.io
pkg-containers.githubusercontent.com
public.ecr.aws
d5l0dvt14r5h8.cloudfront.net
```

The default list names the registries but not where two of them serve image layers from:
GHCR redirects to `pkg-containers.githubusercontent.com` and ECR Public to a CloudFront host
(`d5l0dvt14r5h8.cloudfront.net` on 2026-10-21). If an ECR pull fails with `host_not_allowed`,
the error names the new host; add it. Edit the Default environment rather than adding a second
one, so `claude --cloud` picks it without `/remote-env`.

## What a session gets

| Piece | What it does |
|---|---|
| `setup.sh` | Runs before Claude Code starts; the VM snapshot is cached ~7 days. Installs the kit into `/opt/claude-cloud-kit`, Node 24 and 26 (SHA-256 checked), `land` and `cloud-prep` on PATH, and `~/.claude/CLAUDE.md` plus `settings.json`. Every kit file is checked against `MANIFEST.sha256`, compiled and smoke-tested (the guard must allow `ls` and deny a push to `main`) before it replaces the current kit. The installer lock is an `flock` the kernel releases when its holder dies. |
| `cloud-prep` (SessionStart) | Refreshes the kit (60 s cap), picks the repo's Node version (`.nvmrc`, `.node-version`, `mise.toml`, `.mise.toml`, `.tool-versions`, `engines`; ranges and `lts/<name>` understood), installs dependencies in the background (pnpm, npm, yarn, bun, uv, poetry; frozen lockfiles; skipped when the lockfile is unchanged), and tells Claude the landing policy. Foreground work has a 180 s budget; if the pinned Node can't be installed in it, cloud-prep uses the newest Node in `/opt` (else the one on PATH) and puts it on the session PATH. Repos that use pnpm or yarn get their corepack shims before the session starts. `cloud-prep --wait` blocks until the install is done, and reports `stale` when the lockfiles or the Node pin changed after the last install (run `cloud-prep` to install again). |
| `land` | REST-only landing (the cloud GitHub proxy blocks GraphQL, so `gh pr` and zs-land cannot run). Push → open or reuse the PR → wait for CI → squash-merge → switch to the synced default branch. |
| `hooks/guard.py` (PreToolUse) | A seatbelt for pushes, merges, direct API writes and remote databases. `git push` runs only as its own command, `[cd <path> &&] git push [-u] [-q] [--force-with-lease[=<ref>]] [--force-if-includes] [<remote> [<refspec>]]`, to a branch other than main, master or the default branch; any other command with the word `push` next to git, a wrapper or a shell expansion is refused. Matching the raw command text, it refuses merges (`gh pr merge`, the merge endpoints, auto-merge), repo delete/archive/rename/edit/sync, rulesets and protection, `gh api` writes to contents, git refs and objects, hooks, Actions settings and environments (any other client is refused on those api.github.com paths, reads included), a hosted-Postgres host anywhere in the command, `supabase` in command position with a remote verb or flag (dump, pull, push, link, repair, fetch, inspect, `--linked`, `--db-url`, `--project-ref`; `--local` does not exempt them), psql, pg_dump, pg_restore, pgcli or usql in command position aimed at a non-local host (an empty host means the inherited `PGHOST`; a set `PGSERVICE` refuses) and database env vars set to a remote value. Local work: `supabase start`, `supabase db reset`, `supabase migration up`. Cloud sessions can't change repo settings, rulesets or protection anyway: the Claude GitHub App has no administration write. |
| `claude/CLAUDE.md` | Devin's working rules for cloud sessions: conventional commits, no AI attribution, how and when to land, no production data work. |
| `claude/settings.json` | The two hooks, no attribution lines (commits, PRs, session links), longer Bash timeouts. |

## Landing rules (same as zs-land)

- Refuses main, master, the default branch, a detached HEAD, a dirty tree and a branch with
  no commits. Pushes the exact SHA it read, so a commit made meanwhile is never merged.
- Merges only for owners `wiggdevin` and `ZeroSum-Solutions`, and never for a repo (or its
  pre-rename name) in `policy/client-repos.txt`. Those get the PR and stop for review. If
  that file cannot be read, nothing auto-merges.
- Lands only an open, non-draft PR whose base is the current default branch. Right before
  the merge it waits until GitHub has computed mergeability, reads the default branch, CI
  and HEAD, then reads the PR once more, judges every PR condition on that last read, and
  pins the merge to the head SHA.
- CI: every non-advisory check run in GitHub's latest view (the check-runs API's default
  `filter=latest`, the view `gh pr checks` and zs-land judge: a re-run replaces its earlier
  attempt) and commit status must pass or be skipped, with at least one real pass. Failure, cancelled,
  neutral, timed out and action required block. Only skips, or no checks, after 2 minutes
  fails closed (merge by hand on GitHub if a repo has no CI). A pass counts only 45 s after
  this run's push and its PR read, so PR workflows have time to register.
- Once the check runs pass, `land` also requires `mergeable` true and `mergeable_state`
  `clean` or `has_hooks`: GitHub's own roll-up of every status and check. That covers commit
  statuses the cloud GitHub app cannot read (403). `unstable`, `blocked` or
  unknown waits `LAND_BLOCKED_WAIT`, then exits 1, so a failing advisory check means a merge
  by hand.
- The squash commit message is the commit list, set explicitly. The cloud proxy appends a
  "Generated by Claude Code" footer to PR bodies; `land` strips it, and it never reaches
  the default branch.
- Exit codes: 0 landed or stopped by policy · 1 nothing merged (including GitHub refusing
  the merge with HTTP 405/409/422 and then reporting the PR unmerged; a PR found merged at
  the pushed SHA counts as landed) · 3 merged, or merge result still unknown after
  `LAND_MERGE_CONFIRM` s, but the follow-up failed (finish by hand, do not re-run) · 2 usage.
- The proxy rejects branch deletion. `land` deletes the head branch through the REST API
  when GitHub's "Automatically delete head branches" is off, only while it still points at
  the merged commit, and reports it if that fails.
- If the default branch is checked out in another worktree, `land` leaves a detached HEAD at
  `origin/<default>`. It keeps both branches unless GitHub reports the merged head as the
  pushed SHA, and reports a failed switch instead of crashing.

Environment knobs: `LAND_OWNERS`, `LAND_CLIENT_REPOS`, `LAND_ADVISORY_CHECKS` (comma
separated), `LAND_CI_TIMEOUT` (seconds, default 2700), `LAND_NO_CI_GRACE` (120),
`LAND_PR_SETTLE` (45), `LAND_BLOCKED_WAIT` (300), `LAND_MERGE_CONFIRM` (30).

## Client repos

`policy/client-repos.txt` takes shell globs, plain names for non-client repos that need a
human review (this kit), and `id:<repository id>` lines, so this public repo does not name
clients. An id survives renames and transfers; `land` checks it before it merges. Never write
a client name anywhere in this repo, tests included. Add one:

```bash
gh api repos/<owner>/<repo> --jq '"id:\(.id)"'
```

The `sha256:<hex>` lines are the older form (a hash of the lowercased `owner/repo`). They
stay until the pre-purge VM snapshots expire (about 2026-10-29), because those installers
fetch files without hashes and an old `land` can read this file; then remove them. A new
`land` refuses to merge if its policy module cannot check ids. New entries use ids.

## Changing the kit

- Everything shipped reaches every session at its next start: the SessionStart hook
  refreshes the kit from `main`, and the refreshed installer re-merges `~/.claude` config.
  The exception is the first session on a snapshot from before 2026-10-21, whose hook
  refreshes only the kit (see Limits). The hook keeps the current kit if
  a fetch fails, a file does not match `MANIFEST.sha256` (raw.githubusercontent.com caches
  files for minutes, so right after a merge a session may keep the old kit once), or the new
  kit does not compile or pass its smoke test.
- After changing any shipped file, run `scripts/write-manifest` (a test fails until you do).
  `MANIFEST` stays a plain path list for kits installed before the hashes existed.
- This repo is on the human-review list: `land` opens the PR and stops. `main` takes changes
  only through a PR with green `ci` (repo ruleset). That is the supply-chain gate for code
  that runs as root in every session.
- Tests: `uv run --with pytest python -m pytest -q tests`. CI runs them on Ubuntu, the cloud VM's OS.

## Limits

- `land` deletes a remote branch with a check then a delete; the REST API has no atomic
  form. A local default branch checked out in another worktree is not fast-forwarded.
- The one refresh from the b35b3fa installer has no lock exclusion with it.
- A Node pin narrower than a major (e.g. `<24.1`) installs that major's latest release.
  Poetry venvs outside the repo are not checked, so a deleted one is not reinstalled until
  `poetry.lock` or `pyproject.toml` changes.
- The guard is a seatbelt for mistakes, not a sandbox. Branch rulesets on GitHub are the hard
  gate, and the Claude GitHub App's permissions (no administration write) keep repo settings,
  rulesets and protection out of reach. The guard covers pushes, merges, direct writes through
  the API, and remote databases by text match only; it does not inspect PR state or base
  changes, GraphQL (the cloud proxy blocks it), an API path held in a variable, or git aliases
  (a saved alias that pushes is not seen; the push ruleset still applies). A bare `git push` is
  judged against main, master and origin's default branch; when the branch tracks another
  remote whose default differs, that push is left to the remote's rulesets (name the remote,
  `git push upstream <branch>`, and the guard checks that remote's default). A remote database
  host that is not on the hosted-Postgres list (Supabase, Neon, RDS, Render, Railway, Aiven,
  Cockroach) is caught only in a psql-style `-h`/`host=`/URL form.
- Because it matches raw text, it refuses some harmless commands on purpose: a commit message,
  here-document or PR body that mentions `push`, `gh pr merge`, a ruleset or a remote database;
  reads of `pulls/<n>/merge` or `rulesets` API paths; any non-gh client reading api.github.com
  contents, git refs or objects, hooks, Actions settings or environments (use `gh api`); a
  hosted-Postgres host anywhere, even in an `echo` or an app's env var. supabase and the pg
  clients count only in command position (after a separator, `$(`, a shell keyword, `VAR=x` or a
  wrapper such as `env`, `npx`, `timeout`), but a here-document line or text after `;` that
  starts with one is in command position, and so is the whole command once it is piped into a
  shell, `eval`, `source` or `xargs`. There, `supabase` with dump, pull, push, link, repair,
  fetch or inspect (`--local` included) and psql or supabase with a `$` are refused. The refusal
  says how to split it: write the file in one command (or with the Write tool), run the tool in
  another.
- Keep cloud environments free of database credentials. The database rules know the common
  hosted-Postgres hosts and loopback; they are not a boundary. A pg_service entry named inside
  quotes (`psql "service='x'"`) is not checked; cloud VMs carry no pg_service file.
- Cloud sessions act on GitHub as the signed-in user through the Claude GitHub App, so a
  ruleset bypass for repository admins also lets cloud sessions through. Leave the bypass
  list empty on repos where cloud sessions must not reach `main`.
- `land` judges the check runs exactly as GitHub's latest view returns them, like zs-land:
  serial matrix legs that share one static name count as one run, the newest.
- The first session on a VM snapshot from before 2026-10-21 keeps that snapshot's ~/.claude
  config; the next snapshot rebuild picks up the new config.
- Draft PRs cannot be marked ready from the cloud (GraphQL only); `land` opens PRs ready.
- Commit statuses are invisible to the cloud GitHub app until its permissions include them;
  `land` relies on the PR's mergeable state, always, in addition to the check runs (see above).
