"""Source A & B: Spotify related-artists graph + local search."""

from __future__ import annotations

import logging

import spotipy

from src.spotify_client import fetch_related_artists, search_local_artists

logger = logging.getLogger("spark")


def build_candidate_set(
    sp: spotipy.Spotify,
    taste_snapshot: dict,
) -> list[dict]:
    """Merge Source A (related-artists graph) and Source B (local search) into a deduplicated candidate set."""
    # Collect user's known artist IDs for dedup
    known_ids = set()
    for key in ("top_artists_long_term", "top_artists_medium_term", "top_artists_short_term"):
        for a in taste_snapshot.get(key, []):
            known_ids.add(a["spotify_id"])

    # Source A: Related artists (may fail with 403 on newer Spotify apps)
    top_artist_ids = [a["spotify_id"] for a in taste_snapshot.get("top_artists_long_term", [])]
    logger.info("Fetching related artists for %d top artists", len(top_artist_ids))
    related = fetch_related_artists(sp, top_artist_ids)

    if not related:
        logger.warning("Related-artists returned nothing (likely 403). Relying on search only.")

    # Source B: Local search — expanded queries for better coverage
    top_genres = list(taste_snapshot.get("genre_distribution", {}).keys())[:8]
    loc = taste_snapshot.get("user_location", {})
    city = loc.get("city", "")
    state = loc.get("state", "")

    # Build diverse location terms
    location_terms = [t for t in [city, state] if t]

    # Also search with artist names + location for taste-adjacent local artists
    top_artist_names = [a["name"] for a in taste_snapshot.get("top_artists_long_term", [])[:5]]

    logger.info("Searching Spotify for local artists: genres=%s, locations=%s", top_genres, location_terms)
    local = search_local_artists(sp, top_genres, location_terms)

    # Additional search: genre-only queries to widen the net
    genre_only = search_local_artists(sp, top_genres[:3], [city] if city else [state])

    # Additional search: broader location terms
    broader_terms = _get_broader_location_terms(city, state)
    if broader_terms:
        logger.info("Broadening search with: %s", broader_terms)
        broader = search_local_artists(sp, top_genres[:5], broader_terms)
    else:
        broader = []

    # Merge and deduplicate
    seen_ids = set()
    candidates = []
    for artist in related + local + genre_only + broader:
        aid = artist["spotify_id"]
        if aid in seen_ids or aid in known_ids:
            continue
        seen_ids.add(aid)
        artist["_from_related"] = aid in {a["spotify_id"] for a in related}
        candidates.append(artist)

    logger.info("Candidate set: %d unique artists (after removing %d known)", len(candidates), len(known_ids))
    return candidates


def _get_broader_location_terms(city: str, state: str) -> list[str]:
    """Generate broader location search terms based on metro areas."""
    metro_map = {
        "Boston": ["Cambridge", "Somerville", "Brookline", "Dorchester"],
        "Atlanta": ["Decatur", "East Atlanta", "College Park"],
        "New York": ["Brooklyn", "Queens", "Bronx", "Harlem"],
        "Los Angeles": ["Compton", "Inglewood", "Long Beach"],
        "Chicago": ["South Side Chicago", "Hyde Park"],
        "Houston": ["Third Ward", "Southside Houston"],
        "Philadelphia": ["North Philly", "West Philly"],
        "Detroit": ["Dearborn", "Highland Park"],
        "Miami": ["Little Haiti", "Overtown", "Liberty City"],
        "Nashville": ["East Nashville", "Music Row"],
        "Austin": ["East Austin", "South Austin"],
        "Portland": ["PDX"],
        "Seattle": ["Capitol Hill Seattle"],
    }
    return metro_map.get(city, [])
