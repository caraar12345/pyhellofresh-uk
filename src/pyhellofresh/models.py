"""Data models for the HelloFresh UK API."""

import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AuthToken:
    """Authentication token data."""

    access_token: str
    refresh_token: str
    token_type: str
    expires_at: datetime
    refresh_expires_at: datetime
    customer_id: str


@dataclass
class CustomerInfo:
    """HelloFresh customer information."""

    id: str
    uuid: str
    first_name: str
    last_name: str
    email: str
    active_subscription_id: int
    subscription_ids: list[int]
    customer_plan_ids: list[str]


@dataclass
class Meal:
    """A HelloFresh meal/recipe."""

    id: str
    name: str
    headline: str
    image_url: str
    website_url: str
    pdf_url: str
    prep_time: str | None = None
    total_time: str | None = None
    category: str | None = None


@dataclass
class WeeklyDelivery:
    """Meals for a delivered week (from past-deliveries endpoint)."""

    week: str
    meals: list[Meal] = field(default_factory=list)


@dataclass
class UpcomingDelivery:
    """Details of the next scheduled delivery."""

    week: str
    cutoff_date: datetime
    delivery_date: datetime
    status: str
    meals: list[Meal] = field(default_factory=list)


# ----------------------------------------------------------------------
# Recipe details (from /gw/recipes/recipes/{id})
# ----------------------------------------------------------------------


@dataclass
class Allergen:
    """An allergen declared on a recipe or ingredient."""

    id: str
    name: str
    slug: str
    icon_url: str | None = None
    traces_of: bool = False
    triggers_traces_of: bool = False


@dataclass
class Ingredient:
    """An ingredient used in a recipe.

    ``shipped`` is False for pantry items (oil, butter, water, ...) the
    customer is expected to supply themselves. ``allergens`` holds allergen
    IDs; resolve them via ``Recipe.allergens``.
    """

    id: str
    name: str
    slug: str
    image_url: str | None = None
    shipped: bool = True
    allergens: list[str] = field(default_factory=list)
    family: str | None = None


@dataclass
class IngredientAmount:
    """An ingredient quantity for a specific number of servings."""

    ingredient: Ingredient
    amount: float | None
    unit: str | None


@dataclass
class NutritionValue:
    """A single nutrition line (per serving)."""

    name: str
    amount: float
    unit: str


@dataclass
class Utensil:
    """A piece of kitchen equipment."""

    id: str
    name: str
    type: str


@dataclass
class RecipeStep:
    """One step of a recipe's method."""

    index: int
    instructions: str
    instructions_html: str
    image_urls: list[str] = field(default_factory=list)
    image_caption: str | None = None
    utensils: list[Utensil] = field(default_factory=list)


@dataclass
class Tag:
    """A recipe tag (e.g. "Pescatarian", "Source of Protein")."""

    id: str
    name: str
    slug: str
    type: str
    display_label: bool = False


@dataclass
class Cuisine:
    """A cuisine classification (e.g. "Asian")."""

    id: str
    name: str
    slug: str
    icon_url: str | None = None


@dataclass
class Recipe:
    """Full details of a HelloFresh recipe."""

    id: str
    uuid: str
    name: str
    slug: str
    headline: str
    description: str
    description_html: str
    image_url: str | None
    website_url: str
    card_url: str | None
    prep_time: str | None
    total_time: str | None
    difficulty: int | None
    average_rating: float | None
    ratings_count: int
    favorites_count: int
    serving_size_grams: int | None
    unique_recipe_code: str | None
    allergens: list[Allergen] = field(default_factory=list)
    ingredients: list[Ingredient] = field(default_factory=list)
    yields: dict[int, list[IngredientAmount]] = field(default_factory=dict)
    nutrition: list[NutritionValue] = field(default_factory=list)
    steps: list[RecipeStep] = field(default_factory=list)
    utensils: list[Utensil] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
    cuisines: list[Cuisine] = field(default_factory=list)

    @property
    def prep_time_minutes(self) -> int | None:
        """``prep_time`` (ISO 8601 duration) as whole minutes."""
        return parse_iso_duration_minutes(self.prep_time)

    @property
    def total_time_minutes(self) -> int | None:
        """``total_time`` (ISO 8601 duration) as whole minutes."""
        return parse_iso_duration_minutes(self.total_time)

    @property
    def available_servings(self) -> list[int]:
        """Serving counts this recipe has ingredient quantities for."""
        return sorted(self.yields)

    def ingredients_for(self, servings: int) -> list[IngredientAmount]:
        """Ingredient quantities for ``servings`` people.

        Raises ``KeyError`` if the recipe has no yield for that serving count.
        """
        return self.yields[servings]

    @property
    def pantry_ingredients(self) -> list[Ingredient]:
        """Ingredients not shipped in the box (customer supplies them)."""
        return [i for i in self.ingredients if not i.shipped]

    def nutrient(self, name: str) -> NutritionValue | None:
        """Look up a nutrition value by name (case-insensitive), e.g. ``"Protein"``."""
        wanted = name.casefold()
        return next((n for n in self.nutrition if n.name.casefold() == wanted), None)


_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_iso_duration_minutes(value: str | None) -> int | None:
    """Convert an ISO 8601 duration like ``"PT1H5M"`` to minutes.

    Returns None for empty or unparseable values.
    """
    if not value:
        return None
    match = _DURATION_RE.match(value)
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return days * 1440 + hours * 60 + minutes + seconds // 60
