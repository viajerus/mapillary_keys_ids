"""Retrieve image locations, altitudes, and capture dates from Mapillary."""

import os
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


## MAPILLARY API CONNECTION

def connect_to_mapillary(
    image_id: str | int,
    access_token: str | None = None,
    timeout: float = 30.0,
    *,
    session: requests.Session | None = None,
) -> requests.Response:
    """Fetch an image using a required Mapillary client or user access token.

    If access_token is omitted, read MAPILLARY_ACCESS_TOKEN from the
    environment or the .env file beside this module.
    Return the HTTP response. Invalid inputs raise ValueError; HTTP errors,
    timeouts, and connection failures propagate as requests exceptions.
    The timeout applies to connection establishment and socket reads.
    """
    request = set_image_request(image_id, access_token)
    client = session if session is not None else requests
    response = client.get(**request, timeout=timeout)
    response.raise_for_status()
    return response


## SET REQUESTS USING THE PUBLIC GRAPH API

def set_image_request(
    image_id: str | int, access_token: str | None = None
) -> dict[str, Any]:
    """Build keyword arguments for requests.get without making a network call.

    Mapillary's public image endpoint uses GET with a fields parameter.
    The token is sent in the Authorization header, never in the URL.
    Token priority: explicit argument, existing environment, then .env.
    """
    if isinstance(image_id, bool) or not isinstance(image_id, (str, int)):
        raise ValueError("image_id must be a positive integer or numeric string.")
    image_id = str(image_id).strip()
    if (
        not image_id
        or not image_id.isascii()
        or not image_id.isdecimal()
        or not image_id.strip("0")
    ):
        raise ValueError("image_id must be a positive integer or numeric string.")
    if access_token is None:
        load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"), override=False)
        access_token = os.getenv("MAPILLARY_ACCESS_TOKEN")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError(
            "A Mapillary access token is required. Set MAPILLARY_ACCESS_TOKEN "
            "in .env or the environment, or pass access_token explicitly."
        )
    access_token = access_token.strip()
    if any(character.isspace() for character in access_token):
        raise ValueError("access_token must not contain whitespace.")

    return {
        "url": f"https://graph.mapillary.com/{image_id}",
        "headers": {"Authorization": f"OAuth {access_token}"},
        "params": {"fields": "id,geometry,altitude,captured_at"},
    }


## RETRIEVE IMAGE METADATA (LAT, LON, ALTITUDE, CAPTURE DATE)

def get_image_metadata(
    image_id: str | int,
    access_token: str | None = None,
    timeout: float = 30.0,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Return image_id, lat, lon, altitude, capture_date, and captured_at.

    Coordinates come from the original image geometry (longitude first in
    GeoJSON). altitude is the original EXIF altitude in meters, or None when
    unavailable. captured_at is when the image was taken, as Unix milliseconds;
    capture_date represents the same instant as an ISO 8601 UTC timestamp.
    Missing required or malformed metadata raises ValueError, and network/HTTP
    errors propagate from requests.
    If access_token is omitted, use MAPILLARY_ACCESS_TOKEN from the
    environment or the .env file beside this module.
    """
    response = connect_to_mapillary(image_id, access_token, timeout=timeout, session=session)
    data = response.json()
    try:
        geometry = data["geometry"]
        if geometry["type"] != "Point":
            raise ValueError("Expected a GeoJSON Point.")
        longitude, latitude = geometry["coordinates"][:2]
        altitude = data.get("altitude")
        captured_at = data["captured_at"]
        numeric_values = (longitude, latitude, captured_at)
        if altitude is not None:
            numeric_values += (altitude,)
        for value in numeric_values:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
            ):
                raise ValueError("Coordinates, altitude, and capture time must be numeric.")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("Coordinates are outside geographic bounds.")
        capture_date = datetime.fromtimestamp(
            captured_at / 1000, tz=timezone.utc
        ).isoformat(timespec="milliseconds")
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError(
            "Mapillary returned missing or invalid image metadata."
        ) from exc

    return {
        "image_id": str(image_id).strip(),
        "lat": latitude,
        "lon": longitude,
        "altitude": altitude,
        "capture_date": capture_date,
        "captured_at": captured_at,
    }
