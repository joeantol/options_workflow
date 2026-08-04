#!/Users/joeandbabs/public-env/bin/python
"""
multiplier_fetch.py

Fetches the latest Review or Plan from The Multiplier Substack,
saves as a PDF, and uploads it to the NotebookLM notebook.

Credentials are stored in macOS Keychain (service: multiplier_substack).
First run prompts for email + password; subsequent runs reuse them.

Usage:
    ./multiplier_fetch.py             # download + upload
    ./multiplier_fetch.py --dryrun    # show what would happen, no I/O
    ./multiplier_fetch.py --download  # download only
    ./multiplier_fetch.py --upload    # upload most-recently-downloaded PDF
"""

import argparse
import asyncio
import csv
import getpass
import os
import re
import smtplib
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

OUTPUT_DIR    = Path("/Users/joeandbabs/work/retirement/options")
SUBSTACK_BASE = "https://themultiplier.substack.com"
SVC_NAME      = "multiplier_substack"
NOTEBOOK_ID   = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID", "")


# ── macOS Keychain helpers ────────────────────────────────────────────────────

def _kget(account: str, svc: str = SVC_NAME) -> str | None:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", svc, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _kset(account: str, value: str, svc: str = SVC_NAME) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", svc, "-a", account],
        capture_output=True,
    )
    subprocess.run(
        ["security", "add-generic-password", "-s", svc, "-a", account, "-w", value],
        check=True, capture_output=True,
    )


def get_credentials() -> tuple[str, str]:
    email    = _kget("email")
    password = _kget("password")
    if not email:
        email = input("Substack email: ").strip()
        _kset("email", email)
        print("  Email saved to Keychain.")
    if not password:
        password = getpass.getpass("Substack password: ")
        _kset("password", password)
        print("  Password saved to Keychain.")
    return email, password


def reset_credentials() -> None:
    """Remove saved credentials from Keychain (re-run to re-enter)."""
    for acct in ("email", "password"):
        subprocess.run(
            ["security", "delete-generic-password", "-s", SVC_NAME, "-a", acct],
            capture_output=True,
        )
    print("Credentials removed from Keychain.")


# ── Email ─────────────────────────────────────────────────────────────────────

SMTP_SVC   = "multiplier_smtp"
EMAIL_TO   = "joe@antol.com"


def _get_smtp_config() -> tuple[str, int, str, str]:
    host = _kget("host", SMTP_SVC)
    port = _kget("port", SMTP_SVC)
    user = _kget("user", SMTP_SVC)
    pwd  = _kget("password", SMTP_SVC)
    changed = False
    if not host:
        host = input("SMTP host (e.g. smtp.gmail.com): ").strip()
        _kset("host", host, SMTP_SVC); changed = True
    if not port:
        port = input("SMTP port [587]: ").strip() or "587"
        _kset("port", port, SMTP_SVC); changed = True
    if not user:
        user = input("SMTP username (your email address): ").strip()
        _kset("user", user, SMTP_SVC); changed = True
    if not pwd:
        pwd = getpass.getpass("SMTP password (or app password): ")
        _kset("password", pwd, SMTP_SVC); changed = True
    if changed:
        print("  SMTP config saved to Keychain.")
    return host, int(port), user, pwd


def send_email(subject: str, body: str) -> None:
    try:
        host, port, user, pwd = _get_smtp_config()
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = user
        msg["To"]      = EMAIL_TO
        with smtplib.SMTP(host, port) as s:
            s.ehlo()
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        print(f"  Email sent → {EMAIL_TO}: {subject}")
    except Exception as exc:
        print(f"  Email failed: {exc}")


# ── Week / date helpers ───────────────────────────────────────────────────────

def _max_week(prefix: str) -> int:
    """Return the highest Week_XX in the notebook for PLAN or REVIEW files."""
    max_w = 0
    try:
        for title in asyncio.run(_notebook_titles()):
            if title.startswith(f"{prefix}_") and title.endswith(".pdf"):
                m = re.search(r"Week_(\d+)", title)
                if m:
                    max_w = max(max_w, int(m.group(1)))
    except Exception:
        pass
    return max_w


