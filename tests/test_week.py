"""Tests for get_recipes_for_week."""

import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from aioresponses import aioresponses

from pyhellofresh import AuthToken, HelloFreshClient
from pyhellofresh.client import _to_iso_week

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "recipe.json").read_text())
BASE = r"https://www\.hellofresh\.co\.uk"
DELIVERIES = re.compile(BASE + r"/gw/api/customers/me/deliveries\?.*")
PAST = re.compile(BASE + r"/gw/my-deliveries/past-deliveries\?.*")
MENU = re.compile(BASE + r"/gw/my-deliveries/menu\?.*")
INFO = re.compile(BASE + r"/gw/api/customers/me/info\?.*")


def recipe_url(rid: str) -> re.Pattern[str]:
    return re.compile(BASE + rf"/gw/recipes/recipes/{rid}\?.*")


def recipe_payload(rid: str) -> dict:
    return {**FIXTURE, "id": rid, "name": f"Recipe {rid}"}


@pytest.fixture
async def client():
    c = HelloFreshClient("user@example.com", "pw")
    now = datetime.now(tz=UTC)
    c.set_token(
        AuthToken("tok", "ref", "Bearer", now + timedelta(hours=1), now + timedelta(days=60), "c")
    )
    yield c
    await c.close()


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (date(2026, 11, 5), "2026-W45"),
        (datetime(2026, 11, 8, 23, 59), "2026-W45"),  # Sunday is still the same ISO week
        (date(2027, 1, 1), "2026-W53"),  # ISO year differs from calendar year
        ("2026-W07", "2026-W07"),
    ],
)
def test_to_iso_week(when, expected) -> None:
    assert _to_iso_week(when) == expected


@pytest.mark.parametrize("bad", ["2026-45", "latest", "2026-W5"])
def test_to_iso_week_rejects_bad_strings(bad) -> None:
    with pytest.raises(ValueError):
        _to_iso_week(bad)


def _requests(m, pattern: re.Pattern[str]) -> list:
    return [(u, calls) for (meth, u), calls in m.requests.items() if pattern.match(str(u))]


async def test_delivered_week_uses_past_deliveries(client) -> None:
    with aioresponses() as m:
        m.get(DELIVERIES, payload={"items": [{"id": "2026-W40", "status": "DELIVERED"}]})
        m.get(
            PAST,
            payload={
                "weeks": [
                    {"week": "2026-W40", "meals": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]},
                    {"week": "2026-W39", "meals": [{"id": "z", "name": "Z"}]},
                ]
            },
        )
        m.get(recipe_url("a"), payload=recipe_payload("a"))
        m.get(recipe_url("b"), payload=recipe_payload("b"))

        recipes = await client.get_recipes_for_week(
            date(2026, 10, 1), subscription_id=1, customer_plan_id="plan"
        )

        (url, _), = _requests(m, DELIVERIES)
        assert url.query["rangeStart"] == url.query["rangeEnd"] == "2026-W40"
        (url, _), = _requests(m, PAST)
        assert url.query["from"] == "2026-W40"
        assert url.query["subscription"] == "1"
        assert not _requests(m, MENU)
    assert [r.id for r in recipes] == ["a", "b"]


async def test_upcoming_week_uses_menu_selection(client) -> None:
    item = {
        "id": "2026-W46",
        "status": "RUNNING",
        "cutoffDate": "2026-11-07T23:59:00+00:00",
        "deliveryDate": "2026-11-12T08:00:00+00:00",
        "product": {"handle": "sku", "specs": {"size": 2}},
        "deliveryOption": {"handle": "opt"},
    }
    menu = {
        "meals": [
            {"recipe": {"id": "x", "name": "X"}, "selection": {"quantity": 1}},
            {"recipe": {"id": "y", "name": "Y"}, "selection": {"quantity": 0}},
            {"recipe": {"id": "z", "name": "Z"}, "selection": {"quantity": 2}},
        ]
    }
    with aioresponses() as m:
        m.get(DELIVERIES, payload={"items": [item]})
        m.get(MENU, payload=menu)
        m.get(recipe_url("x"), payload=recipe_payload("x"))
        m.get(recipe_url("z"), payload=recipe_payload("z"))

        recipes = await client.get_recipes_for_week(
            "2026-W46", subscription_id=1, customer_plan_id="plan"
        )
        (url, _), = _requests(m, MENU)
        assert url.query["week"] == "2026-W46"
        assert url.query["customerPlanId"] == "plan"
        assert not _requests(m, PAST)
    assert [r.id for r in recipes] == ["x", "z"]


