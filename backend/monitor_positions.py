from ast import literal_eval
import hashlib
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests
import schedule
from hyperliquid.info import Info
from hyperliquid.utils.error import ClientError, ServerError

try:
    from backend.state_store import (
        load_state_snapshot,
        refresh_state_store_configuration,
        register_state_store_alert_handler,
        save_state_snapshot,
    )
except ImportError:  # pragma: no cover - fallback when executed as a script
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from backend.state_store import (
        load_state_snapshot,
        refresh_state_store_configuration,
        register_state_store_alert_handler,
        save_state_snapshot,
    )

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None


logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

MESSAGE_DELAY_SECONDS = 0.75
MAX_RETRIES = 3
RETRY_DELAY = 2
API_TIMEOUT = 30
STATE_POSITIONS_KEY = "positions"
STATE_META_KEY = "meta"
STATE_META_COINS_KEY = "coins"
_STATE_LOCK = threading.Lock()

_snapshot_initialized = False

_websocket_running = False
_stop_event = threading.Event()
info_client: Info = Info()

SIZE_EPSILON = 1e-9


_raw_prefix = Path(__file__).stem.upper()
if _raw_prefix and _raw_prefix[0].isdigit():
    _raw_prefix = f"SCRIPT_{_raw_prefix}"
ENV_PREFIX = _raw_prefix.replace("-", "_")


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if load_dotenv is not None:
        load_dotenv(dotenv_path=str(env_path), override=False)
        return
    if not env_path.exists():
        return
    with env_path.open() as env_file:
        for line in env_file:
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


def _get_env_var(name: str) -> Optional[str]:
    prefixed = f"{ENV_PREFIX}_{name}"
    return os.getenv(prefixed) or os.getenv(name)


def _parse_wallet_addresses(raw_value: str) -> List[str]:
    try:
        parsed = literal_eval(raw_value)
    except (ValueError, SyntaxError):
        parsed = [addr.strip() for addr in raw_value.split(",") if addr.strip()]
    if isinstance(parsed, (str, bytes)):
        parsed = [parsed]
    if not isinstance(parsed, (list, tuple, set)):
        parsed = [parsed]
    return [str(addr).strip() for addr in parsed if str(addr).strip()]


_load_env_file()
refresh_state_store_configuration()

TELEGRAM_BOT_TOKEN = _get_env_var("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get_env_var("TELEGRAM_CHAT_ID")
CONFIGURED_ADDRESSES: Tuple[str, ...] = tuple(
    _parse_wallet_addresses(_get_env_var("WALLET_ADDRESSES") or "[]")
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str) and not value.strip():
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str) and not value.strip():
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


LOCAL_TIME_OFFSET = timezone(timedelta(hours=8))


def _format_timestamp(timestamp_ms: Optional[int]) -> str:
    if not timestamp_ms:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        local_dt = dt.astimezone(LOCAL_TIME_OFFSET)
        formatted = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        return f"{formatted} UTC+8"
    except (OSError, OverflowError, ValueError):
        return "N/A"


def _format_leverage(leverage: Optional[float]) -> str:
    if leverage is None or leverage <= 0:
        return "N/A"
    return f"{leverage:.2f}x"


def _extract_tx_hash(fill: Optional[Dict[str, Any]]) -> str:
    if not fill or not isinstance(fill, dict):
        return "N/A"
    for key in ("hash", "txHash", "orderHash", "transactionHash"):
        value = fill.get(key)
        if value:
            return str(value)
    return "N/A"


def _split_state_entry(entry: Any) -> Tuple[Dict[str, Dict], Dict[str, Any]]:
    if not isinstance(entry, dict):
        return {}, {}
    if STATE_POSITIONS_KEY in entry or STATE_META_KEY in entry:
        positions = entry.get(STATE_POSITIONS_KEY, {}) or {}
        meta = entry.get(STATE_META_KEY, {}) or {}
    else:
        positions = entry
        meta = {}
    return positions, meta


def _normalize_meta(raw_meta: Dict[str, Any]) -> Dict[str, Any]:
    coins_meta_raw = raw_meta.get(STATE_META_COINS_KEY, {}) if isinstance(raw_meta, dict) else {}
    coins_meta: Dict[str, Dict[str, Any]] = {}
    if isinstance(coins_meta_raw, dict):
        for coin, data in coins_meta_raw.items():
            if not isinstance(data, dict):
                continue
            coins_meta[coin] = {
                "last_open_id": data.get("last_open_id"),
                "last_close_id": data.get("last_close_id"),
                "last_reduce_id": data.get("last_reduce_id"),
            }

    return {
        "empty_notified": bool(raw_meta.get("empty_notified", False)) if isinstance(raw_meta, dict) else False,
        STATE_META_COINS_KEY: coins_meta,
    }


def _compose_state_entry(positions: Dict[str, Dict], meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        STATE_POSITIONS_KEY: positions,
        STATE_META_KEY: meta,
    }


def _make_event_id(event_type: str, coin: str, fill: Optional[Dict[str, Any]], position: Optional[Dict[str, Any]]) -> str:
    timestamp = _safe_int(fill.get("time")) if fill else 0
    tx_hash = _extract_tx_hash(fill)
    size = position.get("szi") if isinstance(position, dict) else None
    entry_px = position.get("entryPx") if isinstance(position, dict) else None
    fill_size = fill.get("sz") if isinstance(fill, dict) else None
    return f"{event_type}:{coin}:{tx_hash}:{timestamp}:{size}:{entry_px}:{fill_size}"


def _apply_fill_to_position(start_position: float, size: float, side: str) -> float:
    if side == "B":
        return start_position + size
    if side == "A":
        return start_position - size
    return start_position


