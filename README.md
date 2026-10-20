# CostLedger 🧾

**Know what every agent run costs — before the invoice does.**

AI agents burn tokens quietly. A runaway loop, a prompt that grew by
2,000 tokens, a model swap nobody priced — none of it shows up until
the provider invoice lands at the end of the month. CostLedger reads
the run logs you already have, tallies token usage per run, per model
and per tool against a local pricing table, gates budgets in CI, and
diffs cost against a saved baseline.

It is the cost counterpart to AgentCI's behavior regression: AgentCI
asks "did the change break it?", CostLedger asks "did the change make
it expensive?"

Offline and dependency-free: logs are parsed locally, money math is
Decimal-exact (never float), and an unknown model is reported as
**UNKNOWN** — never silently costed at $0.

```bash
python src/costledger.py tally runs/ --save ledger.json
python src/costledger.py budget runs/ --max 5.00
python src/costledger.py compare ledger.v1.json ledger.v2.json
```

## The commands

| Command | What it does |
|---------|--------------|
| `tally <log-or-dir>` | Parse logs; print per-run, per-model and per-tool token + cost rollups and a grand total. `--format json` for scripts, `--save ledger.json` to keep the ledger for `compare`. |
| `budget <log-or-dir> --max <usd>` | Exit `1` when total cost exceeds the budget — or when an unpriced model makes the total unverifiable. Exit `0` when provably within budget. |
| `compare <baseline.json> <new.json>` | Cost diff per model and per tool, with % change. Exit `1` when the total regressed beyond `--threshold-pct` (default **10%**), `0` otherwise. |
| `prices` | Print the effective pricing table with its `as_of` date and currency. |
| `init` | Scaffold a sample `pricing.yaml`, a sample run log, and an `EXPECTED.md` explaining what you should see. |

Exit codes everywhere: `0` = ok, `1` = budget exceeded / cost
regression, `2` = usage or IO error. Drop `budget` and `compare` in
GitHub Actions and a cost blowout fails the build like a test would.

## Log format

JSONL — one event per line (a directory is read recursively; a single
JSON file holding a list of events also works). A usage event carries
a model and token counts in any of these dialects:

```json
{"model": "gpt-5", "input_tokens": 1200, "output_tokens": 300}
{"model": "gpt-5", "prompt_tokens": 800, "completion_tokens": 200}
{"model": "gpt-5", "total_tokens": 1500}
```

