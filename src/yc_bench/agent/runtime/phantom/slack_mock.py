"""Slack mock for the Phantom × YC-Bench adapter.

Phantom production hits slack.com via `SlackClient` (HTTP layer in
`slack_interface.py:1049`). For the bench we patch three methods at runtime so
they read/write local JSONL files instead:

    send_message          → append to <PHANTOM_MOCK_DIR>/outbox.jsonl
    get_channel_history   → merged view of inbox + outbox (chronological)
    get_thread_replies    → filtered inbox entries with matching thread_ts

Everything above `SlackClient` (the `SlackInterface` wrapper, the `slack_cli`
CLI) runs unmodified above the patch.

Installation is two-step:
    1. PhantomRuntime stages a `sitecustomize.py` into the per-session dir that
       imports this module at Python startup. PYTHONPATH ensures it's found.
    2. This module's `install()` is called on import — patches `SlackClient`
       once per Python process.

For the host adapter (PhantomRuntime side), use:
    write_inbound_message  — bench posts a turn message into the inbox
    read_outbound_messages — bench drains claude's replies after a turn
    reset_state            — wipe inbox/outbox between sessions
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _mock_dir() -> Path:
    """Per-session mock state directory. Set by PhantomRuntime via env var."""
    raw = os.environ.get("PHANTOM_MOCK_DIR")
    if not raw:
        raise RuntimeError(
            "PHANTOM_MOCK_DIR is not set. The slack mock cannot operate without "
            "a session-scoped state directory; PhantomRuntime must set this env "
            "var before spawning the orchestrator."
        )
    return Path(raw)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _patch_slack_client() -> None:
    """Replace three SlackClient HTTP methods with local-JSONL equivalents.

    Idempotent: safe to call multiple times in the same Python process.
    """
    try:
        from slack_interface import SlackClient  # type: ignore[import-not-found]
    except Exception as exc:
        logger.debug("slack_mock: slack_interface not importable yet (%s)", exc)
        return

    if getattr(SlackClient, "_yc_bench_patched", False):
        return

    def send_message(
        self,
        token: str,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        username: str | None = None,
        icon_emoji: str | None = None,
        icon_url: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        ts = f"{time.time():.6f}"
        _append_jsonl(
            _mock_dir() / "outbox.jsonl",
            {
                "text": text,
                "channel": channel,
                "thread_ts": thread_ts,
                "username": username,
                "ts": ts,
            },
        )
        return {
            "ok": True,
            "channel": channel,
            "ts": ts,
            "message": {"text": text, "ts": ts, "user": "U_PHANTOM"},
        }

    def get_channel_history(
        self,
        token: str,
        channel: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        # Merge bench-posted (inbox) and phantom-posted (outbox) so the channel
        # reads back like a real conversation; Slack returns newest-first.
        inbox = _read_jsonl(_mock_dir() / "inbox.jsonl")
        outbox = _read_jsonl(_mock_dir() / "outbox.jsonl")
        for msg in outbox:
            msg.setdefault("user", "U_PHANTOM")
            msg.setdefault("bot_id", "B_PHANTOM")
        merged = inbox + outbox
        merged.sort(key=lambda m: float(m.get("ts", 0)))
        return list(reversed(merged))[:limit]

    def get_thread_replies(
        self,
        token: str,
        channel: str,
        thread_ts: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        msgs = _read_jsonl(_mock_dir() / "inbox.jsonl")
        return [m for m in msgs if m.get("thread_ts") == thread_ts][:limit]

    SlackClient.send_message = send_message
    SlackClient.get_channel_history = get_channel_history
    SlackClient.get_thread_replies = get_thread_replies
    SlackClient._yc_bench_patched = True
    logger.info("slack_mock: patched SlackClient (mock_dir=%s)", _mock_dir())


def install() -> None:
    """Entry point used by sitecustomize.py. Idempotent.

    Silent when PHANTOM_MOCK_DIR is unset — the bench host process imports this
    module too, but only needs the helper functions (write_inbound_message
    etc., which take an explicit path). The patches are only meaningful inside
    the orchestrator subprocess, where PHANTOM_MOCK_DIR is always set.
    """
    if not os.environ.get("PHANTOM_MOCK_DIR"):
        return
    try:
        _mock_dir().mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("slack_mock: could not create mock dir (%s)", exc)
    _patch_slack_client()


# ---------------------------------------------------------------------------
# Host-side helpers (PhantomRuntime calls these — they do NOT depend on the
# patches above, they touch the JSONL files directly).
# ---------------------------------------------------------------------------


def write_inbound_message(
    text: str,
    mock_dir: Path,
    user: str = "U_BENCH",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Append a message to the inbox as if a slack user had posted it."""
    entry = {
        "user": user,
        "text": text,
        "ts": f"{time.time():.6f}",
        "id": uuid.uuid4().hex,
    }
    if thread_ts is not None:
        entry["thread_ts"] = thread_ts
    _append_jsonl(mock_dir / "inbox.jsonl", entry)
    return entry


def read_outbound_messages(mock_dir: Path) -> list[dict[str, Any]]:
    """Return all messages claude has posted via the patched send_message."""
    return _read_jsonl(mock_dir / "outbox.jsonl")


def reset_state(mock_dir: Path) -> None:
    """Wipe inbox and outbox. Call between sessions, NOT between turns —
    cross-turn continuity is the whole point of the slack channel."""
    for name in ("inbox.jsonl", "outbox.jsonl"):
        path = mock_dir / name
        if path.is_file():
            path.unlink()


# Auto-install when imported via sitecustomize.py.
install()
