import argparse
from ast import literal_eval
from collections import deque
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from trader import HyperliquidTrader, TraderConfig, StrategyConfig

import requests
from hyperliquid_monitor.monitor import HyperliquidMonitor
from hyperliquid_monitor.types import Trade

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None


logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

RECENT_TRADES_LIMIT = 1000
_recent_trade_order = deque()
_recent_trade_keys = set()
_startup_timestamp = datetime.now(timezone.utc)

_raw_prefix = Path(__file__).stem.upper()
if _raw_prefix and _raw_prefix[0].isdigit():
    _raw_prefix = f"SCRIPT_{_raw_prefix}"
ENV_PREFIX = _raw_prefix.replace("-", "_")


def _get_env_var(name: str) -> str | None:
    prefixed = f"{ENV_PREFIX}_{name}"
    return os.getenv(prefixed) or os.getenv(name)


def _load_env_file(env_file: Path | None = None) -> None:
    env_path = env_file or (Path(__file__).resolve().parent / ".env")
    if load_dotenv is not None:
        load_dotenv(dotenv_path=str(env_path), override=False)
        return
    if not env_path.exists():
        return
    with env_path.open() as env_file_handle:
        for line in env_file_handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


def _parse_wallet_addresses(raw_value: str) -> list[str]:
    try:
        parsed = literal_eval(raw_value)
    except (ValueError, SyntaxError):
        parsed = [addr.strip() for addr in raw_value.split(",") if addr.strip()]
    if isinstance(parsed, (str, bytes)):
        parsed = [parsed]
    if not isinstance(parsed, (list, tuple, set)):
        parsed = [parsed]
    return [str(addr).strip() for addr in parsed if str(addr).strip()]


def _remember_trade(trade_key) -> bool:
    if trade_key in _recent_trade_keys:
        return False
    if len(_recent_trade_order) >= RECENT_TRADES_LIMIT:
        oldest = _recent_trade_order.popleft()
        _recent_trade_keys.discard(oldest)
    _recent_trade_order.append(trade_key)
    _recent_trade_keys.add(trade_key)
    return True


@dataclass
class RuntimeSettings:
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    wallet_addresses: tuple[str, ...]


TELEGRAM_BOT_TOKEN: str | None = None
TELEGRAM_CHAT_ID: str | None = None
WALLET_ADDRESSES: tuple[str, ...] = ()


def _initialise_runtime_settings(
    *,
    telegram_bot_token: str | None,
    telegram_chat_id: str | None,
    wallet_inputs: Iterable[str] | None,
    env_file: Path | None,
    require_telegram: bool = True,
    require_wallets: bool = True,
) -> RuntimeSettings:
    if env_file is not None:
        _load_env_file(env_file)
    else:
        _load_env_file()

    token = telegram_bot_token or _get_env_var("TELEGRAM_BOT_TOKEN")
    chat_id = telegram_chat_id or _get_env_var("TELEGRAM_CHAT_ID")

    wallets: list[str] = []
    if wallet_inputs:
        for item in wallet_inputs:
            wallets.extend(_parse_wallet_addresses(item))

    if not wallets:
        env_wallets = _get_env_var("WALLET_ADDRESSES")
        if env_wallets:
            wallets = _parse_wallet_addresses(env_wallets)

    unique_wallets: tuple[str, ...] = tuple(dict.fromkeys(wallets)) if wallets else ()

    if require_telegram and (not token or not chat_id):
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Provide them via environment or CLI."
        )
    if require_wallets and not unique_wallets:
        raise RuntimeError(
            "No wallet addresses supplied. Use WALLET_ADDRESSES env or --wallet-address option."
        )

    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WALLET_ADDRESSES

    TELEGRAM_BOT_TOKEN = token
    TELEGRAM_CHAT_ID = chat_id
    WALLET_ADDRESSES = unique_wallets

    if unique_wallets:
        logger.info(
            "Configured %d wallet address(es) for monitoring", len(WALLET_ADDRESSES)
        )
    elif require_wallets:
        logger.warning("Wallet list is empty despite requiring wallets")

    return RuntimeSettings(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        wallet_addresses=unique_wallets,
    )


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram credentials not initialised before sending message")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to post Telegram message: %s", exc, exc_info=True)
        return False
    return True


