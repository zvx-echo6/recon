"""
Friction layer reader for OFFROUTE.

Reads friction values from the WorldCover friction VRT and resamples
to match the elevation grid dimensions.
"""
import numpy as np
from pathlib import Path
from typing import Tuple, Optional

try:
    import rasterio
    from rasterio.windows import from_bounds
    from rasterio.enums import Resampling
except ImportError:
    raise ImportError("rasterio is required for friction layer support")

# Default path to the friction VRT
DEFAULT_FRICTION_PATH = Path("/mnt/nav/worldcover/friction/friction_conus.vrt")


class FrictionReader:
    """Reader for WorldCover friction raster."""
    
    def __init__(self, friction_path: Path = DEFAULT_FRICTION_PATH):
        self.friction_path = friction_path
        self._dataset = None
    
    def _open(self):
        """Lazy open the dataset."""
        if self._dataset is None:
            self._dataset = rasterio.open(self.friction_path)
        return self._dataset
    
    def get_friction_grid(
        self,
        south: float,
        north: float,
        west: float,
        east: float,
        target_shape: Tuple[int, int]
    ) -> np.ndarray:
        """
        Get friction values for a bounding box, resampled to target shape.
        
        Args:
            south, north, west, east: Bounding box coordinates
            target_shape: (rows, cols) to resample to (matches elevation grid)
        
        Returns:
            np.ndarray of uint8 friction values, same shape as target_shape.
            Values: 10-40 = friction multiplier (divide by 10)
                    255 = impassable
                    0 = nodata (treat as impassable)
        """
        ds = self._open()
        
        # Create a window from the bounding box
        window = from_bounds(west, south, east, north, ds.transform)
        
        # Read with resampling to target shape
        # Use nearest neighbor for categorical data
        friction = ds.read(
            1,
            window=window,
            out_shape=target_shape,
            resampling=Resampling.nearest
        )
        
        return friction
    
    def sample_point(self, lat: float, lon: float) -> int:
        """Sample friction value at a single point."""
        ds = self._open()
        
        # Get pixel coordinates
        row, col = ds.index(lon, lat)
        
        # Check bounds
        if row < 0 or row >= ds.height or col < 0 or col >= ds.width:
            return 0  # Out of bounds = nodata
        
        # Read single pixel
        window = rasterio.windows.Window(col, row, 1, 1)
        value = ds.read(1, window=window)
        return int(value[0, 0])
    
    def close(self):
        """Close the dataset."""
        if self._dataset is not None:
            self._dataset.close()
            self._dataset = None


def friction_to_multiplier(friction: np.ndarray) -> np.ndarray:
    """
    Convert friction values to cost multipliers.
    
    Args:
        friction: uint8 array of friction values
        
    Returns:
        float32 array of multipliers.
        Values 10-40 become 1.0-4.0 (divide by 10).
        Values 0 or 255 become np.inf (impassable).
    """
    multiplier = friction.astype(np.float32) / 10.0
    
    # Mark impassable cells
    multiplier[friction == 0] = np.inf    # nodata
    multiplier[friction == 255] = np.inf  # water/impassable
    
    return multiplier


if __name__ == "__main__":
    print("Testing FrictionReader...")
    
    reader = FrictionReader()
    
    # Test point sampling - Murtaugh Lake (should be water = 255)
    lake_lat, lake_lon = 42.47, -114.15
    lake_friction = reader.sample_point(lake_lat, lake_lon)
    print(f"Murtaugh Lake ({lake_lat}, {lake_lon}): friction = {lake_friction}")
    print(f"  Expected: 255 (water/impassable)")
    
    # Test grid read for small bbox
    friction = reader.get_friction_grid(
        south=42.4, north=42.5, west=-114.2, east=-114.1,
        target_shape=(100, 100)
    )
    print(f"\nGrid test shape: {friction.shape}")
    print(f"Unique values: {np.unique(friction)}")
    print(f"Water cells (255): {np.sum(friction == 255)}")
    
    reader.close()
    print("\nFrictionReader test complete.")
