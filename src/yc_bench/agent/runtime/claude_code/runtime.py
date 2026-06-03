from __future__ import annotations

import json
import logging
import time

from ..base import AgentRuntime
from ..schemas import RuntimeSettings, RuntimeTurnRequest, RuntimeTurnResult
from . import claude_runner, cost_tracker, log_parser
from .session import ClaudeCodeSession

logger = logging.getLogger(__name__)


class ClaudeCodeRuntime(AgentRuntime):
    """Drives a `claude -c -p` subprocess per bench turn.

    No slack, no orchestrator scaffolding. The bench's user_input is composed
    directly into claude's prompt argument; claude's stdout is the turn's
    final_output. Cross-turn continuity is provided by claude's `-c` flag
    plus the per-session HOME redirect.
    """

    def __init__(self, settings: RuntimeSettings, command_executor):
        self._settings = settings
        self._command_executor = command_executor  # held for symmetry; not used

        if not settings.model.startswith("claude-code/"):
            raise ValueError(
                f"ClaudeCodeRuntime expects model='claude-code/<inner>', got {settings.model!r}"
            )
        self._inner_model = settings.model.removeprefix("claude-code/")

        self._sessions: dict[str, ClaudeCodeSession] = {}
        self._turn_counters: dict[str, int] = {}

        logger.info(
            "ClaudeCodeRuntime configured: inner_model=%s timeout=%ss retries=%d",
            self._inner_model,
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

        prompt = self._compose_prompt(
            turn_id=turn_id,
            user_input=request.user_input,
            scratchpad=request.scratchpad,
        )
        logger.info(
            "Turn %d: built %d-char prompt (session=%s)",
            turn_id,
            len(prompt),
            request.session_id,
        )

        cost_before = cost_tracker.snapshot()
        claude_result = self._spawn_with_retries(session, turn_id, prompt)
        cost_after = cost_tracker.snapshot()
        cost_delta = cost_after.diff(cost_before)

        raw_calls = log_parser.extract_turn_calls(session.calls_log_path, turn_id)
        tool_calls_made = [_format_tool_call(c) for c in raw_calls]
        resume_payload = log_parser.extract_resume_payload(raw_calls)

        final_output = self._extract_final_output(claude_result, tool_calls_made)

        if not claude_result.ok and not tool_calls_made:
            raise RuntimeError(
                f"ClaudeCode turn {turn_id} failed: claude exit={claude_result.exit_code}, "
                f"no yc-bench calls. stderr={claude_result.stderr[:400]!r}"
            )

        return RuntimeTurnResult(
            final_output=final_output,
            raw_result={
                "tool_calls": tool_calls_made,
                "prompt_tokens": cost_delta.prompt_tokens,
                "completion_tokens": cost_delta.completion_tokens,
                "claude_exit_code": claude_result.exit_code,
                "claude_duration_seconds": claude_result.duration_seconds,
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
            logger.info("ClaudeCodeRuntime: cleared session %s", session_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create_session(self, session_id: str) -> ClaudeCodeSession:
        if session_id not in self._sessions:
            session = ClaudeCodeSession(session_id=session_id)
            session.stage()
            session.reset_calls_log()
            self._sessions[session_id] = session
        return self._sessions[session_id]

    def _compose_prompt(
        self,
        turn_id: int,
        user_input: str,
        scratchpad: str | None,
    ) -> str:
        """Build claude's prompt for this turn.

        Turn 1: SYSTEM_PROMPT (CEO rules) + initial state + execution
        instructions. Subsequent turns rely on claude's `-c` resume (claude
        remembers the rules already) and pass only the new state + scratchpad.
        """
        parts: list[str] = []

        if turn_id == 1:
            # Imported lazily to avoid coupling at module-import time.
            from ...prompt import SYSTEM_PROMPT

            parts.append(
                "You are running a long-horizon engagement: act as the CEO of a "
                "simulated AI startup for one year, using the `yc-bench` CLI for "
                "every action. The rules of the engagement are below.\n\n"
                "**Execution notes for this environment:**\n"
                "- Use your Bash tool to run `yc-bench <subcommand>`. Multiple "
                "commands per response are expected.\n"
                "- Drive the simulation forward with `yc-bench sim resume` once "
                "you have at least one active task. Keep going until you reach "
                "the 1-year horizon (`terminal_reason=horizon_end`) or run out "
                "of usable time. Do not stop early.\n"
                "- All `yc-bench` commands return JSON; read them carefully."
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
                "actions per response are expected. Don't call `sim resume` unless "
                "you have at least one active task."
            )

        return "\n\n---\n\n".join(parts)

    def _spawn_with_retries(
        self,
        session: ClaudeCodeSession,
        turn_id: int,
        prompt: str,
    ) -> claude_runner.ClaudeResult:
        """Run claude with retries on failure."""
        attempts = self._settings.retry_max_attempts
        backoff = self._settings.retry_backoff_seconds
        timeout = self._settings.request_timeout_seconds

        env = session.build_subprocess_env(turn_id=turn_id)
        result: claude_runner.ClaudeResult | None = None

        for attempt in range(1, attempts + 1):
            result = claude_runner.run_claude(
                prompt=prompt,
                cwd=session.work_dir,
                env=env,
                timeout_seconds=timeout,
                model=self._inner_model,
            )
            if result.ok:
                return result
            logger.warning(
                "Turn %d: claude attempt %d/%d failed rc=%d stderr=%r",
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
        claude_result: claude_runner.ClaudeResult,
        tool_calls_made: list[dict],
    ) -> str:
        """Pick the most useful single-string summary of what claude did.

        Preference order:
          1. Claude's stdout (its final reply in -p mode).
          2. A synthesized "Executed N commands: ..." line.
          3. Claude's stderr tail (when both above are empty).
        """
        stdout = (claude_result.stdout or "").strip()
        if stdout:
            return stdout

        if tool_calls_made:
            cmds = [c["command"] for c in tool_calls_made]
            return f"Executed {len(cmds)} yc-bench command(s): {', '.join(cmds)}"

        return (
            f"[claude turn produced no output; rc={claude_result.exit_code}; "
            f"stderr={claude_result.stderr[:300]!r}]"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_tool_call(raw: dict) -> dict:
    """Reshape a log_parser record into the bench's tool_call format.

    Bench loop._extract_commands reads {"command", "result"} per call; result
    is treated as a string. We serialize the parts the loop's summary line
    would care about (matches LiteLLMRuntime via normalize_result).
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


__all__ = ["ClaudeCodeRuntime"]
