# AGENTS.md — operating `1177` as an AI agent

Instructions for AI agents (Claude Code, etc.) that need to use this tool on
a user's behalf. For *developing* this codebase, see the README and the
comments in `scraper.py`.

## What this tool is

`1177` exports the user's personal medical journal from journalen.1177.se
(Anteckningar, Provsvar, Diagnoser, Läkemedel) into a local JSON store and
Markdown files. Authentication is Swedish BankID.

## ⚠️ Data handling — read first

The output is the user's **personal medical records**.

- **Never** send record contents to external services, APIs, or web tools.
- **Never** commit, publish, or copy the data outside its output directory.
- Quote record contents back to the user only when they ask about their own
  records, and only the minimum needed.
- The output directory is git-ignored — keep it that way.

## The two ways to use it

### 1. Reading local data (no login — prefer this)

If the user asks about their records ("what did the doctor write in March?",
"summarize my provsvar"), you do **not** need to run a sync. Read the local
store directly:

```
<data root>/<user-slug>/json/journal.json   # canonical store (schema below)
<data root>/<user-slug>/md/<category>/YYYY-MM-DD.md
<data root>/<user-slug>/md/index.md         # overview table
```

The data root is `./output` in the repo if it holds a store, otherwise
`~/.1177`. Each BankID identity has its own directory (e.g.
`sven-svensson/`) — list the root to see whose journals exist, and confirm
with the `owner` field inside each `journal.json`. Check the newest
`scraped_at` to know how fresh the data is; suggest a sync if it's stale.

`journal.json` schema:

```json
{
  "owner": "Firstname Lastname",
  "records": [
    {
      "date": "2026-05-08",
      "record_type": "Besöksanteckning",
      "author": "Dr Name",
      "facility": "Vårdcentral Name",
      "record_id": "…",
      "content_md": "…the record body as Markdown…",
      "scraped_at": "2026-07-21",
      "category": "anteckningar"
    }
  ]
}
```

### 2. Syncing fresh data (requires the human)

**You cannot complete BankID authentication.** Only the user can, by scanning
a QR code with their phone or approving in their BankID app. Never attempt to
bypass, automate, or wait out the login.

The correct flow:

1. Tell the user you're starting a sync and that a BankID QR will appear.
2. Run the sync **in the foreground where the user can see the terminal**:
   `1177 --sync` (or with flags below). The QR renders in the terminal;
   the user scans it within ~2 minutes.
3. If the user is not present, do not start a sync — read local data instead
   and tell them a sync needs their BankID.

Useful invocations (non-TTY runs are automatically non-interactive):

```bash
1177 --sync                          # incremental, anteckningar only
1177 --sync --category all           # all four categories
1177 --sync --category provsvar diagnoser   # space-separated list
1177 --since 2026-01-01              # date-bounded fetch
1177 --dry-run                       # show what a sync would fetch (still needs login)
1177 --full                          # re-fetch everything (slow; ask the user first)
1177 --output-dir DIR                # a different data directory
1177 --version / --help
```

Bare `1177` on a real terminal opens an interactive TUI meant for the human,
not for you — as an agent, always pass explicit flags.

## Multi-user caution

Each BankID identity gets its own profile directory automatically — the tool
switches to the logged-in person's profile after authentication, so records
never mix. In the TUI, "Log out / switch user" (key `o`) drops the session so
another person can log in; logging in while already logged in does the same.
Only export someone else's journal with that person's presence and consent —
they must authenticate with their own BankID.

## Troubleshooting

- Full run logs: `<data dir>/scraper.log` — read the tail after any failure.
- Login stuck: the tool auto-dumps the login page's buttons/links to the log
  and saves `<data dir>/debug_login.png`. Both are safe to inspect (pre-login
  IdP page, no medical data); the screenshot may show the user's name.
- `--auth window` opens a visible browser for manual login — the fallback
  when headless auth misbehaves.
- Site DOM/IdP facts and architecture live in the comments in `scraper.py`.

## Known limitations (as of 2026-07-21)

- If the logged-in name cannot be detected (rare — it is read from the 1177
  header's avatar component), records land in the `default/` profile and a
  diagnostic block is written to `scraper.log` — surface it to the developer.
- Record identity is metadata-based (journalen's `data-id` changes every
  session).
