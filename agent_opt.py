"""
agent_opt.py — the AI builder-optimizer loop.

For each fairness rule, an AI agent repeatedly writes a builder-STRATEGY (arbitrary Python
`build(base, book)` code). Each candidate is scored on the multi-pool sim (mean block value subject
to the rule accepting the block), the score + errors feed back, and the agent iterates — keeping the
best. This is the open-ended search a fixed-knob grid can't do: the strategy space is "any ordering
algorithm," which is where hours of optimization actually go. Precommit-only (the strategy adds its
own legs but can't read a victim's future content). Value-max. Structure: train to optimize,
held-out seed to validate; bounded budget + early-stop; incremental ledger; per-rule isolation.

Generator: by default this shells out to a headless model CLI (`claude -p`). Swap `call_codex` for
any code-writing model you like (OpenAI codex, a local model, an API call). Progress is written to
`results/agent_ledger.jsonl` + a heartbeat file; wire your own notifier into `tg()` if you want one.
"""
import os, re, json, time, subprocess, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
STRAT = os.path.join(RESULTS, "strategies")
os.makedirs(STRAT, exist_ok=True)
LEDGER = os.path.join(RESULTS, "agent_ledger.jsonl")
HEARTBEAT = os.path.join(RESULTS, "heartbeat_agent.txt")
OUT = os.path.join(RESULTS, "agent_result.json")

BUDGET_SEC = float(os.environ.get("BUDGET_SEC", "1800"))   # per-rule wall budget
PATIENCE = int(os.environ.get("PATIENCE", "12"))            # rounds w/o improvement -> stop
TRAIN_SEED = int(os.environ.get("TRAIN_SEED", "31"))
HOLD_SEED = int(os.environ.get("HOLD_SEED", "97"))
N_TRAIN = int(os.environ.get("N_TRAIN", "120"))
N_HOLD = int(os.environ.get("N_HOLD", "300"))
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "300"))
EVAL_TIMEOUT = int(os.environ.get("EVAL_TIMEOUT", "120"))

import harness_mp as M
CONSTRAINTS = list(M.SPINES.keys())

SEED_STRATEGY = '''import harness as H
def build(base, book):
    # champion seed: sandwich the single most price-impactful user with a tiny front-run
    users=[o for o in base if o.actor=="user"]
    if not users: return H.baseline_priority_fee(base)
    def frac(o):
        p=book[o.pool]; return o.amount_in/(p.y if o.side=="buy" else p.x)
    tgt=max(users, key=frac)
    front=H.Order("bld0", tgt.side, 0.03*tgt.amount_in, -1, "BL0", 0.0, tgt.pool)
    return [front]+H.baseline_priority_fee(base)
'''

API_DOC = '''Improve a block-builder sandwich strategy. EDIT the champion below into a higher-scoring one.
Reply IMMEDIATELY with ONLY one ```python code block defining `def build(base, book)` — no analysis.

API: pools book={pool_id:Pool}; Pool has .x(token) .y(numeraire) .spot()==y/x. base=list[Order] of
user/bg swaps. Order(actor,side,amount_in,ts,oid,priority,pool); side "buy"=spend numeraire get token,
"sell"=spend token get numeraire. Add your own leg with a NEW oid, actor "bld*", priority=0.0, pool set;
put front BEFORE victim and back AFTER in the returned list (execution follows list order, pools
independent). Return the full ordering (base + your legs). `import harness as H`; H.baseline_priority_fee(base)
gives the honest ordering.

Score = priority fees + your net numeraire extraction, ONLY IF the fairness rule accepts the block
(else you get the low honest value). So sandwich hard but stay UNDER the rule. amount_in must be finite
and < 1e12. Levers: size the front so the victim's loss sits just under the rule threshold; hit several
victims across pools; add back-legs to realize; skip victims that would trip the rule.'''


# ---------- infra ----------
def tg(msg):
    # progress lives in results/agent_ledger.jsonl + the heartbeat file. Wire your own notifier here
    # (Telegram/Slack/etc.) if you want push alerts on per-rule completion / crashes.
    pass

def beat(m):
    try: open(HEARTBEAT, "w").write(f"{int(time.time())} {m}\n")
    except Exception: pass

def log(r):
    with open(LEDGER, "a") as f: f.write(json.dumps(r) + "\n")


def call_codex(prompt):
    # generator agent = a headless code-writing model CLI. `claude -p` by default; swap for any model.
    try:
        p = subprocess.run(["claude", "-p", prompt],
                           cwd=HERE, capture_output=True, text=True, timeout=CODEX_TIMEOUT)
        return p.stdout or ""
    except Exception as e:
        return f"__AGENT_ERR__ {e}"

