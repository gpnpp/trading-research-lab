"""
memecoin_scanner.py — сканер мем-коинов Solana (DexScreener + RugCheck, публичные API, без ключей).

Находит кандидатов, классифицирует на СКАЛЬП / ДЕЙТРЕЙД / СРЕДНЕСРОК, считает вход/стоп/тейки,
пишет подробное описание сделки и ведёт БУМАЖНОЕ сопровождение (TP/SL/истечение) — реальная
статистика побед в data/memecoin.db. Ничего не покупает и не требует ключей кошелька.
"""
from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import time
import urllib.request

BASE = pathlib.Path(__file__).parent
DB = BASE / "data" / "memecoin.db"
log = logging.getLogger("memecoin")
UA = {"User-Agent": "Mozilla/5.0"}

# стоп/тейки в долях, ttl — время жизни идеи (сек)
MODES = {
    "scalp": dict(label="Скальп", sl=0.08, tp=(0.10, 0.20), ttl=3 * 3600),
    "day": dict(label="Дейтрейд", sl=0.15, tp=(0.25, 0.50), ttl=36 * 3600),
    "swing": dict(label="Среднесрок", sl=0.25, tp=(0.50, 1.20), ttl=10 * 86400),
}


def _get(url):
    err = None
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.5 * (i + 1))
    log.warning("GET %s: %s", url, err)
    return None


