#!/usr/bin/env python3
"""PreToolUse guard for Bash in Claude Code cloud sessions.

Blocks the few commands that skip the landing gate or touch production:
  - git push to the default branch, main or master (any refspec form, --all, --mirror,
    matching and wildcard refspecs, configured push mappings)
  - force pushes without --force-with-lease
  - merging a PR, moving a protected ref or committing to a branch through the API by hand
    (use `land`)
  - deleting, archiving, renaming or editing the repository; branch protection and rulesets
  - Supabase commands against a linked or remote database

It is a seatbelt against a model's mistakes, not a sandbox: anyone set on getting around
it can. GitHub rulesets on the default branch are the server-side boundary; `land` is the
CI gate. Commands are parsed into words like a shell would (quotes, `&&`, `;`, pipes,
subshells, `bash -c`, `env`/`timeout`/`sudo` wrappers), so prose such as
`echo 'gh pr merge'` passes. When a push or API write can't be resolved (a shell variable,
an unknown directory, `git -c`), the guard refuses it and says what to type instead.
Outside a cloud session (CLAUDE_CODE_REMOTE != true) it allows everything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import NamedTuple

PROTECTED = {"main", "master"}
LAND_HINT = "Push a feature branch and run `land` (it waits for green CI, then squash-merges)."
SPELL_OUT = "Spell out the branch, e.g. `git push -u origin HEAD` from your feature branch."
DB_HINT = ("Production database changes are not made from cloud sessions. Commit the migration; "
           "it is applied from Devin's machine with a backup first.")
REPO_HINT = "Changing or deleting the repository is not done from cloud sessions; ask Devin."
RULES_HINT = "Branch protection and rulesets are changed by Devin, not from cloud sessions."

# Words that make an unparseable command worth refusing rather than waving through.
_RISKY_WORDS = ("push", "merge", "gh api", "curl", "supabase", "refs")
UNKNOWN_DIR = "/nonexistent/cloud-guard-unknown-dir"

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OPS = ("&&", "||", ";;", "|&", ";", "|", "&", "(", ")", "`")
_REDIRS = ("&>>", "&>", ">>", ">&", "<&", ">|", "<<<", "<>", ">", "<")
_HEREDOC_TAG = re.compile(r"[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([^\s;&|<>()]+))")
_SHELL_ON_LINE = re.compile(r"(?:^|[\s|;&(/])(?:bash|sh|zsh|dash|ksh)\b(?![.\w-])")
_KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_DB_CLIENTS = {"psql", "pg_dump", "pg_dumpall", "pg_restore", "pgcli", "usql"}
_SUPABASE_HOST = re.compile(r"supabase\.(?:co|com)\b")
_PG_URL = re.compile(r"^postgres(?:ql)?://\S*supabase\.(?:co|com)")
_WRITES = {"POST", "PUT", "PATCH", "DELETE"}


class Simple(NamedTuple):
    """One simple command: its words, plus any leading VAR=value assignments."""
    words: list[str]
    assigns: list[str]


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
    """Where a bare `git push` sends the current branch (honors push.default and remote.*.push)."""
    out = _git(where, "rev-parse", "--abbrev-ref", "@{push}")
    if not out:
        return None
    for remote in (_git(where, "remote") or "").split():
        if out.startswith(remote + "/"):
            return out[len(remote) + 1:]
    return out


def push_config(where: str | None) -> str:
    return _git(where, "config", "--get-regexp", r"^(push\.default|remote\..*\.(push|mirror))$") or ""


def protected_for(where: str | None, remote: str | None = None) -> set[str]:
    names = set(PROTECTED)
    default = default_branch(where, remote or "origin")
    if default:
        names.add(default)
    return names


# ---------------------------------------------------------------- shell parsing


def tokenize(command: str) -> list[tuple[str, str]]:
    """Split a shell line into ("word" | "op" | "redir" | "heredoc" | "subst", text) tokens.

    Bash-like enough for a seatbelt: quotes, backslashes, comments, operators, redirections
    and here-documents. A here-document body is dropped unless a shell on that line reads it.
    A `$(...)` or backquoted command inside double quotes also comes out as a "subst" token
    holding its body. Raises ValueError on an unclosed quote.
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
        elif c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                raise ValueError("unclosed single quote")
            word.append(command[i + 1:j])
            in_word, i = True, j + 1
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


