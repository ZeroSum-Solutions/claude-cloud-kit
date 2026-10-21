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
    return state


BLOCKED = [
    # pushes to a protected branch, in any spelling
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin feat/x:refs/heads/main",
    "git push origin HEAD:heads/main",
    "git push origin HEAD:refs/heads/master",
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
    "gh api -X PUT repos/o/r/pulls/$PR/merge --input b.json",
    "gh api -X PUT 'repos/{owner}/{repo}/pulls/5/merge'",
    "NWO=o/r; gh api -X PUT repos/$NWO/pulls/5/merge",
    "curl -X PUT https://api.github.com/repos/o/r/pulls/3/merge",
    "curl --request PUT https://api.github.com/repos/o/r/pulls/1/merge",
    "curl --request=PUT https://api.github.com/repos/o/r/pulls/1/merge",
    "curl -X PUT api.github.com/repos/o/r/pulls/1/merge",
    "curl -sX PUT -H 'Authorization: token x' https://api.github.com/repos/o/r/pulls/1/merge",
    ("curl \\\n  -H 'Accept: application/vnd.github+json' \\\n  -d '{\"merge_method\":\"squash\"}' \\\n"
     "  https://api.github.com/repos/o/r/pulls/7/merge"),
    "gh api repos/o/r/merges -f base=main -f head=feat/x",
    "gh api -X POST repos/o/r/merge-upstream -f branch=main",
    "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc",
    "gh api -X PATCH repos/o/r/git/refs/heads/$B -f sha=abc",
    "gh api repos/o/r/git/refs -f ref=refs/heads/main -f sha=abc",
    "gh api -X POST repos/o/r/git/refs --input ref.json",
    "gh api -X PUT repos/o/r/contents/README.md -f message=x -f content=eA==",
    "gh api -X DELETE repos/o/r/contents/README.md -f message=x -f sha=abc",
    "gh api -X POST \"$URL\" -f x=1",
    "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }'",
    # repository settings, protection and rulesets
    "gh repo delete o/r --yes",
    "gh repo archive o/r -y",
    "gh repo edit o/r --default-branch dev",
    "gh api -X DELETE repos/o/r",
    "NWO=o/r; gh api -X DELETE repos/$NWO",
    "gh api -X PATCH repos/o/r -F archived=true",
    "gh api -X PATCH repos/o/r -f name=renamed",
    "gh api -X PATCH repos/o/r -f default_branch=feat/x",
    "gh api -X POST repos/o/r/transfer -f new_owner=x",
    "gh api -X POST repos/o/r/branches/main/rename -f new_name=x",
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
    "psql postgresql://postgres:pw@db.abc.supabase.co:5432/postgres -c 'select 1'",
    "pg_dump -h db.abc.supabase.co -U postgres",
    "PGHOST=db.abc.supabase.co psql -U postgres",
    "node migrate.js postgresql://postgres:pw@db.abc.supabase.co:5432/postgres",
    "DATABASE_URL=postgres://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres node migrate.js",
    # unparseable but risky
    "git push origin 'main",
]

