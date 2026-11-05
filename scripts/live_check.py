"""Run read-only checks against the live HelloFresh UK API.

Unit tests use mocked responses. This script makes real requests with your
account, to confirm the library still matches what HelloFresh actually
returns. It only ever reads data: it never changes selections, skips weeks or
touches your subscription.

Usage (from the repo root, so the local package is used):

    export HELLOFRESH_EMAIL=you@example.com
    export HELLOFRESH_PASSWORD=...
    export HELLOFRESH_FLARESOLVERR_URL=http://flaresolverr:8191   # optional
    uv run python scripts/live_check.py
    uv run python scripts/live_check.py --token-cache ~/.cache/hellofresh-token.json
    uv run python scripts/live_check.py --recipe-id 6a98470271b095d5fbaf833f --dump /tmp/hf -v

Exit code is 0 when nothing failed (warnings allowed), 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pyhellofresh import (
    AuthToken,
    HelloFreshClient,
    HelloFreshError,
    Recipe,
    UpcomingDelivery,
    WeeklyDelivery,
)
from pyhellofresh.client import _parse_recipe

FALLBACK_RECIPE_ID = "6a98470271b095d5fbaf833f"

# Fields seen in recipe responses when the parser was written. New or missing
# keys are reported as warnings, so API drift shows up before it breaks things.
KNOWN_RECIPE_KEYS = {
    "active", "allergens", "averageRating", "canonical", "canonicalLink", "cardLink",
    "category", "clonedFrom", "comment", "country", "createdAt", "cuisines",
    "description", "descriptionHTML", "descriptionMarkdown", "difficulty",
    "favoritesCount", "headline", "id", "imageLink", "imagePath", "ingredients",
    "isAddon", "isComplete", "isPublished", "label", "link", "name", "nutrition",
    "prepTime", "promotion", "ratingsCount", "seoDescription", "seoName",
    "servingSize", "slug", "steps", "tags", "totalTime", "uniqueRecipeCode",
    "updatedAt", "utensils", "uuid", "videoLink", "websiteUrl", "yields",
}  # fmt: skip
KNOWN_STEP_KEYS = {
    "index", "instructions", "instructionsHTML", "instructionsMarkdown",
    "ingredients", "utensils", "timers", "images", "videos",
}  # fmt: skip
# Keys the parser actually reads; their absence is more serious than drift.
REQUIRED_RECIPE_KEYS = {"id", "name", "ingredients", "yields", "steps", "nutrition"}

# An unescaped "<" followed by a tag-ish character means raw HTML leaked through.
RAW_HTML = re.compile(r"(?<!\\)<[a-zA-Z/!]")


# ----------------------------------------------------------------------
# Result tracking
# ----------------------------------------------------------------------


class Skip(Exception):
    """Raise inside a check to mark it skipped (e.g. no upcoming delivery)."""


class CheckFailed(Exception):
    """Raise inside a check to mark it failed with a message."""


@dataclass
class Result:
    name: str
    status: str  # PASS / WARN / FAIL / SKIP
    seconds: float
    details: list[str] = field(default_factory=list)


_COLOURS = {"PASS": "32", "WARN": "33", "FAIL": "31", "SKIP": "90"}


def _badge(status: str) -> str:
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return f"\033[{_COLOURS[status]}m{status:4}\033[0m"
    return f"{status:4}"


class Runner:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.results: list[Result] = []
        self._details: list[str] = []
        self._warned = False

    def info(self, message: str) -> None:
        """Detail line, shown with -v (and always for warnings/failures)."""
        self._details.append(message)

    def warn(self, message: str) -> None:
        self._details.append(f"warning: {message}")
        self._warned = True

    def expect(self, condition: object, message: str) -> None:
        if not condition:
            raise CheckFailed(message)

    async def check(self, name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        self._details, self._warned = [], False
        start = time.monotonic()
        value = None
        try:
            value = await fn()
            status = "WARN" if self._warned else "PASS"
        except Skip as err:
            status = "SKIP"
            self._details.append(str(err))
        except CheckFailed as err:
            status = "FAIL"
            self._details.append(str(err))
        except Exception as err:  # noqa: BLE001
            status = "FAIL"
            self._details.append(f"{type(err).__name__}: {err}")
            if self.verbose:
                self._details.extend(traceback.format_exc().rstrip().splitlines())
        result = Result(name, status, time.monotonic() - start, self._details)
        self.results.append(result)
        print(f"{_badge(status)}  {name}  ({result.seconds:.1f}s)")
        if self.verbose or status in ("WARN", "FAIL", "SKIP"):
            for line in result.details:
                print(f"        {line}")
        return value

    def summary(self) -> int:
        counts = {s: sum(r.status == s for r in self.results) for s in _COLOURS}
        print()
        print("  ".join(f"{_badge(s)} {n}" for s, n in counts.items()))
        return 1 if counts["FAIL"] else 0


# ----------------------------------------------------------------------
# Token cache
# ----------------------------------------------------------------------


def load_token(path: Path) -> AuthToken | None:
    try:
        raw = json.loads(path.read_text())
        token = AuthToken(
            **{
                **raw,
                "expires_at": datetime.fromisoformat(raw["expires_at"]),
                "refresh_expires_at": datetime.fromisoformat(raw["refresh_expires_at"]),
            }
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if token.refresh_expires_at <= datetime.now(tz=UTC):
        return None
    return token


def save_token(path: Path, token: AuthToken) -> None:
    data = asdict(token)
    data["expires_at"] = token.expires_at.isoformat()
    data["refresh_expires_at"] = token.refresh_expires_at.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 so the refresh token isn't world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------


def _ids(meals: list) -> list[str]:
    return list(dict.fromkeys(m.id for m in meals))


def _mask_email(email: str) -> str:
    user, _, domain = email.partition("@")
    return f"{user[:1]}***@{domain}" if domain else "***"


async def run(args: argparse.Namespace) -> int:
    email = os.environ.get("HELLOFRESH_EMAIL")
    password = os.environ.get("HELLOFRESH_PASSWORD")
    if not email or not password:
        print("Set HELLOFRESH_EMAIL and HELLOFRESH_PASSWORD.", file=sys.stderr)
        return 2

    r = Runner(args.verbose)
    dump_dir: Path | None = args.dump
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)

    def dump(name: str, data: Any) -> None:
        if dump_dir:
            (dump_dir / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))

    async with HelloFreshClient(
        email, password, flaresolverr_url=os.environ.get("HELLOFRESH_FLARESOLVERR_URL")
    ) as client:
        # -- auth -------------------------------------------------------
        async def auth() -> None:
            cached = load_token(args.token_cache) if args.token_cache else None
            if cached:
                client.set_token(cached)
                r.info(f"using cached token from {args.token_cache}")
            else:
                token = await client.authenticate()
                r.expect(token.access_token, "empty access token")
                r.info(f"access token expires {token.expires_at:%Y-%m-%d %H:%M} UTC")
                r.info(f"refresh token expires {token.refresh_expires_at:%Y-%m-%d}")

        await r.check("authenticate", auth)
        if r.results[-1].status == "FAIL":
            print("\nCannot continue without authentication.")
            return r.summary()

        # -- customer ---------------------------------------------------
        async def customer():
            info = await client.get_customer_info()
            r.info(f"{info.first_name} ({_mask_email(info.email)})")
            r.info(f"active subscription {info.active_subscription_id}, plans {info.customer_plan_ids}")
            r.expect(info.active_subscription_id, "no active subscription id")
            if not info.customer_plan_ids:
                r.warn("no customerPlanIds: menu-based checks will fail")
            return info

        info = await r.check("get_customer_info", customer)
        if info is None:
            print("\nCannot continue without customer info.")
            return r.summary()
        sub_id = info.active_subscription_id
        plan_id = info.customer_plan_ids[0] if info.customer_plan_ids else ""

        # -- token refresh ---------------------------------------------
        async def refresh() -> None:
            assert client._token is not None
            refreshed = False
            original = client._refresh_token

            async def spy(refresh_token: str) -> AuthToken:
                nonlocal refreshed
                token = await original(refresh_token)
                refreshed = True
                return token

            client._refresh_token = spy  # type: ignore[method-assign]
            try:
                # Pretend the access token expired so the next call must refresh.
                client._token.expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
                await client.get_customer_info()
            finally:
                client._refresh_token = original  # type: ignore[method-assign]
            r.expect(
                client._token.expires_at > datetime.now(tz=UTC),
                "token still expired after refresh",
            )
            if refreshed:
                r.info("Auth0 refresh succeeded")
            else:
                r.warn("Auth0 refresh failed; client fell back to email/password login")

        await r.check("token refresh (Auth0)", refresh)

        # -- deliveries ------------------------------------------------
        async def last_delivery() -> WeeklyDelivery:
            last = await client.get_last_delivery(sub_id)
            if last is None:
                raise Skip("no delivered box in the last ~16 weeks")
            r.expect(last.meals, "delivered week has no meals")
            r.info(f"{last.week}: {len(last.meals)} meals")
            for m in last.meals:
                r.info(f"  {m.name}")
                r.expect(m.id and m.name, f"meal missing id/name: {m}")
            return last

        last = await r.check("get_last_delivery", last_delivery)

        async def upcoming() -> UpcomingDelivery:
            if not plan_id:
                raise Skip("no customer plan id")
            nxt = await client.get_upcoming_delivery(sub_id, plan_id)
            if nxt is None:
                raise Skip("no upcoming delivery in the next 8 weeks")
            r.info(f"{nxt.week} [{nxt.status}] cutoff {nxt.cutoff_date:%a %d %b %H:%M}, "
                   f"delivery {nxt.delivery_date:%a %d %b}")
            r.expect(nxt.cutoff_date < nxt.delivery_date, "cutoff is not before delivery")
            if not nxt.meals:
                r.warn("no meals selected (skipped week, or menu not yet published)")
            for m in nxt.meals:
                r.info(f"  {m.name}")
            return nxt

        nxt = await r.check("get_upcoming_delivery", upcoming)

        async def changeable() -> None:
            if not plan_id:
                raise Skip("no customer plan id")
            ch = await client.get_next_changeable_delivery(sub_id, plan_id)
            if ch is None:
                raise Skip("every upcoming delivery is past its cutoff")
            cutoff = ch.cutoff_date if ch.cutoff_date.tzinfo else ch.cutoff_date.replace(tzinfo=UTC)
            r.expect(cutoff > datetime.now(tz=UTC), "cutoff already passed")
            r.info(f"{ch.week}: change by {ch.cutoff_date:%a %d %b %H:%M}, {len(ch.meals)} meals")
            if nxt is not None:
                r.expect(ch.week >= nxt.week, "changeable week is before the next delivery")

        await r.check("get_next_changeable_delivery", changeable)

        # -- recipe ----------------------------------------------------
        recipe_id = (
            args.recipe_id
            or (last.meals[0].id if last and last.meals else None)
            or (nxt.meals[0].id if nxt and nxt.meals else None)
            or FALLBACK_RECIPE_ID
        )

        async def raw_recipe() -> dict:
            raw = await client._request(f"/gw/recipes/recipes/{recipe_id}", client._std_params())
            dump(f"recipe-{recipe_id}", raw)
            keys = set(raw)
            missing_required = REQUIRED_RECIPE_KEYS - keys
            r.expect(not missing_required, f"missing keys the parser needs: {sorted(missing_required)}")
            if new := sorted(keys - KNOWN_RECIPE_KEYS):
                r.warn(f"new recipe keys: {new}")
            if gone := sorted(KNOWN_RECIPE_KEYS - keys):
                r.warn(f"recipe keys no longer returned: {gone}")
            step_keys = set().union(*(s.keys() for s in raw.get("steps") or [{}]))
            if new := sorted(step_keys - KNOWN_STEP_KEYS):
                r.warn(f"new step keys: {new}")
            md = [s["instructionsMarkdown"] for s in raw.get("steps") or [] if s.get("instructionsMarkdown")]
            if md and not any(RAW_HTML.search(x) for x in md):
                r.info("instructionsMarkdown no longer contains HTML; could use it directly")
            r.info(f"{len(keys)} top-level keys")
            return raw

        raw = await r.check(f"recipe response shape ({recipe_id})", raw_recipe)

        async def recipe() -> Recipe:
            rec = await client.get_recipe(recipe_id)
            r.info(f"{rec.name} {rec.headline}")
            r.expect(rec.id == recipe_id, f"id mismatch: {rec.id}")
            r.expect(rec.name, "empty name")
            r.expect(rec.ingredients, "no ingredients")
            r.expect(rec.yields, "no yields")
            r.expect(rec.steps, "no steps")
            r.info(f"servings available: {rec.available_servings}")
            r.info(f"prep {rec.prep_time} -> {rec.prep_time_minutes} min, "
                   f"total {rec.total_time} -> {rec.total_time_minutes} min")
            if rec.prep_time and rec.prep_time_minutes is None:
                r.warn(f"could not parse prepTime {rec.prep_time!r}")
            if rec.total_time and rec.total_time_minutes is None:
                r.warn(f"could not parse totalTime {rec.total_time!r}")

            # Every yield should map onto a known ingredient.
            if raw:
                for y in raw.get("yields") or []:
                    servings = y.get("yields")
                    raw_n = len(y.get("ingredients") or [])
                    parsed_n = len(rec.yields.get(servings, []))
                    if raw_n != parsed_n:
                        r.warn(f"{servings} servings: {raw_n - parsed_n} yield entries had unknown ingredient ids")
            servings = 2 if 2 in rec.yields else rec.available_servings[0]
            for a in rec.ingredients_for(servings):
                r.info(f"  {a.amount:g} {a.unit} {a.ingredient.name}"
                       + ("" if a.ingredient.shipped else " (pantry)"))

            kcal = rec.nutrient("Energy (kcal)")
            if kcal is None:
                r.warn(f"no 'Energy (kcal)' nutrient; names are {[n.name for n in rec.nutrition]}")
            else:
                r.info(f"{kcal.amount:g} {kcal.unit}")

            allergen_ids = {a.id for a in rec.allergens}
            unknown = {aid for i in rec.ingredients for aid in i.allergens} - allergen_ids
            if unknown:
                r.warn(f"ingredients reference allergens not in recipe.allergens: {sorted(unknown)}")
            r.info(f"allergens: {', '.join(a.name for a in rec.allergens) or 'none'}")

            indices = [s.index for s in rec.steps]
            r.expect(indices == list(range(1, len(indices) + 1)), f"step indices not 1..n: {indices}")
            r.expect(rec.card_url is None or rec.card_url.startswith("https://"), f"odd card url {rec.card_url}")
            return rec

        rec = await r.check("get_recipe", recipe)

        async def markdown() -> None:
            if rec is None:
                raise Skip("get_recipe failed")
            alerts = 0
            for step in rec.steps:
                md = step.instructions_markdown
                r.expect(md.strip(), f"step {step.index}: empty Markdown")
                r.expect(not RAW_HTML.search(md), f"step {step.index}: raw HTML in Markdown: {md!r}")
                r.expect("\x00" not in md, f"step {step.index}: alert placeholder leaked")
                alerts += md.count("> [!")
            r.info(f"{len(rec.steps)} steps, {alerts} GFM alerts")
            if args.verbose:
                for step in rec.steps:
                    r.info(f"--- step {step.index}")
                    for line in step.instructions_markdown.splitlines():
                        r.info(f"  {line}")

        await r.check("step Markdown", markdown)

        # -- get_recipes_for_week -------------------------------------
        week_ids: dict[str, list[str]] = {}

        async def for_week(when: Any, label: str) -> None:
            recipes = await client.get_recipes_for_week(
                when, subscription_id=sub_id, customer_plan_id=plan_id
            )
            week_ids[label] = [x.id for x in recipes]
            r.info(f"{len(recipes)} recipes: {', '.join(x.name for x in recipes) or '-'}")

        for kw in ("last", "latest", "next", "upcoming"):
            await r.check(f"get_recipes_for_week({kw!r})", lambda kw=kw: for_week(kw, kw))

        async def keyword_consistency() -> None:
            if not week_ids:
                raise Skip("week checks failed")
            r.expect(week_ids.get("last") == week_ids.get("latest"), "'last' != 'latest'")
            r.expect(week_ids.get("next") == week_ids.get("upcoming"), "'next' != 'upcoming'")
            if last is not None:
                r.expect(week_ids.get("last") == _ids(last.meals), "'last' differs from get_last_delivery")
            if nxt is not None:
                r.expect(week_ids.get("next") == _ids(nxt.meals), "'next' differs from get_upcoming_delivery")

        await r.check("keyword aliases agree with delivery methods", keyword_consistency)

        async def by_date() -> None:
            checked = 0
            if last is not None:
                await for_week(last.week, "last-week-string")
                r.expect(week_ids["last-week-string"] == week_ids.get("last"),
                         f"{last.week!r} differs from 'last'")
                checked += 1
            if nxt is not None:
                # A datetime inside the next delivery's week must give the same box.
                await for_week(nxt.delivery_date, "next-date")
                r.expect(week_ids["next-date"] == week_ids.get("next"),
                         f"date {nxt.delivery_date:%Y-%m-%d} differs from 'next'")
                checked += 1
            await for_week(date.today(), "today")
            if not checked:
                raise Skip("no last or next delivery to compare against")

        await r.check("get_recipes_for_week by ISO week / date", by_date)

        async def bad_keyword() -> None:
            try:
                await client.get_recipes_for_week("previous", subscription_id=sub_id, customer_plan_id=plan_id)
            except ValueError:
                return
            raise CheckFailed("'previous' did not raise ValueError")

        await r.check("get_recipes_for_week rejects unknown keyword", bad_keyword)

        if args.token_cache and client._token:
            save_token(args.token_cache, client._token)

    return r.summary()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recipe-id", help="recipe to inspect (default: first meal of your last box)")
    parser.add_argument("--token-cache", type=Path,
                        help="JSON file to reuse/save the auth token (written with 0600 permissions)")
    parser.add_argument("--dump", type=Path, metavar="DIR",
                        help="save raw API responses here (contains account data)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show details for every check")
    parser.add_argument("--debug", action="store_true", help="enable library debug logging")
    args = parser.parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    try:
        sys.exit(asyncio.run(run(args)))
    except HelloFreshError as err:
        print(f"HelloFresh error: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
