"""
OFFROUTE Router — Bidirectional wilderness-to-network path orchestration.

Supports four routing scenarios:
  A: off-network start → on-network end (wilderness then Valhalla)
  B: off-network start → off-network end (wilderness, Valhalla, wilderness)
  C: on-network start → off-network end (Valhalla then wilderness)
  D: on-network start → on-network end (pure Valhalla passthrough)

Off-network detection: Valhalla /locate snap distance > 500m = off-network.

IMPORTANT: The wilderness segment ALWAYS uses foot mode for pathfinding.
The user's selected mode affects:
  1. Which entry points are valid (foot=any, mtb=tracks+roads, vehicle=roads only)
  2. The Valhalla costing profile for the network segment
"""
import gc
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Literal, Set

import numpy as np
import requests
import psycopg2
import psycopg2.extras
from shapely.geometry import LineString
from skimage.graph import MCP_Geometric

from .dem import DEMReader
from .cost import compute_cost_grid
from .friction import FrictionReader, friction_to_multiplier
from .barriers import BarrierReader, WildernessReader, DEFAULT_WILDERNESS_PATH
from .trails import TrailReader
from .mvum import get_mvum_access_grid
from ..deployment_config import get_deployment_config

# Load configuration
_deploy_config = get_deployment_config()
_offroute_config = _deploy_config.get("offroute", {})

# Paths (configurable via home.yaml)
OSM_PBF_PATH = Path(_offroute_config.get("osm_pbf_path", "/mnt/nav/sources/idaho-latest.osm.pbf"))
DENSIFY_INTERVAL_M = _offroute_config.get("densify_interval_m", 100)
POSTGIS_DSN = _offroute_config.get("postgis_dsn", "dbname=padus user=postgres")

# Legacy SQLite path (still used by MVUM)
NAVI_DB_PATH = Path("/mnt/nav/navi.db")

# Valhalla endpoint
VALHALLA_URL = "http://localhost:8002"

# Search radius for entry points (km)
DEFAULT_SEARCH_RADIUS_KM = 50
EXPANDED_SEARCH_RADIUS_KM = 100

# Memory limit
MEMORY_LIMIT_GB = 12

# Off-network detection threshold (meters)
OFF_NETWORK_THRESHOLD_M = 10

# Mode to Valhalla costing mapping
MODE_TO_COSTING = {
    "auto": "auto",
    "foot": "pedestrian",
    "mtb": "bicycle",
    "atv": "auto",
    "vehicle": "auto",
}

