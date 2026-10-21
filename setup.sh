#!/bin/bash
# Claude cloud kit — provisions a Claude Code cloud session VM.
#
# Environment setup script (claude.ai/code → environment → Setup script), one line:
#   curl -fsSL https://raw.githubusercontent.com/ZeroSum-Solutions/claude-cloud-kit/main/setup.sh | bash || true
#
# Runs as root before Claude Code starts; the VM snapshot is cached ~7 days. Installs the
# kit into /opt/claude-cloud-kit, Node 24 and 26, `land` and `cloud-prep` on PATH, and the
# user-level ~/.claude/CLAUDE.md and settings.json. `setup.sh --refresh` (run by the
# SessionStart hook under `timeout 60`) refreshes the kit's code and config. Never fails
# the session.
#
# Integrity: MANIFEST.sha256 pins the SHA-256 of every file, so a download that mixes commits
# (raw.githubusercontent.com caches each file for minutes) is refused. A new kit replaces
# the current one only after it verifies, compiles and passes a smoke test; otherwise the
# current kit stays. This is code from a public repo running as root: `main` only changes
# through a reviewed PR with green CI (repo ruleset), and `land` never auto-merges this repo.
set -uo pipefail

RAW="${CLOUD_KIT_RAW:-https://raw.githubusercontent.com/ZeroSum-Solutions/claude-cloud-kit/main}"
KIT="${CLOUD_KIT_DIR:-/opt/claude-cloud-kit}"
BIN="${CLOUD_KIT_BIN:-/usr/local/bin}"
REQUIRED="setup.sh bin/land bin/cloud-prep hooks/guard.py lib/kitpolicy.py claude/CLAUDE.md claude/settings.json policy/client-repos.txt"
log() { echo "[cloud-kit] $*" >&2; }

LOCKED=0
STAGE=""

# One installer at a time: an flock on fd 9, which this shell keeps open. The kernel drops it
# when the holder exits, so a dead installer never leaves a stale lock and nothing has to
# guess. Python takes the lock (macOS has no flock binary); it belongs to the open file, so
# it stays with this shell after Python exits.
lock() {
  mkdir -p "$(dirname "$KIT")" && exec 9>>"$KIT.flock" || return 1
  if python3 - "${CLOUD_KIT_LOCK_WAIT:-30}" <<'PY'
import fcntl, sys, time
deadline = time.monotonic() + float(sys.argv[1])
while True:
    try:
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.2)
PY
  then
    LOCKED=1
    return 0
  fi
  exec 9>&-
  return 1
}
unlock() {
  [ "$LOCKED" = 1 ] || return 0
  LOCKED=0
  exec 9>&-
}

restore_kit() {  # an installer killed between the two moves leaves only $KIT.old
  if [ ! -d "$KIT" ] && [ -d "$KIT.old" ]; then
    mv "$KIT.old" "$KIT" && log "restored the kit after an interrupted install"
  fi
}

cleanup() {  # EXIT trap: never leave the kit missing, a staging dir behind, or the lock held
  [ "$LOCKED" = 1 ] || return 0
  restore_kit
  [ -n "$STAGE" ] && rm -rf "$STAGE"
  unlock
}

fetch_kit() {  # $1 = empty target dir; fetches every file in MANIFEST and verifies it
  local dir="$1"
  curl -fsSL --retry 2 --connect-timeout 5 --max-time 15 "$RAW/MANIFEST.sha256" -o "$dir/MANIFEST.sha256" || return 1
  # Build one curl config so all files download in parallel.
  python3 - "$dir" "$RAW" <<'PY' || return 1
import pathlib, re, sys
root, raw = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = [ln.strip() for ln in (root / "MANIFEST.sha256").read_text().splitlines()]
entries = [ln for ln in lines if ln and not ln.startswith("#")]
if not entries:
    sys.exit("empty MANIFEST.sha256")
conf = []
for ln in entries:
    m = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._/-]+)", ln)
    if not m or m.group(2).startswith("/") or ".." in m.group(2):
        sys.exit(f"bad MANIFEST.sha256 line: {ln}")
    (root / m.group(2)).parent.mkdir(parents=True, exist_ok=True)
    conf += [f'url = "{raw}/{m.group(2)}"', f'output = "{root / m.group(2)}"']
