import ccxt
import pandas as pd
import numpy as np
import time
import talib
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta
import sqlite3
import sys
import logging
from flask import Flask, render_template_string, request, redirect, url_for, session, flash
import threading
from collections import defaultdict

# ====================== LOGGING CONFIGURATION ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# ====================== CONSTANTS ======================
db_file = 'trading.db'

# KuCoin API keys
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
PASSPHRASE = os.getenv('PASSPHRASE')

# SMTP configuration
SMTP_SERVER = os.getenv('SMTP_SERVER')
SMTP_PORT = int(os.getenv('SMTP_PORT'))
SMTP_USER = os.getenv('SMTP_USER')
SMTP_PASS = os.getenv('SMTP_PASS')
TO_EMAIL = os.getenv('TO_EMAIL')

# Web configuration
WEB_LOGIN = os.getenv('WEB_LOGIN')
WEB_PASSWORD = os.getenv('WEB_PASSWORD')
FLASK_SECRET_KEY = os.getenv('FLASK_SECRET_KEY')

exchange = ccxt.kucoin({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'password': PASSPHRASE,
    'enableRateLimit': True,
})

# ====================== CUSTOMIZABLE SETTINGS ======================
timeframe = '1h'
limit = 200
rsi_period = 14
macd_fast = 12
macd_slow = 26
macd_signal = 9

timeframes_for_confirmation = ['5m', '15m', '30m', '1h']
min_buy_signals = 1
min_sell_signals = 1

# ====================== PERSISTENT SETTINGS ======================
settings = {
    'email_enabled': True,
    'report_interval': 86400,
    'public_access_enabled': True,
    'max_slots': 5,
    'amount_per_trade': 10,
    'max_pairs': 200,
    'take_profit_pct': 20.0,
    'cache_duration': 15
}

def load_settings():
    global settings
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
    defaults = {
        'email_enabled': 'true',
        'report_interval': '86400',
        'public_access_enabled': 'true',
        'max_slots': '5',
        'amount_per_trade': '10',
        'max_pairs': '200',
        'take_profit_pct': '20.0',
        'cache_duration': '15'
    }
    for key, default_value in defaults.items():
        c.execute("SELECT value FROM config WHERE key=?", (key,))
        row = c.fetchone()
        if row is None:
            c.execute("INSERT INTO config (key, value) VALUES (?, ?)", (key, default_value))
            if key.endswith('_enabled'):
                settings[key] = (default_value.lower() == 'true')
            elif key in ['max_slots', 'max_pairs', 'amount_per_trade', 'cache_duration']:
                settings[key] = int(default_value)
            else:
                settings[key] = float(default_value)
        else:
            val = row[0]
            if key.endswith('_enabled'):
                settings[key] = (str(val).lower() == 'true')
            elif key in ['max_slots', 'max_pairs', 'amount_per_trade', 'cache_duration']:
                settings[key] = int(val)
            else:
                settings[key] = float(val)
    conn.commit()
    conn.close()

def save_setting(key, value):
    global settings
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    if isinstance(value, bool):
        value_str = 'true' if value else 'false'
    else:
        value_str = str(value)
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value_str))
    conn.commit()
    conn.close()
    settings[key] = value

# ====================== PRICE CACHE ======================
price_cache = {}

def get_current_price(symbol):
    now = time.time()
    if symbol in price_cache:
        price, timestamp = price_cache[symbol]
        if now - timestamp < settings['cache_duration']:
            return price
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        price_cache[symbol] = (price, now)
        return price
    except Exception as e:
        logger.warning(f"Error fetching price for {symbol}: {e}")
        return price_cache.get(symbol, (None, 0))[0]

# ====================== ANTI BRUTE-FORCE ======================
login_attempts = defaultdict(list)
MAX_ATTEMPTS = 5
BLOCK_TIME = 300

# ====================== CANDLESTICK DETECTION FUNCTIONS ======================
def detect_hammer(row):
    body = abs(row['close'] - row['open'])
    lower_wick = min(row['open'], row['close']) - row['low']
    upper_wick = row['high'] - max(row['open'], row['close'])
    return lower_wick > 2 * body and upper_wick < body * 0.5

def detect_shooting_star(row):
    body = abs(row['close'] - row['open'])
    lower_wick = min(row['open'], row['close']) - row['low']
    upper_wick = row['high'] - max(row['open'], row['close'])
    return upper_wick > 2 * body and lower_wick < body * 0.5

def detect_bullish_engulfing(df, i):
    if i < 1: return False
    prev = df.iloc[i-1]
    curr = df.iloc[i]
    return (prev['close'] < prev['open']) and (curr['close'] > curr['open']) and \
           (curr['open'] < prev['close']) and (curr['close'] > prev['open'])