def segments(command: str, depth: int = 0) -> list[Simple]:
    """Split a shell line into simple commands, unwrapping `bash -c '...'`, eval and wrappers."""
    if depth > 4:
        raise ValueError("nested too deeply")
    out: list[Simple] = []
    cur: list[str] = []
    skip_target = False
    for kind, text in tokenize(command):
        if kind == "op":
            out.extend(_simple(cur, depth))
            cur, skip_target = [], False
        elif kind == "redir":
            skip_target = True
        elif kind == "subst":
            out.extend(segments(text, depth + 1))
        elif kind == "word":
            if skip_target:
                skip_target = False
            else:
                cur.append(text)
    out.extend(_simple(cur, depth))
    return out


def _simple(words: list[str], depth: int) -> list[Simple]:
    while words and words[0] in _KEYWORDS:
        words = words[1:]
    return unwrap(words, depth) if words else []


def _skip_opts(args: list[str], with_value: frozenset[str] = frozenset(),
               assigns: bool = False, positional: int = 0) -> list[str]:
    """Drop leading options (and the values of options in with_value), then `positional` words."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("-") and len(a) > 1:
            i += 2 if a in with_value else 1
        elif assigns and _ASSIGN.match(a):
            i += 1
        else:
            break
    return args[i + positional:]


def _wrapper(with_value: str = "", assigns: bool = False, positional: int = 0) -> Callable[[list[str]], list[str]]:
    opts = frozenset(with_value.split())
    return lambda args: _skip_opts(args, opts, assigns, positional)


# Commands that run another command: how to find it.
WRAPPERS: dict[str, Callable[[list[str]], list[str]]] = {
    "env": _wrapper("-u --unset -C --chdir -P", assigns=True),
    "timeout": _wrapper("-s --signal -k --kill-after", positional=1),
    "nice": _wrapper("-n --adjustment"),
    "ionice": _wrapper("-c --class -n --classdata -p --pid -P --pgid -u --uid"),
    "nohup": _wrapper(),
    "command": _wrapper(),
    "exec": _wrapper("-a"),
    "time": _wrapper("-f --format -o --output"),
    "sudo": _wrapper("-u --user -g --group -C --close-from -D --chdir -h --host -p --prompt "
                     "-r --role -t --type -U --other-user -T --command-timeout", assigns=True),
    "doas": _wrapper("-u -C"),
    "stdbuf": _wrapper("-i -o -e --input --output --error"),
    "npx": _wrapper("-p --package"),
    "bunx": _wrapper("-p --package"),
    "pnpx": _wrapper("-p --package"),
}
_XARGS = frozenset("-I -L -n -P -s -d -E -a -e -i --max-args --max-procs --delimiter --arg-file".split())


def unwrap(words: list[str], depth: int = 0) -> list[Simple]:
    assigns: list[str] = []
    while words:
        if _ASSIGN.match(words[0]):
            assigns.append(words[0])
            words = words[1:]
            continue
        head = os.path.basename(words[0])
        if head in _SHELLS:
            script = _shell_script(words[1:])
            return segments(script, depth + 1) if script is not None else [Simple(words, assigns)]
        if head == "eval":
            return segments(" ".join(words[1:]), depth + 1)
        if head == "env" and any(a in ("-S", "--split-string") or a.startswith("--split-string=") for a in words[1:]):
            return segments(" ".join(_skip_opts(words[1:], frozenset({"-u", "--unset", "-C", "--chdir"}), True)), depth + 1)
        if head == "xargs":
            # xargs appends words we can't see; the `$` marker makes push checks refuse to guess.
            words = _skip_opts(words[1:], _XARGS) + ["${xargs}"]
            continue
        if head in ("pnpm", "yarn", "npm", "bun") and len(words) > 1 and words[1] in ("dlx", "exec", "x"):
            words = _skip_opts(words[2:], frozenset({"-p", "--package"}))
            continue
        skip = WRAPPERS.get(head)
        if skip is None:
            break
        words = skip(words[1:])
    return [Simple(words, assigns)] if words else []


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
                rest = _skip_opts(args[i + 1:], frozenset({"-o", "+o", "-O", "+O"}))
                return rest[0] if rest else ""
            i += 1
        else:
            return None
    return None


# ---------------------------------------------------------------- checks


def _is_var(text: str) -> bool:
    return any(c in text for c in "$`{}*%<>") or text == ""


def _join(here: str | None, path: str) -> str:
    if _is_var(path) or path == "-":
        return UNKNOWN_DIR
    return os.path.join(here or os.getcwd(), os.path.expanduser(path))


def _chdir(here: str | None, args: list[str]) -> str:
    args = [a for a in args if a not in ("-L", "-P", "-e", "-@", "--")]
    return _join(here, args[0]) if args else os.path.expanduser("~")


def ref_branch(ref: str, branch: str | None) -> str | None:
    """Branch name a push refspec side names; None when it is HEAD and HEAD is unknown."""
    if ref in ("HEAD", "@"):
        return branch
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


KEEP = object()  # HEAD did not move


def _repo_key(where: str | None) -> str:
    return _git(where, "rev-parse", "--show-toplevel") or os.path.normpath(where or os.getcwd())


def head_after(sub: str, args: list[str]):
    """Branch HEAD names after `git <sub> <args>`: KEEP if HEAD does not move, None if unknown."""
    if any("$" in a or "`" in a for a in args):
        return None
    flags = [a for a in args if a.startswith("-")]
    plain = [a for a in args if not a.startswith("-")]
    if sub == "branch":
        if not any(f in ("-m", "-M", "--move") for f in flags):
            return KEEP
        return plain[0] if len(plain) == 1 else None
    if sub == "symbolic-ref":
        if any(f in ("-d", "--delete") for f in flags):
            return None
        return ref_branch(plain[1], None) if plain[:1] == ["HEAD"] and len(plain) > 1 else KEEP
    if sub == "bisect":
        return None if plain[:1] in (["start"], ["reset"]) else KEEP
    # checkout / switch
    if sub == "checkout" and "--" in args:
        return KEEP  # `git checkout [<tree-ish>] -- <paths>` restores files
    create, track, detach, rest, i = None, False, False, [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a in ("-b", "-B", "-c", "-C", "--create", "--force-create", "--orphan"):
            create = args[i] if i < len(args) else None
            i += 1
        elif a.startswith(("--create=", "--force-create=", "--orphan=")):
            create = a.split("=", 1)[1]
        elif a in ("-d", "--detach"):
            detach = True
        elif a == "-t" or a.startswith("--track"):
            track = True
        elif a == "-" or not a.startswith("-"):
            rest.append(a)  # a lone "-" is the previous branch, not a flag
    if create:
        return create
    if detach:
        return None
    if not rest:
        return None if track else KEEP
    if sub == "checkout" and len(rest) > 1:
        return KEEP  # `git checkout <tree-ish> <paths>`
    target = rest[0]
    if target == "-" or target.startswith("@{"):
        return None
    return target.split("/", 1)[1] if track and "/" in target else target


def check_git(cmd: Simple, here: str | None, moved: dict[str, object] | None = None) -> str | None:
    moved = {} if moved is None else moved
    words = cmd.words
    if os.path.basename(words[0]) != "git":
        return None
    where, config, i = here, False, 1
    while i < len(words):
        w = words[i]
        if w == "-C" and i + 1 < len(words):
            where = _join(where, words[i + 1])
            i += 2
        elif w in ("-c", "--config-env"):
            config = True
            i += 2
        elif w.startswith("--config-env="):
            config = True
            i += 1
        elif w in ("--git-dir", "--work-tree", "--namespace", "--super-prefix") and i + 1 < len(words):
            if w == "--git-dir":
                where = _join(where, words[i + 1])
            i += 2
        elif w.startswith("--git-dir="):
            where = _join(where, w.split("=", 1)[1])
            i += 1
        elif w.startswith("-"):
            i += 1
        else:
            break
    if i >= len(words):
        return None
    if words[i] in ("checkout", "switch", "branch", "symbolic-ref", "bisect"):
        head = head_after(words[i], words[i + 1:])
        if head is not KEEP:
            moved[_repo_key(where)] = head
        return None
    if words[i] != "push":
        return None
    if config or any(a.startswith("GIT_CONFIG") for a in cmd.assigns):
        return ("`git -c ... push` can change where the push goes. Drop the -c option and push your "
                "feature branch: `git push -u origin HEAD`.")
    return check_push(words[i + 1:], where, moved.get(_repo_key(where), KEEP) if moved else KEEP)


_PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def check_push(args: list[str], where: str | None, head: object = KEEP) -> str | None:
    """head: the branch an earlier command in the same line switched to (None if unknown)."""
    force_all = delete = lease_all = False
    leased: set[str] = set()
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if "$" in a or "`" in a:
            return f"`git push` with a shell variable or substitution (`{a}`) can't be checked. {SPELL_OUT}"
        if a == "--":
            positional.extend(args[i:])
            break
        if a in ("--all", "--branches", "--mirror"):
            return f"`git push {a}` would update the default branch. {LAND_HINT}"
        if a == "--force":
            force_all = True
        elif a == "--force-with-lease":
            lease_all = True
        elif a.startswith("--force-with-lease="):
            leased.add(ref_branch(a.split("=", 1)[1].split(":", 1)[0], None) or "")
        elif a == "--no-force-with-lease":
            lease_all = False
            leased.clear()
        elif a == "--delete":
            delete = True
        elif a in _PUSH_VALUE_OPTS:
            i += 1
        elif a.startswith("--"):
            continue
        elif a.startswith("-") and len(a) > 1:
            for j, c in enumerate(a[1:], start=2):
                if c == "f":
                    force_all = True
                elif c == "d":
                    delete = True
                elif c == "o":  # -o <option> takes the rest of the cluster or the next word
                    i += 1 if j == len(a) else 0
                    break
        else:
            positional.append(a)

    remote = positional[0] if positional else None
    specs = positional[1:]
    branch = current_branch(where) if head is KEEP else head
    protected = protected_for(where, remote)
    if force_all:
        # --force disables the lease checks too (git-push(1)), so a lease does not make it safe.
        return "Force push without a lease is blocked. Use `git push --force-with-lease` on your feature branch."
    if not specs:
        if delete:
            return None
        if head is not KEEP:
            return ("A bare `git push` after switching branches in the same command can't be checked. "
                    "Push as its own command, or name the branch: `git push -u origin HEAD`.")
        return _check_default_push(where, branch, protected)
    for spec in specs:
        forced = spec.startswith("+")
        spec = spec.removeprefix("+")
        if spec in ("", ":") or "*" in spec:
            return (f"`git push {remote} {spec or ':'}` pushes many branches at once, including the "
                    f"default branch. {LAND_HINT}")
        if spec.startswith("^"):
            continue  # negative refspec: excludes, never pushes
        src, colon, dst = spec.partition(":")
        if delete or (colon and not src):
            name = ref_branch(dst if colon else src, branch)
            if name is None or name in protected:
                return f"Deleting `{name or spec}` is blocked in cloud sessions."
            continue
        name = ref_branch(dst or src, branch)
        if name is None:
            return f"Could not tell which branch `{spec}` pushes to (no branch checked out here). {SPELL_OUT}"
        if name in protected:
            return f"Pushing to `{name}` is blocked in cloud sessions. {LAND_HINT}"
        if forced and not (lease_all or name in leased):
            return "Force push without a lease is blocked. Use `git push --force-with-lease` on your feature branch."
    return None


def _check_default_push(where: str | None, branch: str | None, protected: set[str]) -> str | None:
    """A push with no refspec: the current branch, unless config says otherwise."""
    for line in push_config(where).splitlines():
        key, _, value = line.partition(" ")
        if key == "push.default" and value == "matching":
            return f"push.default=matching pushes every branch, including the default branch. {SPELL_OUT}"
        if key.endswith(".mirror") and value.lower() in ("true", "1", "yes", "on"):
            return f"This remote is configured as a mirror; a push would update the default branch. {LAND_HINT}"
        if key.endswith(".push"):
            spec = value.removeprefix("+")
            dst = spec.partition(":")[2] or spec.partition(":")[0]
            name = ref_branch(dst, branch)
            if spec in ("", ":") or "*" in spec or name is None or name in protected:
                return f"The configured push refspec `{value}` can update the default branch. {SPELL_OUT}"
    if branch is None:
        return f"Could not tell which branch a bare `git push` would update here. {SPELL_OUT}"
    target = push_target(where) or branch
    for name in (branch, target):
        if name in protected:
            return f"Pushing to `{name}` is blocked in cloud sessions. {LAND_HINT}"
    return None


def _api_path(endpoint: str) -> list[str]:
    segs = [s for s in endpoint.split("://", 1)[-1].split("?", 1)[0].split("/") if s]
    if segs and "." in segs[0]:  # a host; curl also takes api.github.com/... without a scheme
        segs = segs[1:]
    if segs[:2] == ["api", "v3"]:  # GitHub Enterprise
        segs = segs[2:]
    return segs


def check_api(method: str, endpoint: str, body: list[str], where: str | None) -> str | None:
    """A REST or GraphQL call to GitHub with a write method."""
    method = method.upper()
    if method not in _WRITES:
        return None
    segs = _api_path(endpoint)
    if not segs or _is_var(segs[0]):
        return f"Writing to an API path held in a variable (`{endpoint}`) can't be checked. Spell out the path."
    if segs[0] == "graphql":
        text = " ".join(body)
        if re.search(r"\bmutation\b", text) and re.search(
                r"merge|updateRefs?|deleteRef|createRef|BranchProtection|Ruleset|archiveRepository|"
                r"deleteRepository|updateRepository|transferRepository", text, re.IGNORECASE):
            return f"This GraphQL mutation merges or changes protected state by hand. {LAND_HINT}"
        return None
    if "rulesets" in segs:
        return RULES_HINT
    if segs[0] != "repos" or len(segs) < 2:
        return None
    rest = segs[1:]
    tails = [rest[2:]] if len(rest) >= 2 else []
    if _is_var(rest[0]) and not (len(rest) > 1 and _is_var(rest[1])):
        tails.append(rest[1:])  # one variable may hold owner/repo, as in repos/$NWO/...
    for tail in tails:
        reason = _check_repo_call(method, endpoint, tail, " ".join(body), where)
        if reason:
            return reason
    return None


def _check_repo_call(method: str, endpoint: str, tail: list[str], text: str, where: str | None) -> str | None:
    """`tail` is the path after repos/<owner>/<repo>."""
    if not tail:
        return REPO_HINT if method in ("PATCH", "DELETE") else None
    head = tail[0]
    if _is_var(head):
        return f"Writing to `{endpoint}` can't be checked (a variable in the path). Spell out the path."
    if head == "pulls" and len(tail) >= 3 and tail[2] == "merge":
        return f"Merging through the API by hand skips the CI gate. {LAND_HINT}"
    if head in ("merges", "merge-upstream"):
        return f"Direct branch merges through the API are blocked. {LAND_HINT}"
    if head == "transfer":
        return REPO_HINT
    if head == "contents" and method in ("PUT", "DELETE"):
        return f"Committing through the contents API writes straight to a branch. Commit with git. {LAND_HINT}"
    if head == "branches" and ("protection" in tail or tail[-1] == "rename"):
        return RULES_HINT
    if head == "git" and tail[1:2] == ["refs"]:
        ref = "/".join(tail[2:])
        protected = protected_for(where)
        if not ref:
            names = re.findall(r"refs/heads/([^\s\"',}]+)", text)
            if method == "POST" and names and not any(_is_var(n) or n in protected for n in names):
                return None
            return f"Creating or moving a protected branch through the API is blocked. {LAND_HINT}"
        if _is_var(ref):
            return f"Writing to `{endpoint}` can't be checked (a variable in the ref). Spell out the branch."
        name = ref.removeprefix("heads/") if ref.startswith("heads/") else None
        if name is not None and (_is_var(name) or name in protected):
            return f"Moving `{name}` through the API is blocked. {LAND_HINT}"
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


_GH_API_VALUE = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header", "--input",
                 "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview"}
_GH_FIELDS = {"-f", "-F", "--field", "--raw-field", "--input"}


def check_gh(words: list[str], where: str | None) -> str | None:
    if os.path.basename(words[0]) != "gh":
        return None
    sub = _non_flags(words[1:], {"-R", "--repo", "--hostname"})
    if sub[:2] == ["pr", "merge"]:
        return f"Merging by hand skips the CI gate. {LAND_HINT}"
    if sub[:1] == ["repo"] and sub[1:2] and sub[1] in ("delete", "archive", "rename", "edit"):
        return REPO_HINT
    if sub[:1] != ["api"]:
        return None
    method, endpoint, body = None, None, []
    for flag, value in _options(words[words.index("api") + 1:], _GH_API_VALUE):
        if flag in ("-X", "--method"):
            method = value
        elif flag in _GH_FIELDS:
            body.append(_field_text(value, flag, where))
        elif flag is None and endpoint is None:
            endpoint = value
    if endpoint is None:
        return None
    return check_api(method or ("POST" if body else "GET"), endpoint, body, where)


def _field_text(value: str, flag: str, where: str | None) -> str:
    """The body text a field or --input carries; for --input, the file's contents when readable."""
    if flag != "--input" or value == "-":
        return value
    try:
        with open(os.path.join(where or os.getcwd(), value), encoding="utf-8") as fh:
            return fh.read(65536)
    except OSError:
        return value