def _find_relevant_fill(coin: str, fills: Sequence[Dict[str, Any]], *, event_type: str) -> Optional[Dict[str, Any]]:
    tolerance = 1e-9
    sorted_fills = sorted(fills, key=lambda item: _safe_int(item.get("time")), reverse=True)
    for fill in sorted_fills:
        if fill.get("coin") != coin:
            continue
        start_position = _safe_float(fill.get("startPosition"))
        size = _safe_float(fill.get("sz"))
        side = str(fill.get("side", ""))
        end_position = _apply_fill_to_position(start_position, size, side)
        if event_type == "open" and abs(start_position) <= tolerance and abs(end_position) > tolerance:
            return fill
        if event_type == "close" and abs(start_position) > tolerance and abs(end_position) <= tolerance:
            return fill
        if (
            event_type == "reduce"
            and abs(start_position) > tolerance
            and abs(end_position) > tolerance
            and abs(end_position) < abs(start_position) - tolerance
        ):
            return fill
    for fill in sorted_fills:
        if fill.get("coin") == coin:
            return fill
    return None


def _calculate_order_average_price(
    coin: str,
    reference_fill: Optional[Dict[str, Any]],
    fills: Sequence[Dict[str, Any]],
) -> float:
    if not reference_fill or not isinstance(reference_fill, dict):
        return 0.0

    target_hash = _extract_tx_hash(reference_fill)
    target_time = _safe_int(reference_fill.get("time"))

    relevant: List[Dict[str, Any]] = []
    for fill in sorted(fills, key=lambda item: _safe_int(item.get("time"))):
        if fill.get("coin") != coin:
            continue
        if target_hash and target_hash != "N/A":
            if _extract_tx_hash(fill) == target_hash:
                relevant.append(fill)
        elif _safe_int(fill.get("time")) == target_time:
            relevant.append(fill)

    if not relevant:
        relevant = [reference_fill]

    total_value = 0.0
    total_size = 0.0
    for fill in relevant:
        price = _safe_float(fill.get("px"))
        size = abs(_safe_float(fill.get("sz")))
        if price <= 0 or size <= 0:
            continue
        total_value += price * size
        total_size += size

    if total_size > SIZE_EPSILON:
        return total_value / total_size
    return _safe_float(reference_fill.get("px"))


def _compute_full_close_average_price(
    coin: str,
    fills: Sequence[Dict[str, Any]],
    previous_position: Dict[str, Any],
) -> float:
    target_size = abs(_safe_float(previous_position.get("szi")))
    if target_size <= SIZE_EPSILON:
        return 0.0

    direction = 1 if _safe_float(previous_position.get("szi")) > 0 else -1
    remaining = target_size
    total_value = 0.0
    total_size = 0.0

    for fill in sorted(fills, key=lambda item: _safe_int(item.get("time")), reverse=True):
        if fill.get("coin") != coin:
            continue
        price = _safe_float(fill.get("px"))
        size = _safe_float(fill.get("sz"))
        if price <= 0 or size <= 0:
            continue
        side = str(fill.get("side", ""))
        start_position = _safe_float(fill.get("startPosition"))
        end_position = _apply_fill_to_position(start_position, size, side)

        reduces = False
        if direction > 0:
            reduces = end_position < start_position - SIZE_EPSILON
        elif direction < 0:
            reduces = end_position > start_position + SIZE_EPSILON

        if not reduces:
            continue

        contribution = min(size, remaining)
        total_value += price * contribution
        total_size += contribution
        remaining -= contribution
        if remaining <= SIZE_EPSILON:
            break

    if total_size > SIZE_EPSILON:
        return total_value / total_size
    return 0.0


    return None


def _calculate_leverage(position: Dict[str, Any]) -> Optional[float]:
    leverage_info = position.get("leverage") if isinstance(position, dict) else None
    if isinstance(leverage_info, dict):
        value = leverage_info.get("value")
        if value is not None:
            return _safe_float(value) or None
    elif leverage_info is not None:
        lv = _safe_float(leverage_info)
        if lv > 0:
            return lv

    position_value = _safe_float(position.get("positionValue")) if isinstance(position, dict) else 0.0
    margin_used = _safe_float(position.get("marginUsed")) if isinstance(position, dict) else 0.0
    if margin_used > 0:
        return position_value / margin_used
    return None