async def test_week_without_box_returns_empty(client) -> None:
    with aioresponses() as m:
        m.get(DELIVERIES, payload={"items": []})
        m.get(PAST, payload={"weeks": [{"week": "2026-W30", "meals": [{"id": "q", "name": "Q"}]}]})
        assert await client.get_recipes_for_week(
            "2026-W31", subscription_id=1, customer_plan_id="plan"
        ) == []


async def test_last_uses_last_delivery_and_dedupes(client) -> None:
    with aioresponses() as m:
        m.get(
            PAST,
            payload={
                "weeks": [
                    {"week": "2026-W45", "meals": []},
                    {"week": "2026-W44", "meals": [{"id": "a", "name": "A"}, {"id": "a", "name": "A"}]},
                ]
            },
        )
        m.get(recipe_url("a"), payload=recipe_payload("a"))
        recipes = await client.get_recipes_for_week("last", subscription_id=1)
        assert not _requests(m, INFO)  # no customer lookup needed
        assert not _requests(m, DELIVERIES)
    assert [r.id for r in recipes] == ["a"]


async def test_next_uses_upcoming_delivery(client) -> None:
    items = [
        {"id": "2026-W45", "status": "DELIVERED"},
        {
            "id": "2026-W46",
            "status": "RUNNING",
            "cutoffDate": "2026-11-07T23:59:00+00:00",
            "deliveryDate": "2026-11-12T08:00:00+00:00",
            "product": {"handle": "sku", "specs": {"size": 2}},
            "deliveryOption": {"handle": "opt"},
        },
    ]
    menu = {"meals": [{"recipe": {"id": "n", "name": "N"}, "selection": {"quantity": 1}}]}
    with aioresponses() as m:
        m.get(DELIVERIES, payload={"items": items})
        m.get(MENU, payload=menu)
        m.get(recipe_url("n"), payload=recipe_payload("n"))
        recipes = await client.get_recipes_for_week("next", subscription_id=1, customer_plan_id="plan")
        (url, _), = _requests(m, MENU)
        assert url.query["week"] == "2026-W46"
    assert [r.id for r in recipes] == ["n"]


async def test_next_with_nothing_scheduled_returns_empty(client) -> None:
    with aioresponses() as m:
        m.get(DELIVERIES, payload={"items": [{"id": "2026-W45", "status": "DELIVERED"}]})
        assert await client.get_recipes_for_week("next", subscription_id=1, customer_plan_id="p") == []


async def test_ids_default_from_customer_info(client) -> None:
    info = {
        "id": "c", "uuid": "u", "firstName": "F", "lastName": "L", "email": "e",
        "activeSubscriptionId": 77, "subscriptionIds": [77], "customerPlanIds": ["plan-1"],
    }
    with aioresponses() as m:
        m.get(INFO, payload=info)
        m.get(DELIVERIES, payload={"items": []})
        m.get(PAST, payload={"weeks": []})
        await client.get_recipes_for_week(date(2026, 10, 1))
        (url, _), = _requests(m, PAST)
        assert url.query["subscription"] == "77"


async def test_step_markdown_populated(client) -> None:
    with aioresponses() as m:
        m.get(recipe_url("6a98470271b095d5fbaf833f"), payload=FIXTURE)
        recipe = await client.get_recipe("6a98470271b095d5fbaf833f")
    assert recipe.steps[0].instructions_markdown == (
        "Preheat your oven to 220°C/200°C fan/gas mark 7."
    )


async def test_concurrent_fetches_share_one_token_refresh(monkeypatch) -> None:
    c = HelloFreshClient("user@example.com", "pw")
    now = datetime.now(tz=UTC)
    c.set_token(AuthToken("old", "ref", "Bearer", now - timedelta(minutes=1), now + timedelta(days=60), "c"))
    refreshes = 0

    async def fake_refresh(refresh_token: str) -> AuthToken:
        nonlocal refreshes
        refreshes += 1
        await asyncio.sleep(0.01)  # yield so concurrent callers would race without the lock
        c._token = AuthToken("new", "ref", "Bearer", now + timedelta(hours=1), now + timedelta(days=60), "c")
        return c._token

    monkeypatch.setattr(c, "_refresh_token", fake_refresh)
    try:
        with aioresponses() as m:
            for rid in ("a", "b", "c"):
                m.get(recipe_url(rid), payload=recipe_payload(rid))
            await c.get_recipes(["a", "b", "c"])
            for (_, url), calls in m.requests.items():
                assert calls[0].kwargs["headers"]["Authorization"] == "Bearer new"
        assert refreshes == 1
    finally:
        await c.close()
