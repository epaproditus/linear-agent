#!/usr/bin/env bash
set -euo pipefail

echo "═══════════════════════════════════════════════"
echo "  Linear Agent — Setup"
echo "═══════════════════════════════════════════════"

cd "$(dirname "$0")"
DIR="$PWD"

# ── 1. Python virtual environment ──
echo ""
echo "🔧 Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

echo "📦 Installing dependencies..."
pip install -q -r requirements.txt

# ── 2. Environment config ──
echo ""
if [ ! -f .env ]; then
    cp .env.example .env
    echo "⚠️  Created .env from .env.example"
    echo "   Edit .env with your LINEAR_API_KEY and LINEAR_WEBHOOK_SECRET"
    echo "   Then re-run setup or start manually."
else
    echo "✅ .env already exists"
fi

# ── 3. Workspace directory ──
mkdir -p "$DIR/workspace"
echo "✅ Workspace: $DIR/workspace"

# ── 4. systemd service ──
echo ""
if [ -f .env ]; then
    # Check if env has required values
    source .env
    if [ -z "${LINEAR_API_KEY:-}" ] || [ -z "${LINEAR_WEBHOOK_SECRET:-}" ]; then
        echo "⚠️  LINEAR_API_KEY or LINEAR_WEBHOOK_SECRET not set in .env"
        echo "   Fill them in first, then install the service manually:"
        echo "   sudo cp linear-agent.service /etc/systemd/system/"
        echo "   sudo systemctl daemon-reload"
        echo "   sudo systemctl enable --now linear-agent"
    else
        echo "🚀 Installing systemd service..."
        sudo cp linear-agent.service /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable --now linear-agent
        echo "✅ Service installed and started!"
        sleep 2
        sudo systemctl status linear-agent --no-pager | head -10
    fi
else
    echo "⚠️  No .env found — skipping service install."
    echo "   Fill in .env first, then copy the service:"
    echo "   sudo cp linear-agent.service /etc/systemd/system/"
    echo "   sudo systemctl daemon-reload"
    echo "   sudo systemctl enable --now linear-agent"
fi

# ── 5. Quick test ──
echo ""
echo "═══════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Health check:  curl http://localhost:8660/health"
echo ""
echo "  To watch logs: journalctl -u linear-agent -f"
echo "═══════════════════════════════════════════════"
