"""Render the Spark Card HTML from Claude's output."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger("spark")

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def render_spark_card(
    claude_output: dict,
    user_handle: str,
    radius_label: str,
    candidate_pool: list[dict],
) -> Path:
    """Render the Spark Card HTML and return the output path."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("spark_card.html.j2")

    # Build a lookup for origin info from the candidate pool
    origin_lookup = {}
    for c in candidate_pool:
        origin_lookup[c["name"].lower()] = {
            "origin_city": c.get("origin_city", ""),
            "origin_state": c.get("origin_state", ""),
        }

    # Enrich spotlight artists with origin data
    spotlight = claude_output.get("spotlight_artists", [])
    for artist in spotlight:
        key = artist["name"].lower()
        if key in origin_lookup:
            artist["origin_city"] = origin_lookup[key].get("origin_city", "")
            artist["origin_state"] = origin_lookup[key].get("origin_state", "")

    context = {
        "user_handle": user_handle,
        "date": datetime.now().strftime("%B %d, %Y"),
        "taste_profile": claude_output.get("taste_profile", {}),
        "spotlight_artists": spotlight,
        "playlist": claude_output.get("playlist", []),
        "radius_label": radius_label,
    }

    html = template.render(**context)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = Path(f"spark-card-{timestamp}.html")
    output_path.write_text(html)

    logger.info("Spark Card rendered: %s", output_path)
    return output_path


def render_spark_card_html(
    claude_output: dict,
    user_handle: str,
    radius_label: str,
    candidate_pool: list[dict],
    user_location: dict = None,
) -> str:
    """Render the Spark Card HTML and return it as a string (for API use)."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("spark_card.html.j2")

    origin_lookup = {}
    for c in candidate_pool:
        origin_lookup[c["name"].lower()] = {
            "origin_city": c.get("origin_city", ""),
            "origin_state": c.get("origin_state", ""),
        }

    spotlight = claude_output.get("spotlight_artists", [])
    for artist in spotlight:
        key = artist["name"].lower()
        if key in origin_lookup:
            artist["origin_city"] = origin_lookup[key].get("origin_city", "")
            artist["origin_state"] = origin_lookup[key].get("origin_state", "")

    context = {
        "user_handle": user_handle,
        "date": datetime.now().strftime("%B %d, %Y"),
        "taste_profile": claude_output.get("taste_profile", {}),
        "spotlight_artists": spotlight,
        "playlist": claude_output.get("playlist", []),
        "radius_label": radius_label,
        "user_location": user_location or {},
    }

    return template.render(**context)
