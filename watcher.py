import json
import os
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
import requests

URL = "https://livebridgeside.com/floorplans/"
STATE_FILE = Path("state.json")
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]


def scrape_units():
  """Load the floorplans page and return a list of available units."""
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
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    # Give the JS floorplan widget a moment to render unit cards
    page.wait_for_timeout(15000)
    text = page.content()
    # DEBUG: print a chunk of the page so we can see the unit format
    import re as _re
    for keyword in ["bedroom", "bed", "available", "rent", "$"]:
      matches = list(_re.finditer(keyword, text, _re.IGNORECASE))
      print(f"DEBUG: '{keyword}' appears {len(matches)} times", file=sys.stderr)
    print("DEBUG: page length =", len(text), file=sys.stderr)
    # Dump 2000 chars around the first "$" so we can see pricing context
    dollar_idx = text.find("$")
    if dollar_idx > 0:
      print("DEBUG: context around first $:", file=sys.stderr)
      print(text[max(0, dollar_idx-500):dollar_idx+1500], file=sys.stderr)
    browser.close()

  units = []
  # Match unit blocks: looks for "Unit ####" followed by surrounding context.
  # Apartment sites typically render: Unit 0123 ... 2 Bed ... $2,450 ... Available Jun 1
  for m in re.finditer(
    r"(Unit\s*#?\s*\d{2,5})(.{0,400}?)(?=Unit\s*#?\s*\d{2,5}|$)",
    text,
    re.IGNORECASE | re.DOTALL,
  ):
    block = (m.group(1) + m.group(2)).strip()
    block_clean = re.sub(r"<[^>]+>", " ", block) # strip HTML tags
    block_clean = re.sub(r"\s+", " ", block_clean)

    unit_id = re.search(r"Unit\s*#?\s*(\d{2,5})", block_clean, re.I)
    beds = re.search(r"(Studio|(\d)\s*Bed)", block_clean, re.I)
    price = re.search(r"\$[\d,]+", block_clean)
    sqft = re.search(r"([\d,]+)\s*sq\.?\s*ft", block_clean, re.I)
    avail = re.search(r"Available[^.]{0,40}", block_clean, re.I)

    if not unit_id:
      continue

    if beds:
      bed_label = "Studio" if beds.group(1).lower().startswith("studio") else f"{beds.group(2)}BR"
    else:
      bed_label = "Unknown"

    units.append({
      "id": unit_id.group(1),
      "type": bed_label,
      "price": price.group(0) if price else "N/A",
      "sqft": sqft.group(1) if sqft else "N/A",
      "available": avail.group(0).strip() if avail else "N/A",
    })

  # Dedupe by unit id
  seen, deduped = set(), []
  for u in units:
    if u["id"] not in seen:
      seen.add(u["id"])
      deduped.append(u)
  return deduped


def load_state():
  if STATE_FILE.exists():
    return json.loads(STATE_FILE.read_text())
  return {"units": []}


def save_state(units):
  STATE_FILE.write_text(json.dumps({"units": units}, indent=2))


def notify(new_units, removed_units):
  lines = []
  if new_units:
    lines.append("🏠 **New apartments available at Bridgeside!**\n")
    for u in new_units:
      lines.append(
        f"• **Unit {u['id']}** — {u['type']} · {u['price']} · "
        f"{u['sqft']} sqft · {u['available']}"
      )
  if removed_units:
    lines.append("\n📭 No longer listed:")
    for u in removed_units:
      lines.append(f"• Unit {u['id']} ({u['type']})")
  lines.append("\nhttps://livebridgeside.com/floorplans/")
  lines.append("Call leasing: (843) 887-1428")

  requests.post(WEBHOOK, json={"content": "\n".join(lines)}, timeout=30).raise_for_status()


def main():
  current = scrape_units()
  print(f"Scraped {len(current)} units", file=sys.stderr)

  prev = load_state()
  prev_ids = {u["id"] for u in prev["units"]}
  curr_ids = {u["id"] for u in current}

  new_units = [u for u in current if u["id"] not in prev_ids]
  removed_units = [u for u in prev["units"] if u["id"] not in curr_ids]

  if new_units or removed_units:
    notify(new_units, removed_units)
    print(f"Notified: +{len(new_units)} new, -{len(removed_units)} removed", file=sys.stderr)
  else:
    print("No changes", file=sys.stderr)

  save_state(current)


if __name__ == "__main__":
  main()
  # DEBUG: send a heartbeat notification so we know if discord is reachable
  requests.post(
    WEBHOOK,
    json={"content": "🔧 Bridgeside watcher ran successfully (test ping)"},
    timeout=30
  )
