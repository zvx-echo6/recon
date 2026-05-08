"""
MVUM (Motor Vehicle Use Map) legal access layer for OFFROUTE.

Queries USFS MVUM data from navi.db and provides rasterized access grids
indicating which roads/trails are open or closed to specific vehicle modes.

MVUM is motor-vehicle specific — foot mode should skip this layer entirely.
"""
import re
import sqlite3
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Literal

import numpy as np

# Path to navi.db
NAVI_DB_PATH = Path("/mnt/nav/navi.db")


def parse_date_range(date_str: str) -> List[Tuple[int, int, int, int]]:
    """
    Parse MVUM date range strings like "05/01-11/30" or "06/15-10/15,12/01-03/31".

    Returns list of (start_month, start_day, end_month, end_day) tuples.
    Returns empty list if unparseable.
    """
    if not date_str or date_str.strip() == "":
        return []

    ranges = []
    # Split by comma for multi-period strings
    for part in date_str.split(","):
        part = part.strip()
        # Match MM/DD-MM/DD pattern
        match = re.match(r"(\d{1,2})/(\d{1,2})-(\d{1,2})/(\d{1,2})", part)
        if match:
            try:
                sm, sd, em, ed = int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))
                if 1 <= sm <= 12 and 1 <= sd <= 31 and 1 <= em <= 12 and 1 <= ed <= 31:
                    ranges.append((sm, sd, em, ed))
            except ValueError:
                pass

    return ranges


def is_date_in_range(month: int, day: int, ranges: List[Tuple[int, int, int, int]]) -> bool:
    """
    Check if a given month/day falls within any of the date ranges.
    Handles ranges that wrap around year end (e.g., 12/01-03/31).
    """
    if not ranges:
        return True  # No ranges = assume open

    date_num = month * 100 + day  # Simple numeric comparison

    for sm, sd, em, ed in ranges:
        start_num = sm * 100 + sd
        end_num = em * 100 + ed

        if start_num <= end_num:
            # Normal range (e.g., 05/01-11/30)
            if start_num <= date_num <= end_num:
                return True
        else:
            # Wrapping range (e.g., 12/01-03/31)
            if date_num >= start_num or date_num <= end_num:
                return True

    return False


def check_access(
    status_field: Optional[str],
    dates_field: Optional[str],
    seasonal: Optional[str],
    check_date: Optional[Tuple[int, int]] = None
) -> Optional[bool]:
    """
    Determine if a road/trail is open to a vehicle type.

    Args:
        status_field: Value of vehicle-class field (e.g., "open", null)
        dates_field: Value of *_DATESOPEN field (e.g., "05/01-11/30")
        seasonal: Value of SEASONAL field ("yearlong", "seasonal")
        check_date: Optional (month, day) tuple to check against date ranges

    Returns:
        True = open
        False = closed
        None = no data (field not populated, defer to SYMBOL)
    """
    if status_field is None or status_field.strip() == "":
        return None  # No data

    status = status_field.strip().lower()

    if status != "open":
        return False  # Explicitly closed or restricted

    # Status is "open" - check seasonal restrictions
    if check_date is not None:
        month, day = check_date

        # Parse date ranges
        if dates_field:
            ranges = parse_date_range(dates_field)
            if ranges:
                return is_date_in_range(month, day, ranges)

        # No date field but seasonal = "yearlong" means always open
        if seasonal and seasonal.strip().lower() == "yearlong":
            return True

        # Seasonal with no dates - assume open (data quality issue)
        if seasonal and seasonal.strip().lower() == "seasonal":
            warnings.warn(f"Seasonal road/trail with no DATESOPEN, assuming open")
            return True

    return True  # Open with no date check


def get_mode_field(mode: str) -> Tuple[str, str]:
    """
    Get the MVUM field names for a given travel mode.

    Returns (status_field, dates_field) tuple.
    """
    mode_mapping = {
        "atv": ("atv", "atv_datesopen"),
        "motorcycle": ("motorcycle", "motorcycle_datesopen"),
        "mtb": ("e_bike_class1", "e_bike_class1_dur"),  # Closest analog for e-bikes
        "vehicle": ("highclearancevehicle", "highclearancevehicle_datesopen"),
        "passenger": ("passengervehicle", "passengervehicle_datesopen"),
    }

    return mode_mapping.get(mode, ("highclearancevehicle", "highclearancevehicle_datesopen"))


