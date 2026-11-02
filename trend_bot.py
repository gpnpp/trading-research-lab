"""
trend_bot.py — тренд-портфель, единственная стратегия, прошедшая проверку на непросмотренных данных.

Правило (strategy_lab2 + validate_trend): 4-часовые свечи, EMA20 против EMA100; выше → лонг, ниже → шорт;
размер позиции по волатильности (цель 1% дневного риска на монету, плечо монеты ≤ 2×), равные доли
BTC, ETH, SOL, XRP, DOGE, SUI. Пересчёт раз в 4 часа по закрытой свече.
Бэктест 2020–2026 с комиссиями, проскальзыванием И финансированием: Sharpe ≈ 0.9, ≈ 15%/год, макс. просадка ≈ 15%,
плюс в каждый из 7 лет; на непросмотренном окне 2020–2023 Sharpe 1.15. Это скромное, но устойчивое преимущество,
а не гарантия: будущее может отличаться.

РЕЖИМ: только БУМАЖНЫЙ портфель + сигналы + Telegram. Реальное исполнение НЕ реализовано намеренно: при капитале
~$40 мин. размер контрактов (BTC 0.001, ETH 0.01) и доли ~$2 на монету делают портфель неисполнимым.
Состояние: data/trend_state.json
"""
from __future__ import annotations

import json
import math
import pathlib
import time

import data_lake as lake
import notifier
import strategy_lab2 as L

STATE = pathlib.Path(__file__).parent / "data" / "trend_state.json"
COINS = lake.MAJORS
FAST, SLOW = 20, 100
TARGET_DAILY_VOL, CAP = 0.01, 2.0
SIDE_COST = L.SIDE_COST


def target_weights():
    """Возвращает {coin: {side, weight, price, bar_ts}} по последней ЗАКРЫТОЙ 4ч свече."""
    out = {}
    for s in COINS:
        lake.update(s, "240", 1500)
        rows = lake.load(s, "240")
        if len(rows) < SLOW + 120:
            continue
        c = [r[4] for r in rows]
        ef, es = L.ema(c, FAST), L.ema(c, SLOW)
        side = 1 if ef[-1] > es[-1] else -1
        ret = [c[i] / c[i - 1] - 1 for i in range(len(c) - 100, len(c))]
        m = sum(ret) / len(ret)
        sd = math.sqrt(sum((x - m) ** 2 for x in ret) / len(ret))
        w = side * min(CAP, TARGET_DAILY_VOL / (sd * math.sqrt(6))) / len(COINS) if sd > 0 else 0.0
        out[s] = dict(side="LONG" if side == 1 else "SHORT", weight=round(w, 4), price=c[-1], bar_ts=rows[-1][0],
                      ema_fast=round(ef[-1], 6), ema_slow=round(es[-1], 6))
    return out


def step():
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else dict(equity=100.0, weights={}, prices={}, last_bar=0, history=[])
    tw = target_weights()
    if not tw:
        return st
    bar = max(v["bar_ts"] for v in tw.values())
    if bar <= st["last_bar"]:
        return st
    # переоценка бумажного портфеля с предыдущей закрытой свечи
    ret = sum(w * (tw[s]["price"] / st["prices"][s] - 1) for s, w in st["weights"].items() if s in st["prices"] and s in tw)
    new_w = {s: v["weight"] for s, v in tw.items()}
    turnover = sum(abs(new_w.get(s, 0) - st["weights"].get(s, 0)) for s in set(new_w) | set(st["weights"]))
    st["equity"] = round(st["equity"] * (1 + ret - SIDE_COST * turnover), 4)
    flips = [s for s in new_w if st["weights"].get(s, 0) * new_w[s] < 0 or (s not in st["weights"] and st["last_bar"])]
    st.update(weights=new_w, prices={s: v["price"] for s, v in tw.items()}, last_bar=bar, updated=time.strftime("%Y-%m-%d %H:%M:%S"),
              detail=tw)
    st["history"] = (st.get("history", []) + [[bar, st["equity"]]])[-2000:]
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    if flips:
        notifier.notify("📈 Тренд-портфель (бумажный): смена направления\n" + "\n".join(
            f"{s}: {tw[s]['side']} (доля {new_w[s]*100:+.1f}% капитала)" for s in flips) + f"\nБумажный капитал: {st['equity']}")
    return st


if __name__ == "__main__":
    s = step()
    print("бумажный капитал:", s["equity"], "| бар:", s["last_bar"])
    for k, v in s.get("detail", {}).items():
        print(f"{k:10s} {v['side']:5s} доля {v['weight']*100:+6.2f}%  цена {v['price']}")