def _build_trade_details(position: Dict[str, Any], fill: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    price = _safe_float(fill.get("px")) if fill else 0.0
    size = _safe_float(fill.get("sz")) if fill else 0.0
    timestamp_ms = _safe_int(fill.get("time")) if fill else 0
    tx_hash = _extract_tx_hash(fill)
    leverage_value = _calculate_leverage(position)
    position_value = abs(_safe_float(position.get("positionValue")))
    position_size = abs(_safe_float(position.get("szi")))
    if price <= 0 and isinstance(position, dict) and "entryPx" in position:
        price = _safe_float(position.get("entryPx"))
    start_position = _safe_float(fill.get("startPosition")) if fill else 0.0
    side = fill.get("side") if fill else None

    return {
        "price": price,
        "size": size,
        "timestamp_ms": timestamp_ms,
        "tx_hash": tx_hash,
        "leverage": leverage_value,
        "position_value": position_value,
        "position_size": position_size,
        "start_position": start_position,
        "side": side,
    }
def _calculate_entry_price(position: Dict[str, Any]) -> float:
    entry_px = _safe_float(position.get("entryPx"))
    if entry_px > 0:
        return entry_px
    position_size = abs(_safe_float(position.get("szi")))
    if position_size <= 0:
        return 0.0
    position_value = abs(_safe_float(position.get("positionValue")))
    if position_value > 0:
        derived_price = position_value / position_size
        if derived_price > 0:
            return derived_price
    return 0.0


def _extract_account_value(user_state: Dict[str, Any]) -> float:
    margin_summary = user_state.get("marginSummary")
    if isinstance(margin_summary, dict):
        account_value = _safe_float(margin_summary.get("accountValue"))
        if account_value:
            return account_value
    withdrawable = _safe_float(user_state.get("withdrawable"))
    if withdrawable:
        return withdrawable
    if isinstance(margin_summary, dict):
        return _safe_float(margin_summary.get("totalRawUsd"))
    return 0.0

def retry_api_call(func, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except (ClientError, ServerError) as exc:
            if attempt == MAX_RETRIES - 1:
                logger.error("API call failed after %s attempts: %s", MAX_RETRIES, exc)
                raise
            wait_time = RETRY_DELAY * (2 ** attempt)
            logger.warning(
                "API call failed (attempt %s/%s), retrying in %ss: %s",
                attempt + 1,
                MAX_RETRIES,
                wait_time,
                exc,
            )
            time.sleep(wait_time)
        except Exception as exc:  # pragma: no cover - unexpected path
            logger.error("Unexpected error in API call: %s", exc)
            raise


def get_positions(address: str) -> Dict:
    try:
        user_state = retry_api_call(info_client.user_state, address)
    except Exception as exc:
        logger.error("Error fetching positions for %s: %s", address, exc)
        raise
    if not user_state:
        logger.warning("Empty response for positions from %s", address)
        return {}
    return user_state


def get_trade_history(address: str) -> List[Dict]:
    try:
        fills = retry_api_call(info_client.user_fills, address)
    except Exception as exc:
        logger.error("Error fetching trade history for %s: %s", address, exc)
        return []
    return fills if fills else []


def get_current_prices() -> Dict[str, float]:
    try:
        mids = retry_api_call(info_client.all_mids)
    except Exception as exc:
        logger.error("Error fetching current prices: %s", exc)
        raise
    if not mids:
        logger.error("Empty response for current prices")
        return {}
    return {coin: float(price) for coin, price in mids.items()}


def calculate_position_metrics(coin: str, fills: Sequence[Dict]) -> Dict[str, float]:
    if not fills:
        return {
            "total_buy_usd": 0.0,
            "total_sell_usd": 0.0,
            "avg_entry_price": 0.0,
            "avg_exit_price": 0.0,
        }

    coin_fills = [f for f in fills if f.get("coin") == coin]
    if not coin_fills:
        return {
            "total_buy_usd": 0.0,
            "total_sell_usd": 0.0,
            "avg_entry_price": 0.0,
            "avg_exit_price": 0.0,
        }

    try:
        coin_fills.sort(key=lambda item: int(item.get("time", 0)))
    except (ValueError, TypeError) as exc:
        logger.warning("Error sorting fills for %s: %s", coin, exc)

    total_buy_usd = 0.0
    total_sell_usd = 0.0
    entry_prices: List[float] = []
    exit_prices: List[float] = []

    for fill in coin_fills:
        try:
            price = float(fill.get("px", 0) or 0)
            size = float(fill.get("sz", 0) or 0)
            side = fill.get("side", "")
            start_position = float(fill.get("startPosition", 0) or 0)
        except (TypeError, ValueError) as exc:
            logger.warning("Error processing fill for %s: %s", coin, exc)
            continue

        if price <= 0 or size <= 0:
            continue

        trade_value = price * size
        if side == "B":
            if start_position < 0:
                total_sell_usd += trade_value
                exit_prices.append(price)
            else:
                total_buy_usd += trade_value
                entry_prices.append(price)
        elif side == "A":
            if start_position > 0:
                total_sell_usd += trade_value
                exit_prices.append(price)
            else:
                total_buy_usd += trade_value
                entry_prices.append(price)

    avg_entry_price = sum(entry_prices) / len(entry_prices) if entry_prices else 0.0
    avg_exit_price = sum(exit_prices) / len(exit_prices) if exit_prices else 0.0

    return {
        "total_buy_usd": total_buy_usd,
        "total_sell_usd": total_sell_usd,
        "avg_entry_price": avg_entry_price,
        "avg_exit_price": avg_exit_price,
    }


def format_number(value: float, decimals: int = 8) -> str:
    if value == 0:
        return "0"
    formatted = f"{value:,.{decimals}f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def format_position_message(
    address: str,
    position: Dict,
    metrics: Dict[str, float],
    current_price: float,
    balance: float,
) -> str:
    coin = position.get("coin", "")
    entry_price = _calculate_entry_price(position)
    position_value = abs(_safe_float(position.get("positionValue")))
    unrealized_pnl = float(position.get("unrealizedPnl", 0) or 0)
    szi = float(position.get("szi", 0) or 0)

    is_long = szi > 0
    position_side = "多" if is_long else "空"

    pnl_percentage = 0.0
    if entry_price > 0 and current_price > 0:
        if is_long:
            pnl_percentage = ((current_price - entry_price) / entry_price) * 100
        else:
            pnl_percentage = ((entry_price - current_price) / entry_price) * 100

    avg_entry = entry_price if entry_price > 0 else metrics.get("avg_entry_price", 0.0)

    pnl_str = format_number(unrealized_pnl, 2)
    pnl_percent_str = format_number(abs(pnl_percentage), 2)
    pnl_sign = "" if unrealized_pnl >= 0 else "-"
    pnl_color = "🟢" if unrealized_pnl >= 0 else "🔴"

    position_value_display = format_number(position_value, 2)
    balance_display = format_number(balance, 2)
    avg_entry_display = format_number(avg_entry) if avg_entry > 0 else "N/A"
    current_price_display = format_number(current_price)

    return f"""💳 <b>钱包地址</b>
<code>{address}</code>

━━━━━━━━━━━━━━━━━━━━
<b>{coin}/USDC</b> (全仓-{position_side}) {pnl_color}
━━━━━━━━━━━━━━━━━━━━

📈 <b>盈亏</b>
<code>{pnl_sign}{pnl_str}</code> ({pnl_sign}{pnl_percent_str}%)

💳 <b>关仓价值</b>
<code>${position_value_display}</code>

⚖️ <b>开仓均价</b>
<code>{avg_entry_display}</code>

💵 <b>当前价格</b>
<code>{current_price_display}</code>

💰 <b>余额</b>
<code>${balance_display}</code>"""


def save_position_state(state: Dict[str, Dict[str, Any]]) -> None:
    try:
        save_state_snapshot(state)
    except Exception as exc:  # pragma: no cover - unexpected persistence failure
        logger.error("Error saving position state: %s", exc)


def load_position_state() -> Dict[str, Dict[str, Any]]:
    try:
        raw_state = load_state_snapshot()
    except Exception as exc:  # pragma: no cover - unexpected persistence failure
        logger.warning("Error loading position state: %s", exc)
        return {}

    if not isinstance(raw_state, dict):
        return {}

    normalized: Dict[str, Dict[str, Any]] = {}
    for address, entry in raw_state.items():
        positions, meta = _split_state_entry(entry)
        normalized[address] = _compose_state_entry(positions, _normalize_meta(meta))
    return normalized


def format_order_placed_message(
    address: str,
    position: Dict,
    trade_details: Dict[str, Any],
    balance: float,
    *,
    current_price: float,
) -> str:
    coin = position.get("coin", "")
    szi = _safe_float(position.get("szi"))
    is_long = szi > 0
    position_side = "多" if is_long else "空"

    position_value = trade_details.get("position_value") or position.get("positionValue")
    position_value = abs(_safe_float(position_value))
    position_size = trade_details.get("position_size") or szi
    position_size = abs(_safe_float(position_size))
    entry_price = _safe_float(trade_details.get("price"))
    if entry_price <= 0:
        entry_price = _safe_float(position.get("entryPx"))
    if entry_price <= 0 and position_size > 0:
        derived_price = position_value / position_size if position_size else 0.0
        if derived_price > 0:
            entry_price = derived_price

    current_price_display = format_number(current_price) if current_price > 0 else "N/A"
    entry_price_display = format_number(entry_price) if entry_price > 0 else "N/A"
    position_value_display = format_number(position_value, 2)
    balance_display = format_number(balance, 2)
    leverage_display = _format_leverage(trade_details.get("leverage"))
    trade_time_display = _format_timestamp(trade_details.get("timestamp_ms"))
    tx_hash_display = trade_details.get("tx_hash") or "N/A"
    fill_price = _safe_float(trade_details.get("price")) or entry_price
    fill_price_display = format_number(fill_price) if fill_price else entry_price_display
    size_display = format_number(position_size) if position_size else "N/A"

    return f"""✅ <b>订单已开仓</b>

💳 <b>钱包地址</b>
<code>{address}</code>

━━━━━━━━━━━━━━━━━━━━
<b>{coin}/USDC</b> (全仓-{position_side})
━━━━━━━━━━━━━━━━━━━━

⚙️ <b>杠杆</b>
<code>{leverage_display}</code>

🕒 <b>开仓时间</b>
<code>{trade_time_display}</code>

🔗 <b>交易哈希</b>
<code>{tx_hash_display}</code>

📦 <b>持仓数量</b>
<code>{size_display}</code>

💳 <b>持仓价值</b>
<code>${position_value_display}</code>

⚖️ <b>开仓均价</b>
<code>{fill_price_display}</code>

💵 <b>当前价格</b>
<code>{current_price_display}</code>

💰 <b>余额</b>
<code>${balance_display}</code>"""


def format_order_closed_message(
    address: str,
    coin: str,
    previous_position: Dict,
    trade_details: Dict[str, Any],
    balance: float,
    current_price: float,
) -> str:
    entry_price = _calculate_entry_price(previous_position)
    position_value = abs(_safe_float(previous_position.get("positionValue")))
    szi = _safe_float(previous_position.get("szi"))
    is_long = szi > 0
    position_side = "多" if is_long else "空"

    close_price = _safe_float(trade_details.get("price"))
    size = abs(szi)
    if close_price <= 0 and size > 0 and entry_price > 0:
        fallback_pnl = _safe_float(previous_position.get("unrealizedPnl"))
        adjustment = fallback_pnl / size if size else 0.0
        candidate = entry_price + adjustment if is_long else entry_price - adjustment
        if candidate > 0:
            close_price = candidate
    if close_price <= 0:
        close_price = current_price
    close_price = _safe_float(close_price)

    realized_pnl = 0.0
    if entry_price > 0 and close_price > 0 and size > 0:
        if is_long:
            realized_pnl = (close_price - entry_price) * size
        else:
            realized_pnl = (entry_price - close_price) * size
    else:
        realized_pnl = _safe_float(previous_position.get("unrealizedPnl"))

    pnl_percentage = 0.0
    if entry_price > 0 and size > 0:
        pnl_percentage = (realized_pnl / (entry_price * size)) * 100

    pnl_sign = "" if realized_pnl >= 0 else "-"
    pnl_color = "🟢" if realized_pnl >= 0 else "🔴"
    pnl_str = format_number(abs(realized_pnl), 2)
    pnl_percent_str = format_number(abs(pnl_percentage), 2)

    leverage_display = _format_leverage(trade_details.get("leverage") or _calculate_leverage(previous_position))
    close_time_display = _format_timestamp(trade_details.get("timestamp_ms"))
    tx_hash_display = trade_details.get("tx_hash") or "N/A"
    entry_price_display = format_number(entry_price) if entry_price > 0 else "N/A"
    close_price_display = format_number(close_price) if close_price > 0 else "N/A"
    current_price_display = format_number(current_price) if current_price > 0 else "N/A"
    position_value_display = format_number(position_value, 2)
    balance_display = format_number(balance, 2)

    return f"""❌ <b>订单已平仓</b> {pnl_color}

💳 <b>钱包地址</b>
<code>{address}</code>

━━━━━━━━━━━━━━━━━━━━
<b>{coin}/USDC</b> (全仓-{position_side})
━━━━━━━━━━━━━━━━━━━━

⚙️ <b>杠杆</b>
<code>{leverage_display}</code>

🕒 <b>平仓时间</b>
<code>{close_time_display}</code>

🔗 <b>交易哈希</b>
<code>{tx_hash_display}</code>

📈 <b>盈亏</b>
<code>{pnl_sign}{pnl_str}</code> ({pnl_sign}{pnl_percent_str}%)

💳 <b>持仓价值</b>
<code>${position_value_display}</code>

⚖️ <b>开仓均价</b>
<code>{entry_price_display}</code>

⚖️ <b>平仓均价</b>
<code>{close_price_display}</code>

💵 <b>当前价格</b>
<code>{current_price_display}</code>

💰 <b>余额</b>
<code>${balance_display}</code>"""


def format_order_reduced_message(
    address: str,
    coin: str,
    previous_position: Dict[str, Any],
    current_position: Dict[str, Any],
    trade_details: Dict[str, Any],
    balance: float,
    current_price: float,
) -> str:
    prev_size = _safe_float(previous_position.get("szi"))
    curr_size = _safe_float(current_position.get("szi"))
    is_long = prev_size > 0
    position_side = "多" if is_long else "空"

    closed_size = abs(prev_size) - abs(curr_size)
    if closed_size <= SIZE_EPSILON:
        closed_size = abs(_safe_float(trade_details.get("size")))
    remaining_size = abs(curr_size)

    entry_price = _calculate_entry_price(previous_position)
    close_price = _safe_float(trade_details.get("price"))
    if close_price <= 0 and closed_size > 0 and entry_price > 0:
        fallback_pnl = _safe_float(previous_position.get("unrealizedPnl"))
        adjustment = fallback_pnl / abs(prev_size) if abs(prev_size) > SIZE_EPSILON else 0.0
        candidate = entry_price + adjustment if is_long else entry_price - adjustment
        if candidate > 0:
            close_price = candidate
    if close_price <= 0:
        close_price = current_price

    closed_value = close_price * closed_size
    remaining_value = abs(_safe_float(current_position.get("positionValue")))

    realized_pnl = 0.0
    if closed_size > 0 and entry_price > 0 and close_price > 0:
        if is_long:
            realized_pnl = (close_price - entry_price) * closed_size
        else:
            realized_pnl = (entry_price - close_price) * closed_size

    pnl_sign = "" if realized_pnl >= 0 else "-"
    pnl_color = "🟢" if realized_pnl >= 0 else "🔴"
    pnl_display = format_number(abs(realized_pnl), 2)

    close_price_display = format_number(close_price) if close_price > 0 else "N/A"
    entry_price_display = format_number(entry_price) if entry_price > 0 else "N/A"
    closed_size_display = format_number(closed_size) if closed_size > 0 else "N/A"
    remaining_size_display = format_number(remaining_size) if remaining_size > 0 else "0"
    closed_value_display = format_number(closed_value, 2)
    remaining_value_display = format_number(remaining_value, 2)
    balance_display = format_number(balance, 2)
    current_price_display = format_number(current_price) if current_price > 0 else "N/A"

    return f"""♻️ <b>订单部分平仓</b> {pnl_color}

💳 <b>钱包地址</b>
<code>{address}</code>

━━━━━━━━━━━━━━━━━━━━
<b>{coin}/USDC</b> (全仓-{position_side})
━━━━━━━━━━━━━━━━━━━━

⚖️ <b>开仓均价</b>
<code>{entry_price_display}</code>

⚖️ <b>平仓价格</b>
<code>{close_price_display}</code>

📦 <b>本次平仓数量</b>
<code>{closed_size_display}</code>

📦 <b>剩余持仓数量</b>
<code>{remaining_size_display}</code>

💳 <b>本次关仓价值</b>
<code>${closed_value_display}</code>

💳 <b>剩余持仓价值</b>
<code>${remaining_value_display}</code>

📈 <b>实现盈亏</b>
<code>{pnl_sign}{pnl_display}</code>

💵 <b>当前价格</b>
<code>{current_price_display}</code>

💰 <b>余额</b>
<code>${balance_display}</code>"""


def format_empty_wallet_message(address: str, balance: float) -> str:
    balance_display = format_number(balance, 2)
    return f"""ℹ️ <b>钱包监控</b>

💳 <b>钱包地址</b>
<code>{address}</code>

📭 当前没有持仓或历史交易记录。

💰 <b>余额</b>
<code>${balance_display}</code>"""


def _format_wallet_snapshot(
    address: str,
    positions: Dict[str, Dict[str, Any]],
    current_prices: Dict[str, float],
    balance: float,
) -> str:
    if not positions:
        return ""

    snapshot_time_display = _format_timestamp(int(time.time() * 1000))
    balance_display = format_number(balance, 2)
    total_value = sum(abs(_safe_float(pos.get("positionValue"))) for pos in positions.values())
    total_value_display = format_number(total_value, 2)

    sections: List[str] = [
        "📊 <b>最新持仓快照</b>",
        "",
        "🕒 <b>更新时间</b>",
        f"<code>{snapshot_time_display}</code>",
        "",
        "💳 <b>钱包地址</b>",
        f"<code>{address}</code>",
        "",
        "💰 <b>账户权益</b>",
        f"<code>${balance_display}</code>",
    ]

    if total_value > 0:
        sections.extend(
            [
                "",
                "💼 <b>总持仓价值</b>",
                f"<code>${total_value_display}</code>",
            ]
        )

    for coin in sorted(positions):
        position = positions[coin]
        current_price = _safe_float(current_prices.get(coin))
        entry_price = _calculate_entry_price(position)
        position_value = abs(_safe_float(position.get("positionValue")))
        size = _safe_float(position.get("szi"))
        position_size = abs(size)
        is_long = size > 0
        position_side = "多" if is_long else "空"

        unrealized_pnl = _safe_float(position.get("unrealizedPnl"))
        pnl_percentage = 0.0
        if entry_price > 0 and position_size > 0 and current_price > 0:
            if is_long:
                pnl_percentage = ((current_price - entry_price) / entry_price) * 100
            else:
                pnl_percentage = ((entry_price - current_price) / entry_price) * 100

        pnl_sign = "" if unrealized_pnl >= 0 else "-"
        pnl_color = "🟢" if unrealized_pnl >= 0 else "🔴"
        pnl_display = format_number(abs(unrealized_pnl), 2)
        pnl_percent_display = format_number(abs(pnl_percentage), 2)

        leverage_display = _format_leverage(_calculate_leverage(position))
        position_value_display = format_number(position_value, 2)
        entry_price_display = format_number(entry_price) if entry_price > 0 else "N/A"
        current_price_display = format_number(current_price) if current_price > 0 else "N/A"
        size_display = format_number(position_size) if position_size > 0 else "N/A"

        sections.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━━━",
                f"<b>{coin}/USDC</b> (全仓-{position_side}) {pnl_color}",
                "━━━━━━━━━━━━━━━━━━━━",
                "",
                "📦 <b>持仓数量</b>",
                f"<code>{size_display}</code>",
                "",
                "⚙️ <b>杠杆</b>",
                f"<code>{leverage_display}</code>",
                "",
                "💳 <b>持仓价值</b>",
                f"<code>${position_value_display}</code>",
                "",
                "⚖️ <b>开仓均价</b>",
                f"<code>{entry_price_display}</code>",
                "",
                "💵 <b>当前价格</b>",
                f"<code>{current_price_display}</code>",
                "",
                "📈 <b>盈亏</b>",
                f"<code>{pnl_sign}{pnl_display}</code> ({pnl_sign}{pnl_percent_display}%)",
            ]
        )

    return "\n".join(sections)


