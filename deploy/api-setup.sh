#!/bin/bash
# Bootstrap script for the API server (t3.micro, Amazon Linux 2023)
# Run as ec2-user: bash api-setup.sh
set -euo pipefail

REPO_URL="https://github.com/kevinzhang019/college-ai.git"
APP_DIR="/home/ec2-user/college-ai"

echo "=== College AI API Server Setup ==="

# --- Swap (512MB safety net for 1GB instance) ---
if [ ! -f /swapfile ]; then
    echo "Creating 512MB swap..."
    sudo dd if=/dev/zero of=/swapfile bs=1M count=512
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
fi

# --- System dependencies ---
echo "Installing system packages..."
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-pip python3.11-devel git gcc

# --- Clone repo ---
if [ ! -d "$APP_DIR" ]; then
    echo "Cloning repository..."
    git clone "$REPO_URL" "$APP_DIR"
else
    echo "Repository exists, pulling latest..."
    cd "$APP_DIR" && git pull
fi

cd "$APP_DIR"

# --- Python venv ---
echo "Setting up Python virtual environment..."
python3.11 -m venv venv
source venv/bin/activate

# Install only API dependencies (no playwright/camoufox/ML)
pip install --upgrade pip
pip install \
    requests beautifulsoup4 lxml pyyaml \
    pymilvus sqlalchemy-libsql \
    openai tiktoken \
    python-dotenv \
    fastapi uvicorn \
    lightgbm scikit-learn joblib venn-abers

# --- Environment file ---
if [ ! -f .env ]; then
    cp deploy/.env.template .env
    echo ""
    echo "*** IMPORTANT: Edit .env with your credentials ***"
    echo "    nano $APP_DIR/.env"
    echo ""
fi

# --- Systemd service ---
echo "Installing systemd service..."
sudo cp deploy/college-ai-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable college-ai-api
sudo systemctl start college-ai-api

echo ""
echo "=== Setup Complete ==="
echo "API running at http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4):8000"
echo ""
echo "Next steps:"
echo "  1. Edit .env:  nano $APP_DIR/.env"
echo "  2. Restart:    sudo systemctl restart college-ai-api"
echo "  3. Logs:       journalctl -u college-ai-api -f"
echo "  4. Health:     curl http://localhost:8000/health"
