# Claude Code Adapter

Runs the **`claude` CLI** directly as the YC-Bench agent — one subprocess per turn,
no orchestrator, no Slack. Claude uses its Bash tool to run `yc-bench` commands.

> Runs as a normal subprocess on your host — **no Docker for the agent**.
> The only Docker is LiteLLM's DB. Claude's model calls go through the LiteLLM proxy.

## Prerequisites

- `claude` CLI on your `PATH` (`which claude`)
- LiteLLM proxy running at `http://0.0.0.0:4000`
- `uv` installed

## Run

```bash
cd /Users/yu.yan/code/yc-bench

export ANTHROPIC_BASE_URL=http://0.0.0.0:4000
export ANTHROPIC_AUTH_TOKEN=<your-litellm-key>

uv run yc-bench run \
  --model claude-code/claude-opus-4-8 \
  --seed 1 --config claude_code --no-live
```

- `claude-code/` prefix → picks this adapter; `claude-opus-4-8` suffix → passed as `--model`
  (**required**, else the CLI uses its own default model).
- One seed ≈ 35–85 min. **Run one seed at a time.**
- Background it: `nohup uv run ... > logs/cc_seed1.log 2>&1 &`

## Change the model

**The `--model` flag in the run command is the only place to change it.** It is
required and always overrides the preset, so you do **not** edit any file:

```bash
--model claude-code/claude-sonnet-4-6     # just change the part after the slash
```

Keep the `claude-code/` prefix (it routes to this adapter); change only the suffix.

## Stop a run

The run is a background process **and the inner `claude` agent does not exit on its
own** — you must kill both, or it keeps playing and spending:

```bash
pkill -f "yc-bench run"                  # 1. stop the bench runner
pkill -f "claude -c -p --dangerously"    # 2. stop the inner claude agent

pgrep -fl "yc-bench run" || echo "runner gone"          # 3. verify
ps aux | grep '[c]laude -c -p --dangerously' || echo "inner claude gone"
```

The `--dangerously` filter targets only the bench agent, leaving any interactive
`claude` sessions untouched.

## Per-seed isolation (no memory leak between seeds)

Seeds run sequentially, but each is a **fully fresh Claude Code session** — seed 1's
state cannot leak into seed 2/3:

- Each seed gets its **own `HOME`** at `db/run-<seed>-<model>.claude_code/home/`, so
  Claude's entire `~/.claude` (session history, config, any `CLAUDE.md` memory) is
  separate per seed. It also means the inner agent never reads *your* personal
  `~/.claude` memory.
- Each seed runs in its **own working directory** (the sandbox), so any file the
  agent writes stays in that seed's folder.
- In practice the agent writes **no Claude memory files at all** — it saves notes via
  `yc-bench scratchpad` (stored in the bench DB), not Claude Code's memory.
- The only cross-seed-shared surface is a `CLAUDE.md`, but this can only be written by us. 

## Output

| Path | What |
|---|---|
| `results/yc_bench_result_claude_code_<seed>_<model>.json` | Outcome, turns, transcript |
| `db/claude_code_<seed>_<model>.db` | Game DB (funds, tasks, ledger) |
| `db/<slug>.claude_code/yc_bench_calls.jsonl` | Every command Claude ran + output |

## Gotchas

1. LiteLLM proxy must be up, or all model calls fail.
2. Confirm LiteLLM `SpendLogs` shows `claude-opus-4-8` (model pin not drifted).
3. Re-running the same config+seed **overwrites** its artifacts — archive first.
4. After killing a run, sweep zombies: `ps | grep 'claude -c'`.
