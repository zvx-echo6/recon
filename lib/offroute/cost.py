"""
Multi-mode travel cost functions for OFFROUTE.

Supports four travel modes: foot, mtb, atv, vehicle.
Each mode has its own speed function, max slope, trail access rules,
and terrain friction overrides.

Mode profiles are data-driven — adding a new mode means adding a profile entry.
"""
import math
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, Literal, Dict, Callable

# ═══════════════════════════════════════════════════════════════════════════════
# SPEED FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def tobler_off_path_speed(grade: np.ndarray, base_speed: float = 6.0) -> np.ndarray:
    """
    Tobler off-path hiking function.

    W = 0.6 * base_speed * exp(-3.5 * |S + 0.05|)

    Peak ~3.6 km/h at grade = -0.05 (slight downhill).
    The 0.6 multiplier is the off-trail penalty.
    """
    return 0.6 * base_speed * np.exp(-3.5 * np.abs(grade + 0.05))


def herzog_wheeled_speed(grade: np.ndarray, base_speed: float = 12.0) -> np.ndarray:
    """
    Herzog wheeled-transport polynomial.

    Relative speed factor:
    1 / (1337.8·S^6 + 278.19·S^5 − 517.39·S^4 − 78.199·S^3 + 93.419·S^2 + 19.825·|S| + 1.64)

    Multiply by base_speed to get km/h.
    """
    S = grade
    S_abs = np.abs(S)

    # Herzog polynomial (returns relative speed factor 0-1)
    denom = (1337.8 * S**6 + 278.19 * S**5 - 517.39 * S**4
             - 78.199 * S**3 + 93.419 * S**2 + 19.825 * S_abs + 1.64)

    # Avoid division by zero and negative speeds
    denom = np.maximum(denom, 0.1)
    rel_speed = 1.0 / denom

    # Clamp relative speed to reasonable bounds (0.05 to 1.5)
    rel_speed = np.clip(rel_speed, 0.05, 1.5)

    return base_speed * rel_speed


def linear_degrade_speed(grade: np.ndarray, base_speed: float = 40.0, max_grade: float = 0.364) -> np.ndarray:
    """
    Linear speed degradation with slope.

    speed = base_speed * max(0, 1 - |grade| / max_grade)

    max_grade = tan(20°) ≈ 0.364 for 20° max slope.
    """
    speed = base_speed * np.maximum(0, 1.0 - np.abs(grade) / max_grade)
    return np.maximum(speed, 0.1)  # Minimum crawl speed


# ═══════════════════════════════════════════════════════════════════════════════
# MODE PROFILES (Data-driven configuration)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ModeProfile:
    """Configuration for a travel mode."""

    name: str
    description: str

    # Speed function parameters
    speed_function: str  # "tobler", "herzog", "linear"
    base_speed_kmh: float
    max_slope_deg: float

    # Trail access: trail_value -> friction multiplier (None = impassable)
    # Trail values: 5=road, 15=track, 25=foot trail
    trail_friction: Dict[int, Optional[float]] = field(default_factory=dict)

    # Off-trail terrain friction overrides (by WorldCover class)
    # These MULTIPLY the base WorldCover friction
    # None = use default, np.inf = impassable
    # WorldCover values: 10=tree, 20=shrub, 30=grass, 40=crop, 50=urban,
    #                    60=bare, 80=water, 90=wetland, 95=mangrove, 100=moss
    terrain_friction_override: Dict[int, Optional[float]] = field(default_factory=dict)

    # Should wilderness areas be impassable?
    wilderness_impassable: bool = False

    # For vehicle mode: can traverse off-trail flat terrain?
    off_trail_flat_threshold_deg: float = 0.0  # 0 = no off-trail allowed
    off_trail_flat_friction: float = np.inf  # friction if allowed


