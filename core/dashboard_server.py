"""
Dashboard Server — HTTP + WebSocket via aiohttp.

  http://localhost:8765      → Dashboard HTML page
  ws://localhost:8765/ws     → Live state broadcast
  http://localhost:8765/state → JSON snapshot

  python bot.py --bankroll 500 --dashboard
  → Open http://localhost:8765 in your browser
"""

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger("dashboard")


class DashboardServer:
    def __init__(self, host="0.0.0.0", port=8765):
        self.host = host
        self.port = port
        self.clients: set[web.WebSocketResponse] = set()
        self._state: dict = {}
        self._running = False
        self._runner: Optional[web.AppRunner] = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self._handle_page)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/state", self._handle_state)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self._running = True
        logger.info(f"Dashboard: http://localhost:{self.port}")

    async def _handle_page(self, request):
        return web.Response(text=_build_html(), content_type="text/html")

    async def _handle_state(self, request):
        return web.json_response(self._state)

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        logger.info(f"Dashboard client connected ({len(self.clients)} total)")
        try:
            if self._state:
                await ws.send_json(self._state)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.PING:
                    await ws.pong(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            self.clients.discard(ws)
            logger.info(f"Dashboard client disconnected ({len(self.clients)} remaining)")
        return ws

    async def broadcast(self, state: dict):
        self._state = state
        dead = set()
        for ws in self.clients:
            try:
                await ws.send_json(state)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    async def stop(self):
        self._running = False
        for ws in list(self.clients):
            await ws.close()
        self.clients.clear()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Dashboard server stopped")

    @property
    def client_count(self):
        return len(self.clients)

    @property
    def is_running(self):
        return self._running


def build_dashboard_state(cycle, consensus, anchor, decision, risk_manager, polymarket_client, edge_config, config, arb_scanner=None):
    stats = polymarket_client.get_stats()
    risk_status = risk_manager.get_status()
    open_trades = polymarket_client.get_trade_records()

    # Live capital from risk manager (updates after each resolved trade)
    live_capital = round(risk_manager.capital, 2)

    signals = {}
    for s in (decision.signals if decision else []):
        signals[s.name] = {"direction": s.direction.value, "strength": round(s.strength, 3), "raw_value": round(s.raw_value, 4), "description": s.description}

    open_pos = []
    for t in open_trades:
        if t.outcome is None:
            open_pos.append({"id": t.trade_id, "direction": t.direction, "size_usd": t.size_usd, "entry_price": t.entry_price, "confidence": t.confidence, "timestamp": t.timestamp, "oracle_price": t.oracle_price_at_entry})

    closed_pos = []
    for t in open_trades:
        if t.outcome is not None:
            closed_pos.append({"id": t.trade_id, "direction": t.direction, "size_usd": t.size_usd, "entry_price": t.entry_price, "confidence": t.confidence, "pnl": t.pnl, "outcome": t.outcome, "timestamp": t.timestamp})

    arb_stats = arb_scanner.get_stats() if arb_scanner else None

    return {
        "type": "state", "timestamp": time.time(), "cycle": cycle,
        "oracle": {"price": consensus.price if consensus else 0, "chainlink": consensus.chainlink_price if consensus else None, "sources": consensus.sources if consensus else [], "spread_pct": consensus.spread_pct if consensus else 0},
        "anchor": {"open_price": anchor.open_price if anchor else None, "source": anchor.source if anchor else None, "drift_pct": decision.drift_pct if decision else None},
        "strategy": {"direction": decision.direction.value if decision else "hold", "confidence": decision.confidence if decision else 0, "should_trade": decision.should_trade if decision else False, "reason": decision.reason if decision else "", "drift_pct": decision.drift_pct if decision else None, "volatility_pct": decision.volatility_pct if decision else 0},
        "signals": signals,
        "stats": {"wins": stats.get("wins", 0), "losses": stats.get("losses", 0), "win_rate": stats.get("win_rate", 0), "total_pnl": stats.get("total_pnl", 0), "total_wagered": stats.get("total_wagered", 0), "total_trades": stats.get("total_trades", 0), "completed": stats.get("completed", 0), "pending": stats.get("pending", 0)},
        "risk": {"capital": live_capital, "daily_trades": risk_status.get("daily_trades", 0), "max_daily_trades": config.risk.max_daily_trades, "daily_pnl": risk_status.get("daily_pnl", 0), "daily_loss_pct": risk_status.get("daily_loss_pct", 0), "consecutive_losses": risk_status.get("consecutive_losses", 0), "cooldown_active": risk_status.get("in_cooldown", False), "total_pnl": risk_status.get("total_pnl", 0)},
        "positions": {"open": open_pos, "closed": closed_pos[-50:]},
        "arb_scanner": arb_stats,
        "config": {"bankroll": live_capital, "arb_enabled": edge_config.enable_arb, "hedge_enabled": edge_config.enable_hedge},
    }


# ── HTML Builder ─────────────────────────────────────────────────

def _build_html():
    """Phase 6: GRIDPHANTOMDEV dark dashboard with full engine visibility."""

    LOGO_B64 = "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"

    CSS = r"""
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Space Grotesk','DM Sans',sans-serif;background:#0a0a0f;color:#e2e8f0;min-height:100vh}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:#111118}::-webkit-scrollbar-thumb{background:#2a2a3a;border-radius:3px}::-webkit-scrollbar-thumb:hover{background:#3a3a4a}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes glow{0%,100%{box-shadow:0 0 15px rgba(245,158,11,.15)}50%{box-shadow:0 0 25px rgba(245,158,11,.3)}}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideIn{from{opacity:0;transform:translateX(-10px)}to{opacity:1;transform:translateX(0)}}
.mono{font-family:'JetBrains Mono','DM Mono',monospace}
.fade-in{animation:fadeIn .4s ease both}
.card{background:#12121a;border-radius:12px;border:1px solid #1e1e2e;padding:16px 18px;transition:border-color .2s}
.card:hover{border-color:#2a2a3e}

/* ── Hero ── */
.hero{background:linear-gradient(145deg,#0f0f18 0%,#12121f 50%,#0f0f18 100%);padding:20px 28px 24px;border-bottom:1px solid #1a1a2a;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:radial-gradient(ellipse at 70% 50%,rgba(245,158,11,.04) 0%,transparent 60%);pointer-events:none}
.hero-grid{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px;margin-top:16px}
.hero-stat{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px 16px;text-align:center;backdrop-filter:blur(8px)}
.hero-stat .hl{font-size:10px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.hero-stat .hv{font-size:22px;font-weight:800;color:#e2e8f0}

/* ── Stat Cards ── */
.stat-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;padding:16px 24px}
.sc{background:#12121a;border-radius:12px;padding:16px 18px;position:relative;overflow:hidden;border:1px solid #1e1e2e}
.sc .ac{position:absolute;top:0;left:0;right:0;height:2px}
.sc .lb{font-size:10px;color:#64748b;font-weight:600;letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px}
.sc .vl{font-size:24px;font-weight:800;line-height:1.1;color:#e2e8f0}
.sc .sb{font-size:10px;color:#4a5568;margin-top:6px}

/* ── Main Grid ── */
.main-grid{display:grid;grid-template-columns:1fr 280px;gap:14px;padding:0 24px 24px}

/* ── Signals ── */
.sr{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #1a1a2a}
.sr:last-child{border:none}
.si{width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0}
.su{background:rgba(22,163,74,.15);color:#4ade80}.sd{background:rgba(220,38,38,.15);color:#f87171}

/* ── Engines ── */
.engine-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #1a1a2a}
.engine-row:last-child{border:none}
.dot{width:8px;height:8px;border-radius:50%}
.engine-badge{font-size:9px;font-weight:700;padding:2px 8px;border-radius:4px}

/* ── Positions ── */
.tab-btn{padding:6px 16px;border:none;background:none;border-radius:8px;cursor:pointer;font-family:inherit;font-size:12px;font-weight:500;color:#64748b;transition:all .15s}
.tab-btn.active{background:#1e1e2e;font-weight:700;color:#e2e8f0}
.tbl-hdr{display:grid;grid-template-columns:56px 50px 1fr 56px 64px 56px;font-size:10px;font-weight:600;color:#4a5568;padding:0 0 6px;border-bottom:1px solid #1e1e2e;letter-spacing:.04em;text-transform:uppercase}
.tbl-row{display:grid;grid-template-columns:56px 50px 1fr 56px 64px 56px;padding:8px 0;border-bottom:1px solid #141420;font-size:12px;align-items:center}
.dir-badge{width:20px;height:20px;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;font-size:9px;font-weight:700}
.result-pill{font-weight:700;font-size:9px;padding:2px 7px;border-radius:4px;display:inline-block;letter-spacing:.02em}

/* ── Arb Scanner ── */
.arb-card{background:#12121a;border-radius:12px;border:1px solid #2a2210;padding:16px 18px}
.arb-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:14px}
.arb-stat{background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.12);border-radius:8px;padding:8px 10px;text-align:center}
.arb-stat .al{font-size:9px;color:#b45309;font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-bottom:2px}
.arb-stat .av{font-size:18px;font-weight:800;color:#f59e0b}
.arb-stat .as{font-size:9px;color:#92400e;margin-top:2px}
.arb-mhdr{display:grid;grid-template-columns:42px 1fr 52px 52px 56px 48px 42px;font-size:9px;font-weight:600;color:#4a5568;padding:0 0 5px;border-bottom:1px solid #1e1e2e;letter-spacing:.04em;text-transform:uppercase;gap:4px}
.arb-mrow{display:grid;grid-template-columns:42px 1fr 52px 52px 56px 48px 42px;padding:5px 0;border-bottom:1px solid #141420;font-size:11px;align-items:center;gap:4px}
.tf-pill{font-size:8px;font-weight:700;padding:1px 5px;border-radius:3px;display:inline-block;white-space:nowrap}

/* ── Equity Canvas ── */
.eq-card{position:relative}
.eq-card canvas{display:block;width:100%;border-radius:0 0 8px 8px}

/* ── RTDS ── */
.rtds-dot{width:6px;height:6px;border-radius:50%;display:inline-block}

/* ── Toast Notifications ── */
.toast-container{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none}

/* ── Heartbeat / Alive Indicators ── */
.heartbeat{display:inline-flex;align-items:center;gap:6px}
.hb-dot{width:8px;height:8px;border-radius:50%;background:#4ade80;position:relative}
.hb-dot::after{content:'';position:absolute;inset:-4px;border-radius:50%;border:2px solid #4ade80;animation:hbRing 2s ease-out infinite;opacity:0}
@keyframes hbRing{0%{transform:scale(.5);opacity:.8}100%{transform:scale(1.8);opacity:0}}
.hb-line{display:flex;align-items:center;gap:1px;height:16px}
.hb-bar{width:2px;border-radius:1px;background:#4ade80;animation:hbBar 1.2s ease-in-out infinite}
.hb-bar:nth-child(1){animation-delay:0s;height:4px}
.hb-bar:nth-child(2){animation-delay:.15s;height:8px}
.hb-bar:nth-child(3){animation-delay:.3s;height:14px}
.hb-bar:nth-child(4){animation-delay:.45s;height:8px}
.hb-bar:nth-child(5){animation-delay:.6s;height:4px}
@keyframes hbBar{0%,100%{opacity:.3;transform:scaleY(.5)}50%{opacity:1;transform:scaleY(1)}}
.price-flash{transition:all .15s ease}
.price-up{color:#4ade80!important;text-shadow:0 0 12px rgba(74,222,128,.4)}
.price-down{color:#f87171!important;text-shadow:0 0 12px rgba(248,113,113,.4)}
.tick-arrow{font-size:10px;margin-left:4px;display:inline-block;transition:all .2s}
.tick-arrow.up{color:#4ade80;animation:tickUp .3s ease}
.tick-arrow.down{color:#f87171;animation:tickDown .3s ease}
@keyframes tickUp{from{transform:translateY(4px);opacity:0}to{transform:translateY(0);opacity:1}}
@keyframes tickDown{from{transform:translateY(-4px);opacity:0}to{transform:translateY(0);opacity:1}}
.live-clock{font-family:'JetBrains Mono',monospace;font-size:10px;color:#64748b;letter-spacing:.02em}
.stream-active{animation:streamPulse 3s ease-in-out infinite}
@keyframes streamPulse{0%,100%{opacity:.6}50%{opacity:1}}
.toast{pointer-events:auto;background:#12121a;border:1px solid #1e1e2e;border-radius:10px;padding:12px 16px;min-width:280px;max-width:360px;box-shadow:0 8px 32px rgba(0,0,0,.5);animation:toastIn .3s ease both;display:flex;align-items:center;gap:10px;backdrop-filter:blur(12px)}
.toast.win{border-color:rgba(74,222,128,.4);box-shadow:0 8px 32px rgba(74,222,128,.1)}
.toast.loss{border-color:rgba(248,113,113,.4);box-shadow:0 8px 32px rgba(248,113,113,.1)}
.toast.trade{border-color:rgba(245,158,11,.4);box-shadow:0 8px 32px rgba(245,158,11,.1)}
.toast-exit{animation:toastOut .3s ease both}
.toast-icon{font-size:20px;flex-shrink:0}
.toast-body{flex:1}
.toast-title{font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:2px}
.toast-detail{font-size:10px;color:#64748b}
.toast-pnl{font-size:14px;font-weight:800;flex-shrink:0}
@keyframes toastIn{from{opacity:0;transform:translateX(40px)}to{opacity:1;transform:translateX(0)}}
@keyframes toastOut{from{opacity:1;transform:translateX(0)}to{opacity:0;transform:translateX(40px)}}

@media(max-width:900px){.stat-grid{grid-template-columns:1fr 1fr}.main-grid{grid-template-columns:1fr}.hero-grid{grid-template-columns:1fr 1fr}.arb-grid{grid-template-columns:repeat(3,1fr)}}
"""

    HERO = f"""
<div class="hero">
  <div style="position:relative;z-index:1;max-width:1100px;margin:0 auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px">
      <div style="display:flex;align-items:center;gap:12px">
        <img src="data:image/png;base64,{{LOGO}}" width="42" height="42" style="border-radius:8px;border:1px solid rgba(245,158,11,.3)" alt="logo">
        <div>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:18px;font-weight:800;color:#f5f5f5;letter-spacing:-.02em">GRIDPHANTOMDEV</span>
            <div id="sb" style="display:flex;align-items:center;gap:4px;padding:3px 10px;border-radius:12px;background:rgba(245,158,11,.15);border:1px solid rgba(245,158,11,.25)">
              <div id="sd" style="width:6px;height:6px;border-radius:50%;background:#f59e0b;animation:pulse 2s infinite"></div>
              <span id="st" style="font-size:10px;font-weight:700;color:#f59e0b">CONNECTING</span>
            </div>
          </div>
          <div style="font-size:10px;color:#4a5568;margin-top:2px;font-family:'JetBrains Mono',monospace">clausea4bswa3mbtc bot &middot; v2.5</div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:12px">
        <div class="heartbeat"><div class="hb-dot" id="hb-dot"></div></div>
        <div class="hb-line" id="hb-line"><div class="hb-bar"></div><div class="hb-bar"></div><div class="hb-bar"></div><div class="hb-bar"></div><div class="hb-bar"></div></div>
        <div id="ci" style="font-size:11px;color:#4a5568">Connecting...</div>
        <div class="live-clock mono" id="live-clock">--:--:--</div>
        <div class="mono" style="font-size:10px;color:#4a5568" id="last-tick">--</div>
      </div>
    </div>
    <div style="text-align:center;margin-bottom:16px">
      <div style="font-size:11px;font-weight:500;color:#64748b;margin-bottom:4px">Total Profit &middot; Cycle <span class="mono" id="hc">0</span></div>
      <div id="hp" style="font-size:48px;font-weight:800;line-height:1;color:#4ade80;text-shadow:0 0 30px rgba(74,222,128,.2)">$0.00</div>
      <div style="font-size:11px;color:#4a5568;margin-top:6px"><span id="ht">0</span> Bets &middot; $<span id="hw">0.00</span> Wagered</div>
    </div>
    <div class="hero-grid">
      <div class="hero-stat"><div class="hl">Win Rate</div><div class="hv" id="hwr">0%</div></div>
      <div class="hero-stat"><div class="hl">BTC (Chainlink)</div><div class="hv mono" id="hbtc" style="color:#f59e0b;font-size:18px">$0</div></div>
      <div class="hero-stat"><div class="hl">Next Entry</div><div class="hv mono" id="htm" style="color:#4ade80">--:--</div></div>
      <div class="hero-stat"><div class="hl">Avg P&L / Bet</div><div class="hv" id="hav">$0.00</div></div>
    </div>
  </div>
</div>
""".replace("{{LOGO}}", LOGO_B64)

    STAT_CARDS = """
  <div class="stat-grid">
    <div class="sc"><div class="ac" style="background:#4ade80"></div><div class="lb">Bankroll</div><div class="vl" id="sb1" style="color:#4ade80">$0</div><div class="sb">Available balance</div></div>
    <div class="sc" id="spc"><div class="ac" id="spa" style="background:#4ade80"></div><div class="lb">Realized P&L</div><div class="vl" id="sp" style="color:#4ade80">+$0.00</div><div class="sb" id="swl">0W &ndash; 0L</div></div>
    <div class="sc"><div class="ac" style="background:#818cf8"></div><div class="lb">Window Open</div><div class="vl mono" id="so" style="color:#818cf8;font-size:18px">&mdash;</div><div class="sb" id="sdr">Waiting...</div></div>
    <div class="sc"><div class="lb">Strategy</div><div class="vl" id="sdi" style="color:#4a5568">HOLD</div><div class="sb" id="scf">Confidence: 0%</div></div>
    <div class="sc"><div class="ac" style="background:#f59e0b"></div><div class="lb">Daily Risk</div><div class="vl" id="sr" style="color:#f59e0b">0/20</div><div class="sb" id="ss">Streak: 0</div></div>
  </div>
"""

    ARB_PANEL = """
      <!-- Arb Scanner Panel -->
      <div class="arb-card" id="arb-panel" style="display:none">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:13px;font-weight:700;color:#f59e0b">&#9889; Arb Scanner</span>
            <span id="arb-status" style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:12px;background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.25)">SCANNING</span>
          </div>
          <div style="font-size:10px;color:#92400e;font-family:'JetBrains Mono',monospace">
            Scan #<span id="arb-scan-count">0</span> &middot; <span id="arb-scan-ms">0</span>ms
          </div>
        </div>
        <div class="arb-grid">
          <div class="arb-stat"><div class="al">Markets Live</div><div class="av" id="arb-mkts">0</div><div class="as" id="arb-tf-breakdown">-</div></div>
          <div class="arb-stat"><div class="al">Arb Trades</div><div class="av" id="arb-trades">0</div><div class="as" id="arb-trade-limit">/ 50 daily</div></div>
          <div class="arb-stat"><div class="al">Arb Profit</div><div class="av" id="arb-profit">$0</div><div class="as" id="arb-spent">$0 committed</div></div>
          <div class="arb-stat"><div class="al">Budget Left</div><div class="av" id="arb-budget">$200</div><div class="as" id="arb-budget-total">of $200/day</div></div>
          <div class="arb-stat"><div class="al">Best Edge</div><div class="av" id="arb-best-edge">0%</div><div class="as">today</div></div>
        </div>
        <div style="font-size:11px;font-weight:700;color:#94a3b8;margin-bottom:6px">Live BTC Markets</div>
        <div class="arb-mhdr">
          <span>TF</span><span>Market</span><span style="text-align:right">YES</span><span style="text-align:right">NO</span><span style="text-align:right">Sum</span><span style="text-align:right">Liq</span><span style="text-align:right">Time</span>
        </div>
        <div id="arb-markets-body" style="max-height:200px;overflow-y:auto">
          <div style="padding:20px;text-align:center;color:#4a5568;font-size:11px">Discovering markets...</div>
        </div>
        <div id="arb-near-misses" style="margin-top:8px;display:none">
          <div style="font-size:10px;color:#4a5568;font-weight:600;margin-bottom:4px">NEAR MISSES (within 2% of threshold)</div>
          <div id="arb-near-body" style="font-size:10px;color:#f59e0b;font-family:'JetBrains Mono',monospace"></div>
        </div>
      </div>
"""

    POSITIONS = """
      <div class="card" style="flex:1">
        <div style="display:flex;gap:0;margin-bottom:10px">
          <button class="tab-btn active" id="tab-open" onclick="switchTab('open')">Open (<span id="open-count">0</span>)</button>
          <button class="tab-btn" id="tab-closed" onclick="switchTab('closed')">History (<span id="closed-count">0</span>)</button>
        </div>
        <div class="tbl-hdr">
          <span>Time</span><span>Dir</span><span>Size</span><span style="text-align:right">Conf</span><span style="text-align:right">P&L</span><span style="text-align:right" id="col-last">Exp</span>
        </div>
        <div id="positions-body" style="max-height:260px;overflow-y:auto">
          <div style="padding:32px;text-align:center;color:#4a5568;font-size:12px">Waiting for entry window...</div>
        </div>
      </div>
"""

    RIGHT_SIDEBAR = """
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="card">
        <div style="font-size:12px;font-weight:700;color:#94a3b8;margin-bottom:8px">Signals</div>
        <div id="sg"><div style="padding:16px;text-align:center;color:#4a5568;font-size:11px">Waiting for first cycle...</div></div>
        <div style="margin-top:10px;font-size:10px;color:#4a5568;line-height:1.5">Price vs Open: <span style="font-weight:700;color:#818cf8">35% weight</span> &middot; Chainlink BTC/USD</div>
      </div>
      <div class="card">
        <div style="font-size:12px;font-weight:700;color:#94a3b8;margin-bottom:8px">Engines</div>
        <div id="en"></div>
      </div>
      <div class="card">
        <div style="font-size:12px;font-weight:700;color:#94a3b8;margin-bottom:8px">Oracle Sources</div>
        <div id="os"></div>
        <div style="margin-top:8px;font-size:10px;color:#4a5568">Spread: <span class="mono" id="osp">0</span>%</div>
      </div>
      <div class="card">
        <div style="font-size:12px;font-weight:700;color:#94a3b8;margin-bottom:8px">Last Decision</div>
        <div id="decision-log" style="font-size:11px;color:#4a5568;line-height:1.8;font-family:'JetBrains Mono',monospace;background:#0e0e16;border-radius:8px;padding:12px;border:1px solid #1e1e2e">
          <div><span style="color:#4a5568">direction:</span> <span id="dl-dir" style="color:#4a5568">hold</span></div>
          <div><span style="color:#4a5568">confidence:</span> <span id="dl-conf">0%</span></div>
          <div><span style="color:#4a5568">should_trade:</span> <span id="dl-trade" style="color:#f87171">false</span></div>
          <div><span style="color:#4a5568">drift:</span> <span id="dl-drift">&mdash;</span></div>
          <div><span style="color:#4a5568">volatility:</span> <span id="dl-vol">&mdash;</span></div>
          <div><span style="color:#4a5568">reason:</span> <span id="dl-reason">&mdash;</span></div>
        </div>
      </div>
      <div class="card">
        <div style="font-size:12px;font-weight:700;color:#94a3b8;margin-bottom:8px">Activity Feed</div>
        <div id="activity-feed" style="max-height:180px;overflow-y:auto">
          <div style="padding:16px;text-align:center;color:#4a5568;font-size:11px">No activity yet</div>
        </div>
      </div>
      <div class="card">
        <div style="font-size:12px;font-weight:700;color:#94a3b8;margin-bottom:8px">RTDS Stream</div>
        <div id="rtds-info" style="font-size:11px;color:#64748b">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px"><span class="rtds-dot" id="rtds-dot" style="background:#4a5568"></span><span id="rtds-status">Waiting...</span></div>
          <div class="mono" style="font-size:10px;color:#4a5568" id="rtds-detail">--</div>
        </div>
      </div>
    </div>
"""

    JS = r"""
let ws,state,eq=[],curTab='open';
function timer(){const n=new Date(),m=n.getMinutes(),s=n.getSeconds(),t=Math.max(0,(((Math.floor(m/15)+1)*15-m-1)*60+(60-s))%900-60);return{text:`${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}`,secs:t,color:t<60?'#f87171':t<240?'#f59e0b':'#4ade80'}}

function switchTab(tab){
  curTab=tab;
  document.getElementById('tab-open').className='tab-btn'+(tab==='open'?' active':'');
  document.getElementById('tab-closed').className='tab-btn'+(tab==='closed'?' active':'');
  document.getElementById('col-last').textContent=tab==='closed'?'Result':'Exp';
  if(state)renderPositions(state);
}

function fmtTime(ts){if(!ts)return'--:--';const d=new Date(typeof ts==='number'?ts*1000:ts);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function fmtShortTime(ts){if(!ts)return'--:--';const d=new Date(typeof ts==='number'?ts*1000:ts);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}

function tradeRow(p,closed){
  const up=p.direction==='up'||p.direction==='UP'||p.d==='UP';
  const dir=(p.direction||p.d||'').toUpperCase();
  const sz=p.size_usd||p.sz||0;
  const ep=p.entry_price||p.ep||0;
  const conf=p.confidence||p.conf||0;
  const pnl=closed?(p.pnl||0):(p.uPnl||0);
  const pc=pnl>=0?'#4ade80':'#f87171';
  const time=closed?fmtTime(p.timestamp||p.ct):fmtTime(p.timestamp||p.t);
  let last='';
  if(closed){
    const win=p.outcome==='win'||p.win===true||pnl>0;
    last=`<span class="result-pill" style="background:${win?'rgba(74,222,128,.12)':'rgba(248,113,113,.12)'};color:${win?'#4ade80':'#f87171'}">${win?'WIN':'LOSS'}</span>`;
  }else{
    last=`<span style="font-size:10px;color:#f59e0b;font-weight:600">${fmtShortTime(p.expiry||p.exp)}</span>`;
  }
  return `<div class="tbl-row">
    <span style="color:#4a5568" class="mono">${time}</span>
    <span style="display:inline-flex;align-items:center;gap:4px">
      <span class="dir-badge" style="background:${up?'rgba(74,222,128,.12)':'rgba(248,113,113,.12)'};color:${up?'#4ade80':'#f87171'}">${up?'&#9650;':'&#9660;'}</span>
      <span style="font-weight:600;color:#94a3b8;font-size:11px">${dir}</span>
    </span>
    <span style="color:#64748b">$${parseFloat(sz).toFixed(2)} <span style="color:#2a2a3a">@</span> <span class="mono">${parseFloat(ep).toFixed(3)}</span></span>
    <span style="color:#4a5568;text-align:right">${(conf*100).toFixed(0)}%</span>
    <span class="mono" style="text-align:right;color:${pc};font-weight:700">${closed?(pnl>=0?'+':'')+pnl.toFixed(2):'--'}</span>
    <span style="text-align:right">${last}</span>
  </div>`;
}

function renderPositions(d){
  const op=d.positions?.open||[];
  const cl=d.positions?.closed||[];
  document.getElementById('open-count').textContent=op.length;
  document.getElementById('closed-count').textContent=cl.length;
  const body=document.getElementById('positions-body');
  const items=curTab==='open'?op:cl;
  if(items.length===0){
    body.innerHTML=`<div style="padding:32px;text-align:center;color:#4a5568;font-size:12px">${curTab==='open'?'Waiting for entry window...':'No history yet'}</div>`;
  }else{
    body.innerHTML=items.map(p=>tradeRow(p,curTab==='closed')).join('');
  }
}

// ── Arb Scanner Rendering ──
const TF_COLORS={'5m':'#a855f7','15m':'#3b82f6','30m':'#10b981','1h':'#ef4444'};
function fmtSecs(s){if(!s||s<=0)return'--';if(s<60)return Math.round(s)+'s';if(s<3600)return Math.round(s/60)+'m';return(s/3600).toFixed(1)+'h'}

function renderArb(d){
  const a=d.arb_scanner;
  const panel=document.getElementById('arb-panel');
  if(!a){panel.style.display='none';return}
  panel.style.display='block';
  document.getElementById('arb-scan-count').textContent=a.scan_count||0;
  document.getElementById('arb-scan-ms').textContent=a.scan_time_ms||0;
  document.getElementById('arb-status').textContent=a.running?'SCANNING':'STOPPED';
  document.getElementById('arb-status').style.background=a.running?'rgba(245,158,11,.15)':'rgba(248,113,113,.15)';
  document.getElementById('arb-status').style.color=a.running?'#f59e0b':'#f87171';
  document.getElementById('arb-mkts').textContent=a.markets_live||0;
  const tf=a.markets_by_timeframe||{};
  const tfStr=Object.entries(tf).map(([k,v])=>`${k}:${v}`).join(' ');
  document.getElementById('arb-tf-breakdown').textContent=tfStr||'-';
  document.getElementById('arb-trades').textContent=a.daily_trades||0;
  document.getElementById('arb-trade-limit').textContent=`/ ${a.daily_max_trades||50} daily`;
  document.getElementById('arb-profit').textContent=`$${(a.daily_profit||0).toFixed(2)}`;
  document.getElementById('arb-spent').textContent=`$${(a.daily_spent||0).toFixed(2)} spent`;
  document.getElementById('arb-budget').textContent=`$${(a.daily_budget_remaining||0).toFixed(0)}`;
  document.getElementById('arb-budget-total').textContent=`of $${(a.daily_budget||200).toFixed(0)}/day`;
  document.getElementById('arb-best-edge').textContent=`${(a.best_edge_pct||0).toFixed(1)}%`;
  const mkts=a.market_list||[];
  const body=document.getElementById('arb-markets-body');
  if(mkts.length===0){
    body.innerHTML='<div style="padding:20px;text-align:center;color:#4a5568;font-size:11px">No BTC markets found.</div>';
  }else{
    body.innerHTML=mkts.map(m=>{
      const c=TF_COLORS[m.timeframe]||'#64748b';
      const sum=m.combined||0;
      const isArb=m.is_arb;
      const sumC=isArb?'#4ade80':sum===0?'#4a5568':'#94a3b8';
      const bg=isArb?'background:rgba(74,222,128,.05);':'';
      const q=m.question.replace(/Bitcoin Up or Down - /,'').replace(/,?\s*\d{4}/,'');
      return `<div class="arb-mrow" style="${bg}">
        <span><span class="tf-pill" style="background:${c}15;color:${c}">${m.tf_label}</span></span>
        <span style="color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${m.question}">${q}</span>
        <span class="mono" style="text-align:right;color:#94a3b8">${m.price_yes?m.price_yes.toFixed(3):'-'}</span>
        <span class="mono" style="text-align:right;color:#94a3b8">${m.price_no?m.price_no.toFixed(3):'-'}</span>
        <span class="mono" style="text-align:right;font-weight:700;color:${sumC}">${sum?sum.toFixed(3):'-'}</span>
        <span style="text-align:right;color:#4a5568;font-size:10px">$${(m.liquidity||0).toFixed(0)}</span>
        <span class="mono" style="text-align:right;color:#f59e0b;font-size:10px;font-weight:600">${fmtSecs(m.time_remaining)}</span>
      </div>`}).join('');
  }
  const nm=a.near_misses||[];
  const nmPanel=document.getElementById('arb-near-misses');
  if(nm.length>0){
    nmPanel.style.display='block';
    document.getElementById('arb-near-body').innerHTML=nm.map(n=>`${n.timeframe} | sum=${n.combined} | gap=${n.gap}%`).join('<br>');
  }else{
    nmPanel.style.display='none';
  }
}

function drawEq(){
  const c=document.getElementById('ec');if(!c)return;
  const x=c.getContext('2d'),w=c.offsetWidth,h=80;
  c.width=w*2;c.height=h*2;x.scale(2,2);
  if(eq.length<2)return;
  const mn=Math.min(...eq),mx=Math.max(...eq),r=mx-mn||1;
  const up=eq[eq.length-1]>=eq[0];
  const cl=up?'#4ade80':'#f87171';
  // Fill
  x.beginPath();x.moveTo(0,h);
  for(let i=0;i<eq.length;i++){x.lineTo((i/(eq.length-1))*w,h-((eq[i]-mn)/r)*(h-12)-6)}
  x.lineTo(w,h);x.closePath();
  const g=x.createLinearGradient(0,0,0,h);
  g.addColorStop(0,up?'rgba(74,222,128,.1)':'rgba(248,113,113,.1)');
  g.addColorStop(1,'rgba(0,0,0,0)');
  x.fillStyle=g;x.fill();
  // Line
  x.beginPath();
  for(let i=0;i<eq.length;i++){
    const px=(i/(eq.length-1))*w,py=h-((eq[i]-mn)/r)*(h-12)-6;
    i===0?x.moveTo(px,py):x.lineTo(px,py)
  }
  x.strokeStyle=cl;x.lineWidth=2;x.stroke();
  // Glow dot at end
  const lastY=h-((eq[eq.length-1]-mn)/r)*(h-12)-6;
  x.beginPath();x.arc(w,lastY,3,0,Math.PI*2);x.fillStyle=cl;x.fill();
  x.beginPath();x.arc(w,lastY,6,0,Math.PI*2);x.fillStyle=cl.replace(')',',0.2)').replace('rgb','rgba');x.fill();
}

function render(d){
  if(!d)return;
  const p=d.stats?.total_pnl||0;
  const pc=p>=0?'#4ade80':'#f87171';
  const wr=d.stats?.win_rate||0;
  const bk=d.config?.bankroll||0;
  const dr=d.anchor?.drift_pct??d.strategy?.drift_pct;
  const op=d.anchor?.open_price;
  const t=timer();

  eq.push(bk);if(eq.length>200)eq.shift();

  // Hero
  document.getElementById('hc').textContent=d.cycle||0;
  const hp=document.getElementById('hp');
  hp.textContent=`${p>=0?'+':''}$${Math.abs(p).toFixed(2)}`;
  hp.style.color=pc;
  hp.style.textShadow=`0 0 30px ${pc}33`;
  document.getElementById('ht').textContent=d.stats?.total_trades||0;
  document.getElementById('hw').textContent=d.stats?.total_wagered||'0.00';
  document.getElementById('hwr').textContent=`${wr}%`;
  document.getElementById('hbtc').textContent=`$${parseFloat(d.oracle?.price||0).toLocaleString()}`;
  const te=document.getElementById('htm');te.textContent=t.text;te.style.color=t.color;
  const av=d.stats?.total_trades>0?p/d.stats.total_trades:0;
  document.getElementById('hav').textContent=`$${av.toFixed(2)}`;

  // Stat cards
  document.getElementById('sb1').textContent=`$${typeof bk==='number'?bk.toFixed(2):bk}`;
  const se=document.getElementById('sp');
  se.textContent=`${p>=0?'+':''}$${Math.abs(p).toFixed(2)}`;
  se.style.color=pc;
  document.getElementById('spa').style.background=pc;
  document.getElementById('swl').textContent=`${d.stats?.wins||0}W – ${d.stats?.losses||0}L`;
  document.getElementById('so').textContent=op?`$${parseFloat(op).toLocaleString()}`:'—';
  document.getElementById('sdr').textContent=dr!=null?`Drift: ${dr>0?'+':''}${parseFloat(dr).toFixed(4)}%`:'Waiting...';
  const di=document.getElementById('sdi');
  di.textContent=(d.strategy?.direction||'hold').toUpperCase();
  di.style.color=d.strategy?.direction==='up'?'#4ade80':d.strategy?.direction==='down'?'#f87171':'#4a5568';
  document.getElementById('scf').textContent=`Confidence: ${((d.strategy?.confidence||0)*100).toFixed(1)}%`;
  document.getElementById('sr').textContent=`${d.risk?.daily_trades||0}/${d.risk?.max_daily_trades||20}`;
  document.getElementById('ss').textContent=`Streak: ${d.risk?.consecutive_losses||0}`;

  // Equity
  const epl=document.getElementById('ep');
  epl.textContent=`${p>=0?'+':''}$${Math.abs(p).toFixed(2)}`;
  epl.style.color=pc;
  drawEq();

  renderPositions(d);
  renderArb(d);

  // Signals
  const sigs=d.signals||{},sk=Object.keys(sigs);
  if(sk.length>0){
    document.getElementById('sg').innerHTML=sk.map(n=>{
      const s=sigs[n],u=s.direction==='up';
      const c=u?'#4ade80':'#f87171';
      const l=n.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
      const x=n==='rsi'?`RSI: ${s.raw_value}`:n==='price_vs_open'?`Drift: ${s.raw_value>0?'+':''}${s.raw_value}%`:'';
      return`<div class="sr"><div class="si ${u?'su':'sd'}">${u?'\u25b2':'\u25bc'}</div><div style="flex:1"><div style="font-size:12px;font-weight:600;color:#94a3b8">${l}</div>${x?`<div style="font-size:10px;color:#4a5568">${x}</div>`:''}</div><div class="mono" style="font-size:13px;font-weight:700;color:${c}">${s.direction.toUpperCase()} ${Math.round(s.strength*100)}%</div></div>`
    }).join('')
  }

  // Engines
  const engines=[
    {n:'Directional 15m',on:true,c:'#4ade80'},
    {n:'Directional 5m',on:d.config?.fivem_enabled!==false,c:'#a855f7'},
    {n:'Late-Window',on:d.config?.late_window_enabled!==false,c:'#38bdf8'},
    {n:'Arbitrage',on:d.config?.arb_enabled,c:'#f59e0b'},
    {n:'Market Maker',on:d.config?.mm_enabled,c:'#f472b6'},
    {n:'Hedge',on:d.config?.hedge_enabled,c:'#818cf8'}
  ];
  document.getElementById('en').innerHTML=engines.map(m=>
    `<div class="engine-row"><div class="dot" style="background:${m.on?m.c:'#2a2a3a'}"></div><span style="flex:1;font-size:12px;font-weight:500;color:${m.on?'#94a3b8':'#4a5568'}">${m.n}</span><span class="engine-badge" style="background:${m.on?m.c+'15':'#1a1a2a'};color:${m.on?m.c:'#4a5568'}">${m.on?'ACTIVE':'OFF'}</span></div>`
  ).join('');

  // Oracle sources
  document.getElementById('os').innerHTML=(d.oracle?.sources||[]).map(s=>
    `<div style="display:flex;align-items:center;gap:6px;padding:4px 0"><div class="dot" style="background:${s==='chainlink'?'#f59e0b':'#4ade80'}"></div><span style="font-size:11px;color:${s==='chainlink'?'#f59e0b':'#94a3b8'};font-weight:${s==='chainlink'?700:400}">${s}${s==='chainlink'?' (resolution)':''}</span></div>`
  ).join('');
  document.getElementById('osp').textContent=d.oracle?.spread_pct||0;

  // Decision log
  const dlDir=document.getElementById('dl-dir');
  dlDir.textContent=d.strategy?.direction||'hold';
  dlDir.style.color=d.strategy?.direction==='up'?'#4ade80':d.strategy?.direction==='down'?'#f87171':'#4a5568';
  dlDir.style.fontWeight=700;
  document.getElementById('dl-conf').textContent=`${((d.strategy?.confidence||0)*100).toFixed(1)}%`;
  const dlTrade=document.getElementById('dl-trade');
  dlTrade.textContent=String(d.strategy?.should_trade??false);
  dlTrade.style.color=d.strategy?.should_trade?'#4ade80':'#f87171';
  const drft=d.strategy?.drift_pct;
  document.getElementById('dl-drift').textContent=drft!=null?`${drft>0?'+':''}${drft.toFixed(4)}%`:'\u2014';
  document.getElementById('dl-vol').textContent=d.strategy?.volatility_pct!=null?`${d.strategy.volatility_pct.toFixed(3)}%`:'\u2014';
  document.getElementById('dl-reason').textContent=d.strategy?.reason||'\u2014';

  // Activity feed
  const feed=[];
  const opArr=d.positions?.open||[];
  const clArr=d.positions?.closed||[];
  opArr.forEach(p=>{feed.push({ts:p.timestamp,icon:'\ud83d\udd34',text:`${(p.direction||'').toUpperCase()} $${(p.size_usd||0).toFixed(2)} @ ${(p.entry_price||0).toFixed(3)}`,color:p.direction==='up'?'#4ade80':'#f87171'})});
  clArr.slice(-5).reverse().forEach(p=>{const win=p.outcome==='win'||p.pnl>0;feed.push({ts:p.timestamp,icon:win?'\u2705':'\u274c',text:`${(p.direction||'').toUpperCase()} \u2192 ${win?'WIN':'LOSS'} ${p.pnl>=0?'+':''}$${(p.pnl||0).toFixed(2)}`,color:win?'#4ade80':'#f87171'})});
  if(d.risk?.cooldown_active){feed.push({ts:Date.now()/1000,icon:'\u23f8\ufe0f',text:'Loss streak cooldown active',color:'#f59e0b'})}
  if(d.anchor?.open_price){feed.push({ts:Date.now()/1000-30,icon:'\ud83d\udccc',text:`Anchor: $${parseFloat(d.anchor.open_price).toLocaleString()} (${d.anchor.source||'?'})`,color:'#818cf8'})}
  feed.sort((a,b)=>(b.ts||0)-(a.ts||0));
  const afEl=document.getElementById('activity-feed');
  if(feed.length===0){afEl.innerHTML='<div style="padding:16px;text-align:center;color:#4a5568;font-size:11px">No activity yet</div>'}
  else{afEl.innerHTML=feed.slice(0,8).map((f,i)=>`<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 0;${i<feed.length-1?'border-bottom:1px solid #1e1e2e':''}"><span style="font-size:12px;flex-shrink:0">${f.icon}</span><div style="flex:1"><div style="font-size:11px;color:${f.color};font-weight:600">${f.text}</div><div class="mono" style="font-size:9px;color:#4a5568">${fmtTime(f.ts)}</div></div></div>`).join('')}

  // RTDS
  const hasCL=d.oracle?.sources?.includes('chainlink');
  document.getElementById('rtds-dot').style.background=hasCL?'#4ade80':'#f59e0b';
  document.getElementById('rtds-status').textContent=hasCL?'Streaming':'Buffered';
  document.getElementById('rtds-status').style.color=hasCL?'#4ade80':'#f59e0b';
  document.getElementById('rtds-detail').textContent=hasCL?`CL: $${(d.oracle?.chainlink||0).toLocaleString()}`:'Using REST fallback';
}

function conn(){
  const p=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(`${p}://${location.host}/ws`);
  ws.onopen=()=>{
    document.getElementById('sb').style.background='rgba(74,222,128,.15)';
    document.getElementById('sb').style.borderColor='rgba(74,222,128,.25)';
    document.getElementById('sd').style.background='#4ade80';
    document.getElementById('st').textContent='LIVE';
    document.getElementById('st').style.color='#4ade80';
    document.getElementById('ci').textContent='Connected';
  };
  ws.onmessage=e=>{try{
    const msg=JSON.parse(e.data);
    if(msg.type==='price_tick'){handlePriceTick(msg)}
    else if(msg.type==='trade_notification'){showTradeToast(msg)}
    else{state=msg;render(msg);pulseHeartbeat()}
  }catch{}};
  ws.onclose=()=>{
    document.getElementById('sb').style.background='rgba(245,158,11,.15)';
    document.getElementById('sb').style.borderColor='rgba(245,158,11,.25)';
    document.getElementById('sd').style.background='#f59e0b';
    document.getElementById('sd').style.animation='pulse 2s infinite';
    document.getElementById('st').textContent='RECONNECTING';
    document.getElementById('st').style.color='#f59e0b';
    document.getElementById('ci').textContent='Retrying...';
    setTimeout(conn,3000);
  };
  ws.onerror=()=>ws.close();
}
// ── Live Price Tick (between cycles) ──
let lastPrice=0,tickCount=0;
function handlePriceTick(msg){
  const p=msg.price||msg.chainlink||msg.binance||0;
  if(p>0){
    const el=document.getElementById('hbtc');
    if(el){
      const prev=lastPrice||parseFloat(el.textContent.replace(/[$,]/g,''))||0;
      el.textContent='$'+p.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
      // Flash direction
      el.classList.remove('price-up','price-down');
      if(prev>0&&p!==prev){
        el.classList.add(p>prev?'price-up':'price-down');
        // Tick arrow
        let arrow=document.getElementById('tick-arrow');
        if(!arrow){arrow=document.createElement('span');arrow.id='tick-arrow';arrow.className='tick-arrow';el.parentNode.appendChild(arrow)}
        arrow.className='tick-arrow '+(p>prev?'up':'down');
        arrow.textContent=p>prev?'\u25B2':'\u25BC';
        setTimeout(()=>{el.classList.remove('price-up','price-down')},800);
      }
      lastPrice=p;
    }
    // Update last tick time
    const lt=document.getElementById('last-tick');
    if(lt){tickCount++;lt.textContent='tick #'+tickCount}
    // Pulse heartbeat on data
    pulseHeartbeat();
  }
}

// ── Heartbeat System ──
let hbTimeout;
function pulseHeartbeat(){
  const dot=document.getElementById('hb-dot');
  const bars=document.getElementById('hb-line');
  if(dot){dot.style.background='#4ade80';dot.style.boxShadow='0 0 8px rgba(74,222,128,.6)'}
  if(bars){bars.querySelectorAll('.hb-bar').forEach(b=>{b.style.background='#4ade80'})}
  clearTimeout(hbTimeout);
  hbTimeout=setTimeout(()=>{
    if(dot){dot.style.background='#f59e0b';dot.style.boxShadow='none'}
    if(bars){bars.querySelectorAll('.hb-bar').forEach(b=>{b.style.background='#f59e0b'})}
  },5000);
}

// ── Live Clock ──
setInterval(()=>{
  const el=document.getElementById('live-clock');
  if(el){el.textContent=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
},1000);

// ── Trade Toast Notifications ──
const ENGINE_LABELS={'directional':'15m','directional_5m':'5m','late_window':'LW','arb':'ARB','mm':'MM'};
function showTradeToast(msg){
  const container=document.getElementById('toasts');
  if(!container)return;
  const toast=document.createElement('div');
  const isResolved=msg.action==='resolved';
  const isWin=msg.outcome==='win';
  const up=(msg.direction||'').toLowerCase()==='up';
  const dir=up?'UP':'DOWN';
  const dirColor=up?'#4ade80':'#f87171';
  const engine=ENGINE_LABELS[msg.engine]||msg.engine||'';

  let cls='toast trade';
  let icon=up?'\u{1F7E2}':'\u{1F534}';
  let title='';
  let detail='';
  let pnlHtml='';

  if(isResolved){
    cls='toast '+(isWin?'win':'loss');
    icon=isWin?'\u2705':'\u274C';
    title=(isWin?'WIN':'LOSS')+' \u2014 '+dir+' ['+engine+']';
    detail='$'+(msg.size_usd||0).toFixed(2)+' trade resolved';
    const pnl=msg.pnl||0;
    pnlHtml=`<div class="toast-pnl" style="color:${pnl>=0?'#4ade80':'#f87171'}">${pnl>=0?'+':''}$${Math.abs(pnl).toFixed(2)}</div>`;
  }else{
    title='TRADE OPENED \u2014 '+dir+' ['+engine+']';
    detail='$'+(msg.size_usd||0).toFixed(2)+' @ '+(msg.entry_price||0).toFixed(3);
    icon='\u{1F4C8}';
  }

  toast.className=cls;
  toast.innerHTML=`<span class="toast-icon">${icon}</span><div class="toast-body"><div class="toast-title">${title}</div><div class="toast-detail mono">${detail}</div></div>${pnlHtml}`;
  container.appendChild(toast);

  // Auto-dismiss after 6s
  setTimeout(()=>{
    toast.classList.add('toast-exit');
    setTimeout(()=>toast.remove(),300);
  },6000);
}

setInterval(()=>{const t=timer();const e=document.getElementById('htm');if(e){e.textContent=t.text;e.style.color=t.color}},1000);
conn();drawEq();
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GRIDPHANTOMDEV — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
{HERO}
<div style="max-width:1100px;margin:0 auto">
{STAT_CARDS}
  <div class="main-grid">
    <div style="display:flex;flex-direction:column;gap:14px">
      <div class="card eq-card"><div style="display:flex;justify-content:space-between;margin-bottom:8px"><span style="font-size:12px;font-weight:700;color:#94a3b8">Equity Curve</span><span class="mono" style="font-size:12px;font-weight:700" id="ep">+$0.00</span></div><canvas id="ec" height="80" style="width:100%;display:block"></canvas></div>
{ARB_PANEL}
{POSITIONS}
    </div>
{RIGHT_SIDEBAR}
  </div>
  <div style="margin-top:20px;padding-bottom:20px;text-align:center;font-size:10px;color:#2a2a3a;letter-spacing:.02em">GRIDPHANTOMDEV &middot; clausea4bswa3mbtc bot v2.5 &middot; Chainlink resolution</div>
</div>
<div class="toast-container" id="toasts"></div>
<script>{JS}</script>
</body>
</html>"""
