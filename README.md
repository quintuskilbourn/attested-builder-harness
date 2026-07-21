# Attested-builder fairness harness

Testing **which fairness rules an open verifier should enforce on a private block builder** — by
letting an **AI adversary** try to beat each rule. Self-contained Python; deterministic; no external
data. This is the eval-rule ("spine") side of the closed-attested-builder problem: run a proprietary
(eventually AI-written) builder, but bind its *output block* with a public, attested rule that rejects
blocks which harm users beyond a limit.

## The problem

A block builder chooses the order of trades in a block and can **sandwich** users — buy just before a
user's buy to push the price up, let them fill worse, sell right after. If the builder's logic is
secret (and changes every deploy), you can't audit the code. So instead you put a **rule on the output
block** that inspects the finished block and rejects it if users were harmed too much. The question:
**which rule?** You can't tell by reading one — a smart builder finds the ordering that slips under it.
So we grade each rule by letting an adversary attack it.

## Method

An AI (any headless code-writing model; `claude -p` by default) **writes its own builder strategy** —
an arbitrary Python `build(base, book)` that reorders trades and inserts the builder's own legs. Each
candidate is scored on a multi-pool AMM sim; the score + errors feed back; it iterates for a bounded
budget, keeping the best. Strategies are optimized on a **training** block set and reported on a
**held-out** set they never trained on.

- **Simulation** (`harness_mp.py`): five constant-product (Uniswap-V2-style) pools of varied depth,
  many traders per block across pools. The builder may **precommit** its own legs but can't read a
  victim's future trade.
- **Score**: builder profit per block (priority fees + net trading extraction), counting **only blocks
  the rule accepts** — so the AI sandwiches as hard as it can *while staying under the rule*.
- **The rules** (`DEFINITIONS.md`): six, differing in the *reference* and the *aggregation* — a
  no-price-check baseline, and regret-vs-fair-baseline capped on the worst victim (L∞), a sanitized
  variant, root-mean-square (L2), 95th-percentile, and volume-weighted.

### How harm is measured — and its limitations (read this)

A trader's harm is its price shortfall versus executing **the same trade alone, from the pool's
start-of-block state** (`harm_to_user` in `harness_mp.py`). It is deliberately rule-independent so
"did a rule catch harm" isn't circular. But the definition has real limits, and the numbers below
should be read with them in mind:

- **No outside reference price.** Harm is measured only against the pool's own state, so it **cannot
  distinguish a *legitimate* price move (arbitrage, including CEX–DEX rebalancing) from a *predatory*
  one** — both look like the pool moving against the user. This is the single biggest limitation.
- **The counterfactual is the user trading *alone*.** Because the baseline is this user by itself, the
  measure **charges the user for the price impact of *every other order in the block*** — other users'
  legitimate trades, arbitrage — not just the builder's sandwich. On a busy honest block this
  **overstates** harm; treat it as an upper bound on sandwich damage, not a clean attribution.
- **Block-start is assumed fair.** Any skew already present at block-start (e.g. from a prior block) is
  baked into the "fair" reference.
- **So the absolute harm figures are a modeling artifact, not ground truth.** Rule-vs-rule comparisons
  are the trustworthy signal; the absolute bps are indicative only.

`harness_market.py` is a v1 fix for the first point: it adds an exogenous "true" price and top-of-block
arbitrageurs, and measures harm against the true price (see below).

## Key results

One draw, seed 97, 300 held-out blocks. "Sandwich profit" is the builder's predatory extraction;
honest fees are a constant ≈119 across all rules, so everything above ≈119 is predation.