ALLOWED = [
    "git push -u origin HEAD",
    "git push origin HEAD",
    "git push origin @",
    "git push -u origin feat/x",
    "git push origin HEAD:feat/x",
    "git push origin HEAD:refs/heads/feat/main-fix",
    "git push",
    "git push --no-verify -u origin HEAD",
    "git push -u origin HEAD 2>&1 | tail -3",
    "git push --force-with-lease origin feat/x",
    "git push --force-with-lease=feat/x:abc origin HEAD",
    "git push --force-with-lease origin +feat/x",
    "git push --force-with-lease --force-if-includes origin feat/x",
    "git push origin --delete old-feature",
    "git push origin :old-feature",
    "git push origin v1.0",
    "git status && git log --oneline -5",
    "land",
    "land --pr",
    'git commit -m "do not push to main"',
    "git commit -m 'never git push origin main'",
    "git log --grep 'push origin main'",
    "echo 'gh pr merge 1'",
    "echo 'git push origin main is blocked' > notes.txt",
    "cat > notes.md <<'EOF'\ngit push origin main\ngh pr merge 5\nEOF",
    "git commit -F - <<'EOF'\nfix: don't push to main\nEOF",
    "git commit -m \"$(cat <<'EOF'\nfix: don't push stale refs to main\n\nHandles \"quoted\" names (and merges).\nEOF\n)\"",
    "gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\n- don't merge until CI is green\nEOF\n)\"",
    'git commit -m "never run \\`git push origin main\\`"',
    'echo "on $(git branch --show-current)"',
    "# don't push to main\ngit status",
    "gh pr view 5",
    "gh pr create --fill",
    "gh repo view o/r",
    "gh api repos/o/r/pulls/5",
    "gh api repos/o/r/pulls/5/merge",
    "gh api repos/o/r/commits/abc/check-runs",
    "gh api repos/o/r/pulls -f title=t -f head=feat/x -f base=main",
    "gh api -X PATCH 'repos/{owner}/{repo}/pulls/5' -f title=x",
    "gh api -X DELETE repos/o/r/git/refs/heads/feat/x",
    "gh api repos/o/r/git/refs -f ref=refs/heads/feat/y -f sha=abc",
    "gh api graphql -f query='query { viewer { login } }'",
    "curl https://example.com",
    "curl -s https://api.github.com/repos/o/r/pulls/1",
    "curl -sSL https://raw.githubusercontent.com/o/r/main/setup.sh -o /tmp/s.sh",
    "curl -d x=1 https://example.com/form",
    "supabase start",
    "supabase status",
    "supabase db reset",
    "supabase db push --local",
    "supabase db dump --local -f x.sql",
    "supabase migration new add_table",
    "supabase migration up",
    "supabase migration list --linked",
    "psql postgres://localhost:5432/app -c 'select 1'",
    "echo postgresql://x@db.abc.supabase.co",
    "npm test",
    "bash scripts/test.sh",
    "bash -c 'npm test && npm run lint'",
    "GIT_TRACE=1 git status",
    "echo 'unclosed",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_blocked(fake_git, cmd):
    assert guard.decide(cmd, None), cmd


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(fake_git, cmd):
    assert guard.decide(cmd, None) is None, cmd


def test_bare_push_on_main_blocked(fake_git):
    fake_git["branch"] = "main"
    assert guard.decide("git push", None)
    assert guard.decide("git push origin HEAD", None)
    assert guard.decide("git push origin @", None)
    assert guard.decide("git push -u origin feat/new", None) is None


def test_custom_default_branch(fake_git):
    fake_git["default"] = "develop"
    assert guard.decide("git push origin develop", None)
    assert guard.decide("gh api -X PATCH repos/o/r/git/refs/heads/develop -f sha=abc", None)


def test_unknown_branch_refuses_head(fake_git):
    fake_git["branch"] = None
    assert guard.decide("git push origin HEAD", None)
    assert guard.decide("git push", None)
    assert guard.decide("git push -u origin feat/y", None) is None


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


def test_at_resolves_to_current_branch(make_repo):
    work = make_repo(branch="main")
    for cmd in ("git push origin @", "git push origin HEAD", "git push", "git push origin"):
        assert guard.decide(cmd, str(work)), cmd
    git(work, "switch", "-q", "-c", "feat/y")
    for cmd in ("git push origin @", "git push -u origin HEAD", "git push"):
        assert guard.decide(cmd, str(work)) is None, cmd


def test_git_C_and_cd_target_the_named_repo(make_repo):
    feature = make_repo("feature")
    on_main = make_repo("on-main", branch="main")
    for cmd in (f"git -C {on_main} push origin HEAD", f"cd {on_main} && git push",
                f"cd {on_main} && git push origin HEAD", "cd ../on-main && git push origin @",
                f"(cd {on_main}; git push)"):
        assert guard.decide(cmd, str(feature)), cmd
    assert guard.decide(f"git -C {feature} push origin HEAD", str(on_main)) is None
    assert guard.decide(f"cd {feature} && git push -u origin HEAD", str(on_main)) is None


def test_unknown_directory_refuses_head(make_repo):
    work = make_repo()
    assert guard.decide("cd $REPO && git push origin HEAD", str(work))
    assert guard.decide("cd $REPO && git push -u origin feat/y", str(work)) is None


def test_default_branch_without_origin_head(make_repo):
    work = make_repo(default="develop", origin_head=False)
    assert subprocess.run(["git", "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], cwd=work, check=False).returncode != 0
    assert guard.default_branch(str(work)) == "develop"
    assert guard.decide("git push origin develop", str(work))
    assert guard.decide("git push origin HEAD:refs/heads/develop", str(work))
    assert guard.decide("gh api -X PATCH repos/o/r/git/refs/heads/develop -f sha=abc", str(work))
    assert guard.decide("git push -u origin feat/x", str(work)) is None


def test_configured_push_destinations(make_repo):
    work = make_repo()
    git(work, "config", "remote.origin.push", "HEAD:refs/heads/main")
    assert guard.decide("git push", str(work))
    assert guard.decide("git push origin", str(work))
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


# ---------------------------------------------------------------- branch switches inside one command

SWITCH_THEN_PUSH_BLOCKED = [
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
]

SWITCH_THEN_PUSH_ALLOWED = [
    "git checkout -b feat/new && git push -u origin HEAD",
    "git switch -c fix/y origin/main && git push -u origin HEAD",
    "git checkout main && git pull && git checkout -b feat/z && git push -u origin HEAD",
    "git checkout -- README.md && git push -u origin HEAD",
    "git checkout main -- README.md && git push -u origin HEAD",
    "git checkout main && git push -u origin feat/x",
    "git branch -d old && git push -u origin HEAD",
    "git symbolic-ref HEAD && git push -u origin HEAD",
]


@pytest.mark.parametrize("cmd", SWITCH_THEN_PUSH_BLOCKED)
def test_switch_then_push_blocked(fake_git, cmd):
    assert guard.decide(cmd, None), cmd


@pytest.mark.parametrize("cmd", SWITCH_THEN_PUSH_ALLOWED)
def test_switch_then_push_allowed(fake_git, cmd):
    assert guard.decide(cmd, None) is None, cmd


def test_switch_in_another_repo_does_not_move_this_one(make_repo):
    here = make_repo("here")
    other = make_repo("other")
    assert guard.decide(f"git -C {other} checkout main && git push -u origin HEAD", str(here)) is None
    assert guard.decide(f"cd {other} && git checkout main && git push -u origin HEAD", str(here))
    assert guard.decide("git checkout main && git push -u origin HEAD", str(here))
