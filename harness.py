"""
Builder-spine attack harness — prototype (2026-07-16).

Turns the assertions in docs/builder-spine/spine-v1-plan.md into measurements on a
self-contained, deterministic single-pool AMM simulation. No reth/rbuilder dependency.

Scope (per Quintus, 2026-07-16):
  - The verifier is handed the AUTHENTICATED ingress set (every order) + arrival timestamps.
    So provenance and arrival-order are GIVENS here, not things to prove.
  - Refunds are out of scope entirely.
  - Single constant-product pool (the tractable setting the ordering-rules literature uses;
    Burak's caveat that multi-pool cycles break these rules is noted, out of scope for v1).

What it measures:
  E1  Structural-only vs regret spines on seeded canonical attacks (does structural catch reordering harm? — no).
  E2  Block-start anchor vs realized-prefix anchor on the sandwich corpus (the plan's headline claim).
  E3  Per-user L-infinity (harm-only) vs volume-summed epsilon (the cross-user-subsidy claim).
  E4  Adversarial ordering search (mini-"AlphaBuilder"): can a searcher find an ordering that the
      spine PASSES yet extracts value and leaves a user worse than block-start-standalone? ("escape rate")
      Run in BOTH settings: S1 (reorder/exclude a known bundle set) and S2 (self-funded injected legs).
  E5  False-positive rate on honest-but-aggressive blocks (legit top-of-block arb paying priority fee).
  E6  Label-diff / disagreement between spines on a mostly-benign random corpus.
  E7  Cost-of-anchoring: builder value forgone when constrained to pass block-start regret vs unconstrained.

Ground truth is INDEPENDENT of any spine: harm_to_user(u) is u's shortfall in execution price versus
u's swap executed ALONE from the block-start pool state (the block-start standalone), and builder
extraction is the numeraire PnL of builder/attacker-controlled actors. Neither reuses a spine's baseline.
"""

from __future__ import annotations
import itertools
import json
import math
import os
import random
from dataclasses import dataclass, field, replace
from typing import Callable

FEE = 0.003  # 0.30% Uniswap-V2-style fee

# ----------------------------------------------------------------------------
# AMM (constant product, exact Uniswap-V2 getAmountOut with fee kept in-pool)
# ----------------------------------------------------------------------------

@dataclass
class Pool:
    """Reserves: x = token, y = numeraire (USDC). Spot price = y / x (numeraire per token)."""
    x: float  # token reserve
    y: float  # numeraire reserve

    def spot(self) -> float:
        return self.y / self.x


def amount_out(reserve_in: float, reserve_out: float, amount_in: float) -> float:
    """Standard V2 getAmountOut: fee taken on the input, fee stays in the pool.
    Hardened against adversarial inputs: a non-finite / negative amount yields 0, and the size is
    capped at 1e13 (>1e6x any realistic pool) so a ~1e308 leg can't overflow float math to inf."""
    if not math.isfinite(amount_in) or amount_in <= 0:
        return 0.0
    amount_in = min(amount_in, 1e13)
    ain = amount_in * (1.0 - FEE)
    out = (ain * reserve_out) / (reserve_in + ain)
    return out if math.isfinite(out) else reserve_out


@dataclass
class Order:
    """One swap intent.
    side='buy'  -> spend `amount_in` numeraire (y) to receive token (x).  Better = MORE token per numeraire.
    side='sell' -> spend `amount_in` token (x) to receive numeraire (y).  Better = MORE numeraire per token.
    """
    actor: str            # identity: 'user', 'attacker', 'builder', 'bg' (benign background)
    side: str             # 'buy' | 'sell'
    amount_in: float      # in numeraire for buy, in token for sell
    ts: int               # arrival timestamp (lower = earlier)
    oid: str = ""         # order id
    priority: float = 0.0 # priority fee (numeraire) — used by the priority-fee baseline & value
    pool: str = "P0"      # which pool this order trades in (multi-pair; single-pool ignores it)

    def protected(self) -> bool:
        # In this prototype every user swap is "protected flow"; attacker/builder/bg legs are not.
        return self.actor == "user"


def execute(pool: Pool, ordering: list[Order]) -> tuple[Pool, dict[str, float]]:
    """Execute an ordering against a COPY of the pool.
    Returns (final_pool, fills) where fills[oid] = execution price:
      buy:  token_out / numeraire_in   (token per numeraire; higher is better for the buyer)
      sell: numeraire_out / token_in   (numeraire per token; higher is better for the seller)
    """
    p = Pool(pool.x, pool.y)
    fills: dict[str, float] = {}
    for o in ordering:
        if o.side == "buy":
            out = amount_out(p.y, p.x, o.amount_in)  # numeraire in -> token out
            p.y += o.amount_in
            p.x -= out
            fills[o.oid] = out / o.amount_in
        else:  # sell
            out = amount_out(p.x, p.y, o.amount_in)  # token in -> numeraire out
            p.x += o.amount_in
            p.y -= out
            fills[o.oid] = out / o.amount_in
    return p, fills


