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
pyproject.toml     # uv project, build backend: hatchling
.github/workflows/publish.yml  # Publishes to PyPI on GitHub release (OIDC trusted publisher)
```

## Development

```bash
uv sync           # install deps + dev deps
uv run pytest     # run tests
uv build          # build wheel + sdist into dist/
```

## Publishing

Create a GitHub release — the `publish.yml` workflow fires automatically and publishes to PyPI via OIDC (no API token needed). Bump `version` in `pyproject.toml` before tagging.

## Architecture notes

### Cloudflare bypass

HelloFresh uses Cloudflare bot protection. The client works around this with:

1. A dedicated `aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())` — the non-default jar retains CF cookies across requests.
2. A browser-like GET to `/login` with `Sec-Fetch-Mode: navigate` headers before authenticating, to seed CF cookies.
3. Optional FlareSolverr support (`flaresolverr_url` kwarg) for harder CF challenges — used **only** for this pre-visit, not for auth or API calls.
4. A retry in `_request()`: on HTTP 403 with `text/html` content-type, re-acquire CF cookies and retry once.

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
- The API's `instructionsMarkdown` field actually contains HTML, so it is ignored. `RecipeStep.instructions_markdown` is generated from `instructionsHTML` by `markdown.html_to_markdown()` instead: allow-listed tags only (p/br/strong/em/lists/links), text escaped, links only for http(s)/mailto, no raw HTML passed through. Whitespace inside `<strong>` is moved outside the `**` markers (HF often writes `the<strong> water</strong>`). Upper-case bold labels `IMPORTANT:`/`TIP:`/`NOTE:`/`WARNING:`/`CAUTION:` at paragraph level are split out into GFM alerts (`> [!IMPORTANT]`); not inside lists or nested emphasis. `alerts=False` gives plain CommonMark.
- `get_recipes(ids)` caps concurrency at 4 (`_MAX_CONCURRENT_RECIPES`). `_ensure_token()` holds an `asyncio.Lock` so parallel requests share a single refresh/re-auth.

### get_recipes_for_week

`get_recipes_for_week(when, *, subscription_id=None, customer_plan_id=None)`; `when` is a `date`/`datetime` (ISO week taken), a `"YYYY-Www"` string, `"last"`/`"latest"` (= `get_last_delivery`, most recently *delivered* box) or `"next"`/`"upcoming"` (= `get_upcoming_delivery`, next scheduled box regardless of cutoff). Keywords are case-sensitive; the pairs are aliases (`_LAST_KEYWORDS`/`_NEXT_KEYWORDS`).

1. Missing IDs → one `get_customer_info()` call (active subscription, first plan ID; plan ID not fetched for `"last"`/`"latest"`).
2. `deliveries?rangeStart=week&rangeEnd=week` → if an item exists and isn't `DELIVERED`, meals come from the menu selection (needs `customer_plan_id`).
3. Otherwise `past-deliveries?from=week`, matching the entry whose `week` equals the requested one.
4. Recipe IDs are de-duplicated (order kept) and fetched with `get_recipes()`. No box that week → `[]`.

### Week strings

ISO week format: `"YYYY-WWW"` e.g. `"2026-W15"`. Built with `f"{iso.year}-W{iso.week:02d}"`.

## Dependencies

- `aiohttp>=3.9.0` — async HTTP
- `yarl` — URL construction for cookie jar seeding (transitive via aiohttp)
- Python `>=3.12`
