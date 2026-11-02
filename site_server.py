"""
site_server.py — сайт: сделки (скальп/дейтрейд/среднесрок), вкладка «Биржа» с графиками, вкладка «Автотрейдер».
Запуск:  python site_server.py   →  http://localhost:8080
Фоном запускает сканер (SCAN_INTERVAL) и бумажный/живой Solana-трейдер (solana_trader.py).
Только stdlib. Приватный ключ кошелька через API НЕ отдаётся.
"""
from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import exchange_api as ex
import memecoin_scanner as ms
import solana_trader as st
import trend_bot
from wallets import solana_wallet as w

BASE = pathlib.Path(__file__).parent
PORT = 8080
SCAN_INTERVAL = 120
_bal = {"t": 0, "v": None}


def scan_loop():
    while True:
        try:
            ms.scan_once()
        except Exception:  # noqa: BLE001
            logging.exception("scan failed")
        time.sleep(SCAN_INTERVAL)


def trader_loop():
    w.create_wallet()
    c = st.tdb()
    while True:
        try:
            st.open_new(c)
            st.manage(c)
        except Exception:  # noqa: BLE001
            logging.exception("trader failed")
        time.sleep(st.POLL)


def trend_loop():
    while True:
        try:
            trend_bot.step()
        except Exception:  # noqa: BLE001
            logging.exception("trend failed")
        time.sleep(300)


def api_trend():
    return json.loads(trend_bot.STATE.read_text(encoding="utf-8")) if trend_bot.STATE.exists() else {}


def api_signals():
    c = ms.db()
    rows = [dict(r) for r in c.execute("SELECT * FROM signals ORDER BY ts DESC LIMIT 300")]
    c.close()
    for r in rows:
        r["metrics"] = json.loads(r["metrics"] or "{}")
    return {"signals": rows, "stats": ms.stats(), "modes": {k: v["label"] for k, v in ms.MODES.items()}, "now": time.time()}


def api_trader():
    d = st.summary()
    if d["address"] and time.time() - _bal["t"] > 60:
        try:
            _bal["v"] = w.sol_balance()
        except Exception:  # noqa: BLE001
            _bal["v"] = None
        _bal["t"] = time.time()
    d["balance_sol"] = _bal["v"]
    closed = sorted((t for t in d["trades"] if t["status"] not in ("open", "rejected") and t["closed_ts"]), key=lambda t: t["closed_ts"])
    cum, curve, by = 0.0, [], {}
    for t in closed:
        pnl = t["sol_out"] - t["sol_in"]
        cum += pnl
        curve.append([int(t["closed_ts"]), round(cum, 6)])
        m = by.setdefault(t["mode"], dict(n=0, wins=0, pnl=0.0, invested=0.0))
        m["n"] += 1; m["wins"] += pnl > 0; m["pnl"] = round(m["pnl"] + pnl, 6); m["invested"] += t["sol_in"]
    d["curve"] = curve
    d["by_mode"] = by
    d["open_positions"] = [t for t in d["trades"] if t["status"] == "open"]
    return d


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ct = "application/json; charset=utf-8"
        try:
            if u.path == "/api/signals":
                data = api_signals()
            elif u.path == "/api/pairs":
                data = ex.all_pairs()
            elif u.path == "/api/chart":
                data = ex.chart(q.get("src"), q.get("symbol"), q.get("cat", "linear"), q.get("mint"), q.get("mode"), q.get("tf"))
            elif u.path == "/api/trend":
                data = api_trend()
            elif u.path == "/api/trader":
                data = api_trader()
            else:
                data = None
            if data is None:
                body, ct = (BASE / "site" / "index.html").read_bytes(), "text/html; charset=utf-8"
            else:
                body = json.dumps(data, ensure_ascii=False).encode()
            code = 200
        except Exception as e:  # noqa: BLE001
            logging.exception("api")
            body, code = json.dumps({"error": str(e)}).encode(), 500
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=trader_loop, daemon=True).start()
    threading.Thread(target=trend_loop, daemon=True).start()
    print(f"Сайт: http://localhost:{PORT}  | Solana-трейдер: {'LIVE' if st.LIVE else 'PAPER'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
