"""1177 — export your medical journal from journalen.1177.se.

A terminal app that logs in with BankID (QR code rendered right in the
terminal), incrementally syncs journal records (Anteckningar, Provsvar,
Diagnoser, Läkemedel) into a local JSON store, and renders them as Markdown.
Run `1177` for the interactive menu, or `1177 --sync` for scripts.

Unofficial personal tool — not affiliated with 1177, Inera, or BankID.
"""

import asyncio
import io
import json
import logging
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

import qrcode
import tyro
import zxingcpp
from markdownify import markdownify as md_convert
from PIL import Image
from playwright.async_api import async_playwright, Page
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             Markdown as MarkdownViewer, OptionList,
                             ProgressBar, RichLog, Static)
from textual.widgets.option_list import Option

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

__version__ = "1.0.0"

JOURNALEN_URL  = "https://journalen.1177.se"
TODAY          = datetime.today().strftime("%Y-%m-%d")
LOGIN_TIMEOUT  = 120_000   # 2 min for manual BankID
NAV_TIMEOUT    = 30_000
EXPAND_TIMEOUT = 15_000    # per-record AJAX expand

# Defaults; overridden by --output-dir via set_output_dir()
OUTPUT_DIR  = Path("output")
MD_DIR      = OUTPUT_DIR / "md"
JSON_DIR    = OUTPUT_DIR / "json"
MASTER_JSON = JSON_DIR / "journal.json"

log = logging.getLogger(__name__)
console = Console(highlight=False)


class _TuiRef:
    """Holds the running Textual app (if any) so the scraping core can route
    user-facing output to it instead of printing over the TUI."""
    app = None


TUI = _TuiRef()


def say(msg: str) -> None:
    """User-facing line: into the TUI log when the app runs, else stdout.
    Always written to the log file, sans markup."""
    log.info(Text.from_markup(msg).plain)
    if TUI.app is not None:
        TUI.app.tui_say(msg)
    else:
        console.print(msg)


def rule(title: str = "", style: str = "cyan") -> None:
    if TUI.app is not None:
        TUI.app.tui_say(f"[bold {style}]── {title} ──[/]" if title else "[dim]────────[/]")
    else:
        console.rule(f"[bold {style}]{title}[/]" if title else "", style=style)


def resolve_output_dir(cli_value: Path | None) -> Path:
    """--output-dir wins; else ./output when it already holds a store (the
    repo workflow); else a stable per-user directory, so plain `1177`
    does the right thing from any cwd."""
    if cli_value:
        return cli_value
    local = Path("output")
    if local.exists() and list(local.glob("*/json/journal.json")):
        return local
    return Path.home() / ".1177"


def set_output_dir(base: Path) -> None:
    global OUTPUT_DIR, MD_DIR, JSON_DIR, MASTER_JSON
    OUTPUT_DIR  = base
    MD_DIR      = base / "md"
    JSON_DIR    = base / "json"
    MASTER_JSON = JSON_DIR / "journal.json"


CONSOLE_LOG_HANDLER: logging.Handler | None = None
BASE_DIR:            Path = Path("output")
ACTIVE_PROFILE:      str | None = None   # set ONLY by a successful login


def profile_slug(name: str) -> str:
    s = name.lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e"), ("ü", "u")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "default"


def init_base(base: Path) -> None:
    """Set up the data root and logging. No profile is selected here — the
    profile (<base>/<user-slug>/) is chosen by whoever logs in with BankID."""
    global BASE_DIR, CONSOLE_LOG_HANDLER
    BASE_DIR = base
    base.mkdir(parents=True, exist_ok=True)
    if CONSOLE_LOG_HANDLER is None:
        CONSOLE_LOG_HANDLER = RichHandler(console=console, show_time=False, show_path=False)
        CONSOLE_LOG_HANDLER.setLevel(logging.WARNING)
        file_handler = logging.FileHandler(base / "scraper.log")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.basicConfig(level=logging.INFO,
                            handlers=[file_handler, CONSOLE_LOG_HANDLER])


def activate_profile(slug: str) -> None:
    """Point OUTPUT_DIR at <base>/<slug>/."""
    global ACTIVE_PROFILE
    ACTIVE_PROFILE = slug
    set_output_dir(BASE_DIR / slug)
    MD_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Profile: {slug} ({OUTPUT_DIR.resolve()})")


def switch_profile_for(name: str, master: dict[str, "Record"]) -> None:
    """After login: land on the logged-in person's own profile (creating it
    for a first-time user), reloading the store in place. check_owner()
    stays as the safety net inside a profile (e.g. when the name could not
    be read and we fall back to 'default')."""
    global STORE_OWNER
    slug = profile_slug(name) if name else (ACTIVE_PROFILE or "default")
    if slug != ACTIVE_PROFILE:
        if name:
            say(f"[dim]Profil: {name}[/]")
        STORE_OWNER = None
        activate_profile(slug)
        master.clear()
        master.update(load_master())
    check_owner(name)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Category:
    slug:         str          # CLI name and output subdirectory
    title:        str          # Swedish display name, also the nav link text
    url_fragment: str | None   # substring of the category URL, if known


CATEGORIES = {
    "anteckningar": Category("anteckningar", "Anteckningar", "CareDocumentation"),
    "provsvar":     Category("provsvar",     "Provsvar",     None),
    "diagnoser":    Category("diagnoser",    "Diagnoser",    None),
    "lakemedel":    Category("lakemedel",    "Läkemedel",    None),
}


@dataclass
class Record:
    date:        str = ""
    record_type: str = ""
    author:      str = ""
    facility:    str = ""
    record_id:   str = ""   # journalen's data-id — SESSION-SCOPED, never identity
    content_md:  str = ""
    scraped_at:  str = ""
    category:    str = "anteckningar"
    seq:         int = 0    # occurrence index among records sharing meta_key()

    def meta_key(self) -> str:
        return f"{self.category}|{self.date}|{self.record_type}|{self.author}|{self.facility}"

    def key(self) -> str:
        """Stable identity. journalen's data-id changes every session
        (verified 2026-07: consecutive full syncs shared zero ids), so
        identity is the metadata plus an occurrence index for the rare
        records that share identical metadata."""
        return f"{self.meta_key()}#{self.seq}"

# ---------------------------------------------------------------------------
# CLI / filtering
# ---------------------------------------------------------------------------

@dataclass
class Options:
    """Resolved run configuration, shared by the one-shot and menu paths."""
    since:       str | None = None
    until:       str | None = None
    full:        bool = False
    dry_run:     bool = False
    categories:  list[Category] = field(
        default_factory=lambda: [CATEGORIES["anteckningar"]])
    auth:        str = "qr"
    qr_invert:   bool = False
    interactive: bool = False


def iso_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"not a YYYY-MM-DD date: {value!r}")
    return value


def category_slug(value: str) -> str:
    slug = value.lower().replace("ä", "a").replace("å", "a").replace("ö", "o")
    if slug != "all" and slug not in CATEGORIES:
        raise ValueError(
            f"unknown category {value!r} (choose from {', '.join(CATEGORIES)}, all)"
        )
    return slug


