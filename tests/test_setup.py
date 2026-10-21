import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def run_setup(tmp_path, *args, home=None, raw=None, lock_wait="30"):
    env = dict(os.environ, PATH=PATH, CLOUD_KIT_LOCK_WAIT=lock_wait,
               CLOUD_KIT_RAW=raw or f"file://{ROOT}",
               CLOUD_KIT_DIR=str(tmp_path / "opt" / "claude-cloud-kit"),
               CLOUD_KIT_BIN=str(tmp_path / "bin"),
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


def test_refresh_refuses_a_kit_whose_guard_fails(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    raw = copy_tree(tmp_path / "raw")
    (raw / "hooks" / "guard.py").write_text("import sys\nsys.exit(1)\n")
    _rehash(raw)
    proc = run_setup(tmp_path, "--refresh", raw=f"file://{raw}")
    assert proc.returncode == 0
    assert "failed verification" in proc.stderr
    assert "sys.exit(1)" not in (tmp_path / "opt" / "claude-cloud-kit" / "hooks" / "guard.py").read_text()


def test_a_held_lock_skips_the_run(tmp_path):
    assert run_setup(tmp_path).returncode == 0
    (tmp_path / "opt" / "claude-cloud-kit.lock").mkdir()
    proc = run_setup(tmp_path, "--refresh", lock_wait="1")
    assert proc.returncode == 0
    assert "another kit install is running" in proc.stderr
