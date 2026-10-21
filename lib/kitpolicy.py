"""Landing policy shared by `land` and `cloud-prep`.

Auto-merge only for allowlisted owners (LAND_OWNERS, default "wiggdevin ZeroSum-Solutions")
and never for repositories listed in policy/client-repos.txt (client repos, plus repos that
always need a human review, such as this kit). The file holds shell globs
(`wiggdevin/client-*`), plain owner/repo names for non-client entries, and, so the public kit
does not name clients, `sha256:<hex>` lines: the SHA-256 of the lowercased owner/repo.
LAND_CLIENT_REPOS adds entries (comma/space separated).

The policy fails closed: if the file cannot be read, nothing auto-merges.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from pathlib import Path

DEFAULT_OWNERS = "wiggdevin ZeroSum-Solutions"


class PolicyError(Exception):
    """The policy file is missing or unreadable."""


def split_names(raw: str | None) -> set[str]:
    """Owner or repo names: separated by commas or whitespace (names contain neither)."""
    return {n for n in re.split(r"[,\s]+", raw or "") if n}


def split_list(raw: str | None) -> set[str]:
    """Check names: separated by commas or newlines only, since names can contain spaces."""
    return {n.strip() for n in re.split(r"[,\n]+", raw or "") if n.strip()}


def load_clients(path: Path) -> set[str]:
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        raise PolicyError(f"cannot read {path}: {e.strerror or e}") from e
    entries = {ln.split("#", 1)[0].strip().lower() for ln in lines}
    entries |= {n.lower() for n in split_names(os.environ.get("LAND_CLIENT_REPOS"))}
    entries.discard("")
    if not entries:
        raise PolicyError(f"{path} has no entries")
    return entries


def is_client(full_name: str, clients: set[str]) -> bool:
    name = full_name.lower()
    digest = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
    return digest in clients or any(
        not c.startswith("sha256:") and fnmatch.fnmatchcase(name, c) for c in clients
    )


def merge_policy(full_name: str, owners: set[str], clients: set[str],
                 aliases: tuple[str, ...] = ()) -> tuple[bool, str]:
    """aliases: other names for the same repo (the origin URL before a rename)."""
    for name in (full_name, *aliases):
        if is_client(name, clients):
            return False, f"{full_name} is on the human-review list (client repo)"
    owner = full_name.split("/", 1)[0]
    if owner.lower() not in {o.lower() for o in owners}:
        return False, f"owner {owner} is not in the auto-merge allowlist"
    return True, "owner allowlisted"


def policy_for(full_name: str, kit_dir: Path, aliases: tuple[str, ...] = ()) -> tuple[bool, str]:
    owners = split_names(os.environ.get("LAND_OWNERS", DEFAULT_OWNERS))
    try:
        clients = load_clients(kit_dir / "policy" / "client-repos.txt")
    except PolicyError as e:
        return False, f"client policy unreadable ({e}), so nothing auto-merges"
    return merge_policy(full_name, owners, clients, aliases)
