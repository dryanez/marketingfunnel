from flask import Flask, render_template, jsonify, request, Response, stream_with_context
import pandas as pd
import json
import os
import sys
import subprocess
import glob
from pathlib import Path
import requests

from datetime import datetime

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TMP_DIR = BASE_DIR / ".tmp"
FB_APP_DIR = BASE_DIR.parent / "fb app"
LEADS_CSV = FB_APP_DIR / "facebook_graphql_vehicles.csv"
LEADS_JSON = TMP_DIR / "filtered_cars.json"
STATUS_FILE = TMP_DIR / "lead_status.json"

# In-memory cache — loaded once at startup
_cached_listings = []


def normalize_apify_item(item):
    """Convert a raw Apify Facebook Marketplace scraper record to dashboard format.
    Handles both the new camelCase format and the old snake_case format."""

    # ── Title ──────────────────────────────────────────────────────────────
    title = (
        item.get("listingTitle")
        or item.get("marketplace_listing_title")
        or item.get("customTitle")
        or item.get("custom_title")
        or "Unknown"
    )

    # ── Price ──────────────────────────────────────────────────────────────
    # New format: listingPrice.amount  (e.g. "11500000")
    # Old format: listing_price.amount
    price_info = item.get("listingPrice") or item.get("listing_price") or {}
    try:
        price_num = int(float(price_info.get("amount", 0)))
        price = f"CLP {price_num:,}" if price_num else "N/A"
    except Exception:
        price = str(price_info.get("formatted_amount") or price_info.get("amount") or "N/A")

    # ── Location ───────────────────────────────────────────────────────────
    # New format: locationText.text  (e.g. "Viña del Mar, VS")
    # Old format: location.reverse_geocode.city_page.display_name
    location = ""
    loc_text = item.get("locationText") or {}
    if loc_text.get("text"):
        location = loc_text["text"]
    else:
        loc = item.get("location") or {}
        rev = loc.get("reverse_geocode") or {}
        city_page = rev.get("city_page") or {}
        location = (
            city_page.get("display_name")
            or f"{rev.get('city', '')}, {rev.get('state', '')}".strip(", ")
            or "Unknown"
        )

    # ── Year (parse from title) ────────────────────────────────────────────
    year = None
    parts = title.split()
    if parts and parts[0].isdigit() and len(parts[0]) == 4:
        year = int(parts[0])

    # ── Mileage from subtitles ─────────────────────────────────────────────
    # New format: customSubTitlesWithRenderingFlags
    # Old format: custom_sub_titles_with_rendering_flags
    mileage = ""
    subtitles = (
        item.get("customSubTitlesWithRenderingFlags")
        or item.get("custom_sub_titles_with_rendering_flags")
        or []
    )
    for s in subtitles:
        sub = s.get("subtitle", "")
        if "km" in sub.lower():
            mileage = sub
            break

    # ── Photo ──────────────────────────────────────────────────────────────
    # New format: primaryListingPhoto.photo_image_url
    # Old format: primary_listing_photo.photo_image_url
    photo = item.get("primaryListingPhoto") or item.get("primary_listing_photo") or {}
    photo_url = photo.get("photo_image_url") or ""

    # Fallback: first photo in listingPhotos array
    if not photo_url:
        photos = item.get("listingPhotos") or item.get("listing_photos") or []
        if photos:
            photo_url = (photos[0].get("image") or {}).get("uri", "")

    # ── URL ────────────────────────────────────────────────────────────────
    url = (
        item.get("itemUrl")
        or item.get("listingUrl")
        or item.get("url")
        or ""
    )

    return {
        "id": item.get("id", ""),
        "url": url,
        "title": title,
        "price": price,
        "location": location,
        "year": year,
        "mileage": mileage,
        "photo_url": photo_url,
        "is_sold": item.get("isSold") or item.get("is_sold", False),
        "status": "new",
    }


def find_latest_apify_json():
    """Find the largest Apify dataset JSON in BASE_DIR or Downloads.
    We pick by size (largest = most complete dataset) rather than modification time."""
    # Search in the Funnels folder first
    pattern = str(BASE_DIR / "dataset_facebook-marketplace-scraper_*.json")
    files = glob.glob(pattern)

    # Also check Downloads as a fallback
    downloads = Path.home() / "Downloads"
    dl_pattern = str(downloads / "dataset_facebook-marketplace-scraper_*.json")
    files += glob.glob(dl_pattern)

    if not files:
        return None
    # Pick the largest file — it contains the most listings
    return max(files, key=os.path.getsize)


