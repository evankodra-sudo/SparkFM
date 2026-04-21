"""FastAPI backend for Spark Your Spotify web app."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("spark.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("spark")

app = FastAPI(title="Spark Your Spotify", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (frontend)
STATIC_DIR = Path(__file__).parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


from typing import Optional


class SparkRequest(BaseModel):
    playlist_url: str
    zip_code: Optional[str] = None


class SparkResponse(BaseModel):
    html: str
    taste_profile: dict
    spotlight_artists: list
    playlist: list
    radius_label: str
    user_handle: str


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main app page."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return index_path.read_text()
    return "<h1>Spark Your Spotify</h1><p>Static files not found.</p>"


@app.post("/api/spark", response_model=SparkResponse)
async def spark(req: SparkRequest):
    """Main endpoint: playlist URL in, Spark Card out. Supports Spotify and Tidal."""
    from src.tidal_client import is_tidal_url
    from src.candidates.spotify_graph import build_candidate_set
    from src.candidates.musicbrainz import enrich_candidates_with_geo
    from src.candidates.pool import build_final_pool, tier_display_name
    from src.claude_client import call_claude
    from src.renderer import render_spark_card_html

    start = time.time()

    # Resolve location
    if req.zip_code:
        from src.location import resolve_zip
        location = resolve_zip(req.zip_code)
    else:
        from src.location import detect_location
        location = detect_location()

    # Detect platform and read playlist
    if is_tidal_url(req.playlist_url):
        taste, sp = _read_tidal_playlist(req.playlist_url, location)
    else:
        taste, sp = _read_spotify_playlist(req.playlist_url, location)

    # Build candidate pool (uses Spotify search for both — Spotify has better discovery)
    if sp:
        raw_candidates = build_candidate_set(sp, taste)
    else:
        raw_candidates = []
    logger.info("Raw candidates: %d", len(raw_candidates))

    # Geo-enrich if pool is large enough
    if len(raw_candidates) > 50:
        enriched = enrich_candidates_with_geo(raw_candidates)
        pool, tier = build_final_pool(enriched, taste, location)
    else:
        pool = raw_candidates[:40]
        tier = "region"

    radius_label = tier_display_name(tier, location)

    # Call Claude
    claude_output = call_claude(taste, pool, tier)

    # Render HTML
    html = render_spark_card_html(
        claude_output,
        user_handle=taste["user_handle"],
        radius_label=radius_label,
        candidate_pool=pool,
        user_location=location,
    )

    elapsed = time.time() - start
    logger.info("Spark completed in %.1fs", elapsed)

    return SparkResponse(
        html=html,
        taste_profile=claude_output.get("taste_profile", {}),
        spotlight_artists=claude_output.get("spotlight_artists", []),
        playlist=claude_output.get("playlist", []),
        radius_label=radius_label,
        user_handle=taste["user_handle"],
    )


def _read_spotify_playlist(url: str, location: dict):
    """Read a Spotify playlist and return (taste, spotify_client)."""
    import os
    from src.spotify_client import (
        get_spotify_client_credentials,
        parse_playlist_url,
        fetch_taste_from_playlist,
    )

    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        if not os.environ.get(var):
            raise HTTPException(500, f"Server missing {var}")

    try:
        playlist_id = parse_playlist_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    sp = get_spotify_client_credentials()

    try:
        taste = fetch_taste_from_playlist(sp, playlist_id, location)
    except Exception as e:
        logger.error("Failed to read Spotify playlist: %s", e)
        raise HTTPException(400, f"Could not read that playlist. Make sure it's public. Error: {e}")

    return taste, sp


def _read_tidal_playlist(url: str, location: dict):
    """Read a Tidal playlist and return (taste, spotify_client_or_none)."""
    import os
    from src.tidal_client import (
        get_tidal_session,
        parse_tidal_playlist_url,
        fetch_taste_from_tidal_playlist,
    )

    try:
        playlist_id = parse_tidal_playlist_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    from src.tidal_client import TidalAuthRequired
    try:
        session = get_tidal_session()
    except TidalAuthRequired:
        raise HTTPException(403, "tidal_auth_required")
    except Exception as e:
        logger.error("Tidal auth failed: %s", e)
        raise HTTPException(500, "Tidal authentication failed.")

    try:
        taste = fetch_taste_from_tidal_playlist(session, playlist_id, location)
    except Exception as e:
        logger.error("Failed to read Tidal playlist: %s", e)
        raise HTTPException(400, f"Could not read that playlist. Error: {e}")

    # Also get a Spotify client for candidate search (if available)
    sp = None
    try:
        from src.spotify_client import get_spotify_client_credentials
        if os.environ.get("SPOTIFY_CLIENT_ID"):
            sp = get_spotify_client_credentials()
    except Exception:
        pass

    return taste, sp


@app.post("/api/tidal/auth")
async def tidal_auth_start():
    """Start the Tidal OAuth device flow. Returns a URL for the user to visit."""
    from src.tidal_client import start_tidal_auth, has_tidal_session
    if has_tidal_session():
        return {"status": "already_connected"}
    result = start_tidal_auth()
    return {"status": "pending", **result}


@app.get("/api/tidal/auth/status")
async def tidal_auth_status():
    """Poll whether the Tidal login has completed."""
    from src.tidal_client import check_tidal_auth_status
    return {"status": check_tidal_auth_status()}


class ArtistSubmission(BaseModel):
    name: str
    city: str
    state: str
    genres: Optional[str] = ""
    submitted_by: Optional[str] = ""
    taste_context: Optional[str] = ""
    spotify_url: Optional[str] = ""
    soundcloud_url: Optional[str] = ""


@app.post("/api/community/submit")
async def community_submit(sub: ArtistSubmission):
    """Submit a local artist the app missed."""
    from src.community import submit_artist
    if not sub.name.strip():
        raise HTTPException(400, "Artist name is required")
    if not sub.city.strip() and not sub.state.strip():
        raise HTTPException(400, "City or state is required")
    result = submit_artist(
        name=sub.name,
        city=sub.city,
        state=sub.state,
        genres=sub.genres,
        submitted_by=sub.submitted_by,
        taste_context=sub.taste_context,
        spotify_url=sub.spotify_url,
        soundcloud_url=sub.soundcloud_url,
    )
    return {"status": "ok", "artist": result}


@app.get("/api/community/stats")
async def community_stats():
    """Get stats about the community artist database."""
    from src.community import get_stats
    return get_stats()


@app.get("/health")
async def health():
    return {"status": "ok"}
