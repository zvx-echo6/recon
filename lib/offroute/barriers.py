"""
PAD-US barrier layer for OFFROUTE.

Provides access to the PAD-US land ownership raster for routing decisions.
Cells with value 255 represent closed/restricted areas (Pub_Access = XA).

Build function rasterizes PAD-US geodatabase to aligned GeoTIFF.
Runtime functions read the raster and resample to match elevation grids.
"""
import numpy as np
from pathlib import Path
from typing import Tuple, Optional
import subprocess
import tempfile
import os

try:
    import rasterio
    from rasterio.windows import from_bounds
    from rasterio.enums import Resampling
except ImportError:
    raise ImportError("rasterio is required for barriers layer support")

# Paths
DEFAULT_BARRIERS_PATH = Path("/mnt/nav/worldcover/padus_barriers.tif")
PADUS_GDB_PATH = Path("/mnt/nav/padus/PADUS4_0_Geodatabase.gdb")
PADUS_LAYER = "PADUS4_0Combined_Proclamation_Marine_Fee_Designation_Easement"

# CONUS bounding box in WGS84
CONUS_BOUNDS = {
    "west": -125.0,
    "east": -66.0,
    "south": 24.0,
    "north": 50.0,
}

# Resolution in degrees (~30m at mid-latitudes)
PIXEL_SIZE = 0.0003  # ~33m


class BarrierReader:
    """Reader for PAD-US barrier raster."""

    def __init__(self, barrier_path: Path = DEFAULT_BARRIERS_PATH):
        self.barrier_path = barrier_path
        self._dataset = None

    def _open(self):
        """Lazy open the dataset."""
        if self._dataset is None:
            if not self.barrier_path.exists():
                raise FileNotFoundError(
                    f"Barrier raster not found at {self.barrier_path}. "
                    f"Run build_barriers_raster() first."
                )
            self._dataset = rasterio.open(self.barrier_path)
        return self._dataset

    def get_barrier_grid(
        self,
        south: float,
        north: float,
        west: float,
        east: float,
        target_shape: Tuple[int, int]
    ) -> np.ndarray:
        """
        Get barrier values for a bounding box, resampled to target shape.

        Args:
            south, north, west, east: Bounding box coordinates (WGS84)
            target_shape: (rows, cols) to resample to (matches elevation grid)

        Returns:
            np.ndarray of uint8 barrier values:
                255 = closed/restricted (impassable when respect_boundaries=True)
                0 = public/accessible
        """
        ds = self._open()

        # Create a window from the bounding box
        window = from_bounds(west, south, east, north, ds.transform)

        # Read with resampling to target shape
        barriers = ds.read(
            1,
            window=window,
            out_shape=target_shape,
            resampling=Resampling.nearest
        )

        return barriers

    def sample_point(self, lat: float, lon: float) -> int:
        """Sample barrier value at a single point."""
        ds = self._open()

        # Get pixel coordinates
        row, col = ds.index(lon, lat)

        # Check bounds
        if row < 0 or row >= ds.height or col < 0 or col >= ds.width:
            return 0  # Out of bounds = accessible

        # Read single pixel
        window = rasterio.windows.Window(col, row, 1, 1)
        value = ds.read(1, window=window)
        return int(value[0, 0])

    def close(self):
        """Close the dataset."""
        if self._dataset is not None:
            self._dataset.close()
            self._dataset = None