@dataclass
class Cli:
    """Export your medical journal from journalen.1177.se to Markdown/JSON,
    with BankID auth from the terminal. Bare `1177` opens the interactive menu."""

    since: str | None = None
    """Only fetch records dated on or after this date (YYYY-MM-DD)."""

    until: str | None = None
    """Only fetch records dated on or before this date (YYYY-MM-DD)."""

    full: bool = False
    """Re-fetch records already present in the local store
    (default is incremental: only new records are expanded)."""

    dry_run: bool = False
    """Log in, list which records would be fetched, but save nothing."""

    category: tuple[str, ...] = ()
    """Journal categories to fetch: anteckningar, provsvar, diagnoser,
    lakemedel, or 'all' (default: anteckningar)."""

    auth: Literal["qr", "app", "window"] = "qr"
    """qr: headless, BankID QR rendered in the terminal; app: headless,
    opens the BankID app on this device; window: a visible browser window."""

    qr_invert: bool = False
    """Flip the terminal QR colors — use if your terminal has a light
    background or the BankID app won't scan the code."""

    output_dir: Path | None = None
    """Where md/, json/ and scraper.log are written (default: ./output if
    it already holds a store, else ~/.1177)."""

    sync: bool = False
    """Run one sync and exit, skipping the interactive menu (implied by
    any filter flag, or when stdin is not a terminal)."""

    version: bool = False
    """Show version and exit."""


def run(c: Cli) -> None:
    if c.version:
        print(f"1177 {__version__}")
        return

    def fail(msg: str) -> None:
        console.print(f"[red]error:[/] {msg}")
        raise SystemExit(2)

    try:
        since = iso_date(c.since) if c.since else None
        until = iso_date(c.until) if c.until else None
        selected = [category_slug(v) for v in (c.category or ("anteckningar",))]
    except ValueError as e:
        fail(str(e))
    if since and until and since > until:
        fail(f"--since {since} is after --until {until}")
    if "all" in selected:
        selected = list(CATEGORIES)

    explicit = bool(c.sync or c.full or c.dry_run or c.since or c.until or c.category)
    opts = Options(
        since=since, until=until, full=c.full, dry_run=c.dry_run,
        categories=[CATEGORIES[s] for s in dict.fromkeys(selected)],
        auth=c.auth, qr_invert=c.qr_invert,
        interactive=sys.stdin.isatty() and not explicit,
    )
    init_base(resolve_output_dir(c.output_dir))
    asyncio.run(main(opts))


