"""
Instagram Safe Follower Bot
Reads `instagram_warm_leads.csv` and slowly follows a limited number of users per day.
Implements extreme human emulation to avoid action blocks.
"""

import asyncio
import csv
import random
import os
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ─── Config ─────────────────────────────────────────────────────────────────────
# Using the new account provided by the user
IG_EMAIL    = "felipe@autodirecto.cl"
IG_PASSWORD = "Comoestas01@"

# EXTREMELY IMPORTANT BAN PREVENTION RULES
MAX_FOLLOWS_PER_RUN = 50       # Increased to 50 per user request
DELAY_BETWEEN_FOLLOWS = (30, 90) # Wait randomly between 30 and 90 seconds after following

INPUT_CSV = Path(__file__).parent / "instagram_warm_leads.csv"
SESSION_DIR = Path(__file__).parent / "ig_follower_session" # Separate session for this account

# ─── Helpers ────────────────────────────────────────────────────────────────────
async def human_delay(min_sec=2, max_sec=6):
    delay = random.uniform(min_sec, max_sec)
    print(f"    [sleeping {delay:.1f}s...]")
    await asyncio.sleep(delay)

def read_leads():
    if not INPUT_CSV.exists():
        print("❌ Leader CSV not found. Please run the scraper first.")
        return []
        
    leads = []
    with open(INPUT_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append(row)
    return leads

def save_leads(leads):
    """Rewrite the CSV with updated status columns."""
    if not leads:
        return
        
    # Ensure 'status' column exists in all rows
    fieldnames = list(leads[0].keys())
    if 'status' not in fieldnames:
        fieldnames.append('status')
        for lead in leads:
            if 'status' not in lead:
                lead['status'] = 'Pending'
                
    with open(INPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(leads)


# ─── Login ──────────────────────────────────────────────────────────────────────
async def login_instagram(page):
    print(f"🔐 Checking Instagram login state for {IG_EMAIL}...")
    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
    await human_delay(4, 7)

    is_logged_in = await page.query_selector('svg[aria-label="Home"]') or await page.query_selector('a[href="/direct/inbox/"]')
    
    if is_logged_in:
        print(f"✅ Already logged in! (URL: {page.url})")
        return True

    print("🔑 Session expired. Logging in...")
    await page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
    await human_delay(3, 5)

    try:
        await page.wait_for_selector('input[type="text"]', timeout=30000)
        await page.fill('input[type="text"]', IG_EMAIL)
        await human_delay(1, 2)
        await page.fill('input[type="password"]', IG_PASSWORD)
        await human_delay(1, 2)
        await page.press('input[type="password"]', 'Enter')
        print("⏳ Waiting for login payload resolving...")
        await human_delay(8, 12)

        # Check for error message (wrong password)
        error_msg = await page.query_selector('#slfErrorAlert')
        if error_msg:
            print("⚠️ Wrong password. Trying alternative...")
            # If wrong password, wait for manual input
            
        print("🔍 Checking if Instagram requires a 2FA code or security challenge...")
        
        # Wait until we see the home icon or direct message icon (meaning we are truly in)
        # Or wait until the user presses enter in the terminal
        logged_in = False
        wait_loops = 0
        
        while wait_loops < 30: # Wait up to 5 minutes (30 * 10s)
            is_in = await page.query_selector('svg[aria-label="Home"]') or await page.query_selector('a[href="/direct/inbox/"]') or await page.query_selector('[aria-label="New post"]')
            if is_in:
                logged_in = True
                break
                
            if "accounts/login" in page.url:
                print("⏳ Still on login page. Waiting...")
            else:
                print("🚨 INSTAGRAM IS ASKING FOR A CODE (2FA) OR SECURITY CHECK!")
                print("👉 Please look at the browser window and enter the code manually.")
                
            await human_delay(8, 12)
            wait_loops += 1

        if logged_in:
            print("✅ Login fully successful!")
            return True
        else:
            print("🛑 Timed out waiting for login/2FA to complete.")
            return False
            
    except Exception as e:
        print(f"❌ Login error: {e}")
        return False


# ─── Following System ───────────────────────────────────────────────────────────
async def process_follows(page, leads):
    follows_today = 0
    updated_leads = leads.copy()
    
    # Check if 'status' key exists in the first lead, if not, add it to all
    if updated_leads and 'status' not in updated_leads[0]:
        for l in updated_leads:
            l['status'] = 'Pending'
            
    # Save the updated schema immediately
    save_leads(updated_leads)

    for i, lead in enumerate(updated_leads):
        if follows_today >= MAX_FOLLOWS_PER_RUN:
            print(f"🛑 Reached the strict safety limit of {MAX_FOLLOWS_PER_RUN} follows.")
            print("Please run this script again tomorrow or later today to avoid bans.")
            break
            
        status = lead.get('status', 'Pending')
        
        # Skip if already interacted
        if status in ['Followed', 'Requested', 'Skipped', 'Error']:
            continue
            
        username = lead['username']
        profile_url = lead['profile_link']
        
        print(f"\n👤 [{follows_today+1}/{MAX_FOLLOWS_PER_RUN}] Visiting @{username}...")
        
        try:
            await page.goto(profile_url, wait_until="domcontentloaded")
            await human_delay(4, 7)
            
            # Check if page is missing or blocked
            content = await page.content()
            if "Esta página no está disponible" in content or "Page Not Found" in await page.title():
                print("  ⚠️ Account not found or deleted. Skipping.")
                updated_leads[i]['status'] = 'Skipped'
                save_leads(updated_leads)
                continue

            # Look for the primary profile action button (Follow, Following, Requested)
            # Instagram often uses a generic header layout, we look for buttons inside it
            action_button = None
            
            # Find all buttons in the profile header
            buttons = await page.query_selector_all('button, [role="button"], a[role="button"]')
            
            button_text = ""
            for btn in buttons:
                text = await btn.text_content()
                if text:
                    text = text.lower().strip()
                    # Check in Spanish and English
                    if any(w in text for w in ['seguir', 'follow', 'siguiendo', 'following', 'pendiente', 'requested']):
                        action_button = btn
                        button_text = text
                        break
                        
            if not action_button:
                print("  ⚠️ Could not find Follow button. Might be restricted.")
                await page.screenshot(path=f"debug_no_button_{username}.png")
                updated_leads[i]['status'] = 'Error'
                save_leads(updated_leads)
                continue
                
            if 'siguiendo' in button_text or 'following' in button_text:
                print("  ✅ Already following this user.")
                updated_leads[i]['status'] = 'Followed'
                save_leads(updated_leads)
                continue
                
            if 'pendiente' in button_text or 'requested' in button_text:
                print("  ✅ Already requested to follow (private account).")
                updated_leads[i]['status'] = 'Requested'
                save_leads(updated_leads)
                continue
                
            if 'seguir' in button_text or 'follow' in button_text or 'seguir también' in button_text or 'follow back' in button_text:
                print(f"  👆 Clicking Follow on @{username}...")
                await action_button.click()
                follows_today += 1
                
                # Check outcome after a moment
                await human_delay(3, 5)
                # Re-check the button text to see if it changed to 'Requested' or 'Following'
                try:
                    new_text = (await action_button.text_content()).lower().strip()
                except:
                    new_text = ""
                    
                if 'pendiente' in new_text or 'requested' in new_text:
                    updated_leads[i]['status'] = 'Requested'
                    print("  🔒 Account is private. Follow requested.")
                else:
                    updated_leads[i]['status'] = 'Followed'
                    print("  ✅ Successfully followed.")
                    
                save_leads(updated_leads)
                
                # Critical step: extremely long human delay to avoid action blocks
                wait_time = random.randint(DELAY_BETWEEN_FOLLOWS[0], DELAY_BETWEEN_FOLLOWS[1])
                print(f"  ⏳ Safety measure: Resting for {wait_time} seconds before the next action...")
                await asyncio.sleep(wait_time)
                
        except Exception as e:
            print(f"  ❌ Error interacting with @{username}: {e}")
            updated_leads[i]['status'] = 'Error'
            save_leads(updated_leads)
            await human_delay(5, 10)

# ─── Main ───────────────────────────────────────────────────────────────────────
async def main():
    print("🚀 Instagram Safe Auto-Follower Bot")
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    
    leads = read_leads()
    if not leads:
        return
        
    pending_count = sum(1 for l in leads if l.get('status', 'Pending') == 'Pending')
    print(f"📊 Total leads in CSV: {len(leads)}")
    print(f"⏳ Leads waiting to be followed: {pending_count}")
    
    if pending_count == 0:
        print("🎉 No more pending leads to follow!")
        return

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            channel="chromium",
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = context.pages[0] if context.pages else await context.new_page()

        success = await login_instagram(page)
        if not success:
            print("🛑 Login failed. Exiting.")
            await context.close()
            return

        await process_follows(page, leads)
                    
        print("\n🏁 Follow session finished!")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
