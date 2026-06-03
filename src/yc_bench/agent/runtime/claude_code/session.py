"""Per-session filesystem layout and subprocess environment for ClaudeCodeRuntime.

A session corresponds to one bench run (one seed × one model). The session owns:

  db/<slug>.claude_code/
    ├── yc_bench_calls.jsonl    — yc_bench_wrapper.py writes; runtime reads
    ├── wrapper_bin/
    │     └── yc-bench          — symlink to yc_bench_wrapper.py; prepended to PATH
    └── home/                   — used as $HOME for the claude subprocess
          └── .claude/          — claude-code's own session storage (per-seed)

Parallel-safe: each session has its own dir keyed by slug.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# yc_bench_wrapper.py reads these.
_ENV_REAL_BIN = "YC_BENCH_REAL_BIN"
_ENV_CALLS_LOG = "YC_BENCH_CALLS_LOG"
_ENV_TURN_ID = "YC_BENCH_TURN_ID"


def _safe_slug(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", session_id)


def _resolve_real_yc_bench() -> Path:
    """Find the real yc-bench script in the same venv as the current Python.

    Mirrors phantom adapter's resolver: raises rather than falling back to PATH
    lookup, because our wrapper bin shadows 'yc-bench' on PATH — a fallback
    would self-recurse.
    """
    venv_bin = Path(sys.executable).parent
    candidate = venv_bin / "yc-bench"
    if candidate.exists():
        return candidate.resolve()
    raise RuntimeError(
        f"Could not find yc-bench binary next to {sys.executable}. "
        "ClaudeCodeRuntime needs an unambiguous real-binary path so its "
        "wrapper can shadow PATH without recursing."
    )


class ClaudeCodeSession:
    """One session's filesystem layout + subprocess-env builder."""

    def __init__(self, session_id: str, db_dir: Path | None = None):
        self.session_id = session_id
        self.slug = _safe_slug(session_id)
        self.db_dir = (db_dir or Path("db")).resolve()
        self.root = self.db_dir / f"{self.slug}.claude_code"

        self._real_yc_bench: Path | None = None

    @property
    def calls_log_path(self) -> Path:
        return self.root / "yc_bench_calls.jsonl"

    @property
    def wrapper_bin_dir(self) -> Path:
        return self.root / "wrapper_bin"

    @property
    def home_dir(self) -> Path:
        return self.root / "home"

    @property
    def work_dir(self) -> Path:
        """cwd for the claude subprocess. We use the session root so claude
        can't accidentally read bench-source files outside it. The bench DB
        is opened via the absolutized DATABASE_URL, not via cwd."""
        return self.root

    def stage(self) -> None:
        """Create the per-session dirs and stage the wrapper symlink.

        Idempotent: safe to call once per turn.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self.wrapper_bin_dir.mkdir(exist_ok=True)
        self.home_dir.mkdir(exist_ok=True)
        (self.home_dir / ".claude").mkdir(exist_ok=True)

        # wrapper_bin/yc-bench → yc_bench_wrapper.py (re-stage every time so
        # source-tree relocations are picked up).
        wrapper_src = Path(__file__).with_name("yc_bench_wrapper.py").resolve()
        wrapper_link = self.wrapper_bin_dir / "yc-bench"
        if wrapper_link.is_symlink() or wrapper_link.exists():
            wrapper_link.unlink()
        wrapper_link.symlink_to(wrapper_src)

    def reset_calls_log(self) -> None:
        if self.calls_log_path.is_file():
            self.calls_log_path.unlink()

    def cleanup(self) -> None:
        import shutil

        if self.root.is_dir():
            shutil.rmtree(self.root, ignore_errors=True)

    def build_subprocess_env(
        self,
        turn_id: int,
        base: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the env dict for a claude subprocess.

        Prepends wrapper_bin/ to PATH so 'yc-bench' resolves to our wrapper.
        Sets HOME to the per-session home/ for claude session-storage isolation.
        Absolutizes DATABASE_URL so claude's cwd doesn't break the SQLite path.
        """
        if self._real_yc_bench is None:
            self._real_yc_bench = _resolve_real_yc_bench()

        env = dict(base if base is not None else os.environ)

        env["PATH"] = f"{self.wrapper_bin_dir}{os.pathsep}{env.get('PATH', '')}"

        env[_ENV_REAL_BIN] = str(self._real_yc_bench)
        env[_ENV_CALLS_LOG] = str(self.calls_log_path)
        env[_ENV_TURN_ID] = str(turn_id)

        # DATABASE_URL bench-side is "sqlite:///db/<base>.db" (relative). Claude's
        # subprocess runs with cwd=self.work_dir, so a relative path would resolve
        # to <work_dir>/db/... — yc-bench commands would mutate a session-local
        # DB instead of the bench's actual SQLite. Convert relative sqlite URLs
        # to absolute (anchored at the current cwd, which IS the bench's working
        # dir at the point this env is built).
        db_url = env.get("DATABASE_URL")
        if db_url and db_url.startswith("sqlite:///") and not db_url.startswith("sqlite:////"):
            rel_path = db_url[len("sqlite:///") :]
            abs_path = (Path.cwd() / rel_path).resolve()
            env["DATABASE_URL"] = f"sqlite:///{abs_path}"

        # Per-session HOME so claude's `~/.claude/` session storage is isolated
        # per seed. `claude -c` will pick up the correct prior-turn session.
        env["HOME"] = str(self.home_dir)

        return env


__all__ = ["ClaudeCodeSession"]