def in_date_range(date: str, since: str | None, until: str | None) -> bool:
    """ISO dates compare correctly as strings. Records with no parseable
    date are kept so a filter never silently drops them."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return True
    if since and date < since:
        return False
    if until and date > until:
        return False
    return True

# ---------------------------------------------------------------------------
# Local store (doubles as incremental-sync state)
# ---------------------------------------------------------------------------

# Whose journal the store holds. Set from journal.json on load, adopted from
# the login name on first sync, and enforced on every login thereafter.
STORE_OWNER: str | None = None


def load_master() -> dict[str, Record]:
    """Load the canonical store, keyed by record identity.

    Format is {"owner": <name>, "records": [...]}.
    """
    global STORE_OWNER
    if not MASTER_JSON.exists():
        return {}
    with open(MASTER_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    STORE_OWNER = raw.get("owner") or None
    records: dict[str, Record] = {}
    for item in raw.get("records", []):
        rec = Record(**{k: v for k, v in item.items() if k in Record.__dataclass_fields__})
        records[rec.key()] = rec
    return records


def save_master(records: dict[str, Record]) -> None:
    """Atomic write: never leave a half-written store on crash."""
    ordered = sorted(records.values(),
                     key=lambda r: (r.date, r.meta_key(), r.seq), reverse=True)
    tmp = MASTER_JSON.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"owner": STORE_OWNER, "records": [asdict(r) for r in ordered]},
                  f, ensure_ascii=False, indent=2)
    tmp.replace(MASTER_JSON)
    log.info(f"Store: {MASTER_JSON} ({len(records)} records, owner: {STORE_OWNER})")


def check_owner(name: str) -> None:
    """Refuse to mix two people's journals in one store. Called after login.

    - Store has an owner and the login name differs -> hard stop.
    - Store has no owner yet -> adopt the login name (stamped on next save).
    - Name could not be read -> proceed, but warn if an owner is set.
    """
    global STORE_OWNER
    if not name:
        if STORE_OWNER:
            log.warning(f"Could not verify who is logged in — this store belongs to {STORE_OWNER}.")
        return
    if STORE_OWNER is None:
        STORE_OWNER = name
        log.info(f"Store owner set to {name}")
    elif name != STORE_OWNER:
        raise RuntimeError(
            f"Logged in as {name}, but this store belongs to {STORE_OWNER}. "
            f"Refusing to mix journals — use --output-dir to export "
            f"{name}'s journal into a separate directory."
        )

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

LOGIN_SUCCESS_RE = re.compile(r"journalen\.1177\.se/(?!LoggedOut)")

# The IdP page structure (idp.inera.se) is not under our control and is only
# loosely known; everything below is best-effort with --auth window as the
# always-working fallback.

COOKIE_BUTTON_PATTERNS = [
    r"endast nödvändiga", r"nödvändiga (kakor|cookies)", r"godkänn",
]
# Verified flow (2026-07): journalen redirects to idp.invanar-idp.inera.se/Citizen
# ("Välj inloggningssätt": BankID / Freja+ / Foreign eID). Clicking "BankID"
# goes to /Citizen/bank-id which shows the QR code directly, with a
# "Starta BankID för att logga in" link for the this-device flow.
METHOD_PATTERNS = {
    "qr":  [r"^\s*bank[- ]?id\s*$", r"mobilt bank[- ]?id", r"qr[- ]?kod",
            r"annan enhet", r"försök igen"],
    "app": [r"starta bank[- ]?id", r"^\s*bank[- ]?id\s*$",
            r"(denna|samma|den här) (enhet|dator)", r"försök igen"],
}
# Never click these: AVBRYT cancels the pending order; "Starta BankID..."
# hijacks a QR login into the this-device flow; "Testa..."/info/guide links
# navigate away from the login; Freja+/Foreign eID are the wrong methods.
_COMMON_EXCLUDE = r"avbryt|testa|mer information|guide|läs mer|freja|foreign|support|tillgänglighet"
METHOD_EXCLUDE = {
    "qr":  re.compile(rf"starta bank[- ]?id|{_COMMON_EXCLUDE}", re.I),
    "app": re.compile(_COMMON_EXCLUDE, re.I),
}

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Animated-QR payload: bankid.<qrStartToken>.<seconds>.<hmac>
QR_PAYLOAD_RE = re.compile(r"\bbankid\.[0-9a-f-]{36}\.\d+\.[0-9a-f]{64}\b", re.I)
BANKID_URL_RE = re.compile(r"bankid:///?[^\s'\"<>\\]*", re.I)
AUTOSTART_RE  = re.compile(r"autostarttoken[\"']?\s*[:=]\s*[\"']?([0-9a-f-]{36})", re.I)
# BankID completion data / patient-info payloads carry the user's name in
# JSON — English and Swedish key spellings both occur in 1177 systems.
USER_JSON_RE  = re.compile(
    r'"(?:name|fullName|displayName|userName|patientName|patientNamn)"\s*:\s*"([^"]{4,60})"')
GIVEN_SUR_RE  = re.compile(
    r'"(?:givenName|given_name|firstName|f(?:ö|o)rnamn|tilltalsnamn)"\s*:\s*"([^"]{1,40})"[^}]{0,200}?'
    r'"(?:surname|sn|lastName|familyName|efternamn)"\s*:\s*"([^"]{1,40})"', re.I)


def install_bankid_sniffer(page: Page) -> dict:
    """Watch network responses and console messages for BankID material the
    page handles internally but never puts in the DOM: the bankid:// launch
    URL (headless Chromium refuses to launch it and logs it to the console),
    the autoStartToken, and the animated-QR payload."""
    state = {"bankid_url": None, "autostart_token": None,
             "qr_payload": None, "qr_seen": 0.0, "user_name": ""}

    def scan_text(text: str) -> None:
        if m := BANKID_URL_RE.search(text):
            state["bankid_url"] = m.group(0)
        if m := AUTOSTART_RE.search(text):
            state["autostart_token"] = m.group(1)
        if m := QR_PAYLOAD_RE.search(text):
            state["qr_payload"] = m.group(0)
            state["qr_seen"] = time.monotonic()
        if not state["user_name"]:
            if (m := USER_JSON_RE.search(text)) and NAME_SHAPE.fullmatch(m.group(1).strip()):
                state["user_name"] = m.group(1).strip()
            elif m := GIVEN_SUR_RE.search(text):
                state["user_name"] = f"{m.group(1).strip()} {m.group(2).strip()}"

    async def on_response(resp) -> None:
        try:
            if resp.request.resource_type not in ("xhr", "fetch", "document"):
                return
            ctype = resp.headers.get("content-type", "")
            if not any(t in ctype for t in ("json", "text", "javascript")):
                return
            body = await resp.text()
            if len(body) < 200_000:
                scan_text(body)
        except Exception:
            pass

    page.on("response", on_response)
    page.on("console", lambda msg: scan_text(msg.text))
    return state

QR_ELEMENT_SELECTORS = [
    "img[src*='qr' i]", "img[alt*='qr' i]",
    "[class*='qr' i] img", "[class*='qr' i] canvas",
    "[id*='qr' i] img", "[id*='qr' i] canvas",
    "canvas", "img[src^='data:image']",
]


async def do_login_window(page: Page) -> None:
    """Original flow: the user completes BankID in the visible browser."""
    say("[dim]Opening journalen.1177.se…[/]")
    await page.goto(f"{JOURNALEN_URL}/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    log.info(f"Redirected to: {page.url}")
    say("Complete BankID login in the browser window [dim](2 min timeout)[/]")
    await page.wait_for_url(LOGIN_SUCCESS_RE, timeout=LOGIN_TIMEOUT)
    say(f"[green]✓ Logged in[/] [dim]{page.url}[/]")


async def try_advance_login(page: Page, mode: str, clicked: set[str]) -> None:
    """Best-effort: dismiss cookie banners and click login-method buttons.
    Called repeatedly while waiting, so a missed click self-heals. `clicked`
    remembers what was already clicked on which URL — clicking the same
    element twice never helps and previously caused an infinite click loop."""
    for patterns, is_method in ((COOKIE_BUTTON_PATTERNS, False),
                                (METHOD_PATTERNS[mode], True)):
        for pat in patterns:
            regex = re.compile(pat, re.I)
            for loc in (page.get_by_role("button", name=regex),
                        page.get_by_role("link", name=regex)):
                try:
                    if not (await loc.count() and await loc.first.is_visible()):
                        continue
                    name = await loc.first.evaluate(
                        "el => (el.innerText || el.getAttribute('aria-label') || '').trim()"
                    )
                    if is_method and METHOD_EXCLUDE[mode].search(name):
                        continue
                    key = f"{page.url}::{pat}::{name}"
                    if key in clicked:
                        continue
                    clicked.add(key)
                    log.info(f"Clicking {name!r} (matched /{pat}/)...")
                    await loc.first.click(timeout=3_000)
                    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
                    if not is_method:
                        break  # cookie banner handled, move on to method
                    return     # one method click per round is enough
                except Exception:
                    continue
            else:
                continue
            break


async def dump_login_page(page: Page) -> None:
    """Diagnostics for a stuck login: log every visible button/link and save
    a screenshot, so the selector patterns can be fixed from real data."""
    log.warning(f"Login not progressing on: {page.url}")
    try:
        items = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a, button')).map(el => ({
                tag: el.tagName.toLowerCase(),
                text: (el.innerText || '').trim().slice(0, 80),
                aria: el.getAttribute('aria-label'),
                href: el.getAttribute('href'),
                visible: !!(el.offsetWidth || el.offsetHeight),
            })).filter(i => i.visible)"""
        )
        log.warning(f"Visible buttons/links ({len(items)}):")
        for it in items[:40]:
            log.warning(f"  <{it['tag']}> text={it['text']!r} aria={it['aria']!r} href={it['href']!r}")
    except Exception as e:
        log.warning(f"Could not enumerate elements: {e}")
    try:
        path = OUTPUT_DIR / "debug_login.png"
        await page.screenshot(path=str(path), full_page=True)
        log.warning(f"Screenshot saved: {path}")
    except Exception as e:
        log.warning(f"Could not screenshot: {e}")


def decode_qr_png(png: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(png)).convert("RGB")
        results = zxingcpp.read_barcodes(img)
    except Exception:
        return None
    for r in results:
        if r.text:
            return r.text
    return None


async def extract_qr_payload(page: Page) -> str | None:
    """Screenshot candidate QR elements and decode them. Screenshotting works
    for both <img> and <canvas>, however the page draws the code."""
    for sel in QR_ELEMENT_SELECTORS:
        loc = page.locator(sel)
        try:
            count = min(await loc.count(), 3)
        except Exception:
            continue
        for i in range(count):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                png = await el.screenshot(timeout=2_000)
            except Exception:
                continue
            text = decode_qr_png(png)
            if text and text.startswith("bankid."):
                return text
    return None


