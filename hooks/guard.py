#!/usr/bin/env python3
"""PreToolUse guard for Bash in Claude Code cloud sessions: a small allowlist.

  - git push runs only as its own command:
      [cd <path> &&] git push [-u|--set-upstream] [-q|--quiet] [--force-with-lease[=<ref>]]
                              [--force-if-includes] [<remote> [<refspec>]]
    to a branch other than main, master or the remote's default branch. Any other command
    that could push (the word `push` next to git, hub, a push alias or a shell expansion) is
    refused, prose in a commit message included.
  - GitHub through gh, hub, curl, wget or httpie: reads, plus opening a PR, editing its title
    or body, and comments and reviews. No merges, ref moves or settings: run `land`.
  - Databases (psql, pg_dump, pg_restore, pgcli, usql, supabase db/migration): local only.

It is a seatbelt against a model's mistakes, not a sandbox: GitHub rulesets on the default
branch are the server-side boundary and `land` is the CI gate. When a command can't be read
for sure (a variable, a substitution, xargs), the guard refuses it and says what to type
instead. Outside a cloud session (CLAUDE_CODE_REMOTE != true) it allows everything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

PROTECTED = {"main", "master"}
LAND_HINT = "Push a feature branch and run `land` (it waits for green CI, then squash-merges)."
PUSH_HINT = ("Push as its own command: `git push -u origin HEAD` (add --force-with-lease to force). "
             "If this was not a push, split it so `push` is not in a git command.")
DB_HINT = ("Production database changes are not made from cloud sessions. Commit the migration; "
           "it is applied from Devin's machine with a backup first. Local runs take --local.")
REPO_HINT = "Changing or deleting the repository is not done from cloud sessions; ask Devin."
RULES_HINT = "Branch protection and rulesets are changed by Devin, not from cloud sessions."
API_HINT = ("From cloud sessions the GitHub API is read-only, apart from opening a PR, editing its "
            "title or body, and comments and reviews. " + LAND_HINT)
VAR_HINT = "can't be checked. Spell it out without shell variables, substitutions or xargs."
UNREADABLE = "Could not read this command safely. Split it into simple commands without nested quoting."

SENT = "\x00"  # marks an unquoted expansion in a word: its value, even its word count, is unknown

_PUSH_WORD = re.compile(r"\b(?:push|send-pack)\b")
_GIT_WORD = re.compile(r"\b(?:git|hub)\b")
_HIDDEN = re.compile(r"[$`\x00]|<<|<\(|>\(|\b(?:eval|xargs|bash|sh|zsh|dash|ksh)\b")
_RISKY = re.compile(r"\b(?:git|hub|gh|curl|wget|http|https|psql|pg_dump|pg_restore|pgcli|usql|supabase|"
                    r"postgres|postgresql|PGHOST|DATABASE_URL)\b")
_EXPANDS = re.compile(r"[$`\x00]")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OPS = ("&&", "||", ";;", "|&", ";", "|", "&", "(", ")")
_REDIRS = ("&>>", "&>", ">>", ">&", "<&", ">|", "<<<", "<>", ">", "<")
_HEREDOC_TAG = re.compile(r"[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([^\s;&|<>()]+))")
_SHELL_ON_LINE = re.compile(r"(?<![\w.-])(?:bash|sh|zsh|dash|ksh)(?![\w.-])")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# Words skipped to find the command a segment runs.
_LEADERS = {"env", "sudo", "doas", "nohup", "nice", "ionice", "timeout", "time", "command", "builtin", "exec",
            "stdbuf", "xargs", "parallel", "npx", "bunx", "pnpx", "export", "declare", "local", "readonly",
            "{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}


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


def git_aliases(where: str | None) -> dict[str, str]:
    out = _git(where, "config", "--get-regexp", r"^alias\.") or ""
    return {key[6:].lower(): value for key, _, value in (line.partition(" ") for line in out.splitlines())}


# ---------------------------------------------------------------- shell parsing


def tokenize(command: str) -> list[tuple[str, str]]:
    """Split a shell line into ("word" | "op" | "redir" | "heredoc" | "subst", text) tokens.

    Bash-like enough for a seatbelt: quotes (including $'...'), backslashes, comments,
    operators, redirections and here-documents. A here-document body is dropped unless a
    shell on that line reads it. Each `$(...)` or backquoted command also comes out as a
    "subst" token holding its body; an unquoted expansion leaves SENT in its word.
    Raises ValueError on an unclosed quote.
    """
    toks: list[tuple[str, str]] = []
    word: list[str] = []
    in_word = False
    heredocs: list[str] = []
    line_start = 0
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal word, in_word
        if in_word:
            toks.append(("word", "".join(word)))
        word, in_word = [], False

    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            if command[i + 1] != "\n":  # backslash-newline is a line continuation
                word.append(command[i + 1])
                in_word = True
            i += 2
        elif c == "'" or command.startswith("$'", i):
            ansi = c == "$"
            j = i + 1 + ansi
            while j < n and command[j] != "'":
                j += 2 if ansi and command[j] == "\\" else 1
            if j >= n:
                raise ValueError("unclosed single quote")
            text = command[i + 1 + ansi:j]
            word.append(text + SENT if ansi and "\\" in text else text)  # escapes can spell any word
            in_word, i = True, j + 1
        elif command.startswith('$"', i):
            i += 1  # a translated string reads like "..."
        elif c == '"':
            j, buf = i + 1, []
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n and command[j + 1] in '"\\$`\n':
                    buf.append(command[j + 1])
                    j += 2
                elif command.startswith("$(", j) or command[j] == "`":
                    end = _subst_end(command, j)
                    toks.append(("subst", command[j + (2 if command[j] == "$" else 1):end - 1]))
                    buf.append(command[j:end])
                    j = end
                else:
                    buf.append(command[j])
                    j += 1
            if j >= n:
                raise ValueError("unclosed double quote")
            word.append("".join(buf))
            in_word, i = True, j + 1
        elif command.startswith("$(", i) or c == "`":
            end = _subst_end(command, i)
            toks.append(("subst", command[i + (2 if c == "$" else 1):end - 1]))
            word.append(command[i:end] + SENT)
            in_word, i = True, end
        elif c == "$":
            word.append("$" + SENT)
            in_word, i = True, i + 1
        elif c in " \t\r":
            flush()
            i += 1
        elif c == "#" and not in_word:
            j = command.find("\n", i)
            i = n if j < 0 else j
        elif c == "\n":
            flush()
            toks.append(("op", "\n"))
            line, i = command[line_start:i], i + 1
            if heredocs and not _SHELL_ON_LINE.search(line):
                i = _skip_heredoc_bodies(command, i, heredocs)
            heredocs, line_start = [], i
        elif command.startswith(("<<-", "<<"), i) and not command.startswith("<<<", i):
            flush()
            i += 3 if command.startswith("<<-", i) else 2
            m = _HEREDOC_TAG.match(command, i)
            if m:
                tag = next(g for g in m.groups() if g is not None)
                heredocs.append(tag)
                toks.append(("heredoc", tag))
                i = m.end()
        elif (r := next((r for r in _REDIRS if command.startswith(r, i)), None)):
            if in_word and "".join(word).isdigit():  # an fd number, as in 2>&1
                word, in_word = [], False
            flush()
            toks.append(("redir", r))
            i += len(r)
        elif (o := next((o for o in _OPS if command.startswith(o, i)), None)):
            flush()
            toks.append(("op", o))
            i += len(o)
        else:
            word.append(c)
            in_word = True
            i += 1
    flush()
    return toks


def _subst_end(command: str, i: int) -> int:
    """Index just past the `$(...)` or `...` command substitution that starts at i."""
    if command[i] == "`":
        j = command.find("`", i + 1)
        if j < 0:
            raise ValueError("unclosed backquote")
        return j + 1
    depth, quote, j, heredocs = 0, None, i + 1, []
    while j < len(command):
        c = command[j]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                j += 1
        elif c in "'\"":
            quote = c
        elif c == "\\":
            j += 1
        elif command.startswith("<<", j) and not command.startswith("<<<", j):
            m = _HEREDOC_TAG.match(command, j + (3 if command.startswith("<<-", j) else 2))
            if m:  # as in git commit -m "$(cat <<'EOF' ... EOF)"; the body may hold quotes
                heredocs.append(next(g for g in m.groups() if g is not None))
                j = m.end()
                continue
        elif c == "\n" and heredocs:
            j, heredocs = _skip_heredoc_bodies(command, j + 1, heredocs), []
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise ValueError("unclosed $(")


def _skip_heredoc_bodies(command: str, i: int, tags: list[str]) -> int:
    for tag in tags:
        while i < len(command):
            j = command.find("\n", i)
            end = len(command) if j < 0 else j + 1
            line, i = command[i:end], end
            if line.strip() == tag:
                break
    return i


def segments(command: str, depth: int = 0) -> list[list[str]]:
    """The simple commands in a shell line, with those inside substitutions, `bash -c '...'`,
    `eval`, `env -S` and here-strings fed to a shell."""
    if depth > 4:
        raise ValueError("nested too deeply")
    out: list[list[str]] = []
    cur: list[str] = []
    here: list[str] = []
    redir = None
    for kind, text in tokenize(command):
        if kind == "op":
            out += _nested(cur, here, depth)
            cur, here, redir = [], [], None
        elif kind == "redir":
            redir = text
        elif kind == "subst":
            out += segments(text, depth + 1)
        elif kind == "word":
            if redir is None:
                cur.append(text)
            elif redir == "<<<":
                here.append(text)
            redir = None
    return out + _nested(cur, here, depth)


def _nested(words: list[str], here: list[str], depth: int) -> list[list[str]]:
    if not words:
        return []
    out = [words]
    names = [os.path.basename(w) for w in words]
    if any(n in _SHELLS for n in names):
        script = _shell_script(words[next(k for k, n in enumerate(names) if n in _SHELLS) + 1:])
        for text in here + ([script] if script is not None else []):
            out += segments(text, depth + 1)
    if "eval" in names:
        out += segments(" ".join(words[names.index("eval") + 1:]), depth + 1)
    if "env" in names and ("-S" in words or "--split-string" in words):
        k = words.index("-S") if "-S" in words else words.index("--split-string")
        out += segments(" ".join(words[k + 1:]), depth + 1)
    return out


def _shell_script(args: list[str]) -> str | None:
    """For `bash [opts] -c 'script' ...` return the script; None when no -c is given."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
        elif a.startswith("--"):
            i += 1
        elif a.startswith(("-", "+")) and len(a) > 1:
            if "c" in a[1:]:
                return args[i + 1] if i + 1 < len(args) else ""
            i += 1
        else:
            return None
    return None