def _last_friday() -> datetime:
    """Most recent Friday that has passed (not today even if today is Friday)."""
    today = datetime.now()
    back = (today.weekday() - 4) % 7 or 7
    return today - timedelta(days=back)


def _next_friday() -> datetime:
    """Next Friday that hasn't arrived yet (not today even if today is Friday)."""
    today = datetime.now()
    fwd = (4 - today.weekday()) % 7 or 7
    return today + timedelta(days=fwd)


def build_filename(post_type: str) -> str:
    week = _max_week(post_type) + 1
    date = _last_friday() if post_type == "REVIEW" else _next_friday()
    return f"{post_type}_{date.strftime('%Y_%m_%d')}_Week_{week:02d}.pdf"


def already_have(post_type: str) -> Path | None:
    """Return existing file for this period if already downloaded."""
    date = _last_friday() if post_type == "REVIEW" else _next_friday()
    date_str = date.strftime("%Y_%m_%d")
    matches = list(OUTPUT_DIR.glob(f"{post_type}_{date_str}_Week_*.pdf"))
    return matches[0] if matches else None


def plan_csv_path_for_review(review_pdf: Path) -> Path:
    """Return the PLAN_*.csv path that corresponds to a given REVIEW PDF.

    Review Week N embeds the plan spreadsheet for Week N+1.  The plan date is
    always review_date + 7 days (the Friday of the following week), derived
    directly from the review filename so it never depends on when we run.
    """
    m = re.search(r"Week_(\d+)", review_pdf.name)
    if not m:
        raise ValueError(f"Cannot extract week number from {review_pdf.name}")
    plan_week = int(m.group(1)) + 1
    dm = re.search(r"REVIEW_(\d{4}_\d{2}_\d{2})_", review_pdf.name)
    if dm:
        review_date = datetime.strptime(dm.group(1), "%Y_%m_%d")
        date_str = (review_date + timedelta(days=7)).strftime("%Y_%m_%d")
    else:
        date_str = _next_friday().strftime("%Y_%m_%d")
    return OUTPUT_DIR / f"PLAN_{date_str}_Week_{plan_week:02d}.csv"


def xlsx_to_csv(xlsx_path: Path, csv_path: Path) -> None:
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in ws.iter_rows(values_only=True):
            writer.writerow(["" if v is None else v for v in row])
    print(f"  Saved: {csv_path}")


# ── Substack post discovery ───────────────────────────────────────────────────

def _classify_post(title: str, pub: str) -> str | None:
    """Classify a post as REVIEW or PLAN.

    Primary rule: Sunday publication → REVIEW, Monday → PLAN.
    Fallback: look for 'Review' or 'Plan' keyword in the title.
    """
    if pub:
        try:
            weekday = datetime.fromisoformat(pub.replace("Z", "+00:00")).weekday()
            if weekday == 6:
                return "REVIEW"
            if weekday == 0:
                return "PLAN"
        except Exception:
            pass
    if re.search(r"\bReview\b", title, re.IGNORECASE):
        return "REVIEW"
    if re.search(r"\bPlan\b", title, re.IGNORECASE):
        return "PLAN"
    return None