(root / ".curl").write_text("\n".join(conf) + "\n")
PY
  curl -fsSL --parallel --retry 2 --connect-timeout 5 --max-time 30 -K "$dir/.curl" || return 1
  rm -f "$dir/.curl"
}

verify_kit() {  # $1 = fetched kit; hashes, required files, syntax and a smoke test
  local dir="$1"
  python3 - "$dir" "$REQUIRED" <<'PY' || return 1
import hashlib, json, pathlib, sys
root, required = pathlib.Path(sys.argv[1]), sys.argv[2].split()
listed = {}
for ln in (root / "MANIFEST.sha256").read_text().splitlines():
    if ln.strip() and not ln.startswith("#"):
        digest, path = ln.split("  ", 1)
        listed[path] = digest
missing = [f for f in required if f not in listed]
if missing:
    sys.exit(f"MANIFEST.sha256 lacks {missing}")
for path, digest in listed.items():
    if hashlib.sha256((root / path).read_bytes()).hexdigest() != digest:
        sys.exit(f"{path} does not match MANIFEST.sha256 (mixed commits?)")
for p in [*root.glob("bin/*"), *root.glob("hooks/*.py"), *root.glob("lib/*.py")]:
    text = p.read_text(errors="ignore")
    if p.suffix == ".py" or text.startswith("#!/usr/bin/env python3"):
        compile(text, str(p), "exec")
settings = json.loads((root / "claude" / "settings.json").read_text())
if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
    sys.exit("claude/settings.json has no hooks")
PY
  bash -n "$dir/setup.sh" || return 1
  python3 -I "$dir/bin/land" --help >/dev/null || return 1
  python3 -I "$dir/bin/cloud-prep" --help >/dev/null || return 1
  python3 -I - "$dir" <<'PY' || return 1
import pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "lib"))
from kitpolicy import PolicyError, load_clients
try:
    load_clients(root / "policy" / "client-repos.txt")
except PolicyError as e:
    sys.exit(f"policy: {e}")
PY
  # The guard must allow a plain command and deny a push to main.
  python3 -I - "$dir/hooks/guard.py" <<'PY' || return 1
import json, os, subprocess, sys
env = dict(os.environ, CLAUDE_CODE_REMOTE="true")

def denied(command):
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/"})
    proc = subprocess.run([sys.executable, "-I", sys.argv[1]], input=event, capture_output=True,
                          text=True, env=env, timeout=20)
    if proc.returncode != 0:
        sys.exit(f"guard exited {proc.returncode} on {command!r}")
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        sys.exit(f"guard printed something that is not JSON on {command!r}")
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"

if denied("ls"):
    sys.exit("guard denies ls")
if not denied("git push origin main"):
    sys.exit("guard allows git push origin main")
PY
}

