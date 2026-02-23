"""
╔══════════════════════════════════════════════════════════════════════════╗
║  BTC-15M-Oracle — Autonomous Polymarket BTC Prediction Bot              ║
║                                                                          ║
║  Clock-synced entries at :59 / :14 / :29 / :44                          ║
║  Live trading via py-clob-client SDK                                     ║
║  BTC 15-minute UP/DOWN binary markets ONLY                               ║
║                                                                          ║
║  v2.5 — Active 5m parallel trading loop (Phase 3)              ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import signal
import sys
import time
import logging
import json
import datetime
import re
from pathlib import Path

from config.settings import BotConfig, MarketDirection
from oracles.price_feed import OracleEngine
from strategies.signal_engine import StrategyEngine
from core.polymarket_client import PolymarketClient
from core.risk_manager import RiskManager
from core.trade_logger import TradeLogger
from core.edge import EdgeEngine
from core.arb_scanner import ArbScanner, ArbScannerConfig
from core.market_maker import MarketMaker
from core.dashboard_server import DashboardServer, build_dashboard_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")


class BTCPredictionBot:
    def __init__(self, config: BotConfig, dashboard: bool = False):
        self.config = config
        self.running = False
        self.trade_logger = TradeLogger(config.logging)
        self.oracle = OracleEngine(config)
        self.strategy = StrategyEngine(config.strategy)
        self.polymarket = PolymarketClient(config)
        self.risk_manager = RiskManager(config.risk, capital=config.bankroll)
        self.edge = EdgeEngine(config.edge)
        self.dashboard = DashboardServer() if dashboard else None
        self._cycle_count = 0
        self._start_time = 0
        self._traded_this_window = False
        self._last_consensus = None
        self._last_anchor = None
        self._last_decision = None
        self._last_live_bankroll_sync = 0.0
        self._last_live_bankroll_value = None
        self._directional_interval_mins = int(config.polymarket.market_interval_minutes or 15)
        self._last_interval_refresh = 0.0
        # ── Late-window state (Phase 2 + 5m) ──
        self._late_window_traded_markets: set = set()  # dedup: condition_ids traded this cycle
        self._last_anchor_price = None  # Saved from main cycle for late-window reuse
        # Wire late-window budget limits into risk manager
        if hasattr(config, 'late_window'):
            self.risk_manager.late_window_max_daily_trades = config.late_window.max_daily_trades
            self.risk_manager.late_window_budget_pct = config.late_window.budget_pct
            self.risk_manager.late_window_max_trade_usd = config.late_window.max_trade_size_usd

        # Wire 5m budget limits into risk manager (Phase 3)
        if hasattr(config, 'active_5m'):
            self.risk_manager.fivem_budget_pct = config.active_5m.budget_pct
            self.risk_manager.fivem_max_daily_trades = config.active_5m.max_daily_trades
            self.risk_manager.fivem_max_trade_usd = config.active_5m.max_trade_size_usd
            self.risk_manager.fivem_max_daily_loss_pct = config.active_5m.max_daily_loss_pct
            self.risk_manager.fivem_max_consecutive_losses = config.active_5m.max_consecutive_losses
            self.risk_manager.fivem_cooldown_mins = config.active_5m.loss_streak_cooldown_mins

        # ── 5m loop state (Phase 3) ──
        self._5m_traded_this_window = False
        self._5m_cycle_count = 0
        self._5m_last_anchor_price = None
        self._5m_trade_ids: set = set()  # Track 5m trade IDs for PnL routing

        # Independent arb scanner (runs its own loop when --arb is enabled)
        if config.edge.enable_arb:
            arb_config = ArbScannerConfig(
                poll_interval_secs=config.edge.arb_poll_secs,
                arb_threshold=config.edge.arb_threshold,
                min_edge_pct=config.edge.arb_min_edge_pct,
                size_per_side_usd=config.edge.arb_size_usd,
                max_daily_arb_trades=config.edge.arb_max_daily_trades,
                max_daily_arb_budget=config.edge.arb_max_daily_budget,
                cooldown_per_market_secs=config.edge.arb_cooldown_secs,
                scan_timeframes=config.edge.arb_timeframes,
            )
            self.arb_scanner = ArbScanner(arb_config, self.polymarket)
        else:
            self.arb_scanner = None

        # Independent market maker (runs its own loop when --mm is enabled)
        if hasattr(config, 'market_maker') and config.market_maker.enabled:
            self.market_maker = MarketMaker(config.market_maker, self.polymarket)
        else:
            self.market_maker = None

    # ── Trading Cycle ───────────────────────────────────────────

    async def _sync_live_bankroll_if_enabled(self, force: bool = False):
        if not self.config.polymarket.sync_live_bankroll:
            return

        now = time.time()
        poll_secs = max(5, int(self.config.polymarket.live_bankroll_poll_secs))
        if not force and (now - self._last_live_bankroll_sync) < poll_secs:
            return

        live_balance = await self.polymarket.get_available_balance_usd()
        self._last_live_bankroll_sync = now

        if live_balance is None:
            return

        self._last_live_bankroll_value = round(float(live_balance), 2)
        self.risk_manager.capital = self._last_live_bankroll_value
        logger.info(f"Synced live bankroll: ${self._last_live_bankroll_value:.2f}")

    async def _trading_cycle(self):
        self._cycle_count += 1

        try:
            # ─────────────────────────────────────────────────────
            # PHASE 1: Capture window anchor (at the boundary)
            # ─────────────────────────────────────────────────────
            # Records the Chainlink opening price — the exact number
            # Polymarket will compare against at resolution.
            anchor = await self.oracle.capture_window_open(window_minutes=self._directional_interval_mins)
            open_price = anchor.open_price if anchor else None
            self._last_anchor = anchor
            self._last_anchor_price = open_price  # Phase 2: saved for late-window

            # ─────────────────────────────────────────────────────
            # PHASE 2: Wait for BTC to drift (strategy_delay_secs)
            # ─────────────────────────────────────────────────────
            # The price_vs_open signal (35% weight) compares current
            # price against the anchor. If we run strategy immediately,
            # drift ≈ 0 and the signal is dead weight. Waiting lets
            # BTC move so we get a meaningful directional read.
            delay = getattr(self.config, 'strategy_delay_secs', 0)
            if delay > 0 and open_price:
                logger.info(f"📌 Anchor: ${open_price:,.2f} — waiting {delay}s for price drift...")
                await asyncio.sleep(delay)

            # ─────────────────────────────────────────────────────
            # PHASE 3: Fresh price + strategy (after the delay)
            # ─────────────────────────────────────────────────────
            consensus = await self.oracle.get_price()
            self._last_consensus = consensus
            self.trade_logger.log_oracle({
                "price": consensus.price, "chainlink": consensus.chainlink_price,
                "sources": consensus.sources, "spread_pct": consensus.spread_pct,
                "window_open": open_price,
            })

            # 3. Candles
            candles = await self.oracle.get_candles(f"{self._directional_interval_mins}m", limit=100)
            if len(candles) < 30:
                logger.warning(f"Only {len(candles)} candles — skipping")
                return

            # 4. Strategy (anchored to window open price)
            # ── Fetch dynamic fee estimate for edge filtering (Phase 1) ──
            market_fee_pct = None
            try:
                market_fee_pct = self.polymarket._fee_fallback_pct * 4.0 * 0.5 * 0.5
                if self.polymarket._active_markets:
                    first_market = next(iter(self.polymarket._active_markets.values()), None)
                    if first_market:
                        token_id = first_market.token_id_up
                        fetched = await self.polymarket.get_fee_pct_for_price(token_id, first_market.price_up)
                        if fetched is not None:
                            market_fee_pct = fetched
            except Exception:
                pass  # Fall back to None → signal_engine uses 1.56% default

            decision = self.strategy.analyze(candles, consensus.price, open_price=open_price, fee_pct=market_fee_pct)
            self._last_decision = decision
            self.trade_logger.log_strategy({
                "direction": decision.direction.value,
                "confidence": decision.confidence,
                "should_trade": decision.should_trade,
                "drift_pct": decision.drift_pct,
                "open_price": open_price,
                "fee_pct": market_fee_pct,
                "signals": {s.name: {"dir": s.direction.value, "str": round(s.strength, 3)} for s in decision.signals},
                "btc_price": consensus.price,
            })

            if not decision.should_trade:
                logger.info(f"Cycle {self._cycle_count}: HOLD — {decision.reason}")
                return

            # 5. Live bankroll sync + risk
            await self._sync_live_bankroll_if_enabled()
            can_trade, reason = self.risk_manager.can_trade()
            if not can_trade:
                logger.info(f"Cycle {self._cycle_count}: BLOCKED — {reason}")
                return

            # 6. Markets — discover then filter to CURRENT window only
            markets = await self.polymarket.discover_markets()
            tradeable = [m for m in markets if m.is_tradeable and m.liquidity >= self.config.polymarket.min_liquidity_usd]

            if not markets:
                logger.info(f"Cycle {self._cycle_count}: No directional markets discovered")
                return
            if not tradeable:
                logger.info(
                    f"Cycle {self._cycle_count}: {len(markets)} markets discovered but none met liquidity "
                    f"threshold ${self.config.polymarket.min_liquidity_usd:.2f}"
                )
                return

            # Filter to current window — prevents trading future windows
            tradeable = self.polymarket.filter_current_window(tradeable, self._directional_interval_mins)
            if not tradeable:
                logger.info(f"Cycle {self._cycle_count}: No markets for current {self._directional_interval_mins}m window")
                return

            market = max(tradeable, key=lambda m: m.liquidity)

            # NOTE: Arb scanning is now an independent fast-polling loop (core/arb_scanner.py).
            # It runs every ~8 seconds across BTC 15m/30m/1h markets. The old per-cycle
            # arb check has been removed — the scanner handles it autonomously.

            # 6b. Hedge check (if enabled)
            direction = decision.direction.value
            open_trades = self.polymarket.get_trade_records()
            hedges = self.edge.check_hedge(
                open_trades=open_trades,
                current_direction=direction,
                current_confidence=decision.confidence,
                markets=self.polymarket._active_markets,
            )
            for h in hedges:
                hedge_market = next(
                    (m for m in tradeable if m.condition_id ==
                     next((t.market_condition_id for t in open_trades if t.trade_id == h.original_trade_id), "")),
                    None
                )
                if hedge_market:
                    trade = await self.polymarket.place_order(
                        market=hedge_market, direction=h.hedge_direction,
                        size_usd=h.hedge_size_usd, oracle_price=consensus.price,
                        confidence=decision.confidence,
                    )
                    if trade:
                        self.edge.mark_hedged(h.original_trade_id)
                        self.trade_logger.log_trade({
                            "type": "hedge", "original": h.original_trade_id,
                            "hedge_dir": h.hedge_direction, "locked_profit": h.locked_profit,
                        })

            # 7. Execute directional trade
            size = self.risk_manager.calculate_position_size(decision.confidence)
            if size <= 0:
                return

            trade = await self.polymarket.place_order(
                market=market, direction=direction, size_usd=size,
                oracle_price=consensus.price, confidence=decision.confidence,
            )

            if trade:
                self.trade_logger.log_trade({
                    "trade_id": trade.trade_id, "direction": trade.direction,
                    "size_usd": trade.size_usd, "confidence": trade.confidence,
                    "oracle_price": trade.oracle_price_at_entry,
                    "order_id": trade.order_id,
                    "market": market.question[:80],
                })
                await self._notify_trade("opened", trade.direction, trade.size_usd,
                                         entry_price=trade.entry_price, engine="directional")

            # 8. Resolutions — route 5m PnL to separate tracker
            resolved = await self.polymarket.check_resolutions()
            for r in resolved:
                is_5m = r.trade_id in self._5m_trade_ids
                if is_5m:
                    self.risk_manager.record_5m_trade(0, pnl=r.pnl)
                    self._5m_trade_ids.discard(r.trade_id)
                else:
                    self.risk_manager.record_trade(r.pnl)
                self.trade_logger.log_resolution({"trade_id": r.trade_id, "outcome": r.outcome, "pnl": r.pnl})
                await self._notify_trade("resolved", r.direction, r.size_usd, pnl=r.pnl,
                                         outcome=r.outcome, engine="directional_5m" if is_5m else "directional")

            

            # 8. Status
            stats = self.polymarket.get_stats()
            self.trade_logger.save_performance({
                "cycle": self._cycle_count, "btc_price": consensus.price,
                **stats, **self.risk_manager.get_status(),
            })

            logger.info(
                f"Cycle {self._cycle_count} | BTC=${consensus.price:,.2f} | "
                f"{direction.upper()} conf={decision.confidence:.2f} | "
                f"W/R={stats.get('win_rate', 0):.0f}%"
            )

        except Exception as e:
            logger.error(f"Cycle {self._cycle_count} error: {e}", exc_info=True)

        finally:
            # Broadcast state to dashboard (even on error/hold)
            if self.dashboard and self.dashboard.is_running:
                try:
                    state = build_dashboard_state(
                        cycle=self._cycle_count,
                        consensus=self._last_consensus,
                        anchor=self._last_anchor,
                        decision=self._last_decision,
                        risk_manager=self.risk_manager,
                        polymarket_client=self.polymarket,
                        edge_config=self.config.edge,
                        config=self.config,
                        arb_scanner=self.arb_scanner,
                    )
                    await self.dashboard.broadcast(state)
                except Exception as e:
                    logger.warning(f"Dashboard broadcast failed: {e}")

    # ── Late-Window Conviction (Phase 2) ────────────────────────

    async def _late_window_check(self):
        """
        Late-window conviction trading across ALL active BTC markets.
        Scans every discovered market (5m, 15m, etc.) and trades any
        where Chainlink has drifted significantly from the anchor with
        ≤lead_secs remaining before resolution.
        """
        lw = getattr(self.config, 'late_window', None)
        if not lw or not lw.enabled:
            return

        try:
            # 1. Fresh Chainlink price
            consensus = await self.oracle.get_price()
            if not consensus or not consensus.price:
                return

            # 2. Discover ALL active markets
            markets = await self.polymarket.discover_markets()
            tradeable = [m for m in markets if m.is_tradeable and m.liquidity >= self.config.polymarket.min_liquidity_usd]
            if not tradeable:
                return

            now = time.time()

            for market in tradeable:
                # Parse end time from market
                end_date = getattr(market, "end_date", "")
                if not end_date:
                    continue
                try:
                    import datetime as _dt
                    end_ts = _dt.datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
                except:
                    continue

                time_remaining = end_ts - now
                if time_remaining <= 30 or time_remaining > lw.lead_secs:
                    continue  # Not in the late-window zone (skip final 30s — too volatile)

                # Infer timeframe for logging
                tf = self._infer_market_interval_minutes(market)
                tf_label = f"{tf}m" if tf else "?"

                # Skip 5m markets — too noisy for late-window conviction
                if tf == 5:
                    continue

                # Need an anchor for this market's window
                # For 15m markets, use stored anchor if available
                # For 5m markets, use the Chainlink open from the market itself
                anchor_price = self._last_anchor_price

                # Try to get a more accurate anchor from the market's open time
                # The market question contains the window start time
                if not anchor_price:
                    continue

                # Skip if we already traded this exact market in late-window
                market_lw_key = f"lw_{market.condition_id}"
                if getattr(self, '_late_window_traded_markets', None) is None:
                    self._late_window_traded_markets = set()
                if market_lw_key in self._late_window_traded_markets:
                    continue

                # 3. Run late-window strategy (pure drift)
                decision = self.strategy.analyze_late_window(
                    current_price=consensus.price,
                    anchor_price=anchor_price,
                    time_remaining_secs=time_remaining,
                    min_drift_pct=lw.min_drift_pct,
                    base_confidence=lw.base_confidence,
                    max_confidence=lw.max_confidence,
                    drift_scale_pct=lw.drift_scale_pct,
                )

                if not decision.should_trade:
                    logger.info(f"Late-window [{tf_label}]: HOLD — {decision.reason}")
                    continue

                # 4. Risk check
                can_trade, reason = self.risk_manager.can_late_window_trade()
                if not can_trade:
                    logger.info(f"Late-window [{tf_label}]: BLOCKED — {reason}")
                    break  # Budget exhausted, stop scanning

                # 5. Size + execute
                size = self.risk_manager.calculate_late_window_size(decision.confidence)
                if size <= 0:
                    continue

                direction = decision.direction.value

                # Check entry price — skip if too expensive (tiny edge, not worth the risk)
                entry_price = market.price_up if direction == "up" else market.price_down
                max_entry = getattr(lw, 'max_entry_price', 0.80)
                if entry_price > max_entry:
                    logger.info(
                        f"Late-window [{tf_label}]: SKIP — entry price ${entry_price:.2f} too high "
                        f"(max ${max_entry:.2f}, only {(1.0 - entry_price)*100:.0f}¢ edge)"
                    )
                    continue

                trade = await self.polymarket.place_order(
                    market=market, direction=direction, size_usd=size,
                    oracle_price=consensus.price, confidence=decision.confidence,
                )

                if trade:
                    self._late_window_traded_markets.add(market_lw_key)
                    self.risk_manager.record_late_window_trade(size)
                    self.trade_logger.log_trade({
                        "type": "late_window",
                        "trade_id": trade.trade_id, "direction": trade.direction,
                        "size_usd": trade.size_usd, "confidence": trade.confidence,
                        "drift_pct": decision.drift_pct,
                        "time_remaining_secs": round(time_remaining, 0),
                        "timeframe": tf_label,
                        "oracle_price": trade.oracle_price_at_entry,
                        "order_id": trade.order_id,
                        "market": market.question[:80],
                    })
                    await self._notify_trade("opened", trade.direction, trade.size_usd,
                                             entry_price=trade.entry_price, engine="late_window")
                    logger.info(
                        f"🔮 LATE-WINDOW [{tf_label}] {direction.upper()} | "
                        f"${size:.2f} @ conf={decision.confidence:.2f} | "
                        f"drift={decision.drift_pct:+.4f}% | {time_remaining:.0f}s left"
                    )

        except Exception as e:
            logger.error(f"Late-window error: {e}", exc_info=True)

    # ── 5-Minute Parallel Loop (Phase 3) ────────────────────────

    def _next_5m_boundary(self) -> float:
        """Next 5-minute boundary timestamp."""
        now = time.time()
        dt = datetime.datetime.fromtimestamp(now)
        next_min = ((dt.minute // 5) + 1) * 5
        if next_min >= 60:
            b = dt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
        else:
            b = dt.replace(minute=next_min, second=0, microsecond=0)
        return b.timestamp()

    def _is_in_5m_entry_window(self) -> bool:
        """Check if we're in the entry window for a 5m boundary."""
        cfg = self.config.active_5m
        secs_until = self._next_5m_boundary() - cfg.entry_lead_secs - time.time()
        return -cfg.entry_window_secs <= secs_until <= 0

    def _is_also_15m_boundary(self) -> bool:
        """Check if the next 5m boundary is also a 15m boundary (avoid double-trading)."""
        boundary = self._next_5m_boundary()
        dt = datetime.datetime.fromtimestamp(boundary)
        return dt.minute % 15 == 0

    async def _5m_trading_cycle(self):
        """Execute a single 5m directional trade — mirrors _trading_cycle but with 5m params."""
        self._5m_cycle_count += 1
        cfg = self.config.active_5m

        try:
            # 1. Capture 5m anchor
            anchor = await self.oracle.capture_window_open(window_minutes=5)
            open_price = anchor.open_price if anchor else None
            self._5m_last_anchor_price = open_price

            # 2. Shorter strategy delay for 5m
            delay = cfg.strategy_delay_secs
            if delay > 0 and open_price:
                logger.info(f"📌 [5m] Anchor: ${open_price:,.2f} — waiting {delay}s...")
                await asyncio.sleep(delay)

            # 3. Fresh price + strategy
            consensus = await self.oracle.get_price()
            if not consensus or not consensus.price:
                logger.warning("[5m] No oracle price — skipping")
                return

            candles = await self.oracle.get_candles("5m", limit=100)
            if len(candles) < 30:
                logger.warning(f"[5m] Only {len(candles)} candles — skipping")
                return

            # Fee estimate
            market_fee_pct = None
            try:
                if self.polymarket._active_markets:
                    first_market = next(iter(self.polymarket._active_markets.values()), None)
                    if first_market:
                        token_id = first_market.token_id_up
                        fetched = await self.polymarket.get_fee_pct_for_price(token_id, first_market.price_up)
                        if fetched is not None:
                            market_fee_pct = fetched
            except Exception:
                pass

            decision = self.strategy.analyze(candles, consensus.price, open_price=open_price, fee_pct=market_fee_pct)

            if not decision.should_trade:
                logger.info(f"[5m] Cycle {self._5m_cycle_count}: HOLD — {decision.reason}")
                return

            # 4. Risk check (separate 5m budget)
            can_trade, reason = self.risk_manager.can_trade_5m()
            if not can_trade:
                logger.info(f"[5m] Cycle {self._5m_cycle_count}: BLOCKED — {reason}")
                return

            # 5. Discover + filter to current 5m window
            markets = await self.polymarket.discover_markets()
            tradeable = [m for m in markets if m.is_tradeable and m.liquidity >= self.config.polymarket.min_liquidity_usd]
            tradeable = self.polymarket.filter_current_window(tradeable, 5)
            if not tradeable:
                logger.info(f"[5m] Cycle {self._5m_cycle_count}: No markets for current 5m window")
                return

            market = max(tradeable, key=lambda m: m.liquidity)

            # 6. Size + execute
            size = self.risk_manager.calculate_5m_size(decision.confidence)
            if size <= 0:
                return

            direction = decision.direction.value
            trade = await self.polymarket.place_order(
                market=market, direction=direction, size_usd=size,
                oracle_price=consensus.price, confidence=decision.confidence,
            )

            if trade:
                self._5m_trade_ids.add(trade.trade_id)
                self.risk_manager.record_5m_trade(size)
                self.trade_logger.log_trade({
                    "type": "directional_5m",
                    "trade_id": trade.trade_id, "direction": trade.direction,
                    "size_usd": trade.size_usd, "confidence": trade.confidence,
                    "oracle_price": trade.oracle_price_at_entry,
                    "order_id": trade.order_id,
                    "market": market.question[:80],
                })
                await self._notify_trade("opened", trade.direction, trade.size_usd,
                                         entry_price=trade.entry_price, engine="directional_5m")
                logger.info(
                    f"⏱️ [5m] {direction.upper()} | ${size:.2f} @ conf={decision.confidence:.2f} | "
                    f"BTC=${consensus.price:,.2f}"
                )

        except Exception as e:
            logger.error(f"[5m] Cycle {self._5m_cycle_count} error: {e}", exc_info=True)

    async def _5m_loop(self):
        """Independent 5m trading loop — runs as async task alongside 15m loop."""
        logger.info("⏱️ [5m] Parallel trading loop started")

        while self.running:
            try:
                # Check resolutions every tick — 5m trades resolve fast
                try:
                    resolved = await self.polymarket.check_resolutions()
                    for r in resolved:
                        if r.trade_id in self._5m_trade_ids:
                            self.risk_manager.record_5m_trade(0, pnl=r.pnl)
                            self._5m_trade_ids.discard(r.trade_id)
                        else:
                            self.risk_manager.record_trade(r.pnl)
                        self.trade_logger.log_resolution({"trade_id": r.trade_id, "outcome": r.outcome, "pnl": r.pnl})
                        await self._notify_trade("resolved", r.direction, r.size_usd, pnl=r.pnl,
                                                 outcome=r.outcome, engine="directional_5m")
                except Exception:
                    pass

                if self._is_in_5m_entry_window():
                    # Skip if this is also a 15m boundary — let the main loop handle it
                    if self._is_also_15m_boundary():
                        if not self._5m_traded_this_window:
                            logger.debug("[5m] Skipping — 15m boundary (main loop handles)")
                            self._5m_traded_this_window = True
                    elif not self._5m_traded_this_window:
                        boundary = datetime.datetime.fromtimestamp(self._next_5m_boundary())
                        logger.info(f"⏱️ [5m] ENTRY — targeting {boundary.strftime('%H:%M')}")
                        await self._5m_trading_cycle()
                        self._5m_traded_this_window = True
                else:
                    # Reset when approaching next 5m entry window
                    cfg = self.config.active_5m
                    secs_until = self._next_5m_boundary() - cfg.entry_lead_secs - time.time()
                    if secs_until > 0 and secs_until < cfg.entry_lead_secs:
                        self._5m_traded_this_window = False

            except Exception as e:
                logger.error(f"[5m] Loop error: {e}", exc_info=True)

            await asyncio.sleep(self.config.sleep_poll_secs)

        logger.info("⏱️ [5m] Parallel trading loop stopped")

    # ── Clock Sync ──────────────────────────────────────────────

    def _next_boundary(self) -> float:
        now = time.time()
        dt = datetime.datetime.fromtimestamp(now)
        interval = max(1, int(self._directional_interval_mins))
        next_min = ((dt.minute // interval) + 1) * interval
        if next_min >= 60:
            b = dt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
        else:
            b = dt.replace(minute=next_min, second=0, microsecond=0)
        return b.timestamp()

    def _seconds_until_entry(self) -> float:
        return self._next_boundary() - self.config.entry_lead_secs - time.time()

    def _is_in_entry_window(self) -> bool:
        secs = self._seconds_until_entry()
        return -self.config.entry_window_secs <= secs <= 0

    def _format_next_entry(self) -> str:
        entry_ts = self._next_boundary() - self.config.entry_lead_secs
        entry_dt = datetime.datetime.fromtimestamp(entry_ts)
        boundary_dt = datetime.datetime.fromtimestamp(self._next_boundary())
        return f"{entry_dt.strftime('%H:%M:%S')} (→ {boundary_dt.strftime('%H:%M')})"

    def _infer_market_interval_minutes(self, market) -> int | None:
        slug = (getattr(market, "slug", "") or "").lower()
        m = re.search(r"btc-updown-(\d+)(m|h)-", slug)
        if m:
            qty = int(m.group(1))
            unit = m.group(2)
            return qty * (60 if unit == "h" else 1)

        text = f"{getattr(market, 'question', '')} {getattr(market, 'slug', '')}".lower()
        if any(k in text for k in ["5-min", "5 min", "5m", "5-minute"]):
            return 5
        if any(k in text for k in ["15-min", "15 min", "15m", "15-minute"]):
            return 15
        return None

    async def _refresh_directional_interval(self, force: bool = False):
        # When 5m parallel loop is active, lock main loop to 15m only
        if hasattr(self.config, 'active_5m') and self.config.active_5m.enabled:
            self._directional_interval_mins = 15
            return

        now = time.time()
        if not force and (now - self._last_interval_refresh) < 45:
            return
        self._last_interval_refresh = now

        markets = await self.polymarket.discover_markets()
        tradeable = [m for m in markets if m.is_tradeable and m.liquidity >= self.config.polymarket.min_liquidity_usd]
        if not tradeable:
            return

        intervals = {self._infer_market_interval_minutes(m) for m in tradeable}
        intervals.discard(None)
        if not intervals:
            return

        target = 15 if 15 in intervals else (5 if 5 in intervals else min(intervals))
        if target != self._directional_interval_mins:
            logger.info(
                f"Directional interval switched: {self._directional_interval_mins}m -> {target}m "
                f"(available: {sorted(intervals)})"
            )
            self._directional_interval_mins = target
            self._traded_this_window = False

    # ── Main Loop ───────────────────────────────────────────────

    async def run(self):
        print()
        print("=" * 60)
        print("  BTC-15M-Oracle — LIVE (v2.5)")
        print(f"  Bankroll: ${self.config.bankroll:,.2f}")
        print(f"  Arb: {'ON (independent scanner)' if self.config.edge.enable_arb else 'off'}  |  Hedge: {'ON' if self.config.edge.enable_hedge else 'off'}")
        lw = getattr(self.config, 'late_window', None)
        print(f"  Late-Window: {'ON (drift ≥' + str(lw.min_drift_pct) + '%, ' + str(lw.lead_secs) + 's before close)' if lw and lw.enabled else 'off'}")
        mm = getattr(self.config, 'market_maker', None)
        print(f"  Market Maker: {'ON (spread=' + str(mm.spread_bps) + 'bps, $' + str(mm.order_size_usd) + '/side, budget=$' + str(mm.max_daily_budget) + '/day)' if mm and mm.enabled else 'off'}")
        a5 = getattr(self.config, 'active_5m', None)
        print(f"  5m Parallel: {'ON (budget=' + str(a5.budget_pct) + '%, max $' + str(a5.max_trade_size_usd) + '/trade, delay=' + str(a5.strategy_delay_secs) + 's)' if a5 and a5.enabled else 'off'}")
        print(f"  Entry: {self.config.entry_lead_secs}s before each 15m boundary")
        print(f"  Strategy delay: {getattr(self.config, 'strategy_delay_secs', 0)}s after anchor capture")
        print(f"  Next: {self._format_next_entry()}")
        if self.config.edge.enable_arb:
            print(f"  Arb Scanner: polling every {self.config.edge.arb_poll_secs}s | "
                  f"timeframes: {', '.join(self.config.edge.arb_timeframes)} | "
                  f"budget: ${self.config.edge.arb_max_daily_budget}/day")
        if self.dashboard:
            print(f"  Dashboard: http://localhost:8765")
        print("=" * 60)
        print()

        # Start dashboard server if enabled
        if self.dashboard:
            await self.dashboard.start()

        # Start persistent RTDS price stream (single websocket for Chainlink + Binance)
        rtds_task = asyncio.create_task(self.oracle.start_rtds_stream())
        logger.info("🔌 RTDS persistent stream launched")

        # Start independent arb scanner (its own async loop)
        arb_task = None
        if self.arb_scanner:
            arb_task = asyncio.create_task(self.arb_scanner.run())
            logger.info("Arb scanner launched as independent task")

        # Start independent market maker (its own async loop)
        mm_task = None
        if self.market_maker:
            mm_task = asyncio.create_task(self.market_maker.run())
            logger.info("Market maker launched as independent task")

        # Start independent 5m trading loop (Phase 3)
        fivem_task = None
        if hasattr(self.config, 'active_5m') and self.config.active_5m.enabled:
            fivem_task = asyncio.create_task(self._5m_loop())
            logger.info("⏱️ [5m] Parallel trading loop launched as independent task")

        # Start dashboard live price push (updates BTC price between cycles)
        price_push_task = None
        if self.dashboard:
            price_push_task = asyncio.create_task(self._price_push_loop())
            logger.info("📊 Dashboard live price push launched")

        self.running = True
        self._start_time = time.time()

        while self.running:
            await self._refresh_directional_interval()

            if self._is_in_entry_window():
                if not self._traded_this_window:
                    boundary = datetime.datetime.fromtimestamp(self._next_boundary())
                    logger.info(f"⏰ ENTRY — targeting {boundary.strftime('%H:%M')}")
                    await self._trading_cycle()
                    self._traded_this_window = True
                    # Reset late-window dedup set for new window cycle
                    self._late_window_traded_markets = set()
                    logger.info(f"💤 Next: {self._format_next_entry()}")
            else:
                # ── Late-window check (Phase 2 + 5m support) ──
                # Runs on every tick, scans ALL markets for any nearing expiry.
                # Internal dedup prevents trading the same market twice.
                lw = getattr(self.config, 'late_window', None)
                if lw and lw.enabled and self._traded_this_window:
                    await self._late_window_check()

                # Only reset _traded_this_window when we're approaching the NEXT
                # entry window — i.e., within entry_lead_secs of the next boundary.
                secs_to_next_entry = self._seconds_until_entry()
                if secs_to_next_entry > 0 and secs_to_next_entry < self.config.entry_lead_secs:
                    self._traded_this_window = False

            await asyncio.sleep(self.config.sleep_poll_secs)

    # ── Dashboard Live Updates ──────────────────────────────────

    async def _price_push_loop(self):
        """Push live BTC price to dashboard every 2 seconds between cycles."""
        while self.running:
            try:
                if self.dashboard and self.dashboard.is_running and self.dashboard.client_count > 0:
                    cl_price = 0
                    bn_price = 0
                    # RTDS buffers are PricePoint objects with .price attribute
                    cl_pp = getattr(self.oracle, '_rtds_chainlink_latest', None)
                    bn_pp = getattr(self.oracle, '_rtds_binance_latest', None)
                    if cl_pp and hasattr(cl_pp, 'price'):
                        cl_price = cl_pp.price or 0
                    if bn_pp and hasattr(bn_pp, 'price'):
                        bn_price = bn_pp.price or 0
                    # Fallback to last consensus
                    if cl_price == 0 and bn_price == 0 and self._last_consensus:
                        cl_price = getattr(self._last_consensus, 'price', 0)
                    if cl_price or bn_price:
                        msg = {
                            "type": "price_tick",
                            "price": cl_price or bn_price,
                            "chainlink": cl_price,
                            "binance": bn_price,
                            "timestamp": time.time(),
                        }
                        await self.dashboard.broadcast(msg)
            except Exception:
                pass
            await asyncio.sleep(2)

    async def _notify_trade(self, action: str, direction: str, size_usd: float,
                            entry_price: float = 0, pnl: float = 0,
                            outcome: str = "", engine: str = "directional"):
        """Send trade notification to dashboard for toast popup."""
        if not self.dashboard or not self.dashboard.is_running:
            return
        try:
            msg = {
                "type": "trade_notification",
                "action": action,
                "direction": direction,
                "size_usd": size_usd,
                "entry_price": entry_price,
                "pnl": pnl,
                "outcome": outcome,
                "engine": engine,
                "timestamp": time.time(),
            }
            await self.dashboard.broadcast(msg)
        except Exception:
            pass

    def stop(self):
        self.running = False
        logger.info("Shutdown initiated")

    async def shutdown(self):
        self.stop()
        if self.arb_scanner:
            self.arb_scanner.stop()
            logger.info(f"Arb scanner stats: {self.arb_scanner.get_stats()}")
        if self.market_maker:
            self.market_maker.stop()
            logger.info(f"Market maker stats: {self.market_maker.get_stats()}")
        await self.oracle.close()
        await self.polymarket.close()
        if self.dashboard:
            await self.dashboard.stop()
        stats = self.polymarket.get_stats()
        self.trade_logger.save_performance({
            "status": "shutdown", "cycles": self._cycle_count,
            "uptime_secs": time.time() - self._start_time, **stats,
        })
        logger.info(f"Stopped after {self._cycle_count} cycles")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="BTC-15M-Oracle — Polymarket Prediction Bot")
    parser.add_argument("--bankroll", type=float, default=500.0, help="Starting bankroll in USD for directional mode; also used as arb-only fallback if live balance read fails (default: 500)")
    parser.add_argument("--cycles", type=int, default=0, help="Max cycles, 0=unlimited (default: 0)")
    parser.add_argument("--arb", action="store_true", help="Enable arbitrage scanner alongside directional trading")
    parser.add_argument("--arb-only", action="store_true", help="Run ONLY the arb scanner — no directional trading")
    parser.add_argument("--hedge", action="store_true", help="Enable hedge engine")
    parser.add_argument("--late-window", action="store_true", help="Enable late-window conviction trading (Phase 2)")
    parser.add_argument("--mm", action="store_true", help="Enable market making engine (Phase 4 — post-only, zero fees, rebates)")
    parser.add_argument("--5m", dest="fivem", action="store_true", help="Enable parallel 5m directional trading (Phase 3)")
    parser.add_argument("--dashboard", action="store_true", help="Start WebSocket server on :8765 for live dashboard")
    parser.add_argument("--sync-live-bankroll", action="store_true", help="Sync risk bankroll from live Polymarket account balance (directional mode)")
    parser.add_argument("--live-bankroll-poll-secs", type=int, default=60, help="Live bankroll sync interval in seconds (default: 60)")
    parser.add_argument("--strategy-delay", type=int, default=None, help="Override strategy_delay_secs (seconds to wait after anchor capture before trading)")
    args = parser.parse_args()

    # ── Arb-Only Mode ────────────────────────────────────────────
    if args.arb_only:
        from core.arb_scanner import ArbScanner, ArbScannerConfig

        # In arb-only mode, limits are sourced from live Polymarket balance.
        # If live balance cannot be read, --bankroll is used as a fallback.
        config = BotConfig(bankroll=0.0)
        config.edge.enable_arb = True
        # Arb-only mode always sources limits from live account balance.
        config.polymarket.sync_live_bankroll = True
        config.polymarket.live_bankroll_poll_secs = args.live_bankroll_poll_secs

        # Polymarket client for order execution + live balance reads
        polymarket = PolymarketClient(config)
        live_balance = await polymarket.get_available_balance_usd()
        if live_balance is None or live_balance <= 0:
            if args.bankroll > 0:
                logger.warning(
                    "Could not read live balance in arb-only mode; "
                    f"falling back to --bankroll ${args.bankroll:.2f}"
                )
                live_balance = float(args.bankroll)
            else:
                logger.error(
                    "Arb-only mode requires readable positive live Polymarket balance "
                    "or a positive --bankroll fallback"
                )
                await polymarket.close()
                return

        base_size_per_side = config.edge.arb_size_usd
        base_daily_budget = config.edge.arb_max_daily_budget
        effective_budget = round(min(base_daily_budget, live_balance), 2)
        effective_size = round(min(base_size_per_side, effective_budget / 2), 2)

        if effective_size < 0.5 or effective_budget <= 0:
            logger.error(
                f"Insufficient live bankroll (${live_balance:.2f}) for arb sizing "
                f"(size_per_side={base_size_per_side}, budget_cap={base_daily_budget})"
            )
            await polymarket.close()
            return

        arb_config = ArbScannerConfig(
            poll_interval_secs=config.edge.arb_poll_secs,
            arb_threshold=config.edge.arb_threshold,
            min_edge_pct=config.edge.arb_min_edge_pct,
            size_per_side_usd=effective_size,
            max_daily_arb_trades=config.edge.arb_max_daily_trades,
            max_daily_arb_budget=effective_budget,
            cooldown_per_market_secs=config.edge.arb_cooldown_secs,
            scan_timeframes=config.edge.arb_timeframes,
        )

        scanner = ArbScanner(arb_config, polymarket)
        last_live_balance = live_balance
        last_live_sync = time.time()

        # Optional dashboard
        dashboard = DashboardServer() if args.dashboard else None

        print()
        print("=" * 60)
        print("  BTC ARB SCANNER — ARBITRAGE ONLY MODE")
        print(f"  Live bankroll: ${live_balance:,.2f}")
        print(f"  Budget cap: ${base_daily_budget:,.2f}/day")
        print(f"  Effective budget: ${effective_budget:,.2f}/day")
        print(f"  Size cap: ${base_size_per_side:,.2f} per side")
        print(f"  Effective size: ${effective_size:,.2f} per side")
        print(f"  Threshold: YES+NO < {config.edge.arb_threshold}")
        print(f"  Polling: every {config.edge.arb_poll_secs}s")
        print(f"  Timeframes: {', '.join(config.edge.arb_timeframes)}")
        print(f"  Max trades: {config.edge.arb_max_daily_trades}/day")
        if dashboard:
            print(f"  Dashboard: http://localhost:8765")
        print()
        print("  No directional trading. Pure arb capture.")
        print("=" * 60)
        print()

        running = True

        def handle_signal(sig, frame):
            nonlocal running
            print("\n\nCtrl+C — shutting down...")
            scanner.stop()
            running = False
        signal.signal(signal.SIGINT, handle_signal)

        try:
            if dashboard:
                await dashboard.start()

            # Run arb scanner as main task
            arb_task = asyncio.create_task(scanner.run())

            # Keep alive + periodic dashboard broadcast
            while running and not arb_task.done():
                if time.time() - last_live_sync >= max(5, args.live_bankroll_poll_secs):
                    refreshed_balance = await polymarket.get_available_balance_usd()
                    last_live_sync = time.time()
                    if refreshed_balance is not None and refreshed_balance > 0:
                        last_live_balance = refreshed_balance
                        refreshed_budget = round(min(base_daily_budget, refreshed_balance), 2)
                        refreshed_size = round(min(base_size_per_side, refreshed_budget / 2), 2)
                        scanner.config.max_daily_arb_budget = refreshed_budget
                        scanner.config.size_per_side_usd = max(0.5, refreshed_size)

                if dashboard and dashboard.is_running:
                    try:
                        arb_stats = scanner.get_stats()
                        state = {
                            "type": "state",
                            "timestamp": time.time(),
                            "cycle": arb_stats.get("scan_count", 0),
                            "mode": "arb_only",
                            "oracle": {"price": 0, "chainlink": None, "sources": [], "spread_pct": 0},
                            "anchor": {"open_price": None, "source": None, "drift_pct": None},
                            "strategy": {"direction": "hold", "confidence": 0, "should_trade": False, "reason": "Arb-only mode"},
                            "signals": {},
                            "stats": {"wins": 0, "losses": 0, "win_rate": 0, "total_pnl": arb_stats.get("daily_profit", 0), "total_wagered": arb_stats.get("daily_spent", 0), "total_trades": arb_stats.get("daily_trades", 0)},
                            "risk": {"daily_trades": arb_stats.get("daily_trades", 0), "max_daily_trades": config.edge.arb_max_daily_trades},
                            "positions": {"open": [], "closed": []},
                            "arb_scanner": arb_stats,
                            "config": {"bankroll": round(last_live_balance, 2), "arb_enabled": True, "hedge_enabled": False},
                        }
                        await dashboard.broadcast(state)
                    except Exception:
                        pass
                await asyncio.sleep(5)

        finally:
            scanner.stop()
            await polymarket.close()
            if dashboard:
                await dashboard.stop()
            stats = scanner.get_stats()
            logger.info(
                f"Arb scanner stopped — "
                f"{stats['daily_trades']} trades, "
                f"${stats['daily_profit']:.2f} profit, "
                f"{stats['markets_tracked']} markets tracked"
            )
        return

    # ── Normal Mode (directional + optional arb/hedge) ───────────
    config = BotConfig(bankroll=args.bankroll)
    config.edge.enable_arb = args.arb
    config.polymarket.sync_live_bankroll = args.sync_live_bankroll
    config.polymarket.live_bankroll_poll_secs = args.live_bankroll_poll_secs
    config.edge.enable_hedge = args.hedge
    config.late_window.enabled = args.late_window
    config.market_maker.enabled = args.mm
    config.active_5m.enabled = args.fivem

    # Apply strategy delay override if provided via CLI
    if args.strategy_delay is not None:
        config.strategy_delay_secs = max(0, args.strategy_delay)

    bot = BTCPredictionBot(config, dashboard=args.dashboard)

    def handle_signal(sig, frame):
        print("\n\nCtrl+C — shutting down...")
        bot.stop()
    signal.signal(signal.SIGINT, handle_signal)

    try:
        if args.cycles > 0:
            bot._start_time = time.time()
            bot.running = True
            completed = 0
            print(f"\nRunning {args.cycles} cycles | Bankroll: ${args.bankroll}")
            print(f"Next: {bot._format_next_entry()}\n")
            while completed < args.cycles and bot.running:
                await bot._refresh_directional_interval()

                if bot._is_in_entry_window():
                    if not bot._traded_this_window:
                        await bot._trading_cycle()
                        bot._traded_this_window = True
                        completed += 1
                        if completed < args.cycles:
                            logger.info(f"Cycle {completed}/{args.cycles}. Next: {bot._format_next_entry()}")
                else:
                    bot._traded_this_window = False
                await asyncio.sleep(bot.config.sleep_poll_secs)
        else:
            await bot.run()
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
