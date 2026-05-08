"""
OFFROUTE Router — Wilderness to network path orchestration.

Connects the raster pathfinder (wilderness segment) to Valhalla (on-network segment).

Entry points are extracted from OSM highways and stored in /mnt/nav/navi.db.
The pathfinder routes from a wilderness start to the nearest entry point,
then Valhalla completes the route to the destination.
"""
import json
import math
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Literal

import numpy as np
import requests
from skimage.graph import MCP_Geometric

from .dem import DEMReader
from .cost import compute_cost_grid
from .friction import FrictionReader, friction_to_multiplier
from .barriers import BarrierReader
from .trails import TrailReader

# Paths
NAVI_DB_PATH = Path("/mnt/nav/navi.db")
OSM_PBF_PATH = Path("/mnt/nav/sources/idaho-latest.osm.pbf")

# Valhalla endpoint
VALHALLA_URL = "http://localhost:8002"

# Search radius for entry points (km)
DEFAULT_SEARCH_RADIUS_KM = 50
EXPANDED_SEARCH_RADIUS_KM = 100

# Memory limit
MEMORY_LIMIT_GB = 12

# Mode to Valhalla costing mapping
MODE_TO_COSTING = {
    "foot": "pedestrian",
    "mtb": "bicycle",
    "atv": "auto",
}


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in meters."""
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c


def check_memory_usage() -> float:
    """Check current memory usage in GB."""
    try:
        import psutil
        process = psutil.Process()
        return process.memory_info().rss / (1024**3)
    except ImportError:
        return 0


class EntryPointIndex:
    """
    Trail entry point index for wilderness-to-network handoff.

    Entry points are endpoints and intersections of OSM highways
    that connect wilderness areas to the routable network.
    """

    def __init__(self, db_path: Path = NAVI_DB_PATH):
        self.db_path = db_path
        self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def table_exists(self) -> bool:
        """Check if trail_entry_points table exists."""
        if not self.db_path.exists():
            return False
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trail_entry_points'"
        )
        return cur.fetchone() is not None

    def get_entry_point_count(self) -> int:
        """Get count of entry points."""
        if not self.table_exists():
            return 0
        conn = self._get_conn()
        cur = conn.execute("SELECT COUNT(*) FROM trail_entry_points")
        return cur.fetchone()[0]

    def query_bbox(self, south: float, north: float, west: float, east: float) -> List[Dict]:
        """Query entry points within a bounding box."""
        if not self.table_exists():
            return []

        conn = self._get_conn()
        cur = conn.execute("""
            SELECT id, lat, lon, highway_class, name
            FROM trail_entry_points
            WHERE lat >= ? AND lat <= ? AND lon >= ? AND lon <= ?
        """, (south, north, west, east))

        return [dict(row) for row in cur.fetchall()]

    def query_radius(self, lat: float, lon: float, radius_km: float) -> List[Dict]:
        """Query entry points within radius of a point."""
        # Approximate bbox for the radius
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))

        points = self.query_bbox(
            lat - lat_delta, lat + lat_delta,
            lon - lon_delta, lon + lon_delta
        )

        # Filter by actual distance and add distance field
        result = []
        for p in points:
            dist = haversine_distance(lat, lon, p['lat'], p['lon'])
            if dist <= radius_km * 1000:
                p['distance_m'] = dist
                result.append(p)

        return sorted(result, key=lambda x: x['distance_m'])

    def build_index(self, osm_pbf_path: Path = OSM_PBF_PATH) -> Dict:
        """
        Build the entry point index from OSM PBF.

        Extracts endpoints of highway features that connect to the network.
        """
        if not osm_pbf_path.exists():
            raise FileNotFoundError(f"OSM PBF not found: {osm_pbf_path}")

        print(f"Building trail entry point index from {osm_pbf_path}...")

        # Highway types to extract (routable network entry points)
        highway_types = [
            "primary", "secondary", "tertiary", "unclassified",
            "residential", "service", "track", "path", "footway", "bridleway"
        ]

        stats = {"total": 0, "by_class": {}}

        with tempfile.TemporaryDirectory() as tmpdir:
            # Extract highways to GeoJSON
            geojson_path = Path(tmpdir) / "highways.geojson"

            # Build osmium tags-filter expressions (one per highway type)
            print(f"  Extracting highways with osmium...")
            cmd = [
                "osmium", "tags-filter",
                str(osm_pbf_path),
            ]
            # Add each highway type as a separate filter expression
            for ht in highway_types:
                cmd.append(f"w/highway={ht}")
            cmd.extend(["-o", str(Path(tmpdir) / "filtered.osm.pbf"), "--overwrite"])

            subprocess.run(cmd, check=True, capture_output=True)

            # Convert to GeoJSON
            print(f"  Converting to GeoJSON with ogr2ogr...")
            cmd = [
                "ogr2ogr", "-f", "GeoJSON",
                str(geojson_path),
                str(Path(tmpdir) / "filtered.osm.pbf"),
                "lines",
                "-t_srs", "EPSG:4326"
            ]
            subprocess.run(cmd, check=True, capture_output=True)

            # Parse GeoJSON and extract endpoints
            print(f"  Extracting entry points...")
            with open(geojson_path) as f:
                data = json.load(f)

            # Collect unique points (endpoints)
            # Key: (lat, lon) rounded to 5 decimal places (~1m precision)
            points = {}

            for feature in data.get("features", []):
                props = feature.get("properties", {})
                geom = feature.get("geometry", {})

                if geom.get("type") != "LineString":
                    continue

                coords = geom.get("coordinates", [])
                if len(coords) < 2:
                    continue

                highway_class = props.get("highway", "unknown")
                name = props.get("name", "")

                # Extract endpoints
                for coord in [coords[0], coords[-1]]:
                    lon, lat = coord[0], coord[1]
                    key = (round(lat, 5), round(lon, 5))

                    if key not in points:
                        points[key] = {
                            "lat": lat,
                            "lon": lon,
                            "highway_class": highway_class,
                            "name": name
                        }
                    else:
                        # Keep the "best" highway class (roads > tracks > paths)
                        existing = points[key]
                        if self._highway_priority(highway_class) < self._highway_priority(existing["highway_class"]):
                            points[key]["highway_class"] = highway_class
                        if name and not existing["name"]:
                            points[key]["name"] = name

        # Create/update database
        print(f"  Writing {len(points)} entry points to {self.db_path}...")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()

        # Create table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trail_entry_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                highway_class TEXT NOT NULL,
                name TEXT
            )
        """)

        # Clear existing data
        conn.execute("DELETE FROM trail_entry_points")

        # Insert new points
        for point in points.values():
            conn.execute("""
                INSERT INTO trail_entry_points (lat, lon, highway_class, name)
                VALUES (?, ?, ?, ?)
            """, (point["lat"], point["lon"], point["highway_class"], point["name"]))

            stats["total"] += 1
            hc = point["highway_class"]
            stats["by_class"][hc] = stats["by_class"].get(hc, 0) + 1

        # Create spatial index
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entry_lat ON trail_entry_points(lat)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entry_lon ON trail_entry_points(lon)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entry_latlon ON trail_entry_points(lat, lon)")

        conn.commit()

        print(f"  Done. Total: {stats['total']} entry points")
        for hc, count in sorted(stats["by_class"].items(), key=lambda x: -x[1]):
            print(f"    {hc}: {count}")

        return stats

    def _highway_priority(self, highway_class: str) -> int:
        """Lower number = better priority for entry points."""
        priority = {
            "primary": 1, "secondary": 2, "tertiary": 3,
            "unclassified": 4, "residential": 5, "service": 6,
            "track": 7, "path": 8, "footway": 9, "bridleway": 10
        }
        return priority.get(highway_class, 99)

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class OffrouteRouter:
    """
    OFFROUTE Router — orchestrates wilderness pathfinding and Valhalla stitching.
    """

    def __init__(self):
        self.dem_reader = None
        self.friction_reader = None
        self.barrier_reader = None
        self.trail_reader = None
        self.entry_index = EntryPointIndex()

    def _init_readers(self):
        """Lazy init readers."""
        if self.dem_reader is None:
            self.dem_reader = DEMReader()
        if self.friction_reader is None:
            self.friction_reader = FrictionReader()
        if self.barrier_reader is None:
            self.barrier_reader = BarrierReader()
        if self.trail_reader is None:
            self.trail_reader = TrailReader()

    def route(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        mode: Literal["foot", "mtb", "atv"] = "foot",
        boundary_mode: Literal["strict", "pragmatic", "emergency"] = "pragmatic"
    ) -> Dict:
        """
        Route from a wilderness start point to a destination.

        Returns a GeoJSON FeatureCollection with wilderness and network segments.
        """
        t0 = time.time()

        # Ensure entry point index exists
        if not self.entry_index.table_exists() or self.entry_index.get_entry_point_count() == 0:
            return {
                "status": "error",
                "message": "Trail entry point index not built. Run build_entry_index() first."
            }

        # Find entry points near start
        entry_points = self.entry_index.query_radius(
            start_lat, start_lon, DEFAULT_SEARCH_RADIUS_KM
        )

        if not entry_points:
            # Try expanded radius
            entry_points = self.entry_index.query_radius(
                start_lat, start_lon, EXPANDED_SEARCH_RADIUS_KM
            )
            if not entry_points:
                return {
                    "status": "error",
                    "message": f"No trail entry points found within {EXPANDED_SEARCH_RADIUS_KM}km of start"
                }

        # Build bbox for pathfinding grid
        # Include start, end, and all entry points
        all_lats = [start_lat, end_lat] + [p["lat"] for p in entry_points]
        all_lons = [start_lon, end_lon] + [p["lon"] for p in entry_points]

        padding = 0.05  # ~5km padding
        bbox = {
            "south": min(all_lats) - padding,
            "north": max(all_lats) + padding,
            "west": min(all_lons) - padding,
            "east": max(all_lons) + padding,
        }

        # Initialize readers
        self._init_readers()

        # Load elevation
        try:
            elevation, meta = self.dem_reader.get_elevation_grid(
                south=bbox["south"],
                north=bbox["north"],
                west=bbox["west"],
                east=bbox["east"],
            )
        except Exception as e:
            return {"status": "error", "message": f"Failed to load elevation: {e}"}

        # Check memory
        mem = check_memory_usage()
        if mem > MEMORY_LIMIT_GB:
            return {"status": "error", "message": f"Memory limit exceeded: {mem:.1f}GB > {MEMORY_LIMIT_GB}GB"}

        # Load friction
        friction_raw = self.friction_reader.get_friction_grid(
            south=bbox["south"],
            north=bbox["north"],
            west=bbox["west"],
            east=bbox["east"],
            target_shape=elevation.shape
        )
        friction_mult = friction_to_multiplier(friction_raw)

        # Load barriers
        barriers = self.barrier_reader.get_barrier_grid(
            south=bbox["south"],
            north=bbox["north"],
            west=bbox["west"],
            east=bbox["east"],
            target_shape=elevation.shape
        )

        # Load trails
        trails = self.trail_reader.get_trails_grid(
            south=bbox["south"],
            north=bbox["north"],
            west=bbox["west"],
            east=bbox["east"],
            target_shape=elevation.shape
        )

        # Compute cost grid
        cost = compute_cost_grid(
            elevation,
            cell_size_m=meta["cell_size_m"],
            friction=friction_mult,
            trails=trails,
            barriers=barriers,
            boundary_mode=boundary_mode,
        )

        # Convert start to pixel coordinates
        start_row, start_col = self.dem_reader.latlon_to_pixel(start_lat, start_lon, meta)

        # Validate start is in bounds
        rows, cols = elevation.shape
        if not (0 <= start_row < rows and 0 <= start_col < cols):
            return {"status": "error", "message": "Start point outside grid bounds"}

        # Mark entry points on the grid
        entry_pixels = []
        for ep in entry_points:
            row, col = self.dem_reader.latlon_to_pixel(ep["lat"], ep["lon"], meta)
            if 0 <= row < rows and 0 <= col < cols:
                entry_pixels.append({
                    "row": row,
                    "col": col,
                    "entry_point": ep
                })

        if not entry_pixels:
            return {"status": "error", "message": "No entry points map to grid bounds"}

        # Run MCP pathfinder
        mcp = MCP_Geometric(cost, fully_connected=True)
        cumulative_costs, traceback = mcp.find_costs([(start_row, start_col)])

        # Find nearest reachable entry point
        best_entry = None
        best_cost = np.inf

        for ep in entry_pixels:
            ep_cost = cumulative_costs[ep["row"], ep["col"]]
            if ep_cost < best_cost:
                best_cost = ep_cost
                best_entry = ep

        if best_entry is None or np.isinf(best_cost):
            return {
                "status": "error",
                "message": "No path found to any entry point (blocked by impassable terrain)"
            }

        # Traceback wilderness path
        path_indices = mcp.traceback((best_entry["row"], best_entry["col"]))

        # Convert to coordinates and collect stats
        wilderness_coords = []
        elevations = []
        trail_values = []

        for row, col in path_indices:
            lat, lon = self.dem_reader.pixel_to_latlon(row, col, meta)
            wilderness_coords.append([lon, lat])
            elevations.append(elevation[row, col])
            trail_values.append(trails[row, col])

        # Calculate wilderness segment stats
        wilderness_distance_m = 0
        for i in range(1, len(wilderness_coords)):
            lon1, lat1 = wilderness_coords[i-1]
            lon2, lat2 = wilderness_coords[i]
            wilderness_distance_m += haversine_distance(lat1, lon1, lat2, lon2)

        elev_arr = np.array(elevations)
        elev_diff = np.diff(elev_arr)
        wilderness_gain = float(np.sum(elev_diff[elev_diff > 0]))
        wilderness_loss = float(np.sum(np.abs(elev_diff[elev_diff < 0])))

        trail_arr = np.array(trail_values)
        on_trail_cells = np.sum(trail_arr > 0)
        total_cells = len(trail_arr)
        on_trail_pct = float(100 * on_trail_cells / total_cells) if total_cells > 0 else 0

        # Entry point reached
        entry_lat = best_entry["entry_point"]["lat"]
        entry_lon = best_entry["entry_point"]["lon"]
        entry_class = best_entry["entry_point"]["highway_class"]
        entry_name = best_entry["entry_point"].get("name", "")

        # Call Valhalla for on-network segment
        valhalla_costing = MODE_TO_COSTING.get(mode, "pedestrian")

        valhalla_request = {
            "locations": [
                {"lat": entry_lat, "lon": entry_lon},
                {"lat": end_lat, "lon": end_lon}
            ],
            "costing": valhalla_costing,
            "directions_options": {"units": "kilometers"}
        }

        network_segment = None
        valhalla_error = None

        try:
            resp = requests.post(
                f"{VALHALLA_URL}/route",
                json=valhalla_request,
                timeout=30
            )

            if resp.status_code == 200:
                valhalla_data = resp.json()
                trip = valhalla_data.get("trip", {})
                legs = trip.get("legs", [])

                if legs:
                    leg = legs[0]
                    shape = leg.get("shape", "")

                    # Decode polyline6
                    network_coords = self._decode_polyline(shape)

                    # Extract maneuvers
                    maneuvers = []
                    for m in leg.get("maneuvers", []):
                        maneuvers.append({
                            "instruction": m.get("instruction", ""),
                            "type": m.get("type", 0),
                            "distance_km": m.get("length", 0),
                            "time_seconds": m.get("time", 0),
                            "street_names": m.get("street_names", []),
                        })

                    summary = trip.get("summary", {})
                    network_segment = {
                        "coordinates": network_coords,
                        "distance_km": summary.get("length", 0),
                        "duration_minutes": summary.get("time", 0) / 60,
                        "maneuvers": maneuvers,
                    }
            else:
                valhalla_error = f"Valhalla returned {resp.status_code}: {resp.text[:200]}"

        except Exception as e:
            valhalla_error = f"Valhalla request failed: {e}"

        # Build response
        features = []

        # Feature 1: Wilderness segment
        wilderness_feature = {
            "type": "Feature",
            "properties": {
                "segment_type": "wilderness",
                "effort_minutes": float(best_cost / 60),
                "distance_km": float(wilderness_distance_m / 1000),
                "elevation_gain_m": wilderness_gain,
                "elevation_loss_m": wilderness_loss,
                "boundary_mode": boundary_mode,
                "on_trail_pct": on_trail_pct,
                "cell_count": total_cells,
            },
            "geometry": {
                "type": "LineString",
                "coordinates": wilderness_coords,
            }
        }
        features.append(wilderness_feature)

        # Feature 2: Network segment (if available)
        if network_segment:
            network_feature = {
                "type": "Feature",
                "properties": {
                    "segment_type": "network",
                    "distance_km": network_segment["distance_km"],
                    "duration_minutes": network_segment["duration_minutes"],
                    "maneuvers": network_segment["maneuvers"],
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": network_segment["coordinates"],
                }
            }
            features.append(network_feature)

        # Build combined route coordinates
        combined_coords = wilderness_coords.copy()
        if network_segment:
            # Skip first point of network segment (it's the same as last wilderness point)
            combined_coords.extend(network_segment["coordinates"][1:])

        # Feature 3: Combined route
        combined_feature = {
            "type": "Feature",
            "properties": {
                "segment_type": "combined",
                "mode": mode,
                "boundary_mode": boundary_mode,
            },
            "geometry": {
                "type": "LineString",
                "coordinates": combined_coords,
            }
        }
        features.append(combined_feature)

        geojson = {
            "type": "FeatureCollection",
            "features": features,
        }

        # Build summary
        total_distance_km = wilderness_distance_m / 1000
        total_effort_minutes = best_cost / 60

        if network_segment:
            total_distance_km += network_segment["distance_km"]
            total_effort_minutes += network_segment["duration_minutes"]

        summary = {
            "total_distance_km": float(total_distance_km),
            "total_effort_minutes": float(total_effort_minutes),
            "wilderness_distance_km": float(wilderness_distance_m / 1000),
            "wilderness_effort_minutes": float(best_cost / 60),
            "network_distance_km": float(network_segment["distance_km"]) if network_segment else 0,
            "network_duration_minutes": float(network_segment["duration_minutes"]) if network_segment else 0,
            "on_trail_pct": on_trail_pct,
            "boundary_mode": boundary_mode,
            "mode": mode,
            "entry_point": {
                "lat": entry_lat,
                "lon": entry_lon,
                "highway_class": entry_class,
                "name": entry_name,
            },
            "computation_time_s": time.time() - t0,
        }

        result = {
            "status": "ok",
            "route": geojson,
            "summary": summary,
        }

        if valhalla_error:
            result["warning"] = f"Network segment incomplete: {valhalla_error}"

        return result

    def _decode_polyline(self, encoded: str, precision: int = 6) -> List[List[float]]:
        """Decode a polyline string into coordinates [lon, lat]."""
        coords = []
        index = 0
        lat = 0
        lon = 0

        while index < len(encoded):
            # Latitude
            shift = 0
            result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            dlat = ~(result >> 1) if result & 1 else result >> 1
            lat += dlat

            # Longitude
            shift = 0
            result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            dlon = ~(result >> 1) if result & 1 else result >> 1
            lon += dlon

            coords.append([lon / (10 ** precision), lat / (10 ** precision)])

        return coords

    def close(self):
        """Close all readers."""
        if self.dem_reader:
            self.dem_reader.close()
        if self.friction_reader:
            self.friction_reader.close()
        if self.barrier_reader:
            self.barrier_reader.close()
        if self.trail_reader:
            self.trail_reader.close()
        self.entry_index.close()


def build_entry_index():
    """Build the trail entry point index."""
    index = EntryPointIndex()
    stats = index.build_index()
    index.close()
    return stats


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "build":
        print("Building trail entry point index...")
        stats = build_entry_index()
        print(f"\nDone. Total entry points: {stats['total']}")

    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Testing router...")

        router = OffrouteRouter()

        # Test route: wilderness to Twin Falls
        result = router.route(
            start_lat=42.35,
            start_lon=-114.30,
            end_lat=42.5629,
            end_lon=-114.4609,
            mode="foot",
            boundary_mode="pragmatic"
        )

        print(json.dumps(result, indent=2, default=str))
        router.close()

    else:
        print("Usage:")
        print("  python router.py build   # Build entry point index")
        print("  python router.py test    # Test route")