def get_latest_posts() -> list[dict]:
    """Return the most recent Review AND Plan post from the RSS feed (whichever exist).

    The RSS feed indexes new posts faster than /api/v1/archive, which can lag
    by hours right after publish and cause the scheduler to miss same-day posts.
    """
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    resp = requests.get(f"{SUBSTACK_BASE}/feed", timeout=30)
    root = ET.fromstring(resp.content)
    found: dict[str, dict] = {}
    for item in root.iter("item"):
        title   = (item.findtext("title") or "").strip()
        link    = (item.findtext("link") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        pub = ""
        if pub_raw:
            try:
                pub = parsedate_to_datetime(pub_raw).isoformat()
            except Exception:
                pass
        post_type = _classify_post(title, pub)
        if post_type and post_type not in found:
            found[post_type] = {"type": post_type, "url": link, "title": title, "post_date": pub}
        if len(found) == 2:
            break
    if not found:
        raise RuntimeError("No recent Review or Plan post found in the RSS feed.")
    return list(found.values())


# ── Scheduler helpers ─────────────────────────────────────────────────────────

SCHEDULER_MAX_HOURS = 15   # give up at midnight if started at 9 AM


def _post_is_new(post: dict) -> bool:
    """True if the post was published after last Friday (i.e. it's this week's)."""
    raw = post.get("post_date", "")
    if not raw:
        return True
    try:
        pub = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        return pub.date() >= _last_friday().date()
    except Exception:
        return True


def _sunday_complete() -> bool:
    review = already_have("REVIEW")
    if not review:
        return False
    csv = plan_csv_path_for_review(review)
    return notebook_has(review.name) and notebook_has(csv.name)


def _monday_complete() -> bool:
    plan = already_have("PLAN")
    return bool(plan and notebook_has(plan.name))


def _scheduler_loop(mode: str, noemail: bool) -> None:
    deadline    = datetime.now() + timedelta(hours=SCHEDULER_MAX_HOURS)
    complete_fn = _sunday_complete if mode == "sunday" else _monday_complete
    downloaded: list[str] = []
    errors:     list[str] = []
    attempt = 0
    _complete = False

    while datetime.now() < deadline:
        attempt += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        print(f"\n[{mode.upper()}] Attempt {attempt} at {ts}")

        if complete_fn():
            if attempt == 1:
                print("  Already complete — nothing to do.")
                return           # silent exit; email was sent on the previous run
            _complete = True
            break                # just finished; fall through to send email

        # ── Check for new content ────────────────────────────────────────────
        try:
            posts = get_latest_posts()
        except Exception as exc:
            msg = f"Archive fetch failed: {exc}"
            errors.append(msg); print(f"  {msg}")
            _sleep_until(datetime.now() + timedelta(hours=1), deadline)
            continue

        target_types = ["REVIEW"] if mode == "sunday" else ["PLAN"]
        new_posts    = [p for p in posts
                        if p["type"] in target_types and _post_is_new(p)]

        if not new_posts:
            remaining_h = int((deadline - datetime.now()).total_seconds() // 3600)
            print(f"  No new {'/'.join(target_types)} post yet. "
                  f"Retrying in 1 hour ({remaining_h}h left).")
            _sleep_until(datetime.now() + timedelta(hours=1), deadline)
            continue

        # ── Download & upload ────────────────────────────────────────────────
        for p in new_posts:
            out_path = OUTPUT_DIR / build_filename(p["type"])
            try:
                print(f"  Downloading [{p['type']}] …")
                xlsx_tmp = download_pdf(p["url"], out_path,
                                        grab_xlsx=(p["type"] == "REVIEW"))
                downloaded.append(out_path.name)
                if xlsx_tmp:
                    csv_out = plan_csv_path_for_review(out_path)
                    xlsx_to_csv(xlsx_tmp, csv_out)
                    xlsx_tmp.unlink(missing_ok=True)
                    downloaded.append(csv_out.name)
                    print(f"  Uploading {csv_out.name} …")
                    upload_to_notebooklm(csv_out)
                print(f"  Uploading {out_path.name} …")
                upload_to_notebooklm(out_path)
            except Exception as exc:
                msg = f"{p['type']} failed: {exc}"
                errors.append(msg); print(f"  {msg}")

        # Sunday mode: if Review exists but CSV is still missing, grab Excel now
        if mode == "sunday":
            review_file = already_have("REVIEW")
            if review_file:
                csv_out = plan_csv_path_for_review(review_file)
                if not csv_out.exists():
                    review_post = next((p for p in posts if p["type"] == "REVIEW"), None)
                    if review_post:
                        try:
                            print("  CSV still missing — downloading Excel only …")
                            xlsx_tmp = download_pdf(review_post["url"], review_file,
                                                    grab_xlsx=True, xlsx_only=True)
                            if xlsx_tmp:
                                xlsx_to_csv(xlsx_tmp, csv_out)
                                xlsx_tmp.unlink(missing_ok=True)
                                downloaded.append(csv_out.name)
                                upload_to_notebooklm(csv_out)
                        except Exception as exc:
                            errors.append(f"PLAN CSV: {exc}")

        if downloaded and not errors:
            _complete = True
            break

        _sleep_until(datetime.now() + timedelta(hours=1), deadline)
    else:
        _complete = False
        errors.append(f"Timed out after {SCHEDULER_MAX_HOURS}h without finding new content.")

    # ── Send completion email ────────────────────────────────────────────────
    if _complete:
        subject = f"SUCCESS :: Multiplier {mode.capitalize()} complete"
        if downloaded:
            body = "Downloaded and loaded into NotebookLM:\n\n"
            body += "\n".join(f"  • {f}" for f in downloaded)
        else:
            body = "Already complete — finished by another run."
        if errors:
            body += "\n\nWarnings:\n" + "\n".join(f"  • {e}" for e in errors)
    else:
        subject = f"FAILURE :: Multiplier {mode.capitalize()} — "
        subject += "timeout" if not errors else errors[0][:60]
        body    = f"Could not complete {mode} run after {SCHEDULER_MAX_HOURS} hours.\n"
        if downloaded:
            body += "\nPartially downloaded:\n" + "\n".join(f"  • {f}" for f in downloaded)
        if errors:
            body += "\nErrors:\n" + "\n".join(f"  • {e}" for e in errors)

    print(f"\n{subject}")
    if not noemail:
        send_email(subject, body)
    else:
        print("  (--noemail: skipped sending)")


def _sleep_until(wake: datetime, deadline: datetime) -> None:
    target = min(wake, deadline)
    secs   = (target - datetime.now()).total_seconds()
    if secs > 0:
        time.sleep(secs)


# ── Session cookie storage ────────────────────────────────────────────────────

COOKIE_FILE = Path.home() / ".config" / "multiplier_fetch" / "cookies.json"


def _load_cookies() -> list | None:
    if COOKIE_FILE.exists():
        import json
        try:
            return json.loads(COOKIE_FILE.read_text())
        except Exception:
            pass
    return None


def _save_cookies(cookies: list) -> None:
    import json
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(cookies))


# ── PDF download via Playwright ───────────────────────────────────────────────

async def _interactive_login() -> list:
    """Open a visible browser so the user can log in (handles CAPTCHA), return cookies."""
    from playwright.async_api import async_playwright
    print("  Opening browser for sign-in (complete login, including any CAPTCHA) …")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx  = await browser.new_context(viewport={"width": 1200, "height": 900})
        page = await ctx.new_page()
        await page.goto("https://substack.com/sign-in", wait_until="domcontentloaded")
        print("  Waiting for you to sign in …")
        # Wait until we're no longer on any sign-in page (up to 3 minutes)
        await page.wait_for_function(
            "() => !window.location.href.includes('/sign-in')",
            timeout=180_000,
        )
        print("  Signed in. Saving session …")
        cookies = await ctx.cookies()
        await browser.close()
    return cookies


async def _download_pdf(post_url: str, out: Path, grab_xlsx: bool = False,
                        xlsx_only: bool = False) -> Path | None:
    """Download post as PDF. If grab_xlsx=True, also download the first .xlsx
    link on the page and return its temp path (caller must delete it).
    If xlsx_only=True, skip the PDF save entirely (just grab the Excel)."""
    from playwright.async_api import async_playwright

    cookies = _load_cookies()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx  = await browser.new_context(viewport={"width": 1200, "height": 900})

        if cookies:
            await ctx.add_cookies(cookies)
        else:
            saved = await _interactive_login()
            _save_cookies(saved)
            await ctx.add_cookies(saved)

        page = await ctx.new_page()

        # ── Load post ────────────────────────────────────────────────────────
        print("  Loading post …")
        await page.goto(post_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # If we got redirected to a login page, the saved session expired
        if "/sign-in" in page.url or "login" in page.url:
            print("  Session expired — re-authenticating …")
            await browser.close()
            COOKIE_FILE.unlink(missing_ok=True)
            saved = await _interactive_login()
            _save_cookies(saved)
            browser = await pw.chromium.launch(headless=True)
            ctx  = await browser.new_context(viewport={"width": 1200, "height": 900})
            await ctx.add_cookies(saved)
            page = await ctx.new_page()
            await page.goto(post_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

        # Dismiss any modals
        for selector in ['[data-testid="close-button"]', 'button:has-text("No thanks")',
                         'button:has-text("Close")']:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
            except Exception:
                pass

        # Scroll to trigger lazy-loaded images
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # ── Optionally grab the Excel spreadsheet ─────────────────────────────
        xlsx_tmp: Path | None = None
        if grab_xlsx:
            xlsx_link = page.locator('a[href*=".xlsx"], a[href*=".xls"]').first
            try:
                await xlsx_link.wait_for(timeout=5_000)
                print("  Downloading Excel spreadsheet …")
                suffix = ".xlsx"
                href = await xlsx_link.get_attribute("href") or ""
                if href.lower().endswith(".xls"):
                    suffix = ".xls"
                async with page.expect_download(timeout=30_000) as dl_info:
                    await xlsx_link.click()
                dl = await dl_info.value
                tmp = Path(tempfile.mktemp(suffix=suffix))
                await dl.save_as(str(tmp))
                xlsx_tmp = tmp
            except Exception as exc:
                print(f"  Warning: no Excel link found on this page ({exc})")

        # ── Save PDF ─────────────────────────────────────────────────────────
        if not xlsx_only:
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.pdf(
                path=str(out),
                format="Letter",
                print_background=True,
                margin={"top": "0.6in", "bottom": "0.6in",
                        "left": "0.75in", "right": "0.75in"},
            )
            print(f"  Saved: {out}")

        _save_cookies(await ctx.cookies())
        await browser.close()

    return xlsx_tmp


def download_pdf(post_url: str, out: Path, grab_xlsx: bool = False,
                 xlsx_only: bool = False) -> Path | None:
    return asyncio.run(_download_pdf(post_url, out, grab_xlsx=grab_xlsx,
                                     xlsx_only=xlsx_only))


# ── NotebookLM upload ─────────────────────────────────────────────────────────

async def _notebook_titles() -> set[str]:
    from notebooklm import NotebookLMClient
    async with NotebookLMClient.from_storage() as client:
        return {(s.title or "") for s in await client.sources.list(NOTEBOOK_ID)}


def notebook_has(name: str) -> bool:
    """Return True if a source with this exact filename is in the notebook."""
    if not NOTEBOOK_ID:
        return False
    return name in asyncio.run(_notebook_titles())


async def _upload(path: Path) -> None:
    from notebooklm import NotebookLMClient
    async with NotebookLMClient.from_storage() as client:
        existing = await client.sources.list(NOTEBOOK_ID)
        for src in existing:
            if (src.title or "") == path.name:
                print(f"  Deleting old source: {src.title}")
                await client.sources.delete(NOTEBOOK_ID, src.id)
        await client.sources.add_file(NOTEBOOK_ID, str(path), wait=True, wait_timeout=300.0)


def upload_to_notebooklm(path: Path) -> None:
    if not NOTEBOOK_ID:
        raise RuntimeError("NOTEBOOKLM_NOTEBOOK_ID not set in .env")
    print(f"  Uploading {path.name} to NotebookLM …")
    asyncio.run(_upload(path))
    print("  Upload complete.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch Multiplier Substack post → PDF → NotebookLM"
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dryrun",   action="store_true", help="Show what would happen")
    mode.add_argument("--download", action="store_true", help="Download only")
    mode.add_argument("--upload",   action="store_true", help="Upload latest PDF only")
    mode.add_argument("--sunday",   action="store_true",
                      help="Sunday scheduler: retry hourly until Review + Plan CSV found")
    mode.add_argument("--monday",   action="store_true",
                      help="Monday scheduler: retry hourly until Plan found")
    ap.add_argument("--noemail",    action="store_true",
                    help="Skip email notification (scheduler modes)")
    ap.add_argument("--reset-creds", action="store_true",
                    help="Remove saved Keychain credentials and session cookies")
    args = ap.parse_args()

    if args.reset_creds:
        reset_credentials()
        COOKIE_FILE.unlink(missing_ok=True)
        for acct in ("host", "port", "user", "password"):
            subprocess.run(
                ["security", "delete-generic-password", "-s", SMTP_SVC, "-a", acct],
                capture_output=True,
            )
        print(f"Substack session and SMTP credentials cleared.")
        if any([args.dryrun, args.download, args.upload, args.sunday, args.monday]):
            return

    # ── Scheduler modes ──────────────────────────────────────────────────────
    if args.sunday:
        _scheduler_loop("sunday", args.noemail)
        return
    if args.monday:
        _scheduler_loop("monday", args.noemail)
        return

    do_download = not args.upload   and not args.dryrun
    do_upload   = not args.download and not args.dryrun

    # ── Discover latest posts ────────────────────────────────────────────────
    print("Checking Substack archive …")
    posts = get_latest_posts()
    for p in posts:
        print(f"  Found [{p['type']}]: {p['title']}")

    # ── Upload-only mode: use most recent PDF ────────────────────────────────
    if args.upload:
        pdfs = sorted(OUTPUT_DIR.glob("*.pdf"), key=lambda f: f.stat().st_mtime)
        if not pdfs:
            print("No PDFs found in output directory.")
            sys.exit(1)
        upload_to_notebooklm(pdfs[-1])
        return

    # ── Dry run ──────────────────────────────────────────────────────────────
    if args.dryrun:
        print("\nDry run — would:")
        new_posts = []
        new_csvs: list[Path] = []
        for p in posts:
            existing = already_have(p["type"])
            out_path = OUTPUT_DIR / build_filename(p["type"])
            if existing:
                in_nb = notebook_has(existing.name)
                status = "in notebook" if in_nb else "NOT in notebook — would upload"
                print(f"  [{p['type']}] Already downloaded: {existing.name} ({status})")
                if not in_nb:
                    new_posts.append(p)
                if p["type"] == "REVIEW":
                    csv_out = plan_csv_path_for_review(existing)
                    if not csv_out.exists():
                        print(f"  [REVIEW]  + Excel missing → would download {csv_out.name}")
                        new_csvs.append((p, csv_out))
                    elif not notebook_has(csv_out.name):
                        print(f"  [REVIEW]  + {csv_out.name} NOT in notebook — would upload")
                        new_csvs.append((p, csv_out))
            else:
                print(f"  [{p['type']}] Download → {out_path.name}")
                if p["type"] == "REVIEW":
                    csv_out = plan_csv_path_for_review(out_path)
                    print(f"  [REVIEW]  + Excel → {csv_out.name}")
                new_posts.append(p)
        if new_posts or new_csvs:
            print(f"  Upload new file(s) → NotebookLM notebook {NOTEBOOK_ID or '(NOTEBOOK_ID not set)'}")
        else:
            print("  Nothing to upload — no new files.")
        return

    # ── Download + upload ─────────────────────────────────────────────────────
    to_upload: list[Path] = []

    for p in posts:
        existing = already_have(p["type"])
        out_path = OUTPUT_DIR / build_filename(p["type"])

        if existing:
            print(f"\n[{p['type']}] Already downloaded: {existing.name}")
            if do_upload and not notebook_has(existing.name):
                print(f"  Not in notebook — queuing upload.")
                to_upload.append(existing)
            if p["type"] == "REVIEW":
                csv_out = plan_csv_path_for_review(existing)
                if not csv_out.exists() and do_download:
                    print(f"  Plan CSV missing — downloading Excel …")
                    xlsx_tmp = download_pdf(p["url"], existing,
                                            grab_xlsx=True, xlsx_only=True)
                    if xlsx_tmp:
                        xlsx_to_csv(xlsx_tmp, csv_out)
                        xlsx_tmp.unlink(missing_ok=True)
                        to_upload.append(csv_out)
                elif csv_out.exists() and do_upload and not notebook_has(csv_out.name):
                    print(f"  Plan CSV not in notebook — queuing upload.")
                    to_upload.append(csv_out)
        elif do_download:
            print(f"\n[{p['type']}] Downloading …")
            xlsx_tmp = download_pdf(p["url"], out_path,
                                    grab_xlsx=(p["type"] == "REVIEW"))
            if xlsx_tmp:
                csv_out = plan_csv_path_for_review(out_path)
                xlsx_to_csv(xlsx_tmp, csv_out)
                xlsx_tmp.unlink(missing_ok=True)
                to_upload.append(csv_out)
            to_upload.append(out_path)

    if do_upload:
        if not to_upload:
            print("\nNothing new to upload.")
        else:
            for path in to_upload:
                print(f"\nUploading {path.name} …")
                upload_to_notebooklm(path)

    print("\nDone.")


if __name__ == "__main__":
    main()