def standalone_price(pool: Pool, o: Order) -> float:
    """Execution price o would get executed ALONE from the given pool state (block-start standalone)."""
    _, f = execute(pool, [o])
    return f[o.oid]


# ----------------------------------------------------------------------------
# Independent ground-truth oracle (does NOT use any spine baseline)
# ----------------------------------------------------------------------------

def harm_to_user(pool0: Pool, ordering: list[Order], o: Order) -> float:
    """Fractional shortfall of user order o vs its block-start STANDALONE execution.
    Side-agnostic: for both sides a HIGHER execution-price metric is better, so
    harm = max(0, (standalone - realized) / standalone). 0 means no harm (or better than standalone).
    """
    ref = standalone_price(pool0, o)
    _, fills = execute(pool0, ordering)
    if o.oid not in fills:
        return 1.0  # excluded from the block -> total shortfall (the exclusion-harm case)
    realized = fills[o.oid]
    return max(0.0, (ref - realized) / ref)


def actor_numeraire_pnl(pool0: Pool, ordering: list[Order], actor: str) -> float:
    """Net numeraire PnL of an actor across all their legs, marking any residual token
    inventory at the FINAL pool spot (so a pure price-move with flat inventory nets ~0,
    and a genuine sandwich that ends flat in token shows positive numeraire extraction)."""
    p = Pool(pool0.x, pool0.y)
    dy = 0.0  # numeraire delta for actor (+ = received)
    dx = 0.0  # token delta for actor (+ = received)
    for o in ordering:
        if o.side == "buy":
            out = amount_out(p.y, p.x, o.amount_in)
            if o.actor == actor:
                dy -= o.amount_in
                dx += out
            p.y += o.amount_in
            p.x -= out
        else:
            out = amount_out(p.x, p.y, o.amount_in)
            if o.actor == actor:
                dx -= o.amount_in
                dy += out
            p.x += o.amount_in
            p.y -= out
    return dy + dx * pool0.spot()  # mark residual token at BLOCK-START spot (final spot is gameable)


# ----------------------------------------------------------------------------
# Baselines (counterfactual orderings the spine compares against)
# ----------------------------------------------------------------------------

def baseline_priority_fee(orders: list[Order]) -> list[Order]:
    """The IRG-default baseline (Bruno's Good/Bad-Algo doc): priority-fee descending,
    ties broken by earlier arrival then oid for determinism."""
    return sorted(orders, key=lambda o: (-o.priority, o.ts, o.oid))


def baseline_arrival(orders: list[Order]) -> list[Order]:
    """FCFS / arrival-order baseline (legible but a weaker counterfactual per the IRG verdict)."""
    return sorted(orders, key=lambda o: (o.ts, o.oid))


BASELINES: dict[str, Callable[[list[Order]], list[Order]]] = {
    "priority": baseline_priority_fee,
    "arrival": baseline_arrival,
}


# ----------------------------------------------------------------------------
# Candidate spines
# ----------------------------------------------------------------------------

@dataclass
class SpineConfig:
    name: str
    kind: str                 # 'structural' | 'regret' | 'causal'
    anchor: str = "blockstart"  # 'blockstart' | 'prefix'   (regret only)
    agg: str = "peruser_linf"   # 'peruser_linf' | 'volume_summed'  (regret only)
    baseline: str = "priority"  # which baseline ordering    (regret only)
    eps: float = 0.005          # 0.50% acceptable-worse bound
    sanitize: bool = False      # run the baseline on provenance-sanitized ingress (drop round-trip actors)


def round_trip_actors(orders: list[Order]) -> set[str]:
    """Non-protected actors that submit BOTH a buy and a sell — a round trip, the structural
    signature of a sandwicher / self-funded manipulator. Identity + side only (we are given both)."""
    buys, sells = {}, {}
    for o in orders:
        if o.protected():
            continue
        (buys if o.side == "buy" else sells).setdefault(o.actor, 0)
        (buys if o.side == "buy" else sells)[o.actor] += 1
    return set(buys) & set(sells)


def sanitize_ingress(orders: list[Order]) -> list[Order]:
    """Drop round-trip (likely-manipulation) non-protected flow before building the baseline."""
    rt = round_trip_actors(orders)
    return [o for o in orders if o.actor not in rt]