def detect_bearish_engulfing(df, i):
    if i < 1: return False
    prev = df.iloc[i-1]
    curr = df.iloc[i]
    return (prev['close'] > prev['open']) and (curr['close'] < curr['open']) and \
           (curr['open'] > prev['close']) and (curr['close'] < prev['open'])

def detect_inverted_hammer(row):
    body = abs(row['close'] - row['open'])
    upper_wick = row['high'] - max(row['open'], row['close'])
    lower_wick = min(row['open'], row['close']) - row['low']
    return upper_wick > 2 * body and lower_wick < body * 0.5

def detect_bullish_harami(df, i):
    if i < 1: return False
    prev = df.iloc[i-1]
    curr = df.iloc[i]
    prev_body = abs(prev['close'] - prev['open'])
    curr_body = abs(curr['close'] - curr['open'])
    return (prev['close'] < prev['open']) and (curr['close'] > curr['open']) and \
           (curr['open'] > prev['open']) and (curr['close'] < prev['close']) and \
           (curr_body < prev_body * 0.6)

def detect_piercing_line(df, i):
    if i < 1: return False
    prev = df.iloc[i-1]
    curr = df.iloc[i]
    return (prev['close'] < prev['open']) and (curr['close'] > curr['open']) and \
           (curr['open'] < prev['close']) and \
           (curr['close'] > (prev['open'] + prev['close']) / 2)

def detect_morning_star(df, i):
    if i < 2: return False
    p2 = df.iloc[i-2]
    p1 = df.iloc[i-1]
    curr = df.iloc[i]
    p2_body = abs(p2['close'] - p2['open'])
    p1_body = abs(p1['close'] - p1['open'])
    curr_body = abs(curr['close'] - curr['open'])
    return (p2['close'] < p2['open']) and \
           (p1_body < p2_body * 0.3) and \
           (p1['high'] < p2['low']) and \
           (curr['close'] > curr['open']) and \
           (curr['close'] > (p2['open'] + p2['close']) / 2) and \
           (curr_body > p1_body * 1.5)

def get_top_pairs():
    markets = exchange.load_markets()
    usdt_pairs = [symbol for symbol in markets if symbol.endswith('/USDT') and markets[symbol]['active'] and markets[symbol].get('spot', False)]
    tickers = exchange.fetch_tickers(usdt_pairs, params={'type': 'spot'})
    sorted_pairs = sorted(
        usdt_pairs,
        key=lambda s: tickers.get(s, {}).get('quoteVolume', 0),
        reverse=True
    )
    return sorted_pairs[:settings['max_pairs']] if settings['max_pairs'] else sorted_pairs

