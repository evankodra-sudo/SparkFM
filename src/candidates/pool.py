"""Candidate pool: adaptive radius filtering, scoring, and truncation."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

logger = logging.getLogger("spark")

REGIONS_PATH = Path(__file__).parent.parent.parent / "data" / "regions.json"

# California metro areas for sub-state regions
CA_BAY_AREA_CITIES = {
    "san francisco", "oakland", "san jose", "berkeley", "palo alto",
    "fremont", "hayward", "sunnyvale", "santa clara", "mountain view",
    "redwood city", "daly city", "richmond", "concord", "walnut creek",
}
CA_SOCAL_CITIES = {
    "los angeles", "san diego", "long beach", "anaheim", "santa ana",
    "irvine", "glendale", "pasadena", "torrance", "pomona",
    "burbank", "costa mesa", "inglewood", "compton", "hollywood",
}


def _load_regions() -> dict:
    return json.loads(REGIONS_PATH.read_text())


def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distance in miles between two lat/lng points."""
    R = 3959  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _get_user_region(state: str) -> str | None:
    """Find which region a state belongs to."""
    regions = _load_regions()
    for region_name, states in regions.items():
        if state in states:
            return region_name
    return None


def _get_ca_subregion(city: str) -> str | None:
    """For California, determine Bay Area vs SoCal."""
    city_lower = city.lower()
    if city_lower in CA_BAY_AREA_CITIES:
        return "bay_area"
    if city_lower in CA_SOCAL_CITIES:
        return "socal"
    return None


def _candidate_in_tier(candidate: dict, user_location: dict, tier: str) -> bool:
    """Check if a candidate falls within the given radius tier."""
    c_city = candidate.get("origin_city", "").lower()
    c_state = candidate.get("origin_state", "")
    u_city = user_location.get("city", "").lower()
    u_state = user_location.get("state", "")

    if tier == "city":
        return c_city == u_city and c_state == u_state

    if tier == "metro":
        # Same state + within ~30 miles (if we have coords), or same city
        if c_city == u_city and c_state == u_state:
            return True
        if c_state == u_state:
            # Approximate: consider it metro if names partially match
            return c_city != "" and u_city != "" and (c_city in u_city or u_city in c_city)
        return False

    if tier == "state":
        return c_state == u_state

    if tier == "region":
        user_region = _get_user_region(u_state)
        if not user_region:
            return False
        candidate_region = _get_user_region(c_state)
        return candidate_region == user_region

    return False


def adaptive_radius_filter(candidates: list[dict], user_location: dict) -> tuple[list[dict], str]:
    """Filter candidates by expanding radius tiers until pool >= 30."""
    tiers = ["city", "metro", "state", "region"]

    for tier in tiers:
        pool = [c for c in candidates if _candidate_in_tier(c, user_location, tier)]
        logger.info("Tier '%s': %d candidates", tier, len(pool))
        if len(pool) >= 30:
            return pool, tier

    # If all tiers exhausted, return whatever we have from region
    pool = [c for c in candidates if _candidate_in_tier(c, user_location, "region")]
    if pool:
        return pool, "region"

    # Last resort: return all candidates
    logger.warning("All tiers exhausted, returning full candidate set")
    return candidates, "region"


def score_candidates(
    candidates: list[dict],
    taste_snapshot: dict,
) -> list[dict]:
    """Score and rank candidates using the spec's formula."""
    user_genres = taste_snapshot.get("genre_distribution", {})
    user_genre_set = set(user_genres.keys())

    for c in candidates:
        # Taste adjacency: higher if came from related-artists graph
        taste_adj = 0.7 if c.get("_from_related") else 0.3

        # Genre match: Jaccard-ish similarity
        c_genres = set(c.get("genres", []))
        if c_genres and user_genre_set:
            intersection = c_genres & user_genre_set
            union = c_genres | user_genre_set
            genre_match = len(intersection) / len(union) if union else 0
        else:
            genre_match = 0

        # Popularity inverse: prefer less popular (more discovery)
        popularity = c.get("popularity", 50)
        pop_inverse = 1.0 - (popularity / 100.0)

        c["_score"] = 0.5 * taste_adj + 0.3 * genre_match + 0.2 * pop_inverse

    candidates.sort(key=lambda c: c["_score"], reverse=True)
    return candidates


def build_final_pool(
    candidates: list[dict],
    taste_snapshot: dict,
    user_location: dict,
) -> tuple[list[dict], str]:
    """Full pipeline: filter by radius, score, truncate to top 40."""
    # Step 1: Adaptive radius
    filtered, tier = adaptive_radius_filter(candidates, user_location)
    logger.info("Radius tier used: %s (%d candidates)", tier, len(filtered))

    # Step 2: Score
    scored = score_candidates(filtered, taste_snapshot)

    # Step 3: Truncate to top 40
    top_40 = scored[:40]

    # Clean internal fields before passing to Claude
    for c in top_40:
        c.pop("_score", None)
        c.pop("_from_related", None)

    logger.info("Final pool: %d candidates at tier '%s'", len(top_40), tier)
    return top_40, tier


def tier_display_name(tier: str, user_location: dict) -> str:
    """Convert a tier to a human-friendly label."""
    city = user_location.get("city", "")
    state = user_location.get("state", "")

    if tier == "city":
        return city or "Your City"
    if tier == "metro":
        return f"Greater {city}" if city else "Your Metro Area"
    if tier == "state":
        return state or "Your State"
    if tier == "region":
        region = _get_user_region(state)
        region_names = {
            "new_england": "New England",
            "tri_state": "the Tri-State Area",
            "dmv": "the DMV",
            "mid_atlantic": "the Mid-Atlantic",
            "southeast": "the Southeast",
            "deep_south": "the Deep South",
            "texas": "Texas",
            "midwest": "the Midwest",
            "plains": "the Plains",
            "mountain_west": "the Mountain West",
            "southwest": "the Southwest",
            "pacific_nw": "the Pacific Northwest",
            "bay_area": "the Bay Area",
            "socal": "Southern California",
            "california": "California",
            "hawaii": "Hawaii",
            "alaska": "Alaska",
        }
        return region_names.get(region, "Your Region")
    return "Your Area"
