"""
strategy_lab2.py — поиск работающей стратегии на портфельном уровне (позиции {-1,0,1}, издержки на каждом изменении позиции).

Протокол (чтобы не обмануть самих себя):
  * решение принимается по закрытым барам до t-1, позиция держится с open[t] до open[t+1] (без подглядывания);
  * издержки 0.085% за каждую сторону (комиссия 0.055% + проскальзывание 0.03%) на каждое изменение позиции;
  * параметры выбираются ТОЛЬКО на train (первые 60% календарного времени), оценка — на test (последние 40%);
  * портфель = равные доли по всем монетам, дневная доходность, Sharpe годовой;
  * «работает» = test Sharpe >= 0.8, t-стат. дневных доходностей >= 2.5 (строже из-за множественных проверок),
    положителен в обеих половинах test и на >= 60% монет.
Запуск: python strategy_lab2.py
"""
from __future__ import annotations

import json
import math
import pathlib
import statistics as st
import time

import data_lake as lake

BASE = pathlib.Path(__file__).parent
SIDE_COST = 0.00085
TRAIN_FRAC = 0.6
DAY = 86_400_000


# ---------------------------------------------------------------- данные
def load(sym, tf):
    r = lake.load(sym, tf)
    return dict(ts=[x[0] for x in r], o=[x[1] for x in r], h=[x[2] for x in r], l=[x[3] for x in r],
                c=[x[4] for x in r], v=[x[5] for x in r])


def sma(a, n):
    out = [None] * len(a); s = 0.0
    for i, x in enumerate(a):
        s += x
        if i >= n: s -= a[i - n]
        if i >= n - 1: out[i] = s / n
    return out


def ema(a, n):
    out = [a[0]]; k = 2 / (n + 1)
    for x in a[1:]: out.append(out[-1] + (x - out[-1]) * k)
    return out


def rolling_std(a, n):
    out = [None] * len(a)
    for i in range(n, len(a)):
        w = a[i - n:i]; m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


def atr(D, n=14):
    out = [0.0] * len(D["c"])
    for i in range(1, len(out)):
        tr = max(D["h"][i] - D["l"][i], abs(D["h"][i] - D["c"][i - 1]), abs(D["l"][i] - D["c"][i - 1]))
        out[i] = tr if i == 1 else out[i - 1] + (tr - out[i - 1]) / n
    return out


