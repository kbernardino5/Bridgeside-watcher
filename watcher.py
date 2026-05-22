import json
import os
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
import requests

BASE = "https://livebridgeside.com"
MAIN_URL = f"{BASE}/floorplans/"
STATE_FILE = Path("state.json")
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]

def fetch_page(page, url):
    last_error = None
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(8000)
            return page.content()
        except Exception as e:
            last_error = e
            print(f"  Attempt {attempt + 1} failed for {url}: {e}", file=sys.stderr)
            page.wait_for_timeout(5000)  # wait 5s before retry
    raise last_error

def discover_floorplan_urls(text):
    """Find all individual floorplan page URLs from the main listing."""
    urls = set()
    for m in re.finditer(r'href="([^"]*?/floorplans/the-[a-z0-9-]+/?)"', text, re.I):
        url = m.group(1)
        if url.startswith("/"):
            url = BASE + url
        urls.add(url.rstrip("/") + "/")
    return sorted(urls)

def parse_floorplan(text, url):
    """Extract floorplan details from a single floorplan page."""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean)

    slug = url.rstrip("/").split("/")[-1]
    name = " ".join(w.capitalize() for w in slug.split("-"))

    # Bed/bath/sqft: "Studio 1 bath 601 sq. ft." or "1 bed 1 bath 833 sq. ft." or "2 bed 2 bath 1265 sq. ft."
    bbs = re.search(
        r"(Studio|(\d)\s*bed)\s*(\d)\s*bath\s*([\d,]+)\s*sq\.?\s*ft",
        clean, re.I,
    )
    if bbs:
        bed_label = "Studio" if bbs.group(1).lower().startswith("studio") else f"{bbs.group(2)}BR"
        bath = bbs.group(3)
        sqft = bbs.group(4)
    else:
        bed_label, bath, sqft = "?", "?", "?"

    # Availability signals
    avail_count = re.search(r"Only\s+(\d+)\s+left", clean, re.I)
    rent = re.search(r"\$([\d,]+)\s*/mo", clean)
    base_rent = re.search(r"\$([\d,]+)\s+Base\s+Rent", clean, re.I)

    if avail_count or rent:
        available = True
        count = avail_count.group(1) if avail_count else "?"
        price = f"${rent.group(1)}/mo" if rent else "N/A"
    else:
        available = False
        count = "0"
        price = "N/A"

    return {
        "slug": slug,
        "name": name,
        "type": bed_label,
        "bath": bath,
        "sqft": sqft,
        "available": available,
        "count": count,
        "price": price,
        "url": url,
    }

def scrape_all():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        main_html = fetch_page(page, MAIN_URL)
        urls = discover_floorplan_urls(main_html)
        print(f"Discovered {len(urls)} floorplan pages", file=sys.stderr)

        floorplans = []
        for url in urls:
            html = fetch_page(page, url)
            fp = parse_floorplan(html, url)
            floorplans.append(fp)
            print(
                f"  {fp['name']} ({fp['type']}): "
                f"available={fp['available']} count={fp['count']} price={fp['price']}",
                file=sys.stderr,
            )

        browser.close()
        return floorplans

def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if "floorplans" in data:
                return data
        except Exception:
            pass
    return {"floorplans": {}}

def save_state(floorplans):
    state = {"floorplans": {fp["slug"]: fp for fp in floorplans}}
    STATE_FILE.write_text(json.dumps(state, indent=2))

def diff(current, prev):
    changes = []
    prev_fps = prev.get("floorplans", {})
    for fp in current:
        slug = fp["slug"]
        prev_fp = prev_fps.get(slug)
        if prev_fp is None and fp["available"]:
            changes.append(("new_available", fp, None))
        elif prev_fp and fp["available"] and not prev_fp.get("available"):
            changes.append(("became_available", fp, prev_fp))
        elif prev_fp and not fp["available"] and prev_fp.get("available"):
            changes.append(("became_unavailable", fp, prev_fp))
        elif (
            prev_fp
            and fp["available"]
            and prev_fp.get("available")
            and fp.get("count") != prev_fp.get("count")
        ):
            changes.append(("count_changed", fp, prev_fp))
    return changes

def notify(lines):
    requests.post(
        WEBHOOK, json={"content": "\n".join(lines)}, timeout=30
    ).raise_for_status()

def main():
    current = scrape_all()
    prev = load_state()

    # First run: send a snapshot of currently-available floorplans
    if not prev.get("floorplans"):
        avail = [fp for fp in current if fp["available"]]
        lines = ["🏠 **Bridgeside watcher initialized**\n"]
        if avail:
            lines.append("Currently available:")
            for fp in avail:
                lines.append(
                    f"• **{fp['name']}** — {fp['type']} · {fp['sqft']} sqft · "
                    f"{fp['price']} · {fp['count']} left"
                )
        else:
            lines.append("No floorplans currently available. Watching for changes.")
        lines.append(f"\n{MAIN_URL}")
        lines.append("Call leasing: (843) 887-1428")
        notify(lines)
        save_state(current)
        return

    changes = diff(current, prev)
    if not changes:
        print("No changes", file=sys.stderr)
        save_state(current)
        return

    lines = ["🏠 **Bridgeside update!**\n"]
    for kind, fp, prev_fp in changes:
        if kind in ("new_available", "became_available"):
            lines.append(
                f"✅ **{fp['name']}** is AVAILABLE — {fp['type']} · {fp['sqft']} sqft · "
                f"{fp['price']} · {fp['count']} left"
            )
        elif kind == "became_unavailable":
            lines.append(f"❌ **{fp['name']}** ({fp['type']}) is no longer available")
        elif kind == "count_changed":
            lines.append(
                f"📊 **{fp['name']}** ({fp['type']}): now {fp['count']} left "
                f"(was {prev_fp.get('count', '?')}) · {fp['price']}"
            )
    lines.append(f"\n{MAIN_URL}")
    lines.append("Call leasing: (843) 887-1428")
    notify(lines)
    print(f"Sent alert for {len(changes)} changes", file=sys.stderr)
    save_state(current)

if __name__ == "__main__":
    main()

