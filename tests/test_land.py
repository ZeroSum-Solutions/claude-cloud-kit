import hashlib
import importlib.machinery
import importlib.util
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name: str, rel: str):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / rel))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


land = load("land", "bin/land")


def run(name, status="completed", conclusion="success", suite=1, rid=1, app=15368):
    return {"name": name, "status": status, "conclusion": conclusion, "id": rid,
            "check_suite": {"id": suite}, "app": {"id": app}}


def sha(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.lower().encode()).hexdigest()


@pytest.fixture
def kit(tmp_path, monkeypatch):
    """A kit dir whose policy hashes a made-up client repo."""
    monkeypatch.delenv("LAND_OWNERS", raising=False)
    monkeypatch.delenv("LAND_CLIENT_REPOS", raising=False)
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "client-repos.txt").write_text(
        f"# test policy\nwiggdevin/client-*\nid:4242\n{sha('wiggdevin/Example-Client-Site')}\n")
    return tmp_path


def test_parse_nwo_forms():
    assert land.parse_nwo("https://github.com/wiggdevin/zs-land-sandbox") == "wiggdevin/zs-land-sandbox"
    assert land.parse_nwo("https://github.com/o/r.git") == "o/r"
    assert land.parse_nwo("git@github.com:o/r.git") == "o/r"
    assert land.parse_nwo("ssh://git@github.com/o/r") == "o/r"
    assert land.parse_nwo("http://local_proxy@127.0.0.1:41000/git/o/r") == "o/r"
    assert land.parse_nwo("https://gitlab.com/o/r") is None


def test_parse_nwo_rejects_lookalikes():
    assert land.parse_nwo("https://notgithub.com/wiggdevin/demo") is None
    assert land.parse_nwo("/tmp/x/github.com/wiggdevin/demo") is None
    assert land.parse_nwo("https://evil.example/git/wiggdevin/demo") is None


def test_policy_owner_and_clients(kit):
    pol = lambda name, **kw: land.policy_for(name, kit, **kw)[0]
    assert pol("wiggdevin/zs-land-sandbox") is True
    assert pol("zerosum-solutions/internal-tool") is True
    assert pol("wiggdevin/example-client-site") is False          # hashed entry, any case
    assert pol("wiggdevin/client-anything") is False                  # glob entry
    assert pol("someone-else/example-client-site") is False       # owner not allowlisted
    # a renamed client repo is still caught through its old (origin) name
    assert pol("wiggdevin/renamed-site", aliases=("wiggdevin/example-client-site",)) is False
    # and through its repository id, whatever it is called now
    assert pol("wiggdevin/any-new-name", repo_id=4242) is False
    assert pol("wiggdevin/any-new-name", repo_id=4243) is True
    assert pol("wiggdevin/id:4242") is True                        # an id line is not a name glob


def test_policy_env_never_replaces_an_empty_file(tmp_path, monkeypatch):
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "client-repos.txt").write_text("# comments only\n")
    monkeypatch.setenv("LAND_CLIENT_REPOS", "someone/else")
    ok, why = land.policy_for("wiggdevin/zs-land-sandbox", tmp_path)
    assert ok is False and "no entries" in why


def test_policy_fails_closed_on_undecodable_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LAND_CLIENT_REPOS", raising=False)
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "client-repos.txt").write_bytes(b"\xff\xfe\x00bad")
    assert land.policy_for("wiggdevin/zs-land-sandbox", tmp_path)[0] is False


def test_policy_fails_closed_without_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LAND_CLIENT_REPOS", raising=False)
    ok, why = land.policy_for("wiggdevin/zs-land-sandbox", tmp_path / "missing")
    assert ok is False and "unreadable" in why


def test_policy_env_overrides(kit, monkeypatch):
    monkeypatch.setenv("LAND_CLIENT_REPOS", "wiggdevin/zs-land-sandbox")
    assert land.policy_for("wiggdevin/zs-land-sandbox", kit)[0] is False
    monkeypatch.delenv("LAND_CLIENT_REPOS")
    monkeypatch.setenv("LAND_OWNERS", "someone")
    assert land.policy_for("wiggdevin/zs-land-sandbox", kit)[0] is False