def load_all_listings():
    """Load and normalize listings from the best available source. Called once at startup."""

    # 1. Raw Apify dataset JSON — full dataset (highest priority for viewing)
    apify_file = find_latest_apify_json()
    if apify_file:
        try:
            raw = json.loads(Path(apify_file).read_text(encoding="utf-8"))
            # Filter out empty/partial records (only have facebookUrl, no actual listing)
            valid = [item for item in raw if item.get("id") or item.get("listingTitle")]
            listings = [normalize_apify_item(item) for item in valid]
            print(f"[data] Loaded {len(listings)} listings from Apify JSON: {Path(apify_file).name}")
            print(f"[data]   (skipped {len(raw) - len(valid)} empty records)")
            return listings
        except Exception as e:
            print(f"[data] Error loading Apify JSON: {e}")

    # 2. Filtered JSON
    if LEADS_JSON.exists():
        try:
            data = json.loads(LEADS_JSON.read_text())
            listings = data.get("listings", [])
            if listings:
                print(f"[data] Loaded {len(listings)} listings from filtered JSON")
                return listings
        except Exception as e:
            print(f"[data] Error loading filtered JSON: {e}")

    # 3. CSV fallback
    if LEADS_CSV.exists():
        try:
            df = pd.read_csv(LEADS_CSV)
            df = df.fillna("")
            rows = df.to_dict(orient="records")
            listings = [normalize_csv_row(r) for r in rows]
            print(f"[data] Loaded {len(listings)} listings from CSV")
            return listings
        except Exception as e:
            print(f"[data] Error loading CSV: {e}")

    print("[data] No data source found!")
    return []


from utils import calculate_liquidity_score, get_region_data

def normalize_csv_row(row):
    """Map CSV column names from the Playwright scraper to dashboard field names."""
    url = row.get("url", "")
    title = row.get("title", "Unknown")
    price_raw = row.get("price", "N/A")
    location = row.get("city", "Unknown")
    
    # Parse year from title
    year = None
    title_str = str(title) if title else ""
    parts = title_str.split()
    if parts and parts[0].isdigit() and len(parts[0]) == 4:
        year = int(parts[0])
    # Parse region and distance
    region_data = get_region_data(location)

    lead = {
        "id": url or row.get("id", ""),
        "url": url,
        "title": title,
        "price": str(price_raw),
        "location": str(location),
        "year": year,
        "mileage": str(row.get("km", "")),
        "photo_url": str(row.get("photo_url", "")),
        "is_sold": False,
        "status": "new",
        "first_seen": row.get("first_seen", ""),
        "last_scraped": row.get("last_scraped", ""),
        "seller": row.get("seller", "")
    }
    lead["score"] = calculate_liquidity_score(lead)
    lead.update(region_data)
    return lead

def get_leads():
    """Return listings merged with current status map."""
    status_map = {}
    if STATUS_FILE.exists():
        try:
            status_map = json.loads(STATUS_FILE.read_text())
        except Exception:
            pass

    results = []
    for item in _cached_listings:
        url = item.get("url") or item.get("id")
        if not url:
            continue
        item_copy = dict(item)
        
        # Handle both legacy string status and new dict status
        val = status_map.get(item.get("url", ""), "new")
        if isinstance(val, dict):
            item_copy["status"] = val.get("status", "new")
            item_copy["contacted_at"] = val.get("contacted_at")
            item_copy["valuation"] = val.get("valuation")
        else:
            item_copy["status"] = val # assume string
            item_copy["contacted_at"] = None
            item_copy["valuation"] = None
            
        results.append(item_copy)
        
    # Sort priority:
    # 1. V Region first (True > False)
    # 2. Closest to Viña del Mar first
    # 3. Highest Liquidity Score first
    results.sort(key=lambda x: (
        not x.get("is_v_region", False),  # False comes first in Python sorting, so we invert
        x.get("distance_to_vina", 9999), 
        -x.get("score", 0)                # Negative to sort descending
    ))
    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/leads", methods=["GET"])
def api_leads():
    return jsonify(get_leads())


@app.route("/api/reload", methods=["POST"])
def api_reload():
    """Reload listings from disk (e.g. after a new scrape)."""
    global _cached_listings
    _cached_listings = load_all_listings()
    return jsonify({"success": True, "count": len(_cached_listings)})


