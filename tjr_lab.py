"""
tjr_lab.py — формализация модели TJR (Asia-sweep → BOS → retest OB/FVG) и честный бэктест на крипто 5м.

Правила (из опубликованных описаний: PickMyTrade/TradingView/Backtestify; сам TJR единого кода не публиковал):
  1) Азия-диапазон: high/low 20:00–24:00 по Нью-Йорку.
  2) Сессии входа: Лондон 03:00–08:00 и NY AM 09:30–12:00 (ET).
  3) Sweep: свеча заходит за Asia high/low минимум на MINSWEEP×ATR и закрывается обратно внутри диапазона.
  4) BOS: в течение 40 баров сильная свеча (тело > 0.5 ATR) закрывается за экстремум последних 8 баров в обратную сторону.
  5) Зона входа: последняя противоположная свеча перед BOS (order block).
  6) Вход: возврат цены в зону и закрытие свечи в направлении сделки → вход по open следующего бара.
  7) Стоп за вершиной sweep + 0.5 ATR. TP1 (50%) и TP2 (остаток), после TP1 стоп в безубыток. Всё закрывается в 12:00 ET.
  8) Максимум 2 сделки и 2 убытка в день на монету.
Контроль: «зеркальная» сделка (та же точка, обратное направление, те же дистанции) — если у правил есть информация о
направлении, зеркало обязано быть хуже. Издержки: 0.085%/сторона. Выбор параметров по первым 60% времени, оценка на последних 40%.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import statistics as st

import data_lake as lake

BASE = pathlib.Path(__file__).parent
SIDE = 0.00085
H = 3_600_000


def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


_dst_cache = {}


def et_offset_ms(ts):
    y = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).year
    if y not in _dst_cache:
        s = dt.datetime.combine(_nth_sunday(y, 3, 2), dt.time(7), dt.timezone.utc)   # 02:00 EST
        e = dt.datetime.combine(_nth_sunday(y, 11, 1), dt.time(6), dt.timezone.utc)  # 02:00 EDT
        _dst_cache[y] = (s.timestamp() * 1000, e.timestamp() * 1000)
    s, e = _dst_cache[y]
    return (-4 if s <= ts < e else -5) * H


def sess_minute(ts):
    """минуты от 20:00 ET предыдущего дня (0..1439) и ключ торгового дня."""
    et = ts + et_offset_ms(ts)
    m = int(((et // 60000) - 20 * 60) % 1440)
    tday = int((et + 4 * H) // 86_400_000)
    return m, tday


def prep(rows):
    ts = [r[0] for r in rows]; o = [r[1] for r in rows]; h = [r[2] for r in rows]; l = [r[3] for r in rows]; c = [r[4] for r in rows]
    n = len(rows); atr = [0.0] * n
    for i in range(1, n):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        atr[i] = tr if i == 1 else atr[i - 1] + (tr - atr[i - 1]) / 14
    mm = [sess_minute(t) for t in ts]
    return dict(ts=ts, o=o, h=h, l=l, c=c, atr=atr, m=[x[0] for x in mm], day=[x[1] for x in mm], n=n)


def in_window(m):
    return 420 <= m < 720 or 810 <= m < 960


def run_trade(X, i_entry, side, sp, tp1r, tp2r, e):
    """Симуляция сделки с входом по open[i_entry]; возвращает (R, bar_exit) с учётом издержек."""
    o, h, l, c, m, n = X["o"], X["h"], X["l"], X["c"], X["m"], X["n"]
    sl = e * (1 - side * sp); t1 = e * (1 + side * tp1r * sp); t2 = e * (1 + side * tp2r * sp)
    part = 1.0; r = 0.0; be = False
    j = i_entry
    while j < n:
        if m[j] >= 960 or m[j] < 420 and j > i_entry:   # вышли из сессии
            r += part * side * (o[j] / e - 1) / sp
            return r - 2 * SIDE / sp, j
        cur_sl = e if be else sl
        hit_sl = (l[j] <= cur_sl) if side == 1 else (h[j] >= cur_sl)
        hit1 = (h[j] >= t1) if side == 1 else (l[j] <= t1)
        hit2 = (h[j] >= t2) if side == 1 else (l[j] <= t2)
        if hit_sl and not (be and False):
            r += part * side * (cur_sl / e - 1) / sp
            return r - 2 * SIDE / sp, j
        if not be and hit1:
            r += 0.5 * tp1r; part = 0.5; be = True
            if hit2:
                r += 0.5 * tp2r
                return r - 2 * SIDE / sp, j
        elif be and hit2:
            r += part * tp2r
            return r - 2 * SIDE / sp, j
        j += 1
    return r - 2 * SIDE / sp, n - 1


def detect(X, minsweep=0.3, tp1r=1.5, tp2r=4.0, max_trades=2, max_losses=2, directions=(1, -1)):
    """Возвращает список сделок TJR и зеркальных контролей: (bar, tday, side, R_tjr, R_mirror)."""
    o, h, l, c, atr, m, day, n = X["o"], X["h"], X["l"], X["c"], X["atr"], X["m"], X["day"], X["n"]
    out = []
    i = 30
    cur_day = None; ahi = alo = None; trades = losses = 0; setup = None
    while i < n - 2:
        d = day[i]
        if d != cur_day:
            cur_day = d; ahi = -1e18; alo = 1e18; trades = losses = 0; setup = None
        mi = m[i]
        if mi < 240:
            ahi = max(ahi, h[i]); alo = min(alo, l[i]); i += 1; continue
        if not in_window(mi) or ahi < 0 or atr[i] <= 0 or trades >= max_trades or losses >= max_losses:
            setup = None if not in_window(mi) else setup
            i += 1; continue
        a = atr[i]
        if setup is None:
            if h[i] > ahi + minsweep * a and c[i] < ahi and -1 in directions:
                setup = dict(side=-1, ext=h[i], idx=i, stage="sweep")
            elif l[i] < alo - minsweep * a and c[i] > alo and 1 in directions:
                setup = dict(side=1, ext=l[i], idx=i, stage="sweep")
        elif setup["stage"] == "sweep":
            s = setup["side"]
            if i - setup["idx"] > 40:
                setup = None
            else:
                setup["ext"] = max(setup["ext"], h[i]) if s == -1 else min(setup["ext"], l[i])
                body = abs(c[i] - o[i])
                if s == -1 and body > 0.5 * a and c[i] < o[i] and c[i] < min(l[i - 8:i]):
                    k = next((j for j in range(i - 1, max(i - 11, 0), -1) if c[j] > o[j]), None)
                    if k is not None:
                        setup.update(stage="armed", zlo=l[k], zhi=h[k], bos=i)
                elif s == 1 and body > 0.5 * a and c[i] > o[i] and c[i] > max(h[i - 8:i]):
                    k = next((j for j in range(i - 1, max(i - 11, 0), -1) if c[j] < o[j]), None)
                    if k is not None:
                        setup.update(stage="armed", zlo=l[k], zhi=h[k], bos=i)
        else:  # armed → ждём ретест зоны
            s = setup["side"]
            if i - setup["bos"] > 40:
                setup = None
            else:
                touched = (h[i] >= setup["zlo"] and c[i] < o[i]) if s == -1 else (l[i] <= setup["zhi"] and c[i] > o[i])
                if touched:
                    e = o[i + 1]
                    stop = setup["ext"] + 0.5 * a if s == -1 else setup["ext"] - 0.5 * a
                    sp = (stop - e) / e if s == -1 else (e - stop) / e
                    setup_ext, zlo_, zhi_, bos_, sw_idx = setup["ext"], setup["zlo"], setup["zhi"], setup["bos"], setup["idx"]
                    setup = None
                    if 0.0015 < sp < 0.05:
                        r, jx = run_trade(X, i + 1, s, sp, tp1r, tp2r, e)
                        rm, _ = run_trade(X, i + 1, -s, sp, tp1r, tp2r, e)   # зеркало: та же дистанция, обратная сторона
                        out.append((i, d, s, r, rm, dict(e=e, stop=stop, sp=sp, ahi=ahi, alo=alo, ext=setup_ext, zlo=zlo_, zhi=zhi_, bos=bos_, sweep_idx=sw_idx)))
                        trades += 1
                        if r < 0: losses += 1
                        i = jx
        i += 1
    return out


def metrics(rs):
    n = len(rs)
    if n < 5:
        return dict(n=n, exp=0.0, win=0.0, pf=0.0, t=0.0, sumR=0.0)
    m = sum(rs) / n; sd = st.pstdev(rs)
    gw = sum(x for x in rs if x > 0); gl = -sum(x for x in rs if x < 0)
    return dict(n=n, exp=round(m, 3), win=round(sum(1 for x in rs if x > 0) / n, 3), pf=round(gw / gl, 2) if gl else 99.0,
                t=round(m / (sd / math.sqrt(n)), 2) if sd else 0.0, sumR=round(sum(rs), 1))


def main(symbols):
    data = {}
    for s in symbols:
        rows = lake.load(s, "5")
        if len(rows) > 5000:
            data[s] = prep(rows)
    cut = {s: X["ts"][int(X["n"] * 0.6)] for s, X in data.items()}
    grid = [(ms, t1, t2) for ms in (0.3, 0.6) for (t1, t2) in ((1.5, 4.0), (1.0, 2.0), (2.0, 3.0))]
    rep = []
    for ms, t1, t2 in grid:
        tr = []; te = []; mir_te = []; by_sym = {}; by_side = {1: [], -1: []}
        for s, X in data.items():
            for (i, d, side, r, rm, _info) in detect(X, ms, t1, t2):
                if X["ts"][i] < cut[s]:
                    tr.append(r)
                else:
                    te.append(r); mir_te.append(rm); by_sym.setdefault(s, []).append(r); by_side[side].append(r)
        rep.append(dict(minsweep=ms, tp1=t1, tp2=t2, train=metrics(tr), test=metrics(te), mirror_test=metrics(mir_te),
                        test_by_symbol={k: metrics(v) for k, v in by_sym.items()},
                        test_long=metrics(by_side[1]), test_short=metrics(by_side[-1])))
    best = max(rep, key=lambda r: r["train"]["exp"])
    out = dict(symbols=list(data), bars={s: X["n"] for s, X in data.items()},
               span_days={s: round((X["ts"][-1] - X["ts"][0]) / 86_400_000) for s, X in data.items()}, grid=rep, best_by_train=best)
    (BASE / "data" / "tjr_report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys
    o = main(sys.argv[1:] or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "SUIUSDT", "WIFUSDT", "1000PEPEUSDT"])
    print("монеты:", o["bars"], "дней:", o["span_days"])
    for r in o["grid"]:
        print(f"sweep>{r['minsweep']}ATR tp {r['tp1']}/{r['tp2']} | TRAIN n={r['train']['n']} E={r['train']['exp']:+.3f}R | "
              f"TEST n={r['test']['n']} E={r['test']['exp']:+.3f}R win={r['test']['win']:.0%} PF={r['test']['pf']} t={r['test']['t']} | "
              f"ЗЕРКАЛО E={r['mirror_test']['exp']:+.3f}R | long E={r['test_long']['exp']:+.2f}(n={r['test_long']['n']}) short E={r['test_short']['exp']:+.2f}(n={r['test_short']['n']})")
    b = o["best_by_train"]
    print("ЛУЧШАЯ по train:", b["minsweep"], b["tp1"], b["tp2"], "→ TEST", b["test"])
    print("по монетам (test):", {k: (v["n"], v["exp"]) for k, v in b["test_by_symbol"].items()})