def init_db():
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, amount REAL, buy_price REAL, buy_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS closed_trades (symbol TEXT, amount REAL, buy_price REAL, buy_time TEXT, sell_price REAL, sell_time TEXT, pnl REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    conn.close()

def load_positions():
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("SELECT * FROM positions")
    rows = c.fetchall()
    conn.close()
    return {row[0]: {'amount': row[1], 'buy_price': row[2], 'buy_time': row[3]} for row in rows}

def save_position(symbol, data):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO positions (symbol, amount, buy_price, buy_time) VALUES (?, ?, ?, ?)''',
              (symbol, data['amount'], data['buy_price'], data['buy_time']))
    conn.commit()
    conn.close()

def delete_position(symbol):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()

def log_closed_trade(symbol, amount, buy_price, buy_time, sell_price, pnl):
    sell_time = datetime.now().isoformat()
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute('''INSERT INTO closed_trades (symbol, amount, buy_price, buy_time, sell_price, sell_time, pnl)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''', (symbol, amount, buy_price, buy_time, sell_price, sell_time, pnl))
    conn.commit()
    conn.close()

def get_recent_closed_trades():
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
    c.execute("SELECT * FROM closed_trades WHERE sell_time > ?", (yesterday,))
    rows = c.fetchall()
    conn.close()
    return rows

def send_report(positions):
    if not settings['email_enabled']:
        return
    content = "Report of open positions:\n\n"
    if not positions:
        content += "No open positions.\n"
    else:
        total_pnl = 0
        for symbol, data in positions.items():
            pnl, pnl_pct, _ = calculate_pnl(symbol, data['amount'], data['buy_price'])
            total_pnl += pnl
            content += f"{symbol}: Amount={data['amount']:.4f}, Buy Price={data['buy_price']:.2f}, PNL={pnl:.2f} USDT ({pnl_pct:.2f}%)\n"
        content += f"\nTotal open PNL: {total_pnl:.2f} USDT\n"
    content += "\nClosed trades in the last 24h:\n"
    recent_closes = get_recent_closed_trades()
    if not recent_closes:
        content += "No recently closed trades.\n"
    else:
        total_closed_pnl = 0
        for row in recent_closes:
            total_closed_pnl += row[6]
            content += f"{row[0]}: Amount={row[1]:.4f}, Buy={row[2]:.2f}, Sell={row[4]:.2f}, PNL={row[6]:.2f} USDT (Sold on {row[5]})\n"
        content += f"Total closed PNL: {total_closed_pnl:.2f} USDT\n"
    msg = MIMEText(content)
    msg['Subject'] = 'KuCoin Trading Report'
    msg['From'] = SMTP_USER
    msg['To'] = TO_EMAIL
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, TO_EMAIL, msg.as_string())
        server.quit()
        logger.info("Email report sent.")
    except Exception as e:
        logger.error(f"Error sending email: {e}")

def get_candlestick_signals(symbol, timeframe):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['rsi'] = talib.RSI(df['close'], timeperiod=rsi_period)
        macd, signal, _ = talib.MACD(df['close'], fastperiod=macd_fast, slowperiod=macd_slow, signalperiod=macd_signal)
        df['macd'] = macd
        df['macd_signal'] = signal
        recent_lows = df['low'].rolling(window=20).min()
        recent_highs = df['high'].rolling(window=20).max()
        support = recent_lows.iloc[-1]
        resistance = recent_highs.iloc[-1]
        last_row = df.iloc[-1]
        i = len(df) - 1
        df['volume_ma'] = df['volume'].rolling(window=20).mean()
        high_volume = last_row['volume'] > df['volume_ma'].iloc[-1] * 1.2 if not pd.isna(df['volume_ma'].iloc[-1]) else False

        buy_signal = (detect_hammer(last_row) or detect_inverted_hammer(last_row) or
                      detect_bullish_engulfing(df, i) or detect_bullish_harami(df, i) or
                      detect_piercing_line(df, i) or detect_morning_star(df, i)) and \
                     last_row['close'] > support * 1.005 and last_row['rsi'] < 40 and \
                     last_row['macd'] > last_row['macd_signal'] and high_volume

        sell_signal = (detect_shooting_star(last_row) or detect_bearish_engulfing(df, i)) and \
                      last_row['close'] < resistance * 0.995 and last_row['rsi'] > 70 and \
                      last_row['macd'] < last_row['macd_signal'] and high_volume

        return buy_signal, sell_signal, float(last_row['close'])
    except Exception as e:
        logger.error(f"Error in get_signals {timeframe} {symbol}: {e}")
        return False, False, None

def analyze_and_trade(symbol, positions):
    try:
        current_price = get_current_price(symbol)
        buy_count = 0
        sell_count = 0
        for tf in timeframes_for_confirmation:
            buy_sig, sell_sig, _ = get_candlestick_signals(symbol, tf)
            if buy_sig: buy_count += 1
            if sell_sig: sell_count += 1

        base_currency = symbol.split('/')[0]
        balance = exchange.fetch_balance()
        usdt_free = balance.get('USDT', {}).get('free', 0)
        asset_free = balance.get(base_currency, {'free': 0})['free']

        if (buy_count >= min_buy_signals and len(positions) < settings['max_slots'] and
            usdt_free >= settings['amount_per_trade'] and symbol not in positions):
            amount = settings['amount_per_trade'] / current_price
            logger.info(f"✅ MULTI-TF BUY (buy:{buy_count}/{len(timeframes_for_confirmation)}) {symbol} | {amount:.4f} @ {current_price:.4f}")
            exchange.create_market_buy_order(symbol, amount)
            data = {'amount': amount, 'buy_price': current_price, 'buy_time': datetime.now().isoformat()}
            save_position(symbol, data)
            positions[symbol] = data

        if symbol in positions:
            amount = positions[symbol]['amount']
            pnl, pnl_pct, _ = calculate_pnl(symbol, amount, positions[symbol]['buy_price'])
            take_profit_triggered = (pnl_pct >= settings['take_profit_pct'])
            if ((sell_count >= min_sell_signals) or take_profit_triggered) and asset_free >= amount * 0.99:
                if take_profit_triggered:
                    logger.info(f"TAKE PROFIT +{pnl_pct:.2f}% reached for {symbol}")
                else:
                    logger.info(f"✅ MULTI-TF SELL (sell:{sell_count}/{len(timeframes_for_confirmation)}) for {symbol}")
                exchange.create_market_sell_order(symbol, amount)
                pnl_log, _, sell_price = calculate_pnl(symbol, amount, positions[symbol]['buy_price'])
                log_closed_trade(symbol, amount, positions[symbol]['buy_price'], positions[symbol]['buy_time'], sell_price, pnl_log)
                delete_position(symbol)
                del positions[symbol]
    except Exception as e:
        logger.error(f"Error processing {symbol}: {e}")

def main():
    load_settings()
    init_db()
    usdt_pairs = get_top_pairs()
    logger.info(f"Analyzing {len(usdt_pairs)} top USDT pairs.")
    last_report_time = time.time() - settings['report_interval'] + 60
    while True:
        positions = load_positions()
        for symbol in usdt_pairs:
            analyze_and_trade(symbol, positions)
            time.sleep(1)
        if time.time() - last_report_time >= settings['report_interval']:
            send_report(load_positions())
            last_report_time = time.time()

# ====================== CLI FUNCTIONS ======================
def get_closed_trades(months=None):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    if months is None or months == 0:
        c.execute("SELECT * FROM closed_trades ORDER BY sell_time DESC")
    else:
        since = (datetime.now() - timedelta(days=months * 30.5)).isoformat()
        c.execute("SELECT * FROM closed_trades WHERE sell_time >= ? ORDER BY sell_time DESC", (since,))
    rows = c.fetchall()
    conn.close()
    return rows

def show_history(months=None):
    trades = get_closed_trades(months)
    period = "since bot started" if months is None or months == 0 else f"last {months} months"
    if not trades:
        print(f"No closed trades found {period}.")
        return
    print(f"\n📊 CLOSED TRADES HISTORY — {period.upper()}")
    print("=" * 120)
    print(f"{'Sell Date':<16} {'Symbol':<12} {'Amount':>9} {'Buy':>10} {'Sell':>10} {'PNL USDT':>12} {'PNL %':>9}")
    print("=" * 120)
    total_pnl = 0.0
    wins = 0
    best_pnl = float('-inf')
    worst_pnl = float('inf')
    best_trade = ""
    worst_trade = ""
    for row in trades:
        symbol, amount, buy_price, _, sell_price, sell_time, pnl = row
        pnl_pct = (pnl / (amount * buy_price) * 100) if (amount * buy_price) > 0 else 0
        date_str = sell_time[:16]
        print(f"{date_str:<16} {symbol:<12} {amount:>9.4f} {buy_price:>10.4f} "
              f"{sell_price:>10.4f} {pnl:>12.2f} {pnl_pct:>+8.2f}%")
        total_pnl += pnl
        if pnl > 0: wins += 1
        if pnl > best_pnl: best_pnl, best_trade = pnl, f"{symbol} (+{pnl:,.2f} USDT)"
        if pnl < worst_pnl: worst_pnl, worst_trade = pnl, f"{symbol} ({pnl:,.2f} USDT)"
    winrate = (wins / len(trades) * 100) if trades else 0
    avg_pnl = total_pnl / len(trades) if trades else 0
    print("=" * 120)
    print(f"Total realized PNL : {total_pnl:,.2f} USDT")
    print(f"Winrate : {winrate:.1f}% ({wins}/{len(trades)} winning trades)")
    print(f"Average PNL per trade : {avg_pnl:,.2f} USDT")
    print(f"Best trade : {best_trade}")
    print(f"Worst trade : {worst_trade}")
    print("=" * 120)

def print_help():
    print("\n" + "="*75)
    print(" " * 20 + "KUCOIN TRADING BOT")
    print("="*75)
    print("python script.py → Run bot + Web interface")
    print("python script.py show → Show open positions + PNL")
    print("python script.py buy SYMBOL → Manual buy")
    print("python script.py sell SYMBOL → Manual sell")
    print("python script.py history → Trade history")
    print("python script.py history 3m → Last 3 months")
    print("python script.py history all → Full history")
    print("python script.py help → This help")
    print("="*75)

def show_positions():
    positions = load_positions()
    if not positions:
        print("No open positions.")
        return
    print("\nOPEN POSITIONS :\n" + "-"*85)
    print(f"{'Symbol':<12} {'Amount':>10} {'Buy':>12} {'Current':>12} {'PNL USDT':>12} {'PNL %':>9}")
    print("-"*85)
    total_pnl = 0
    for symbol, data in positions.items():
        pnl, pnl_pct, price = calculate_pnl(symbol, data['amount'], data['buy_price'])
        total_pnl += pnl
        print(f"{symbol:<12} {data['amount']:>10.4f} {data['buy_price']:>12.4f} "
              f"{price:>12.4f} {pnl:>12.2f} {pnl_pct:>+8.2f}%")
    print("-"*85)
    print(f"Total open PNL : {total_pnl:,.2f} USDT")
    print("-"*85)

def manual_sell(symbol):
    if not symbol.upper().endswith('/USDT'):
        symbol = symbol.upper() + '/USDT'
    else:
        symbol = symbol.upper()
    positions = load_positions()
    if symbol not in positions:
        print(f"❌ No open position for {symbol}")
        return
    data = positions[symbol]
    amount = data['amount']
    base = symbol.split('/')[0]
    try:
        balance = exchange.fetch_balance()
        asset_free = balance.get(base, {'free': 0})['free']
        print(f"🔴 MANUAL SELL of {amount:.4f} {base} ({symbol})...")
        exchange.create_market_sell_order(symbol, amount)
        pnl, pnl_pct, sell_price = calculate_pnl(symbol, amount, data['buy_price'])
        log_closed_trade(symbol, amount, data['buy_price'], data['buy_time'], sell_price, pnl)
        delete_position(symbol)
        print(f"✅ Sell successful! Price ≈ {sell_price:.4f} | PNL : {pnl:,.2f} USDT ({pnl_pct:+.2f}%)")
    except Exception as e:
        print(f"❌ Error during sell: {e}")

def manual_buy(symbol):
    if not symbol.upper().endswith('/USDT'):
        symbol = symbol.upper() + '/USDT'
    else:
        symbol = symbol.upper()
    positions = load_positions()
    if symbol in positions:
        print(f"❌ You already have an open position on {symbol}")
        return
    base_currency = symbol.split('/')[0]
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance.get('USDT', {}).get('free', 0)
        if usdt_free < settings['amount_per_trade']:
            print(f"❌ Insufficient USDT balance: {usdt_free:.2f} USDT")
            return
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        amount = settings['amount_per_trade'] / current_price
        print(f"🟢 MANUAL BUY {symbol} | {amount:.4f} @ {current_price:.4f}")
        exchange.create_market_buy_order(symbol, amount)
        data = {'amount': amount, 'buy_price': current_price, 'buy_time': datetime.now().isoformat()}
        save_position(symbol, data)
        print(f"✅ Manual buy successful! Quantity: {amount:.4f} | Invested: {settings['amount_per_trade']:.2f} USDT")
    except Exception as e:
        print(f"❌ Error during manual buy: {e}")

# ====================== WEB FUNCTIONS ======================
def calculate_pnl(symbol, amount, buy_price):
    current_price = get_current_price(symbol)
    if current_price is None:
        current_price = buy_price
    value_now = amount * current_price
    value_buy = amount * buy_price
    pnl = value_now - value_buy
    pnl_pct = (pnl / value_buy) * 100 if value_buy > 0 else 0
    return pnl, pnl_pct, current_price

def get_positions_data():
    positions = load_positions()
    data = []
    total_pnl = 0
    for symbol, pos in positions.items():
        pnl, pnl_pct, current_price = calculate_pnl(symbol, pos['amount'], pos['buy_price'])
        total_pnl += pnl
        data.append({
            'symbol': symbol,
            'amount': round(pos['amount'], 4),
            'buy_price': round(pos['buy_price'], 4),
            'current_price': round(current_price, 4),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2)
        })
    return data, round(total_pnl, 2)

def get_history_data(limit=100):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("SELECT * FROM closed_trades ORDER BY sell_time DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    data = []
    for row in rows:
        symbol, amount, buy_price, _, sell_price, sell_time, pnl = row
        pnl_pct = (pnl / (amount * buy_price) * 100) if (amount * buy_price) > 0 else 0
        data.append({
            'sell_time': sell_time[:16],
            'symbol': symbol,
            'amount': round(amount, 4),
            'buy_price': round(buy_price, 4),
            'sell_price': round(sell_price, 4),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2)
        })
    return data

def get_last_logs(lines=100):
    try:
        with open('tradibot.log', 'r', encoding='utf-8') as f:
            logs = f.readlines()[-lines:]
            return list(reversed(logs))
    except FileNotFoundError:
        return ["No log file found yet."]

# ====================== FLASK APPLICATION ======================
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

@app.route('/')
def public_home():
    if not settings['public_access_enabled']:
        return redirect(url_for('admin_dashboard'))
    positions, total_pnl = get_positions_data()
    history = get_history_data(50)
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>KuCoin Trading Bot - Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-dark text-light">
    <div class="container py-4">
        <h1 class="mb-4">🚀 KuCoin Trading Bot</h1>
        <h3>Open Positions</h3>
        {% if not positions %}
            <div class="alert alert-info">No open positions.</div>
        {% else %}
            <table class="table table-dark table-striped">
                <thead><tr><th>Symbol</th><th>Qty</th><th>Buy</th><th>Current Price</th><th>PNL USDT</th><th>PNL %</th></tr></thead>
                <tbody>
                {% for p in positions %}
                <tr>
                    <td>{{ p.symbol }}</td>
                    <td>{{ p.amount }}</td>
                    <td>{{ p.buy_price }}</td>
                    <td>{{ p.current_price }}</td>
                    <td class="{% if p.pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ p.pnl }}</td>
                    <td class="{% if p.pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ p.pnl_pct }}%</td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
            <h5>Total Open PNL : <strong class="{% if total_pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ total_pnl }} USDT</strong></h5>
        {% endif %}
        <hr>
        <h3>Recent Trades</h3>
        <a href="/history" class="btn btn-outline-light mb-3">View full history →</a>
        <table class="table table-dark table-striped">
            <thead><tr><th>Date</th><th>Symbol</th><th>Buy</th><th>Sell</th><th>PNL</th></tr></thead>
            <tbody>
            {% for t in history %}
            <tr>
                <td>{{ t.sell_time }}</td>
                <td>{{ t.symbol }}</td>
                <td>{{ t.buy_price }}</td>
                <td>{{ t.sell_price }}</td>
                <td class="{% if t.pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ t.pnl }} ({{ t.pnl_pct }}%)</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        <a href="/login" class="btn btn-primary mt-3">🔑 Admin Access</a>
    </div>
</body>
</html>
    ''', positions=positions, total_pnl=total_pnl, history=history)

