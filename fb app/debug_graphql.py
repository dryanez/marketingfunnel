"""
Facebook Marketplace - Quick Debug Script (v2)
Opens FB, waits for you to be logged in (checks URL), then goes to marketplace
and dumps the first 5 GraphQL responses to JSON files so we can inspect the structure.
Run this, log in manually if needed, let it capture, then check the .json files.
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path(__file__).parent

TARGET_URL = (
    "https://www.facebook.com/marketplace/106647439372422/search/"
    "?minPrice=8000000&query=Vehicles&exact=false&radius=20"
)

graphql_count = 0
all_urls = []

async def handle_response(response):
    global graphql_count
    url = response.url
    all_urls.append(url)

    if "graphql" not in url:
        return

    graphql_count += 1
    try:
        text = await response.text()
        try:
            data = json.loads(text)
        except Exception:
            data = {"raw_text": text[:2000]}

        out_file = OUTPUT_DIR / f"graphql_response_{graphql_count:02d}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        top_keys = list(data.keys())[:8] if isinstance(data, dict) else f"list len={len(data)}"
        print(f"  📡 #{graphql_count:>2}: {out_file.name}  keys={top_keys}")

        if graphql_count >= 8:
            print("  ℹ️  Captured 8 responses — stopping early.")
    except Exception as e:
        print(f"  ⚠️  Could not parse response {graphql_count}: {e}")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.on("response", handle_response)

        # Go to Facebook home
        await page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)

        current_url = page.url
        print(f"📍 Current URL: {current_url}")

        if "login" in current_url or "checkpoint" in current_url:
            print("⚠️  You need to log in. Please log in in the browser window.")
            print("   Waiting up to 90 seconds…")
            # Wait until URL changes away from login
            for _ in range(90):
                await asyncio.sleep(1)
                u = page.url
                if "login" not in u and "checkpoint" not in u:
                    print(f"✅ Logged in! URL: {u}")
                    break
            else:
                print("⚠️  Still not logged in, continuing anyway.")
        else:
            print("✅ Already logged in!")

        print(f"\n🌐 Navigating to marketplace…")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(4)

        print(f"\n🔄 Scrolling 8 times and listening for GraphQL…\n")
        for i in range(8):
            await page.evaluate("window.scrollBy(0, 1200)")
            print(f"  Scroll {i+1}/8 — GraphQL captured: {graphql_count}")
            await asyncio.sleep(2.5)
            if graphql_count >= 8:
                break

        await asyncio.sleep(3)

        print(f"\n✅ Done. Captured {graphql_count} GraphQL responses.")
        print(f"   Unique URLs seen: {len(set(all_urls))}")
        print(f"\n📁 Check {OUTPUT_DIR} for graphql_response_*.json files\n")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
