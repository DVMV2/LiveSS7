import os
import time
import json
import gspread
import pandas as pd
import mysql.connector
from datetime import datetime
import sys

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

# ---------------- DRIVER ---------------- #
def get_optimized_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts
    )
    return driver

# ---------------- MAIN ---------------- #
def main():
    driver = None
    db_conn = None

    try:
        # Use UTC for logging
        print(f"🕒 Execution Started at UTC: {datetime.utcnow()}")

        # ---------------- DB CONNECTION ---------------- #
        print("🔗 Connecting to Database...")
        db_conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
            autocommit=True # Keep this, but we will call commit explicitly to be safe
        )
        cur = db_conn.cursor(dictionary=True)

        # ✅ CLEAR OLD DATA
        print("🧹 Clearing old data...")
        cur.execute(f"TRUNCATE TABLE `{TARGET_TABLE}`")

        # ---------------- FETCH STOCKS ---------------- #
        cur.execute(f"""
            SELECT Symbol, real_close, real_change 
            FROM `{SOURCE_TABLE}` 
            WHERE CAST(real_change AS DECIMAL(10,2)) >= %s
        """, (CHANGE_THRESHOLD,))
        
        stocks = cur.fetchall()

        if not stocks:
            print("😴 No signals found. Terminating.")
            return

        # ---------------- LOAD GOOGLE SHEET ---------------- #
        creds = json.loads(os.getenv("GSPREAD_CREDENTIALS"))
        gc = gspread.service_account_from_dict(creds)
        ws = gc.open_by_url(STOCK_LIST_URL).get_worksheet_by_id(STOCK_LIST_GID)

        # Ensure we handle the dataframe indexing safely
        data = ws.get_all_values()
        df = pd.DataFrame(data[1:], columns=data[0]) 
        # Mapping Symbol (Col A) to URL (Col D)
        url_map = dict(zip(df.iloc[:, 0].str.upper().str.strip(), df.iloc[:, 3]))

        # ---------------- BROWSER ---------------- #
        print(f"🚀 Processing {len(stocks)} stocks...")
        driver = get_optimized_driver()
        driver.get("https://www.tradingview.com/")

        # Apply Cookies
        cookies = json.loads(os.getenv("TRADINGVIEW_COOKIES"))
        for c in cookies:
            driver.add_cookie({
                "name": c["name"],
                "value": c["value"],
                "domain": ".tradingview.com",
                "path": "/"
            })
        driver.refresh()

        success_count = 0

        # ---------------- LOOP ---------------- #
        for stock in stocks:
            symbol = stock["Symbol"].upper().strip()
            url = url_map.get(symbol)
            if not url:
                print(f"⚠️ No URL for {symbol}")
                continue

            try:
                db_conn.ping(reconnect=True, attempts=3, delay=2)
                print(f"📸 Capturing {symbol}...", end=" ", flush=True)

                driver.get(url)

                WebDriverWait(driver, 25).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "chart-container"))
                )
                time.sleep(5) 

                img_data = driver.get_screenshot_as_png()

                # ---------------- INSERT (UTC TIME) ---------------- #
                sql = f"""
                    INSERT INTO `{TARGET_TABLE}` 
                    (symbol, timeframe, real_change, real_close, screenshot, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """

                cur.execute(sql, (
                    symbol,
                    "day",
                    stock["real_change"],
                    stock["real_close"],
                    img_data,
                    datetime.utcnow()
                ))
                
                # Explicitly commit each record to ensure it is written immediately
                db_conn.commit()

                print("✅")
                success_count += 1

            except Exception as e:
                print(f"❌ Error: {str(e)[:50]}")

        print(f"🏁 Done. Total successful screenshots: {success_count}")

    except Exception as e:
        print(f"🚨 CRITICAL ERROR: {e}")

    finally:
        if db_conn and db_conn.is_connected():
            db_conn.commit() # Final safety commit
            cur.close()
            db_conn.close()
            print("🔌 Database connection closed.")
        if driver:
            driver.quit()

if __name__ == "__main__":
    main()
