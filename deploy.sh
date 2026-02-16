#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/acropolis-bot"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   AcropolisBot — One-Click Deploy    ║"
echo "╚══════════════════════════════════════╝"
echo ""

# --- Root check ---
[[ $EUID -eq 0 ]] || error "Please run as root (sudo bash deploy.sh)"

# --- Install Docker ---
if ! command -v docker &>/dev/null; then
    warn "Docker not found — installing..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    info "Docker installed"
else
    info "Docker found"
fi

# --- Install Docker Compose (plugin) ---
if ! docker compose version &>/dev/null; then
    warn "Docker Compose plugin not found — installing..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
    info "Docker Compose installed"
else
    info "Docker Compose found"
fi

# --- Install apache2-utils for htpasswd ---
if ! command -v htpasswd &>/dev/null; then
    warn "htpasswd not found — installing apache2-utils..."
    apt-get update -qq && apt-get install -y -qq apache2-utils
    info "apache2-utils installed"
else
    info "htpasswd found"
fi

# --- Clone or pull repo ---
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Repo exists at $INSTALL_DIR — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only || warn "Pull failed — continuing with existing code"
else
    echo ""
    read -rp "GitHub Personal Access Token (for private repo): " GH_TOKEN
    [[ -z "$GH_TOKEN" ]] && error "Token required — repo is private"
    REPO_URL="https://${GH_TOKEN}@github.com/agamil245/acropolis-bot.git"
    info "Cloning repo to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# --- .env setup ---
if [[ ! -f .env ]]; then
    cp .env.example .env
    warn ".env created from template — opening editor..."
    ${EDITOR:-nano} .env
else
    info ".env already exists — skipping"
fi

# --- Dashboard auth ---
if [[ ! -f .htpasswd ]]; then
    echo ""
    read -rp "Dashboard username: " DASH_USER
    htpasswd -c .htpasswd "$DASH_USER"
    info "Created .htpasswd for user '$DASH_USER'"
else
    info ".htpasswd already exists — skipping"
    echo "  (Run 'htpasswd .htpasswd <user>' to add/change users)"
fi

# --- Build & start ---
info "Building and starting containers..."
docker compose up -d --build

echo ""
info "AcropolisBot is running!"
echo ""
docker compose ps
echo ""
SERVER_IP=$(hostname -I | awk '{print $1}')
info "Dashboard: http://${SERVER_IP}/"
info "Logs:      docker compose -f $INSTALL_DIR/docker-compose.yml logs -f"
echo ""
