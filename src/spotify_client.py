"""Spotify OAuth + API wrapper for Spark Your Spotify."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from pathlib import Path

import re

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

logger = logging.getLogger("spark")

SPARK_DIR = Path.home() / ".spark"
TOKEN_PATH = SPARK_DIR / "token.json"
RELATED_CACHE_PATH = SPARK_DIR / "related_cache.json"
RELATED_CACHE_TTL = 7 * 24 * 3600  # 7 days

SCOPES = [
    "user-top-read",
    "user-read-recently-played",
    "playlist-read-private",
    "user-read-private",
]


def _load_cache(path: Path, ttl: int) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        now = time.time()
        return {k: v for k, v in data.items() if now - v.get("_ts", 0) < ttl}
    except (json.JSONDecodeError, KeyError):
        return {}


def _save_cache(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def get_spotify_client() -> spotipy.Spotify:
    """Authenticate with Spotify, returning a ready client."""
    SPARK_DIR.mkdir(parents=True, exist_ok=True)

    cache_handler = spotipy.CacheFileHandler(cache_path=str(TOKEN_PATH))
    auth_manager = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8765/callback"),
        scope=" ".join(SCOPES),
        cache_handler=cache_handler,
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def fetch_taste_snapshot(sp: spotipy.Spotify, user_location: dict) -> dict:
    """Pull all taste data from Spotify and build the snapshot."""
    me = sp.current_user()
    user_handle = me.get("display_name") or me.get("id", "listener")
    logger.info("Fetching taste data for %s", user_handle)

    top_artists = {}
    for time_range in ("long_term", "medium_term", "short_term"):
        results = sp.current_user_top_artists(limit=30, time_range=time_range)
        top_artists[f"top_artists_{time_range}"] = [
            {
                "name": a["name"],
                "genres": a.get("genres", []),
                "spotify_id": a["id"],
                "popularity": a.get("popularity", 50),
            }
            for a in results.get("items", [])
        ]

    top_tracks = sp.current_user_top_tracks(limit=50, time_range="medium_term")
    top_tracks_list = [
        {
            "track": t["name"],
            "artist": t["artists"][0]["name"] if t["artists"] else "Unknown",
            "spotify_url": t["external_urls"].get("spotify", ""),
            "spotify_id": t["id"],
        }
        for t in top_tracks.get("items", [])
    ]

    recent = sp.current_user_recently_played(limit=50)
    recent_plays = [
        {
            "track": item["track"]["name"],
            "artist": item["track"]["artists"][0]["name"] if item["track"]["artists"] else "Unknown",
        }
        for item in recent.get("items", [])
    ]

    playlists = sp.current_user_playlists(limit=50)
    playlist_names = [p["name"] for p in playlists.get("items", []) if p.get("name")]

    # Build genre distribution from long-term top artists
    # If genres are empty (Spotify sometimes omits them), fetch full artist details
    genre_counts: Counter = Counter()
    long_term = top_artists.get("top_artists_long_term", [])
    has_genres = any(a["genres"] for a in long_term)

    if not has_genres and long_term:
        logger.info("Top artists missing genres, fetching full artist details...")
        artist_ids = [a["spotify_id"] for a in long_term]
        # Spotify allows up to 50 IDs per call
        for i in range(0, len(artist_ids), 50):
            batch = artist_ids[i:i + 50]
            try:
                full_artists = sp.artists(batch)
                for full_a in full_artists.get("artists", []):
                    genres = full_a.get("genres", [])
                    # Backfill genres into our snapshot
                    for a in long_term:
                        if a["spotify_id"] == full_a["id"] and not a["genres"]:
                            a["genres"] = genres
            except Exception as e:
                logger.warning("Failed to fetch artist details: %s", e)

    for a in long_term:
        for g in a["genres"]:
            genre_counts[g] += 1
    total = sum(genre_counts.values()) or 1
    genre_distribution = {g: round(c / total, 2) for g, c in genre_counts.most_common(15)}

    snapshot = {
        "user_handle": user_handle,
        "user_location": user_location,
        **top_artists,
        "top_tracks_medium_term": top_tracks_list,
        "recent_plays": recent_plays,
        "playlist_names": playlist_names,
        "genre_distribution": genre_distribution,
    }

    # Enforce 8KB cap
    serialized = json.dumps(snapshot)
    if len(serialized) > 8192:
        # Trim lists progressively
        for key in ("recent_plays", "playlist_names", "top_tracks_medium_term"):
            while len(json.dumps(snapshot)) > 8192 and snapshot[key]:
                snapshot[key].pop()

    logger.info("Taste snapshot built: %d bytes", len(json.dumps(snapshot)))
    return snapshot


def fetch_related_artists(sp: spotipy.Spotify, artist_ids: list[str]) -> list[dict]:
    """Fetch related artists for a list of artist IDs, with caching."""
    cache = _load_cache(RELATED_CACHE_PATH, RELATED_CACHE_TTL)
    all_related = []

    for aid in artist_ids:
        if aid in cache:
            all_related.extend(cache[aid]["artists"])
            continue
        try:
            result = sp.artist_related_artists(aid)
            artists = [
                {
                    "name": a["name"],
                    "genres": a.get("genres", []),
                    "spotify_id": a["id"],
                    "spotify_url": a["external_urls"].get("spotify", ""),
                    "popularity": a.get("popularity", 50),
                }
                for a in result.get("artists", [])
            ]
            cache[aid] = {"artists": artists, "_ts": time.time()}
            all_related.extend(artists)
        except Exception as e:
            logger.warning("Failed to get related artists for %s: %s", aid, e)

    _save_cache(RELATED_CACHE_PATH, cache)
    return all_related


def search_local_artists(sp: spotipy.Spotify, genres: list[str], location_terms: list[str]) -> list[dict]:
    """Search Spotify for artists matching genre+location combos."""
    results = []
    seen_ids = set()

    queries = []

    # Genre + location combos
    for genre in genres[:5]:
        for loc in location_terms[:3]:
            queries.append(f'{genre} {loc}')

    # Location-only queries if genres are empty
    if not genres:
        for loc in location_terms:
            queries.append(f'{loc} music')
            queries.append(f'{loc} artist')

    for query in queries:
        try:
            search = sp.search(q=query, type="artist", limit=20)
            for a in search.get("artists", {}).get("items", []):
                if a["id"] not in seen_ids:
                    seen_ids.add(a["id"])
                    results.append({
                        "name": a["name"],
                        "genres": a.get("genres", []),
                        "spotify_id": a["id"],
                        "spotify_url": a["external_urls"].get("spotify", ""),
                        "popularity": a.get("popularity", 50),
                    })
        except Exception as e:
            logger.warning("Search failed for '%s': %s", query, e)

    logger.info("Spotify search returned %d artists from %d queries", len(results), len(queries))
    return results


def get_spotify_client_credentials() -> spotipy.Spotify:
    """Get a Spotify client using client credentials (no user login needed)."""
    auth_manager = SpotifyClientCredentials(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def parse_playlist_url(url: str) -> str:
    """Extract playlist ID from a Spotify playlist URL or URI."""
    # Handle URLs like https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=...
    match = re.search(r"playlist[/:]([a-zA-Z0-9]+)", url)
    if match:
        return match.group(1)
    raise ValueError(f"Could not parse playlist ID from: {url}")


def fetch_taste_from_playlist(sp: spotipy.Spotify, playlist_id: str, user_location: dict) -> dict:
    """Build a taste snapshot from a public Spotify playlist."""
    # Get playlist metadata
    playlist = sp.playlist(playlist_id, fields="name,owner(display_name),tracks(total)")
    playlist_name = playlist.get("name", "")
    user_handle = playlist.get("owner", {}).get("display_name", "listener")
    logger.info("Reading playlist '%s' by %s", playlist_name, user_handle)

    # Fetch all tracks (paginate if needed)
    all_tracks = []
    results = sp.playlist_tracks(playlist_id, limit=100)
    while results:
        for item in results.get("items", []):
            track = item.get("track")
            if not track or not track.get("id"):
                continue
            all_tracks.append(track)
        results = sp.next(results) if results.get("next") else None

    logger.info("Playlist has %d tracks", len(all_tracks))

    # Extract unique artists
    artist_map = {}  # id -> {name, genres, spotify_id, popularity}
    for track in all_tracks:
        for artist in track.get("artists", []):
            aid = artist.get("id")
            if aid and aid not in artist_map:
                artist_map[aid] = {
                    "name": artist["name"],
                    "genres": [],
                    "spotify_id": aid,
                    "popularity": 50,
                }

    # Fetch full artist details in batches (for genres + popularity)
    artist_ids = list(artist_map.keys())
    for i in range(0, len(artist_ids), 50):
        batch = artist_ids[i:i + 50]
        try:
            full_artists = sp.artists(batch)
            for fa in full_artists.get("artists", []):
                if fa and fa["id"] in artist_map:
                    artist_map[fa["id"]]["genres"] = fa.get("genres", [])
                    artist_map[fa["id"]]["popularity"] = fa.get("popularity", 50)
        except Exception as e:
            logger.warning("Failed to fetch artist batch: %s", e)

    # Rank artists by how many tracks they appear in
    artist_track_count = Counter()
    for track in all_tracks:
        for artist in track.get("artists", []):
            if artist.get("id"):
                artist_track_count[artist["id"]] += 1

    top_artist_ids = [aid for aid, _ in artist_track_count.most_common(30)]
    top_artists_list = [artist_map[aid] for aid in top_artist_ids if aid in artist_map]

    # Build genre distribution
    genre_counts: Counter = Counter()
    for a in top_artists_list:
        for g in a["genres"]:
            genre_counts[g] += 1
    total = sum(genre_counts.values()) or 1
    genre_distribution = {g: round(c / total, 2) for g, c in genre_counts.most_common(15)}

    # Build track list
    top_tracks_list = [
        {
            "track": t["name"],
            "artist": t["artists"][0]["name"] if t["artists"] else "Unknown",
            "spotify_url": t["external_urls"].get("spotify", ""),
            "spotify_id": t["id"],
        }
        for t in all_tracks[:50]
    ]

    snapshot = {
        "user_handle": user_handle,
        "user_location": user_location,
        "top_artists_long_term": top_artists_list,
        "top_artists_medium_term": top_artists_list[:15],
        "top_artists_short_term": top_artists_list[:5],
        "top_tracks_medium_term": top_tracks_list,
        "recent_plays": [],
        "playlist_names": [playlist_name],
        "genre_distribution": genre_distribution,
    }

    # Enforce 8KB cap
    serialized = json.dumps(snapshot)
    if len(serialized) > 8192:
        for key in ("top_tracks_medium_term", "playlist_names"):
            while len(json.dumps(snapshot)) > 8192 and snapshot[key]:
                snapshot[key].pop()

    logger.info("Playlist taste snapshot built: %d bytes, %d artists, %d genres",
                len(json.dumps(snapshot)), len(top_artists_list), len(genre_distribution))
    return snapshot
