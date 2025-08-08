import requests
import pandas as pd
from datetime import datetime, timedelta
from telegram.ext import ApplicationBuilder
import yfinance as yf
import asyncio
from telegram import Bot
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
import os
from dotenv import load_dotenv

# === Load env variables ===
# .env for local execution; repository secrets for Github Actions execution
load_dotenv()

# === Config ===
DAYS_LIMIT = 15
MIN_AMOUNT = 1000
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Faltan las variables TELEGRAM_TOKEN o TELEGRAM_CHAT_ID.")

application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# --- Aux functions ---

# Parses the size string (e.g., "$1K-$5K") and returns the minimum amount as a float.
# If the size is 'Undisclosed' or invalid, returns 0.
def parse_size(size_str):
    if not size_str or 'Undisclosed' in size_str:
        return 0
    size_str = size_str.replace('–', '-')
    if '-' in size_str:
        min_part = size_str.split('-')[0]
    else:
        min_part = size_str
    match = re.match(r'(\d+(?:\.\d+)?)K?', min_part.strip())
    if match:
        num = float(match.group(1))
        if 'K' in min_part:
            num *= 1000
        return num
    try:
        return float(min_part)
    except:
        return 0

# Parses the publication date string and converts it to a datetime object.
# Handles formats like "12:34 Today" or "12:34 Yesterday", or falls back to pandas parsing.
def parse_pub_date(pub_date_raw):
    pub_date_raw = pub_date_raw.strip()
    
    if re.match(r'^\d{1,2}:\d{2}\s+(Today|Yesterday)$', pub_date_raw, re.IGNORECASE):
        time_str, day_word = pub_date_raw.split()
        time_part = datetime.strptime(time_str, "%H:%M").time()
        
        if day_word.lower() == "today":
            date_part = datetime.now().date()
        elif day_word.lower() == "yesterday":
            date_part = (datetime.now() - timedelta(days=1)).date()
        else:
            return None
        
        return datetime.combine(date_part, time_part)

    try:
        return pd.to_datetime(pub_date_raw, dayfirst=True)
    except:
        return None

# Scrapes trade data from Capitol Trades using Selenium.
# Iterates through multiple pages, extracts trade info, and returns it as a pandas DataFrame.
def get_trades_selenium(pages=5):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--remote-debugging-port=9222")

    all_trades = []

    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

        for page_num in range(1, pages + 1):
            url = f"https://www.capitoltrades.com/trades?sortBy=-txDate&page={page_num}"
            driver.get(url)

            rows = WebDriverWait(driver, 15).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "tr.border-b.h-14.border-primary-15"))
            )

            for row in rows:
                cols = row.find_elements(By.TAG_NAME, "td")
                if len(cols) >= 10:
                    politician = cols[0].text.strip()
                    company_ticker_raw = cols[1].text.strip()

                    if '\n' in company_ticker_raw:
                        company, ticker = company_ticker_raw.split('\n', 1)
                        ticker = ticker.strip()
                    else:
                        company = company_ticker_raw
                        ticker = None

                    date_raw = cols[3].text.strip().replace('\n', ' ')
                    try:
                        date = pd.to_datetime(date_raw, dayfirst=True)
                        if date > datetime.now():
                            continue
                    except:
                        date = None

                    pub_date_raw = cols[2].text.strip().replace('\n', ' ')
                    pub_date = parse_pub_date(pub_date_raw)

                    trade_type = cols[6].text.strip().lower()
                    amount_raw = cols[7].text.strip()
                    amount = parse_size(amount_raw)

                    all_trades.append([date, pub_date, politician, company, amount, trade_type, ticker])

        df = pd.DataFrame(all_trades, columns=['Date', 'PublicationDate', 'Politician', 'Company', 'Amount', 'Type', 'Ticker'])
        return df

    except WebDriverException as e:
        print(f"Selenium error: {e}")
        return pd.DataFrame(columns=['Date', 'PublicationDate', 'Politician', 'Company', 'Amount', 'Type', 'Ticker'])

    finally:
        try:
            driver.quit()
        except:
            pass

# Filters the trades DataFrame by date (last DAYS_LIMIT days) and minimum amount (MIN_AMOUNT).
def filter_trades(df):
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    limit_date = datetime.now() - timedelta(days=DAYS_LIMIT)
    df = df.dropna(subset=['Date'])
    df = df[(df['Date'] >= limit_date) & (df['Amount'] >= MIN_AMOUNT)]
    return df

# Cleans the ticker by removing the ":US" suffix if present.
def clean_ticker(ticker):
    if ticker and ticker.endswith(":US"):
        return ticker.split(":")[0]
    return ticker