def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not configured")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(url, data=payload, timeout=API_TIMEOUT)
            response.raise_for_status()
            return True
        except requests.exceptions.Timeout:
            if attempt == MAX_RETRIES - 1:
                logger.error("Telegram API timeout after %s attempts", MAX_RETRIES)
                return False
            wait_time = RETRY_DELAY * (2 ** attempt)
            logger.warning(
                "Telegram API timeout (attempt %s/%s), retrying in %ss",
                attempt + 1,
                MAX_RETRIES,
                wait_time,
            )
            time.sleep(wait_time)
        except requests.exceptions.RequestException as exc:
            logger.error("Error sending Telegram message: %s", exc)
            if attempt == MAX_RETRIES - 1:
                return False
            wait_time = RETRY_DELAY * (2 ** attempt)
            time.sleep(wait_time)
        except Exception as exc:  # pragma: no cover - unexpected path
            logger.error("Unexpected error sending Telegram message: %s", exc)
            return False
    return False


_STATE_STORE_ALERT_REGISTERED = False


def _ensure_state_store_alerts() -> None:
    global _STATE_STORE_ALERT_REGISTERED
    if _STATE_STORE_ALERT_REGISTERED:
        return

    def _handle_state_store_alert(message: str) -> None:
        logger.warning("State store alert: %s", message)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            alert_text = f"⚠️ Redis state store issue\n{message}"
            if send_telegram_message(alert_text):
                logger.info("Dispatched Redis alert to Telegram")
            else:
                logger.error("Failed to dispatch Redis alert to Telegram")
        else:
            logger.error("Cannot dispatch Redis alert; Telegram credentials missing")

    try:
        register_state_store_alert_handler(_handle_state_store_alert)
    except Exception as exc:  # pragma: no cover - defensive registration
        logger.warning("Failed to register state store alert handler: %s", exc)
        return

    _STATE_STORE_ALERT_REGISTERED = True


