import fcntl
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("cloud_prep", str(ROOT / "bin" / "cloud-prep"))
spec = importlib.util.spec_from_loader("cloud_prep", loader)
prep = importlib.util.module_from_spec(spec)
sys.modules["cloud_prep"] = prep
loader.exec_module(prep)


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setattr(prep, "STATE", d)
    return d


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# ---------------------------------------------------------------- version picking

@pytest.mark.parametrize("pin, major", [
    (None, 24),
    ("24", 24),
    ("v24.16.0", 24),
    ("~24", 24),
    (">=22", 24),
    (">=20 <23", 22),
    ("26", 26),
    ("^22.22.0 || ^24.11.0 || >=26.0.0", 24),
    ("lts/*", 24),
    ("lts", 24),
    ("node", 24),
    ("20.x", 20),
    ("lts/iron", 20),
    ("lts/jod", 22),
    ("lts/hydrogen", 18),
    ("LTS/Iron", 20),
    (">24", 26),          # strict: 24 itself is excluded
    (">24.1", 24),        # 24.2+ still fits 24
    ("<24", 22),          # strict upper bound, newest even major below it
    ("<=22", 22),
    ("20 - 22", 22),
    ("20 - 24", 24),
    ("^20 || ^22", 22),
    (">= 20 < 24", 22),
    (">=18 <20", 18),
    ("25", 25),           # odd majors only when nothing else fits
    ("27", 27),           # beyond the known majors: fall back to the number in the pin
    (">=20<23", 22),      # comparators without whitespace
    (">=18<=20", 20),
    ("lts/gallium", 16),  # nothing known fits: the lowest major the pin allows
    (">26", 27),
    ("garbage20", 20),    # unreadable: the number in the pin, not the default
    ("lts/unknown", 24),
    ("^22 || nonsense", 22),
])
def test_pick_major(pin, major):
    assert prep.pick_major(pin) == major


def test_pin_allows():
    assert prep.pin_allows(None, 24) and prep.pin_allows(">=20<23", 22)
    assert not prep.pin_allows("<18", 18) and not prep.pin_allows("garbage20", 20)


def test_pick_major_uses_installed_majors():
    assert prep.pick_major(">26", installed=(28,)) == 28


def test_node_spec_order(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"engines": {"node": ">=22"}}))
    assert prep.node_spec(tmp_path) == (">=22", "package.json engines")
    (tmp_path / ".tool-versions").write_text("python 3.12\nnodejs 26.1.0\n")
    assert prep.node_spec(tmp_path) == ("26.1.0", ".tool-versions")
    (tmp_path / ".mise.toml").write_text('[tools]\nnode = "22"\n')
    assert prep.node_spec(tmp_path) == ("22", ".mise.toml")
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "24.16.0"\n')
    assert prep.node_spec(tmp_path) == ("24.16.0", "mise.toml")
    (tmp_path / ".node-version").write_text("20\n")
    assert prep.node_spec(tmp_path) == ("20", ".node-version")
    (tmp_path / ".nvmrc").write_text("24\n")
    assert prep.node_spec(tmp_path) == ("24", ".nvmrc")


def test_nvmrc_skips_comments_and_blank_lines(tmp_path):
    (tmp_path / ".nvmrc").write_text("# pinned for CI\n\n  22  # current LTS\n")
    assert prep.node_spec(tmp_path) == ("22", ".nvmrc")
    (tmp_path / ".nvmrc").write_text("# only a comment\n\n")
    (tmp_path / ".node-version").write_text("  # c\nlts/iron\n")
    assert prep.node_spec(tmp_path) == ("lts/iron", ".node-version")


@pytest.mark.parametrize("toml, pin", [
    ('[tools]\nnode = "26"\n', "26"),
    ('[tools]\nnode = { version = "26" }\n', "26"),
    ('[tools]\nnode = ["22", "24"]\n', "22"),
    ('[tools]\nnodejs = 24\n', "24"),
    ('[env]\nnode = "22"\n\n[tools]\nnode = "26"\n', "26"),
])
def test_mise_forms(tmp_path, toml, pin):
    (tmp_path / "mise.toml").write_text(toml)
    assert prep.node_spec(tmp_path) == (pin, "mise.toml")