@app.route('/history')
def public_history():
    if not settings['public_access_enabled']:
        return redirect(url_for('admin_dashboard'))
    history = get_history_data(200)
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Full History</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body class="bg-dark text-light">
<div class="container py-4">
    <h1>📊 Full Trade History</h1>
    <a href="/" class="btn btn-outline-light mb-3">← Back</a>
    <table class="table table-dark table-striped">
        <thead><tr><th>Date</th><th>Symbol</th><th>Qty</th><th>Buy</th><th>Sell</th><th>PNL USDT</th><th>PNL %</th></tr></thead>
        <tbody>
        {% for t in history %}
        <tr>
            <td>{{ t.sell_time }}</td>
            <td>{{ t.symbol }}</td>
            <td>{{ t.amount }}</td>
            <td>{{ t.buy_price }}</td>
            <td>{{ t.sell_price }}</td>
            <td class="{% if t.pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ t.pnl }}</td>
            <td class="{% if t.pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ t.pnl_pct }}%</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
</div>
</body>
</html>
    ''', history=history)

@app.route('/login', methods=['GET', 'POST'])
def login():
    client_ip = request.remote_addr or 'unknown'
    login_attempts[client_ip] = [t for t in login_attempts[client_ip] if time.time() - t < BLOCK_TIME]
    if len(login_attempts[client_ip]) >= MAX_ATTEMPTS:
        flash('Too many attempts. Try again in 5 minutes.', 'danger')
        return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Login</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body class="bg-dark text-light">
<div class="container mt-5" style="max-width:400px">
    <h2 class="text-center">🔑 Admin Login</h2>
    {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
        <div class="alert alert-{{ category }}">{{ message }}</div>
        {% endfor %}
    {% endif %}
    {% endwith %}
    <form method="post">
        <div class="mb-3"><input type="text" name="username" class="form-control" placeholder="Username" required></div>
        <div class="mb-3"><input type="password" name="password" class="form-control" placeholder="Password" required></div>
        <button type="submit" class="btn btn-success w-100">Login</button>
    </form>
</div>
</body>
</html>
        ''')

    if request.method == 'POST':
        login_attempts[client_ip].append(time.time())
        if request.form['username'] == WEB_LOGIN and request.form['password'] == WEB_PASSWORD:
            session['logged_in'] = True
            flash('Login successful!', 'success')
            login_attempts.pop(client_ip, None)
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Incorrect credentials', 'danger')

    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Login</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body class="bg-dark text-light">