# Downloads 6 months of price data for the given ticker and checks its trend using SMA50 and SMA100.
# Returns the last price, SMA50, SMA100, and the trend ("Bullish", "Bearish", or "Neutral").
def check_trend(ticker):
    try:
        data = yf.download(ticker, period='6mo', progress=False, auto_adjust=True)
        if data.empty:
            print(f"No data for {ticker}")
            return None

        close = data['Close']
        sma50 = close.rolling(window=50).mean()
        sma100 = close.rolling(window=100).mean()

        last_price_val = close.iloc[-1].item()
        sma50_val = sma50.iloc[-1].item()
        sma100_val = sma100.iloc[-1].item()

        if last_price_val > sma50_val > sma100_val:
            trend = "Bullish"
        elif last_price_val < sma50_val < sma100_val:
            trend = "Bearish"
        else:
            trend = "Neutral"

        print(f"{ticker} -> Price: {last_price_val:.2f}, SMA50: {sma50_val:.2f}, SMA100: {sma100_val:.2f}, Trend: {trend}")

        return {
            "price": last_price_val,
            "sma50": sma50_val,
            "sma100": sma100_val,
            "trend": trend
        }

    except Exception as e:
        print(f"Error getting trend for {ticker}: {e}")
        return None

# Checks if the given trend info indicates a bullish trend.
def is_bullish(trend_info):
    return trend_info and trend_info.get("trend") == "Bullish"

# Checks if the given trend info indicates a bearish trend.
def is_bearish(trend_info):
    return trend_info and trend_info.get("trend") == "Bearish"

# Sends a formatted alert message to a Telegram chat with trade opportunities and trend info.
async def send_alert(trades):
    if trades.empty:
        await application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text="No new opportunities based on Capitol Trades."
        )
        return
    
    total_ops = len(trades)

    # Copy of the original Trend
    trades['TrendFull'] = trades['Trend']

    # Aux column just with "Bullish", "Bearish" or "Neutral"
    trades['TrendLabel'] = trades['Trend'].apply(lambda x: x['trend'] if isinstance(x, dict) else None)

    # Group to count repetitions
    trades_grouped = (
        trades.groupby(['Politician', 'Ticker', 'Date', 'Amount', 'Type', 'Company', 'PublicationDate', 'TrendLabel'])
            .size()
            .reset_index(name='Repetitions')
    )

    # Recover original Trend for the message
    trades_grouped['Trend'] = trades_grouped.apply(
        lambda row: trades.loc[
            (trades['Politician'] == row['Politician']) &
            (trades['Ticker'] == row['Ticker']) &
            (trades['Date'] == row['Date']) &
            (trades['Amount'] == row['Amount']) &
            (trades['Type'] == row['Type']) &
            (trades['Company'] == row['Company']) &
            (trades['PublicationDate'] == row['PublicationDate']),
            'TrendFull'
        ].iloc[0],
        axis=1
    )

    message = "📊 Opportunities detected (Total trades: {total_ops}):\n"
    messages = []
    counter = 1

    for _, row in trades_grouped.iterrows():
        ticker = row['Ticker'] if pd.notnull(row['Ticker']) else "N/A"
        type_upper = row['Type'].upper()
        company = row['Company']
        politician = row['Politician']
        trend_info = row['Trend']

        trade_text = (
            f"\n{counter}️⃣ {ticker} ({type_upper}) by {company}"
            f"\n👤 Politician: {politician}"
            f"\n📅 Trade Date: {row['Date'].date()}"
            f"\n🗓️ Publication Date: {row['PublicationDate'].date() if pd.notnull(row['PublicationDate']) else 'N/A'}"
            f"\n💰 Amount: ${row['Amount']:,.0f}"
        )

        if trend_info:
            trade_text += (
                f"\n📊 Price: {trend_info['price']:.2f} | SMA50: {trend_info['sma50']:.2f} | SMA100: {trend_info['sma100']:.2f}"
                f"\n📈 Trend: {trend_info['trend']}\n"
            )

        if row['Repetitions'] > 1:
            trade_text += f"\n🔁 Repeated {row['Repetitions']} times"
        
        trade_text += "\n"
        counter += 1

        if len(message) + len(trade_text) > 4000:  # Telegram has a lenth limit for its messages
            messages.append(message)
            message = trade_text
        else:
            message += trade_text

    if message:
        messages.append(message)

    # Enviar todos los mensajes en orden
    for msg in messages:
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)

async def main():
    df = get_trades_selenium()
    print("Extracted data:")
    print(df.head())

    filtered_df = filter_trades(df)
    print("Filtered data:")
    print(filtered_df)

    filtered_df['Trend'] = filtered_df['Ticker'].apply(
        lambda x: check_trend(clean_ticker(x)) if pd.notnull(x) and x != "N/A" else None
    )

    bullish_buys = filtered_df[(filtered_df['Type'] == 'buy') & (filtered_df['Trend'].apply(is_bullish))]
    bearish_sells = filtered_df[(filtered_df['Type'] == 'sell') & (filtered_df['Trend'].apply(is_bearish))]

    alerts = pd.concat([bullish_buys, bearish_sells])

    await send_alert(alerts)
    print("Process completed.")

if __name__ == "__main__":
    asyncio.run(main())
