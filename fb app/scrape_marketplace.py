"""
Facebook Marketplace - GraphQL Scraper
Uses Playwright to log in directly, intercept /api/graphql/ responses,
and parse the vehicle listings.
"""

import asyncio
import csv
import json
import shutil
import tempfile
import sys
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ─── Config ─────────────────────────────────────────────────────────────────────
FB_EMAIL    = "felipe@autodirecto.cl"
FB_PASSWORD = "Comoestas01@"

# Facebook Marketplace V Region minimum 4M CLP (Exact match)
TARGET_URL = (
    "https://www.facebook.com/marketplace/106647439372422/search/"
    "?minPrice=4000000&query=Vehicles&category_id=546583916084032"
    "&exact=false&referral_ui_component=category_menu_item"
)
OUTPUT_FILE  = Path(__file__).parent / "facebook_graphql_vehicles.csv"
TARGET_TOP_TIER_COUNT = 500  # We want 500 V Region leads with score >= 80
MAX_SCROLLS = 2000           # Failsafe so it won't run forever if the market runs out
SCROLL_PX    = 1200
SCROLL_DELAY = 2.5
CHROME_USER_DATA = Path.home() / "Library/Application Support/Google/Chrome"

# ─── Dashboard Imports ─────────────────────────────────────────────────────────
# Dynamically import the exact scoring and region checking logic from the dashboard
dashboard_dir = Path(__file__).parent.parent / "Funnels" / "dashboard"
sys.path.append(str(dashboard_dir))
from utils import get_region_data, calculate_liquidity_score

# ─── Global store ───────────────────────────────────────────────────────────────
vehicles: dict[str, dict] = {}
graphql_count = 0
top_tier_count = 0  # Counter for our target goal

# ─── Parser ─────────────────────────────────────────────────────────────────────
def parse_feed_units(data: dict):
    try:
        edges = data["data"]["marketplace_search"]["feed_units"]["edges"]
    except (KeyError, TypeError):
        return

    for edge in edges:
        try:
            listing = edge["node"]["listing"]
            lid = str(listing.get("id", ""))
            title = listing.get("marketplace_listing_title") or listing.get("custom_title", "")
            price = (listing.get("listing_price") or {}).get("formatted_amount", "")
            city = (
                (listing.get("location") or {})
                .get("reverse_geocode", {})
                .get("city", "")
            )
            km_list = listing.get("custom_sub_titles_with_rendering_flags") or []
            km = km_list[0].get("subtitle", "") if km_list else ""
            seller = (listing.get("marketplace_listing_seller") or {}).get("name", "")
            listing_url = f"https://www.facebook.com/marketplace/item/{lid}/"

            # Extract photo if available
            photo_url = ""
            primary_photo = listing.get("primary_listing_photo") or {}
            image_obj = primary_photo.get("image") or {}
            photo_url = image_obj.get("uri", "")

            if lid and title:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # We format a mock lead object exactly how dashboard expects it for scoring
                price_str = price.replace("CLP", "").replace("$", "").replace(".", "").replace(",", "")
                try: price_num = int(price_str)
                except ValueError: price_num = 0
                
                mock_lead = {
                    "title": title,
                    "price": str(price_num),
                    "km": km,
                    "mileage": km,
                    "city": city,
                    "location": city
                }
                
                # Dynamic dashboard scoring
                region_data = get_region_data(city)
                score = calculate_liquidity_score(mock_lead)
                
                is_top_tier = False
                # Must be V Region, price >= 4M, and score >= 80 to count toward the 500
                if region_data.get("is_v_region") and price_num >= 4000000 and score >= 80:
                    is_top_tier = True
                    global top_tier_count
                
                if lid not in vehicles:
                    vehicles[lid] = {
                        "id": lid,
                        "title": title,
                        "price": price,
                        "city": city,
                        "km": km,
                        "seller": seller,
                        "url": listing_url,
                        "photo_url": photo_url,
                        "first_seen": now_str,
                        "last_scraped": now_str,
                    }
                    if is_top_tier:
                        top_tier_count += 1
                        print(f"  ⭐ [GOLDEN {top_tier_count}/{TARGET_TOP_TIER_COUNT}] {title[:30]:<30} | {price:<13} | Score: {score} | {city}")
                    else:
                        print(f"  ✅ [NEW] {title[:45]:<45} | {price:<13} | {city} | Score: {score}")
                else:
                    vehicles[lid]["last_scraped"] = now_str
                    vehicles[lid]["price"] = price # update price if changed
                    if photo_url:
                        vehicles[lid]["photo_url"] = photo_url
                        
        except Exception as e:
            continue


# ─── Response handler ────────────────────────────────────────────────────────────
async def handle_response(response):
    global graphql_count
    if "/api/graphql" not in response.url:
        return
    graphql_count += 1
    try:
        text = await response.text()
        data = json.loads(text)
        parse_feed_units(data)
    except Exception:
        pass