def trade_callback(trade: Trade) -> None:
    global _startup_timestamp
    trade_time = trade.timestamp
    if trade_time.tzinfo is None:
        trade_time = trade_time.replace(tzinfo=timezone.utc)
    if trade_time < _startup_timestamp:
        logger.debug("Ignoring historical trade at %s", trade_time)
        return

    trade_key = (trade.tx_hash, trade.size, trade.price)
    if not _remember_trade(trade_key):
        logger.debug("Skipping duplicate trade: %s", trade_key)
        return

    side_sent = trade.side
    if side_sent == "SELL":
        side_sent = "BUY"
    elif side_sent == "BUY":
        side_sent = "SELL"

    trade_time_utc8 = trade_time.astimezone(timezone(timedelta(hours=8)))
    trade_time_str = trade_time_utc8.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    message = (
        "New trade detected:\n"
        f"Address: {trade.address}\n"
        f"Coin: {trade.coin}\n"
        f"Side: {side_sent}\n"
        f"Size: {trade.size}\n"
        f"Price: {trade.price}\n"
        f"Type: {trade.trade_type}\n"
        f"Tx Hash: {trade.tx_hash}\n"
        f"Time: {trade_time_str}"
    )

    if not send_telegram_message(message):
        logger.warning("Telegram notification failed for trade %s", trade.tx_hash)


