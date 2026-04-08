# pyhellofresh-uk

Async Python library for the undocumented HelloFresh UK REST API.

## Project layout

```
src/pyhellofresh/
  __init__.py      # Public exports
  client.py        # HelloFreshClient — all API logic lives here
  models.py        # Dataclasses: AuthToken, CustomerInfo, Meal, WeeklyDelivery, UpcomingDelivery
  exceptions.py    # HelloFreshError, AuthenticationError, CloudflareBlockError, ApiError
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

All API calls include `country=GB&locale=en-GB` except `/gw/my-deliveries/menu` which uses `locale` only.

### Week strings

ISO week format: `"YYYY-WWW"` e.g. `"2026-W15"`. Built with `f"{iso.year}-W{iso.week:02d}"`.

## Dependencies

- `aiohttp>=3.9.0` — async HTTP
- `yarl` — URL construction for cookie jar seeding (transitive via aiohttp)
- Python `>=3.12`
