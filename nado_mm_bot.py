#!/usr/bin/env python3
"""Simple Nado perp market-making bot scaffold.

This script focuses on structure, safety checks, and configurable parameters.
It uses the Nado Python SDK for price queries and expects you to wire in
order/position methods for your specific account + SDK version.
"""

from __future__ import annotations

import os
import time
import math
import json
import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import dotenv_values
# --- Trading parameters (Nado perp product IDs) ---
# For deposit ~$40, better to start with one market
# Accepts token symbols or numeric product IDs (e.g. ["HYPE"] or [16]).
DEFAULT_PRODUCT_IDS: List[Union[int, str]] = ["HYPE"]  # BTC/ETH/SOL/HYPE

PRODUCT_ID_BY_SYMBOL = {
    "BTC": 2,
    "ETH": 4,
    "SOL": 8,
    "HYPE": 16,
}

# Market making parameters
SPREAD_PERCENT = 0.05  # Spread in percent (0.05% = 5 bps)
MIN_SPREAD_BPS = 5.0  # Min spread in bps for placing orders
REQUOTE_BPS = 3.0     # Requote orders if price moves > this bps
ORDER_SIZE_USD = 110.0  # Order size
MAX_POSITION_SIZE_USD = 100.0  # Max position size

# Risk management
MAX_DAILY_LOSS_USD = 2.0  # Max daily loss
MAX_OPEN_ORDERS = 4  # Max open orders

# Reliability
MAX_CONSECUTIVE_ERRORS = 5  # Restart after N consecutive errors
RESTART_ON_FAILURE = True   # Auto-restart on failure
RESTART_DELAY_SECONDS = 5   # Pause before restart

# Update intervals
ORDER_UPDATE_INTERVAL = 10  # Seconds between order refresh
PRICE_CHECK_INTERVAL = 1  # Seconds between price checks
FILL_POLL_INTERVAL = 10  # Seconds between fill checks

# Network / timeouts
REQUEST_TIMEOUT = 15  # API request timeout
QUERY_RETRIES = 2  # Retry count for query requests
QUERY_RETRY_BACKOFF = 1.5  # Backoff seconds between retries
ORDER_EXPIRATION_SECONDS = 60  # How long limit orders remain valid
RECV_TIME_MS = 5000  # Allowed receive window for execute requests (ms)
HEALTH_COOLDOWN_SECONDS = 10  # Pause posting after health error 2006
POST_ONLY_REPRICE_TICKS = 2  # Extra ticks away from mid on post-only cross (2008)
POST_ONLY_MAX_RETRIES = 0  # 0 = skip cycle on 2008 instead of aggressive reposting
REDUCE_IOC_COOLDOWN_SECONDS = 60  # Throttle reduce-only IOC attempts on repeated 2006
TP_PNL_PERCENT = 0.25  # Close position when unrealized PnL >= this percent
TP_COOLDOWN_SECONDS = 30  # Cooldown between TP close attempts
HEALTH_REDUCE_PNL_PERCENT = 0.25  # On 2006, reduce position only if pnl% >= threshold
HEALTH_REDUCE_FRACTION = 0.5  # On 2006, cut position by half
PRICE_INCREMENT_X18_BY_SYMBOL = {
    "BTC": 1_000_000_000_000_000_000,
    "ETH": 0,  # auto-loaded from market metadata at runtime
    "SOL": 0,  # auto-loaded from market metadata at runtime
    "HYPE": 0,  # auto-loaded from market metadata at runtime
}
SIZE_INCREMENT_X18_BY_SYMBOL = {
    "BTC": 50_000_000_000_000,
    "ETH": 0,  # auto-loaded from market metadata at runtime
    "SOL": 100_000_000_000_000_000,
    "HYPE": 0,  # auto-loaded from market metadata at runtime
}
MIN_NOTIONAL_X18_BY_PRODUCT = {
    2: 105_000_000_000_000_000_000,   # BTC-PERP
    4: 105_000_000_000_000_000_000,   # ETH-PERP
    8: 105_000_000_000_000_000_000,   # SOL-PERP
    16: 105_000_000_000_000_000_000,  # HYPE-PERP
}