def _collect_wallet_updates(
    address: str,
    *,
    current_prices: Dict[str, float],
    previous_positions: Dict[str, Dict],
    meta: Dict[str, Any],
    include_snapshot: bool = False,
    force_snapshot: bool = False,
    suppress_events: bool = False,
) -> Tuple[Dict[str, Dict], List[Tuple[str, str, str]], Dict[str, Any]]:
    user_state = get_positions(address)
    positions = user_state.get("assetPositions", []) if user_state else []
    fills = get_trade_history(address)
    balance = _extract_account_value(user_state)

    current_positions: Dict[str, Dict] = {}
    notifications: List[Tuple[str, str, str]] = []
    current_coins: Set[str] = set()

    coins_meta = meta.setdefault(STATE_META_COINS_KEY, {})
    if not isinstance(coins_meta, dict):
        coins_meta = {}
        meta[STATE_META_COINS_KEY] = coins_meta

    for asset_position in positions:
        position_data = asset_position.get("position", {})
        coin = position_data.get("coin", "")
        if not coin:
            continue

        current_coins.add(coin)
        current_positions[coin] = position_data
        meta["empty_notified"] = False

        current_price = current_prices.get(coin, 0.0)
        if current_price <= 0:
            logger.warning("Current price unavailable for %s", coin)

        coin_meta = coins_meta.setdefault(coin, {})
        previous_position = previous_positions.get(coin)
        processed = False

        if previous_position:
            prev_size = _safe_float(previous_position.get("szi"))
            curr_size = _safe_float(position_data.get("szi"))
            prev_sign = 1 if prev_size > 0 else -1 if prev_size < 0 else 0
            curr_sign = 1 if curr_size > 0 else -1 if curr_size < 0 else 0

            if prev_sign != 0 and curr_sign != 0 and prev_sign == curr_sign:
                if abs(curr_size) < abs(prev_size) - SIZE_EPSILON:
                    reduce_fill = _find_relevant_fill(coin, fills, event_type="reduce")
                    reduce_event_id = _make_event_id("reduce", coin, reduce_fill, previous_position)
                    if coin_meta.get("last_reduce_id") != reduce_event_id:
                        trade_details = _build_trade_details(previous_position, reduce_fill)
                        avg_price = _calculate_order_average_price(coin, reduce_fill, fills)
                        if avg_price > 0:
                            trade_details["price"] = avg_price
                        if not suppress_events:
                            message = format_order_reduced_message(
                                address,
                                coin,
                                previous_position,
                                position_data,
                                trade_details,
                                balance,
                                current_price,
                            )
                            notifications.append(("reduced", coin, message))
                        coin_meta["last_reduce_id"] = reduce_event_id
                    processed = True
                elif abs(curr_size) <= abs(prev_size) + SIZE_EPSILON:
                    processed = True
                else:
                    processed = True

            elif prev_sign != 0 and curr_sign != 0 and prev_sign != curr_sign:
                close_fill = _find_relevant_fill(coin, fills, event_type="close")
                close_event_id = _make_event_id("close", coin, close_fill, previous_position)
                if coin_meta.get("last_close_id") != close_event_id:
                    trade_details = _build_trade_details(previous_position, close_fill)
                    avg_price = _compute_full_close_average_price(coin, fills, previous_position)
                    if avg_price <= 0:
                        avg_price = _calculate_order_average_price(coin, close_fill, fills)
                    if avg_price > 0:
                        trade_details["price"] = avg_price
                    if not suppress_events:
                        message = format_order_closed_message(
                            address,
                            coin,
                            previous_position,
                            trade_details,
                            balance,
                            current_price,
                        )
                        notifications.append(("closed", coin, message))
                    coin_meta["last_close_id"] = close_event_id
                coin_meta.pop("last_reduce_id", None)
                coin_meta.pop("last_open_id", None)
                previous_position = None
            else:
                processed = False

        if processed:
            continue

        open_fill = _find_relevant_fill(coin, fills, event_type="open")
        open_event_id = _make_event_id("open", coin, open_fill, position_data)
        if coin_meta.get("last_open_id") == open_event_id:
            continue

        trade_details = _build_trade_details(position_data, open_fill)
        entry_price = _calculate_entry_price(position_data)
        if entry_price > 0:
            trade_details["price"] = entry_price
        else:
            avg_price = _calculate_order_average_price(coin, open_fill, fills)
            if avg_price > 0:
                trade_details["price"] = avg_price
        if not suppress_events:
            message = format_order_placed_message(
                address,
                position_data,
                trade_details,
                balance,
                current_price=current_price,
            )
            notifications.append(("opened", coin, message))
        coin_meta["last_open_id"] = open_event_id
        coin_meta.pop("last_close_id", None)
        coin_meta.pop("last_reduce_id", None)

    closed_coins = set(previous_positions.keys()) - current_coins
    for coin in closed_coins:
        previous_position = previous_positions[coin]
        current_price = current_prices.get(coin, 0.0)
        coin_meta = coins_meta.setdefault(coin, {})
        close_fill = _find_relevant_fill(coin, fills, event_type="close")
        close_event_id = _make_event_id("close", coin, close_fill, previous_position)
        if coin_meta.get("last_close_id") == close_event_id:
            continue

        trade_details = _build_trade_details(previous_position, close_fill)
        avg_price = _compute_full_close_average_price(coin, fills, previous_position)
        if avg_price <= 0:
            avg_price = _calculate_order_average_price(coin, close_fill, fills)
        if avg_price > 0:
            trade_details["price"] = avg_price
        if not suppress_events:
            message = format_order_closed_message(
                address,
                coin,
                previous_position,
                trade_details,
                balance,
                current_price,
            )
            notifications.append(("closed", coin, message))
        coin_meta["last_close_id"] = close_event_id
        coin_meta.pop("last_open_id", None)
        coin_meta.pop("last_reduce_id", None)

    if not current_positions:
        empty_message = format_empty_wallet_message(address, balance)
        snapshot_hash = hashlib.sha256(empty_message.encode("utf-8")).hexdigest()
        last_snapshot = meta.get("last_snapshot_hash")
        if include_snapshot:
            if force_snapshot or snapshot_hash != last_snapshot:
                notifications.append(("snapshot", "", empty_message))
            meta["empty_notified"] = True
            meta["last_snapshot_hash"] = snapshot_hash
        elif not meta.get("empty_notified", False):
            notifications.append(("empty", "", empty_message))
            meta["empty_notified"] = True
            meta["last_snapshot_hash"] = snapshot_hash
    else:
        meta["empty_notified"] = False

    if current_positions and include_snapshot:
        snapshot_message = _format_wallet_snapshot(
            address,
            current_positions,
            current_prices,
            balance,
        )
        if snapshot_message:
            snapshot_hash = hashlib.sha256(snapshot_message.encode("utf-8")).hexdigest()
            last_snapshot = meta.get("last_snapshot_hash")
            if force_snapshot or snapshot_hash != last_snapshot:
                notifications.insert(0, ("snapshot", "", snapshot_message))
            meta["last_snapshot_hash"] = snapshot_hash

    return current_positions, notifications, meta