`total_tokens` with no split is counted as input. Optional fields are
used when present: `tool` (per-tool attribution), `run_id` or
`session` (per-run rollup; default is the log file's name), and
`timestamp` (kept for your records). Lines without usage — notes,
checkpoints, heartbeats — are ignored but counted in the report as
skipped, as are malformed lines.

A model with no price in the table still gets its tokens tallied. Its
cost is `UNKNOWN`, flagged loudly in every report — a partial total is
always labelled with its known part, never presented as the whole.

## Pricing

A local file you control — no network, no provider account. The
restricted YAML subset (a JSON file with the same shape works too):

```yaml
as_of: 2026-10-20        # when these prices were last verified
currency: USD
models:
  gpt-5:
    input: 1.25          # USD per 1M input tokens
    output: 10.00        # USD per 1M output tokens
```

`as_of` is honesty metadata: `prices` prints it, and a pricing file
without one earns a visible stale-price warning — cost gates are only
as trustworthy as the prices behind them. With no `--pricing`,
CostLedger falls back to a small built-in table of well-known models,
clearly labelled approximate with its own `as_of` date. Pin your own
file for anything you gate money on.

All money is computed with `Decimal`: totals in JSON output are exact
strings, display rounds to 4 decimal places ($0.0001).

## Quick start

```bash
# 1. Scaffold a sample pricing table + log and see what to expect:
costledger init

# 2. Tally your agent's run logs:
costledger tally runs/ --pricing pricing.yaml

# 3. Save a ledger as today's cost baseline:
costledger tally runs/ --pricing pricing.yaml --save ledger.v1.json

# 4. Gate CI on a budget:
costledger budget runs/ --pricing pricing.yaml --max 5.00

# 5. After the next prompt change, diff the cost:
costledger tally runs/ --pricing pricing.yaml --save ledger.v2.json
costledger compare ledger.v1.json ledger.v2.json
```

## Example

From `examples/` — a two-run log across two models, plus one model
nobody has priced yet:

```
$ costledger tally examples/sample-run.jsonl --pricing examples/pricing.yaml
====================================================================
CostLedger — tally report
====================================================================
Sources: examples/sample-run.jsonl
Pricing: examples/pricing.yaml (as of 2026-10-20, USD)

Per run:
  run-alpha                         4,000 in /      1,400 out   $0.0270
  run-beta                          2,140 in /        128 out   UNKNOWN (known part only; total ≥ $0.0019)

Per model:
  claude-sonnet-4.5                 2,000 in /        900 out   $0.0195
  gpt-5                             3,500 in /        500 out   $0.0094
  mystery-model-9000                  640 in /        128 out   UNKNOWN

Per tool:
  coder                             4,140 in /      1,028 out   UNKNOWN (known part only; total ≥ $0.0214)
  planner                           1,200 in /        300 out   $0.0045
  web-search                          800 in /        200 out   $0.0030

Events: 5 used · 1 skipped (0 malformed)
Tokens: 6,140 in / 1,528 out (7,668 total)
⚠️  UNKNOWN MODEL(S): mystery-model-9000 — tokens tallied above, cost UNKNOWN (not $0). Add prices for them to price these runs.
Total cost: UNKNOWN — known part is $0.0289; the true total is higher.
```

That unknown model has teeth in a budget gate. Even with a generous
ceiling, the gate refuses to certify what it cannot price:

```
$ costledger budget examples/sample-run.jsonl --pricing examples/pricing.yaml --max 0.05
❌ BUDGET UNVERIFIABLE — unknown model(s): mystery-model-9000. Known cost is $0.0289 of budget $0.0500, but unpriced usage can only push the true total up. Add prices and re-run.
(exit code 1)
```

And once every model is priced, `compare` catches the quiet blowups.
Here the coder's context grew 10× between two saved ledgers:

```
$ costledger compare ledger.v1.json ledger.v2.json
====================================================================
CostLedger compare — ❌ COST REGRESSION
====================================================================
Baseline total: $0.028875
New total:      $0.082875
Change:         $0.054000 (+187.01%) · threshold 10%

Per model:
  claude-sonnet-4.5            $0.0195 → $0.0735  (🔺 $0.0540)
  gpt-5                        $0.009375 → $0.009375  (— $0.000000)
```

## Why

"Tokens are cheap" is true per call and false per month. The teams
that get surprised by agent bills share one trait: nobody owned the
number between the run and the invoice. CostLedger makes the number a
build artifact — tallied from logs, gated in CI, and diffed on every
change — so the surprise happens in a pull request, where it's cheap,
instead of in billing, where it isn't.

## Part of AgentForge

CostLedger is the fifth tool in the [AgentForge](https://github.com/maticcretic-commits)
trust layer for AI agents:

- **AgentProof** — verify agent work — [github.com/maticcretic-commits/agentproof](https://github.com/maticcretic-commits/agentproof)
- **PromptRE** — reverse-engineer agent prompts — [github.com/maticcretic-commits/promptre](https://github.com/maticcretic-commits/promptre)
- **AgentCI** — regression harness for agents — [github.com/maticcretic-commits/agentci](https://github.com/maticcretic-commits/agentci)
- **SecretScan** — scan agent outputs for leaked secrets — [github.com/maticcretic-commits/secretscan](https://github.com/maticcretic-commits/secretscan)
- **CostLedger** — cost accounting for agent runs (this repo)
- **SchemaGuard** — validate agent outputs against their contracts — [github.com/maticcretic-commits/schemaguard](https://github.com/maticcretic-commits/schemaguard)
- **SkillAudit** — audit agent skills for hidden instructions and quality — [github.com/maticcretic-commits/skillaudit](https://github.com/maticcretic-commits/skillaudit)
- **PromptDiff** — diff prompt versions — [github.com/maticcretic-commits/promptdiff](https://github.com/maticcretic-commits/promptdiff)

AgentProof checks what an agent *did*; PromptRE checks what it was *told*; AgentCI checks it still *works* after you change it; SecretScan checks that nothing *leaked*; CostLedger checks what it *cost*; SchemaGuard checks the output still *fits the contract*; SkillAudit checks the skill was *safe to install*; PromptDiff checks what the prompt change *changed*.

## License

MIT
