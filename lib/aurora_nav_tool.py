"""
title: Navigation
author: Echo6
version: 1.1.0
description: Turn-by-turn directions and geocoding via Photon + Valhalla on recon-vm. Supports driving, walking, cycling, and truck routing with worldwide coverage (281M places).
"""

import re
import json
import requests
from pydantic import BaseModel, Field

_COORD_RE = re.compile(r'^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$')


class Tools:
    class Valves(BaseModel):
        photon_url: str = Field(
            default="http://100.64.0.24:2322",
            description="Photon geocoding service URL (recon-vm)",
        )
        valhalla_url: str = Field(
            default="http://100.64.0.24:8002",
            description="Valhalla routing service URL (recon-vm)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _geocode(self, query: str):
        m = _COORD_RE.match(query.strip())
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            return lat, lon, query
        resp = requests.get(
            f"{self.valves.photon_url}/api",
            params={"q": query, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            return None, None, None
        props = features[0]["properties"]
        coords = features[0]["geometry"]["coordinates"]
        parts = [props.get("name", "")]
        for key in ("city", "state", "country"):
            v = props.get(key)
            if v and v != parts[-1]:
                parts.append(v)
        return coords[1], coords[0], ", ".join(p for p in parts if p)

    def get_directions(
        self,
        origin: str,
        destination: str,
        mode: str = "auto",
    ) -> str:
        """
        Get turn-by-turn directions between two locations. When this tool returns results, present the directions exactly as returned — do not summarize or rephrase. Include all steps.

        :param origin: Starting location — address, place name, or lat,lon coordinates
        :param destination: Destination — address, place name, or lat,lon coordinates
        :param mode: Travel mode: auto, pedestrian, bicycle, or truck (default: auto)
        :return: Formatted turn-by-turn directions
        """
        if mode not in ("auto", "pedestrian", "bicycle", "truck"):
            mode = "auto"

        orig_lat, orig_lon, orig_name = self._geocode(origin)
        if orig_lat is None:
            return f"Could not find location: {origin}"

        dest_lat, dest_lon, dest_name = self._geocode(destination)
        if dest_lat is None:
            return f"Could not find location: {destination}"

        try:
            resp = requests.post(
                f"{self.valves.valhalla_url}/route",
                json={
                    "locations": [
                        {"lat": orig_lat, "lon": orig_lon},
                        {"lat": dest_lat, "lon": dest_lon},
                    ],
                    "costing": mode,
                    "directions_options": {"units": "miles"},
                },
                timeout=30,
            )
        except requests.RequestException:
            return "Navigation service unavailable"

        if resp.status_code != 200:
            return "No route found between locations"

        trip = resp.json()["trip"]
        summary = trip["summary"]
        legs = trip["legs"][0]["maneuvers"]

        miles = round(summary["length"], 1)
        minutes = round(summary["time"] / 60, 1)

        lines = [
            f"Directions from {orig_name} to {dest_name} ({mode}):",
            f"Distance: {miles} miles | Time: {minutes} minutes",
            "",
        ]
        for i, m in enumerate(legs, 1):
            inst = m["instruction"]
            dist = m.get("length", 0)
            if dist > 0:
                lines.append(f"{i}. {inst} — {round(dist, 1)} mi")
            else:
                lines.append(f"{i}. {inst}")

        return "\n".join(lines)
