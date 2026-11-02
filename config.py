"""
config.py — Единый источник правды по секретам доступа и Risk Core.

АРХИТЕКТУРНЫЙ ПРИНЦИП:
    Этот файл НЕ принимает решений и НЕ исполняет ордера. Он только:
      1) безопасно загружает ключи доступа к Bybit из переменных окружения;
      2) объявляет ЖЁСТКИЕ (immutable) лимиты риска, которые executor.py
         обязан проверять перед КАЖДЫМ ордером;
      3) описывает торгуемую вселенную инструментов (спот/фьючерсы,
         режимы скальпинг/дейтрейдинг/свинг) — то есть ЧТО вообще разрешено
         торговать, а не КАК принимать решения (это зона analysis.py).

    Ключевое правило безопасности: лимиты риска — это Python-константы в
    frozen dataclass. Их нельзя изменить из кода во время выполнения
    (попытка присвоения вызовет FrozenInstanceError). Если нужно изменить
    лимит — это осознанное редактирование исходного кода, а не действие ИИ
    или рантайм-логики.

    АБСОЛЮТНЫЙ ЗАПРЕТ: ALLOW_WITHDRAWALS всегда False и ЖЁСТКО захардкожен
    (не читается из .env/окружения). Ни один модуль в проекте не должен
    импортировать/вызывать withdraw-эндпоинты pybit. executor.py содержит
    дополнительный рантайм-guard, который блокирует любой вызов метода,
    содержащего "withdraw" / "transfer" в названии — см. FORBIDDEN_ACTIONS.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# Опциональная подгрузка .env (если установлен python-dotenv).
# Секреты никогда не хранятся в коде — только в переменных окружения / .env,
# который обязан быть в .gitignore.
# --------------------------------------------------------------------------
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except ImportError:
    pass


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "data" / "trading.db"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
DB_PATH.parent.mkdir(exist_ok=True)


# ==========================================================================
# 1. СЕКРЕТЫ ДОСТУПА К BYBIT
# ==========================================================================
@dataclass(frozen=True)
class BybitCredentials:
    """
    Ключи доступа. ТРЕБОВАНИЕ К API-КЛЮЧУ НА СТОРОНЕ BYBIT (обязательно!):
      - Разрешение: ТОЛЬКО "Contract/Spot Trade" (торговля).
      - Разрешение "Withdrawal" (Вывод средств) ДОЛЖНО БЫТЬ ВЫКЛЮЧЕНО
        физически в настройках API-ключа на Bybit. Это главный, настоящий
        рубеж защиты — код является лишь второй линией обороны.
      - Рекомендуется привязка ключа к белому списку IP-адресов сервера.
    """

    api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""))

    testnet: bool = field(
        default_factory=lambda: os.getenv("BYBIT_TESTNET", "true").strip().lower()
        in ("1", "true", "yes")
    )

    recv_window: int = 5000

    def validate(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("BYBIT_API_KEY")
        if not self.api_secret:
            missing.append("BYBIT_API_SECRET")
        if missing:
            sys.exit(
                f"[config] Отсутствуют обязательные переменные окружения: "
                f"{', '.join(missing)}. Создайте .env на основе .env.example."
            )


CREDENTIALS = BybitCredentials()


# ==========================================================================
# 1.1 DRY_RUN — глобальный предохранитель "бумажной торговли"
# ==========================================================================
# По умолчанию True: executor.OrderExecutor НЕ отправляет реальные ордера
# на биржу (ни на testnet, ни тем более на mainnet) — вместо этого он
# подменяет вызов place_order синтетическим успешным ответом и честно
# логирует пометку [DRY-RUN]. Все остальные вызовы (баланс, цены,
# позиции) выполняются по-настоящему, поэтому цикл можно безопасно
# прогонять днями/неделями на реальных живых данных и смотреть, что бы
# бот сделал, не рискуя ни центом.
#
# Переключить в боевой режим можно ТОЛЬКО осознанно: DRY_RUN=false в .env.
# Рекомендуемый путь: testnet + DRY_RUN=true -> testnet + DRY_RUN=false ->
# mainnet + DRY_RUN=true (долгое наблюдение) -> mainnet + DRY_RUN=false.
DRY_RUN: bool = os.getenv("DRY_RUN", "true").strip().lower() in ("1", "true", "yes")


# ==========================================================================
# 1.2 Выбор модели генерации сигналов (без внешнего LLM)
# ==========================================================================
class SignalEngineMode(str, Enum):
    # Ансамбль: тренд-модель (SMA/RSI) + объёмная модель (Volume Profile/
    # CVD, см. volume_strategy.py). Сигнал сильнее, если обе согласны;
    # слабее (с понижающим коэффициентом), если согласна только одна;
    # HOLD при прямом конфликте направлений. Рекомендуемый режим по умолчанию.
    ENSEMBLE = "ENSEMBLE"
    # Только объёмная модель (Volume Profile + всплеск объёма + CVD) —
    # стиль, ориентированный на объём, без учёта SMA/RSI тренда.
    VOLUME_ONLY = "VOLUME_ONLY"
    # Только старая тренд-модель (SMA-кроссовер + RSI-фильтр), без учёта объёма.
    TREND_ONLY = "TREND_ONLY"
    # Самая полная модель: направление берёт ансамбль (тренд+объём), а
    # итоговую уверенность — объяснимая многофакторная probability_model.py
    # (мультитаймфрейм профиль, CVD, sweep, структура, поток OI/funding,
    # магнит ликвидаций, новости). Рекомендуемый режим по умолчанию.
    PROBABILITY = "PROBABILITY"


SIGNAL_ENGINE_MODE: SignalEngineMode = SignalEngineMode(
    os.getenv("SIGNAL_ENGINE_MODE", "PROBABILITY").strip().upper()
)


# ==========================================================================
# 1.3 Режим позиционирования и торговое окно
# ==========================================================================
class PositionSizingMode(str, Enum):
    # Всегда ровно MAX_ORDER_USD (или меньше, если сигнал просит меньше).
    FIXED_USD = "FIXED_USD"
    # Объём считается от % риска баланса и дистанции до стопа (профессиональный
    # подход: чем шире стоп, тем меньше контрактов на тот же рублёвый риск).
    # ВАЖНО: даже в этом режиме RiskGate.validate() НИКОГДА не выпускает ордер
    # крупнее RISK.MAX_ORDER_USD — это неснимаемый потолок поверх любого расчёта.
    RISK_PERCENT = "RISK_PERCENT"


POSITION_SIZING_MODE: PositionSizingMode = PositionSizingMode(
    os.getenv("POSITION_SIZING_MODE", "FIXED_USD").strip().upper()
)
RISK_PERCENT_PER_TRADE: float = float(os.getenv("RISK_PERCENT_PER_TRADE", "1.0"))  # % баланса, если RISK_PERCENT


def _parse_optional_hour(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw.strip() == "":
        return None
    try:
        hour = int(raw)
    except ValueError:
        return None
    return hour if 0 <= hour <= 23 else None


# Торговое окно в часах UTC. Если оба заданы — сделки вне окна блокируются
# RiskGate (например, избегать тонкого ночного рынка). None/None = круглосуточно.
TRADING_START_HOUR_UTC: Optional[int] = _parse_optional_hour(os.getenv("TRADING_START_HOUR_UTC"))
TRADING_END_HOUR_UTC: Optional[int] = _parse_optional_hour(os.getenv("TRADING_END_HOUR_UTC"))

# Best-effort новостной сентимент (osint_lite.py, RSS + ключевые слова, без
# платных API). По умолчанию выключен, чтобы не делать лишних сетевых вызовов,
# если пользователь не планирует его использовать.
ENABLE_NEWS_SENTIMENT: bool = os.getenv("ENABLE_NEWS_SENTIMENT", "false").strip().lower() in ("1", "true", "yes")


# ==========================================================================
# 1.4 Автономный бэктест и самоограничение слабых стратегий
# ==========================================================================
# AUTO_BACKTEST_ENABLED: главный цикл (main.py) сам, без участия человека,
# раз в AUTO_BACKTEST_INTERVAL_HOURS часов прогоняет backtest.simulate() по
# ВСЕЙ config.SYMBOL_UNIVERSE в фоновом потоке (см. scheduler.py), пишет
# результат в БД и шлёт сводку в Telegram. Это чисто read-only анализ
# исторических данных — сам по себе он НИКАК не меняет поведение бота.
AUTO_BACKTEST_ENABLED: bool = os.getenv("AUTO_BACKTEST_ENABLED", "true").strip().lower() in ("1", "true", "yes")
AUTO_BACKTEST_INTERVAL_HOURS: float = float(os.getenv("AUTO_BACKTEST_INTERVAL_HOURS", "24"))

# AUTO_DISABLE_WEAK_STRATEGIES: ОТДЕЛЬНЫЙ осознанный opt-in переключатель.
# Если True — комбинации (symbol, asset_class, mode), чей последний бэктест
# набрал достаточно сделок (>= AUTO_DISABLE_MIN_TRADES) и показал win_rate
# ниже AUTO_DISABLE_MIN_WINRATE или отрицательный суммарный PnL, временно
# исключаются из торговли (main.py их пропускает) — до тех пор, пока
# следующий автоматический бэктест не покажет приемлемый результат.
# ЭТО НЕ МЕНЯЕТ Risk Core: MAX_ORDER_USD/HARD_STOP_LOSS_PCT/лимиты остаются
# теми же, меняется только САМ СПИСОК торгуемых комбинаций. По умолчанию
# выключено — бэктест по умолчанию только наблюдает и отчитывается.
AUTO_DISABLE_WEAK_STRATEGIES: bool = os.getenv("AUTO_DISABLE_WEAK_STRATEGIES", "false").strip().lower() in ("1", "true", "yes")
AUTO_DISABLE_MIN_TRADES: int = int(os.getenv("AUTO_DISABLE_MIN_TRADES", "15"))
AUTO_DISABLE_MIN_WINRATE: float = float(os.getenv("AUTO_DISABLE_MIN_WINRATE", "0.35"))


# ==========================================================================
# 2. Торгуемая вселенная: классы активов и режимы
# ==========================================================================
class AssetClass(str, Enum):
    SPOT = "spot"
    LINEAR = "linear"  # USDT-margined перпетуальные фьючерсы на Bybit


class StrategyMode(str, Enum):
    SCALPING = "SCALPING"   # секунды/минуты, очень узкие стопы
    DAYTRADE = "DAYTRADE"   # часы, внутри дня
    SWING = "SWING"         # дни/недели, среднесрок


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    asset_class: AssetClass
    modes: tuple[StrategyMode, ...]
    # Своё, более консервативное плечо на инструмент (для фьючерсов).
    # Для spot всегда игнорируется — там плеча нет (см. max_leverage_for).
    max_leverage: int = 3


# Единственное место, где перечислено "что вообще разрешено торговать".
# Добавление нового инструмента = осознанное редактирование этого списка,
# а не решение, принятое ИИ или рантайм-логикой.
SYMBOL_UNIVERSE: tuple[SymbolConfig, ...] = (
    SymbolConfig("BTCUSDT", AssetClass.LINEAR, (StrategyMode.SCALPING, StrategyMode.DAYTRADE, StrategyMode.SWING), max_leverage=3),
    SymbolConfig("ETHUSDT", AssetClass.LINEAR, (StrategyMode.SCALPING, StrategyMode.DAYTRADE, StrategyMode.SWING), max_leverage=3),
    # Spot — без плеча, подходит для дейтрейда/свинга, но не для
    # высокочастотного скальпинга (выше комиссии относительно движения цены).
    SymbolConfig("BTCUSDT", AssetClass.SPOT, (StrategyMode.DAYTRADE, StrategyMode.SWING)),
    SymbolConfig("ETHUSDT", AssetClass.SPOT, (StrategyMode.DAYTRADE, StrategyMode.SWING)),
)


def find_symbol_config(
    symbol: str, asset_class: AssetClass
) -> Optional[SymbolConfig]:
    for sc in SYMBOL_UNIVERSE:
        if sc.symbol == symbol.upper() and sc.asset_class == asset_class:
            return sc
    return None


def is_pair_allowed(symbol: str, asset_class: AssetClass, mode: StrategyMode) -> bool:
    sc = find_symbol_config(symbol, asset_class)
    return sc is not None and mode in sc.modes


# ==========================================================================
# 3. RISK CORE — неизменяемые жёсткие лимиты
# ==========================================================================
@dataclass(frozen=True)
class RiskLimits:
    """
    Жёсткие лимиты торгового риска. frozen=True => после создания объекта
    ни одно поле не может быть переопределено в рантайме.
    Все суммы — в USD (USDT), если не указано иное.
    """

    # --- Лимит на размер ОДНОГО ордера -----------------------------------
    MAX_ORDER_USD: float = 10.0

    # --- Дневной лимит просадки --------------------------------------------
    MAX_DAILY_DRAWDOWN_PCT: float = 5.0

    # --- Жёсткий стоп-лосс по каждой позиции -------------------------------
    # Действует одинаково и на споте, и на фьючерсах, и для любого режима
    # (скальпинг/дейтрейд/свинг). executor.py всегда берёт МИНИМУМ из
    # предложенного ИИ стопа и этого значения.
    HARD_STOP_LOSS_PCT: float = 2.0

    # --- Абсолютный запрет на вывод средств --------------------------------
    ALLOW_WITHDRAWALS: bool = False

    # --- Дополнительные защитные лимиты (defense in depth) -----------------
    MIN_CONFIDENCE: float = 0.65
    MAX_OPEN_POSITIONS: int = 1           # суммарно по ВСЕЙ вселенной инструментов/режимов
    MAX_LEVERAGE: int = 3                 # потолок плеча для фьючерсов (spot всегда 1x)
    MAX_TRADES_PER_DAY: int = 20
    ALLOWED_ORDER_TYPES: tuple[str, ...] = ("Market", "Limit")
    ALLOWED_ACTIONS: tuple[str, ...] = ("BUY", "SELL", "HOLD")

    FORBIDDEN_ACTION_SUBSTRINGS: tuple[str, ...] = (
        "withdraw",
        "transfer",
        "sub_member",
        "sub-member",
        "subaccount",
    )

    @property
    def ALLOWED_SYMBOLS(self) -> tuple[str, ...]:
        """Обратная совместимость: плоский список символов без учёта класса актива."""
        return tuple(sorted({sc.symbol for sc in SYMBOL_UNIVERSE}))

    @staticmethod
    def max_leverage_for(asset_class: AssetClass, symbol_cfg: Optional[SymbolConfig] = None) -> int:
        if asset_class == AssetClass.SPOT:
            return 1  # на споте плечо принципиально не используется — так безопаснее
        if symbol_cfg is not None:
            return min(symbol_cfg.max_leverage, RISK.MAX_LEVERAGE)
        return RISK.MAX_LEVERAGE


RISK = RiskLimits()


# ==========================================================================
# 4. (ОТКЛЮЧЕНО) Задел на будущее: Solana / Phantom / мем-коины
# ==========================================================================
# ЭТО НЕ РЕАЛИЗОВАНО и НАМЕРЕННО отключено по умолчанию. Торговля мем-
# коинами через DEX (Jupiter/Raydium на Solana) — принципиально другой
# класс риска по сравнению с CEX-деривативами Bybit:
#   - нет центрального ордербука и гарантированного исполнения стоп-лосса;
#   - огромный риск rug-pull / honeypot-токенов (нельзя продать купленное);
#   - sandwich-атаки и фронтраннинг в мемпуле;
#   - ончейн-транзакции необратимы — ошибка = мгновенная потеря средств.
#
# Приватные ключи Phantom-кошелька НИКОГДА не должны храниться в этом же
# .env / процессе, что и ключи Bybit — компрометация одного не должна
# давать доступ к другому. См. wallets/phantom_stub.py — интерфейс описан,
# но методы намеренно бросают NotImplementedError.
ENABLE_SOLANA_MODULE: bool = False


# ==========================================================================
# 5. Операционные пути (файлы состояния, kill switch, логи, БД)
# ==========================================================================
DAILY_STATE_FILE = STATE_DIR / "daily_risk_state.json"
KILL_SWITCH_FILE = STATE_DIR / "KILL_SWITCH"   # наличие файла = торговля запрещена
TRADE_LOG_FILE = LOG_DIR / "trading.log"


def assert_safe_to_start() -> None:
    CREDENTIALS.validate()
    if RISK.ALLOW_WITHDRAWALS is not False:
        sys.exit("[config] КРИТИЧЕСКАЯ ОШИБКА: ALLOW_WITHDRAWALS должен быть False.")
    if ENABLE_SOLANA_MODULE:
        sys.exit(
            "[config] Solana/Phantom модуль не реализован. "
            "ENABLE_SOLANA_MODULE должен оставаться False."
        )


if __name__ == "__main__":
    assert_safe_to_start()
    print("[config] Конфигурация валидна.")
    print(f"  DRY_RUN            = {DRY_RUN} ({'ордера НЕ отправляются' if DRY_RUN else 'БОЕВОЙ РЕЖИМ, ордера реальны'})")
    print(f"  SIGNAL_ENGINE_MODE = {SIGNAL_ENGINE_MODE.value}  (PROBABILITY = самая полная модель)")
    print(f"  POSITION_SIZING    = {POSITION_SIZING_MODE.value} (risk%={RISK_PERCENT_PER_TRADE})")
    print(f"  TRADING_WINDOW_UTC = {TRADING_START_HOUR_UTC} .. {TRADING_END_HOUR_UTC} (None=круглосуточно)")
    print(f"  NEWS_SENTIMENT     = {ENABLE_NEWS_SENTIMENT}")
    print(f"  AUTO_BACKTEST      = {AUTO_BACKTEST_ENABLED} (раз в {AUTO_BACKTEST_INTERVAL_HOURS} ч)")
    print(f"  AUTO_DISABLE_WEAK  = {AUTO_DISABLE_WEAK_STRATEGIES} (min_trades={AUTO_DISABLE_MIN_TRADES}, min_winrate={AUTO_DISABLE_MIN_WINRATE})")
    print(f"  testnet            = {CREDENTIALS.testnet}")
    print(f"  MAX_ORDER_USD      = {RISK.MAX_ORDER_USD}")
    print(f"  MAX_DAILY_DRAWDOWN = {RISK.MAX_DAILY_DRAWDOWN_PCT}%")
    print(f"  HARD_STOP_LOSS_PCT = {RISK.HARD_STOP_LOSS_PCT}%")
    print(f"  ALLOW_WITHDRAWALS  = {RISK.ALLOW_WITHDRAWALS}")
    print(f"  ENABLE_SOLANA      = {ENABLE_SOLANA_MODULE}")
    print("  Торгуемая вселенная:")
    for sc in SYMBOL_UNIVERSE:
        print(f"    {sc.symbol:10s} {sc.asset_class.value:7s} modes={[m.value for m in sc.modes]}")