def _process_addresses(addresses: Iterable[str], *, reason: str) -> None:
    addresses = [addr for addr in addresses if addr]
    if not addresses:
        return

    global _snapshot_initialized
    include_snapshot = reason in {"full position scan", "snapshot"}
    suppress_events = reason in {"full position scan", "snapshot"}
    force_snapshot = False
    if reason == "snapshot":
        force_snapshot = True
    elif reason == "full position scan" and not _snapshot_initialized:
        force_snapshot = True
        _snapshot_initialized = True

    try:
        current_prices = get_current_prices()
    except Exception as exc:
        logger.error("Failed to fetch current prices during %s: %s", reason, exc)
        return

    pending_notifications: List[Tuple[str, str, str, str]] = []

    with _STATE_LOCK:
        previous_state = load_position_state()
        updated_state = dict(previous_state)

        for address in addresses:
            previous_entry = previous_state.get(address, {})
            previous_positions, raw_meta = _split_state_entry(previous_entry)
            previous_positions = dict(previous_positions)
            meta = _normalize_meta(raw_meta)

            try:
                current_positions, notifications, meta = _collect_wallet_updates(
                    address,
                    current_prices=current_prices,
                    previous_positions=previous_positions,
                    meta=meta,
                    include_snapshot=include_snapshot,
                    force_snapshot=force_snapshot or not meta.get("last_snapshot_hash"),
                    suppress_events=suppress_events,
                )
            except Exception as exc:
                logger.error("Error processing wallet %s during %s: %s", address, reason, exc)
                continue

            updated_state[address] = _compose_state_entry(current_positions, meta)
            for event_type, coin, message in notifications:
                pending_notifications.append((address, event_type, coin, message))

        save_position_state(updated_state)

    for address, event_type, coin, message in pending_notifications:
        if send_telegram_message(message):
            logger.info("Sent %s notification for %s from %s", event_type, coin, address)
        else:
            logger.warning(
                "Failed to send %s notification for %s from %s",
                event_type,
                coin,
                address,
            )
        time.sleep(MESSAGE_DELAY_SECONDS)


