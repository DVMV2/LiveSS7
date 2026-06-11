import os
import time
import json
import gspread
import pandas as pd
import mysql.connector
from datetime import datetime, timezone
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
    
    # Performance & anti-bot evasion optimizations
    opts.add_argument("--disable-gpu")
    opts.add_argument("--blink-features=AutomationControlled")
    
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts
    )
    return driver

# ---------------- POPUP CLEANER ---------------- #
def clear_popups(driver):
    """
    Injects JavaScript to force-delete known TradingView modal dialogs, 
    overlays, and login/upgrade prompts that block the chart.
    """
    selectors_to_remove = [
        "[class^='overlap-']",            # General overlay screens
        "[class*='dialog']",             # Dialog boxes/Popups
        "[class*='modal']",              # Generic modals
        ".tv-dialog",                    # Old legacy TV dialogs
        "div[data-role='modal-container']", # Modern TV modal wrapper
        "div[class*='overlapManager']",  # TV's custom popup layers
        "#gcap-reveal-modal",            # Adblock/Promo specific IDs
        "[class*='toast']",              # Notification snackbars
        "[class*='cookie']"              # Cookie consent banners
    ]
    
    js_script = f"""
        const selectors = {json.dumps(selectors_to_remove)};
        selectors.forEach(selector => {{
            document.querySelectorAll(selector).forEach(el => {{
                try {{ el.remove(); }} catch(e) {{}}
            }});
        }});
        // Restore scrolling on the body if a modal disabled it
        document.body.style.overflow = 'auto';
        document.documentElement.style.overflow = 'auto';
    """
    try:
        driver.execute_script(js_script)
    except Exception as e:
        print(f"⚠️ Non-fatal: Failed to clean popups via JS: {str(e)[:50]}")

# ---------------- MAIN ---------------- #
def main():
    driver = None
    db_conn = None

    try:
        # Use timezone-aware UTC logging
        print(f"🕒 Execution Started at UTC: {datetime.now(timezone.utc)}")

        # ---------------- DB CONNECTION WITH RETRIES ---------------- #
        print("🔗 Connecting to Database...")
        max_retries = 5
        retry_delay = 5  # seconds
        
        for attempt in range(1, max_retries + 1):
            try:
                db_conn = mysql.connector.connect(
                    host=os.getenv("DB_HOST"),
                    user=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    database=os.getenv("DB_NAME"),
                    autocommit=False, # Set to False for true transaction grouping
                    connect_timeout=10 
                )
                if db_conn.is_connected():
                    print("✅ Successfully connected to the database!")
                    break
            except mysql.connector.Error as err:
                print(f"⚠️ Connection attempt {attempt}/{max_retries} failed: {err}")
                if attempt < max_retries:
                    print(f"⏳ Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    print("🚨 Fatal: Could not establish a database connection after multiple attempts.")
                    return

        cur = db_conn.cursor(dictionary=True)

        # ✅ REMOVE ONLY ROWS WHERE TAGS ARE BLANK OR NULL
        print("🧹 Clearing rows with blank tags...")
        cur.execute(f"DELETE FROM `{TARGET_TABLE}` WHERE `tags` IS NULL OR TRIM(`tags`) = ''")
        db_conn.commit()

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

        data = ws.get_all_values()
        if len(data) <= 1:
            print("🚨 Sheet is empty or missing data rows.")
            return
            
        df = pd.DataFrame(data[1:], columns=data[0]) 
        
        # Mapping Symbol Safely
        url_map = {}
        for _, row in df.iterrows():
            if len(row) < 1 or pd.isna(row.iloc[0]):
                continue
            symbol_key = str(row.iloc[0]).upper().strip()
            if symbol_key:
                url_map[symbol_key] = {
                    "week": row.iloc[2] if len(row) > 2 else None,
                    "day": row.iloc[3] if len(row) > 3 else None
                }

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
        uncommitted_mutations = 0

        # ---------------- LOOP ---------------- #
        for stock in stocks:
            symbol = stock["Symbol"].upper().strip()
            urls = url_map.get(symbol)
            
            if not urls:
                print(f"⚠️ No URLs found in sheet for {symbol}")
                continue

            for timeframe in ["week", "day"]:
                url = urls.get(timeframe)
                if not url or str(url).strip() == "":
                    print(f"⚠️ No {timeframe} URL for {symbol}")
                    continue

                try:
                    print(f"📸 Capturing {symbol} ({timeframe})...", end=" ", flush=True)
                    driver.get(url)

                    # Better wait setup: Ensure the canvas element rendering engine is loaded
                    WebDriverWait(driver, 20).until(
                        EC.visibility_of_element_located((By.XPATH, "//*[contains(@class, 'chart-container')]//canvas"))
                    )
                    
                    # Nuke any popups right before taking the shot
                    clear_popups(driver)
                    
                    # Small grace sleep for crosshairs/indicators to clean up and settle
                    time.sleep(2.5) 

                    img_data = driver.get_screenshot_as_png()

                    # ---------------- DEFENSIVE RECONNECT PING ---------------- #
                    # Restored specifically right before execution to prevent idle timeouts from breaking the batch
                    try:
                        db_conn.ping(reconnect=True, attempts=3, delay=1)
                    except mysql.connector.Error:
                        # Fallback case if ping fails completely: regenerate cursor context
                        cur.close()
                        cur = db_conn.cursor(dictionary=True)

                    # ---------------- INSERT OR UPDATE ---------------- #
                    sql = f"""
                        INSERT INTO `{TARGET_TABLE}` 
                        (symbol, timeframe, real_change, real_close, screenshot, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                        real_change = VALUES(real_change),
                        real_close = VALUES(real_close),
                        screenshot = VALUES(screenshot),
                        created_at = VALUES(created_at)
                    """

                    cur.execute(sql, (
                        symbol,
                        timeframe,
                        stock["real_change"],
                        stock["real_close"],
                        img_data,
                        datetime.now(timezone.utc)
                    ))
                    
                    success_count += 1
                    uncommitted_mutations += 1

                    # Batch commits every 6 actions (3 stocks) to lower disk overhead
                    if uncommitted_mutations >= 6:
                        db_conn.commit()
                        uncommitted_mutations = 0

                    print("✅")

                except Exception as e:
                    print(f"❌ Error during {timeframe} capture: {str(e)[:60]}")

        # Final batch commit for any remaining mutations
        if uncommitted_mutations > 0:
            try:
                db_conn.ping(reconnect=True)
                db_conn.commit()
            except Exception:
                pass

        print(f"🏁 Done. Total successful screenshots: {success_count}")

    except Exception as e:
        print(f"🚨 CRITICAL ERROR: {e}")
        if db_conn:
            try:
                db_conn.rollback() # Rollback open transaction on failure
            except Exception:
                pass

    finally:
        if db_conn and db_conn.is_connected():
            cur.close()
            db_conn.close()
            print("🔌 Database connection closed.")
        if driver:
            driver.quit()

if __name__ == "__main__":
    main()
