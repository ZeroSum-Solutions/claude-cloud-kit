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

lock() {  # one installer at a time; a lock older than 5 minutes is stale
  local i
  for i in $(seq 1 "${CLOUD_KIT_LOCK_WAIT:-30}"); do
    mkdir "$KIT.lock" 2>/dev/null && return 0
    if [ -n "$(find "$KIT.lock" -maxdepth 0 -mmin +5 2>/dev/null)" ]; then
      rmdir "$KIT.lock" 2>/dev/null
      continue
    fi
    sleep 1
  done
  return 1
}
unlock() { rmdir "$KIT.lock" 2>/dev/null; }

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
  echo '{"tool_name":"Bash","tool_input":{"command":"ls"},"cwd":"/"}' \
    | CLAUDE_CODE_REMOTE=true python3 -I "$dir/hooks/guard.py" >/dev/null || return 1
}

install_kit() {
  local tmp
  tmp="$(mktemp -d "$(dirname "$KIT")/.claude-cloud-kit.XXXXXX")" || return 1
  if ! fetch_kit "$tmp"; then
    log "fetch failed; keeping the current kit"; rm -rf "$tmp"; return 1
  fi
  if ! verify_kit "$tmp"; then
    log "new kit failed verification; keeping the current kit"; rm -rf "$tmp"; return 1
  fi
  chmod 755 "$tmp"/bin/* "$tmp"/hooks/*.py 2>/dev/null
  chmod -R a+rX "$tmp"
  rm -rf "$KIT.old"
  if [ -d "$KIT" ] && ! mv "$KIT" "$KIT.old"; then
    log "cannot move the current kit aside"; rm -rf "$tmp"; return 1
  fi
  if ! mv "$tmp" "$KIT"; then
    log "cannot install the new kit; restoring the current one"
    [ -d "$KIT.old" ] && mv "$KIT.old" "$KIT"
    rm -rf "$tmp"; return 1
  fi
  rm -rf "$KIT.old"
  ln -sf "$KIT/bin/land" "$BIN/land" && ln -sf "$KIT/bin/cloud-prep" "$BIN/cloud-prep"
}

install_claude_config() {
  mkdir -p "$HOME/.claude" || return 1
  cp "$KIT/claude/CLAUDE.md" "$HOME/.claude/CLAUDE.md.tmp" && mv "$HOME/.claude/CLAUDE.md.tmp" "$HOME/.claude/CLAUDE.md"
  # Merge the kit's settings into any settings file already there: kit keys win, and the
  # kit's own hook groups replace older kit groups instead of piling up.
  python3 - "$KIT/claude/settings.json" "$HOME/.claude/settings.json" "$KIT" <<'PY'
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

def is_kit_group(group):
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(
        m in str(h.get("command", "")) for h in hooks if isinstance(h, dict) for m in markers)

for key, value in kit.items():
    if key == "hooks":
        hooks = cur.get("hooks") if isinstance(cur.get("hooks"), dict) else {}
        for event, groups in value.items():
            existing = hooks.get(event) if isinstance(hooks.get(event), list) else []
            hooks[event] = [g for g in existing if not is_kit_group(g)] + groups
        cur["hooks"] = hooks
    elif isinstance(value, dict) and isinstance(cur.get(key), dict):
        cur[key].update(value)
    else:
        cur[key] = value
tmp = dest.with_name(dest.name + ".tmp")
tmp.write_text(json.dumps(cur, indent=2) + "\n")
os.replace(tmp, dest)
PY
}

if ! lock; then
  log "another kit install is running; skipping"
  exit 0
fi
trap unlock EXIT

if [ "${1:-}" = "--refresh" ]; then
  install_kit && { install_claude_config || log "could not refresh ~/.claude config"; }
  exit 0
fi

if ! install_kit && [ ! -d "$KIT" ]; then
  log "kit not installed"; exit 0
fi
install_claude_config || log "could not write ~/.claude config"
# Node 24 is the default for Devin's repos; 26 covers the newest pins. Both in parallel.
if [ "${CLOUD_KIT_SKIP_NODE:-}" != "1" ]; then
  python3 "$KIT/bin/cloud-prep" --install-node 24 >/dev/null 2>&1 &
  python3 "$KIT/bin/cloud-prep" --install-node 26 >/dev/null 2>&1 &
  wait
fi
log "ready: $(ls -d /opt/node* 2>/dev/null | tr '\n' ' ')"
exit 0
