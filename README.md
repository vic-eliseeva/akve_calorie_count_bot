# Duo Food Bot v4 — Telegram food diary, connected to AKVE calorie count master

**What it does:** Two people use a *single* Telegram group. Each sends food descriptions, voice messages (Russian or English), or photos. The bot calculates food calories and macros for each person separately, keeps entries in a durable SQLite database, reads your shared ingredient reference from an existing Google Sheet, and automatically saves **one macro-total row per person per day** into your existing [AKVE calorie count master](https://docs.google.com/spreadsheets/d/1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A/edit).

**Status:** Full source code and offline tests (database, Russian/English text and OGG input plumbing with a mocked AI response, bilingual aliases, per-100g calculation, Google Sheets API request shapes with a simulated spreadsheet, and idempotent daily summaries). **Your exact spreadsheet ID and existing tab names are preconfigured and its column headers were verified through Google Drive. The independent bot has not authenticated to your Google account yet.** The real AI language and audio recognition, API permissions, Google Sheets writes, and midnight scheduled run must be verified after deployment. Do not assume this zip is a live bot.

## What is stored in your existing Google spreadsheet?

These two tabs **already exist** in your AKVE spreadsheet, with the exact matching column headers; the bot will reuse them without changing unrelated tabs:

1. `Bot Ingredients`: `Ingredient | Aliases | kcal_per_100g | protein_per_100g | fat_per_100g | carbs_per_100g | Notes | Updated_at | Updated_by`.
   - `Ingredient` can be in Russian or English. Put alternative names separated by `|` in the *Aliases* cell, e.g. `cottage cheese 1.5% | quark 1.5%`.
   - Every nutrition number is **per 100 g**. Ingredients can be added or corrected directly in the spreadsheet, or by Telegram command. The sheet is the source of truth for those values.
2. `Bot Daily Totals`: `Date | Telegram_user_id | Name | kcal | protein_g | fat_g | carbs_g | Meals_count | Last_synced`.
   - One row for each person and date. Every night at **00:05 Europe/Copenhagen** the bot exports the previous day's results.
   - `/sync` resyncs both today's and yesterday's rows, and `/sync YYYY-MM-DD` resyncs any specified date. Repeated syncs **update** existing rows instead of duplicating them.

The default names can be changed via `GOOGLE_INGREDIENTS_TAB` / `GOOGLE_DAILY_TAB`. **If a tab already exists under the chosen name with a different header format, startup stops rather than overwrite your data**. Set a different tab name or adapt the mapping first. If your existing spreadsheet *already has ingredient rows with other column names*, share its structure so we can map/import them rather than creating a blank parallel catalog.

## Setup (you need these four accounts/services)

### 1. Telegram
1. In Telegram open [@BotFather](https://t.me/BotFather) and send `/newbot`; pick a username ending with `bot`.
2. BotFather replies with a secret token; keep it private. **Never send tokens or service account JSON in Telegram, this chat, or a GitHub repository.**
3. Send `/setprivacy` to BotFather, choose this bot, select **Disable**. This is needed for ordinary food messages in a group. Remove/re-add the bot to the group afterwards if needed.
4. Create one Telegram group with yourself, your partner, and the bot.

### 2. AI
1. Open [Google AI Studio](https://aistudio.google.com/apikey), create a Gemini API key, keep it private.
2. The project defaults to `gemini-3.5-flash-lite` for combined Russian/English text, audio, images and structured JSON. Costs/quotas depend on your Gemini API account.

### 3. Authorize your **existing Google Sheet**
1. This project already targets **AKVE calorie count master**: [open your actual sheet](https://docs.google.com/spreadsheets/d/1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A/edit). The `GOOGLE_SHEET_ID` is prefilled in `.env.example` and built into the code as a fallback. **You do not need to create another spreadsheet or import tabs.**
2. At [Google Cloud Console](https://console.cloud.google.com/), choose or create a project. **Enable the Google Sheets API** for it.
3. Go to **IAM & Admin → Service Accounts**, create a service account for this bot. Give the service account **no broad project roles**; it only needs spreadsheet sharing. In the service account's **Keys** section, create a private JSON key and save it securely. Some organizations prohibit key creation; you'll need a supported alternative credential method if so.
4. In your existing spreadsheet, click **Share** and add the `client_email` address from that JSON as **Editor**. You are sharing just that spreadsheet with the service account, not all of Google Drive.
5. On your Railway service, save the full JSON as the secret variable `GOOGLE_SERVICE_ACCOUNT_JSON`. If Railway's variable editor has trouble with JSON, use the raw multiline content; do not prepend `Bearer` and do not add extra quotes. Alternatively, on your own machine use `GOOGLE_SERVICE_ACCOUNT_FILE=/full/path/to/key.json`. **Never commit this key.**
6. The existing tab names are already set. The bot will verify their headers at startup and will **not** create another spreadsheet.

For privacy, this system directly accesses your Sheet via Google's API. Connecting Google Drive inside ChatGPT by itself **does not** give your independent Telegram bot permission to access it.

### 4. Host the bot continuously on Railway
1. Unzip this project. Create a private repo at [GitHub](https://github.com/new) and upload **only the files in the project**. Do not upload a populated `.env`, any Google credential JSON, or `data/`.
2. At [Railway](https://railway.com/), create New Project → Deploy from GitHub repository. Choose the repo; it uses the included `Dockerfile`.
3. Set Railway service variables:

   | Name | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | Secret from BotFather |
   | `GEMINI_API_KEY` | Secret from Google AI Studio |
   | `GEMINI_MODEL` | `gemini-3.5-flash-lite` |
   | `TIMEZONE` | `Europe/Copenhagen` |
   | `GOOGLE_SHEET_ID` | `1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A` (already preconfigured in code; optional override) |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Secret service-account JSON |
   | `DB_PATH` | `/data/food.sqlite3` |
   | `GOOGLE_INGREDIENTS_TAB` | `Bot Ingredients` |
   | `GOOGLE_DAILY_TAB` | `Bot Daily Totals` |

4. In Railway attach a **persistent volume mounted at `/data`**, or all SQLite food entries could disappear when your app restarts/redeploys. Use just **one bot replica**, not multiple. API & hosting charges may apply.
5. Deploy. In the Telegram group send `/chatid`. Copy its returned negative number into Railway variable `ALLOWED_CHAT_ID` (with `-`), redeploy.
6. Both people send `/join` in the group, once each.
7. Test `/sheetstatus` and then a test ingredient, meal, voice message and `/sync`.

### Test the bot together

Send these in your Telegram group:

- `/ingredientadd Творог 1.5% | 73 | 12 | 1.5 | 2.7 | cottage cheese 1.5%, quark 1.5%` → saves one row with macros **per 100g** (aliases comma-separated **in this command**; in the sheet they are `|`-separated).
- `/ingredient cottage cheese 1.5%` → retrieves the same reference as `/ingredient Творог 1.5%`.
- `Я съела 200 г творога 1.5%` → logs **146 kcal, 24 g protein, 3 g fat, 5.4 g carbs** when correctly recognized as that saved ingredient.
- `I ate 200g cottage cheese 1.5%` → the same reference macros in the English message.
- Send a Russian and then an English **voice note** that mentions ingredients and weights; check recognition accuracy using the bot's confirmation.
- `/today` → shows separate daily sums for the two Telegram accounts.
- `/sync` → creates/updates today and yesterday rows for both people in `Bot Daily Totals`.
- `/editlast Actually 250g, not 200g` → corrects your own last meal; `/sync` to update today's summary. Correcting a past day's meal triggers a past-day sync automatically.

**Other commands:** `/goal 2200 150`, `/mine`, `/yesterday`, `/estimate ...` (no save), `/log ...`, `/edit ID ...`, `/undo`, `/copy ID 0.75`, `/ingredients`, `/export` (CSV), `/help`.

## Accuracy & safeguards

- Known foods: When the AI matches an item to a saved reference **and supplies the eaten grams**, the application overwrites its AI estimate with the exact per-100g arithmetic from the Sheet. If no reliable match/quantity is provided, the item remains an AI estimate.
- Recipe-level totals are not saved as eaten unless you explicitly log eating them. Photo-only estimates are inherently uncertain.
- If the Google Sheet is configured but unavailable, the bot **does not silently ignore the reference library**; food analysis returns an error and the message must be resubmitted after recovery.
- An ingredient alias that would collide with another food is rejected. Updating an ingredient's reference value does **not retroactively change** already logged meal values; correct specific meals if needed.
- Google's API access requires sharing the sheet with a service account, not installing a ChatGPT connector.
- Text, voice and photos are processed by Telegram/hosting/Gemini according to their terms; only the interpreted food descriptions/macros are kept in the bot's SQLite database (the original photo/audio is not retained there). The shared Sheet stores ingredient definitions and daily sums, including each person's Telegram numeric ID.
- If Railway is down at 00:05, the bot tries to resync yesterday on next startup, and you can always run `/sync` manually. Keep a backup of SQLite; the Sheet is **not** a complete meal-by-meal backup.

## Offline automated tests

Install `requirements.txt` in a Python 3.12+ environment and run:

```bash
python -m unittest discover -s tests -v
```

This suite has 10 tests which don't need real API credentials. A live acceptance test is still required after deploying your bot: a Russian voice note, an English voice note, a meal correction, an ingredient change, `/sync`, and a nightly export.


## Version 4: preconnected spreadsheet and deploy fix

The project targets only your existing Google spreadsheet, **AKVE calorie count master**, ID `1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A`.
The two worksheet tabs `Bot Ingredients` and `Bot Daily Totals` already exist with the exact headers expected by the code, as verified in ChatGPT. No Google Sheet import is needed.
**The source cannot contain a valid Google credential key:** create a Google Cloud service account, share just this spreadsheet with its `client_email` as Editor, and place the JSON in Railway's secret variable `GOOGLE_SERVICE_ACCOUNT_JSON`. Never upload JSON credential files or the bot token to GitHub.

This version also fixes the Docker build to include `google_sheets.py` (it was missing in v3).

The ZIP is ready for uploading its **contents** into a *private* GitHub repository. Railway deployment still requires `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, and `DB_PATH=/data/food.sqlite3` with a `/data` persistent volume.