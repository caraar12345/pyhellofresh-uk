"""Tests for the error details and debug logging added for blocked requests."""

import logging
import re

import pytest
from aioresponses import aioresponses

from pyhellofresh import AuthenticationError, CloudflareBlockError, HelloFreshClient

PASSWORD = "s3cret-pw-value"
BASE = r"https://www\.hellofresh\.co\.uk"
LOGIN_PAGE = re.compile(BASE + r"/login$")
LOGIN_POST = re.compile(BASE + r"/gw/login\?.*")
CHALLENGE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    "<body>Checking your browser</body></html>"
)
LINUX_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)


@pytest.fixture
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="pyhellofresh.client")
    return caplog


async def test_cloudflare_challenge_403_is_described(debug_logs) -> None:
    async with HelloFreshClient("user@example.com", PASSWORD) as client:
        with aioresponses() as m:
            m.get(LOGIN_PAGE, status=200)
            m.post(
                LOGIN_POST,
                status=403,
                body=CHALLENGE,
                content_type="text/html",
                headers={"CF-Mitigated": "challenge", "CF-Ray": "abc123-LHR", "Server": "cloudflare"},
            )
            with pytest.raises(CloudflareBlockError) as err:
                await client.authenticate()
    message = str(err.value)
    for expected in ("HTTP 403", "text/html", "cf-mitigated=challenge", "cf-ray=abc123-LHR",
                     "title='Just a moment...'", "Provide a FlareSolverr URL"):
        assert expected in message
    assert PASSWORD not in message and PASSWORD not in debug_logs.text
    assert "Login refused" in debug_logs.text


async def test_json_403_is_not_mistaken_for_a_challenge() -> None:
    async with HelloFreshClient("user@example.com", PASSWORD, flaresolverr_url="http://fls:8191") as client:
        with aioresponses() as m:
            m.post(
                "http://fls:8191/v1",
                payload={"status": "ok", "solution": {"status": 200, "userAgent": LINUX_UA, "cookies": []}},
            )
            m.post(LOGIN_POST, status=403, payload={"message": "Too many login attempts"})
            with pytest.raises(CloudflareBlockError) as err:
                await client.authenticate()
    message = str(err.value)
    assert "application/json" in message and "Too many login attempts" in message
    assert "FlareSolverr is configured" in message and "rate limit" in message


async def test_flaresolverr_logging_hides_cookie_values_and_flags_mismatch(debug_logs) -> None:
    async with HelloFreshClient("user@example.com", PASSWORD, flaresolverr_url="http://fls:8191") as client:
        with aioresponses() as m:
            m.post(
                "http://fls:8191/v1",
                payload={
                    "status": "ok",
                    "message": "Challenge not detected!",
                    "solution": {
                        "status": 200,
                        "userAgent": LINUX_UA,
                        "cookies": [{"name": "cf_clearance", "value": "SECRETCOOKIEVALUE"}],
                    },
                },
            )
            m.post(LOGIN_POST, status=403, body="no", content_type="text/plain")
            with pytest.raises(CloudflareBlockError):
                await client.authenticate()
            (_, calls), = [(k, v) for k, v in m.requests.items() if "fls" in str(k[1])]
            # A hung FlareSolverr must fail after 90 s, not hang for minutes.
            assert calls[0].kwargs["timeout"].total == 90
    logs = debug_logs.text
    assert "cf_clearance present: True" in logs and "['cf_clearance']" in logs
    assert "SECRETCOOKIEVALUE" not in logs and PASSWORD not in logs
    assert "Chrome 146" in logs and "Linux / Chrome 152" in logs and "MISMATCH" in logs


async def test_matching_client_hints_are_not_flagged(debug_logs) -> None:
    mac_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    async with HelloFreshClient("user@example.com", PASSWORD, flaresolverr_url="http://fls:8191") as client:
        with aioresponses() as m:
            m.post("http://fls:8191/v1",
                   payload={"status": "ok", "solution": {"status": 200, "userAgent": mac_ua, "cookies": []}})
            await client._acquire_cf_cookies()
    assert "MISMATCH" not in debug_logs.text and "macOS / Chrome 146" in debug_logs.text


async def test_auth0_rejection_is_logged_with_its_reason(debug_logs) -> None:
    async with HelloFreshClient("user@example.com", PASSWORD) as client:
        with aioresponses() as m:
            m.post(
                "https://hellofresh-live.eu.auth0.com/oauth/token",
                status=401,
                payload={"error": "access_denied", "error_description": "Unauthorized"},
            )
            with pytest.raises(AuthenticationError):
                await client._refresh_token("refresh-token-value")
    assert "access_denied" in debug_logs.text and "Unauthorized" in debug_logs.text
    assert "refresh-token-value" not in debug_logs.text


async def test_api_cloudflare_403_includes_details_and_requests_are_logged(debug_logs) -> None:
    from datetime import UTC, datetime, timedelta

    from pyhellofresh import AuthToken

    async with HelloFreshClient("user@example.com", PASSWORD) as client:
        now = datetime.now(tz=UTC)
        client.set_token(AuthToken("tok-value", "ref", "Bearer", now + timedelta(hours=1), now + timedelta(days=1), "c"))
        with aioresponses() as m:
            m.get(re.compile(BASE + r"/gw/api/customers/me/info\?.*"), status=403, body=CHALLENGE,
                  content_type="text/html", headers={"CF-Mitigated": "challenge"})
            with pytest.raises(CloudflareBlockError) as err:
                await client.get_customer_info()
    assert "cf-mitigated=challenge" in str(err.value) and "Just a moment" in str(err.value)
    assert "GET /gw/api/customers/me/info -> HTTP 403" in debug_logs.text
    assert "tok-value" not in debug_logs.text and "tok-value" not in str(err.value)