def causal_bracket_ok(ingress: list[Order], ordering: list[Order]) -> bool:
    """Vitaliy's identity-aware causal check: WITHHOLD if some non-protected actor round-trips
    (a leg strictly before AND a leg strictly after a protected user's swap, on OPPOSITE sides).
    Side-agnostic so it catches both buy-victim (attacker buy-before/sell-after) and sell-victim
    (attacker sell-before/buy-after) sandwiches. Needs only ordering + actor identity — both given.
    Evasion (by construction): split the front/back across DISTINCT signers (sybil) -> misses."""
    pos = {o.oid: i for i, o in enumerate(ordering)}
    users = [o for o in ordering if o.protected()]
    for u in users:
        ui = pos[u.oid]
        for actor in {o.actor for o in ordering if not o.protected()}:
            before = {o.side for o in ordering if o.actor == actor and pos[o.oid] < ui}
            after = {o.side for o in ordering if o.actor == actor and pos[o.oid] > ui}
            if ("buy" in before and "sell" in after) or ("sell" in before and "buy" in after):
                return False  # round-trip straddling the user -> withhold
    return True


def structural_ok(ingress: list[Order], ordering: list[Order]) -> bool:
    """Structural checks that survive the locked scope: provenance (every output order came through
    ingress — trivially true here since we hand the spine the ingress set) and contiguity for any
    atomic bundle. With single-tx orders and no bundles there is nothing reordering-sensitive to catch,
    which is exactly the point: structural rules do NOT constrain sandwiching-by-reordering."""
    ingress_ids = {o.oid for o in ingress}
    return all(o.oid in ingress_ids for o in ordering)


def regret_labels(cfg: SpineConfig, pool0: Pool, ingress: list[Order],
                  ordering: list[Order]) -> dict[str, float]:
    """Per-user harm-only regret of each protected order under the chosen anchor + baseline.
    regret_u = max(0, (baseline_price_u - realized_price_u) / baseline_price_u)."""
    included = [o for o in ordering]
    _, realized_fills = execute(pool0, included)

    labels: dict[str, float] = {}
    # Evaluate every protected order in the INGRESS (not just those included), so an EXCLUDED
    # protected order that the baseline would fill is scored as harm (realized fill = 0 -> regret 1.0).
    for u in [o for o in ingress if o.protected()]:
        if cfg.anchor == "blockstart":
            # Baseline = baseline-algo ordering of the (optionally sanitized) ingress, from block start.
            base_ingress = sanitize_ingress(ingress) if cfg.sanitize else ingress
            base_ord = BASELINES[cfg.baseline](base_ingress)
            _, base_fills = execute(pool0, base_ord)
            ref = base_fills.get(u.oid)
            if ref is None:  # baseline excluded it (shouldn't for full ingress) -> fall back to standalone
                ref = standalone_price(pool0, u)
        else:  # 'prefix' — anchor to the realized prefix state just before u (the gameable anchor)
            prefix = []
            for o in included:
                if o.oid == u.oid:
                    break
                prefix.append(o)
            p_prefix, _ = execute(pool0, prefix)
            # u's counterfactual = execute u alone from the realized-prefix state
            ref = standalone_price(p_prefix, u)
        realized = realized_fills.get(u.oid, 0.0)  # excluded protected order -> realized fill = 0
        labels[u.oid] = max(0.0, (ref - realized) / ref) if ref > 0 else 0.0
    return labels


def spine_pass(cfg: SpineConfig, pool0: Pool, ingress: list[Order],
               ordering: list[Order]) -> bool:
    """True = spine RELEASES the block (no violation). False = withhold."""
    if not structural_ok(ingress, ordering):
        return False
    if cfg.kind == "structural":
        return True
    if cfg.kind == "causal":
        return causal_bracket_ok(ingress, ordering)
    labels = regret_labels(cfg, pool0, ingress, ordering)
    if not labels:
        return True
    vals = list(labels.values())
    if cfg.agg == "peruser_linf":
        # every user's harm must be within eps (L-infinity over users)
        return max(vals) <= cfg.eps
    if cfg.agg == "l2":
        # root-mean-square of the per-user regret vector (big harms dominate; whale can't average it down)
        return (sum(v * v for v in vals) / len(vals)) ** 0.5 <= cfg.eps
    if cfg.agg == "p95":
        # 95th-percentile user regret (robust to a couple of outliers; protects the bulk-worst)
        s = sorted(vals)
        return s[min(len(s) - 1, int(math.ceil(0.95 * len(s)) - 1))] <= cfg.eps
    else:  # 'volume_summed' — volume-weighted sum of harm within eps * total volume
        num = 0.0
        den = 0.0
        by_oid = {o.oid: o for o in ingress}
        for oid, r in labels.items():
            vol = _numeraire_volume(by_oid[oid], pool0)
            num += r * vol
            den += vol
        return (num / den if den else 0.0) <= cfg.eps


