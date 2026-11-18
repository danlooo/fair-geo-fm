"""openEO UDF: TESSERA v1 frozen annual pixel embeddings.

TESSERA v1 is distributed as a frozen product, not as a checkpoint to run:
global, annual, 10 m, 128-band int8 embeddings in 0.1 degree tiles on
`https://data.source.coop/tessera/tessera`. This UDF downloads the tiles that
intersect the requested area and samples them onto the UTM grid of the input
cube. It deliberately does not depend on `geotessera` or `rs-embed`, whose
current releases require Python >= 3.12; the UDF only uses packages that the
CDSE UDF sandbox already provides (numpy, xarray, pyproj, rasterio).

Tile layout (write access is neither needed nor possible):

    https://data.source.coop/tessera/tessera/npy/v1/<year>/grid_<lon>_<lat>/grid_<lon>_<lat>.npy
    https://data.source.coop/tessera/tessera/npy/v1/<year>/grid_<lon>_<lat>/grid_<lon>_<lat>_scales.npy
    https://data.source.coop/tessera/tessera/landmasks/v1/grid_<lon>_<lat>.tiff

The `.npy` is (H, W, 128) int8 and the scales are (H, W) float32. The landmask
GeoTIFF carries the tile's UTM CRS and affine transform.

Caching: a module-level cache survives across `apply_datacube` calls in one
executor process because openEO lru-caches the exec'd UDF module (see
`openeo.udf.run_code.load_module_from_string`). Each executor still downloads
a tile at most once; the cache is per process and per job, not shared between
executors. Do not cache the dequantized tile in memory: one tile is about
450 MB.

The input cube is only a grid/trigger. The graph loads a static DEM and
resamples it to the requested UTM grid; the DEM values are ignored.
"""
import functools
import logging
import math
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from openeo.udf import XarrayDataCube
from pyproj import CRS, Transformer

_log = logging.getLogger(__name__)

_BASE_URL = "https://data.source.coop/tessera/tessera"
_VERSION = "v1"
_DIMS = 128
_UA = "fair-geo-fm-tessera-v1/1"


