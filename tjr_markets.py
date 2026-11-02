"""
tjr_markets.py — TJR (Asia-sweep→BOS→OB retest) на индексах, форексе и золоте. Параметры НЕ подбираются: берутся опубликованные по умолчанию
(sweep>0.3 ATR, TP1 1.5R / TP2 4R) как основной вариант; остальные 5 вариантов сетки — для устойчивости.
Данные: SPX/NDX — Dukascopy 5м (301/149 дней, из lake.db); EURUSD, GBPUSD, USDJPY, золото (GC=F), ES/NQ-фьючерсы — Yahoo 5м за 60 дней.
Издержки на сторону: индексы/ES/NQ 0.003%, форекс 0.004%, золото 0.008%.
"""
import json
import urllib.request

import dukas
import tjr_lab as T

COST = {"SPX": 0.00003, "NDX": 0.00003, "ES=F": 0.00003, "NQ=F": 0.00003, "EURUSD": 0.00004, "GBPUSD": 0.00004, "USDJPY": 0.00004, "GOLD": 0.00008}
YAHOO = {"ES=F": "ES=F", "NQ=F": "NQ=F", "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X", "GOLD": "GC=F"}
GRID = [(0.3, 1.5, 4.0), (0.3, 1.0, 2.0), (0.3, 2.0, 3.0), (0.6, 1.5, 4.0), (0.6, 1.0, 2.0), (0.6, 2.0, 3.0)]


def yahoo_rows(sym):
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m&range=60d"
    d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
    r = d["chart"]["result"][0]; q = r["indicators"]["quote"][0]
    rows = []
    for t, o, h, l, c, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]):
        if None in (o, h, l, c):
            continue
        rows.append((t * 1000, o, h, l, c, v or 0))
    return rows


def main():
    data = {}
    for n in ("SPX", "NDX"):
        rows = dukas.bars(n, 5)
        if len(rows) > 5000:
            data[n] = T.prep(rows)
    for n, s in YAHOO.items():
        try:
            data[n] = T.prep(yahoo_rows(s))
        except Exception as e:  # noqa: BLE001
            print("пропуск", n, e)
    print("бары:", {n: X["n"] for n, X in data.items()})
    res = []
    for ms, t1, t2 in GRID:
        allr = []; allm = []; by = {}; sides = {1: [], -1: []}
        for n, X in data.items():
            T.SIDE = COST[n]
            for (i, d, side, r, rm, _info) in T.detect(X, ms, t1, t2):
                allr.append(r); allm.append(rm); by.setdefault(n, []).append(r); sides[side].append(r)
        res.append(dict(minsweep=ms, tp1=t1, tp2=t2, all=T.metrics(allr), mirror=T.metrics(allm),
                        by_inst={k: T.metrics(v) for k, v in by.items()}, longs=T.metrics(sides[1]), shorts=T.metrics(sides[-1])))
    json.dump(res, open("data/tjr_markets_report.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for r in res:
        a, m = r["all"], r["mirror"]
        print(f"sweep>{r['minsweep']} tp {r['tp1']}/{r['tp2']}: n={a['n']} E={a['exp']:+.3f}R win={a['win']:.0%} PF={a['pf']} t={a['t']} sumR={a['sumR']} | "
              f"зеркало E={m['exp']:+.3f} PF={m['pf']} | long {r['longs']['exp']:+.2f}(n={r['longs']['n']}) short {r['shorts']['exp']:+.2f}(n={r['shorts']['n']})")
    print("основной вариант по инструментам:", {k: (v["n"], v["exp"], v["pf"]) for k, v in res[0]["by_inst"].items()})


if __name__ == "__main__":
    main()
