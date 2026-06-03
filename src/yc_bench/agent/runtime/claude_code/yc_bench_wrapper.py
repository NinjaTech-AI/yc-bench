#!/usr/bin/env python3
"""yc-bench wrapper — sits on claude's PATH ahead of the real binary.

When claude (driven by ClaudeCodeRuntime) runs `yc-bench <args>`, this wrapper:
  1. Appends a "call" event to YC_BENCH_CALLS_LOG (JSONL).
  2. Executes the real binary at YC_BENCH_REAL_BIN with the same args.
  3. Captures stdout/stderr/exit_code and appends a "result" event.
  4. Forwards stdout/stderr/exit_code to the caller transparently.

The bench reads YC_BENCH_CALLS_LOG after each turn (filtered by YC_BENCH_TURN_ID)
to reconstruct what claude did — including detecting `yc-bench sim resume` for
terminal-state checks.

Required env (set by ClaudeCodeRuntime before spawning claude):
  YC_BENCH_REAL_BIN   absolute path to the real yc-bench entry point
  YC_BENCH_CALLS_LOG  absolute path to the JSONL log file
  YC_BENCH_TURN_ID    integer turn id (tagged on every event for filtering)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _env_or_die(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(
            f"yc-bench-wrapper: missing required env var {name}.\n"
            "This wrapper must be invoked by ClaudeCodeRuntime, not directly.\n"
        )
        sys.exit(2)
    return value


def _append_event(log_path: str, event: dict) -> None:
    # O_APPEND on POSIX guarantees atomic writes up to PIPE_BUF (~4KB), so a
    # single-line JSON event won't interleave with concurrent wrapper calls.
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def main() -> int:
    real_bin = _env_or_die("YC_BENCH_REAL_BIN")
    log_path = _env_or_die("YC_BENCH_CALLS_LOG")
    turn_id = _env_or_die("YC_BENCH_TURN_ID")

    # Self-recursion guard. If YC_BENCH_REAL_BIN resolves to this same script
    # (e.g. PATH was shadowed before the env was set), running it would loop
    # forever and exhaust the process table. Bail loudly instead.
    real_resolved = Path(real_bin).resolve()
    self_resolved = Path(__file__).resolve()
    if real_resolved == self_resolved:
        sys.stderr.write(
            f"yc-bench-wrapper: YC_BENCH_REAL_BIN ({real_bin}) resolves to this "
            "wrapper, not the real binary. Refusing to recurse.\n"
        )
        sys.exit(2)

    # Ensure log dir exists (idempotent).
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    args = sys.argv[1:]
    pid = os.getpid()
    command_str = "yc-bench " + " ".join(args) if args else "yc-bench"

    _append_event(
        log_path,
        {
            "event": "call",
            "turn_id": turn_id,
            "pid": pid,
            "ts": time.time(),
            "command": command_str,
            "argv": args,
        },
    )

    start = time.monotonic()
    try:
        proc = subprocess.run(
            [real_bin, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
    except FileNotFoundError as exc:
        stdout, stderr, exit_code = "", f"yc-bench-wrapper: real binary not found: {exc}", 127
    except Exception as exc:  # pragma: no cover — defensive
        stdout, stderr, exit_code = "", f"yc-bench-wrapper: subprocess error: {exc}", 1

    duration_ms = int((time.monotonic() - start) * 1000)

    _append_event(
        log_path,
        {
            "event": "result",
            "turn_id": turn_id,
            "pid": pid,
            "ts": time.time(),
            "command": command_str,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
        },
    )

    # Forward to caller transparently.
    sys.stdout.write(stdout)
    sys.stdout.flush()
    sys.stderr.write(stderr)
    sys.stderr.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
