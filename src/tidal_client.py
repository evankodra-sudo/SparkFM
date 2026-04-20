"""Tidal API wrapper for Spark Your Spotify — reads public playlists via tidalapi."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from pathlib import Path

import tidalapi

logger = logging.getLogger("spark")

SPARK_DIR = Path.home() / ".spark"
TIDAL_SESSION_PATH = SPARK_DIR / "tidal_session.json"

# In-memory state for pending OAuth flows
_pending_auth = {
    "session": None,
    "login": None,
    "future": None,
    "status": "idle",  # idle | pending | done | error
}


def has_tidal_session() -> bool:
    """Check if we have a valid saved Tidal session."""
    if not TIDAL_SESSION_PATH.exists():
        return False
    try:
        session = tidalapi.Session()
        saved = json.loads(TIDAL_SESSION_PATH.read_text())
        session.load_oauth_session(
            token_type=saved["token_type"],
            access_token=saved["access_token"],
            refresh_token=saved.get("refresh_token", ""),
            expiry_time=saved.get("expiry_time"),
        )
        return session.check_login()
    except Exception:
        return False


def get_tidal_session() -> tidalapi.Session:
    """Get an authenticated Tidal session from saved credentials."""
    session = tidalapi.Session()

    if not TIDAL_SESSION_PATH.exists():
        raise TidalAuthRequired()

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

    raise TidalAuthRequired()


class TidalAuthRequired(Exception):
    """Raised when Tidal login is needed."""
    pass


def start_tidal_auth() -> dict:
    """Start the Tidal OAuth device flow. Returns the login URL for the user."""
    session = tidalapi.Session()
    login, future = session.login_oauth()

    _pending_auth["session"] = session
    _pending_auth["login"] = login
    _pending_auth["future"] = future
    _pending_auth["status"] = "pending"

    # Monitor the future in a background thread
    def _watch():
        try:
            future.result(timeout=300)  # 5 min timeout
            if session.check_login():
                _save_tidal_session(session)
                _pending_auth["status"] = "done"
                logger.info("Tidal auth completed via browser")
            else:
                _pending_auth["status"] = "error"
        except Exception as e:
            logger.warning("Tidal auth failed: %s", e)
            _pending_auth["status"] = "error"

    thread = threading.Thread(target=_watch, daemon=True)
    thread.start()

    # Build the login URL
    verification_uri = login.verification_uri_complete
    if not verification_uri.startswith("http"):
        verification_uri = f"https://{verification_uri}"

    return {
        "login_url": verification_uri,
        "user_code": login.user_code,
        "verification_uri": login.verification_uri,
    }


def check_tidal_auth_status() -> str:
    """Check if the pending Tidal auth has completed. Returns: pending | done | error | idle."""
    return _pending_auth["status"]


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
    match = re.search(r"playlist/([a-f0-9\-]{36})", url)
    if match:
        return match.group(1)
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

    tracks = playlist.tracks()
    logger.info("Playlist has %d tracks", len(tracks))

    artist_map = {}
    artist_track_count = Counter()

    for track in tracks:
        for artist in track.artists:
            aid = str(artist.id)
            if aid not in artist_map:
                artist_map[aid] = {
                    "name": artist.name,
                    "genres": [],
                    "spotify_id": "",
                    "tidal_id": aid,
                    "popularity": 50,
                }
            artist_track_count[aid] += 1

    top_artist_ids = [aid for aid, _ in artist_track_count.most_common(30)]
    top_artists_list = [artist_map[aid] for aid in top_artist_ids if aid in artist_map]

    genre_distribution = {}

    top_tracks_list = []
    for track in tracks[:50]:
        artist_name = track.artists[0].name if track.artists else "Unknown"
        tidal_url = f"https://tidal.com/browse/track/{track.id}"
        top_tracks_list.append({
            "track": track.name,
            "artist": artist_name,
            "spotify_url": tidal_url,
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

    serialized = json.dumps(snapshot)
    if len(serialized) > 8192:
        for key in ("top_tracks_medium_term",):
            while len(json.dumps(snapshot)) > 8192 and snapshot[key]:
                snapshot[key].pop()

    logger.info("Tidal taste snapshot built: %d bytes, %d artists", len(json.dumps(snapshot)), len(top_artists_list))
    return snapshot
