"""Per-session filesystem layout and subprocess environment for PhantomRuntime.

A session corresponds to one bench run (one seed × one model). The session owns:

  db/<slug>.phantom/
    ├── inbox.jsonl             — bench writes; claude reads via patched SlackClient
    ├── outbox.jsonl            — claude writes; bench reads after each turn
    ├── yc_bench_calls.jsonl    — yc_bench_wrapper.py writes; bench reads
    ├── wrapper_bin/
    │     └── yc-bench          — symlink to yc_bench_wrapper.py; prepended to PATH
    ├── pyshim/
    │     └── sitecustomize.py  — imports the 4 monkey-patches at Python startup
    └── home/                   — used as $HOME for the orchestrator subprocess
          ├── .agent_settings.json   — stub bot_token so slack_interface bypasses live auth
          ├── ninja-squad/s3_config.json — stub creds so slack_interface import-check passes
          ├── .claude/                — claude-code's own session storage (kept session-scoped)
          └── workspace/logs/         — PHANTOM_LOG_DIR points here (phantom's hardcoded
                                        /workspace/logs is sourced from this env var)

Parallel-safe: each session has its own dir keyed by slug. v1 only ever has one.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Bench-source slack mock writes use this env var to find the per-session dir.
_ENV_MOCK_DIR = "PHANTOM_MOCK_DIR"
# yc_bench_wrapper.py reads these.
_ENV_REAL_BIN = "YC_BENCH_REAL_BIN"
_ENV_CALLS_LOG = "YC_BENCH_CALLS_LOG"
_ENV_TURN_ID = "YC_BENCH_TURN_ID"

# Bench-side configuration: where phantom's source lives on host.
_ENV_PHANTOM_REPO = "PHANTOM_REPO_PATH"


def _safe_slug(session_id: str) -> str:
    """Make session_id filesystem-safe. session_id can contain slashes
    (e.g. 'run-1-phantom/claude-opus-4-7'); replace any non-alphanum with '_'."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", session_id)


def _resolve_real_yc_bench() -> Path:
    """Find the real yc-bench script in the same venv as the current Python.

    Mirrors agent/commands/executor.py:_resolve_yc_bench, but raises rather
    than returning a PATH-lookup fallback because the wrapper bin we stage
    will mask 'yc-bench' on PATH — a fallback would self-recurse.
    """
    venv_bin = Path(sys.executable).parent
    candidate = venv_bin / "yc-bench"
    if candidate.exists():
        return candidate.resolve()
    raise RuntimeError(
        f"Could not find yc-bench binary next to {sys.executable}. "
        "PhantomRuntime needs an unambiguous real-binary path so its wrapper "
        "can shadow PATH without recursing."
    )


def _resolve_phantom_repo() -> Path:
    """Phantom source directory (where orchestrator.py lives).

    Must contain claude-wrapper.sh, agent-docs/, memory/. Read from the
    PHANTOM_REPO_PATH env var; raise with a clear message if unset.
    """
    raw = os.environ.get(_ENV_PHANTOM_REPO)
    if not raw:
        raise RuntimeError(
            f"{_ENV_PHANTOM_REPO} env var is not set. PhantomRuntime needs to "
            "know where phantom's source lives on host. Set it to the directory "
            "containing orchestrator.py + agent-docs/ (e.g. "
            "/path/to/phantom/src/phantom)."
        )
    path = Path(raw).expanduser().resolve()
    must_exist = ["orchestrator.py", "claude-wrapper.sh", "agent-docs", "slack_interface.py"]
    missing = [n for n in must_exist if not (path / n).exists()]
    if missing:
        raise RuntimeError(
            f"{_ENV_PHANTOM_REPO}={path} is missing expected files: {missing}. "
            "Point it at phantom/src/phantom."
        )
    return path


