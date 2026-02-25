import datetime as dt
import os
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
import certifi
from bs4 import BeautifulSoup

BASE = "https://bincollection.northumberland.gov.uk"

# ========= USER SETTINGS =========
POSTCODE = "NE18 0QP"
ADDRESS_LABEL_MATCH = "The Bastle"
WATCH_FOR = {"General"}  # e.g. {"General", "Recycling"}
# =================================

# Telegram (set as environment variables in PyCharm / GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Optional: FORCE_TEST_MESSAGE=1 sends a test message regardless of bin day
FORCE_TEST_MESSAGE = os.getenv("FORCE_TEST_MESSAGE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


# ---------- GitHub timing logic ----------

def should_run_now_on_github_actions() -> bool:
    if os.getenv("GITHUB_ACTIONS") != "true":
        return True

    event = os.getenv("GITHUB_EVENT_NAME", "")
    if event != "schedule":
        return True  # allow manual runs anytime

    now_uk = dt.datetime.now(ZoneInfo("Europe/London"))
    return now_uk.hour == 19 and now_uk.minute < 15  # 7:00–7:14pm window


# ---------- Telegram ----------

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
        timeout=30,
    )
    resp.raise_for_status()


def build_reminder_message(due_bins: list[dict]) -> str:
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
        short = b["date"].strftime("%a %d %b")
        lines.append(f"{b['type']} bin — {short}")

    lines.extend(["", "Put them out tonight 👌"])
    return "\n".join(lines)


# ---------- Scraping helpers ----------

def save_html(filename: str, html: str) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)


def get_csrf(soup: BeautifulSoup) -> str:
    token = soup.select_one('input[name="_csrf"]')
    if not token or not token.get("value"):
        raise RuntimeError("Couldn't find CSRF token")
    return token["value"]


def select_address_option(soup: BeautifulSoup, label_match: str):
    sel = soup.select_one('select[name="address"]')
    if not sel:
        raise RuntimeError("Address dropdown not found")

    options = []
    for opt in sel.select("option"):
        value = (opt.get("value") or "").strip()
        label = opt.get_text(" ", strip=True)
        if value:
            options.append((label, value))

    needle = label_match.lower()
    for label, value in options:
        if needle in label.lower():
            return value, label

    raise RuntimeError("Address not matched")


def extract_next_collections(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []

    for card in soup.select("div.ncc-bin-calendar"):
        ps = [p.get_text(strip=True) for p in card.find_all("p")]
        if len(ps) < 3:
            continue

        bin_type, day, date_text = ps[:3]

        try:
            date_val = dt.datetime.strptime(date_text, "%d %B %Y").date()
        except ValueError:
            continue

        results.append(
            {"type": bin_type, "day": day, "date": date_val, "raw": date_text}
        )

    return results


# ---------- Main ----------

def main():
    if not should_run_now_on_github_actions():
        print("Not within 7pm UK window — exiting.")
        return

    if FORCE_TEST_MESSAGE:
        send_telegram("✅ Binchecker test: Telegram working.")
        print("Sent test message.")

    # Session with proper CA bundle (fixes GitHub SSL error)
    s = requests.Session()
    s.verify = certifi.where()

    s.cookies.set("x-bni-ja", "1707374704", domain="bincollection.northumberland.gov.uk")

    # Step 1 — postcode page
    r0 = s.get(f"{BASE}/", timeout=30)
    r0.raise_for_status()
    soup0 = BeautifulSoup(r0.text, "lxml")
    csrf0 = get_csrf(soup0)

    # Step 2 — postcode submit
    r1 = s.post(
        f"{BASE}/postcode",
        data={"_csrf": csrf0, "postcode": POSTCODE},
        timeout=30,
    )
    r1.raise_for_status()

    soup1 = BeautifulSoup(r1.text, "lxml")
    csrf1 = get_csrf(soup1)

    form = soup1.find("form")
    submit_url = urljoin(BASE, form.get("action"))

    address_value, address_label = select_address_option(
        soup1, ADDRESS_LABEL_MATCH
    )
    print("Selected:", address_label)

    # Step 3 — address submit
    r2 = s.post(
        submit_url,
        data={"_csrf": csrf1, "address": address_value},
        timeout=30,
    )
    r2.raise_for_status()

    collections = extract_next_collections(r2.text)
    tomorrow = dt.date.today() + dt.timedelta(days=1)

    watched = [c for c in collections if c["type"] in WATCH_FOR]
    due = [c for c in watched if c["date"] == tomorrow]

    if due:
        send_telegram(build_reminder_message(due))
        print("Reminder sent.")
    else:
        print("No watched bins due tomorrow.")


if __name__ == "__main__":
    main()