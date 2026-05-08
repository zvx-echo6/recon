"""
Tobler off-path hiking cost function for OFFROUTE.

Computes travel time cost based on terrain slope using Tobler's
hiking function with off-trail penalty. Optionally applies friction
multipliers from land cover data, trail corridors, and barrier grids.
"""
import math
import numpy as np
from typing import Optional, Literal

# Maximum passable slope in degrees
MAX_SLOPE_DEG = 40.0

# Tobler off-path parameters
TOBLER_BASE_SPEED = 6.0
TOBLER_OFF_TRAIL_MULT = 0.6

# Pragmatic mode friction multiplier for private land
PRAGMATIC_BARRIER_MULTIPLIER = 5.0

# Trail value to friction multiplier mapping
# Trail friction REPLACES land cover friction (a road through forest is still easy)
TRAIL_FRICTION_MAP = {
    5: 0.1,   # road
    15: 0.3,  # track
    25: 0.5,  # foot trail
}


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
    cell_size_lon_m: float = None,
    friction: Optional[np.ndarray] = None,
    trails: Optional[np.ndarray] = None,
    barriers: Optional[np.ndarray] = None,
    boundary_mode: Literal["strict", "pragmatic", "emergency"] = "pragmatic"
) -> np.ndarray:
    """
    Compute isotropic travel cost grid from elevation data.

    Each cell's cost represents the time (in seconds) to traverse that cell,
    based on the average slope from neighboring cells.

    Args:
        elevation: 2D array of elevation values in meters
        cell_size_m: Average cell size in meters
        cell_size_lat_m: Cell size in latitude direction (optional)
        cell_size_lon_m: Cell size in longitude direction (optional)
        friction: Optional 2D array of friction multipliers (WorldCover).
                  Values should be float (1.0 = baseline, 2.0 = 2x slower).
                  np.inf marks impassable cells.
                  If None, no friction is applied (backward compatible).
        trails: Optional 2D array of trail values (uint8).
                  0 = no trail (use friction)
                  5 = road (0.1× friction, replaces WorldCover)
                  15 = track (0.3× friction, replaces WorldCover)
                  25 = foot trail (0.5× friction, replaces WorldCover)
                  Trail friction REPLACES land cover friction where trails exist.
                  If None, no trail burn-in is applied.
        barriers: Optional 2D array of barrier values (uint8).
                  255 = closed/restricted area (from PAD-US Pub_Access = XA).
                  0 = accessible.
                  If None, no barriers are applied.
        boundary_mode: How to handle private/restricted land barriers:
                  "strict"    - cells with barrier=255 become impassable (np.inf)
                  "pragmatic" - cells with barrier=255 get 5.0x friction penalty
                  "emergency" - barriers are ignored entirely
                  Default: "pragmatic"

    Returns:
        2D array of travel cost in seconds per cell.
        np.inf for impassable cells.
    """
    if boundary_mode not in ("strict", "pragmatic", "emergency"):
        raise ValueError(f"boundary_mode must be 'strict', 'pragmatic', or 'emergency', got '{boundary_mode}'")

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

    # Build effective friction array
    # Start with WorldCover friction if provided, else 1.0
    if friction is not None:
        if friction.shape != elevation.shape:
            raise ValueError(
                f"Friction shape {friction.shape} does not match elevation shape {elevation.shape}"
            )
        effective_friction = friction.copy()
    else:
        effective_friction = np.ones(elevation.shape, dtype=np.float32)

    # Apply trail burn-in: trails REPLACE land cover friction
    if trails is not None:
        if trails.shape != elevation.shape:
            raise ValueError(
                f"Trails shape {trails.shape} does not match elevation shape {elevation.shape}"
            )
        # Replace friction where trails exist
        for trail_value, trail_friction in TRAIL_FRICTION_MAP.items():
            trail_mask = trails == trail_value
            effective_friction[trail_mask] = trail_friction

    # Apply friction to cost
    cost = cost * effective_friction

    # Apply barriers based on boundary_mode
    if barriers is not None and boundary_mode != "emergency":
        if barriers.shape != elevation.shape:
            raise ValueError(
                f"Barriers shape {barriers.shape} does not match elevation shape {elevation.shape}"
            )

        barrier_mask = barriers == 255

        if boundary_mode == "strict":
            # Mark closed/restricted areas as impassable
            cost[barrier_mask] = np.inf
        elif boundary_mode == "pragmatic":
            # Apply friction penalty to closed/restricted areas
            cost[barrier_mask] = cost[barrier_mask] * PRAGMATIC_BARRIER_MULTIPLIER

    return cost


if __name__ == "__main__":
    print("Testing Tobler speed function:")
    for grade in [-0.3, -0.1, -0.05, 0.0, 0.05, 0.1, 0.3]:
        speed = tobler_speed(grade)
        print(f"  Grade {grade:+.2f}: {speed:.2f} km/h")

    print("\nTesting cost grid computation (no friction, no trails):")
    elev = np.arange(100).reshape(10, 10).astype(np.float32) * 10
    cost = compute_cost_grid(elev, cell_size_m=30.0)
    print(f"  Elevation range: {elev.min():.0f} - {elev.max():.0f} m")
    finite = cost[~np.isinf(cost)]
    if len(finite) > 0:
        print(f"  Cost range: {finite.min():.1f} - {finite.max():.1f} s")
    else:
        print(f"  All cells impassable (test data too steep)")

    print("\nTesting cost grid with friction and trails:")
    elev = np.ones((10, 10), dtype=np.float32) * 1000  # flat terrain
    friction = np.ones((10, 10), dtype=np.float32) * 2.0  # 2.0x friction (forest)
    trails = np.zeros((10, 10), dtype=np.uint8)
    trails[5, :] = 5  # road across middle row

    cost_no_trail = compute_cost_grid(elev, cell_size_m=30.0, friction=friction)
    cost_with_trail = compute_cost_grid(elev, cell_size_m=30.0, friction=friction, trails=trails)

    base_cost = 30 * 3.6 / (0.6 * 6.0 * np.exp(-3.5 * 0.05))
    print(f"  Base cost (flat, 30m cell): {base_cost:.1f} s")
    print(f"  Forest cell (2.0x friction): {cost_no_trail[0, 0]:.1f} s")
    print(f"  Road cell (0.1x friction, replaces forest): {cost_with_trail[5, 0]:.1f} s")
    print(f"  Road friction advantage: {cost_no_trail[0, 0] / cost_with_trail[5, 0]:.1f}x faster")

    print("\nTesting cost grid with barriers (three modes):")
    elev = np.ones((10, 10), dtype=np.float32) * 1000
    barriers = np.zeros((10, 10), dtype=np.uint8)
    barriers[3:7, 3:7] = 255

    for mode in ["strict", "pragmatic", "emergency"]:
        cost = compute_cost_grid(elev, cell_size_m=30.0, barriers=barriers, boundary_mode=mode)
        impassable = np.sum(np.isinf(cost))
        barrier_cost = cost[5, 5] if not np.isinf(cost[5, 5]) else "inf"
        print(f"  {mode:10s}: {impassable} impassable, barrier cell cost = {barrier_cost}")
