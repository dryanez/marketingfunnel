import asyncio
import csv
import random
import re
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# Configuration
IG_EMAIL = "felipe@autodirecto.cl"
IG_PASSWORD = "Comoestas01@"

# Local V Region hashtags to mine for car posters.
TARGET_HASHTAGS = [
    "autosvina", "autosvalparaiso", "ventadeautosvalparaiso", "ventadeautos5tareg",
    "usadosvina", "conconauto", "quilpueautos", "autos5tareg", "clubvregion", "vregionunida"
]

LOCAL_KEYWORDS = [
    "viña", "valparaiso", "valpo", "concon", "quilpue", "villa alemana", 
    "reñaca", "curauma", "peñablanca", "limache", "quillota", "v region", 
    "5ta region", "quinta region", "chile"
]

MAX_LEADS_PER_TAG = 20
OUTPUT_FILE = Path(__file__).parent / "instagram_warm_leads.csv"
SESSION_DIR = Path(__file__).parent / "ig_session"

def is_valid_handle(handle):
    if not handle: return False
    handle = handle.lower().strip('@').strip(':').strip('/')
    if not 3 <= len(handle) <= 30: return False
    if handle in ("p", "reel", "explore", "accounts", "liked_by", "tags", "comments", "locations", "stories"):
        return False
    if handle.replace('.', '').replace('_', '').isdigit():
        return False
    if not re.match(r'^[a-z0-9._]+$', handle):
        return False
    return handle

async def human_delay(min_sec=2, max_sec=5):
    await asyncio.sleep(random.uniform(min_sec, max_sec))

def save_lead(username, source):
    file_exists = OUTPUT_FILE.exists()
    existing_leads = set()
    if file_exists:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_leads.add(row["username"])
    
    if username in existing_leads:
        return

    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["username", "source", "date_scraped", "profile_link", "status"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "username": username,
            "source": source,
            "date_scraped": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "profile_link": f"https://www.instagram.com/{username}/",
            "status": "Pending"
        })
    print(f"  ✅ Saved verified lead: @{username}")

async def login_instagram(page):
    print("🔐 Checking Instagram login state...")
    try:
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)
    except:
        pass
    
    await human_delay(3, 5)
    
    # Check for proof of login
    is_logged_in = await page.query_selector('svg[aria-label="Direct"]') or \
                   await page.query_selector('svg[aria-label="Home"]')
    
    if is_logged_in:
        print("✅ Already logged in!")
        return True

    print("🔑 Not logged in. Starting login flow...")
    await page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded", timeout=60000)
    await human_delay(3, 5)

    # Handle cookie banners
    try:
        cookie_btn = await page.query_selector('button:has-text("Decline optional cookies"), button:has-text("Allow all cookies")')
        if cookie_btn:
            await cookie_btn.click()
            await human_delay(1, 2)
    except: pass

    # Check for "saved profile" screen
    try:
        use_another = await page.query_selector('button:has-text("Use another profile"), a:has-text("Log in with another account")')
        if use_another:
            await use_another.click()
            await human_delay(2, 4)
    except: pass

    # Fill credentials
    try:
        await page.wait_for_selector('input[name="username"], input[type="text"]', timeout=30000)
        await page.fill('input[name="username"], input[type="text"]', IG_EMAIL)
        await human_delay(1, 2)
        await page.fill('input[name="password"]', IG_PASSWORD)
        await human_delay(1, 2)
        await page.press('input[name="password"]', 'Enter')
    except Exception as e:
        print(f"⚠️ Could not find login fields: {e}")
        return False
    
    print("⏳ Waiting for login success or 2FA...")
    # Wait for typical home/feed markers
    try:
        await page.wait_for_selector('svg[aria-label="Home"], a[href="/direct/inbox/"]', timeout=60000)
        print("✅ Login successful!")
        return True
    except:
        print("⚠️ Login timeout. Check if 2FA/Security Code is required in the browser window.")
        return False

async def scrape_hashtag(page, hashtag, limit=20):
    print(f"\n🏷️  Scraping #{hashtag}...")
    try:
        await page.goto(f"https://www.instagram.com/explore/tags/{hashtag}/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"  ⚠️ Error loading tag: {e}")
        return

    await human_delay(4, 7)

    # Find the post links
    post_links = await page.eval_on_selector_all(
        'a[href*="/p/"], a[href*="/reel/"]',
        "els => [...new Set(els.map(el => el.href))].slice(0, 30)"
    )

    if not post_links:
        print(f"  ⚠️ Could not find posts for #{hashtag}")
        return

    print(f"  Found {len(post_links)} posts. Verifying and filtering local posters...")
    seen = set()
    count = 0

    for post_url in post_links:
        if count >= limit: break
        try:
            await page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            await human_delay(2, 4)
            await page.wait_for_selector('article header', timeout=5000)

            username = None
            # Source 1: Meta description (usually the first word)
            try:
                meta = await page.get_attribute('meta[property="og:description"]', 'content')
                if meta:
                    username = is_valid_handle(meta.split(' ')[0])
            except: pass

            # Source 2: Article Header links
            if not username:
                links = await page.eval_on_selector_all('article header a', "els => els.map(el => el.getAttribute('href'))")
                for l in links:
                    parts = [p for p in l.split('/') if p]
                    if parts:
                        username = is_valid_handle(parts[0])
                        if username: break
            
            if not username:
                continue

            # Check if username is local (fast check)
            if any(kw in username.lower() for kw in LOCAL_KEYWORDS):
                save_lead(username, f"#{hashtag} Post (Username Match)")
                count += 1
                continue

            # Navigation to profile for deep Bio check
            await page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=30000)
            await human_delay(3, 5)
            
            bio_text = ""
            try:
                bio_el = await page.query_selector('header section div:nth-child(3)') or await page.query_selector('header section h1 + div')
                if bio_el:
                    bio_text = (await bio_el.text_content()).lower()
            except: pass
            
            is_local = any(kw in bio_text for kw in LOCAL_KEYWORDS)
            
            if is_local:
                if username not in seen:
                    seen.add(username)
                    save_lead(username, f"#{hashtag} Post (Verified Bio)")
                    count += 1
            else:
                # Optional: quiet skip log
                pass

        except Exception as e:
            continue

async def main():
    print("🚀 Instagram Refined Lead Scraper - V Region Priority")
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            viewport={'width': 1280, 'height': 800},
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = browser.pages[0]
        
        if await login_instagram(page):
            for tag in TARGET_HASHTAGS:
                await scrape_hashtag(page, tag, limit=MAX_LEADS_PER_TAG)
                await human_delay(15, 25)
        else:
            print("🛑 Login failed. Closing for security.")
        
        print("\n✨ Scrape finished!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
