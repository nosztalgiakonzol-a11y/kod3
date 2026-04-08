#!/bin/bash
# VPS Setup Script for Surebet Bot
# This script automates the installation of all dependencies

set -e

echo "================================"
echo "🚀 Surebet Bot VPS Setup Script"
echo "================================"
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    echo "⚠️  Please do not run as root. Use a regular user with sudo privileges."
    exit 1
fi

# Update system
echo "📦 Step 1/8: Updating system..."
sudo apt update && sudo apt upgrade -y
echo "✅ System updated"
echo ""

# Install basic tools
echo "🔧 Step 2/8: Installing basic tools..."
sudo apt install -y software-properties-common apt-transport-https wget curl git unzip
echo "✅ Basic tools installed"
echo ""

# Install Python 3.11
echo "🐍 Step 3/8: Installing Python 3.11..."
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev
curl -sS https://bootstrap.pypa.io/get-pip.py | sudo python3.11
python3.11 --version
echo "✅ Python 3.11 installed"
echo ""

# Install Google Chrome
echo "🌐 Step 4/8: Installing Google Chrome..."
if ! command -v google-chrome &> /dev/null; then
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo dpkg -i google-chrome-stable_current_amd64.deb
    sudo apt --fix-broken install -y
    rm google-chrome-stable_current_amd64.deb
fi
google-chrome --version
echo "✅ Google Chrome installed"
echo ""

# Install ChromeDriver
echo "🚗 Step 5/8: Installing ChromeDriver..."
CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+\.\d+')
CHROMEDRIVER_VERSION=$(curl -s "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_VERSION%%.*}")
echo "   Chrome version: $CHROME_VERSION"
echo "   ChromeDriver version: $CHROMEDRIVER_VERSION"
wget -q "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip"
unzip -q chromedriver_linux64.zip
sudo mv chromedriver /usr/local/bin/
sudo chmod +x /usr/local/bin/chromedriver
rm chromedriver_linux64.zip
chromedriver --version
echo "✅ ChromeDriver installed"
echo ""

# Install system dependencies
echo "📚 Step 6/8: Installing system dependencies..."
sudo apt install -y \
    xvfb \
    libxss1 \
    libappindicator1 \
    libindicator7 \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils
echo "✅ System dependencies installed"
echo ""

# Create project directory
echo "📁 Step 7/8: Creating project directory..."
mkdir -p ~/surebet-bot
cd ~/surebet-bot
echo "✅ Project directory created: ~/surebet-bot"
echo ""

# Setup Python virtual environment
echo "🔧 Step 8/8: Setting up Python virtual environment..."
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Install Python packages
echo "   Installing Python packages..."
pip install selenium aiohttp beautifulsoup4 lxml requests python-dotenv

# Save installed versions
pip freeze > requirements.txt
echo "✅ Python environment setup complete"
echo ""

# Installation complete
echo "================================"
echo "✅ Installation Complete!"
echo "================================"
echo ""
echo "📋 Next Steps:"
echo ""
echo "1. Upload your bot file:"
echo "   scp 'Arbify Beta.py' user@$(hostname -I | awk '{print $1}'):~/surebet-bot/"
echo ""
echo "2. Activate virtual environment:"
echo "   cd ~/surebet-bot"
echo "   source venv/bin/activate"
echo ""
echo "3. Test the bot:"
echo "   python3.11 'Arbify Beta.py'"
echo ""
echo "4. Setup as service (optional):"
echo "   See VPS_DEPLOYMENT.md for systemd setup"
echo ""
echo "📊 System Info:"
echo "   Python: $(python3.11 --version)"
echo "   Chrome: $(google-chrome --version)"
echo "   ChromeDriver: $(chromedriver --version)"
echo "   Working Directory: $(pwd)"
echo ""
echo "🔒 Security Reminder:"
echo "   - Setup firewall: sudo ufw enable"
echo "   - Use non-root user"
echo "   - Disable root SSH"
echo ""