def check_position_changes_for_address(address: str) -> None:
    _process_addresses([address], reason="websocket event")


def create_websocket_handler(address: str):
    def handle_event(event: Dict[str, Any]) -> None:
        if _stop_event.is_set() or not isinstance(event, dict):
            return
        data = event.get("data", {})
        if "fills" in data or "orderUpdates" in data:
            threading.Timer(1.0, lambda: check_position_changes_for_address(address)).start()

    return handle_event


def start_websocket_monitoring() -> None:
    global _websocket_running
    addresses = list(CONFIGURED_ADDRESSES)
    if not addresses:
        logger.error("No wallet addresses configured for websocket monitoring")
        return

    logger.info("Starting websocket monitoring for %s wallet(s)", len(addresses))
    for address in addresses:
        try:
            info_client.subscribe(
                {"type": "userEvents", "user": address},
                create_websocket_handler(address),
            )
            info_client.subscribe(
                {"type": "userFills", "user": address},
                create_websocket_handler(address),
            )
            logger.info("Subscribed to websocket streams for %s", address)
        except Exception as exc:
            logger.error("Error subscribing to websocket for %s: %s", address, exc)

    _websocket_running = True
    logger.info("Websocket monitoring started. Waiting for events...")

    try:
        while not _stop_event.is_set():
            _stop_event.wait(1)
    except KeyboardInterrupt:
        logger.info("Websocket monitoring stopped by user")
        stop_websocket_monitoring()


