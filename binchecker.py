import datetime as dt
import os
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
import truststore
from bs4 import BeautifulSoup

truststore.inject_into_ssl()

BASE = "https://bincollection.northumberland.gov.uk"

# ========= USER SETTINGS =========
POSTCODE = "NE18 0QP"

# Must match (fully or partially) one of the dropdown options on the address page
ADDRESS_LABEL_MATCH = "The Bastle"

# Which bins do you want reminders for?
# (from schedule page cards: "General", "Garden", "Recycling")
WATCH_FOR = {"General"}  # e.g. {"General", "Recycling"}
# =================================

# Telegram (set these as environment variables in PyCharm / GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def save_html(filename: str, html: str) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Saved {filename}")


def get_csrf(soup: BeautifulSoup) -> str:
    token = soup.select_one('input[name="_csrf"]')
    if not token or not token.get("value"):
        raise RuntimeError("Couldn't find _csrf token on page")
    return token["value"]


def select_address_option(soup: BeautifulSoup, label_match: str) -> tuple[str, str]:
    """Returns (address_value, address_label) from <select name='address'> matching label_match."""
    sel = soup.select_one('select[name="address"]')
    if not sel:
        raise RuntimeError("Couldn't find <select name='address'> on address selection page")

    options: list[tuple[str, str]] = []
    for opt in sel.select("option"):
        value = (opt.get("value") or "").strip()
        label = opt.get_text(" ", strip=True)
        if value:
            options.append((label, value))

    if not options:
        raise RuntimeError("No address options found in dropdown")

    needle = label_match.strip().lower()
    for label, value in options:
        if needle and needle in label.lower():
            return value, label

    print("\nCouldn't match ADDRESS_LABEL_MATCH. Available options:")
    for i, (label, value) in enumerate(options, start=1):
        print(f"{i:>2}. {label}  (value={value})")

    raise RuntimeError("Set ADDRESS_LABEL_MATCH to match one of the option labels above.")


def extract_next_collections(schedule_html: str) -> list[dict]:
    """
    Extracts the 'next collection' cards at the top of the schedule page:
      <div class="ncc-bin-calendar ...">
        <p>General</p>
        <p>Wednesday</p>
        <p>25 February 2026</p>
      </div>
    """
    soup = BeautifulSoup(schedule_html, "lxml")
    results: list[dict] = []

    for card in soup.select("div.ncc-bin-calendar"):
        ps = [p.get_text(strip=True) for p in card.find_all("p")]
        if len(ps) < 3:
            continue

        bin_type = ps[0]
        day = ps[1]
        date_text = ps[2]

        try:
            date_val = dt.datetime.strptime(date_text, "%d %B %Y").date()
        except ValueError:
            continue

        results.append({"type": bin_type, "day": day, "date": date_val, "raw": date_text})

    return results


def is_7pm_uk_now() -> bool:
    """True iff current time in Europe/London is exactly 19:00."""
    now_uk = dt.datetime.now(ZoneInfo("Europe/London"))
    return now_uk.hour == 19 and now_uk.minute == 0


def send_telegram(message: str) -> None:
    """Send a Telegram message using bot token + chat id from environment variables."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID environment variables")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def build_reminder_message(due_bins: list[dict]) -> str:
    """Version A (single bin) / Version B (multiple bins) formatting."""
    if not due_bins:
        return ""

    if len(due_bins) == 1:
        b = due_bins[0]
        return (
            "🗑️ BIN DAY TOMORROW\n\n"
            f"{b['type']} bin\n"
            f"📅 {b['day']} {b['raw']}\n\n"
            "Put it out tonight 👌"
        )

    lines = ["🗑️ BIN DAY TOMORROW", ""]
    for b in due_bins:
        try:
            short = b["date"].strftime("%a %d %b")  # e.g., Wed 25 Feb
        except Exception:
            short = f"{b['day']} {b['raw']}"
        lines.append(f"{b['type']} bin — {short}")

    lines.extend(["", "Put them out tonight 👌"])
    return "\n".join(lines)


def main():
    s = requests.Session()

    # When running on GitHub Actions, only run at 7pm UK time (BST/GMT safe).
    if os.getenv("GITHUB_ACTIONS") == "true" and not is_7pm_uk_now():
        print("Not 7pm UK time — exiting (GitHub Actions run).")
        return

    # Cookie observed in HTML (anti-bot-ish)
    s.cookies.set("x-bni-ja", "1707374704", domain="bincollection.northumberland.gov.uk", path="/")

    # 1) Load postcode page for CSRF
    r0 = s.get(f"{BASE}/", timeout=30)
    r0.raise_for_status()
    soup0 = BeautifulSoup(r0.text, "lxml")
    csrf0 = get_csrf(soup0)

    # 2) Submit postcode -> address select page
    r1 = s.post(
        f"{BASE}/postcode",
        data={"_csrf": csrf0, "postcode": POSTCODE},
        timeout=30,
        allow_redirects=True,
    )
    r1.raise_for_status()
    save_html("address_page.html", r1.text)

    soup1 = BeautifulSoup(r1.text, "lxml")
    csrf1 = get_csrf(soup1)

    form = soup1.find("form")
    if not form or not form.get("action"):
        raise RuntimeError("Couldn't find form/action on address selection page")
    submit_url = urljoin(BASE, form.get("action"))

    address_value, address_label = select_address_option(soup1, ADDRESS_LABEL_MATCH)
    print(f"\nSelected address: {address_label} (value={address_value})")
    print(f"Submitting to: {submit_url}")

    # 3) Submit address -> schedule page
    r2 = s.post(
        submit_url,
        data={"_csrf": csrf1, "address": address_value},
        timeout=30,
        allow_redirects=True,
    )
    r2.raise_for_status()
    save_html("schedule_page.html", r2.text)

    # 4) Extract next collections
    collections = extract_next_collections(r2.text)
    if not collections:
        print("\nCouldn't find next-collection cards in the schedule page.")
        print("Open schedule_page.html to inspect the layout.")
        return

    tomorrow = dt.date.today() + dt.timedelta(days=1)

    print("\nNext collections:")
    for c in collections:
        flag = " (TOMORROW)" if c["date"] == tomorrow else ""
        print(f"- {c['type']} → {c['raw']} ({c['day']}){flag}")

    watched = [c for c in collections if c["type"] in WATCH_FOR]
    due_tomorrow = [c for c in watched if c["date"] == tomorrow]

    if due_tomorrow:
        print("\n✅ REMINDER:")
        for c in due_tomorrow:
            print(f"Put out: {c['type']} bin (collection {c['day']} {c['raw']})")

        message = build_reminder_message(due_tomorrow)
        send_telegram(message)
    else:
        print("\nNo watched bins due tomorrow.")


if __name__ == "__main__":
    main()