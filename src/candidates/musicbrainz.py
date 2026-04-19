"""MusicBrainz geo-enrichment with caching and rate limiting."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger("spark")

SPARK_DIR = Path.home() / ".spark"
MB_CACHE_PATH = SPARK_DIR / "mb_cache.json"
MB_CACHE_TTL = 30 * 24 * 3600  # 30 days
MB_BASE = "https://musicbrainz.org/ws/2/artist"
MB_HEADERS = {"User-Agent": "SparkYourSpotify/0.1 (spark-cli)"}

# Rate limiting: 1 req/sec
_last_request_time = 0.0


def _rate_limit():
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_request_time = time.time()


def _load_cache() -> dict:
    if not MB_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(MB_CACHE_PATH.read_text())
        now = time.time()
        return {k: v for k, v in data.items() if now - v.get("_ts", 0) < MB_CACHE_TTL}
    except (json.JSONDecodeError, KeyError):
        return {}


def _save_cache(cache: dict):
    MB_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MB_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def lookup_artist_origin(name: str, cache: dict) -> dict | None:
    """Look up an artist's origin city/state on MusicBrainz. Returns dict with city/state or None."""
    cache_key = name.lower().strip()
    if cache_key in cache:
        entry = cache[cache_key]
        if entry.get("origin"):
            return entry["origin"]
        return None

    _rate_limit()

    try:
        resp = requests.get(
            MB_BASE,
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": "3"},
            headers=MB_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("MusicBrainz lookup failed for %s: %s", name, e)
        cache[cache_key] = {"origin": None, "_ts": time.time()}
        return None

    artists = data.get("artists", [])
    if not artists:
        cache[cache_key] = {"origin": None, "_ts": time.time()}
        return None

    # Find the best match
    for mb_artist in artists:
        # Try to extract location from area or begin-area
        origin = _extract_origin(mb_artist)
        if origin:
            cache[cache_key] = {"origin": origin, "_ts": time.time()}
            return origin

    cache[cache_key] = {"origin": None, "_ts": time.time()}
    return None


def _extract_origin(mb_artist: dict) -> dict | None:
    """Extract city/state from a MusicBrainz artist record."""
    # Check begin-area first (where they're from), then area (where they're based)
    for field in ("begin-area", "area"):
        area = mb_artist.get(field)
        if not area:
            continue

        area_name = area.get("name", "")
        area_type = area.get("type", "")

        # If it's a city, try to get the state from the parent
        if area_type in ("City", "District", "Municipality") or area_type == "":
            state = _extract_state_from_area(area)
            if state:
                return {"city": area_name, "state": state}
            # Still useful even without state
            if area_name:
                return {"city": area_name, "state": ""}

        # If it's a subdivision (state), use it directly
        if area_type in ("Subdivision", "State"):
            return {"city": "", "state": _state_from_name(area_name)}

        # Country-level isn't useful for local filtering
        if area_type == "Country":
            continue

        # Unknown type but has a name — use it
        if area_name:
            return {"city": area_name, "state": ""}

    return None


def _extract_state_from_area(area: dict) -> str:
    """Try to extract state from MusicBrainz area relations or name parsing."""
    # Check if the area name contains a state-like suffix (e.g., "Boston, MA")
    name = area.get("name", "")
    if ", " in name:
        parts = name.rsplit(", ", 1)
        if len(parts[1]) == 2 and parts[1].isupper():
            return parts[1]
    return ""


def _state_from_name(name: str) -> str:
    """Convert a state name to abbreviation if possible."""
    from src.location import _STATE_MAP
    abbrev = _STATE_MAP.get(name.lower(), "")
    if abbrev:
        return abbrev
    if len(name) == 2 and name.isupper():
        return name
    return name


def enrich_candidates_with_geo(candidates: list[dict]) -> list[dict]:
    """Add origin city/state to each candidate via MusicBrainz. Drops candidates with no geo data."""
    cache = _load_cache()
    enriched = []

    total = len(candidates)
    for i, candidate in enumerate(candidates):
        name = candidate["name"]
        if i % 20 == 0:
            logger.info("MusicBrainz enrichment: %d/%d", i, total)

        origin = lookup_artist_origin(name, cache)
        if origin:
            candidate["origin_city"] = origin.get("city", "")
            candidate["origin_state"] = origin.get("state", "")
            enriched.append(candidate)

    _save_cache(cache)
    logger.info("Geo-enriched: %d/%d candidates have location data", len(enriched), total)
    return enriched
