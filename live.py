import os
import io
import time
import json
import gspread
import pandas as pd
import mysql.connector
import traceback
from datetime import datetime
from PIL import Image

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ---------------- CONFIG ---------------- #
STOCK_LIST_URL    = "https://docs.google.com/spreadsheets/d/1V8DsH-R3vdUbXqDKZYWHk_8T0VRjqTEVyj7PhlIDtG4/edit#gid=0"
STOCK_LIST_GID    = 1400370843
SOURCE_TABLE      = "wp_live_close"
TARGET_TABLE      = "live_screen"
CHANGE_THRESHOLD  = 7.0
MAX_DB_RETRIES    = 5
BASE_RETRY_DELAY  = 5   # seconds — doubles each attempt (exponential backoff)
CONNECT_TIMEOUT   = 30  # increased from 10 → gives DB more breathing room


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


# ---------------- COMPRESS SCREENSHOT ---------------- #
def compress_screenshot(raw_png: bytes, size=(1280, 720), quality=70) -> bytes:
    """Resize + JPEG-compress a PNG screenshot to reduce DB bloat."""
    img = Image.open(io.BytesIO(raw_png))
    img = img.resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---------------- DB CONNECT WITH EXPONENTIAL BACKOFF ---------------- #
def connect_db():
    for attempt in range(1, MAX_DB_RETRIES + 1):
        try:
            conn = mysql.connector.connect(
                host=os.getenv("DB_HOST"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                database=os.getenv("DB_NAME"),
                autocommit=False,           # manual commits only — no conflict
                connect_timeout=CONNECT_TIMEOUT,
            )
            if conn.is_connected():
                print("✅ Database connected successfully.")
                return conn
        except mysql.connector.Error as err:
            wait = BASE_RETRY_DELAY * (2 ** (attempt - 1))  # 5,10,20,40,80s
            print(f"⚠️  Attempt {attempt}/{MAX_DB_RETRIES} failed: {err}")
            if attempt < MAX_DB_RETRIES:
                print(f"⏳ Retrying in {wait}s...")
                time.sleep(wait)

    print("🚨 Fatal: Could not connect to DB after all attempts.")
    return None


# ---------------- MAIN ---------------- #
def main():
    driver   = None
    db_conn  = None
    cur      = None   # ← initialised here so finally block is always safe

    try:
        print(f"🕒 Started at UTC: {datetime.utcnow()}")

        # ── DB ──────────────────────────────────────────────────────────── #
        print("🔗 Connecting to Database...")
        db_conn = connect_db()
        if db_conn is None:
            return

        cur = db_conn.cursor(dictionary=True)

        # ── CLEAN BLANK-TAG ROWS ─────────────────────────────────────────── #
        print("🧹 Removing rows with blank/null tags...")
        cur.execute(
            f"DELETE FROM `{TARGET_TABLE}` WHERE `tags` IS NULL OR TRIM(`tags`) = ''"
        )
        db_conn.commit()

        # ── FETCH QUALIFYING STOCKS ──────────────────────────────────────── #
        cur.execute(f"""
            SELECT Symbol, real_close, real_change
            FROM `{SOURCE_TABLE}`
            WHERE CAST(real_change AS DECIMAL(10,2)) >= %s
        """, (CHANGE_THRESHOLD,))
        stocks = cur.fetchall()

        if not stocks:
            print("😴 No signals found. Terminating.")
            return

        print(f"📊 {len(stocks)} stock(s) qualify.")

        # ── GOOGLE SHEET ─────────────────────────────────────────────────── #
        print("📄 Loading stock URL map from Google Sheets...")
        creds = json.loads(os.getenv("GSPREAD_CREDENTIALS"))
        gc    = gspread.service_account_from_dict(creds)
        ws    = gc.open_by_url(STOCK_LIST_URL).get_worksheet_by_id(STOCK_LIST_GID)

        data = ws.get_all_values()
        df   = pd.DataFrame(data[1:], columns=data[0])

        symbols_col = df.iloc[:, 0].str.upper().str.strip()

        # Warn about duplicate symbols in the sheet
        dupes = symbols_col[symbols_col.duplicated()].tolist()
        if dupes:
            print(f"⚠️  Duplicate symbols in sheet (last row wins): {dupes}")

        url_map = dict(zip(symbols_col, df.iloc[:, 3]))

        # ── BROWSER ──────────────────────────────────────────────────────── #
        print("🚀 Launching browser...")
        driver = get_optimized_driver()
        driver.get("https://www.tradingview.com/")

        cookies = json.loads(os.getenv("TRADINGVIEW_COOKIES"))
        for c in cookies:
            driver.add_cookie({
                "name":   c["name"],
                "value":  c["value"],
                "domain": ".tradingview.com",
                "path":   "/",
            })
        driver.refresh()

        success_count = 0

        # ── LOOP ─────────────────────────────────────────────────────────── #
        for stock in stocks:
            symbol = stock["Symbol"].upper().strip()
            url    = url_map.get(symbol)

            if not url:
                print(f"⚠️  No URL found for {symbol} — skipping.")
                continue

            try:
                # Keep DB alive across long loops; recreate cursor after reconnect
                db_conn.ping(reconnect=True, attempts=3, delay=2)
                cur = db_conn.cursor(dictionary=True)  # ← fresh cursor after ping

                print(f"📸 Capturing {symbol}...", end=" ", flush=True)

                driver.get(url)

                # Wait for chart container
                WebDriverWait(driver, 25).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "chart-container"))
                )
                # Wait for full page load
                WebDriverWait(driver, 15).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(2)  # small buffer for chart data to render

                raw_png  = driver.get_screenshot_as_png()
                img_data = compress_screenshot(raw_png)  # ← compressed JPEG

                cur.execute(f"""
                    INSERT INTO `{TARGET_TABLE}`
                        (symbol, timeframe, real_change, real_close, screenshot, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    symbol,
                    "day",
                    stock["real_change"],
                    stock["real_close"],
                    img_data,
                    datetime.utcnow(),
                ))
                db_conn.commit()

                print("✅")
                success_count += 1

            except Exception as e:
                print(f"❌ Error on {symbol}: {e}")
                traceback.print_exc()   # full stack trace — no more [:50] truncation

        print(f"🏁 Done. Successful screenshots: {success_count}/{len(stocks)}")

    except Exception as e:
        print(f"🚨 CRITICAL ERROR: {e}")
        traceback.print_exc()

    finally:
        # Safe teardown — cur/db_conn/driver all initialised to None above
        if db_conn and db_conn.is_connected():
            try:
                db_conn.commit()
            except Exception:
                pass
            if cur:
                cur.close()
            db_conn.close()
            print("🔌 Database connection closed.")
        if driver:
            driver.quit()
            print("🌐 Browser closed.")


if __name__ == "__main__":
    main()
