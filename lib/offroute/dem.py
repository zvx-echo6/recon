"""
DEM tile reader for OFFROUTE.

Reads elevation tiles from planet-dem.pmtiles (Terrarium-encoded WebP),
decodes them into numpy arrays, and provides a stitched elevation grid
for a given bounding box.
"""
import math
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Tuple, Optional

import numpy as np
from PIL import Image
from pmtiles.reader import MmapSource, Reader as PMTilesReader

# Default path to the planet DEM PMTiles file
DEFAULT_DEM_PATH = Path("/mnt/nas/nav/planet-dem.pmtiles")

# Tile size in pixels (z12 tiles are 512x512 in this tileset)
TILE_SIZE = 512

# Zoom level to use for elevation data
ZOOM_LEVEL = 12


def terrarium_decode(rgb_array: np.ndarray) -> np.ndarray:
    """
    Decode Terrarium-encoded RGB values to elevation in meters.
    
    Formula: elevation = (R * 256 + G + B/256) - 32768
    """
    r = rgb_array[:, :, 0].astype(np.float32)
    g = rgb_array[:, :, 1].astype(np.float32)
    b = rgb_array[:, :, 2].astype(np.float32)
    
    elevation = (r * 256.0 + g + b / 256.0) - 32768.0
    return elevation


def lat_lon_to_tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
    """Convert lat/lon to tile coordinates at given zoom level."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_to_lat_lon(x: int, y: int, zoom: int) -> Tuple[float, float, float, float]:
    """Convert tile coordinates to bounding box (north, south, west, east)."""
    n = 2 ** zoom
    lon_west = x / n * 360.0 - 180.0
    lon_east = (x + 1) / n * 360.0 - 180.0
    lat_north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lat_north, lat_south, lon_west, lon_east


class DEMReader:
    """Reader for Terrarium-encoded DEM tiles from PMTiles."""
    
    def __init__(self, pmtiles_path: Path = DEFAULT_DEM_PATH, tile_cache_size: int = 128):
        self.pmtiles_path = pmtiles_path
        self._source = MmapSource(open(pmtiles_path, "rb"))
        self._reader = PMTilesReader(self._source)
        self._header = self._reader.header()
        self._decode_tile = lru_cache(maxsize=tile_cache_size)(self._decode_tile_impl)
    
    def _decode_tile_impl(self, z: int, x: int, y: int) -> Optional[np.ndarray]:
        """Fetch and decode a single tile."""
        tile_data = self._reader.get(z, x, y)
        if tile_data is None:
            return None
        
        img = Image.open(BytesIO(tile_data))
        rgb_array = np.array(img)
        
        if rgb_array.shape[2] == 4:
            rgb_array = rgb_array[:, :, :3]
        
        elevation = terrarium_decode(rgb_array)
        return elevation
    
    def get_elevation_grid(
        self,
        south: float,
        north: float,
        west: float,
        east: float,
        zoom: int = ZOOM_LEVEL
    ) -> Tuple[np.ndarray, dict]:
        """Get a stitched elevation grid for the given bounding box."""
        x_min, y_max = lat_lon_to_tile(south, west, zoom)
        x_max, y_min = lat_lon_to_tile(north, east, zoom)
        
        n = 2 ** zoom
        x_min = max(0, x_min)
        x_max = min(n - 1, x_max)
        y_min = max(0, y_min)
        y_max = min(n - 1, y_max)
        
        n_tiles_x = x_max - x_min + 1
        n_tiles_y = y_max - y_min + 1
        out_height = n_tiles_y * TILE_SIZE
        out_width = n_tiles_x * TILE_SIZE
        
        elevation = np.full((out_height, out_width), np.nan, dtype=np.float32)
        
        for ty in range(y_min, y_max + 1):
            for tx in range(x_min, x_max + 1):
                tile_elev = self._decode_tile(zoom, tx, ty)
                if tile_elev is not None:
                    out_y = (ty - y_min) * TILE_SIZE
                    out_x = (tx - x_min) * TILE_SIZE
                    elevation[out_y:out_y + TILE_SIZE, out_x:out_x + TILE_SIZE] = tile_elev
        
        grid_north, _, grid_west, _ = tile_to_lat_lon(x_min, y_min, zoom)
        _, grid_south, _, grid_east = tile_to_lat_lon(x_max, y_max, zoom)
        
        pixel_size_lat = (grid_north - grid_south) / out_height
        pixel_size_lon = (grid_east - grid_west) / out_width
        
        origin_lat = grid_north - pixel_size_lat / 2
        origin_lon = grid_west + pixel_size_lon / 2
        
        center_lat = (south + north) / 2
        lat_m = 111320.0
        lon_m = 111320.0 * math.cos(math.radians(center_lat))
        cell_size_lat_m = abs(pixel_size_lat) * lat_m
        cell_size_lon_m = abs(pixel_size_lon) * lon_m
        cell_size_m = (cell_size_lat_m + cell_size_lon_m) / 2
        
        row_start = int((grid_north - north) / abs(pixel_size_lat))
        row_end = int((grid_north - south) / abs(pixel_size_lat))
        col_start = int((west - grid_west) / pixel_size_lon)
        col_end = int((east - grid_west) / pixel_size_lon)
        
        row_start = max(0, row_start)
        row_end = min(out_height, row_end)
        col_start = max(0, col_start)
        col_end = min(out_width, col_end)
        
        elevation = elevation[row_start:row_end, col_start:col_end]
        
        origin_lat = grid_north - (row_start + 0.5) * abs(pixel_size_lat)
        origin_lon = grid_west + (col_start + 0.5) * pixel_size_lon
        
        metadata = {
            "bounds": (south, north, west, east),
            "pixel_size_lat": -abs(pixel_size_lat),
            "pixel_size_lon": pixel_size_lon,
            "origin_lat": origin_lat,
            "origin_lon": origin_lon,
            "cell_size_m": cell_size_m,
            "shape": elevation.shape,
        }
        
        return elevation, metadata
    
    def pixel_to_latlon(self, row: int, col: int, metadata: dict) -> Tuple[float, float]:
        """Convert pixel coordinates to lat/lon."""
        lat = metadata["origin_lat"] + row * metadata["pixel_size_lat"]
        lon = metadata["origin_lon"] + col * metadata["pixel_size_lon"]
        return lat, lon
    
    def latlon_to_pixel(self, lat: float, lon: float, metadata: dict) -> Tuple[int, int]:
        """Convert lat/lon to pixel coordinates."""
        row = int((metadata["origin_lat"] - lat) / abs(metadata["pixel_size_lat"]))
        col = int((lon - metadata["origin_lon"]) / metadata["pixel_size_lon"])
        return row, col
    
    def close(self):
        """Close the PMTiles file."""
        pass  # MmapSource handles cleanup


if __name__ == "__main__":
    reader = DEMReader()
    elevation, meta = reader.get_elevation_grid(
        south=42.4, north=42.6, west=-114.5, east=-114.3
    )
    print(f"Elevation grid shape: {elevation.shape}")
    print(f"Cell size: {meta['cell_size_m']:.1f} m")
    print(f"Elevation range: {np.nanmin(elevation):.1f} - {np.nanmax(elevation):.1f} m")
    center_row, center_col = elevation.shape[0] // 2, elevation.shape[1] // 2
    lat, lon = reader.pixel_to_latlon(center_row, center_col, meta)
    print(f"Center pixel lat/lon: {lat:.4f}, {lon:.4f}")
    reader.close()
