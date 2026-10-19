# AKVE Duo Food Bot — strict ingredient-based calorie and macro logging

Two people use one private Telegram group. The bot reads Russian and English text, voice notes and food photos, but **only the existing Google Sheet** supplies nutritional values. No AI-generated calorie or macro estimates are accepted.

**Google spreadsheet:** https://docs.google.com/spreadsheets/d/1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A/edit

## Rules: no guessed nutrition, full arithmetic

1. Identify EVERY ingredient (including dressings, cooking oils, sauces and drinks) and its actual measured grams.
2. Find a matching ingredient in the spreadsheet. Gemini extracts ingredient identities and quantities; it never supplies nutrient numbers.
3. If any food is missing from the spreadsheet or has an unconfirmed portion weight, **do not log ANY part of that meal**. Ask for the missing reference values per 100 g and/or grams. After the user supplies them, ask them to resend the meal.
4. For a complete meal, calculate every ingredient's kcal, P, F and C as `(reference per 100 g × eaten grams) / 100`. Show the arithmetic for each ingredient in the Telegram reply, followed by totals. The calculation is visible for saved meals, /estimate requests, meal edits and copied meals. Long replies are split into multiple Telegram messages.
5. Keep separate food histories and daily totals for the two Telegram accounts. Never log a batch recipe unless the user explicitly says they ate it.

**Known limitation:** photos without a reliable measured weight require clarification, not a guessed quantity. The AI can misunderstand a food name, so check the reference chosen in its calculation.

## The FOUR remaining Google Sheet tabs

- `База продуктов` — custom products. A=comments, B=name (comma-separated aliases allowed), C=calories, D=protein, E=fat, F=carbs, G=fiber, H=original check formulas.
- `База ингредиентов` — Russian ingredient table. A=category, B=name, C–F=kcal/P/F/C. Its existing lower translated section uses B=Danish name, D=Russian alias, E–H=kcal/P/F/C; the bot reads that layout as well.
- `Ingredients base` — English ingredient table. A=category, B=name, C–F=kcal/P/F/C.
- `Bot Daily Totals` — daily total macros: Date, Telegram_user_id, Name, kcal, protein_g, fat_g, carbs_g, Meals_count, Last_synced. The bot writes one row per person per day at 00:05 Copenhagen time and via `/sync`.

The obsolete `Bot Ingredients` sheet was empty and has been deleted. The bot never reads or writes it. Existing ingredient rows and their original formulas were left alone.

Priority for duplicate ingredient names: `База продуктов` → `База ингредиентов` → `Ingredients base`. New ingredients are appended to the tab selected explicitly in Telegram; existing rows are never overwritten.

## Adding values to the actual reference tabs

All numbers must come from the user, **per 100 g**, in this order: kcal | protein | fat | carbohydrates.

```text
/ingredientadd ru | Творог 1.5% | 73 | 12 | 1.5 | 2.7
/ingredientadd en | Cottage cheese | 73 | 12 | 1.5 | 2.7
/ingredientadd custom | Скюр, Скир | 57 | 10 | 0.2 | 3.5
```

Specify `ru`, `en`, or `custom`; the bot won't guess a destination. The `custom` name field may list aliases separated by commas. Duplicate names are rejected; to correct an existing value, edit its original row in Google Sheets and resend the meal. For a missing food, the bot requests all four numerical values and does not silently skip anything.

## Railway setup

1. Create a Telegram bot at @BotFather and **disable group privacy** for it using `/setprivacy`; add it to a private Telegram group containing the two users.
2. Create a Gemini API key in Google AI Studio.
3. In Google Cloud enable Google Sheets API, create a service account and share **only the AKVE spreadsheet** with the account's `client_email` as **Editor**.
4. Connect this GitHub repository to Railway (Dockerfile provided). Mount a **persistent volume** at `/data`. Run exactly one bot replica.
5. Add Railway secret environment variables:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather secret token |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GEMINI_MODEL` | A valid text/audio/image-capable model available to your API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Entire service-account JSON (secret; never commit) |
| `GOOGLE_SHEET_ID` | `1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A` (also configured as default) |
| `GOOGLE_DAILY_TAB` | `Bot Daily Totals` |
| `DB_PATH` | `/data/food.sqlite3` |
| `TIMEZONE` | `Europe/Copenhagen` |

6. Deploy, send `/chatid` in the group, then set Railway's `ALLOWED_CHAT_ID` to the returned negative numeric group ID and redeploy. Each of the two users sends `/join`.

**Never upload secrets, `.env`, or a Google JSON key to GitHub.** Google Drive access inside ChatGPT does not authorize the independent deployed Telegram bot.

## Verification before relying on it

Run `python -m unittest discover -s tests -v`. Offline tests verify reference lookup, source-tab writes, strict no-guess behavior, bilingual voice request wiring, and two-person daily exports. After deploying, test text, Russian and English voice, a photo without measured weights, an unknown ingredient, a known meal with detailed arithmetic, `/editlast`, and `/sync`. Real Gemini speech recognition, private Google-service-account access and timed nightly exports cannot be verified without the deployment credentials.

## Telegram commands

`/join`, `/today`, `/mine`, `/yesterday`, `/goal kcal protein`, `/log ...`, `/estimate ...`, `/editlast ...`, `/edit id ...`, `/copy id factor`, `/undo`, `/ingredient name`, `/ingredientadd ru|en|custom | name | kcal | protein | fat | carbs`, `/ingredients`, `/sheetstatus`, `/sync`, `/sync YYYY-MM-DD`, `/export`, `/help`.
