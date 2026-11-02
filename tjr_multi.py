"""
tjr_multi.py — модель TJR (Asia-sweep → BOS → retest OB) на его рынках: S&P500, Nasdaq-100, EURUSD, GBPUSD, USDJPY, золото.
Данные: Dukascopy 1м → 5м (dukas.py). Те же правила и тот же протокол, что и в tjr_lab: параметры по первым 60% времени, оценка на последних 40%,
зеркальный контроль. Издержки на сторону: индексы 0.003%, форекс 0.004%, золото 0.008% (спред + проскальзывание; у брокеров может быть больше).
"""
import json
import pathlib

import dukas
import tjr_lab as T

COST = {"SPX": 0.00003, "NDX": 0.00003, "EURUSD": 0.00004, "GBPUSD": 0.00004, "USDJPY": 0.00004, "XAUUSD": 0.00008}
GRID = [(ms, t1, t2) for ms in (0.3, 0.6) for (t1, t2) in ((1.5, 4.0), (1.0, 2.0), (2.0, 3.0))]


def main():
    data = {}
    for name in COST:
        rows = dukas.bars(name, 5)
        if len(rows) > 5000:
            data[name] = T.prep(rows)
    cut = {n: X["ts"][int(X["n"] * 0.6)] for n, X in data.items()}
    rep = []
    for ms, t1, t2 in GRID:
        tr = []; te = []; mi = []; by = {}
        for n, X in data.items():
            T.SIDE = COST[n]
            for (i, d, side, r, rm, _info) in T.detect(X, ms, t1, t2):
                if X["ts"][i] < cut[n]:
                    tr.append(r)
                else:
                    te.append(r); mi.append(rm); by.setdefault(n, []).append((side, r))
        rep.append(dict(minsweep=ms, tp1=t1, tp2=t2, train=T.metrics(tr), test=T.metrics(te), mirror=T.metrics(mi),
                        by_inst={k: T.metrics([x[1] for x in v]) for k, v in by.items()},
                        longs=T.metrics([x[1] for v in by.values() for x in v if x[0] == 1]),
                        shorts=T.metrics([x[1] for v in by.values() for x in v if x[0] == -1])))
    best = max(rep, key=lambda r: r["train"]["exp"])
    out = dict(bars={n: X["n"] for n, X in data.items()},
               days={n: round((X["ts"][-1] - X["ts"][0]) / 86_400_000) for n, X in data.items()}, grid=rep, best=best)
    pathlib.Path("data/tjr_multi_report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("бары:", out["bars"], "дней:", out["days"])
    for r in rep:
        print(f"sweep>{r['minsweep']} tp {r['tp1']}/{r['tp2']} | TRAIN n={r['train']['n']} E={r['train']['exp']:+.3f} | TEST n={r['test']['n']} E={r['test']['exp']:+.3f}R "
              f"win={r['test']['win']:.0%} PF={r['test']['pf']} t={r['test']['t']} | зеркало E={r['mirror']['exp']:+.3f} | long {r['longs']['exp']:+.2f}(n={r['longs']['n']}) short {r['shorts']['exp']:+.2f}(n={r['shorts']['n']})")
    print("ЛУЧШАЯ по train:", best["minsweep"], best["tp1"], best["tp2"], "→ TEST", best["test"])
    print("по инструментам:", {k: (v["n"], v["exp"], v["pf"]) for k, v in best["by_inst"].items()})


if __name__ == "__main__":
    main()
