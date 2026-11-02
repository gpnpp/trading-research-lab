"""
data_lake.py — локальная база свечей (SQLite data/lake.db) для бэктестов и обучения.

Качает историю Bybit (public API, без ключей) постранично и докачивает только недостающее.
Таблица candles(symbol, tf, ts, o, h, l, c, v). tf — минуты как строка Bybit: "15", "60", "240".
Запуск:  python data_lake.py        — обновить всю вселенную из LAKE_UNIVERSE
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import time
import urllib.request

DB = pathlib.Path(__file__).parent / "data" / "lake.db"
MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "SUIUSDT"]
MEMES = ["1000PEPEUSDT", "WIFUSDT", "1000BONKUSDT", "FARTCOINUSDT", "POPCATUSDT", "TRUMPUSDT"]
LAKE_UNIVERSE = MAJORS + MEMES
TF_BARS = {"15": 20000, "60": 20000, "240": 8000}


def con():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS candles(symbol TEXT, tf TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
                 PRIMARY KEY(symbol, tf, ts))""")
    return c


def _kline(symbol, tf, end=None, limit=1000):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={tf}&limit={limit}"
    if end:
        url += f"&end={end}"
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
                d = json.load(r)
            if d.get("retCode") == 0:
                return d["result"]["list"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5 * (i + 1))
    return []


def _rows(symbol, tf, raw):
    return [(symbol, tf, int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw]


def update(symbol: str, tf: str, bars: int) -> int:
    c = con()
    added = 0
    n_have, mn, mx = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()
    # 1) догрузить свежее: идём назад от «сейчас», пока не дойдём до уже имеющегося
    end = None
    while True:
        raw = _kline(symbol, tf, end)
        if not raw:
            break
        rows = _rows(symbol, tf, raw)
        c.executemany("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?)", rows)
        added += len(rows)
        oldest = min(r[2] for r in rows)
        if not mx or oldest <= mx:
            break  # пустая база (глубокая докачка ниже) либо дошли до уже имеющегося
        end = oldest - 1
        time.sleep(0.1)
    # 2) докачать вглубь до нужного количества
    cnt, oldest = c.execute("SELECT COUNT(*), MIN(ts) FROM candles WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()
    while cnt < bars and oldest:
        raw = _kline(symbol, tf, oldest - 1)
        if not raw:
            break
        rows = _rows(symbol, tf, raw)
        c.executemany("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?)", rows)
        added += len(rows)
        new_oldest = min(r[2] for r in rows)
        if new_oldest >= oldest:
            break
        oldest = new_oldest
        cnt = c.execute("SELECT COUNT(*) FROM candles WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()[0]
        time.sleep(0.1)
    c.commit()
    c.close()
    return added


def load(symbol: str, tf: str) -> list[tuple]:
    c = con()
    rows = c.execute("SELECT ts,o,h,l,c,v FROM candles WHERE symbol=? AND tf=? ORDER BY ts", (symbol, tf)).fetchall()
    c.close()
    return rows[:-1] if rows else rows  # последняя свеча может быть незакрытой


def stats():
    c = con()
    r = c.execute("SELECT symbol, tf, COUNT(*), MIN(ts), MAX(ts) FROM candles GROUP BY symbol, tf ORDER BY symbol, tf").fetchall()
    c.close()
    return r


if __name__ == "__main__":
    for s in LAKE_UNIVERSE:
        for tf, n in TF_BARS.items():
            print(s, tf, "+", update(s, tf, n), flush=True)
    print("всего свечей в базе:", sum(r[2] for r in stats()))
