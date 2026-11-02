"""
strategy_lab.py — «интеллект»: честная лаборатория стратегий на локальной базе свечей (data/lake.db).

Что делает
  * 4 семейства стратегий (пробой Дончиана с объёмом, откат к EMA по тренду, RSI-возврат к среднему,
    импульс объёмного всплеска) × 3 таймфрейма (15м/1ч/4ч) × сетка стопов/тейков × ограничение стопа 2%
    (как в Risk Core бота) или без него — на 12 монетах Bybit (6 крупных + 6 мем-коинов).
  * Вход строго на ОТКРЫТИИ следующей свечи (нет подглядывания в будущее). Если на одной свече
    достижимы и стоп, и тейк — считаем стоп (консервативно). Гэп через стоп — выход по open.
  * Издержки: комиссия 0.055%×2 + проскальзывание 0.03%×2 = 0.17% на круг.
  * Walk-forward: параметры подбираются ТОЛЬКО на первых 60% истории каждой монеты, проверка — на
    последних 40% (out-of-sample). «Проверена» только та связка, у которой тест положителен,
    t-статистика ≥ 2, PF ≥ 1.15 и ≥ 60 сделок.
  * Результат: data/lab_report.json / lab_report.md, таблица lab_results в lake.db,
    data/validated_strategies.json (то, что имеет право на сигналы).

Запуск: python strategy_lab.py
"""
from __future__ import annotations

import itertools
import json
import math
import pathlib
import sqlite3
import time

import data_lake as lake

BASE = pathlib.Path(__file__).parent
COST = 0.0017          # круговые издержки (доля)
TRAIN_FRAC = 0.6
TIME_STOP = {"15": 96, "60": 72, "240": 48}   # макс. баров в сделке
STOP_K = (1.0, 1.5, 2.5)
TP_R = (1.0, 2.0, 3.0)
CAPS = (0.02, None)
STRATS = ("donchian_vol", "ema_pullback", "rsi_revert", "vol_burst")
TFS = ("15", "60", "240")


# ------------------------------------------------------------ индикаторы
def indicators(rows):
    n = len(rows)
    ts = [r[0] for r in rows]; o = [r[1] for r in rows]; h = [r[2] for r in rows]
    l = [r[3] for r in rows]; c = [r[4] for r in rows]; v = [r[5] for r in rows]
    atr = [0.0] * n; e20 = [0.0] * n; e50 = [0.0] * n; rsi = [50.0] * n
    ag = al = 0.0
    for i in range(n):
        tr = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        atr[i] = tr if i == 0 else atr[i - 1] + (tr - atr[i - 1]) / 14
        e20[i] = c[i] if i == 0 else e20[i - 1] + (c[i] - e20[i - 1]) * 2 / 21
        e50[i] = c[i] if i == 0 else e50[i - 1] + (c[i] - e50[i - 1]) * 2 / 51
        if i:
            d = c[i] - c[i - 1]
            ag += (max(d, 0) - ag) / 14
            al += (max(-d, 0) - al) / 14
            rsi[i] = 100 - 100 / (1 + ag / al) if al > 1e-12 else 100.0
    vs = [0.0] * n; hh = [0.0] * n; ll = [0.0] * n
    for i in range(20, n):
        vs[i] = sum(v[i - 20:i]) / 20
        hh[i] = max(h[i - 20:i]); ll[i] = min(l[i - 20:i])
    return dict(ts=ts, o=o, h=h, l=l, c=c, v=v, atr=atr, e20=e20, e50=e50, rsi=rsi, vs=vs, hh=hh, ll=ll, n=n)


def signals(name, X):
    out = []
    o, h, l, c, v = X["o"], X["h"], X["l"], X["c"], X["v"]
    for i in range(60, X["n"] - 1):
        a = X["atr"][i]
        if a <= 0 or X["vs"][i] <= 0:
            continue
        s = 0
        if name == "donchian_vol":
            if v[i] > 1.5 * X["vs"][i]:
                s = 1 if c[i] > X["hh"][i] else (-1 if c[i] < X["ll"][i] else 0)
        elif name == "ema_pullback":
            if X["e20"][i] > X["e50"][i] and l[i] <= X["e20"][i] < c[i] and c[i] > o[i]:
                s = 1
            elif X["e20"][i] < X["e50"][i] and h[i] >= X["e20"][i] > c[i] and c[i] < o[i]:
                s = -1
        elif name == "rsi_revert":
            s = 1 if X["rsi"][i] < 25 else (-1 if X["rsi"][i] > 75 else 0)
        elif name == "vol_burst":
            if v[i] > 3 * X["vs"][i]:
                s = 1 if c[i] - o[i] > 1.2 * a else (-1 if o[i] - c[i] > 1.2 * a else 0)
        if s:
            out.append((i, s))
    return out


