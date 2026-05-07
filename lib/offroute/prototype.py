#!/usr/bin/env python3
"""
OFFROUTE Phase O1 Prototype

Validates the PMTiles decoder, Tobler cost function, and MCP pathfinder
on a real Idaho bounding box.

Test bbox (four Idaho towns as corners):
  SW: Rogerson, ID (~42.21, -114.60)
  NW: Buhl, ID (~42.60, -114.76)
  NE: Burley, ID (~42.54, -113.79)
  SE: Oakley, ID (~42.24, -113.88)
  Approximate bbox: south=42.21, north=42.60, west=-114.76, east=-113.79
"""
import json
import time
import sys
from pathlib import Path

import numpy as np
from skimage.graph import MCP_Geometric

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.offroute.dem import DEMReader
from lib.offroute.cost import compute_cost_grid

# Test bounding box
BBOX = {
    "south": 42.21,
    "north": 42.60,
    "west": -114.76,
    "east": -113.79,
}

# Start point: wilderness area south of Twin Falls
# (in the Sawtooth National Forest foothills)
START_LAT = 42.35
START_LON = -114.50

# End point: near Burley, ID (on road network)
END_LAT = 42.52
END_LON = -113.85

# Output file
OUTPUT_PATH = Path("/opt/recon/data/offroute-test.geojson")

# Memory limit in GB
MEMORY_LIMIT_GB = 12


def check_memory_usage():
    """Check current memory usage and abort if over limit."""
    try:
        import psutil
        process = psutil.Process()
        mem_gb = process.memory_info().rss / (1024**3)
        if mem_gb > MEMORY_LIMIT_GB:
            print(f"ERROR: Memory usage {mem_gb:.1f}GB exceeds {MEMORY_LIMIT_GB}GB limit")
            sys.exit(1)
        return mem_gb
    except ImportError:
        return 0


