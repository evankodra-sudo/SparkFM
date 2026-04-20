"""Tidal API wrapper for Spark Your Spotify — reads public playlists via tidalapi."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path

import tidalapi

logger = logging.getLogger("spark")

SPARK_DIR = Path.home() / ".spark"
TIDAL_SESSION_PATH = SPARK_DIR / "tidal_session.json"


def get_tidal_session() -> tidalapi.Session:
    """Get an authenticated Tidal session, loading saved credentials or prompting login."""
    session = tidalapi.Session()

    # Try to load saved session
    if TIDAL_SESSION_PATH.exists():
        try:
            saved = json.loads(TIDAL_SESSION_PATH.read_text())
            session.load_oauth_session(
                token_type=saved["token_type"],
                access_token=saved["access_token"],
                refresh_token=saved.get("refresh_token", ""),
                expiry_time=saved.get("expiry_time"),
            )
            if session.check_login():
                logger.info("Tidal session loaded from cache")
                return session
        except Exception as e:
            logger.warning("Failed to load saved Tidal session: %s", e)

    # Need fresh login — device code flow
    login, future = session.login_oauth()
    print(f"\n{'='*50}")
    print(f"TIDAL LOGIN REQUIRED (one-time setup)")
    print(f"Go to: https://{login.verification_uri_complete}")
    print(f"Or visit {login.verification_uri} and enter code: {login.user_code}")
    print(f"{'='*50}\n")

    future.result()  # Blocks until user completes login

    if session.check_login():
        _save_tidal_session(session)
        logger.info("Tidal session authenticated and saved")
        return session

    raise RuntimeError("Tidal login failed")


def _save_tidal_session(session: tidalapi.Session):
    """Save Tidal session credentials for reuse."""
    SPARK_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": session.expiry_time.timestamp() if session.expiry_time else None,
    }
    TIDAL_SESSION_PATH.write_text(json.dumps(data))


def parse_tidal_playlist_url(url: str) -> str:
    """Extract playlist UUID from a Tidal playlist URL or URI."""
    # Handle URLs like https://tidal.com/browse/playlist/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    # or https://listen.tidal.com/playlist/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    match = re.search(r"playlist/([a-f0-9\-]{36})", url)
    if match:
        return match.group(1)

    # Also handle short UUIDs or direct IDs
    match = re.search(r"playlist/([a-zA-Z0-9\-]+)", url)
    if match:
        return match.group(1)

    raise ValueError(f"Could not parse Tidal playlist ID from: {url}")


def is_tidal_url(url: str) -> bool:
    """Check if a URL is a Tidal link."""
    return "tidal.com" in url.lower()


def fetch_taste_from_tidal_playlist(session: tidalapi.Session, playlist_id: str, user_location: dict) -> dict:
    """Build a taste snapshot from a Tidal playlist."""
    playlist = session.playlist(playlist_id)
    playlist_name = playlist.name or "Tidal Playlist"
    user_handle = playlist.creator.name if playlist.creator else "listener"
    logger.info("Reading Tidal playlist '%s' by %s", playlist_name, user_handle)

    # Fetch all tracks
    tracks = playlist.tracks()
    logger.info("Playlist has %d tracks", len(tracks))

    # Extract unique artists and count appearances
    artist_map = {}
    artist_track_count = Counter()

    for track in tracks:
        for artist in track.artists:
            aid = str(artist.id)
            if aid not in artist_map:
                artist_map[aid] = {
                    "name": artist.name,
                    "genres": [],
                    "spotify_id": "",  # Not applicable
                    "tidal_id": aid,
                    "popularity": 50,
                }
            artist_track_count[aid] += 1

    # Tidal doesn't expose genres on artists easily, but we can try
    for aid, info in artist_map.items():
        try:
            full_artist = session.artist(int(aid))
            # tidalapi may not expose genres directly, but let's try
            if hasattr(full_artist, 'roles') and full_artist.roles:
                pass  # Roles aren't genres
        except Exception:
            pass

    # Rank artists by track count
    top_artist_ids = [aid for aid, _ in artist_track_count.most_common(30)]
    top_artists_list = [artist_map[aid] for aid in top_artist_ids if aid in artist_map]

    # Build genre distribution from artist names + search hints
    # Since Tidal doesn't give us genres easily, we'll let Claude infer from artist names
    genre_distribution = {}

    # Build track list with Tidal URLs
    top_tracks_list = []
    for track in tracks[:50]:
        artist_name = track.artists[0].name if track.artists else "Unknown"
        tidal_url = f"https://tidal.com/browse/track/{track.id}"
        top_tracks_list.append({
            "track": track.name,
            "artist": artist_name,
            "spotify_url": tidal_url,  # Reuse field name for template compatibility
            "spotify_id": str(track.id),
        })

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
        "_source": "tidal",
    }

    # Enforce 8KB cap
    serialized = json.dumps(snapshot)
    if len(serialized) > 8192:
        for key in ("top_tracks_medium_term",):
            while len(json.dumps(snapshot)) > 8192 and snapshot[key]:
                snapshot[key].pop()

    logger.info("Tidal taste snapshot built: %d bytes, %d artists", len(json.dumps(snapshot)), len(top_artists_list))
    return snapshot
