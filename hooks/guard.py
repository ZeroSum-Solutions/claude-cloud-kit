#!/usr/bin/env python3
"""PreToolUse guard for Bash in Claude Code cloud sessions: a seatbelt, not a sandbox.

  - git push runs only as its own command:
      [cd <path> &&] git push [-u|--set-upstream] [-q|--quiet] [--force-with-lease[=<ref>]]
                              [--force-if-includes] [<remote> [<refspec>]]
    to a branch other than main, master or the remote's default branch. Any other command
    with the word `push` next to git, hub, a shell expansion, a here-document or a nested
    shell is refused, prose in a commit message included. git aliases and unknown git
    subcommands are refused too.
  - GitHub: merges, auto-merge, repo delete/archive/rename/edit/sync, rulesets and branch
    protection, and API writes to contents, git refs and objects, hooks, Actions settings and
    environments. Other GitHub calls pass; the Claude GitHub App has no administration write,
    so GitHub itself refuses settings changes.
  - Databases: a remote database host next to a database or migration tool, remote supabase
    commands, Supabase Management API database calls, and database env vars set to a remote
    value.

The GitHub and database rules match the raw command text, here-documents and substitutions
included, so a command that only mentions one of these is refused too, and the message says
how to split it. GitHub rulesets on the default branch are the hard gate; `land` is the CI
gate. Outside a cloud session (CLAUDE_CODE_REMOTE != true) the guard allows everything.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = {"main", "master"}
LAND_HINT = "Push a feature branch and run `land` (it waits for green CI, then squash-merges)."
PUSH_HINT = ("Push as its own command: `git push -u origin HEAD` (add --force-with-lease to force). "
             "If this was not a push, split it so `push` is not in a git command.")
SPLIT = ("If the command only writes text that mentions this, write the file in one command, run the tool "
         "in another (or write the text with the Write tool).")
MERGE_HINT = f"Merging by hand skips the CI gate. {LAND_HINT} {SPLIT}"
RULES_HINT = f"Repository settings, branch protection and rulesets are changed by Devin, not from cloud sessions. {SPLIT}"
API_HINT = ("API writes to contents, git refs and objects, hooks, Actions settings and environments are not "
            f"made from cloud sessions. {LAND_HINT} {SPLIT}")
DB_HINT = ("Production database changes are not made from cloud sessions. Commit the migration; it is "
           f"applied from Devin's machine with a backup first. Local runs take --local. {SPLIT}")
ALIAS_HINT = "git aliases and unknown git subcommands are refused in cloud sessions; run the underlying git command."
UNREADABLE = "Could not read this command safely. Split it into simple commands without nested quoting."

_PUSH_WORD = re.compile(r"\b(?:push|send-pack)\b")
_GIT_WORD = re.compile(r"\b(?:git|hub)\b")
_HIDDEN = re.compile(r"[$`]|<<|<\(|>\(|\b(?:eval|xargs|bash|sh|zsh|dash|ksh)\b")
_RISKY = re.compile(r"\b(?:git|hub|gh|curl|wget|http|https|psql|pg_dump|pg_restore|pgcli|supabase|"
                    r"postgres|postgresql|PGHOST|DATABASE_URL)\b")


# ---------------------------------------------------------------- git state


def _git(where: str | None, *args: str, timeout: float = 3) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, cwd=where or None, timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def current_branch(where: str | None) -> str | None:
    out = _git(where, "rev-parse", "--abbrev-ref", "HEAD")
    return out if out and out != "HEAD" else None


def default_branch(where: str | None, remote: str = "origin") -> str | None:
    """The remote's default branch: origin/HEAD if set, else ask the remote (5 s)."""
    if re.fullmatch(r"[\w.-]+", remote):
        out = _git(where, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        if out and out.startswith(remote + "/"):
            return out[len(remote) + 1:]
    out = _git(where, "ls-remote", "--symref", remote, "HEAD", timeout=5)
    m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD", out or "", re.MULTILINE)
    return m.group(1) if m else None


def push_target(where: str | None) -> str | None:
    """Where a bare `git push` sends the current branch (honors push.default=upstream)."""
    out = _git(where, "rev-parse", "--abbrev-ref", "@{push}")
    if not out:
        return None
    for remote in (_git(where, "remote") or "").split():
        if out.startswith(remote + "/"):
            return out[len(remote) + 1:]
    return out


def push_config(where: str | None) -> str:
    return _git(where, "config", "--get-regexp", r"^(push\.default|remote\..*\.(push|mirror))$") or ""


# ---------------------------------------------------------------- git push


_PUSH_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "--force-with-lease", "--force-if-includes"}
_BRANCH = re.compile(r"(?!(?:refs|heads|remotes|tags)/)[A-Za-z0-9_][\w./-]*")


