"""Monkey-patches to let phantom's orchestrator run on macOS host.

Phantom was designed for a Linux container. Three things in `orchestrator.py`
would otherwise misbehave on host:

  1. `ensure_settings_file()` reads `~/.claude/settings.json` for an `env`
     block, then WRITES `<phantom_repo>/settings.json` — touching phantom's
     source tree. We bypass it: PhantomRuntime passes ANTHROPIC_AUTH_TOKEN /
     ANTHROPIC_BASE_URL directly to the claude subprocess via env, so the
     file isn't needed.

  2. `upgrade_claude_cli()` runs `claude update` once per start. Harmless
     but slow on host and modifies the user's claude installation. Skip it.

  3. The `/workspace/logs` LOG_DIR (already handled by a one-line phantom
     source edit that respects PHANTOM_LOG_DIR; nothing for us to patch here).

Like the other patches, installs itself on import via sitecustomize.py.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _import_orchestrator():
    try:
        import orchestrator  # type: ignore[import-not-found]
        return orchestrator
    except Exception as exc:
        logger.debug("host_compat_patch: flat import of orchestrator failed: %s", exc)
    try:
        from phantom import orchestrator as _orch  # type: ignore[import-not-found]
        return _orch
    except Exception as exc:
        logger.debug("host_compat_patch: package import of orchestrator failed: %s", exc)
        return None


def install() -> None:
    orch = _import_orchestrator()
    if orch is None:
        logger.debug("host_compat_patch: orchestrator not importable yet")
        return

    if getattr(orch, "_yc_bench_host_compat_patched", False):
        return

    def patched_ensure_settings_file(logger_=None) -> bool:
        """No-op replacement. We pass ANTHROPIC env vars to claude via
        subprocess.env in run_agent_patch, so phantom's settings.json file
        isn't load-bearing for the bench."""
        return True

    def patched_upgrade_claude_cli(logger_=None, timeout: int = 60) -> None:
        """No-op replacement. We don't want phantom to run `claude update` on
        the user's host claude install on every turn."""
        return None

    orch.ensure_settings_file = patched_ensure_settings_file
    orch.upgrade_claude_cli = patched_upgrade_claude_cli
    orch._yc_bench_host_compat_patched = True
    logger.info("host_compat_patch: orchestrator settings/upgrade calls neutralized")


install()
