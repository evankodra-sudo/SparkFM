"""Claude API integration for Spark Your Spotify."""

from __future__ import annotations

import json
import logging
import os

import anthropic

logger = logging.getLogger("spark")

SYSTEM_PROMPT = """You are the curation voice of a local-music discovery tool inspired by
SparkFM Online Radio, a Boston-based digital station that amplifies
underserved voices. Your job: look at a listener's Spotify taste and put
them on to local/regional artists they've never heard, in a way that feels
like a friend with great taste making the intro.

Voice guidelines:
- Warm, observant, confident. 6/10 on the flattery scale — affirming
  without being sycophantic.
- Spark-adjacent register: "pour into," "tap in," "for the culture," light
  emoji use (≤2 per section). Never corporate, never horoscope-y.
- Specific over general. "You've been living in your neo-soul bag" beats
  "You love R&B." Name actual artists from their taste when drawing
  connections.
- Never say "I noticed" or "based on your listening" or "I can see" —
  just state the observation directly.

Calibration:
- TOO COLD: "Your top artists include SZA and Jazmine Sullivan. You may
  enjoy [Local Artist]."
- TOO WARM: "Your taste is absolutely IMPECCABLE and speaks volumes about
  your soul."
- JUST RIGHT: "You've been deep in emotional-weight R&B — SZA, Jazmine,
  Summer Walker. [Local Artist] is your scene's answer. Same vulnerability,
  her own grain."

For each recommended artist, write a 1–2 sentence "why this matches you"
bridge that names at least one artist from the listener's actual top
artists. This is the whole trick — it has to feel earned.

Track roles:
- "discovery" = a track by a local/regional artist the listener hasn't heard.
  Pure new-to-them local music.
- "bridge" = a track where a local/regional artist connects to something the
  listener already knows. Examples: a local producer's beat that a known artist
  rapped on, a local artist's collab or feature with one of their favorites,
  a local artist's remix or cover of a song they love. The bridge track MUST
  still feature a local/regional artist — it's not just a comfort song from
  their existing rotation.

Hard rules:
- ALL 10 playlist tracks must feature local/regional artists. No exceptions.
  Every track should serve the mission of putting the listener on to their
  local scene.
- Every spotlight artist MUST be a real artist local/regional to the user's
  area. Prefer artists from the candidate pool when available, but you may
  use your own knowledge of real local artists if the pool is thin.
- Do not recommend artists already in the user's top artists list.
- Output strictly as JSON matching the schema. No prose outside the JSON."""

OUTPUT_SCHEMA = """{
  "taste_profile": {
    "headline": "string, max 8 words",
    "body": "string, 2–3 sentences, names ≥2 of their actual artists"
  },
  "spotlight_artists": [
    {
      "name": "string (must be from candidate_pool)",
      "one_liner": "string, 1 sentence on their sound",
      "why_this_matches_you": "string, 1–2 sentences, names ≥1 user top artist",
      "spotify_url": "string (from candidate_pool)"
    }
  ],
  "playlist": [
    {
      "track": "string",
      "artist": "string",
      "role": "bridge | discovery",
      "spotify_url": "string",
      "note": "string, optional, 1 line — for bridge tracks explain the local connection, for discovery tracks explain the sound"
    }
  ]
}"""


def build_user_prompt(taste_snapshot: dict, candidate_pool: list[dict], radius_tier: str) -> str:
    """Build the structured user prompt for Claude."""
    # Strip internal fields from candidates
    clean_candidates = []
    for c in candidate_pool:
        clean_candidates.append({
            "name": c["name"],
            "genres": c.get("genres", []),
            "spotify_id": c.get("spotify_id", ""),
            "spotify_url": c.get("spotify_url", ""),
            "origin_city": c.get("origin_city", ""),
            "origin_state": c.get("origin_state", ""),
        })

    pool_note = ""
    if len(clean_candidates) < 10:
        location = taste_snapshot.get("user_location", {})
        city = location.get("city", "")
        state = location.get("state", "")
        pool_note = f"""
<important>
The candidate pool is small or empty. Use your own knowledge to recommend
real artists who are local to {city}, {state} or the surrounding region and
who match this listener's taste. Every artist you recommend MUST be a real
artist with music on Spotify. Include their real Spotify URLs in the format
https://open.spotify.com/artist/ARTIST_ID. For tracks, use real track names
and real Spotify URLs in the format https://open.spotify.com/track/TRACK_ID.
Do NOT make up fake URLs or fake artists.
</important>
"""

    return f"""<taste_snapshot>
{json.dumps(taste_snapshot, indent=2)}
</taste_snapshot>

<candidate_pool>
{json.dumps(clean_candidates, indent=2)}
</candidate_pool>
{pool_note}
<config>
playlist_size: 10
bridge_tracks: 3
discovery_tracks: 7
spotlight_artists: 4
radius_tier: "{radius_tier}"
user_city: "{taste_snapshot.get('user_location', {}).get('city', '')}"
user_state: "{taste_snapshot.get('user_location', {}).get('state', '')}"
</config>

<output_schema>
{OUTPUT_SCHEMA}
</output_schema>"""


def call_claude(taste_snapshot: dict, candidate_pool: list[dict], radius_tier: str) -> dict:
    """Send taste + candidates to Claude and parse the JSON response."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_prompt = build_user_prompt(taste_snapshot, candidate_pool, radius_tier)
    logger.info("Calling Claude (candidate pool size: %d)", len(candidate_pool))

    for attempt in range(2):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            response_text = message.content[0].text.strip()

            # Strip markdown code fences if present
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                # Remove first and last lines (```json and ```)
                lines = [l for l in lines if not l.strip().startswith("```")]
                response_text = "\n".join(lines)

            result = json.loads(response_text)
            _validate_response(result)
            logger.info("Claude response parsed successfully")
            return result

        except json.JSONDecodeError as e:
            logger.warning("Claude returned non-JSON (attempt %d): %s", attempt + 1, e)
            if attempt == 0:
                # Retry with stricter instruction
                user_prompt += "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no prose, no code fences."
                continue
            raise RuntimeError(f"Claude failed to return valid JSON after 2 attempts: {e}")

        except ValueError as e:
            logger.warning("Claude response failed validation (attempt %d): %s", attempt + 1, e)
            if attempt == 0:
                user_prompt += f"\n\nYour previous response had a validation error: {e}. Please fix and try again."
                continue
            raise RuntimeError(f"Claude response failed validation after 2 attempts: {e}")

    raise RuntimeError("Claude call failed unexpectedly")


def _validate_response(data: dict):
    """Basic schema validation of Claude's response."""
    if "taste_profile" not in data:
        raise ValueError("Missing 'taste_profile'")
    if "headline" not in data["taste_profile"] or "body" not in data["taste_profile"]:
        raise ValueError("taste_profile missing headline or body")

    if "spotlight_artists" not in data or not data["spotlight_artists"]:
        raise ValueError("Missing or empty 'spotlight_artists'")
    for artist in data["spotlight_artists"]:
        for field in ("name", "one_liner", "why_this_matches_you", "spotify_url"):
            if field not in artist:
                raise ValueError(f"Spotlight artist missing '{field}'")

    if "playlist" not in data or not data["playlist"]:
        raise ValueError("Missing or empty 'playlist'")
    for track in data["playlist"]:
        for field in ("track", "artist", "role", "spotify_url"):
            if field not in track:
                raise ValueError(f"Playlist track missing '{field}'")
        if track["role"] not in ("bridge", "discovery"):
            raise ValueError(f"Invalid track role: {track['role']}")
