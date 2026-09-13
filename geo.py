"""
Great-circle geometry — one definition, three consumers.

haversine_mi lived in Fire/collector.py with a second copy in
ATProto/publisher.py, whose comment explained why: "the publisher ships in
its own image and cannot import it." That stopped being true when the ATProto
image started copying agent_runtime.py from the repo root, and adding a
second function to a duplicated pair would have made two copies into four —
the shape that produced the threshold drift this repo keeps citing.

Pure standard library, so the per-domain venvs and both images need nothing
new beyond a COPY line. check_image_files.py verifies those exist.
"""

import math

EARTH_RADIUS_MI = 3958.8

_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return EARTH_RADIUS_MI * 2 * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees true.

    The companion to haversine_mi, and it exists for the same reason: a
    distance without a direction is half a location. Fire records published a
    distance and no bearing, so on 2026-09-13 a synthesis advisory read
    "Steele Fire (~14mi NE of Napa)" with nothing in the pipeline able to say
    whether NE was right. The tools hand the agents latitude and longitude,
    and the agents were turning those into compass points in prose.

    Load-bearing rather than decorative. NE is the Diablo sector, and the same
    advisory warned Diablo season began in two days — a fire NE of the valley
    under offshore flow is a different risk from one to the southwest, and the
    difference was resting on mental arithmetic nobody could check.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass_point(bearing: float) -> str:
    """A bearing in degrees as one of the 16 conventional compass points.

    Sixteen rather than eight, because the distinction the fire prompts draw
    is NE/E against everything else. An eight-point rose would round an ENE
    detection into either E or NE and settle that question by rounding.
    """
    return _COMPASS[int((bearing / 22.5) + 0.5) % 16]


def direction_from(home_lat: float, home_lon: float,
                   lat, lon) -> tuple:
    """(bearing degrees, compass point) from home, or (None, None).

    None when either coordinate is missing or unparseable, so a caller
    records "we don't know" rather than publishing a direction derived from
    a null — absent and due-north are not the same claim.
    """
    try:
        b = bearing_deg(home_lat, home_lon, float(lat), float(lon))
    except (TypeError, ValueError):
        return None, None
    return round(b, 1), compass_point(b)
