"""
harness_mp.py — multi-pair environment (2026-07-21).

Generalizes the single-pool harness to a BOOK of independent constant-product pools of varied
liquidity, with many users per block spread across pools. This is what makes the rule-shape
aggregations (L-infinity vs L2 vs p95 vs dollar-weighted) actually diverge, and exercises
whale-masking, both of which the single-pool run could not.

Scope of v1: pools are INDEPENDENT (no direct cross-pool arb yet — that's the next step). A block is
one global ordering of orders, each targeting its own pool; execution routes each order to its pool.
Harm is per-user vs that user's block-start standalone in its own pool. Priority fees are random
(uniform), per the current decision; a notional-tied variant is queued.

Reuses the exact AMM math, Pool, Order, SpineConfig, and per-pool primitives from harness.py.
"""
from __future__ import annotations
import math, os, random
import harness as H

# Priority-fee model: "uniform" (random, the first-run default) or "notional" (fee proportional to the
# trade's numeraire value, ~a few bps — more realistic: bigger trades bid more to be ordered first).
PRIORITY_MODE = os.environ.get("PRIORITY_MODE", "uniform")


def _priority(notional_num: float, rng: random.Random) -> float:
    if PRIORITY_MODE == "notional":
        return max(0.01, notional_num * rng.uniform(5e-5, 2e-4))
    return rng.uniform(1, 20)
from harness import Pool, Order, SpineConfig, amount_out, baseline_priority_fee

Book = dict  # {pool_id: Pool}


# ---------------- book-level execution ----------------
def copy_book(book: Book) -> Book:
    return {pid: Pool(p.x, p.y) for pid, p in book.items()}


def execute_book(book0: Book, ordering: list[Order]):
    """Apply each order to its own pool, in order. Returns (final_book, fills{oid: exec_price})."""
    book = copy_book(book0)
    fills = {}
    for o in ordering:
        p = book[o.pool]
        if o.side == "buy":
            out = amount_out(p.y, p.x, o.amount_in)
            p.y += o.amount_in; p.x -= out
            fills[o.oid] = out / o.amount_in
        else:
            out = amount_out(p.x, p.y, o.amount_in)
            p.x += o.amount_in; p.y -= out
            fills[o.oid] = out / o.amount_in
    return book, fills


def standalone_price(book0: Book, o: Order) -> float:
    _, f = execute_book(book0, [o])
    return f[o.oid]


def harm_to_user(book0: Book, ordering: list[Order], o: Order) -> float:
    ref = standalone_price(book0, o)
    _, fills = execute_book(book0, ordering)
    if o.oid not in fills:
        return 1.0
    return max(0.0, (ref - fills[o.oid]) / ref) if ref > 0 else 0.0


def actor_numeraire_pnl(book0: Book, ordering: list[Order], actor: str) -> float:
    """Net numeraire PnL of an actor across all pools, marking residual token inventory per-pool at
    the pool's BLOCK-START spot (NOT the final spot — marking at final spot is gameable: draining a
    pool to near-zero reserves makes the mark explode, a fake profit an optimizer will chase. Marking
    at block-start makes drain-and-hold net ~0; only a real unwind (back-leg) realizes a sandwich)."""
    book = copy_book(book0)
    dy = 0.0
    dtok = {pid: 0.0 for pid in book}  # residual token per pool
    for o in ordering:
        p = book[o.pool]
        if o.side == "buy":
            out = amount_out(p.y, p.x, o.amount_in)
            if o.actor == actor:
                dy -= o.amount_in; dtok[o.pool] += out
            p.y += o.amount_in; p.x -= out
        else:
            out = amount_out(p.x, p.y, o.amount_in)
            if o.actor == actor:
                dtok[o.pool] -= o.amount_in; dy += out
            p.x += o.amount_in; p.y -= out
    return dy + sum(dtok[pid] * book0[pid].spot() for pid in book)


def _numeraire_volume(o: Order, book0: Book) -> float:
    return o.amount_in if o.side == "buy" else o.amount_in * book0[o.pool].spot()


# ---------------- constraints (book-aware; mirrors harness.spine_pass) ----------------
def _regret_labels(cfg: SpineConfig, book0: Book, ingress: list[Order], ordering: list[Order]):
    _, realized = execute_book(book0, ordering)
    labels = {}
    for u in [o for o in ingress if o.protected()]:
        if cfg.anchor == "blockstart":
            base_ing = _sanitize(ingress) if cfg.sanitize else ingress
            _, bf = execute_book(book0, baseline_priority_fee(base_ing))
            ref = bf.get(u.oid)
            if ref is None:
                ref = standalone_price(book0, u)
        else:  # realized-prefix
            prefix = []
            for o in ordering:
                if o.oid == u.oid:
                    break
                prefix.append(o)
            pbook, _ = execute_book(book0, prefix)
            ref = standalone_price(pbook, u)
        r = realized.get(u.oid, 0.0)
        labels[u.oid] = max(0.0, (ref - r) / ref) if ref > 0 else 0.0
    return labels


