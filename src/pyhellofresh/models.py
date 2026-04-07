"""Data models for the HelloFresh UK API."""

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
    """Meals for a given delivery week."""

    week: str
    meals: list[Meal] = field(default_factory=list)