def _is_branch(name: str) -> bool:
    return bool(_BRANCH.fullmatch(name)) and name != "HEAD" and not re.search(r"\.\.|//|/\.|\.lock$|[/.]$", name)


def push_form(command: str) -> tuple[str | None, list[str]] | None:
    """(cd path or None, push arguments) when the whole command is one plain `git push`."""
    command = command.strip()
    if re.search(r"[$`\n]|\S#", command):
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return None
    cd = None
    if len(words) > 3 and words[0] == "cd" and words[2] == "&&":
        cd, words = words[1], words[3:]
    if words[:2] != ["git", "push"] or any(re.fullmatch(r"[();<>|&]+", w) for w in words):
        return None
    return cd, words[2:]


def check_push(args: list[str], where: str | None) -> str | None:
    positional = []
    for a in args:
        if not a.startswith("-"):
            positional.append(a)
        elif a not in _PUSH_FLAGS and not a.startswith("--force-with-lease="):
            return f"`git push {a}` is not allowed in cloud sessions. {PUSH_HINT}"
    if len(positional) > 2:
        return f"Push one branch at a time. {PUSH_HINT}"
    remote, spec = (positional + [None, None])[:2]
    if remote is not None and not re.fullmatch(r"[A-Za-z0-9_][\w.-]*", remote):
        return f"Push to a named remote such as `origin`, not `{remote}`."
    config = push_config(where)
    if re.search(r"^remote\.\S*\.mirror (?:true|1|yes|on)$", config, re.MULTILINE | re.IGNORECASE):
        return f"This remote is configured as a mirror; a push would update the default branch. {LAND_HINT}"
    branch = current_branch(where)
    unknown = ("Could not tell which branch this push updates (detached HEAD or no repository here). "
               "Name it: `git push -u origin <branch>`.")
    configured = re.search(r"^remote\.\S*\.push ", config, re.MULTILINE)
    if spec is None:
        if configured or re.search(r"^push\.default matching$", config, re.MULTILINE | re.IGNORECASE):
            return "This repo's push config can send a bare push elsewhere. Push `HEAD:<branch>` explicitly."
        if branch is None:
            return unknown
        targets = {branch, push_target(where) or branch}
    else:
        src, colon, dst = spec.partition(":")
        if src == "HEAD" and branch is None:
            return unknown
        if not (src == "HEAD" or _is_branch(src)):
            return f"Push refspec `{spec}` is not allowed. Use HEAD, <branch> or HEAD:<branch>."
        if colon:
            dst = dst[len("refs/heads/"):] if src == "HEAD" and dst.startswith("refs/heads/") else dst
            if not _is_branch(dst):
                return f"Push refspec `{spec}` is not allowed. Use HEAD, <branch> or HEAD:<branch>."
            targets = {dst}
        elif configured:
            return f"A configured remote.*.push can redirect `{spec}`. Push `HEAD:<branch>` explicitly."
        else:
            targets = {branch if src == "HEAD" else src}
    protected = {name.lower() for name in PROTECTED | {default_branch(where, remote or "origin") or "main"}}
    for name in targets:
        if name.lower() in protected:
            return f"Pushing to `{name}` is blocked in cloud sessions. {LAND_HINT}"
    return None


def could_push(command: str) -> bool:
    for text in (command, re.sub(r"[\\'\"]", "", command)):  # "gi""t" and g\it spell git
        text = re.sub(r"\bstash\s+push\b", "stash", text)
        if _PUSH_WORD.search(text) and (_GIT_WORD.search(text) or _HIDDEN.search(text)):
            return True
    return False


# ---------------------------------------------------------------- git subcommands


