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
    Advanced recursive pop-up cleaner. Destroys marketing boxes, sales promotions, 
    and backdrops across the main document, Shadow DOMs, and nested Iframes.
    """
    js_script = """
        const selectors = [
            "[class*='overlap']", "[class*='dialog']", "[class*='modal']", 
            ".tv-dialog", "div[data-role='modal-container']", 
            "div[class*='overlapManager']", "#gcap-reveal-modal", 
            "[class*='toast']", "[class*='cookie']", "[id*='overlap']"
        ];

        function nukeElements(root) {
            if (!root) return;
            // 1. Clear matching nodes in this root
            selectors.forEach(selector => {
                root.querySelectorAll(selector).forEach(el => {
                    try { el.remove(); } catch(e) {}
                });
            });

            // 2. Scan and clear within Shadow DOMs
            root.querySelectorAll('*').forEach(el => {
                if (el.shadowRoot) {
                    nukeElements(el.shadowRoot);
                }
            });
        }

        // Clean Main Document Frame
        nukeElements(document);

        // Clean All Available IFrames Deeply
        document.querySelectorAll('iframe').forEach(iframe => {
            try {
                const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                if (iframeDoc) {
                    nukeElements(iframeDoc);
                    // If the iframe itself is a promotional popup canvas, eliminate it entirely
                    if (iframe.outerHTML.includes('sale') || iframe.outerHTML.includes('promo') || iframe.outerHTML.includes('overlap')) {
                        iframe.remove();
                    }
                }
            } catch(e) {
                // Cross-origin boundaries are bypassed if standard elements; otherwise safely ignored
            }
        });

        // Re-enable scrolling layouts just in case they were locked by an overlay
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
        
        driver.set_window_size(1920, 1080)

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

                    # Ensure the canvas element rendering engine is loaded
                    WebDriverWait(driver, 20).until(
                        EC.visibility_of_element_located((By.XPATH, "//*[contains(@class, 'chart-container')]//canvas"))
                    )
                    
                    # Wait for TradingView to finish loading
                    time.sleep(5)

                    # Run popup cleaner multiple times
                    for _ in range(3):
                        clear_popups(driver)
                        time.sleep(1)

                    # Force page to normal state
                    driver.execute_script("""
                    window.scrollTo(0,0);
                    document.body.style.overflow='visible';
                    document.documentElement.style.overflow='visible';
                    """)

                    # Give DOM time to settle
                    time.sleep(2)

                    # DEBUG SCREENSHOT
                    driver.save_screenshot(f"debug_{symbol}_{timeframe}.png")

                    # Final screenshot
                    img_data = driver.get_screenshot_as_png()

                    # ---------------- DEFENSIVE RECONNECT PING ---------------- #
                    try:
                        db_conn.ping(reconnect=True, attempts=3, delay=1)
                    except mysql.connector.Error:
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
                db_conn.rollback()
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
