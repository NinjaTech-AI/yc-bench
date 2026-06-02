# Phantom adapter for YC-Bench

Drives NinjaTech Phantom (orchestrator + claude-code with `-c` resume) as a
bench runtime. Activated when `settings.model` starts with `phantom/`.

## How it fits together

```
yc-bench loop
   └── PhantomRuntime.run_turn(user_input, scratchpad)
        ├── PhantomSession.stage()           # wrapper bin + sitecustomize.py
        ├── slack_mock.write_inbound_message  # post turn message to inbox JSONL
        ├── cost_tracker.snapshot (before)
        ├── orchestrator_runner.run_orchestrator
        │     └── python <phantom>/orchestrator.py --task <fixed text>
        │           ├── claude-wrapper.sh -c -p <built_prompt>
        │           │     └── claude CLI
        │           │           ├── reads slack via `python slack_cli read`
        │           │           │     (SlackClient HTTP layer patched by slack_mock)
        │           │           ├── runs `yc-bench <subcommand>` via Bash
        │           │           │     (yc_bench_wrapper.py shadows PATH, logs to JSONL)
        │           │           └── posts replies via `python slack_cli say`
        │           │                 (SlackClient.send_message → outbox JSONL)
        │           └── exits
        ├── cost_tracker.snapshot (after)
        ├── log_parser.extract_turn_calls    # JSONL → tool_calls + resume_payload
        └── return RuntimeTurnResult
```

## Required environment

| Var | Purpose |
|-----|---------|
| `PHANTOM_REPO_PATH` | Absolute path to phantom source dir (must contain `orchestrator.py`, `claude-wrapper.sh`, `agent-docs/`, `slack_interface.py`). |
| `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` | Routed through the LiteLLM proxy (`http://0.0.0.0:4000` by default). Also used by `cost_tracker.snapshot()` for per-turn cost diffs. |
| `PHANTOM_PYTHON` *(optional)* | Override Python interpreter used to launch the orchestrator. Defaults to `sys.executable` (yc-bench's venv). |

The chosen Python interpreter must have both `yc_bench` AND phantom's runtime
deps importable (`slackify-markdown`, `requests`, `boto3`, etc. — see
`<phantom>/requirements.txt`). Easiest: install phantom's deps into yc-bench's
venv:

```bash
uv pip install -r $PHANTOM_REPO_PATH/requirements.txt
```

Alternatively, create a dedicated phantom venv and point `PHANTOM_PYTHON` at
its python binary — but that venv must also have `yc_bench` importable for the
sitecustomize patches to load.

## Per-session filesystem layout

For session_id `run-1-phantom/claude-opus-4-7`, the runtime stages:

```
db/run-1-phantom_claude-opus-4-7.phantom/
├── inbox.jsonl         — bench writes turn messages here
├── outbox.jsonl        — phantom (claude) writes replies here
├── yc_bench_calls.jsonl — every yc-bench call claude made, tagged by turn_id
├── wrapper_bin/yc-bench → ../../yc_bench_wrapper.py   (symlink; PATH-prepended)
└── pyshim/sitecustomize.py — installs slack_mock + prompt_patch at Python startup
                              (PYTHONPATH-prepended)
```

The slack mock files and the yc-bench calls log persist for the whole run —
that's how cross-turn continuity works alongside claude's `-c`.

## Running it

```bash
export PHANTOM_REPO_PATH=/path/to/phantom/src/phantom
export ANTHROPIC_BASE_URL=http://0.0.0.0:4000
export ANTHROPIC_AUTH_TOKEN=sk-...

uv run yc-bench run \
  --model phantom/claude-opus-4-7 \
  --seed 1 \
  --config phantom
```

The `phantom/` prefix routes through `PhantomRuntime`. The suffix
(`claude-opus-4-7`) is what claude-code sees — phantom's orchestrator picks
whatever model its own config selects.

## What's deferred (v1)

- **`memory/phantom_memory.md` as active state.** File is left empty; orchestrator
  prints "No previous memory" each turn. The bench's `yc-bench scratchpad` is the
  cross-turn state vehicle.
- **Parallel seeds.** Code is parallel-safe (session_id plumbed through paths and
  env), but multi-seed runs in one process are untested.
- **Mid-run resume.** `save_session_messages` / `restore_session_messages` are
  no-ops. A crashed phantom run starts from turn 1.
- **Container isolation.** Phantom runs on host. Production CL-Bench used docker;
  YC-Bench has no equivalent attack surface that demands it.

## Open seams (tune via env or the phantom.toml preset)

| Knob | Where | Notes |
|------|-------|-------|
| Turn timeout | `agent.request_timeout_seconds` in `config/presets/phantom.toml` | Production phantom uses 900s. |
| Retry count + backoff | `agent.retry_max_attempts`, `agent.retry_backoff_seconds` | 3 retries default; exponential. |
| `--task` text for orchestrator | `_DEFAULT_TASK` in `runtime.py` | Adjust if claude misinterprets the slack-pull instruction. |
| Doc list claude cats | `_KEPT_DOCS` in `prompt_patch.py` | Currently AGENT_PROTOCOL + SLACK_INTERFACE + ORCHESTRATOR. |
| First-turn slack message wrap | `_compose_slack_message` in `runtime.py` | Includes the bench's `SYSTEM_PROMPT` body verbatim. |