def _numeraire_volume(o: Order, pool0: Pool) -> float:
    """Approximate numeraire notional of an order (for volume weighting)."""
    if o.side == "buy":
        return o.amount_in
    return o.amount_in * pool0.spot()


# Active constraint set (2026-07-20: causal-bracket removed — evadable + false-positive-heavy;
# realized-prefix removed — a no-op that accepts everything; the anchor-comparison finding is
# preserved in docs/builder-spine/spine-harness-findings.md). The causal/prefix code paths remain
# below for reference but are not in the set the optimizer runs against.
SPINES = {
    "Spine-R (structural)":         SpineConfig("Spine-R (structural)", "structural"),
    "regret blockstart L-inf":      SpineConfig("regret blockstart L-inf", "regret", "blockstart", "peruser_linf"),
    "regret blockstart L-inf +sanit": SpineConfig("regret blockstart L-inf +sanit", "regret", "blockstart", "peruser_linf", sanitize=True),
    "regret blockstart L2":          SpineConfig("regret blockstart L2", "regret", "blockstart", "l2"),
    "regret blockstart p95":         SpineConfig("regret blockstart p95", "regret", "blockstart", "p95"),
    "regret blockstart vol-summed": SpineConfig("regret blockstart vol-summed", "regret", "blockstart", "volume_summed"),
}


# ----------------------------------------------------------------------------
# Scenario generators
# ----------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    pool0: Pool
    ingress: list[Order]
    honest_ordering: list[Order]        # what a benign builder would produce (baseline)
    attack_ordering: list[Order] | None # the adversarial ordering (None for honest-only scenarios)
    setting: str                        # 'S1' | 'S2'
    is_attack: bool                     # ground-truth: does attack_ordering harm a protected user?


def _oid(prefix, i):
    return f"{prefix}{i}"


def seeded_scenarios() -> list[Scenario]:
    sc: list[Scenario] = []
    pool = Pool(x=1000.0, y=2_000_000.0)  # spot 2000 numeraire/token (think ETH/USDC)

    # --- S1 sandwich: attacker submitted front(buy) + back(sell) as SEPARATE orders; a victim buys.
    #     A malicious builder orders [front, victim, back]; a benign builder (priority/arrival) does not.
    victim = Order("user", "buy", 200_000.0, ts=2, oid="V", priority=5.0)
    front = Order("attacker", "buy", 150_000.0, ts=1, oid="F", priority=1.0)
    back = Order("attacker", "sell", _match_token(pool, 150_000.0), ts=3, oid="B", priority=1.0)
    ingress = [front, victim, back]
    attack = [front, victim, back]                        # sandwich
    honest = baseline_priority_fee(ingress)               # victim's high priority => not bracketed
    sc.append(Scenario("S1 sandwich (separate legs)", pool, ingress, honest, attack, "S1", True))

    # --- S2 sandwich: builder self-funds & injects front/back (legitimate ingress it authored).
    b_front = Order("builder", "buy", 150_000.0, ts=1, oid="BF", priority=0.0)
    b_back = Order("builder", "sell", _match_token(pool, 150_000.0), ts=3, oid="BB", priority=0.0)
    ingress2 = [b_front, victim, b_back]
    attack2 = [b_front, victim, b_back]
    honest2 = [victim]                                    # benign: just the victim, no builder legs
    sc.append(Scenario("S2 sandwich (self-funded)", pool, ingress2, honest2, attack2, "S2", True))

    # --- Split-trade dilution: victim's harm hidden by splitting the front-run across many small legs
    #     (tests whether per-intent vs aggregate matters; here it feeds E-agg via a whale, see below).
    #     Each front leg uses a DISTINCT signer (sybil) so identity-based causal detection misses it.
    legs = [Order(f"atk{i}", "buy", 30_000.0, ts=1, oid=f"SF{i}", priority=1.0) for i in range(5)]
    back_s = Order("atk_back", "sell", _match_token(pool, 150_000.0), ts=3, oid="SB", priority=1.0)
    ingress3 = legs + [victim, back_s]
    attack3 = legs + [victim, back_s]
    sc.append(Scenario("S1 split-front sandwich (sybil)", pool, ingress3, baseline_priority_fee(ingress3), attack3, "S1", True))

    # --- Exclusion (censorship), ISOLATED from sandwiching: two protected users the baseline both
    #     fills; the builder simply DROPS one. Ground-truth harm = the excluded user gets nothing vs a
    #     positive-surplus baseline fill. No bracketing here, so only a full-ingress check can catch it.
    uA = Order("user", "buy", 60_000.0, ts=1, oid="UA", priority=6.0)
    uB = Order("user", "buy", 60_000.0, ts=2, oid="UB", priority=5.0)
    ingressE = [uA, uB]
    attackE = [uA]                            # UB censored; UA executes normally (not sandwiched)
    honestE = baseline_priority_fee(ingressE)
    sc.append(Scenario("S1 exclusion (censorship)", pool, ingressE, honestE, attackE, "S1", True))

    # --- HONEST top-of-block arb (should NOT be flagged): an arbitrageur buys cheap at ToB paying
    #     high priority; the user's own swap executes at ~standalone. No user is harmed.
    arb = Order("attacker", "buy", 40_000.0, ts=1, oid="A", priority=50.0)  # high priority = legit ToB
    u_alone = Order("user", "sell", 30.0, ts=2, oid="US", priority=5.0)
    ingressH = [arb, u_alone]
    honestH = baseline_priority_fee(ingressH)
    sc.append(Scenario("honest ToB arb (no victim)", pool, ingressH, honestH, honestH, "S1", False))

    # --- HONEST multi-user block: several users trade; benign price movement, no bracketing.
    us = [Order("user", "buy" if i % 2 == 0 else "sell",
                20_000.0 if i % 2 == 0 else 10.0, ts=i, oid=f"MU{i}", priority=float(10 - i))
          for i in range(6)]
    ingressM = us
    honestM = baseline_priority_fee(ingressM)
    sc.append(Scenario("honest multi-user", pool, ingressM, honestM, honestM, "S1", False))

    return sc


