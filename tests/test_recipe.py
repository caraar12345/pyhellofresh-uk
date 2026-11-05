"""Tests for recipe detail parsing and fetching."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aioresponses import aioresponses

from pyhellofresh import AuthToken, HelloFreshClient, Recipe
from pyhellofresh.client import _parse_recipe
from pyhellofresh.models import parse_iso_duration_minutes

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "recipe.json").read_text())
RECIPE_ID = "6a98470271b095d5fbaf833f"


@pytest.fixture
def recipe() -> Recipe:
    return _parse_recipe(FIXTURE)


def test_basic_fields(recipe: Recipe) -> None:
    assert recipe.id == RECIPE_ID
    assert recipe.name == "Hot Honey Baked Salmon"
    assert recipe.headline.startswith("with Garlic Rice")
    assert recipe.description == "Salmon is a delicate and flavoursome fish."
    assert recipe.card_url.endswith("-auto.pdf")
    assert recipe.website_url.endswith(RECIPE_ID)
    assert recipe.difficulty == 1
    assert recipe.average_rating == 3.6
    assert recipe.ratings_count == 79
    assert recipe.serving_size_grams == 499
    assert recipe.unique_recipe_code == "R23421-16"


def test_times(recipe: Recipe) -> None:
    assert recipe.prep_time_minutes == 30
    assert recipe.total_time_minutes == 65


@pytest.mark.parametrize(
    ("value", "expected"),
    [("PT25M", 25), ("PT1H", 60), ("P1DT2M", 1442), ("", None), (None, None), ("PT", None), ("bogus", None)],
)
def test_parse_iso_duration(value: str | None, expected: int | None) -> None:
    assert parse_iso_duration_minutes(value) == expected


def test_ingredients_and_yields(recipe: Recipe) -> None:
    assert [i.name for i in recipe.ingredients] == ["Salmon Fillets", "Soy Sauce", "Butter"]
    assert recipe.ingredients[0].allergens == ["57962a07b7e8697d4b3052f7"]
    assert recipe.ingredients[1].family == "sauce"
    assert [i.name for i in recipe.pantry_ingredients] == ["Butter"]

    assert recipe.available_servings == [2, 4]
    two = recipe.ingredients_for(2)
    assert [(a.ingredient.name, a.amount, a.unit) for a in two] == [
        ("Salmon Fillets", 200, "grams"),
        ("Soy Sauce", 10, "milliliter(s)"),
        ("Butter", 30, "grams"),
    ]
    # Unknown ingredient IDs in a yield are dropped rather than raising.
    assert len(recipe.ingredients_for(4)) == 3
    with pytest.raises(KeyError):
        recipe.ingredients_for(3)


def test_nutrition(recipe: Recipe) -> None:
    kcal = recipe.nutrient("energy (kcal)")
    assert kcal is not None and kcal.amount == 786 and kcal.unit == "kcal"
    assert recipe.nutrient("Protein").amount == 29.9
    assert recipe.nutrient("Fibre") is None


def test_steps(recipe: Recipe) -> None:
    assert len(recipe.steps) == 2
    first = recipe.steps[0]
    assert first.index == 1
    assert first.instructions.startswith("Preheat")
    assert first.instructions_html.startswith("<p>")
    assert first.image_urls == [
        "https://d3hvwccx09j84u.cloudfront.net/0,0/6a98470271b095d5fbaf833f/step-7203ba00.jpg"
    ]
    assert first.image_caption == "Start the Garlic Rice"
    # Utensil IDs are resolved; unknown IDs are skipped.
    assert [u.name for u in first.utensils] == ["Medium Saucepan"]
    assert recipe.steps[1].image_urls == []
    assert recipe.steps[1].image_caption is None


def test_allergens_tags_cuisines(recipe: Recipe) -> None:
    assert [a.name for a in recipe.allergens] == ["Fish", "Wheat"]
    assert recipe.allergens[1].icon_url is None
    assert [t.name for t in recipe.tags if t.display_label] == ["Pescatarian"]
    assert [c.name for c in recipe.cuisines] == ["Asian"]
    assert [u.name for u in recipe.utensils] == ["Medium Saucepan", "Large Frying Pan"]


def test_minimal_payload() -> None:
    recipe = _parse_recipe({"id": "abc"})
    assert recipe.id == "abc"
    assert recipe.ingredients == [] and recipe.yields == {} and recipe.steps == []
    assert recipe.prep_time_minutes is None


def _client_with_token() -> HelloFreshClient:
    client = HelloFreshClient("user@example.com", "pw")
    now = datetime.now(tz=UTC)
    client.set_token(
        AuthToken(
            access_token="tok",
            refresh_token="ref",
            token_type="Bearer",
            expires_at=now + timedelta(hours=1),
            refresh_expires_at=now + timedelta(days=60),
            customer_id="c",
        )
    )
    return client


async def test_get_recipe_calls_endpoint() -> None:
    async with _client_with_token() as client:
        with aioresponses() as m:
            m.get(
                re.compile(rf"https://www\.hellofresh\.co\.uk/gw/recipes/recipes/{RECIPE_ID}\?.*"),
                payload=FIXTURE,
            )
            recipe = await client.get_recipe(RECIPE_ID)
            (method, url), calls = next(iter(m.requests.items()))
            assert method == "GET"
            assert url.query["country"] == "GB"
            assert url.query["locale"] == "en-GB"
            assert calls[0].kwargs["headers"]["Authorization"] == "Bearer tok"
    assert recipe.name == "Hot Honey Baked Salmon"


async def test_get_recipes_preserves_order() -> None:
    other = {**FIXTURE, "id": "other", "name": "Other"}
    async with _client_with_token() as client:
        with aioresponses() as m:
            m.get(re.compile(r".*/gw/recipes/recipes/other\?.*"), payload=other)
            m.get(re.compile(rf".*/gw/recipes/recipes/{RECIPE_ID}\?.*"), payload=FIXTURE)
            recipes = await client.get_recipes(["other", RECIPE_ID])
    assert [r.name for r in recipes] == ["Other", "Hot Honey Baked Salmon"]
