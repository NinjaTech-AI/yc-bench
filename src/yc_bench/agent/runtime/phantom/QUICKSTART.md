# Phantom × YC-Bench — Quickstart

How to run one seed and check its progress. For architecture details see `README.md`.

## 1. Set credentials

Phantom routes claude through a LiteLLM proxy. Set two env vars in your shell:

```bash
export ANTHROPIC_BASE_URL=http://0.0.0.0:4000     # LiteLLM proxy
export ANTHROPIC_AUTH_TOKEN=sk-...                # your LiteLLM virtual key
export PHANTOM_REPO_PATH=/path/to/phantom/src/phantom
```

The Anthropic key is **not** read directly by claude — it goes through the proxy. Use a LiteLLM virtual key (whatever your team's proxy admin issued), not a raw `sk-ant-...` key.

## 2. Start one seed

```bash
uv run yc-bench run \
  --model phantom/claude-opus-4-7 \
  --seed 1 \
  --config phantom \
  --no-live > logs/phantom_seed1.log 2>&1 &
```

- `--model phantom/<inner>` — the `phantom/` prefix routes to `PhantomRuntime`.
- `--seed 1` — repeat with `2` and `3` for the canonical 3-seed run.
- `--no-live` — disables the terminal dashboard; the bench runs headless.

One seed takes ~1 hours and ~$90 of LLM credits.

## 3. Check status while it runs

The bench writes everything to a SQLite DB. Two queries cover most of what you want:

```bash
DB=db/phantom_1_phantom_claude-opus-4-7.db    # adjust seed/model in the name

# Funds + sim time + horizon progress
sqlite3 -header -column "$DB" "
  SELECT printf('\$%,d', c.funds_cents/100) AS funds,
         s.sim_time,
         ROUND((julianday(s.sim_time) - julianday('2025-01-01'))
               / (julianday(s.horizon_end) - julianday('2025-01-01')) * 100, 1) AS pct_horizon
  FROM companies c JOIN sim_state s ON s.company_id = c.id;"

# Tasks done so far
sqlite3 -header -column "$DB" "
  SELECT status, COUNT(*) AS n
  FROM tasks WHERE status != 'market'
  GROUP BY status ORDER BY n DESC;"
```

To see what claude is doing **right now**, tail the per-call log:

```bash
SESS=db/run-1-phantom_claude-opus-4-7.phantom
tail -F "$SESS/yc_bench_calls.jsonl" | grep --line-buffered '"event":"call"'
```

## 4. When it finishes

The bench writes its final result here:

```
results/yc_bench_result_phantom_1_phantom_claude-opus-4-7.json
```

Look for `"terminal_reason": "horizon_end"` (success) or `"bankruptcy"` (the agent went broke). The same DB queries above give you the final funds.
