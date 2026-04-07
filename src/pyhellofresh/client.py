"""Async client for the HelloFresh UK API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiohttp
from yarl import URL

from .exceptions import ApiError, AuthenticationError, CloudflareBlockError
from .models import AuthToken, CustomerInfo, Meal, UpcomingDelivery, WeeklyDelivery

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://www.hellofresh.co.uk"
_AUTH0_URL = "https://hellofresh-live.eu.auth0.com"
_AUTH0_CLIENT_ID = "B1n0Q24hv7e4AHc7yG1WwQyuMvpCAIya"
_COUNTRY = "GB"
_LOCALE = "en-GB"
_TOKEN_EXPIRY_BUFFER = timedelta(seconds=60)
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

    async def get_current_week_meals(
        self,
        subscription_id: int,
        *,
        week: str | None = None,
    ) -> WeeklyDelivery:
        """Return meals for the specified week (defaults to the current ISO week)."""
        if week is None:
            iso = datetime.now().isocalendar()
            week = f"{iso.year}-W{iso.week:02d}"

        data = await self._request(
            "/gw/my-deliveries/past-deliveries",
            self._std_params(**{
                "from": week,
                "rating-scale": "5",
                "subscription": str(subscription_id),
            }),
        )

        for entry in data.get("weeks", []):
            if entry.get("week") == week:
                return WeeklyDelivery(
                    week=week,
                    meals=_parse_meals(entry.get("meals", [])),
                )

        return WeeklyDelivery(week=week, meals=[])

    async def get_upcoming_delivery(
        self,
        subscription_id: int,
        customer_plan_id: str,
    ) -> UpcomingDelivery | None:
        """Return the next scheduled delivery with currently selected meals.

        Returns None if no upcoming (non-delivered) delivery is found.
        """
        now = datetime.now()
        iso = now.isocalendar()
        range_start = f"{iso.year}-W{iso.week:02d}"
        # Look 8 weeks ahead to cover skipped/holiday weeks
        range_end_dt = now + timedelta(weeks=8)
        range_end_iso = range_end_dt.isocalendar()
        range_end = f"{range_end_iso.year}-W{range_end_iso.week:02d}"

        data = await self._request(
            "/gw/api/customers/me/deliveries",
            self._std_params(rangeStart=range_start, rangeEnd=range_end),
        )

        next_item = _find_next_delivery(data.get("items", []))
        if next_item is None:
            return None

        week = next_item["id"]
        cutoff_date = datetime.fromisoformat(next_item["cutoffDate"])
        delivery_date = datetime.fromisoformat(next_item["deliveryDate"])
        status = next_item.get("status", "")
        product = next_item.get("product", {})
        delivery_option = next_item.get("deliveryOption", {})

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

def _find_next_delivery(items: list[dict]) -> dict | None:
    """Return the earliest non-delivered delivery, or None."""
    for item in items:
        if item.get("status") != "DELIVERED":
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
