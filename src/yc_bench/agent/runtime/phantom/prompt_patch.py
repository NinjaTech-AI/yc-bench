"""Monkey-patch `orchestrator.build_prompt` to use a YC-Bench-trimmed doc list.

Production phantom's `build_prompt()` (orchestrator.py:567) instructs claude to
`cat` 5 agent-docs: PHANTOM_SPEC.md, AGENT_PROTOCOL.md, SLACK_INTERFACE.md,
ORCHESTRATOR.md, PIPEDREAM_CONNECT.md.

For YC-Bench, only 3 are relevant (AGENT_PROTOCOL, SLACK_INTERFACE,
ORCHESTRATOR). PHANTOM_SPEC is browser-automation; PIPEDREAM is OAuth/integrations.
Dropping them saves input tokens and reduces attention dilution without touching
phantom's source.

Like slack_mock, this module installs itself on import via sitecustomize.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Single source of truth for the trimmed doc list. Edit here to A/B different
# subsets later; PhantomRuntime can also override via env (TODO).
_KEPT_DOCS: list[tuple[str, str]] = [
    ("Agent Protocol", "AGENT_PROTOCOL.md"),
    ("Slack Interface Docs", "SLACK_INTERFACE.md"),
    ("Workflow Docs", "ORCHESTRATOR.md"),
]


def _import_orchestrator():
    """Return the orchestrator module, trying both flat and package imports.

    Phantom's modules are written flat-style (`from slack_interface import ...`)
    so the typical PYTHONPATH layout puts /path/to/phantom/src/phantom directly
    on sys.path. We accept either.

    Catches Exception, not just ImportError — an in-progress phantom edit can
    surface as NameError / AttributeError at import time, and we want our other
    patches to still install rather than all of them dying together.
    """
    try:
        import orchestrator  # type: ignore[import-not-found]

        return orchestrator
    except Exception as exc:
        logger.debug("prompt_patch: flat import of orchestrator failed: %s", exc)
    try:
        from phantom import orchestrator as _orch  # type: ignore[import-not-found]

        return _orch
    except Exception as exc:
        logger.debug("prompt_patch: package import of orchestrator failed: %s", exc)
        return None


def _build_doc_list_block() -> str:
    """Render the numbered cat-instructions for the kept docs."""
    return "\n".join(
        f"{i}. **{label}:** `cat agent-docs/{filename}`"
        for i, (label, filename) in enumerate(_KEPT_DOCS, 1)
    )


def _patched_build_prompt(orch_module, agent: dict, task: str = "") -> str:
    """Replacement for orchestrator.build_prompt with the trimmed doc list.

    Reuses orchestrator.load_config() and orchestrator.read_file() so any
    upstream changes to those helpers are honored.
    """
    config = orch_module.load_config()
    channel = config.get("default_channel_name", config.get("default_channel", "#your-channel"))
    default_task = (
        f"Check Slack {channel} for new requests, do your work, update your "
        "memory file and reflect and improve your toolkit as per "
        "agent-docs/ORCHESTRATOR.md."
    )

    memory_path = orch_module.REPO_ROOT / "memory" / f"{agent['name'].lower()}_memory.md"
    memory = orch_module.read_file(memory_path)

    doc_list = _build_doc_list_block()

    return f"""# You are {agent['name']} {agent['emoji']}

## Your Identity
- **Name:** {agent['name']}
- **Role:** {agent['role']}
- **Emoji:** {agent['emoji']}

---

## 🚨 Environment Overrides (READ BEFORE THE DOCS)

This environment differs from production phantom in two ways. **Follow these
overrides instead of what the docs say**, in cases of conflict:

1. **Slack:** wherever the docs tell you to run `python slack_interface.py <subcmd>`,
   run `slack_cli <subcmd>` instead. Same flags, same behavior — but ONLY
   `slack_cli` works here. `python slack_interface.py` will silently return
   no messages because the underlying client isn't wired up for this run.
2. **Game actions:** run `yc-bench <subcmd>` (e.g. `yc-bench market browse`,
   `yc-bench task accept --task-id Task-42`) for every game action. Get the
   command list and game rules from the first slack message in #yc-bench —
   read that with `slack_cli read -c '#yc-bench' -l 50` before doing anything.

---

## Documentation Files (READ THESE FIRST)

You are currently running as the orchestrator agent. Before starting work, read these files for full context:

{doc_list}

---

## Your Memory

{memory if memory else "No previous memory. This is your first session."}

---

## Current Task

{task if task else default_task}
"""


def install() -> None:
    """Replace orchestrator.build_prompt with our trimmed version. Idempotent."""
    orch = _import_orchestrator()
    if orch is None:
        logger.debug("prompt_patch: orchestrator module not importable yet")
        return

    if getattr(orch, "_yc_bench_prompt_patched", False):
        return

    def wrapped(agent: dict, task: str = "") -> str:
        return _patched_build_prompt(orch, agent, task)

    orch.build_prompt = wrapped
    orch._yc_bench_prompt_patched = True
    logger.info(
        "prompt_patch: orchestrator.build_prompt replaced (kept docs: %s)",
        [f for _, f in _KEPT_DOCS],
    )


# Auto-install on import (via sitecustomize.py).
install()
