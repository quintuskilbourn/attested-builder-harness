"""
harness_market.py — realistic market model (2026-07-21, v1, NEEDS REVIEW before a full run).

Adds the piece every prior run lacked: an EXOGENOUS off-exchange "true" price that moves independently
of the pools, plus TOP-OF-BLOCK ARBITRAGEURS that re-peg each pool toward the true price each block.
This is the CEX-DEX dynamic, and it lets us measure harm against the TRUE price instead of the pool's
own (stale, manipulable) state — so we can finally distinguish LEGITIMATE arbitrage (moves the pool
toward the true price, doesn't hurt users) from MANIPULATION (a sandwich that moves price away from
true around a user).

Core objects:
  - Market: a true price per token, evolving as geometric Brownian motion between blocks.
  - pools persist across blocks, so at block-start the pool price lags the true price (staleness gap).
  - tob_arb_order(): the legitimate arbitrage that re-pegs a pool to the true price.
  - harm_vs_true(): a user's price shortfall vs the TRUE price (the off-exchange reference).
  - external rule vs pool-baseline rule: the new complementary constraint uses the true price as the
    reference, which the pool-baseline rules cannot.

Reuses harness.Pool / amount_out / Order.
"""
from __future__ import annotations
import math, random
import harness as H
from harness import Pool, Order, amount_out, FEE

# tokens: name -> initial true price (numeraire per token). Varied liquidity via reserve size.
TOKENS = {"ETH": 3000.0, "BTC": 65000.0, "MIDA": 3.0, "ALTB": 0.4, "ALTC": 0.08}
DEPTH = {"ETH": 3000.0, "BTC": 400.0, "MIDA": 200000.0, "ALTB": 500000.0, "ALTC": 1500000.0}  # token reserve


class Market:
    """Exogenous true price per token; GBM step between blocks."""
    def __init__(self):
        self.true = dict(TOKENS)

    def step(self, rng: random.Random, sigma: float = 0.02):
        for t in self.true:
            self.true[t] *= math.exp(sigma * rng.gauss(0.0, 1.0) - 0.5 * sigma * sigma)


def fresh_pools(rng: random.Random, market: Market) -> dict:
    """Pools initialized AT the current true price (used only at t=0; afterwards pools persist)."""
    book = {}
    for t, depth in DEPTH.items():
        x = depth * rng.uniform(0.8, 1.2)
        y = x * market.true[t]  # spot == true at init
        book[t] = Pool(x, y)
    return book


def repeg_amount(pool: Pool, true_price: float):
    """(side, amount_in) for an arb that moves the pool spot to ~true_price (no-fee target; the fee
    makes it land just inside the no-arb band, which is realistic). Returns None if already ~pegged."""
    k = pool.x * pool.y
    s0 = pool.spot()
    if abs(s0 - true_price) / true_price < FEE:  # inside the no-arb band already
        return None
    x_t = math.sqrt(k / true_price)   # target reserves at spot == true
    y_t = math.sqrt(k * true_price)
    if true_price > s0:               # pool token underpriced -> buy token (numeraire in)
        return ("buy", max(0.0, y_t - pool.y))
    else:                              # token overpriced -> sell token (token in)
        return ("sell", max(0.0, x_t - pool.x))


def tob_arb_orders(book: dict, market: Market) -> list:
    """One legitimate top-of-block arb per pool that is off-peg, re-pegging it to the true price."""
    out = []
    for pid, pool in book.items():
        r = repeg_amount(pool, market.true[pid])
        if r is None:
            continue
        side, amt = r
        if amt > 0:
            out.append(Order("arb", side, amt, ts=0, oid=f"arb_{pid}", priority=50.0, pool=pid))
    return out


def execute_book(book0: dict, ordering: list):
    p = {pid: Pool(pl.x, pl.y) for pid, pl in book0.items()}
    fills = {}
    for o in ordering:
        pool = p[o.pool]
        if o.side == "buy":
            out = amount_out(pool.y, pool.x, o.amount_in); pool.y += o.amount_in; pool.x -= out
            fills[o.oid] = out / o.amount_in if o.amount_in else 0.0
        else:
            out = amount_out(pool.x, pool.y, o.amount_in); pool.x += o.amount_in; pool.y -= out
            fills[o.oid] = out / o.amount_in if o.amount_in else 0.0
    return p, fills


def true_priced_pool(pool: Pool, true_price: float) -> Pool:
    """Same liquidity (k) as `pool` but repriced to the true off-exchange price."""
    k = pool.x * pool.y
    return Pool(math.sqrt(k / true_price), math.sqrt(k * true_price))


