import requests
import pandas as pd
from datetime import datetime, timedelta
import yfinance as yf
import asyncio
from telegram import Bot
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
import time
import re
from dotenv import load_dotenv
import os

load_dotenv()

# === CONFIGURATION ===
DAYS_LIMIT = 15
MIN_AMOUNT = 1000
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

# --- Functions ---

# Converts a size string like '5K' or '1.2K-3K' into a float representing the minimum value.
# Returns 0 if the size is undisclosed or cannot be parsed.
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

def parse_pub_date(pub_date_raw):
    pub_date_raw = pub_date_raw.strip()
    
    # Caso especial: contiene hora y Today/Yesterday
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

    # Intento normal
    try:
        return pd.to_datetime(pub_date_raw, dayfirst=True)
    except:
        return None

# Scrapes trade data from "https://www.capitoltrades.com/trades" using Selenium.
# Returns a pandas DataFrame with columns: Date, Politician, Company, Amount, Type, and Ticker.
def get_trades_selenium(pages=5):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")

    all_trades = []

    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

        for page_num in range(1, pages + 1):
            url = f"https://www.capitoltrades.com/trades?sortBy=-txDate&page={page_num}"
            driver.get(url)
            time.sleep(7)  # espera que cargue la página

            rows = driver.find_elements(By.CSS_SELECTOR, "tr.border-b.h-14.border-primary-15")

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
                        # Skip dates in the future
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

# Filters the trades DataFrame to keep only trades within the last DAYS_LIMIT days
# and with an Amount greater or equal to MIN_AMOUNT.
def filter_trades(df):
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    limit_date = datetime.now() - timedelta(days=DAYS_LIMIT)
    df = df.dropna(subset=['Date'])
    df = df[(df['Date'] >= limit_date) & (df['Amount'] >= MIN_AMOUNT)]
    return df


# Cleans the ticker string by removing the ':US' suffix if present.
def clean_ticker(ticker):
    if ticker and ticker.endswith(":US"):
        return ticker.split(":")[0]
    return ticker


# Downloads 6 months of price data for a given ticker using yfinance.
# Calculates the 50-day and 100-day simple moving averages (SMA).
# Determines the trend based on the relationship between current price and SMAs:
# returns 'Bullish', 'Bearish', or 'Neutral'.
def check_trend(ticker):
    try:
        data = yf.download(ticker, period='6mo', progress=False)
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


# Returns True if the trend info indicates a 'Bullish' trend.
def is_bullish(trend_info):
    return trend_info and trend_info.get("trend") == "Bullish"


# Returns True if the trend info indicates a 'Bearish' trend.
def is_bearish(trend_info):
    return trend_info and trend_info.get("trend") == "Bearish"


# Sends a Telegram message alert with trade opportunities.
# Includes ticker, trade type, politician, trade date, amount, and trend info.
async def send_alert(trades):
    if trades.empty:
        message = "No new opportunities based on Capitol Trades."
    else:
        message = "📊 Opportunities detected:\n"
        for _, row in trades.iterrows():
            ticker = row['Ticker'] if pd.notnull(row['Ticker']) else "N/A"
            type_upper = row['Type'].upper()
            company = row['Company']
            politician = row['Politician']
            trend_info = row['Trend']

            message += (
                f"\n✅ {ticker} ({type_upper}) by {company}"
                f"\n👤 Politician: {politician}"
                f"\n📅 Trade Date: {row['Date'].date()}"
                f"\n🗓️ Publication Date: {row['PublicationDate'].strftime('%d/%m/%Y %H:%M') if pd.notnull(row['PublicationDate']) else 'N/A'}"
                f"\n💰 Amount: ${row['Amount']:,.0f}"
            )

            if trend_info:
                message += (
                    f"\n📊 Price: {trend_info['price']:.2f} | SMA50: {trend_info['sma50']:.2f} | SMA100: {trend_info['sma100']:.2f}"
                    f"\n📈 Trend: {trend_info['trend']}\n"
                )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

async def main():
    df = get_trades_selenium()
    print("Extracted data:")
    print(df.head())

    filtered_df = filter_trades(df)
    print("Filtered data:")
    print(filtered_df)

    # Get trends for each ticker
    filtered_df['Trend'] = filtered_df['Ticker'].apply(lambda x: check_trend(clean_ticker(x)))

    # Filter bullish buys and bearish sells
    bullish_buys = filtered_df[(filtered_df['Type'] == 'buy') & (filtered_df['Trend'].apply(is_bullish))]
    bearish_sells = filtered_df[(filtered_df['Type'] == 'sell') & (filtered_df['Trend'].apply(is_bearish))]

    alerts = pd.concat([bullish_buys, bearish_sells])

    await send_alert(alerts)
    print("Process completed.")

if __name__ == "__main__":
    asyncio.run(main())