# ---------------------------------------------------------------- генераторы позиций (pos[i] = позиция на баре i, решение по данным до i-1)
def pos_donchian(D, N, both=True):
    c, h, l, n = D["c"], D["h"], D["l"], len(D["c"])
    M = max(5, N // 2)
    pos = [0] * n; cur = 0
    for i in range(N + 1, n):
        j = i - 1  # последний закрытый бар
        hh = max(h[j - N:j]); ll = min(l[j - N:j])
        xh = max(h[j - M:j]); xl = min(l[j - M:j])
        if cur == 0:
            if c[j] > hh: cur = 1
            elif both and c[j] < ll: cur = -1
        elif cur == 1 and c[j] < xl: cur = 0
        elif cur == -1 and c[j] > xh: cur = 0
        pos[i] = cur
    return pos


def pos_ema(D, f, s, both=True):
    c = D["c"]; ef, es = ema(c, f), ema(c, s); n = len(c)
    pos = [0] * n
    for i in range(s + 1, n):
        j = i - 1
        pos[i] = 1 if ef[j] > es[j] else (-1 if both else 0)
    return pos


def pos_bb_revert(D, n_, k):
    c = D["c"]; m = sma(c, n_); sd = rolling_std(c, n_); n = len(c)
    pos = [0] * n; cur = 0
    for i in range(n_ + 2, n):
        j = i - 1
        if m[j] is None or sd[j] is None or sd[j] == 0: continue
        z = (c[j] - m[j]) / sd[j]
        if cur == 0:
            if z < -k: cur = 1
            elif z > k: cur = -1
        elif cur == 1 and z >= 0: cur = 0
        elif cur == -1 and z <= 0: cur = 0
        pos[i] = cur
    return pos


def pos_fade_burst(D, L, z, H):
    c = D["c"]; n = len(c)
    ret = [0.0] + [math.log(c[i] / c[i - 1]) for i in range(1, n)]
    sd = rolling_std(ret, 100)
    pos = [0] * n; hold = 0; cur = 0
    for i in range(110, n):
        j = i - 1
        if hold > 0:
            hold -= 1
            if hold == 0: cur = 0
        if cur == 0 and sd[j]:
            r = sum(ret[j - L + 1:j + 1]); zz = r / (sd[j] * math.sqrt(L))
            if zz < -z: cur, hold = 1, H
            elif zz > z: cur, hold = -1, H
        pos[i] = cur
    return pos


def vol_target(D, pos, tf_bars_per_day, target_daily=0.01, cap=2.0):
    c = D["c"]; n = len(c)
    ret = [0.0] + [c[i] / c[i - 1] - 1 for i in range(1, n)]
    sd = rolling_std(ret, 100); out = []
    for i in range(n):
        if pos[i] == 0 or not sd[i]: out.append(0.0); continue
        dvol = sd[i] * math.sqrt(tf_bars_per_day)
        out.append(pos[i] * min(cap, target_daily / dvol))
    return out


# ---------------------------------------------------------------- портфельная оценка
def series_returns(D, pos):
    """[(day, ret)] с учётом издержек; ret для бара i = pos[i]*(o[i+1]/o[i]-1) - cost*|pos[i]-pos[i-1]|."""
    o, ts = D["o"], D["ts"]; out = []
    prev = 0.0
    for i in range(len(o) - 1):
        p = pos[i]
        r = p * (o[i + 1] / o[i] - 1) - SIDE_COST * abs(p - prev)
        prev = p
        out.append((ts[i] // DAY, r))
    return out


def daily(series_list, lo_day=None, hi_day=None):
    acc = {}
    for ser in series_list:
        d = {}
        for day, r in ser:
            if lo_day is not None and day < lo_day: continue
            if hi_day is not None and day >= hi_day: continue
            d[day] = d.get(day, 0.0) + r
        for day, r in d.items(): acc.setdefault(day, []).append(r)
    days = sorted(acc)
    return days, [sum(acc[d]) / len(series_list) for d in days]   # равные доли на все монеты (неактивные = 0)


def perf(rets):
    n = len(rets)
    if n < 30: return dict(n=n, sharpe=0.0, t=0.0, ann=0.0, mdd=0.0)
    m = sum(rets) / n; sd = st.pstdev(rets)
    eq = pk = mdd = 0.0
    for r in rets:
        eq += r; pk = max(pk, eq); mdd = max(mdd, pk - eq)
    sh = m / sd * math.sqrt(365) if sd > 0 else 0.0
    return dict(n=n, sharpe=round(sh, 2), t=round(m / (sd / math.sqrt(n)), 2) if sd > 0 else 0.0,
                ann=round(m * 365 * 100, 1), mdd=round(mdd * 100, 1))


def evaluate(series_per_sym, cut_day):
    ser = list(series_per_sym.values())
    d_tr, r_tr = daily(ser, None, cut_day)
    d_te, r_te = daily(ser, cut_day, None)
    half = len(r_te) // 2
    per_sym = {}
    for s, x in series_per_sym.items():
        _, rr = daily([x], cut_day, None)
        per_sym[s] = sum(rr)
    return dict(train=perf(r_tr), test=perf(r_te), test_h1=perf(r_te[:half]), test_h2=perf(r_te[half:]),
                pos_syms=sum(1 for v in per_sym.values() if v > 0), n_syms=len(per_sym),
                per_sym={k: round(v * 100, 1) for k, v in per_sym.items()})


# ---------------------------------------------------------------- перебор
def run():
    t0 = time.time()
    syms = lake.LAKE_UNIVERSE
    results = []
    # общий cut: 60% календарного диапазона 1h-данных
    cache = {}
    def D_(s, tf):
        if (s, tf) not in cache: cache[(s, tf)] = load(s, tf)
        return cache[(s, tf)]
    all_days = [D_(s, "60")["ts"][0] // DAY for s in syms] + [D_(s, "60")["ts"][-1] // DAY for s in syms]
    lo, hi = min(all_days), max(all_days)
    # чтобы все монеты имели train: берём cut по 60% от самого позднего старта (TRUMP/FARTCOIN стартуют позже)
    start_common = sorted(D_(s, "60")["ts"][0] // DAY for s in syms)[len(syms) // 2]
    cut = int(start_common + (hi - start_common) * TRAIN_FRAC)

    cfgs = []
    for tf, per_day in (("60", 24), ("240", 6)):
        for N in (20, 55, 100):
            cfgs.append(("donchian", tf, dict(N=N, both=True), per_day))
            cfgs.append(("donchian_long", tf, dict(N=N, both=False), per_day))
        for f, s_ in ((12, 48), (20, 100), (50, 200)):
            cfgs.append(("ema_cross", tf, dict(f=f, s=s_, both=True), per_day))
            cfgs.append(("ema_cross_long", tf, dict(f=f, s=s_, both=False), per_day))
        for n_, k in ((20, 2.0), (20, 3.0), (50, 2.5)):
            cfgs.append(("bb_revert", tf, dict(n_=n_, k=k), per_day))
        for L, z, H in ((6, 2.5, 6), (24, 2.5, 12), (6, 3.5, 12), (24, 3.5, 24)):
            cfgs.append(("fade_burst", tf, dict(L=L, z=z, H=H), per_day))

    for name, tf, p, per_day in cfgs:
        for vt in (False, True):
            series = {}
            for s in syms:
                D = D_(s, tf)
                if len(D["c"]) < 500: continue
                if name.startswith("donchian"): pos = pos_donchian(D, p["N"], p["both"])
                elif name.startswith("ema_cross"): pos = pos_ema(D, p["f"], p["s"], p["both"])
                elif name == "bb_revert": pos = pos_bb_revert(D, p["n_"], p["k"])
                else: pos = pos_fade_burst(D, p["L"], p["z"], p["H"])
                if vt: pos = vol_target(D, pos, per_day)
                series[s] = series_returns(D, pos)
            ev = evaluate(series, cut)
            results.append(dict(strategy=name, tf=tf, params=p, vol_target=vt, **ev))
    # выбор по train внутри семейства (strategy, tf, vt) -> смотрим test
    fam = {}
    for r in results:
        key = (r["strategy"], r["tf"], r["vol_target"])
        if key not in fam or r["train"]["sharpe"] > fam[key]["train"]["sharpe"]:
            fam[key] = r
    chosen = sorted(fam.values(), key=lambda r: -r["test"]["sharpe"])
    for r in chosen:
        t = r["test"]
        r["works"] = (t["sharpe"] >= 0.8 and t["t"] >= 2.5 and r["test_h1"]["sharpe"] > 0 and r["test_h2"]["sharpe"] > 0
                      and r["pos_syms"] / max(1, r["n_syms"]) >= 0.6 and r["train"]["sharpe"] > 0)
    out = dict(generated=time.strftime("%Y-%m-%d %H:%M:%S"), cut_day=cut, all_configs=len(results),
               families=len(chosen), best_by_train=chosen, seconds=round(time.time() - t0, 1))
    (BASE / "data" / "lab2_report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    o = run()
    print("конфигов:", o["all_configs"], "семейств:", o["families"], "сек:", o["seconds"])
    for r in o["best_by_train"]:
        tr, te = r["train"], r["test"]
        print(f"{r['strategy']:15s} {r['tf']:>3s} vt={int(r['vol_target'])} {json.dumps(r['params'])[:34]:34s} | TRAIN Sh={tr['sharpe']:+.2f} | "
              f"TEST Sh={te['sharpe']:+.2f} t={te['t']:+.1f} ann={te['ann']:+.0f}% mdd={te['mdd']}% h1={r['test_h1']['sharpe']:+.1f} h2={r['test_h2']['sharpe']:+.1f} "
              f"монет+ {r['pos_syms']}/{r['n_syms']} {'✅' if r['works'] else ''}")