def run_trade_monitor() -> None:
    global _startup_timestamp
    _startup_timestamp = datetime.now(timezone.utc)

    if WALLET_ADDRESSES:
        try:
            import monitor_positions as mp_module  # pylint: disable=import-error

            if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != mp_module.TELEGRAM_BOT_TOKEN:
                mp_module.TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN
            if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID != mp_module.TELEGRAM_CHAT_ID:
                mp_module.TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID
            if WALLET_ADDRESSES and WALLET_ADDRESSES != mp_module.CONFIGURED_ADDRESSES:
                mp_module.CONFIGURED_ADDRESSES = WALLET_ADDRESSES

            mp_module.send_wallet_snapshot(WALLET_ADDRESSES)
        except ImportError as exc:  # pragma: no cover - optional dependency path
            logger.warning("Unable to import monitor_positions for snapshot: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Snapshot dispatch failed before trade monitor start: %s", exc)

    monitor = HyperliquidMonitor(
        addresses=WALLET_ADDRESSES,
        callback=trade_callback,
        db_path=None,
    )

    try:
        print("Starting trade monitor... Press Ctrl+C to exit")
        monitor.start()
    except KeyboardInterrupt:
        monitor.stop()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hyperliquid monitoring entry point")
    parser.add_argument(
        "--mode",
        choices=("trades", "positions", "live-trade"),
        default="trades",
        help="Select which monitor to run",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional path to .env file containing Telegram credentials and wallet list",
    )
    parser.add_argument(
        "--telegram-bot-token",
        dest="telegram_bot_token",
        help="Override Telegram bot token from environment",
    )
    parser.add_argument(
        "--telegram-chat-id",
        dest="telegram_chat_id",
        help="Override Telegram chat id from environment",
    )
    parser.add_argument(
        "--wallet-address",
        dest="wallet_addresses",
        action="append",
        help="Wallet address to monitor. Can be provided multiple times or as a Python/JSON list",
    )
    parser.add_argument(
        "--recent-trades-limit",
        type=int,
        default=RECENT_TRADES_LIMIT,
        help="Number of recent trades to keep for deduplication",
    )
    parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help="Skip Telegram configuration (useful for live trading without notifications)",
    )
    parser.add_argument(
        "--hl-private-key",
        dest="hl_private_key",
        help="Hex encoded private key for Hyperliquid trading",
    )
    parser.add_argument(
        "--hl-private-key-file",
        dest="hl_private_key_file",
        type=Path,
        help="Path to a file containing the Hyperliquid private key",
    )
    parser.add_argument(
        "--hl-account-address",
        dest="hl_account_address",
        help="Optional Hyperliquid account address (defaults to wallet address)",
    )
    parser.add_argument(
        "--hl-vault-address",
        dest="hl_vault_address",
        help="Optional vault/subaccount address for Hyperliquid",
    )
    parser.add_argument(
        "--hl-base-url",
        dest="hl_base_url",
        help="Hyperliquid API base URL (defaults to mainnet endpoint)",
    )
    parser.add_argument(
        "--hl-coins",
        dest="hl_coins",
        default="BTC",
        help="Comma separated list of Hyperliquid coins to trade",
    )
    parser.add_argument(
        "--hl-interval",
        dest="hl_interval",
        default="1h",
        help="Candlestick interval for strategy calculations (e.g. 1m, 1h, 4h)",
    )
    parser.add_argument(
        "--hl-lookback",
        dest="hl_lookback",
        type=int,
        default=240,
        help="Number of candles to request for strategy context",
    )
    parser.add_argument(
        "--hl-short-window",
        dest="hl_short_window",
        type=int,
        default=24,
        help="Short moving average window (in candles)",
    )
    parser.add_argument(
        "--hl-long-window",
        dest="hl_long_window",
        type=int,
        default=96,
        help="Long moving average window (in candles)",
    )
    parser.add_argument(
        "--hl-threshold",
        dest="hl_threshold",
        type=float,
        default=0.002,
        help="Relative threshold between short and long averages required to flip positions",
    )
    parser.add_argument(
        "--hl-threshold-long",
        dest="hl_threshold_long",
        type=float,
        help="Threshold for opening long positions (overrides --hl-threshold)",
    )
    parser.add_argument(
        "--hl-threshold-short",
        dest="hl_threshold_short",
        type=float,
        help="Threshold for opening short positions (overrides --hl-threshold)",
    )
    parser.add_argument(
        "--hl-flat-band",
        dest="hl_flat_band",
        type=float,
        default=0.0005,
        help="Momentum band treated as neutral (prevents constant positioning)",
    )
    parser.add_argument(
        "--hl-max-usd",
        dest="hl_max_usd",
        type=float,
        default=100.0,
        help="Maximum per-coin notional exposure in USD",
    )
    parser.add_argument(
        "--hl-leverage",
        dest="hl_leverage",
        type=int,
        default=2,
        help="Target leverage setting per coin",
    )
    parser.add_argument(
        "--hl-min-size",
        dest="hl_min_size",
        type=float,
        default=0.0005,
        help="Minimum order size in contract units",
    )
    parser.add_argument(
        "--hl-slippage",
        dest="hl_slippage",
        type=float,
        default=0.01,
        help="Fractional slippage buffer when placing IOC orders",
    )
    parser.add_argument(
        "--hl-poll-seconds",
        dest="hl_poll_seconds",
        type=float,
        default=300.0,
        help="Sleep interval (seconds) between full trading cycles",
    )
    parser.add_argument(
        "--hl-sleep-between",
        dest="hl_sleep_between",
        type=float,
        default=1.0,
        help="Sleep interval (seconds) between coin evaluations",
    )
    parser.add_argument(
        "--hl-iterations",
        dest="hl_iterations",
        type=int,
        default=0,
        help="Number of trading iterations to execute (0 = run indefinitely)",
    )
    parser.add_argument(
        "--hl-dry-run",
        dest="hl_dry_run",
        action="store_true",
        help="Simulate orders without sending them to the exchange",
    )
    parser.add_argument(
        "--hl-execute",
        dest="hl_execute",
        action="store_true",
        help="Send real orders to the exchange (overrides --hl-dry-run)",
    )
    parser.add_argument(
        "--hl-analytics",
        dest="hl_analytics",
        action="store_true",
        help="Log rolling Sharpe ratio and drawdown analytics per coin",
    )
    parser.add_argument(
        "--hl-analytics-window",
        dest="hl_analytics_window",
        type=int,
        default=120,
        help="Number of returns used when computing analytics",
    )
    return parser.parse_args()


def _resolve_private_key(args: argparse.Namespace) -> str:
    if args.hl_private_key_file:
        try:
            key = args.hl_private_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Failed to read private key file: {exc}") from exc
        if not key:
            raise RuntimeError("Private key file is empty")
        return key

    if args.hl_private_key:
        return args.hl_private_key.strip()

    env_key = _get_env_var("HYPERLIQUID_PRIVATE_KEY") or os.getenv("HYPERLIQUID_PRIVATE_KEY")
    if env_key:
        return env_key.strip()

    raise RuntimeError(
        "Hyperliquid private key not provided. Use --hl-private-key, --hl-private-key-file, or set HYPERLIQUID_PRIVATE_KEY."
    )