_CURL_DATA = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "--json",
              "-F", "--form", "--form-string"}
_CURL_UPLOAD = {"-T", "--upload-file"}
_CURL_VALUE = {"-X", "--request", "-H", "--header", "-o", "--output", "-u", "--user", "-A", "--user-agent",
               "-e", "--referer", "-b", "--cookie", "-c", "--cookie-jar", "-K", "--config", "--url",
               "-w", "--write-out", "-m", "--max-time", "--connect-timeout", "-x", "--proxy", "--retry",
               "-r", "--range", "-E", "--cert", "--cacert", "--key", "--oauth2-bearer", "--resolve",
               "--connect-to", "-D", "--dump-header"} | _CURL_DATA | _CURL_UPLOAD


def check_curl(words: list[str], where: str | None) -> str | None:
    if os.path.basename(words[0]) != "curl":
        return None
    method, urls, body, upload, get = None, [], [], False, False
    for flag, value in _options(words[1:], _CURL_VALUE):
        if flag in ("-X", "--request"):
            method = value
        elif flag in _CURL_DATA:
            body.append(value)
        elif flag in _CURL_UPLOAD:
            upload = True
        elif flag in ("--url", None):
            urls.append(value)
        elif flag in ("-G", "--get"):
            get = True
    if method is None:
        method = "PUT" if upload else ("POST" if body and not get else "GET")
    for url in urls:
        if "api.github.com" in url.lower() or "repos/" in url or url.rstrip("/").endswith("graphql"):
            reason = check_api(method, url, body, where)
            if reason:
                return reason
    return None


