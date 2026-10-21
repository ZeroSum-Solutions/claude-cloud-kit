import hashlib
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

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
        f"# test policy\nwiggdevin/client-*\n{sha('wiggdevin/Example-Client-Site')}\n")
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
    plain = [ln for ln in lines if ln and not ln.startswith("sha256:")]
    # only globs and the kit itself are written out; every client is a hash
    assert plain == ["zerosum-solutions/claude-cloud-kit", "wiggdevin/client-*"]
    assert land.policy_for("ZeroSum-Solutions/claude-cloud-kit", ROOT)[0] is False


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


def test_latest_runs_keeps_same_named_jobs_from_other_workflows():
    runs = [run("test", conclusion="failure", suite=1, rid=10), run("test", suite=2, rid=11)]
    assert land.judge(land.latest_runs(runs), [], set()).state == "fail"


def test_latest_runs_takes_the_newest_attempt_in_a_suite():
    runs = [run("test", conclusion="failure", suite=1, rid=10), run("test", suite=1, rid=12)]
    assert land.judge(land.latest_runs(runs), [], set()).state == "pass"


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


# ------------------------------------------------ CI roll-up when statuses are unreadable

REPO = land.Repo(nwo="o/r", owner="o", default="main", delete_branch_on_merge=True)


def fake_ci(monkeypatch, runs, mergeable_state, head="abc"):
    def gh_list(path, key=None):
        if "/status" in path:
            raise land.GhError(path, "Resource not accessible by integration")
        return runs

    monkeypatch.setattr(land, "gh_list", gh_list)
    monkeypatch.setattr(land, "read_pr", lambda repo, n: {
        "mergeable": True, "mergeable_state": mergeable_state, "head": {"sha": head}})


def test_rollup_clean_passes(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "clean")
    verdict, rollup = land.ci_state(REPO, 1, "abc", "u", set())
    assert verdict.state == "pass" and rollup == "ok"


def test_rollup_unstable_waits(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "unstable")
    assert land.ci_state(REPO, 1, "abc", "u", set())[1] == "wait"


def test_rollup_blocked_is_reported(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "blocked")
    assert land.ci_state(REPO, 1, "abc", "u", set())[1] == "blocked"


def test_rollup_dirty_stops(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "dirty")
    with pytest.raises(land.Stop):
        land.ci_state(REPO, 1, "abc", "u", set())


def test_rollup_head_moved_stops(monkeypatch):
    fake_ci(monkeypatch, [run("check")], "clean", head="other")
    with pytest.raises(land.Stop):
        land.ci_state(REPO, 1, "abc", "u", set())


def test_other_status_errors_are_not_swallowed(monkeypatch):
    def gh_list(path, key=None):
        if "/status" in path:
            raise land.GhError(path, "Server Error")
        return [run("check")]

    monkeypatch.setattr(land, "gh_list", gh_list)
    with pytest.raises(land.Stop):
        land.ci_state(REPO, 1, "abc", "u", set())


def test_wait_fails_closed_on_only_skipped(monkeypatch):
    fake_ci(monkeypatch, [run("a", conclusion="skipped")], "clean")
    monkeypatch.setattr(land, "NO_CI_GRACE", 0)
    monkeypatch.setattr(land.time, "sleep", lambda s: None)
    with pytest.raises(land.Stop, match="only skipped"):
        land.wait_for_ci(REPO, 1, "abc", "u", set(), None)