def symbol_to_access(symbol: str, mode: str, maint_level: Optional[str] = None) -> Optional[bool]:
    """
    Fallback: interpret SYMBOL field when per-vehicle-class fields are null.

    MVUM SYMBOL meanings (roads):
        1 = Open to all vehicles
        2 = Open to highway legal vehicles only
        3 = Road closed to motorized
        4 = Road open seasonally
        11 = Administrative use only
        12 = Decommissioned

    For trails, similar logic applies based on TRAILCLASS.
    """
    if symbol is None:
        return None

    sym = str(symbol).strip()

    # Symbol 1: Open to all
    if sym == "1":
        return True

    # Symbol 2: Highway legal only
    if sym == "2":
        # ATVs/motorcycles typically not highway legal
        if mode in ("atv", "motorcycle"):
            return False
        return True

    # Symbol 3: Closed to motorized
    if sym == "3":
        return False

    # Symbol 4: Seasonally open (assume open if no date check)
    if sym == "4":
        return True

    # Symbol 11/12: Administrative/decommissioned = closed
    if sym in ("11", "12"):
        return False

    # Unknown symbol - defer
    return None


class MVUMReader:
    """
    Reader for MVUM data from navi.db.

    Queries roads and trails by bounding box and returns access grids.
    """

    def __init__(self, db_path: Path = NAVI_DB_PATH):
        self.db_path = db_path
        self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if not self.db_path.exists():
                raise FileNotFoundError(f"navi.db not found at {self.db_path}")
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            # Load Spatialite extension if available
            try:
                self._conn.enable_load_extension(True)
                self._conn.load_extension("mod_spatialite")
            except Exception:
                pass  # Spatialite not available, will use manual bbox queries
        return self._conn

    def table_exists(self, table_name: str) -> bool:
        """Check if an MVUM table exists."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return cur.fetchone() is not None

    def query_roads_bbox(
        self,
        south: float, north: float, west: float, east: float,
        mode: str = "atv",
        check_date: Optional[Tuple[int, int]] = None
    ) -> List[Dict]:
        """
        Query MVUM roads within a bounding box.

        Returns list of dicts with access info for the given mode.
        """
        if not self.table_exists("mvum_roads"):
            return []

        conn = self._get_conn()

        # Query using bbox on geometry
        # Since we don't have spatialite, we'll query all and filter in Python
        # For production, consider pre-computing bbox columns
        cur = conn.execute("""
            SELECT ogc_fid, id, name, symbol, operationalmaintlevel, seasonal,
                   atv, atv_datesopen, motorcycle, motorcycle_datesopen,
                   highclearancevehicle, highclearancevehicle_datesopen,
                   passengervehicle, passengervehicle_datesopen,
                   e_bike_class1, e_bike_class1_dur,
                   shape
            FROM mvum_roads
        """)

        status_field, dates_field = get_mode_field(mode)
        results = []

        for row in cur:
            # Parse geometry to check bbox intersection
            # The shape is stored as WKB blob
            shape = row["shape"]
            if shape is None:
                continue

            # Quick bbox check using geometry extent
            # Since we don't have Spatialite functions, we'll include all
            # and let the rasterization handle it

            access = check_access(
                row[status_field] if status_field in row.keys() else None,
                row[dates_field] if dates_field in row.keys() else None,
                row["seasonal"],
                check_date
            )

            # Fallback to SYMBOL if no per-vehicle data
            if access is None:
                access = symbol_to_access(row["symbol"], mode, row["operationalmaintlevel"])

            if access is not None:
                results.append({
                    "id": row["id"],
                    "name": row["name"],
                    "access": access,
                    "symbol": row["symbol"],
                    "maint_level": row["operationalmaintlevel"],
                    "shape": shape,
                })

        return results

    def query_trails_bbox(
        self,
        south: float, north: float, west: float, east: float,
        mode: str = "atv",
        check_date: Optional[Tuple[int, int]] = None
    ) -> List[Dict]:
        """
        Query MVUM trails within a bounding box.
        """
        if not self.table_exists("mvum_trails"):
            return []

        conn = self._get_conn()

        cur = conn.execute("""
            SELECT ogc_fid, id, name, symbol, seasonal, trailclass,
                   atv, atv_datesopen, motorcycle, motorcycle_datesopen,
                   highclearancevehicle, highclearancevehicle_datesopen,
                   passengervehicle, passengervehicle_datesopen,
                   e_bike_class1, e_bike_class1_dur,
                   shape
            FROM mvum_trails
        """)

        status_field, dates_field = get_mode_field(mode)
        results = []

        for row in cur:
            shape = row["shape"]
            if shape is None:
                continue

            access = check_access(
                row[status_field] if status_field in row.keys() else None,
                row[dates_field] if dates_field in row.keys() else None,
                row["seasonal"],
                check_date
            )

            if access is None:
                access = symbol_to_access(row["symbol"], mode)

            if access is not None:
                results.append({
                    "id": row["id"],
                    "name": row["name"],
                    "access": access,
                    "symbol": row["symbol"],
                    "trail_class": row["trailclass"],
                    "shape": shape,
                })

        return results

    def query_nearest(
        self,
        lat: float, lon: float,
        radius_m: float = 50,
        table: str = "mvum_roads"
    ) -> Optional[Dict]:
        """
        Query the nearest MVUM feature to a point.

        Used for the places panel API.
        """
        if not self.table_exists(table):
            return None

        conn = self._get_conn()

        # Convert radius to degrees (approximate)
        radius_deg = radius_m / 111000

        # Query features in bbox around point
        if table == "mvum_roads":
            cur = conn.execute("""
                SELECT ogc_fid, id, name, forestname, districtname, symbol,
                       operationalmaintlevel, surfacetype, seasonal, jurisdiction,
                       passengervehicle, passengervehicle_datesopen,
                       highclearancevehicle, highclearancevehicle_datesopen,
                       atv, atv_datesopen, motorcycle, motorcycle_datesopen,
                       fourwd_gt50inches, fourwd_gt50_datesopen,
                       twowd_gt50inches, twowd_gt50_datesopen,
                       e_bike_class1, e_bike_class1_dur,
                       e_bike_class2, e_bike_class2_dur,
                       e_bike_class3, e_bike_class3_dur,
                       shape
                FROM mvum_roads
                LIMIT 1000
            """)
        else:
            cur = conn.execute("""
                SELECT ogc_fid, id, name, forestname, districtname, symbol,
                       seasonal, jurisdiction, trailclass, trailsystem,
                       passengervehicle, passengervehicle_datesopen,
                       highclearancevehicle, highclearancevehicle_datesopen,
                       atv, atv_datesopen, motorcycle, motorcycle_datesopen,
                       fourwd_gt50inches, fourwd_gt50_datesopen,
                       twowd_gt50inches, twowd_gt50_datesopen,
                       e_bike_class1, e_bike_class1_dur,
                       e_bike_class2, e_bike_class2_dur,
                       e_bike_class3, e_bike_class3_dur,
                       shape
                FROM mvum_trails
                LIMIT 1000
            """)

        # Find nearest feature
        # This is a simplified approach - for production, use spatial index
        try:
            from shapely import wkb
            from shapely.geometry import Point

            query_point = Point(lon, lat)
            nearest = None
            min_dist = float('inf')

            for row in cur:
                try:
                    geom = wkb.loads(row["shape"])
                    dist = query_point.distance(geom)
                    if dist < min_dist and dist < radius_deg:
                        min_dist = dist
                        nearest = dict(row)
                        nearest["geometry"] = geom
                except Exception:
                    continue

            if nearest:
                # Convert geometry to GeoJSON
                nearest["geojson"] = nearest["geometry"].__geo_interface__
                del nearest["geometry"]
                del nearest["shape"]
                return nearest

        except ImportError:
            warnings.warn("shapely not available for nearest query")

        return None

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


def get_mvum_access_grid(
    south: float, north: float, west: float, east: float,
    target_shape: Tuple[int, int],
    mode: Literal["foot", "mtb", "atv", "vehicle"] = "atv",
    check_date: Optional[str] = None,
    db_path: Path = NAVI_DB_PATH
) -> np.ndarray:
    """
    Get MVUM access grid for pathfinding.

    Args:
        south, north, west, east: Bounding box (WGS84)
        target_shape: (rows, cols) to match elevation grid
        mode: Travel mode (foot skips MVUM entirely)
        check_date: Optional "MM/DD" string for seasonal checking
        db_path: Path to navi.db

    Returns:
        np.ndarray of uint8:
            0   = no MVUM data (defer to existing trail/friction logic)
            1   = road/trail is OPEN to this vehicle mode
            255 = road/trail EXISTS but is CLOSED to this mode
    """
    # Foot mode bypasses MVUM entirely
    if mode == "foot":
        return np.zeros(target_shape, dtype=np.uint8)

    # Parse check_date if provided
    parsed_date = None
    if check_date:
        match = re.match(r"(\d{1,2})/(\d{1,2})", check_date)
        if match:
            parsed_date = (int(match.group(1)), int(match.group(2)))

    # Initialize output grid
    grid = np.zeros(target_shape, dtype=np.uint8)
    rows, cols = target_shape

    # Pixel size
    pixel_lat = (north - south) / rows
    pixel_lon = (east - west) / cols

    reader = MVUMReader(db_path)

    try:
        # Query roads and trails
        roads = reader.query_roads_bbox(south, north, west, east, mode, parsed_date)
        trails = reader.query_trails_bbox(south, north, west, east, mode, parsed_date)

        # Rasterize features
        try:
            from shapely import wkb

            for features in [roads, trails]:
                for feat in features:
                    try:
                        geom = wkb.loads(feat["shape"])

                        # Get geometry bounds
                        minx, miny, maxx, maxy = geom.bounds

                        # Check if intersects our bbox
                        if maxx < west or minx > east or maxy < south or miny > north:
                            continue

                        # Rasterize line
                        value = 1 if feat["access"] else 255

                        # Simple line rasterization
                        if geom.geom_type in ("LineString", "MultiLineString"):
                            if geom.geom_type == "MultiLineString":
                                coords_list = [list(line.coords) for line in geom.geoms]
                            else:
                                coords_list = [list(geom.coords)]

                            for coords in coords_list:
                                for i in range(len(coords) - 1):
                                    x1, y1 = coords[i]
                                    x2, y2 = coords[i + 1]

                                    # Convert to pixel coordinates
                                    col1 = int((x1 - west) / pixel_lon)
                                    row1 = int((north - y1) / pixel_lat)
                                    col2 = int((x2 - west) / pixel_lon)
                                    row2 = int((north - y2) / pixel_lat)

                                    # Bresenham's line algorithm
                                    _draw_line(grid, row1, col1, row2, col2, value)

                    except Exception as e:
                        continue

        except ImportError:
            warnings.warn("shapely not available, MVUM rasterization skipped")

    finally:
        reader.close()

    return grid


def _draw_line(grid: np.ndarray, r1: int, c1: int, r2: int, c2: int, value: int):
    """Draw a line on the grid using Bresenham's algorithm."""
    rows, cols = grid.shape

    dr = abs(r2 - r1)
    dc = abs(c2 - c1)
    sr = 1 if r1 < r2 else -1
    sc = 1 if c1 < c2 else -1
    err = dr - dc

    r, c = r1, c1

    while True:
        if 0 <= r < rows and 0 <= c < cols:
            # Only overwrite if current value is 0 (no data) or we're marking closed
            if grid[r, c] == 0 or value == 255:
                grid[r, c] = value

        if r == r2 and c == c2:
            break

        e2 = 2 * err
        if e2 > -dc:
            err -= dc
            r += sr
        if e2 < dr:
            err += dr
            c += sc


