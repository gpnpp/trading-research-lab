"""
solana_trader.py — автономный трейдер мем-коинов Solana (горячий кошелёк + Jupiter).

РЕЖИМЫ
  по умолчанию PAPER: сделки считаются по реальным котировкам Jupiter (с проскальзыванием), но
  деньги не двигаются. LIVE включается ТОЛЬКО переменной окружения SOL_LIVE=true (в .env),
  и только если на горячем кошельке есть SOL.

RISK CORE (Solana, строже, чем Bybit; значения можно менять только в .env, не в рантайме):
  SOL_ORDER_SOL=0.02        размер одной сделки в SOL
  SOL_MAX_OPEN=3            макс. одновременных позиций
  SOL_MIN_SCORE=70          минимальная оценка сигнала сканера
  SOL_MODES=scalp,day       режимы, в которые разрешено входить (swing по умолчанию выключен)
  SOL_DAILY_LOSS_SOL=0.05   дневной лимит реализованного убытка → kill switch до следующих суток
  Входу всегда предшествует: RugCheck (в сканере) + roundtrip-проверка продажи через Jupiter.
  Стоп эмулируется опросом цены каждые SOL_POLL_SEC (на DEX нет стоп-ордеров) — возможен гэп.

Запуск:  python solana_trader.py
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

import memecoin_scanner as ms
import notifier
from wallets import solana_wallet as w

log = logging.getLogger("sol_trader")
LIVE = os.getenv("SOL_LIVE", "false").strip().lower() in ("1", "true", "yes")
ORDER_SOL = float(os.getenv("SOL_ORDER_SOL", "0.02"))
MAX_OPEN = int(os.getenv("SOL_MAX_OPEN", "3"))
MIN_SCORE = int(os.getenv("SOL_MIN_SCORE", "70"))
MODES = {m.strip() for m in os.getenv("SOL_MODES", "scalp,day").split(",")}
DAILY_LOSS = float(os.getenv("SOL_DAILY_LOSS_SOL", "0.05"))
POLL = int(os.getenv("SOL_POLL_SEC", "20"))
LAM = 1_000_000_000


def tdb():
    c = ms.db()
    c.execute("""CREATE TABLE IF NOT EXISTS sol_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER UNIQUE, ts REAL, mint TEXT, symbol TEXT, mode TEXT,
        paper INTEGER, sol_in REAL, tokens INTEGER, tokens_left INTEGER, sol_out REAL DEFAULT 0, status TEXT DEFAULT 'open',
        tx_buy TEXT, tx_sells TEXT DEFAULT '', closed_ts REAL, note TEXT)""")
    return c


def _sell(t, frac, price_note):
    amt = int(t["tokens_left"] * frac) if frac < 1 else t["tokens_left"]
    if amt <= 0:
        return 0.0, ""
    if LIVE:
        real = w.token_balance(t["mint"])
        amt = min(amt, real)
        sig = w.swap(t["mint"], w.SOL, amt, 500)
        out = int(w.quote(t["mint"], w.SOL, amt, 500)["outAmount"]) / LAM  # оценка; точная сумма — в кошельке
    else:
        sig = "paper"
        out = int(w.quote(t["mint"], w.SOL, amt, 500)["outAmount"]) / LAM
    log.info("SELL %s %s%% -> %.5f SOL (%s)", t["symbol"], int(frac * 100), out, price_note)
    return out, sig


def open_new(c):
    day_start = time.time() - 86400
    lost = -sum((r["sol_out"] - r["sol_in"]) for r in c.execute(
        "SELECT sol_in,sol_out FROM sol_trades WHERE status!='open' AND closed_ts>?", (day_start,))
        if r["sol_out"] - r["sol_in"] < 0)
    if lost >= DAILY_LOSS:
        log.warning("KILL SWITCH: дневной убыток %.4f SOL >= лимита %.4f", lost, DAILY_LOSS)
        return
    n_open = c.execute("SELECT COUNT(*) FROM sol_trades WHERE status='open'").fetchone()[0]
    if LIVE and w.sol_balance() < ORDER_SOL + 0.01:
        log.warning("LIVE: на кошельке мало SOL для сделки")
        return
    for s in c.execute("SELECT * FROM signals WHERE status='open' AND score>=? ORDER BY score DESC", (MIN_SCORE,)).fetchall():
        if n_open >= MAX_OPEN:
            break
        if s["mode"] not in MODES or time.time() - s["ts"] > 900:
            continue  # входим только в свежие сигналы
        if c.execute("SELECT 1 FROM sol_trades WHERE signal_id=?", (s["id"],)).fetchone():
            continue
        if c.execute("SELECT 1 FROM sol_trades WHERE mint=? AND status='open'", (s["mint"],)).fetchone():
            continue  # одна позиция на токен, без удвоения экспозиции
        chk = w.roundtrip_check(s["mint"], int(ORDER_SOL * LAM))
        if not chk["ok"]:
            log.info("пропуск %s: %s", s["symbol"], chk["reason"])
            c.execute("INSERT INTO sol_trades(signal_id,ts,mint,symbol,mode,paper,sol_in,tokens,tokens_left,status,closed_ts,note)"
                      " VALUES(?,?,?,?,?,?,0,0,0,'rejected',?,?)",
                      (s["id"], time.time(), s["mint"], s["symbol"], s["mode"], 0 if LIVE else 1, time.time(), chk["reason"]))
            c.commit()
            continue
        q = w.quote(w.SOL, s["mint"], int(ORDER_SOL * LAM), 300)
        tokens = int(q["outAmount"])
        sig = w.swap(w.SOL, s["mint"], int(ORDER_SOL * LAM), 300) if LIVE else "paper"
        c.execute("INSERT INTO sol_trades(signal_id,ts,mint,symbol,mode,paper,sol_in,tokens,tokens_left,tx_buy)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (s["id"], time.time(), s["mint"], s["symbol"], s["mode"], 0 if LIVE else 1, ORDER_SOL, tokens, tokens, sig))
        c.commit()
        n_open += 1
        log.info("BUY %s [%s] %.4f SOL (%s)", s["symbol"], s["mode"], ORDER_SOL, "LIVE" if LIVE else "paper")
        notifier.notify(
            f"🟢 {'LIVE' if LIVE else 'PAPER'} ВХОД Solana: {s['symbol']} [{s['mode']}]\n"
            f"Сумма: {ORDER_SOL} SOL | оценка {s['score']}\n"
            f"Вход {s['entry']:.8g} | стоп {s['stop']:.8g} | TP1 {s['tp1']:.8g} | TP2 {s['tp2']:.8g}\n{s['url']}")


def manage(c):
    for t in c.execute("SELECT * FROM sol_trades WHERE status='open'").fetchall():
        s = c.execute("SELECT * FROM signals WHERE id=?", (t["signal_id"],)).fetchone()
        pr = ms.pairs_for([t["mint"]]).get(t["mint"])
        if not pr:
            continue
        px = float(pr.get("priceUsd") or 0)
        half_done = t["tokens_left"] < t["tokens"]
        stop = s["entry"] if half_done else s["stop"]
        if px <= stop:
            out, sig = _sell(t, 1.0, "стоп")
            status = "be" if half_done else "sl"
        elif px >= s["tp2"]:
            out, sig = _sell(t, 1.0, "TP2")
            status = "tp2"
        elif px >= s["tp1"] and not half_done:
            out, sig = _sell(t, 0.5, "TP1")
            c.execute("UPDATE sol_trades SET sol_out=sol_out+?, tokens_left=tokens_left-?, tx_sells=tx_sells||? WHERE id=?",
                      (out, t["tokens_left"] // 2, sig + ";", t["id"]))
            c.commit()
            notifier.notify(f"🟡 {t['symbol']}: TP1 достигнут, продана половина (+{out:.4f} SOL), стоп в безубыток")
            continue
        elif time.time() > s["expires"]:
            out, sig = _sell(t, 1.0, "истёк срок")
            status = "expired"
        else:
            continue
        c.execute("UPDATE sol_trades SET sol_out=sol_out+?, tokens_left=0, status=?, closed_ts=?, tx_sells=tx_sells||? WHERE id=?",
                  (out, status, time.time(), sig + ";", t["id"]))
        c.commit()
        total = t["sol_out"] + out
        pnl = total - t["sol_in"]
        icon = "✅" if pnl > 0 else "🔴"
        names = {"sl": "стоп", "be": "безубыток", "tp2": "TP2", "expired": "истёк срок"}
        notifier.notify(f"{icon} {'LIVE' if LIVE else 'PAPER'} ЗАКРЫТО Solana: {t['symbol']} ({names.get(status, status)})\n"
                        f"Вложено {t['sol_in']} SOL → получено {total:.4f} SOL | PnL {pnl:+.4f} SOL")


def summary():
    c = tdb()
    rows = c.execute("SELECT * FROM sol_trades ORDER BY ts DESC LIMIT 100").fetchall()
    closed = [r for r in rows if r["status"] not in ("open", "rejected")]
    res = dict(live=LIVE, order_sol=ORDER_SOL, max_open=MAX_OPEN, min_score=MIN_SCORE, modes=sorted(MODES),
               daily_loss_sol=DAILY_LOSS, address=w.load_address() if w.WALLET_FILE.exists() else None,
               trades=[dict(r) for r in rows],
               pnl_sol=round(sum(r["sol_out"] - r["sol_in"] for r in closed), 5), closed=len(closed),
               wins=sum(1 for r in closed if r["sol_out"] > r["sol_in"]))
    c.close()
    return res


def main():
    logging.basicConfig(level=logging.INFO)
    w.create_wallet()
    log.info("Кошелёк %s | режим: %s", w.load_address(), "LIVE" if LIVE else "PAPER")
    c = tdb()
    while True:
        try:
            open_new(c)
            manage(c)
        except Exception:  # noqa: BLE001
            log.exception("цикл трейдера")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
