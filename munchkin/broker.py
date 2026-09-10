"""Thin, plain-python wrapper over the Alpaca trading API.

No risk logic lives here: see risk.py. Everything returned is JSON-friendly.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (AssetStatus, ContractType, OrderClass, OrderSide, OrderType,
                                  PositionIntent, QueryOrderStatus, TimeInForce)
from alpaca.trading.requests import (ClosePositionRequest, GetCalendarRequest, GetOptionContractsRequest,
                                     GetOrdersRequest, GetPortfolioHistoryRequest, LimitOrderRequest,
                                     MarketOrderRequest, OptionLegRequest, StopLimitOrderRequest, StopOrderRequest)

from .config import alpaca_credentials
from .util import ET, to_plain

log = logging.getLogger("munchkin.broker")


class Broker:
    def __init__(self) -> None:
        key, secret, paper = alpaca_credentials()
        self.paper = paper
        self.tc = TradingClient(key, secret, paper=paper)

    # ------------------------------------------------------------------ account
    def account(self) -> dict[str, Any]:
        return to_plain(self.tc.get_account())

    def clock(self) -> dict[str, Any]:
        return to_plain(self.tc.get_clock())

    def calendar(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        return to_plain(self.tc.get_calendar(GetCalendarRequest(start=start, end=end)))

    def is_trading_day(self, d: dt.date) -> bool:
        return any(c["date"] == d.isoformat() for c in self.calendar(d, d))

    def portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> dict[str, Any]:
        return to_plain(self.tc.get_portfolio_history(GetPortfolioHistoryRequest(period=period, timeframe=timeframe)))

    # ------------------------------------------------------------------ positions / orders
    def positions(self) -> list[dict[str, Any]]:
        return to_plain(self.tc.get_all_positions())

    def open_orders(self) -> list[dict[str, Any]]:
        return to_plain(self.tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=200)))

    def closed_orders(self, after: dt.datetime | None = None, limit: int = 200) -> list[dict[str, Any]]:
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, nested=True, limit=limit, after=after)
        return to_plain(self.tc.get_orders(req))

    def order(self, order_id: str) -> dict[str, Any]:
        return to_plain(self.tc.get_order_by_id(order_id))

    def cancel_order(self, order_id: str) -> None:
        self.tc.cancel_order_by_id(order_id)

    def fills(self, after: dt.datetime | None = None, page_size: int = 100) -> list[dict[str, Any]]:
        """Trade fills from the account activities endpoint (not wrapped by alpaca-py)."""
        params: dict[str, Any] = {"page_size": page_size, "direction": "asc"}
        if after is not None:
            params["after"] = after.astimezone(dt.timezone.utc).isoformat()
        out: list[dict[str, Any]] = []
        page_token = None
        for _ in range(20):
            if page_token:
                params["page_token"] = page_token
            data = self.tc.get("/account/activities/FILL", params)
            if not data:
                break
            out.extend(data)
            if len(data) < page_size:
                break
            page_token = data[-1].get("id")
        return out

    # ------------------------------------------------------------------ order submission
    def submit_stock_order(self, symbol: str, side: str, qty: float | None = None, notional: float | None = None,
                           order_type: str = "market", limit_price: float | None = None,
                           tif: str = "day", extended_hours: bool = False,
                           client_order_id: str | None = None) -> dict[str, Any]:
        s = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        t = TimeInForce(tif.lower())
        kwargs: dict[str, Any] = dict(symbol=symbol.upper(), side=s, time_in_force=t, extended_hours=extended_hours,
                                      client_order_id=client_order_id)
        if qty is not None:
            kwargs["qty"] = qty
        else:
            kwargs["notional"] = round(float(notional), 2)
        if order_type == "limit":
            req = LimitOrderRequest(limit_price=round(float(limit_price), 2), **kwargs)
        else:
            req = MarketOrderRequest(**kwargs)
        return to_plain(self.tc.submit_order(req))

    def submit_option_order(self, symbol: str, side: str, qty: int, limit_price: float | None,
                            position_intent: str, client_order_id: str | None = None) -> dict[str, Any]:
        s = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        kwargs: dict[str, Any] = dict(symbol=symbol.upper(), qty=int(qty), side=s, time_in_force=TimeInForce.DAY,
                                      position_intent=PositionIntent(position_intent), client_order_id=client_order_id)
        if limit_price is not None:
            req = LimitOrderRequest(limit_price=round(float(limit_price), 2), **kwargs)
        else:
            req = MarketOrderRequest(**kwargs)
        return to_plain(self.tc.submit_order(req))

    def submit_mleg_order(self, legs: list[dict[str, Any]], qty: int, limit_price: float,
                          client_order_id: str | None = None) -> dict[str, Any]:
        """Multi-leg options order. limit_price is the net debit (positive) or credit (negative)."""
        leg_objs = [OptionLegRequest(symbol=l["symbol"].upper(), ratio_qty=int(l.get("ratio_qty", 1)),
                                     side=OrderSide(l["side"].lower()), position_intent=PositionIntent(l["position_intent"]))
                    for l in legs]
        req = LimitOrderRequest(qty=int(qty), order_class=OrderClass.MLEG, legs=leg_objs,
                                limit_price=round(float(limit_price), 2), time_in_force=TimeInForce.DAY,
                                client_order_id=client_order_id)
        return to_plain(self.tc.submit_order(req))

    def submit_stop_order(self, symbol: str, qty: float, stop_price: float, tif: str = "day",
                          limit_price: float | None = None, is_option: bool = False,
                          client_order_id: str | None = None) -> dict[str, Any]:
        """Protective sell stop (stop-market, or stop-limit when limit_price is given)."""
        kwargs: dict[str, Any] = dict(symbol=symbol.upper(), qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce(tif.lower()),
                                      stop_price=round(float(stop_price), 2), client_order_id=client_order_id)
        if is_option:
            kwargs["qty"] = int(qty)
            kwargs["position_intent"] = PositionIntent.SELL_TO_CLOSE
        if limit_price is not None:
            req = StopLimitOrderRequest(limit_price=round(float(limit_price), 2), **kwargs)
        else:
            req = StopOrderRequest(**kwargs)
        return to_plain(self.tc.submit_order(req))

    def cancel_orders_for(self, symbol: str, side: str | None = "sell", open_orders: list[dict[str, Any]] | None = None) -> int:
        n = 0
        for o in (open_orders if open_orders is not None else self.open_orders()):
            if o.get("symbol") == symbol.upper() and (side is None or str(o.get("side")) == side):
                try:
                    self.tc.cancel_order_by_id(o["id"])
                    n += 1
                except Exception as e:  # already filled/cancelled
                    log.warning("cancel %s failed: %s", o["id"], e)
        return n

    def close_position(self, symbol: str, qty: float | None = None, percentage: float | None = None) -> dict[str, Any]:
        req = None
        if qty is not None:
            req = ClosePositionRequest(qty=str(qty))
        elif percentage is not None:
            req = ClosePositionRequest(percentage=str(percentage))
        return to_plain(self.tc.close_position(symbol.upper(), req))

    # ------------------------------------------------------------------ options contracts
    def option_contracts(self, underlying: str, exp_gte: dt.date | None = None, exp_lte: dt.date | None = None,
                         contract_type: str | None = None, strike_gte: float | None = None,
                         strike_lte: float | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying.upper()], status=AssetStatus.ACTIVE,
            expiration_date_gte=exp_gte, expiration_date_lte=exp_lte,
            type=ContractType(contract_type) if contract_type else None,
            strike_price_gte=str(strike_gte) if strike_gte is not None else None,
            strike_price_lte=str(strike_lte) if strike_lte is not None else None,
            limit=min(limit, 10000),
        )
        out: list[dict[str, Any]] = []
        for _ in range(10):
            res = self.tc.get_option_contracts(req)
            out.extend(to_plain(res.option_contracts or []))
            if not res.next_page_token or len(out) >= limit:
                break
            req.page_token = res.next_page_token
        return out

    def do_not_exercise(self, symbol: str) -> dict[str, Any]:
        """File a do-not-exercise instruction (accepted only on the contract's expiration day, before the close)."""
        return to_plain(self.tc.post(f"/positions/{symbol.upper()}/do-not-exercise", {}) or {"ok": True})

    def option_contract(self, symbol: str) -> dict[str, Any]:
        return to_plain(self.tc.get_option_contract(symbol.upper()))

    def asset(self, symbol: str) -> dict[str, Any]:
        return to_plain(self.tc.get_asset(symbol.upper()))
