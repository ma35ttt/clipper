"""Update clipper in place from its own git remote.

The tool is meant to be cloned and run from source, so "updating" is just: pull
the newest commits, then restart. This module keeps that one step honest about
what it actually found — a folder that was unzipped instead of cloned, a
detached checkout, a branch with local edits — instead of surfacing a raw git
error nobody can act on.

It never deletes anything and never force-resets: if the tree cannot
fast-forward cleanly it stops and says so, because throwing away local changes
is the one thing a self-updater must not do.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import __version__

# The repository root is the parent of the package directory.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BRANCH = "main"
UPSTREAM = "https://github.com/ma35ttt/clipper"

_VERSION = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


def _git(args: list[str], timeout: float = 90.0) -> subprocess.CompletedProcess | None:
    if shutil.which("git") is None:
        return None
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _out(proc: subprocess.CompletedProcess | None) -> str:
    return (proc.stdout or "").strip() if proc is not None else ""


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def is_repo() -> bool:
    """True when this folder is a git working tree we can pull into."""
    return _out(_git(["rev-parse", "--is-inside-work-tree"])) == "true"


def _rev(ref: str) -> str:
    return _out(_git(["rev-parse", "--verify", "--quiet", ref]))


def _version_at(ref: str) -> str:
    """Read the clipper version recorded at a ref, without checking it out."""
    text = (_git(["show", f"{ref}:clipper/__init__.py"], timeout=30) or
            subprocess.CompletedProcess([], 1, "", ""))
    match = _VERSION.search(text.stdout or "")
    return match.group(1) if match else ""


def check(log=lambda message: None) -> dict:
    """Fetch the remote and report whether an update is waiting. Pulls nothing."""
    return _update(log=log, pull=False)


def run(log=lambda message: None) -> dict:
    """Fetch the remote and fast-forward to it. Never discards local work."""
    return _update(log=log, pull=True)


def _update(*, log, pull: bool) -> dict:
    result = {
        "ok": True, "before": __version__, "after": __version__,
        "updated": False, "available": False, "restart": False,
        "git": True, "message": "",
    }

    if shutil.which("git") is None:
        result.update(ok=False, git=False,
                      message="git is not installed — get the latest version from "
                              f"{UPSTREAM}")
        return result
    if not is_repo():
        result.update(ok=False, git=False,
                      message="this copy was not cloned with git — download the "
                              f"latest ZIP from {UPSTREAM} and replace the folder")
        return result

    fetch = _git(["fetch", "--prune", "origin"], timeout=180)
    if fetch is None or fetch.returncode != 0:
        result.update(ok=False,
                      message="could not reach GitHub: "
                              + (_last_line((fetch.stderr if fetch else "") or "")
                                 or "network unavailable"))
        return result

    branch = _out(_git(["rev-parse", "--abbrev-ref", "HEAD"])) or DEFAULT_BRANCH
    if branch == "HEAD":            # detached checkout: follow whatever origin/HEAD is
        branch = DEFAULT_BRANCH
    upstream = f"origin/{branch}"
    if not _rev(upstream):
        upstream = _out(_git(["symbolic-ref", "--quiet", "--short",
                              "refs/remotes/origin/HEAD"])) or f"origin/{DEFAULT_BRANCH}"
    remote = _rev(upstream)
    if not remote:
        result.update(ok=False,
                      message=f"no branch to update from (origin/{branch} not found)")
        return result

    local = _rev("HEAD")
    remote_version = _version_at(remote) or __version__
    if local == remote:
        result.update(message=f"already up to date — v{__version__}")
        return result

    ahead = _out(_git(["rev-list", "--count", f"{remote}..HEAD"]))
    if ahead and ahead != "0":
        result.update(ok=False,
                      message="your copy has local commits; update it with git "
                              "(clipper will not overwrite them)")
        return result

    result["available"] = True
    if not pull:
        result.update(
            after=remote_version, available=True,
            message=f"update available — v{__version__} → v{remote_version}",
        )
        return result

    dirty = bool(_out(_git(["status", "--porcelain"])))
    log(f"updating clipper — v{__version__} → v{remote_version}")
    pulled = _git(["pull", "--ff-only", upstream], timeout=300)
    if pulled is None or pulled.returncode != 0:
        hint = " (uncommitted changes block the update)" if dirty else ""
        result.update(ok=False,
                      message="could not fast-forward"
                              + hint + ": "
                              + (_last_line((pulled.stderr if pulled else "") or "")))
        return result

    after = _version_at("HEAD") or remote_version
    result.update(updated=True, after=after, restart=True,
                  message=f"updated v{__version__} → v{after} — restart to use it")
    log(result["message"])
    return result


def version_line() -> str:
    return f"clipper {__version__} · {UPSTREAM}"