def _round_trip_actors(orders):
    buys, sells = set(), set()
    for o in orders:
        if o.protected():
            continue
        (buys if o.side == "buy" else sells).add((o.actor, o.pool))
    rt = buys & sells
    return {a for a, _ in rt}


def _sanitize(orders):
    rt = _round_trip_actors(orders)
    return [o for o in orders if o.actor not in rt]


def spine_pass(cfg: SpineConfig, book0: Book, ingress: list[Order], ordering: list[Order]) -> bool:
    # structural: provenance only
    ids = {o.oid for o in ingress}
    if not all(o.oid in ids for o in ordering):
        return False
    if cfg.kind == "structural":
        return True
    labels = _regret_labels(cfg, book0, ingress, ordering)
    if not labels:
        return True
    vals = list(labels.values())
    if cfg.agg == "peruser_linf":
        return max(vals) <= cfg.eps
    if cfg.agg == "l2":
        return (sum(v * v for v in vals) / len(vals)) ** 0.5 <= cfg.eps
    if cfg.agg == "p95":
        s = sorted(vals)
        return s[min(len(s) - 1, int(math.ceil(0.95 * len(s)) - 1))] <= cfg.eps
    # volume_summed
    by = {o.oid: o for o in ingress}
    num = sum(r * _numeraire_volume(by[oid], book0) for oid, r in labels.items())
    den = sum(_numeraire_volume(by[oid], book0) for oid in labels)
    return (num / den if den else 0.0) <= cfg.eps


# reuse the active constraint set from harness
SPINES = H.SPINES


def builder_value(book0: Book, ordering: list[Order]) -> float:
    prio = sum(o.priority for o in ordering)
    actors = {o.actor for o in ordering if o.actor not in ("user", "bg")}
    ext = sum(actor_numeraire_pnl(book0, ordering, a) for a in actors)
    return prio + ext


# ---------------- multi-pair block generator ----------------
# pools of varied depth: majors (deep, low slippage) → thin alts (shallow, very sandwichable)
POOL_SPECS = {
    "ETH":  {"x": 3000.0,  "px": 3000.0},   # deep
    "BTC":  {"x": 400.0,   "px": 65000.0},  # deep
    "MIDA": {"x": 200000.0, "px": 3.0},     # mid
    "ALTB": {"x": 500000.0, "px": 0.4},     # thin
    "ALTC": {"x": 1500000.0, "px": 0.08},   # very thin
}


def fresh_book(rng: random.Random) -> Book:
    """A book of pools with jittered depth so blocks vary."""
    book = {}
    for pid, s in POOL_SPECS.items():
        jitter = rng.uniform(0.6, 1.6)
        x = s["x"] * jitter
        y = x * s["px"] * rng.uniform(0.9, 1.1)
        book[pid] = Pool(x, y)
    return book


def random_block(rng: random.Random, min_users=5, max_users=14):
    """Honest flow only (users + bg), many users spread across pools. The builder adds its own legs."""
    book = fresh_book(rng)
    pools = list(book.keys())
    orders = []
    ts = 0
    nu = rng.randint(min_users, max_users)
    for i in range(nu):
        ts += 1
        pid = rng.choice(pools)
        p = book[pid]
        side = rng.choice(["buy", "sell"])
        # size as a fraction of pool depth so slippage is meaningful but not absurd
        amt = (rng.uniform(0.005, 0.05) * p.y) if side == "buy" else (rng.uniform(0.005, 0.05) * p.x)
        notional = amt if side == "buy" else amt * p.spot()
        orders.append(Order("user", side, amt, ts=ts, oid=f"u{i}", priority=_priority(notional, rng), pool=pid))
    for i in range(rng.randint(0, 4)):
        ts += 1
        pid = rng.choice(pools)
        p = book[pid]
        side = rng.choice(["buy", "sell"])
        amt = (rng.uniform(0.003, 0.02) * p.y) if side == "buy" else (rng.uniform(0.003, 0.02) * p.x)
        notional = amt if side == "buy" else amt * p.spot()
        orders.append(Order("bg", side, amt, ts=ts, oid=f"g{i}", priority=_priority(notional, rng), pool=pid))
    return book, orders


if __name__ == "__main__":
    # smoke: build a block, sanity-check execution, harm, and each constraint
    rng = random.Random(1)
    book, ing = random_block(rng)
    honest = baseline_priority_fee(ing)
    _, fills = execute_book(book, honest)
    users = [o for o in ing if o.protected()]
    print(f"pools={list(book)} orders={len(ing)} users={len(users)} pools_used={sorted({o.pool for o in ing})}")
    harms = sorted(harm_to_user(book, honest, u) for u in users)
    print(f"honest per-user harm: min={harms[0]:.4f} med={harms[len(harms)//2]:.4f} max={harms[-1]:.4f}")
    for name, cfg in SPINES.items():
        print(f"  {name:32} honest_pass={spine_pass(cfg, book, ing, honest)}")
    print("value(honest)=", round(builder_value(book, honest), 1))
