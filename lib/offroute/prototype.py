#!/usr/bin/env python3
"""
OFFROUTE Phase O3a Prototype

Validates trail burn-in integration with the MCP pathfinder.
The path should actively seek out trails and roads when nearby.

Compares paths with and without trail burn-in to show the benefit
of trail-seeking behavior.
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
from lib.offroute.friction import FrictionReader, friction_to_multiplier
from lib.offroute.barriers import BarrierReader, DEFAULT_BARRIERS_PATH
from lib.offroute.trails import TrailReader, DEFAULT_TRAILS_PATH

# Test bounding box - Idaho area
BBOX = {
    "south": 42.21,
    "north": 42.60,
    "west": -114.76,
    "east": -113.79,
}

# Start point: wilderness area away from roads
START_LAT = 42.35
START_LON = -114.60

# End point: near Twin Falls (has roads/trails)
END_LAT = 42.55
END_LON = -114.20

# Output files
OUTPUT_PATH_WITH_TRAILS = Path("/opt/recon/data/offroute-test-trails.geojson")
OUTPUT_PATH_NO_TRAILS = Path("/opt/recon/data/offroute-test-no-trails.geojson")

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


def run_pathfinder(
    elevation: np.ndarray,
    meta: dict,
    friction_mult: np.ndarray,
    trails: np.ndarray,
    barriers: np.ndarray,
    use_trails: bool,
    start_row: int,
    start_col: int,
    end_row: int,
    end_col: int,
    dem_reader: DEMReader,
) -> dict:
    """Run the MCP pathfinder with given parameters."""
    # Compute cost grid
    cost = compute_cost_grid(
        elevation,
        cell_size_m=meta["cell_size_m"],
        friction=friction_mult,
        trails=trails if use_trails else None,
        barriers=barriers,
        boundary_mode="pragmatic",
    )

    # Run MCP
    mcp = MCP_Geometric(cost, fully_connected=True)
    cumulative_costs, traceback = mcp.find_costs([(start_row, start_col)])

    end_cost = cumulative_costs[end_row, end_col]

    if np.isinf(end_cost):
        return {
            "success": False,
            "reason": "No path found (blocked by impassable terrain)",
        }

    # Traceback path
    path_indices = mcp.traceback((end_row, end_col))

    # Convert to coordinates and collect stats
    coordinates = []
    elevations = []
    trail_values = []

    for row, col in path_indices:
        lat, lon = dem_reader.pixel_to_latlon(row, col, meta)
        elev = elevation[row, col]
        trail_val = trails[row, col] if trails is not None else 0
        coordinates.append([lon, lat])
        elevations.append(elev)
        trail_values.append(trail_val)

    # Compute distance
    total_distance_m = 0
    for i in range(1, len(coordinates)):
        lon1, lat1 = coordinates[i-1]
        lon2, lat2 = coordinates[i]
        R = 6371000
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)
        a = np.sin(dlat/2)**2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon/2)**2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        total_distance_m += R * c

    # Elevation stats
    elev_arr = np.array(elevations)
    elev_diff = np.diff(elev_arr)
    elev_gain = np.sum(elev_diff[elev_diff > 0])
    elev_loss = np.sum(np.abs(elev_diff[elev_diff < 0]))

    # Trail stats
    trail_arr = np.array(trail_values)
    road_cells = np.sum(trail_arr == 5)
    track_cells = np.sum(trail_arr == 15)
    trail_cells = np.sum(trail_arr == 25)
    off_trail_cells = np.sum(trail_arr == 0)
    on_trail_cells = road_cells + track_cells + trail_cells
    total_cells = len(trail_arr)

    return {
        "success": True,
        "coordinates": coordinates,
        "total_time_seconds": float(end_cost),
        "total_time_minutes": float(end_cost / 60),
        "total_distance_m": float(total_distance_m),
        "total_distance_km": float(total_distance_m / 1000),
        "elevation_gain_m": float(elev_gain),
        "elevation_loss_m": float(elev_loss),
        "min_elevation_m": float(np.min(elev_arr)),
        "max_elevation_m": float(np.max(elev_arr)),
        "cell_count": total_cells,
        "road_cells": int(road_cells),
        "track_cells": int(track_cells),
        "trail_cells": int(trail_cells),
        "off_trail_cells": int(off_trail_cells),
        "on_trail_pct": float(100 * on_trail_cells / total_cells) if total_cells > 0 else 0,
    }


def main():
    print("=" * 80)
    print("OFFROUTE Phase O3a Prototype (Trail Burn-In)")
    print("=" * 80)

    t0 = time.time()

    # Check for required rasters
    if not DEFAULT_BARRIERS_PATH.exists():
        print(f"\nERROR: Barrier raster not found at {DEFAULT_BARRIERS_PATH}")
        sys.exit(1)
    if not DEFAULT_TRAILS_PATH.exists():
        print(f"\nERROR: Trails raster not found at {DEFAULT_TRAILS_PATH}")
        sys.exit(1)

    # Step 1: Load elevation data
    print(f"\n[1] Loading DEM for bbox: {BBOX}")
    dem_reader = DEMReader()

    elevation, meta = dem_reader.get_elevation_grid(
        south=BBOX["south"],
        north=BBOX["north"],
        west=BBOX["west"],
        east=BBOX["east"],
    )

    print(f"    Elevation grid shape: {elevation.shape}")
    print(f"    Cell count: {elevation.size:,}")
    print(f"    Cell size: {meta['cell_size_m']:.1f} m")

    mem = check_memory_usage()
    if mem > 0:
        print(f"    Memory usage: {mem:.1f} GB")

    # Step 2: Load friction data
    print(f"\n[2] Loading WorldCover friction layer...")
    friction_reader = FrictionReader()

    friction_raw = friction_reader.get_friction_grid(
        south=BBOX["south"],
        north=BBOX["north"],
        west=BBOX["west"],
        east=BBOX["east"],
        target_shape=elevation.shape
    )
    friction_mult = friction_to_multiplier(friction_raw)

    print(f"    Friction grid shape: {friction_raw.shape}")
    print(f"    Water/impassable cells: {np.sum(np.isinf(friction_mult)):,}")

    # Step 3: Load barrier data
    print(f"\n[3] Loading PAD-US barrier layer...")
    barrier_reader = BarrierReader()

    barriers = barrier_reader.get_barrier_grid(
        south=BBOX["south"],
        north=BBOX["north"],
        west=BBOX["west"],
        east=BBOX["east"],
        target_shape=elevation.shape
    )

    closed_cells = np.sum(barriers == 255)
    print(f"    Barrier grid shape: {barriers.shape}")
    print(f"    Closed/restricted cells: {closed_cells:,}")

    # Step 4: Load trails data
    print(f"\n[4] Loading OSM trails layer...")
    trail_reader = TrailReader()

    trails = trail_reader.get_trails_grid(
        south=BBOX["south"],
        north=BBOX["north"],
        west=BBOX["west"],
        east=BBOX["east"],
        target_shape=elevation.shape
    )

    road_cells = np.sum(trails == 5)
    track_cells = np.sum(trails == 15)
    trail_cells = np.sum(trails == 25)
    print(f"    Trails grid shape: {trails.shape}")
    print(f"    Road cells: {road_cells:,}")
    print(f"    Track cells: {track_cells:,}")
    print(f"    Trail cells: {trail_cells:,}")
    print(f"    Total trail coverage: {100*(road_cells+track_cells+trail_cells)/trails.size:.2f}%")

    mem = check_memory_usage()
    if mem > 0:
        print(f"    Memory usage: {mem:.1f} GB")

    # Step 5: Convert start/end to pixel coordinates
    print(f"\n[5] Converting coordinates...")
    start_row, start_col = dem_reader.latlon_to_pixel(START_LAT, START_LON, meta)
    end_row, end_col = dem_reader.latlon_to_pixel(END_LAT, END_LON, meta)

    print(f"    Start: ({START_LAT}, {START_LON}) -> pixel ({start_row}, {start_col})")
    print(f"    End: ({END_LAT}, {END_LON}) -> pixel ({end_row}, {end_col})")

    # Validate coordinates
    rows, cols = elevation.shape
    if not (0 <= start_row < rows and 0 <= start_col < cols):
        print(f"ERROR: Start point outside grid bounds")
        sys.exit(1)
    if not (0 <= end_row < rows and 0 <= end_col < cols):
        print(f"ERROR: End point outside grid bounds")
        sys.exit(1)

    # Step 6: Run pathfinder WITH trails
    print(f"\n[6] Running pathfinder WITH trail burn-in...")
    t6a = time.time()
    result_trails = run_pathfinder(
        elevation, meta, friction_mult, trails, barriers,
        use_trails=True,
        start_row=start_row, start_col=start_col,
        end_row=end_row, end_col=end_col,
        dem_reader=dem_reader,
    )
    t6b = time.time()
    print(f"    Completed in {t6b - t6a:.1f}s")

    # Step 7: Run pathfinder WITHOUT trails
    print(f"\n[7] Running pathfinder WITHOUT trail burn-in...")
    t7a = time.time()
    result_no_trails = run_pathfinder(
        elevation, meta, friction_mult, trails, barriers,
        use_trails=False,
        start_row=start_row, start_col=start_col,
        end_row=end_row, end_col=end_col,
        dem_reader=dem_reader,
    )
    t7b = time.time()
    print(f"    Completed in {t7b - t7a:.1f}s")

    # Step 8: Save GeoJSON outputs
    print(f"\n[8] Saving GeoJSON outputs...")

    OUTPUT_PATH_WITH_TRAILS.parent.mkdir(parents=True, exist_ok=True)

    if result_trails["success"]:
        geojson = {
            "type": "Feature",
            "properties": {
                "type": "offroute_with_trails",
                "phase": "O3a",
                "trail_burn_in": True,
                "start": {"lat": START_LAT, "lon": START_LON},
                "end": {"lat": END_LAT, "lon": END_LON},
                **{k: v for k, v in result_trails.items() if k not in ["success", "coordinates"]},
            },
            "geometry": {
                "type": "LineString",
                "coordinates": result_trails["coordinates"],
            }
        }
        with open(OUTPUT_PATH_WITH_TRAILS, "w") as f:
            json.dump(geojson, f, indent=2)
        print(f"    Saved: {OUTPUT_PATH_WITH_TRAILS}")

    if result_no_trails["success"]:
        geojson = {
            "type": "Feature",
            "properties": {
                "type": "offroute_no_trails",
                "phase": "O3a",
                "trail_burn_in": False,
                "start": {"lat": START_LAT, "lon": START_LON},
                "end": {"lat": END_LAT, "lon": END_LON},
                **{k: v for k, v in result_no_trails.items() if k not in ["success", "coordinates"]},
            },
            "geometry": {
                "type": "LineString",
                "coordinates": result_no_trails["coordinates"],
            }
        }
        with open(OUTPUT_PATH_NO_TRAILS, "w") as f:
            json.dump(geojson, f, indent=2)
        print(f"    Saved: {OUTPUT_PATH_NO_TRAILS}")

    t_total = time.time()

    # Final report
    print(f"\n" + "=" * 80)
    print("SIDE-BY-SIDE COMPARISON: Trail Burn-In Effect")
    print("=" * 80)

    if result_trails["success"] and result_no_trails["success"]:
        print(f"{'Metric':<25} {'WITH TRAILS':<20} {'WITHOUT TRAILS':<20} {'Delta':<15}")
        print("-" * 80)

        metrics = [
            ("Distance (km)", "total_distance_km", ".2f"),
            ("Effort time (min)", "total_time_minutes", ".1f"),
            ("Cell count", "cell_count", "d"),
            ("Elevation gain (m)", "elevation_gain_m", ".0f"),
            ("On-trail %", "on_trail_pct", ".1f"),
            ("Road cells", "road_cells", "d"),
            ("Track cells", "track_cells", "d"),
            ("Trail cells", "trail_cells", "d"),
        ]

        for label, key, fmt in metrics:
            val_with = result_trails[key]
            val_without = result_no_trails[key]
            if isinstance(val_with, int):
                delta = val_with - val_without
                delta_str = f"{delta:+d}"
            else:
                delta = val_with - val_without
                delta_str = f"{delta:+.2f}"
            print(f"{label:<25} {val_with:<20{fmt}} {val_without:<20{fmt}} {delta_str:<15}")

        # Analysis
        print(f"\n" + "-" * 80)
        print("ANALYSIS")
        print("-" * 80)

        time_saved = result_no_trails["total_time_minutes"] - result_trails["total_time_minutes"]
        if time_saved > 0:
            print(f"Trail burn-in saves {time_saved:.1f} minutes ({100*time_saved/result_no_trails['total_time_minutes']:.1f}% faster)")
        elif time_saved < 0:
            print(f"Trail burn-in adds {-time_saved:.1f} minutes (path seeks trails even if longer)")

        on_trail_with = result_trails["on_trail_pct"]
        on_trail_without = result_no_trails["on_trail_pct"]
        if on_trail_with > on_trail_without:
            print(f"Trail burn-in increases on-trail travel: {on_trail_without:.1f}% → {on_trail_with:.1f}%")
        else:
            print(f"Both paths have similar on-trail percentage")

    else:
        if not result_trails["success"]:
            print(f"WITH TRAILS: FAILED - {result_trails.get('reason', 'unknown')}")
        if not result_no_trails["success"]:
            print(f"WITHOUT TRAILS: FAILED - {result_no_trails.get('reason', 'unknown')}")

    print(f"\n" + "-" * 80)
    print(f"Total wall time: {t_total - t0:.1f}s")

    # Cleanup
    dem_reader.close()
    friction_reader.close()
    barrier_reader.close()
    trail_reader.close()

    print("\nPrototype completed.")


if __name__ == "__main__":
    main()