GIT_BUILTINS = set("""
add am annotate apply archimport archive backfill bisect blame branch bugreport bundle cat-file check-attr
check-ignore check-mailmap check-ref-format checkout checkout-index cherry cherry-pick citool clean clone
column commit commit-graph commit-tree config count-objects credential credential-cache credential-store
cvsexportcommit cvsimport cvsserver daemon describe diagnose diff diff-files diff-index diff-pairs diff-tree
difftool fast-export fast-import fetch fetch-pack filter-branch fmt-merge-msg for-each-ref for-each-repo
format-patch fsck fsck-objects gc get-tar-commit-id grep gui hash-object help history hook http-backend
http-fetch http-push imap-send index-pack init init-db instaweb interpret-trailers last-modified lfs log
ls-files ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base merge-file merge-index
merge-one-file merge-tree mergetool mktag mktree multi-pack-index mv name-rev notes p4 pack-objects
pack-redundant pack-refs patch-id prune prune-packed pull push quiltimport range-diff read-tree rebase
receive-pack reflog refs remote repack replace replay repo request-pull rerere reset restore rev-list
rev-parse revert rm send-email send-pack shell shortlog show show-branch show-index show-ref sparse-checkout
stage stash status stripspace submodule subtree switch symbolic-ref tag unpack-file unpack-objects
update-index update-ref update-server-info upload-archive upload-pack var verify-commit verify-pack
verify-tag version whatchanged worktree write-tree
""".split())
# `git` in command position (after a separator, a quote, assignments or a wrapper), its global
# options, then the subcommand.
_GIT_CMD = re.compile(
    r"(?:^|[;&|(){}`!\n'\"]|\$\()\s*"
    r"(?:(?:[A-Za-z_]\w*=\S*|env|sudo|nohup|nice|time|timeout|command|exec|xargs|then|do|else|-\S+|\d\S*)\s+)*"
    r"(?:\S*/)?git\s+"
    r"(?:(?:-C|-c|--git-dir|--work-tree|--namespace|--exec-path|--config-env)\s+\S+\s+|-\S+\s+)*"
    r"([^\s;&|()<>-][^\s;&|()<>]*)")


def check_git(command: str) -> str | None:
    for m in _GIT_CMD.finditer(command):
        sub = re.sub(r"[\\'\"]", "", m.group(1))
        if sub and sub not in GIT_BUILTINS:
            return f"`git {sub}`: {ALIAS_HINT}"
    return None


# ---------------------------------------------------------------- GitHub


_MERGE = re.compile(r"\bpulls/\S*/merge\b|/merges\b|/merge-upstream\b|\bpr\s+merge\b|\bauto-merge\b"
                    r"|enablePullRequestAutoMerge|mergePullRequest")
_GH_VARIABLE = re.compile(r"\b(?:gh|hub)\s+(?:(?:pr|repo)\s+)?[\"']?[$`]")  # gh pr $A
_SETTINGS = re.compile(r"\brepo\s+(?:delete|archive|rename|edit|sync)\b|\bruleset|/protection\b")
_GITHUB_API = re.compile(r"api\.github\.com|\b(?:gh|hub)\b.*\bapi\b", re.IGNORECASE)
_GITHUB_WRITE_PATH = re.compile(r"/contents/|/git/(?:refs|commits|trees|blobs|tags)\b|/hooks\b"
                                r"|/actions/(?:secrets|variables|permissions)\b|/environments/")
_WRITE = re.compile(r"(?<![\w-])(?:-X|-d|-T|--method|--request|--data[\w-]*|--field|--raw-field|--input"
                    r"|--upload-file|--json|--form[\w-]*|--post-data|--post-file|--body-data|--body-file)"
                    r"|(?<![\w-])-[fF](?=\s|\w+=)|\b(?:POST|PUT|PATCH|DELETE)\b")


def check_github(command: str) -> str | None:
    text = re.sub(r"%2f", "/", command, flags=re.IGNORECASE)
    text = re.sub(r"/(?:\.?/)+", "/", text)  # repos/o/r/./pulls, repos//o
    if _MERGE.search(text):
        return MERGE_HINT
    if _GH_VARIABLE.search(text):
        return f"A gh subcommand held in a variable can't be checked; spell it out. {LAND_HINT}"
    if _SETTINGS.search(text):
        return RULES_HINT
    if _GITHUB_API.search(text) and _GITHUB_WRITE_PATH.search(text) and _WRITE.search(text):
        return API_HINT
    return None


# ---------------------------------------------------------------- databases


_REMOTE_HOST = re.compile(r"\.supabase\.co\b|pooler\.supabase\.com|\.rds\.amazonaws\.com|\.neon\.tech|\.render\.com"
                          r"|\.railway\.app|\.aivencloud\.com|\.cockroachlabs\.cloud", re.IGNORECASE)
