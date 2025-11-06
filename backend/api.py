"""REST API surface for Hyperliquid monitoring data."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .monitor_positions import (
    CONFIGURED_ADDRESSES,
    _calculate_entry_price,
    _calculate_leverage,
    _extract_account_value,
    _extract_tx_hash,
    _safe_float,
    _safe_int,
    calculate_position_metrics,
    get_current_prices,
    get_positions,
    get_trade_history,
    load_position_state,
)


class PositionPayload(BaseModel):
    coin: str
    side: str
    size: float
    entry_price: Optional[float] = Field(None, alias="entryPrice")
    mark_price: Optional[float] = Field(None, alias="markPrice")
    position_value: float = Field(..., alias="positionValue")
    margin_used: Optional[float] = Field(None, alias="marginUsed")
    liquidation_price: Optional[float] = Field(None, alias="liquidationPrice")
    leverage: Optional[float]
    unrealized_pnl: Optional[float] = Field(None, alias="unrealizedPnl")
    pnl_percent: Optional[float] = Field(None, alias="pnlPercent")
    funding_all_time: Optional[float] = Field(None, alias="fundingAllTime")
    funding_since_open: Optional[float] = Field(None, alias="fundingSinceOpen")
    updated_at: int = Field(..., alias="updatedAt")

    class Config:
        populate_by_name = True


class WalletSummaryPayload(BaseModel):
    address: str
    balance: float
    withdrawable: Optional[float]
    equity: Optional[float]
    total_position_value: float = Field(..., alias="totalPositionValue")
    timestamp: int
    positions: List[PositionPayload]

    class Config:
        populate_by_name = True


class FillPayload(BaseModel):
    coin: str
    side: str
    price: float
    size: float
    time_ms: int = Field(..., alias="timeMs")
    start_position: Optional[float] = Field(None, alias="startPosition")
    end_position: Optional[float] = Field(None, alias="endPosition")
    tx_hash: str = Field(..., alias="txHash")

    class Config:
        populate_by_name = True


class FillListPayload(BaseModel):
    address: str
    count: int
    items: List[FillPayload]


class WalletListPayload(BaseModel):
    wallets: List[str]
    count: int


class WalletMetricsPayload(BaseModel):
    address: str
    summary: WalletSummaryPayload
    fills: FillListPayload
    per_coin: Dict[str, Dict[str, float]] = Field(..., alias="perCoin")

    class Config:
        populate_by_name = True


def _format_side(size: float) -> str:
    if size > 0:
        return "long"
    if size < 0:
        return "short"
    return "flat"


def _build_position_payload(position: Dict[str, Any], mark_prices: Dict[str, float]) -> PositionPayload:
    coin = position.get("coin", "")
    size = _safe_float(position.get("szi"))
    position_value = abs(_safe_float(position.get("positionValue")))
    entry_price = _calculate_entry_price(position)
    mark_price = _safe_float(mark_prices.get(coin))
    margin_used = _safe_float(position.get("marginUsed")) or None
    leverage = _calculate_leverage(position)
    liquidation_price = (_safe_float(position.get("liquidationPx")) or None)
    pnl = _safe_float(position.get("unrealizedPnl")) or None
    pnl_percent: Optional[float] = None
    if entry_price > 0 and abs(size) > 0:
        pnl_percent = ((pnl or 0.0) / (entry_price * abs(size))) * 100
    funding = position.get("cumFunding") or {}
    updated_at = int(time.time() * 1000)

    return PositionPayload(
        coin=coin,
        side=_format_side(size),
        size=size,
        entryPrice=entry_price if entry_price > 0 else None,
        markPrice=mark_price if mark_price > 0 else None,
        positionValue=position_value,
        marginUsed=margin_used,
        liquidationPrice=liquidation_price,
        leverage=leverage,
        unrealizedPnl=pnl,
        pnlPercent=pnl_percent,
        fundingAllTime=_safe_float(funding.get("allTime")) or None,
        fundingSinceOpen=_safe_float(funding.get("sinceOpen")) or None,
        updatedAt=updated_at,
    )


def _compose_wallet_summary(address: str) -> WalletSummaryPayload:
    try:
        user_state = get_positions(address)
    except Exception as exc:  # pragma: no cover - network layer
        raise HTTPException(status_code=502, detail=f"Failed to fetch positions for {address}") from exc

    positions_payload: List[PositionPayload] = []
    prices: Dict[str, float] = {}
    try:
        prices = get_current_prices()
    except Exception:  # pragma: no cover - network layer
        prices = {}

    positions_raw = user_state.get("assetPositions", []) or []
    for entry in positions_raw:
        position = entry.get("position", {})
        if not position:
            continue
        positions_payload.append(_build_position_payload(position, prices))

    balance = _extract_account_value(user_state)
    withdrawable = _safe_float(user_state.get("withdrawable")) or None
    margin_summary = user_state.get("marginSummary") or {}
    equity = _safe_float(margin_summary.get("accountValue")) or None
    total_position_value = sum(item.position_value for item in positions_payload)

    timestamp = int(time.time() * 1000)

    return WalletSummaryPayload(
        address=address,
        balance=balance,
        withdrawable=withdrawable,
        equity=equity,
        totalPositionValue=total_position_value,
        timestamp=timestamp,
        positions=positions_payload,
    )


def _compose_fills(
    address: str,
    limit: int,
    fills: Optional[List[Dict[str, Any]]] = None,
) -> FillListPayload:
    try:
        fills_data = fills if fills is not None else get_trade_history(address)
    except Exception as exc:  # pragma: no cover - network layer
        raise HTTPException(status_code=502, detail=f"Failed to fetch fills for {address}") from exc

    sorted_fills = sorted(fills_data, key=lambda item: _safe_int(item.get("time")), reverse=True)

    payload: List[FillPayload] = []
    for fill in sorted_fills[:limit]:
        coin = fill.get("coin", "")
        price = _safe_float(fill.get("px"))
        size = _safe_float(fill.get("sz"))
        side_raw = str(fill.get("side", "")).upper()
        side = "buy" if side_raw == "B" else "sell" if side_raw == "A" else side_raw
        time_ms = _safe_int(fill.get("time"))
        start_position = _safe_float(fill.get("startPosition")) if fill.get("startPosition") is not None else None
        end_position = _safe_float(fill.get("endPosition")) if fill.get("endPosition") is not None else None
        tx_hash = _extract_tx_hash(fill)

        payload.append(
            FillPayload(
                coin=coin,
                side=side,
                price=price,
                size=size,
                timeMs=time_ms,
                startPosition=start_position,
                endPosition=end_position,
                txHash=tx_hash,
            )
        )

    return FillListPayload(address=address, count=len(payload), items=payload)


def _list_known_wallets() -> List[str]:
    state = load_position_state()
    state_wallets = list(state.keys())
    configured = list(CONFIGURED_ADDRESSES)
    merged = sorted({*(wallet for wallet in state_wallets if wallet), *(wallet for wallet in configured if wallet)})
    return merged


app = FastAPI(title="Hyperliquid Monitor API", version="0.1.0")

allowed_origins = [
    origin.strip()
    for origin in os.getenv("API_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def healthcheck() -> Dict[str, Any]:
    return {"status": "ok", "timestamp": int(time.time() * 1000)}


@app.get("/api/wallets", response_model=WalletListPayload)
def list_wallets() -> WalletListPayload:
    wallets = _list_known_wallets()
    return WalletListPayload(wallets=wallets, count=len(wallets))


@app.get("/api/wallets/{address}", response_model=WalletSummaryPayload)
def wallet_summary(address: str) -> WalletSummaryPayload:
    wallets = _list_known_wallets()
    if wallets and address not in wallets:
        raise HTTPException(status_code=404, detail=f"Wallet {address} is not tracked")
    return _compose_wallet_summary(address)


@app.get("/api/wallets/{address}/positions", response_model=WalletSummaryPayload)
def wallet_positions(address: str) -> WalletSummaryPayload:
    return wallet_summary(address)


@app.get("/api/wallets/{address}/fills", response_model=FillListPayload)
def wallet_fills(address: str, limit: int = Query(50, ge=1, le=200)) -> FillListPayload:
    return _compose_fills(address, limit)


@app.get("/api/wallets/{address}/metrics", response_model=WalletMetricsPayload)
def wallet_metrics(address: str) -> WalletMetricsPayload:
    summary = wallet_summary(address)
    try:
        fills_raw = get_trade_history(address)
    except Exception as exc:  # pragma: no cover - network layer
        raise HTTPException(status_code=502, detail=f"Failed to fetch fills for {address}") from exc

    fills_payload = _compose_fills(address, limit=200, fills=fills_raw)
    per_coin: Dict[str, Dict[str, float]] = {}
    for position in summary.positions:
        metrics = calculate_position_metrics(position.coin, fills_raw)
        per_coin[position.coin] = metrics

    return WalletMetricsPayload(
        address=address,
        summary=summary,
        fills=fills_payload,
        perCoin=per_coin,
    )
