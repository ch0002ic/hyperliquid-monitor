"""Hyperliquid live trading integration.

This module exposes a thin wrapper that bridges the existing AI-Trader
simulation stack with real Hyperliquid execution. It implements a
simple moving-average momentum strategy with basic risk controls so the
`backend/main.py` entry point can launch a continuous trading loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Callable, Dict, Optional, Sequence

import pandas as pd
from eth_account import Account

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.signing import OrderType


logger = logging.getLogger(__name__)

NotificationFn = Callable[[str], bool]


@dataclass(slots=True)
class StrategyConfig:
    short_window: int
    long_window: int
    long_threshold: float
    short_threshold: float
    neutral_band: float


@dataclass(slots=True)
class TraderConfig:
    private_key: str
    coins: Sequence[str]
    interval: str
    lookback: int
    poll_seconds: float
    sleep_between: float
    max_position_usd: float
    leverage: int
    min_trade_size: float
    slippage: float
    iterations: int
    base_url: Optional[str] = None
    account_address: Optional[str] = None
    vault_address: Optional[str] = None
    dry_run: bool = True
    strategy_config: StrategyConfig = field(
        default_factory=lambda: StrategyConfig(24, 96, 0.002, 0.002, 0.0)
    )
    notification_callback: Optional[NotificationFn] = None
    analytics_enabled: bool = False
    analytics_window: int = 120


class HyperliquidTrader:
    """Drive the Hyperliquid exchange based on simple technical signals."""

    def __init__(self, cfg: TraderConfig) -> None:
        self.cfg = cfg
        self.wallet = Account.from_key(cfg.private_key)
        self.info = Info(base_url=cfg.base_url, skip_ws=True)
        self.exchange = Exchange(
            wallet=self.wallet,
            base_url=cfg.base_url,
            account_address=cfg.account_address,
            vault_address=cfg.vault_address,
        )
        self.notification_callback = cfg.notification_callback
        self._last_signal: Dict[str, str] = {}

    def run(self) -> None:
        iteration = 0
        logger.info("Starting Hyperliquid trading loop (dry_run=%s)", self.cfg.dry_run)

        while self.cfg.iterations == 0 or iteration < self.cfg.iterations:
            iteration += 1
            logger.info("Trading iteration %s", iteration)
            for coin in self.cfg.coins:
                try:
                    self._process_coin(coin)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("Error processing %s: %s", coin, exc)
                time.sleep(self.cfg.sleep_between)
            logger.info("Sleeping for %.1fs before next iteration", self.cfg.poll_seconds)
            time.sleep(self.cfg.poll_seconds)

    def _process_coin(self, coin: str) -> None:
        logger.info("Evaluating coin %s", coin)
        candles = self._fetch_candles(coin)
        if candles is None:
            logger.warning("No candle data returned for %s", coin)
            return

        signal = self._generate_signal(coin, candles)
        positions = self.info.user_state(self._effective_address())
        current_position = self._extract_position(coin, positions)

        logger.info("Signal for %s: %s", coin, signal)
        logger.debug("Current position for %s: %s", coin, current_position)

        if self.cfg.dry_run:
            logger.info("Dry-run mode: skipping order execution for %s", coin)
            self._notify(f"[DRY RUN] Signal for {coin}: {signal}")
            return

        self._maybe_adjust_leverage(coin)

        if signal == "long":
            self._target_position(coin, is_long=True, current_position=current_position)
        elif signal == "short":
            self._target_position(coin, is_long=False, current_position=current_position)
        else:
            self._flatten_position(coin, current_position)

    def _effective_address(self) -> str:
        if self.cfg.account_address:
            return self.cfg.account_address
        if self.cfg.vault_address:
            return self.cfg.vault_address
        return self.wallet.address

    def _fetch_candles(self, coin: str) -> Optional[pd.DataFrame]:
        end = int(time.time() * 1000)
        start = end - self.cfg.lookback * self._interval_millis()
        payload = self.info.candles_snapshot(coin, self.cfg.interval, start, end)
        if not payload:
            return None
        frame = pd.DataFrame(payload)
        if frame.empty:
            return None
        frame = frame.rename(
            columns={"o": "open", "c": "close", "h": "high", "l": "low", "v": "volume", "T": "timestamp"}
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms")
        frame.sort_values("timestamp", inplace=True)
        return frame

    def _interval_millis(self) -> int:
        unit = self.cfg.interval[-1]
        value = int(self.cfg.interval[:-1])
        seconds_per_unit = {"m": 60, "h": 3600, "d": 86400}
        return value * seconds_per_unit.get(unit, 60) * 1000

    def _generate_signal(self, coin: str, candles: pd.DataFrame) -> str:
        closes = candles["close"].astype(float)
        long_window = self.cfg.strategy_config.long_window
        if len(closes) < long_window:
            logger.debug("Not enough data for MA calculation (need %d)", long_window)
            return "flat"

        short_ma = closes.rolling(self.cfg.strategy_config.short_window).mean().iloc[-1]
        long_ma = closes.rolling(long_window).mean().iloc[-1]
        momentum = (short_ma - long_ma) / long_ma if long_ma else 0.0

        logger.info(
            "short MA %.4f, long MA %.4f, momentum %.6f", short_ma, long_ma, momentum
        )
        self._log_analytics(coin, closes, momentum)

        thresholds = self.cfg.strategy_config
        prev_signal = self._last_signal.get(coin, "flat")

        if abs(momentum) <= thresholds.neutral_band:
            signal = "flat"
        elif momentum >= thresholds.long_threshold:
            signal = "long"
        elif momentum <= -thresholds.short_threshold:
            signal = "short"
        else:
            signal = prev_signal

        self._last_signal[coin] = signal
        return signal

    def _extract_position(self, coin: str, user_state: Dict) -> float:
        if not user_state:
            return 0.0
        for asset in user_state.get("assetPositions", []):
            position = asset.get("position") or {}
            if position.get("coin") == coin:
                try:
                    return float(position.get("szi", 0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _target_position(self, coin: str, *, is_long: bool, current_position: float) -> None:
        target_notional = self.cfg.max_position_usd
        mid_price = float(self.info.all_mids().get(coin, 0))
        if mid_price <= 0:
            logger.warning("No mid price for %s", coin)
            return

        target_size = target_notional / mid_price
        if not is_long:
            target_size = -target_size
        delta = target_size - current_position

        if abs(delta) < self.cfg.min_trade_size:
            logger.info(
                "Delta %.6f below min_trade_size %.6f for %s",
                delta,
                self.cfg.min_trade_size,
                coin,
            )
            return

        is_buy = delta > 0
        order_size = self._round_size(abs(delta))
        if order_size < self.cfg.min_trade_size:
            logger.info(
                "Rounded order size %.6f below min_trade_size %.6f for %s",
                order_size,
                self.cfg.min_trade_size,
                coin,
            )
            return
        logger.info("Placing order: coin=%s is_buy=%s size=%.6f", coin, is_buy, order_size)
        self._submit_order(coin, is_buy, order_size, reduce_only=False)
        self._notify(f"Placed order on {coin}: {'BUY' if is_buy else 'SELL'} {order_size:.4f}")

    def _flatten_position(self, coin: str, current_position: float) -> None:
        if abs(current_position) < self.cfg.min_trade_size:
            logger.info("Position already flat for %s (%.6f)", coin, current_position)
            return

        is_buy = current_position < 0
        size = self._round_size(abs(current_position))
        if size < self.cfg.min_trade_size:
            logger.info(
                "Rounded flat size %.6f below min_trade_size %.6f for %s",
                size,
                self.cfg.min_trade_size,
                coin,
            )
            return
        logger.info("Flattening %s position %.6f", coin, current_position)
        self._submit_order(coin, is_buy, size, reduce_only=True)
        self._notify(f"Flattened {coin} position of {current_position:.4f}")

    def _submit_order(self, coin: str, is_buy: bool, size: float, *, reduce_only: bool) -> None:
        order_type: OrderType = {"limit": {"tif": "Ioc"}}
        mids = self.info.all_mids()
        mid_px = float(mids.get(coin, 0))
        if mid_px <= 0:
            logger.warning("Cannot determine mid price for %s; skipping order", coin)
            return
        if self.cfg.slippage < 0:
            slippage = 0.0
        else:
            slippage = self.cfg.slippage
        if is_buy:
            limit_px = mid_px * (1 + slippage)
        else:
            limit_px = mid_px * max(0.0, 1 - slippage)
        limit_px = self._round_price(limit_px)
        response = self.exchange.order(
            name=coin,
            is_buy=is_buy,
            sz=size,
            limit_px=limit_px,
            order_type=order_type,
            reduce_only=reduce_only,
        )
        logger.info("Exchange response: %s", response)

    def _maybe_adjust_leverage(self, coin: str) -> None:
        try:
            self.exchange.update_leverage(self.cfg.leverage, coin)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to update leverage for %s: %s", coin, exc)

    def _notify(self, message: str) -> None:
        if not self.notification_callback:
            return
        try:
            self.notification_callback(message)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to send notification")

    def _round_size(self, size: float) -> float:
        if size <= 0:
            return 0.0
        step = self.cfg.min_trade_size if self.cfg.min_trade_size > 0 else 0.0001
        step_decimal = Decimal(str(step))
        if step_decimal <= 0:
            step_decimal = Decimal("0.0001")
        raw = Decimal(str(size)) / step_decimal
        snapped = raw.to_integral_value(rounding=ROUND_DOWN) * step_decimal
        snapped_float = float(snapped)
        logger.debug(
            "Rounded size from %.8f to %.8f (step %.8f)",
            size,
            snapped_float,
            float(step_decimal),
        )
        return snapped_float

    def _round_price(self, price: float) -> float:
        if price <= 0:
            return 0.0
        step_decimal = Decimal("0.1")
        raw = Decimal(str(price)) / step_decimal
        snapped = raw.to_integral_value(rounding=ROUND_DOWN) * step_decimal
        snapped_float = float(snapped)
        logger.debug(
            "Rounded price from %.4f to %.4f", price, snapped_float
        )
        return snapped_float

    def _log_analytics(self, coin: str, closes: pd.Series, momentum: float) -> None:
        if not self.cfg.analytics_enabled:
            return

        returns = closes.pct_change().dropna()
        if returns.empty:
            return

        window = min(self.cfg.analytics_window, len(returns))
        windowed = returns.tail(window)
        std = windowed.std()
        sharpe = 0.0
        if std:
            sharpe = windowed.mean() / std
            sharpe *= self._annualisation_factor() ** 0.5

        equity_curve = (1 + windowed).cumprod()
        drawdowns = (equity_curve / equity_curve.cummax()) - 1
        max_drawdown = drawdowns.min() if not drawdowns.empty else 0.0

        logger.info(
            "Analytics[%s] window=%d Sharpe=%.3f MaxDD=%.2f%% momentum=%.4f",
            coin,
            window,
            sharpe,
            max_drawdown * 100,
            momentum,
        )

    def _annualisation_factor(self) -> float:
        millis = self._interval_millis()
        if millis <= 0:
            return 1.0
        seconds_per_year = 365 * 24 * 3600
        return seconds_per_year / (millis / 1000.0)
