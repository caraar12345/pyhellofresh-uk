"""Async client for the HelloFresh UK API."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from urllib.parse import quote

import aiohttp
from yarl import URL

from .exceptions import ApiError, AuthenticationError, CloudflareBlockError
from .markdown import html_to_markdown
from .models import (
    Allergen,
    AuthToken,
    Cuisine,
    CustomerInfo,
    Ingredient,
    IngredientAmount,
    Meal,
    NutritionValue,
    Recipe,
    RecipeStep,
    Tag,
    UpcomingDelivery,
    Utensil,
    WeeklyDelivery,
)

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://www.hellofresh.co.uk"
_AUTH0_URL = "https://hellofresh-live.eu.auth0.com"
_AUTH0_CLIENT_ID = "B1n0Q24hv7e4AHc7yG1WwQyuMvpCAIya"
_COUNTRY = "GB"
_LOCALE = "en-GB"
_TOKEN_EXPIRY_BUFFER = timedelta(seconds=60)
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_MAX_CONCURRENT_RECIPES = 4
_PDF_URL_TEMPLATE = f"{_BASE_URL}/recipecards/card/{{recipe_id}}-en-GB.pdf"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

_FETCH_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-GB,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Origin": _BASE_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

_NAV_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
}


class HelloFreshClient:
    """Async client for the HelloFresh UK API.

    The client owns its aiohttp session with a persistent cookie jar.

    When ``flaresolverr_url`` is provided, it is used *only* for the initial
    cookie pre-visit. All subsequent requests go directly to HelloFresh.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        flaresolverr_url: str | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._flaresolverr_url = flaresolverr_url.rstrip("/") if flaresolverr_url else None
        self._session: aiohttp.ClientSession | None = None
        self._token: AuthToken | None = None
        self._token_lock = asyncio.Lock()
        self._recipe_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RECIPES)

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> HelloFreshClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Cloudflare cookie acquisition
    # ------------------------------------------------------------------

    async def _acquire_cf_cookies(self) -> str:
        """Visit the login page to obtain CF cookies. Returns the User-Agent to use."""
        session = self._ensure_session()

        if self._flaresolverr_url:
            return await self._acquire_cf_cookies_via_flaresolverr(session)

        try:
            async with session.get(
                f"{_BASE_URL}/login",
                headers=_NAV_HEADERS,
                allow_redirects=True,
            ) as resp:
                _LOGGER.debug("CF pre-visit status: %s", resp.status)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("CF pre-visit failed (continuing): %s", err)

        return _USER_AGENT

    async def _acquire_cf_cookies_via_flaresolverr(
        self, session: aiohttp.ClientSession
    ) -> str:
        assert self._flaresolverr_url is not None
        _LOGGER.debug("Acquiring CF cookies via FlareSolverr")

        async with session.post(
            f"{self._flaresolverr_url}/v1",
            json={"cmd": "request.get", "url": f"{_BASE_URL}/login", "maxTimeout": 60000},
        ) as resp:
            fls = await resp.json()

        if fls.get("status") != "ok":
            raise ApiError(0, f"FlareSolverr: {fls.get('message', 'unknown error')}")

        solution = fls["solution"]
        cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}
        if cookies:
            session.cookie_jar.update_cookies(cookies, URL(f"{_BASE_URL}/"))
            _LOGGER.debug("Imported %d CF cookies from FlareSolverr", len(cookies))

        return solution.get("userAgent", _USER_AGENT)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> AuthToken:
        """Authenticate with email and password, returning a fresh token."""
        user_agent = await self._acquire_cf_cookies()
        session = self._ensure_session()

        async with session.post(
            f"{_BASE_URL}/gw/login",
            params={"country": _COUNTRY, "locale": _LOCALE},
            json={"username": self._email, "password": self._password},
            headers={
                **_FETCH_HEADERS,
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{_BASE_URL}/login",
            },
        ) as resp:
            if resp.status == 401:
                raise AuthenticationError("Invalid email or password")
            if resp.status == 403:
                raise CloudflareBlockError(
                    "Cloudflare blocked the login request. "
                    "Provide a FlareSolverr URL to bypass this."
                )
            if resp.status != 200:
                text = await resp.text()
                raise ApiError(resp.status, text)
            data = await resp.json()

        issued_at = datetime.fromtimestamp(data["issued_at"], tz=UTC)
        token = AuthToken(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            token_type=data["token_type"],
            expires_at=issued_at + timedelta(seconds=data["expires_in"]),
            refresh_expires_at=issued_at + timedelta(seconds=data["refresh_expires_in"]),
            customer_id=data["user_data"]["id"],
        )
        self._token = token
        return token

    async def _refresh_token(self, refresh_token: str) -> AuthToken:
        session = self._ensure_session()
        async with session.post(
            f"{_AUTH0_URL}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _AUTH0_CLIENT_ID,
            },
        ) as resp:
            if resp.status != 200:
                raise AuthenticationError("Refresh token is invalid or expired")
            data = await resp.json()

        now = datetime.now(tz=UTC)
        token = AuthToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            token_type=data.get("token_type", "Bearer"),
            expires_at=now + timedelta(seconds=data["expires_in"]),
            refresh_expires_at=self._token.refresh_expires_at
            if self._token
            else now + timedelta(days=60),
            customer_id=self._token.customer_id if self._token else "",
        )
        self._token = token
        return token

    async def _ensure_token(self) -> str:
        # Serialise so concurrent requests (e.g. get_recipes) share one
        # authenticate/refresh instead of each starting their own.
        async with self._token_lock:
            return await self._ensure_token_locked()

    async def _ensure_token_locked(self) -> str:
        if self._token is None:
            await self.authenticate()
            assert self._token is not None
            return self._token.access_token

        if datetime.now(tz=UTC) >= self._token.expires_at - _TOKEN_EXPIRY_BUFFER:
            _LOGGER.debug("Access token near expiry, refreshing")
            try:
                await self._refresh_token(self._token.refresh_token)
            except AuthenticationError:
                _LOGGER.debug("Refresh failed, re-authenticating with credentials")
                await self.authenticate()

        assert self._token is not None
        return self._token.access_token

    def set_token(self, token: AuthToken) -> None:
        """Inject a previously obtained token (e.g. from HA config entry)."""
        self._token = token

    # ------------------------------------------------------------------
    # Core request helper
    # ------------------------------------------------------------------

    async def _request(self, path: str, params: dict) -> dict:
        """Authenticated GET with one CF-cookie retry on HTML 403."""
        session = self._ensure_session()
        access_token = await self._ensure_token()

        for attempt in range(2):
            async with session.get(
                f"{_BASE_URL}{path}",
                params=params,
                headers={
                    **_FETCH_HEADERS,
                    "Accept": "*/*",
                    "Authorization": f"Bearer {access_token}",
                },
            ) as resp:
                if resp.status == 401:
                    raise AuthenticationError("Access token rejected by API")

                if resp.status == 403:
                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" in content_type and self._flaresolverr_url and attempt == 0:
                        _LOGGER.debug(
                            "Cloudflare block on API call, refreshing cookies via FlareSolverr"
                        )
                        await self._acquire_cf_cookies()
                        continue
                    text = await resp.text()
                    if "text/html" in content_type:
                        raise CloudflareBlockError(f"Cloudflare blocked request to {path}")
                    raise ApiError(resp.status, text)

                if resp.status != 200:
                    text = await resp.text()
                    raise ApiError(resp.status, text)

                return await resp.json()

        raise CloudflareBlockError(f"Cloudflare block persisted after cookie refresh for {path}")

    def _std_params(self, **extra: str | int) -> dict:
        """Standard params (country + locale) merged with extras."""
        return {"country": _COUNTRY, "locale": _LOCALE, **extra}

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    async def get_customer_info(self) -> CustomerInfo:
        """Return customer account information."""
        data = await self._request("/gw/api/customers/me/info", self._std_params())
        return CustomerInfo(
            id=data["id"],
            uuid=data["uuid"],
            first_name=data["firstName"],
            last_name=data["lastName"],
            email=data["email"],
            active_subscription_id=data["activeSubscriptionId"],
            subscription_ids=data["subscriptionIds"],
            customer_plan_ids=data["customerPlanIds"],
        )

    async def get_last_delivery(self, subscription_id: int) -> WeeklyDelivery | None:
        """Return the most recently delivered week with its meals.

        The API returns weeks in descending order (most recent first) and
        paginates via a ``nextWeek`` cursor. Follows up to 4 pages (~16 weeks
        back) to handle longer subscription pauses.
        """
        now = datetime.now()
        iso = now.isocalendar()
        from_week = f"{iso.year}-W{iso.week:02d}"

        for _ in range(4):
            data = await self._request(
                "/gw/my-deliveries/past-deliveries",
                self._std_params(**{
                    "from": from_week,
                    "rating-scale": "5",
                    "subscription": str(subscription_id),
                }),
            )
            for entry in data.get("weeks", []):
                meals = _parse_meals(entry.get("meals", []))
                if meals:
                    return WeeklyDelivery(week=entry["week"], meals=meals)
            from_week = data.get("nextWeek")
            if not from_week:
                break

        return None

    async def get_upcoming_delivery(
        self,
        subscription_id: int,
        customer_plan_id: str,
    ) -> UpcomingDelivery | None:
        """Return the next scheduled delivery with currently selected meals.

        Returns the first non-delivered delivery regardless of whether its
        cutoff has passed. Returns None if no upcoming delivery is found.
        """
        items = await self._get_delivery_items()
        next_item = _find_next_delivery(items)
        if next_item is None:
            return None
        return await self._build_upcoming_delivery(next_item, subscription_id, customer_plan_id)

    async def get_next_changeable_delivery(
        self,
        subscription_id: int,
        customer_plan_id: str,
    ) -> UpcomingDelivery | None:
        """Return the next delivery that can still be changed (cutoff not yet passed).

        Returns None if all upcoming deliveries are past their cutoff.
        """
        items = await self._get_delivery_items()
        next_item = _find_next_changeable_delivery(items)
        if next_item is None:
            return None
        return await self._build_upcoming_delivery(next_item, subscription_id, customer_plan_id)

    async def get_recipe(self, recipe_id: str) -> Recipe:
        """Return full details for a recipe: ingredients, quantities per
        serving count, nutrition, method steps, allergens, utensils and tags.

        ``recipe_id`` is the ``Meal.id`` returned by the delivery methods.
        """
        async with self._recipe_semaphore:
            data = await self._request(
                f"/gw/recipes/recipes/{quote(recipe_id, safe='')}", self._std_params()
            )
        return _parse_recipe(data)

    async def get_recipes(self, recipe_ids: list[str]) -> list[Recipe]:
        """Fetch several recipes concurrently (at most 4 in flight), preserving input order."""
        return list(await asyncio.gather(*(self.get_recipe(r) for r in recipe_ids)))

    async def get_recipes_for_week(
        self,
        when: date | datetime | Literal["last", "next"] | str,
        *,
        subscription_id: int | None = None,
        customer_plan_id: str | None = None,
    ) -> list[Recipe]:
        """Return full recipes for the meals in your box for a given week.

        ``when`` is any ``date``/``datetime`` (its ISO week is used), an ISO
        week string such as ``"2026-W45"``, or:

        * ``"last"``: the most recently delivered box (as ``get_last_delivery``).
        * ``"next"``: the next scheduled box, whether or not its cutoff has
          passed (as ``get_upcoming_delivery``).

        Delivered weeks are read from past deliveries; weeks not yet delivered
        use the meals currently selected on the menu. Returns an empty list
        when there is no box that week (skipped, paused, or out of range).

        ``subscription_id`` and ``customer_plan_id`` default to the account's
        active subscription and first customer plan, looked up with one extra
        call when not given.
        """
        # customer_plan_id is only needed for weeks not yet delivered (menu API).
        if subscription_id is None or (customer_plan_id is None and when != "last"):
            info = await self.get_customer_info()
            if subscription_id is None:
                subscription_id = info.active_subscription_id
            if customer_plan_id is None:
                customer_plan_id = info.customer_plan_ids[0] if info.customer_plan_ids else ""

        if when == "last":
            last = await self.get_last_delivery(subscription_id)
            meals = last.meals if last else []
        elif when == "next":
            if not customer_plan_id:
                raise ValueError("customer_plan_id is required for 'next'")
            upcoming = await self.get_upcoming_delivery(subscription_id, customer_plan_id)
            meals = upcoming.meals if upcoming else []
        else:
            week = _to_iso_week(when)
            meals = await self._meals_for_week(week, subscription_id, customer_plan_id)

        recipe_ids = list(dict.fromkeys(m.id for m in meals))
        return await self.get_recipes(recipe_ids)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _meals_for_week(
        self, week: str, subscription_id: int, customer_plan_id: str
    ) -> list[Meal]:
        """Meals in the box for ``week``: menu selection if upcoming, else past deliveries."""
        data = await self._request(
            "/gw/api/customers/me/deliveries",
            self._std_params(rangeStart=week, rangeEnd=week),
        )
        item = next((i for i in data.get("items", []) if i.get("id") == week), None)
        if item is not None and item.get("status") != "DELIVERED":
            if not customer_plan_id:
                raise ValueError("customer_plan_id is required for weeks not yet delivered")
            upcoming = await self._build_upcoming_delivery(item, subscription_id, customer_plan_id)
            return upcoming.meals

        past = await self._request(
            "/gw/my-deliveries/past-deliveries",
            self._std_params(**{
                "from": week,
                "rating-scale": "5",
                "subscription": str(subscription_id),
            }),
        )
        for entry in past.get("weeks", []):
            if entry.get("week") == week:
                return _parse_meals(entry.get("meals", []))
        return []


    async def _get_delivery_items(self) -> list[dict]:
        """Fetch raw delivery items from current week through 8 weeks ahead."""
        now = datetime.now()
        iso = now.isocalendar()
        range_start = f"{iso.year}-W{iso.week:02d}"
        range_end_dt = now + timedelta(weeks=8)
        range_end_iso = range_end_dt.isocalendar()
        range_end = f"{range_end_iso.year}-W{range_end_iso.week:02d}"

        data = await self._request(
            "/gw/api/customers/me/deliveries",
            self._std_params(rangeStart=range_start, rangeEnd=range_end),
        )
        return data.get("items", [])

    async def _build_upcoming_delivery(
        self,
        item: dict,
        subscription_id: int,
        customer_plan_id: str,
    ) -> UpcomingDelivery:
        """Fetch the menu for a delivery item and return an UpcomingDelivery."""
        week = item["id"]
        cutoff_date = datetime.fromisoformat(item["cutoffDate"])
        delivery_date = datetime.fromisoformat(item["deliveryDate"])
        status = item.get("status", "")
        product = item.get("product", {})
        delivery_option = item.get("deliveryOption", {})

        menu_data = await self._request(
            "/gw/my-deliveries/menu",
            {
                "customerPlanId": customer_plan_id,
                "delivery-option": delivery_option.get("handle", ""),
                "exclude": "",
                "exclude-feedback": "true",
                "include-filters": "true",
                "include-future-feedback": "false",
                "locale": _LOCALE,
                "preference": "chefschoice",
                "product-sku": product.get("handle", ""),
                "servings": str(product.get("specs", {}).get("size", 2)),
                "subscription": str(subscription_id),
                "week": week,
            },
        )

        selected = [
            _parse_meal_from_menu(m)
            for m in menu_data.get("meals", [])
            if m.get("selection") and m["selection"].get("quantity", 0) > 0
        ]

        return UpcomingDelivery(
            week=week,
            cutoff_date=cutoff_date,
            delivery_date=delivery_date,
            status=status,
            meals=selected,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _to_iso_week(when: date | datetime | str) -> str:
    """Normalise a date, datetime or ``"YYYY-Www"`` string to an ISO week string."""
    if isinstance(when, str):
        if _WEEK_RE.match(when):
            return when
        raise ValueError(
            f"Expected a date, datetime, ISO week like '2026-W45', 'last' or 'next'; got {when!r}"
        )
    if isinstance(when, date):  # datetime is a subclass of date
        iso = when.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    raise TypeError(f"Unsupported type for week: {type(when).__name__}")


def _find_next_delivery(items: list[dict]) -> dict | None:
    """Return the earliest non-delivered delivery, or None."""
    for item in items:
        if item.get("status") != "DELIVERED":
            return item
    return None


def _find_next_changeable_delivery(items: list[dict]) -> dict | None:
    """Return the earliest non-delivered delivery whose cutoff is still in the future."""
    now = datetime.now(tz=UTC)
    for item in items:
        if item.get("status") == "DELIVERED":
            continue
        cutoff_str = item.get("cutoffDate", "")
        if not cutoff_str:
            continue
        cutoff = datetime.fromisoformat(cutoff_str)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        if now < cutoff:
            return item
    return None


def _parse_meals(raw: list[dict]) -> list[Meal]:
    return [
        Meal(
            id=m["id"],
            name=m["name"],
            headline=m.get("headline", ""),
            image_url=m.get("image", ""),
            website_url=m.get("websiteURL", ""),
            pdf_url=_PDF_URL_TEMPLATE.format(recipe_id=m["id"]),
            prep_time=m.get("prepTime"),
            total_time=m.get("totalTime"),
            category=m.get("category"),
        )
        for m in raw
        if "id" in m and "name" in m
    ]


def _parse_meal_from_menu(item: dict) -> Meal:
    r = item.get("recipe", {})
    return Meal(
        id=r["id"],
        name=r["name"],
        headline=r.get("headline", ""),
        image_url=r.get("image", ""),
        website_url=r.get("websiteURL", ""),
        pdf_url=_PDF_URL_TEMPLATE.format(recipe_id=r["id"]),
        prep_time=r.get("prepTime"),
        total_time=r.get("totalTime"),
        category=r.get("category"),
    )


def _parse_recipe(data: dict) -> Recipe:
    utensils = [
        Utensil(id=u["id"], name=u.get("name", ""), type=u.get("type", ""))
        for u in data.get("utensils") or []
        if "id" in u
    ]
    utensils_by_id = {u.id: u for u in utensils}

    ingredients = [
        Ingredient(
            id=i["id"],
            name=i.get("name", ""),
            slug=i.get("slug", ""),
            image_url=i.get("imageLink"),
            shipped=i.get("shipped", True),
            allergens=list(i.get("allergens") or []),
            family=(i.get("family") or {}).get("name"),
        )
        for i in data.get("ingredients") or []
        if "id" in i
    ]
    ingredients_by_id = {i.id: i for i in ingredients}

    yields: dict[int, list[IngredientAmount]] = {}
    for y in data.get("yields") or []:
        servings = y.get("yields")
        if servings is None:
            continue
        yields[int(servings)] = [
            IngredientAmount(
                ingredient=ingredients_by_id[a["id"]],
                amount=a.get("amount"),
                unit=a.get("unit"),
            )
            for a in y.get("ingredients") or []
            if a.get("id") in ingredients_by_id
        ]

    steps = []
    for st in data.get("steps") or []:
        images = st.get("images") or []
        steps.append(
            RecipeStep(
                index=st.get("index", len(steps) + 1),
                instructions=st.get("instructions") or "",
                instructions_html=st.get("instructionsHTML") or "",
                instructions_markdown=html_to_markdown(st.get("instructionsHTML"))
                or st.get("instructions")
                or "",
                image_urls=[img["link"] for img in images if img.get("link")],
                image_caption=next(
                    (img["caption"] for img in images if img.get("caption")), None
                ),
                utensils=[
                    utensils_by_id[u] for u in st.get("utensils") or [] if u in utensils_by_id
                ],
            )
        )

    return Recipe(
        id=data["id"],
        uuid=data.get("uuid", ""),
        name=data.get("name", ""),
        slug=data.get("slug", ""),
        headline=data.get("headline") or "",
        description=data.get("descriptionMarkdown") or data.get("description") or "",
        description_html=data.get("descriptionHTML") or "",
        image_url=data.get("imageLink"),
        website_url=data.get("websiteUrl") or "",
        card_url=data.get("cardLink"),
        prep_time=data.get("prepTime"),
        total_time=data.get("totalTime"),
        difficulty=data.get("difficulty"),
        average_rating=data.get("averageRating"),
        ratings_count=data.get("ratingsCount") or 0,
        favorites_count=data.get("favoritesCount") or 0,
        serving_size_grams=data.get("servingSize"),
        unique_recipe_code=data.get("uniqueRecipeCode"),
        allergens=[
            Allergen(
                id=a["id"],
                name=a.get("name", ""),
                slug=a.get("slug", ""),
                icon_url=a.get("iconLink"),
                traces_of=a.get("tracesOf", False),
                triggers_traces_of=a.get("triggersTracesOf", False),
            )
            for a in data.get("allergens") or []
            if "id" in a
        ],
        ingredients=ingredients,
        yields=yields,
        nutrition=[
            NutritionValue(name=n["name"], amount=n["amount"], unit=n.get("unit", ""))
            for n in data.get("nutrition") or []
            if "name" in n and n.get("amount") is not None
        ],
        steps=steps,
        utensils=utensils,
        tags=[
            Tag(
                id=t["id"],
                name=t.get("name", ""),
                slug=t.get("slug", ""),
                type=t.get("type", ""),
                display_label=t.get("displayLabel", False),
            )
            for t in data.get("tags") or []
            if "id" in t
        ],
        cuisines=[
            Cuisine(
                id=c["id"],
                name=c.get("name", ""),
                slug=c.get("slug", ""),
                icon_url=c.get("iconLink"),
            )
            for c in data.get("cuisines") or []
            if "id" in c
        ],
    )