def test_shipped_policy_names_no_client(monkeypatch):
    monkeypatch.delenv("LAND_CLIENT_REPOS", raising=False)
    lines = [ln.split("#", 1)[0].strip() for ln in (ROOT / "policy" / "client-repos.txt").read_text().splitlines()]
    plain = [ln for ln in lines if ln and not ln.startswith(("sha256:", "id:"))]
    # only globs and the kit itself are written out; every client is an id or a hash
    assert plain == ["zerosum-solutions/claude-cloud-kit", "wiggdevin/client-*"]
    assert all(re.fullmatch(r"id:\d+|sha256:[0-9a-f]{64}", ln) for ln in lines if ln and ln not in plain)
    assert land.policy_for("ZeroSum-Solutions/claude-cloud-kit", ROOT)[0] is False
    assert land.policy_for("ZeroSum-Solutions/renamed-kit", ROOT, repo_id=1428360273)[0] is False


def test_advisory_names_split_on_commas_only():
    assert land.split_list("Visual check, coderabbit\nPreview build") == {"Visual check", "coderabbit", "Preview build"}


def test_judge_all_green():
    v = land.judge([run("check"), run("lint")], [{"context": "vercel", "state": "success"}], set())
    assert v.state == "pass" and len(v.passed) == 3


def test_judge_pending_waits():
    v = land.judge([run("check"), run("e2e", status="in_progress", conclusion=None)], [], set())
    assert v.state == "pending" and v.pending == ["e2e"]


def test_judge_status_pending_waits():
    v = land.judge([run("check")], [{"context": "vercel", "state": "pending"}], set())
    assert v.state == "pending"


def test_judge_failure_blocks_even_while_pending():
    v = land.judge([run("check", conclusion="failure"), run("e2e", status="queued", conclusion=None)], [], set())
    assert v.state == "fail"


def test_judge_cancelled_and_neutral_block():
    assert land.judge([run("a"), run("b", conclusion="cancelled")], [], set()).state == "fail"
    assert land.judge([run("a"), run("b", conclusion="neutral")], [], set()).state == "fail"


def test_judge_only_skipped_waits_for_real_checks():
    v = land.judge([run("a", conclusion="skipped")], [], set())
    assert v.state == "skipped"


def test_judge_skipped_with_a_pass_is_fine():
    assert land.judge([run("a"), run("b", conclusion="skipped")], [], set()).state == "pass"


def test_judge_no_checks():
    assert land.judge([], [], set()).state == "none"


def test_advisory_excluded_from_both_sides():
    adv = {"coderabbit"}
    assert land.judge([run("check"), run("coderabbit", conclusion="failure")], [], adv).state == "pass"
    # an advisory pass alone verifies nothing
    assert land.judge([run("coderabbit")], [], adv).state == "none"


def test_status_error_blocks():
    assert land.judge([run("check")], [{"context": "ci/x", "state": "error"}], set()).state == "fail"


def test_strip_proxy_footer():
    body = land.pr_body(["feat: a"])
    proxied = body + "\n---\n_Generated by [Claude Code](https://claude.ai/code/session_01ABC)_\n"
    assert land.strip_proxy_footer(proxied) == body
    assert land.strip_proxy_footer(body) == body


def test_squash_message():
    assert land.squash_message(["feat: a"]) == ""
    assert land.squash_message(["feat: a", "fix: b"]) == "* feat: a\n* fix: b"


def test_pr_body_lists_commits():
    body = land.pr_body(["feat: a", "fix: b"])
    assert "- feat: a" in body and "- fix: b" in body and "## Test plan" in body




