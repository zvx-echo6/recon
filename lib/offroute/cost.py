"""
Tobler off-path hiking cost function for OFFROUTE.

Computes travel time cost based on terrain slope using Tobler's
hiking function with off-trail penalty.
"""
import math
import numpy as np
from typing import Tuple

# Maximum passable slope in degrees
MAX_SLOPE_DEG = 40.0

# Tobler off-path parameters
TOBLER_BASE_SPEED = 6.0
TOBLER_OFF_TRAIL_MULT = 0.6


def tobler_speed(grade: float) -> float:
    """
    Calculate hiking speed using Tobler's off-path function.

    speed_kmh = 0.6 * 6.0 * exp(-3.5 * |grade + 0.05|)

    Peak speed is ~3.6 km/h at grade = -0.05 (slight downhill).
    """
    return TOBLER_OFF_TRAIL_MULT * TOBLER_BASE_SPEED * math.exp(-3.5 * abs(grade + 0.05))


def compute_cost_grid(
    elevation: np.ndarray,
    cell_size_m: float,
    cell_size_lat_m: float = None,
    cell_size_lon_m: float = None
) -> np.ndarray:
    """
    Compute isotropic travel cost grid from elevation data.

    Each cell's cost represents the time (in seconds) to traverse that cell,
    based on the average slope from neighboring cells.
    """
    if cell_size_lat_m is None:
        cell_size_lat_m = cell_size_m
    if cell_size_lon_m is None:
        cell_size_lon_m = cell_size_m

    rows, cols = elevation.shape

    # Compute gradients in both directions
    dy = np.zeros_like(elevation)
    dx = np.zeros_like(elevation)

    # Central differences for interior, forward/backward at edges
    dy[1:-1, :] = (elevation[:-2, :] - elevation[2:, :]) / (2 * cell_size_lat_m)
    dy[0, :] = (elevation[0, :] - elevation[1, :]) / cell_size_lat_m
    dy[-1, :] = (elevation[-2, :] - elevation[-1, :]) / cell_size_lat_m

    dx[:, 1:-1] = (elevation[:, 2:] - elevation[:, :-2]) / (2 * cell_size_lon_m)
    dx[:, 0] = (elevation[:, 1] - elevation[:, 0]) / cell_size_lon_m
    dx[:, -1] = (elevation[:, -1] - elevation[:, -2]) / cell_size_lon_m

    # Compute slope magnitude (grade = rise/run)
    grade_magnitude = np.sqrt(dx**2 + dy**2)

    # Convert to slope angle in degrees
    slope_deg = np.degrees(np.arctan(grade_magnitude))

    # Compute speed for each cell using Tobler function
    speed_kmh = TOBLER_OFF_TRAIL_MULT * TOBLER_BASE_SPEED * np.exp(-3.5 * np.abs(grade_magnitude + 0.05))

    # Convert speed to time cost (seconds to traverse one cell)
    avg_cell_size = (cell_size_lat_m + cell_size_lon_m) / 2
    cost = avg_cell_size * 3.6 / speed_kmh

    # Set impassable cells (slope > MAX_SLOPE_DEG) to infinity
    cost[slope_deg > MAX_SLOPE_DEG] = np.inf

    # Handle NaN elevations (no data)
    cost[np.isnan(elevation)] = np.inf

    return cost


if __name__ == "__main__":
    print("Testing Tobler speed function:")
    for grade in [-0.3, -0.1, -0.05, 0.0, 0.05, 0.1, 0.3]:
        speed = tobler_speed(grade)
        print(f"  Grade {grade:+.2f}: {speed:.2f} km/h")

    print("\nTesting cost grid computation:")
    elev = np.arange(100).reshape(10, 10).astype(np.float32) * 10
    cost = compute_cost_grid(elev, cell_size_m=30.0)
    print(f"  Elevation range: {elev.min():.0f} - {elev.max():.0f} m")
    print(f"  Cost range: {cost[~np.isinf(cost)].min():.1f} - {cost[~np.isinf(cost)].max():.1f} s")
