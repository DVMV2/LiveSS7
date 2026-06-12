import os
import time
import json
import gspread
import pandas as pd
import mysql.connector
from datetime import datetime, timezone

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ---------------- CONFIG ---------------- #
STOCK_LIST_URL = "https://docs.google.com/spreadsheets/d/1V8DsH-R3vdUbXqDKZYWHk_8T0VRjqTEVyj7PhlIDtG4/edit#gid=0"
STOCK_LIST_GID = 1400370843
SOURCE_TABLE = "wp_live_close"
TARGET_TABLE = "live_screen"
CHANGE_THRESHOLD = 7.0 

def get_optimized_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--blink-features=AutomationControlled")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

def main():
    driver = None
    db_conn = None

    try:
        print(f"🕒 Started at: {datetime.now(timezone.utc)} UTC")

        # 1. Connect to Database
        db_conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
            autocommit=True, # Auto-save transactions instantly
            connect_timeout=10 
        )
        cur = db_conn.cursor(dictionary=True)
        print("✅ Connected to Database.")

        # 2. Fetch Breakout Stocks
        cur.execute(f"""
            SELECT Symbol, real_close, real_change 
            FROM `{SOURCE_TABLE}` 
            WHERE CAST(real_change AS DECIMAL(10,2)) >= %s
        """, (CHANGE_THRESHOLD,))
        stocks = cur.fetchall()

        if not stocks:
            print("😴 No breakout stocks found above threshold today. Exiting safely.")
            return

        # 3. Load URL map from Google Sheet
        creds = json.loads(os.getenv("GSPREAD_CREDENTIALS"))
        gc = gspread.service_account_from_dict(creds)
        ws = gc.open_by_url(STOCK_LIST_URL).get_worksheet_by_id(STOCK_LIST_GID)
        data = ws.get_all_values()
        
        df = pd.DataFrame(data[1:], columns=data[0]) 
        url_map = {}
        for _, row in df.iterrows():
            if len(row) >= 1 and not pd.isna(row.iloc[0]):
                sym = str(row.iloc[0]).upper().strip()
                url_map[sym] = {
                    "week": row.iloc[2] if len(row) > 2 else None,
                    "day": row.iloc[3] if len(row) > 3 else None
                }

        # 4. Process screenshots
        print(f"🚀 Processing {len(stocks)} breakthrough tickers...")
        driver = get_optimized_driver()
        
        # Inject TradingView login credentials via cookies once
        driver.get("https://www.tradingview.com/")
        for c in json.loads(os.getenv("TRADINGVIEW_COOKIES")):
            driver.add_cookie({"name": c["name"], "value": c["value"], "domain": ".tradingview.com", "path": "/"})
        driver.refresh()

        for stock in stocks:
            symbol = stock["Symbol"].upper().strip()
            urls = url_map.get(symbol)
            if not urls:
                continue

            for timeframe in ["week", "day"]:
                url = urls.get(timeframe)
                if not url or str(url).strip() == "":
                    continue

                try:
                    print(f"📸 Scraping {symbol} ({timeframe})...", end="", flush=True)
                    driver.get(url)

                    # Ensure the chart layout engine renders fully before snapshotting
                    WebDriverWait(driver, 15).until(
                        EC.visibility_of_element_located((By.XPATH, "//*[contains(@class, 'chart-container')]//canvas"))
                    )
                    time.sleep(4) # Brief pause for candles to draw cleanly
                    
                    img_data = driver.get_screenshot_as_png()

                    # 5. Insert or Update cleanly 
                    # `tags` = IFNULL(`tags`, VALUES(`tags`)) protects any manually typed text from getting touched!
                    sql = f"""
                        INSERT INTO `{TARGET_TABLE}` 
                        (symbol, timeframe, real_change, real_close, screenshot, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                        real_change = VALUES(real_change),
                        real_close = VALUES(real_close),
                        screenshot = VALUES(screenshot),
                        created_at = VALUES(created_at),
                        `tags` = IFNULL(`tags`, VALUES(`tags`))
                    """

                    cur.execute(sql, (
                        symbol, timeframe, 
                        stock["real_change"], stock["real_close"], 
                        img_data, datetime.now(timezone.utc)
                    ))
                    print(" ✅")

                except Exception as e:
                    print(f" ❌ Skipping: {str(e)[:40]}")

        print("🏁 Processing finished successfully.")

    except Exception as e:
        print(f"🚨 Script Interrupted: {e}")

    finally:
        if db_conn and db_conn.is_connected():
            cur.close()
            db_conn.close()
            print("🔌 Database link disconnected.")
        if driver:
            driver.quit()

if __name__ == "__main__":
    main()
