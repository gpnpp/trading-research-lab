"""
blind_build.py — готовит СЛЕПЫЕ пакеты для «дискреционной» проверки: на каждый механический сетап TJR (тестовый период)
создаётся описание ровно на момент решения — без исхода, без названия монеты и даты (чтобы модель не «вспомнила» историю).
Исходы лежат отдельно в data/blind_private/outcomes.json (судьи их не видят).
"""
import json
import pathlib
import random
import datetime as dt

import data_lake as lake
import tjr_lab as T

BASE = pathlib.Path(__file__).parent
OUT = BASE / "data" / "blind"
PRIV = BASE / "data" / "blind_private"
N_BATCH = 10
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "SUIUSDT", "WIFUSDT", "1000PEPEUSDT"]


def pct(x, e):
    return round((x / e - 1) * 100, 2)


def htf_rows(rows, upto_ts, tf_ms, n, e):
    sel = [r for r in rows if r[0] + tf_ms <= upto_ts][-n:]
    avg = sum(r[5] for r in sel) / max(1, len(sel))
    return [f"{k - len(sel):>4d}  {pct(r[1], e):7.2f} {pct(r[2], e):7.2f} {pct(r[3], e):7.2f} {pct(r[4], e):7.2f}  {r[5] / avg if avg else 0:4.1f}x"
            for k, r in enumerate(sel)]


def main():
    T.SIDE = 0.0   # валовая R; издержки применяем позже
    setups = []
    for s in SYMS:
        rows5 = lake.load(s, "5")
        X = T.prep(rows5)
        cut = X["ts"][int(X["n"] * 0.6)]
        r60, r240 = lake.load(s, "60"), lake.load(s, "240")
        vol = [r[5] for r in rows5]
        for (i, d, side, r, rm, info) in T.detect(X, 0.6, 1.5, 4.0):
            if X["ts"][i] < cut:
                continue
            setups.append((s, i, d, side, r, rm, info, X, rows5, r60, r240, vol))
    random.Random(7).shuffle(setups)
    OUT.mkdir(parents=True, exist_ok=True); PRIV.mkdir(parents=True, exist_ok=True)
    out_priv = {}; batches = [[] for _ in range(N_BATCH)]
    for n, (s, i, d, side, r, rm, info, X, rows5, r60, r240, vol) in enumerate(setups):
        pid = f"S{n:03d}"
        e = info["e"]; ts_dec = X["ts"][i + 1]       # момент входа = открытие следующего бара
        m = X["m"][i]
        sess = "London" if 420 <= m < 720 else "NY AM"
        et_min = (m + 20 * 60) % 1440
        L = [f"=== {pid} ===", f"Сессия: {sess}, время решения {et_min // 60:02d}:{et_min % 60:02d} ET (после закрытия 5м свечи)",
             f"Предлагаемая сделка: {'LONG' if side == 1 else 'SHORT'} по рынку (вход ≈ 0.00%)",
             f"Стоп: {pct(info['stop'], e):+.2f}% | TP1 (50% позиции, потом стоп в БУ): {side * 1.5 * info['sp'] * 100:+.2f}% | TP2: {side * 4 * info['sp'] * 100:+.2f}% | выход по времени в 12:00 ET",
             f"Азия high {pct(info['ahi'], e):+.2f}% / low {pct(info['alo'], e):+.2f}% ; экстремум sweep {pct(info['ext'], e):+.2f}%",
             f"Зона (OB) {pct(info['zlo'], e):+.2f}% … {pct(info['zhi'], e):+.2f}% ; sweep был {i - info['sweep_idx']} баров назад, BOS {i - info['bos']} баров назад",
             "5м свечи (баров назад, O H L C в % от цены входа, объём к средн.20):"]
        lo = max(0, i - 47)
        for k in range(lo, i + 1):
            a20 = sum(vol[max(0, k - 20):k]) / max(1, len(vol[max(0, k - 20):k]))
            L.append(f"{k - i:>4d}  {pct(X['o'][k], e):7.2f} {pct(X['h'][k], e):7.2f} {pct(X['l'][k], e):7.2f} {pct(X['c'][k], e):7.2f}  {vol[k] / a20 if a20 else 0:4.1f}x")
        L.append("1ч свечи (последние 30, баров назад):"); L += htf_rows(r60, ts_dec, 3_600_000, 30, e)
        L.append("4ч свечи (последние 14):"); L += htf_rows(r240, ts_dec, 14_400_000, 14, e)
        batches[n % N_BATCH].append("\n".join(L))
        out_priv[pid] = dict(symbol=s, ts=ts_dec, side=side, gross_R=r, mirror_gross_R=rm, sp=info["sp"])
    for k, b in enumerate(batches):
        (OUT / f"batch_{k}.txt").write_text("\n\n".join(b), encoding="utf-8")
    (PRIV / "outcomes.json").write_text(json.dumps(out_priv), encoding="utf-8")
    print("сетапов:", len(setups), "батчей:", N_BATCH, "размер batch_0:", (OUT / "batch_0.txt").stat().st_size)


if __name__ == "__main__":
    main()