def _split_opt(arg: str, with_value: set[str]) -> tuple[str, str | None]:
    """`--x=v` -> ("--x", "v"); `-XPUT` or `-sX` -> ("-X", "PUT" | None); anything else -> (arg, None)."""
    if arg.startswith("--"):
        flag, eq, value = arg.partition("=")
        return (flag, value) if eq else (arg, None)
    if arg.startswith("-") and len(arg) > 1:
        for k in range(1, len(arg)):
            if "-" + arg[k] in with_value:
                return "-" + arg[k], arg[k + 1:] or None
    return arg, None


def _options(args: list[str], with_value: set[str]):
    """Yield (flag, value) for options and (None, word) for other words."""
    i = 0
    while i < len(args):
        a = args[i]
        flag, value = _split_opt(a, with_value)
        if flag in with_value:
            if value is None:
                value = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            yield flag, value
        elif a.startswith("-") and len(a) > 1:
            yield a, None
        else:
            yield None, a
        i += 1


def _non_flags(args: list[str], with_value: set[str]) -> list[str]:
    return [word for flag, word in _options(args, with_value) if flag is None]


# ---------------------------------------------------------------- git push


_PUSH_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "--force-with-lease", "--force-if-includes"}
_BRANCH = re.compile(r"(?!(?:refs|heads|remotes|tags)/)[A-Za-z0-9_][\w./-]*")