def _cache_root() -> Path:
    """Writable per-process tile cache.

    The openEO job work directory may be FUSE/S3-backed and is a poor place
    for many small files, so default to the local temporary directory.
    """
    root = Path(
        os.environ.get("TESSERA_V1_CACHE")
        or Path(tempfile.gettempdir()) / f"tessera_v1_{os.getpid()}"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _download(url: str, destination: Path) -> bool:
    """Download *url* to *destination*, returning False for a 404 tile."""
    if destination.is_file() and destination.stat().st_size > 0:
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Per-process suffix so two executors sharing TESSERA_V1_CACHE cannot
    # clobber each other's partial download.
    partial = destination.with_name(f"{destination.name}.{os.getpid()}.part")
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                with partial.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
            os.replace(partial, destination)
            return True
        except urllib.error.HTTPError as error:
            partial.unlink(missing_ok=True)
            if error.code == 404:
                return False
            last_error = error
        except Exception as error:  # noqa: BLE001 - retry on any network error
            partial.unlink(missing_ok=True)
            last_error = error
        time.sleep(2**attempt)
    raise RuntimeError(f"Failed to download {url}: {last_error}")


def _landmask_data(path: str) -> tuple[str, object, np.ndarray]:
    """Return the CRS WKT, affine transform and valid-pixel mask of a tile.

    The landmask is a Byte GeoTIFF with 0 for no data (water or gaps). It is
    small, so it is read together with the metadata rather than cached.
    """
    with rasterio.open(path) as source:
        return source.crs.to_wkt(), source.transform, source.read(1) != 0


@functools.lru_cache(maxsize=32)
def _transformer(source_wkt: str, destination_wkt: str) -> Transformer:
    return Transformer.from_crs(source_wkt, destination_wkt, always_xy=True)


@functools.lru_cache(maxsize=8)
def _crs_wkt(epsg: int) -> str:
    return CRS.from_user_input(int(epsg)).to_wkt()


def _grid_name(lon: float, lat: float) -> str:
    return f"grid_{lon:.2f}_{lat:.2f}"


def _tile_centers(bounds: tuple[float, float, float, float]):
    """Yield the 0.1 degree tile centres intersecting a WGS84 bbox."""
    min_lon, min_lat, max_lon, max_lat = bounds
    epsilon = 1e-9
    for i in range(math.floor(min_lon * 10), math.floor(max_lon * 10 - epsilon) + 1):
        lon = i / 10.0 + 0.05
        if not -180.0 < lon <= 180.0:
            continue
        for j in range(math.floor(min_lat * 10), math.floor(max_lat * 10 - epsilon) + 1):
            lat = j / 10.0 + 0.05
            if not -90.0 < lat <= 90.0:
                continue
            yield lon, lat


def _lonlat_bounds(
    xs: np.ndarray, ys: np.ndarray, target_crs_wkt: str
) -> tuple[float, float, float, float]:
    transformer = _transformer(target_crs_wkt, "EPSG:4326")
    xmin, xmax = float(np.min(xs)), float(np.max(xs))
    ymin, ymax = float(np.min(ys)), float(np.max(ys))
    lons, lats = transformer.transform(
        [xmin, xmin, xmax, xmax], [ymin, ymax, ymin, ymax]
    )
    return float(np.min(lons)), float(np.min(lats)), float(np.max(lons)), float(np.max(lats))


def _load_tile(
    lon: float, lat: float, year: int
) -> tuple[np.ndarray, str, object, np.ndarray] | None:
    """Fetch and dequantize one tile, or None when the tile does not exist."""
    grid = _grid_name(lon, lat)
    root = _cache_root()
    year_dir = root / "npy" / _VERSION / str(year) / grid
    embedding_path = year_dir / f"{grid}.npy"
    scales_path = year_dir / f"{grid}_scales.npy"
    landmask_path = root / "landmasks" / _VERSION / f"{grid}.tiff"

    if not _download(
        f"{_BASE_URL}/npy/{_VERSION}/{year}/{grid}/{grid}.npy", embedding_path
    ):
        return None
    if not _download(
        f"{_BASE_URL}/npy/{_VERSION}/{year}/{grid}/{grid}_scales.npy", scales_path
    ):
        return None
    if not _download(
        f"{_BASE_URL}/landmasks/{_VERSION}/{grid}.tiff", landmask_path
    ):
        return None

    quantized = np.load(embedding_path)
    scales = np.load(scales_path)
    if quantized.shape[:2] != scales.shape[:2]:
        raise ValueError(
            f"Tile {grid} mismatch: embedding {quantized.shape}, "
            f"scales {scales.shape}"
        )
    embedding = np.asarray(quantized, dtype=np.float32)
    if scales.ndim == 2:
        embedding *= scales[..., None]
    elif scales.ndim == 3:
        embedding *= scales
    else:
        raise ValueError(f"Unexpected scales shape for {grid}: {scales.shape}")

    crs_wkt, transform, valid_mask = _landmask_data(str(landmask_path))
    return embedding, crs_wkt, transform, valid_mask


def _sample_into(
    canvas: np.ndarray,
    filled: np.ndarray,
    tile: np.ndarray,
    source_crs_wkt: str,
    transform,
    valid_mask: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    target_crs_wkt: str,
) -> None:
    """Nearest-neighbour sample one tile onto the target grid."""
    if tile.ndim != 3:
        raise ValueError(f"Unexpected tile shape: {tile.shape}")
    if tile.shape[-1] == _DIMS:
        height, width = tile.shape[:2]
        hwc = True
    elif tile.shape[0] == _DIMS:
        height, width = tile.shape[1:]
        hwc = False
    else:
        raise ValueError(f"Unexpected tile shape: {tile.shape}")

    transformer = _transformer(target_crs_wkt, source_crs_wkt)
    grid_x, grid_y = np.meshgrid(xs, ys)
    tx, ty = transformer.transform(grid_x, grid_y)

    a, b, c = transform.a, transform.b, transform.c
    d, e, f = transform.d, transform.e, transform.f
    determinant = a * e - b * d
    if determinant == 0:
        raise ValueError("Degenerate tile transform")
    column = (e * (tx - c) - b * (ty - f)) / determinant
    row = (-d * (tx - c) + a * (ty - f)) / determinant
    cols = np.floor(column).astype(np.int64)
    rows = np.floor(row).astype(np.int64)

    in_bounds = (
        (cols >= 0)
        & (cols < width)
        & (rows >= 0)
        & (rows < height)
        & ~filled
    )
    if not in_bounds.any():
        return

    row_index, col_index = np.nonzero(in_bounds)
    source_rows = rows[row_index, col_index]
    source_cols = cols[row_index, col_index]
    valid = valid_mask[source_rows, source_cols]
    if not valid.any():
        return
    row_index = row_index[valid]
    col_index = col_index[valid]
    source_rows = source_rows[valid]
    source_cols = source_cols[valid]

    if hwc:
        canvas[row_index, col_index] = tile[source_rows, source_cols, :]
    else:
        canvas[row_index, col_index] = tile[:, source_rows, source_cols].T
    filled[row_index, col_index] = True


def _year(context: dict) -> int:
    temporal_extent = (context or {}).get("temporal_extent")
    if not temporal_extent:
        raise ValueError("UDF context requires 'temporal_extent'")
    return int(str(temporal_extent[0])[:4])


def _target_crs(context: dict) -> int:
    projection = (context or {}).get("target_crs")
    if projection is None:
        raise ValueError("UDF context requires 'target_crs'")
    epsg = int(projection)
    # UTM north (326xx) and south (327xx) only: the product is stored in UTM
    # zones and a 10 m resolution in a geographic CRS would be meaningless.
    if not 32601 <= epsg <= 32760:
        raise ValueError(f"target_crs must be a UTM EPSG code, got {epsg}")
    return epsg


def apply_metadata(metadata, context: dict):
    """Replace the trigger band with the 128 TESSERA bands and update the year."""
    year = _year(context)
    band_names = [f"tessera_v1_{index}" for index in range(_DIMS)]
    updated = metadata.rename_labels(dimension="bands", target=band_names)
    try:
        from openeo.metadata import TemporalDimension

        dimensions = []
        for dimension in updated._dimensions:
            if getattr(dimension, "type", None) == "temporal":
                dimensions.append(
                    TemporalDimension(
                        name=dimension.name,
                        extent=[
                            f"{year:04d}-01-01T00:00:00Z",
                            f"{year + 1:04d}-01-01T00:00:00Z",
                        ],
                    )
                )
            else:
                dimensions.append(dimension)
        updated = updated._clone_and_update(dimensions=dimensions)
    except Exception:  # noqa: BLE001 - metadata cosmetics must not fail the job
        _log.debug("Could not update the temporal metadata", exc_info=True)
    return updated


def apply_datacube(cube: XarrayDataCube, context: dict) -> XarrayDataCube:
    """Sample frozen TESSERA v1 tiles onto the input cube's UTM grid."""
    year = _year(context)
    target_epsg = _target_crs(context)
    target_crs_wkt = _crs_wkt(target_epsg)

    array = cube.get_array()
    xs = np.asarray(array.coords["x"].values, dtype=np.float64)
    ys = np.asarray(array.coords["y"].values, dtype=np.float64)
    if xs.size == 0 or ys.size == 0:
        raise ValueError("Input cube has no spatial extent")

    canvas = np.full((ys.size, xs.size, _DIMS), np.nan, dtype=np.float32)
    filled = np.zeros((ys.size, xs.size), dtype=bool)

    bounds = _lonlat_bounds(xs, ys, target_crs_wkt)
    tiles = 0
    for lon, lat in _tile_centers(bounds):
        tile = _load_tile(lon, lat, year)
        if tile is None:
            continue
        tiles += 1
        embedding, source_crs_wkt, transform, valid_mask = tile
        _sample_into(
            canvas,
            filled,
            embedding,
            source_crs_wkt,
            transform,
            valid_mask,
            xs,
            ys,
            target_crs_wkt,
        )
        del embedding

    if tiles == 0:
        _log.warning(
            "No TESSERA v1 tiles for year %s in bbox %s; returning no-data",
            year,
            bounds,
        )
    elif not filled.any():
        _log.warning(
            "TESSERA v1 tiles for year %s do not cover the target grid %s",
            year,
            target_epsg,
        )

    data = np.moveaxis(canvas, -1, 0)
    result = xr.DataArray(
        data[None, ...],
        dims=("t", "bands", "y", "x"),
        coords={
            "t": np.array([np.datetime64(f"{year:04d}-01-01")], dtype="datetime64[ns]"),
            "bands": [f"tessera_v1_{index}" for index in range(_DIMS)],
            "y": ys,
            "x": xs,
        },
    )
    return XarrayDataCube(result)