def qr_terminal_lines(payload: str, invert: bool) -> list[str]:
    """Render a QR as unicode half-blocks, two modules per character row.
    On a dark terminal the non-inverted rendering ("light" modules as blocks)
    gives the correct black-on-white polarity; --qr-invert flips it."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    width = len(matrix[0])
    blocks = {(False, False): "█", (True, False): "▄", (False, True): "▀", (True, True): " "}
    lines = []
    for y in range(0, len(matrix), 2):
        top = matrix[y]
        bot = matrix[y + 1] if y + 1 < len(matrix) else [False] * width
        line = "".join(
            blocks[(top[x] != invert, bot[x] != invert)] for x in range(width)
        )
        lines.append(line)
    return lines


async def do_login_qr(page: Page, invert: bool, sniff: dict) -> None:
    """Headless: poll the page for the animated BankID QR and mirror it in
    the terminal until the login succeeds."""
    log.info("Waiting for the BankID QR code...")
    deadline = time.monotonic() + LOGIN_TIMEOUT / 1000
    prev_payload = None
    printed = 0
    clicked: set[str] = set()
    quiet_ticks = 0
    dumped = False

    while time.monotonic() < deadline:
        if LOGIN_SUCCESS_RE.search(page.url):
            if TUI.app is not None:
                TUI.app.tui_qr(None)
            elif printed:  # erase the QR block — it has served its purpose
                sys.stdout.write(f"\x1b[{printed}F\x1b[0J")
                sys.stdout.flush()
            say(f"[green]✓ Logged in[/] [dim]{page.url}[/]")
            return

        # Sniffed payload (from the IdP's own polling responses) beats
        # screenshot-decoding; only trust it while fresh.
        if sniff["qr_payload"] and time.monotonic() - sniff["qr_seen"] < 5:
            payload = sniff["qr_payload"]
        else:
            payload = await extract_qr_payload(page)
        if payload is None:
            n_clicked = len(clicked)
            await try_advance_login(page, "qr", clicked)
            quiet_ticks = 0 if len(clicked) > n_clicked else quiet_ticks + 1
            if quiet_ticks >= 10 and not dumped:  # ~10 s with no QR and nothing new to click
                dumped = True
                await dump_login_page(page)
        elif payload != prev_payload:
            prev_payload = payload
            lines = qr_terminal_lines(payload, invert)
            out = ["Scan with the BankID app on your phone (the code refreshes automatically):", *lines]
            if TUI.app is not None:
                TUI.app.tui_qr(out)
            else:
                if printed:
                    sys.stdout.write(f"\x1b[{printed}F")
                for line in out:
                    sys.stdout.write(line + "\x1b[K\n")
                sys.stdout.flush()
                printed = len(out)

        await page.wait_for_timeout(1_000)

    if TUI.app is not None:
        TUI.app.tui_qr(None)
    raise RuntimeError(
        "BankID login timed out. If no QR appeared, the IdP page layout may "
        "have changed — retry with --auth window."
    )


async def find_bankid_launch_url(page: Page, sniff: dict, tick: int) -> str | None:
    """Best available source for the bankid:// launch URL, in order of trust:
    a real DOM anchor, the sniffed launch URL (console/network), a sniffed
    autoStartToken, and finally the UUID the IdP puts in the URL fragment."""
    try:
        loc = page.locator("a[href^='bankid:']")
        if await loc.count():
            href = await loc.first.get_attribute("href")
            if href:
                return href
    except Exception:
        pass
    if sniff["bankid_url"]:
        return sniff["bankid_url"]
    if sniff["autostart_token"]:
        return f"bankid:///?autostarttoken={sniff['autostart_token']}&redirect=null"
    # Last resort, and only after the sniffer has had a few seconds:
    fragment = page.url.split("#", 1)[1] if "#" in page.url else ""
    if tick > 10 and UUID_RE.match(fragment):
        log.info("Using the URL-fragment UUID as autostart token (last resort).")
        return f"bankid:///?autostarttoken={fragment}&redirect=null"
    return None


async def do_login_app(page: Page, sniff: dict) -> None:
    """Headless: recover the bankid:// launch URL the page tried to open
    itself, and open the BankID app on this machine with it."""
    log.info("Looking for the BankID autostart URL...")
    href = None
    clicked: set[str] = set()
    quiet_ticks = 0
    for tick in range(80):
        if LOGIN_SUCCESS_RE.search(page.url):
            say(f"[green]✓ Logged in[/] [dim]{page.url}[/]")
            return
        href = await find_bankid_launch_url(page, sniff, tick)
        if href:
            break
        n_clicked = len(clicked)
        await try_advance_login(page, "app", clicked)
        quiet_ticks = 0 if len(clicked) > n_clicked else quiet_ticks + 1
        if quiet_ticks == 20:  # ~10 s with no URL and nothing new to click
            await dump_login_page(page)
        await page.wait_for_timeout(500)

    if not href:
        raise RuntimeError(
            "No bankid:// launch URL found — see the dump above; "
            "try --auth qr or --auth window."
        )

    say("[bold]Opening the BankID app on THIS computer[/] — approve the login there.\n"
        "[dim](To scan a QR with your mobile instead, run without --auth app.)[/]")
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, href], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await page.wait_for_url(LOGIN_SUCCESS_RE, timeout=LOGIN_TIMEOUT)
    say(f"[green]✓ Logged in[/] [dim]{page.url}[/]")


NAME_RE = re.compile(r"inloggad(?:\s+som)?:?\s*\n?\s*([^\n]{2,60})", re.I)
# Two+ words, EACH starting uppercase: "Sven Svensson" passes, Swedish
# sentence-case UI labels ("Dina vårdkontakter") do not.
NAME_SHAPE = re.compile(
    r"[A-ZÅÄÖÜÉ][a-zåäöüé'.\-]*(?: [A-ZÅÄÖÜÉ][A-Za-zåäöüé'.\-]*)+")
NOT_A_NAME = re.compile(r"logga ut|inställningar|huvudinnehåll|e-tjänst|meny|1177", re.I)


def extract_name(body: str) -> str:
    """The 1177 header shows the logged-in name as a bare text line right
    above the 'Inställningar' / 'Logga ut' links (verified 2026-07); some
    pages use an 'Inloggad som <name>' phrasing instead."""
    lines = [line.strip() for line in body.splitlines()]
    for i, line in enumerate(lines):
        if re.fullmatch(r"(inställningar|logga ut)", line, re.I):
            for prev in reversed(lines[max(0, i - 5):i]):
                if prev and NAME_SHAPE.fullmatch(prev) and not NOT_A_NAME.search(prev):
                    return prev
    if m := NAME_RE.search(body):
        name = m.group(1).strip().strip(".,:;")
        name = re.sub(r"\s*\b(logga ut|meny|inställningar).*$", "", name, flags=re.I).strip()
        if NAME_SHAPE.fullmatch(name):
            return name
    return ""


DOM_NAME_CANDIDATES_JS = """() => {
    const out = [];
    const push = (s) => { if (s && s.trim()) out.push(s.trim()); };
    document.querySelectorAll(
        "header *, nav *, [class*='user' i], [class*='profil' i], [id*='user' i]"
    ).forEach(el => {
        if (el.children.length === 0) push(el.textContent);
        if (el.getAttribute) push(el.getAttribute('aria-label'));
    });
    return out.slice(0, 300);
}"""


# The 1177 header is a Lit web component: its avatar shows the logged-in
# name in `.ids-header-1177-avatar-content__name` (verified 2026-07, present
# on every journalen page) — but inside a SHADOW ROOT, invisible to plain
# document.querySelectorAll and excluded from body.innerText. Hence a walker
# that descends into shadow roots.
SHADOW_NAME_JS = """() => {
    const results = [];
    const collect = (root) => {
        for (const el of root.querySelectorAll(
                ".ids-header-1177-avatar-content__name, [class*='avatar'] [class*='name' i]")) {
            results.push(el.getAttribute('title') || el.textContent);
        }
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collect(el.shadowRoot);
        }
    };
    collect(document);
    return results.map(s => (s || '').trim()).filter(Boolean).slice(0, 20);
}"""

ARIA_LABELS_JS = ("() => [...document.querySelectorAll('[aria-label],[title]')]"
                  ".map(e => e.getAttribute('aria-label') || e.getAttribute('title'))")


def _name_from_json_blob(text: str) -> str:
    if (m := USER_JSON_RE.search(text)) and NAME_SHAPE.fullmatch(m.group(1).strip()):
        return m.group(1).strip()
    if m := GIVEN_SUR_RE.search(text):
        return f"{m.group(1).strip()} {m.group(2).strip()}"
    return ""


async def get_logged_in_name(page: Page, sniff: dict | None = None) -> str:
    """Best-effort, in order: BankID data seen by the sniffer, header/nav
    elements, every aria-label/title in the document, page text heuristics,
    JSON embedded in the HTML, and finally the sniffer again after the
    page's own XHRs settle. Cosmetic plus profile input — '' never blocks.

    journalen's Dashboard shows no name in its body text (verified 2026-07),
    hence the wide net."""
    if sniff and sniff.get("user_name"):
        log.info(f"Logged in as (from BankID data): {sniff['user_name']}")
        return sniff["user_name"]
    try:
        for cand in await page.evaluate(SHADOW_NAME_JS):
            if NAME_SHAPE.fullmatch(cand) and not NOT_A_NAME.search(cand):
                log.info(f"Logged in as (from 1177 header avatar): {cand}")
                return cand
        for cand in await page.evaluate(DOM_NAME_CANDIDATES_JS):
            if NAME_SHAPE.fullmatch(cand) and not NOT_A_NAME.search(cand):
                log.info(f"Logged in as (from page header): {cand}")
                return cand
        arias = [a.strip() for a in await page.evaluate(ARIA_LABELS_JS) if a]
        for a in arias:
            if m := re.search(r"inloggad(?:\s+som)?:?\s*(.+)", a, re.I):
                cand = m.group(1).strip().strip(".,:;")
                if NAME_SHAPE.fullmatch(cand):
                    log.info(f"Logged in as (from aria-label): {cand}")
                    return cand
            if NAME_SHAPE.fullmatch(a) and not NOT_A_NAME.search(a):
                log.info(f"Logged in as (from aria-label): {a}")
                return a
        body = await page.evaluate("() => document.body.innerText")
        if name := extract_name(body):
            log.info(f"Logged in as (from page text): {name}")
            return name
        if name := _name_from_json_blob(await page.content()):
            log.info(f"Logged in as (from embedded JSON): {name}")
            return name
    except Exception:
        return ""
    # The Dashboard's own XHRs may deliver patient info just after login.
    if sniff is not None:
        try:
            await page.wait_for_timeout(2_500)
        except Exception:
            pass
        if sniff.get("user_name"):
            log.info(f"Logged in as (from late XHR): {sniff['user_name']}")
            return sniff["user_name"]
    # Diagnostics: everything name-shaped we saw, for pinning the pattern.
    shaped = [a for a in arias if NAME_SHAPE.fullmatch(a)][:15]
    hints = [ln for ln in body.splitlines()
             if re.search(r"inloggad|logga ut", ln, re.I)][:5]
    log.info("Could not find a logged-in name. "
             f"Name-shaped aria-labels: {shaped!r}; inloggad/logga-ut lines: {hints!r}; "
             "page text head:\n" + "\n".join(body.splitlines()[:40]))
    return ""


async def do_login(page: Page, args: Options) -> dict:
    """Logs in and returns the sniffer state (may hold the user's name from
    the BankID completion data). Empty dict for the manual window flow."""
    if args.auth == "window":
        await do_login_window(page)
        return {}
    say(f"[dim]Opening journalen.1177.se… (auth: {args.auth})[/]")
    sniff = install_bankid_sniffer(page)
    await page.goto(f"{JOURNALEN_URL}/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    log.info(f"Redirected to: {page.url}")
    if args.auth == "app":
        await do_login_app(page, sniff)
    else:
        await do_login_qr(page, args.qr_invert, sniff)
    return sniff

# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


async def goto_link(page: Page, name_re: re.Pattern, description: str) -> bool:
    """Find an anchor matching `name_re` and navigate to its href directly.
    Clicking nav links breaks headless — they can sit in a collapsed menu and
    never become 'visible' — but reading the href works regardless.
    Retries for ~10 s: right after login the Dashboard nav renders a moment
    after 'domcontentloaded', and failing instantly there broke syncs."""
    for attempt in range(20):
        for loc in (page.get_by_role("link", name=name_re),
                    page.locator("a").filter(has_text=name_re)):
            try:
                count = await loc.count()
            except Exception:
                continue
            for i in range(count):
                href = await loc.nth(i).get_attribute("href")
                if href and not href.startswith(("javascript:", "#")):
                    url = urljoin(page.url, href)
                    log.info(f"Navigating to {description}: {url}")
                    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                    return True
        await page.wait_for_timeout(500)
    return False


async def navigate_to_category(page: Page, cat: Category) -> None:
    """Follow Journalen → <category> via the nav links' hrefs."""
    if not await goto_link(page, re.compile(r"^\s*Journalen\s*$", re.I), "'Journalen'"):
        raise RuntimeError("No 'Journalen' link found.")

    if not (cat.url_fragment and cat.url_fragment in page.url):
        cat_re = re.compile(re.escape(cat.title), re.I)
        if not await goto_link(page, cat_re, f"'{cat.title}'"):
            raise RuntimeError(f"No '{cat.title}' link found.")

    log.info(f"{cat.title} URL: {page.url}")
    if any(x in page.url for x in ["LoggedOut", "NotFound", "login"]):
        raise RuntimeError(f"Navigation failed: {page.url}")