| Rule | Sandwich profit | Worst-trader loss | Traders harmed / block | Worst victim's share of harm | Block acceptance |
|---|---:|---:|---:|---:|---:|
| **structural** (no price check) | **1,810,270** | 99.8% | 9.2 | 12% | 100% |
| worst-case + sanitized (L∞) | 1,137 | 7.5% | 2.7 | 59% | 27% |
| p95 | 1,382 | 7.5% | 2.7 | 62% | 97% |
| worst-case (L∞) | 1,457 | 7.6% | 2.7 | 56% | 66% |
| dollar / volume-weighted | 4,479 | 7.8% | 3.5 | 54% | 92% |
| **averaging (L2)** | **9,088** | 7.8% | 3.6 | 51% | 99% |

- **Any rule beats no rule by ~1,240×** (1,810,270 → 1,457 for worst-case), and cuts the worst
  trader's loss from 99.8% to ~7.6%.
- **Rule *shape* matters ~6×.** The **averaging (L2)** rule leaks **9,088** — about **6× the
  worst-case rule** — at the *same* worst-trader loss, by spreading the same per-trader hit over more
  victims. Volume-weighting leaks ~3×. **Lesson: cap the single worst victim; don't average.**
- **The AI found real exploits**: under worst-case it sandwiched the benign **background** (non-user)
  flow in pools with *no protected user* — the rule measures only *user* harm, so that extraction never
  registers and is free (a rule protects exactly whoever it defines as a "user"; any other flow is fair
  game); under L2 it wrote a per-victim sizing table so combined harm sat just under the threshold. Each
  rule's champion `build()` is embedded in `results/characterize_result.json`.
- **It also battletested the scorer.** Earlier iterations found two ways to fake profit without real
  harm (draining a pool so leftover inventory marked at the crashed price exploded; a ~1e308 trade
  overflowing to infinity). Both are fixed (`eval_strategy.py` + block-start marking); in this run
  every value is bounded and real.

## The realistic market model (`harness_market.py`)

Fixes the "no outside reference" limitation: an exogenous **true price** per token (geometric Brownian
motion), **top-of-block arbitrageurs** that re-peg stale pools each block, and harm measured against
the **true price**. Validated (`python3 harness_market.py`) on a 6.5%-stale block:

| case | true-price rule | pool-based rule |
|---|---:|---:|
| legit arb re-pegs, then a user trades | **0.0%** harm | 6.4% (false positive) |
| arb + a sandwich around the user | **1.9%** harm (caught) | — |

The true-price reference separates legitimate arbitrage (0%) from a sandwich (1.9%), where the
pool-based reference reads the arb's honest correction as 6.4% "harm". Built + validated, **not yet run
at scale** with the AI adversary.

## Reproduce

Python 3 (stdlib only for the sim). Deterministic — results replay from seed.

```bash
python3 harness_market.py          # market-model validation (prints the table above)
python3 characterize.py            # per-rule behaviour report from champions -> results/characterize_result.json
python3 agent_opt.py               # the full AI-adversary run (needs a `claude` CLI on PATH, or edit call_codex)
```

## Files

| file | what |
|---|---|
| `harness.py` | single-pool sim, `Order`/`Pool`, the fairness-rule definitions (`SPINES`) |
| `harness_mp.py` | multi-pool sim, harm measure, rule check (`spine_pass`), block generator |
| `harness_market.py` | realistic market model — exogenous true price + arbitrageurs |
| `agent_opt.py` | the AI-adversary loop (writes/edits `build()`, scores, iterates) |
| `eval_strategy.py` | sandboxed scorer for an AI-written strategy (subprocess-isolated, hardened) |
| `characterize.py` | per-rule behaviour report on the held-out set |
| `DEFINITIONS.md` | precise definition of every rule and metric |
| `results/characterize_result.json` | the seed-97 run: per-rule objective means + each champion's code |

## Caveats

One random draw, one search per rule. Toy constant-product pools, independent (no cross-pool
arbitrage yet). Harm figures are a modeling artifact per the limitations above. Findings are directional
— the rule-ordering (don't average; cap the worst victim) is the robust takeaway; the ~1,240× and ~6×
magnitudes want more seeds before load-bearing use.
