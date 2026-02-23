"""
╔══════════════════════════════════════════════════════════════════╗
║  ARB SCANNER — Independent Fast-Polling Arbitrage Engine          ║
║                                                                    ║
║  Discovers BTC up/down via /events/slug/{deterministic_slug}       ║
║  Parses clobTokenIds + outcomePrices (JSON strings in response)    ║
║  Fallback: pagination scan on /events endpoint                     ║
║                                                                    ║
║  Activated via: python bot.py --arb  /  python bot.py --arb-only   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json as _json
import re
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger("arb_scanner")

SLUG_PATTERN = re.compile(r'^btc-updown-(\d+m|\d+h)-(\d+)$')
TIMEFRAME_LABELS = {"5m": "5-Min", "15m": "15-Min", "30m": "30-Min", "1h": "1-Hour"}
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


# ── Helpers ──────────────────────────────────────────────────────

def _safe_json(val):
    """Parse a value that might be a JSON string, list, or None."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return _json.loads(val)
        except Exception:
            return []
    return []


def _parse_market_from_event(event: dict, slug: str, timeframe: str) -> Optional['ArbMarket']:
    """
    Extract tradeable market from a Gamma event response.

    Key finding from API testing:
      - tokens[] array is EMPTY on these markets
      - clobTokenIds is a JSON STRING like '["tokenid1","tokenid2"]'
      - outcomePrices is a JSON STRING like '["0.515","0.485"]'
    """
    if not event:
        return None

    markets = event.get("markets", [])
    if not markets:
        return None

    m = markets[0]
    cid = m.get("conditionId", m.get("id", ""))
    if not cid:
        return None

    # Parse clobTokenIds (JSON string → list)
    token_ids = _safe_json(m.get("clobTokenIds"))
    if len(token_ids) < 2:
        # Fallback: try tokens array
        tokens = m.get("tokens", [])
        if len(tokens) >= 2:
            token_ids = [
                tokens[0].get("token_id", tokens[0].get("tokenId", "")),
                tokens[1].get("token_id", tokens[1].get("tokenId", "")),
            ]
        else:
            return None

    # Parse outcomePrices (JSON string → list of floats)
    raw_prices = _safe_json(m.get("outcomePrices"))
    if len(raw_prices) >= 2:
        try:
            price_yes = float(raw_prices[0])
            price_no = float(raw_prices[1])
        except (ValueError, TypeError):
            price_yes, price_no = 0.5, 0.5
    else:
        # Fallback: tokens array prices
        tokens = m.get("tokens", [])
        if len(tokens) >= 2:
            price_yes = float(tokens[0].get("price", 0.5))
            price_no = float(tokens[1].get("price", 0.5))
        else:
            price_yes, price_no = 0.5, 0.5

    return ArbMarket(
        condition_id=cid,
        question=m.get("question", event.get("title", "")),
        slug=slug,
        token_id_yes=str(token_ids[0]),
        token_id_no=str(token_ids[1]),
        price_yes=price_yes,
        price_no=price_no,
        liquidity=float(m.get("liquidityClob", m.get("liquidityNum", 0))),
        end_date=m.get("endDate", event.get("endDate", "")),
        timeframe=timeframe,
        volume=float(m.get("volumeNum", m.get("volume", 0))),
        last_refreshed=time.time(),
    )


# ── Data Models ──────────────────────────────────────────────────

