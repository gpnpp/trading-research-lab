"""
funding.py — история ставок финансирования Bybit (perp) в lake.db и учёт её в доходности позиции.
Лонг платит положительную ставку, шорт её получает. Расчёт каждые 8 часов (00/08/16 UTC).
"""
import json
import sqlite3
import time
import urllib.request

import data_lake as lake


def con():
    c = lake.con()
    c.execute("CREATE TABLE IF NOT EXISTS funding(symbol TEXT, ts INTEGER, rate REAL, PRIMARY KEY(symbol, ts))")
    return c


def _page(sym, end=None):
    url = f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={sym}&limit=200"
    if end:
        url += f"&endTime={end}"
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


def update(sym, since_ms=1_580_000_000_000):
    c = con(); end = None; n = 0
    while True:
        rows = _page(sym, end)
        if not rows:
            break
        c.executemany("INSERT OR IGNORE INTO funding VALUES(?,?,?)", [(sym, int(r["fundingRateTimestamp"]), float(r["fundingRate"])) for r in rows])
        n += len(rows)
        oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
        if oldest <= since_ms or len(rows) < 200:
            break
        end = oldest - 1
        time.sleep(0.1)
    c.commit(); c.close()
    return n


def load(sym):
    c = con()
    r = dict(c.execute("SELECT ts, rate FROM funding WHERE symbol=?", (sym,)).fetchall())
    c.close()
    return r


if __name__ == "__main__":
    for s in lake.LAKE_UNIVERSE:
        print(s, update(s), flush=True)
