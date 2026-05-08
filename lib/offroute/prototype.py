#!/usr/bin/env python3
"""
OFFROUTE Phase O2c Prototype

Validates the PMTiles decoder, Tobler cost function, WorldCover friction,
PAD-US barriers integration, and MCP pathfinder on a real Idaho bounding box.

Runs THREE pathfinding passes with different boundary modes:
  1. boundary_mode="strict"    - private land is impassable
  2. boundary_mode="pragmatic" - private land has 5x friction penalty
  3. boundary_mode="emergency" - private land barriers ignored

Outputs comparison showing impact of boundary mode on routing.
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

# Test bounding box - Idaho area known to have mixed public/private land
BBOX = {
    "south": 42.21,
    "north": 42.60,
    "west": -114.76,
    "east": -113.79,
}

# Start point: wilderness area south of Twin Falls
START_LAT = 42.36
START_LON = -114.55

# End point: near Burley, ID (on road network)
END_LAT = 42.55
END_LON = -114.25

# Output files
OUTPUT_PATHS = {
    "strict": Path("/opt/recon/data/offroute-test-strict.geojson"),
    "pragmatic": Path("/opt/recon/data/offroute-test-pragmatic.geojson"),
    "emergency": Path("/opt/recon/data/offroute-test-emergency.geojson"),
}

# Old files to delete
OLD_FILES = [
    Path("/opt/recon/data/offroute-test-barriers-on.geojson"),
    Path("/opt/recon/data/offroute-test-barriers-off.geojson"),
]

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
    barriers: np.ndarray,
    boundary_mode: str,
    start_row: int,
    start_col: int,
    end_row: int,
    end_col: int,
    dem_reader: DEMReader,
) -> dict:
    """
    Run the MCP pathfinder with given parameters.

    Returns dict with path info and stats.
    """
    # Compute cost grid
    cost = compute_cost_grid(
        elevation,
        cell_size_m=meta["cell_size_m"],
        friction=friction_mult,
        barriers=barriers,
        boundary_mode=boundary_mode,
    )

    # Count impassable cells
    impassable_count = np.sum(np.isinf(cost))
    barrier_count = np.sum(barriers == 255) if barriers is not None else 0

    # Run MCP
    mcp = MCP_Geometric(cost, fully_connected=True)
    cumulative_costs, traceback = mcp.find_costs([(start_row, start_col)])

    end_cost = cumulative_costs[end_row, end_col]

    if np.isinf(end_cost):
        return {
            "success": False,
            "reason": "No path found (blocked by impassable terrain)",
            "impassable_cells": int(impassable_count),
            "barrier_cells": int(barrier_count),
        }

    # Traceback path
    path_indices = mcp.traceback((end_row, end_col))

    # Convert to coordinates
    coordinates = []
    elevations = []
    barrier_values = []

    for row, col in path_indices:
        lat, lon = dem_reader.pixel_to_latlon(row, col, meta)
        elev = elevation[row, col]
        barr = barriers[row, col] if barriers is not None else 0
        coordinates.append([lon, lat])
        elevations.append(elev)
        barrier_values.append(barr)

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

    # Barrier crossings on path
    barr_arr = np.array(barrier_values)
    barrier_crossings = np.sum(barr_arr == 255)

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
        "cell_count": len(path_indices),
        "impassable_cells": int(impassable_count),
        "barrier_cells": int(barrier_count),
        "barrier_crossings": int(barrier_crossings),
    }


def main():
    print("=" * 80)
    print("OFFROUTE Phase O2c Prototype (Three-Mode Boundary Respect)")
    print("=" * 80)

    t0 = time.time()

    # Delete old output files
    for old_file in OLD_FILES:
        if old_file.exists():
            old_file.unlink()
            print(f"Deleted old file: {old_file}")

    # Check if barrier raster exists
    if not DEFAULT_BARRIERS_PATH.exists():
        print(f"\nERROR: Barrier raster not found at {DEFAULT_BARRIERS_PATH}")
        print(f"Run first: python /opt/recon/lib/offroute/barriers.py build")
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
    print(f"    Closed/restricted cells: {closed_cells:,} ({100*closed_cells/barriers.size:.2f}%)")

    if closed_cells == 0:
        print("\n    WARNING: No closed/restricted areas in this bbox.")
        print("    The test may not show meaningful differences between modes.")

    mem = check_memory_usage()
    if mem > 0:
        print(f"    Memory usage: {mem:.1f} GB")

    # Step 4: Convert start/end to pixel coordinates
    print(f"\n[4] Converting coordinates...")
    start_row, start_col = dem_reader.latlon_to_pixel(START_LAT, START_LON, meta)
    end_row, end_col = dem_reader.latlon_to_pixel(END_LAT, END_LON, meta)

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

    # Step 5: Run pathfinder THREE times
    results = {}
    modes = ["strict", "pragmatic", "emergency"]

    for i, mode in enumerate(modes, start=5):
        print(f"\n[{i}] Running pathfinder (boundary_mode=\"{mode}\")...")
        t_start = time.time()
        results[mode] = run_pathfinder(
            elevation, meta, friction_mult, barriers,
            boundary_mode=mode,
            start_row=start_row, start_col=start_col,
            end_row=end_row, end_col=end_col,
            dem_reader=dem_reader,
        )
        t_end = time.time()
        print(f"    Completed in {t_end - t_start:.1f}s")

    # Step 6: Save GeoJSON outputs
    print(f"\n[8] Saving GeoJSON outputs...")

    OUTPUT_PATHS["strict"].parent.mkdir(parents=True, exist_ok=True)

    for mode, result in results.items():
        output_path = OUTPUT_PATHS[mode]
        if result["success"]:
            geojson = {
                "type": "Feature",
                "properties": {
                    "type": f"offroute_{mode}",
                    "phase": "O2c",
                    "boundary_mode": mode,
                    "start": {"lat": START_LAT, "lon": START_LON},
                    "end": {"lat": END_LAT, "lon": END_LON},
                    **{k: v for k, v in result.items() if k not in ["success", "coordinates"]},
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": result["coordinates"],
                }
            }
            with open(output_path, "w") as f:
                json.dump(geojson, f, indent=2)
            print(f"    Saved: {output_path}")
        else:
            print(f"    SKIPPED ({mode}): {result['reason']}")

    t_total = time.time()

    # Final report - three-way comparison
    print(f"\n" + "=" * 80)
    print("THREE-WAY COMPARISON")
    print("=" * 80)

    # Check how many succeeded
    success_count = sum(1 for r in results.values() if r["success"])

    if success_count == 3:
        print(f"{'Metric':<22} {'STRICT':<18} {'PRAGMATIC':<18} {'EMERGENCY':<18}")
        print("-" * 80)

        metrics = [
            ("Distance (km)", "total_distance_km", ".2f"),
            ("Effort time (min)", "total_time_minutes", ".1f"),
            ("Cell count", "cell_count", "d"),
            ("Elevation gain (m)", "elevation_gain_m", ".0f"),
            ("Elevation loss (m)", "elevation_loss_m", ".0f"),
            ("Barrier crossings", "barrier_crossings", "d"),
            ("Impassable cells", "impassable_cells", ",d"),
        ]

        for label, key, fmt in metrics:
            vals = [results[m][key] for m in modes]
            print(f"{label:<22} {vals[0]:<18{fmt}} {vals[1]:<18{fmt}} {vals[2]:<18{fmt}}")

        # Analysis
        print(f"\n" + "-" * 80)
        print("ANALYSIS")
        print("-" * 80)

        strict_crossings = results["strict"]["barrier_crossings"]
        pragmatic_crossings = results["pragmatic"]["barrier_crossings"]
        emergency_crossings = results["emergency"]["barrier_crossings"]

        print(f"Barrier crossings: strict={strict_crossings}, pragmatic={pragmatic_crossings}, emergency={emergency_crossings}")

        if strict_crossings == 0 and pragmatic_crossings == 0 and emergency_crossings == 0:
            print("No path crosses private land - terrain naturally avoids barriers.")
        else:
            if emergency_crossings > pragmatic_crossings:
                print(f"Pragmatic mode reduces barrier crossings vs emergency: {emergency_crossings} -> {pragmatic_crossings}")
            if pragmatic_crossings > 0 and strict_crossings == 0:
                print(f"Strict mode completely avoids private land (pragmatic crosses {pragmatic_crossings} cells)")

            # Time/distance comparison
            if results["strict"]["total_time_minutes"] > results["emergency"]["total_time_minutes"]:
                time_penalty = results["strict"]["total_time_minutes"] - results["emergency"]["total_time_minutes"]
                print(f"Time cost of strict boundary respect: +{time_penalty:.1f} min")

    else:
        print(f"Only {success_count}/3 modes found a path:")
        for mode, result in results.items():
            if result["success"]:
                print(f"  {mode}: {result['total_distance_km']:.2f} km, {result['total_time_minutes']:.1f} min")
            else:
                print(f"  {mode}: FAILED - {result.get('reason', 'unknown')}")

    print(f"\n" + "-" * 80)
    print(f"Total wall time: {t_total - t0:.1f}s")
    print(f"Closed cells in bbox: {closed_cells:,}")

    # Cleanup
    dem_reader.close()
    friction_reader.close()
    barrier_reader.close()

    print("\nPrototype completed.")


if __name__ == "__main__":
    main()