def _match_token(pool: Pool, numeraire_front: float) -> float:
    """Token amount an attacker holds after front-running with `numeraire_front` (so the back-leg
    sells roughly the token acquired). Approximate at block-start spot — good enough for seeds."""
    return numeraire_front / pool.spot()


def random_block(rng: random.Random, embed_attack_prob: float = 0.35) -> Scenario:
    """A mostly-benign random block; with some probability embed a sandwich opportunity."""
    x = rng.uniform(500, 5000)
    y = x * rng.uniform(800, 3000)
    pool = Pool(x, y)
    n_users = rng.randint(2, 5)
    orders: list[Order] = []
    ts = 0
    for i in range(n_users):
        ts += 1
        side = rng.choice(["buy", "sell"])
        amt = (rng.uniform(0.01, 0.06) * y) if side == "buy" else (rng.uniform(0.01, 0.06) * x)
        orders.append(Order("user", side, amt, ts=ts, oid=f"u{i}", priority=rng.uniform(1, 20)))
    # benign background flow
    for i in range(rng.randint(0, 3)):
        ts += 1
        side = rng.choice(["buy", "sell"])
        amt = (rng.uniform(0.005, 0.03) * y) if side == "buy" else (rng.uniform(0.005, 0.03) * x)
        orders.append(Order("bg", side, amt, ts=ts, oid=f"g{i}", priority=rng.uniform(1, 20)))

    is_attack = rng.random() < embed_attack_prob
    attack_ordering = None
    if is_attack and any(o.actor == "user" for o in orders):
        victim = rng.choice([o for o in orders if o.actor == "user"])
        # front leg SAME side as victim (amplifies its price impact), back leg OPPOSITE (unwind)
        if victim.side == "buy":
            f_amt = rng.uniform(0.4, 1.2) * victim.amount_in           # numeraire
            f = Order("attacker", "buy", f_amt, ts=0, oid="af", priority=rng.uniform(1, 20))
            b = Order("attacker", "sell", f_amt / pool.spot(), ts=99, oid="ab", priority=rng.uniform(1, 20))
        else:  # sell victim: attacker sells before (pushes price down), buys back after
            f_amt = rng.uniform(0.4, 1.2) * victim.amount_in           # token
            f = Order("attacker", "sell", f_amt, ts=0, oid="af", priority=rng.uniform(1, 20))
            b = Order("attacker", "buy", f_amt * pool.spot(), ts=99, oid="ab", priority=rng.uniform(1, 20))
        orders = orders + [f, b]
        # attack ordering: front, victim, then the rest (victim bracketed)
        rest = [o for o in orders if o.oid not in ("af", "ab", victim.oid)]
        attack_ordering = [f, victim] + rest + [b]
    ingress = orders
    honest = baseline_priority_fee(ingress)
    # ground truth for the random corpus: attack scenario harms victim iff harm(victim) > tau under attack
    return Scenario("random", pool, ingress, honest,
                    attack_ordering, "S1", bool(attack_ordering))


