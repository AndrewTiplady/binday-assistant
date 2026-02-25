import datetime as dt
import os
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
import certifi
from bs4 import BeautifulSoup
from requests.exceptions import SSLError

BASE = "https://bincollection.northumberland.gov.uk"

# ========= USER SETTINGS =========
POSTCODE = "NE18 0QP"
ADDRESS_LABEL_MATCH = "The Bastle"
WATCH_FOR = {"General"}  # e.g. {"General", "Recycling"}
# =================================

# Telegram (set as env vars in PyCharm / GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Optional: FORCE_TEST_MESSAGE=1 sends a test message regardless of bin day (useful for Actions tests)
FORCE_TEST_MESSAGE = os.getenv("FORCE_TEST_MESSAGE", "").strip().lower() in {"1", "true", "yes"}

# If council SSL fails on GitHub Actions, allow fallback to insecure mode (bin dates are non-sensitive).
# You can force it on/off with ALLOW_INSECURE_SSL_FALLBACK=1/0 as a repo secret.
ALLOW_INSECURE_SSL_FALLBACK = os.getenv("ALLOW_INSECURE_SSL_FALLBACK", "1").strip() in {"1", "true", "yes"}


# ---------- GitHub timing logic ----------

def should_run_now_on_github_actions() -> bool:
    """Only enforce 7pm UK time for scheduled runs; allow manual runs anytime."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        return True

    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True  # manual run (workflow_dispatch)

    now_uk = dt.datetime.now(ZoneInfo("Europe/London"))
    return now_uk.hour == 19 and now_uk.minute < 15  # 19:00–19:14 window


# ---------- Telegram ----------

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=30)
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
        short = b["date"].strftime("%a %d %b")  # e.g. Wed 25 Feb
        lines.append(f"{b['type']} bin — {short}")
    lines.extend(["", "Put them out tonight 👌"])
    return "\n".join(lines)


# ---------- Scraping helpers ----------

def get_csrf(soup: BeautifulSoup) -> str:
    token = soup.select_one('input[name="_csrf"]')
    if not token or not token.get("value"):
        raise RuntimeError("Couldn't find CSRF token")
    return token["value"]


def select_address_option(soup: BeautifulSoup, label_match: str):
    sel = soup.select_one('select[name="address"]')
    if not sel:
        raise RuntimeError("Address dropdown not found")

    needle = label_match.lower()
    options = []
    for opt in sel.select("option"):
        value = (opt.get("value") or "").strip()
        label = opt.get_text(" ", strip=True)
        if value:
            options.append((label, value))
            if needle in label.lower():
                return value, label

    print("\nAvailable address options:")
    for label, value in options:
        print(f"- {label} (value={value})")

    raise RuntimeError("Address not matched — update ADDRESS_LABEL_MATCH.")


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
        results.append({"type": bin_type, "day": day, "date": date_val, "raw": date_text})
    return results


# ---------- Requests wrapper with SSL fallback ----------

def session_with_ssl_fallback() -> requests.Session:
    s = requests.Session()

    # Try strict verification first using certifi
    s.verify = certifi.where()

    # Cookie observed in site HTML (anti-bot-ish)
    s.cookies.set("x-bni-ja", "1707374704", domain="bincollection.northumberland.gov.uk")
    return s


def safe_get(s: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET with strict SSL first, optional verify=False fallback if the council SSL chain fails on Actions."""
    try:
        r = s.get(url, **kwargs)
        r.raise_for_status()
        return r
    except SSLError as e:
        if not ALLOW_INSECURE_SSL_FALLBACK:
            raise
        print("⚠️ SSL verification failed for council site. Retrying with verify=False (insecure).")
        r = s.get(url, verify=False, **kwargs)  # fallback only for non-sensitive bin data
        r.raise_for_status()
        return r


def safe_post(s: requests.Session, url: str, **kwargs) -> requests.Response:
    """POST with strict SSL first, optional verify=False fallback if the council SSL chain fails on Actions."""
    try:
        r = s.post(url, **kwargs)
        r.raise_for_status()
        return r
    except SSLError:
        if not ALLOW_INSECURE_SSL_FALLBACK:
            raise
        print("⚠️ SSL verification failed for council site. Retrying with verify=False (insecure).")
        r = s.post(url, verify=False, **kwargs)
        r.raise_for_status()
        return r


# ---------- Main ----------

def main():
    if not should_run_now_on_github_actions():
        print("Not within 7pm UK window — exiting (scheduled run).")
        return

    if FORCE_TEST_MESSAGE:
        send_telegram("✅ Binchecker test: Telegram working (GitHub Actions ran).")
        print("Sent test message (FORCE_TEST_MESSAGE=1).")

    s = session_with_ssl_fallback()

    # Step 1 — postcode page
    r0 = safe_get(s, f"{BASE}/", timeout=30)
    soup0 = BeautifulSoup(r0.text, "lxml")
    csrf0 = get_csrf(soup0)

    # Step 2 — submit postcode -> address select page
    r1 = safe_post(
        s,
        f"{BASE}/postcode",
        data={"_csrf": csrf0, "postcode": POSTCODE},
        timeout=30,
        allow_redirects=True,
    )
    soup1 = BeautifulSoup(r1.text, "lxml")
    csrf1 = get_csrf(soup1)

    form = soup1.find("form")
    if not form or not form.get("action"):
        raise RuntimeError("Couldn't find address form/action")
    submit_url = urljoin(BASE, form.get("action"))

    address_value, address_label = select_address_option(soup1, ADDRESS_LABEL_MATCH)
    print(f"Selected: {address_label}")

    # Step 3 — submit address -> schedule
    r2 = safe_post(
        s,
        submit_url,
        data={"_csrf": csrf1, "address": address_value},
        timeout=30,
        allow_redirects=True,
    )

    collections = extract_next_collections(r2.text)
    if not collections:
        print("Couldn't parse next collections from schedule page.")
        return

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