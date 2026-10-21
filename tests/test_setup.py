import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# The real interpreter first: a version-manager shim may refuse to run under a temp HOME.
PATH = f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"

_loader = importlib.machinery.SourceFileLoader("write_manifest", str(ROOT / "scripts" / "write-manifest"))
_spec = importlib.util.spec_from_loader("write_manifest", _loader)
write_manifest = importlib.util.module_from_spec(_spec)
_loader.exec_module(write_manifest)


def manifest():
    return [ln.strip() for ln in (ROOT / "MANIFEST").read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def test_manifests_are_current():
    plain, hashed = write_manifest.render()
    assert (ROOT / "MANIFEST").read_text() == plain, "run scripts/write-manifest"
    assert (ROOT / "MANIFEST.sha256").read_text() == hashed, "run scripts/write-manifest"


def run_setup(tmp_path, *args, home=None, raw=None, lock_wait="30", bin_dir=None, path=PATH):
    env = dict(os.environ, PATH=path, CLOUD_KIT_LOCK_WAIT=lock_wait,
               CLOUD_KIT_RAW=raw or f"file://{ROOT}",
               CLOUD_KIT_DIR=str(tmp_path / "opt" / "claude-cloud-kit"),
               CLOUD_KIT_BIN=str(bin_dir or tmp_path / "bin"),
               CLOUD_KIT_SKIP_NODE="1",
               HOME=str(home or tmp_path / "home"))
    (tmp_path / "opt").mkdir(exist_ok=True)
    (tmp_path / "bin").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(ROOT / "setup.sh"), *args], env=env, capture_output=True, text=True)


def copy_tree(dest: Path) -> Path:
    """A writable copy of the shipped files, to serve as a fake raw URL."""
    for f in [*manifest(), "MANIFEST", "MANIFEST.sha256"]:
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, dest / f)
    return dest


def test_setup_installs_kit_and_merges_settings(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
    proc = run_setup(tmp_path, home=home)
    assert proc.returncode == 0, proc.stderr
    kit = tmp_path / "opt" / "claude-cloud-kit"
    for f in manifest():
        assert (kit / f).is_file(), f
    assert os.access(kit / "bin" / "land", os.X_OK)
    assert (tmp_path / "bin" / "land").resolve() == (kit / "bin" / "land").resolve()
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["model"] == "x"
    assert set(settings["hooks"]) == {"Stop", "SessionStart", "PreToolUse"}
    assert settings["attribution"]["sessionUrl"] is False
    assert (home / ".claude" / "CLAUDE.md").read_text().startswith("# Cloud session rules")
    # running again does not duplicate hooks
    assert run_setup(tmp_path, home=home).returncode == 0
    again = json.loads((home / ".claude" / "settings.json").read_text())
    assert len(again["hooks"]["PreToolUse"]) == 1
    assert len(again["hooks"]["Stop"]) == 1


def test_changed_kit_hook_replaces_the_old_one(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    kit = str(tmp_path / "opt" / "claude-cloud-kit")
    old = {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {kit}/hooks/guard.py", "timeout": 5}]}
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [old]}}))
    # the shipped settings name /opt/claude-cloud-kit; point them at the test kit dir
    raw = copy_tree(tmp_path / "raw")
    s = (raw / "claude" / "settings.json").read_text().replace("/opt/claude-cloud-kit", kit)
    (raw / "claude" / "settings.json").write_text(s)
    _rehash(raw)
    assert run_setup(tmp_path, home=home, raw=f"file://{raw}").returncode == 0
    groups = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    assert len(groups) == 1 and groups[0]["hooks"][0]["timeout"] == 10


