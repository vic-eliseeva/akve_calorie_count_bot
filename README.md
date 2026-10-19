# AKVE Duo Food Bot — conversational Russian food diary with transparent estimates

Two people use one private Telegram group. The bot reads Russian and English text, voice notes and food photos. Nutritional values come from **your Google Sheet first**, with a **verified USDA FoodData Central online fallback for confidently identified simple ingredients**. For canteen/restaurant dishes and food photos, it can also produce **clearly labeled visual estimates** of weights and nutritional values when no trustworthy reference exists. These are approximate, not sourced nutritional facts.

**Google spreadsheet:** https://docs.google.com/spreadsheets/d/1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A/edit

## How it works — natural conversation, no required slash commands

The two participants may simply message or voice-record:
- `Добавь мне 100 г банана и 150 г манго` — logs food using the Sheet first and a cited USDA reference for missing simple foods.
- `Вот мой обед` + photo — identifies visible components and estimates portion sizes, showing which gram weights are **visual assumptions**.
- `Я съела в столовой пасту с соусом` — if composition is ambiguous, shows **rough AI food estimates** with low confidence and assumptions, never pretending those are measured or verified values.
- `Сколько будет КБЖУ в 150 г риса?` — calculates but **does not log** an uneaten meal.
- `Давай добавим в базу творог 1,5%, на 100 грамм 73 ккал, белков 12, жиров 1,5, углеводов 2,7` — writes a verified entry to the appropriate tab.
- `Добавь новый продукт скир` followed by another message with the four nutrient figures — keeps a private, per-person pending entry until complete.

**Source hierarchy per ingredient:**
1. Your own three-tab Google Sheet reference, including product aliases;
2. A conservatively matched [USDA FoodData Central](https://fdc.nal.usda.gov/api-guide/) online per-100g average (a URL is shown);
3. **Only for food photos, restaurant/canteen meals or explicit requests to roughly estimate**: clearly marked AI approximation, including assumptions about visible components, per-100g values and assumed grams. This third fallback is not an internet fact and should be treated as rough bookkeeping.

The bot displays **kcal, protein, fat and carbohydrates for each identified component**, with the arithmetic `per 100 g × grams / 100`, the component's source and meal total. Estimated inputs are explicitly labeled; you can reply to the saved message with corrections. Where ingredients are unclear and no defensible estimate is possible, the bot asks instead of quietly omitting them.

Photos and restaurants can have substantial uncertainty (portion size, oil, ingredients inside sauces). If you supply actual weights or a nutrition label, those values take priority. The bot will not silently add assumed ingredients to your spreadsheet.

The bot responds in Russian even when users type or record in English. Ordinary messages from the two people go through the existing separate-account diary; normal conversation not about food can be ignored.

**USDA API key:** Without a key, the bot can use the low-limit `DEMO_KEY`; for frequent use set Railway variable `FDC_API_KEY` from [USDA's API guide](https://fdc.nal.usda.gov/api-guide/). This is separate from your Gemini key and service-account credentials.

## The FOUR remaining Google Sheet tabs

- `База продуктов` — custom products. A=comments, B=name (comma-separated aliases allowed), C=calories, D=protein, E=fat, F=carbs, G=fiber, H=original check formulas.
- `База ингредиентов` — Russian ingredient table. A=category, B=name, C–F=kcal/P/F/C. Its existing lower translated section uses B=Danish name, D=Russian alias, E–H=kcal/P/F/C; the bot reads that layout as well.
- `Ingredients base` — English ingredient table. A=category, B=name, C–F=kcal/P/F/C.
- `Bot Daily Totals` — daily total macros: Date, Telegram_user_id, Name, kcal, protein_g, fat_g, carbs_g, Meals_count, Last_synced. The bot writes one row per person per day at 00:05 Copenhagen time and via `/sync`.

The obsolete `Bot Ingredients` sheet was empty and has been deleted. The bot never reads or writes it. Existing ingredient rows and their original formulas were left alone.

Priority for duplicate ingredient names: `База продуктов` → `База ингредиентов` → `Ingredients base`. New ingredients are appended to the tab selected explicitly in Telegram; existing rows are never overwritten.

## Adding ingredients conversationally or directly in Google Sheets

You can edit the original three data tabs directly as before, or simply talk to the bot:

```text
Добавь в базу творог 1,5%. На 100 г: 73 ккал, белки 12 г, жиры 1,5 г, углеводы 2,7 г.
Add a new English ingredient: rye flakes. Per 100g 350 kcal, 10g protein, 2g fat, 72g carbs.
Добавь в базу мой брендовый йогурт, на упаковку 200 г: 120 ккал, 20 г белка, 0 жиров, 10 г углеводов.
```

The bot extracts user-supplied numbers from free text and divides by serving grams when the basis is explicit, then adds the result as kcal/P/F/C **per 100 g**. It asks clarifying questions if any of the four nutrients, the name or the weight basis is missing. It remembers the unfinished request separately for each person in SQLite, including across restarts, and `отмена` abandons it.

Russian names go to `База ингредиентов`, English names to `Ingredients base`; clearly branded/custom products can go to `База продуктов`. The old `/ingredientadd ru | name | kcal | protein | fat | carbs` syntax continues to work, but isn't required. Duplicate ingredient names are rejected; edit the existing cell directly to correct it.

A database addition isn't a meal log. After adding values, resend the food message to log it. The bot does not invent missing label values or silently convert data when the serving size is ambiguous.

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
| `FDC_API_KEY` | Optional but recommended free USDA FoodData Central API key; omit for limited DEMO_KEY testing |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Entire service-account JSON (secret; never commit) |
| `GOOGLE_SHEET_ID` | `1w6s6N5IpNHtuYUHBT6QO3PJ2ZDsLVysq4iHIQXLZg1A` (also configured as default) |
| `GOOGLE_DAILY_TAB` | `Bot Daily Totals` |
| `DB_PATH` | `/data/food.sqlite3` |
| `TIMEZONE` | `Europe/Copenhagen` |

6. Deploy, send `/chatid` in the group, then set Railway's `ALLOWED_CHAT_ID` to the returned negative numeric group ID and redeploy. Each of the two users sends `/join`.

**Never upload secrets, `.env`, or a Google JSON key to GitHub.** Google Drive access inside ChatGPT does not authorize the independent deployed Telegram bot.

## Verification before relying on it

Run `python -m unittest discover -s tests -v`. Offline tests verify Sheet priority, USDA public lookup matching, full per-ingredient calculations, the no-estimate default for ordinary unknown recipes, controlled restaurant/photo estimates, natural multi-turn ingredient intake, bilingual audio wiring and two-person exports. After deploying, test text, Russian and English voice, a restaurant photo with guessed weights, an unknown mixed meal, a 100 g banana not in the sheet, a known meal with detailed arithmetic, `/editlast`, and `/sync`. Real Gemini speech recognition, private Google-service-account access and timed nightly exports cannot be verified without the deployment credentials.

## Telegram commands

`/join`, `/today`, `/mine`, `/yesterday`, `/goal kcal protein`, `/log ...`, `/estimate ...`, `/editlast ...`, `/edit id ...`, `/copy id factor`, `/undo`, `/ingredient name`, `/ingredientadd ru|en|custom | name | kcal | protein | fat | carbs`, `/ingredients`, `/sheetstatus`, `/sync`, `/sync YYYY-MM-DD`, `/export`, `/help`.