# Define all mode profiles
MODE_PROFILES: Dict[str, ModeProfile] = {
    "foot": ModeProfile(
        name="foot",
        description="Hiking on foot (Tobler off-path model)",
        speed_function="tobler",
        base_speed_kmh=6.0,
        max_slope_deg=40.0,
        trail_friction={
            5: 0.1,   # road
            15: 0.3,  # track
            25: 0.5,  # foot trail
        },
        terrain_friction_override={
            # Use default WorldCover friction for foot mode
        },
        wilderness_impassable=False,
    ),

    "mtb": ModeProfile(
        name="mtb",
        description="Mountain bike / dirt bike (Herzog wheeled model)",
        speed_function="herzog",
        base_speed_kmh=12.0,
        max_slope_deg=25.0,
        trail_friction={
            5: 0.1,   # road
            15: 0.2,  # track
            25: 0.5,  # foot trail (rideable but slow)
        },
        terrain_friction_override={
            30: 2.0,   # Grassland: rideable but slow
            20: 4.0,   # Shrubland: barely rideable
            10: 8.0,   # Tree cover/forest: effectively impassable
            60: 3.0,   # Bare/rocky
            90: np.inf,  # Wetland: impassable
            95: np.inf,  # Mangrove: impassable
            80: np.inf,  # Water: impassable
        },
        wilderness_impassable=True,
    ),

    "atv": ModeProfile(
        name="atv",
        description="ATV / side-by-side (Herzog wheeled model, higher base speed)",
        speed_function="herzog",
        base_speed_kmh=25.0,
        max_slope_deg=30.0,
        trail_friction={
            5: 0.1,    # road
            15: 0.3,   # track
            25: None,  # foot trail: impassable (too narrow)
        },
        terrain_friction_override={
            30: 1.5,   # Grassland: passable
            20: 3.0,   # Shrubland: rough
            10: np.inf,  # Forest: impassable
            60: 2.0,   # Bare/rocky
            90: np.inf,  # Wetland: impassable
            95: np.inf,  # Mangrove: impassable
            80: np.inf,  # Water: impassable
        },
        wilderness_impassable=True,
    ),

    "vehicle": ModeProfile(
        name="vehicle",
        description="4x4 truck / jeep (linear speed degradation)",
        speed_function="linear",
        base_speed_kmh=40.0,
        max_slope_deg=20.0,
        trail_friction={
            5: 0.1,    # road
            15: 0.5,   # track (rough but passable)
            25: None,  # foot trail: impassable
        },
        terrain_friction_override={
            # All off-trail terrain is impassable by default
            10: np.inf,  # Forest
            20: np.inf,  # Shrubland
            30: np.inf,  # Grassland (except flat - see below)
            40: np.inf,  # Cropland (except flat - see below)
            60: np.inf,  # Bare
            90: np.inf,  # Wetland
            95: np.inf,  # Mangrove
            80: np.inf,  # Water
        },
        wilderness_impassable=True,
        off_trail_flat_threshold_deg=5.0,  # Can drive on flat fields
        off_trail_flat_friction=5.0,       # But very slow
    ),
}


# Pragmatic mode friction multiplier for private land
PRAGMATIC_BARRIER_MULTIPLIER = 5.0


# ═══════════════════════════════════════════════════════════════════════════════
# COST GRID COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════