PRICE_INCREMENT_X18_BY_PRODUCT = {
    PRODUCT_ID_BY_SYMBOL[symbol]: value
    for symbol, value in PRICE_INCREMENT_X18_BY_SYMBOL.items()
    if symbol in PRODUCT_ID_BY_SYMBOL
}
SIZE_INCREMENT_X18_BY_PRODUCT = {
    PRODUCT_ID_BY_SYMBOL[symbol]: value
    for symbol, value in SIZE_INCREMENT_X18_BY_SYMBOL.items()
    if symbol in PRODUCT_ID_BY_SYMBOL
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_subaccount_sender(address: str, name: str) -> str:
    """Build a subaccount sender (bytes32) from address + 12-byte name."""
    addr = address.lower()
    if addr.startswith("0x"):
        addr = addr[2:]
    if len(addr) != 40:
        raise ValueError("Signer address must be 20 bytes (40 hex chars)")
    name_bytes = name.encode("ascii", "ignore")[:12]
    name_bytes += b"\x00" * (12 - len(name_bytes))
    return "0x" + addr + name_bytes.hex()

def _load_env_file(path: str) -> Dict[str, str]:
    # python-dotenv handles quoting, comments, and blank lines.
    data = dotenv_values(path)
    # Coerce None to empty string for safe .get() usage.
    return {k: (v if v is not None else "") for k, v in data.items()}


def _env_bool(val: str) -> bool:
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _setup_logger() -> logging.Logger:
    env = _load_env_file(".env")
    level = env.get("NADO_LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    # Reduce third-party transport noise.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    return logging.getLogger("nado-mm")


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("orders", "data", "result", "items"):
            v = value.get(key)
            if isinstance(v, list):
                return v
        return []
    # Try common attributes on SDK response objects
    for key in ("orders", "data", "result", "items"):
        v = getattr(value, key, None)
        if isinstance(v, list):
            return v
    return []


def _resolve_product_ids(values: List[Union[int, str]]) -> List[int]:
    resolved: List[int] = []
    for value in values:
        if isinstance(value, int):
            resolved.append(value)
            continue
        token = str(value).strip().upper()
        if token in PRODUCT_ID_BY_SYMBOL:
            resolved.append(PRODUCT_ID_BY_SYMBOL[token])
            continue
        if token.isdigit():
            resolved.append(int(token))
            continue
        raise ValueError(f"Unsupported product token/id: {value}")
    return resolved


@dataclass
class Config:
    product_ids: List[int] = field(default_factory=lambda: _resolve_product_ids(list(DEFAULT_PRODUCT_IDS)))
    spread_percent: float = SPREAD_PERCENT
    min_spread_bps: float = MIN_SPREAD_BPS
    requote_bps: float = REQUOTE_BPS
    order_size_usd: float = ORDER_SIZE_USD
    max_position_size_usd: float = MAX_POSITION_SIZE_USD
    max_daily_loss_usd: float = MAX_DAILY_LOSS_USD
    max_open_orders: int = MAX_OPEN_ORDERS

    max_consecutive_errors: int = MAX_CONSECUTIVE_ERRORS
    restart_on_failure: bool = RESTART_ON_FAILURE
    restart_delay_seconds: int = RESTART_DELAY_SECONDS

    order_update_interval: int = ORDER_UPDATE_INTERVAL
    price_check_interval: int = PRICE_CHECK_INTERVAL
    fill_poll_interval: int = FILL_POLL_INTERVAL

    request_timeout: int = REQUEST_TIMEOUT
    query_retries: int = QUERY_RETRIES
    query_retry_backoff: float = QUERY_RETRY_BACKOFF
    price_increment_x18_by_product: Dict[int, int] = field(
        default_factory=lambda: dict(PRICE_INCREMENT_X18_BY_PRODUCT)
    )
    size_increment_x18_by_product: Dict[int, int] = field(
        default_factory=lambda: dict(SIZE_INCREMENT_X18_BY_PRODUCT)
    )
    min_notional_x18_by_product: Dict[int, int] = field(
        default_factory=lambda: dict(MIN_NOTIONAL_X18_BY_PRODUCT)
    )


class NadoApiAdapter:
    """Thin wrapper around the Nado Python SDK.

    Only get_prices is wired to the documented example; order and position
    methods must be implemented for your SDK version.
    """

    def __init__(self, client: Any):
        self.client = client
        self.log = logging.getLogger("nado-mm")
        self.sender = self._derive_sender()
        self._warned_pnl = False
        self._debug_pnl = _env_bool(_load_env_file(".env").get("NADO_DEBUG_PNL", ""))
        self._product_symbol_by_id: Dict[int, str] = {}

    def _derive_sender(self) -> str:
        # Sender is a bytes32 subaccount identifier built from signer address + subaccount name.
        env = _load_env_file(".env")
        subaccount_name = env.get("NADO_SUBACCOUNT_NAME", "default")
        addr = None
        # Preferred: engine client signer address
        try:
            addr = self.client.context.engine_client.signer.address
        except Exception:
            addr = None
        if not addr:
            try:
                addr = self.client.context.signer.address
            except Exception:
                addr = None
        if not addr:
            raise RuntimeError("Cannot determine signer address from client")
        return build_subaccount_sender(addr, subaccount_name)

    def _order_nonce(self) -> int:
        # Order nonce encodes recv_time; use SDK helper to avoid clock-window errors.
        try:
            from nado_protocol.utils.nonce import gen_order_nonce

            return int(gen_order_nonce(int(time.time() * 1000) + RECV_TIME_MS))
        except Exception as exc:
            raise RuntimeError("Cannot fetch order nonce from engine client") from exc

    def _construct_params(self, cls, **kwargs):
        # Filter kwargs to match the constructor signature to avoid version mismatches.
        sig = inspect.signature(cls)
        allowed = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return cls(**allowed)

    def _recv_time(self) -> int:
        # Provide a time window for engine execute requests.
        return int(time.time() * 1000) + RECV_TIME_MS

    def _call_indexer_matches(self, since_ts: float) -> List[Any]:
        # Fetch trade matches from indexer to compute realized PnL.
        now_ts = time.time()
        indexer = getattr(self.client, "indexer", None)
        if indexer is None:
            indexer = getattr(self.client.context, "indexer", None)
        if indexer is None:
            return []
        for method_name in (
            "get_matches",
            "get_subaccount_matches",
            "get_subaccount_historical_matches",
        ):
            method = getattr(indexer, method_name, None)
            if not method:
                continue
            sig = inspect.signature(method)
            kwargs: Dict[str, Any] = {}
            # Common parameter names across versions
            if "sender" in sig.parameters:
                kwargs["sender"] = self.sender
            if "subaccount" in sig.parameters:
                kwargs["subaccount"] = self.sender
            if "start_time" in sig.parameters:
                kwargs["start_time"] = since_ts
            if "end_time" in sig.parameters:
                kwargs["end_time"] = now_ts
            if "limit" in sig.parameters:
                kwargs["limit"] = 1000
            if "after" in sig.parameters:
                kwargs["after"] = since_ts
            res = method(**kwargs)
            # Unwrap common response shapes
            if isinstance(res, dict):
                matches = res.get("matches") or res.get("data") or res.get("result")
                if isinstance(matches, list):
                    return matches
            if isinstance(res, list):
                return res
            matches = _get(res, "matches") or _get(res, "data") or _get(res, "result")
            if isinstance(matches, list):
                return matches
        return []

    def get_prices(self, product_id: int) -> Dict[str, Any]:
        return self.client.perp.get_prices(product_id)

    def get_top_of_book(self, product_id: int) -> Tuple[Optional[float], Optional[float]]:
        # Return (best_bid, best_ask) if available.
        def _parse_px(v: Any) -> Optional[float]:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                fv = float(v)
                return fv / 1e18 if abs(fv) > 1e12 else fv
            s = str(v).strip()
            if not s:
                return None
            fv = float(s)
            return fv / 1e18 if abs(fv) > 1e12 else fv

        ticker = self._product_symbol_by_id.get(product_id)
        if not ticker:
            try:
                symbols = self.client.market.get_all_product_symbols()
                items = symbols if isinstance(symbols, list) else _as_list(_get(symbols, "symbols"))
                for item in items:
                    pid = _get(item, "product_id")
                    sym = _get(item, "symbol")
                    if pid is not None and sym:
                        self._product_symbol_by_id[int(pid)] = str(sym)
                ticker = self._product_symbol_by_id.get(product_id)
            except Exception:
                ticker = None
        if not ticker:
            return None, None
        try:
            ob = self.client.context.engine_client.get_orderbook(ticker, 1)
            bids = _get(ob, "bids", []) or []
            asks = _get(ob, "asks", []) or []
            best_bid = None
            best_ask = None
            if bids:
                # Expected shape: [[price, size], ...]
                b0 = bids[0]
                bp = b0[0] if isinstance(b0, (list, tuple)) and b0 else _get(b0, "price")
                best_bid = _parse_px(bp)
            if asks:
                a0 = asks[0]
                ap = a0[0] if isinstance(a0, (list, tuple)) and a0 else _get(a0, "price")
                best_ask = _parse_px(ap)
            return best_bid, best_ask
        except Exception:
            return None, None

    def get_product_constraints(self, product_id: int) -> Dict[str, int]:
        """Fetch size/price increments and min notional from market metadata."""
        try:
            from nado_protocol.indexer_client.types.query import IndexerProductSnapshotsParams

            params = self._construct_params(
                IndexerProductSnapshotsParams,
                product_id=product_id,
                limit=1,
            )
            data = self.client.market.get_product_snapshots(params)
            products = _as_list(_get(data, "products"))
            if not products:
                return {}
            product_entry = products[0]
            product = _get(product_entry, "product")
            perp = _get(product, "perp") if product is not None else None
            book = _get(perp, "book_info")
            if book is None:
                return {}
            size_increment = _get(book, "size_increment")
            price_increment = _get(book, "price_increment_x18")
            min_size = _get(book, "min_size")
            out: Dict[str, int] = {}
            if size_increment is not None:
                out["size_increment"] = int(size_increment)
            if price_increment is not None:
                out["price_increment_x18"] = int(price_increment)
            if min_size is not None:
                out["min_size"] = int(min_size)
            return out
        except Exception:
            return {}

    def list_open_orders(self, product_id: int) -> List[Dict[str, Any]]:
        res = self.client.market.get_subaccount_open_orders(product_id, self.sender)
        return _as_list(res)

    def cancel_orders(self, order_ids: List[str], product_id: Optional[int] = None) -> None:
        if not order_ids:
            return
        from nado_protocol.engine_client.types import CancelOrdersParams

        params = self._construct_params(
            CancelOrdersParams,
            productIds=[product_id] if product_id is not None else [],
            digests=order_ids,
            nonce=self._order_nonce(),
        )
        self.client.market.cancel_orders(params)

    def place_limit_order(
        self,
        product_id: int,
        is_buy: bool,
        price: float,
        size: float,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> str:
        from nado_protocol.engine_client.types import OrderParams, PlaceOrderParams
        from nado_protocol.utils.expiration import get_expiration_timestamp, OrderType
        from nado_protocol.utils.math import to_x18
        from nado_protocol.utils.order import build_appendix

        amount = size if is_buy else -size
        order_type = OrderType.POST_ONLY if post_only else OrderType.IOC
        order = self._construct_params(
            OrderParams,
            sender=self.sender,
            priceX18=str(to_x18(price)),
            amount=str(to_x18(amount)),
            expiration=get_expiration_timestamp(ORDER_EXPIRATION_SECONDS),
            nonce=self._order_nonce(),
            appendix=build_appendix(order_type=order_type, reduce_only=reduce_only),
            recv_time=self._recv_time(),
        )
        params = self._construct_params(
            PlaceOrderParams,
            product_id=product_id,
            order=order,
            recv_time=self._recv_time(),
        )
        res = self.client.market.place_order(params)
        # Try to return digest if available
        if isinstance(res, dict):
            return res.get("digest", "")
        return getattr(res, "digest", "")

    def get_position_usd(self, product_id: int) -> float:
        # Convert base position to USD using current mark/index price.
        summary = self.client.subaccount.get_engine_subaccount_summary(self.sender)
        perp_balances = _get(summary, "perp_balances", [])
        if not perp_balances:
            return 0.0
        pos_amount = 0.0
        for bal in perp_balances:
            if _get(bal, "product_id") == product_id:
                amt = _get(bal, "amount") or _get(bal, "position") or _get(bal, "size")
                if amt is not None:
                    pos_amount = float(amt)
                break
        if pos_amount == 0.0:
            return 0.0
        prices = self.client.perp.get_prices(product_id)
        price = (
            _get(prices, "mark_price")
            or _get(prices, "index_price")
            or _get(prices, "markPrice")
            or _get(prices, "indexPrice")
        )
        if price is None:
            price = (
                _get(prices, "mark_price_x18")
                or _get(prices, "index_price_x18")
                or _get(prices, "markPriceX18")
                or _get(prices, "indexPriceX18")
            )
            if isinstance(price, str) and price.isdigit():
                price = int(price) / 1e18
            elif isinstance(price, (int, float)):
                price = float(price) / 1e18
        return float(price) * pos_amount if price else 0.0

    def _to_float_amount(self, val: Any) -> float:
        if val is None:
            return 0.0
        if isinstance(val, (int, float)):
            num = float(val)
            return num / 1e18 if abs(num) > 1e12 else num
        s = str(val).strip()
        if not s:
            return 0.0
        try:
            num = float(s)
        except Exception:
            return 0.0
        return num / 1e18 if abs(num) > 1e12 else num

    def get_position_snapshot(self, product_id: int) -> Dict[str, float]:
        """Return current position amount, vQuote, and mark price."""
        summary = self.client.subaccount.get_engine_subaccount_summary(self.sender)
        perp_balances = _as_list(_get(summary, "perp_balances", []))
        amount = 0.0
        v_quote = 0.0
        for bal in perp_balances:
            if _get(bal, "product_id") != product_id:
                continue
            balance = _get(bal, "balance", bal)
            amount = self._to_float_amount(_get(balance, "amount") or _get(bal, "amount"))
            v_quote = self._to_float_amount(
                _get(balance, "v_quote_balance")
                or _get(balance, "vQuoteBalance")
                or _get(bal, "v_quote_balance")
                or _get(bal, "vQuoteBalance")
            )
            break
        prices = self.client.perp.get_prices(product_id)
        mark = (
            _get(prices, "mark_price")
            or _get(prices, "index_price")
            or _get(prices, "markPrice")
            or _get(prices, "indexPrice")
            or _get(prices, "mark_price_x18")
            or _get(prices, "index_price_x18")
            or _get(prices, "markPriceX18")
            or _get(prices, "indexPriceX18")
        )
        mark_price = self._to_float_amount(mark)
        return {"amount": amount, "v_quote": v_quote, "mark_price": mark_price}

    def get_realized_pnl_usd(self, since_ts: float) -> float:
        # Prefer indexer matches; fall back to summary if needed.
        matches = self._call_indexer_matches(since_ts)
        if matches:
            total = 0.0
            for m in matches:
                pnl = (
                    _get(m, "realized_pnl")
                    or _get(m, "realizedPnl")
                    or _get(m, "pnl")
                    or _get(m, "profit_loss")
                )
                if pnl is None:
                    # Try nested fields
                    pnl = _get(_get(m, "stats"), "realized_pnl") or _get(_get(m, "stats"), "realizedPnl")
                if pnl is None:
                    pnl = _get(_get(m, "trade"), "realized_pnl") or _get(_get(m, "trade"), "realizedPnl")
                if pnl is not None:
                    try:
                        total += float(pnl)
                    except Exception:
                        pass
            if total == 0.0 and self._debug_pnl:
                sample = matches[0]
                self.log.debug(
                    "pnl-debug: first match keys=%s",
                    list(sample.keys()) if isinstance(sample, dict) else dir(sample),
                )
            return total

        summary = self.client.subaccount.get_engine_subaccount_summary(self.sender)
        pnl = _get(summary, "realized_pnl") or _get(summary, "realizedPnl")
        if pnl is None:
            if not self._warned_pnl:
                if self._debug_pnl:
                    self.log.debug("pnl-debug: indexer returned no matches; check indexer access and time range")
                self.log.warning("pnl: realized PnL unavailable in indexer/summary; daily loss guard disabled")
                self._warned_pnl = True
            return 0.0
        return float(pnl)


class MarketMakerBot:
    def __init__(self, cfg: Config, api: NadoApiAdapter):
        self.cfg = cfg
        self.api = api
        self.log = logging.getLogger("nado-mm")
        env = _load_env_file(".env")
        self._trace = _env_bool(env.get("NADO_TRACE", ""))
        self._trace_interval = max(1, int(env.get("NADO_TRACE_INTERVAL", "5") or "5"))
        self._consecutive_errors = 0
        self._start_of_day_ts = self._day_start_ts()
        self._last_prices: Dict[int, float] = {}
        self._last_order_refresh: Dict[int, float] = {}
        self._last_trace_ts = 0.0
        self._constraints_loaded = False
        self._iteration = 0
        self._health_cooldown_until: Dict[int, float] = {}
        self._last_reduce_ioc_ts: Dict[int, float] = {}
        self._last_tp_ts: Dict[int, float] = {}

    def _day_start_ts(self) -> float:
        # UTC day boundary for daily PnL.
        now = datetime.now(timezone.utc)
        day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        return day_start.timestamp()

    def _safe_spread_fraction(self) -> float:
        # Use the larger of percent-spread or min bps to avoid too-tight quoting.
        spread_from_percent = self.cfg.spread_percent / 100.0
        spread_from_bps = self.cfg.min_spread_bps / 10000.0
        return max(spread_from_percent, spread_from_bps)

    def _snap_price(self, product_id: int, price: float, is_buy: bool) -> float:
        inc = self.cfg.price_increment_x18_by_product.get(product_id, 1_000_000_000_000_000_000)
        price_x18 = int(price * 1e18)
        if inc <= 0:
            return price
        if is_buy:
            snapped = (price_x18 // inc) * inc
        else:
            snapped = ((price_x18 + inc - 1) // inc) * inc
        return snapped / 1e18

    def _tick_size(self, product_id: int) -> float:
        inc = self.cfg.price_increment_x18_by_product.get(product_id, 1_000_000_000_000_000_000)
        return inc / 1e18 if inc > 0 else 1.0

    def _snap_size(self, product_id: int, size: float) -> float:
        inc = self.cfg.size_increment_x18_by_product.get(product_id, 0)
        if inc <= 0:
            return size
        size_x18 = int(size * 1e18)
        snapped = (size_x18 // inc) * inc
        return snapped / 1e18

    def _extract_error_code(self, exc: Exception) -> Optional[int]:
        msg = str(exc)
        try:
            payload = json.loads(msg)
            code = payload.get("error_code")
            return int(code) if code is not None else None
        except Exception:
            return None

    async def _handle_insufficient_health(self, product_id: int, mid_price: float) -> None:
        self._health_cooldown_until[product_id] = time.time() + HEALTH_COOLDOWN_SECONDS
        self.log.warning(
            "health: cooldown started | product=%s cooldown_s=%s",
            product_id,
            HEALTH_COOLDOWN_SECONDS,
        )
        # 1) Free margin by canceling active quotes.
        open_orders = await self._query_with_retries(self.api.list_open_orders, product_id)
        order_ids = [_get(o, "id") for o in open_orders if _get(o, "id")]
        if order_ids:
            await self._query_with_retries(self.api.cancel_orders, order_ids, product_id)
            self.log.warning("health: canceled %s open orders | product=%s", len(order_ids), product_id)

        # 2) Reduce existing position with a reduce-only IOC order.
        # For 2006 we reduce by fraction only when current pnl% is good enough.
        now = time.time()
        last_reduce = self._last_reduce_ioc_ts.get(product_id, 0.0)
        if now - last_reduce < REDUCE_IOC_COOLDOWN_SECONDS:
            self.log.warning(
                "health: skip reduce IOC (cooldown) | product=%s wait_s=%.1f",
                product_id,
                REDUCE_IOC_COOLDOWN_SECONDS - (now - last_reduce),
            )
            return
        snap = await self._query_with_retries(self.api.get_position_snapshot, product_id)
        amount = snap.get("amount", 0.0)
        mark = snap.get("mark_price", 0.0)
        v_quote = snap.get("v_quote", 0.0)
        if abs(amount) <= 0 or mark <= 0:
            return
        position_notional = abs(amount * mark)
        if position_notional <= 0:
            return
        unrealized_pnl = amount * mark + v_quote
        pnl_pct = unrealized_pnl / position_notional * 100.0
        if pnl_pct < HEALTH_REDUCE_PNL_PERCENT:
            self.log.warning(
                "health: skip reduce IOC (pnl below threshold) | product=%s pnl_pct=%.4f threshold=%.4f",
                product_id,
                pnl_pct,
                HEALTH_REDUCE_PNL_PERCENT,
            )
            return

        reduce_size = self._snap_size(product_id, abs(amount) * HEALTH_REDUCE_FRACTION)
        if reduce_size <= 0:
            self.log.warning("health: cannot reduce, snapped size is zero | product=%s", product_id)
            return
        reduce_is_buy = amount < 0  # buy to reduce short, sell to reduce long
        aggressive_price = self._snap_price(
            product_id,
            mark * (1.02 if reduce_is_buy else 0.98),
            reduce_is_buy,
        )
        try:
            await self._query_with_retries(
                self.api.place_limit_order,
                product_id,
                reduce_is_buy,
                aggressive_price,
                reduce_size,
                False,  # post_only
                True,   # reduce_only
            )
            self._last_reduce_ioc_ts[product_id] = now
            self.log.warning(
                "health: sent reduce-only IOC | product=%s side=%s size=%.6f price=%.4f pnl_pct=%.4f",
                product_id,
                "BUY" if reduce_is_buy else "SELL",
                reduce_size,
                aggressive_price,
                pnl_pct,
            )
        except Exception as exc:
            self.log.error("health: failed to reduce position | product=%s err=%s", product_id, exc)


    async def _load_constraints_once(self) -> None:
        if self._constraints_loaded:
            return
        for product_id in self.cfg.product_ids:
            constraints = await self._query_with_retries(self.api.get_product_constraints, product_id)
            if constraints.get("size_increment"):
                self.cfg.size_increment_x18_by_product[product_id] = constraints["size_increment"]
            if constraints.get("price_increment_x18"):
                self.cfg.price_increment_x18_by_product[product_id] = constraints["price_increment_x18"]
            if constraints.get("min_size"):
                self.cfg.min_notional_x18_by_product[product_id] = constraints["min_size"]
            self.log.info(
                "constraints: product=%s size_inc=%s price_inc=%s min_notional=%s",
                product_id,
                self.cfg.size_increment_x18_by_product.get(product_id, 0),
                self.cfg.price_increment_x18_by_product.get(product_id, 0),
                self.cfg.min_notional_x18_by_product.get(product_id, 0),
            )
        self._constraints_loaded = True

    async def _try_take_profit(self, product_id: int) -> bool:
        now = time.time()
        if now - self._last_tp_ts.get(product_id, 0.0) < TP_COOLDOWN_SECONDS:
            return False
        snap = await self._query_with_retries(self.api.get_position_snapshot, product_id)
        amount = snap.get("amount", 0.0)
        mark = snap.get("mark_price", 0.0)
        v_quote = snap.get("v_quote", 0.0)
        if abs(amount) <= 0 or mark <= 0:
            return False
        position_notional = abs(amount * mark)
        if position_notional <= 0:
            return False
        unrealized_pnl = amount * mark + v_quote
        pnl_pct = unrealized_pnl / position_notional * 100.0
        if pnl_pct < TP_PNL_PERCENT:
            return False

        open_orders = await self._query_with_retries(self.api.list_open_orders, product_id)
        order_ids = [_get(o, "id") for o in open_orders if _get(o, "id")]
        if order_ids:
            await self._query_with_retries(self.api.cancel_orders, order_ids, product_id)

        close_is_buy = amount < 0
        close_size = self._snap_size(product_id, abs(amount))
        if close_size <= 0:
            return False
        close_price = self._snap_price(
            product_id,
            mark * (1.01 if close_is_buy else 0.99),
            close_is_buy,
        )
        await self._query_with_retries(
            self.api.place_limit_order,
            product_id,
            close_is_buy,
            close_price,
            close_size,
            False,  # post_only
            True,   # reduce_only
        )
        self._last_tp_ts[product_id] = now
        self.log.info(
            "tp: close submitted | product=%s pnl_pct=%.4f side=%s size=%.6f",
            product_id,
            pnl_pct,
            "BUY" if close_is_buy else "SELL",
            close_size,
        )
        return True

    def _should_requote(self, old_price: float, new_price: float) -> bool:
        # Requote when mid moves beyond the configured bps threshold.
        if old_price <= 0:
            return True
        diff_bps = abs(new_price - old_price) / old_price * 10000.0
        return diff_bps >= self.cfg.requote_bps

    def _mid_from_prices(self, prices: Dict[str, Any]) -> Optional[float]:
        # Extract mid from common fields, including x18-encoded values.
        # Try common shapes: {"index_price": x, "mark_price": y}
        for key in ("mark_price", "index_price", "markPrice", "indexPrice"):
            val = _get(prices, key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
            if isinstance(val, str) and val.isdigit():
                try:
                    return float(val)
                except Exception:
                    pass
        # X18 encoded prices
        for key in ("mark_price_x18", "index_price_x18", "markPriceX18", "indexPriceX18"):
            val = _get(prices, key)
            if isinstance(val, str) and val.isdigit():
                try:
                    return int(val) / 1e18
                except Exception:
                    pass
            if isinstance(val, (int, float)) and val > 0:
                return float(val) / 1e18
        # Some SDKs return objects with attributes
        for key in ("mark_price", "index_price"):
            val = getattr(prices, key, None)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
        return None

    async def _query_with_retries(self, fn, *args, **kwargs):
        # Retry wrapper for transient RPC failures.
        attempt = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                # Business-rule errors are not transient; do not retry them.
                code = self._extract_error_code(exc)
                if code in {2000, 2001, 2006, 2008, 2070, 2094, 2011}:
                    raise
                if attempt >= self.cfg.query_retries:
                    raise
                attempt += 1
                await asyncio.sleep(self.cfg.query_retry_backoff)

    async def _refresh_orders_for_product(self, product_id: int, mid_price: float, last_mid: float) -> None:
        # Core market-making step: compute bid/ask and maintain quotes.
        spread = self._safe_spread_fraction()
        bid_price = self._snap_price(product_id, mid_price * (1 - spread), True)
        ask_price = self._snap_price(product_id, mid_price * (1 + spread), False)
        tick = self._tick_size(product_id)
        best_bid, best_ask = await self._query_with_retries(self.api.get_top_of_book, product_id)
        if best_bid is not None:
            bid_price = self._snap_price(product_id, min(bid_price, best_bid - tick), True)
        if best_ask is not None:
            ask_price = self._snap_price(product_id, max(ask_price, best_ask + tick), False)
        if ask_price <= bid_price:
            ask_price = self._snap_price(product_id, bid_price + max(tick, 1e-9), False)

        # Size in base units
        size = self._snap_size(product_id, self.cfg.order_size_usd / mid_price)
        if size <= 0:
            self.log.warning("size: too small after snap | product=%s", product_id)
            return
        min_notional_x18 = self.cfg.min_notional_x18_by_product.get(product_id, 0)
        if min_notional_x18 > 0:
            notional_x18 = int(size * mid_price * 1e18)
            if notional_x18 < min_notional_x18:
                self.log.warning(
                    "size: below min_notional | product=%s notional=%.4f min=%.4f",
                    product_id,
                    notional_x18 / 1e18,
                    min_notional_x18 / 1e18,
                )
                return

        # Risk checks
        pos_usd = await self._query_with_retries(self.api.get_position_usd, product_id)
        if abs(pos_usd) >= self.cfg.max_position_size_usd:
            self.log.info("risk: max position reached | product=%s pos_usd=%.4f", product_id, pos_usd)
            return

        open_orders = await self._query_with_retries(self.api.list_open_orders, product_id)
        self.log.info("order-state: product=%s open_orders=%s", product_id, len(open_orders))
        if len(open_orders) > self.cfg.max_open_orders:
            # Cancel oldest orders to get under limit
            order_ids = [_get(o, "id") for o in open_orders if _get(o, "id")]
            await self._query_with_retries(
                self.api.cancel_orders,
                order_ids[: len(open_orders) - self.cfg.max_open_orders],
                product_id,
            )
            self.log.info("orders: trimmed | product=%s open=%s", product_id, len(open_orders))

        # Simple strategy: cancel and replace if price moved enough
        # Always place if no open orders.
        if not open_orders or self._should_requote(last_mid, mid_price):
            order_ids = [_get(o, "id") for o in open_orders if _get(o, "id")]
            if order_ids:
                await self._query_with_retries(self.api.cancel_orders, order_ids, product_id)
            bid_id = ""
            ask_id = ""
            for attempt in range(POST_ONLY_MAX_RETRIES + 1):
                try:
                    bid_id = await self._query_with_retries(self.api.place_limit_order, product_id, True, bid_price, size)
                    ask_id = await self._query_with_retries(self.api.place_limit_order, product_id, False, ask_price, size)
                    break
                except Exception as exc:
                    code = self._extract_error_code(exc)
                    if code == 2006:
                        self.log.warning("health: insufficient account health; attempting risk-off actions | product=%s", product_id)
                        await self._handle_insufficient_health(product_id, mid_price)
                        return
                    if code == 2070:
                        self.log.warning("Market has reached maximum open interest.")
                        return
                    if code == 2008 and attempt < POST_ONLY_MAX_RETRIES:
                        active = await self._query_with_retries(self.api.list_open_orders, product_id)
                        active_ids = [_get(o, "id") for o in active if _get(o, "id")]
                        if active_ids:
                            await self._query_with_retries(self.api.cancel_orders, active_ids, product_id)
                        widen_ticks = POST_ONLY_REPRICE_TICKS * (attempt + 1)
                        bid_retry = self._snap_price(product_id, bid_price - widen_ticks * tick, True)
                        ask_retry = self._snap_price(product_id, ask_price + widen_ticks * tick, False)
                        self.log.info(
                            "orders: post-only reprice | product=%s attempt=%s/%s bid=%.4f->%.4f ask=%.4f->%.4f",
                            product_id,
                            attempt + 1,
                            POST_ONLY_MAX_RETRIES,
                            bid_price,
                            bid_retry,
                            ask_price,
                            ask_retry,
                        )
                        bid_price, ask_price = bid_retry, ask_retry
                        continue
                    if code == 2008:
                        self.log.info("orders: skip cycle due to post-only cross | product=%s", product_id)
                        return
                    raise
            self.log.info(
                "orders: placed | product=%s mid=%.4f bid=%.4f ask=%.4f size=%.6f bid_id=%s ask_id=%s",
                product_id,
                mid_price,
                bid_price,
                ask_price,
                size,
                bid_id,
                ask_id,
            )

    async def _check_daily_loss(self) -> Tuple[bool, float]:
        # Returns (should_stop, realized_pnl_today)
        if datetime.now(timezone.utc).timestamp() - self._start_of_day_ts >= 24 * 3600:
            self._start_of_day_ts = self._day_start_ts()
        pnl = await self._query_with_retries(self.api.get_realized_pnl_usd, self._start_of_day_ts)
        return pnl <= -abs(self.cfg.max_daily_loss_usd), pnl

    async def run(self) -> None:
        self.log.info("bot: start | products=%s", self.cfg.product_ids)
        await self._load_constraints_once()
        while True:
            try:
                self._iteration += 1
                stop_for_loss, pnl_today = await self._check_daily_loss()
                self.log.info("iteration=%s pnl_today_usd=%.4f", self._iteration, pnl_today)
                if stop_for_loss:
                    self.log.warning("risk: max daily loss reached; stopping")
                    return

                now = time.time()
                for product_id in self.cfg.product_ids:
                    prices = await self._query_with_retries(self.api.get_prices, product_id)
                    mid = self._mid_from_prices(prices)
                    if not mid:
                        self.log.warning("market: no mid price | product=%s", product_id)
                        continue

                    if await self._try_take_profit(product_id):
                        continue

                    last_mid = self._last_prices.get(product_id, 0.0)
                    if self._should_requote(last_mid, mid):
                        self._last_prices[product_id] = mid

                    last_refresh = self._last_order_refresh.get(product_id, 0.0)
                    if now - last_refresh >= self.cfg.order_update_interval:
                        cooldown_until = self._health_cooldown_until.get(product_id, 0.0)
                        if now < cooldown_until:
                            continue
                        await self._refresh_orders_for_product(product_id, mid, last_mid)
                        self._last_order_refresh[product_id] = now

                self._consecutive_errors = 0
                await asyncio.sleep(self.cfg.price_check_interval)
            except Exception as exc:
                self._consecutive_errors += 1
                self.log.exception("error: %s", exc)
                if self._consecutive_errors >= self.cfg.max_consecutive_errors:
                    if self.cfg.restart_on_failure:
                        self.log.warning("bot: restarting after consecutive errors")
                        await asyncio.sleep(self.cfg.restart_delay_seconds)
                        self._consecutive_errors = 0
                        continue
                    raise


def _load_client() -> Any:
    """Create Nado SDK client from env vars.

    Required:
      - NADO_PRIVATE_KEY
    Optional:
      - NADO_NETWORK (e.g. mainnet/testnet if supported by your SDK)
    """
    from nado_protocol.client import create_nado_client, NadoClientMode

    env = _load_env_file(".env")
    private_key = env.get("NADO_PRIVATE_KEY")
    if not private_key:
        raise SystemExit("NADO_PRIVATE_KEY is required in .env")
    private_key = private_key.strip()
    if private_key.startswith("0x"):
        # normalize for libraries that expect raw hex
        private_key = private_key[2:]

    # SDK supports string network names or NadoClientMode.
    network = env.get("NADO_NETWORK", "").strip()
    if network:
        return create_nado_client(network, private_key)
    return create_nado_client(NadoClientMode.MAINNET, private_key)


def main() -> None:
    _setup_logger()
    cfg = Config()
    client = _load_client()
    api = NadoApiAdapter(client)
    bot = MarketMakerBot(cfg, api)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
