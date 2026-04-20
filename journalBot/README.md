# journalBot (safe-assisted flow)

This project prepares actions on `cont.ws` and requires manual confirmation before repost/publish clicks.

## What it does

- Checks authorization state on `https://cont.ws/` for each profile run.
- Loads pending posts from Google Sheets worksheet `Posts`.
- Filters rows by `Profile ID`.
- Opens target profile latest post and prepares repost action.
- Fills new post form from sheet data and asks manual confirmation.
- Logs results to `run_log.csv`.
- Uses persistent Playwright browser profiles per `Profile ID`.
- Supports optional proxy per profile.
- Checks and logs external IP for each profile run.

## Google Sheet format

Create worksheet named `Posts` with headers:

- `Profile ID`
- `Title`
- `Body`
- `Status`

Rows with `Status` equal to `done`, `posted`, or `skip` are ignored.
Successful publish marks row status as `done`.

## Setup

1. Create and activate virtual environment.
2. Install dependencies.
3. Install Playwright browser.
4. Fill `.env`.
5. Place Google service account json file.
6. Share your Google Sheet with the service account email.

Commands:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

## Environment

Use `.env`:

```env
GOOGLE_SHEETS_ID=your_google_sheet_id
GOOGLE_SERVICE_ACCOUNT_JSON=service-account.json
PROFILES=profile_1,profile_2,profile_3,profile_4,profile_5
PROFILES_DIR=browser_profiles
TARGET_PROFILE_URL=https://cont.ws/@target_profile
HEADLESS=false
PROFILE_PROXY_MAP=profile_1=http://user:pass@127.0.0.1:8080;profile_2=socks5://127.0.0.1:9050
```

`PROFILES` values must match `Profile ID` values in the sheet.
`PROFILES_DIR` stores local browser state for each profile (`<PROFILES_DIR>/<Profile ID>`).
`PROFILE_PROXY_MAP` is optional, format:

- `profile_id=proxy_url;profile_id=proxy_url`
- proxy URL examples:
  - `http://user:pass@host:port`
  - `http://host:port`
  - `socks5://host:port`

## Run

```bash
python main.py
```

The script pauses before repost and publish actions and asks:

- `y` -> click action
- any other key -> skip

`run_log.csv` includes `external_ip` so you can verify proxy routing per profile.

## First run tip

On first run for each profile, the browser opens with empty local state. If auth check fails,
log in manually in that profile window and run script again. Session will persist in
`PROFILES_DIR/<Profile ID>`.
