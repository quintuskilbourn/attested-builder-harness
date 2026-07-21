# Constraints and metrics — explicit definitions

Two different kinds of thing. A **constraint** is a gate: it looks at an output block and says
release or withhold. A **metric** is a score: it puts a number on a block for cross-comparison,
it never accepts or rejects. Every constraint here also induces a metric (how far over its own
threshold a block sits), but the list below is what the code uses today.

Notation: the *ingress set* = every order the builder was handed (each has an actor, a side
buy/sell, an input amount, an arrival timestamp, a priority fee). The *block* = an ordering
(and inclusion subset) of those orders. A *protected user* = an order from a real user (not the
builder or a searcher). Prices are exact from the constant-product pool; ε = τ = 0.5%.

## Constraints (accept / reject gates)

The active set (2026-07-20): **causal-bracket removed** (evadable by sybil-splitting the front/back
across two identities, and false-positive-heavy on honest round-trippers → no meaningful defense);
**realized-prefix regret removed** (a no-op — the front-run is already baked into the pre-tx state, so
it accepts every sandwich). The anchor-comparison finding that justifies dropping realized-prefix is
preserved in `docs/builder-spine/spine-harness-findings.md`.

| name | releases the block iff … | what it constrains | known weakness |
|---|---|---|---|
| **structural** | every tx in the block traces to a declared ingress order (provenance) and atomic bundles stay contiguous | *what txs appear* — never *how they're ordered* | with single txs / no bundles it accepts every reordering → kept as the "no price defense" reference (worst-case builder baseline) |
| **regret · block-start · per-user L∞** | the *worst* protected user's regret ≤ ε | per-user price vs a fair baseline | leaks when the baseline itself moves the price (the poisoning gap) |
| **regret · block-start · +sanitized** | same, but the baseline is built after dropping round-trip (likely-manipulation) actors from the ingress | tries to stop a self-funded front-run poisoning the baseline | removes legit arb from the reference → false positives |
| **regret · block-start · dollar-weighted** | Σ(regret × trade-size) / Σ(trade-size) ≤ ε | (control — the "bad aggregation") | a whale's volume dilutes a small user's harm under the threshold |

**"regret" defined once:** for a protected user, `regret = max(0, (baseline_price − realized_price) / baseline_price)`.
`realized_price` is what they got in the block. `baseline_price` is what they'd get in a **priority-fee
greedy ordering of the full ingress**, run from the stated **anchor state** (block-start = top-of-block
pool state; realized-prefix = the pool as it stood just before their swap). Anchor and aggregation are
the two knobs; everything else is held fixed.

## Metrics (scores, for the cross-comparison — no accept/reject)

All harm is vs the block-start standalone (the `[oracle, CEX-DEX-blind]` reference — one yardstick,
not ground truth: no off-chain price, so it can't tell legitimate arb from manipulation and it
over-counts). Grouped by what they measure:

**Harm to users**
| name | value |
|---|---|
| **worst_user_harm** | max over protected users of `(standalone − realized) / standalone` |
| **total_slippage** | sum of that shortfall over all protected users |
| **n_users_harmed** | count of protected users with shortfall > τ (breadth) |
| **p95_user_harm** | 95th-percentile user shortfall (tail, for many-user blocks) |
| **harm_top1_share** | worst-user harm ÷ total harm — 1.0 = it all falls on one victim, ~0 = spread |

**Builder extraction**
| name | value |
|---|---|
| **block_value** | priority fees + builder/searcher net numeraire extraction — what the builder maximizes |
| **private_extraction** | just the builder/searcher's net numeraire take from the flow (excludes fees) = the *predatory* value |

*(extraction share = `private_extraction ÷ block_value` — read it off the two aggregate columns, not as a per-block average.)*

**User quality (the good direction)**
| name | value |
|---|---|
| **total_user_surplus** | signed sum over users of `(realized − standalone)/standalone` — positive = users beat their alone-price (CoW-style surplus); measures execution *quality*, not just harm |

**Diagnostics**
| name | value |
|---|---|
| **whale_masking_gap** | worst-user harm − the dollar-weighted aggregate harm — how much size-weighting hides on this block |
| **cex_adjusted_harm** *(pending)* | harm vs an off-chain reference — needs the exogenous-price extension; the one that would un-blind the oracle |

## Candidates to add (not yet wired — flagged for the column set)

- **regret · external-CEX anchor** — baseline price level from an off-chain reference (fixes the harm
  oracle's CEX-DEX blindness; the biggest missing column).
- **aggregate slippage, non-dollar** — L2 or p95 of the per-user regret vector (robust middle ground
  between worst-case L∞ and the gameable dollar-weighted mean).
- **exclusion-aware** — score a protected order the baseline would fill with positive surplus but the
  block omits, as harm (censorship). Currently exclusion counts as 100% shortfall inside the harm oracle;
  a dedicated column would separate censorship from mispricing.
