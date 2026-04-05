#!/bin/bash
# Portfolio Bot - Ubuntu Server Deployment Script
# Run this on your Ubuntu server to set everything up

set -e

echo "============================================"
echo "  Portfolio Bot - Ubuntu Server Setup"
echo "============================================"

# 1. Update system and install Python
echo ""
echo "[1/5] Installing system dependencies..."
sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv git

# 2. Create bot directory
echo ""
echo "[2/5] Setting up bot directory..."
BOT_DIR="$HOME/portfolio_bot"
mkdir -p "$BOT_DIR/data"

# 3. Copy bot files (assumes bot.py and requirements.txt are in current directory)
echo ""
echo "[3/5] Copying bot files..."
cp bot.py "$BOT_DIR/"
cp requirements.txt "$BOT_DIR/"

# 4. Create virtual environment and install dependencies
echo ""
echo "[4/5] Creating virtual environment and installing packages..."
cd "$BOT_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Create environment file
echo ""
echo "[5/5] Setting up environment variables..."
if [ ! -f "$BOT_DIR/.env" ]; then
    cat > "$BOT_DIR/.env" << 'ENVEOF'
# Portfolio Bot Environment Variables
# Fill in your keys below

BOT_TOKEN=8709993960:AAFbzoVv6n-MuYjkszMGpWTqEH0h07RspZU
CHAT_ID=940058001
ANTHROPIC_API_KEY=your-anthropic-key-here
FINANCIAL_DATASETS_API_KEY=your-financial-datasets-key-here
ENVEOF
    echo "Created .env file at $BOT_DIR/.env"
    echo ">>> IMPORTANT: Edit .env and add your real API keys! <<<"
else
    echo ".env file already exists, skipping."
fi

# 6. Create systemd service
echo ""
echo "Creating systemd service..."
sudo tee /etc/systemd/system/portfolio-bot.service > /dev/null << SVCEOF
[Unit]
Description=Portfolio Intelligence Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR
EnvironmentFile=$BOT_DIR/.env
ExecStart=$BOT_DIR/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable portfolio-bot

echo ""
echo "============================================"
echo "  Setup Complete!"
echo "============================================"
echo ""
echo "NEXT STEPS:"
echo ""
echo "1. Edit your API keys:"
echo "   nano $BOT_DIR/.env"
echo ""
echo "2. Start the bot:"
echo "   sudo systemctl start portfolio-bot"
echo ""
echo "3. Check status:"
echo "   sudo systemctl status portfolio-bot"
echo ""
echo "4. View logs:"
echo "   sudo journalctl -u portfolio-bot -f"
echo ""
echo "5. Restart after changes:"
echo "   sudo systemctl restart portfolio-bot"
echo ""
echo "The bot will auto-start on server reboot."
echo "============================================"