def _is_branch(name: str) -> bool:
    return bool(_BRANCH.fullmatch(name)) and name != "HEAD" and not re.search(r"\.\.|//|/\.|\.lock$|[/.]$", name)


def push_form(command: str) -> tuple[str | None, list[str]] | None:
    """(cd path or None, push arguments) when the whole command is one plain `git push`."""
    if re.search(r"[$`<]|>\(", command):
        return None
    try:
        toks = tokenize(command.strip())
    except ValueError:
        return None
    cd = None
    if len(toks) > 3 and toks[0] == ("word", "cd") and toks[1][0] == "word" and toks[2] == ("op", "&&"):
        cd, toks = toks[1][1], toks[3:]
    if any(kind != "word" for kind, _ in toks) or [t for _, t in toks[:2]] != ["git", "push"]:
        return None
    return cd, [t for _, t in toks[2:]]


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


def could_push(command: str, flat: str) -> bool:
    for text in (command, flat):
        text = re.sub(r"\bstash\s+push\b", "stash", text)
        if _PUSH_WORD.search(text) and (_GIT_WORD.search(text) or _HIDDEN.search(text)):
            return True
    return False


def check_aliases(segs: list[list[str]], where: str | None) -> str | None:
    """A git alias that pushes or runs a shell (`!...`) hides what it does."""
    gits = [words for words in segs if any(os.path.basename(w) == "git" for w in words)]
    if not gits:
        return None
    risky = {name for name, value in git_aliases(where).items()
             if _PUSH_WORD.search(value) or value.lstrip().startswith("!")}
    for words in gits:
        hit = next((w for w in words if w.lower() in risky), None)
        if hit:
            return f"`git {hit}` is an alias that can push or run a shell. {PUSH_HINT}"
    return None


