"""FastAPI web server for AcropolisBot dashboard.

Full-featured backend with WebSocket real-time updates, REST API,
per-strategy toggles, market selectors, and trade history pagination.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from src.config import Config, LOCAL_TZ, MarketType
from src.bot_engine import BotEngine


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBALS
# ═══════════════════════════════════════════════════════════════════════════════

bot: Optional[BotEngine] = None
bot_task: Optional[asyncio.Task] = None
active_connections: list[WebSocket] = []
_broadcaster_task: Optional[asyncio.Task] = None

# Track runtime-level overrides (strategies / markets)
strategy_overrides: dict[str, bool] = {}
market_overrides: dict[str, bool] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# LIFESPAN
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _broadcaster_task
    _broadcaster_task = asyncio.create_task(_status_broadcaster())
    yield
    # Shutdown
    if _broadcaster_task:
        _broadcaster_task.cancel()
    global bot, bot_task
    if bot and bot.running:
        await bot.stop()
    if bot_task:
        bot_task.cancel()


# ═══════════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent.parent.parent
app = FastAPI(title="AcropolisBot", version="2.0.0", lifespan=lifespan)

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ═══════════════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ═══════════════════════════════════════════════════════════════════════════════
# STATUS / STATS
# ═══════════════════════════════════════════════════════════════════════════════

def _build_status() -> dict:
    """Build comprehensive status payload."""
    if not bot:
        return {
            "running": False,
            "mode": "PAPER" if Config.PAPER_TRADE else "LIVE",
            "uptime_seconds": 0,
            "bankroll": Config.INITIAL_BANKROLL,
            "total_pnl": 0,
            "daily_pnl": 0,
            "daily_bets": 0,
            "win_rate": 0,
            "wins": 0,
            "losses": 0,
            "pending_trades": 0,
            "total_trades": 0,
            "strategies": {
                "arbitrage": {"enabled": Config.ENABLE_ARBITRAGE, "pnl": 0, "trades": 0, "wins": 0, "losses": 0},
                "streak": {"enabled": Config.ENABLE_STREAK, "pnl": 0, "trades": 0, "wins": 0, "losses": 0},
                "copytrade": {"enabled": Config.ENABLE_COPYTRADE, "pnl": 0, "trades": 0, "wins": 0, "losses": 0},
            },
            "active_markets": [m.value for m in Config.ACTIVE_MARKETS],
            "all_markets": _all_markets_status(),
            "websocket_connected": False,
            "paper_trading": {
                "running": False,
                "stats": {
                    "bankroll": Config.PAPER_INITIAL_BANKROLL,
                    "initial_bankroll": Config.PAPER_INITIAL_BANKROLL,
                    "peak_bankroll": Config.PAPER_INITIAL_BANKROLL,
                    "total_pnl": 0, "win_rate": 0, "wins": 0, "losses": 0,
                    "total_trades": 0, "pending_trades": 0, "settled_trades": 0, "drawdown_pct": 0,
                },
            },
        }

    status = bot.get_status()

    # Per-strategy stats from trade history
    strat_stats = {"arbitrage": _empty_strat(), "streak": _empty_strat(), "copytrade": _empty_strat()}
    for t in bot.state.trades:
        s = t.strategy if t.strategy in strat_stats else "copytrade"
        if s not in strat_stats:
            continue
        strat_stats[s]["trades"] += 1
        if t.outcome:
            strat_stats[s]["pnl"] += t.net_pnl
            if t.won:
                strat_stats[s]["wins"] += 1
            else:
                strat_stats[s]["losses"] += 1

    # Merge enabled flags
    strat_enabled = status.get("strategies", {})
    for k in strat_stats:
        strat_stats[k]["enabled"] = strategy_overrides.get(k, strat_enabled.get(k, False))

    return {
        "running": status.get("running", False),
        "mode": status.get("mode", "PAPER"),
        "uptime_seconds": status.get("uptime_seconds", 0),
        "bankroll": status.get("bankroll", Config.INITIAL_BANKROLL),
        "total_pnl": status.get("total_pnl", 0),
        "daily_pnl": status.get("daily_pnl", 0),
        "daily_bets": status.get("daily_bets", 0),
        "win_rate": status.get("win_rate", 0),
        "wins": status.get("wins", 0),
        "losses": status.get("losses", 0),
        "pending_trades": status.get("pending_trades", 0),
        "total_trades": status.get("total_trades", 0),
        "strategies": strat_stats,
        "active_markets": status.get("active_markets", []),
        "all_markets": _all_markets_status(),
        "websocket_connected": status.get("websocket_connected", False),
        "session": status.get("session", {}),
        "paper_trading": status.get("paper_trading", {
            "running": False,
            "stats": {"bankroll": Config.PAPER_INITIAL_BANKROLL, "total_pnl": 0, "win_rate": 0,
                       "wins": 0, "losses": 0, "total_trades": 0, "pending_trades": 0,
                       "settled_trades": 0, "initial_bankroll": Config.PAPER_INITIAL_BANKROLL,
                       "peak_bankroll": Config.PAPER_INITIAL_BANKROLL, "drawdown_pct": 0},
        }),
    }


def _empty_strat():
    return {"enabled": False, "pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}


def _all_markets_status() -> dict:
    """Return all markets with enabled/disabled status."""
    active_values = {m.value for m in Config.ACTIVE_MARKETS}
    result = {}
    for mt in MarketType:
        override = market_overrides.get(mt.value)
        if override is not None:
            result[mt.value] = {"name": mt.display_name, "enabled": override}
        else:
            result[mt.value] = {"name": mt.display_name, "enabled": mt.value in active_values}
    return result


@app.get("/api/status")
async def get_status():
    return JSONResponse(_build_status())


@app.get("/api/pnl-history")
async def get_pnl_history(range: str = Query("24h")):
    """Return timestamped cumulative P&L data points for charting."""
    points = []
    if not bot or not hasattr(bot, 'state') or not bot.state.trades:
        return JSONResponse({"points": points, "range": range})

    # Determine time window
    range_seconds = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}.get(range, 86400)
    cutoff_ms = (time.time() - range_seconds) * 1000

    cumulative = 0.0
    for t in bot.state.trades:
        if t.executed_at < cutoff_ms:
            cumulative += t.net_pnl
            continue
        cumulative += t.net_pnl
        ts = datetime.fromtimestamp(t.executed_at / 1000, tz=LOCAL_TZ)
        if range in ("1h", "6h"):
            label = ts.strftime("%H:%M")
        elif range == "7d":
            label = ts.strftime("%m/%d %H:%M")
        else:
            label = ts.strftime("%H:%M")
        points.append({"time": label, "pnl": round(cumulative, 2)})

    # If no points, add a zero point
    if not points:
        now = datetime.now(tz=LOCAL_TZ)
        points.append({"time": now.strftime("%H:%M"), "pnl": 0.0})

    return JSONResponse({"points": points, "range": range})


@app.get("/api/stats")
async def get_stats():
    if not bot:
        return JSONResponse({"error": "Bot not initialized"})
    return JSONResponse(bot.state.get_statistics())


# ═══════════════════════════════════════════════════════════════════════════════
# TRADES (with pagination)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/trades")
async def get_trades(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    strategy: Optional[str] = None,
    status: Optional[str] = None,
):
    if not bot:
        return JSONResponse({"trades": [], "total": 0})

    trades = list(bot.state.trades)

    # Filter
    if strategy:
        trades = [t for t in trades if t.strategy == strategy]
    if status == "pending":
        trades = [t for t in trades if not t.outcome]
    elif status == "settled":
        trades = [t for t in trades if t.outcome]

    total = len(trades)
    trades = list(reversed(trades))  # newest first
    page = trades[offset:offset + limit]

    trades_data = []
    for t in page:
        trades_data.append({
            "id": t.id,
            "timestamp": datetime.fromtimestamp(t.executed_at / 1000, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "market": t.market_slug,
            "strategy": t.strategy,
            "direction": t.direction.upper(),
            "amount": round(t.amount, 2),
            "price": round(t.execution_price, 4),
            "outcome": t.outcome.upper() if t.outcome else "PENDING",
            "won": t.won,
            "pnl": round(t.net_pnl, 2),
            "status": "settled" if t.outcome else "pending",
        })

    return JSONResponse({"trades": trades_data, "total": total, "offset": offset, "limit": limit})


# ═══════════════════════════════════════════════════════════════════════════════
# BOT CONTROL
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/bot/start")
async def start_bot():
    global bot, bot_task
    if bot and bot.running:
        return JSONResponse({"error": "Bot already running"}, status_code=400)
    try:
        bot = BotEngine()
        bot_task = asyncio.create_task(bot.start())
        await _broadcast_status()
        return JSONResponse({"success": True, "message": "Bot started"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/bot/stop")
async def stop_bot():
    global bot, bot_task
    if not bot or not bot.running:
        return JSONResponse({"error": "Bot not running"}, status_code=400)
    try:
        await bot.stop()
        if bot_task:
            bot_task.cancel()
            bot_task = None
        await _broadcast_status()
        return JSONResponse({"success": True, "message": "Bot stopped"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY TOGGLES
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/strategy/{name}/enable")
async def enable_strategy(name: str):
    if name not in ("arbitrage", "streak", "copytrade"):
        return JSONResponse({"error": "Unknown strategy"}, status_code=400)
    strategy_overrides[name] = True
    # Apply to Config at runtime
    if name == "arbitrage":
        Config.ENABLE_ARBITRAGE = True
    elif name == "streak":
        Config.ENABLE_STREAK = True
    elif name == "copytrade":
        Config.ENABLE_COPYTRADE = True
    await _broadcast_status()
    return JSONResponse({"success": True, "strategy": name, "enabled": True})


@app.post("/api/strategy/{name}/disable")
async def disable_strategy(name: str):
    if name not in ("arbitrage", "streak", "copytrade"):
        return JSONResponse({"error": "Unknown strategy"}, status_code=400)
    strategy_overrides[name] = False
    if name == "arbitrage":
        Config.ENABLE_ARBITRAGE = False
    elif name == "streak":
        Config.ENABLE_STREAK = False
    elif name == "copytrade":
        Config.ENABLE_COPYTRADE = False
    await _broadcast_status()
    return JSONResponse({"success": True, "strategy": name, "enabled": False})


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET SELECTOR
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/market/{market_id}/enable")
async def enable_market(market_id: str):
    try:
        mt = MarketType(market_id)
    except ValueError:
        return JSONResponse({"error": "Unknown market"}, status_code=400)
    market_overrides[market_id] = True
    if mt not in Config.ACTIVE_MARKETS:
        Config.ACTIVE_MARKETS.append(mt)
    await _broadcast_status()
    return JSONResponse({"success": True, "market": market_id, "enabled": True})


@app.post("/api/market/{market_id}/disable")
async def disable_market(market_id: str):
    try:
        mt = MarketType(market_id)
    except ValueError:
        return JSONResponse({"error": "Unknown market"}, status_code=400)
    market_overrides[market_id] = False
    Config.ACTIVE_MARKETS = [m for m in Config.ACTIVE_MARKETS if m != mt]
    await _broadcast_status()
    return JSONResponse({"success": True, "market": market_id, "enabled": False})


# ═══════════════════════════════════════════════════════════════════════════════
# PAPER TRADING (Independent)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/paper/start")
async def start_paper():
    global bot
    if not bot:
        bot = BotEngine()
    if bot.paper_running:
        return JSONResponse({"error": "Paper trading already running"}, status_code=400)
    await bot.start_paper()
    await _broadcast_status()
    return JSONResponse({"success": True, "message": "Paper trading started"})


@app.post("/api/paper/stop")
async def stop_paper():
    if not bot or not bot.paper_running:
        return JSONResponse({"error": "Paper trading not running"}, status_code=400)
    await bot.stop_paper()
    await _broadcast_status()
    return JSONResponse({"success": True, "message": "Paper trading stopped"})


@app.get("/api/paper/stats")
async def get_paper_stats():
    if not bot:
        return JSONResponse({"bankroll": Config.PAPER_INITIAL_BANKROLL, "total_pnl": 0,
                             "win_rate": 0, "wins": 0, "losses": 0,
                             "total_trades": 0, "pending_trades": 0, "settled_trades": 0,
                             "initial_bankroll": Config.PAPER_INITIAL_BANKROLL,
                             "peak_bankroll": Config.PAPER_INITIAL_BANKROLL, "drawdown_pct": 0})
    return JSONResponse(bot.paper_engine.state.get_stats())


@app.get("/api/paper/trades")
async def get_paper_trades(limit: int = Query(50, ge=1, le=500)):
    if not bot:
        return JSONResponse({"trades": []})
    return JSONResponse({"trades": bot.paper_engine.get_recent_trades(limit)})


@app.get("/api/paper/export")
async def export_paper_trades():
    """Export full paper trade history as JSON download."""
    if not bot:
        return JSONResponse({"trades": []})
    trades = bot.paper_engine.get_trades_json()
    return JSONResponse(trades, headers={
        "Content-Disposition": "attachment; filename=paper_trades_export.json"
    })


@app.post("/api/paper/reset")
async def reset_paper():
    if not bot:
        return JSONResponse({"error": "Bot not initialized"}, status_code=400)
    if bot.paper_running:
        return JSONResponse({"error": "Stop paper trading before resetting"}, status_code=400)
    bot.paper_engine.reset()
    await _broadcast_status()
    return JSONResponse({"success": True, "message": "Paper trading reset"})


# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/settings")
async def update_settings(request: Request):
    body = await request.json()
    applied = {}

    field_map = {
        "paper_trade": ("PAPER_TRADE", bool),
        "initial_bankroll": ("INITIAL_BANKROLL", float),
        "bet_amount": ("BET_AMOUNT", float),
        "min_bet": ("MIN_BET", float),
        "max_bet": ("MAX_BET", float),
        "kelly_fraction": ("KELLY_FRACTION", float),
        "max_daily_bets": ("MAX_DAILY_BETS", int),
        "max_daily_loss": ("MAX_DAILY_LOSS", float),
        "streak_trigger": ("STREAK_TRIGGER", int),
        "arb_threshold": ("ARB_THRESHOLD", float),
        "arb_min_edge_pct": ("ARB_MIN_EDGE_PCT", float),
    }

    for key, value in body.items():
        if key in field_map:
            attr, typ = field_map[key]
            try:
                setattr(Config, attr, typ(value))
                applied[key] = typ(value)
            except (ValueError, TypeError):
                pass

    await _broadcast_status()
    return JSONResponse({"success": True, "applied": applied})


@app.get("/api/settings")
async def get_settings():
    return JSONResponse({
        "paper_trade": Config.PAPER_TRADE,
        "initial_bankroll": Config.INITIAL_BANKROLL,
        "bet_amount": Config.BET_AMOUNT,
        "min_bet": Config.MIN_BET,
        "max_bet": Config.MAX_BET,
        "kelly_fraction": Config.KELLY_FRACTION,
        "use_kelly": Config.USE_KELLY,
        "max_daily_bets": Config.MAX_DAILY_BETS,
        "max_daily_loss": Config.MAX_DAILY_LOSS,
        "risk_level": Config.RISK_LEVEL.value,
        "streak_trigger": Config.STREAK_TRIGGER,
        "arb_threshold": Config.ARB_THRESHOLD,
        "arb_min_edge_pct": Config.ARB_MIN_EDGE_PCT,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        # Send initial status
        await websocket.send_json({"type": "status", "data": _build_status()})
        while True:
            data = await websocket.receive_text()
            # Handle client pings / commands if needed
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)


async def _broadcast_status():
    if not active_connections:
        return
    payload = {"type": "status", "data": _build_status()}
    dead = []
    for ws in active_connections:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        active_connections.remove(ws)


async def _broadcast_trade(trade_data: dict):
    payload = {"type": "trade", "data": trade_data}
    dead = []
    for ws in active_connections:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        active_connections.remove(ws)


async def _status_broadcaster():
    """Push status to all WS clients every 1 second."""
    while True:
        await asyncio.sleep(1)
        await _broadcast_status()


# ═══════════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════════

def run_server(host: str = None, port: int = None):
    host = host or Config.WEB_HOST
    port = port or Config.WEB_PORT
    print(f"\n🌐 AcropolisBot Dashboard → http://{host}:{port}\n")
    uvicorn.run("src.web.server:app", host=host, port=port, reload=False, log_level="info")
