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
    """Main endpoint: playlist URL in, Spark Card out."""
    from src.spotify_client import (
        get_spotify_client_credentials,
        parse_playlist_url,
        fetch_taste_from_playlist,
    )
    from src.candidates.spotify_graph import build_candidate_set
    from src.candidates.musicbrainz import enrich_candidates_with_geo
    from src.candidates.pool import build_final_pool, tier_display_name
    from src.claude_client import call_claude
    from src.renderer import render_spark_card_html

    start = time.time()

    # Validate env vars
    for var in ("ANTHROPIC_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        if not os.environ.get(var):
            raise HTTPException(500, f"Server missing {var}")

    # Parse playlist URL
    try:
        playlist_id = parse_playlist_url(req.playlist_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Resolve location
    if req.zip_code:
        from src.location import resolve_zip
        location = resolve_zip(req.zip_code)
    else:
        from src.location import detect_location
        location = detect_location()

    # Get Spotify client (client credentials — no user login)
    sp = get_spotify_client_credentials()

    # Build taste snapshot from playlist
    try:
        taste = fetch_taste_from_playlist(sp, playlist_id, location)
    except Exception as e:
        logger.error("Failed to read playlist: %s", e)
        raise HTTPException(400, f"Could not read that playlist. Make sure it's public. Error: {e}")

    # Build candidate pool
    raw_candidates = build_candidate_set(sp, taste)
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
    )

    elapsed = time.time() - start
    logger.info("Spark completed in %.1fs for playlist %s", elapsed, playlist_id)

    return SparkResponse(
        html=html,
        taste_profile=claude_output.get("taste_profile", {}),
        spotlight_artists=claude_output.get("spotlight_artists", []),
        playlist=claude_output.get("playlist", []),
        radius_label=radius_label,
        user_handle=taste["user_handle"],
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
