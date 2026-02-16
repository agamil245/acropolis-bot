# 🚀 Quick Start Guide - AcropolisBot

## Installation (2 minutes)

```bash
# 1. Clone the repo
git clone https://github.com/agamil245/acropolis-bot.git
cd acropolis-bot

# 2. Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Install dependencies
uv sync

# 4. Configure
cp .env.example .env
# Edit .env if needed (defaults work for paper trading)
```

## Running the Bot

### Option 1: Web Dashboard (Recommended) 🌐

```bash
uv run python web.py
```

Then open: **http://localhost:8080**

Click **"Start Bot"** in the dashboard.

### Option 2: Command Line 💻

```bash
uv run python main.py
```

## What to Expect

### Micro-Arbitrage Strategy (PRIMARY)
- Checks prices every **100ms**
- Looks for markets where YES + NO < $0.98
- Executes instantly when found
- **Example**: If BTC 5m has YES=$0.47, NO=$0.48 (combined=$0.95), bot buys the underpriced side for a 5% edge

### Streak Reversal Strategy
- Monitors last 10 outcomes for each market
- When 4+ consecutive same outcomes detected, bets on reversal
- Historical win rate: **67-82%**

### First Run
1. Bot starts in **PAPER MODE** (no real money)
2. Initial bankroll: **$100** (virtual)
3. Monitors: **BTC 5m, ETH 5m, SOL 5m** by default
4. See trades in dashboard or console logs

## Key Settings (in .env)

```bash
# Must-configure for live trading
PAPER_TRADE=true              # Set to false for live trading
PRIVATE_KEY=0x...             # Your wallet key (for live)

# Strategy toggles
ENABLE_ARBITRAGE=true         # THE MONEY MAKER
ENABLE_STREAK=true
ENABLE_COPYTRADE=false

# Risk management
INITIAL_BANKROLL=100.0        # Starting amount
RISK_LEVEL=moderate           # conservative/moderate/aggressive
MAX_DAILY_LOSS=100            # Stop if lose this much

# Arbitrage tuning
ARB_THRESHOLD=0.98            # Buy when YES+NO < this
ARB_MIN_EDGE_PCT=0.5          # Minimum edge to trade
```

## Understanding the Dashboard

### Stats Cards
- **Bankroll**: Current virtual/real balance
- **Win Rate**: Percentage of winning trades
- **Daily P&L**: Today's profit/loss
- **Active Trades**: Pending settlements

### Strategy Performance
- Shows P&L per strategy
- Arbitrage should be most profitable
- Streak should have high win rate
- Copytrade depends on wallets followed

### Trade Log
- Real-time trade updates
- Filter by pending/settled
- Shows execution price, slippage, P&L

## Tips for Success

### 1. Start with Paper Trading
- Run for a day to understand behavior
- Watch how arbitrage opportunities appear
- Check win rates and P&L trends

### 2. Optimize for Arbitrage
- This is your **primary edge**
- Lower ARB_THRESHOLD = fewer opportunities but higher edge
- Higher threshold = more trades but lower edge per trade
- Default 0.98 (2% edge) is balanced

### 3. Manage Risk
- Don't risk more than you can lose
- Start conservative, scale up slowly
- Watch the circuit breaker (pauses after losses)
- Daily limits protect from bad days

### 4. Monitor Performance
- Check per-strategy P&L
- If arbitrage isn't profitable, markets may be efficient
- Streak works best on volatile days
- Adjust bet sizes based on bankroll growth

## Common Issues

### "Bot not trading"
- Check if markets are open (5m/15m intervals)
- Verify strategies are enabled
- Check circuit breaker status (may be paused)
- Look for "can_trade" messages in logs

### "No arbitrage opportunities"
- Markets might be efficiently priced
- Try lowering ARB_MIN_EDGE_PCT to 0.3
- Check more markets (add ETH_15M, SOL_15M)
- Arbitrage is rare but profitable

### "Streak not triggering"
- Need 4+ consecutive outcomes (takes time)
- Check STREAK_TRIGGER in config
- May take 20-30 minutes to build a streak

## Going Live

### Prerequisites
1. Funded Polygon wallet with USDC
2. Private key from MetaMask/wallet
3. Tested in paper mode first
4. Comfortable with the risks

### Steps
1. Edit `.env`:
   ```bash
   PAPER_TRADE=false
   PRIVATE_KEY=0x_your_actual_key_here
   ```
2. Start small:
   ```bash
   INITIAL_BANKROLL=50.0
   MIN_BET=5
   MAX_BET=20
   ```
3. Start the bot
4. Monitor closely for first few hours
5. Scale up gradually as confidence grows

## Support

- **Issues**: https://github.com/agamil245/acropolis-bot/issues
- **Docs**: README.md in the repo
- **Code**: Fully open source, inspect and modify

---

**Remember**: This is experimental. Start small, test thoroughly, never risk what you can't afford to lose. 🏛️

**The arbitrage edge is real, but markets adapt. What works today may not work tomorrow.**
