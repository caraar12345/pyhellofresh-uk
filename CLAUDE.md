# pyhellofresh-uk

Async Python library for the undocumented HelloFresh UK REST API.

## Project layout

```
src/pyhellofresh/
  __init__.py      # Public exports
  client.py        # HelloFreshClient — all API logic lives here
  models.py        # Dataclasses: AuthToken, CustomerInfo, Meal, WeeklyDelivery, UpcomingDelivery, Recipe (+ Ingredient, IngredientAmount, NutritionValue, RecipeStep, Allergen, Utensil, Tag, Cuisine)
  markdown.py      # html_to_markdown(): stdlib-only, allow-listed HTML → CommonMark for recipe text
  exceptions.py    # HelloFreshError, AuthenticationError, CloudflareBlockError, ApiError
tests/             # pytest + aioresponses; fixtures/ holds trimmed real API responses
scripts/live_check.py  # read-only checks against the real API with your account (see below)
pyproject.toml     # uv project, build backend: hatchling
.github/workflows/publish.yml  # Publishes to PyPI on GitHub release (OIDC trusted publisher)
```

## Development

```bash
uv sync           # install deps + dev deps
uv run pytest     # run tests
uv build          # build wheel + sdist into dist/
```

### Live API check

`uv run pytest` only uses mocked responses. Before a release, run the read-only live check with a real account:

```bash
HELLOFRESH_EMAIL=... HELLOFRESH_PASSWORD=... ./scripts/live_check.py -v   # shebang: env -S uv run --script; run from the repo root
```

Optional: `HELLOFRESH_FLARESOLVERR_URL`, `--token-cache FILE` (reuses/saves the token, `0600`; the access token lasts ~30 min), `--skip-refresh-check` (the forced Auth0 refresh falls back to a full login while AARON-283 is open, so use this with a cache to make no login at all), `--recipe-id ID`, `--dump DIR` (raw responses, contains account data; useful for new fixtures), `--debug`.

It covers login, Auth0 refresh (forced by back-dating the token), customer info, last/upcoming/next-changeable deliveries, `get_recipe` (yields, nutrition, allergens, durations), step Markdown (no raw HTML), every `get_recipes_for_week` form (including the next changeable week, which is further ahead than the next box) and that the aliases agree with the delivery methods (compared as sets of recipe IDs: a pure reordering is a WARN, a differing recipe is a FAIL). If the Auth0 refresh falls back to a full login, it repeats the refresh once and prints Auth0's HTTP status and `error`/`error_description`, plus the refresh token's shape (empty/opaque/JWT-like, length, expiry; never the value). It compares recipe/step keys against `KNOWN_RECIPE_KEYS`/`KNOWN_STEP_KEYS` and warns on drift; update those sets when HelloFresh adds fields. It never writes anything (no selections, skips or subscription changes). Exit code: 0 if no FAIL (WARN allowed), 1 on any FAIL, 2 for missing credentials.

## Publishing

Create a GitHub release — the `publish.yml` workflow fires automatically and publishes to PyPI via OIDC (no API token needed). Bump `version` in `pyproject.toml` before tagging.

## Architecture notes

### Cloudflare bypass

HelloFresh uses Cloudflare bot protection. The client works around this with:

1. A dedicated `aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())` — the non-default jar retains CF cookies across requests.
2. A browser-like GET to `/login` with `Sec-Fetch-Mode: navigate` headers before authenticating, to seed CF cookies.
3. Optional FlareSolverr support (`flaresolverr_url` kwarg) for harder CF challenges — used **only** for this pre-visit, not for auth or API calls.
4. A retry in `_request()`: on HTTP 403 with `text/html` content-type, re-acquire CF cookies and retry once.

### Diagnostics

Enable with `logging.getLogger("pyhellofresh").setLevel(logging.DEBUG)` (or `./scripts/live_check.py --debug`). DEBUG logs: every request (`GET <path> -> HTTP <status> in <ms> (<content type>)`), the Cloudflare pre-visit (HTTP status, cookie *names*), FlareSolverr's answer (status, message, page HTTP status, cookie names, whether `cf_clearance` is present), the User-Agent used for login, whether it agrees with the fixed `sec-ch-ua*` client hints (`-> MISMATCH` flags a likely bot-detection signal: the hints say Chrome 146 on macOS but the User-Agent comes from FlareSolverr), and Auth0 refresh rejections with Auth0's own error. A refused login also logs a WARNING.

A 403 on login or on an API call raises `CloudflareBlockError` whose message carries `_describe_failure()`: HTTP status, content type, `server`, `cf-ray`, `cf-mitigated`, `retry-after`, the HTML `<title>` and the first 300 characters of the body. A real Cloudflare challenge has `cf-mitigated: challenge` and a "Just a moment..." title; a JSON 403 is more likely HelloFresh itself (rate limit or account lock). The FlareSolverr pre-visit times out after 90 s (`_FLARESOLVERR_TIMEOUT`) instead of hanging.

**Never log or put in an error:** passwords, tokens, the `Authorization` header, cookie values or request bodies. `tests/test_diagnostics.py` asserts this; keep it true when adding logging.

### Authentication