if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("MVUM Reader Test")
    print("=" * 60)

    reader = MVUMReader()

    if not reader.table_exists("mvum_roads"):
        print("ERROR: mvum_roads table not found in navi.db")
        sys.exit(1)

    # Test bbox query (Sawtooth NF area)
    print("\n[1] Testing bbox query (Sawtooth NF area)...")
    roads = reader.query_roads_bbox(
        south=43.5, north=44.0, west=-115.0, east=-114.0,
        mode="atv"
    )
    print(f"  Found {len(roads)} roads")

    open_count = sum(1 for r in roads if r["access"])
    closed_count = sum(1 for r in roads if not r["access"])
    print(f"  Open to ATV: {open_count}")
    print(f"  Closed to ATV: {closed_count}")

    # Test with seasonal date
    print("\n[2] Testing with date check (July 15)...")
    roads_summer = reader.query_roads_bbox(
        south=43.5, north=44.0, west=-115.0, east=-114.0,
        mode="atv",
        check_date=(7, 15)
    )
    open_summer = sum(1 for r in roads_summer if r["access"])
    print(f"  Open to ATV on 07/15: {open_summer}")

    print("\n[3] Testing with date check (January 15)...")
    roads_winter = reader.query_roads_bbox(
        south=43.5, north=44.0, west=-115.0, east=-114.0,
        mode="atv",
        check_date=(1, 15)
    )
    open_winter = sum(1 for r in roads_winter if r["access"])
    print(f"  Open to ATV on 01/15: {open_winter}")

    # Test grid generation
    print("\n[4] Testing grid generation...")
    grid = get_mvum_access_grid(
        south=43.5, north=44.0, west=-115.0, east=-114.0,
        target_shape=(500, 1000),
        mode="atv"
    )
    print(f"  Grid shape: {grid.shape}")
    print(f"  No data (0): {np.sum(grid == 0)}")
    print(f"  Open (1): {np.sum(grid == 1)}")
    print(f"  Closed (255): {np.sum(grid == 255)}")

    reader.close()
    print("\nDone.")
