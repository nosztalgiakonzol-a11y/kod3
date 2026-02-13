# 🚀 VPS Deployment Guide

## Complete guide for deploying the Surebet Bot on a VPS

---

## 📋 System Requirements

### Minimum Specifications:
- **OS:** Ubuntu 20.04 / 22.04 LTS (recommended)
- **RAM:** 4GB minimum, 8GB recommended
- **CPU:** 2 cores minimum, 4 cores recommended
- **Storage:** 20GB minimum
- **Network:** Good bandwidth for web scraping

---

## 🔧 Installation Steps

### 1. Update System

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y software-properties-common apt-transport-https wget curl git
```

### 2. Install Python 3.11

```bash
# Add Python repository
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update

# Install Python 3.11
sudo apt install -y python3.11 python3.11-venv python3.11-dev

# Install pip
curl -sS https://bootstrap.pypa.io/get-pip.py | sudo python3.11

# Verify
python3.11 --version
```

### 3. Install Google Chrome

```bash
# Download and install Chrome
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo dpkg -i google-chrome-stable_current_amd64.deb
sudo apt --fix-broken install -y

# Verify
google-chrome --version
```

### 4. Install ChromeDriver

```bash
# Get Chrome version
CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+\.\d+')
CHROMEDRIVER_VERSION=$(curl -s "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_VERSION%%.*}")

# Download ChromeDriver
wget "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip"
unzip chromedriver_linux64.zip
sudo mv chromedriver /usr/local/bin/
sudo chmod +x /usr/local/bin/chromedriver

# Verify
chromedriver --version
```

### 5. Install System Dependencies

```bash
# X Virtual Framebuffer for headless mode
sudo apt install -y xvfb

# Chrome dependencies
sudo apt install -y libxss1 libappindicator1 libindicator7 \
  fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
  libatspi2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
  libnspr4 libnss3 libwayland-client0 libxcomposite1 libxdamage1 \
  libxfixes3 libxkbcommon0 libxrandr2 xdg-utils
```

### 6. Setup Project

```bash
# Create directory
mkdir -p ~/surebet-bot
cd ~/surebet-bot

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install Python packages
pip install --upgrade pip
pip install selenium aiohttp beautifulsoup4 lxml requests python-dotenv
```

### 7. Upload Bot Files

```bash
# Option A: Using git
git clone <your-repo-url> ~/surebet-bot

# Option B: Using SCP
scp "Arbify Beta.py" user@vps-ip:~/surebet-bot/
```

### 8. Configure for Headless Mode

Make sure these Chrome options are in your code:

```python
options.add_argument('--headless=new')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-gpu')
```

---

## 🔄 Running as a Service

### Create systemd service file:

```bash
sudo nano /etc/systemd/system/surebet-bot.service
```

**Content:**

```ini
[Unit]
Description=Surebet Bot
After=network.target

[Service]
Type=simple
User=yourusername
WorkingDirectory=/home/yourusername/surebet-bot
Environment="DISPLAY=:99"
ExecStartPre=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 &
ExecStart=/home/yourusername/surebet-bot/venv/bin/python3.11 "Arbify Beta.py"
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Enable and start service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable surebet-bot
sudo systemctl start surebet-bot

# Check status
sudo systemctl status surebet-bot

# View logs
sudo journalctl -u surebet-bot -f
```

---

## 📊 Monitoring

### Check service status:
```bash
sudo systemctl status surebet-bot
```

### View logs in real-time:
```bash
sudo journalctl -u surebet-bot -f
```

### Check resource usage:
```bash
sudo apt install htop
htop
```

---

## 🔧 Troubleshooting

### Chrome won't start:
```bash
# Install missing dependencies
sudo apt install -y libgbm1 libnss3

# Verify versions match
google-chrome --version
chromedriver --version
```

### Permission errors:
```bash
chmod +x /usr/local/bin/chromedriver
sudo usermod -a -G video $USER
```

### Memory issues:
```bash
# Check available memory
free -h

# Reduce Chrome memory usage (add to options):
options.add_argument('--disable-dev-shm-usage')
```

---

## 🔒 Security Best Practices

### 1. Setup Firewall:
```bash
sudo ufw allow ssh
sudo ufw enable
```

### 2. Create non-root user:
```bash
sudo adduser botuser
sudo usermod -aG sudo botuser
```

### 3. Disable root SSH:
```bash
sudo nano /etc/ssh/sshd_config
# Set: PermitRootLogin no
sudo systemctl restart sshd
```

### 4. Setup fail2ban:
```bash
sudo apt install -y fail2ban
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

---

## 📦 Complete Package List

### System Packages (apt):
- python3.11
- python3.11-venv
- python3.11-dev
- google-chrome-stable
- xvfb
- libxss1
- libappindicator1
- fonts-liberation
- libasound2
- libatk-bridge2.0-0
- libgbm1
- libgtk-3-0
- libnss3
- (and more - see setup script)

### Python Packages (pip):
- selenium
- aiohttp
- beautifulsoup4
- lxml
- requests
- python-dotenv

---

## 🚀 Quick Setup

Use the provided `vps-setup.sh` script for automated installation:

```bash
chmod +x vps-setup.sh
./vps-setup.sh
```

---

## 📞 Support

If you encounter issues:
1. Check logs: `sudo journalctl -u surebet-bot -f`
2. Verify Chrome: `google-chrome --version`
3. Check Python: `python3.11 --version`
4. Test manually: `cd ~/surebet-bot && source venv/bin/activate && python3.11 "Arbify Beta.py"`
