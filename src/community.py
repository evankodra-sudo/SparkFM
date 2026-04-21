"""Community artist database — learns from user feedback."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("spark")

DB_PATH = Path(__file__).parent.parent / "data" / "community_artists.json"


def _load_db() -> dict:
    try:
        return json.loads(DB_PATH.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {"artists": []}


def _save_db(db: dict):
    DB_PATH.write_text(json.dumps(db, indent=2))


def submit_artist(
    name: str,
    city: str,
    state: str,
    genres: str = "",
    submitted_by: str = "",
    taste_context: str = "",
    spotify_url: str = "",
    soundcloud_url: str = "",
) -> dict:
    """Add a community-submitted artist. If already exists, increment upvotes."""
    db = _load_db()
    name_lower = name.strip().lower()

    # Check if artist already exists
    for artist in db["artists"]:
        if artist["name"].lower() == name_lower:
            artist["upvotes"] = artist.get("upvotes", 1) + 1
            # Update fields if new submission has more info
            if city and not artist.get("city"):
                artist["city"] = city
            if state and not artist.get("state"):
                artist["state"] = state
            if genres and not artist.get("genres"):
                artist["genres"] = genres
            if spotify_url and not artist.get("spotify_url"):
                artist["spotify_url"] = spotify_url
            if soundcloud_url and not artist.get("soundcloud_url"):
                artist["soundcloud_url"] = soundcloud_url
            _save_db(db)
            logger.info("Community artist upvoted: %s (now %d)", name, artist["upvotes"])
            return artist

    # New artist
    entry = {
        "name": name.strip(),
        "city": city.strip(),
        "state": state.strip().upper(),
        "genres": genres.strip(),
        "submitted_by": submitted_by,
        "submitted_at": datetime.now().isoformat(),
        "taste_context": taste_context,
        "upvotes": 1,
        "spotify_url": spotify_url.strip(),
        "soundcloud_url": soundcloud_url.strip(),
    }
    db["artists"].append(entry)
    _save_db(db)
    logger.info("Community artist added: %s (%s, %s)", name, city, state)
    return entry


def get_local_artists(city: str, state: str, genres: list[str] = None, limit: int = 20) -> list[dict]:
    """Get community-submitted artists for a location, ranked by upvotes and genre match."""
    db = _load_db()
    candidates = []

    city_lower = city.lower()
    state_upper = state.upper()

    for artist in db["artists"]:
        a_city = artist.get("city", "").lower()
        a_state = artist.get("state", "").upper()

        # Match by city or state
        if a_city == city_lower or a_state == state_upper:
            score = artist.get("upvotes", 1)

            # Boost if genre matches
            if genres and artist.get("genres"):
                a_genres = {g.strip().lower() for g in artist["genres"].split(",")}
                user_genres = {g.lower() for g in genres}
                overlap = a_genres & user_genres
                if overlap:
                    score += len(overlap) * 2

            candidates.append({**artist, "_score": score})

    # Sort by score descending
    candidates.sort(key=lambda x: x["_score"], reverse=True)

    # Clean and return
    for c in candidates:
        c.pop("_score", None)

    return candidates[:limit]


def get_stats() -> dict:
    """Get basic stats about the community database."""
    db = _load_db()
    artists = db["artists"]
    cities = set()
    for a in artists:
        if a.get("city"):
            cities.add(f"{a['city']}, {a.get('state', '')}")
    return {
        "total_artists": len(artists),
        "total_cities": len(cities),
        "top_cities": list(cities)[:10],
    }