def build_barriers_raster(
    output_path: Path = DEFAULT_BARRIERS_PATH,
    gdb_path: Path = PADUS_GDB_PATH,
    pixel_size: float = PIXEL_SIZE,
    bounds: dict = CONUS_BOUNDS,
) -> Path:
    """
    Build the PAD-US barriers raster from the source geodatabase.

    Extracts polygons where Pub_Access = 'XA' (Closed) and rasterizes them.

    Args:
        output_path: Output GeoTIFF path
        gdb_path: Path to PAD-US geodatabase
        pixel_size: Pixel size in degrees
        bounds: CONUS bounding box

    Returns:
        Path to the created raster
    """
    import shutil

    if not gdb_path.exists():
        raise FileNotFoundError(f"PAD-US geodatabase not found at {gdb_path}")

    # Check for required tools
    if not shutil.which('ogr2ogr'):
        raise RuntimeError("ogr2ogr not found. Install GDAL.")
    if not shutil.which('gdal_rasterize'):
        raise RuntimeError("gdal_rasterize not found. Install GDAL.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Building PAD-US barriers raster...")
    print(f"  Source: {gdb_path}")
    print(f"  Output: {output_path}")
    print(f"  Pixel size: {pixel_size} degrees (~{pixel_size * 111000:.0f}m)")
    print(f"  Bounds: {bounds}")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 1: Extract closed areas and reproject to WGS84
        closed_gpkg = Path(tmpdir) / "closed_areas.gpkg"

        print(f"\n[1/3] Extracting closed areas (Pub_Access = 'XA')...")

        ogr_cmd = [
            "ogr2ogr",
            "-f", "GPKG",
            str(closed_gpkg),
            str(gdb_path),
            PADUS_LAYER,
            "-where", "Pub_Access = 'XA'",
            "-t_srs", "EPSG:4326",
            "-nlt", "MULTIPOLYGON",
            "-nln", "closed_areas",
        ]

        result = subprocess.run(ogr_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"STDERR: {result.stderr}")
            raise RuntimeError(f"ogr2ogr failed: {result.stderr}")

        # Check feature count
        info_cmd = ["ogrinfo", "-so", str(closed_gpkg), "closed_areas"]
        info_result = subprocess.run(info_cmd, capture_output=True, text=True)
        print(f"  Extraction result:\n{info_result.stdout}")

        # Step 2: Create empty raster
        print(f"\n[2/3] Creating raster grid...")

        width = int((bounds['east'] - bounds['west']) / pixel_size)
        height = int((bounds['north'] - bounds['south']) / pixel_size)

        print(f"  Grid size: {width} x {height} pixels")
        print(f"  Memory estimate: {width * height / 1e6:.1f} MB")

        # Step 3: Rasterize
        print(f"\n[3/3] Rasterizing closed areas...")

        rasterize_cmd = [
            "gdal_rasterize",
            "-burn", "255",
            "-init", "0",
            "-a_nodata", "0",  # No nodata - 0 means accessible
            "-te", str(bounds['west']), str(bounds['south']),
                   str(bounds['east']), str(bounds['north']),
            "-tr", str(pixel_size), str(pixel_size),
            "-ot", "Byte",
            "-co", "COMPRESS=LZW",
            "-co", "TILED=YES",
            "-l", "closed_areas",
            str(closed_gpkg),
            str(output_path),
        ]

        result = subprocess.run(rasterize_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"STDERR: {result.stderr}")
            raise RuntimeError(f"gdal_rasterize failed: {result.stderr}")

    # Verify output
    print(f"\n[Done] Verifying output...")
    with rasterio.open(output_path) as ds:
        print(f"  Size: {ds.width} x {ds.height}")
        print(f"  CRS: {ds.crs}")
        print(f"  Bounds: {ds.bounds}")

        # Sample a few tiles to check
        sample = ds.read(1, window=rasterio.windows.Window(0, 0, 1000, 1000))
        closed_count = np.sum(sample == 255)
        print(f"  Sample (1000x1000): {closed_count} closed cells")

    file_size = output_path.stat().st_size / (1024**2)
    print(f"  File size: {file_size:.1f} MB")

    return output_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "build":
        # Build the raster
        print("="*60)
        print("PAD-US Barriers Raster Build")
        print("="*60)
        build_barriers_raster()
    else:
        # Test the reader
        print("Testing BarrierReader...")

        if not DEFAULT_BARRIERS_PATH.exists():
            print(f"Barrier raster not found at {DEFAULT_BARRIERS_PATH}")
            print(f"Run: python barriers.py build")
            sys.exit(1)

        reader = BarrierReader()

        # Test grid read for Idaho area
        barriers = reader.get_barrier_grid(
            south=42.2, north=42.6, west=-114.8, east=-113.8,
            target_shape=(400, 1000)
        )
        print(f"\nGrid test shape: {barriers.shape}")
        print(f"Unique values: {np.unique(barriers)}")
        closed_cells = np.sum(barriers == 255)
        print(f"Closed cells: {closed_cells} ({100*closed_cells/barriers.size:.2f}%)")

        reader.close()
        print("\nBarrierReader test complete.")
