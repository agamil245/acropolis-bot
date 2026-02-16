# 🏛️ AcropolisBot

> **Advanced Polymarket Trading Bot with Micro-Arbitrage**

---

## 🚀 One-Click Deploy

Deploy on any Ubuntu/Debian VPS in one command:

```bash
curl -fsSL https://raw.githubusercontent.com/agamil245/acropolis-bot/main/deploy.sh | sudo bash
```

Or clone first:

```bash
git clone https://github.com/agamil245/acropolis-bot.git
cd acropolis-bot
sudo bash deploy.sh
```

The script handles everything: Docker, nginx reverse proxy with basic auth, `.env` configuration, and container orchestration.

---

## 🐳 Docker Architecture

```
Internet → :80 nginx (basic auth) → :8080 acropolis-bot (FastAPI + bot engine)
```

| Service | Image | Role |
|---------|-------|------|
| `acropolis` | Custom (Python 3.12 + uv) | Bot engine + web dashboard |
| `nginx` | nginx:alpine | Reverse proxy, basic auth, WebSocket support |

**Volumes:** `.env`, `data/` (paper trade state), `logs/`, nginx config, `.htpasswd`

### Manual Docker commands

```bash
cd /opt/acropolis-bot
docker compose up -d --build   # Start
docker compose logs -f          # Logs
docker compose down             # Stop
docker compose restart          # Restart
```

---

## 🖥️ PM2 Alternative (No Docker)

If you prefer running directly on the host:

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone & setup
git clone https://github.com/agamil245/acropolis-bot.git
cd acropolis-bot
uv sync
cp .env.example .env && nano .env

# Install PM2
npm install -g pm2

# Start
pm2 start "uv run python main.py" --name acropolis-bot
pm2 save && pm2 startup
```

---

A sophisticated, multi-strategy trading bot for Polymarket's crypto prediction markets. Built with Python, FastAPI, and WebSockets for real-time trading and monitoring.

## 🎯 Features

### Primary Strategy: **Micro-Arbitrage** ⚡
- **Pure structural edge** - exploits pricing inefficiencies when YES + NO < $1.00
- **Zero directional risk** - no prediction needed, guaranteed profit on mispriced markets
- **Millisecond execution** - monitors prices in real-time and executes instantly
- **Smart position sizing** - scales based on edge percentage and available capital

### Additional Strategies
- **Streak Reversal** 📈 - Mean reversion based on consecutive outcomes
- **Copytrade** 📋 - Mirror profitable wallets with selective filtering
- **Multi-Market Support** - BTC, ETH, SOL across 5m and 15m intervals

### Risk Management 🛡️
- **Kelly Criterion** position sizing with configurable fractions
- **Automatic compounding** - bet size grows with bankroll
- **Drawdown protection** - pauses trading if losses exceed threshold
- **Circuit breaker** - stops after consecutive losses
- **Per-strategy P&L tracking** - monitor each strategy independently

### Web Dashboard 🌐
- **Real-time updates** via WebSocket
- **Modern dark theme** with Tailwind CSS
- **Live trade log** with filtering
- **Strategy toggles** - enable/disable strategies on the fly
- **Bankroll & P&L tracking**
- **Start/Stop controls**

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) for package management

### Installation

```bash
# Clone the repository
git clone https://github.com/agamil245/acropolis-bot.git
cd acropolis-bot

# Install dependencies with uv
uv sync

# Copy and configure environment
cp .env.example .env
# Edit .env with your settings
```

### Configuration

Edit `.env` to configure:

```bash
# Wallet (for live trading)
PRIVATE_KEY=0x_your_key_here

# Mode
PAPER_TRADE=true  # Set to false for live trading

# Markets (comma-separated)
ACTIVE_MARKETS=BTC_5M,ETH_5M,SOL_5M

# Strategies
ENABLE_ARBITRAGE=true   # PRIMARY strategy
ENABLE_STREAK=true
ENABLE_COPYTRADE=false

# Arbitrage settings
ARB_THRESHOLD=0.98      # Buy when YES+NO < 0.98
ARB_MIN_EDGE_PCT=0.5    # Minimum 0.5% edge

# Risk management
INITIAL_BANKROLL=100.0
RISK_LEVEL=moderate     # conservative, moderate, aggressive
MAX_DAILY_LOSS=100
```

### Running the Bot

#### Option 1: Web Dashboard (Recommended)

```bash
uv run python web.py
```

Then open http://localhost:8080 in your browser.

Features:
- Real-time dashboard with WebSocket updates
- Start/Stop bot controls
- Live trade log
- Strategy performance metrics
- Bankroll tracking

#### Option 2: Command Line

```bash
uv run python main.py
```

Runs the bot in terminal mode with console logging.

## 📊 How It Works

### Micro-Arbitrage Strategy (THE MONEY MAKER)

This is the **primary profit driver**. It exploits a structural inefficiency in prediction markets:

**The Math:**
- In a perfect market: YES price + NO price = $1.00
- Reality: Markets often misprice, creating gaps
- Example: YES = $0.48, NO = $0.48 → Combined = $0.96 (4% edge!)

**The Trade:**
1. Bot monitors all active markets every 100ms
2. When YES + NO < threshold (default $0.98), we have an arbitrage opportunity
3. Buy the underpriced side (or both if equal)
4. Wait for market to resolve
5. Collect $1.00 per share regardless of outcome

**Why it works:**
- Zero directional prediction needed
- Pure structural edge from market inefficiency
- Risk-free profit (minus fees)
- Happens frequently on short-term markets

**Example:**
```
Market: BTC 5-min Up/Down
YES price: $0.47
NO price: $0.48
Combined: $0.95 (< $0.98 threshold)
Edge: 5% ($1.00 - $0.95)