<div class="container mt-5" style="max-width:400px">
    <h2 class="text-center">🔑 Admin Login</h2>
    {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
        <div class="alert alert-{{ category }}">{{ message }}</div>
        {% endfor %}
    {% endif %}
    {% endwith %}
    <form method="post">
        <div class="mb-3"><input type="text" name="username" class="form-control" placeholder="Username" required></div>
        <div class="mb-3"><input type="password" name="password" class="form-control" placeholder="Password" required></div>
        <button type="submit" class="btn btn-success w-100">Login</button>
    </form>
</div>
</body>
</html>
    ''')

@app.route('/api/logs')
def api_logs():
    if not session.get('logged_in'):
        return "Unauthorized", 401
    logs = get_last_logs(100)
    return render_template_string('''
<pre class="bg-black p-3 text-light" style="max-height:400px;overflow:auto;font-size:0.85em;line-height:1.3">{{ ''.join(logs) }}</pre>
    ''', logs=logs)

@app.route('/admin', methods=['GET', 'POST'])
def admin_dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    if request.method == 'POST':
        email_enabled = request.form.get('email_enabled') == 'on'
        public_enabled = request.form.get('public_access_enabled') == 'on'
        try:
            max_slots = int(request.form.get('max_slots', 5))
            amount_per_trade = int(request.form.get('amount_per_trade', 10))
            max_pairs = int(request.form.get('max_pairs', 200))
            take_profit_pct = float(request.form.get('take_profit_pct', 20.0))
            cache_duration = int(request.form.get('cache_duration', 15))
            hours = int(request.form.get('report_interval_hours', 24))
            interval_sec = hours * 3600
        except:
            max_slots = 5
            amount_per_trade = 10
            max_pairs = 200
            take_profit_pct = 20.0
            cache_duration = 15
            interval_sec = 86400

        save_setting('email_enabled', email_enabled)
        save_setting('public_access_enabled', public_enabled)
        save_setting('max_slots', max_slots)
        save_setting('amount_per_trade', amount_per_trade)
        save_setting('max_pairs', max_pairs)
        save_setting('take_profit_pct', take_profit_pct)
        save_setting('cache_duration', cache_duration)
        save_setting('report_interval', interval_sec)
        flash('Settings updated successfully!', 'success')
        return redirect(url_for('admin_dashboard'))

    positions, total_pnl = get_positions_data()
    logs = get_last_logs(100)
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Admin Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <script>
        let timeLeft = 10;
        let timerInterval;
        function startTimer() {
            clearInterval(timerInterval);
            timeLeft = 10;
            document.getElementById('countdown').textContent = timeLeft;
            timerInterval = setInterval(() => {
                timeLeft--;
                document.getElementById('countdown').textContent = timeLeft;
                if (timeLeft <= 0) {
                    fetch('/api/logs')
                        .then(response => response.text())
                        .then(html => {
                            document.getElementById('logs-container').innerHTML = html;
                            startTimer();
                        })
                        .catch(err => console.error(err));
                }
            }, 1000);
        }
        window.onload = startTimer;
    </script>
</head>
<body class="bg-dark text-light">
<div class="container py-4">
    <h1>🔧 Admin Panel</h1>
    <a href="/" class="btn btn-outline-light">← Public View</a>
    <a href="/logout" class="btn btn-outline-danger float-end">Logout</a>

    <!-- Donation banner -->
    <div class="alert alert-secondary mt-3 d-flex align-items-center">
        <span class="me-3">💙 This bot is free and open-source.</span>
        <span class="me-2">If you find it useful, you can support development with a small donation:</span>
        <strong class="me-2">Dogecoin (DOGE)</strong>
        <code onclick="navigator.clipboard.writeText('DJxLre4fbUiaHJ3KvBTCjUpaDheWmDY8bz'); this.style.backgroundColor='#28a745'; setTimeout(()=>{this.style.backgroundColor='';}, 800);" 
              style="cursor:pointer; padding:4px 8px; border-radius:4px; font-size:0.9em;">
            DJxLre4fbUiaHJ3KvBTCjUpaDheWmDY8bz
        </code>
        <span class="ms-auto text-success small">Click to copy</span>
    </div>

    {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
        <div class="alert alert-{{ category }}">{{ message }}</div>
        {% endfor %}
    {% endif %}
    {% endwith %}

    <h3 class="mt-4">⚙️ Global Settings</h3>
    <form method="post" class="row g-3">
        <div class="col-md-3">
            <label>Max Positions</label>
            <input type="number" name="max_slots" class="form-control" value="{{ settings.max_slots }}" min="1">
        </div>
        <div class="col-md-3">
            <label>USDT per trade</label>
            <input type="number" name="amount_per_trade" class="form-control" value="{{ settings.amount_per_trade }}" min="1">
        </div>
        <div class="col-md-3">
            <label>Max pairs to scan</label>
            <input type="number" name="max_pairs" class="form-control" value="{{ settings.max_pairs }}" min="10">
        </div>
        <div class="col-md-3">
            <label>Take Profit %</label>
            <input type="number" name="take_profit_pct" class="form-control" value="{{ settings.take_profit_pct }}" min="1" step="0.1">
        </div>
        <div class="col-md-3">
            <label>Cache duration (seconds)</label>
            <input type="number" name="cache_duration" class="form-control" value="{{ settings.cache_duration }}" min="5">
        </div>
        <div class="col-md-3">
            <label>Report frequency (hours)</label>
            <input type="number" name="report_interval_hours" class="form-control" value="{{ settings.report_interval // 3600 }}" min="1">
        </div>
        <div class="col-md-3">
            <div class="form-check mt-4">
                <input type="checkbox" class="form-check-input" name="email_enabled" id="email_enabled" {% if settings.email_enabled %}checked{% endif %}>
                <label class="form-check-label" for="email_enabled">Enable email reports</label>
            </div>
        </div>
        <div class="col-md-3">
            <div class="form-check mt-4">
                <input type="checkbox" class="form-check-input" name="public_access_enabled" id="public_access_enabled" {% if settings.public_access_enabled %}checked{% endif %}>
                <label class="form-check-label" for="public_access_enabled">Enable public access</label>
            </div>
        </div>
        <div class="col-12 mt-3">
            <button type="submit" class="btn btn-primary">Save All Settings</button>
        </div>
    </form>

    <h3 class="mt-4">Open Positions</h3>
    <table class="table table-dark table-striped">
        <thead><tr><th>Symbol</th><th>Qty</th><th>Buy</th><th>Current Price</th><th>PNL USDT</th><th>PNL %</th></tr></thead>
        <tbody>
        {% for p in positions %}
        <tr>
            <td>{{ p.symbol }}</td>
            <td>{{ p.amount }}</td>
            <td>{{ p.buy_price }}</td>
            <td>{{ p.current_price }}</td>
            <td class="{% if p.pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ p.pnl }}</td>
            <td class="{% if p.pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ p.pnl_pct }}%</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    <h5>Total Open PNL : <strong class="{% if total_pnl > 0 %}text-success{% else %}text-danger{% endif %}">{{ total_pnl }} USDT</strong></h5>

    <h3 class="mt-4">Manual Actions</h3>
    <div class="row g-4">
        <div class="col-md-6">
            <h5>Manual Buy</h5>
            <form action="/buy" method="post">
                <input type="text" name="symbol" class="form-control mb-2" placeholder="BTC/USDT" required>
                <button type="submit" class="btn btn-success">Buy Now</button>
            </form>
        </div>
        <div class="col-md-6">
            <h5>Manual Sell</h5>
            <form action="/sell" method="post">
                <input type="text" name="symbol" class="form-control mb-2" placeholder="BTC/USDT" required>
                <button type="submit" class="btn btn-danger">Sell Now</button>
            </form>
        </div>
    </div>

    <h3 class="mt-4">Live Logs</h3>
    <p>Next refresh in <strong><span id="countdown">10</span></strong> seconds</p>
    <div id="logs-container">
        <pre class="bg-black p-3 text-light" style="max-height:400px;overflow:auto;font-size:0.85em;line-height:1.3">{{ ''.join(logs) }}</pre>
    </div>
</div>
</body>
</html>
    ''', positions=positions, total_pnl=total_pnl, logs=logs, settings=settings)