def extract_code(text):
    blocks = re.findall(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    for b in reversed(blocks):
        if "def build" in b:
            return b.strip()
    return None

def eval_strategy(path, cname, seed, n):
    try:
        p = subprocess.run(["python3", os.path.join(HERE, "eval_strategy.py"), path, cname, str(seed), str(n)],
                           cwd=HERE, capture_output=True, text=True, timeout=EVAL_TIMEOUT)
        line = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else ""
        return json.loads(line)
    except Exception as e:
        return {"mean_val": -1e18, "errs": 999, "last_err": f"eval-failed: {e} / {p.stderr[-200:] if 'p' in dir() else ''}", "pass_rate": 0}


def optimize(cname):
    t0 = time.time()
    champ_path = os.path.join(STRAT, f"{_slug(cname)}_champ.py")
    open(champ_path, "w").write(SEED_STRATEGY)
    best = eval_strategy(champ_path, cname, TRAIN_SEED, N_TRAIN)
    best_val = best["mean_val"]
    log({"constraint": cname, "event": "seed", "train_val": best_val, "pass_rate": best.get("pass_rate")})
    since = rnd = 0
    feedback = f"Champion scores {best_val:.1f} mean value. Beat it."
    while time.time() - t0 < BUDGET_SEC and since < PATIENCE:
        rnd += 1
        prompt = (f"{API_DOC}\n\nThe fairness rule in force this run: '{cname}'.\n"
                  f"Round {rnd}. Current CHAMPION build (mean value {best_val:.1f}):\n"
                  f"```python\n{open(champ_path).read()}\n```\n"
                  f"Feedback: {feedback}\nWrite an IMPROVED build that scores higher. Output ONLY the code block.")
        out = call_codex(prompt)
        code = extract_code(out)
        if not code:
            feedback = "Your last reply had no usable ```python def build``` block. Output ONLY the code block."
            since += 1; beat(f"{cname} r{rnd} no-code since={since}")
            log({"constraint": cname, "event": "nocode", "round": rnd})
            continue
        cand = os.path.join(STRAT, f"{_slug(cname)}_r{rnd}.py")
        open(cand, "w").write(code)
        res = eval_strategy(cand, cname, TRAIN_SEED, N_TRAIN)
        v = res["mean_val"]
        improved = v > best_val + 1e-6 and res.get("errs", 0) <= N_TRAIN * 0.1
        log({"constraint": cname, "event": "cand", "round": rnd, "val": round(v, 2),
             "errs": res.get("errs"), "pass_rate": res.get("pass_rate"), "improved": improved})
        if improved:
            best_val = v; open(champ_path, "w").write(code); since = 0
            feedback = f"You beat it — new champion scores {v:.1f}. Push further (more victims / tighter sizing)."
        else:
            since += 1
            feedback = (f"Your candidate scored {v:.1f} vs champion {best_val:.1f}"
                        + (f"; it ERRORED on {res.get('errs')} blocks (last: {res.get('last_err')})" if res.get("errs") else "; no improvement")
                        + ". Try a different idea.")
        beat(f"{cname} r{rnd} val={v:.1f} best={best_val:.1f} since={since}")
    hold = eval_strategy(champ_path, cname, HOLD_SEED, N_HOLD)
    return {"train_val": round(best_val, 2), "hold_val": round(hold["mean_val"], 2),
            "hold_pass_rate": hold.get("pass_rate"), "rounds": rnd, "converged": since >= PATIENCE,
            "seconds": round(time.time() - t0, 1), "champ_file": champ_path}

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main():
    beat("init")
    learned = {}
    for cname in CONSTRAINTS:
        try:
            beat(f"optimizing {cname}")
            res = optimize(cname)
            learned[cname] = res
            log({"constraint": cname, "event": "learned", **{k: v for k, v in res.items() if k != "champ_file"}})
            tg(f"{cname} DONE: train={res['train_val']} hold={res['hold_val']} rounds={res['rounds']} conv={res['converged']}")
        except Exception as e:
            log({"constraint": cname, "event": "error", "err": str(e), "tb": traceback.format_exc()[-800:]})
            tg(f"OFF-RAILS: {cname} crashed: {e} — isolated, continuing")
    json.dump({"meta": {"env": "multi-pair", "budget_sec": BUDGET_SEC, "constraints": CONSTRAINTS,
                        "n_train": N_TRAIN, "n_hold": N_HOLD}, "learned": learned},
              open(OUT, "w"), indent=2)
    beat("done")
    tg("AGENT DONE\n" + "\n".join(f"{c[:22]}: train={learned[c]['train_val']} hold={learned[c]['hold_val']} rounds={learned[c]['rounds']}"
                                     for c in CONSTRAINTS if c in learned)[:3500])
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