# ----------------------------------------------------------------------------
# Adversarial ordering search ("mini-AlphaBuilder")
# ----------------------------------------------------------------------------

def all_orderings(ingress: list[Order], allow_exclusion: bool, cap: int = 40320):
    """Yield candidate orderings (permutations, optionally with any subset excluded).
    Capped for tractability; for n<=7 exhaustive permutations = 5040."""
    n = len(ingress)
    if n > 8:
        return  # caller should use a heuristic instead
    subsets = [ingress]
    if allow_exclusion:
        subsets = []
        for r in range(n, 0, -1):
            for combo in itertools.combinations(ingress, r):
                subsets.append(list(combo))
    count = 0
    for sub in subsets:
        for perm in itertools.permutations(sub):
            yield list(perm)
            count += 1
            if count >= cap:
                return


def builder_value(pool0: Pool, ordering: list[Order]) -> float:
    """Proxy block value the builder maximizes: total priority fees of included orders
    + builder/attacker numeraire extraction. (The bidding proxy; enough to model the incentive.)"""
    prio = sum(o.priority for o in ordering)
    extract = actor_numeraire_pnl(pool0, ordering, "builder") + actor_numeraire_pnl(pool0, ordering, "attacker")
    return prio + extract


def search_escape(cfg: SpineConfig, sc: Scenario, tau: float = 0.005,
                  allow_exclusion: bool = True) -> dict:
    """Red-team win: find an ordering the spine PASSES that (a) yields positive builder value
    beyond the honest baseline AND (b) leaves some protected user harmed > tau vs block-start standalone.
    Returns the best such 'escape' (max builder value) or None."""
    pool0 = sc.pool0
    honest_val = builder_value(pool0, sc.honest_ordering)
    best = None
    n = len(sc.ingress)
    if n <= 8:
        candidates = all_orderings(sc.ingress, allow_exclusion=allow_exclusion)
    else:
        candidates = _greedy_candidates(cfg, sc)
    for ordering in candidates:
        if not spine_pass(cfg, pool0, sc.ingress, ordering):
            continue
        # is any protected INGRESS user harmed beyond tau vs block-start standalone (incl. exclusion)?
        harmed = max((harm_to_user(pool0, ordering, o) for o in sc.ingress if o.protected()), default=0.0)
        if harmed <= tau:
            continue
        val = builder_value(pool0, ordering)
        if val <= honest_val + 1e-9:
            continue  # not profitable vs behaving honestly
        if best is None or val > best["value"]:
            best = {"value": val, "honest_value": honest_val, "max_user_harm": harmed,
                    "ordering": [o.oid for o in ordering]}
    return best


def _greedy_candidates(cfg, sc, tries: int = 400):
    """Heuristic for larger blocks: random shuffles + targeted bracketing around each victim."""
    rng = random.Random(hash(sc.name) & 0xffffffff)
    ing = sc.ingress
    for _ in range(tries):
        o = ing[:]
        rng.shuffle(o)
        yield o
    # targeted brackets
    users = [o for o in ing if o.protected()]
    atk = [o for o in ing if o.actor in ("attacker", "builder")]
    for v in users:
        for f in atk:
            for b in atk:
                if f.oid == b.oid:
                    continue
                rest = [o for o in ing if o.oid not in (v.oid, f.oid, b.oid)]
                yield [f, v] + rest + [b]


# ----------------------------------------------------------------------------
# Experiments
# ----------------------------------------------------------------------------

def run_seeded(scs: list[Scenario]) -> list[dict]:
    """E1/E2: for each seeded scenario, does each spine PASS or WITHHOLD the attack ordering?
    A correct spine WITHHOLDS a true attack and RELEASES an honest ordering."""
    rows = []
    for sc in scs:
        order = sc.attack_ordering if sc.attack_ordering is not None else sc.honest_ordering
        gt = "ATTACK" if sc.is_attack else "HONEST"
        for name, cfg in SPINES.items():
            passed = spine_pass(cfg, sc.pool0, sc.ingress, order)
            # correct = withhold attacks (pass=False good), release honest (pass=True good)
            correct = (not passed) if sc.is_attack else passed
            rows.append({"scenario": sc.name, "setting": sc.setting, "ground_truth": gt,
                         "spine": name, "released": passed, "correct": correct})
    return rows