# ---------------------------------------------------------------- GitHub


_O, _R = r"(?:[\w.-]+|\{owner\})", r"(?:[\w.-]+|\{repo\})"
_REPO = rf"(?:repos/{_O}/{_R}|repositories/\d+)"
_WRITE_OK = [("POST", re.compile(rf"{_REPO}/pulls")),
             ("PATCH", re.compile(rf"{_REPO}/pulls/\d+")),
             ("POST", re.compile(rf"{_REPO}/(?:issues|pulls)/\d+/comments")),
             ("POST", re.compile(rf"{_REPO}/pulls/\d+/reviews"))]
_STATE_BASE = re.compile(r"^\s*(?:state|base)\b|[\"'](?:state|base)[\"']\s*:|[&?](?:state|base)=")


def _body(text: str, cwd: str | None) -> str | None:
    """A request body or field; a file it names (`@file`, `key=@file`) is read. None if unreadable."""
    key, eq, value = text.partition("=")
    name = value if eq and value.startswith("@") else text if text.startswith("@") else None
    if name is None:
        return text
    try:
        with open(os.path.join(cwd or os.getcwd(), name[1:]), encoding="utf-8") as fh:
            return (key + "=" if eq else "") + fh.read(65536)
    except (OSError, ValueError):
        return None


def check_github_write(method: str, path: str, bodies: list[str], cwd: str | None) -> str | None:
    """Reads pass; writes only from _WRITE_OK, with PR state and base left to `land`."""
    if method in ("GET", "HEAD"):
        return None
    if not re.fullmatch(r"[A-Z]+", method):
        return f"An API method held in a variable {VAR_HINT}"
    path = path[1:] if path.startswith("/") else path
    if re.search(r"[%\\#$`\x00]|//|\.\.", path) or "." in path.split("?")[0].split("/"):
        return f"The API path `{path.replace(SENT, '')}` is not plain (no %, #, \\, .., //, . segments or variables)."
    texts = [_body(b, cwd) for b in bodies]
    if path == "graphql" and method == "POST":
        if all(t is not None and not re.search(r"\bmutation\b", t, re.IGNORECASE) for t in texts):
            return None
        return f"GraphQL mutations are not run from cloud sessions. {LAND_HINT}"
    if not any(m == method and rx.fullmatch(path) for m, rx in _WRITE_OK):
        if path.endswith("/merge") or "/merges" in path:
            return f"Merging by hand skips the CI gate. {LAND_HINT}"
        return RULES_HINT if re.search(r"/protection|rulesets", path) else f"`{method} {path}`: {API_HINT}"
    if method == "PATCH" and any(t is None or _STATE_BASE.search(t) for t in texts):
        return f"Changing a PR's state or base by hand is not done from cloud sessions. {LAND_HINT}"
    return None


