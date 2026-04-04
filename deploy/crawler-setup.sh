#!/bin/bash
# Bootstrap script for the crawler instance (m7i-flex.large, Amazon Linux 2023)
# Run as ec2-user: bash crawler-setup.sh
set -euo pipefail

REPO_URL="https://github.com/kevinzhang019/college-ai.git"
APP_DIR="/home/ec2-user/college-ai"

echo "=== College AI Crawler Setup ==="

# --- Swap (1GB safety net for 4GB instance) ---
if [ ! -f /swapfile ]; then
    echo "Creating 1GB swap..."
    sudo dd if=/dev/zero of=/swapfile bs=1M count=1024
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
fi

# --- System dependencies ---
echo "Installing system packages..."
sudo dnf update -y
sudo dnf install -y \
    python3.11 python3.11-pip python3.11-devel \
    git gcc gcc-c++ \
    libcurl-devel openssl-devel \
    nss atk cups-libs at-spi2-atk libdrm mesa-libgbm \
    alsa-lib pango gtk3 libXcomposite libXdamage libXrandr

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

# Install all crawler dependencies
pip install --upgrade pip
pip install -r requirements.txt

# --- Playwright browsers (chromium only to save disk) ---
echo "Installing Playwright Chromium..."
playwright install chromium
playwright install-deps chromium 2>/dev/null || true

# --- Environment file ---
if [ ! -f .env ]; then
    cp deploy/.env.template .env
    echo ""
    echo "*** IMPORTANT: Edit .env with your credentials ***"
    echo "    nano $APP_DIR/.env"
    echo ""
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To run the crawler:"
echo "  cd $APP_DIR"
echo "  source venv/bin/activate"
echo "  python -m college_ai.scraping.crawler"
echo ""
echo "Recommended .env overrides for this instance:"
echo "  INTER_COLLEGE_PARALLELISM=2"
echo "  CRAWLER_MAX_WORKERS=3"
echo "  PLAYWRIGHT_POOL_SIZE=2"
echo "  PLAYWRIGHT_MAX_CONCURRENCY=2"
echo "  USE_CAMOUFOX=0"
echo ""
echo "REMEMBER: Stop this instance when done to avoid charges!"
echo "  aws ec2 stop-instances --instance-ids \$(curl -s http://169.254.169.254/latest/meta-data/instance-id)"