def run_agg_experiment() -> list[dict]:
    """E3: whale+victim block. A benign whale's volume lets a volume-summed epsilon absorb real harm
    to a small victim that a per-user L-infinity bound catches."""
    rows = []
    pool = Pool(1000.0, 2_000_000.0)
    victim = Order("user", "buy", 20_000.0, ts=2, oid="V", priority=5.0)
    whale = Order("user", "buy", 600_000.0, ts=3, oid="W", priority=5.0)  # big benign flow, ~no harm
    front = Order("attacker", "buy", 60_000.0, ts=1, oid="F", priority=1.0)
    back = Order("attacker", "sell", 60_000.0 / pool.spot(), ts=4, oid="B", priority=1.0)
    ingress = [front, victim, whale, back]
    attack = [front, victim, back, whale]  # bracket the small victim; whale executes later, ~unharmed
    gt_harm_v = harm_to_user(pool, attack, victim)
    for name in ["regret blockstart L-inf", "regret blockstart vol-summed"]:
        cfg = SPINES[name]
        passed = spine_pass(cfg, pool, ingress, attack)
        rows.append({"experiment": "E3 whale-masking", "spine": name,
                     "released": passed, "victim_harm_frac": round(gt_harm_v, 5),
                     "correct_withhold": (not passed)})
    return rows


def run_random_corpus(n: int, seed: int) -> dict:
    """E4/E5/E6: over a random corpus measure per-spine detection, false-positive, disagreement,
    and adversarial escape rate."""
    rng = random.Random(seed)
    spine_names = list(SPINES.keys())
    # detection/FP on the honest-vs-attack labeling
    stats = {s: {"tp": 0, "fn": 0, "fp": 0, "tn": 0} for s in spine_names}
    escape_counts = {s: 0 for s in spine_names}
    escape_denom = {s: 0 for s in spine_names}
    escape_harm = {s: [] for s in spine_names}  # magnitude of harm that slipped through
    disagree = 0
    total = 0
    forgone = []            # E7 forgone value on HONEST blocks
    forbidden_extract = []  # E7 extraction the anchor forbids on ATTACK blocks
    base_vs_standalone = [] # baseline-vs-clean-standalone divergence (the "poisoning gap")

    for _ in range(n):
        sc = random_block(rng)
        order = sc.attack_ordering if sc.attack_ordering is not None else sc.honest_ordering
        # ground truth: attack scenario is a true-harm only if it actually harms the victim > tau
        tau = 0.005
        true_harm = False
        if sc.is_attack:
            true_harm = max((harm_to_user(sc.pool0, order, o) for o in sc.ingress if o.protected()), default=0) > tau
        labels = {}
        for s in spine_names:
            cfg = SPINES[s]
            released = spine_pass(cfg, sc.pool0, sc.ingress, order)
            labels[s] = released
            if true_harm:
                if released:
                    stats[s]["fn"] += 1
                else:
                    stats[s]["tp"] += 1
            else:
                if released:
                    stats[s]["tn"] += 1
                else:
                    stats[s]["fp"] += 1
        if len(set(labels.values())) > 1:
            disagree += 1
        total += 1

        # baseline-vs-standalone divergence (the poisoning gap) for any victim in an attack block
        if sc.is_attack:
            for u in [o for o in sc.ingress if o.protected()]:
                base_ord = baseline_priority_fee(sc.ingress)
                _, bf = execute(sc.pool0, base_ord)
                if u.oid in bf:
                    std = standalone_price(sc.pool0, u)
                    base_vs_standalone.append(round(abs(bf[u.oid] - std) / std, 5))

        # E4 escape search (only where an adversary actor exists and block is small enough)
        if any(o.actor not in ("user", "bg") for o in sc.ingress) and len(sc.ingress) <= 7:
            for s in spine_names:
                escape_denom[s] += 1
                esc = search_escape(SPINES[s], sc, tau=tau, allow_exclusion=False)
                if esc is not None:
                    escape_counts[s] += 1
                    escape_harm[s].append(round(esc["max_user_harm"], 5))

        # E7 cost of anchoring: on HONEST blocks, forgone value; on ATTACK blocks, extraction forbidden
        if len(sc.ingress) <= 7:
            if not sc.is_attack:
                fv = _forgone_value(sc)
                if fv is not None:
                    forgone.append(fv)
            else:
                fe = _forbidden_extraction(sc)
                if fe is not None:
                    forbidden_extract.append(fe)

    def rates(d):
        tp, fn, fp, tn = d["tp"], d["fn"], d["fp"], d["tn"]
        recall = tp / (tp + fn) if (tp + fn) else None
        fpr = fp / (fp + tn) if (fp + tn) else None
        return {"tp": tp, "fn": fn, "fp": fp, "tn": tn,
                "recall": None if recall is None else round(recall, 3),
                "fpr": None if fpr is None else round(fpr, 3)}

    return {
        "n_blocks": total,
        "detection": {s: rates(stats[s]) for s in spine_names},
        "escape_rate": {s: (round(escape_counts[s] / escape_denom[s], 3) if escape_denom[s] else None)
                        for s in spine_names},
        "escape_denom": escape_denom,
        "escape_harm_frac": {s: _summ(escape_harm[s]) for s in spine_names},
        "disagreement_rate": round(disagree / total, 3) if total else None,
        "baseline_vs_standalone_gap": _summ(base_vs_standalone),
        "forgone_value_bps_honest": _summ(forgone),
        "forbidden_extraction_bps_attack": _summ(forbidden_extract),
    }


