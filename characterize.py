"""
characterize.py — draw results from the FINAL builder algorithm the loop produced per constraint.

For each constraint, load the champion strategy the agent loop converged to (results/strategies/
<slug>_champ.py), RUN it on a held-out block corpus, and characterize what that algorithm actually
does: the harm distribution it inflicts, the value it extracts, how many users it hurts, whether the
blocks it produces also pass the OTHER constraints (cross-acceptance), plus instructive example
blocks and the strategy code itself. This is the result — the behavior of the best builder-under-X —
not the optimizer's score.

Run after the loop finishes (or anytime, on the current champions). Writes results/characterize_result.json.
"""
import os, json, importlib.util, random, re, math
import harness as H
import harness_mp as M

HERE = os.path.dirname(os.path.abspath(__file__))
STRAT = os.path.join(HERE, "results", "strategies")
OUT = os.path.join(HERE, "results", "characterize_result.json")
HOLD_SEED = int(os.environ.get("HOLD_SEED", "97"))
N_HOLD = int(os.environ.get("N_HOLD", "300"))
TAU = 0.005
CONSTRAINTS = list(M.SPINES.keys())


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def load_build(cname):
    path = os.path.join(STRAT, f"{slug(cname)}_champ.py")
    if not os.path.exists(path):
        return None, None
    spec = importlib.util.spec_from_file_location("champ", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.build, open(path).read()

def clone(o):
    return H.Order(o.actor, o.side, o.amount_in, o.ts, o.oid, o.priority, o.pool)

def corpus(seed, n):
    rng = random.Random(seed)
    return [M.random_block(rng) for _ in range(n)]

def val(book, ordering):
    prio = sum(o.priority for o in ordering)
    actors = {o.actor for o in ordering if o.actor not in ("user", "bg")}
    return prio + sum(M.actor_numeraire_pnl(book, ordering, a) for a in actors)

def score_block(book, ing, ordering):
    hs = sorted(M.harm_to_user(book, ordering, o) for o in ing if o.protected())
    t = sum(hs)
    surplus = 0.0
    for o in ing:
        if not o.protected():
            continue
        ref = M.standalone_price(book, o); _, f = M.execute_book(book, ordering); r = f.get(o.oid, 0.0)
        surplus += (r - ref) / ref if ref > 0 else 0.0
    return {
        "worst_user_harm": max(hs, default=0.0),
        "total_slippage": t,
        "n_users_harmed": sum(1 for h in hs if h > TAU),
        "p95_user_harm": hs[min(len(hs) - 1, int(math.ceil(0.95 * len(hs)) - 1))] if hs else 0.0,
        "harm_top1_share": (max(hs) / t) if t > 0 else 0.0,
        "block_value": val(book, ordering),
        "private_extraction": sum(M.actor_numeraire_pnl(book, ordering, a)
                                  for a in {o.actor for o in ordering if o.actor not in ("user", "bg")}),
        "total_user_surplus": surplus,
    }

def run_champ(build, cfg, book, base):
    """Apply the champion; if its block is rejected by its own constraint, fall back to honest."""
    honest = H.baseline_priority_fee(base)
    base_ids = {o.oid for o in base}
    try:
        ordering = build([clone(o) for o in base], {k: M.Pool(p.x, p.y) for k, p in book.items()})
        added = [o for o in ordering if o.oid not in base_ids]
        ing = base + added
        if M.spine_pass(cfg, book, ing, ordering):
            return ing, ordering, True
    except Exception:
        pass
    return base, honest, False


def main():
    hold = corpus(HOLD_SEED, N_HOLD)
    out = {"meta": {"env": "multi-pair", "hold_seed": HOLD_SEED, "n_hold": N_HOLD, "constraints": CONSTRAINTS}, "per_constraint": {}}
    for cname in CONSTRAINTS:
        build, code = load_build(cname)
        if build is None:
            out["per_constraint"][cname] = {"error": "no champion file"}
            continue
        cfg = M.SPINES[cname]
        objsum = {}; cross = {y: 0 for y in CONSTRAINTS}; applied = 0; examples = []
        for book, base in hold:
            ing, block, was_applied = run_champ(build, cfg, book, base)
            applied += 1 if was_applied else 0
            for y in CONSTRAINTS:
                if M.spine_pass(M.SPINES[y], book, ing, block):
                    cross[y] += 1
            s = score_block(book, ing, block)
            for k, v in s.items():
                objsum[k] = objsum.get(k, 0.0) + v
            if s["worst_user_harm"] > 0.05 and len(examples) < 12:
                examples.append({"worst_user_harm": round(s["worst_user_harm"], 4),
                                 "n_users_harmed": s["n_users_harmed"], "block_value": round(s["block_value"], 1)})
        n = len(hold)
        out["per_constraint"][cname] = {
            "champion_code": code,
            "applied_rate": round(applied / n, 3),
            "objective_means": {k: round(v / n, 4) for k, v in objsum.items()},
            "cross_acceptance": {y: round(cross[y] / n, 3) for y in CONSTRAINTS},
            "instructive_examples": examples,
        }
    json.dump(out, open(OUT, "w"), indent=2)
    print("WROTE", OUT)
    for c in CONSTRAINTS:
        pc = out["per_constraint"][c]
        if "objective_means" in pc:
            om = pc["objective_means"]
            print(f"  {c[:26]:26} worst={om['worst_user_harm']} n_harm={om['n_users_harmed']} "
                  f"val={om['block_value']} applied={pc['applied_rate']}")


if __name__ == "__main__":
    main()