def main():
    print("=" * 60)
    print("OFFROUTE Phase O1 Prototype")
    print("=" * 60)

    t0 = time.time()

    # Step 1: Load elevation data
    print(f"\n[1] Loading DEM for bbox: {BBOX}")
    reader = DEMReader()

    t1 = time.time()
    elevation, meta = reader.get_elevation_grid(
        south=BBOX["south"],
        north=BBOX["north"],
        west=BBOX["west"],
        east=BBOX["east"],
    )
    t2 = time.time()

    print(f"    Elevation grid shape: {elevation.shape}")
    print(f"    Cell count: {elevation.size:,}")
    print(f"    Cell size: {meta['cell_size_m']:.1f} m")
    print(f"    Elevation range: {np.nanmin(elevation):.0f} - {np.nanmax(elevation):.0f} m")
    print(f"    Load time: {t2 - t1:.1f}s")

    mem = check_memory_usage()
    if mem > 0:
        print(f"    Memory usage: {mem:.1f} GB")

    # Step 2: Compute cost grid
    print(f"\n[2] Computing Tobler cost grid...")
    t3 = time.time()
    cost = compute_cost_grid(elevation, cell_size_m=meta["cell_size_m"])
    t4 = time.time()

    finite_cost = cost[~np.isinf(cost)]
    print(f"    Cost range: {finite_cost.min():.1f} - {finite_cost.max():.1f} s/cell")
    print(f"    Impassable cells: {np.sum(np.isinf(cost)):,} ({100*np.sum(np.isinf(cost))/cost.size:.1f}%)")
    print(f"    Compute time: {t4 - t3:.1f}s")

    mem = check_memory_usage()
    if mem > 0:
        print(f"    Memory usage: {mem:.1f} GB")

    # Step 3: Convert start/end to pixel coordinates
    print(f"\n[3] Converting coordinates...")
    start_row, start_col = reader.latlon_to_pixel(START_LAT, START_LON, meta)
    end_row, end_col = reader.latlon_to_pixel(END_LAT, END_LON, meta)

    print(f"    Start: ({START_LAT}, {START_LON}) -> pixel ({start_row}, {start_col})")
    print(f"    End: ({END_LAT}, {END_LON}) -> pixel ({end_row}, {end_col})")

    # Validate coordinates are within bounds
    rows, cols = elevation.shape
    if not (0 <= start_row < rows and 0 <= start_col < cols):
        print(f"ERROR: Start point outside grid bounds")
        sys.exit(1)
    if not (0 <= end_row < rows and 0 <= end_col < cols):
        print(f"ERROR: End point outside grid bounds")
        sys.exit(1)

    start_elev = elevation[start_row, start_col]
    end_elev = elevation[end_row, end_col]
    print(f"    Start elevation: {start_elev:.0f} m")
    print(f"    End elevation: {end_elev:.0f} m")

    # Step 4: Run MCP pathfinder
    print(f"\n[4] Running MCP_Geometric pathfinder...")
    t5 = time.time()

    # MCP_Geometric finds minimum cost path
    # It uses Dijkstra's algorithm internally
    mcp = MCP_Geometric(cost, fully_connected=True)

    # Find costs from start to all reachable cells
    cumulative_costs, traceback = mcp.find_costs([(start_row, start_col)])
    t6 = time.time()

    print(f"    Dijkstra completed in {t6 - t5:.1f}s")

    # Get cost to reach end point
    end_cost = cumulative_costs[end_row, end_col]
    print(f"    Total cost to endpoint: {end_cost:.0f} seconds ({end_cost/60:.1f} minutes)")

    if np.isinf(end_cost):
        print("ERROR: No path found to endpoint (blocked by impassable terrain)")
        sys.exit(1)

    # Trace back the path
    t7 = time.time()
    path_indices = mcp.traceback((end_row, end_col))
    t8 = time.time()

    print(f"    Traceback completed in {t8 - t7:.2f}s")
    print(f"    Path length: {len(path_indices)} cells")

    mem = check_memory_usage()
    if mem > 0:
        print(f"    Memory usage: {mem:.1f} GB")

    # Step 5: Convert path to coordinates and compute stats
    print(f"\n[5] Converting path to GeoJSON...")

    coordinates = []
    elevations = []

    for row, col in path_indices:
        lat, lon = reader.pixel_to_latlon(row, col, meta)
        elev = elevation[row, col]
        coordinates.append([lon, lat])  # GeoJSON is [lon, lat]
        elevations.append(elev)

    # Compute path distance
    total_distance_m = 0
    for i in range(1, len(coordinates)):
        lon1, lat1 = coordinates[i-1]
        lon2, lat2 = coordinates[i]
        # Haversine formula
        R = 6371000  # Earth radius in meters
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)
        a = np.sin(dlat/2)**2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon/2)**2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        total_distance_m += R * c

    # Compute elevation gain/loss
    elev_arr = np.array(elevations)
    elev_diff = np.diff(elev_arr)
    elev_gain = np.sum(elev_diff[elev_diff > 0])
    elev_loss = np.sum(np.abs(elev_diff[elev_diff < 0]))

    # Build GeoJSON
    geojson = {
        "type": "Feature",
        "properties": {
            "type": "offroute_prototype",
            "start": {"lat": START_LAT, "lon": START_LON},
            "end": {"lat": END_LAT, "lon": END_LON},
            "total_time_seconds": float(end_cost),
            "total_time_minutes": float(end_cost / 60),
            "total_distance_m": float(total_distance_m),
            "total_distance_km": float(total_distance_m / 1000),
            "elevation_gain_m": float(elev_gain),
            "elevation_loss_m": float(elev_loss),
            "min_elevation_m": float(np.min(elev_arr)),
            "max_elevation_m": float(np.max(elev_arr)),
            "cell_count": len(path_indices),
            "cell_size_m": meta["cell_size_m"],
        },
        "geometry": {
            "type": "LineString",
            "coordinates": coordinates,
        }
    }

    # Write output
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(geojson, f, indent=2)

    t_end = time.time()

    # Final report
    print(f"\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Start:            ({START_LAT:.4f}, {START_LON:.4f})")
    print(f"End:              ({END_LAT:.4f}, {END_LON:.4f})")
    print(f"Total effort:     {end_cost/60:.1f} minutes ({end_cost/3600:.2f} hours)")
    print(f"Distance:         {total_distance_m/1000:.2f} km")
    print(f"Elevation gain:   {elev_gain:.0f} m")
    print(f"Elevation loss:   {elev_loss:.0f} m")
    print(f"Elevation range:  {np.min(elev_arr):.0f} - {np.max(elev_arr):.0f} m")
    print(f"Path cells:       {len(path_indices):,}")
    print(f"Wall time:        {t_end - t0:.1f}s")
    print(f"\nOutput saved to:  {OUTPUT_PATH}")

    # Validation checks
    print(f"\n" + "-" * 60)
    print("VALIDATION")
    print("-" * 60)

    # Check coordinates are within bbox
    lons = [c[0] for c in coordinates]
    lats = [c[1] for c in coordinates]
    lon_ok = BBOX["west"] <= min(lons) and max(lons) <= BBOX["east"]
    lat_ok = BBOX["south"] <= min(lats) and max(lats) <= BBOX["north"]
    print(f"Coordinates within bbox: {'PASS' if lon_ok and lat_ok else 'FAIL'}")

    # Check path is not trivial
    is_nontrivial = len(path_indices) > 10 and total_distance_m > 1000
    print(f"Path is non-trivial: {'PASS' if is_nontrivial else 'FAIL'}")

    # Check it's not a straight line (measure sinuosity)
    straight_line_dist = np.sqrt(
        (coordinates[-1][0] - coordinates[0][0])**2 +
        (coordinates[-1][1] - coordinates[0][1])**2
    ) * 111000  # rough degrees to meters
    sinuosity = total_distance_m / max(straight_line_dist, 1)
    print(f"Sinuosity: {sinuosity:.2f} (>1.0 means path curves around obstacles)")

    reader.close()
    print("\nPrototype completed successfully.")


if __name__ == "__main__":
    main()