install_kit() {  # the caller holds the lock
  # Staging dirs left by an installer that was killed outright; only a lock holder makes them.
  rm -rf "$(dirname "$KIT")"/.claude-cloud-kit.??????
  STAGE="$(mktemp -d "$(dirname "$KIT")/.claude-cloud-kit.XXXXXX")" || { STAGE=""; return 1; }
  if ! fetch_kit "$STAGE"; then
    log "fetch failed; keeping the current kit"; rm -rf "$STAGE"; STAGE=""; return 1
  fi
  if ! verify_kit "$STAGE"; then
    log "new kit failed verification; keeping the current kit"; rm -rf "$STAGE"; STAGE=""; return 1
  fi
  chmod 755 "$STAGE"/bin/* "$STAGE"/hooks/*.py 2>/dev/null
  chmod -R a+rX "$STAGE"
  restore_kit
  rm -rf "$KIT.old"
  if [ -d "$KIT" ] && ! mv "$KIT" "$KIT.old"; then
    log "cannot move the current kit aside"; rm -rf "$STAGE"; STAGE=""; return 1
  fi
  # mv into an existing directory would nest the kit inside it (macOS mv has no -T).
  if [ -e "$KIT" ] || ! mv "$STAGE" "$KIT"; then
    log "cannot install the new kit; restoring the current one"
    restore_kit
    rm -rf "$STAGE"; STAGE=""; return 1
  fi
  STAGE=""
  rm -rf "$KIT.old"  # only now that the new kit is in place
}

link_bins() {
  mkdir -p "$BIN" && ln -sf "$KIT/bin/land" "$BIN/land" && ln -sf "$KIT/bin/cloud-prep" "$BIN/cloud-prep"
}

install_claude_config() {
  local rc=0
  mkdir -p "$HOME/.claude" || return 1
  if ! { cp "$KIT/claude/CLAUDE.md" "$HOME/.claude/CLAUDE.md.tmp" \
         && mv "$HOME/.claude/CLAUDE.md.tmp" "$HOME/.claude/CLAUDE.md"; }; then
    rm -f "$HOME/.claude/CLAUDE.md.tmp"
    log "could not write ~/.claude/CLAUDE.md"
    rc=1
  fi
  # Merge the kit's settings into any settings file already there: kit keys win, and the
  # kit's own hooks replace older kit hooks instead of piling up. Other hooks stay, even in
  # a group that also held a kit hook.
  python3 - "$KIT/claude/settings.json" "$HOME/.claude/settings.json" "$KIT" <<'PY' || rc=1
import json, os, pathlib, sys
kit = json.loads(pathlib.Path(sys.argv[1]).read_text())
dest = pathlib.Path(sys.argv[2])
markers = {"/opt/claude-cloud-kit/", sys.argv[3].rstrip("/") + "/"}
try:
    cur = json.loads(dest.read_text())
except (OSError, ValueError):
    cur = {}
if not isinstance(cur, dict):
    cur = {}

def is_kit_hook(hook):
    return isinstance(hook, dict) and any(m in str(hook.get("command", "")) for m in markers)

def without_kit_hooks(group):
    """The group minus its kit hooks; None when nothing else was in it."""
    hooks = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(hooks, list) or not any(is_kit_hook(h) for h in hooks):
        return group
    kept = [h for h in hooks if not is_kit_hook(h)]
    return {**group, "hooks": kept} if kept else None

for key, value in kit.items():
    if key == "hooks":
        hooks = cur.get("hooks") if isinstance(cur.get("hooks"), dict) else {}
        for event, groups in value.items():
            existing = hooks.get(event) if isinstance(hooks.get(event), list) else []
            kept = [g for g in map(without_kit_hooks, existing) if g is not None]
            hooks[event] = kept + groups
        cur["hooks"] = hooks
    elif isinstance(value, dict) and isinstance(cur.get(key), dict):
        cur[key].update(value)
    else:
        cur[key] = value
tmp = dest.with_name(dest.name + ".tmp")
tmp.write_text(json.dumps(cur, indent=2) + "\n")
os.replace(tmp, dest)
PY
  return $rc
}

if ! lock; then
  log "another kit install is running; skipping"
  exit 0
fi
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
restore_kit

if [ "${1:-}" = "--refresh" ]; then
  if install_kit; then
    link_bins || log "could not link land and cloud-prep into $BIN"
    install_claude_config || log "could not refresh ~/.claude config"
  fi
  exit 0
fi

if ! install_kit && [ ! -d "$KIT" ]; then
  log "kit not installed"; exit 0
fi
link_bins || log "could not link land and cloud-prep into $BIN"
install_claude_config || log "could not write ~/.claude config"
unlock  # the Node installs below take their own locks and can run for minutes
# Node 24 is the default for Devin's repos; 26 covers the newest pins. Both in parallel.
if [ "${CLOUD_KIT_SKIP_NODE:-}" != "1" ]; then
  python3 "$KIT/bin/cloud-prep" --install-node 24 >/dev/null 2>&1 &
  python3 "$KIT/bin/cloud-prep" --install-node 26 >/dev/null 2>&1 &
  wait
fi
log "ready: $(ls -d /opt/node* 2>/dev/null | tr '\n' ' ')"
exit 0
