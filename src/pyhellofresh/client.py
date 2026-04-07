"""Async client for the HelloFresh UK API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiohttp

from .exceptions import ApiError, AuthenticationError
from .models import AuthToken, CustomerInfo, Meal, WeeklyDelivery

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://www.hellofresh.co.uk"
_AUTH0_URL = "https://hellofresh-live.eu.auth0.com"
_AUTH0_CLIENT_ID = "B1n0Q24hv7e4AHc7yG1WwQyuMvpCAIya"
_COUNTRY = "GB"
_LOCALE = "en-GB"
_TOKEN_EXPIRY_BUFFER = timedelta(seconds=60)
_PDF_URL_TEMPLATE = f"{_BASE_URL}/recipecards/card/{{recipe_id}}-en-GB.pdf"

# Headers required to pass Cloudflare bot protection
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Origin": _BASE_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


class HelloFreshClient:
    """Async client for the HelloFresh UK API.

    Usage::

        async with HelloFreshClient(email, password) as client:
            info = await client.get_customer_info()
            delivery = await client.get_current_week_meals(info.active_subscription_id)
    """

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._session = session
        self._owns_session = session is None
        self._token: AuthToken | None = None

    async def __aenter__(self) -> HelloFreshClient:
        if self._owns_session:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> AuthToken:
        """Authenticate with email and password, returning a fresh token."""
        assert self._session is not None
        payload = {"username": self._email, "password": self._password}
        async with self._session.post(
            f"{_BASE_URL}/gw/login",
            params={"country": _COUNTRY, "locale": _LOCALE},
            json=payload,
            headers={
                **_BROWSER_HEADERS,
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{_BASE_URL}/login",
            },
        ) as resp:
            if resp.status == 401:
                raise AuthenticationError("Invalid email or password")
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
        """Obtain a new access token using the stored refresh token."""
        assert self._session is not None
        async with self._session.post(
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
        """Return a valid access token, refreshing or re-authenticating as needed."""
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
    # API calls
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: str | int) -> dict:
        """Perform an authenticated GET request."""
        assert self._session is not None
        access_token = await self._ensure_token()
        query = {"country": _COUNTRY, "locale": _LOCALE, **params}
        async with self._session.get(
            f"{_BASE_URL}{path}",
            params=query,
            headers={
                **_BROWSER_HEADERS,
                "Accept": "*/*",
                "Authorization": f"Bearer {access_token}",
            },
        ) as resp:
            if resp.status == 401:
                raise AuthenticationError("Access token rejected by API")
            if resp.status != 200:
                text = await resp.text()
                raise ApiError(resp.status, text)
            return await resp.json()

    async def get_customer_info(self) -> CustomerInfo:
        """Return customer account information."""
        data = await self._get("/gw/api/customers/me/info")
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
        """Return meals for the specified week (defaults to the current ISO week).

        Args:
            subscription_id: The customer's active subscription ID.
            week: ISO week string like "2026-W15". Defaults to the current week.

        Returns:
            A WeeklyDelivery for the requested week, or an empty one if not found.
        """
        if week is None:
            iso = datetime.now().isocalendar()
            week = f"{iso.year}-W{iso.week:02d}"

        data = await self._get(
            "/gw/my-deliveries/past-deliveries",
            **{
                "from": week,
                "rating-scale": "5",
                "subscription": str(subscription_id),
            },
        )

        weeks: list[dict] = data.get("weeks", [])
        for entry in weeks:
            if entry.get("week") == week:
                return WeeklyDelivery(
                    week=week,
                    meals=_parse_meals(entry.get("meals", [])),
                )

        return WeeklyDelivery(week=week, meals=[])


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