def _rehash(raw: Path) -> None:
    import hashlib
    lines = []
    for ln in (raw / "MANIFEST.sha256").read_text().splitlines():
        if ln.startswith("#") or not ln.strip():
            lines.append(ln)
            continue
        path = ln.split("  ", 1)[1]
        lines.append(f"{hashlib.sha256((raw / path).read_bytes()).hexdigest()}  {path}")
    (raw / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")


def test_settings_that_are_not_an_object_are_replaced(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("[]")
    assert run_setup(tmp_path, home=home).returncode == 0
    assert "PreToolUse" in json.loads((home / ".claude" / "settings.json").read_text())["hooks"]


def test_refresh_keeps_kit_when_fetch_fails(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    proc = run_setup(tmp_path, "--refresh", raw="file:///nonexistent")
    assert proc.returncode == 0
    assert (tmp_path / "opt" / "claude-cloud-kit" / "bin" / "land").is_file()


def test_refresh_refuses_files_that_do_not_match_the_hashes(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    raw = copy_tree(tmp_path / "raw")
    (raw / "bin" / "land").write_text("#!/usr/bin/env python3\nprint('tampered')\n")
    proc = run_setup(tmp_path, "--refresh", raw=f"file://{raw}")
    assert proc.returncode == 0
    assert "does not match" in proc.stderr
    assert "tampered" not in (tmp_path / "opt" / "claude-cloud-kit" / "bin" / "land").read_text()


def test_refresh_refuses_an_empty_manifest(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    raw = copy_tree(tmp_path / "raw")
    (raw / "MANIFEST.sha256").write_text("# nothing\n")
    proc = run_setup(tmp_path, "--refresh", raw=f"file://{raw}")
    assert proc.returncode == 0
    assert (tmp_path / "opt" / "claude-cloud-kit" / "bin" / "land").is_file()


@pytest.mark.parametrize("guard", [
    "import sys\nsys.stdin.read()\n",  # allows everything
    'import json, sys\nsys.stdin.read()\nprint(json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}}))\n',
], ids=["allows-a-push-to-main", "denies-ls"])
def test_refresh_refuses_a_guard_that_decides_wrongly(tmp_path, guard):
    assert run_setup(tmp_path).returncode == 0
    raw = copy_tree(tmp_path / "raw")
    (raw / "hooks" / "guard.py").write_text(guard)
    _rehash(raw)
    proc = run_setup(tmp_path, "--refresh", raw=f"file://{raw}")
    assert proc.returncode == 0
    assert "failed verification" in proc.stderr
    assert (tmp_path / "opt" / "claude-cloud-kit" / "hooks" / "guard.py").read_text() == (ROOT / "hooks" / "guard.py").read_text()


def test_refresh_refuses_a_kit_whose_guard_fails(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    raw = copy_tree(tmp_path / "raw")
    (raw / "hooks" / "guard.py").write_text("import sys\nsys.exit(1)\n")
    _rehash(raw)
    proc = run_setup(tmp_path, "--refresh", raw=f"file://{raw}")
    assert proc.returncode == 0
    assert "failed verification" in proc.stderr
    assert "sys.exit(1)" not in (tmp_path / "opt" / "claude-cloud-kit" / "hooks" / "guard.py").read_text()


HOLD = """
import fcntl, sys, time
f = open(sys.argv[1], "a")
fcntl.flock(f, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(60)
"""


def hold_lock(tmp_path):
    """Another installer, alive and holding the kit lock."""
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(tmp_path / "opt" / "claude-cloud-kit.flock")],
                              stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline() == "held\n"
    return holder


def lock_is_free(tmp_path) -> bool:
    import fcntl
    with open(tmp_path / "opt" / "claude-cloud-kit.flock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def test_a_held_lock_skips_the_run_and_touches_nothing(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    opt = tmp_path / "opt"
    (opt / "claude-cloud-kit").rename(opt / "claude-cloud-kit.old")  # the holder is mid-swap
    (opt / ".claude-cloud-kit.abcdef").mkdir()  # and staging
    holder = hold_lock(tmp_path)
    try:
        proc = run_setup(tmp_path, "--refresh", lock_wait="1")
    finally:
        holder.kill()
        holder.wait()
    assert proc.returncode == 0
    assert "another kit install is running" in proc.stderr
    assert (opt / "claude-cloud-kit.old").is_dir() and not (opt / "claude-cloud-kit").exists()
    assert (opt / ".claude-cloud-kit.abcdef").is_dir()


def test_a_dead_holder_frees_the_lock_at_once(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    holder = hold_lock(tmp_path)
    holder.kill()
    holder.wait()
    proc = run_setup(tmp_path, "--refresh", lock_wait="1")
    assert proc.returncode == 0 and "another kit install" not in proc.stderr
    assert lock_is_free(tmp_path)


def test_node_installs_do_not_hold_the_kit_lock(tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    # stands in for python3 running `cloud-prep --install-node`: fails if fd 9 is still open
    (fake / "python3").write_text(f"""#!/bin/sh
case "$2" in --install-node) if [ -e /dev/fd/9 ]; then echo leaked >>"{tmp_path}/fd9"; fi; exit 0;; esac
exec "{sys.executable}" "$@"
""")
    (fake / "python3").chmod(0o755)
    env_path = f"{fake}:{PATH}"
    env = dict(os.environ, PATH=env_path, CLOUD_KIT_RAW=f"file://{ROOT}",
               CLOUD_KIT_DIR=str(tmp_path / "opt" / "claude-cloud-kit"), CLOUD_KIT_BIN=str(tmp_path / "bin"),
               HOME=str(tmp_path / "home"))
    (tmp_path / "opt").mkdir()
    (tmp_path / "bin").mkdir()
    proc = subprocess.run(["bash", str(ROOT / "setup.sh")], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "fd9").exists()


def test_a_kit_left_only_as_old_is_restored(tmp_path):
    """An installer killed between `mv $KIT $KIT.old` and `mv $STAGE $KIT`."""
    assert run_setup(tmp_path).returncode == 0
    kit = tmp_path / "opt" / "claude-cloud-kit"
    kit.rename(tmp_path / "opt" / "claude-cloud-kit.old")
    proc = run_setup(tmp_path, "--refresh", raw="file:///nonexistent")
    assert proc.returncode == 0 and "restored the kit" in proc.stderr
    assert (kit / "bin" / "land").is_file()
    assert not (tmp_path / "opt" / "claude-cloud-kit.old").exists()
    # and a full run installs over a leftover .old
    kit.rename(tmp_path / "opt" / "claude-cloud-kit.old")
    assert run_setup(tmp_path).returncode == 0
    assert (kit / "bin" / "land").is_file() and not (tmp_path / "opt" / "claude-cloud-kit.old").exists()


def test_a_killed_refresh_cleans_up(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "curl").write_text("#!/bin/sh\nkill -TERM $PPID\nexit 1\n")
    (fake / "curl").chmod(0o755)
    proc = run_setup(tmp_path, "--refresh", path=f"{fake}:{PATH}")
    assert proc.returncode != 0
    opt = tmp_path / "opt"
    assert (opt / "claude-cloud-kit" / "bin" / "land").is_file()
    assert not list(opt.glob(".claude-cloud-kit.*")) and lock_is_free(tmp_path)


def test_user_hooks_in_a_kit_group_are_kept(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    mixed = {"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 /opt/claude-cloud-kit/hooks/guard.py", "timeout": 5},
        {"type": "command", "command": "my-own-check"}]}
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [mixed]}}))
    assert run_setup(tmp_path, home=home).returncode == 0
    groups = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert commands.count("my-own-check") == 1
    assert commands.count("python3 /opt/claude-cloud-kit/hooks/guard.py") == 1  # the kit's, timeout 10
    assert all(g["hooks"] for g in groups)


def test_refresh_refuses_a_kit_whose_policy_is_empty(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    raw = copy_tree(tmp_path / "raw")
    (raw / "policy" / "client-repos.txt").write_text("# nothing\n")
    _rehash(raw)
    proc = run_setup(tmp_path, "--refresh", raw=f"file://{raw}")
    assert "failed verification" in proc.stderr and "no entries" in proc.stderr
    assert "# nothing" not in (tmp_path / "opt" / "claude-cloud-kit" / "policy" / "client-repos.txt").read_text()


def test_refresh_refuses_a_kit_whose_cloud_prep_fails(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    raw = copy_tree(tmp_path / "raw")
    (raw / "bin" / "cloud-prep").write_text("#!/usr/bin/env python3\nimport no_such_module\n")
    _rehash(raw)
    proc = run_setup(tmp_path, "--refresh", raw=f"file://{raw}")
    assert "failed verification" in proc.stderr
    assert "no_such_module" not in (tmp_path / "opt" / "claude-cloud-kit" / "bin" / "cloud-prep").read_text()


def test_missing_bin_dir_is_created_and_link_failure_keeps_config(tmp_path):
    bin_dir = tmp_path / "nested" / "bin"
    assert run_setup(tmp_path, bin_dir=bin_dir).returncode == 0
    assert (bin_dir / "land").resolve() == (tmp_path / "opt" / "claude-cloud-kit" / "bin" / "land").resolve()
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a dir")
    home = tmp_path / "home2"
    proc = run_setup(tmp_path, "--refresh", home=home, bin_dir=blocked / "bin")
    assert "could not link" in proc.stderr
    assert (home / ".claude" / "settings.json").is_file()


def test_failed_claude_md_copy_is_reported(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md.tmp").symlink_to(tmp_path / "missing-dir" / "x")
    proc = run_setup(tmp_path, home=home)
    assert "could not write ~/.claude/CLAUDE.md" in proc.stderr
    assert "could not write ~/.claude config" in proc.stderr
    assert (home / ".claude" / "settings.json").is_file()  # the merge still ran
