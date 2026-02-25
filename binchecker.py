import datetime as dt
import os
import warnings
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import certifi
import requests
from bs4 import BeautifulSoup
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning

BASE = "https://bincollection.northumberland.gov.uk"
ENTRY_PATH = "/postcode"  # IMPORTANT: start here, not "/"

# ========= USER SETTINGS =========
POSTCODE = "NE18 0QP"
ADDRESS_LABEL_MATCH = "The Bastle"
WATCH_FOR = {"General"}  # e.g. {"General", "Recycling"}
# =================================

# Telegram (set as env vars in PyCharm / GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Optional: FORCE_TEST_MESSAGE=1 sends a test message regardless of bin day
FORCE_TEST_MESSAGE = os.getenv("FORCE_TEST_MESSAGE", "").strip().lower() in {"1", "true", "yes"}

# Allow SSL verify=False fallback (default ON). Optional secret to disable: ALLOW_INSECURE_SSL_FALLBACK=0
ALLOW_INSECURE_SSL_FALLBACK = os.getenv("ALLOW_INSECURE_SSL_FALLBACK", "1").strip().lower() in {"1", "true", "yes"}


def should_run_now_on_github_actions() -> bool:
    """Only enforce 7pm UK time for scheduled runs; allow manual runs anytime."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        return True
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True
    now_uk = dt.datetime.now(ZoneInfo("Europe/London"))
    return now_uk.hour == 19 and now_uk.minute < 15  # 19:00–19:14 window


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
        short = b["date"].strftime("%a %d %b")
        lines.append(f"{b['type']} bin — {short}")
    lines.extend(["", "Put them out tonight 👌"])
    return "\n".join(lines)


def get_csrf(soup: BeautifulSoup) -> str:
    token = soup.select_one('input[name="_csrf"]')
    if not token or not token.get("value"):
        raise RuntimeError("Couldn't find CSRF token on page")
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


def make_session() -> requests.Session:
    s = requests.Session()

    # Browser-like headers (helps with WAF/bot filters)
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Connection": "keep-alive",
        }
    )

    # Try strict SSL first using certifi
    s.verify = certifi.where()

    # Cookie seen in their HTML anti-bot snippet
    s.cookies.set("x-bni-ja", "1707374704", domain="bincollection.northumberland.gov.uk", path="/")
    return s


def safe_get(s: requests.Session, url: str, **kwargs) -> requests.Response:
    try:
        r = s.get(url, **kwargs)
        r.raise_for_status()
        return r
    except SSLError:
        if not ALLOW_INSECURE_SSL_FALLBACK:
            raise
        print("⚠️ SSL verification failed for council site. Retrying with verify=False (insecure).")
        warnings.simplefilter("ignore", InsecureRequestWarning)
        r = s.get(url, verify=False, **kwargs)
        r.raise_for_status()
        return r


def safe_post(s: requests.Session, url: str, **kwargs) -> requests.Response:
    try:
        r = s.post(url, **kwargs)
        r.raise_for_status()
        return r
    except SSLError:
        if not ALLOW_INSECURE_SSL_FALLBACK:
            raise
        print("⚠️ SSL verification failed for council site. Retrying with verify=False (insecure).")
        warnings.simplefilter("ignore", InsecureRequestWarning)
        r = s.post(url, verify=False, **kwargs)
        r.raise_for_status()
        return r


def main():
    if not should_run_now_on_github_actions():
        print("Not within 7pm UK window — exiting (scheduled run).")
        return

    if FORCE_TEST_MESSAGE:
        send_telegram("✅ Binchecker test: workflow ran and Telegram is working.")
        print("Sent test message (FORCE_TEST_MESSAGE=1).")

    s = make_session()

    # Step 1 — load ENTRY page for CSRF (use /postcode, not /)
    entry_url = f"{BASE}{ENTRY_PATH}"
    r0 = safe_get(s, entry_url, timeout=30)

    # If the site is blocking automation, it may return a fake 404/blank page.
    if r0.status_code == 404 or "Check your bin collection dates" not in r0.text:
        raise RuntimeError(
            "Council site appears to be blocking GitHub Actions (WAF/bot protection). "
            "This often shows as 404/validation pages from automated IPs. "
            "Best fix: run the script at home (cron/launchd/Raspberry Pi) or on a small VPS."
        )

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