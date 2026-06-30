"""
check_sdk.py — TastyTrade SDK Diagnostic Tool
Prints all model fields, method signatures, and module contents.
Run: python check_sdk.py
"""

import inspect
import pkgutil

print("=" * 60)
print("  TastyTrade SDK Diagnostic")
print("=" * 60)

# ── Version & top-level ───────────────────────────────────────
import tastytrade
print(f"\nVersion: {getattr(tastytrade, '__version__', 'unknown')}")
print(f"Top-level: {[x for x in dir(tastytrade) if not x.startswith('_')]}")

print("\nModules:")
for importer, modname, ispkg in pkgutil.walk_packages(
    path=tastytrade.__path__,
    prefix=tastytrade.__name__ + '.',
    onerror=lambda x: None
):
    print(f"  {modname}")

# ── Session ───────────────────────────────────────────────────
print("\n" + "=" * 60)
print("  SESSION")
print("=" * 60)
from tastytrade import Session
print(f"Session methods: {[x for x in dir(Session) if not x.startswith('_')]}")

# ── Instruments ───────────────────────────────────────────────
print("\n" + "=" * 60)
print("  INSTRUMENTS")
print("=" * 60)
from tastytrade.instruments import (
    Option, NestedOptionChain, NestedOptionChainExpiration,
    Strike, get_option_chain
)
print(f"Option fields:                    {list(Option.model_fields.keys())}")
print(f"Strike fields:                    {list(Strike.model_fields.keys())}")
print(f"NestedOptionChain fields:         {list(NestedOptionChain.model_fields.keys())}")
print(f"NestedOptionChainExpiration fields:{list(NestedOptionChainExpiration.model_fields.keys())}")
print(f"get_option_chain signature:       {inspect.signature(get_option_chain)}")

# ── DXFeed Events ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("  DXFEED EVENTS")
print("=" * 60)
from tastytrade.dxfeed import Candle, Quote, Greeks
print(f"Candle fields:  {list(Candle.model_fields.keys())}")
print(f"Quote fields:   {list(Quote.model_fields.keys())}")
print(f"Greeks fields:  {list(Greeks.model_fields.keys())}")

# ── DXLinkStreamer ────────────────────────────────────────────
print("\n" + "=" * 60)
print("  DXLINKSTREAMER")
print("=" * 60)
from tastytrade import DXLinkStreamer
print(f"Methods: {[x for x in dir(DXLinkStreamer) if not x.startswith('_')]}")
print(f"__init__:         {inspect.signature(DXLinkStreamer.__init__)}")
print(f"subscribe:        {inspect.signature(DXLinkStreamer.subscribe)}")
print(f"subscribe_candle: {inspect.signature(DXLinkStreamer.subscribe_candle)}")
print(f"get_event:        {inspect.signature(DXLinkStreamer.get_event)}")
print(f"listen:           {inspect.signature(DXLinkStreamer.listen)}")

# ── Orders ────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("  ORDERS")
print("=" * 60)
from tastytrade.order import (
    NewOrder, Leg, OrderAction, OrderType,
    OrderTimeInForce, PriceEffect, InstrumentType,
    PlacedOrder, PlacedOrderResponse
)
print(f"NewOrder fields:          {list(NewOrder.model_fields.keys())}")
print(f"Leg fields:               {list(Leg.model_fields.keys())}")
print(f"PlacedOrder fields:       {list(PlacedOrder.model_fields.keys())}")
print(f"PlacedOrderResponse:      {list(PlacedOrderResponse.model_fields.keys())}")
print(f"OrderAction values:       {list(OrderAction)}")
print(f"OrderType values:         {list(OrderType)}")
print(f"OrderTimeInForce values:  {list(OrderTimeInForce)}")
print(f"PriceEffect values:       {list(PriceEffect)}")
print(f"InstrumentType values:    {list(InstrumentType)}")

# ── Account ───────────────────────────────────────────────────
print("\n" + "=" * 60)
print("  ACCOUNT")
print("=" * 60)
from tastytrade import Account
from tastytrade.account import CurrentPosition
print(f"Account methods:          {[x for x in dir(Account) if not x.startswith('_')]}")
print(f"Account.get:              {inspect.signature(Account.get)}")
print(f"Account.place_order:      {inspect.signature(Account.place_order)}")
print(f"Account.get_positions:    {inspect.signature(Account.get_positions)}")
print(f"Account.get_history:      {inspect.signature(Account.get_history)}")
print(f"CurrentPosition fields:   {list(CurrentPosition.model_fields.keys())}")

# ── Market Data ───────────────────────────────────────────────
print("\n" + "=" * 60)
print("  MARKET DATA")
print("=" * 60)
from tastytrade.market_data import get_market_data, get_market_data_by_type, MarketData
print(f"MarketData fields:            {list(MarketData.model_fields.keys())}")
print(f"get_market_data:              {inspect.signature(get_market_data)}")
print(f"get_market_data_by_type:      {inspect.signature(get_market_data_by_type)}")

# ── Metrics ───────────────────────────────────────────────────
print("\n" + "=" * 60)
print("  METRICS")
print("=" * 60)
from tastytrade.metrics import get_market_metrics, MarketMetricInfo
print(f"MarketMetricInfo fields:  {list(MarketMetricInfo.model_fields.keys())}")
print(f"get_market_metrics:       {inspect.signature(get_market_metrics)}")

# ── Market Sessions ───────────────────────────────────────────
print("\n" + "=" * 60)
print("  MARKET SESSIONS")
print("=" * 60)
import tastytrade.market_sessions as ms
print(f"get_market_sessions:  {inspect.signature(ms.get_market_sessions)}")

# ── Utils & Search ────────────────────────────────────────────
print("\n" + "=" * 60)
print("  UTILS & SEARCH")
print("=" * 60)
import tastytrade.utils as utils
import tastytrade.search as search
print(f"Utils:  {[x for x in dir(utils) if not x.startswith('_')]}")
print(f"Search: {[x for x in dir(search) if not x.startswith('_')]}")

print("\n" + "=" * 60)
print("  DONE")
print("=" * 60)