@dataclass
class ArbMarket:
    condition_id: str
    question: str
    slug: str
    token_id_yes: str
    token_id_no: str
    price_yes: float
    price_no: float
    liquidity: float
    end_date: str
    timeframe: str
    volume: float = 0.0
    last_refreshed: float = 0.0

    @property
    def combined(self) -> float:
        return self.price_yes + self.price_no

    @property
    def edge_pct(self) -> float:
        return (1.0 - self.combined) * 100 if self.combined < 1.0 else 0.0

    @property
    def is_arb(self) -> bool:
        return self.combined > 0 and self.combined < 1.0

    @property
    def end_ts(self) -> float:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(self.end_date.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0

    @property
    def time_remaining_secs(self) -> float:
        return max(0, self.end_ts - time.time())


@dataclass
class ArbExecution:
    timestamp: float
    condition_id: str
    question: str
    timeframe: str
    price_yes: float
    price_no: float
    combined: float
    edge_pct: float
    size_per_side: float
    guaranteed_profit: float
    order_id_yes: Optional[str] = None
    order_id_no: Optional[str] = None
    status: str = "pending"


@dataclass
class ArbScannerConfig:
    poll_interval_secs: float = 8.0
    arb_threshold: float = 0.98
    min_edge_pct: float = 1.0
    size_per_side_usd: float = 10.0
    max_daily_arb_trades: int = 50
    max_daily_arb_budget: float = 200.0
    min_liquidity_usd: float = 0.0
    cooldown_per_market_secs: float = 120.0
    scan_timeframes: list = field(default_factory=lambda: ["5m", "15m", "30m", "1h"])


class ArbScanner:
    def __init__(self, config: ArbScannerConfig, polymarket_client=None):
        self.config = config
        self.polymarket = polymarket_client
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._known_markets: dict[str, ArbMarket] = {}
        self._expired_markets: dict[str, ArbMarket] = {}
        self._executions: list[ArbExecution] = []
        self._cooldowns: dict[str, float] = {}
        self._daily_trades = 0
        self._daily_spent = 0.0
        self._daily_profit = 0.0
        self._day_start = 0.0
        self._scan_count = 0
        self._last_discovery = 0.0
        self._last_scan_time_ms = 0.0
        self._near_misses: list[dict] = []
        self._best_edge_seen: float = 0.0
        self._total_markets_discovered = 0
        self._consecutive_errors: int = 0
        self._backoff_until: float = 0.0
        self._discovery_interval = 45.0
        self._gamma_url = "https://gamma-api.polymarket.com"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def _close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Deterministic Slug Generation ────────────────────────────

    @staticmethod
    def _generate_slugs(timeframes: list, offsets: list = None) -> list[tuple[str, str]]:
        if offsets is None:
            offsets = [-1, 0, 1, 2]
        now = int(time.time())
        slugs = []
        for tf in timeframes:
            secs = TIMEFRAME_SECONDS.get(tf)
            if not secs:
                continue
            for offset in offsets:
                window_ts = (now // secs + offset) * secs
                if window_ts > 0:
                    slugs.append((f"btc-updown-{tf}-{window_ts}", tf))
        return slugs

    # ── Event Slug Lookup (PRIMARY) ──────────────────────────────

    async def _fetch_event_by_slug(self, slug: str, timeframe: str) -> Optional[ArbMarket]:
        """GET /events/slug/{slug} → parse clobTokenIds + outcomePrices."""
        try:
            session = await self._get_session()
            url = f"{self._gamma_url}/events/slug/{slug}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                event = await resp.json()
            return _parse_market_from_event(event, slug, timeframe)
        except Exception as e:
            logger.debug(f"Event slug lookup failed for {slug}: {e}")
            return None

    async def _discover_by_slug(self) -> list[ArbMarket]:
        slugs = self._generate_slugs(self.config.scan_timeframes)
        found = []
        tasks = []
        for slug, tf in slugs:
            existing = next((m for m in self._known_markets.values() if m.slug == slug), None)
            if existing and existing.time_remaining_secs > 0:
                found.append(existing)
                continue
            tasks.append(self._fetch_event_by_slug(slug, tf))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, ArbMarket):
                    found.append(r)
        return found

    # ── Fallback: Events Pagination ──────────────────────────────

    async def _discover_by_pagination(self) -> list[ArbMarket]:
        """Fallback: paginate /events and filter for btc-updown slugs."""
        try:
            session = await self._get_session()
            found = []
            offset = 0
            for _ in range(5):
                params = {"active": "true", "closed": "false", "limit": 100, "offset": offset, "order": "id", "ascending": "false"}
                async with session.get(f"{self._gamma_url}/events", params=params) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                if not data:
                    break
                for ev in data:
                    slug = ev.get("slug", "")
                    match = SLUG_PATTERN.match(slug)
                    if not match:
                        continue
                    tf = match.group(1)
                    if tf not in self.config.scan_timeframes:
                        continue
                    mkt = _parse_market_from_event(ev, slug, tf)
                    if mkt:
                        found.append(mkt)
                if len(data) < 100:
                    break
                offset += 100
            return found
        except Exception as e:
            logger.error(f"Pagination error: {e}")
            return []

    # ── Combined Discovery ───────────────────────────────────────

    async def _discover_markets(self) -> list[ArbMarket]:
        now = time.time()
        if now - self._last_discovery < self._discovery_interval and self._known_markets:
            return list(self._known_markets.values())

        found = await self._discover_by_slug()
        if not found:
            logger.info("Slug lookup found 0 — trying events pagination...")
            found = await self._discover_by_pagination()

        # Expire old
        for cid, mkt in list(self._known_markets.items()):
            if mkt.time_remaining_secs <= 0:
                self._expired_markets[cid] = mkt
                del self._known_markets[cid]

        for market in found:
            if market.time_remaining_secs > 0:
                self._known_markets[market.condition_id] = market

        self._last_discovery = now
        self._total_markets_discovered = len(self._known_markets)

        by_tf = {}
        for mkt in self._known_markets.values():
            by_tf.setdefault(mkt.timeframe, 0)
            by_tf[mkt.timeframe] += 1
        summary = " · ".join(f"{TIMEFRAME_LABELS.get(k, k)}: {v}" for k, v in sorted(by_tf.items()))
        logger.info(f"🔍 Discovered {len(self._known_markets)} BTC markets — {summary}")
        return list(self._known_markets.values())

    # ── Price Refresh ────────────────────────────────────────────

    async def _refresh_prices(self, markets: list[ArbMarket]) -> list[ArbMarket]:
        """Re-fetch events by slug to get updated outcomePrices."""
        if not markets:
            return markets
        now = time.time()
        stale = self.config.poll_interval_secs * 0.8
        try:
            session = await self._get_session()
            for mkt in markets:
                if now - mkt.last_refreshed < stale or mkt.time_remaining_secs <= 0:
                    continue
                try:
                    url = f"{self._gamma_url}/events/slug/{mkt.slug}"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        event = await resp.json()
                    ms = event.get("markets", [])
                    if not ms:
                        continue
                    m = ms[0]
                    prices = _safe_json(m.get("outcomePrices"))
                    if len(prices) >= 2:
                        mkt.price_yes = float(prices[0])
                        mkt.price_no = float(prices[1])
                    mkt.liquidity = float(m.get("liquidityClob", m.get("liquidityNum", 0)))
                    mkt.volume = float(m.get("volumeNum", m.get("volume", 0)))
                    mkt.last_refreshed = now
                except Exception:
                    pass
            return list(self._known_markets.values())
        except Exception as e:
            logger.error(f"Price refresh error: {e}")
            return markets

    # ── Fee Estimation (Phase 1) ───────────────────────────────

    @staticmethod
    def _estimate_taker_fee_pct(price: float, fallback_max: float = 1.56) -> float:
        """
        Estimate Polymarket taker fee % at a given share price.
        Fee curve: parabolic, highest at p=0.50, zero at extremes.
        At p=0.50: 1.56%, at p=0.10: ~0.56%, at p=0.90: ~0.56%
        """
        if price <= 0 or price >= 1:
            return 0.0
        return round(fallback_max * 4.0 * price * (1.0 - price), 4)

    # ── Arb Detection ────────────────────────────────────────────

    def _find_opportunities(self, markets: list[ArbMarket]) -> list[ArbMarket]:
        now = time.time()
        opps = []
        for m in markets:
            if m.combined == 0 or m.time_remaining_secs <= 0:
                continue
            if m.combined < self.config.arb_threshold + 0.02 and m.combined >= self.config.arb_threshold:
                self._near_misses = [nm for nm in self._near_misses if now - nm.get("time", 0) < 300]
                self._near_misses.append({"time": now, "question": m.question[:60], "timeframe": m.timeframe, "combined": round(m.combined, 4), "gap": round((1.0 - m.combined) * 100, 2)})
            if m.combined >= self.config.arb_threshold:
                continue
            if m.edge_pct < self.config.min_edge_pct:
                continue

            # ── Fee-aware profit check (Phase 1) ──
            fee_yes_pct = self._estimate_taker_fee_pct(m.price_yes)
            fee_no_pct = self._estimate_taker_fee_pct(m.price_no)
            total_fee_pct = fee_yes_pct + fee_no_pct
            net_edge_pct = m.edge_pct - total_fee_pct
            if net_edge_pct <= 0:
                logger.debug(
                    f"Arb edge {m.edge_pct:.2f}% wiped by fees "
                    f"({fee_yes_pct:.2f}%+{fee_no_pct:.2f}%={total_fee_pct:.2f}%) — skipping"
                )
                continue

            if net_edge_pct > self._best_edge_seen:
                self._best_edge_seen = net_edge_pct
            if now - self._cooldowns.get(m.condition_id, 0) < self.config.cooldown_per_market_secs:
                continue
            if m.liquidity < self.config.min_liquidity_usd:
                continue
            opps.append(m)
        return opps

    # ── Execution ────────────────────────────────────────────────

    async def _execute_arb(self, market: ArbMarket) -> Optional[ArbExecution]:
        now = time.time()
        if self._daily_trades >= self.config.max_daily_arb_trades:
            return None
        cost = self.config.size_per_side_usd * 2
        if self._daily_spent + cost > self.config.max_daily_arb_budget:
            return None

        # ── Fee-adjusted profit calculation (Phase 1) ──
        gross_profit = self.config.size_per_side_usd * (1.0 / market.combined - 1.0)
        fee_yes_pct = self._estimate_taker_fee_pct(market.price_yes)
        fee_no_pct = self._estimate_taker_fee_pct(market.price_no)
        total_fees = (self.config.size_per_side_usd * fee_yes_pct / 100) + (self.config.size_per_side_usd * fee_no_pct / 100)
        net_profit = round(gross_profit - total_fees, 2)

        if net_profit <= 0:
            logger.info(f"⚠️ ARB gross=${gross_profit:.2f} but fees=${total_fees:.2f} → net=${net_profit:.2f} — skipping")
            return None

        execution = ArbExecution(timestamp=now, condition_id=market.condition_id, question=market.question, timeframe=market.timeframe, price_yes=market.price_yes, price_no=market.price_no, combined=market.combined, edge_pct=market.edge_pct, size_per_side=self.config.size_per_side_usd, guaranteed_profit=net_profit)
        tf_label = TIMEFRAME_LABELS.get(market.timeframe, market.timeframe)
        logger.info(
            f"💰 ARB [{tf_label}]: {market.question[:60]}... | "
            f"YES={market.price_yes:.3f} + NO={market.price_no:.3f} = {market.combined:.3f} | "
            f"edge={market.edge_pct:.1f}% | gross=${gross_profit:.2f} fees=${total_fees:.2f} net=${net_profit:.2f}"
        )
        if self.polymarket:
            try:
                from core.polymarket_client import BinaryMarket, MarketStatus
                bm = BinaryMarket(condition_id=market.condition_id, question=market.question, slug=market.slug, token_id_up=market.token_id_yes, token_id_down=market.token_id_no, price_up=market.price_yes, price_down=market.price_no, volume=market.volume, liquidity=market.liquidity, created_at="", end_date=market.end_date, status=MarketStatus.ACTIVE)
                yes_trade = await self.polymarket.place_order(market=bm, direction="up", size_usd=self.config.size_per_side_usd, oracle_price=0.0, confidence=1.0)
                if yes_trade: execution.order_id_yes = yes_trade.order_id
                no_trade = await self.polymarket.place_order(market=bm, direction="down", size_usd=self.config.size_per_side_usd, oracle_price=0.0, confidence=1.0)
                if no_trade: execution.order_id_no = no_trade.order_id
                execution.status = "filled" if (yes_trade and no_trade) else "partial" if (yes_trade or no_trade) else "failed"
            except Exception as e:
                execution.status = "failed"
                logger.error(f"Arb execution error: {e}")
        else:
            execution.status = "dry_run"
        self._executions.append(execution)
        self._cooldowns[market.condition_id] = now
        self._daily_trades += 1
        self._daily_spent += cost
        if execution.status in ("filled", "dry_run"):
            self._daily_profit += net_profit
        return execution

    def _check_daily_reset(self):
        if time.time() - self._day_start > 86400:
            self._daily_trades = 0; self._daily_spent = 0.0; self._daily_profit = 0.0
            self._day_start = time.time(); self._best_edge_seen = 0.0; self._near_misses.clear()

    # ── Main Loop ────────────────────────────────────────────────

    async def run(self):
        self._running = True
        self._day_start = time.time()
        logger.info(f"🚀 Arb scanner started — polling every {self.config.poll_interval_secs}s | timeframes: {', '.join(self.config.scan_timeframes)} | threshold: {self.config.arb_threshold} | budget: ${self.config.max_daily_arb_budget}/day")
        while self._running:
            try:
                self._check_daily_reset()

                # ── Backoff check: skip scan if backing off after errors ──
                now = time.time()
                if now < self._backoff_until:
                    remaining = int(self._backoff_until - now)
                    if self._scan_count % 10 == 0:
                        logger.info(f"⏳ Arb scanner backing off — {remaining}s remaining ({self._consecutive_errors} consecutive errors)")
                    self._scan_count += 1
                    await asyncio.sleep(self.config.poll_interval_secs)
                    continue

                self._scan_count += 1
                scan_start = time.time()
                if time.time() - self._last_discovery > self._discovery_interval:
                    await self._discover_markets()
                else:
                    await self._refresh_prices(list(self._known_markets.values()))
                for cid in [c for c, m in self._known_markets.items() if m.time_remaining_secs <= 0]:
                    self._expired_markets[cid] = self._known_markets.pop(cid)
                opps = self._find_opportunities(list(self._known_markets.values()))
                if opps:
                    opps.sort(key=lambda m: m.edge_pct, reverse=True)
                    for opp in opps:
                        await self._execute_arb(opp)
                        if self._daily_trades >= self.config.max_daily_arb_trades or self._daily_spent >= self.config.max_daily_arb_budget:
                            break
                self._last_scan_time_ms = (time.time() - scan_start) * 1000

                # ── Success: reset error counter ──
                self._consecutive_errors = 0

                if self._scan_count % 30 == 0:
                    by_tf = self._count_by_timeframe()
                    tf_str = ", ".join(f"{TIMEFRAME_LABELS.get(k,k)}:{v}" for k, v in sorted(by_tf.items()))
                    logger.info(f"📡 Scan #{self._scan_count} | {len(self._known_markets)} live ({tf_str}) | today: {self._daily_trades} arbs, ${self._daily_profit:.2f} profit | scan: {self._last_scan_time_ms:.0f}ms")
            except Exception as e:
                self._consecutive_errors += 1
                backoff_secs = min(300, self.config.poll_interval_secs * (2 ** self._consecutive_errors))
                self._backoff_until = time.time() + backoff_secs
                logger.error(f"Arb scan error: {e} — backing off {backoff_secs:.0f}s after {self._consecutive_errors} consecutive errors", exc_info=True)
            await asyncio.sleep(self.config.poll_interval_secs)
        await self._close_session()
        logger.info("Arb scanner stopped")

    def stop(self):
        self._running = False

    # ── Stats ────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        by_tf = self._count_by_timeframe()
        market_list = [{"question": m.question[:70], "timeframe": m.timeframe, "tf_label": TIMEFRAME_LABELS.get(m.timeframe, m.timeframe), "price_yes": m.price_yes, "price_no": m.price_no, "combined": round(m.combined, 4), "edge_pct": round(m.edge_pct, 2), "liquidity": m.liquidity, "volume": m.volume, "time_remaining": m.time_remaining_secs, "end_date": m.end_date, "is_arb": m.combined > 0 and m.combined < self.config.arb_threshold} for m in sorted(self._known_markets.values(), key=lambda x: x.end_ts)]
        return {"running": self._running, "scan_count": self._scan_count, "scan_time_ms": round(self._last_scan_time_ms, 1), "poll_interval": self.config.poll_interval_secs, "markets_live": len(self._known_markets), "markets_expired": len(self._expired_markets), "markets_by_timeframe": by_tf, "market_list": market_list[-50:], "threshold": self.config.arb_threshold, "size_per_side": self.config.size_per_side_usd, "timeframes": self.config.scan_timeframes, "daily_trades": self._daily_trades, "daily_profit": round(self._daily_profit, 2), "daily_spent": round(self._daily_spent, 2), "daily_budget": self.config.max_daily_arb_budget, "daily_budget_remaining": round(self.config.max_daily_arb_budget - self._daily_spent, 2), "daily_max_trades": self.config.max_daily_arb_trades, "best_edge_pct": round(self._best_edge_seen, 2), "near_misses": self._near_misses[-5:], "total_executions": len(self._executions), "consecutive_errors": self._consecutive_errors, "backoff_remaining_secs": max(0, round(self._backoff_until - time.time(), 1)) if self._backoff_until > time.time() else 0, "recent_arbs": [{"time": e.timestamp, "timeframe": e.timeframe, "tf_label": TIMEFRAME_LABELS.get(e.timeframe, e.timeframe), "edge_pct": round(e.edge_pct, 2), "profit": e.guaranteed_profit, "status": e.status, "yes": e.price_yes, "no": e.price_no, "combined": round(e.combined, 4), "question": e.question[:60]} for e in self._executions[-10:]]}

    def _count_by_timeframe(self) -> dict:
        counts = {}
        for m in self._known_markets.values():
            counts.setdefault(m.timeframe, 0)
            counts[m.timeframe] += 1
        return counts

    def get_executions(self) -> list[ArbExecution]:
        return self._executions.copy()
