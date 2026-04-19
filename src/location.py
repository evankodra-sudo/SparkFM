"""IP geolocation + user confirmation for Spark Your Spotify."""

from __future__ import annotations

import logging
import sys

import requests

logger = logging.getLogger("spark")


def detect_location() -> dict:
    """Detect user's approximate location via IP geolocation."""
    try:
        resp = requests.get("http://ip-api.com/json/?fields=city,regionName,zip,lat,lon,countryCode", timeout=5)
        resp.raise_for_status()
        data = resp.json()

        if data.get("countryCode") and data["countryCode"] != "US":
            print("Spark Your Spotify is US-only for now. International coming soon.")
            sys.exit(1)

        return {
            "city": data.get("city", "Unknown"),
            "state": _state_abbrev(data.get("regionName", "")),
            "zip": data.get("zip", ""),
            "lat": data.get("lat", 0),
            "lng": data.get("lon", 0),
        }
    except Exception as e:
        logger.warning("IP geolocation failed: %s", e)
        return {"city": "Unknown", "state": "", "zip": "", "lat": 0, "lng": 0}


def confirm_location(location: dict) -> dict:
    """Ask user to confirm or correct their detected location."""
    city = location["city"]
    state = location["state"]
    zip_code = location["zip"]

    prompt = f"Detected location: {city}, {state} {zip_code}. Is this right? [Y/n/enter zip] "
    answer = input(prompt).strip()

    if not answer or answer.lower() in ("y", "yes"):
        return location

    if answer.lower() in ("n", "no"):
        new_zip = input("Enter your ZIP code: ").strip()
        return resolve_zip(new_zip)

    # Assume they entered a ZIP
    return resolve_zip(answer)


def resolve_zip(zip_code: str) -> dict:
    """Resolve a ZIP code to location data."""
    try:
        resp = requests.get(
            f"https://api.zippopotam.us/us/{zip_code}",
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        place = data["places"][0]
        return {
            "city": place.get("place name", "Unknown"),
            "state": place.get("state abbreviation", ""),
            "zip": zip_code,
            "lat": float(place.get("latitude", 0)),
            "lng": float(place.get("longitude", 0)),
        }
    except Exception as e:
        logger.warning("ZIP resolution failed for %s: %s", zip_code, e)
        print(f"Could not resolve ZIP {zip_code}. Using default.")
        return {"city": "Unknown", "state": "", "zip": zip_code, "lat": 0, "lng": 0}


# Mapping of full state names to abbreviations
_STATE_MAP = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


def _state_abbrev(name: str) -> str:
    """Convert a full state name to its abbreviation, or return as-is if already abbreviated."""
    if len(name) == 2 and name.isupper():
        return name
    return _STATE_MAP.get(name.lower(), name)