async def login_facebook(page):
    print("🔐 Logging in to Facebook…")
    await page.goto("https://www.facebook.com", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    
    # Check if already logged in (look for profile icon or absence of login form)
    cookie_consent = await page.query_selector('button[title="Allow all cookies"]')
    if cookie_consent:
        await cookie_consent.click()
        await asyncio.sleep(1)
        
    email_input = await page.query_selector('#email')
    if email_input:
        print("  Filling credentials...")
        await page.fill('#email', FB_EMAIL)
        await asyncio.sleep(0.5)
        await page.fill('#pass', FB_PASSWORD)
        await asyncio.sleep(0.5)
        await page.click('[name="login"]')
        await asyncio.sleep(5)  # wait for redirect
        
        url = page.url
        if "checkpoint" in url:
            print("⚠️  Facebook checkpoint/2FA hit. Waiting 45s for manual intervention.")
            await asyncio.sleep(45)
        else:
            print("✅ Logged in!")
    else:
        print("✅ Already logged in (no login form found).")


# ─── Main ────────────────────────────────────────────────────────────────────────
async def main():
    if not CHROME_USER_DATA.exists():
        print("❌ Chrome user data directory not found!")
        return
        
    # Read existing vehicles to prevent duplication
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                vehicles[row["id"]] = row
                
                # Pre-calculate how many golden leads we already have saved
                price_str = row.get("price", "0").replace("CLP", "").replace("$", "").replace(".", "").replace(",", "")
                try: p_num = int(price_str)
                except ValueError: p_num = 0
                
                mock_lead = {
                    "title": row.get("title", ""), "price": str(p_num),
                    "km": row.get("km", ""), "mileage": row.get("km", "")
                }
                reg = get_region_data(row.get("city", ""))
                sc = calculate_liquidity_score(mock_lead)
                
                if reg.get("is_v_region") and p_num >= 4000000 and sc >= 80:
                    global top_tier_count
                    top_tier_count += 1
                    
        print(f"📂 Loaded {len(vehicles)} existing vehicles from CSV.")
        print(f"🏆 Found {top_tier_count} existing Golden Leads (V Region & >=80 score) already saved.")

    print("📂 Copying Chrome profile to temp directory…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="fb_chrome_"))
    default_src = CHROME_USER_DATA / "Default"
    default_dst = tmp_dir / "Default"
    shutil.copytree(default_src, default_dst, ignore=shutil.ignore_patterns(
        'Cache', 'Code Cache', 'GPUCache', 'Service Worker',
        'blob_storage', 'IndexedDB', 'File System',
        'GCM Store', 'BudgetDatabase', 'optimization_guide*',
        'heavy_ad*', 'AutofillStrikeDatabase',
        'databases', 'Platform Notifications', 'shared_proto_db',
    ), dirs_exist_ok=True)
    local_state = CHROME_USER_DATA / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, tmp_dir / "Local State")
    print(f"✅ Profile ready at: {tmp_dir}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(tmp_dir),
            headless=False,
            channel="chromium",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        page.on("response", handle_response)

        await login_facebook(page)

        print(f"\n🌐 Navigating to marketplace…")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(5)
        
        # Wait and handle marketplace login popup overlay if it appears
        print("Waiting a few seconds to see if login popup overlay appears...")
        await asyncio.sleep(4)
        try:
            # Facebook overlays are usually dialogs
            # Look for an input that is not hidden, usually email or text
            email_input = await page.wait_for_selector('div[role="dialog"] input:not([type="hidden"])', state="visible", timeout=3000)
            if email_input:
                print("⚠️ Detected login popup overlay on marketplace! Filling it...")
                await email_input.fill(FB_EMAIL)
                await asyncio.sleep(0.5)
                pass_input = await page.query_selector('div[role="dialog"] input[type="password"]')
                if pass_input:
                    await pass_input.fill(FB_PASSWORD)
                    await asyncio.sleep(0.5)
                    await pass_input.press("Enter")
                    print("✅ Submitted popup login. Waiting for page to reload/settle...")
                    await asyncio.sleep(8)
        except Exception:
            print("No login popup overlay detected.")

        print(f"\n🔄 Scrolling indefinitely until {TARGET_TOP_TIER_COUNT} Golden Leads are found...\n")
        scroll_count = 0
        
        while top_tier_count < TARGET_TOP_TIER_COUNT and scroll_count < MAX_SCROLLS:
            scroll_count += 1
            await page.evaluate(f"window.scrollBy(0, {SCROLL_PX})")
            print(f"  Scroll {scroll_count:>3} | {len(vehicles)} default vehicles | ⭐ {top_tier_count}/{TARGET_TOP_TIER_COUNT} Golden | {graphql_count} GraphQL hits")
            await asyncio.sleep(SCROLL_DELAY)

        if top_tier_count >= TARGET_TOP_TIER_COUNT:
            print(f"\n🎉 Target Reached! Scraped {TARGET_TOP_TIER_COUNT} high-liquidity V Region vehicles.")
        else:
            print(f"\n⚠️ Loop stopped early after hitting {MAX_SCROLLS} scrolls failsafe. Only found {top_tier_count} golden leads.")

        await asyncio.sleep(3)

        print(f"\n💾 Saving {len(vehicles)} vehicles → {OUTPUT_FILE}")
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            if vehicles:
                writer = csv.DictWriter(f, fieldnames=["id", "title", "price", "city", "km", "seller", "url", "photo_url", "first_seen", "last_scraped"])
                writer.writeheader()
                writer.writerows(vehicles.values())

        print(f"\n✅ Done! {len(vehicles)} vehicles → {OUTPUT_FILE}")
        print(f"   GraphQL responses intercepted: {graphql_count}")
        await context.close()

    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("🗑️  Temp profile cleaned up.")


if __name__ == "__main__":
    asyncio.run(main())
