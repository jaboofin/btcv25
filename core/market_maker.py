"""
╔══════════════════════════════════════════════════════════════════╗
║  MARKET MAKER — Post-only quoting engine for BTC short-term      ║
║                                                                    ║
║  Phase 4: Zero-fee limit orders + daily USDC maker rebates        ║
║                                                                    ║
║  Places resting bids on YES and NO tokens around the Chainlink-  ║
║  derived mid price. All orders are post-only (rejected if they    ║
║  would immediately fill), guaranteeing maker status. Revenue      ║
║  comes from capturing the bid-ask spread + daily rebate pool.     ║
║                                                                    ║
║  Runs as an independent async loop alongside directional + arb.   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import time
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from config.settings import MarketMakerConfig

logger = logging.getLogger("market_maker")


# ── Types ────────────────────────────────────────────────────────

@dataclass
class ActiveQuote:
    """Tracks a single posted maker order."""
    order_id: str
    token_id: str
    condition_id: str
    side: str           # "BUY_YES" or "BUY_NO"
    price: float
    size: float
    posted_at: float
    timeframe: str


@dataclass
class MMStats:
    """Daily market making statistics."""
    quotes_posted: int = 0
    quotes_filled: int = 0
    quotes_cancelled: int = 0
    quotes_rejected: int = 0
    total_fills_usd: float = 0.0
    yes_fills_usd: float = 0.0
    no_fills_usd: float = 0.0
    skipped_for_imbalance: int = 0
    skipped_extreme_price: int = 0


class MarketMaker:
    """
    Conservative market making loop.

    Key safety features:
    - Only 1 market at a time (configurable)
    - Tracks resting order value against available capital
    - Fill detection excludes orders we cancelled ourselves
    - Imbalance protection pauses heavy side
    - Pre-close pull cancels before resolution
    - Hard cap on daily fill budget
    """

    def __init__(self, config: MarketMakerConfig, polymarket_client):
        self.config = config
        self.polymarket = polymarket_client
        self._running = False
        self._active_quotes: list[ActiveQuote] = []
        self._stats = MMStats()
        self._day_start = time.time()
        self._cycle_count = 0
        self._known_markets: dict[str, dict] = {}

        # ── Fill / imbalance tracking ──
        self._daily_fills_usd = 0.0
        self._yes_fills_usd = 0.0
        self._no_fills_usd = 0.0
        self._max_imbalance_usd = config.max_inventory_imbalance

        # ── Cancelled order tracking (prevents false fill detection) ──
        self._cancelled_order_ids: set = set()

    # ── Helpers ───────────────────────────────────────────────────

    def _resting_order_value(self) -> float:
        """Total USD locked in resting maker orders."""
        return sum(q.price * q.size for q in self._active_quotes)

    def _get_imbalance(self) -> tuple[float, str]:
        diff = self._yes_fills_usd - self._no_fills_usd
        if diff > 0:
            return diff, "YES"
        elif diff < 0:
            return abs(diff), "NO"
        return 0.0, "BALANCED"

    # ── Fill Detection ───────────────────────────────────────────

    async def _detect_fills(self):
        """
        Compare active_quotes against CLOB open orders.
        Orders we cancelled ourselves are excluded from fill detection.
        """
        if not self._active_quotes:
            return

        by_market: dict[str, list[ActiveQuote]] = {}
        for q in self._active_quotes:
            by_market.setdefault(q.condition_id, []).append(q)

        filled_ids = set()

        for condition_id, quotes in by_market.items():
            try:
                open_orders = self.polymarket.get_open_orders_for_market(condition_id)
                open_ids = set()
                for o in open_orders:
                    if isinstance(o, dict):
                        open_ids.add(o.get("id", o.get("orderID", "")))
                    else:
                        open_ids.add(getattr(o, "id", getattr(o, "order_id", "")))

                for q in quotes:
                    if not q.order_id:
                        continue
                    if q.order_id in open_ids:
                        continue  # Still resting — not filled
                    if q.order_id in self._cancelled_order_ids:
                        continue  # We cancelled this — not a fill

                    # Genuinely filled
                    fill_usd = q.price * q.size
                    filled_ids.add(q.order_id)
                    self._stats.quotes_filled += 1
                    self._stats.total_fills_usd += fill_usd
                    self._daily_fills_usd += fill_usd

                    if "YES" in q.side:
                        self._yes_fills_usd += fill_usd
                        self._stats.yes_fills_usd += fill_usd
                    else:
                        self._no_fills_usd += fill_usd
                        self._stats.no_fills_usd += fill_usd

                    imbalance = abs(self._yes_fills_usd - self._no_fills_usd)
                    logger.info(
                        f"📗 MM FILL: {q.side} {q.size:.1f}@{q.price:.4f} "
                        f"(${fill_usd:.2f}) [{q.timeframe}] | "
                        f"YES=${self._yes_fills_usd:.2f} NO=${self._no_fills_usd:.2f} "
                        f"imbal=${imbalance:.2f}"
                    )

            except Exception as e:
                logger.debug(f"Fill detection error for {condition_id[:12]}: {e}")

        # Remove filled quotes from active list
        if filled_ids:
            self._active_quotes = [q for q in self._active_quotes if q.order_id not in filled_ids]

    # ── Market Discovery ─────────────────────────────────────────

    async def _discover_markets(self) -> list[dict]:
        try:
            markets = await self.polymarket.discover_markets()
            tradeable = []
            for m in markets:
                if not m.is_tradeable:
                    continue
                if m.liquidity < self.config.order_size_usd:
                    continue
                tf = self._parse_timeframe(m.slug)
                if tf and tf in self.config.timeframes:
                    tradeable.append({
                        "market": m,
                        "timeframe": tf,
                        "condition_id": m.condition_id,
                        "token_yes": m.token_id_up,
                        "token_no": m.token_id_down,
                        "end_date": getattr(m, "end_date", ""),
                    })
            return tradeable
        except Exception as e:
            logger.error(f"MM discovery error: {e}")
            return []

    @staticmethod
    def _parse_timeframe(slug: str) -> Optional[str]:
        if not slug:
            return None
        m = re.search(r"btc-updown-(\d+)(m|h)-", slug.lower())
        if m:
            qty = int(m.group(1))
            unit = m.group(2)
            return f"{qty}{'h' if unit == 'h' else 'm'}"
        return None

    @staticmethod
    def _parse_end_time(end_date: str) -> Optional[float]:
        if not end_date:
            return None
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            return dt.timestamp()
        except:
            return None

    # ── Pricing ──────────────────────────────────────────────────

    def _calculate_quotes(self, mid_price: float) -> list[dict]:
        """
        Generate quote levels around the mid price.
        Only returns quotes with prices in the safe 0.10-0.90 range.
        """
        quotes = []
        half_spread = self.config.spread_bps / 10000.0
        level_step = self.config.level_spacing_bps / 10000.0

        for level in range(self.config.num_levels):
            offset = half_spread + (level * level_step)

            yes_bid = round(mid_price - offset, 2)
            no_bid = round((1.0 - mid_price) - offset, 2)

            # Hard price filter — skip extreme prices
            for side, price in [("YES", yes_bid), ("NO", no_bid)]:
                if price < 0.25 or price > 0.75:
                    self._stats.skipped_extreme_price += 1
                    continue

                shares = round(self.config.order_size_usd / price, 1)
                if shares < 5:
                    shares = 5.0

                quotes.append({
                    "side": side,
                    "price": price,
                    "size": shares,
                    "level": level,
                })

        return quotes

    # ── Order Management ─────────────────────────────────────────

    async def _cancel_all_for_market(self, condition_id: str):
        """Cancel all our open maker orders for a market. Track IDs to avoid false fill detection."""
        # Record order IDs as cancelled BEFORE cancelling
        for q in self._active_quotes:
            if q.condition_id == condition_id and q.order_id:
                self._cancelled_order_ids.add(q.order_id)

        cancelled = len([q for q in self._active_quotes if q.condition_id == condition_id])

        try:
            self.polymarket.cancel_market_orders(condition_id)
        except Exception as e:
            logger.error(f"Cancel market orders error: {e}")

        self._active_quotes = [q for q in self._active_quotes if q.condition_id != condition_id]
        self._stats.quotes_cancelled += cancelled

        # Keep cancelled set from growing forever — prune old entries
        if len(self._cancelled_order_ids) > 500:
            self._cancelled_order_ids = set(list(self._cancelled_order_ids)[-200:])

        return cancelled

    async def _post_quotes(self, market_info: dict, mid_price: float):
        """Post post-only maker orders for a single market with safety checks."""
        condition_id = market_info["condition_id"]
        token_yes = market_info["token_yes"]
        token_no = market_info["token_no"]
        tf = market_info["timeframe"]

        # ── Budget gate: based on actual fills ──
        if self._daily_fills_usd >= self.config.max_daily_budget:
            if self._cycle_count % 10 == 0:
                logger.info(f"📘 MM budget exhausted: ${self._daily_fills_usd:.2f}/${self.config.max_daily_budget}")
            return

        # ── Open order limit ──
        if len(self._active_quotes) >= self.config.max_open_orders:
            return

        # ── Imbalance check ──
        imbalance_usd, heavy_side = self._get_imbalance()
        skip_side = None
        if imbalance_usd >= self._max_imbalance_usd:
            skip_side = heavy_side
            self._stats.skipped_for_imbalance += 1
            if self._cycle_count % 5 == 0:
                logger.info(
                    f"📘 MM imbalance: {heavy_side} heavy by ${imbalance_usd:.2f} — "
                    f"skipping {heavy_side} quotes"
                )

        # ── Get fee rate for signing ──
        fee_bps = await self.polymarket.get_fee_rate_bps(token_yes) or 0

        # ── Generate and post quotes ──
        quotes = self._calculate_quotes(mid_price)

        for q in quotes:
            if len(self._active_quotes) >= self.config.max_open_orders:
                break

            # Skip heavy side when imbalanced
            if skip_side and q["side"] == skip_side:
                continue

            token_id = token_yes if q["side"] == "YES" else token_no
            size_usd = q["price"] * q["size"]

            resp = self.polymarket.place_maker_order(
                token_id=token_id,
                price=q["price"],
                size=q["size"],
                side="BUY",
                fee_bps=fee_bps,
            )

            if resp:
                order_id = resp.get("orderID", "")
                self._active_quotes.append(ActiveQuote(
                    order_id=order_id,
                    token_id=token_id,
                    condition_id=condition_id,
                    side=f"BUY_{q['side']}",
                    price=q["price"],
                    size=q["size"],
                    posted_at=time.time(),
                    timeframe=tf,
                ))
                self._stats.quotes_posted += 1
                logger.info(
                    f"📘 MM quote: BUY {q['side']} {q['size']:.1f}@{q['price']:.4f} "
                    f"(${size_usd:.2f}) [{tf}]"
                )
            else:
                self._stats.quotes_rejected += 1

    # ── Pre-Close Pull ───────────────────────────────────────────

    async def _pull_expiring_quotes(self):
        now = time.time()
        pulled_conditions = set()

        for q in list(self._active_quotes):
            mkt_info = self._known_markets.get(q.condition_id)
            if not mkt_info:
                continue

            end_time = self._parse_end_time(mkt_info.get("end_date", ""))
            if end_time is None:
                continue

            time_to_close = end_time - now
            if time_to_close <= self.config.pull_before_close_secs and q.condition_id not in pulled_conditions:
                logger.info(
                    f"⏫ MM pulling quotes on {q.timeframe} market "
                    f"({time_to_close:.0f}s to close)"
                )
                await self._cancel_all_for_market(q.condition_id)
                pulled_conditions.add(q.condition_id)
                self._stats.pulls_before_close += 1

    # ── Daily Reset ──────────────────────────────────────────────

    def _check_daily_reset(self):
        if time.time() - self._day_start > 86400:
            logger.info(
                f"📅 MM daily reset — posted={self._stats.quotes_posted} "
                f"filled={self._stats.quotes_filled} "
                f"YES=${self._stats.yes_fills_usd:.2f} NO=${self._stats.no_fills_usd:.2f} "
                f"skipped_imbal={self._stats.skipped_for_imbalance}"
            )
            self._stats = MMStats()
            self._daily_fills_usd = 0.0
            self._yes_fills_usd = 0.0
            self._no_fills_usd = 0.0
            self._cancelled_order_ids.clear()
            self._day_start = time.time()

    # ── Main Loop ────────────────────────────────────────────────

    async def run(self):
        logger.info(
            f"📘 Market maker started | spread={self.config.spread_bps}bps | "
            f"size=${self.config.order_size_usd}/side | levels={self.config.num_levels} | "
            f"refresh={self.config.refresh_secs}s | budget=${self.config.max_daily_budget}/day | "
            f"max_imbalance=${self._max_imbalance_usd}"
        )
        self._running = True

        while self._running:
            try:
                self._check_daily_reset()
                self._cycle_count += 1

                # 1. Detect fills (before cancelling anything)
                await self._detect_fills()

                # 2. Pull quotes near expiry
                await self._pull_expiring_quotes()

                # 3. Discover markets
                markets = await self._discover_markets()
                if not markets:
                    if self._cycle_count % 20 == 0:
                        logger.debug("MM: No tradeable markets")
                    await asyncio.sleep(self.config.refresh_secs)
                    continue

                # Cache for pre-close pull
                for m in markets:
                    self._known_markets[m["condition_id"]] = m

                # Pick ONLY the single most liquid market
                markets.sort(key=lambda m: m["market"].liquidity, reverse=True)
                markets = markets[:1]

                # 4. For the chosen market: cancel ALL old quotes first, get mid, re-quote
                for mkt_info in markets:
                    condition_id = mkt_info["condition_id"]
                    token_yes = mkt_info["token_yes"]

                    # Don't quote if too close to resolution
                    end_time = self._parse_end_time(mkt_info.get("end_date", ""))
                    if end_time:
                        time_to_close = end_time - time.time()
                        if time_to_close <= self.config.pull_before_close_secs:
                            continue

                    # Cancel ALL active quotes (any market) before posting new ones
                    # This prevents stale quotes accumulating when the top market changes
                    if self._active_quotes:
                        stale_conditions = set(q.condition_id for q in self._active_quotes)
                        for cid in stale_conditions:
                            await self._cancel_all_for_market(cid)

                    # Get mid price — only quote balanced markets
                    mid = self.polymarket.get_midpoint(token_yes)
                    if mid is None or mid <= 0.35 or mid >= 0.65:
                        if self._cycle_count % 10 == 0:
                            logger.info(f"📘 MM: Skipping market mid={mid:.3f} (too lopsided)")
                        continue

                    # Post new quotes
                    await self._post_quotes(mkt_info, mid)

                # Periodic status
                if self._cycle_count % 10 == 0:
                    active = len(self._active_quotes)
                    resting = self._resting_order_value()
                    imbalance_usd, heavy_side = self._get_imbalance()
                    logger.info(
                        f"📘 MM cycle {self._cycle_count} | "
                        f"{active} quotes (${resting:.2f} resting) | "
                        f"filled={self._stats.quotes_filled} "
                        f"rejected={self._stats.quotes_rejected} | "
                        f"YES=${self._yes_fills_usd:.2f} NO=${self._no_fills_usd:.2f} "
                        f"imbal={heavy_side}${imbalance_usd:.2f} | "
                        f"fills ${self._daily_fills_usd:.2f}/${self.config.max_daily_budget}"
                    )

            except Exception as e:
                logger.error(f"MM cycle error: {e}", exc_info=True)

            await asyncio.sleep(self.config.refresh_secs)

    def stop(self):
        self._running = False
        logger.info("Market maker stopping...")
        try:
            self.polymarket.cancel_all_orders()
        except:
            pass

    def get_stats(self) -> dict:
        imbalance_usd, heavy_side = self._get_imbalance()
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "active_quotes": len(self._active_quotes),
            "resting_usd": round(self._resting_order_value(), 2),
            "quotes_posted": self._stats.quotes_posted,
            "quotes_filled": self._stats.quotes_filled,
            "quotes_rejected": self._stats.quotes_rejected,
            "quotes_cancelled": self._stats.quotes_cancelled,
            "yes_fills_usd": round(self._yes_fills_usd, 2),
            "no_fills_usd": round(self._no_fills_usd, 2),
            "imbalance": f"{heavy_side}${imbalance_usd:.2f}",
            "skipped_for_imbalance": self._stats.skipped_for_imbalance,
            "daily_fills_usd": round(self._daily_fills_usd, 2),
            "daily_budget_max": self.config.max_daily_budget,
            "spread_bps": self.config.spread_bps,
            "order_size_usd": self.config.order_size_usd,
        }