async def load_all_records(page: Page) -> int:
    """
    Wait for all records to appear in the DOM.

    The page first renders 10 records with 'Visa 10 till' / 'Visa alla' controls.
    Clicking 'Visa alla' triggers multiple AJAX requests that progressively add
    <li> elements. We wait until the DOM count matches total-number.

    Returns the total record count.
    """
    log.info("Waiting for initial records to render...")
    try:
        await page.wait_for_selector(
            "#nc-list-posts li:not(.nc-loading-spinner-row)",
            timeout=NAV_TIMEOUT,
        )
    except Exception:
        log.error("Records never appeared.")
        return 0

    total_str = await page.locator("[data-cy-id='total-number']").get_attribute("data-cy-value")
    total = int(total_str or "0")
    log.info(f"Total records: {total}")

    if total == 0:
        return 0

    # Click 'Visa alla' if present; otherwise fall back to 'Visa 10 till' loop
    load_all = page.locator("button.load-all")
    load_more = page.locator("button.load-more")

    if await load_all.is_visible() and not await load_all.is_disabled():
        log.info("Clicking 'Visa alla' to request all records...")
        await load_all.click()
    elif await load_more.is_visible() and not await load_more.is_disabled():
        while await load_more.is_visible() and not await load_more.is_disabled():
            log.info("Clicking 'Visa 10 till'...")
            await load_more.click()
            await page.wait_for_load_state("networkidle", timeout=15_000)

    # Wait until all <li> elements are in the DOM (up to 60 s)
    log.info(f"Waiting for all {total} records to appear in the DOM...")
    try:
        await page.wait_for_function(
            f"""() => document.querySelectorAll(
                '#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row)'
            ).length >= {total}""",
            timeout=60_000,
        )
    except Exception:
        actual = await page.locator(
            "#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row)"
        ).count()
        log.warning(f"Timeout — only {actual}/{total} records loaded. Proceeding with {actual}.")
        total = actual

    log.info(f"All {total} records loaded in DOM.")
    return total