def compute_cost_grid(
    elevation: np.ndarray,
    cell_size_m: float,
    cell_size_lat_m: float = None,
    cell_size_lon_m: float = None,
    friction: Optional[np.ndarray] = None,
    friction_raw: Optional[np.ndarray] = None,
    trails: Optional[np.ndarray] = None,
    barriers: Optional[np.ndarray] = None,
    wilderness: Optional[np.ndarray] = None,
    boundary_mode: Literal["strict", "pragmatic", "emergency"] = "pragmatic",
    mode: Literal["foot", "mtb", "atv", "vehicle"] = "foot"
) -> np.ndarray:
    """
    Compute isotropic travel cost grid from elevation data.

    Args:
        elevation: 2D array of elevation values in meters
        cell_size_m: Average cell size in meters
        cell_size_lat_m: Cell size in latitude direction (optional)
        cell_size_lon_m: Cell size in longitude direction (optional)
        friction: Optional 2D array of friction multipliers (WorldCover).
                  Values should be float (1.0 = baseline, 2.0 = 2x slower).
                  np.inf marks impassable cells.
        friction_raw: Optional 2D array of raw WorldCover class values (uint8).
                  Used for mode-specific terrain overrides.
                  Values: 10=tree, 20=shrub, 30=grass, etc.
        trails: Optional 2D array of trail values (uint8).
                  0 = no trail, 5 = road, 15 = track, 25 = foot trail
        barriers: Optional 2D array of barrier values (uint8).
                  255 = closed/restricted area (PAD-US Pub_Access = XA).
        wilderness: Optional[np.ndarray] of wilderness values (uint8).
                  255 = designated wilderness area.
        boundary_mode: How to handle barriers ("strict", "pragmatic", "emergency")
        mode: Travel mode ("foot", "mtb", "atv", "vehicle")

    Returns:
        2D array of travel cost in seconds per cell.
        np.inf for impassable cells.
    """
    if boundary_mode not in ("strict", "pragmatic", "emergency"):
        raise ValueError(f"boundary_mode must be 'strict', 'pragmatic', or 'emergency'")

    if mode not in MODE_PROFILES:
        raise ValueError(f"mode must be one of {list(MODE_PROFILES.keys())}")

    profile = MODE_PROFILES[mode]

    if cell_size_lat_m is None:
        cell_size_lat_m = cell_size_m
    if cell_size_lon_m is None:
        cell_size_lon_m = cell_size_m

    rows, cols = elevation.shape

    # ─── Compute gradients (in-place where possible) ─────────────────────────
    # Use float32 to reduce memory footprint
    grade = np.zeros(elevation.shape, dtype=np.float32)

    # Compute dy contribution to grade squared
    dy_contrib = np.zeros(elevation.shape, dtype=np.float32)
    dy_contrib[1:-1, :] = ((elevation[:-2, :] - elevation[2:, :]) / (2 * cell_size_lat_m)) ** 2
    dy_contrib[0, :] = ((elevation[0, :] - elevation[1, :]) / cell_size_lat_m) ** 2
    dy_contrib[-1, :] = ((elevation[-2, :] - elevation[-1, :]) / cell_size_lat_m) ** 2

    # Compute dx contribution and add to dy_contrib in-place
    dy_contrib[:, 1:-1] += ((elevation[:, 2:] - elevation[:, :-2]) / (2 * cell_size_lon_m)) ** 2
    dy_contrib[:, 0] += ((elevation[:, 1] - elevation[:, 0]) / cell_size_lon_m) ** 2
    dy_contrib[:, -1] += ((elevation[:, -1] - elevation[:, -2]) / cell_size_lon_m) ** 2

    # grade = sqrt(dx^2 + dy^2)
    np.sqrt(dy_contrib, out=grade)
    del dy_contrib  # Free memory immediately

    # ─── Compute speed based on mode ─────────────────────────────────────────
    max_grade_val = np.tan(np.radians(profile.max_slope_deg))

    if profile.speed_function == "tobler":
        speed_kmh = tobler_off_path_speed(grade, profile.base_speed_kmh)
    elif profile.speed_function == "herzog":
        speed_kmh = herzog_wheeled_speed(grade, profile.base_speed_kmh)
    elif profile.speed_function == "linear":
        speed_kmh = linear_degrade_speed(grade, profile.base_speed_kmh, max_grade_val)
    else:
        raise ValueError(f"Unknown speed function: {profile.speed_function}")

    # ─── Base cost (seconds per cell) ─────────────────────────────────────────
    avg_cell_size = (cell_size_lat_m + cell_size_lon_m) / 2
    cost = (avg_cell_size * 3.6) / speed_kmh
    del speed_kmh

    # ─── Max slope limit ──────────────────────────────────────────────────────
    cost[grade > max_grade_val] = np.inf

    # ─── NaN elevations ──────────────────────────────────────────────────────
    cost[np.isnan(elevation)] = np.inf

    # ─── Apply friction in-place ─────────────────────────────────────────────
    # Instead of creating effective_friction copy, apply directly to cost

    # Start with base friction
    if friction is not None:
        if friction.shape != elevation.shape:
            raise ValueError(f"Friction shape mismatch")
        np.multiply(cost, friction, out=cost)

    # ─── Mode-specific terrain friction overrides (memory-efficient) ─────────
    if friction_raw is not None and profile.terrain_friction_override:
        if friction_raw.shape != elevation.shape:
            raise ValueError(f"Friction_raw shape mismatch")

        # Process all overrides without creating large intermediate masks
        for wc_class, override in profile.terrain_friction_override.items():
            if override is not None:
                if override == np.inf:
                    # Use np.where for in-place-like behavior
                    np.putmask(cost, friction_raw == wc_class, np.inf)
                else:
                    # Multiply cost where friction_raw matches
                    # Using a loop with putmask is more memory efficient
                    mask = friction_raw == wc_class
                    cost[mask] *= override
                    del mask

    # ─── Vehicle mode: allow flat grassland/cropland ─────────────────────────
    if mode == "vehicle" and profile.off_trail_flat_threshold_deg > 0:
        if friction_raw is not None:
            # Compute slope in degrees for flat terrain check
            slope_deg = np.degrees(np.arctan(grade))
            # Flat grassland or cropland - recompute cost for these cells
            flat_field_mask = (
                (slope_deg <= profile.off_trail_flat_threshold_deg) &
                ((friction_raw == 30) | (friction_raw == 40))
            )
            del slope_deg
            # Recalculate cost for these cells with flat field friction
            if np.any(flat_field_mask):
                base_time = avg_cell_size * 3.6 / linear_degrade_speed(
                    grade[flat_field_mask], profile.base_speed_kmh, max_grade_val
                )
                cost[flat_field_mask] = base_time * profile.off_trail_flat_friction
                del base_time
            del flat_field_mask

    # ─── Trail friction (mode-specific) ──────────────────────────────────────
    if trails is not None:
        if trails.shape != elevation.shape:
            raise ValueError(f"Trails shape mismatch")

        for trail_value, trail_friction in profile.trail_friction.items():
            if trail_friction is None:
                # Impassable for this mode
                np.putmask(cost, trails == trail_value, np.inf)
            else:
                # Trail friction REPLACES terrain friction
                # Recalculate cost = base_time * trail_friction
                trail_mask = trails == trail_value
                if np.any(trail_mask):
                    # Get base travel time (without friction)
                    if profile.speed_function == "tobler":
                        trail_speed = tobler_off_path_speed(grade[trail_mask], profile.base_speed_kmh)
                    elif profile.speed_function == "herzog":
                        trail_speed = herzog_wheeled_speed(grade[trail_mask], profile.base_speed_kmh)
                    else:
                        trail_speed = linear_degrade_speed(
                            grade[trail_mask], profile.base_speed_kmh, max_grade_val
                        )
                    cost[trail_mask] = (avg_cell_size * 3.6 / trail_speed) * trail_friction
                    del trail_speed
                del trail_mask

    # ─── Wilderness areas (mode-specific) ────────────────────────────────────
    if wilderness is not None and profile.wilderness_impassable:
        if wilderness.shape != elevation.shape:
            raise ValueError(f"Wilderness shape mismatch")
        np.putmask(cost, wilderness == 255, np.inf)

    # ─── Barriers (private land) ─────────────────────────────────────────────
    if barriers is not None and boundary_mode != "emergency":
        if barriers.shape != elevation.shape:
            raise ValueError(f"Barriers shape mismatch")

        if boundary_mode == "strict":
            np.putmask(cost, barriers == 255, np.inf)
        elif boundary_mode == "pragmatic":
            barrier_mask = barriers == 255
            cost[barrier_mask] *= PRAGMATIC_BARRIER_MULTIPLIER
            del barrier_mask

    return cost


