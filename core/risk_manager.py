"""
╔══════════════════════════════════════════════════════════════════╗
║  RISK MANAGER — Capital, Position Sizing, and Loss Controls      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import time
import logging
from dataclasses import dataclass

from config.settings import RiskConfig

logger = logging.getLogger("risk")


@dataclass
class DailyStats:
    date: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    consecutive_losses: int = 0
    last_trade_time: float = 0.0
    cooldown_until: float = 0.0
    start_of_day_capital: float = 0.0
    # ── Late-window tracking (Phase 2) ──
    late_window_trades: int = 0
    late_window_pnl: float = 0.0
    late_window_spent: float = 0.0
    # ── 5m tracking (Phase 3) ──
    trades_5m: int = 0
    wins_5m: int = 0
    losses_5m: int = 0
    pnl_5m: float = 0.0
    spent_5m: float = 0.0
    consecutive_losses_5m: int = 0
    cooldown_until_5m: float = 0.0


class RiskManager:
    def __init__(self, config: RiskConfig, capital: float):
        self.config = config
        self.capital = capital
        self._daily = DailyStats(date=self._today(), start_of_day_capital=capital)
        self._total_pnl = 0.0
        # ── Late-window limits (Phase 2) — set by bot from LateWindowConfig ──
        self.late_window_max_daily_trades: int = 15
        self.late_window_budget_pct: float = 30.0
        self.late_window_max_trade_usd: float = 10.0
        # ── 5m limits (Phase 3) — set by bot from Active5mConfig ──
        self.fivem_budget_pct: float = 30.0
        self.fivem_max_daily_trades: int = 30
        self.fivem_max_trade_usd: float = 10.0
        self.fivem_max_daily_loss_pct: float = 15.0
        self.fivem_max_consecutive_losses: int = 4
        self.fivem_cooldown_mins: int = 30

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _reset_daily_if_needed(self):
        today = self._today()
        if self._daily.date != today:
            logger.info(
                f"📅 Daily reset (UTC midnight) — "
                f"yesterday: {self._daily.trades} trades, "
                f"${self._daily.total_pnl:+.2f} P&L, "
                f"W/L={self._daily.wins}/{self._daily.losses}"
            )
            self._daily = DailyStats(date=today, start_of_day_capital=self.capital)

    def can_trade(self) -> tuple[bool, str]:
        self._reset_daily_if_needed()

        if time.time() < self._daily.cooldown_until:
            remaining = int(self._daily.cooldown_until - time.time())
            return False, f"Cooldown ({remaining}s remaining)"

        if self._daily.trades >= self.config.max_daily_trades:
            return False, f"Daily limit ({self.config.max_daily_trades})"

        if self.capital > 0:
            # Measure loss % against start-of-day capital, not current
            # This prevents the breaker from being overly sensitive as
            # capital shrinks during the day from small losses
            reference = self._daily.start_of_day_capital or self.capital
            daily_loss_pct = abs(min(0, self._daily.total_pnl)) / reference * 100
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                return False, f"Daily loss limit ({daily_loss_pct:.1f}%)"

        if self._daily.consecutive_losses >= self.config.max_consecutive_losses:
            self._daily.cooldown_until = time.time() + (self.config.loss_streak_cooldown_mins * 60)
            return False, f"Loss streak ({self.config.max_consecutive_losses}) — cooldown"

        if self.capital <= 0:
            return False, "No capital"

        return True, "OK"

    def calculate_position_size(self, confidence: float) -> float:
        if self.capital <= 0:
            return 0.0
        kelly = max(0, 2 * confidence - 1)
        fractional_kelly = kelly * self.config.kelly_fraction
        size = self.capital * fractional_kelly
        size = min(size, self.capital * (self.config.max_trade_pct / 100))
        size = min(size, self.config.max_trade_size_usd)
        size = max(size, self.config.min_trade_size_usd)
        size = min(size, self.capital)
        return round(size, 2)

    def record_trade(self, pnl: float):
        self._reset_daily_if_needed()
        self._daily.trades += 1
        self._daily.total_pnl += pnl
        self._daily.last_trade_time = time.time()
        self._total_pnl += pnl
        self.capital += pnl

        if pnl >= 0:
            self._daily.wins += 1
            self._daily.consecutive_losses = 0
        else:
            self._daily.losses += 1
            self._daily.consecutive_losses += 1
            if self._daily.consecutive_losses >= self.config.max_consecutive_losses:
                logger.warning(f"⚠️ {self._daily.consecutive_losses} consecutive losses — cooldown")

        logger.info(
            f"Risk: trades={self._daily.trades} W/L={self._daily.wins}/{self._daily.losses} "
            f"pnl=${self._daily.total_pnl:+.2f} capital=${self.capital:.2f}"
        )

    def get_status(self) -> dict:
        self._reset_daily_if_needed()
        can, reason = self.can_trade()
        return {
            "can_trade": can, "reason": reason, "capital": self.capital,
            "daily_trades": self._daily.trades, "daily_pnl": self._daily.total_pnl,
            "consecutive_losses": self._daily.consecutive_losses,
            "in_cooldown": time.time() < self._daily.cooldown_until,
            "total_pnl": self._total_pnl,
            # Phase 2
            "late_window_trades": self._daily.late_window_trades,
            "late_window_pnl": round(self._daily.late_window_pnl, 2),
            "late_window_spent": round(self._daily.late_window_spent, 2),
        }

    # ── Late-Window Risk (Phase 2) ────────────────────────────

    def can_late_window_trade(self) -> tuple[bool, str]:
        """Check if a late-window trade is allowed under its own budget."""
        self._reset_daily_if_needed()

        if self._daily.late_window_trades >= self.late_window_max_daily_trades:
            return False, f"Late-window daily limit ({self.late_window_max_daily_trades})"

        # Budget: % of start-of-day capital
        reference = self._daily.start_of_day_capital or self.capital
        budget = reference * (self.late_window_budget_pct / 100)
        if self._daily.late_window_spent >= budget:
            return False, f"Late-window budget exhausted (${budget:.2f})"

        if self.capital <= 0:
            return False, "No capital"

        return True, "OK"

    def calculate_late_window_size(self, confidence: float) -> float:
        """Position sizing for late-window trades (separate from main)."""
        if self.capital <= 0:
            return 0.0
        kelly = max(0, 2 * confidence - 1)
        fractional_kelly = kelly * self.config.kelly_fraction
        size = self.capital * fractional_kelly
        size = min(size, self.late_window_max_trade_usd)
        size = max(size, self.config.min_trade_size_usd)
        size = min(size, self.capital)
        return round(size, 2)

    def record_late_window_trade(self, size_usd: float, pnl: float = 0.0):
        """Record a late-window trade (budget tracking only — P&L recorded on resolution)."""
        self._reset_daily_if_needed()
        self._daily.late_window_trades += 1
        self._daily.late_window_spent += size_usd
        if pnl != 0:
            self._daily.late_window_pnl += pnl

    # ── 5m Risk (Phase 3) ─────────────────────────────────────

    def can_trade_5m(self) -> tuple[bool, str]:
        """Check if a 5m trade is allowed under its own budget/limits."""
        self._reset_daily_if_needed()

        if time.time() < self._daily.cooldown_until_5m:
            remaining = int(self._daily.cooldown_until_5m - time.time())
            return False, f"5m cooldown ({remaining}s remaining)"

        if self._daily.trades_5m >= self.fivem_max_daily_trades:
            return False, f"5m daily limit ({self.fivem_max_daily_trades})"

        reference = self._daily.start_of_day_capital or self.capital
        budget = reference * (self.fivem_budget_pct / 100)
        if self._daily.spent_5m >= budget:
            return False, f"5m budget exhausted (${budget:.2f})"

        if reference > 0:
            loss_pct = abs(min(0, self._daily.pnl_5m)) / reference * 100
            if loss_pct >= self.fivem_max_daily_loss_pct:
                return False, f"5m daily loss limit ({loss_pct:.1f}%)"

        if self._daily.consecutive_losses_5m >= self.fivem_max_consecutive_losses:
            self._daily.cooldown_until_5m = time.time() + (self.fivem_cooldown_mins * 60)
            self._daily.consecutive_losses_5m = 0
            return False, f"5m loss streak ({self.fivem_max_consecutive_losses}) — cooldown"

        if self.capital <= 0:
            return False, "No capital"

        return True, "OK"

    def calculate_5m_size(self, confidence: float) -> float:
        """Position sizing for 5m trades."""
        if self.capital <= 0:
            return 0.0
        kelly = max(0, 2 * confidence - 1)
        fractional_kelly = kelly * self.config.kelly_fraction
        size = self.capital * fractional_kelly
        size = min(size, self.fivem_max_trade_usd)
        size = max(size, self.config.min_trade_size_usd)
        size = min(size, self.capital)
        return round(size, 2)

    def record_5m_trade(self, size_usd: float, pnl: float = 0.0):
        """Record a 5m trade."""
        self._reset_daily_if_needed()
        self._daily.trades_5m += 1
        self._daily.spent_5m += size_usd
        if pnl != 0:
            self._daily.pnl_5m += pnl
            self._total_pnl += pnl
            self.capital += pnl
            if pnl >= 0:
                self._daily.wins_5m += 1
                self._daily.consecutive_losses_5m = 0
            else:
                self._daily.losses_5m += 1
                self._daily.consecutive_losses_5m += 1
            logger.info(
                f"Risk [5m]: trades={self._daily.trades_5m} "
                f"W/L={self._daily.wins_5m}/{self._daily.losses_5m} "
                f"pnl=${self._daily.pnl_5m:+.2f} capital=${self.capital:.2f}"
            )