def harm_vs_true(market: Market, book0: dict, ordering: list, o: Order) -> float:
    """Fractional shortfall of the user's realized price vs the price it WOULD get trading ALONE from a
    pool at the TRUE off-exchange price. This is the poison-resistant reference: it credits the user
    only their own unavoidable slippage (not a staleness windfall, and not a sandwich's damage), so a
    legitimate arb re-peg leaves harm ~0 while a sandwich shows positive harm. Excluded user -> 1.0."""
    ref_pool = true_priced_pool(book0[o.pool], market.true[o.pool])
    _, rf = execute_book({o.pool: ref_pool}, [o])   # user alone, from a true-priced pool
    ref = rf[o.oid]
    _, fills = execute_book(book0, ordering)
    if o.oid not in fills:
        return 1.0
    realized = fills[o.oid]
    return max(0.0, (ref - realized) / ref) if ref > 0 else 0.0


def next_block_pools(book0: dict, ordering: list) -> dict:
    """Pool state after a block (carried to the next block — this is what makes the pool go stale)."""
    p, _ = execute_book(book0, ordering)
    return p


def gen_block(rng: random.Random, market: Market, book: dict, n_users=(4, 10)):
    """Advance the true price, then generate the block's ingress: legitimate ToB arbs (re-peg) + users.
    Returns (arbs, users) — the honest block is arbs-first then users; the builder may sandwich users."""
    market.step(rng)
    arbs = tob_arb_orders(book, market)
    users = []
    pools = list(book.keys())
    ts = 1
    for i in range(rng.randint(*n_users)):
        pid = rng.choice(pools); pool = book[pid]; side = rng.choice(["buy", "sell"])
        amt = (rng.uniform(0.005, 0.04) * pool.y) if side == "buy" else (rng.uniform(0.005, 0.04) * pool.x)
        users.append(Order("user", side, amt, ts=ts, oid=f"u{i}", priority=rng.uniform(1, 20), pool=pid)); ts += 1
    return arbs, users


if __name__ == "__main__":
    # VALIDATION: does measuring harm vs the TRUE price distinguish legit arb from a sandwich,
    # where the pool's own (stale) price cannot?
    rng = random.Random(7)
    market = Market()
    book = fresh_pools(rng, market)
    # advance a few blocks so pools go stale vs the drifting true price
    for _ in range(3):
        arbs, users = gen_block(rng, market, book)
        book = next_block_pools(book, arbs + users)  # honest block, carried forward

    # now a fresh block with a clear staleness gap
    market.step(rng, sigma=0.05)  # a bigger move so the gap is visible
    pid = "ETH"; pool = book[pid]
    s0, true = pool.spot(), market.true[pid]
    print(f"block-start pool spot={s0:.2f} vs true={true:.2f}  gap={(true-s0)/true*100:+.2f}%")

    arb = tob_arb_orders(book, market)  # the legit re-peg
    user = Order("user", "buy", 0.02 * pool.y, ts=1, oid="V", priority=5.0, pool=pid)

    # (A) LEGIT: arb re-pegs, then user trades -> user gets ~fair (own slippage only) -> harm ~0
    honest = arb + [user]
    hA = harm_vs_true(market, book, honest, user)
    print(f"(A) legit arb+user:  true-price oracle harm = {hA*100:.3f}%   (should be ~0)")

    # (B) SANDWICH: builder brackets the user -> user worse than fair -> harm > 0
    front = Order("bld", "buy", 0.5 * user.amount_in, ts=0, oid="F", priority=0.0, pool=pid)
    back = Order("bld", "sell", (0.5 * user.amount_in) / pool.spot(), ts=99, oid="B", priority=0.0, pool=pid)
    sandwich = arb + [front, user, back]
    hB = harm_vs_true(market, book, sandwich, user)
    print(f"(B) arb+SANDWICH:    true-price oracle harm = {hB*100:.3f}%   (should be >0 = caught)")

    # (C) the OLD stale-pool oracle on the SAME legit block: it compares vs the mispriced stale pool,
    #     so it reads the arb's price correction as "harm" -> a FALSE POSITIVE.
    _, sf = execute_book(book, [user])          # user alone from the STALE block-start pool
    stale_ref = sf["V"]
    _, hf = execute_book(book, honest)
    realized_legit = hf["V"]
    old_flag = max(0.0, (stale_ref - realized_legit) / stale_ref)
    print(f"(C) legit block, OLD stale-pool oracle harm = {old_flag*100:.3f}%   (FALSE POSITIVE if >0)")
    print(f"\nRESULT: true-price oracle separates legit ({hA*100:.2f}%) from sandwich ({hB*100:.2f}%); "
          f"the old stale-pool oracle false-positives on the legit block ({old_flag*100:.2f}%).")
