"""Spawn phantom's orchestrator.py as a subprocess and capture its result.

The orchestrator is treated as a black box per bench turn: build env, spawn,
block on exit, return stdout/stderr/exit_code. PhantomRuntime layers retry
logic and result parsing on top.

We invoke orchestrator.py directly (not `python -m phantom.orchestrator`)
because phantom's modules import each other flat-style (`from slack_interface
import ...`) — PYTHONPATH includes phantom's source dir so this just works.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestratorResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def run_orchestrator(
    phantom_repo: Path,
    python_bin: str,
    task: str,
    env: dict[str, str],
    timeout_seconds: float,
) -> OrchestratorResult:
    """Spawn `python phantom_launch.py --task <task>` and wait for it.

    We go through phantom_launch.py (not orchestrator.py directly) so the
    monkey-patches our sitecustomize installs into the `orchestrator` module
    are still in effect when main() runs. Invoking orchestrator.py as a
    script would make Python load it as __main__ — a separate module
    instance that sees none of the patches.
    """
    orchestrator_py = phantom_repo / "orchestrator.py"
    if not orchestrator_py.exists():
        return OrchestratorResult(
            ok=False,
            exit_code=127,
            stdout="",
            stderr=f"orchestrator.py not found at {orchestrator_py}",
            duration_seconds=0.0,
        )

    launcher = Path(__file__).with_name("phantom_launch.py").resolve()
    argv = [python_bin, str(launcher), "--task", task]
    logger.info(
        "Spawning orchestrator: python=%s task=%r timeout=%ss",
        python_bin,
        task[:80] + ("..." if len(task) > 80 else ""),
        timeout_seconds,
    )

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(phantom_repo),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        logger.warning("Orchestrator timed out after %.1fs", duration)
        return OrchestratorResult(
            ok=False,
            exit_code=124,
            stdout=(exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=f"orchestrator timed out after {timeout_seconds}s",
            duration_seconds=duration,
        )
    except Exception as exc:  # pragma: no cover — defensive
        return OrchestratorResult(
            ok=False,
            exit_code=1,
            stdout="",
            stderr=f"orchestrator spawn error: {exc}",
            duration_seconds=time.monotonic() - start,
        )

    duration = time.monotonic() - start
    logger.info(
        "Orchestrator exited rc=%d in %.1fs (stdout=%dB, stderr=%dB)",
        proc.returncode,
        duration,
        len(proc.stdout or ""),
        len(proc.stderr or ""),
    )

    return OrchestratorResult(
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_seconds=duration,
    )


__all__ = ["OrchestratorResult", "run_orchestrator"]