def test_mise_without_node_does_not_shadow(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\nnode = "22"\n[tools]\npython = "3.12"\n')
    (tmp_path / ".tool-versions").write_text("nodejs 20.1.0\n")
    assert prep.node_spec(tmp_path) == ("20.1.0", ".tool-versions")
    (tmp_path / ".mise.toml").write_text("this is [not toml")
    assert prep.node_spec(tmp_path) == ("20.1.0", ".tool-versions")


# ---------------------------------------------------------------- install plans

def test_install_plans(tmp_path):
    assert prep.install_plans(tmp_path) == []
    (tmp_path / "uv.lock").write_text("")
    assert [p[0] for p in prep.install_plans(tmp_path)] == ["uv"]
    (tmp_path / "package-lock.json").write_text("{}")
    assert [p[0] for p in prep.install_plans(tmp_path)] == ["npm", "uv"]
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert [p[0] for p in prep.install_plans(tmp_path)] == ["pnpm", "uv"]


def test_yarn_flags(tmp_path):
    (tmp_path / "yarn.lock").write_text("")
    assert prep.install_plans(tmp_path)[0][1] == ["yarn", "install", "--frozen-lockfile"]
    (tmp_path / "package.json").write_text(json.dumps({"packageManager": "yarn@4.5.0"}))
    assert prep.install_plans(tmp_path)[0][1] == ["yarn", "install", "--immutable"]
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
    assert prep.install_plans(tmp_path)[0][1] == ["yarn", "install", "--immutable"]


def test_yarn_pnp_output_is_the_pnp_file(tmp_path):
    (tmp_path / "yarn.lock").write_text("")
    (tmp_path / ".yarnrc.yml").write_text("enableTelemetry: false\n")
    assert prep.install_plans(tmp_path)[0][3] == ".pnp.cjs"
    (tmp_path / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
    assert prep.install_plans(tmp_path)[0][3] == "node_modules"


def test_poetry_has_no_install_dir(tmp_path):
    (tmp_path / "poetry.lock").write_text("")
    assert prep.install_plans(tmp_path)[0][3] is None


def test_stamp_changes_with_lockfile(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text("a")
    inputs = prep.install_plans(tmp_path)[0][2]
    first = prep.stamp_for(tmp_path, inputs, 24)
    assert prep.stamp_for(tmp_path, inputs, 24) == first
    assert prep.stamp_for(tmp_path, inputs, 26) != first
    (tmp_path / "pnpm-lock.yaml").write_text("b")
    assert prep.stamp_for(tmp_path, inputs, 24) != first


# ---------------------------------------------------------------- background install

def _fake_managers(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    _exe(bin_dir / "npm", 'mkdir -p node_modules; echo "$@" >> npm.calls')
    _exe(bin_dir / "uv", 'mkdir -p .venv; echo "$@" >> uv.calls')
    _exe(bin_dir / "poetry", 'echo "$@" >> poetry.calls')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return bin_dir


def test_run_install_runs_every_plan_and_skips_when_unchanged(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    (repo / "uv.lock").write_text("")
    assert prep.run_install(repo, 24, None) == 0
    status = (prep.state_dir(repo) / "status").read_text()
    assert status.startswith("ok npm (") and "; ok uv (" in status
    assert (repo / "npm.calls").read_text().count("ci") == 1
    assert prep.run_install(repo, 24, None) == 0
    assert (prep.state_dir(repo) / "status").read_text().strip() == "ok npm (unchanged); ok uv (unchanged)"
    assert (repo / "npm.calls").read_text().count("ci") == 1
    # Stamps live in the state dir, not in the repo.
    assert not list(repo.rglob("*.sha256"))


def test_poetry_stamp_does_not_create_venv(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "poetry.lock").write_text("")
    assert prep.run_install(repo, None, None) == 0
    assert not (repo / ".venv").exists()
    assert (prep.state_dir(repo) / "poetry.sha256").exists()
    assert prep.run_install(repo, None, None) == 0
    assert (repo / "poetry.calls").read_text().count("install") == 1


def test_missing_executable_fails_instead_of_running(tmp_path, state, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pnpm-lock.yaml").write_text("")
    assert prep.run_install(repo, None, None) == 1
    status = (prep.state_dir(repo) / "status").read_text()
    assert status.startswith("failed pnpm install --frozen-lockfile (pnpm not found on PATH)")


def test_failure_is_listed_first(tmp_path, state, monkeypatch):
    bin_dir = _fake_managers(tmp_path, monkeypatch)
    _exe(bin_dir / "uv", "exit 3")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    (repo / "uv.lock").write_text("")
    assert prep.run_install(repo, 24, None) == 1
    status = (prep.state_dir(repo) / "status").read_text()
    assert status.startswith("failed uv sync --frozen (exit 3,") and "; ok npm (" in status


def test_unwritable_stamp_is_reported(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "uv.lock").write_text("")
    real_write = Path.write_text

    def write_text(self, *a, **kw):
        if self.name.endswith(".sha256"):
            raise PermissionError("read-only")
        return real_write(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", write_text)
    assert prep.run_install(repo, None, None) == 0
    sd = prep.state_dir(repo)
    assert "stamp not saved" in (sd / "status").read_text()
    assert "could not save the install stamp" in (sd / "log").read_text()


def test_second_launch_skips_while_an_install_runs(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    launched = []
    monkeypatch.setattr(prep.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    sd = prep.state_dir(repo)
    (sd / "status").write_text("running npm ci\n")
    with open(sd / "lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        msg = prep.start_background_install(repo, 24, Path("/opt/node24/bin"))
    assert "already running" in msg and not launched
    msg = prep.start_background_install(repo, 22, Path("/opt/node22/bin"))
    assert msg.startswith("installing in the background")
    assert launched[0][-4:] == ["--node", "22", "--node-bin", "/opt/node22/bin"]


def test_current_stamps_start_nothing(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    assert prep.run_install(repo, 24, None) == 0
    sd = prep.state_dir(repo)
    (sd / "log").write_text("the real install log\n")
    status = (sd / "status").read_text()
    launched = []
    monkeypatch.setattr(prep.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    assert prep.start_background_install(repo, 24, None).startswith("up to date")
    assert not launched and (sd / "status").read_text() == status
    assert prep.run_install(repo, 24, None) == 0  # a child that finds nothing to do
    assert (sd / "log").read_text() == "the real install log\n"


def test_inputs_that_change_during_an_install_install_again(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    sd = prep.state_dir(repo)
    # A second prep asks for Node 26 while the Node 24 install runs.
    bin_dir = tmp_path / "fakebin"
    _exe(bin_dir / "npm", f'''mkdir -p node_modules; echo "$@" >> npm.calls
[ -f once ] || {{ touch once; echo '{{"node": 26, "node_bin": null}}' > "{sd}/request.json"; }}''')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert prep.run_install(repo, 24, None) == 0
    assert (repo / "npm.calls").read_text().count("ci") == 2
    assert all(prep.plan_current(repo, p, 26, sd) for p in prep.install_plans(repo))


def test_a_failed_reinstall_drops_the_old_stamp(tmp_path, state, monkeypatch):
    bin_dir = _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    assert prep.run_install(repo, 24, None) == 0
    sd = prep.state_dir(repo)
    assert (sd / "npm.sha256").exists()
    # node_modules goes away; the reinstall makes part of it and fails
    (repo / "node_modules").rmdir()
    _exe(bin_dir / "npm", "mkdir -p node_modules; exit 1")
    assert prep.run_install(repo, 24, None) == 1
    assert not (sd / "npm.sha256").exists()
    assert not prep.plan_current(repo, prep.install_plans(repo)[0], 24, sd)


def test_a_timed_out_install_stops_its_children(tmp_path, state, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    _exe(bin_dir / "npm", "(sleep 2; touch late) & sleep 30")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(prep, "INSTALL_TIMEOUT", 1)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    assert prep.run_install(repo, 24, None) == 1
    assert "timed out after 1s" in (prep.state_dir(repo) / "status").read_text()
    time.sleep(2.5)
    assert not (repo / "late").exists()


def test_wait_reports_a_stale_result(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    assert prep.run_install(repo, 24, None) == 0
    prep._write_request(repo, prep.state_dir(repo), 24, None)
    assert prep.wait_status(repo, 1).startswith("ok npm (")
    (repo / "package-lock.json").write_text('{"changed": true}')
    assert prep.wait_status(repo, 1).startswith("stale: the lockfiles or Node changed")


def test_wait_reports_a_changed_node_pin_as_stale(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    (repo / ".nvmrc").write_text("24\n")
    prep._write_request(repo, prep.state_dir(repo), 24, None)
    assert prep.run_install(repo, 24, None) == 0
    assert prep.wait_status(repo, 1).startswith("ok npm (")
    (repo / ".nvmrc").write_text("26\n")  # no prep ran since
    assert prep.wait_status(repo, 1).startswith("stale: the lockfiles or Node changed")


def test_a_pin_changed_while_node_resolves_reads_as_stale(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    (repo / ".nvmrc").write_text("24\n")
    node_dir = tmp_path / "opt" / "node24"
    _exe(node_dir / "bin" / "node", "echo v24.16.0")

    def install_node(major, deadline):
        (repo / ".nvmrc").write_text("26\n")  # the pin changes while Node 24 installs
        return node_dir

    monkeypatch.setattr(prep, "install_node", install_node)
    real_popen = prep.subprocess.Popen

    def popen(argv, **kw):  # the background child runs inline; npm and git stay real processes
        return prep.run_install(repo, 24, node_dir / "bin") if "--install" in argv else real_popen(argv, **kw)

    monkeypatch.setattr(prep.subprocess, "Popen", popen)
    prep.prep(repo, write_session_env=False, primary=True, deadline=time.monotonic() + 5)
    assert json.loads((prep.state_dir(repo) / "request.json").read_text())["pin"] == "24"
    assert prep.wait_status(repo, 1).startswith("stale: the lockfiles or Node changed")


def test_the_install_records_the_pin_of_the_request_it_built(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    (repo / ".nvmrc").write_text("24\n")
    sd = prep.state_dir(repo)
    prep._write_request(repo, sd, 24, None, pin="24")
    assert prep.run_install(repo, 24, None) == 0
    assert prep.wait_status(repo, 1).startswith("ok npm (")
    # A later prep reads lts/krypton, which is also Node 24: nothing to install, and not stale.
    (repo / ".nvmrc").write_text("lts/krypton\n")
    assert prep.start_background_install(repo, 24, None, pin="lts/krypton").startswith("up to date")
    assert prep.wait_status(repo, 1).startswith("ok npm (")


def test_wait_reports_a_fallback_node_build_as_stale(tmp_path, state, monkeypatch):
    # Argon R5: Node 26 could not be installed, cloud-prep fell back to 24; --wait must not say ok.
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    (repo / ".nvmrc").write_text("26\n")
    sd = prep.state_dir(repo)
    prep._write_request(repo, sd, 24, None, pin="26")
    assert prep.run_install(repo, 24, None) == 0
    assert json.loads((sd / "built.json").read_text()) == {"pin": "26", "node": 24}
    status = prep.wait_status(repo, 1)
    assert status.startswith("stale: built with Node 24, which does not satisfy the pin (26)"), status


def test_a_later_round_records_the_pin_the_repo_holds_then(tmp_path, state, monkeypatch):
    # Argon R5: the pin and a lockfile change during round 1 with no new cloud-prep; round 2
    # reinstalls and records the new pin, not the first request's.
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    (repo / ".nvmrc").write_text("24\n")
    sd = prep.state_dir(repo)
    prep._write_request(repo, sd, 24, None, pin="24")
    real = prep._install_round
    rounds = []

    def round_(*args):
        rounds.append(1)
        if len(rounds) == 1:
            (repo / ".nvmrc").write_text(">=24\n")
            (repo / "package-lock.json").write_text('{"lockfileVersion": 3}')
        return real(*args)

    monkeypatch.setattr(prep, "_install_round", round_)
    assert prep.run_install(repo, 24, None) == 0
    assert len(rounds) == 2
    assert json.loads((sd / "built.json").read_text()) == {"pin": ">=24", "node": 24}
    assert prep.wait_status(repo, 1).startswith("ok npm (")


def test_concurrent_requests_do_not_share_a_temp_file(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = prep.state_dir(repo)
    real = os.replace
    nested = []

    def replace(src, dst):
        if not nested:  # another prep writes its request in between
            nested.append(1)
            prep._write_request(repo, sd, 26, None)
        real(src, dst)

    monkeypatch.setattr(prep.os, "replace", replace)
    prep._write_request(repo, sd, 24, None)
    assert prep._read_request(sd, (None, None))[0] == 24
    assert sorted(f.name for f in sd.iterdir()) == ["request.json"]


def test_wait_never_sees_a_final_status_between_install_rounds(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    sd = prep.state_dir(repo)
    bin_dir = tmp_path / "fakebin"
    _exe(bin_dir / "npm", f'''mkdir -p node_modules; echo "$@" >> npm.calls
[ -f once ] || {{ touch once; echo '{{"node": 26, "node_bin": null}}' > "{sd}/request.json"; }}''')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    seen = []
    real_round = prep._install_round

    def install_round(*a):
        seen.append(prep._read(sd / "status") or "")
        return real_round(*a)

    monkeypatch.setattr(prep, "_install_round", install_round)
    assert prep.run_install(repo, 24, None) == 0
    assert len(seen) == 2 and seen[1].startswith("running")
    assert (sd / "status").read_text().startswith("ok npm (")


def test_wait_keeps_waiting_while_a_newer_install_runs(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    assert prep.run_install(repo, 24, None) == 0
    sd = prep.state_dir(repo)
    prep._write_request(repo, sd, 26, None)  # a newer request, its install running
    with open(sd / "lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert prep.wait_status(repo, 1).startswith("running")
    assert prep.wait_status(repo, 1).startswith("stale")


def test_up_to_date_replaces_an_old_failed_status(tmp_path, state, monkeypatch):
    _fake_managers(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}")
    assert prep.run_install(repo, 24, None) == 0
    sd = prep.state_dir(repo)
    (sd / "status").write_text("failed npm ci (exit 1, 3s) — log: x\n")  # from another branch
    assert prep.start_background_install(repo, 24, None).startswith("up to date")
    assert prep.wait_status(repo, 1).startswith("ok npm")


def test_wait_reports_a_dead_install(tmp_path, state):
    repo = tmp_path / "repo"
    repo.mkdir()
    sd = prep.state_dir(repo)
    (sd / "status").write_text("running npm ci\n")
    old = time.time() - 60
    os.utime(sd / "status", (old, old))
    assert prep.wait_status(repo, 5).startswith("failed (the install stopped without a result)")


def test_wait_from_a_subdirectory(tmp_path, state, monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert prep.repos_in(sub) == [repo]
    (prep.state_dir(repo) / "status").write_text("ok npm (3s)\n")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(sub)
    assert prep.main(["--wait", "--timeout", "1"]) == 0
    assert "repo: ok npm (3s)" in capsys.readouterr().out


# ---------------------------------------------------------------- node install

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _node_tarball(tmp_path, version="v26.1.0"):
    name = f"node-{version}-linux-x64"
    src = tmp_path / "src" / name
    _exe(src / "bin" / "node", f"echo {version}")
    _exe(src / "bin" / "npm", "true")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        tar.add(src, arcname=name)
    return f"{name}.tar.xz", buf.getvalue()


def _serve(monkeypatch, files):
    def urlopen(url, timeout=None):
        for suffix, body in files.items():
            if url.endswith(suffix):
                return _Response(body)
        raise OSError(f"unexpected url {url}")

    monkeypatch.setattr(prep.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prep, "_arch", lambda: "x64")


def test_install_node_is_atomic_and_marked(tmp_path, monkeypatch):
    opt = tmp_path / "opt"
    opt.mkdir()
    monkeypatch.setattr(prep, "OPT", opt)
    name, blob = _node_tarball(tmp_path)
    digest = hashlib.sha256(blob).hexdigest()
    _serve(monkeypatch, {"SHASUMS256.txt": f"{digest}  {name}\n".encode(), name: blob})
    # Half-installed by an older kit: node binary, no npm, no marker.
    _exe(opt / "node26" / "bin" / "node", "echo v26.0.0")
    target = prep.install_node(26, time.monotonic() + 30)
    assert target == opt / "node26"
    assert (target / prep.NODE_MARKER).exists() and (target / "bin" / "npm").exists()
    assert not list(opt.glob(".node26-tmp-*"))
    assert prep.node_ready(target, 26)


def test_install_node_checksum_mismatch_leaves_nothing(tmp_path, monkeypatch):
    opt = tmp_path / "opt"
    opt.mkdir()
    monkeypatch.setattr(prep, "OPT", opt)
    name, blob = _node_tarball(tmp_path)
    _serve(monkeypatch, {"SHASUMS256.txt": f"{'0' * 64}  {name}\n".encode(), name: blob})
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        prep.install_node(26, time.monotonic() + 30)
    assert not (opt / "node26").exists()
    assert not list(opt.glob(".node26-tmp-*"))


def test_install_node_respects_the_deadline(tmp_path, monkeypatch):
    opt = tmp_path / "opt"
    opt.mkdir()
    monkeypatch.setattr(prep, "OPT", opt)
    _serve(monkeypatch, {})
    with pytest.raises(TimeoutError):
        prep.install_node(26, time.monotonic() - 1)


class _Trickle(_Response):
    """A server that sends one byte every 50 ms, forever."""
    def read1(self, n=-1):
        time.sleep(0.05)
        return b"x"

    read = read1


def test_trickling_headers_stop_at_the_deadline(monkeypatch):
    def urlopen(url, timeout=None):  # headers that arrive a line every 50 ms, forever
        while True:
            time.sleep(0.05)

    monkeypatch.setattr(prep.urllib.request, "urlopen", urlopen)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        prep._fetch("https://example.invalid/x", time.monotonic() + 0.5)
    assert time.monotonic() - start < 2


@pytest.mark.parametrize("call", ["fetch", "download"])
def test_a_trickling_download_stops_at_the_deadline(tmp_path, monkeypatch, call):
    monkeypatch.setattr(prep.urllib.request, "urlopen", lambda url, timeout=None: _Trickle())
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        if call == "fetch":
            prep._fetch("https://example.invalid/x", time.monotonic() + 0.5)
        else:
            prep._download("https://example.invalid/x", tmp_path / "x", time.monotonic() + 0.5)
    assert time.monotonic() - start < 2


def test_install_node_waits_for_the_lock(tmp_path, monkeypatch):
    opt = tmp_path / "opt"
    opt.mkdir()
    monkeypatch.setattr(prep, "OPT", opt)
    with open(opt / ".node26.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError, match="waiting for .node26.lock"):
            prep.install_node(26, time.monotonic() + 1)


def test_image_node_without_marker_is_ready(tmp_path):
    target = tmp_path / "node22"
    _exe(target / "bin" / "node", "echo v22.11.0")
    _exe(target / "bin" / "npm", "true")
    assert prep.node_ready(target, 22)
    assert not prep.node_ready(target, 24)


def test_corepack_is_pinned_and_checked(tmp_path, monkeypatch):
    node_dir = tmp_path / "node26"
    _exe(node_dir / "bin" / "npm", "true")
    calls = []

    class Done:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", "npm ERR! network down\n"

    monkeypatch.setattr(prep.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done(1))
    problems = prep.ensure_corepack(node_dir, time.monotonic() + 30)
    assert calls == [[str(node_dir / "bin" / "npm"), "install", "-g", "--silent", "--ignore-scripts",
                      f"corepack@{prep.COREPACK_VERSION}"]]
    assert problems == ["npm install: exit 1 npm ERR! network down"]


def test_corepack_checks_for_shims_inside_the_lock(tmp_path):
    node_dir = tmp_path / "node26"
    _exe(node_dir / "bin" / "pnpm", "true")
    _exe(node_dir / "bin" / "yarn", "true")
    with open(tmp_path / ".node26.corepack.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            prep.ensure_corepack(node_dir, time.monotonic() + 1)
    assert prep.ensure_corepack(node_dir, time.monotonic() + 1) == []


def test_prep_makes_pnpm_shims_before_returning(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"packageManager": "pnpm@10.0.0"}))
    opt = tmp_path / "opt"
    node_dir = opt / "node24"
    _exe(node_dir / "bin" / "node", "echo v24.16.0")
    monkeypatch.setattr(prep, "OPT", opt)
    monkeypatch.setattr(prep, "install_node", lambda major, deadline: node_dir)
    order = []
    monkeypatch.setattr(prep, "ensure_corepack", lambda d, deadline: order.append(("corepack", d)) or ["offline"])
    monkeypatch.setattr(prep, "start_background_install",
                        lambda *a: order.append(("deps",)) or "no lockfile at the repo root; nothing to install")
    lines = prep.prep(repo, write_session_env=False, primary=True, deadline=time.monotonic() + 5)
    assert order == [("corepack", node_dir), ("deps",)]
    assert "pnpm/yarn not ready: offline" in lines


# ---------------------------------------------------------------- fallback Node

def test_fallback_major_is_used_everywhere(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".nvmrc").write_text("26\n")
    (repo / "package-lock.json").write_text("{}")
    path_node = _exe(tmp_path / "pathnode" / "node", "echo v22.11.0")
    monkeypatch.setenv("PATH", f"{path_node.parent}:/usr/bin:/bin")
    monkeypatch.setattr(prep, "OPT", tmp_path / "opt")

    def no_node(major, deadline):
        raise TimeoutError("ran out of the time budget")

    monkeypatch.setattr(prep, "install_node", no_node)
    started = []
    monkeypatch.setattr(prep, "start_background_install",
                        lambda r, major, node_bin, pin: started.append((major, node_bin)) or "installing")
    env_file = tmp_path / "env"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    lines = prep.prep(repo, write_session_env=True, primary=True, deadline=time.monotonic() + 5)
    assert lines[0] == f"Node 26 not available (ran out of the time budget); using Node 22 from {path_node.parent}"
    assert started == [(22, path_node.parent)]
    written = env_file.read_text()
    assert f'export PATH="{path_node.parent}:$PATH"' in written and "COREPACK_ENABLE_DOWNLOAD_PROMPT=0" in written


def test_fallback_prefers_the_newest_node_in_opt(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".nvmrc").write_text("20\n")
    opt = tmp_path / "opt"
    for major in (24, 26):
        _exe(opt / f"node{major}" / "bin" / "node", f"echo v{major}.1.0")
        (opt / f"node{major}" / prep.NODE_MARKER).write_text("x\n")
    monkeypatch.setattr(prep, "OPT", opt)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    def no_node(major, deadline):
        raise TimeoutError("ran out of the time budget")

    monkeypatch.setattr(prep, "install_node", no_node)
    monkeypatch.setattr(prep, "start_background_install", lambda *a: "installing")
    env_file = tmp_path / "env"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    lines = prep.prep(repo, write_session_env=True, primary=True, deadline=time.monotonic() + 5)
    assert lines[0] == f"Node 20 not available (ran out of the time budget); using Node 26 from {opt / 'node26' / 'bin'}"
    assert f'export PATH="{opt / "node26" / "bin"}:$PATH"' in env_file.read_text()


def test_an_unsatisfiable_pin_is_said_plainly(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".nvmrc").write_text("<18 >20\n")
    node_dir = tmp_path / "opt" / "node21"
    _exe(node_dir / "bin" / "node", "echo v21.7.0")
    monkeypatch.setattr(prep, "install_node", lambda major, deadline: node_dir)
    lines = prep.prep(repo, write_session_env=False, primary=True, deadline=time.monotonic() + 5)
    assert "does not satisfy the pin (from .nvmrc: <18 >20)" in lines[0]


def test_an_upper_bound_below_the_known_majors_picks_the_newest_lts_under_it():
    assert prep.pick_major("<18") == 16
    assert prep.pick_major("<=17") == 16


def test_only_the_primary_repo_sets_path(tmp_path, state, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    node_dir = tmp_path / "opt" / "node24"
    _exe(node_dir / "bin" / "node", "echo v24.16.0")
    monkeypatch.setattr(prep, "install_node", lambda major, deadline: node_dir)
    env_file = tmp_path / "env"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    lines = prep.prep(repo, write_session_env=True, primary=False, deadline=time.monotonic() + 5)
    assert not env_file.exists()
    assert "not on the session PATH" in lines[0]
    prep.prep(repo, write_session_env=True, primary=True, deadline=time.monotonic() + 5)
    assert env_file.read_text().count("export PATH=") == 1


def test_land_policy_lines():
    assert "decides at merge time" in prep.land_policy("wiggdevin/zs-land-sandbox")
    assert "stops for review" in prep.land_policy("wiggdevin/client-example")
    assert "not in the auto-merge allowlist" in prep.land_policy("someone/else")
