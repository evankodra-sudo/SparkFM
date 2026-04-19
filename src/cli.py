"""CLI entry point for Spark Your Spotify."""

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Configure logging
LOG_PATH = Path("spark.log")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stderr),
        ],
    )


def main():
    parser = argparse.ArgumentParser(
        description="Spark Your Spotify — discover local artists from your taste",
    )
    parser.add_argument("--zip", dest="zip_code", help="Override detected location with a ZIP code")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("spark")

    # Load .env
    load_dotenv()

    # Validate required env vars
    import os
    missing = []
    for var in ("ANTHROPIC_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your keys.")
        sys.exit(1)

    from src.spotify_client import get_spotify_client, fetch_taste_snapshot
    from src.location import detect_location, confirm_location, resolve_zip
    from src.candidates.spotify_graph import build_candidate_set
    from src.candidates.musicbrainz import enrich_candidates_with_geo
    from src.candidates.pool import build_final_pool, tier_display_name
    from src.claude_client import call_claude
    from src.renderer import render_spark_card

    start = time.time()

    # Step 1: Location
    print("\n⚡ Spark Your Spotify\n")

    if args.zip_code:
        location = resolve_zip(args.zip_code)
        print(f"Using location: {location['city']}, {location['state']} {location['zip']}")
    else:
        location = detect_location()
        location = confirm_location(location)

    logger.info("Location: %s", location)

    # Step 2: Spotify auth + taste snapshot
    print("Connecting to Spotify...")
    sp = get_spotify_client()
    print("Pulling your taste data...")
    taste = fetch_taste_snapshot(sp, location)
    print(f"Got taste data for {taste['user_handle']}.")

    # Step 3: Build candidate pool
    print("Finding artists near you...")
    raw_candidates = build_candidate_set(sp, taste)
    print(f"Found {len(raw_candidates)} candidate artists.")

    # Step 4: Geo-enrich via MusicBrainz (only if pool is large enough to survive filtering)
    if len(raw_candidates) > 50:
        print("Checking origins...")
        enriched = enrich_candidates_with_geo(raw_candidates)
        print(f"Geo-located {len(enriched)} artists.")

        # Step 5: Adaptive radius + scoring
        pool, tier = build_final_pool(enriched, taste, location)
    else:
        # Pool is thin — skip MusicBrainz filtering, let Claude handle local knowledge
        logger.info("Small candidate pool (%d), skipping MusicBrainz geo-filter", len(raw_candidates))
        pool = raw_candidates[:40]
        tier = "region"

    radius_label = tier_display_name(tier, location)
    print(f"Using {len(pool)} artists from {radius_label} (tier: {tier}).")

    if len(pool) < 10:
        print(f"\nNote: Your local scene is small — only {len(pool)} candidates found.")
        print("We'll work with what we've got.\n")

    # Step 6: Call Claude
    print("Curating your Spark playlist...")
    claude_output = call_claude(taste, pool, tier)

    # Step 7: Render HTML
    output_path = render_spark_card(
        claude_output,
        user_handle=taste["user_handle"],
        radius_label=radius_label,
        candidate_pool=pool,
    )

    elapsed = time.time() - start

    # Print playlist to stdout
    print(f"\n{'='*50}")
    print(f"⚡ YOUR SPARK PLAYLIST — {radius_label}")
    print(f"{'='*50}\n")

    taste_profile = claude_output.get("taste_profile", {})
    print(f"  {taste_profile.get('headline', '')}")
    print(f"  {taste_profile.get('body', '')}\n")

    print("  SPOTLIGHT ARTISTS:")
    for artist in claude_output.get("spotlight_artists", []):
        print(f"  ★ {artist['name']}")
        print(f"    {artist['one_liner']}")
        print(f"    → {artist['why_this_matches_you']}")
        print(f"    {artist['spotify_url']}\n")

    print("  PLAYLIST:")
    for i, track in enumerate(claude_output.get("playlist", []), 1):
        tag = "🔥" if track["role"] == "discovery" else "🌉"
        print(f"  {i:2d}. {track['track']} — {track['artist']}  {tag} [{track['role']}]")
        if track.get("note"):
            print(f"      {track['note']}")
    print()

    print(f"Spark Card saved: {output_path}")
    print(f"Done in {elapsed:.1f}s.\n")


if __name__ == "__main__":
    main()
