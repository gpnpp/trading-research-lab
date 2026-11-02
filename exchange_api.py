"""
exchange_api.py — данные для вкладки «Биржа»: пары из файлов бота, реальные свечи, уровни и риск-менеджмент.

Пары берутся из:  config.py (SYMBOL_UNIVERSE — Bybit spot/linear)  и  data/memecoin.db (мем-коины Solana).
Свечи: Bybit public API (без ключей) и GeckoTerminal (Solana). Уровни стоп/тейк считаются по ATR
(Bybit) или берутся из сигнала сканера (мем-коины).
"""
from __future__ import annotations

import json
import pathlib
import re
import time
import urllib.request

import memecoin_scanner as ms

BASE = pathlib.Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0"}
MODE_TF = {"scalp": "1", "day": "15", "swing": "240"}          # таймфрейм по режиму
BYBIT_MODE = {"SCALPING": "scalp", "DAYTRADE": "day", "SWING": "swing"}
TF_LABEL = {"1": "1м", "15": "15м", "240": "4ч"}


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.load(r)


def _limits():
    src = (BASE / "config.py").read_text(encoding="utf-8")
    g = lambda n, d: float(re.search(rf"{n}:\s*float\s*=\s*([\d.]+)", src).group(1)) if re.search(rf"{n}:\s*float\s*=\s*([\d.]+)", src) else d
    return dict(max_order_usd=g("MAX_ORDER_USD", 10), max_daily_dd=g("MAX_DAILY_DRAWDOWN_PCT", 5), hard_sl=g("HARD_STOP_LOSS_PCT", 2))


def bybit_pairs():
    src = (BASE / "config.py").read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r'SymbolConfig\("(\w+)",\s*AssetClass\.(\w+),\s*\(([^)]*)\)(?:,\s*max_leverage=(\d+))?', src):
        modes = [BYBIT_MODE[x] for x in re.findall(r"StrategyMode\.(\w+)", m.group(3))]
        out.append(dict(src="bybit", symbol=m.group(1), cat="spot" if m.group(2) == "SPOT" else "linear",
                        modes=modes, leverage=int(m.group(4) or 1)))
    return out


def solana_pairs():
    c = ms.db()
    rows = c.execute("""SELECT mint,symbol,MAX(ts) ts,GROUP_CONCAT(DISTINCT mode) modes,MAX(score) score
                        FROM signals GROUP BY mint ORDER BY ts DESC LIMIT 100""").fetchall()
    c.close()
    return [dict(src="solana", symbol=r["symbol"], mint=r["mint"], cat="dex", modes=r["modes"].split(","), score=r["score"])
            for r in rows]


def all_pairs():
    return dict(pairs=bybit_pairs() + solana_pairs(), limits=_limits())


# ------------------------------------------------------------ свечи
def bybit_candles(symbol, cat, tf, limit=300):
    d = _get(f"https://api.bybit.com/v5/market/kline?category={cat}&symbol={symbol}&interval={tf}&limit={limit}")
    return [dict(t=int(k[0]) // 1000, o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]), v=float(k[5]))
            for k in reversed(d["result"]["list"])]


def solana_candles(mint, tf, limit=300):
    pair = None
    c = ms.db()
    row = c.execute("SELECT metrics FROM signals WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
    c.close()
    if row:
        pair = json.loads(row["metrics"] or "{}").get("pair")
    if not pair:
        d = _get("https://api.dexscreener.com/tokens/v1/solana/" + mint)
        pair = max(d, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))["pairAddress"]
    unit, agg = {"1": ("minute", 1), "15": ("minute", 15), "240": ("hour", 4)}[tf]
    d = _get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair}/ohlcv/{unit}?aggregate={agg}&limit={limit}")
    k = d["data"]["attributes"]["ohlcv_list"]
    return [dict(t=int(x[0]), o=x[1], h=x[2], l=x[3], c=x[4], v=x[5]) for x in reversed(k)]


# ------------------------------------------------------------ уровни
def atr(cs, n=14):
    trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(cs, cs[1:])]
    return sum(trs[-n:]) / max(1, len(trs[-n:]))


def ema(vals, n):
    k, e = 2 / (n + 1), vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def poc(cs, buckets=40):
    lo, hi = min(c["l"] for c in cs), max(c["h"] for c in cs)
    if hi <= lo:
        return lo
    step = (hi - lo) / buckets
    vol = [0.0] * buckets
    for c in cs:
        i = min(buckets - 1, int(((c["h"] + c["l"]) / 2 - lo) / step))
        vol[i] += c["v"]
    return lo + (vol.index(max(vol)) + 0.5) * step


def bybit_levels(cs, mode, lim, cat):
    last = cs[-1]["c"]
    a = atr(cs)
    closes = [c["c"] for c in cs]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    # спот — только лонг; на фьючерсах допускается шорт по тренду
    side = "LONG" if e20 >= e50 or cat == "spot" else "SHORT"
    mult = {"scalp": 1.2, "day": 1.5, "swing": 2.0}[mode]
    dist = min(a * mult, last * lim["hard_sl"] / 100)       # стоп не шире HARD_STOP_LOSS_PCT
    sgn = 1 if side == "LONG" else -1
    stop = last - sgn * dist
    tp1, tp2 = last + sgn * dist * 1.5, last + sgn * dist * 3
    return dict(side=side, entry=last, stop=stop, tp1=tp1, tp2=tp2, atr=a, poc=poc(cs[-150:]),
                swing_high=max(c["h"] for c in cs[-60:]), swing_low=min(c["l"] for c in cs[-60:]),
                trend=f"EMA20 {'>' if e20 >= e50 else '<'} EMA50",
                note=f"Стоп ограничен HARD_STOP_LOSS_PCT={lim['hard_sl']}%. Цели 1.5R и 3R. Это предложение сканера, "
                     f"реальные ордера бота проходят через Risk Core в executor.py.")


def solana_levels(cs, mint, mode):
    c = ms.db()
    q = "SELECT * FROM signals WHERE mint=?" + (" AND mode=?" if mode else "") + " ORDER BY ts DESC LIMIT 1"
    s = c.execute(q, (mint, mode) if mode else (mint,)).fetchone()
    c.close()
    last = cs[-1]["c"]
    if not s:
        return dict(side="LONG", entry=last, stop=None, tp1=None, tp2=None, poc=poc(cs[-150:]), note="Нет активного сигнала.")
    return dict(side="LONG", entry=s["entry"], stop=s["stop"], tp1=s["tp1"], tp2=s["tp2"], poc=poc(cs[-150:]),
                signal_mode=s["mode"], status=s["status"], score=s["score"], risk=s["risk"], note=s["description"],
                swing_high=max(c["h"] for c in cs[-60:]), swing_low=min(c["l"] for c in cs[-60:]))


def chart(src, symbol, cat, mint, mode, tf):
    tf = tf or MODE_TF.get(mode, "15")
    lim = _limits()
    if src == "bybit":
        cs = bybit_candles(symbol, cat, tf)
        lv = bybit_levels(cs, mode or "day", lim, cat)
    else:
        cs = solana_candles(mint, tf)
        lv = solana_levels(cs, mint, mode)
    return dict(candles=cs, levels=lv, tf=tf, tf_label=TF_LABEL[tf], limits=lim, fetched=time.time())