def db():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, mint TEXT, symbol TEXT, name TEXT,
        url TEXT, entry REAL, stop REAL, tp1 REAL, tp2 REAL, score INTEGER, risk TEXT,
        description TEXT, metrics TEXT, status TEXT DEFAULT 'open', last_price REAL, closed_ts REAL,
        pnl_pct REAL, tp1_hit INTEGER DEFAULT 0, expires REAL)"""
    )
    return c


def candidates():
    mints = {}
    for u in (
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ):
        for x in _get(u) or []:
            if x.get("chainId") == "solana":
                mints[x["tokenAddress"]] = 1
    for q in ("pump", "dog", "cat", "pepe", "ai", "trump"):
        d = _get("https://api.dexscreener.com/latest/dex/search?q=" + q) or {}
        for p in d.get("pairs", [])[:30]:
            if p.get("chainId") == "solana":
                mints[p["baseToken"]["address"]] = 1
    return list(mints)


def pairs_for(mints):
    best = {}
    for i in range(0, len(mints), 30):
        for p in _get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []:
            if p.get("quoteToken", {}).get("symbol") not in ("SOL", "USDC", "USDT"):
                continue
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd", 0) or 0
            if m not in best or liq > ((best[m].get("liquidity") or {}).get("usd", 0) or 0):
                best[m] = p
    return best


def rugcheck(mint):
    d = _get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
    if not d:
        return None
    risks = d.get("risks", [])
    return dict(
        score=d.get("score_normalised"),
        lp_locked=d.get("lpLockedPct"),
        danger=[r["name"] for r in risks if r.get("level") == "danger"],
        warn=[r["name"] for r in risks if r.get("level") == "warn"],
    )


def classify(p):
    """Возвращает (mode, score, reasons) или None."""
    liq = (p.get("liquidity") or {}).get("usd", 0) or 0
    v = p.get("volume") or {}
    ch = p.get("priceChange") or {}
    tx = p.get("txns") or {}
    age_h = (time.time() * 1000 - (p.get("pairCreatedAt") or 0)) / 3.6e6
    fdv = p.get("fdv") or 0

    def ratio(w):
        t = tx.get(w) or {}
        b, s = t.get("buys", 0), t.get("sells", 0)
        return b / max(s, 1), b + s

    r5, n5 = ratio("m5")
    r1, _ = ratio("h1")
    r6, _ = ratio("h6")
    c5, c1, c6, c24 = (ch.get(k) or 0 for k in ("m5", "h1", "h6", "h24"))
    if liq < 15000 or (fdv and fdv < 30000):
        return None
    turn = (v.get("h1", 0) or 0) / liq

    if liq >= 20000 and 0.5 <= c5 <= 15 and 2 <= c1 <= 40 and r5 >= 1.15 and n5 >= 40 and turn >= 0.3:
        sc = 50 + min(15, (r5 - 1) * 20) + min(15, turn * 8) + (10 if r1 > 1.1 else 0) + (5 if liq > 60000 else 0)
        return "scalp", sc, [f"импульс 5м {c5:+.1f}% / 1ч {c1:+.1f}%",
                             f"покупок/продаж 5м = {r5:.2f} ({n5} сделок)",
                             f"оборот за час = {turn:.1f}× ликвидности"]
    if liq >= 50000 and age_h >= 3 and 3 <= c6 <= 80 and c1 > -3 and r1 >= 1.05 and r6 >= 1.0 and turn >= 0.1:
        sc = 48 + min(15, c6 / 4) + min(12, (r1 - 1) * 20) + min(10, liq / 50000) + (5 if c1 > 0 else 0)
        return "day", sc, [f"тренд 6ч {c6:+.1f}%, 1ч {c1:+.1f}%", f"покупок/продаж 1ч = {r1:.2f}",
                           f"ликвидность ${liq:,.0f}, возраст {age_h:.0f} ч"]
    if liq >= 150000 and age_h >= 72 and 5 <= c24 <= 120 and c1 > -5 and r6 >= 1.0 and fdv >= 500000:
        sc = 45 + min(15, c24 / 6) + min(15, liq / 150000 * 3) + min(8, age_h / 72) + (5 if c6 > 0 else 0)
        return "swing", sc, [f"суточный рост {c24:+.1f}%, зрелость {age_h / 24:.0f} дн.",
                             f"ликвидность ${liq:,.0f}, FDV ${fdv:,.0f}"]
    return None


def describe(mode, p, reasons, rc, entry, stop, tp1, tp2):
    m = MODES[mode]
    sym = p["baseToken"]["symbol"]
    txt = [f"{m['label']}-сделка по {sym}. Причины входа: " + "; ".join(reasons) + "."]
    txt.append(
        f"План: вход ≈ {entry:.8g} $, стоп {stop:.8g} (−{m['sl'] * 100:.0f}%), "
        f"TP1 {tp1:.8g} (+{m['tp'][0] * 100:.0f}%) — зафиксировать половину и перенести стоп в безубыток; "
        f"TP2 {tp2:.8g} (+{m['tp'][1] * 100:.0f}%). "
        f"Идея живёт {m['ttl'] / 3600:.0f} ч, затем закрывается по рынку."
    )
    if rc:
        txt.append(
            f"RugCheck: риск-скор {rc['score']}/100 (меньше — лучше), LP заблокирован {rc['lp_locked'] or 0:.0f}%"
            + (f", предупреждения: {', '.join(rc['warn'])}" if rc["warn"] else "") + "."
        )
    txt.append("Бумажная идея сканера, не гарантия: мем-коин может упасть на 90% за минуты, "
               "стоп на DEX исполняется с проскальзыванием.")
    return " ".join(txt)


def update_open(c, prices):
    now = time.time()
    for r in c.execute("SELECT * FROM signals WHERE status='open'").fetchall():
        p = prices.get(r["mint"])
        if not p:
            if now > r["expires"]:
                c.execute("UPDATE signals SET status='expired',closed_ts=?,pnl_pct=? WHERE id=?",
                          (now, -100.0 if not r["tp1_hit"] else 0.0, r["id"]))
            continue
        px = float(p.get("priceUsd") or 0)
        if px <= 0:
            continue
        tp1_hit = r["tp1_hit"]
        status, pnl = "open", None
        half = (r["tp1"] / r["entry"] - 1) * 50 if tp1_hit else 0  # результат зафиксированной половины, %
        w = 0.5 if tp1_hit else 1.0
        if px <= r["stop"]:
            status = "be" if tp1_hit else "sl"
            pnl = (r["stop"] / r["entry"] - 1) * 100 * w + half
        elif px >= r["tp2"]:
            status = "tp2"
            pnl = (r["tp2"] / r["entry"] - 1) * 50 + (r["tp1"] / r["entry"] - 1) * 50
        elif px >= r["tp1"] and not tp1_hit:
            tp1_hit = 1
            c.execute("UPDATE signals SET stop=entry WHERE id=?", (r["id"],))
        elif now > r["expires"]:
            status = "expired"
            pnl = (px / r["entry"] - 1) * 100 * w + half
        c.execute("UPDATE signals SET last_price=?,tp1_hit=?,status=?,pnl_pct=?,closed_ts=? WHERE id=?",
                  (px, tp1_hit, status, pnl, now if status != "open" else None, r["id"]))


def scan_once():
    c = db()
    pr = pairs_for(candidates())
    open_m = [r["mint"] for r in c.execute("SELECT DISTINCT mint FROM signals WHERE status='open'")]
    missing = [m for m in open_m if m not in pr]
    if missing:
        pr.update(pairs_for(missing))
    update_open(c, pr)
    new = 0
    for mint, p in pr.items():
        res = classify(p)
        if not res:
            continue
        mode, score, reasons = res
        if c.execute("SELECT 1 FROM signals WHERE mint=? AND mode=? AND (status='open' OR ts>?)",
                     (mint, mode, time.time() - 6 * 3600)).fetchone():
            continue
        rc = rugcheck(mint)
        if rc and (rc["danger"] or (rc["score"] or 0) > 60):
            continue
        if rc is None:
            score -= 10
        else:
            score += (10 if (rc["lp_locked"] or 0) > 90 else 0) - min(10, (rc["score"] or 0) / 6)
        if score < 55:
            continue
        m = MODES[mode]
        entry = float(p["priceUsd"])
        stop, tp1, tp2 = entry * (1 - m["sl"]), entry * (1 + m["tp"][0]), entry * (1 + m["tp"][1])
        risk = "высокий" if mode == "scalp" or (rc and (rc["score"] or 0) > 35) else "средний"
        metrics = dict(pair=p.get('pairAddress'), liq=(p.get("liquidity") or {}).get("usd"), fdv=p.get("fdv"),
                       vol24=(p.get("volume") or {}).get("h24"), ch=p.get("priceChange"),
                       txns=p.get("txns"), rug=rc)
        c.execute(
            """INSERT INTO signals(ts,mode,mint,symbol,name,url,entry,stop,tp1,tp2,score,risk,description,
               metrics,last_price,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), mode, mint, p["baseToken"]["symbol"], p["baseToken"]["name"], p["url"], entry, stop,
             tp1, tp2, int(score), risk, describe(mode, p, reasons, rc, entry, stop, tp1, tp2),
             json.dumps(metrics), entry, time.time() + m["ttl"]),
        )
        new += 1
    c.commit()
    c.close()
    log.info("скан: токенов %d, новых сигналов %d", len(pr), new)
    return new


def stats():
    c = db()
    out = {}
    for mode in MODES:
        rows = c.execute("SELECT status,pnl_pct FROM signals WHERE mode=?", (mode,)).fetchall()
        closed = [r for r in rows if r["status"] != "open"]
        wins = [r for r in closed if (r["pnl_pct"] or 0) > 0]
        out[mode] = dict(
            total=len(rows), open=len(rows) - len(closed), closed=len(closed),
            winrate=round(len(wins) / len(closed) * 100, 1) if closed else None,
            avg_pnl=round(sum(r["pnl_pct"] or 0 for r in closed) / len(closed), 2) if closed else None)
    c.close()
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("новых:", scan_once())
    print(stats())
