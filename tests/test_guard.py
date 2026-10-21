import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("guard", ROOT / "hooks" / "guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.fixture
def fake_git(monkeypatch):
    """Pretend the session repo is on feat/x with main as the default branch."""
    state = {"branch": "feat/x", "default": "main", "push": None, "config": ""}
    monkeypatch.setattr(guard, "current_branch", lambda where: state["branch"])
    monkeypatch.setattr(guard, "default_branch", lambda where, remote="origin": state["default"])
    monkeypatch.setattr(guard, "push_target", lambda where: state["push"])
    monkeypatch.setattr(guard, "push_config", lambda where: state["config"])
    for name in ("PGHOST", "PGHOSTADDR", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)
    return state


BLOCKED = [
    # pushes to a protected branch, in any spelling
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin feat/x:refs/heads/main",
    "git push origin HEAD:heads/main",
    "git push origin HEAD:refs/heads/master",
    "git push origin HEAD:Main",
    "git push origin heads/main",
    "git push -u origin master",
    "git push origin main 2>&1 | tail -5",
    "git push origin main > /tmp/out.log",
    "GIT_TRACE=1 git push origin main",
    "git -C /root/repo push origin main",
    "git push --all origin",
    "git push --branches origin",
    "git push --mirror",
    "git push origin :",
    "git push origin +:",
    "git push origin 'refs/heads/*:refs/heads/*'",
    "git push origin --delete main",
    "git push origin :main",
    "git push origin feat/x feat/y",
    "git push https://github.com/o/r.git HEAD",
    # refspecs the guard can't see
    "dst=main; git push origin HEAD:$dst",
    "git push origin HEAD:$BRANCH",
    'git push -u origin "$(git branch --show-current)"',
    "git push -u origin $(git branch --show-current)",
    "echo main | xargs git push origin",
    "git -c remote.origin.push=HEAD:main push origin",
    "git -c push.default=matching push",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=HEAD:main git push origin",
    # force without a usable lease
    "git push --force origin feat/x",
    "git push -f",
    "git push -uf origin feat/x",
    "git push -fu origin HEAD",
    "git push origin +feat/x",
    "git push -f --force-if-includes origin feat/x",
    "git push --force-if-includes origin +feat/x",
    "git push --force --force-with-lease origin feat/x",
    "git push --force-with-lease=other origin +feat/x",
    # wrappers, subshells and nested shells
    "cd repo && git push origin main",
    "bash -c 'git push origin main'",
    "bash -lc 'true && git push origin main'",
    'bash -c "true; git push origin main"',
    "/bin/bash -c 'git push origin main'",
    "bash -ec 'git push origin main'",
    "sh -xc 'git push origin main'",
    "zsh -c 'cd r && git push origin main'",
    "eval 'git push origin main'",
    "env -u FOO git push origin main",
    "env -i git push origin main",
    "env -i PATH=/usr/bin git push origin main",
    "env -S 'git push origin main'",
    "timeout 60 git push origin main",
    "timeout -s KILL 60 git push origin main",
    "nice -n 5 git push origin main",
    "sudo -u root git push origin main",
    "nohup git push origin main &",
    "(git push origin main)",
    "(cd r && git push origin main)",
    "{ git push origin main; }",
    "if true; then git push origin main; fi",
    "echo ok\ngit push origin main",
    "git status  # check first\ngit push origin main",
    "echo start; `git push origin main`",
    "cat <<'EOF' | bash\ngit push origin main\nEOF",
    "bash <<'EOF'\ngit push origin main\nEOF",
    'echo "$(git push origin main)"',
    'out="$(git push origin main 2>&1)"; echo "$out"',
    'echo "`git push origin main`"',
    '"gi""t" push origin main',
    "g\\it push origin main",
    "hub push origin main",
    # branch switches before a push (any compound command with a push is refused)
    "git checkout main && git push -u origin HEAD",
    "git switch main; git push origin @",
    "git checkout -q main && git push origin HEAD",
    "git checkout -B main && git push origin HEAD",
    "git switch -c main origin/main && git push origin HEAD",
    "git switch --create=main && git push origin HEAD",
    "git checkout -t origin/main && git push origin HEAD",
    "git branch -m main && git push origin HEAD",
    "git branch -M feat/x main && git push origin HEAD",
    "git symbolic-ref HEAD refs/heads/main && git push origin HEAD",
    "git checkout --detach && git push origin HEAD",
    "git checkout - && git push origin HEAD",
    "git checkout $BRANCH && git push origin HEAD",
    "gh pr checkout 5 && git push origin HEAD",
    "git checkout feat/other && git push",
    "(git checkout main) && git push origin HEAD",
    "bash -c 'git checkout main && git push -u origin HEAD'",
    # merging, or moving protected refs, through the API
    "gh pr merge 5 --squash",
    "gh -R o/r pr merge 5",
    "gh pr merge 5 -R o/r --squash",
    "gh api -X PUT repos/o/r/pulls/5/merge -f merge_method=squash",
    "gh api repos/o/r/pulls/5/merge -f merge_method=squash",
    "gh api repos/o/r/pulls/5/merge -fmerge_method=squash",
    "gh api --method PUT repos/o/r/pulls/12/merge",
    "gh api --method=PUT repos/o/r/pulls/12/merge",
    "gh api -XPUT repos/o/r/pulls/12/merge",
    "gh api -iX PUT repos/o/r/pulls/12/merge",
    "gh api -X PUT repos/o/r/pulls/$PR/merge --input b.json",
    "gh api -X PUT 'repos/{owner}/{repo}/pulls/5/merge'",
    "NWO=o/r; gh api -X PUT repos/$NWO/pulls/5/merge",
    "hub api -X PUT repos/o/r/pulls/5/merge",
    "curl -X PUT https://api.github.com/repos/o/r/pulls/3/merge",
    "curl --request PUT https://api.github.com/repos/o/r/pulls/1/merge",
    "curl --request=PUT https://api.github.com/repos/o/r/pulls/1/merge",
    "curl -X PUT api.github.com/repos/o/r/pulls/1/merge",
    "curl -sX PUT -H 'Authorization: token x' https://api.github.com/repos/o/r/pulls/1/merge",
    ("curl \\\n  -H 'Accept: application/vnd.github+json' \\\n  -d '{\"merge_method\":\"squash\"}' \\\n"
     "  https://api.github.com/repos/o/r/pulls/7/merge"),
    "wget --method=PUT https://api.github.com/repos/o/r/pulls/1/merge",
    "wget --post-data '{}' https://api.github.com/repos/o/r/merges",
    "http PUT api.github.com/repos/o/r/pulls/1/merge",
    "http api.github.com/repos/o/r/merges base=main head=feat/x",
    "gh api repos/o/r/merges -f base=main -f head=feat/x",
    "gh api -X POST repos/o/r/merge-upstream -f branch=main",
    "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc",
    "gh api -X PATCH repos/o/r/git/refs/heads/$B -f sha=abc",
    "gh api repos/o/r/git/refs -f ref=refs/heads/main -f sha=abc",
    "gh api -X POST repos/o/r/git/refs --input ref.json",
    "gh api -X PUT repos/o/r/contents/README.md -f message=x -f content=eA==",
    "gh api -X DELETE repos/o/r/contents/README.md -f message=x -f sha=abc",
    "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }'",
    # repository settings, protection and rulesets
    "gh repo delete o/r --yes",
    "gh repo archive o/r -y",
    "gh repo rename new-name",
    "gh repo edit o/r --default-branch dev",
    "gh repo sync o/r --branch main --force",
    "gh ruleset list",
    "gh alias set m 'pr merge'",
    "gh api -X PUT repos/o/r/branches/main/protection --input p.json",
    "gh api -X DELETE repos/o/r/rulesets/7",
    "gh api -X PUT orgs/o/rulesets/1 --input r.json",
    "curl -X DELETE https://api.github.com/repos/o/r/branches/main/protection",
    "curl --request DELETE https://api.github.com/repos/o/r/rulesets/3",
    # remote databases
    "supabase db push",
    "supabase db --debug push",
    "supabase --workdir app db push",
    "npx supabase db push --linked",
    "npx -y supabase@latest db push",
    "pnpm dlx supabase db push",
    "pnpm supabase db push",
    "supabase db pull",
    "supabase db dump -f out.sql",
    "supabase migration repair 20240101 --status applied",
    "supabase migration fetch",
    "supabase db reset --linked",
    "supabase migration up --linked",
    "supabase migration up --db-url postgres://x",
    "supabase db diff --db-url=postgresql://x",
    "supabase db push --local --project-ref abc",
    "supabase inspect db bloat",
    "psql postgresql://postgres:pw@db.abc.supabase.co:5432/postgres -c 'select 1'",
    "pg_dump -h db.abc.supabase.co -U postgres",
    "pg_dump -hdb.abc.supabase.co -U postgres",
    "psql --host=db.example.net app",
    "psql 'host=db.example.net dbname=app'",
    "psql 'service=prod'",
    "psql 'postgresql:///app?host=db.example.net'",
    "PGHOST=db.abc.supabase.co psql -U postgres",
    "node migrate.js postgresql://postgres:pw@db.abc.supabase.co:5432/postgres",
    "DATABASE_URL=postgres://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres node migrate.js",
    "SUPABASE_DB_URL=$PROD npm run migrate",
    "curl -X POST https://abc.supabase.co/rest/v1/users -d '{}'",
    # unparseable but risky
    "git push origin 'main",
    "gh pr merge 'x",
]

# Every guard bypass the round-3 review (GPT-6.1 Sol, Gemini 4 Argon) found.
BYPASSES = [
    # Sol
    "git switch does-not-exist; git push origin HEAD",
    "git switch -cmain && git push origin HEAD",
    "git checkout -bmain && git push origin HEAD",
    "git checkout main -- && git push origin HEAD",
    "(cd /feature-repo && git status); git push origin HEAD",
    "git -C/on-main push origin HEAD",
    "GIT_DIR=/on-main/.git git push origin HEAD",
    "env -C /on-main git push origin HEAD",
    "env PGHOST=db.example.supabase.co psql -c 'delete from t'",
    'git -c alias.publish="push origin HEAD:main" publish',
    "dst=main; git push origin -- HEAD:$dst",
    'METHOD=PUT; gh api -X "$METHOD" repos/o/r/pulls/5/merge',
    "cat <<EOF\n$(git push origin HEAD:main)\nEOF",
    "psql postgresql://u@db.example.net/app -c 'delete from t'",
    'psql "$DATABASE_URL" -f migration.sql',
    "curl -X POST https://api.supabase.com/v1/projects/example/database/query --json '{\"query\":\"delete from t\"}'",
    # Argon: unquoted command substitution
    "git -C $(pwd) push origin main",
    "git -C `pwd` push origin main",
    "git -C $(pwd) push --force origin feat/x",
    "git push `echo origin` main",
    "git push `echo -f` origin feat/x",
    "git push -o $(echo x) origin main",
    "git push --repo $(echo origin) main",
    "gh api -H $(echo Accept:text/plain) -X PUT repos/o/r/pulls/5/merge",
    "curl -H $(echo X:1) -X PUT https://api.github.com/repos/o/r/pulls/1/merge",
    "curl $(echo -X PUT) https://api.github.com/repos/o/r/pulls/1/merge",
    "supabase --workdir $(pwd) db push",
    "psql $(echo -w) postgresql://postgres:pw@db.abc.supabase.co:5432/postgres",
    # Argon: long-option prefixes and attached git options
    "git push --al origin",
    "git push --br origin",
    "git push --mi origin",
    "git -cremote.origin.push=HEAD:refs/heads/main push origin",
    "git -cpush.default=matching push",
    # Argon: branch moves
    "git checkout -Bmain && git push origin HEAD",
    "git switch -Cmain origin/main && git push origin HEAD",
    "git checkout --conflict merge main && git push origin HEAD",
    "git branch -qm main && git push origin HEAD",
    "git branch -fM main && git push origin HEAD",
    "git rebase origin/main main && git push -u origin HEAD",
    # Argon: quoting, here-strings and here-documents
    "bash -c $'git push origin main'",
    'bash -c $"git push origin main"',
    "$'git' push origin main",
    "$'gh' pr merge 5",
    "$'\\x67h' pr merge 5",
    "$'supabase' db push",
    "bash <<< 'git push origin main'",
    "sh <<< 'gh pr merge 5 --squash'",
    "\"bash\" <<'EOF'\ngit push origin main\nEOF",
    "\\bash <<'EOF'\ngit push origin main\nEOF",
    "\"bash\" <<'EOF'\ngh pr merge 5\nEOF",
    "cat <<'EOF' |\ngit push origin main\nEOF\nbash",
    # Argon: variables and exports
    "env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=HEAD:main git push origin",
    "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=HEAD:main && git push origin",
    'U=postgresql://postgres:pw@db.abc.supabase.co:5432/postgres; psql "$U" -c "select 1"',
    "env PGHOST=db.abc.supabase.co psql -U postgres",
    "export PGHOST=db.abc.supabase.co && psql -U postgres",
    "env DATABASE_URL=postgres://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres node migrate.js",
    "P=push; git $P origin main",
    "A=merge; gh pr $A 5 --squash",
    "S=api; gh $S -X PUT repos/o/r/pulls/5/merge",
    'M=PUT; gh api -X "$M" repos/o/r/pulls/5/merge',
    'URL=https://api.github.com/repos/o/r/pulls/5/merge; curl -X PUT "$URL"',
    "A=push; supabase db $A",
    'C=gh; "$C" pr merge 5',
    # Argon: xargs
    'echo "push origin main" | xargs git',
    'echo "-X PUT" | xargs gh api repos/o/r/pulls/5/merge',
    "echo https://api.github.com/repos/o/r/pulls/5/merge | xargs curl -X PUT",
    "echo push | xargs supabase db",
    # Argon: persistent config and plumbing
    'git config alias.p "push origin main" && git p',
    "git config remote.origin.push HEAD:refs/heads/main && git push origin",
    "git send-pack origin HEAD:refs/heads/main",
    # Argon: API paths
    "gh api -X PUT repos/o/r/./pulls/5/merge",
    "curl -X PUT https://api.github.com/repos/o/r/./pulls/5/merge",
    'gh api -X PUT "repos/o/r/pulls/5/merge#"',
    "curl -X PUT 'https://api.github.com/repos/o/r/pulls/5/merge#'",
    "gh api -X PATCH 'repos/o/r/git/refs/heads/main#' -f sha=abc",
    "gh api -X PUT 'repos/o/r/branches/main/protection#' --input p.json",
    "gh api -X PUT repositories/12345/pulls/5/merge",
    "curl -X PUT https://api.github.com/repositories/12345/pulls/5/merge",
    "gh api -X POST 'repos/o/r/pulls/5/comments/../../../merges' -f base=main",
    "gh api -X POST 'repos/o/r/pulls%2F5%2Fmerge'",
    # Round 4, Sol
    "psql --dbname='host=db.example.supabase.co dbname=app' -c 'delete from t'",
    "psql -dhost=db.example.net -c 'delete from t'",
    "supabase --dns-resolver https db push",
    "supabase --log-level debug db push",
    "npx --yes --package supabase supabase db push",
    "npm exec --package supabase -- supabase db push",
    "cat <<EOF\n`gh api -X PUT repos/o/r/pulls/5/merge`\nEOF",
    "curl -X PUT https://api.github.com./repos/o/r/pulls/5/merge",
    "curl -X PUT https://API.GitHub.com./repos/o/r/contents/a -d '{}'",
    # Round 4, Argon: here-documents and process substitution
    "cat <<EOF\n$(gh pr merge 5 --squash)\nEOF",
    "cat <<EOF\n$(gh api -X PUT repos/o/r/pulls/5/merge)\nEOF",
    "cat <<EOF\n$(psql postgresql://postgres:pw@db.abc.supabase.co:5432/postgres -c \"drop table u\")\nEOF",
    "cat <<'EOF' |\ngh pr merge 5\nEOF\nbash",
    "gh api --input <(jq -n '{merge_method:\"squash\"}') -X PUT repos/o/r/pulls/5/merge",
    "curl -d @<(echo '{}') -X PUT https://api.github.com/repos/o/r/pulls/5/merge",
    "psql -f <(cat migration.sql) -h db.abc.supabase.co postgres",
    # Round 4, Argon: connection strings
    'psql --dbname="host=db.abc.supabase.co dbname=postgres"',
    'psql -d"host=db.abc.supabase.co dbname=postgres"',
    'psql "dbname=postgres;host=db.abc.supabase.co"',
    'DATABASE_URL="host=db.abc.supabase.co dbname=postgres" psql',
    # Round 4, Argon: quoted expansions, options before the subcommand, shells and wrappers
    'F=-f; gh api repos/o/r/pulls/5/merge "$F" merge_method=squash',
    'F=-X; curl "$F" PUT https://api.github.com/repos/o/r/pulls/5/merge',
    'curl "$(echo -X)" PUT https://api.github.com/repos/o/r/pulls/5/merge',
    'F=--linked; supabase db reset "$F"',
    'A=pull; supabase db "$A"',
    "gh -X PUT api repos/o/r/pulls/5/merge",
    "gh -f merge_method=squash api repos/o/r/pulls/5/merge",
    "supabase --env-file .env db pull",
    'echo "gh pr merge 5 --squash" | bash',
    'printf "gh api -X PUT repos/o/r/pulls/5/merge\\n" | sh',
    'echo "supabase db reset --linked" | bash',
    "env -C /tmp/echo gh pr merge 5",
    "sudo -u printf gh api -X PUT repos/o/r/pulls/5/merge",
]

# Every round-5 BLOCK repro (Sol, Argon) the coarser rules catch.
BYPASSES += [
    "supabase db --debug dump -f backup.sql",
    "supabase db -f out.sql dump",
    "supabase db --schema auth pull",
    "supabase migration --debug repair 20240101 --status applied",
    "supabase migration --debug fetch",
    "supabase db dump -f x && supabase db reset --local",
    "supabase db dump --local=false -f backup.sql",
    "supabase db pull --local=0",
    "node_modules/.bin/supabase db dump",
    """python -c 'import psycopg; psycopg.connect("postgresql://u@db.example.supabase.co/postgres", autocommit=True).execute("drop table t")'""",
    "psql -Xqh db.abc.supabase.co -c 'delete from users'",
    "pg_dump -ch db.abc.supabase.co app",
    "http api.github.com/repos/o/r/contents/README.md message=fix content=eA==",
    "curl -fsST body.json https://api.github.com/repos/o/r/contents/README.md",
    "gh \\\n  api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc",
    "gh -R o/r \\\n  api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc",
    "gh pr -R o/r merge 5 --squash",
    "gh repo -R o/r edit --default-branch x",
    "git -C /tmp/stash push origin main",
    "git -C ../stash push --force origin feat/x",
    "git -c x=stash push origin main",
    "git --namespace=stash push origin main",
    "git --namespace stash push origin main",
    # round 6 (Sol): attached gh api fields are writes
    "gh api repos/o/r/git/refs -f'ref=refs/heads/backup' -f\"sha=$(git rev-parse HEAD)\"",
    "gh api repos/o/r/git/refs -Fsha=abc -fref=refs/heads/x",
    # round 7 (Sol): shell grouping is command position; a GET in another call exempts nothing
    "(supabase db pull)",
    "{ supabase db push; }",
    'gh api repos/o/r/git/refs -f ref=refs/heads/backup -f "sha=$(git rev-parse origin/main)" && gh api -X GET repos/o/r/git/refs',
    "pnpm exec supabase db dump",
    "yarn dlx supabase inspect db",
    "bun x supabase link",
    "source .env && supabase db pull",
]

# Harmless commands refused on purpose: the price of matching raw text instead of parsing shell.
REFUSED_ON_PURPOSE = [
    # supabase remote verbs, --local or not; hosted hosts anywhere; raw clients on sensitive GitHub paths
    "supabase db dump --local -f x.sql",
    "echo postgresql://x@db.abc.supabase.co",
    "curl -fsSL https://api.github.com/repos/o/r/contents/README.md",
    # text that only mentions a guarded command
    "echo 'gh pr merge 1'",
    "echo gh pr merge 1",
    "cat > notes.md <<'EOF'\ngh pr merge 5\nsupabase db reset --linked\nEOF",
    "cat <<'EOF' > pr.md\n## Summary\n- Fix array.push bug\nEOF",
    "git commit -m \"$(cat <<'EOF'\nfeat: add web push notifications\nEOF\n)\"",
    'gh pr create --title t --body "Turns on auto-merge for docs"',
    # reads on paths the merge and settings rules match
    "gh api repos/o/r/pulls/5/merge",
    "gh api repos/o/r/rulesets",
    # local database work the coarse rules can't tell from remote
    "supabase db push --local",
    "psql -h localhost -c 'DO $$ BEGIN END $$;'",
    "psql -h localhost -p $PORT app",
    'DATABASE_URL="postgresql://postgres:postgres@localhost:$PORT/app" npm test',
    'DATABASE_URL="sqlite:///$TMPDIR/test.db" pytest',
    # pushes other than the plain form, even harmless ones
    "git push origin @",
    "git push --no-verify -u origin HEAD",
    "git push -u origin HEAD 2>&1 | tail -3",
    "git push --force-with-lease origin +feat/x",
    "git push origin --delete old-feature",
    "git push origin :old-feature",
    'git commit -m "do not push to main"',
    "git log --grep 'push origin main'",
    "echo 'git push origin main is blocked' > notes.txt",
    "# don't push to main\ngit status",
    "git checkout -b feat/new && git push -u origin HEAD",
    "gh api -X DELETE repos/o/r/git/refs/heads/feat/x",
    "gh api repos/o/r/git/refs -f ref=refs/heads/feat/y -f sha=abc",
    "supabase migration list --linked",
    "psql -h db.internal app",
    "cat > notes.md <<'EOF'\nsupabase db push\nEOF",
]

ALLOWED = [
    'git -c user.name="Cloud Agent" commit -m "fix: validate inputs"',
    'git commit -m "docs: document `git status` output"',
    'git commit -m "git hooks: run lint"',
    "DATABASE_URL=sqlite:///tmp/test.db python -m pytest -q",
    "DATABASE_URL=file:./test.db npm test",
    'PGHOST=::1 psql -c "SELECT 1"',
    "supabase start",
    "supabase db reset",
    "supabase migration up",
    "pnpm add @supabase/ssr",
    "git add supabase/migrations/20261021_x.sql",
    "gh api repos/o/r/pulls -X POST -f title=t -f head=b -f base=main",
    "gh api repos/o/r/issues/1/comments -f body=x",
    "git stash push -u -m wip",
    "git push -u origin HEAD",
    "git push origin HEAD",
    "git push --set-upstream origin HEAD",
    "git push -q -u origin feat/x",
    "git push origin HEAD:feat/x",
    "git push origin HEAD:refs/heads/feat/main-fix",
    "git push origin feat/x:feat/x",
    "git push origin 'HEAD:feat/x'",
    "git push",
    "git push origin",
    "git push origin v1.0",
    "git push --force-with-lease origin feat/x",
    "git push --force-with-lease=feat/x:abc origin HEAD",
    "git push --force-with-lease --force-if-includes origin feat/x",
    "git push -u origin HEAD  # first push",
    "cd /tmp/repo && git push -u origin HEAD",
    "git status && git log --oneline -5",
    "git stash push -m wip",
    "land",
    "land --pr",
    'git commit -m "fix: guard merges"',
    "git commit -F - <<'EOF'\nfix: guard merges\nEOF",
    "git commit -m \"$(cat <<'EOF'\nfix: don't merge stale refs to main\n\nHandles \"quoted\" names (and merges).\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\n- don't merge until CI is green\nEOF\n)\"",
    'echo "on $(git branch --show-current)"',
    "gh pr view 5",
    "gh pr create --fill",
    "gh pr list --state open",
    "gh pr checks 5",
    "gh repo view o/r",
    "gh run view 123 --log-failed",
    "gh api repos/o/r/pulls/5",
    "gh api repos/o/r/commits/abc/check-runs",
    "gh api -X GET repos/o/r/pulls -f state=open",
    "gh api --method HEAD repos/o/r",
    "gh api 'repos/{owner}/{repo}/pulls?state=open' --jq '.[].number'",
    "gh api repos/o/r/pulls -f title=t -f head=feat/x -f base=main",
    "gh api repositories/123/pulls -f title=t -f head=feat/x -f base=main",
    "gh api -X PATCH 'repos/{owner}/{repo}/pulls/5' -f title=x -f body='fix base layout'",
    "gh api repos/o/r/issues/5/comments -f body=hi",
    "gh api -X POST repos/o/r/pulls/5/comments -f body=x -f commit_id=abc -f path=a -F line=1",
    "gh api repos/o/r/pulls/5/reviews -f event=COMMENT -f body=x",
    'gh api repos/o/r/issues/5/comments -f body="$(cat notes.md)"',
    "gh api graphql -f query='query { viewer { login } }'",
    "curl https://example.com",
    "curl -s https://api.github.com/repos/o/r/pulls/1",
    'curl -s -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/repos/o/r/pulls',
    "curl -sSL https://raw.githubusercontent.com/o/r/main/setup.sh -o /tmp/s.sh",
    "curl -d x=1 https://example.com/form",
    "curl -fsS -X POST http://localhost:3000/api/items -d '{}'",
    "curl -X POST https://api.github.com/repos/o/r/issues/5/comments -d '{\"body\":\"x\"}'",
    "wget -qO- https://api.github.com/repos/o/r",
    "http GET api.github.com/repos/o/r",
    "http POST api.github.com/repos/o/r/issues/5/comments body=hi",
    "curl -s https://api.supabase.com/v1/projects",
    "supabase start",
    "supabase status",
    "supabase stop",
    "supabase db reset",
    "supabase migration new add_table",
    "supabase migration list",
    "supabase migration up --local",
    "supabase test db",
    "supabase gen types typescript --local",
    "npx supabase db reset",
    "psql postgres://localhost:5432/app -c 'select 1'",
    "psql -h 127.0.0.1 -U postgres -c 'select 1'",
    "psql postgresql://postgres:postgres@127.0.0.1:54322/postgres -f seed.sql",
    "psql 'host=localhost dbname=app'",
    "psql -h /var/run/postgresql app",
    "psql app",
    "pg_dump -h localhost app > dump.sql",
    "DATABASE_URL=postgres://u:p@localhost:5432/app npm test",
    "npm test",
    "bash scripts/test.sh",
    "bash -c 'npm test && npm run lint'",
    "GIT_TRACE=1 git status",
    "$HOME/.local/bin/uv run pytest",
    "for f in *.py; do python -m py_compile \"$f\"; done",
    "echo 'unclosed",
    # round-4 false positives the raw-text rules allow
    "gh api repos/o/r/pulls -f title=t -f head=feat/x -f base=main -f body='See postgresql://db.example.net/app'",
    "gh api -X PATCH repos/o/r/pulls/5 -f body='Example {\"state\":\"open\"}'",
    "gh api repos/o/r/actions/runs/$RUN_ID/jobs",
    'gh api "repos/o/r/actions/runs/$RUN_ID/jobs"',
    "gh api repos/o/r/pulls/$PR",
    'gh api -f body="$MSG" repos/o/r/issues/5/comments',
    "gh api -f body='run `pytest`' repos/o/r/issues/5/comments",
    "curl -s https://api.github.com/repos/o/r/actions/runs/$RUN_ID/jobs",
    "curl -s http://localhost:$PORT/health",
    'curl -X POST "http://localhost:$PORT/api/items"',
    "rg supabase db",
    "grep -r supabase db",
    "git grep supabase db",
    "pytest tests/http -k $TEST",
    "supabase db diff -f new_migration",
    "supabase db lint",
    '"$PYTHON" -m pytest',
    # everyday commands that must always pass
    ("gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\n"
     "- Adds this PR to the merge queue once CI passes; merges stay with land\nEOF\n)\""),
    "gh api repos/o/r/pulls/5/comments",
    "pnpm test",
    "rg supabase src/",
    "ls apps/gh",
    'git commit -m "Move git hooks into scripts"',
    "git -C /tmp/repo status",
    "git lfs pull",
    # round-6 false positives: tools only count in command position, GitHub paths only as API paths
    "git commit -m \"$(cat <<'EOF'\nfeat(supabase): add RLS policies for profiles\nEOF\n)\"",
    "git commit -m 'fix(supabase): handle `null` session on sign-out'",
    'git commit -m "feat(supabase): add magic link login"',
    ("gh api repos/o/r/pulls -f title=\"feat(supabase): add RLS\" -f head=feat/x -f base=main "
     "-f body=\"$(cat <<'EOF'\n## Summary\n- Add RLS policies\nEOF\n)\""),
    "gh api repos/o/r/issues/5/comments -f body='Updated `supabase` client config'",
    'pytest tests/supabase -k "$TEST"',
    "cp -r supabase $TMP/backup",
    'gh api repos/o/r/pulls -f title="feat: auth" -f head=feat/x -f base=main -f body="Updated src/hooks/useAuth.ts"',
    "gh api -X POST repos/o/r/pulls/5/comments -f body=nit -f commit_id=abc -f path=src/hooks/useAuth.ts -F line=10",
    'gh api repos/o/r/issues/5/comments -f body="Fixed in ./hooks/guard.py"',
    "gh api -X GET repos/o/r/contents/README.md -f ref=feat/x",
    "git commit -m \"$(cat <<'EOF'\ndocs: add local psql usage\nEOF\n)\"",
    "git commit -m 'docs: run `psql` against 127.0.0.1'",
    "psql -h 127.0.0.1 -U postgres -c \"SELECT * FROM tenants WHERE host = 'example.com'\"",
    "psql -h 127.0.0.1 -U postgres -c \"SELECT * FROM jobs WHERE service = 'billing'\"",
    "pg_dump -h 127.0.0.1 postgres > dump.sql && du -h dump.sql",
    'git commit -m "fix: repo sync timeout"',
    'gh api repos/o/r/pulls -f title="feat: repo edit modal" -f head=feat/x -f base=main',
    "pytest tests/protection",
    "gh api repos/o/r/pulls -f title=t -f head=feat/x -f base=main -f body='Updated src/hooks/useAuth.ts'",
    "git commit -m 'docs: explain `supabase start` readiness'",
    "DATABASE_URL='host=127.0.0.1 dbname=test_db' python -m pytest -q",
    # round 7 (Sol): a filename is not `source`; SQL `service='x'` is not a libpq service
    'pytest tests/supabase -k "$TEST" --cov=src/source.py',
    "psql -h127.0.0.1 -c \"SELECT service='billing' FROM jobs\"",
]

# Allowed on purpose: GitHub refuses these (the Claude GitHub App has no administration write),
# the cloud proxy blocks GraphQL, or rulesets keep them off main. PR state and base are not inspected.
ALLOWED_BY_DESIGN = [
    'gh api -X POST "$URL" -f x=1',
    "gh api graphql -F query=@q.graphql",
    "curl -X POST -T mutation.json https://api.github.com/graphql",
    "gh api -X PATCH repos/o/r/pulls/5 -f state=closed",
    "gh api -X PATCH repos/o/r/pulls/5 -f base=main",
    "curl -X PATCH -T close.json https://api.github.com/repos/o/r/pulls/5",
    "gh api -X POST repos/o/r/pulls/5/requested_reviewers -f reviewers[]=x",
    "gh api -X DELETE repos/o/r",
    "gh api -X PATCH repos/o/r -f default_branch=feat/x",
    "gh api -X POST repos/o/r/branches/main/rename -f new_name=x",
]


# Not inspected (README, Limits): git aliases, and remote hosts other than the hosted-Postgres list
# when the host is hidden in bundled client flags. Rulesets and App permissions are the gate.
NOT_INSPECTED = [
    "git publish",
    "cd /other && git publish",
    "git -C /other publish",
    "if git publish; then echo shipped; fi",
    "sudo -u root git publish",
    "git co feat/x",
    "git config alias.run '!sh s.sh' && git run",
    "psql -Xqh db.internal app",
]


@pytest.mark.parametrize("cmd", BLOCKED + BYPASSES + REFUSED_ON_PURPOSE)
def test_blocked(fake_git, cmd):
    assert guard.decide(cmd, None), cmd


@pytest.mark.parametrize("cmd", ALLOWED + ALLOWED_BY_DESIGN + NOT_INSPECTED)
def test_allowed(fake_git, cmd):
    assert guard.decide(cmd, None) is None, cmd


def test_push_hint_names_the_canonical_form(fake_git):
    assert "git push -u origin HEAD" in guard.decide("git commit -m wip && git push", None)


def test_bare_push_on_main_blocked(fake_git):
    fake_git["branch"] = "main"
    for cmd in ("git push", "git push origin", "git push origin HEAD", "git push -u origin HEAD:refs/heads/main"):
        assert guard.decide(cmd, None), cmd
    assert guard.decide("git push -u origin feat/new", None) is None
    assert guard.decide("git push origin HEAD:feat/new", None) is None


def test_custom_default_branch(fake_git):
    fake_git["default"] = "develop"
    assert guard.decide("git push origin develop", None)
    assert guard.decide("git push origin HEAD:develop", None)
    assert guard.decide("git push origin main", None)


def test_unknown_branch_refuses_head(fake_git):
    fake_git["branch"] = None
    assert guard.decide("git push origin HEAD", None)
    assert guard.decide("git push", None)
    assert guard.decide("git push -u origin feat/y", None) is None


def test_configured_push_refspec(fake_git):
    # Sol: a `+` push refspec turns a bare push into a force push; any configured one is refused.
    fake_git["config"] = "remote.origin.push +HEAD:refs/heads/feat/x"
    for cmd in ("git push", "git push origin", "git push origin HEAD", "git push origin feat/x"):
        assert guard.decide(cmd, None), cmd
    assert guard.decide("git push origin HEAD:feat/x", None) is None


def test_remote_db_from_environment(fake_git, monkeypatch):
    monkeypatch.setenv("PGHOST", "db.abc.supabase.co")
    assert guard.decide("psql -c 'select 1'", None)
    assert guard.decide("psql -h localhost -c 'select 1'", None) is None
    monkeypatch.setenv("PGHOST", "localhost")
    assert guard.decide("psql -c 'select 1'", None) is None


def test_inherited_pghost_with_empty_uri_host(fake_git, monkeypatch):
    # Sol round 6: libpq fills an empty URI host from PGHOST.
    monkeypatch.setenv("PGHOST", "db.example.supabase.co")
    assert guard.decide("psql postgresql:///app -c 'select 1'", None)
    assert guard.decide("psql 'host= dbname=app'", None)
    monkeypatch.setenv("PGHOST", "db.internal")
    assert guard.decide("psql postgresql:///app", None)
    assert guard.decide("DATABASE_URL='dbname=x host=db.internal' npm test", None)
    # Sol round 7: grouping, and a hostless keyword string in DATABASE_URL, inherit PGHOST too
    monkeypatch.setenv("PGHOST", "db.example.supabase.co")
    assert guard.decide('(psql -c "SELECT 1")', None)
    assert guard.decide("DATABASE_URL='dbname=app' python -c 'import os'", None)


def test_non_origin_default_not_inspected(fake_git, monkeypatch):
    # README Limits: the guard judges main, master and origin's default branch. A branch that tracks
    # another remote whose default differs (upstream/release) is left to that repo's rulesets.
    monkeypatch.setattr(guard, "default_branch",
                        lambda where, remote="origin": "release" if remote == "upstream" else "main")
    fake_git.update(branch="release", push="release")
    assert guard.decide("git push", None) is None
    assert guard.decide("git push upstream release", None)  # a named remote is judged by its own default


def test_long_wrapper_chains_stay_fast(fake_git):
    # A flag value that could also be the next wrapper made the command-position regex backtrack
    # exponentially (`sudo -u sudo -u ...` hung the hook).
    import time
    wrapper = "su" + "do"
    for chunk in (f"{wrapper} -u ", f"{wrapper} -u A=1 ", "nice -n 5 ", "env -i ", "npx -y ",
                  "pnpm exec ", "npm exec ", "yarn dlx ", "bun x ", "( ", "{ "):
        start = time.monotonic()
        guard.decide(chunk * 60 + "psql -h db.example.net", None)
        assert time.monotonic() - start < 1
    assert guard.decide(f"{wrapper} -u 1000 env -i A=1 nice -n 5 psql -h db.example.net", None)


def test_text_match_says_how_to_split(fake_git):
    for cmd in ("echo gh pr merge 1", "cat > n.md <<'EOF'\nsupabase db reset --linked\nEOF",
                "psql postgresql://u@db.abc.supabase.co/app"):
        assert "write the file in one command, run the tool in another" in guard.decide(cmd, None), cmd


# ---------------------------------------------------------------- real repositories


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def make_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@example.com")

    def make(name="work", default="main", origin_head=True, branch="feat/x"):
        remote = tmp_path / f"{name}-remote.git"
        work = tmp_path / name
        git(tmp_path, "init", "-q", "--bare", "-b", default, str(remote))
        git(tmp_path, "init", "-q", "-b", default, str(work))
        git(work, "commit", "-q", "--allow-empty", "-m", "init")
        git(work, "remote", "add", "origin", str(remote))
        git(work, "push", "-q", "-u", "origin", default)
        if origin_head:
            git(work, "remote", "set-head", "origin", default)
        if branch != default:
            git(work, "switch", "-q", "-c", branch)
        return work

    return make


def test_head_resolves_to_current_branch(make_repo):
    work = make_repo(branch="main")
    for cmd in ("git push origin HEAD", "git push -u origin HEAD", "git push", "git push origin"):
        assert guard.decide(cmd, str(work)), cmd
    git(work, "switch", "-q", "-c", "feat/y")
    for cmd in ("git push -u origin HEAD", "git push", "git push origin", "git push --force-with-lease origin feat/y"):
        assert guard.decide(cmd, str(work)) is None, cmd
    git(work, "switch", "-q", "--detach")
    assert guard.decide("git push origin HEAD", str(work))
    assert guard.decide("git push origin HEAD:feat/y", str(work))


def test_cd_targets_the_named_repo(make_repo):
    feature = make_repo("feature")
    on_main = make_repo("on-main", branch="main")
    for cmd in (f"cd {on_main} && git push", f"cd {on_main} && git push origin HEAD",
                "cd ../on-main && git push -u origin HEAD", f"(cd {on_main}; git push)",
                f"git -C {on_main} push origin HEAD", f"git -C {feature} push origin HEAD"):
        assert guard.decide(cmd, str(feature)), cmd
    assert guard.decide(f"cd {feature} && git push -u origin HEAD", str(on_main)) is None
    assert guard.decide("cd ../feature && git push -u origin HEAD", str(on_main)) is None


def test_variable_directory_refused(make_repo):
    work = make_repo()
    assert guard.decide("cd $REPO && git push origin HEAD", str(work))
    assert guard.decide("cd $REPO && git push -u origin feat/y", str(work))


def test_default_branch_without_origin_head(make_repo):
    work = make_repo(default="develop", origin_head=False)
    assert subprocess.run(["git", "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], cwd=work, check=False).returncode != 0
    assert guard.default_branch(str(work)) == "develop"
    assert guard.decide("git push origin develop", str(work))
    assert guard.decide("git push origin HEAD:refs/heads/develop", str(work))
    assert guard.decide("git push -u origin feat/x", str(work)) is None


def test_configured_push_destinations(make_repo):
    work = make_repo()
    git(work, "config", "remote.origin.push", "HEAD:refs/heads/main")
    for cmd in ("git push", "git push origin", "git push origin HEAD"):
        assert guard.decide(cmd, str(work)), cmd
    assert guard.decide("git push origin HEAD:feat/x", str(work)) is None
    git(work, "config", "--unset", "remote.origin.push")
    assert guard.decide("git push", str(work)) is None

    git(work, "config", "push.default", "matching")
    assert guard.decide("git push", str(work))
    git(work, "config", "--unset", "push.default")

    git(work, "config", "branch.feat/x.remote", "origin")
    git(work, "config", "branch.feat/x.merge", "refs/heads/main")
    git(work, "config", "push.default", "upstream")
    assert guard.push_target(str(work)) == "main"
    assert guard.decide("git push", str(work))
    git(work, "config", "push.default", "simple")

    git(work, "config", "remote.origin.mirror", "true")
    assert guard.decide("git push", str(work))
    assert guard.decide("git push origin HEAD:feat/x", str(work))


def test_hook_outside_cloud_allows(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("CLOUD_GUARD_FORCE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}})))
    assert guard.main() == 0
    assert capsys.readouterr().out == ""


def test_hook_in_cloud_denies(monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}})))
    assert guard.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("[cloud guard] ")