def parse_expander_label(label: str) -> tuple[str, str, str, str]:
    """Extract date, record_type, author, facility from the button aria-label.

    The field names are verified for Anteckningar; other categories use
    similar labels but may name the type differently, hence the generic
    fallbacks. Unparsed fields stay empty rather than guessing wrong.
    """
    date        = re.search(r"Datum\s+([\d-]+)", label) \
                  or re.search(r"(\d{4}-\d{2}-\d{2})", label)
    record_type = re.search(
        r"\b(?:anteckningstyp|diagnos|läkemedel|vaccination|typ)\s+([^,]+)", label, re.I)
    # Author runs until the profession parenthesis / facility / trailing help
    # text — Läkemedel labels contain commas inside the name itself
    # ("antecknad av Wilger, Sophia, ST-läkare, wis039, (Läkare), på ...").
    author      = re.search(
        r"antecknad av\s+(.+?)(?=,\s*\(|,?\s+på\s|[.,]\s*Klicka|$)", label, re.I) \
                  or re.search(r"\bav\s+([^,]+)", label, re.I)
    facility    = re.search(r"på\s+([^,.]+)", label, re.I)
    return (
        date.group(1).strip()        if date        else "",
        record_type.group(1).strip() if record_type else "",
        author.group(1).strip()      if author      else "",
        facility.group(1).strip()    if facility    else "",
    )


async def read_record_metadata(page: Page, index: int, cat: Category) -> Record:
    """Read a record's metadata from the expander button attributes,
    WITHOUT clicking — no AJAX request is triggered."""
    expander_sel = (
        "#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row) "
        "button.nc-list-post-expander"
    )
    btn = page.locator(expander_sel).nth(index)

    label     = await btn.get_attribute("aria-label") or ""
    record_id = await btn.get_attribute("data-id") or ""
    date      = await btn.get_attribute("data-date") or ""
    parsed_date, record_type, author, facility = parse_expander_label(label)

    # DOM facts are only verified for Anteckningar; surface one raw label per
    # category when parsing comes up short, so the patterns can be fixed.
    if cat.slug != "anteckningar" and not record_type and cat.slug not in _label_warned:
        _label_warned.add(cat.slug)
        log.warning(f"Could not fully parse a {cat.title} label — raw aria-label: {label!r}")

    return Record(
        date        = parsed_date or date,
        record_type = record_type,
        author      = author,
        facility    = facility,
        record_id   = record_id,
        category    = cat.slug,
    )


_label_warned: set[str] = set()


async def expand_and_extract(page: Page, rec: Record, index: int) -> Record:
    """
    Click record at `index`, wait for its AJAX content to load, fill in
    `content_md`. The detail container starts empty with class 'nu-hidden'.
    After clicking the expander button, the content is injected and
    'nu-hidden' is removed.
    """
    expander_sel = (
        "#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row) "
        "button.nc-list-post-expander"
    )
    btn = page.locator(expander_sel).nth(index)

    # Click to load detail
    await btn.click()

    # The sibling div loses 'nu-hidden' once content arrives
    container = btn.locator("xpath=following-sibling::div[contains(@class,'nc-list-post-container')]")
    try:
        await page.wait_for_function(
            f"""() => {{
                const btns = document.querySelectorAll(
                    '#nc-list-posts li.nc-list-post:not(.nc-loading-spinner-row) button.nc-list-post-expander'
                );
                const btn = btns[{index}];
                if (!btn) return false;
                const div = btn.nextElementSibling;
                return div && !div.classList.contains('nu-hidden') && div.innerHTML.trim().length > 0;
            }}""",
            timeout=EXPAND_TIMEOUT,
        )
    except Exception:
        log.warning(f"    Timeout expanding record {index+1}. Content may be empty.")

    content_html = await container.inner_html()
    rec.content_md = md_convert(content_html).strip() if content_html.strip() else ""
    rec.scraped_at = TODAY

    # Collapse before moving to next (keeps DOM clean)
    if await btn.get_attribute("aria-expanded") == "true":
        await btn.click()

    return rec