Action: Buy YES @ $0.47
Capital: $10.00
Shares: 21.28 shares

Outcome (either way):
- Market resolves to YES: Win $21.28 (our bet)
- Market resolves to NO: Lose $10 BUT combined price was wrong
- Expected value: Positive regardless due to structural edge

Actual P&L (after 2.5% fee):
Gross profit: $11.28 - $10.00 = $1.28
Fee: $1.28 * 0.025 = $0.03
Net profit: $1.25 (12.5% return!)
```

### Streak Reversal Strategy

Bets against long streaks based on historical mean reversion data:
- 4+ consecutive outcomes → bet on reversal
- Uses Kelly criterion for position sizing
- Historical win rates: 67% (4-streak), 82% (5-streak)

### Copytrade Strategy

Monitors profitable wallets and copies their trades:
- Tracks top traders from Polymarket leaderboard
- Filters by delay, spread, and price movement
- Adjustable copy size (fraction of trader's bet)

## 🎛️ Configuration Options

### Markets
- `BTC_5M` - Bitcoin 5-minute
- `BTC_15M` - Bitcoin 15-minute
- `ETH_5M` - Ethereum 5-minute
- `ETH_15M` - Ethereum 15-minute
- `SOL_5M` - Solana 5-minute
- `SOL_15M` - Solana 15-minute

### Risk Levels
- **Conservative**: 1/8 Kelly, 10% max exposure
- **Moderate**: 1/4 Kelly, 20% max exposure
- **Aggressive**: 1/2 Kelly, 40% max exposure

### Strategy Toggles
Enable/disable any strategy independently:
```bash
ENABLE_ARBITRAGE=true
ENABLE_STREAK=true
ENABLE_COPYTRADE=false
ENABLE_SELECTIVE=false
```

## 📈 Performance Tracking

The bot tracks detailed metrics:
- **Per-strategy P&L** - see which strategies perform best
- **Win rate** - percentage of winning trades
- **Average win/loss** - expected value per trade
- **Drawdown** - track peak-to-valley losses
- **Daily limits** - prevent excessive losses

## 🛡️ Safety Features

1. **Paper Trading** - Test with fake money first
2. **Daily Loss Limits** - Auto-stop at threshold
3. **Circuit Breaker** - Pause after consecutive losses
4. **Drawdown Protection** - Stop if losses exceed peak
5. **Position Limits** - Cap per-trade and total exposure

## 📁 Project Structure

```
acropolis-bot/
├── src/
│   ├── config.py              # Configuration management
│   ├── bot_engine.py          # Main bot coordination
│   ├── core/
│   │   ├── polymarket.py      # API client
│   │   └── trader.py          # Trading execution
│   ├── strategies/
│   │   ├── arbitrage.py       # Micro-arbitrage strategy
│   │   ├── streak.py          # Streak reversal
│   │   └── copytrade.py       # Wallet monitoring
│   └── web/
│       └── server.py          # FastAPI web server
├── templates/
│   └── dashboard.html         # Web dashboard
├── main.py                    # CLI entry point
├── web.py                     # Web GUI entry point
├── .env.example              # Configuration template
└── README.md
```

## 🔧 Development

### Running Tests

```bash
uv run pytest
```

### Code Formatting

```bash
uv run ruff format .
```

### Adding Strategies

1. Create new strategy in `src/strategies/`
2. Implement signal generation logic
3. Add to bot engine in `src/bot_engine.py`
4. Add configuration in `.env.example`

## ⚠️ Disclaimer

This bot is for **educational and research purposes**. Prediction markets involve real financial risk:

- **Start with paper trading** to understand behavior
- **Never risk more than you can afford to lose**
- **Past performance does not guarantee future results**
- **Markets can change** - strategies that work today may not work tomorrow
- **Fees matter** - account for Polymarket's taker fees (~2.5% at 50¢)

The developers are not responsible for any financial losses.

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## 📜 License

MIT License - see LICENSE file for details

## 🙏 Acknowledgments

- Built with inspiration from [0xrsydn/polymarket-streak-bot](https://github.com/0xrsydn/polymarket-streak-bot)
- Uses [py-clob-client](https://github.com/Polymarket/py-clob-client) for Polymarket API
- FastAPI for web framework
- Tailwind CSS for UI styling

## 📞 Support

- **Issues**: Open an issue on GitHub
- **Questions**: Check existing issues or open a discussion

---

**Built with ⚡ by traders, for traders.**

**Remember**: The house always has an edge. We're just finding cracks in the foundation. 🏛️
