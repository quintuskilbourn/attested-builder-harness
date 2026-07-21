"""
eval_strategy.py — safely evaluate an AI-generated builder strategy on the multi-pair sim.

Usage: python3 eval_strategy.py <strategy_file.py> <constraint_name> <seed> <n>
Prints one JSON line: {"mean_val", "ok", "errs", "pass_rate", "last_err"}.

The strategy file must define:  build(base_orders, book) -> ordering
  base_orders : list[Order]  (users + background; each has actor/side/amount_in/ts/oid/priority/pool)
  book        : dict[pool_id -> Pool]  (Pool has .x, .y, .spot())
  returns     : list[Order]  — the full block ordering. To sandwich, add your own legs as
                Order(actor="bld0", side=..., amount_in=..., ts=..., oid="BL0", priority=0.0, pool=<pid>)
                and place them in the returned list. Any order with a new oid is treated as a
                precommitted builder leg (part of ingress). If the block fails the constraint, the
                honest priority-fee ordering is scored instead (so a rejected block earns baseline).

Run in a subprocess with a timeout by the caller; a crash on a block falls back to honest for that
block and is counted in errs.
"""
import sys, json, importlib.util, random
import harness as H
import harness_mp as M


def load_build(path):
    spec = importlib.util.spec_from_file_location("cand", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.build


def corpus(seed, n):
    rng = random.Random(seed)
    return [M.random_block(rng) for _ in range(n)]


def clone_order(o):
    return H.Order(o.actor, o.side, o.amount_in, o.ts, o.oid, o.priority, o.pool)


def main():
    path, cname, seed, n = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    cfg = M.SPINES[cname]
    build = load_build(path)
    data = corpus(seed, n)
    tot = 0.0; ok = 0; errs = 0; passed = 0; last_err = ""
    for book, base in data:
        base_ids = {o.oid for o in base}
        honest = H.baseline_priority_fee(base)
        try:
            base_copy = [clone_order(o) for o in base]
            book_copy = {k: M.Pool(p.x, p.y) for k, p in book.items()}
            ordering = build(base_copy, book_copy)
            if not ordering:
                raise ValueError("empty ordering")
            # reject nonsensical legs (non-finite or absurdly large amounts) — a builder can't submit
            # a trade bigger than any real pool; this closes the float-overflow scoring exploit.
            import math as _mm
            if any((not _mm.isfinite(o.amount_in)) or o.amount_in > 1e12 or o.amount_in < 0 for o in ordering):
                raise ValueError("invalid leg amount")
            added = [o for o in ordering if o.oid not in base_ids]
            ing = base + added
            if M.spine_pass(cfg, book, ing, ordering):
                passed += 1
                block = ordering
            else:
                block = honest
        except Exception as e:  # broken strategy -> honest for this block, count it
            errs += 1
            if not last_err:
                last_err = repr(e)[:300]
            block = honest
        try:
            prio = sum(o.priority for o in block)
            actors = {o.actor for o in block if o.actor not in ("user", "bg")}
            ext = sum(M.actor_numeraire_pnl(book, block, a) for a in actors)
            v = prio + ext
            import math as _m
            if not _m.isfinite(v):
                errs += 1
            else:
                tot += v; ok += 1
        except Exception as e:
            errs += 1
            if not last_err:
                last_err = "score:" + repr(e)[:280]
    import math as _m2
    mv = (tot / len(data)) if data else 0.0
    if not _m2.isfinite(mv):
        mv = -1e18
    print(json.dumps({"mean_val": round(mv, 3), "ok": ok, "errs": errs,
                      "pass_rate": round(passed / len(data), 3) if data else 0.0, "last_err": last_err}))


if __name__ == "__main__":
    main()