def _parse_coin_list(raw: str) -> tuple[str, ...]:
    coins = tuple(symbol.strip().upper() for symbol in raw.split(",") if symbol.strip())
    if not coins:
        raise RuntimeError("At least one coin must be provided via --hl-coins")
    return coins


def _run_live_trading(args: argparse.Namespace, runtime_settings: RuntimeSettings) -> None:
    private_key = _resolve_private_key(args)
    coins = _parse_coin_list(args.hl_coins)

    short_window = args.hl_short_window
    long_window = args.hl_long_window
    if long_window <= short_window:
        raise RuntimeError("--hl-long-window must be greater than --hl-short-window")

    lookback = max(args.hl_lookback, long_window + 10)

    base_threshold = max(args.hl_threshold, 0.0)
    long_threshold = (
        args.hl_threshold_long if args.hl_threshold_long is not None else base_threshold
    )
    short_threshold = (
        args.hl_threshold_short if args.hl_threshold_short is not None else base_threshold
    )
    if long_threshold <= 0 or short_threshold <= 0:
        raise RuntimeError("Threshold values must be positive")

    neutral_band = max(args.hl_flat_band, 0.0)

    base_url = (
        args.hl_base_url
        or _get_env_var("HYPERLIQUID_BASE_URL")
        or os.getenv("HYPERLIQUID_BASE_URL")
    )
    account_address = (
        args.hl_account_address
        or _get_env_var("HYPERLIQUID_ACCOUNT_ADDRESS")
        or os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")
    )
    vault_address = (
        args.hl_vault_address
        or _get_env_var("HYPERLIQUID_VAULT_ADDRESS")
        or os.getenv("HYPERLIQUID_VAULT_ADDRESS")
    )

    dry_run = True
    if args.hl_execute:
        dry_run = False
    elif args.hl_dry_run:
        dry_run = True

    notify_function = None
    if runtime_settings.telegram_bot_token and runtime_settings.telegram_chat_id and not args.skip_telegram:
        notify_function = send_telegram_message

    strategy_cfg = StrategyConfig(
        short_window=short_window,
        long_window=long_window,
        long_threshold=long_threshold,
        short_threshold=short_threshold,
        neutral_band=neutral_band,
    )

    trader_cfg = TraderConfig(
        private_key=private_key,
        coins=coins,
        interval=args.hl_interval,
        lookback=lookback,
        poll_seconds=args.hl_poll_seconds,
        sleep_between=args.hl_sleep_between,
        max_position_usd=args.hl_max_usd,
        leverage=args.hl_leverage,
        min_trade_size=args.hl_min_size,
        slippage=args.hl_slippage,
        iterations=args.hl_iterations,
        base_url=base_url,
        account_address=account_address,
        vault_address=vault_address,
        dry_run=dry_run,
        strategy_config=strategy_cfg,
        notification_callback=notify_function,
        analytics_enabled=args.hl_analytics,
        analytics_window=max(1, args.hl_analytics_window),
    )

    trader = HyperliquidTrader(trader_cfg)
    try:
        trader.run()
    except KeyboardInterrupt:
        logger.info("Live trading interrupted by user")


def main() -> None:
    args = _parse_args()
    global RECENT_TRADES_LIMIT
    RECENT_TRADES_LIMIT = max(1, args.recent_trades_limit)

    require_telegram = not args.skip_telegram or args.mode in {"trades", "positions"}
    require_wallets = args.mode in {"trades", "positions"}

    runtime_settings = _initialise_runtime_settings(
        telegram_bot_token=args.telegram_bot_token,
        telegram_chat_id=args.telegram_chat_id,
        wallet_inputs=args.wallet_addresses,
        env_file=args.env_file,
        require_telegram=require_telegram,
        require_wallets=require_wallets,
    )

    if args.mode == "positions":
        from monitor_positions import main as run_positions  # pylint: disable=import-error

        run_positions()
        return

    if args.mode == "live-trade":
        _run_live_trading(args, runtime_settings)
        return

    run_trade_monitor()


if __name__ == "__main__":
    main()