async def sync_records(
    page: Page,
    total: int,
    master: dict[str, Record],
    args: Options,
    cat: Category,
) -> tuple[int, int, int]:
    """
    Walk every record in the list, but only expand (= one AJAX request each)
    those that pass the date filter and, in incremental mode, are not
    already in the store. Returns (fetched, skipped_seen, skipped_range).
    """
    fetched = skipped_seen = skipped_range = 0
    occ: dict[str, int] = defaultdict(int)  # per-page occurrence -> Record.seq

    progress = task = None
    if TUI.app is None:
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[dim]{task.completed}/{task.total}[/]"),
            console=console,
            transient=True,
        )
        progress.start()
        task = progress.add_task(cat.title, total=total)
    try:
        for i in range(total):
            rec = await read_record_metadata(page, i, cat)
            rec.seq = occ[rec.meta_key()]
            occ[rec.meta_key()] += 1
            desc = f"{cat.title}  [dim]{rec.date}[/]"
            if progress is not None:
                progress.update(task, advance=1, description=desc)
            else:
                TUI.app.tui_progress(i + 1, total, desc)
            tag = f"[{cat.title} {i+1}/{total}] {rec.date} — {rec.record_type or cat.title}"

            if not in_date_range(rec.date, args.since, args.until):
                skipped_range += 1
                continue

            if not args.full and rec.key() in master:
                skipped_seen += 1
                continue

            if args.dry_run:
                say(f"  [cyan]would fetch[/] {rec.date} — {rec.record_type or cat.title}")
                fetched += 1
                continue

            log.info(f"  {tag}")
            rec = await expand_and_extract(page, rec, i)
            master[rec.key()] = rec
            fetched += 1
    finally:
        if progress is not None:
            progress.stop()
        elif TUI.app is not None:
            TUI.app.tui_progress(total, total, "")  # hides the bar

    parts = [f"[green]{fetched} fetched[/]" if fetched else "[dim]0 fetched[/]"]
    if skipped_seen:
        parts.append(f"[dim]{skipped_seen} already stored[/]")
    if skipped_range:
        parts.append(f"[dim]{skipped_range} outside range[/]")
    say(f"  {cat.title}: " + ", ".join(parts))
    return fetched, skipped_seen, skipped_range

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render_markdown(records: dict[str, Record]) -> None:
    """Regenerate all per-date Markdown files and the index from the store.
    Pure function of journal.json, so files are always consistent."""
    if not records:
        log.warning("Store is empty — nothing to render.")
        return

    # Per-date files used to live directly in md/; they now live in
    # md/<category>/. Remove stale top-level copies (pure derived output,
    # regenerated below) so there are no duplicates.
    for stale in MD_DIR.glob("*.md"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stale.stem):
            stale.unlink()
            log.info(f"Removed stale {stale} (now under md/<category>/)")

    by_cat_date: dict[tuple[str, str], list[Record]] = defaultdict(list)
    for rec in records.values():
        by_cat_date[(rec.category, rec.date or "unknown-date")].append(rec)

    for (cat_slug, date), day_records in sorted(by_cat_date.items()):
        title     = CATEGORIES[cat_slug].title if cat_slug in CATEGORIES else cat_slug
        safe_date = date.replace("/", "-")
        md_path   = MD_DIR / cat_slug / f"{safe_date}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# {title} — {date}\n\n")
            f.write(f"**Exported:** {TODAY}  \n")
            f.write(f"**Records this day:** {len(day_records)}\n\n---\n\n")

            for i, rec in enumerate(day_records, 1):
                f.write(f"## {i}. {rec.record_type or title}\n\n")
                f.write(f"**Date:** {rec.date}  \n")
                if rec.author:
                    f.write(f"**By:** {rec.author}  \n")
                if rec.facility:
                    f.write(f"**Provider:** {rec.facility}  \n")
                f.write("\n")
                f.write(rec.content_md if rec.content_md else "*(no content extracted)*")
                f.write("\n\n---\n\n")

    index_path = MD_DIR / "index.md"
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("# Journal index\n\n")
        f.write(f"**Updated:** {TODAY} — {len(records)} records, "
                f"{len(by_cat_date)} category-dates\n\n")
        f.write("| Date | Category | Type | By | Provider |\n")
        f.write("|---|---|---|---|---|\n")
        for cat_slug, date in sorted(by_cat_date, key=lambda k: k[1], reverse=True):
            title = CATEGORIES[cat_slug].title if cat_slug in CATEGORIES else cat_slug
            for rec in by_cat_date[(cat_slug, date)]:
                f.write(f"| [{date}]({cat_slug}/{date}.md) | {title} "
                        f"| {rec.record_type} | {rec.author} | {rec.facility} |\n")

    log.info(f"Rendered {len(by_cat_date)} date files and {index_path}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def open_browser(p, args) -> tuple:
    browser = await p.chromium.launch(headless=args.auth != "window")
    # Desktop-size viewport so the site renders its full navigation
    context = await browser.new_context(viewport={"width": 1920, "height": 1080})
    return browser, await context.new_page()


async def run_sync(page: Page, master: dict[str, Record],
                   args: Options) -> tuple[int, int, int]:
    fetched = seen = out_of_range = 0
    if args.full:
        say("[yellow]Full mode:[/] already-stored records will be re-fetched")
    if args.since or args.until:
        say(f"[dim]Date filter: {args.since or '…'} → {args.until or '…'}[/]")
    log.info(f"Categories: {', '.join(c.title for c in args.categories)}")
    for cat in args.categories:
        rule(cat.title)
        log.info(f"--- {cat.title} ---")
        await navigate_to_category(page, cat)
        total = await load_all_records(page)
        if total > 0:
            f, s, r = await sync_records(page, total, master, args, cat)
            fetched += f
            seen += s
            out_of_range += r
    return fetched, seen, out_of_range


def finish_sync(master: dict[str, Record], args: Options,
                stats: tuple[int, int, int]) -> None:
    fetched, seen, out_of_range = stats
    rule(style="dim")
    if args.dry_run:
        say(f"[cyan]Dry run:[/] {fetched} record(s) would be fetched "
            f"[dim]({seen} already stored, {out_of_range} outside date range) "
            f"— nothing was saved[/]")
        return
    save_master(master)
    render_markdown(master)
    say(f"[green bold]✓ Done[/] — {fetched} new record(s) fetched "
        f"[dim]({seen} already stored, {out_of_range} outside date range) "
        f"· store: {len(master)} records[/]")

# ---------------------------------------------------------------------------

def store_summary(master: dict[str, Record]) -> str:
    if not master:
        return "store is empty"
    newest = max(r.date for r in master.values())
    n = len(master)
    return f"{n} record{'s' if n != 1 else ''}, newest {newest}"


def status_table(master: dict[str, Record]) -> Table:
    table = Table(title=f"[dim]{MASTER_JSON}[/]", border_style="dim",
                  title_justify="left")
    table.add_column("Category", style="cyan")
    table.add_column("Records", justify="right")
    table.add_column("Newest", style="dim")
    table.add_column("Last sync", style="dim")
    per_cat: dict[str, list[Record]] = defaultdict(list)
    for rec in master.values():
        per_cat[rec.category].append(rec)
    for slug, recs in sorted(per_cat.items()):
        title = CATEGORIES[slug].title if slug in CATEGORIES else slug
        newest = max(r.date for r in recs)
        synced = max((r.scraped_at for r in recs if r.scraped_at), default="never")
        table.add_row(title, str(len(recs)), newest, synced)
    return table


class TuiLogHandler(logging.Handler):
    """Routes warnings/errors into the TUI's log widget while the app runs."""
    def emit(self, record: logging.LogRecord) -> None:
        if TUI.app is not None:
            try:
                color = "red" if record.levelno >= logging.ERROR else "yellow"
                TUI.app.tui_say(f"[{color}]{record.getMessage()}[/]")
            except Exception:
                pass


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no modal."""
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel", show=False)]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Yes", variant="primary", id="yes")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class DateRangeScreen(ModalScreen["tuple[str | None, str | None] | None"]):
    """Asks for an optional since/until date pair."""
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Sync a date range (YYYY-MM-DD, blank = no limit)")
            yield Input(placeholder="From: 2026-01-01", id="since")
            yield Input(placeholder="To:   2026-12-31", id="until")
            yield Label("", id="dialog-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Sync", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        since = self.query_one("#since", Input).value.strip() or None
        until = self.query_one("#until", Input).value.strip() or None
        try:
            since = iso_date(since) if since else None
            until = iso_date(until) if until else None
            if since and until and since > until:
                raise ValueError(f"{since} is after {until}")
        except ValueError as e:
            self.query_one("#dialog-error", Label).update(str(e))
            return
        self.dismiss((since, until))


class RecordScreen(Screen):
    """One record, rendered as scrollable Markdown."""
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, rec: Record) -> None:
        super().__init__()
        self.rec = rec

    def compose(self) -> ComposeResult:
        rec = self.rec
        meta = " · ".join(x for x in (rec.author, rec.facility) if x)
        yield Static(
            f"[bold]{rec.date} — {rec.record_type or '?'}[/]"
            + (f"\n[dim]{meta}[/]" if meta else ""),
            id="record-meta")
        with VerticalScroll():
            yield MarkdownViewer(rec.content_md or "*(no content)*")
        yield Footer()


class BrowseScreen(Screen):
    """All records, newest first; Enter opens one."""
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, master: dict[str, Record]) -> None:
        super().__init__()
        self.master = master

    def compose(self) -> ComposeResult:
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Date", "Category", "Type", "Provider")
        recs = sorted(self.master.values(), key=lambda r: r.date, reverse=True)
        for rec in recs:
            title = CATEGORIES.get(rec.category, CATEGORIES["anteckningar"]).title
            table.add_row(rec.date, title, rec.record_type or "-", rec.facility,
                          key=rec.key())
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        rec = self.master.get(event.row_key.value)
        if rec:
            self.app.push_screen(RecordScreen(rec))


class App1177(App):
    """The interactive TUI: menu, sync workers, QR display, record browser."""

    TITLE = "1177"
    SUB_TITLE = f"v{__version__}"
    CSS = """
    #hdr { margin: 1 2 0 2; padding: 0 2; border: round $primary; height: auto; }
    #menu { margin: 0 2; height: auto; border: round $panel; }
    #qr { margin: 0 2; width: 100%; height: auto; display: none; text-align: center; }
    #progress { margin: 0 3; display: none; }
    #log { margin: 0 2 1 2; padding: 0 1; border: round $panel; height: 1fr; }
    #record-meta { margin: 1 2 0 2; height: auto; }
    ConfirmScreen, DateRangeScreen { align: center middle; }
    #dialog { width: 56; height: auto; border: round $primary;
              padding: 1 2; background: $surface; }
    .dialog-buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #dialog-error { color: $text-error; height: auto; }
    #dialog Input { margin-top: 1; }
    """
    BINDINGS = (
        [Binding(str(i), f"pick('sync-{slug}')", f"Sync {slug}", show=False)
         for i, slug in enumerate(CATEGORIES, 1)]
        + [
            Binding("a", "pick('sync-all')", "Sync all"),
            Binding("d", "pick('range')", "Date range", show=False),
            Binding("f", "pick('full')", "Full re-sync", show=False),
            Binding("b", "pick('browse')", "Browse"),
            Binding("s", "pick('status')", "Status", show=False),
            Binding("l", "pick('login')", "Log in"),
            Binding("o", "pick('logout')", "Log out", show=False),
            Binding("q", "quit", "Quit"),
        ]
    )

    def __init__(self, opts: Options, master: dict[str, Record]) -> None:
        super().__init__()
        self.opts = opts
        self.master = master
        self.pw = self.browser = self.page = None
        self.logged_in = False
        self.user_name = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="hdr")
        items = [Option(f"Sync {cat.title}", id=f"sync-{slug}")
                 for slug, cat in CATEGORIES.items()]
        items += [
            Option("Sync all categories", id="sync-all"),
            Option("Sync a date range…", id="range"),
            Option("Full re-sync (re-fetch everything)", id="full"),
            Option("Browse local records", id="browse"),
            Option("Store status", id="status"),
            Option("Log in now", id="login"),
            Option("Log out / switch user", id="logout"),
        ]
        yield OptionList(*items, id="menu")
        yield Static(id="qr")
        yield ProgressBar(id="progress", show_eta=False)
        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        TUI.app = self
        self.refresh_header()

    # -- hooks used by the scraping core ------------------------------------

    def tui_say(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def tui_progress(self, done: int, total: int, label: str) -> None:
        bar = self.query_one("#progress", ProgressBar)
        bar.display = 0 < done < total
        bar.update(total=total, progress=done)

    def tui_qr(self, lines: list[str] | None) -> None:
        qr = self.query_one("#qr", Static)
        if lines:
            qr.update(Text("\n".join(lines)))
            qr.display = True
        else:
            qr.update("")
            qr.display = False

    # -- actions -------------------------------------------------------------

    def refresh_header(self) -> None:
        if self.logged_in:
            who = f"[bold green]● {self.user_name or 'logged in'}[/]"
        elif STORE_OWNER:  # showing stored data, not an identity claim
            who = f"[dim]profile: {STORE_OWNER} · not logged in[/]"
        else:
            who = "[dim]not logged in[/]"
        self.query_one("#hdr", Static).update(
            f"[bold cyan]⚕ 1177[/]\n\n"
            f"  {who}\n"
            f"  [dim]{store_summary(self.master) if self.master else 'log in to load your records'}[/]"
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_pick(event.option.id)

    def action_pick(self, oid: str) -> None:
        if oid in ("browse", "status") and not self.master:
            self.tui_say("[dim]No records loaded — log in first (l).[/]")
        elif oid == "browse":
            self.push_screen(BrowseScreen(self.master))
        elif oid == "status":
            self.query_one("#log", RichLog).write(status_table(self.master))
        else:
            self.run_menu_action(oid)

    @work(exclusive=True)
    async def run_menu_action(self, oid: str) -> None:
        all_cats = list(CATEGORIES.values())
        if oid == "login":
            if self.logged_in:
                await self.log_out()  # switching users: drop the old session first
            await self.try_login()
        elif oid == "logout":
            await self.log_out()
        elif oid == "range":
            result = await self.push_screen_wait(DateRangeScreen())
            if result:
                await self.do_sync(since=result[0], until=result[1],
                                   categories=all_cats)
        elif oid == "full":
            if await self.push_screen_wait(ConfirmScreen("Re-fetch everything?")):
                await self.do_sync(full=True, categories=all_cats)
        elif oid == "sync-all":
            await self.do_sync(categories=all_cats)
        elif oid.startswith("sync-"):
            await self.do_sync(categories=[CATEGORIES[oid[5:]]])

    async def log_out(self) -> None:
        """Drop the browser session so the next login can be someone else.
        Their profile is picked automatically after they authenticate."""
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
        self.pw = self.browser = self.page = None
        was = self.logged_in
        self.logged_in = False
        self.user_name = ""
        self.refresh_header()
        if was:
            say("[dim]Logged out — the next login can be a different person.[/]")

    async def try_login(self) -> bool:
        try:
            await self.ensure_session()
            return True
        except Exception as e:
            log.error(f"Login failed: {e}")
            self.logged_in = False
            self.refresh_header()
            return False

    async def ensure_session(self) -> None:
        if self.page is None:
            self.pw = await async_playwright().start()
            self.browser, self.page = await open_browser(self.pw, self.opts)
        if not self.logged_in:
            sniff = await do_login(self.page, self.opts)
            self.logged_in = True
            self.user_name = await get_logged_in_name(self.page, sniff)
            switch_profile_for(self.user_name, self.master)  # raises on mismatch
            self.refresh_header()

    async def do_sync(self, **overrides) -> None:
        run_args = replace(self.opts, **overrides)
        try:
            await self.ensure_session()
            stats = await run_sync(self.page, self.master, run_args)
        except Exception as e:
            log.error(f"Sync failed: {e}")
            self.logged_in = False  # session may have expired; re-login next time
            self.refresh_header()
            return
        finish_sync(self.master, run_args, stats)
        self.refresh_header()


async def interactive_main(args: Options, master: dict[str, Record]) -> None:
    app = App1177(args, master)
    root = logging.getLogger()
    tui_handler = TuiLogHandler(level=logging.WARNING)
    if CONSOLE_LOG_HANDLER:
        root.removeHandler(CONSOLE_LOG_HANDLER)
    root.addHandler(tui_handler)
    try:
        await app.run_async()
    finally:
        TUI.app = None
        root.removeHandler(tui_handler)
        if CONSOLE_LOG_HANDLER:
            root.addHandler(CONSOLE_LOG_HANDLER)
        if app.browser:
            await app.browser.close()
        if app.pw:
            await app.pw.stop()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: Options) -> None:
    master = load_master() if ACTIVE_PROFILE else {}
    if master and not args.interactive:  # the menu header shows this already
        say(f"[dim]Local store: {store_summary(master)}[/]")

    if args.interactive:
        await interactive_main(args, master)
        return

    async with async_playwright() as p:
        browser, page = await open_browser(p, args)
        try:
            sniff = await do_login(page, args)
            if name := await get_logged_in_name(page, sniff):
                say(f"[dim]Logged in as {name}[/]")
            switch_profile_for(name, master)  # raises on owner mismatch
            stats = await run_sync(page, master, args)
        except Exception as e:
            log.error(f"Fatal error: {e}", exc_info=True)
            raise
        finally:
            await browser.close()

    finish_sync(master, args, stats)


def cli() -> None:
    """Console-script entry point (`1177` after `uv tool install`)."""
    try:
        run(tyro.cli(Cli, prog="1177"))
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)


if __name__ == "__main__":
    cli()
