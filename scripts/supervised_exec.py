#!/usr/bin/env python3
"""Exec a command only while its exact parent process remains alive."""

from __future__ import annotations

import ctypes
import os
import signal
import sys


PR_SET_PDEATHSIG = 1
PARENT_CHANGED = 70
SUPERVISION_FAILED = 71


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: supervised_exec.py <parent-pid> <command> [args...]", file=sys.stderr)
        return SUPERVISION_FAILED
    try:
        expected_parent = int(argv[1])
    except ValueError:
        print("invalid parent pid", file=sys.stderr)
        return SUPERVISION_FAILED

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        print(f"could not set parent-death signal: {os.strerror(error)}", file=sys.stderr)
        return SUPERVISION_FAILED
    if os.getppid() != expected_parent:
        print("parent identity changed before supervision", file=sys.stderr)
        return PARENT_CHANGED

    try:
        os.execvpe(argv[2], argv[2:], os.environ)
    except OSError as exc:
        print(f"could not exec supervised command: {exc}", file=sys.stderr)
        return SUPERVISION_FAILED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