def stop_websocket_monitoring() -> None:
    global _websocket_running
    _websocket_running = False
    _stop_event.set()
    logger.info("Websocket monitoring stopped")


def send_wallet_snapshot(addresses: Optional[Iterable[str]] = None) -> None:
    target_addresses = tuple(addresses) if addresses else CONFIGURED_ADDRESSES
    _process_addresses(target_addresses, reason="snapshot")


def monitor_all_wallets() -> None:
    _process_addresses(CONFIGURED_ADDRESSES, reason="full position scan")


def check_order_changes() -> None:
    _process_addresses(CONFIGURED_ADDRESSES, reason="order poll")


def validate_config() -> bool:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not configured in environment")
        return False
    if not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID not configured in environment")
        return False
    if not CONFIGURED_ADDRESSES:
        logger.error("WALLET_ADDRESSES not configured or empty")
        return False
    logger.info("Configuration validated: %s wallet(s) configured", len(CONFIGURED_ADDRESSES))
    return True


def signal_handler(signum: int, frame: Any) -> None:  # pragma: no cover - signal handler
    logger.info("Received shutdown signal (%s), stopping...", signum)
    stop_websocket_monitoring()
    sys.exit(0)


def main() -> None:
    logger.info("Starting Hyperliquid position monitor with websocket support")

    if not validate_config():
        logger.error("Configuration validation failed. Exiting.")
        return

    _ensure_state_store_alerts()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        monitor_all_wallets()
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user during initial scan")
        return
    except Exception as exc:
        logger.error("Unexpected error during initial monitoring: %s", exc)
        return

    websocket_thread = threading.Thread(target=start_websocket_monitoring, daemon=True)
    websocket_thread.start()

    schedule.every(4).hours.do(monitor_all_wallets)
    logger.info("Websocket monitoring active. Full position updates scheduled every 4 hours.")

    try:
        while not _stop_event.is_set():
            schedule.run_pending()
            _stop_event.wait(60)
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")
        stop_websocket_monitoring()
    except Exception as exc:
        logger.error("Unexpected error in scheduler loop: %s", exc)
        stop_websocket_monitoring()


if __name__ == "__main__":
    main()