def _supabase_command(args: list[str]) -> tuple[str | None, str | None]:
    values = {"-s", "--schema", "-p", "--password", "-f", "--file", "--workdir", "--project-ref",
              "--version", "-o", "--output", "--db-url", "--status"}
    for i, a in enumerate(args):
        if a in ("db", "migration", "migrations"):
            rest = _non_flags(args[i + 1:], values)
            return a, rest[0] if rest else None
    return None, None


def check_database(cmd: Simple) -> str | None:
    words = cmd.words
    head = os.path.basename(words[0]).split("@", 1)[0]
    if head in ("pnpm", "yarn", "bun") and len(words) > 1 and words[1].split("@", 1)[0] == "supabase":
        words, head = words[1:], "supabase"
    if head == "supabase":
        args = words[1:]
        local, linked = "--local" in args, "--linked" in args
        if any(a == "--db-url" or a.startswith("--db-url=") for a in args):
            return DB_HINT
        group, action = _supabase_command(args)
        if group == "db" and action in ("push", "pull", "dump") and not local:
            return DB_HINT
        if group in ("migration", "migrations") and action in ("repair", "fetch") and not local:
            return DB_HINT
        if linked and ((group == "db" and action == "reset")
                       or (group in ("migration", "migrations") and action in ("up", "down"))):
            return DB_HINT
        return None
    if head in _DB_CLIENTS and any(_SUPABASE_HOST.search(w) for w in words[1:] + cmd.assigns):
        return "Direct connections to a Supabase database are not made from cloud sessions."
    values = words[1:] + [a.split("=", 1)[1] for a in cmd.assigns]
    if head not in ("echo", "printf") and any(_PG_URL.match(v) for v in values):
        return "Direct connections to a Supabase database are not made from cloud sessions."
    return None


def _decide(command: str, cwd: str | None) -> str | None:
    here = cwd
    moved: dict[str, object] = {}  # repo -> branch an earlier command switched to (None: unknown)
    for cmd in segments(command):
        head = os.path.basename(cmd.words[0])
        if head in ("cd", "pushd"):
            here = _chdir(here, cmd.words[1:])
            continue
        if head == "gh" and cmd.words[1:3] == ["pr", "checkout"]:
            moved[_repo_key(here)] = None
        reason = (check_git(cmd, here, moved) or check_gh(cmd.words, here)
                  or check_curl(cmd.words, here) or check_database(cmd))
        if reason:
            return reason
    return None


def decide(command: str, cwd: str | None) -> str | None:
    try:
        return _decide(command, cwd)
    except Exception:  # odd input must not switch the guard off for risky commands
        if any(w in command for w in _RISKY_WORDS):
            return "Could not read this command safely. Split it into simple commands without nested quoting."
        return None


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
