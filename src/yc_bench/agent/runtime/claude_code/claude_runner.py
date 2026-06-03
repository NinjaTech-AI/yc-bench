"""Spawn `claude -c -p <prompt>` as a subprocess and capture its result.

Claude is treated as a black box per bench turn: build env, spawn, block on
exit, return stdout/stderr/exit_code. ClaudeCodeRuntime layers retry logic and
result parsing on top.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaudeResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def _resolve_claude() -> str:
    """Find claude on PATH; respect CLAUDE_BIN override if set."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    raise RuntimeError(
        "claude binary not found on PATH and CLAUDE_BIN env is unset. "
        "Install claude-code (npm i -g @anthropic-ai/claude-code) or set CLAUDE_BIN."
    )


def run_claude(
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    model: str | None = None,
) -> ClaudeResult:
    """Spawn `claude -c -p --dangerously-skip-permissions [--model M] <prompt>` and wait.

    `-c` continues the most recent session in $HOME/.claude/ — for turn 1 there
    is no prior session, so claude starts fresh; subsequent turns resume. The
    HOME redirect in ClaudeCodeSession ensures per-seed isolation.

    `--dangerously-skip-permissions` is required in headless `-p` mode:
    otherwise claude auto-denies Bash + Edit tool calls and we'd see zero
    yc-bench calls in the log.

    `--model <M>` pins the inner model. Without it, claude CLI uses its own
    default (currently opus 4.8), which would silently ignore the suffix in
    our `claude-code/<inner>` model spec.
    """
    claude_bin = _resolve_claude()
    argv = [claude_bin, "-c", "-p", "--dangerously-skip-permissions"]
    if model:
        argv.extend(["--model", model])
    argv.append(prompt)

    logger.info(
        "Spawning claude: bin=%s cwd=%s timeout=%ss prompt_len=%d",
        claude_bin,
        cwd,
        timeout_seconds,
        len(prompt),
    )

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        logger.warning("Claude timed out after %.1fs", duration)
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        return ClaudeResult(
            ok=False,
            exit_code=124,
            stdout=stdout,
            stderr=f"claude timed out after {timeout_seconds}s",
            duration_seconds=duration,
        )
    except FileNotFoundError as exc:
        return ClaudeResult(
            ok=False,
            exit_code=127,
            stdout="",
            stderr=f"claude binary not found: {exc}",
            duration_seconds=time.monotonic() - start,
        )
    except Exception as exc:  # pragma: no cover — defensive
        return ClaudeResult(
            ok=False,
            exit_code=1,
            stdout="",
            stderr=f"claude spawn error: {exc}",
            duration_seconds=time.monotonic() - start,
        )

    duration = time.monotonic() - start
    logger.info(
        "Claude exited rc=%d in %.1fs (stdout=%dB, stderr=%dB)",
        proc.returncode,
        duration,
        len(proc.stdout or ""),
        len(proc.stderr or ""),
    )

    return ClaudeResult(
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_seconds=duration,
    )


__all__ = ["ClaudeResult", "run_claude"]
