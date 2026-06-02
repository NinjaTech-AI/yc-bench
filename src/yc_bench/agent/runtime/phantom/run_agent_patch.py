"""Monkey-patch `orchestrator.run_agent` to invoke `claude` directly.

Production phantom routes through `claude-wrapper.sh`, which is Linux-specific:
hardcoded `/root/.local/bin/claude`, hardcoded `HOME=/root`, and uses Linux
`script -c "..."` syntax that macOS `script` doesn't accept. CL-Bench's
Dockerfile already bypassed the wrapper for the same reason — we follow that
precedent on host.

The replacement uses the same flags `-c -p <prompt>` and the same cwd
(REPO_ROOT), and inherits ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN from the
bench process. No `--settings` flag — claude uses its defaults; phantom's
settings.json wasn't load-bearing for this benchmark.

Like the other patches, installs itself on import via sitecustomize.py.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


def _import_orchestrator():
    try:
        import orchestrator  # type: ignore[import-not-found]
        return orchestrator
    except Exception as exc:
        logger.debug("run_agent_patch: flat import of orchestrator failed: %s", exc)
    try:
        from phantom import orchestrator as _orch  # type: ignore[import-not-found]
        return _orch
    except Exception as exc:
        logger.debug("run_agent_patch: package import of orchestrator failed: %s", exc)
        return None


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


def install() -> None:
    orch = _import_orchestrator()
    if orch is None:
        logger.debug("run_agent_patch: orchestrator module not importable yet")
        return

    if getattr(orch, "_yc_bench_run_agent_patched", False):
        return

    def patched_run_agent(agent: dict, task: str = "") -> None:
        """Replacement for orchestrator.run_agent: call `claude` directly."""
        agent_logger = orch.setup_logging(agent["name"].lower())
        agent_logger.info(f"\n{'='*60}")
        agent_logger.info(f"{agent['emoji']} Starting {agent['name']} ({agent['role']})")
        agent_logger.info(f"{'='*60}\n")

        prompt = orch.build_prompt(agent, task)
        claude_bin = _resolve_claude()

        try:
            # --dangerously-skip-permissions mirrors what production phantom's
            # claude-wrapper.sh achieves via `--settings <file>` with
            # allow=["Edit(**)","Bash"]: claude is free to call Bash + Edit
            # without per-command approval. Required in headless `-p` mode,
            # which otherwise auto-denies tool use.
            result = subprocess.run(
                [claude_bin, "-c", "-p", "--dangerously-skip-permissions", prompt],
                cwd=str(orch.REPO_ROOT),
                # MUST stay STRICTLY LESS than orchestrator_runner's outer
                # timeout (in phantom.toml — currently 8100s). The inner
                # timeout fires on `claude` (direct child of this process),
                # so the kill cascades cleanly. If the outer fires first
                # it kills phantom_launch (parent of this process), leaving
                # claude orphaned and racing against retries — DB corruption.
                # 7200s = 120 min: enough for claude to play a full 1-year
                # game in one shot (seed 2 reached month 7.4 in 50 min).
                timeout=7200,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                agent_logger.info(f"Claude output:\n{result.stdout}")
            if result.stderr:
                agent_logger.warning(f"Claude stderr:\n{result.stderr}")
        except subprocess.TimeoutExpired:
            agent_logger.warning("⏰ Claude CLI timed out after 15 minutes")
        except FileNotFoundError:
            agent_logger.error("❌ Claude CLI not found! Set CLAUDE_BIN or install claude-code.")
            raise

        agent_logger.info(f"\n✅ {agent['name']} completed\n")

    orch.run_agent = patched_run_agent
    orch._yc_bench_run_agent_patched = True
    logger.info("run_agent_patch: orchestrator.run_agent replaced (claude=%s)", _resolve_claude())


install()
