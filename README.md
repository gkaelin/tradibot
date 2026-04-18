# Tradibot - Multi-Timeframe Candlestick Strategy

**An intelligent automated trading bot for KuCoin (USDT Spot)**  
Detects bullish and bearish candlestick patterns across multiple timeframes, combined with RSI, MACD, volume and support/resistance filters.

---

## 🚀 Features

- Multi-timeframe analysis (5m, 15m, 30m, 1h)
- Advanced candlestick pattern detection (Hammer, Inverted Hammer, Engulfing, Harami, Piercing Line, Morning Star, etc.)
- RSI + MACD + Volume + Support/Resistance filters
- Automatic position management with configurable max slots
- Automatic Take-Profit (20% by default)
- Full web interface (real-time positions + history)
- Manual Buy / Sell directly from the web UI
- Live logs with auto-refresh every 10 seconds
- Configurable email reports (enable/disable + frequency from admin panel)
- Public access can be enabled or disabled
- Anti-brute-force protection on admin login
- Price cache to avoid KuCoin rate limits

---

## 🔗 KuCoin Referral Link (Please use it!)

If you don't have a KuCoin account yet, you can support this project by signing up with this link:

👉 **[https://www.kucoin.com/r/rf/QBSFHUA7](https://www.kucoin.com/r/rf/QBSFHUA7)**

You get welcome bonuses and I receive a percentage of trading fees (at no extra cost to you).

---

## 📥 Installation & Setup

### 1. Clone the repository
```bash
git clone https://github.com/gkaelin/tradibot-kucoin.git
cd tradibot-kucoin
```

### 2. Create virtual environment
```
python -m venv venv
source venv/bin/activate      # Linux / macOS
# or
venv\Scripts\activate         # Windows
```

### 3. Install dependencies
```
pip install -r requirements.txt
```

### 4. Configure the .env file
A ready-to-use env file is included in the repository. Copy it and rename:
```
cp env .env
```

Edit .env and fill in:
- Your KuCoin API keys
- SMTP settings for email reports
- Web login/password
- Flask secret key

### 5. Run the bot
```
python main.py
```

The bot will run in the background and the web interface will be available at: http://your-server-ip:5000

## ⚙️ Configuration
### Via .env file

- KuCoin API credentials
- Admin login / password
- SMTP email settings

### Via Admin Panel (/admin)

Once logged in, you can easily configure:

- Enable / Disable email reports
- Report frequency (in hours)
- Enable / Disable public access
- Max positions
- USDT per position
- Max pairs to scan
- Take profit %
- Cache duration

## 🌐 Exposing the Web Interface to the Internet
To make the dashboard accessible from anywhere, we recommend using a reverse proxy.
### Recommended: Apache2 (or Nginx)

**Example with Apache on Ubuntu/Debian:**
```
sudo apt update
sudo apt install apache2
sudo a2enmod proxy proxy_http
sudo nano /etc/apache2/sites-available/tradibot.conf
```

Content of tradibot.conf:

```
<VirtualHost *:80>
    ServerName your-domain.com

    ProxyPass / http://127.0.0.1:5000/
    ProxyPassReverse / http://127.0.0.1:5000/

    ErrorLog ${APACHE_LOG_DIR}/tradibot_error.log
    CustomLog ${APACHE_LOG_DIR}/tradibot_access.log combined
</VirtualHost>
```

Enable the site:

```
sudo a2ensite tradibot.conf
sudo systemctl restart apache2
```

**Security tip**: Always use HTTPS (Let’s Encrypt) and change the default admin password.

## Wanna see running one ?
I have my personnal bot running at https://tradibot.gkaelin.com
Feel free to have a look

## 💰 Support the Project
This bot is **completely free and open-source**. If you find it useful and it helps you make money, you can support further development with a donation:

**Dogecoin (DOGE)**
DJxLre4fbUiaHJ3KvBTCjUpaDheWmDY8bz

Every donation, no matter how small, is greatly appreciated!

## ⚠️ Important Disclaimer

- This bot is provided **as is**.
- Trading involves significant risk of loss. You can lose all your capital.
- Use at your own risk.
- Always test with small amounts first.

## License

MIT License — Feel free to use, modify, and distribute (even commercially), as long as the original copyright notice is kept.

Thank you for using this bot! If you have suggestions or improvements, feel free to open an Issue or Pull Request.