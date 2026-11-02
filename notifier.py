"""
notifier.py — Необязательные уведомления в Telegram о ключевых событиях:
Kill Switch, открытие/закрытие сделок, заблокированные опасные действия.

Принцип: notify() — best-effort и no-op по умолчанию. Если
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы в .env — вызовы просто
ничего не делают. Любая ошибка сети молча логируется и никогда не
пробрасывается наружу: уведомления не должны мочь сломать торговый цикл
или заблокировать Risk Gate.

Используется только стандартная библиотека (urllib) — без лишних
зависимостей ради одной функции отправки сообщения.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger("notifier")

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def is_configured() -> bool:
    return bool(_BOT_TOKEN and _CHAT_ID)


def notify(text: str) -> None:
    """Best-effort отправка сообщения в Telegram. Никогда не бросает исключений."""
    if not is_configured():
        return
    try:
        url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
        payload = json.dumps({"chat_id": _CHAT_ID, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # сеть/неверный токен и т.п. — не критично для торговли
        logger.warning("Не удалось отправить Telegram-уведомление: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if is_configured():
        notify("🔔 Тестовое уведомление от торгового бота ИИ-бот.")
        print("Отправлено (проверьте чат, если токен/chat_id верны).")
    else:
        print(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы в .env — "
            "notifier отключён, это нормальный режим работы."
        )