def _split_url(url: str) -> tuple[str | None, str]:
    rest = url.split("://", 1)[1] if "://" in url else url
    host, _, path = rest.partition("/")
    host = re.sub(r":\d*$", "", host.rpartition("@")[2].split("?")[0].lower())
    return (None if _EXPANDS.search(host) or not host and not path else host), path


def _check_http(method: str, urls: list[str], bodies: list[str], cwd: str | None) -> str | None:
    write = method not in ("GET", "HEAD")
    for url in urls:
        host, path = _split_url(url)
        if host is None:
            if write:
                return f"A write to a URL held in a variable {VAR_HINT}"
        elif host == "api.supabase.com" or host.endswith(".supabase.co"):
            if write or re.search(r"/database/(?:query|migrations)", "/" + path):
                return DB_HINT
        elif host == "github.com" or host.endswith(".github.com"):
            reason = write and check_github_write(method, path, bodies, cwd)
            if reason:
                return reason
    return None


def _method(methods: list[str], args: list[str], default: str) -> str:
    if any(SENT in a for a in args):
        return "?"  # an unquoted expansion may hold -X PUT
    return methods[-1].upper() if methods else default


_GH_API_VALUE = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header", "--input",
                 "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview", "-d", "--data"}


def check_gh(args: list[str], cwd: str | None) -> str | None:
    sub = _non_flags(args, {"-R", "--repo", "--hostname"})[:2]
    if any(_EXPANDS.search(w) for w in sub):
        return f"A gh subcommand held in a variable {VAR_HINT}"
    if sub == ["pr", "merge"]:
        return f"Merging by hand skips the CI gate. {LAND_HINT}"
    if sub[:1] == ["repo"] and sub[1:] and sub[1] in ("delete", "archive", "rename", "edit", "sync"):
        return REPO_HINT
    if sub[:1] == ["ruleset"]:
        return RULES_HINT
    if sub[:1] == ["alias"] and sub[1:] and sub[1] in ("set", "import"):
        return "gh aliases can hide a merge; run the gh command itself."
    if sub[:1] != ["api"]:
        return None
    methods, endpoint, bodies = [], None, []
    for flag, value in _options(args[args.index("api") + 1:], _GH_API_VALUE):
        if flag in ("-X", "--method"):
            methods.append(value)
        elif flag in ("-f", "-F", "--field", "--raw-field", "-d", "--data"):
            bodies.append(value)
        elif flag == "--input":
            bodies.append("@" + value)
        elif flag is None and endpoint is None:
            endpoint = value
    if endpoint is None:
        return None
    method = _method(methods, args, "POST" if bodies else "GET")
    path = endpoint.split("://", 1)[1].partition("/")[2] if "://" in endpoint else endpoint
    return check_github_write(method, path, bodies, cwd)


_CURL_DATA = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "--json",
              "-F", "--form", "--form-string"}
_CURL_VALUE = {"-X", "--request", "-H", "--header", "-o", "--output", "-u", "--user", "-A", "--user-agent",
               "-e", "--referer", "-b", "--cookie", "-c", "--cookie-jar", "-K", "--config", "--url",
               "-w", "--write-out", "-m", "--max-time", "--connect-timeout", "-x", "--proxy", "--retry",
               "-r", "--range", "-E", "--cert", "--cacert", "--key", "--oauth2-bearer", "--resolve",
               "--connect-to", "-D", "--dump-header", "-T", "--upload-file"} | _CURL_DATA


def check_curl(args: list[str], cwd: str | None) -> str | None:
    methods, urls, bodies, upload, get = [], [], [], False, False
    for flag, value in _options(args, _CURL_VALUE):
        if flag in ("-X", "--request"):
            methods.append(value)
        elif flag in _CURL_DATA:
            bodies.append(value)
        elif flag in ("-T", "--upload-file"):
            upload = True
        elif flag in ("-K", "--config"):
            return "`curl -K` reads its URL and method from a file. Put them on the command line."
        elif flag in ("-G", "--get"):
            get = True
        elif flag in (None, "--url"):
            urls.append(value)
    method = _method(methods, args, "PUT" if upload else "POST" if bodies and not get else "GET")
    return _check_http(method, urls, bodies, cwd)


