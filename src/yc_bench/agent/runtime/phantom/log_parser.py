"""Parse the yc_bench_calls.jsonl log written by yc_bench_wrapper.py.

The bench needs two things from this log after each phantom turn:
  - The list of (command, result) pairs claude executed → tool_calls in transcript.
  - The resume_payload from `yc-bench sim resume`, if any → terminal-state checks.

Events come in pairs: a "call" event followed by a "result" event, tied by pid.
A wrapper invocation can produce only a "call" if the wrapper crashes mid-flight;
we silently drop unpaired entries.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def extract_turn_calls(log_path: Path | str, turn_id: int | str) -> list[dict[str, Any]]:
    """Return tool-call records for one turn, in execution order.

    Each record: {"command": str, "stdout": str, "stderr": str, "exit_code": int, "duration_ms": int}.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return []

    turn_id_str = str(turn_id)
    calls: dict[int, dict[str, Any]] = {}  # pid → call event
    results: list[dict[str, Any]] = []  # paired records in order

    with log_path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed line in %s: %r", log_path, raw[:120])
                continue
            if str(ev.get("turn_id")) != turn_id_str:
                continue
            pid = ev.get("pid")
            if ev.get("event") == "call":
                calls[pid] = ev
            elif ev.get("event") == "result":
                call = calls.pop(pid, None)
                results.append(
                    {
                        "command": ev.get("command", ""),
                        "stdout": ev.get("stdout", ""),
                        "stderr": ev.get("stderr", ""),
                        "exit_code": ev.get("exit_code", -1),
                        "duration_ms": ev.get("duration_ms", 0),
                        "ts": ev.get("ts"),
                    }
                )

    if calls:
        logger.warning(
            "%d unpaired call events in turn %s (wrapper may have crashed): %s",
            len(calls),
            turn_id_str,
            [c.get("command") for c in calls.values()],
        )
    return results


def extract_resume_payload(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the LAST `yc-bench sim resume` call with parseable JSON stdout.

    Claude (autonomous) may call `sim resume` many times inside one bench turn.
    The bench's terminal-state checks key off "is the sim terminal NOW?" — i.e.
    the state after claude's most recent advance. Returning the FIRST resume's
    payload would miss horizon_end / bankruptcy that fired later in the turn,
    costing one extra wasted turn before clean termination.
    """
    latest: dict[str, Any] | None = None
    for call in calls:
        cmd = call.get("command", "")
        if not cmd.startswith("yc-bench sim resume"):
            continue
        stdout = call.get("stdout") or ""
        if not stdout.strip():
            continue
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("sim resume stdout not JSON-parseable: %r", stdout[:200])
            continue
        if isinstance(payload, dict):
            latest = payload
    return latest


__all__ = ["extract_turn_calls", "extract_resume_payload"]
