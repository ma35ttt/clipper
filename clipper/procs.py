"""Stop a clipper process and everything it started.

A ``clipper run`` worker is a Python process that starts its own ffmpeg
ingest, an audio probe and render jobs. Killing only the Python parent
leaves those ffmpeg processes writing into the stream folder and burning
CPU until the machine is rebooted, which is exactly what "stop" must never
do. Every shutdown path goes through :func:`kill_tree`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

IS_WINDOWS = os.name == "nt"


def _table() -> dict[int, list[int]]:
    """One snapshot of the process table as ``pid -> children``."""
    children: dict[int, list[int]] = {}
    if IS_WINDOWS:
        return children
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return children
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return children


def descendants(root: int | None = None) -> list[int]:
    """Every live descendant of ``root`` (default: this process)."""
    root = os.getpid() if root is None else int(root)
    if IS_WINDOWS:
        return []
    children = _table()
    found: list[int] = []
    seen: set[int] = set()
    stack = list(children.get(root, ()))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        found.append(pid)
        stack.extend(children.get(pid, ()))
    return found


def alive(pid: int) -> bool:
    """True when the pid can still be signalled (zombies count as alive)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(pids: list[int], timeout: float = 3.0) -> list[int]:
    """SIGTERM every pid, then SIGKILL whatever is left after ``timeout``."""
    targets = sorted({int(pid) for pid in pids
                      if int(pid) > 0 and int(pid) != os.getpid()})
    if not targets:
        return []
    if IS_WINDOWS:
        for pid in targets:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        return targets

    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(alive(pid) for pid in targets):
            return targets
        time.sleep(0.05)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return targets


def kill_tree(root: int, timeout: float = 3.0) -> list[int]:
    """Stop ``root`` and every process below it. Returns the pids signalled.

    The descendants are collected *before* the parent is signalled: once the
    parent dies its children are reparented to init and can no longer be
    found from the tree.
    """
    root = int(root)
    targets = descendants(root)
    targets.append(root)
    return terminate(targets, timeout=timeout)