def _forbidden_extraction(sc: Scenario):
    """On an attack block: builder value of the best ordering the block-start L-inf spine WITHHOLDS
    minus the best it RELEASES, as bps of the released optimum — i.e., the extraction the anchor forbids."""
    cfg = SPINES["regret blockstart L-inf"]
    best_pass = None
    best_block = None
    for ordering in all_orderings(sc.ingress, allow_exclusion=False):
        v = builder_value(sc.pool0, ordering)
        released = spine_pass(cfg, sc.pool0, sc.ingress, ordering)
        if released:
            if best_pass is None or v > best_pass:
                best_pass = v
        else:
            if best_block is None or v > best_block:
                best_block = v
    if best_pass is None or best_block is None or best_pass <= 0:
        return None
    return round(10_000 * (best_block - best_pass) / abs(best_pass), 2)


def _forgone_value(sc: Scenario):
    """E7: builder value of the unconstrained max-value ordering minus the max-value ordering that
    passes the block-start L-inf spine, as a fraction of unconstrained (only meaningful when the
    unconstrained optimum would be withheld)."""
    cfg = SPINES["regret blockstart L-inf"]
    best_uncon = None
    best_pass = None
    for ordering in all_orderings(sc.ingress, allow_exclusion=False):
        v = builder_value(sc.pool0, ordering)
        if best_uncon is None or v > best_uncon:
            best_uncon = v
        if spine_pass(cfg, sc.pool0, sc.ingress, ordering):
            if best_pass is None or v > best_pass:
                best_pass = v
    if best_uncon is None or best_pass is None or best_uncon <= 0:
        return None
    return round(10_000 * (best_uncon - best_pass) / abs(best_uncon), 2)  # in bps of block value


def _summ(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return {"n": len(xs), "min": xs[0], "median": xs[len(xs) // 2], "max": xs[-1],
            "mean": round(sum(xs) / len(xs), 2)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="random corpus size")
    ap.add_argument("--seed", type=int, default=20260716)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "results"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    scs = seeded_scenarios()
    seeded = run_seeded(scs)
    agg = run_agg_experiment()
    corpus = run_random_corpus(args.n, args.seed)

    results = {
        "meta": {"fee": FEE, "n_random": args.n, "seed": args.seed, "spines": list(SPINES.keys())},
        "E1_E2_seeded": seeded,
        "E3_aggregation": agg,
        "E4_E7_corpus": corpus,
    }
    outpath = os.path.join(args.out, "results.json")
    with open(outpath, "w") as f:
        json.dump(results, f, indent=2)
    print("WROTE", outpath)
    # console summary
    print("\n=== E1/E2 seeded (correct = withhold attack / release honest) ===")
    for r in seeded:
        print(f"  {r['ground_truth']:6} {r['setting']:2} {r['scenario']:28} | {r['spine']:32} "
              f"released={r['released']!s:5} correct={r['correct']}")
    print("\n=== E3 aggregation (whale-masking; correct_withhold=True means it caught the victim harm) ===")
    for r in agg:
        print(f"  {r['spine']:32} released={r['released']!s:5} victim_harm={r['victim_harm_frac']} correct_withhold={r['correct_withhold']}")
    print("\n=== E4/E5/E6/E7 corpus ===")
    print("  detection:", json.dumps(corpus["detection"], indent=2))
    print("  escape_rate:", corpus["escape_rate"], "denom:", corpus["escape_denom"])
    print("  escape_harm_frac:", json.dumps(corpus["escape_harm_frac"]))
    print("  disagreement_rate:", corpus["disagreement_rate"])
    print("  baseline_vs_standalone_gap:", corpus["baseline_vs_standalone_gap"])
    print("  forgone_value_bps_honest:", corpus["forgone_value_bps_honest"])
    print("  forbidden_extraction_bps_attack:", corpus["forbidden_extraction_bps_attack"])


if __name__ == "__main__":
    main()