# Mode to valid entry point highway classes
# foot = any trail/track/road, mtb = tracks and roads, vehicle = roads only
MODE_TO_VALID_HIGHWAYS = {
    "auto": {"primary", "secondary", "tertiary", "unclassified", "residential",
             "service"},
    "foot": {"primary", "secondary", "tertiary", "unclassified", "residential",
             "service", "track", "path", "footway", "bridleway"},
    "mtb": {"primary", "secondary", "tertiary", "unclassified", "residential",
            "service", "track"},
    "atv": {"primary", "secondary", "tertiary", "unclassified", "residential",
            "service", "track"},
    "vehicle": {"primary", "secondary", "tertiary", "unclassified", "residential",
                "service"},
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
    PostGIS-backed spatial index of road/trail entry points.
    Uses ST_DWithin for fast radius queries with meter-accurate distances.
    Densifies highway LineStrings at 100m intervals for better coverage.
    """

    def __init__(self, dsn: str = None):
        self.dsn = dsn or POSTGIS_DSN
        self._conn: Optional[psycopg2.extensions.connection] = None

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.dsn)
        return self._conn

    def table_exists(self) -> bool:
        """Check if entry_points table exists."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'entry_points'
                )
            """)
            return cur.fetchone()[0]

    def get_entry_point_count(self) -> int:
        """Return the number of entry points in the index."""
        if not self.table_exists():
            return 0
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM entry_points")
            return cur.fetchone()[0]

    def query_bbox(
        self,
        south: float,
        north: float,
        west: float,
        east: float,
        valid_highways: Optional[Set[str]] = None
    ) -> List[Dict]:
        """Find entry points within a bounding box."""
        if not self.table_exists():
            return []

        conn = self._get_conn()

        highway_filter = ""
        params = [west, south, east, north]
        if valid_highways:
            placeholders = ','.join(['%s'] * len(valid_highways))
            highway_filter = f"AND highway_class IN ({placeholders})"
            params.extend(list(valid_highways))

        query = f"""
            SELECT
                id,
                ST_Y(geom) as lat,
                ST_X(geom) as lon,
                highway_class,
                name,
                land_status
            FROM entry_points
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
            {highway_filter}
        """

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def query_radius(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        valid_highways: Optional[Set[str]] = None,
        limit: int = 50
    ) -> List[Dict]:
        """
        Find entry points within radius_km of (lat, lon).
        Uses PostGIS ST_DWithin with geography cast for meter-accurate distance.
        """
        if not self.table_exists():
            return []

        conn = self._get_conn()
        radius_m = radius_km * 1000

        # Build query with optional highway filter
        highway_filter = ""
        params = [lon, lat, lon, lat, radius_m]
        if valid_highways:
            placeholders = ','.join(['%s'] * len(valid_highways))
            highway_filter = f"AND highway_class IN ({placeholders})"
            params.extend(list(valid_highways))
        params.append(limit)

        query = f"""
            SELECT
                id,
                ST_Y(geom) as lat,
                ST_X(geom) as lon,
                highway_class,
                name,
                land_status,
                ST_Distance(
                    geom::geography,
                    ST_SetSRID(ST_Point(%s, %s), 4326)::geography
                ) as distance_m
            FROM entry_points
            WHERE ST_DWithin(
                geom::geography,
                ST_SetSRID(ST_Point(%s, %s), 4326)::geography,
                %s
            )
            {highway_filter}
            ORDER BY distance_m
            LIMIT %s
        """

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def build_index(self, osm_pbf_path: Path = None) -> Dict:
        """
        Build the entry point index from OSM PBF.
        Densifies LineStrings to sample points every 100m.
        Tags points with land_status from PAD-US.
        """
        if osm_pbf_path is None:
            osm_pbf_path = OSM_PBF_PATH

        if not osm_pbf_path.exists():
            raise FileNotFoundError(f"OSM PBF not found: {osm_pbf_path}")

        print(f"Building entry point index from {osm_pbf_path}...")
        start_time = time.time()

        highway_types = [
            "primary", "secondary", "tertiary", "unclassified",
            "residential", "service", "track", "path", "footway", "bridleway"
        ]

        stats = {"total": 0, "by_class": {}, "lines_processed": 0}

        with tempfile.TemporaryDirectory() as tmpdir:
            geojson_path = Path(tmpdir) / "highways.geojson"

            # Extract highways with osmium
            print("  Extracting highways with osmium...")
            cmd = ["osmium", "tags-filter", str(osm_pbf_path)]
            for ht in highway_types:
                cmd.append(f"w/highway={ht}")
            cmd.extend(["-o", str(Path(tmpdir) / "filtered.osm.pbf"), "--overwrite"])
            subprocess.run(cmd, check=True, capture_output=True)

            # Convert to GeoJSON
            print("  Converting to GeoJSON with ogr2ogr...")
            cmd = [
                "ogr2ogr", "-f", "GeoJSON",
                str(geojson_path),
                str(Path(tmpdir) / "filtered.osm.pbf"),
                "lines", "-t_srs", "EPSG:4326"
            ]
            subprocess.run(cmd, check=True, capture_output=True)

            # Load GeoJSON
            print("  Loading GeoJSON...")
            with open(geojson_path) as f:
                data = json.load(f)

            # Process features and densify
            print(f"  Densifying LineStrings at {DENSIFY_INTERVAL_M}m intervals...")
            points_to_insert = []
            seen_keys = set()

            features = data.get("features", [])
            total_features = len(features)

            for idx, feature in enumerate(features):
                if idx > 0 and idx % 100000 == 0:
                    print(f"    Processed {idx}/{total_features} features...")

                props = feature.get("properties", {})
                geom = feature.get("geometry", {})

                if geom.get("type") != "LineString":
                    continue

                coords = geom.get("coordinates", [])
                if len(coords) < 2:
                    continue

                highway_class = props.get("highway", "unknown")
                name = props.get("name", "")
                stats["lines_processed"] += 1

                # Densify this LineString
                densified = self._densify_line(coords, DENSIFY_INTERVAL_M)

                for lon, lat in densified:
                    # Deduplicate by rounding to 5 decimal places (~1m precision)
                    key = (round(lat, 5), round(lon, 5))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    points_to_insert.append((lon, lat, highway_class, name))

        # Insert into PostGIS
        print(f"  Inserting {len(points_to_insert)} entry points into PostGIS...")
        conn = self._get_conn()

        with conn.cursor() as cur:
            # Truncate existing data
            cur.execute("TRUNCATE entry_points RESTART IDENTITY")

            # Batch insert with execute_values for speed
            batch_size = 50000
            for i in range(0, len(points_to_insert), batch_size):
                batch = points_to_insert[i:i+batch_size]
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO entry_points (geom, highway_class, name)
                    VALUES %s
                    """,
                    batch,
                    template="(ST_SetSRID(ST_Point(%s, %s), 4326), %s, %s)",
                    page_size=10000
                )
                if i > 0 and i % 500000 == 0:
                    print(f"    Inserted {i}/{len(points_to_insert)} points...")

        conn.commit()

        # Tag land_status from PAD-US
        print("  Tagging land_status from PAD-US subdivided polygons...")
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE entry_points e
                SET land_status = 'public'
                FROM padus_sub p
                WHERE ST_Intersects(e.geom, p.geom)
            """)
            public_count = cur.rowcount
            print(f"    Tagged {public_count} points as public land")

        conn.commit()

        # Gather stats
        elapsed = time.time() - start_time
        stats["total"] = len(points_to_insert)
        stats["build_time_sec"] = round(elapsed, 1)

        for lon, lat, hc, name in points_to_insert:
            stats["by_class"][hc] = stats["by_class"].get(hc, 0) + 1

        print(f"  Done in {elapsed:.1f}s. Total: {stats['total']} entry points from {stats['lines_processed']} lines")
        for hc, count in sorted(stats["by_class"].items(), key=lambda x: -x[1]):
            print(f"    {hc}: {count}")

        return stats

    def _densify_line(self, coords: List[List[float]], interval_m: float) -> List[tuple]:
        """
        Sample points along a LineString at regular intervals.
        coords: [[lon, lat], ...] in GeoJSON order
        Returns: [(lon, lat), ...] sampled points including first and last
        """
        if len(coords) < 2:
            return [(coords[0][0], coords[0][1])] if coords else []

        # Calculate line length in meters using haversine on segments
        total_m = 0
        for i in range(len(coords) - 1):
            lon1, lat1 = coords[i]
            lon2, lat2 = coords[i + 1]
            total_m += haversine_distance(lat1, lon1, lat2, lon2)

        if total_m == 0:
            return [(coords[0][0], coords[0][1])]

        # Create Shapely LineString
        line = LineString(coords)

        # Calculate number of points needed
        n_points = max(2, int(total_m / interval_m) + 1)

        # Sample using normalized interpolation
        result = []
        for i in range(n_points):
            fraction = min(i / (n_points - 1), 1.0) if n_points > 1 else 0
            point = line.interpolate(fraction, normalized=True)
            result.append((point.x, point.y))  # (lon, lat)

        # Always ensure first and last original coordinates are included
        first_coord = (coords[0][0], coords[0][1])
        last_coord = (coords[-1][0], coords[-1][1])

        if result[0] != first_coord:
            result[0] = first_coord
        if result[-1] != last_coord:
            result[-1] = last_coord

        return result

    def _highway_priority(self, highway_class: str) -> int:
        """Lower number = better priority for entry points."""
        priority = {
            "primary": 1, "secondary": 2, "tertiary": 3,
            "unclassified": 4, "residential": 5, "service": 6,
            "track": 7, "path": 8, "footway": 9, "bridleway": 10
        }
        return priority.get(highway_class, 99)

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None


class OffrouteRouter:
    """
    OFFROUTE Router — orchestrates wilderness pathfinding and Valhalla stitching.

    Supports four scenarios:
      A: off-network start → on-network end
      B: off-network start → off-network end
      C: on-network start → off-network end
      D: on-network start → on-network end (pure Valhalla)

    IMPORTANT: Wilderness segment ALWAYS uses foot mode for pathfinding.
    User's mode affects entry point selection and Valhalla costing only.
    """

    def __init__(self):
        self.dem_reader = None
        self.friction_reader = None
        self.barrier_reader = None
        self.wilderness_reader = None
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
        if self.wilderness_reader is None and DEFAULT_WILDERNESS_PATH.exists():
            self.wilderness_reader = WildernessReader()
        if self.trail_reader is None:
            self.trail_reader = TrailReader()

    def _locate_on_network(self, lat: float, lon: float, mode: str) -> Dict:
        """
        Check if a point is on the routable network using Valhalla's /locate.

        Returns:
            {
                "on_network": bool,
                "snap_distance_m": float,
                "snapped_lat": float,
                "snapped_lon": float
            }
        """
        costing = MODE_TO_COSTING.get(mode, "pedestrian")
        try:
            resp = requests.post(
                f"{VALHALLA_URL}/locate",
                json={"locations": [{"lat": lat, "lon": lon}], "costing": costing},
                timeout=10
            )

            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0 and data[0].get("edges"):
                    edge = data[0]["edges"][0]
                    snap_lat = edge.get("correlated_lat", lat)
                    snap_lon = edge.get("correlated_lon", lon)
                    snap_dist = haversine_distance(lat, lon, snap_lat, snap_lon)
                    return {
                        "on_network": snap_dist <= OFF_NETWORK_THRESHOLD_M,
                        "snap_distance_m": snap_dist,
                        "snapped_lat": snap_lat,
                        "snapped_lon": snap_lon
                    }
        except Exception:
            pass

        return {
            "on_network": False,
            "snap_distance_m": float('inf'),
            "snapped_lat": lat,
            "snapped_lon": lon
        }

    def route(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        mode: Literal["foot", "mtb", "atv", "vehicle"] = "foot",
        boundary_mode: Literal["strict", "pragmatic", "emergency"] = "pragmatic"
    ) -> Dict:
        """
        Route between two points, handling all four scenarios.

        Scenarios:
            A: off-network start → on-network end (wilderness then network)
            B: off-network start → off-network end (wilderness, network, wilderness)
            C: on-network start → off-network end (network then wilderness)
            D: on-network start → on-network end (pure network)

        Args:
            start_lat, start_lon: Starting coordinates
            end_lat, end_lon: Destination coordinates
            mode: Travel mode (foot, mtb, atv, vehicle)
            boundary_mode: How to handle private land (strict, pragmatic, emergency)

        Returns a GeoJSON FeatureCollection with route segments.
        """
        if mode not in MODE_TO_COSTING:
            return {"status": "error", "message": f"Unknown mode: {mode}"}

        # Detect network status for both endpoints
        start_status = self._locate_on_network(start_lat, start_lon, mode)
        end_status = self._locate_on_network(end_lat, end_lon, mode)

        start_off_network = not start_status["on_network"]
        end_off_network = not end_status["on_network"]

        # Dispatch to appropriate handler
        if not start_off_network and not end_off_network:
            # Scenario D: on-network → on-network (pure Valhalla)
            return self._route_D_network_only(
                start_lat, start_lon, end_lat, end_lon, mode
            )
        elif not start_off_network and end_off_network:
            # Scenario C: on-network → off-network
            return self._route_C_network_to_wilderness(
                start_lat, start_lon, end_lat, end_lon, mode, boundary_mode
            )
        elif start_off_network and not end_off_network:
            # Scenario A: off-network → on-network
            return self._route_A_wilderness_to_network(
                start_lat, start_lon, end_lat, end_lon, mode, boundary_mode
            )
        else:
            # Scenario B: off-network → off-network
            return self._route_B_wilderness_both(
                start_lat, start_lon, end_lat, end_lon, mode, boundary_mode
            )

    def _route_D_network_only(
        self,
        start_lat: float, start_lon: float,
        end_lat: float, end_lon: float,
        mode: str
    ) -> Dict:
        """
        Scenario D: Both endpoints on-network. Pure Valhalla routing.
        """
        t0 = time.time()
        costing = MODE_TO_COSTING.get(mode, "pedestrian")

        valhalla_request = {
            "locations": [
                {"lat": start_lat, "lon": start_lon},
                {"lat": end_lat, "lon": end_lon}
            ],
            "costing": costing,
            "directions_options": {"units": "kilometers"}
        }

        try:
            resp = requests.post(f"{VALHALLA_URL}/route", json=valhalla_request, timeout=30)

            if resp.status_code != 200:
                return {
                    "status": "error",
                    "message": f"Network routing failed: {resp.text[:200]}"
                }

            valhalla_data = resp.json()
            trip = valhalla_data.get("trip", {})
            legs = trip.get("legs", [])

            if not legs:
                return {"status": "error", "message": "No route found"}

            leg = legs[0]
            shape = leg.get("shape", "")
            network_coords = self._decode_polyline(shape)

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
            distance_km = summary.get("length", 0)
            duration_min = summary.get("time", 0) / 60

            # Build response in same format as wilderness routes
            network_feature = {
                "type": "Feature",
                "properties": {
                    "segment_type": "network",
                    "distance_km": distance_km,
                    "duration_minutes": duration_min,
                    "maneuvers": maneuvers,
                    "network_mode": mode,
                },
                "geometry": {"type": "LineString", "coordinates": network_coords}
            }

            combined_feature = {
                "type": "Feature",
                "properties": {
                    "segment_type": "combined",
                    "network_mode": mode,
                },
                "geometry": {"type": "LineString", "coordinates": network_coords}
            }

            geojson = {"type": "FeatureCollection", "features": [network_feature, combined_feature]}

            result = {
                "status": "ok",
                "route": geojson,
                "summary": {
                    "total_distance_km": float(distance_km),
                    "total_effort_minutes": float(duration_min),
                    "wilderness_distance_km": 0.0,
                    "wilderness_effort_minutes": 0.0,
                    "network_distance_km": float(distance_km),
                    "network_duration_minutes": float(duration_min),
                    "on_trail_pct": 100.0,
                    "barrier_crossings": 0,
                    "network_mode": mode,
                    "scenario": "D",
                    "computation_time_s": time.time() - t0,
                }
            }
            return result

        except Exception as e:
            return {"status": "error", "message": f"Network routing failed: {e}"}

    def _route_A_wilderness_to_network(
        self,
        start_lat: float, start_lon: float,
        end_lat: float, end_lon: float,
        mode: str, boundary_mode: str
    ) -> Dict:
        """
        Scenario A: Off-network start → on-network end.
        Wilderness pathfinding from start to entry point, then Valhalla to end.
        """
        t0 = time.time()

        # Ensure entry point index exists
        if not self.entry_index.table_exists() or self.entry_index.get_entry_point_count() == 0:
            return {
                "status": "error",
                "message": "Trail entry point index not built. Run build_entry_index() first."
            }

        # Get valid highway classes for this mode
        valid_highways = MODE_TO_VALID_HIGHWAYS.get(mode)

        # Find entry points near start, filtered by mode
        MAX_ENTRY_POINTS = 10
        entry_points = self.entry_index.query_radius(
            start_lat, start_lon, DEFAULT_SEARCH_RADIUS_KM, valid_highways
        )

        if not entry_points:
            entry_points = self.entry_index.query_radius(
                start_lat, start_lon, EXPANDED_SEARCH_RADIUS_KM, valid_highways
            )
            if not entry_points:
                if mode == "vehicle":
                    msg = f"No roads found within {EXPANDED_SEARCH_RADIUS_KM}km. Try a different mode."
                elif mode in ("mtb", "atv"):
                    msg = f"No tracks or roads found within {EXPANDED_SEARCH_RADIUS_KM}km. Try foot mode."
                else:
                    msg = f"No trail entry points found within {EXPANDED_SEARCH_RADIUS_KM}km of start."
                return {"status": "error", "message": msg}

        entry_points = entry_points[:MAX_ENTRY_POINTS]

        # Run wilderness pathfinding
        wilderness_result = self._pathfind_wilderness(
            start_lat, start_lon, end_lat, end_lon,
            entry_points, boundary_mode, "start"
        )

        if wilderness_result.get("status") == "error":
            return wilderness_result

        # Extract results
        wilderness_coords = wilderness_result["coords"]
        wilderness_stats = wilderness_result["stats"]
        wilderness_elevations = wilderness_result.get("elevations", [])
        best_entry = wilderness_result["entry_point"]

        entry_lat = best_entry["lat"]
        entry_lon = best_entry["lon"]

        # Call Valhalla from entry point to destination
        network_result = self._valhalla_route(entry_lat, entry_lon, end_lat, end_lon, mode)

        # Build response
        return self._build_response(
            wilderness_start=wilderness_coords,
            wilderness_start_stats=wilderness_stats,
            wilderness_start_elevations=wilderness_elevations,
            network_segment=network_result.get("segment"),
            wilderness_end=None,
            wilderness_end_stats=None,
            wilderness_end_elevations=None,
            mode=mode,
            boundary_mode=boundary_mode,
            entry_start=best_entry,
            entry_end=None,
            scenario="A",
            t0=t0,
            valhalla_error=network_result.get("error")
        )

    def _route_C_network_to_wilderness(
        self,
        start_lat: float, start_lon: float,
        end_lat: float, end_lon: float,
        mode: str, boundary_mode: str
    ) -> Dict:
        """
        Scenario C: On-network start → off-network end.
        Valhalla from start to entry point, then wilderness pathfinding to end.
        """
        t0 = time.time()

        if not self.entry_index.table_exists() or self.entry_index.get_entry_point_count() == 0:
            return {
                "status": "error",
                "message": "Trail entry point index not built. Run build_entry_index() first."
            }

        valid_highways = MODE_TO_VALID_HIGHWAYS.get(mode)

        # Find entry points near END (destination)
        MAX_ENTRY_POINTS = 10
        entry_points = self.entry_index.query_radius(
            end_lat, end_lon, DEFAULT_SEARCH_RADIUS_KM, valid_highways
        )

        if not entry_points:
            entry_points = self.entry_index.query_radius(
                end_lat, end_lon, EXPANDED_SEARCH_RADIUS_KM, valid_highways
            )
            if not entry_points:
                if mode == "vehicle":
                    msg = f"No roads found within {EXPANDED_SEARCH_RADIUS_KM}km of destination. Try a different mode."
                elif mode in ("mtb", "atv"):
                    msg = f"No tracks or roads found within {EXPANDED_SEARCH_RADIUS_KM}km of destination. Try foot mode."
                else:
                    msg = f"No trail entry points found within {EXPANDED_SEARCH_RADIUS_KM}km of destination."
                return {"status": "error", "message": msg}

        entry_points = entry_points[:MAX_ENTRY_POINTS]

        # Run wilderness pathfinding FROM END toward entry points
        wilderness_result = self._pathfind_wilderness(
            end_lat, end_lon, start_lat, start_lon,
            entry_points, boundary_mode, "end"
        )

        if wilderness_result.get("status") == "error":
            return wilderness_result

        # The path is from end→entry, reverse it for display (entry→end)
        wilderness_coords = list(reversed(wilderness_result["coords"]))
        wilderness_stats = wilderness_result["stats"]
        wilderness_elevations = list(reversed(wilderness_result.get("elevations", [])))
        best_entry = wilderness_result["entry_point"]

        entry_lat = best_entry["lat"]
        entry_lon = best_entry["lon"]

        # Call Valhalla from start to entry point
        network_result = self._valhalla_route(start_lat, start_lon, entry_lat, entry_lon, mode)

        # Build response (network first, then wilderness)
        return self._build_response(
            wilderness_start=None,
            wilderness_start_stats=None,
            wilderness_start_elevations=None,
            network_segment=network_result.get("segment"),
            wilderness_end=wilderness_coords,
            wilderness_end_stats=wilderness_stats,
            wilderness_end_elevations=wilderness_elevations,
            mode=mode,
            boundary_mode=boundary_mode,
            entry_start=None,
            entry_end=best_entry,
            scenario="C",
            t0=t0,
            valhalla_error=network_result.get("error")
        )

    def _route_B_wilderness_both(
        self,
        start_lat: float, start_lon: float,
        end_lat: float, end_lon: float,
        mode: str, boundary_mode: str
    ) -> Dict:
        """
        Scenario B: Off-network start → off-network end.
        Wilderness from start to entry_A, Valhalla entry_A to entry_B, wilderness from entry_B to end.
        """
        t0 = time.time()

        if not self.entry_index.table_exists() or self.entry_index.get_entry_point_count() == 0:
            return {
                "status": "error",
                "message": "Trail entry point index not built. Run build_entry_index() first."
            }

        valid_highways = MODE_TO_VALID_HIGHWAYS.get(mode)
        MAX_ENTRY_POINTS = 10

        # Find entry points near START
        entry_points_start = self.entry_index.query_radius(
            start_lat, start_lon, DEFAULT_SEARCH_RADIUS_KM, valid_highways
        )
        if not entry_points_start:
            entry_points_start = self.entry_index.query_radius(
                start_lat, start_lon, EXPANDED_SEARCH_RADIUS_KM, valid_highways
            )
        if not entry_points_start:
            return {"status": "error", "message": f"No entry points found near start within {EXPANDED_SEARCH_RADIUS_KM}km."}
        entry_points_start = entry_points_start[:MAX_ENTRY_POINTS]

        # Find entry points near END
        entry_points_end = self.entry_index.query_radius(
            end_lat, end_lon, DEFAULT_SEARCH_RADIUS_KM, valid_highways
        )
        if not entry_points_end:
            entry_points_end = self.entry_index.query_radius(
                end_lat, end_lon, EXPANDED_SEARCH_RADIUS_KM, valid_highways
            )
        if not entry_points_end:
            return {"status": "error", "message": f"No entry points found near destination within {EXPANDED_SEARCH_RADIUS_KM}km."}
        entry_points_end = entry_points_end[:MAX_ENTRY_POINTS]

        # Phase 1: Wilderness pathfinding from START
        wilderness_start_result = self._pathfind_wilderness(
            start_lat, start_lon, end_lat, end_lon,
            entry_points_start, boundary_mode, "start"
        )

        if wilderness_start_result.get("status") == "error":
            return wilderness_start_result

        wilderness_start_coords = wilderness_start_result["coords"]
        wilderness_start_stats = wilderness_start_result["stats"]
        wilderness_start_elevations = wilderness_start_result.get("elevations", [])
        entry_A = wilderness_start_result["entry_point"]

        # Phase 2: Wilderness pathfinding from END (run after freeing phase 1 memory)
        wilderness_end_result = self._pathfind_wilderness(
            end_lat, end_lon, start_lat, start_lon,
            entry_points_end, boundary_mode, "end"
        )

        if wilderness_end_result.get("status") == "error":
            return wilderness_end_result

        # Reverse the end wilderness path (it's end→entry, we want entry→end for display)
        wilderness_end_coords = list(reversed(wilderness_end_result["coords"]))
        wilderness_end_stats = wilderness_end_result["stats"]
        wilderness_end_elevations = list(reversed(wilderness_end_result.get("elevations", [])))
        entry_B = wilderness_end_result["entry_point"]

        # Phase 3: Valhalla from entry_A to entry_B
        network_result = self._valhalla_route(
            entry_A["lat"], entry_A["lon"],
            entry_B["lat"], entry_B["lon"],
            mode
        )

        # Build response
        return self._build_response(
            wilderness_start=wilderness_start_coords,
            wilderness_start_stats=wilderness_start_stats,
            wilderness_start_elevations=wilderness_start_elevations,
            network_segment=network_result.get("segment"),
            wilderness_end=wilderness_end_coords,
            wilderness_end_stats=wilderness_end_stats,
            wilderness_end_elevations=wilderness_end_elevations,
            mode=mode,
            boundary_mode=boundary_mode,
            entry_start=entry_A,
            entry_end=entry_B,
            scenario="B",
            t0=t0,
            valhalla_error=network_result.get("error")
        )

    def _pathfind_wilderness(
        self,
        origin_lat: float, origin_lon: float,
        dest_lat: float, dest_lon: float,
        entry_points: List[Dict],
        boundary_mode: str,
        label: str
    ) -> Dict:
        """
        Run MCP wilderness pathfinding from origin toward entry points.

        Args:
            origin_lat, origin_lon: Starting point for pathfinding
            dest_lat, dest_lon: Ultimate destination (for bbox calculation)
            entry_points: List of candidate entry points
            boundary_mode: How to handle barriers
            label: "start" or "end" for error messages

        Returns:
            {"status": "ok", "coords": [...], "stats": {...}, "entry_point": {...}}
            or {"status": "error", "message": "..."}
        """
        # Build bbox - only include origin and entry points, NOT distant destination
        # The destination is handled by Valhalla, wilderness only needs to reach entry points
        MAX_BBOX_DEGREES = 2.0
        all_lats = [origin_lat] + [p["lat"] for p in entry_points]
        all_lons = [origin_lon] + [p["lon"] for p in entry_points]

        padding = 0.05
        bbox = {
            "south": min(all_lats) - padding,
            "north": max(all_lats) + padding,
            "west": min(all_lons) - padding,
            "east": max(all_lons) + padding,
        }

        # Clamp bbox size, centering on origin
        lat_span = bbox["north"] - bbox["south"]
        lon_span = bbox["east"] - bbox["west"]
        if lat_span > MAX_BBOX_DEGREES or lon_span > MAX_BBOX_DEGREES:
            half_span = MAX_BBOX_DEGREES / 2
            bbox = {
                "south": origin_lat - half_span,
                "north": origin_lat + half_span,
                "west": origin_lon - half_span,
                "east": origin_lon + half_span,
            }

        # Initialize readers
        self._init_readers()

        # Load elevation
        try:
            elevation, meta = self.dem_reader.get_elevation_grid(
                south=bbox["south"], north=bbox["north"],
                west=bbox["west"], east=bbox["east"],
            )
        except Exception as e:
            return {"status": "error", "message": f"Failed to load elevation for {label}: {e}"}

        # Check memory
        mem = check_memory_usage()
        if mem > MEMORY_LIMIT_GB:
            return {"status": "error", "message": f"Memory limit exceeded: {mem:.1f}GB > {MEMORY_LIMIT_GB}GB"}

        # Load friction
        friction_raw = self.friction_reader.get_friction_grid(
            south=bbox["south"], north=bbox["north"],
            west=bbox["west"], east=bbox["east"],
            target_shape=elevation.shape
        )
        friction_mult = friction_to_multiplier(friction_raw)

        # Load barriers
        barriers = self.barrier_reader.get_barrier_grid(
            south=bbox["south"], north=bbox["north"],
            west=bbox["west"], east=bbox["east"],
            target_shape=elevation.shape
        )

        # Load trails
        trails = self.trail_reader.get_trails_grid(
            south=bbox["south"], north=bbox["north"],
            west=bbox["west"], east=bbox["east"],
            target_shape=elevation.shape
        )

        # Compute cost grid (ALWAYS foot mode for wilderness)
        cost = compute_cost_grid(
            elevation,
            cell_size_m=meta["cell_size_m"],
            friction=friction_mult,
            friction_raw=friction_raw,
            trails=trails,
            barriers=barriers,
            wilderness=None,
            mvum=None,
            boundary_mode=boundary_mode,
            mode="foot",
        )

        # Free intermediate arrays
        del friction_mult, friction_raw
        gc.collect()

        # Convert origin to pixel coordinates
        origin_row, origin_col = self.dem_reader.latlon_to_pixel(origin_lat, origin_lon, meta)

        rows, cols = elevation.shape
        if not (0 <= origin_row < rows and 0 <= origin_col < cols):
            return {"status": "error", "message": f"{label.capitalize()} point outside grid bounds"}

        # Map entry points to pixels
        entry_pixels = []
        for ep in entry_points:
            row, col = self.dem_reader.latlon_to_pixel(ep["lat"], ep["lon"], meta)
            if 0 <= row < rows and 0 <= col < cols:
                entry_pixels.append({"row": row, "col": col, "entry_point": ep})

        if not entry_pixels:
            return {"status": "error", "message": f"No entry points map to grid bounds for {label}"}

        # Run MCP
        mcp = MCP_Geometric(cost, fully_connected=True)
        cumulative_costs, traceback = mcp.find_costs([(origin_row, origin_col)])

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
                "message": f"No path found from {label} to any entry point (blocked by impassable terrain)"
            }

        # Traceback path
        path_indices = mcp.traceback((best_entry["row"], best_entry["col"]))

        # Convert to coordinates and collect stats
        coords = []
        elevations = []
        trail_values = []
        barrier_crossings = 0

        for row, col in path_indices:
            lat, lon = self.dem_reader.pixel_to_latlon(row, col, meta)
            coords.append([lon, lat])
            elevations.append(elevation[row, col])
            trail_values.append(trails[row, col])
            if barriers[row, col] == 255:
                barrier_crossings += 1

        # Calculate distance
        distance_m = 0
        for i in range(1, len(coords)):
            lon1, lat1 = coords[i-1]
            lon2, lat2 = coords[i]
            distance_m += haversine_distance(lat1, lon1, lat2, lon2)

        # Elevation stats
        elev_arr = np.array(elevations)
        elev_diff = np.diff(elev_arr)
        elev_gain = float(np.sum(elev_diff[elev_diff > 0]))
        elev_loss = float(np.sum(np.abs(elev_diff[elev_diff < 0])))

        # Trail stats
        trail_arr = np.array(trail_values)
        on_trail_cells = np.sum(trail_arr > 0)
        total_cells = len(trail_arr)
        on_trail_pct = float(100 * on_trail_cells / total_cells) if total_cells > 0 else 0

        # Free memory
        del mcp, cumulative_costs, traceback, cost, trails, barriers, elevation
        gc.collect()

        return {
            "status": "ok",
            "coords": coords,
            "elevations": elevations,  # Raw elevation values for maneuver generation
            "stats": {
                "distance_km": distance_m / 1000,
                "effort_minutes": best_cost / 60,
                "elevation_gain_m": elev_gain,
                "elevation_loss_m": elev_loss,
                "on_trail_pct": on_trail_pct,
                "barrier_crossings": barrier_crossings,
                "cell_count": total_cells,
            },
            "entry_point": best_entry["entry_point"]
        }

    def _valhalla_route(
        self,
        start_lat: float, start_lon: float,
        end_lat: float, end_lon: float,
        mode: str
    ) -> Dict:
        """
        Call Valhalla for network routing.

        Returns:
            {"segment": {...}, "error": None} on success
            {"segment": None, "error": "..."} on failure
        """
        costing = MODE_TO_COSTING.get(mode, "pedestrian")

        valhalla_request = {
            "locations": [
                {"lat": start_lat, "lon": start_lon},
                {"lat": end_lat, "lon": end_lon}
            ],
            "costing": costing,
            "directions_options": {"units": "kilometers"}
        }

        try:
            resp = requests.post(f"{VALHALLA_URL}/route", json=valhalla_request, timeout=30)

            if resp.status_code == 200:
                valhalla_data = resp.json()
                trip = valhalla_data.get("trip", {})
                legs = trip.get("legs", [])

                if legs:
                    leg = legs[0]
                    shape = leg.get("shape", "")
                    coords = self._decode_polyline(shape)

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
                    return {
                        "segment": {
                            "coordinates": coords,
                            "distance_km": summary.get("length", 0),
                            "duration_minutes": summary.get("time", 0) / 60,
                            "maneuvers": maneuvers,
                        },
                        "error": None
                    }

            return {"segment": None, "error": f"Valhalla returned {resp.status_code}: {resp.text[:200]}"}

        except Exception as e:
            return {"segment": None, "error": f"Valhalla request failed: {e}"}

    def _generate_wilderness_maneuvers(
        self,
        coords: List[List[float]],
        elevations: List[float],
        position: str = "start"
    ) -> List[Dict]:
        """
        Generate turn-by-turn maneuvers for a wilderness segment.

        Segment breaks occur when:
        - Bearing changes more than 30° from segment start
        - Grade category changes (flat→steep etc)
        - Distance exceeds 0.5 miles without a break

        Args:
            coords: [[lon, lat], ...] coordinate list
            elevations: Elevation values (meters) for each coord
            position: "start" or "end" for labeling

        Returns:
            List of maneuver dicts with instruction, distance, elevation, grade, bearing
        """
        if not coords or len(coords) < 2:
            return []

        # Constants
        COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                   "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        MAX_SEGMENT_M = 804.672  # 0.5 miles in meters
        BEARING_THRESHOLD = 30  # degrees
        M_TO_FT = 3.28084
        M_TO_MI = 0.000621371

        def get_bearing(lat1, lon1, lat2, lon2):
            """Calculate bearing between two points (degrees 0-360)."""
            dlon = math.radians(lon2 - lon1)
            lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
            x = math.sin(dlon) * math.cos(lat2_r)
            y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
            return (math.degrees(math.atan2(x, y)) + 360) % 360

        def bearing_to_cardinal(bearing):
            """Convert bearing to 16-point compass direction."""
            return COMPASS[round(bearing / 22.5) % 16]

        def get_grade_category(grade_deg):
            """Categorize grade angle: flat (0-2°), gentle (2-5°), moderate (5-10°), steep (10-15°), very steep (15°+)."""
            grade_abs = abs(grade_deg)
            if grade_abs < 2:
                return "flat"
            elif grade_abs < 5:
                return "gentle"
            elif grade_abs < 10:
                return "moderate"
            elif grade_abs < 15:
                return "steep"
            else:
                return "very steep"

        def format_distance(meters):
            """Format distance: feet with commas if under 1 mile, miles with one decimal if over."""
            miles = meters * M_TO_MI
            if miles < 1.0:
                feet = round(meters * M_TO_FT)
                return f"{feet:,} ft"
            else:
                return f"{miles:.1f} mi"

        def build_instruction(cardinal, gain_ft, loss_ft, grade_cat, distance_m):
            """Build instruction string per spec."""
            dist_str = format_distance(distance_m)
            if grade_cat == "flat":
                return f"Head {cardinal} on level ground — {dist_str}"
            elif gain_ft > loss_ft:
                return f"Head {cardinal}, gaining {gain_ft:,} ft ({grade_cat} uphill) — {dist_str}"
            else:
                return f"Head {cardinal}, descending {loss_ft:,} ft ({grade_cat} downhill) — {dist_str}"

        maneuvers = []
        i = 0

        while i < len(coords) - 1:
            seg_start_idx = i
            seg_start_lon, seg_start_lat = coords[i]
            seg_start_elev = elevations[i] if i < len(elevations) else 0

            # Initial bearing for this segment
            next_lon, next_lat = coords[i + 1]
            seg_bearing = get_bearing(seg_start_lat, seg_start_lon, next_lat, next_lon)

            # Accumulate elevation changes within segment
            seg_distance_m = 0
            seg_elev_gain = 0
            seg_elev_loss = 0
            prev_elev = seg_start_elev

            # Calculate initial grade category
            step_dist = haversine_distance(seg_start_lat, seg_start_lon, next_lat, next_lon)
            step_elev_change = (elevations[i + 1] if i + 1 < len(elevations) else seg_start_elev) - seg_start_elev
            initial_grade = math.degrees(math.atan(step_elev_change / step_dist)) if step_dist > 0 else 0
            seg_grade_cat = get_grade_category(initial_grade)

            j = i
            while j < len(coords) - 1:
                lon1, lat1 = coords[j]
                lon2, lat2 = coords[j + 1]
                elev1 = elevations[j] if j < len(elevations) else prev_elev
                elev2 = elevations[j + 1] if j + 1 < len(elevations) else elev1

                step_dist = haversine_distance(lat1, lon1, lat2, lon2)
                step_bearing = get_bearing(lat1, lon1, lat2, lon2)
                step_elev_change = elev2 - elev1
                step_grade = math.degrees(math.atan(step_elev_change / step_dist)) if step_dist > 0 else 0
                step_grade_cat = get_grade_category(step_grade)

                # Check break conditions
                bearing_diff = abs(step_bearing - seg_bearing)
                if bearing_diff > 180:
                    bearing_diff = 360 - bearing_diff

                # Break if: bearing changed >30°, grade category changed, or distance >0.5mi
                if seg_distance_m > 0:  # Don't break on first step
                    if bearing_diff > BEARING_THRESHOLD:
                        break
                    if step_grade_cat != seg_grade_cat:
                        break
                    if seg_distance_m >= MAX_SEGMENT_M:
                        break

                # Accumulate
                seg_distance_m += step_dist
                if step_elev_change > 0:
                    seg_elev_gain += step_elev_change
                else:
                    seg_elev_loss += abs(step_elev_change)
                prev_elev = elev2
                j += 1

            # Compute segment stats
            seg_end_idx = j
            gain_ft = round(seg_elev_gain * M_TO_FT)
            loss_ft = round(seg_elev_loss * M_TO_FT)

            # Net elevation change for grade calculation
            net_elev_change = seg_elev_gain - seg_elev_loss
            grade_deg = math.degrees(math.atan(net_elev_change / seg_distance_m)) if seg_distance_m > 0 else 0
            grade_cat = get_grade_category(grade_deg)

            cardinal = bearing_to_cardinal(seg_bearing)
            instruction = build_instruction(cardinal, gain_ft, loss_ft, grade_cat, seg_distance_m)

            maneuvers.append({
                "instruction": instruction,
                "type": "wilderness",
                "distance_m": round(seg_distance_m, 1),
                "elevation_gain_ft": gain_ft,
                "elevation_loss_ft": loss_ft,
                "grade_degrees": round(grade_deg, 1),
                "grade_category": grade_cat,
                "bearing": round(seg_bearing, 1),
                "cardinal": cardinal,
            })

            i = seg_end_idx

        # Add arrival maneuver
        arrival_text = "Arrive at trail/road" if position == "start" else "Arrive at destination"
        last_bearing = maneuvers[-1]["bearing"] if maneuvers else 0
        last_cardinal = maneuvers[-1]["cardinal"] if maneuvers else "N"

        maneuvers.append({
            "instruction": arrival_text,
            "type": "arrival",
            "distance_m": 0,
            "elevation_gain_ft": 0,
            "elevation_loss_ft": 0,
            "grade_degrees": 0,
            "grade_category": "flat",
            "bearing": last_bearing,
            "cardinal": last_cardinal,
        })

        return maneuvers

    def _build_response(
        self,
        wilderness_start: Optional[List],
        wilderness_start_stats: Optional[Dict],
        wilderness_start_elevations: Optional[List],
        network_segment: Optional[Dict],
        wilderness_end: Optional[List],
        wilderness_end_stats: Optional[Dict],
        wilderness_end_elevations: Optional[List],
        mode: str,
        boundary_mode: str,
        entry_start: Optional[Dict],
        entry_end: Optional[Dict],
        scenario: str,
        t0: float,
        valhalla_error: Optional[str]
    ) -> Dict:
        """Build the final GeoJSON response."""
        features = []

        # Wilderness start segment
        if wilderness_start and wilderness_start_stats:
            wild_start_maneuvers = []
            if wilderness_start_elevations:
                wild_start_maneuvers = self._generate_wilderness_maneuvers(
                    wilderness_start, wilderness_start_elevations, position="start"
                )
            features.append({
                "type": "Feature",
                "properties": {
                    "segment_type": "wilderness",
                    "segment_position": "start",
                    "effort_minutes": float(wilderness_start_stats["effort_minutes"]),
                    "distance_km": float(wilderness_start_stats["distance_km"]),
                    "elevation_gain_m": wilderness_start_stats["elevation_gain_m"],
                    "elevation_loss_m": wilderness_start_stats["elevation_loss_m"],
                    "boundary_mode": boundary_mode,
                    "on_trail_pct": wilderness_start_stats["on_trail_pct"],
                    "barrier_crossings": wilderness_start_stats["barrier_crossings"],
                    "wilderness_mode": "foot",
                    "maneuvers": wild_start_maneuvers,
                },
                "geometry": {"type": "LineString", "coordinates": wilderness_start}
            })

        # Network segment
        if network_segment:
            features.append({
                "type": "Feature",
                "properties": {
                    "segment_type": "network",
                    "distance_km": network_segment["distance_km"],
                    "duration_minutes": network_segment["duration_minutes"],
                    "maneuvers": network_segment["maneuvers"],
                    "network_mode": mode,
                },
                "geometry": {"type": "LineString", "coordinates": network_segment["coordinates"]}
            })

        # Wilderness end segment
        if wilderness_end and wilderness_end_stats:
            wild_end_maneuvers = []
            if wilderness_end_elevations:
                wild_end_maneuvers = self._generate_wilderness_maneuvers(
                    wilderness_end, wilderness_end_elevations, position="end"
                )
            features.append({
                "type": "Feature",
                "properties": {
                    "segment_type": "wilderness",
                    "segment_position": "end",
                    "effort_minutes": float(wilderness_end_stats["effort_minutes"]),
                    "distance_km": float(wilderness_end_stats["distance_km"]),
                    "elevation_gain_m": wilderness_end_stats["elevation_gain_m"],
                    "elevation_loss_m": wilderness_end_stats["elevation_loss_m"],
                    "boundary_mode": boundary_mode,
                    "on_trail_pct": wilderness_end_stats["on_trail_pct"],
                    "barrier_crossings": wilderness_end_stats["barrier_crossings"],
                    "wilderness_mode": "foot",
                    "maneuvers": wild_end_maneuvers,
                },
                "geometry": {"type": "LineString", "coordinates": wilderness_end}
            })

        # Combined path
        combined_coords = []
        if wilderness_start:
            combined_coords.extend(wilderness_start)
        if network_segment:
            # Skip first coord if we already have wilderness_start (avoid duplicate)
            start_idx = 1 if wilderness_start else 0
            combined_coords.extend(network_segment["coordinates"][start_idx:])
        if wilderness_end:
            # Skip first coord (avoid duplicate with network end)
            start_idx = 1 if (wilderness_start or network_segment) else 0
            combined_coords.extend(wilderness_end[start_idx:])

        if combined_coords:
            features.append({
                "type": "Feature",
                "properties": {
                    "segment_type": "combined",
                    "wilderness_mode": "foot",
                    "network_mode": mode,
                    "boundary_mode": boundary_mode,
                    "scenario": scenario,
                },
                "geometry": {"type": "LineString", "coordinates": combined_coords}
            })

        geojson = {"type": "FeatureCollection", "features": features}

        # Calculate totals
        total_distance_km = 0.0
        total_effort_minutes = 0.0
        wilderness_distance_km = 0.0
        wilderness_effort_minutes = 0.0
        network_distance_km = 0.0
        network_duration_minutes = 0.0
        barrier_crossings = 0
        on_trail_pct = 0.0

        if wilderness_start_stats:
            wilderness_distance_km += wilderness_start_stats["distance_km"]
            wilderness_effort_minutes += wilderness_start_stats["effort_minutes"]
            barrier_crossings += wilderness_start_stats["barrier_crossings"]
            on_trail_pct = wilderness_start_stats["on_trail_pct"]

        if wilderness_end_stats:
            wilderness_distance_km += wilderness_end_stats["distance_km"]
            wilderness_effort_minutes += wilderness_end_stats["effort_minutes"]
            barrier_crossings += wilderness_end_stats["barrier_crossings"]
            # Average on-trail percentage if we have both
            if wilderness_start_stats:
                on_trail_pct = (on_trail_pct + wilderness_end_stats["on_trail_pct"]) / 2
            else:
                on_trail_pct = wilderness_end_stats["on_trail_pct"]

        if network_segment:
            network_distance_km = network_segment["distance_km"]
            network_duration_minutes = network_segment["duration_minutes"]

        total_distance_km = wilderness_distance_km + network_distance_km
        total_effort_minutes = wilderness_effort_minutes + network_duration_minutes

        summary = {
            "total_distance_km": float(total_distance_km),
            "total_effort_minutes": float(total_effort_minutes),
            "wilderness_distance_km": float(wilderness_distance_km),
            "wilderness_effort_minutes": float(wilderness_effort_minutes),
            "network_distance_km": float(network_distance_km),
            "network_duration_minutes": float(network_duration_minutes),
            "on_trail_pct": float(on_trail_pct),
            "barrier_crossings": barrier_crossings,
            "boundary_mode": boundary_mode,
            "wilderness_mode": "foot",
            "network_mode": mode,
            "scenario": scenario,
            "computation_time_s": time.time() - t0,
        }

        if entry_start:
            summary["entry_point_start"] = {
                "lat": entry_start["lat"],
                "lon": entry_start["lon"],
                "highway_class": entry_start["highway_class"],
                "name": entry_start.get("name", ""),
            }

        if entry_end:
            summary["entry_point_end"] = {
                "lat": entry_end["lat"],
                "lon": entry_end["lon"],
                "highway_class": entry_end["highway_class"],
                "name": entry_end.get("name", ""),
            }

        result = {"status": "ok", "route": geojson, "summary": summary}

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
        if self.wilderness_reader:
            self.wilderness_reader.close()
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
        print("Testing router (all scenarios)...")
        print("=" * 60)

        router = OffrouteRouter()

        # Test points
        wilderness_start = (44.0543, -115.4237)  # Off-network
        wilderness_end = (45.2, -115.5)          # Deep wilderness (Frank Church)
        road_start = (43.6150, -116.2023)        # Boise downtown (on-network)
        road_end = (43.5867, -116.5625)          # Nampa (on-network)

        tests = [
            ("A: wilderness→road", wilderness_start, (44.0814, -115.5021)),
            ("B: wilderness→wilderness", wilderness_start, wilderness_end),
            ("C: road→wilderness", road_start, wilderness_start),
            ("D: road→road", road_start, road_end),
        ]

        for label, (slat, slon), (elat, elon) in tests:
            print(f"\n{label}")
            print("-" * 40)

            result = router.route(
                start_lat=slat, start_lon=slon,
                end_lat=elat, end_lon=elon,
                mode="foot", boundary_mode="pragmatic"
            )

            if result["status"] == "ok":
                s = result["summary"]
                print(f"  Scenario: {s.get('scenario', '?')}")
                print(f"  Total: {s['total_distance_km']:.2f} km, {s['total_effort_minutes']:.1f} min")
                print(f"  Wilderness: {s['wilderness_distance_km']:.2f} km")
                print(f"  Network: {s['network_distance_km']:.2f} km")
                if s.get('entry_point_start'):
                    ep = s['entry_point_start']
                    print(f"  Entry (start): {ep['highway_class']} at {ep['lat']:.4f}, {ep['lon']:.4f}")
                if s.get('entry_point_end'):
                    ep = s['entry_point_end']
                    print(f"  Entry (end): {ep['highway_class']} at {ep['lat']:.4f}, {ep['lon']:.4f}")
            else:
                print(f"  ERROR: {result['message']}")

        router.close()

    else:
        print("Usage:")
        print("  python router.py build   # Build entry point index")
        print("  python router.py test    # Test all scenarios")
