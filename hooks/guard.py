#!/usr/bin/env python3
"""PreToolUse guard for Bash in Claude Code cloud sessions: a seatbelt, not a sandbox.

  - git push runs only as its own command:
      [cd <path> &&] git push [-u|--set-upstream] [-q|--quiet] [--force-with-lease[=<ref>]]
                              [--force-if-includes] [<remote> [<refspec>]]
    to a branch other than main, master or the remote's default branch. Any other command
    with the word `push` next to git, hub, a shell expansion, a here-document or a nested
    shell is refused, prose in a commit message included. git aliases are not inspected.
  - GitHub: merges, auto-merge, repo delete/archive/rename/edit/sync, rulesets and branch
    protection; `gh api` writes to contents, git refs and objects, hooks, Actions settings and
    environments; and any other client's call to those api.github.com paths, reads included.
    Other GitHub calls pass; the Claude GitHub App has no administration write, so GitHub
    itself refuses settings changes.
  - Databases: a hosted-Postgres host anywhere in the command, supabase with a remote verb or
    flag (dump, pull, push, link, repair, fetch, inspect, --linked, --db-url, --project-ref;
    --local does not exempt them), Supabase Management API database calls, and database env
    vars set to a remote value.

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
           "applied from Devin's machine with a backup first. Local work: `supabase start`, `supabase db reset`, "
           f"`supabase migration up` (local by default), or psql/pg_dump against 127.0.0.1. {SPLIT}")
UNREADABLE = "Could not read this command safely. Split it into simple commands without nested quoting."

_PUSH_WORD = re.compile(r"\b(?:push|send-pack)\b")
_GIT_WORD = re.compile(r"\b(?:git|hub)\b")
_HIDDEN = re.compile(r"[$`]|<<|<\(|>\(|\b(?:eval|xargs|bash|sh|zsh|dash|ksh)\b")
_RISKY = re.compile(r"\b(?:git|hub|gh|curl|wget|http|https|xh|psql|pg_dump|pg_restore|pgcli|supabase|"
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


# `git [global options] stash push`: options that take a value consume it, so a value that
# ends in "stash" (`git -C /tmp/stash push`) is not read as the subcommand.
_VALUE_OPTS = r"(?:-C|-c|--git-dir|--work-tree|--namespace|--exec-path|--config-env)"
_STASH_PUSH = re.compile(rf"\bgit\s+(?:{_VALUE_OPTS}\s+\S+\s+|(?!{_VALUE_OPTS}\s)--?[\w-]+(?:=\S*)?\s+)*stash\s+push\b")


def could_push(command: str) -> bool:
    for text in (command, re.sub(r"[\\'\"]", "", command)):  # "gi""t" and g\it spell git
        text = _STASH_PUSH.sub(lambda m: m.group(0)[:-len("push")], text)
        if _PUSH_WORD.search(text) and (_GIT_WORD.search(text) or _HIDDEN.search(text)):
            return True
    return False


# ---------------------------------------------------------------- GitHub


_FLAGS = r"(?:\s+-\S+(?:\s+[^-\s]\S*)?)*"  # flags between words: `gh pr -R o/r merge`
_MERGE = re.compile(r"\bpulls/\S*/merge\b|/merges\b|/merge-upstream\b|\bpr\b" + _FLAGS + r"\s+merge\b"
                    r"|\bauto-merge\b|enablePullRequestAutoMerge|mergePullRequest")
_GH_VARIABLE = re.compile(r"\b(?:gh|hub)\s+(?:(?:pr|repo)\s+)?[\"']?[$`]")  # gh pr $A
# API paths only (api.github.com/repos/..., gh api repos/...): a source path such as src/hooks/x.ts in a body does not count
_REPO = r"(?:api\.github\.com\.?/|(?<![\w/.-])/?)repos/[^/\s'\"]+/[^/\s'\"]+/"
_SETTINGS = re.compile(r"\b(?:gh|hub)" + _FLAGS + r"\s+repo\b" + _FLAGS + r"\s+(?:delete|archive|rename|edit|sync)\b"
                       r"|\b(?:gh|hub)" + _FLAGS + r"\s+ruleset\b|" + _REPO + r"(?:rulesets\b|branches/[^\s'\"]+/protection\b)"
                       r"|(?:api\.github\.com\.?/|(?<![\w/.-])/?)orgs/[^/\s'\"]+/rulesets\b", re.IGNORECASE)
_RAW_GITHUB = re.compile(r"api\.github\.com", re.IGNORECASE)
_GH_API = re.compile(r"\b(?:gh|hub)\b.*\bapi\b", re.IGNORECASE | re.DOTALL)
_GITHUB_WRITE_PATH = re.compile(_REPO + r"(?:contents/|git/(?:refs|commits|trees|blobs|tags)\b|hooks\b"
                                r"|actions/(?:secrets|variables|permissions)\b|environments/)", re.IGNORECASE)
_WRITE = re.compile(r"(?<![\w-])(?:-X|-d|-T|--method|--request|--data[\w-]*|--field|--raw-field|--input"
                    r"|--upload-file|--json|--form[\w-]*|--post-data|--post-file|--body-data|--body-file)"
                    r"|(?<![\w-])-[fF](?=\s|\w+=)|\b(?:POST|PUT|PATCH|DELETE)\b")
_GH_FIELD = re.compile(r"(?<![\w-])-[fF]\S")  # attached gh fields: -f'ref=x', -Fsha=y
_GH_GET = re.compile(r"(?:-X|--method)[\s=]*['\"]?GET\b")


def _gh_writes(text: str) -> bool:
    """gh api sends a write unless the method is GET (fields then become query parameters). Judged per command."""
    for part in re.split(r"&&|\|\||[;|\n]", text):
        if _GH_GET.search(part) and not re.search(r"\b(?:POST|PUT|PATCH|DELETE)\b|--input\b", part):
            continue
        if _WRITE.search(part) or _GH_FIELD.search(part):
            return True
    return False


def check_github(command: str) -> str | None:
    text = re.sub(r"%2f", "/", command, flags=re.IGNORECASE)
    text = re.sub(r"/(?:\.?/)+", "/", text)  # repos/o/r/./pulls, repos//o
    if _MERGE.search(text):
        return MERGE_HINT
    if _GH_VARIABLE.search(text):
        return f"A gh subcommand held in a variable can't be checked; spell it out. {LAND_HINT}"
    if _SETTINGS.search(text):
        return RULES_HINT
    # gh/hub api: writes only. Any other client: every method (curl -T, HTTPie fields and others imply writes).
    if _GITHUB_WRITE_PATH.search(text) and (_RAW_GITHUB.search(text) or (_GH_API.search(text) and _gh_writes(text))):
        return API_HINT
    return None


# ---------------------------------------------------------------- databases


_REMOTE_HOST = re.compile(r"\.supabase\.co\b|pooler\.supabase\.com|\.rds\.amazonaws\.com|\.neon\.tech|\.render\.com"
                          r"|\.railway\.app|\.aivencloud\.com|\.cockroachlabs\.cloud", re.IGNORECASE)
# A tool in command position: after a separator, `$(`, a shell keyword, wrappers, VAR=value and package runners.
# Not after `(`, a backtick or a quote, so `feat(supabase):` and `run \`psql\`` in a message are text.
# `pnpm exec`, `yarn dlx`, `bun x`: each word is a wrapper of its own, so no word can be read two ways
_WRAP = (r"(?:time|nohup|nice|watch|sudo|env|exec|command|xargs|timeout|npx|bunx|uvx"
         r"|npm|pnpm|yarn|bun|dlx|x)(?![\w-])")
# a flag value is never a number, an assignment or a wrapper, so each word parses one way (no backtracking blowup)
_START = (r"(?:^|[;&|\n]|\$\(|(?<![\w$])[({]|\b(?:then|do|else|if|while|until)\s|\b(?:bash|sh|zsh|dash|ksh)\s+-\w*c\s+['\"]?)\s*"
          r"(?:(?:[A-Za-z_]\w*=\S*|" + _WRAP + r"(?:\s+-\S+(?:\s+(?!\d|[A-Za-z_]\w*=|" + _WRAP
          + r")[^-\s]\S*)?|\s+\d\S*)*)\s+)*\$?(?:\S*/)?")
_SUPABASE_NAME = r"(supabase)(?![\w/-])"
_PG_NAME = r"(psql|pg_dump|pg_dumpall|pg_restore|pgcli|usql)(?![\w-])"
# Piped into a shell, eval'd or fed through xargs: the tool's words can come from anywhere, so match the whole text.
_SHELL_FED = re.compile(r"\|\s*(?:sudo\s+)?(?:bash|sh|zsh|dash|ksh)\b|(?:^|[;&|\n({]|\$\()\s*(?:eval|source|xargs)\b"
                        r"|<\(")
_SUPABASE_REMOTE = re.compile(r"--linked\b|--db-url\b|--project-ref\b|[$`]"
                              r"|\b(?:dump|pull|push|link|repair|fetch|inspect)\b")
_PG_HOST = re.compile(r"postgres(?:ql)?://(?:[^@/\s'\"]*@)?([^/?\s'\"]*)|(?:host|hostaddr)=['\"]?([^\s'\";&]*)"
                      r"|(?<![\w-])(?:-h|--host)(?:\s+|=)?['\"]?([^\s'\"]*)")
_LOCAL = re.compile(r"|/.*|%2f.*|(?:localhost|127\.0\.0\.1|::1|\[::1\])(?::\d+)?", re.IGNORECASE)
_DB_ENV = re.compile(r"\b(PGHOST|PGHOSTADDR|PGSERVICE|DATABASE_URL|SUPABASE_DB_URL|POSTGRES_URL)="
                     r"(?:'([^']*)'|\"([^\"]*)\"|([^\s'\"]*))")
_LOCAL_VALUE = re.compile(r"|/.*|(?:sqlite3?|file):.*|::1"
                          r"|(?:[\w+]+://(?:[^@/\s]*@)?)?(?:localhost|127(?:\.\d+){3}|\[::1\])(?:[:/?].*)?",
                          re.IGNORECASE)


def _local_value(value: str) -> bool:
    if "://" not in value and "=" in value:  # libpq keywords: host=127.0.0.1 dbname=test
        hosts = re.findall(r"\b(?:host|hostaddr)\s*=\s*([^\s]*)", value) or [
            os.environ.get("PGHOST", ""), os.environ.get("PGHOSTADDR", "")]  # no host: libpq uses PGHOST
        return all(_LOCAL.fullmatch(h) for host in hosts for h in host.split(","))
    return bool(_LOCAL_VALUE.fullmatch(value))


def _segments(name: str, command: str) -> list[str]:
    """Each invocation of the tool, from its name to the next separator (the whole text when shell-fed)."""
    if _SHELL_FED.search(command):
        return [command] if re.search(r"(?<![\w.@-])" + name, command) else []
    return [m.group(1) + re.match(r"[^;&|\n]*", command[m.end():]).group(0)
            for m in re.finditer(_START + name, command)]


def check_db(command: str) -> str | None:
    if _REMOTE_HOST.search(command):  # whatever the tool: psql, python -c, node -e, an env var
        return DB_HINT
    for name, *values in _DB_ENV.findall(command):
        value = "".join(values)
        if name == "PGSERVICE" or "$" in value or not _local_value(value):
            return f"`{name}` must name a local database here. {DB_HINT}"
    plain = re.sub(r"[\\'\"]", "", command)  # "supabase", s\upabase and $'supabase' spell supabase
    if any(_SUPABASE_REMOTE.search(seg) for text in (command, plain) for seg in _segments(_SUPABASE_NAME, text)):
        return DB_HINT  # --local does not exempt
    if re.search(r"api\.supabase\.com", command, re.IGNORECASE) and (
            re.search(r"/database/(?:query|migrations)", command) or _WRITE.search(command)):
        return DB_HINT
    # service= on the original text only: stripping quotes turns SQL `service='x'` into a keyword
    for seg, original in ((s, text is command) for text in (command, plain) for s in _segments(_PG_NAME, text)):
        if re.search(r"[$`]", seg) or (original and re.search(r"\bservice=[\w-]", seg)):
            return f"A database target held in a variable or pg_service entry can't be checked. {DB_HINT}"
        if os.environ.get("PGSERVICE"):
            return f"PGSERVICE is set. {DB_HINT}"
        hosts = ["".join(g or "" for g in m.groups()) for m in _PG_HOST.finditer(seg)] or [""]
        env = [os.environ.get("PGHOST", ""), os.environ.get("PGHOSTADDR", "")]
        hosts = [h for host in hosts for h in ([host] if host else env)]  # an empty host means libpq's PGHOST
        if not all(_LOCAL.fullmatch(h.strip()) for host in hosts for h in host.split(",")):
            return f"Connections to a remote database are not made from cloud sessions. {DB_HINT}"
    return None


# ---------------------------------------------------------------- decision


def _decide(command: str, cwd: str | None) -> str | None:
    command = command.replace("\\\n", " ")  # line continuations: `gh \<newline> api`
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
    return check_github(command) or check_db(command)


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