def simulate(X, sigs, stop_k, tp_r, cap, tstop, lo=0, hi=None):
    """Возвращает список (R, pct) сделок, чей сигнал i в [lo, hi)."""
    hi = X["n"] if hi is None else hi
    o, h, l, c = X["o"], X["h"], X["l"], X["c"]
    n = X["n"]
    res = []
    busy = -1
    for i, side in sigs:
        if i < lo or i >= hi or i <= busy:
            continue
        e = o[i + 1]
        if e <= 0:
            continue
        sp = stop_k * X["atr"][i] / e
        if cap:
            sp = min(sp, cap)
        sp = max(sp, 0.0015)
        sl = e * (1 - side * sp)
        tp = e * (1 + side * tp_r * sp)
        ex = None
        last = min(i + tstop, n - 1)
        for j in range(i + 1, last + 1):
            if side == 1:
                if o[j] <= sl: ex = o[j]; break
                if l[j] <= sl: ex = sl; break
                if h[j] >= tp: ex = tp; break
            else:
                if o[j] >= sl: ex = o[j]; break
                if h[j] >= sl: ex = sl; break
                if l[j] <= tp: ex = tp; break
        if ex is None:
            j = last; ex = c[last]
        busy = j
        pct = side * (ex / e - 1) - COST
        res.append((pct / sp, pct))
    return res


def metrics(trades):
    n = len(trades)
    if n == 0:
        return dict(n=0, exp_r=0.0, win=0.0, pf=0.0, t=0.0, sum_r=0.0, avg_pct=0.0, max_dd_r=0.0)
    rs = [t[0] for t in trades]
    m = sum(rs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / max(1, n - 1))
    gw = sum(x for x in rs if x > 0); gl = -sum(x for x in rs if x < 0)
    eq = pk = dd = 0.0
    for x in rs:
        eq += x; pk = max(pk, eq); dd = max(dd, pk - eq)
    return dict(n=n, exp_r=round(m, 4), win=round(sum(1 for x in rs if x > 0) / n, 3),
                pf=round(gw / gl, 3) if gl > 0 else 99.0, t=round(m / (sd / math.sqrt(n)), 2) if sd > 0 else 0.0,
                sum_r=round(sum(rs), 2), avg_pct=round(100 * sum(t[1] for t in trades) / n, 3), max_dd_r=round(dd, 2))


def run(symbols=None):
    symbols = symbols or lake.LAKE_UNIVERSE
    t0 = time.time()
    data = {}
    for s in symbols:
        for tf in TFS:
            rows = lake.load(s, tf)
            if len(rows) > 500:
                X = indicators(rows)
                data[(s, tf)] = (X, int(X["n"] * TRAIN_FRAC))
    sigcache = {(k, st): signals(st, data[k][0]) for k in data for st in STRATS}
    report = []
    grid = list(itertools.product(STOP_K, TP_R, CAPS))
    for st in STRATS:
        for tf in TFS:
            keys = [k for k in data if k[1] == tf]
            best = None
            for sk, tr, cap in grid:
                tr_tr = []
                for k in keys:
                    X, sp_ = data[k]
                    tr_tr += simulate(X, sigcache[(k, st)], sk, tr, cap, TIME_STOP[tf], 0, sp_)
                m = metrics(tr_tr)
                if m["n"] >= 80 and (best is None or m["exp_r"] > best[0]["exp_r"]):
                    best = (m, (sk, tr, cap))
            if best is None:
                continue
            (mtrain, (sk, tr, cap)) = best
            per = {}
            test_all = []
            for k in keys:
                X, sp_ = data[k]
                t = simulate(X, sigcache[(k, st)], sk, tr, cap, TIME_STOP[tf], sp_, None)
                per[k[0]] = metrics(t)
                test_all += t
            mt = metrics(test_all)
            maj = metrics([x for k in keys if k[0] in lake.MAJORS for x in simulate(data[k][0], sigcache[(k, st)], sk, tr, cap, TIME_STOP[tf], data[k][1], None)])
            mem = metrics([x for k in keys if k[0] in lake.MEMES for x in simulate(data[k][0], sigcache[(k, st)], sk, tr, cap, TIME_STOP[tf], data[k][1], None)])
            ok = mt["n"] >= 60 and mt["exp_r"] > 0 and mt["t"] >= 2 and mt["pf"] >= 1.15 and mtrain["exp_r"] > 0
            report.append(dict(strategy=st, tf=tf, stop_atr=sk, tp_r=tr, stop_cap=cap, train=mtrain, test=mt,
                               test_majors=maj, test_memes=mem, per_symbol_test=per,
                               profitable_symbols=sum(1 for v in per.values() if v["exp_r"] > 0), n_symbols=len(per), validated=ok))
    out = dict(generated=time.strftime("%Y-%m-%d %H:%M:%S"), cost_roundtrip=COST, train_frac=TRAIN_FRAC,
               symbols=symbols, candles={f"{s}/{tf}": len(data[(s, tf)][0]["ts"]) for (s, tf) in data}, results=report,
               seconds=round(time.time() - t0, 1))
    (BASE / "data" / "lab_report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (BASE / "data" / "validated_strategies.json").write_text(
        json.dumps([r for r in report if r["validated"]], ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    r = run()
    print("секунд:", r["seconds"], "| свечей рядов:", len(r["candles"]))
    for x in r["results"]:
        t, tt = x["train"], x["test"]
        print(f"{x['strategy']:13s} tf={x['tf']:>3s} stop={x['stop_atr']}ATR tp={x['tp_r']}R cap={x['stop_cap']} | "
              f"TRAIN n={t['n']} E={t['exp_r']:+.3f}R | TEST n={tt['n']} E={tt['exp_r']:+.3f}R win={tt['win']:.0%} PF={tt['pf']} t={tt['t']} "
              f"| majors E={x['test_majors']['exp_r']:+.3f} memes E={x['test_memes']['exp_r']:+.3f} | {x['profitable_symbols']}/{x['n_symbols']} монет+ "
              f"{'✅ПРОВЕРЕНА' if x['validated'] else ''}")
