# Watershed Scripts

QGIS Processing Toolbox scripts for automated watershed delineation and stream network extraction, powered by [WhiteboxTools](https://www.whiteboxgeo.com/).

## Requirements

- **QGIS 3.x**
- **Internet connection** on first run (WhiteboxTools installs automatically — ~50 MB, once only)
- **OpenTopography API key** — free from [portal.opentopography.org](https://portal.opentopography.org/requestApiKey) — required only for SRTM / ALOS / NASADEM sources

> WhiteboxTools is installed automatically the first time any script runs. No manual setup needed.

---

## Scripts

### 1. `download_dem.py` — Download DEM

Downloads a Digital Elevation Model for any area of interest from global sources.

| Parameter | Required | Description |
|-----------|----------|-------------|
| DEM source | ✅ | Copernicus 30 m / 90 m (no key), SRTM 30 m / 90 m, ALOS World 3D 30 m, NASADEM 30 m |
| Area of interest | ✅ | Bounding box in any CRS — auto-converted to WGS84 |
| Buffer around extent (km) | ✅ | Extra margin added around the area; recommended ≥ 2 km (default: 5 km) |
| Clip output to extent | ⬜ | Trim output to selected extent + buffer; uncheck to keep full tiles (default: on) |
| Target CRS | ⬜ | Output projection; leave blank for auto UTM zone |
| OpenTopography API key | ⬜ | Required only for SRTM / ALOS / NASADEM sources |
| Output DEM | ✅ | Output raster file |

**Notes:**
- Copernicus 30 m is the recommended default — high quality, no API key required.
- The downloaded DEM is reprojected to the target CRS and clipped.

---

### 2. `create_streams.py` — Create Stream Network

Extracts a stream network from a DEM using WhiteboxTools. Optimised for any DEM size — from a single watershed to a full country. Outputs a vector line layer styled by Strahler order.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Input DEM | ✅ | Any projected raster DEM |
| Stream threshold (km²) | ✅ | Minimum upstream drainage area to initiate a stream (default: 0.5 km²) |
| Vectorisation tiles | ⬜ | Number of parallel tiles for vectorisation; 0 = auto (1 per CPU core) |
| Skip Strahler order | ⬜ | Omit stream ordering for faster runs on very large DEMs (default: off) |
| Force Wang & Liu fill | ⬜ | Override the auto-selected depression-fill algorithm (default: off) |
| Force reprocess | ⬜ | Clear cached intermediate rasters and rerun from scratch (default: off) |
| Output stream network | ✅ | Output vector line layer |

**Pipeline:** pit fill → depression fill → D8 flow direction → D8 flow accumulation → stream extraction → Strahler order → parallel tile vectorisation

**Auto-optimisation:** fill algorithm (breach vs Wang & Liu), thread count, and compression are chosen automatically based on DEM size and available RAM.

**Output styling:** the stream layer is automatically styled by Strahler order (order 1 = thin/pale, order 7+ = thick/dark navy).

---

### 3. `delineate_catchments.py` — Delineate Catchments

Delineates catchment polygons for one or more pour points on an existing DEM. Supports both a click-on-map single point and a batch pour points layer.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Input DEM | ✅ | Projected raster DEM (e.g. output from Download DEM) |
| Pour points layer | ⬜ | Point vector layer for batch delineation |
| Single pour point | ⬜ | Click directly on the map canvas (alternative to layer input) |
| Snap distance (m) | ✅ | Snaps pour points to the nearest stream within this radius (default: 200 m) |
| Stream threshold (km²) | ✅ | Used if stream rasters need to be (re)built (default: 0.5 km²) |
| Force re-run stream pipeline | ⬜ | Ignore cached flow direction / accumulation rasters (default: off) |
| Output catchment polygons | ✅ | Output vector polygon layer |
| Output snapped outlets (QA) | ⬜ | Point layer showing where pour points snapped to the stream |

> Either **Pour points layer** or **Single pour point** must be provided (not both).

---

### 4. `auto_catchment.py` — Auto Catchment (One-Click)

All-in-one tool: click a pour point, optionally provide an existing DEM or download one automatically, and get a delineated catchment. If the catchment touches the edge of the downloaded area, the coverage is automatically expanded and delineation is re-run until the catchment is fully enclosed.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Pour point | ✅ | Click on the map canvas |
| Existing DEM | ⬜ | Use a DEM already loaded in QGIS; leave blank to auto-download |
| DEM source | ⬜ | Source for auto-download (default: Copernicus 30 m) |
| OpenTopography API key | ⬜ | Required only for SRTM / ALOS / NASADEM sources |
| Snap distance (m) | ⬜ | Snaps pour point to nearest stream (default: 200 m) |
| Stream threshold (km²) | ⬜ | Minimum drainage area to define a stream (default: 0.5 km²) |
| Target CRS for download | ⬜ | Output projection; leave blank for auto UTM |
| Max tiles / expansions | ⬜ | Safety cap on how many times the DEM can be expanded (default: 16) |
| Output catchment polygons | ✅ | Output vector polygon layer |
| Output snapped outlet | ⬜ | Point layer showing the snapped pour point location |
| Output downloaded DEM | ⬜ | Saves the downloaded DEM (download mode only) |

**Workflow:**
1. Click a pour point on the map.
2. If an existing DEM is provided, it is used directly. Otherwise a DEM is downloaded.
3. The catchment is delineated with WhiteboxTools.
4. If the catchment touches the DEM boundary, the coverage is expanded and delineation is re-run automatically.

---

## Typical Workflow

```
Download DEM  →  Create Streams  →  Delineate Catchments
```

Or use **Auto Catchment** to do all steps in a single click.

---

## Installation

1. Open QGIS → Processing Toolbox → Scripts → ⋮ → **Add Script from File**
2. Add each `.py` file from this folder.
3. Run any script — WhiteboxTools installs automatically on first use.