# ═══════════════════════════════════════════════════════════════════════════════
# LEGACY API (backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════════

def tobler_speed(grade: float) -> float:
    """Legacy single-value Tobler speed function."""
    return 0.6 * 6.0 * math.exp(-3.5 * abs(grade + 0.05))


# ═══════════════════════════════════════════════════════════════════════════════
# TESTING
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("OFFROUTE Multi-Mode Cost Function Tests")
    print("=" * 70)

    print("\n[1] Speed functions at various grades:")
    print(f"{'Grade':<10} {'Foot':<12} {'MTB':<12} {'ATV':<12} {'Vehicle':<12}")
    print("-" * 60)

    for grade_val in [-0.3, -0.1, 0.0, 0.1, 0.2, 0.3]:
        grade_arr = np.array([grade_val])
        foot = tobler_off_path_speed(grade_arr, 6.0)[0]
        mtb = herzog_wheeled_speed(grade_arr, 12.0)[0]
        atv = herzog_wheeled_speed(grade_arr, 25.0)[0]
        veh = linear_degrade_speed(grade_arr, 40.0, np.tan(np.radians(20)))[0]
        print(f"{grade_val:+.2f}      {foot:>6.2f} km/h   {mtb:>6.2f} km/h   {atv:>6.2f} km/h   {veh:>6.2f} km/h")

    print("\n[2] Mode profiles:")
    for name, profile in MODE_PROFILES.items():
        print(f"\n  {name.upper()}: {profile.description}")
        print(f"    Max slope: {profile.max_slope_deg}°")
        print(f"    Trail access: {profile.trail_friction}")
        print(f"    Wilderness blocked: {profile.wilderness_impassable}")

    print("\n[3] Cost grid test (flat terrain, forest):")
    elev = np.ones((10, 10), dtype=np.float32) * 1000
    friction = np.ones((10, 10), dtype=np.float32) * 2.0  # Forest friction
    friction_raw = np.ones((10, 10), dtype=np.uint8) * 10  # Tree cover class

    trails = np.zeros((10, 10), dtype=np.uint8)
    trails[5, :] = 5  # Road across middle

    for mode_name in ["foot", "mtb", "atv", "vehicle"]:
        cost = compute_cost_grid(
            elev, cell_size_m=30.0,
            friction=friction,
            friction_raw=friction_raw,
            trails=trails,
            mode=mode_name
        )
        off_trail_cost = cost[0, 0]
        road_cost = cost[5, 0]
        impassable = np.sum(np.isinf(cost))
        print(f"  {mode_name:8s}: off-trail={off_trail_cost:>8.1f}s, road={road_cost:>6.1f}s, impassable={impassable}")

    print("\n[4] Wilderness blocking test:")
    wilderness = np.zeros((10, 10), dtype=np.uint8)
    wilderness[3:7, 3:7] = 255

    for mode_name in ["foot", "mtb", "atv", "vehicle"]:
        cost = compute_cost_grid(
            elev, cell_size_m=30.0,
            wilderness=wilderness,
            mode=mode_name
        )
        wilderness_impassable = np.sum(np.isinf(cost[3:7, 3:7]))
        print(f"  {mode_name:8s}: wilderness cells impassable = {wilderness_impassable}/16")

    print("\nDone.")
