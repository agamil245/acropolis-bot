"""FastAPI web server for AcropolisBot dashboard."""

import asyncio
import json
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from src.config import Config, LOCAL_TZ, MarketType
from src.bot_engine import BotEngine


app = FastAPI(title="AcropolisBot", version="1.0.0")

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Global bot instance
bot: Optional[BotEngine] = None
bot_task: Optional[asyncio.Task] = None

# WebSocket connections
active_connections: list[WebSocket] = []


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render main dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/api/status")
async def get_status():
    """Get bot status."""
    if not bot:
        return JSONResponse({
            "running": False,
            "error": "Bot not initialized"
        })
    
    status = bot.get_status()
    return JSONResponse(status)


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    """Get recent trades."""
    if not bot:
        return JSONResponse({"trades": []})
    
    trades = bot.state.trades[-limit:]
    trades_data = []
    
    for t in reversed(trades):
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
            "status": "settled" if t.outcome else "pending"
        })
    
    return JSONResponse({"trades": trades_data})


@app.get("/api/statistics")
async def get_statistics():
    """Get comprehensive statistics."""
    if not bot:
        return JSONResponse({"error": "Bot not initialized"})
    
    stats = bot.state.get_statistics()
    return JSONResponse(stats)


@app.post("/api/start")
async def start_bot():
    """Start the bot."""
    global bot, bot_task
    
    if bot and bot.running:
        return JSONResponse({"error": "Bot already running"}, status_code=400)
    
    try:
        # Initialize bot
        bot = BotEngine()
        
        # Start bot in background task
        bot_task = asyncio.create_task(bot.start())
        
        # Broadcast status update
        await broadcast_status()
        
        return JSONResponse({"success": True, "message": "Bot started"})
    
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/stop")
async def stop_bot():
    """Stop the bot."""
    global bot, bot_task
    
    if not bot or not bot.running:
        return JSONResponse({"error": "Bot not running"}, status_code=400)
    
    try:
        await bot.stop()
        
        if bot_task:
            bot_task.cancel()
            bot_task = None
        
        # Broadcast status update
        await broadcast_status()
        
        return JSONResponse({"success": True, "message": "Bot stopped"})
    
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/config")
async def get_config():
    """Get current configuration."""
    return JSONResponse({
        "markets": {
            market.name: market.name in [m.name for m in Config.ACTIVE_MARKETS]
            for market in MarketType
        },
        "strategies": {
            "arbitrage": Config.ENABLE_ARBITRAGE,
            "streak": Config.ENABLE_STREAK,
            "copytrade": Config.ENABLE_COPYTRADE,
            "selective": Config.ENABLE_SELECTIVE,
        },
        "risk": {
            "level": Config.RISK_LEVEL.value,
            "kelly_fraction": Config.KELLY_FRACTION,
            "max_daily_bets": Config.MAX_DAILY_BETS,
            "max_daily_loss": Config.MAX_DAILY_LOSS,
            "min_bet": Config.MIN_BET,
            "max_bet": Config.MAX_BET,
        },
        "arbitrage": {
            "threshold": Config.ARB_THRESHOLD,
            "min_edge_pct": Config.ARB_MIN_EDGE_PCT,
            "max_exposure": Config.ARB_MAX_EXPOSURE,
        },
        "mode": "PAPER" if Config.PAPER_TRADE else "LIVE"
    })


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time updates."""
    await websocket.accept()
    active_connections.append(websocket)
    
    try:
        # Send initial status
        if bot:
            status = bot.get_status()
            await websocket.send_json({"type": "status", "data": status})
        
        # Keep connection alive and listen for messages
        while True:
            data = await websocket.receive_text()
            # Could handle client messages here
            
    except WebSocketDisconnect:
        active_connections.remove(websocket)


async def broadcast_status():
    """Broadcast status update to all connected clients."""
    if not bot:
        return
    
    status = bot.get_status()
    message = {"type": "status", "data": status}
    
    for connection in active_connections:
        try:
            await connection.send_json(message)
        except:
            pass


async def broadcast_trade(trade):
    """Broadcast new trade to all connected clients."""
    message = {"type": "trade", "data": trade}
    
    for connection in active_connections:
        try:
            await connection.send_json(message)
        except:
            pass


# Periodic status broadcaster
async def status_broadcaster():
    """Broadcast status every 2 seconds."""
    while True:
        await asyncio.sleep(2)
        await broadcast_status()


@app.on_event("startup")
async def startup_event():
    """Start background tasks on startup."""
    asyncio.create_task(status_broadcaster())


@app.on_event("shutdown")
async def shutdown_event():
    """Clean shutdown."""
    global bot, bot_task
    
    if bot and bot.running:
        await bot.stop()
    
    if bot_task:
        bot_task.cancel()


def run_server(host: str = None, port: int = None):
    """Run the web server."""
    host = host or Config.WEB_HOST
    port = port or Config.WEB_PORT
    
    print(f"\n🌐 Starting AcropolisBot Web GUI on http://{host}:{port}\n")
    
    uvicorn.run(
        "src.web.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info"
    )
