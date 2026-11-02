"""
wallets/base.py — Общий контракт для торговых адаптеров (CEX и будущих DEX).

Идея: executor.py/TradeExecutionEngine в перспективе может работать с
любой торговой площадкой через единый интерфейс, не меняя Risk Core.
Сегодня единственная боевая реализация — Bybit (см. executor.OrderExecutor,
который концептуально следует этому контракту, но с более богатым API,
учитывающим spot/linear).

Будущий Solana/Phantom-адаптер (wallets/phantom_stub.py) обязан
реализовать этот же интерфейс, прежде чем его можно будет подключить к
TradeExecutionEngine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class ExchangeAdapter(ABC):
    @abstractmethod
    def get_equity(self) -> float:
        """Текущий доступный капитал в USD/USDT-эквиваленте."""

    @abstractmethod
    def get_last_price(self, symbol: str) -> float:
        """Последняя цена инструмента."""

    @abstractmethod
    def place_market_order_with_stops(
        self,
        symbol: str,
        side: str,
        qty: str,
        stop_loss_price: float,
        take_profit_price: Optional[float] = None,
    ) -> dict[str, Any]:
        """Рыночный вход с обязательным защитным стопом."""