_WGET_VALUE = {"-O", "--output-document", "-o", "--output-file", "-a", "--append-output", "-i", "--input-file",
               "-P", "--directory-prefix", "-U", "--user-agent", "--header", "--user", "--password", "-t",
               "--tries", "-T", "--timeout", "-w", "--wait", "-e", "--execute", "--method", "--post-data",
               "--post-file", "--body-data", "--body-file", "-B", "--base"}


def check_wget(args: list[str], cwd: str | None) -> str | None:
    methods, urls, bodies = [], [], []
    for flag, value in _options(args, _WGET_VALUE):
        if flag == "--method":
            methods.append(value)
        elif flag in ("--post-data", "--body-data"):
            bodies.append(value)
        elif flag in ("--post-file", "--body-file"):
            bodies.append("@" + value)
        elif flag in ("-i", "--input-file"):
            urls.append(SENT)
        elif flag is None:
            urls.append(value)
    return _check_http(_method(methods, args, "POST" if bodies else "GET"), urls, bodies, cwd)


_HTTPIE_VALUE = {"-a", "--auth", "-A", "--auth-type", "-o", "--output", "--session", "--session-read-only",
                 "--verify", "--cert", "--cert-key", "--proxy", "-p", "--print", "--pretty", "-s", "--style",
                 "--timeout", "--max-redirects", "--default-scheme", "--raw", "--boundary", "--format-options"}


def check_httpie(args: list[str], cwd: str | None) -> str | None:
    pos, bodies = [], []
    for flag, value in _options(args, _HTTPIE_VALUE):
        if flag == "--raw":
            bodies.append(value)
        elif flag is None:
            pos.append(value)
    methods = [pos.pop(0)] if len(pos) > 1 and not re.search(r"[./:]", pos[0]) else []
    bodies += [item for item in pos[1:] if re.search(r"(?<!=)=(?!=)|@", item)]
    return _check_http(_method(methods, args, "POST" if bodies else "GET"), pos[:1], bodies, cwd)


# ---------------------------------------------------------------- databases


_LOCAL = {"localhost", "127.0.0.1", "::1", "[::1]", ""}
_PG_URL = re.compile(r"postgres(?:ql)?://(?:[^@/?#\s]*@)?([^/?#\s]*)(\S*)")
_DB_ENV = re.compile(r"^(PGHOST|PGHOSTADDR|PGSERVICE|PGSERVICEFILE|DATABASE_URL|SUPABASE_DB_URL|POSTGRES_URL)=(.*)$",
                     re.DOTALL)
_SUPABASE_VALUE = {"--workdir", "-o", "--output", "-s", "--schema", "-f", "--file", "-p", "--password",
                   "--version", "--status", "--profile", "--network-id"}
_SUPABASE_LOCAL = {("db", "reset"), ("db", "start"), ("test", "db"), ("test", "new"), ("migration", "new"),
                   ("migration", "list"), ("migrations", "new"), ("migrations", "list")}
REMOTE_DB = "Connections to a remote database are not made from cloud sessions. " + DB_HINT


def _is_local(host: str) -> bool:
    host = host.strip("'\"").lower()
    if not host.startswith("["):
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    elif "]" in host:
        host = host[:host.index("]") + 1]
    return host in _LOCAL or host.startswith(("/", "%2f"))


def _url_hosts(text: str) -> list[str]:
    hosts = []
    for m in _PG_URL.finditer(text):
        hosts += m.group(1).split(",") + re.findall(r"[?&](?:host|hostaddr)=([^&\s]*)", m.group(2))
    return hosts


def check_db_env(words: list[str]) -> str | None:
    for w in words:
        m = _DB_ENV.match(w)
        if not m:
            continue
        name, value = m.groups()
        hosts = value.split(",") if name in ("PGHOST", "PGHOSTADDR") else _url_hosts(value)
        if name.startswith("PGSERVICE") or _EXPANDS.search(value) or not all(map(_is_local, hosts)):
            return f"`{name}` must name a local database here. {REMOTE_DB}"
    return None