class PhantomSession:
    """One session's filesystem layout + subprocess-env builder."""

    def __init__(self, session_id: str, db_dir: Path | None = None):
        self.session_id = session_id
        self.slug = _safe_slug(session_id)
        self.db_dir = (db_dir or Path("db")).resolve()
        self.root = self.db_dir / f"{self.slug}.phantom"

        # Lazy-resolved on first build_subprocess_env() call so construction
        # never raises just for instantiation diagnostics.
        self._real_yc_bench: Path | None = None
        self._phantom_repo: Path | None = None

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def mock_dir(self) -> Path:
        return self.root

    @property
    def inbox_path(self) -> Path:
        # Filename must match slack_mock._append_jsonl(_mock_dir() / "inbox.jsonl").
        return self.root / "inbox.jsonl"

    @property
    def outbox_path(self) -> Path:
        # Filename must match slack_mock send_message → _mock_dir() / "outbox.jsonl".
        return self.root / "outbox.jsonl"

    @property
    def calls_log_path(self) -> Path:
        return self.root / "yc_bench_calls.jsonl"

    @property
    def wrapper_bin_dir(self) -> Path:
        return self.root / "wrapper_bin"

    @property
    def pyshim_dir(self) -> Path:
        return self.root / "pyshim"

    @property
    def home_dir(self) -> Path:
        return self.root / "home"

    @property
    def log_dir(self) -> Path:
        return self.home_dir / "workspace" / "logs"

    # ------------------------------------------------------------------
    # Staging
    # ------------------------------------------------------------------

    def stage(self) -> None:
        """Create the per-session dirs and stage all the host-compat stubs.

        Idempotent: safe to call once per turn.
        """
        import json

        self.root.mkdir(parents=True, exist_ok=True)
        self.wrapper_bin_dir.mkdir(exist_ok=True)
        self.pyshim_dir.mkdir(exist_ok=True)
        self.home_dir.mkdir(exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.home_dir / "ninja-squad").mkdir(exist_ok=True)
        (self.home_dir / ".claude").mkdir(exist_ok=True)

        # Wrapper-bin/yc-bench → yc_bench_wrapper.py (re-stage every time so
        # source-tree relocations are picked up).
        wrapper_src = Path(__file__).with_name("yc_bench_wrapper.py").resolve()
        wrapper_link = self.wrapper_bin_dir / "yc-bench"
        if wrapper_link.is_symlink() or wrapper_link.exists():
            wrapper_link.unlink()
        wrapper_link.symlink_to(wrapper_src)

        # Wrapper-bin/slack_cli → slack_cli (imports slack_interface as a
        # module so our SlackClient patches are in effect — bypasses the
        # `python slack_interface.py` → __main__ duplication problem).
        slack_cli_src = Path(__file__).with_name("slack_cli").resolve()
        slack_cli_link = self.wrapper_bin_dir / "slack_cli"
        if slack_cli_link.is_symlink() or slack_cli_link.exists():
            slack_cli_link.unlink()
        slack_cli_link.symlink_to(slack_cli_src)

        # sitecustomize.py — Python auto-imports it at startup when its dir is
        # on sys.path. PYTHONPATH prepended in build_subprocess_env().
        sitecustomize = self.pyshim_dir / "sitecustomize.py"
        sitecustomize.write_text(_SITECUSTOMIZE_BODY, encoding="utf-8")

        # ~/.agent_settings.json — phantom's slack_interface reads bot_token
        # from here; stubbing it makes get_slack_tokens() short-circuit
        # without hitting slack.com or /dev/shm/mcp-token. default_agent
        # tells orchestrator which AGENTS entry to use.
        agent_settings = self.home_dir / ".agent_settings.json"
        if not agent_settings.exists():
            agent_settings.write_text(
                json.dumps(
                    {
                        "default_agent": "phantom",
                        "default_channel": "#yc-bench",
                        "default_channel_id": "C_YC_BENCH",
                        "default_team_id": "T_YC_BENCH",
                        "default_team_name": "YcBench",
                        "default_team_domain": "yc-bench",
                        "workspace": "YcBench",
                        "bot_token": "xoxb-yc-bench-fake-token",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        # ~/ninja-squad/s3_config.json — phantom's slack_interface validates
        # this at import time (line 397). Stub creds; boto3 calls fail later
        # but slack_interface tolerates that path.
        s3_cfg = self.home_dir / "ninja-squad" / "s3_config.json"
        if not s3_cfg.exists():
            s3_cfg.write_text(
                json.dumps(
                    {
                        "aws_access_key_id": "AKIAFAKE",
                        "aws_secret_access_key": "fake",
                        "bucket_name": "yc-bench-fake",
                        "region": "us-east-1",
                        "cache_prefix": "slack-channel",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def reset_slack_state(self) -> None:
        """Wipe slack inbox + outbox. Call between sessions; cross-turn state
        is the point of the channel, so do NOT call between turns."""
        for p in (self.inbox_path, self.outbox_path):
            if p.is_file():
                p.unlink()

    def reset_calls_log(self) -> None:
        """Wipe the yc-bench calls log. Optional — turn_id filtering also
        works, but truncating keeps the file small over long runs."""
        if self.calls_log_path.is_file():
            self.calls_log_path.unlink()

    def cleanup(self) -> None:
        """Remove the entire session dir. Called from clear_session."""
        import shutil

        if self.root.is_dir():
            shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------
    # Subprocess environment
    # ------------------------------------------------------------------

    def build_subprocess_env(self, turn_id: int, base: dict[str, str] | None = None) -> dict[str, str]:
        """Build the env dict for a phantom-orchestrator subprocess.

        Prepends wrapper_bin/ to PATH so 'yc-bench' resolves to our wrapper.
        Prepends pyshim/ to PYTHONPATH so sitecustomize.py runs at Python
        startup in any subprocess (including claude's `python slack_cli.py`).
        Sets the env vars the wrapper + slack_mock + sitecustomize need.
        """
        if self._real_yc_bench is None:
            self._real_yc_bench = _resolve_real_yc_bench()
        if self._phantom_repo is None:
            self._phantom_repo = _resolve_phantom_repo()

        env = dict(base if base is not None else os.environ)

        env["PATH"] = f"{self.wrapper_bin_dir}{os.pathsep}{env.get('PATH', '')}"
        pyparts = [str(self.pyshim_dir), str(self._phantom_repo)]
        if existing := env.get("PYTHONPATH"):
            pyparts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(pyparts)

        env[_ENV_REAL_BIN] = str(self._real_yc_bench)
        env[_ENV_CALLS_LOG] = str(self.calls_log_path)
        env[_ENV_TURN_ID] = str(turn_id)
        env[_ENV_MOCK_DIR] = str(self.mock_dir)

        # DATABASE_URL bench-side is "sqlite:///db/<base>.db" (relative). Claude's
        # subprocess runs with cwd=phantom_repo, so a relative path would resolve
        # to <phantom_repo>/db/... — yc-bench commands would write to a phantom-
        # local DB instead of mutating the bench's actual SQLite. Convert relative
        # sqlite URLs to absolute (anchored at the current cwd, which IS the
        # bench's working dir at the point this env is built).
        db_url = env.get("DATABASE_URL")
        if db_url and db_url.startswith("sqlite:///") and not db_url.startswith("sqlite:////"):
            rel_path = db_url[len("sqlite:///") :]
            abs_path = (Path.cwd() / rel_path).resolve()
            env["DATABASE_URL"] = f"sqlite:///{abs_path}"

        # Redirect phantom's expansions into the session home subtree:
        #   ~/.agent_settings.json    → <session>/home/.agent_settings.json
        #   ~/ninja-squad/...         → <session>/home/ninja-squad/...
        #   claude's ~/.claude/       → <session>/home/.claude/
        env["HOME"] = str(self.home_dir)
        # Phantom's source uses PHANTOM_LOG_DIR if set (one-line host-compat
        # patch in orchestrator.py); otherwise it falls back to /workspace/logs.
        env["PHANTOM_LOG_DIR"] = str(self.log_dir)

        return env

    @property
    def phantom_repo(self) -> Path:
        if self._phantom_repo is None:
            self._phantom_repo = _resolve_phantom_repo()
        return self._phantom_repo


# Body of the sitecustomize.py file we stage into pyshim_dir. Imported at
# Python startup (both by the orchestrator process and by claude's child
# python invocations) — applies the two monkey-patches and is a no-op if
# slack_interface / orchestrator aren't importable in that process.
_SITECUSTOMIZE_BODY = '''\
"""Auto-installed by yc-bench PhantomSession. Patches phantom for bench use."""
import sys
for _mod_name in ("slack_mock", "prompt_patch", "run_agent_patch", "host_compat_patch"):
    try:
        __import__(f"yc_bench.agent.runtime.phantom.{_mod_name}")
    except Exception as exc:  # pragma: no cover — defensive
        sys.stderr.write(f"[yc-bench sitecustomize] {_mod_name} import failed: {exc}\\n")
'''


__all__ = ["PhantomSession"]