- `POST /gw/login` → returns `access_token` (expires ~30 min) + `refresh_token` (expires ~60 days).
- Token refresh uses Auth0 (`hellofresh-live.eu.auth0.com`, client ID `B1n0Q24hv7e4AHc7yG1WwQyuMvpCAIya`). On refresh failure, falls back to full email/password re-auth.
- `_ensure_token()` is called before every API request; it refreshes proactively 60 s before expiry.
- `set_token(token)` lets callers inject a stored token (used by the HA integration to avoid re-auth on startup).

### Key API endpoints

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/gw/login` | Email/password auth |
| `GET` | `/gw/api/customers/me/info` | Customer profile + `customerPlanIds` |
| `GET` | `/gw/my-deliveries/past-deliveries` | Meals for a given ISO week |
| `GET` | `/gw/api/customers/me/deliveries` | Upcoming delivery schedule (cutoff/delivery dates) |
| `GET` | `/gw/my-deliveries/menu` | Full menu for a week; `selection.quantity > 0` = chosen meal |
| `GET` | `/gw/recipes/recipes/{id}` | Full recipe: ingredients, per-serving `yields`, nutrition, steps, allergens, utensils, tags |

All API calls include `country=GB&locale=en-GB` except `/gw/my-deliveries/menu` which uses `locale` only.

### Recipe details

`get_recipe(recipe_id)` (and `get_recipes([...])`, concurrent) take a `Meal.id` and return a `Recipe`.

- `yields` in the API lists `{id, amount, unit}` per serving count; the parser joins these onto `ingredients` by ID → `Recipe.yields: dict[int, list[IngredientAmount]]`, via `recipe.ingredients_for(servings)`.
- Step `utensils` are IDs resolved against the top-level `utensils` list; ingredient `allergens` are IDs into `Recipe.allergens`.
- `shipped: false` ingredients are pantry items (`recipe.pantry_ingredients`).
- `prepTime`/`totalTime` are ISO 8601 durations; `prep_time_minutes`/`total_time_minutes` convert them.
- The API's `instructionsMarkdown` field actually contains HTML, so it is ignored. `RecipeStep.instructions_markdown` is generated from `instructionsHTML` by `markdown.html_to_markdown()` instead: allow-listed tags only (p/br/strong/em/lists/links), text escaped, links only for http(s)/mailto, no raw HTML passed through. Whitespace inside `<strong>` is moved outside the `**` markers (HF often writes `the<strong> water</strong>`). Upper-case bold labels `IMPORTANT:`/`TIP:`/`NOTE:`/`WARNING:`/`CAUTION:` at paragraph level are split out into GFM alerts (`> [!IMPORTANT]`); not inside lists or nested emphasis. `alerts=False` gives plain CommonMark. If a step has no usable `instructionsHTML`, its plain `instructions` text is HTML-escaped, wrapped one `<p>` per line and sent through the same converter (`_plain_to_markdown`), so `instructions_markdown` is never raw API text.
- `get_recipes(ids)` caps concurrency at 4 (`_MAX_CONCURRENT_RECIPES`). `_ensure_token()` holds an `asyncio.Lock` so parallel requests share a single refresh/re-auth.

### get_recipes_for_week

`get_recipes_for_week(when, *, subscription_id=None, customer_plan_id=None)`; `when` is a `date`/`datetime` (ISO week taken), a `"YYYY-Www"` string, `"last"`/`"latest"` (= `get_last_delivery`, most recently *delivered* box) or `"next"`/`"upcoming"` (= `get_upcoming_delivery`, next scheduled box regardless of cutoff). Keywords are case-sensitive; the pairs are aliases (`_LAST_KEYWORDS`/`_NEXT_KEYWORDS`).

1. Missing IDs → one `get_customer_info()` call (active subscription, first plan ID; plan ID not fetched for `"last"`/`"latest"`).
2. `deliveries?rangeStart=<previous week>&rangeEnd=week` → if an item with id `week` exists and isn't `DELIVERED`, meals come from the menu selection (needs `customer_plan_id`). **`rangeStart` is exclusive** (verified live: `W47..W47` returns nothing, `W46..W47` returns W47), hence the previous week; asking for `week..week` never finds the week and silently falls through to past-deliveries, which only includes the very next box.
3. Otherwise `past-deliveries?from=week`, matching the entry whose `week` equals the requested one.
4. Recipe IDs are de-duplicated (order kept) and fetched with `get_recipes()`. No box that week → `[]`.

`"next"` looks for the delivery first (`_find_next_delivery`) and only needs `customer_plan_id` when one exists, so nothing scheduled returns `[]` even for an account without plan ids. Every deliveries range query must start one week early: `_get_delivery_items` and `_meals_for_week` both do, because `rangeStart` is exclusive.

### Week strings

ISO week format: `"YYYY-WWW"` e.g. `"2026-W15"`. Built with `f"{iso.year}-W{iso.week:02d}"`.

HelloFresh's week ids are *cooking* weeks, not delivery weeks. Verified live: the box with `deliveryDate` Sunday 8 Nov 2026 has id `2026-W46`, but 8 Nov is in ISO week 45. So `get_recipes_for_week(date)` maps a date to the box whose id equals that date's ISO week: pass a date in the cooking week, not the delivery date. Meal order within a box is also not stable between API calls, so compare boxes as sets of recipe IDs.

## Dependencies

- `aiohttp>=3.9.0` — async HTTP
- `yarl` — URL construction for cookie jar seeding (transitive via aiohttp)
- Python `>=3.12`
