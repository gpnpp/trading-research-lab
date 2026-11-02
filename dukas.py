"""
dukas.py — загрузка минутных свечей Dukascopy (индексы, форекс, золото) в data/lake.db, таблица m1(symbol, ts, o,h,l,c,v).
Источник: https://datafeed.dukascopy.com/datafeed/<INST>/<YYYY>/<MM(0-11)>/<DD>/BID_candles_min_1.bi5 (LZMA, записи 24 байта big-endian:
сек от 00:00 UTC, open, close, low, high (int), объём float). Месяцы в пути нумеруются с 0.
"""
import concurrent.futures as cf
import datetime as dt
import lzma
import sqlite3
import struct
import sys
import time
import urllib.request

import data_lake as lake

INST = {  # имя в базе -> (имя у Dukascopy, делитель цены)
    "SPX": ("USA500IDXUSD", 1000), "NDX": ("USATECHIDXUSD", 1000),
    "EURUSD": ("EURUSD", 100000), "GBPUSD": ("GBPUSD", 100000), "USDJPY": ("USDJPY", 1000), "XAUUSD": ("XAUUSD", 1000),
}


def con():
    c = lake.con()
    c.execute("CREATE TABLE IF NOT EXISTS m1(symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY(symbol, ts))")
    c.execute("CREATE TABLE IF NOT EXISTS m1_days(symbol TEXT, day TEXT, n INTEGER, PRIMARY KEY(symbol, day))")
    return c


def fetch_day(name, day):
    time.sleep(0.4)
    inst, div = INST[name]
    url = f"https://datafeed.dukascopy.com/datafeed/{inst}/{day.year}/{day.month - 1:02d}/{day.day:02d}/BID_candles_min_1.bi5"
    for i in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=12) as r:
                raw = r.read()
            if not raw:
                return name, day, []
            d = lzma.decompress(raw)
            base = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
            out = []
            for k in range(len(d) // 24):
                s, o, c_, l, h, v = struct.unpack(">IIIIIf", d[k * 24:(k + 1) * 24])
                out.append((name, (base + s) * 1000, o / div, h / div, l / div, c_ / div, v))
            return name, day, out
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return name, day, []
            time.sleep(45 if e.code == 429 else 1 + i)   # 429 = лимит запросов: ждём и не долбим сервер
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1))
    return name, day, None   # не удалось — повторим позже


def update(names, days_back=330, workers=8):
    c = con()
    end = dt.date.today() - dt.timedelta(days=1)
    todo = []
    for n in names:
        have = {r[0] for r in c.execute("SELECT day FROM m1_days WHERE symbol=?", (n,))}
        for k in range(days_back):
            d = end - dt.timedelta(days=k)
            if d.weekday() == 5:      # суббота: рынок закрыт
                continue
            if d.isoformat() not in have:
                todo.append((n, d))
    done = fail = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        for name, day, rows in ex.map(lambda t: fetch_day(*t), todo):
            if rows is None:
                fail += 1
                continue
            if rows:
                c.executemany("INSERT OR IGNORE INTO m1 VALUES(?,?,?,?,?,?,?)", rows)
            c.execute("INSERT OR REPLACE INTO m1_days VALUES(?,?,?)", (name, day.isoformat(), len(rows)))
            done += 1
            if done % 25 == 0:
                c.commit()
                print(f"  {done}/{len(todo)} (ошибок {fail})", flush=True)
    c.commit()
    c.close()
    return done, fail


def bars(name, tf_min=5):
    """Агрегирует минутки в tf_min-свечи: [(ts, o, h, l, c, v)]."""
    c = con()
    rows = c.execute("SELECT ts,o,h,l,c,v FROM m1 WHERE symbol=? ORDER BY ts", (name,)).fetchall()
    c.close()
    ms = tf_min * 60_000
    out = []; cur = None
    for ts, o, h, l, cl, v in rows:
        b = ts - ts % ms
        if cur is None or cur[0] != b:
            if cur:
                out.append(tuple(cur))
            cur = [b, o, h, l, cl, v]
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = cl; cur[5] += v
    if cur:
        out.append(tuple(cur))
    return out


if __name__ == "__main__":
    names = sys.argv[1:] or list(INST)
    t = time.time()
    print(update(names), round(time.time() - t), "сек")
