"""
Trail corridor reader for OFFROUTE.

Provides access to the OSM-derived trail raster for pathfinding.
Trail values replace WorldCover friction where trails exist.

Raster values:
    0  = no trail (use WorldCover friction)
    5  = road (0.1× friction)
    15 = track (0.3× friction)
    25 = foot trail (0.5× friction)
"""
import numpy as np
from pathlib import Path
from typing import Tuple, Optional

try:
    import rasterio
    from rasterio.windows import from_bounds
    from rasterio.enums import Resampling
except ImportError:
    raise ImportError("rasterio is required for trails layer support")

# Default path to the trails raster
DEFAULT_TRAILS_PATH = Path("/mnt/nav/worldcover/trails.tif")

# Trail value to friction multiplier mapping
TRAIL_FRICTION_MAP = {
    5: 0.1,   # road
    15: 0.3,  # track
    25: 0.5,  # foot trail
}


class TrailReader:
    """Reader for OSM-derived trail corridor raster."""

    def __init__(self, trails_path: Path = DEFAULT_TRAILS_PATH):
        self.trails_path = trails_path
        self._dataset = None

    def _open(self):
        """Lazy open the dataset."""
        if self._dataset is None:
            if not self.trails_path.exists():
                raise FileNotFoundError(
                    f"Trails raster not found at {self.trails_path}. "
                    f"Run the Phase B rasterization script first."
                )
            self._dataset = rasterio.open(self.trails_path)
        return self._dataset

    def get_trails_grid(
        self,
        south: float,
        north: float,
        west: float,
        east: float,
        target_shape: Tuple[int, int]
    ) -> np.ndarray:
        """
        Get trail values for a bounding box, resampled to target shape.

        Args:
            south, north, west, east: Bounding box coordinates (WGS84)
            target_shape: (rows, cols) to resample to (matches elevation grid)

        Returns:
            np.ndarray of uint8 trail values:
                0  = no trail
                5  = road (0.1× friction)
                15 = track (0.3× friction)
                25 = foot trail (0.5× friction)
        """
        ds = self._open()

        # Create a window from the bounding box
        window = from_bounds(west, south, east, north, ds.transform)

        # Read with resampling to target shape
        # Use nearest neighbor to preserve discrete values
        trails = ds.read(
            1,
            window=window,
            out_shape=target_shape,
            resampling=Resampling.nearest
        )

        return trails

    def sample_point(self, lat: float, lon: float) -> int:
        """Sample trail value at a single point."""
        ds = self._open()

        # Get pixel coordinates
        row, col = ds.index(lon, lat)

        # Check bounds
        if row < 0 or row >= ds.height or col < 0 or col >= ds.width:
            return 0  # Out of bounds = no trail

        # Read single pixel
        window = rasterio.windows.Window(col, row, 1, 1)
        value = ds.read(1, window=window)
        return int(value[0, 0])

    def close(self):
        """Close the dataset."""
        if self._dataset is not None:
            self._dataset.close()
            self._dataset = None


def trails_to_friction(trails: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Convert trail values to friction multipliers.

    Args:
        trails: uint8 array of trail values (0, 5, 15, or 25)

    Returns:
        Tuple of:
        - friction: float32 array of friction multipliers
        - has_trail: bool array indicating where trails exist
    """
    friction = np.ones_like(trails, dtype=np.float32)
    has_trail = trails > 0

    # Apply friction values where trails exist
    friction[trails == 5] = 0.1   # road
    friction[trails == 15] = 0.3  # track
    friction[trails == 25] = 0.5  # foot trail

    return friction, has_trail


if __name__ == "__main__":
    print("Testing TrailReader...")

    if not DEFAULT_TRAILS_PATH.exists():
        print(f"Trails raster not found at {DEFAULT_TRAILS_PATH}")
        print("Run Phase B rasterization first.")
        exit(1)

    reader = TrailReader()

    # Test point sampling - Twin Falls downtown (should have roads)
    test_lat, test_lon = 42.563, -114.461
    trail_value = reader.sample_point(test_lat, test_lon)
    print(f"\nTwin Falls ({test_lat}, {test_lon}): trail value = {trail_value}")
    label = {0: "no trail", 5: "road", 15: "track", 25: "trail"}.get(trail_value, "unknown")
    print(f"  Type: {label}")

    # Test grid read for test bbox
    trails = reader.get_trails_grid(
        south=42.21, north=42.60, west=-114.76, east=-113.79,
        target_shape=(400, 1000)
    )
    print(f"\nGrid test shape: {trails.shape}")

    unique, counts = np.unique(trails, return_counts=True)
    print("Value distribution:")
    for v, c in zip(unique, counts):
        pct = 100 * c / trails.size
        label = {0: "no trail", 5: "road", 15: "track", 25: "trail"}.get(v, f"unknown({v})")
        print(f"  {label}: {c:,} pixels ({pct:.2f}%)")

    # Test conversion to friction
    friction, has_trail = trails_to_friction(trails)
    print(f"\nTrail coverage: {100 * np.sum(has_trail) / trails.size:.2f}%")
    print(f"Friction range (on trails): {friction[has_trail].min():.1f} - {friction[has_trail].max():.1f}")

    reader.close()
    print("\nTrailReader test complete.")