@app.route('/buy', methods=['POST'])
def web_buy():
    if not session.get('logged_in'): return redirect(url_for('login'))
    symbol = request.form['symbol'].strip().upper()
    if not symbol.endswith('/USDT'): symbol += '/USDT'
    try:
        manual_buy(symbol)
        flash(f'Manual buy started for {symbol}', 'success')
    except Exception as e:
        flash(f'Buy error: {e}', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/sell', methods=['POST'])
def web_sell():
    if not session.get('logged_in'): return redirect(url_for('login'))
    symbol = request.form['symbol'].strip().upper()
    if not symbol.endswith('/USDT'): symbol += '/USDT'
    try:
        manual_sell(symbol)
        flash(f'Manual sell started for {symbol}', 'success')
    except Exception as e:
        flash(f'Sell error: {e}', 'danger')
    return redirect(url_for('admin_dashboard'))

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('public_home'))

# ====================== LAUNCH ======================
def run_web_server():
    host = os.getenv('WEB_HOST', '0.0.0.0')
    port = int(os.getenv('WEB_PORT', 5000))
    logger.info(f"🌐 Web interface started → http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    load_settings()
    init_db()
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ['-h', '--help', '/?', 'help']:
            print_help()
        elif arg == 'show':
            show_positions()
        elif arg == 'sell':
            if len(sys.argv) < 3:
                print("❌ Usage: python script.py sell SYMBOL")
            else:
                manual_sell(sys.argv[2])
        elif arg == 'buy':
            if len(sys.argv) < 3:
                print("❌ Usage: python script.py buy SYMBOL")
            else:
                manual_buy(sys.argv[2])
        elif arg == 'history':
            if len(sys.argv) > 2:
                param = sys.argv[2].lower()
                if param == 'all':
                    show_history(0)
                elif param.endswith('m'):
                    try:
                        months = int(param[:-1])
                        show_history(months)
                    except ValueError:
                        print("❌ Invalid format.")
                else:
                    print("❌ Use: history 3m or history all")
            else:
                show_history()
        else:
            print(f"Unknown command: {arg}")
            print_help()
    else:
        file_handler = logging.FileHandler('tradibot.log', encoding='utf-8', mode='a')
        file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        logger.addHandler(file_handler)

        bot_thread = threading.Thread(target=main, daemon=True)
        bot_thread.start()

        run_web_server()