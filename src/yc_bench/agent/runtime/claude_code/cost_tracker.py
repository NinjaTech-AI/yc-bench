"""Per-turn cost + token accounting via the LiteLLM proxy.

Claude Code is routed through a LiteLLM proxy (set via ANTHROPIC_BASE_URL). The
proxy logs every API call with cost and token usage. We snapshot cumulative
spend before each turn and after, and report the diff as the turn's cost.

Best-effort: if the proxy is unreachable or its schema changes, the runtime
logs a warning and returns zeros. The proxy's own DB remains the authoritative
source for post-hoc analysis.

LiteLLM proxy endpoints used:
    GET /spend/logs?api_key=<key>   — list of recent calls with cost+tokens
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CostSnapshot:
    total_cost_usd: float
    prompt_tokens: int
    completion_tokens: int

    def diff(self, prior: "CostSnapshot") -> "CostSnapshot":
        return CostSnapshot(
            total_cost_usd=max(0.0, self.total_cost_usd - prior.total_cost_usd),
            prompt_tokens=max(0, self.prompt_tokens - prior.prompt_tokens),
            completion_tokens=max(0, self.completion_tokens - prior.completion_tokens),
        )

    @classmethod
    def zero(cls) -> "CostSnapshot":
        return cls(0.0, 0, 0)


def _proxy_base_url() -> str | None:
    return (
        os.environ.get("LITELLM_PROXY_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
    )


def _proxy_api_key() -> str | None:
    return (
        os.environ.get("LITELLM_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )


def snapshot() -> CostSnapshot:
    """Snapshot cumulative spend + tokens from the LiteLLM proxy.

    Returns a zero snapshot on any error. Logs a warning at most once per
    distinct error string.
    """
    base = _proxy_base_url()
    key = _proxy_api_key()
    if not base:
        return CostSnapshot.zero()

    try:
        url = base.rstrip("/") + "/spend/logs"
        params = {}
        if key:
            params["api_key"] = key
        resp = httpx.get(url, params=params, timeout=5.0)
        resp.raise_for_status()
        logs = resp.json()
    except Exception as exc:
        _warn_once(f"snapshot failed: {exc}")
        return CostSnapshot.zero()

    total_cost = 0.0
    prompt = 0
    completion = 0
    if not isinstance(logs, list):
        _warn_once(f"unexpected /spend/logs shape: {type(logs).__name__}")
        return CostSnapshot.zero()

    for row in logs:
        try:
            total_cost += float(row.get("spend", 0) or 0)
            usage = row.get("usage_object") or row.get("usage") or {}
            prompt += int(usage.get("prompt_tokens", 0) or 0)
            completion += int(usage.get("completion_tokens", 0) or 0)
        except (TypeError, ValueError):
            continue

    return CostSnapshot(total_cost, prompt, completion)


_warned: set[str] = set()


def _warn_once(msg: str) -> None:
    if msg in _warned:
        return
    _warned.add(msg)
    logger.warning("cost_tracker: %s", msg)


__all__ = ["CostSnapshot", "snapshot"]
