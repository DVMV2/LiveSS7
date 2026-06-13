import os
import time
import json
import gspread
import pandas as pd
import mysql.connector
from mysql.connector import Error as MySQLError
from datetime import datetime, timezone

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ---------------- CONFIG ---------------- #
STOCK_LIST_URL = "https://docs.google.com/spreadsheets/d/1V8DsH-R3vdUbXqDKZYWHk_8T0VRjqTEVyj7PhlIDtG4/edit#gid=0"
STOCK_LIST_GID = 1400370843
SOURCE_TABLE = "wp_live_close"
TARGET_TABLE = "live_screen"
CHANGE_THRESHOLD = 7.0

DB_CONNECT_RETRIES = 5       # number of attempts
DB_CONNECT_RETRY_DELAY = 5   # seconds between attempts (will increase)


def get_optimized_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--blink-features=AutomationControlled")
    opts.add_argument("--disable-notifications")
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2
    })
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)


def get_db_connection():
    """
    Robust DB connection with retries + exponential backoff.
    Also sets sane timeouts so the connection doesn't hang forever
    or silently drop later.
    """
    last_err = None
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        try:
            conn = mysql.connector.connect(
                host=os.getenv("DB_HOST"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                database=os.getenv("DB_NAME"),
                autocommit=True,
                connect_timeout=10,
                connection_timeout=10,
                use_pure=True,          # avoids some C-extension flakiness
                pool_reset_session=True,
            )
            if conn.is_connected():
                print(f"✅ Connected to Database (attempt {attempt}).")
                return conn
        except MySQLError as e:
            last_err = e
            wait = DB_CONNECT_RETRY_DELAY * attempt
            print(f"⚠️ DB connect attempt {attempt}/{DB_CONNECT_RETRIES} failed: {e}")
            if attempt < DB_CONNECT_RETRIES:
                print(f"⏳ Retrying in {wait}s...")
                time.sleep(wait)

    raise ConnectionError(f"❌ Could not connect to database after {DB_CONNECT_RETRIES} attempts: {last_err}")


def ensure_connection(conn):
    """Re-ping / reconnect if the connection has dropped mid-run."""
    try:
        conn.ping(reconnect=True, attempts=3, delay=2)
    except MySQLError:
        return get_db_connection()
    return conn


def dismiss_popups(driver):
    """Close common TradingView overlay dialogs/popups that block the chart."""
    selectors = [
        "//button[contains(@aria-label, 'Close')]",
        "//button[contains(@class, 'close')]",
        "//div[@data-name='popup-button-close']",
        "//*[contains(@class, 'dialogCloseButton')]",
        "//button[contains(text(), 'Got it')]",
        "//button[contains(text(), 'No, thanks')]",
        "//button[contains(text(), 'Accept')]",
        "//button[contains(text(), 'Accept all')]",
        "//div[contains(@class,'tv-dialog__close')]",
        "//*[contains(@class, 'tv-dialog__close')]",
        "//*[@data-name='close']",
    ]
    for sel in selectors:
        try:
            elems = driver.find_elements(By.XPATH, sel)
            for el in elems:
                if el.is_displayed():
                    try:
                        el.click()
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].click();", el)
                        except Exception:
                            pass
                    time.sleep(0.3)
        except Exception:
            pass

    # ESC key fallback — closes most TradingView modals
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass


def main():
    driver = None
    db_conn = None
    cur = None

    try:
        print(f"🕒 Started at: {datetime.now(timezone.utc)} UTC")

        # 1. Connect to Database (robust, with retries)
        db_conn = get_db_connection()
        cur = db_conn.cursor(dictionary=True)

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

        # 3. Cleanup: remove all untagged rows from live_screen first
        cur.execute(f"DELETE FROM `{TARGET_TABLE}` WHERE `tags` IS NULL")
        print(f"🧹 Removed {cur.rowcount} untagged row(s) from `{TARGET_TABLE}`.")

        # 4. Load URL map from Google Sheet
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

        # 5. Process screenshots
        print(f"🚀 Processing {len(stocks)} breakthrough tickers...")
        driver = get_optimized_driver()

        # Inject TradingView login credentials via cookies once
        driver.get("https://www.tradingview.com/")
        for c in json.loads(os.getenv("TRADINGVIEW_COOKIES")):
            driver.add_cookie({"name": c["name"], "value": c["value"], "domain": ".tradingview.com", "path": "/"})
        driver.refresh()
        time.sleep(2)
        dismiss_popups(driver)

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

                    # Dismiss any popups/dialogs that appear after chart load
                    time.sleep(1)
                    dismiss_popups(driver)
                    time.sleep(1)
                    dismiss_popups(driver)  # second pass for late-appearing popups

                    time.sleep(3)  # Brief pause for candles to draw cleanly

                    img_data = driver.get_screenshot_as_png()

                    # Make sure DB connection is alive before each write
                    db_conn = ensure_connection(db_conn)
                    cur = db_conn.cursor(dictionary=True)

                    # Insert new row or update existing tagged row
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
                        symbol, timeframe,
                        stock["real_change"], stock["real_close"],
                        img_data, datetime.now(timezone.utc)
                    ))
                    print(" ✅")

                except Exception as e:
                    print(f" ❌ Skipping: {str(e)[:80]}")

        print("🏁 Processing finished successfully.")

    except Exception as e:
        print(f"🚨 Script Interrupted: {e}")

    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        if db_conn and db_conn.is_connected():
            db_conn.close()
            print("🔌 Database link disconnected.")
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()