def test_land_starts_with_an_older_kitpolicy(tmp_path, monkeypatch):
    # an older installer can pair a new land with a kitpolicy that has no split_list
    (tmp_path / "bin").mkdir()
    (tmp_path / "lib").mkdir()
    shutil.copy(ROOT / "bin" / "land", tmp_path / "bin" / "land")
    (tmp_path / "lib" / "kitpolicy.py").write_text(
        "def policy_for(full_name, kit_dir, aliases=(), repo_id=None):\n    return False, 'old'\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "kitpolicy")
    old = load("land_old_policy", str(tmp_path / "bin" / "land"))
    assert old.split_list("a, b\nc d") == {"a", "b", "c d"}


def test_a_kitpolicy_without_repo_ids_never_merges(tmp_path, monkeypatch):
    # a mixed refresh (CDN skew under the pre-hash installer) pairs this land with a
    # kitpolicy that cannot check repository ids: open the PR, never merge
    (tmp_path / "bin").mkdir()
    (tmp_path / "lib").mkdir()
    shutil.copy(ROOT / "bin" / "land", tmp_path / "bin" / "land")
    (tmp_path / "lib" / "kitpolicy.py").write_text(
        "def policy_for(full_name, kit_dir):\n    return True, 'owner allowlisted'\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "kitpolicy")
    old = load("land_older_policy", str(tmp_path / "bin" / "land"))
    may_merge, why = old.merge_allowed(REPO_FOR_POLICY, "o/r")
    assert may_merge is False and "kit" in why


# ------------------------------------------------ CI gate: check runs, then GitHub's roll-up

REPO_FOR_POLICY = land.Repo(nwo="o/r", id=1, owner="o", default="main", delete_branch_on_merge=True)
REPO = land.Repo(nwo="o/r", id=1, owner="o", default="main", delete_branch_on_merge=True)


class Clock:
    """Stands in for land's time module: sleeping moves the clock."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(land, "time", c)
    return c


def live_pr(mergeable_state="clean", head="abc", mergeable=True, **extra):
    return {"number": 1, "state": "open", "draft": False, "mergeable": mergeable,
            "mergeable_state": mergeable_state, "head": {"sha": head}, "base": {"ref": "main"}, **extra}


def fake_ci(monkeypatch, runs, mergeable_state="clean", head="abc", statuses=None, **extra):
    """statuses=None: the token cannot read commit statuses (the cloud GitHub app)."""
    reads = []

    def gh_list(path, key=None):
        if "/status" in path:
            if statuses is None:
                raise land.GhError(path, "Resource not accessible by integration", 403)
            return statuses
        return runs

    def read_pr(repo, n):
        reads.append(n)
        return live_pr(mergeable_state, head, **extra)

    monkeypatch.setattr(land, "gh_list", gh_list)
    monkeypatch.setattr(land, "read_pr", read_pr)
    return reads


def test_rollup_clean_passes(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "clean")
    verdict, rollup = land.ci_state(REPO, 1, "abc", "u", set())
    assert verdict.state == "pass" and rollup == "ok"


@pytest.mark.parametrize("statuses", [None, []], ids=["statuses-unreadable", "statuses-readable"])
@pytest.mark.parametrize("state", ["unstable", "blocked"])
def test_rollup_gates_whether_or_not_statuses_are_readable(monkeypatch, statuses, state):
    fake_ci(monkeypatch, [run("check")], state, statuses=statuses)
    assert land.ci_state(REPO, 1, "abc", "u", set())[1] == state


def test_rollup_needs_mergeable_true(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "clean", mergeable=None)
    assert land.ci_state(REPO, 1, "abc", "u", set())[1] != "ok"


def test_rollup_dirty_stops(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "dirty", mergeable=False)
    with pytest.raises(land.Stop, match="conflicts"):
        land.ci_state(REPO, 1, "abc", "u", set())


def test_rollup_head_moved_stops(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "clean", head="other")
    with pytest.raises(land.Stop, match="head changed"):
        land.ci_state(REPO, 1, "abc", "u", set())


def test_other_status_errors_are_not_swallowed(monkeypatch):
    def gh_list(path, key=None):
        if "/status" in path:
            raise land.GhError(path, "Server Error", 500)
        return [run("check")]

    monkeypatch.setattr(land, "gh_list", gh_list)
    with pytest.raises(land.Stop):
        land.ci_state(REPO, 1, "abc", "u", set())


def test_wait_fails_closed_on_only_skipped(monkeypatch, clock):
    fake_ci(monkeypatch, [run("a", conclusion="skipped")], "clean")
    monkeypatch.setattr(land, "NO_CI_GRACE", 0)
    with pytest.raises(land.Stop, match="only skipped"):
        land.wait_for_ci(REPO, 1, "abc", "u", set(), clock.t)


def test_check_runs_use_githubs_latest_view_and_every_run_is_judged(monkeypatch):
    # land judges exactly what the check-runs API returns under its default filter=latest
    # (GitHub's newest run per check name, the view `gh pr checks` and zs-land use)
    paths = []

    def gh_list(path, key=None):
        paths.append(path)
        if "/status" in path:
            return []
        return [run("test"), run("test", conclusion="neutral", rid=2), run("lint")]

    monkeypatch.setattr(land, "gh_list", gh_list)
    verdict = land.read_checks(REPO, "abc", set(), "u")
    runs_path = next(p for p in paths if "/check-runs" in p)
    assert "filter=" not in runs_path
    assert verdict.state == "fail"


def test_rollup_catches_a_leg_land_cannot_see(monkeypatch, clock):
    fake_ci(monkeypatch, [run("test")], "unstable", statuses=[])
    with pytest.raises(land.Stop, match="unstable"):
        land.wait_for_ci(REPO, 1, "abc", "u", set(), clock.t)


@pytest.mark.parametrize("state", ["unstable", "blocked"])
def test_unstable_and_blocked_stop_after_the_bounded_wait(monkeypatch, clock, state):
    fake_ci(monkeypatch, [run("check"), run("coderabbit", conclusion="failure")], state)
    start = clock.t
    with pytest.raises(land.Stop) as stop:
        land.wait_for_ci(REPO, 1, "abc", "u", {"coderabbit"}, start)
    msg = str(stop.value)
    assert stop.value.code == 1
    assert land.BLOCKED_WAIT <= clock.t - start < land.BLOCKED_WAIT + 2 * land.POLL
    assert "coderabbit [failure]" in msg and "advisory" in msg and "merge by hand" in msg
    assert "run land again" in msg


def test_pass_waits_for_the_settle_window_after_the_push(monkeypatch, clock):
    fake_ci(monkeypatch, [run("check")], "clean")
    pushed_at = clock.t
    assert land.wait_for_ci(REPO, 1, "abc", "u", set(), pushed_at).state == "pass"
    assert clock.t - pushed_at >= land.PR_SETTLE


def test_settle_window_starts_after_the_push_and_the_pr_for_a_reused_pr(monkeypatch, clock):
    seen = {}
    monkeypatch.setattr(land, "resolve_repo", lambda: REPO)
    monkeypatch.setattr(land, "preflight", lambda repo: ("feat/x", ["feat: x"]))
    monkeypatch.setattr(land, "merge_allowed", lambda *a: (True, "owner allowlisted"))
    monkeypatch.setattr(land, "git", lambda *a, **k: "abc" if a == ("rev-parse", "HEAD") else "")

    def push(cmd, **kw):
        clock.t += 7
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(land, "subprocess", SimpleNamespace(run=push))

    def find_or_open_pr(*a):
        clock.t += 5  # a PR an earlier run opened, read after the push
        return {"number": 1, "html_url": "u", "draft": False}

    monkeypatch.setattr(land, "find_or_open_pr", find_or_open_pr)

    def wait(repo, number, sha, url, advisory, settle_from):
        seen["settle_from"] = settle_from
        raise land.Stop("stop here")

    monkeypatch.setattr(land, "wait_for_ci", wait)
    with pytest.raises(land.Stop, match="stop here"):
        land.main([])
    # the window counts from when the PR is known, after this run's push
    assert seen["settle_from"] == 1012.0


# ------------------------------------------------ recheck: wait for mergeability, then read the PR last

def fake_recheck(monkeypatch, runs=None, waited=None, **pr):
    """waited: the PR read_pr returns after its wait; the final read returns live_pr(**pr)."""
    order = []

    def gh_api(path, *a, **k):
        if "/pulls/" in path:
            order.append("pr")
            return live_pr(**pr)
        order.append("repo")
        return {"default_branch": "main"}

    def read_pr(repo, n):
        order.append("wait")
        return live_pr(**(waited or {}))

    def git(*a, **k):
        order.append("head")
        return "abc"

    monkeypatch.setattr(land, "gh_api", gh_api)
    monkeypatch.setattr(land, "git", git)
    monkeypatch.setattr(land, "gh_list", lambda path, key=None: order.append("ci") or ([] if "/status" in path else runs or [run("check")]))
    monkeypatch.setattr(land, "read_pr", read_pr)
    return order


def test_recheck_waits_first_and_reads_the_pr_last_and_once(monkeypatch):
    order = fake_recheck(monkeypatch)
    assert land.recheck(REPO, 1, "abc", "u", set())["mergeable_state"] == "clean"
    assert order[0] == "wait" and order[-1] == "pr" and order.count("pr") == 1
    assert {"repo", "ci", "head"} <= set(order[1:-1])


def test_recheck_judges_the_last_read_not_the_waited_one(monkeypatch):
    fake_recheck(monkeypatch, waited={}, mergeable_state="unstable")
    with pytest.raises(land.Stop, match="unstable"):
        land.recheck(REPO, 1, "abc", "u", set())


@pytest.mark.parametrize("pr", [
    {"mergeable_state": "unstable"},
    {"mergeable_state": "blocked"},
    {"mergeable": None},
    {"state": "closed"},
    {"draft": True},
    {"head": "other"},
    {"base": {"ref": "release"}},
], ids=["unstable", "blocked", "mergeable-null", "closed", "draft", "head-moved", "base-moved"])
def test_recheck_judges_every_condition_on_the_one_pr_read(monkeypatch, pr):
    order = fake_recheck(monkeypatch, **pr)
    with pytest.raises(land.Stop):
        land.recheck(REPO, 1, "abc", "u", set())
    assert order.count("pr") == 1


# ------------------------------------------------ merge: refused vs unknown

PR = {"number": 1, "html_url": "u", "title": "feat: x"}


def fake_merge(monkeypatch, put_error, gets):
    gets = list(gets)

    def gh_api(path, method="GET", body=None):
        if method == "PUT":
            raise put_error
        item = gets.pop(0) if len(gets) > 1 else gets[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(land, "gh_api", gh_api)


@pytest.mark.parametrize("status", [405, 409, 422])
def test_merge_refused_means_nothing_merged(monkeypatch, clock, status):
    fake_merge(monkeypatch, land.GhError("m", "Pull Request is not mergeable", status), [{"merged": False}])
    with pytest.raises(land.Stop, match="refused") as stop:
        land.merge(REPO, PR, "abc", ["feat: x"])
    assert stop.value.code == 1


@pytest.mark.parametrize("status", [405, 409, 422])
def test_a_refusal_for_an_already_merged_pr_continues(monkeypatch, clock, status):
    # 405 also means "already merged", e.g. by an earlier run whose answer was lost
    merged = {"merged": True, "merge_commit_sha": "m1", "head": {"sha": "abc"}}
    fake_merge(monkeypatch, land.GhError("m", "Pull Request is not mergeable", status), [merged])
    assert land.merge(REPO, PR, "abc", ["feat: x"]) == "m1"


def test_a_refusal_for_a_pr_merged_at_another_head_is_exit_3(monkeypatch, clock):
    merged = {"merged": True, "merge_commit_sha": "m1", "head": {"sha": "other"}}
    fake_merge(monkeypatch, land.GhError("m", "Method Not Allowed", 405), [merged])
    with pytest.raises(land.Stop, match="MERGED") as stop:
        land.merge(REPO, PR, "abc", ["feat: x"])
    assert stop.value.code == 3


def test_a_refusal_that_cannot_be_confirmed_is_exit_3(monkeypatch, clock):
    fake_merge(monkeypatch, land.GhError("m", "Method Not Allowed", 405), [land.GhError("p", "Bad Gateway", 502)])
    with pytest.raises(land.Stop, match="unknown") as stop:
        land.merge(REPO, PR, "abc", ["feat: x"])
    assert stop.value.code == 3


@pytest.mark.parametrize("error", [land.GhError("m", "Bad Gateway", 502), ValueError("Expecting value"), OSError("reset")],
                         ids=["5xx", "undecodable", "network"])
def test_merge_unknown_result_is_exit_3_never_1(monkeypatch, clock, error):
    fake_merge(monkeypatch, error, [land.GhError("p", "Bad Gateway", 502), {"merged": False}])
    start = clock.t
    with pytest.raises(land.Stop, match="unknown") as stop:
        land.merge(REPO, PR, "abc", ["feat: x"])
    assert stop.value.code == 3 and clock.t - start >= 30


def test_merge_unknown_result_recovers_when_github_reports_merged(monkeypatch, clock):
    merged = {"merged": True, "merge_commit_sha": "m1", "head": {"sha": "abc"}}
    fake_merge(monkeypatch, ValueError("Expecting value"), [land.GhError("p", "Bad Gateway", 502), {"merged": False}, merged])
    assert land.merge(REPO, PR, "abc", ["feat: x"]) == "m1"


# ------------------------------------------------ after the merge

class FakeGit:
    def __init__(self, top, fail=()):
        self.top, self.fail, self.calls = str(top), set(fail), []

    def git(self, *args, check=True):
        self.calls.append(args)
        if args in self.fail:
            if check:
                raise land.Stop(f"git {' '.join(args)} failed")
            return ""
        if args == ("rev-parse", "--show-toplevel"):
            return self.top
        return "abc" if args == ("rev-parse", "HEAD") else ""

    def git_ok(self, *args):
        self.calls.append(args)
        return args not in self.fail


def fake_after(monkeypatch, tmp_path, fail=(), remote_head="abc", pr_reads=None):
    g = FakeGit(tmp_path, fail)
    monkeypatch.setattr(land, "git", g.git)
    monkeypatch.setattr(land, "git_ok", g.git_ok)
    reads = list(pr_reads or [{"merged": True, "merge_commit_sha": "m1", "head": {"sha": "abc"}}])
    api = []

    def gh_api(path, method="GET", body=None):
        api.append((method, path))
        if "/pulls/" in path:
            item = reads.pop(0) if len(reads) > 1 else reads[0]
            if isinstance(item, Exception):
                raise item
            return item
        if "/git/ref/" in path:
            return {"object": {"sha": remote_head}}
        return {}

    monkeypatch.setattr(land, "gh_api", gh_api)
    monkeypatch.chdir(ROOT)
    return g, api


def test_after_merge_fetches_with_an_explicit_refspec_from_the_toplevel(monkeypatch, clock, tmp_path):
    g, _ = fake_after(monkeypatch, tmp_path)
    problems, where = land.after_merge(REPO, PR, "feat/x", "abc", "m1")
    assert problems == [] and where == "main"
    assert ("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main") in g.calls
    assert Path.cwd() == tmp_path.resolve()


def test_after_merge_retries_the_pr_read(monkeypatch, clock, tmp_path):
    fake_after(monkeypatch, tmp_path, pr_reads=[land.GhError("p", "Bad Gateway", 502), {"merged": False},
                                                {"merged": True, "merge_commit_sha": "m1", "head": {"sha": "abc"}}])
    assert land.after_merge(REPO, PR, "feat/x", "abc", "m1")[0] == []


@pytest.mark.parametrize("remote_head,deleted", [("abc", True), ("pushed-later", False)])
def test_remote_branch_is_deleted_only_at_the_merged_head(monkeypatch, clock, tmp_path, remote_head, deleted):
    repo = land.Repo(nwo="o/r", id=1, owner="o", default="main", delete_branch_on_merge=False)
    _, api = fake_after(monkeypatch, tmp_path, remote_head=remote_head)
    assert land.after_merge(repo, PR, "feat/x", "abc", "m1")[0] == []
    assert (("DELETE", "repos/o/r/git/refs/heads/feat/x") in api) is deleted


def test_after_merge_detaches_when_the_default_branch_is_in_another_worktree(monkeypatch, clock, tmp_path):
    g, _ = fake_after(monkeypatch, tmp_path, fail={("switch", "-q", "main")})
    problems, where = land.after_merge(REPO, PR, "feat/x", "abc", "m1")
    assert problems == [] and "detached" in where
    assert ("switch", "-q", "--detach", "origin/main") in g.calls
    assert ("update-ref", "-d", "refs/heads/feat/x", "abc") in g.calls


@pytest.mark.parametrize("head", [{"sha": "other"}, None], ids=["other-head", "no-head"])
def test_after_merge_keeps_the_branches_unless_the_merged_head_is_ours(monkeypatch, clock, tmp_path, head):
    repo = land.Repo(nwo="o/r", id=1, owner="o", default="main", delete_branch_on_merge=False)
    read = {"merged": True, "merge_commit_sha": "m1", **({"head": head} if head else {})}
    g, api = fake_after(monkeypatch, tmp_path, pr_reads=[read])
    problems, _ = land.after_merge(repo, PR, "feat/x", "abc", "m1")
    assert problems and "head" in problems[0]
    assert not any(m == "DELETE" for m, _ in api)
    assert not any(c[:1] in (("update-ref",), ("switch",)) for c in g.calls)


def test_after_merge_reports_a_failed_detach(monkeypatch, clock, tmp_path):
    g, _ = fake_after(monkeypatch, tmp_path, fail={("switch", "-q", "main"), ("switch", "-q", "--detach", "origin/main")})
    problems, where = land.after_merge(REPO, PR, "feat/x", "abc", "m1")
    assert problems and where == "feat/x"
    assert ("update-ref", "-d", "refs/heads/feat/x", "abc") not in g.calls
