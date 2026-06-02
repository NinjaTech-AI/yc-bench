from __future__ import annotations

import json
import logging
import os
import sys
import time

from ..base import AgentRuntime
from ..schemas import RuntimeSettings, RuntimeTurnRequest, RuntimeTurnResult
from . import cost_tracker, log_parser, orchestrator_runner, slack_mock
from .session import PhantomSession

logger = logging.getLogger(__name__)


# Default orchestrator --task text. Tells claude to pull bench instructions
# from slack rather than execute a pre-defined task.
_DEFAULT_TASK = (
    "Check Slack #yc-bench for the latest engagement message and act on it. "
    "Use your Bash tool to run `yc-bench <subcommand>` for every action. "
    "Multiple yc-bench commands per turn are expected."
)


class PhantomRuntime(AgentRuntime):
    """Drives the Phantom orchestrator + claude-code subprocess per bench turn.

    Cross-turn continuity is provided by claude's `-c` flag inside
    claude-wrapper.sh, plus the mock slack channel that accumulates messages
    across turns. Slack mock state is wiped only on clear_session.
    """

    def __init__(self, settings: RuntimeSettings, command_executor):
        self._settings = settings
        self._command_executor = command_executor  # held for symmetry; not used

        if not settings.model.startswith("phantom/"):
            raise ValueError(
                f"PhantomRuntime expects model='phantom/<inner>', got {settings.model!r}"
            )
        self._inner_model = settings.model.removeprefix("phantom/")
        self._python_bin = os.environ.get("PHANTOM_PYTHON") or sys.executable

        self._sessions: dict[str, PhantomSession] = {}
        self._turn_counters: dict[str, int] = {}

        logger.info(
            "PhantomRuntime configured: inner_model=%s python=%s timeout=%ss retries=%d",
            self._inner_model,
            self._python_bin,
            settings.request_timeout_seconds,
            settings.retry_max_attempts,
        )

    # ------------------------------------------------------------------
    # AgentRuntime interface
    # ------------------------------------------------------------------

    def run_turn(self, request: RuntimeTurnRequest) -> RuntimeTurnResult:
        session = self._get_or_create_session(request.session_id)
        session.stage()
        turn_id = self._turn_counters[request.session_id] = (
            self._turn_counters.get(request.session_id, 0) + 1
        )

        message_text = self._compose_slack_message(
            turn_id=turn_id,
            user_input=request.user_input,
            scratchpad=request.scratchpad,
        )
        slack_mock.write_inbound_message(message_text, session.mock_dir)
        logger.info(
            "Turn %d: posted %d-char message to slack inbox (session=%s)",
            turn_id,
            len(message_text),
            request.session_id,
        )

        cost_before = cost_tracker.snapshot()
        orch_result = self._spawn_with_retries(session, turn_id)
        cost_after = cost_tracker.snapshot()
        cost_delta = cost_after.diff(cost_before)

        # Extract tool calls + resume payload from the yc-bench wrapper log.
        raw_calls = log_parser.extract_turn_calls(session.calls_log_path, turn_id)
        tool_calls_made = [_format_tool_call(c) for c in raw_calls]
        resume_payload = log_parser.extract_resume_payload(raw_calls)

        final_output = self._extract_final_output(session, orch_result, tool_calls_made)

        if not orch_result.ok and not tool_calls_made:
            # Orchestrator failed AND claude didn't execute anything — surface
            # this clearly so the bench's terminal-error handling fires.
            raise RuntimeError(
                f"Phantom turn {turn_id} failed: orchestrator exit={orch_result.exit_code}, "
                f"no yc-bench calls. stderr={orch_result.stderr[:400]!r}"
            )

        return RuntimeTurnResult(
            final_output=final_output,
            raw_result={
                "tool_calls": tool_calls_made,
                "prompt_tokens": cost_delta.prompt_tokens,
                "completion_tokens": cost_delta.completion_tokens,
                "phantom_exit_code": orch_result.exit_code,
                "phantom_duration_seconds": orch_result.duration_seconds,
            },
            checkpoint_advanced=resume_payload is not None,
            resume_payload=resume_payload,
            turn_cost_usd=cost_delta.total_cost_usd,
        )

    def clear_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        self._turn_counters.pop(session_id, None)
        if session is not None:
            session.cleanup()
            logger.info("PhantomRuntime: cleared session %s", session_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create_session(self, session_id: str) -> PhantomSession:
        if session_id not in self._sessions:
            session = PhantomSession(session_id=session_id)
            session.stage()
            session.reset_slack_state()  # fresh channel per session
            session.reset_calls_log()
            self._sessions[session_id] = session
        return self._sessions[session_id]

    def _compose_slack_message(
        self,
        turn_id: int,
        user_input: str,
        scratchpad: str | None,
    ) -> str:
        """Render the slack message body for this turn.

        Turn 1 includes the YC-Bench SYSTEM_PROMPT body so claude learns the
        rules of the engagement. Subsequent turns rely on claude's `-c` resume
        plus the in-channel history.
        """
        parts: list[str] = []

        if turn_id == 1:
            # Imported lazily — keeps PhantomRuntime importable when prompt.py
            # is being reshuffled in dev.
            from ...prompt import SYSTEM_PROMPT

            parts.append(
                "Hi @phantom! 👋\n\n"
                "Kicking off a new long-horizon engagement: you'll run the CEO of "
                "a simulated AI startup for one year via the `yc-bench` CLI. The "
                "rules of the engagement are below — use your Bash tool to run "
                "`yc-bench <subcommand>` for every action.\n\n"
                "**Environment notes for this session (different from production):**\n"
                "- Use `slack_cli <subcmd>` (NOT `python slack_interface.py <subcmd>`) "
                "to read/post in this channel. Same flags, same behavior — just a "
                "shorter command provided by this environment.\n"
                "- Use `yc-bench <subcommand>` for game actions. Multiple per turn are expected."
            )
            parts.append("# Rules of the engagement\n\n" + SYSTEM_PROMPT.strip())
            parts.append("# Your starting state\n\n" + user_input.strip())
        else:
            parts.append(user_input.strip())

        if scratchpad and scratchpad.strip():
            parts.append("# Your scratchpad notes\n\n" + scratchpad.strip())

        if turn_id > 1:
            parts.append(
                "Decide and act now. Run yc-bench commands via Bash; multiple "
                "actions per turn are expected. Don't call `sim resume` unless "
                "you have at least one active task."
            )

        return "\n\n---\n\n".join(parts)

    def _spawn_with_retries(
        self,
        session: PhantomSession,
        turn_id: int,
    ) -> orchestrator_runner.OrchestratorResult:
        """Run the orchestrator with retries on failure."""
        attempts = self._settings.retry_max_attempts
        backoff = self._settings.retry_backoff_seconds
        timeout = self._settings.request_timeout_seconds

        env = session.build_subprocess_env(turn_id=turn_id)
        result: orchestrator_runner.OrchestratorResult | None = None

        for attempt in range(1, attempts + 1):
            result = orchestrator_runner.run_orchestrator(
                phantom_repo=session.phantom_repo,
                python_bin=self._python_bin,
                task=_DEFAULT_TASK,
                env=env,
                timeout_seconds=timeout,
            )
            if result.ok:
                return result
            logger.warning(
                "Turn %d: orchestrator attempt %d/%d failed rc=%d stderr=%r",
                turn_id,
                attempt,
                attempts,
                result.exit_code,
                result.stderr[:200],
            )
            if attempt < attempts:
                time.sleep(backoff * (2 ** (attempt - 1)))

        return result  # last failed result

    def _extract_final_output(
        self,
        session: PhantomSession,
        orch_result: orchestrator_runner.OrchestratorResult,
        tool_calls_made: list[dict],
    ) -> str:
        """Pick the most useful single-string summary of what claude did.

        Preference order:
          1. Claude's last slack outbox message (the natural "reply").
          2. A synthesized "Executed N commands: ..." line.
          3. Orchestrator stderr tail (when both above are empty).
        """
        try:
            outbox = slack_mock.read_outbound_messages(session.mock_dir)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Could not read slack outbox: %s", exc)
            outbox = []

        if outbox:
            return outbox[-1].get("text", "")

        if tool_calls_made:
            cmds = [c["command"] for c in tool_calls_made]
            return f"Executed {len(cmds)} yc-bench command(s): {', '.join(cmds)}"

        # Last resort — surface the orchestrator's failure mode.
        return f"[phantom turn produced no output; rc={orch_result.exit_code}; stderr={orch_result.stderr[:300]!r}]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_tool_call(raw: dict) -> dict:
    """Reshape a log_parser record into the bench's tool_call format.

    Bench loop._extract_commands reads {"command", "result"} per call;
    result is treated as a string. We serialize the parts the loop's
    summary line would care about (matches what LiteLLMRuntime does via
    normalize_result).
    """
    result_obj = {
        "ok": raw["exit_code"] == 0,
        "exit_code": raw["exit_code"],
        "stdout": raw["stdout"],
        "stderr": raw["stderr"],
        "duration_ms": raw.get("duration_ms"),
    }
    return {
        "command": raw["command"],
        "result": json.dumps(result_obj, ensure_ascii=False),
    }


__all__ = ["PhantomRuntime"]
