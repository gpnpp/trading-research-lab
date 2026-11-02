"""
validate_trend.py — независимая проверка (pre-registered) лучшей находки strategy_lab2 на СТАРЫХ данных,
которые не участвовали ни в подборе параметров, ни в тесте: 4ч, крупные монеты, 2020–начало 2023.
Конфиг зафиксирован заранее: ema_cross 20/100 (long+short) с vol-target; соседи — только для оценки устойчивости.
"""
import datetime as dt
import json
import math
import statistics as st

import strategy_lab2 as L
import funding as F
import data_lake as lake

CUT_DAY = int(dt.datetime(2023, 2, 1, tzinfo=dt.timezone.utc).timestamp() * 1000 // L.DAY)
CONFIGS = [(20, 100, True), (12, 48, True), (30, 150, True), (50, 200, True), (20, 100, False)]


def port(series_by_sym, lo=None, hi=None):
    acc = {}
    for ser in series_by_sym.values():
        d = {}
        for day, r in ser:
            if (lo is not None and day < lo) or (hi is not None and day >= hi):
                continue
            d[day] = d.get(day, 0.0) + r
        for day, r in d.items():
            acc.setdefault(day, []).append(r)
    days = sorted(acc)
    return days, [sum(acc[d]) / len(acc[d]) for d in days]   # равные доли среди активных монет


def year_table(days, rets):
    by = {}
    for d, r in zip(days, rets):
        by.setdefault(dt.date.fromordinal(719163 + d).year, []).append(r)
    return {y: (round(sum(v) * 100, 1), L.perf(v)["sharpe"]) for y, v in sorted(by.items())}


def main():
    D = {s: L.load(s, "240") for s in lake.MAJORS}
    out = []
    for f, s_, vt in CONFIGS:
        ser = {}
        for s, d in D.items():
            pos = L.pos_ema(d, f, s_, True)
            if vt:
                pos = L.vol_target(d, pos, 6)
            base = L.series_returns(d, pos)
            fr = F.load(s)
            ser[s] = [(dd, r - pos[i] * fr.get(d["ts"][i], 0.0)) for i, (dd, r) in enumerate(base)]   # лонг платит ставку, шорт получает
        d_old, r_old = port(ser, None, CUT_DAY)     # НОВОЕ ОКНО: до 2023-02
        d_new, r_new = port(ser, CUT_DAY, None)     # окно, где подбирали/тестировали
        d_all, r_all = port(ser)
        m_old = L.perf(r_old)
        pos_syms = {}
        for s, x in ser.items():
            pr = [r for dd, r in x if dd < CUT_DAY]
            pos_syms[s] = round(sum(pr) * 100, 1)
        out.append(dict(cfg=f"EMA {f}/{s_} 4ч {'vol-target' if vt else 'без vol-target'}", old_window=m_old,
                        new_window=L.perf(r_new), all=L.perf(r_all), years=year_table(d_all, r_all), old_by_coin=pos_syms))
    json.dump(out, open("data/validate_trend_funding.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for o in out:
        print(o["cfg"])
        print("  СТАРОЕ окно (<2023-02):", o["old_window"])
        print("  окно подбора (≥2023-02):", o["new_window"])
        print("  всё:", o["all"])
        print("  по годам (сумма %, Sharpe):", o["years"])
        print("  старое окно по монетам %:", o["old_by_coin"])


if __name__ == "__main__":
    main()