@app.route("/api/auto_message", methods=["POST", "GET"])
def trigger_auto_message():
    """Stream auto-messenger output via SSE."""
    limit = request.args.get("limit", 50, type=int)
    
    def generate():
        yield f"data: {json.dumps({'log': f'💬 Starting auto-messenger (limit: {limit} leads)...', 'pct': 0})}\\n\\n"
        
        messenger_script = Path(__file__).resolve().parent.parent.parent / "auto_messenger.py"
        cmd = [sys.executable, str(messenger_script), "--limit", str(limit)]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        proc = subprocess.Popen(
            cmd, cwd=str(messenger_script.parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, bufsize=1
        )
        
        sent = 0
        total = limit
        
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if "✅ Marked" in line or "Message sent" in line:
                sent += 1
            pct = min(95, int((sent / max(total, 1)) * 90))
            yield f"data: {json.dumps({'log': line, 'pct': pct, 'sent': sent})}\\n\\n"
        
        proc.wait()
        success = proc.returncode == 0
        done_msg = f"✅ Done! Sent {sent} message(s)." if success else "❌ Messenger exited with errors."
        yield f"data: {json.dumps({'log': done_msg, 'pct': 100, 'sent': sent, 'done': True, 'success': success})}\\n\\n"
        
        # Reload leads data
        global _cached_listings
        _cached_listings = load_all_listings()
        
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/scrape", methods=["POST", "GET"])
def trigger_scrape():
    """Stream scraper output via SSE, then send WhatsApp notification on completion."""
    WHATSAPP_NUMBER = "4917632407062"
    CALLMEBOT_API_KEY = "4106204"
    
    def _wa_encode(text):
        """Encode text for CallMeBot URL (official format)."""
        out = str(text)
        out = out.replace(' ', '%20')
        out = out.replace(':', '%3A')
        out = out.replace('/', '%2F')
        out = out.replace('\n', '%0A')
        return out
    
    def generate():
        yield f"data: {json.dumps({'log': '🚀 Starting Facebook Marketplace scraper...', 'pct': 0})}\n\n"
        
        cmd = [sys.executable, str(FB_APP_DIR / "scrape_marketplace.py")]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        proc = subprocess.Popen(
            cmd, cwd=FB_APP_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, bufsize=1
        )
        
        total_scrolls = 40  # matches scraper default
        scroll_count = 0
        vehicle_count = 0
        
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            
            # Parse scroll progress
            if "Scroll " in line and "/" in line:
                try:
                    part = line.split("Scroll ")[1].split(" — ")[0]
                    cur, tot = part.split("/")
                    scroll_count = int(cur)
                    total_scrolls = int(tot)
                    if "vehicles" in line:
                        vehicle_count = int(line.split("vehicles")[0].split("— ")[-1].strip())
                except Exception:
                    pass
            
            pct = min(95, int((scroll_count / max(total_scrolls, 1)) * 90))
            
            yield f"data: {json.dumps({'log': line, 'pct': pct, 'vehicles': vehicle_count})}\n\n"
        
        proc.wait()
        success = proc.returncode == 0
        
        # Reload cached listings
        global _cached_listings
        _cached_listings = load_all_listings()
        
        done_msg = f"✅ Scrape complete! {vehicle_count} vehicles saved." if success else "❌ Scraper exited with errors."
        yield f"data: {json.dumps({'log': done_msg, 'pct': 100, 'vehicles': vehicle_count, 'done': True, 'success': success})}\n\n"
        
        # WhatsApp notification
        try:
            msg = f"🚗 Autodirecto Scraper\n{done_msg} ({datetime.now().strftime('%H:%M')})"
            wa_url = (
                f"https://api.callmebot.com/whatsapp.php"
                f"?phone={WHATSAPP_NUMBER}"
                f"&text={_wa_encode(msg)}"
                f"&apikey={CALLMEBOT_API_KEY}"
            )
            resp = requests.get(wa_url, timeout=8)
            print(f"[whatsapp] Response: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            print(f"[whatsapp] Notification failed: {e}")
        
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/leads/status", methods=["POST"])
def api_update_status():
    data = request.json
    url = data.get("url")
    status = data.get("status")
    valuation = data.get("valuation")

    if not url:
        return jsonify({"error": "Missing url"}), 400

    status_map = {}
    if STATUS_FILE.exists():
        try:
            status_map = json.loads(STATUS_FILE.read_text())
        except Exception:
            pass

    # Get existing entry or create new
    entry = status_map.get(url, {})
    if not isinstance(entry, dict):
        entry = {"status": entry if entry else "new"}
    
    import time
    entry["updated_at"] = int(time.time())

    # Update status if provided
    if status:
        entry["status"] = status
        if status == "contacted":
            entry["contacted_at"] = int(time.time())
    
    # Update valuation if provided
    if valuation:
        entry["valuation"] = valuation

    status_map[url] = entry
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status_map, indent=2))
    return jsonify({"success": True, "status": entry.get("status"), "valuation": entry.get("valuation")})


@app.route("/api/valuation", methods=["POST"])
def api_valuation():
    """Proxy to MrcarCotizacion API to get real market valuation."""
    data = request.json
    make = data.get("make")
    model = data.get("model")
    year = data.get("year")
    mileage = data.get("mileage")

    if not all([make, model, year]):
        return jsonify({"error": "Missing make, model, or year"}), 400

    # Clean mileage (remove 'km', 'miles', etc)
    if mileage:
        mileage = str(mileage).lower().replace("km", "").replace("miles", "").replace(",", "").strip()
        # extract digits only if mixed
        import re
        digits = re.findall(r'\d+', mileage)
        if digits:
            mileage = digits[0]
        else:
            mileage = "0"

    print(f"[valuation] Requesting for {make} {model} {year} ({mileage} km)")

    try:
        # Call external API
        url = "https://mrcar-cotizacion.vercel.app/api/market-price"
        params = {
            "make": make,
            "model": model,
            "year": year,
            "mileage": mileage or "0"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp_data = resp.json()

        if not resp_data.get("success"):
            return jsonify({"error": "Valuation failed", "details": resp_data}), 400

        return jsonify(resp_data)

    except Exception as e:
        print(f"[valuation] Error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print(f"[startup] Loading listings...")
    _cached_listings = load_all_listings()
    print(f"[startup] Ready — {len(_cached_listings)} listings cached")
    print(f"[startup] Dashboard at http://localhost:5001")
    # use_reloader=False prevents double-startup in background mode
    app.run(debug=False, port=5001, use_reloader=False)