_DB_TOOL = re.compile(r"(?<![\w.-])(?:psql|pg_dump|pg_dumpall|pg_restore|pgcli|usql|supabase|prisma|drizzle-kit"
                      r"|knex|sqitch|flyway|dbmate|migrate)(?![\w-])")
_PG_CLIENT = re.compile(r"(?<![\w.-])(?:psql|pg_dump|pg_dumpall|pg_restore|pgcli|usql)(?![\w-])")
_PG_HOST = re.compile(r"postgres(?:ql)?://(?:[^@/\s'\"]*@)?([^/?\s'\"]*)|(?:host|hostaddr)\s*=\s*['\"]?([^\s'\";&]*)"
                      r"|(?<![\w-])(?:-h|--host)(?:\s+|=)?['\"]?([^\s'\"]*)")
_LOCAL = re.compile(r"|/.*|%2f.*|(?:localhost|127\.0\.0\.1|::1|\[::1\])(?::\d+)?", re.IGNORECASE)
_DB_ENV = re.compile(r"\b(PGHOST|PGHOSTADDR|PGSERVICE|DATABASE_URL|SUPABASE_DB_URL|POSTGRES_URL)=['\"]?([^\s'\"]*)")
_LOCAL_VALUE = re.compile(r"|/.*|(?:[\w+]+://(?:[^@/\s]*@)?)?(?:localhost|127\.0\.0\.1|\[::1\])(?:[:/?].*)?",
                          re.IGNORECASE)
_SUPABASE = re.compile(r"(?<![\w.-])supabase(?![\w-])")
_SUPABASE_REMOTE = re.compile(r"--linked\b|--db-url\b|--project-ref\b|\bpush\b|\blink\b|[$`]")
_SUPABASE_UNLESS_LOCAL = re.compile(r"\bmigrations?\s+(?:up|repair|squash|fetch)\b|\bdb\s+(?:dump|pull)\b|\binspect\b")


def check_db(command: str) -> str | None:
    if _REMOTE_HOST.search(command) and _DB_TOOL.search(command):
        return DB_HINT
    for name, value in _DB_ENV.findall(command):
        if name == "PGSERVICE" or "$" in value or not _LOCAL_VALUE.fullmatch(value):
            return f"`{name}` must name a local database here. {DB_HINT}"
    if _SUPABASE.search(command):
        if _SUPABASE_REMOTE.search(command) or (
                _SUPABASE_UNLESS_LOCAL.search(command) and not re.search(r"--local\b", command)):
            return DB_HINT
    if re.search(r"api\.supabase\.com", command, re.IGNORECASE) and (
            re.search(r"/database/(?:query|migrations)", command) or _WRITE.search(command)):
        return DB_HINT
    if re.search(r"\.supabase\.co\b", command, re.IGNORECASE) and _WRITE.search(command):
        return DB_HINT
    if _PG_CLIENT.search(command):
        if re.search(r"[$`]|\bservice\s*=", command):
            return f"A database target held in a variable or pg_service entry can't be checked. {DB_HINT}"
        hosts = ["".join(g or "" for g in m.groups()) for m in _PG_HOST.finditer(command)]
        if not hosts:
            if os.environ.get("PGSERVICE"):
                return f"PGSERVICE is set. {DB_HINT}"
            hosts = [os.environ.get("PGHOST", ""), os.environ.get("PGHOSTADDR", "")]
        if not all(_LOCAL.fullmatch(h.strip()) for host in hosts for h in host.split(",")):
            return f"Connections to a remote database are not made from cloud sessions. {DB_HINT}"
    return None


# ---------------------------------------------------------------- decision


def _decide(command: str, cwd: str | None) -> str | None:
    form = push_form(command)
    if form is not None:
        cd, args = form
        if cd is not None:
            if cd.startswith("-"):
                return PUSH_HINT
            cwd = os.path.join(cwd or os.getcwd(), os.path.expanduser(cd))
        return check_push(args, cwd)
    if could_push(command):
        return PUSH_HINT
    return check_git(command) or check_github(command) or check_db(command)


def decide(command: str, cwd: str | None) -> str | None:
    try:
        return _decide(command, cwd)
    except Exception:  # odd input must not switch the guard off for risky commands
        return UNREADABLE if _RISKY.search(command) else None


def main() -> int:
    if os.environ.get("CLAUDE_CODE_REMOTE") != "true" and os.environ.get("CLOUD_GUARD_FORCE") != "1":
        return 0
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    reason = decide(command, event.get("cwd"))
    if reason:
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"[cloud guard] {reason}",
            }
        }, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