def check_db_client(args: list[str], cwd: str | None) -> str | None:
    if any(_EXPANDS.search(a) for a in args):
        return f"A database target held in a variable {VAR_HINT}"
    if any(re.search(r"\bservice\s*=", a) for a in args):
        return f"A pg_service entry can point anywhere. {REMOTE_DB}"
    hosts = []
    for k, a in enumerate(args):
        if a in ("-h", "--host"):
            hosts.append(args[k + 1] if k + 1 < len(args) else "")
        elif a.startswith("--host="):
            hosts.append(a[len("--host="):])
        elif a.startswith("-h") and not a.startswith("--"):
            hosts.append(a[2:])
        hosts += _url_hosts(a) + re.findall(r"(?:^|\s)(?:host|hostaddr)\s*=\s*'?([^\s']*)", a)
    if not hosts:
        hosts = [os.environ.get("PGHOST", ""), os.environ.get("PGHOSTADDR", "")]
        if os.environ.get("PGSERVICE"):
            return f"PGSERVICE is set. {REMOTE_DB}"
    return None if all(_is_local(h) for host in hosts for h in host.split(",")) else REMOTE_DB


def check_supabase(args: list[str], cwd: str | None) -> str | None:
    if any(SENT in a for a in args):
        return f"A supabase command with a shell expansion {VAR_HINT}"
    if any(a.split("=", 1)[0] in ("--linked", "--db-url", "--project-ref") for a in args):
        return DB_HINT
    pos = _non_flags(args, _SUPABASE_VALUE)
    group, action = (pos + [None, None])[:2]
    if group not in ("db", "migration", "migrations", "test", "inspect", "sql") or "--local" in args:
        return None
    return None if (group, action) in _SUPABASE_LOCAL else DB_HINT


# ---------------------------------------------------------------- decision


CHECKS = {"gh": check_gh, "hub": check_gh, "curl": check_curl, "wget": check_wget, "http": check_httpie,
          "https": check_httpie, "xh": check_httpie, "xhs": check_httpie, "supabase": check_supabase,
          **dict.fromkeys(("psql", "pg_dump", "pg_dumpall", "pg_restore", "pgcli", "usql"), check_db_client)}


def check_segment(words: list[str], cwd: str | None) -> str | None:
    cmd = next((w for w in words if not (_ASSIGN.match(w) or os.path.basename(w) in _LEADERS
                                         or w.startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", w))), None)
    if cmd is not None and os.path.basename(cmd) in ("echo", "printf"):
        return None
    if cmd is not None and _EXPANDS.search(cmd) and not re.match(r"\$\{?HOME\}?/", cmd.replace(SENT, "")):
        return f"The command name `{cmd.replace(SENT, '')}` {VAR_HINT}"
    reason = check_db_env(words)
    if reason:
        return reason
    k = next((k for k, w in enumerate(words) if os.path.basename(w).split("@")[0] in CHECKS), None)
    if k is None:
        return None if all(map(_is_local, _url_hosts(" ".join(words)))) else REMOTE_DB
    args = words[k + 1:]
    if any(os.path.basename(w) in ("xargs", "parallel") for w in words[:k]):
        args = args + [SENT]  # xargs appends words we can't see
    return (CHECKS[os.path.basename(words[k]).split("@")[0]](args, cwd)
            or (None if all(map(_is_local, _url_hosts(" ".join(words)))) else REMOTE_DB))


def _decide(command: str, cwd: str | None) -> str | None:
    form = push_form(command)
    if form is not None:
        cd, args = form
        if cd is not None:
            if cd.startswith("-"):
                return PUSH_HINT
            cwd = os.path.join(cwd or os.getcwd(), os.path.expanduser(cd))
        return check_push(args, cwd)
    try:
        segs = segments(command)
    except ValueError:
        segs = None
    flat = "" if segs is None else " ".join(w for words in segs for w in words).replace(SENT, "$")
    if could_push(command, flat):
        return PUSH_HINT
    if segs is None:
        return UNREADABLE if _RISKY.search(command) else None
    for words in segs:
        reason = check_segment(words, cwd)
        if reason:
            return reason
    return check_aliases(segs, cwd)


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
