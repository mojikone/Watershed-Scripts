"""
Auto Catchment — one-click click-to-watershed
QGIS Processing Toolbox Script

Load in QGIS:
  Processing Toolbox → Scripts → ⋮ → Add Script from File → pick this file

Workflow (single click):
  1. You click one pour point on the map canvas.
  2. If you select an existing DEM, it is used as-is.
     If you leave the DEM blank, a DEM is downloaded from the selected source:
       • Copernicus 30 m / 90 m — no key required
       • SRTM 30 m / 90 m, ALOS World 3D 30 m, NASADEM 30 m — free
         OpenTopography API key required (portal.opentopography.org/requestApiKey)
  3. The catchment is delineated with WhiteboxTools.
  4. (download mode only) If the catchment touches the edge of the downloaded
     coverage, adjacent area is added and delineation is re-run — repeating
     until the catchment is fully enclosed.

This tool does NOT reinvent anything: the download logic mirrors `download_dem.py`
and the WhiteboxTools pipeline mirrors `delineate_catchments.py`.
"""

import os, sys, math, time, glob, csv, concurrent.futures
from qgis.core import (
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingAlgorithm,
    QgsProcessingParameterPoint,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterCrs,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterVectorDestination,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRectangle,
    QgsPointXY,
    QgsProject,
    QgsFeatureSink,
    QgsWkbTypes,
    QgsProcessing,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication


def _get_wbt_site():
    """Return a user-writable site-packages dir inside the QGIS profile.
    This path survives QGIS reinstalls and works on any machine / user account."""
    try:
        from qgis.core import QgsApplication
        d = os.path.join(QgsApplication.qgisSettingsDirPath(),
                         "python", "site-packages")
    except Exception:
        import site as _site
        try:
            d = _site.getusersitepackages()
        except Exception:
            d = os.path.join(os.path.expanduser("~"), ".qgis-python-packages")
    os.makedirs(d, exist_ok=True)
    return d


WBT_SITE = _get_wbt_site()


def _pip_install(package, feedback=None):
    """Install *package* into WBT_SITE by calling pip in-process.
    Avoids subprocess entirely — subprocess.check_call crashes in QGIS worker
    threads because WaitForSingleObject is not safe from those threads."""
    import importlib
    try:
        from pip._internal.cli.main import main as _pip_main
    except ImportError:
        raise QgsProcessingException(
            f"Cannot auto-install '{package}': pip is not available inside QGIS.\n"
            f"Please install it manually from the OSGeo4W Shell:\n"
            f"  pip install {package} --target \"{WBT_SITE}\"")
    if feedback:
        feedback.pushInfo(f"  pip install {package} → {WBT_SITE}")
    ret = _pip_main(["install", package,
                     "--target", WBT_SITE,
                     "--quiet",
                     "--no-warn-script-location"])
    importlib.invalidate_caches()
    if ret not in (0, None):
        raise QgsProcessingException(
            f"pip install {package} failed (exit {ret}).\n"
            f"Try manually from the OSGeo4W Shell:\n"
            f"  pip install {package} --target \"{WBT_SITE}\"")


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers (ported verbatim in spirit from download_dem.py / delineate_*.py)
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_requests(feedback=None):
    if WBT_SITE not in sys.path:
        sys.path.insert(0, WBT_SITE)
    try:
        import requests
        return requests
    except ImportError:
        _pip_install("requests", feedback)
        import requests
        return requests


def _download_copernicus_tiles(cells, temp_dir, feedback, resolution=30):
    """cells: iterable of (lat, lon) integer 1°×1° tile origins. Copernicus 30 or 90 m.
    Returns list of local .tif paths (cached tiles reused, missing tiles skipped)."""
    requests = _ensure_requests(feedback)
    if resolution == 90:
        bucket, code = "copernicus-dem-90m", "COG_30"
    else:
        bucket, code = "copernicus-dem-30m", "COG_10"

    jobs = []
    for (lat, lon) in sorted(set(cells)):
        ns  = "N" if lat >= 0 else "S"
        ew  = "E" if lon >= 0 else "W"
        tag = f"Copernicus_DSM_{code}_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"
        url = f"https://{bucket}.s3.amazonaws.com/{tag}/{tag}.tif"
        out = os.path.join(temp_dir, f"{tag}.tif")
        jobs.append((url, out))

    feedback.pushInfo(f"Downloading {len(jobs)} Copernicus {resolution} m tile(s) ...")

    def _fetch(job):
        url, out = job
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        try:
            r = requests.get(url, stream=True, timeout=180)
            if r.status_code == 200:
                with open(out, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                return out
            feedback.pushInfo(f"  Tile missing (HTTP {r.status_code}): {url.split('/')[-1]}")
            return None
        except Exception as e:
            feedback.pushInfo(f"  Download error: {e}")
            return None

    downloaded = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
        futures = {ex.submit(_fetch, j): j for j in jobs}
        for fut in concurrent.futures.as_completed(futures):
            if feedback.isCanceled():
                break
            res = fut.result()
            if res:
                downloaded.append(res)
                feedback.pushInfo(f"  ✓ {os.path.basename(res)}"
                                  f"  ({os.path.getsize(res)/1e6:.1f} MB)")
    return downloaded


def _mosaic_warp(tiles, target_authid, out_path, feedback):
    """Build VRT from WGS84 tiles and warp (full tiles, no clip) to target CRS.
    Output is striped / predictor-free so WhiteboxTools can read it."""
    from osgeo import gdal
    gdal.UseExceptions()
    if not tiles:
        raise QgsProcessingException("No DEM tiles available to mosaic.")
    feedback.pushInfo(f"Mosaicking {len(tiles)} tile(s) → warp to {target_authid} ...")
    vrt = gdal.BuildVRT("/vsimem/auto_mosaic.vrt", tiles)
    if vrt is None:
        raise QgsProcessingException("Failed to build VRT from downloaded tiles.")
    warp_opts = gdal.WarpOptions(
        srcSRS="EPSG:4326",
        dstSRS=target_authid,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Float32,
        srcNodata=-9999,
        dstNodata=-9999,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=IF_SAFER"],
        multithread=True,
        warpOptions=["NUM_THREADS=ALL_CPUS"],
    )
    ds = gdal.Warp(out_path, vrt, options=warp_opts)
    if ds is None:
        raise QgsProcessingException("gdal.Warp reprojection failed.")
    ds.FlushCache(); ds = None; vrt = None
    gdal.Unlink("/vsimem/auto_mosaic.vrt")
    return out_path


def _download_opentopography(west, south, east, north, demtype, api_key, out_path, feedback):
    """Download a single merged GeoTIFF from the OpenTopography Global DEM API.
    demtype: SRTMGL1 | SRTMGL3 | AW3D30 | NASADEM
    Overwrites out_path each call (no per-tile cache — bbox changes on expansion)."""
    requests = _ensure_requests(feedback)
    url = (
        "https://portal.opentopography.org/API/globaldem"
        f"?demtype={demtype}"
        f"&south={south}&north={north}&west={west}&east={east}"
        f"&outputFormat=GTiff&API_Key={api_key}"
    )
    feedback.pushInfo(f"Downloading {demtype} from OpenTopography "
                      f"(W{west:.2f} S{south:.2f} E{east:.2f} N{north:.2f}) ...")
    r = requests.get(url, stream=True, timeout=300)
    if r.status_code != 200:
        raise QgsProcessingException(
            f"OpenTopography returned HTTP {r.status_code}.\n"
            "Check your API key and that the extent is within the DEM coverage.\n"
            "Get a free key at: https://portal.opentopography.org/requestApiKey")
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    feedback.pushInfo(f"  Downloaded: {os.path.getsize(out_path)/1e6:.1f} MB")
    return out_path


def _init_wbt(feedback):
    import importlib as _il

    if WBT_SITE not in sys.path:
        sys.path.insert(0, WBT_SITE)

    def _wbt_ok():
        try:
            import whitebox
            f = getattr(whitebox, "__file__", None)
            return bool(f and os.path.exists(os.path.dirname(f)))
        except Exception:
            return False

    if not _wbt_ok():
        feedback.pushInfo("WhiteboxTools not found — installing (runs once) ...")
        import shutil as _sh, tempfile as _tf

        def _copy_into(src_dir, dst_dir):
            os.makedirs(dst_dir, exist_ok=True)
            for _item in os.listdir(src_dir):
                if _item == "__pycache__":
                    continue
                _s = os.path.join(src_dir, _item)
                _d = os.path.join(dst_dir, _item)
                if os.path.isdir(_s):
                    _copy_into(_s, _d)
                else:
                    try:
                        _sh.copy2(_s, _d)
                    except Exception:
                        pass

        for _n in os.listdir(WBT_SITE):
            if _n.startswith("_wbt_install_"):
                _sh.rmtree(os.path.join(WBT_SITE, _n), ignore_errors=True)

        _tmp = _tf.mkdtemp(prefix="_wbt_install_", dir=WBT_SITE)
        from pip._internal.cli.main import main as _pip_main
        _pip_main(["install", "whitebox", "--target", _tmp,
                   "--quiet", "--no-warn-script-location"])
        for _item in os.listdir(_tmp):
            _src = os.path.join(_tmp, _item)
            _dst = os.path.join(WBT_SITE, _item)
            if os.path.isdir(_src):
                _copy_into(_src, _dst)
            else:
                try:
                    _sh.copy2(_src, _dst)
                except Exception:
                    pass
        _sh.rmtree(_tmp, ignore_errors=True)

        _il.invalidate_caches()
        for _k in list(sys.modules.keys()):
            if "whitebox" in _k.lower():
                del sys.modules[_k]
        if not _wbt_ok():
            raise QgsProcessingException(
                "WhiteboxTools installation failed.\n"
                f"Try manually: pip install whitebox --target \"{WBT_SITE}\"")

    import whitebox
    _exe = os.path.join(os.path.dirname(whitebox.__file__), "WBT", "whitebox_tools.exe")
    if not os.path.exists(_exe):
        feedback.pushInfo("Downloading WhiteboxTools binary (runs once, ~50 MB) ...")
        try:
            whitebox.download_wbt()
        except Exception:
            pass
        if not os.path.exists(_exe):
            raise QgsProcessingException(
                "WhiteboxTools binary could not be downloaded.\n"
                "Check your internet connection and try again.")

    if sys.platform == "win32":
        import subprocess as _sp
        _orig = _sp.Popen.__init__
        def _no_window(self_popen, args, **kwargs):
            si = kwargs.pop("startupinfo", None) or _sp.STARTUPINFO()
            si.dwFlags |= _sp.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000
            _orig(self_popen, args, **kwargs)
        _sp.Popen.__init__ = _no_window

    import io as _io
    if sys.stdout is None:
        sys.stdout = _io.StringIO()

    wbt = whitebox.WhiteboxTools()
    cpu = os.cpu_count() or 4
    wbt.set_max_procs(max(1, cpu - 2) if cpu > 4 else cpu)
    wbt.set_verbose_mode(False)
    return wbt


def _delineate(wbt, dem_path, coords, snap_dist, threshold_km2, epsg, temp_dir, feedback):
    """Full WhiteboxTools watershed pipeline (mirrors delineate_catchments.py).
    Returns (watershed_vector_shp, snapped_outlets_shp). Caches by DEM mtime."""
    from osgeo import gdal
    gdal.UseExceptions()

    src = gdal.Open(dem_path)
    gt = src.GetGeoTransform()
    cell_x, cell_y = abs(gt[1]), abs(gt[5])
    src = None
    threshold_cells = max(1, int(threshold_km2 * 1e6 / (cell_x * cell_y)))

    # WBT-safe copy (striped, LZW, no predictor)
    dem_wbt       = os.path.join(temp_dir, "dem_wbt.tif")
    dem_wbt_stamp = os.path.join(temp_dir, "dem_wbt_source.txt")
    src_sig = f"{dem_path}|{os.path.getmtime(dem_path):.0f}"
    cached  = open(dem_wbt_stamp).read().strip() if os.path.exists(dem_wbt_stamp) else ""
    if not os.path.exists(dem_wbt) or cached != src_sig:
        feedback.pushInfo("Converting DEM to WBT-compatible format (striped, LZW) ...")
        _ds = gdal.Translate(dem_wbt, dem_path,
                             creationOptions=["COMPRESS=LZW", "BIGTIFF=IF_SAFER"])
        if _ds is None:
            raise QgsProcessingException("Failed to convert DEM for WhiteboxTools.")
        _ds.FlushCache(); _ds = None
        with open(dem_wbt_stamp, "w") as f:
            f.write(src_sig)
    dem_use = dem_wbt

    filled      = os.path.join(temp_dir, "filled.tif")
    flowdir     = os.path.join(temp_dir, "flowdir.tif")
    flowacc     = os.path.join(temp_dir, "flowacc.tif")
    streams_r   = os.path.join(temp_dir, "streams.tif")
    pour_csv    = os.path.join(temp_dir, "pour.csv")
    pour_v      = os.path.join(temp_dir, "pour.shp")
    snapped_v   = os.path.join(temp_dir, "pour_snapped.shp")
    pour_r      = os.path.join(temp_dir, "pour_raster.tif")
    watershed_r = os.path.join(temp_dir, "watershed.tif")
    watershed_v = os.path.join(temp_dir, "watershed_vec.shp")
    dem_stamp   = os.path.join(temp_dir, "dem_source.txt")

    # Invalidate pipeline cache when the converted DEM changes
    dem_sig = f"{dem_use}|{os.path.getmtime(dem_use):.0f}"
    prev = open(dem_stamp).read().strip() if os.path.exists(dem_stamp) else ""
    rebuild = (prev != dem_sig)
    if rebuild:
        feedback.pushInfo("DEM changed — (re)building stream rasters.")

    def _step(label, fn, prog):
        if feedback.isCanceled():
            raise QgsProcessingException("Cancelled by user.")
        feedback.pushInfo(f"{label} ...")
        feedback.setProgress(prog)
        t = time.time()
        fn()
        feedback.pushInfo(f"  done  {time.time()-t:.1f}s")

    # Pour points → vector
    with open(pour_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["X", "Y", "CAT_ID"])
        for i, (x, y) in enumerate(coords, 1):
            w.writerow([f"{x:.3f}", f"{y:.3f}", i])
    _step("Prepare pour point(s) → vector",
          lambda: wbt.csv_points_to_vector(pour_csv, pour_v, xfield=0, yfield=1, epsg=epsg), 4)

    # Stream pipeline (cached unless DEM changed)
    need = rebuild or not all(os.path.exists(f) for f in [filled, flowdir, streams_r])
    if need:
        _step("[1/4] Fill depressions (Wang & Liu)",
              lambda: wbt.fill_depressions_wang_and_liu(dem_use, filled), 10)
        _step("[2/4] D8 flow direction",
              lambda: wbt.d8_pointer(filled, flowdir), 24)
        _step("[3/4] D8 flow accumulation",
              lambda: wbt.d8_flow_accumulation(filled, flowacc, out_type="cells"), 34)
        _step(f"[4/4] Extract streams ({threshold_cells:,} cells = {threshold_km2} km²)",
              lambda: wbt.extract_streams(flowacc, streams_r, threshold=threshold_cells), 42)
        with open(dem_stamp, "w") as f:
            f.write(dem_sig)
    else:
        feedback.pushInfo("  Reusing cached fill / flowdir / streams.")
        feedback.setProgress(42)

    # Delineation
    _step(f"Snap pour point(s) to stream (tol {snap_dist} m)",
          lambda: wbt.jenson_snap_pour_points(pour_v, streams_r, snapped_v, snap_dist=snap_dist), 52)
    if not os.path.exists(snapped_v):
        raise QgsProcessingException(
            "Snapping failed — no snapped point produced. Increase snap distance "
            "or check the point lies within the DEM and near a stream.")
    _step("Rasterize snapped pour point(s)",
          lambda: wbt.vector_points_to_raster(snapped_v, pour_r, field="CAT_ID",
                                              nodata=True, cell_size=None, base=filled), 64)
    _step("Delineate watershed(s)",
          lambda: wbt.watershed(flowdir, pour_r, watershed_r), 76)

    feedback.pushInfo("Vectorise catchment polygon(s) ...")
    feedback.setProgress(84)
    for f in glob.glob(watershed_v.replace(".shp", ".*")):
        try: os.remove(f)
        except: pass
    wbt.raster_to_vector_polygons(watershed_r, watershed_v)
    if not os.path.exists(watershed_v):
        raise QgsProcessingException("WBT did not produce polygon output.")
    return watershed_v, snapped_v


# ══════════════════════════════════════════════════════════════════════════════
# MERIT-Hydro / delineator helpers
# ══════════════════════════════════════════════════════════════════════════════

def _apply_merit_patches():
    """Patch delineator.queries for the SpatiaLite format used by mghydro.com.

    gpd.read_file(sql=...) silently ignores WHERE clauses for SpatiaLite files;
    the 'comid' column is treated as OGR FID and dropped from the result.
    Fix: use layer= + where= for filtering; identify the downstream reach by
    max uparea instead of comid column lookup.
    """
    import sqlite3 as _sl3
    import geopandas as _gpd
    import delineator.queries as _dq
    import delineator.core as _dc

    def _get_upstream_area_patched(home_unit_catchment, config):
        if len(str(home_unit_catchment)) < 7:
            return 0
        megabasin = str(home_unit_catchment)[0:2]
        from delineator.data import _find_data_file
        rivers_db_path = _find_data_file(f"vector/rivers{megabasin}.db", config)
        if rivers_db_path is None:
            return 0
        conn = _sl3.connect(rivers_db_path)
        cur = conn.cursor()
        cur.execute(f"SELECT uparea FROM rivers WHERE comid = {home_unit_catchment}")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0

    def _get_rivers_patched(upstream_catchment_list, split_catchment_polygon, config):
        home_unit_catchment = upstream_catchment_list[0]
        if len(str(home_unit_catchment)) < 6:
            return None
        megabasin = str(home_unit_catchment)[0:2]
        from delineator.data import _find_data_file
        rivers_db_path = _find_data_file(f"vector/rivers{megabasin}.db", config)

        id_list = ", ".join(str(i) for i in upstream_catchment_list)
        if len(upstream_catchment_list) == 1:
            where_clause = f"comid = {home_unit_catchment}"
        else:
            conn = _sl3.connect(rivers_db_path)
            cur = conn.cursor()
            cur.execute(f"SELECT sorder FROM rivers WHERE comid = {home_unit_catchment}")
            row = cur.fetchone()
            conn.close()
            if row is None:
                return None
            where_clause = (f"comid IN ({id_list}) AND "
                            f"sorder > {row[0] - config.num_stream_orders}")

        rivers_gdf = _gpd.read_file(rivers_db_path, layer="rivers", where=where_clause)
        if len(rivers_gdf) == 0:
            return None

        if split_catchment_polygon is not None:
            ds_idx = rivers_gdf["uparea"].idxmax()
            split_reach = rivers_gdf.loc[ds_idx, "geometry"].intersection(split_catchment_polygon)
            rivers_gdf.loc[ds_idx, "geometry"] = split_reach

        return rivers_gdf

    _dq.get_upstream_area = _get_upstream_area_patched
    _dq.get_rivers = _get_rivers_patched
    _dc.get_upstream_area = _get_upstream_area_patched
    _dc.get_rivers = _get_rivers_patched


def _merit_parallel_download(url, dest_path, total_bytes, supports_range, known_hash, feedback):
    """Download url to dest_path using parallel Range requests when server allows it."""
    import hashlib, threading, time
    from pathlib import Path
    requests = _ensure_requests(feedback)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    n_threads = min(4, os.cpu_count() or 2) if (supports_range and total_bytes > 10 * 1024 * 1024) else 1

    if n_threads == 1 or not supports_range:
        r = requests.get(url, stream=True, timeout=300)
        r.raise_for_status()
        downloaded = 0
        t0 = time.time()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if feedback.isCanceled():
                    dest.unlink(missing_ok=True)
                    raise QgsProcessingException("Download cancelled.")
                f.write(chunk)
                downloaded += len(chunk)
                if total_bytes > 0 and downloaded % (10 * 1024 * 1024) < 65536:
                    pct = int(downloaded / total_bytes * 100)
                    elapsed = time.time() - t0
                    speed = downloaded / elapsed / 1e6 if elapsed > 0 else 0
                    feedback.pushInfo(f"    {pct}%  {downloaded/1e6:.0f}/{total_bytes/1e6:.0f} MB"
                                      f"  @ {speed:.1f} MB/s")
    else:
        with open(dest, "wb") as f:
            f.seek(total_bytes - 1)
            f.write(b"\0")

        progress = {"downloaded": 0}
        lock = threading.Lock()
        chunk_size_each = total_bytes // n_threads
        ranges = [(i * chunk_size_each,
                   (i * chunk_size_each + chunk_size_each - 1) if i < n_threads - 1 else total_bytes - 1)
                  for i in range(n_threads)]

        def _fetch_chunk(start, end):
            r = requests.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=300, stream=True)
            r.raise_for_status()
            with open(dest, "r+b") as f:
                f.seek(start)
                for data in r.iter_content(chunk_size=65536):
                    f.write(data)
                    with lock:
                        progress["downloaded"] += len(data)

        t0 = time.time()
        feedback.pushInfo(f"    Parallel download: {n_threads} threads")
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
            futs = [ex.submit(_fetch_chunk, s, e) for s, e in ranges]
            while not all(f.done() for f in futs):
                if feedback.isCanceled():
                    for f in futs:
                        f.cancel()
                    dest.unlink(missing_ok=True)
                    raise QgsProcessingException("Download cancelled.")
                done = progress["downloaded"]
                pct = int(done / total_bytes * 100) if total_bytes else 0
                elapsed = time.time() - t0
                speed = done / elapsed / 1e6 if elapsed > 0 else 0
                feedback.pushInfo(f"    {pct}%  {done/1e6:.0f}/{total_bytes/1e6:.0f} MB  @ {speed:.1f} MB/s")
                feedback.setProgress(pct)
                time.sleep(2.0)
            for f in futs:
                if f.exception():
                    dest.unlink(missing_ok=True)
                    raise QgsProcessingException(f"Download error: {f.exception()}")

        elapsed = time.time() - t0
        feedback.pushInfo(f"    Done: {total_bytes/1e6:.0f} MB in {elapsed:.0f}s"
                          f"  @ {total_bytes/elapsed/1e6:.1f} MB/s avg")

    if known_hash:
        feedback.pushInfo("    Verifying SHA256 ...")
        import hashlib
        sha = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        if sha.hexdigest() != known_hash:
            dest.unlink(missing_ok=True)
            raise QgsProcessingException(
                f"Hash mismatch for {dest.name} — file deleted. Re-run to re-download.")
        feedback.pushInfo("    Hash OK")


def _delineate_merit(lat, lng, include_rivers, data_dir, feedback):
    """Delineate watershed via MERIT-Hydro pre-computed data (delineator library)."""
    from pathlib import Path

    if WBT_SITE not in sys.path:
        sys.path.insert(0, WBT_SITE)
    try:
        import delineator  # noqa: F401
    except ImportError:
        feedback.pushInfo("Installing delineator package (runs once) ...")
        _pip_install("delineator", feedback)
        import importlib
        importlib.invalidate_caches()

    _apply_merit_patches()

    import sqlite3 as _sl3
    from delineator.constants import MEGABASINS_DB_FILE, HASHES, USE_SPATIALITE
    from delineator.spatial import _point_in_polygon_analysis
    from delineator import DelineatorConfig, delineate
    from shapely.geometry import Point

    data_path = Path(data_dir)

    # Determine megabasin from bundled megabasins.db (no download needed)
    conn = _sl3.connect(MEGABASINS_DB_FILE)
    megabasin = _point_in_polygon_analysis(
        conn, Point(lng, lat), table="megabasins", geom_col="geometry",
        id_col="basin", use_spatialite=USE_SPATIALITE, search_dist=0.1)
    conn.close()

    if megabasin is None:
        raise QgsProcessingException(
            f"Point ({lat:.5f}, {lng:.5f}) is not within any MERIT megabasin. "
            "Check that the point is over land.")

    feedback.pushInfo(f"  Megabasin   : {megabasin}")

    files_needed = [
        f"vector/basins{megabasin}.db",
        f"vector/rivers{megabasin}.db",
        f"raster/flowdir{megabasin}.tif",
        f"raster/accum{megabasin}.tif",
    ]

    requests = _ensure_requests(feedback)
    for rel_path in files_needed:
        dest = data_path / rel_path
        url = f"https://mghydro.com/watersheds/{rel_path.replace(os.sep, '/')}"

        if dest.exists() and dest.stat().st_size > 10_000:
            feedback.pushInfo(f"  Cached      : {rel_path}  ({dest.stat().st_size/1e6:.0f} MB)")
            continue

        try:
            head = requests.head(url, timeout=15, allow_redirects=True)
            head.raise_for_status()
            total_bytes = int(head.headers.get("Content-Length", 0))
            supports_range = head.headers.get("Accept-Ranges", "").lower() == "bytes"
        except Exception as e:
            raise QgsProcessingException(
                f"Cannot reach mghydro.com to check {rel_path}:\n{e}\n"
                "Check your internet connection.")

        feedback.pushInfo(f"  Download    : {rel_path}  ({total_bytes/1e6:.0f} MB)")
        if feedback.isCanceled():
            raise QgsProcessingException("Cancelled by user.")

        _merit_parallel_download(url, dest, total_bytes, supports_range,
                                 HASHES.get(rel_path), feedback)

    feedback.pushInfo("  Delineating watershed ...")
    cfg = DelineatorConfig(
        data_dir=data_path,
        high_res=True,
        rivers=include_rivers,
        auto_download=False,
    )
    watershed_gdf, rivers_gdf, outlet_gdf = delineate(lat=lat, lng=lng, config=cfg)

    if watershed_gdf is None:
        raise QgsProcessingException(
            "MERIT-Hydro delineation returned no watershed. "
            "The point may be in a coastal catchment or within a data gap.")

    return watershed_gdf, rivers_gdf, outlet_gdf


# ══════════════════════════════════════════════════════════════════════════════
class AutoCatchmentAlgorithm(QgsProcessingAlgorithm):

    METHOD         = "METHOD"
    CLICK_POINT    = "CLICK_POINT"
    INPUT_DEM      = "INPUT_DEM"
    SOURCE         = "SOURCE"
    OT_API_KEY     = "OT_API_KEY"
    SNAP_DISTANCE  = "SNAP_DISTANCE"
    THRESHOLD_KM2  = "THRESHOLD_KM2"
    TARGET_CRS     = "TARGET_CRS"
    MAX_TILES      = "MAX_TILES"
    INCLUDE_RIVERS = "INCLUDE_RIVERS"
    OUTPUT         = "OUTPUT"
    OUTPUT_OUTLET  = "OUTPUT_OUTLET"
    OUTPUT_DEM     = "OUTPUT_DEM"
    OUTPUT_RIVERS  = "OUTPUT_RIVERS"

    METHODS = [
        "WhiteboxTools  (DEM-based · auto-delineation)",
        "MERIT-Hydro / delineator  (pre-computed · no DEM needed)",
    ]

    EPS_DEG = 0.002   # ~200 m — boundary-touch tolerance in degrees

    SOURCES = [
        "Copernicus DEM 30 m  (ESA · no key · best quality)",
        "Copernicus DEM 90 m  (ESA · no key · faster download)",
        "SRTM 30 m            (NASA · OpenTopography key required)",
        "SRTM 90 m            (NASA · OpenTopography key required)",
        "ALOS World 3D 30 m   (JAXA · OpenTopography key required)",
        "NASADEM 30 m         (NASA · OpenTopography key required)",
    ]
    _OT_DEMTYPE = {2: "SRTMGL1", 3: "SRTMGL3", 4: "AW3D30", 5: "NASADEM"}

    def tr(self, s):
        return QCoreApplication.translate("Processing", s)

    def createInstance(self):
        return AutoCatchmentAlgorithm()

    def name(self):
        return "auto_catchment"

    def displayName(self):
        return self.tr("Auto Catchment  [click → DEM → delineate]")

    def group(self):
        return self.tr("Hydrology")

    def groupId(self):
        return "hydrology"

    def shortHelpString(self):
        return self.tr(
            "<b>Auto Catchment</b><br><br>"
            "Click one pour point. If you select an existing DEM it is used directly. "
            "If you leave the DEM blank, a DEM is downloaded from the chosen source "
            "and the catchment is delineated automatically.<br><br>"
            "<b>Sources (no login needed):</b><br>"
            "• <b>Copernicus 30 m</b> — best accuracy, recommended<br>"
            "• <b>Copernicus 90 m</b> — same quality, much faster download<br><br>"
            "<b>Sources (free OpenTopography API key needed):</b><br>"
            "• SRTM 30 m / 90 m, ALOS World 3D 30 m, NASADEM 30 m<br>"
            "Get a free key at: <i>portal.opentopography.org/requestApiKey</i><br><br>"
            "In download mode, if the catchment reaches the edge of the downloaded "
            "coverage, adjacent area is added automatically and delineation is "
            "repeated until the catchment is fully enclosed."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD,
            self.tr("Delineation method"),
            options=self.METHODS,
            defaultValue=0))
        self.addParameter(QgsProcessingParameterPoint(
            self.CLICK_POINT, self.tr("Pour point  (click on map canvas)")))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_DEM,
            self.tr("Existing DEM  (leave blank to auto-download)"),
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE,
            self.tr("DEM source  (used only when no existing DEM is selected)"),
            options=self.SOURCES,
            defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.OT_API_KEY,
            self.tr("OpenTopography API key  (only for SRTM / ALOS / NASADEM)"),
            optional=True,
            defaultValue=""))
        self.addParameter(QgsProcessingParameterNumber(
            self.SNAP_DISTANCE, self.tr("Snap distance (m)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=200.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.THRESHOLD_KM2, self.tr("Stream threshold (km²)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=0.5, minValue=0.001))
        self.addParameter(QgsProcessingParameterCrs(
            self.TARGET_CRS, self.tr("Target CRS for download  (blank = auto UTM)"),
            optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_TILES, self.tr("Max tiles / expansions (safety cap)"),
            type=QgsProcessingParameterNumber.Integer, defaultValue=16, minValue=1))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_RIVERS,
            self.tr("Include river network  (MERIT method only)"),
            defaultValue=False, optional=True))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.OUTPUT, self.tr("Output catchment polygons"),
            type=QgsProcessing.TypeVectorPolygon))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.OUTPUT_OUTLET, self.tr("Output snapped outlet"),
            type=QgsProcessing.TypeVectorPoint, optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT_DEM, self.tr("Output downloaded DEM  (download mode only)"),
            optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.OUTPUT_RIVERS, self.tr("Output river network  (MERIT method only)"),
            type=QgsProcessing.TypeVectorLine, optional=True, createByDefault=False))

    # ── main ───────────────────────────────────────────────────────────────────
    def processAlgorithm(self, parameters, context, feedback):
        method = self.parameterAsEnum(parameters, self.METHOD, context)
        if method == 1:
            return self._run_merit(parameters, context, feedback)

        snap_dist     = self.parameterAsDouble(parameters, self.SNAP_DISTANCE, context)
        threshold_km2 = self.parameterAsDouble(parameters, self.THRESHOLD_KM2, context)
        max_tiles     = self.parameterAsInt(parameters, self.MAX_TILES, context)
        source_idx    = self.parameterAsEnum(parameters, self.SOURCE, context)
        ot_key        = self.parameterAsString(parameters, self.OT_API_KEY, context).strip()
        dem_layer     = self.parameterAsRasterLayer(parameters, self.INPUT_DEM, context)

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        pt_wgs = self.parameterAsPoint(parameters, self.CLICK_POINT, context, wgs84)
        lon, lat = pt_wgs.x(), pt_wgs.y()

        script_dir = os.path.dirname(os.path.abspath(__file__))
        temp_dir   = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "auto_catchment")
        tile_dir   = os.path.join(temp_dir, "cop_tiles")
        os.makedirs(temp_dir, exist_ok=True)
        os.makedirs(tile_dir, exist_ok=True)

        wbt = _init_wbt(feedback)
        t0 = time.time()

        # ── Resolve DEM + target CRS ────────────────────────────────────────────
        download_mode = dem_layer is None or not dem_layer.isValid()

        if not download_mode:
            target_crs = dem_layer.crs()
            dem_path   = dem_layer.source()
            feedback.pushInfo("─" * 60)
            feedback.pushInfo("  Mode        : existing DEM")
            feedback.pushInfo(f"  DEM         : {dem_path}")
            feedback.pushInfo(f"  CRS         : {target_crs.authid()}")
        else:
            target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)
            if not target_crs.isValid():
                zone = int((lon + 180) / 6) + 1
                epsg = (32600 if lat >= 0 else 32700) + zone
                target_crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
            is_ot = source_idx >= 2
            if is_ot and not ot_key:
                raise QgsProcessingException(
                    "An OpenTopography API key is required for this DEM source.\n"
                    "Get a free key at: https://portal.opentopography.org/requestApiKey\n"
                    "Paste it into the 'OpenTopography API key' parameter.")
            feedback.pushInfo("─" * 60)
            feedback.pushInfo(f"  Mode        : auto-download  [{self.SOURCES[source_idx]}]")
            feedback.pushInfo(f"  Click WGS84 : lon {lon:.5f}, lat {lat:.5f}")
            feedback.pushInfo(f"  Target CRS  : {target_crs.authid()}")

        epsg_int = target_crs.postgisSrid()
        pt_xy = self.parameterAsPoint(parameters, self.CLICK_POINT, context, target_crs)
        coords = [(pt_xy.x(), pt_xy.y())]
        feedback.pushInfo(f"  Pour point  : {pt_xy.x():.2f}, {pt_xy.y():.2f}  ({target_crs.authid()})")
        feedback.pushInfo("─" * 60)

        out_dem_path = self.parameterAsOutputLayer(parameters, self.OUTPUT_DEM, context) \
                       if parameters.get(self.OUTPUT_DEM) else None

        # ── Delineate (with tile-expansion loop in download mode) ───────────────
        if not download_mode:
            watershed_v, snapped_v = _delineate(
                wbt, dem_path, coords, snap_dist, threshold_km2, epsg_int, temp_dir, feedback)
        else:
            # integer 1°×1° coverage bounds (inclusive SW corner of each tile)
            w_t = e_t = int(math.floor(lon))
            s_t = n_t = int(math.floor(lat))
            mosaic_path = out_dem_path or os.path.join(temp_dir, "auto_dem.tif")

            is_copernicus = source_idx in (0, 1)
            cop_res       = 90 if source_idx == 1 else 30
            ot_demtype    = self._OT_DEMTYPE.get(source_idx, "SRTMGL1")

            watershed_v = snapped_v = None
            for it in range(1, 9):
                n_cells = (e_t - w_t + 1) * (n_t - s_t + 1)
                feedback.pushInfo("═" * 60)
                feedback.pushInfo(f"  Iteration {it}: coverage "
                                  f"lon[{w_t}..{e_t+1}] lat[{s_t}..{n_t+1}]  "
                                  f"({n_cells} tile(s))")
                feedback.pushInfo("═" * 60)

                if is_copernicus:
                    if n_cells > max_tiles:
                        raise QgsProcessingException(
                            f"Catchment needs {n_cells} tiles, exceeding the cap of "
                            f"{max_tiles}. Raise 'Max tiles / expansions' if expected.")
                    cells = [(la, lo) for la in range(s_t, n_t + 1)
                                       for lo in range(w_t, e_t + 1)]
                    tiles = _download_copernicus_tiles(cells, tile_dir, feedback, cop_res)
                    if not tiles:
                        raise QgsProcessingException(
                            "No Copernicus tiles downloaded — check internet connection "
                            "and that the point is over land within Copernicus coverage.")
                    _mosaic_warp(tiles, target_crs.authid(), mosaic_path, feedback)
                else:
                    if it > max_tiles:
                        raise QgsProcessingException(
                            f"Reached {max_tiles} expansion iterations. "
                            "Raise 'Max tiles / expansions' if the catchment is larger.")
                    ot_raw = os.path.join(temp_dir, f"ot_raw_{it}.tif")
                    _download_opentopography(
                        west=float(w_t), south=float(s_t),
                        east=float(e_t + 1), north=float(n_t + 1),
                        demtype=ot_demtype, api_key=ot_key,
                        out_path=ot_raw, feedback=feedback)
                    _mosaic_warp([ot_raw], target_crs.authid(), mosaic_path, feedback)

                watershed_v, snapped_v = _delineate(
                    wbt, mosaic_path, coords, snap_dist, threshold_km2,
                    epsg_int, temp_dir, feedback)

                grew = self._expand_if_touching(
                    watershed_v, target_crs, context, feedback,
                    [w_t, e_t, s_t, n_t])
                if grew is None:
                    feedback.pushInfo("  Catchment fully enclosed — no expansion needed.")
                    break
                w_t, e_t, s_t, n_t = grew
            else:
                feedback.pushInfo("  Reached iteration limit — using last result.")

        # ── Write outputs ───────────────────────────────────────────────────────
        crs = target_crs
        poly_layer = QgsVectorLayer(watershed_v, "catchments_tmp", "ogr")
        if not poly_layer.isValid():
            raise QgsProcessingException("Could not load catchment polygon output.")
        poly_layer.setCrs(crs)

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, poly_layer.fields(),
            QgsWkbTypes.Polygon, crs)

        total_area = 0.0
        feats = [f for f in poly_layer.getFeatures()
                 if f.geometry() and f.geometry().type() == QgsWkbTypes.PolygonGeometry]
        for feat in feats:
            if feedback.isCanceled():
                break
            total_area += feat.geometry().area()
            sink.addFeature(feat, QgsFeatureSink.FastInsert)

        out_outlet_id = None
        if parameters.get(self.OUTPUT_OUTLET):
            outlet_layer = QgsVectorLayer(snapped_v, "outlet_tmp", "ogr")
            if outlet_layer.isValid():
                outlet_layer.setCrs(crs)
                (osink, out_outlet_id) = self.parameterAsSink(
                    parameters, self.OUTPUT_OUTLET, context, outlet_layer.fields(),
                    QgsWkbTypes.Point, crs)
                for feat in outlet_layer.getFeatures():
                    osink.addFeature(feat, QgsFeatureSink.FastInsert)

        feedback.setProgress(100)
        feedback.pushInfo("─" * 60)
        feedback.pushInfo(f"  Catchments  : {len(feats)}")
        feedback.pushInfo(f"  Total area  : {total_area/1e6:.2f} km²")
        feedback.pushInfo(f"  CRS         : {crs.authid()}")
        feedback.pushInfo(f"  Total time  : {time.time()-t0:.1f}s")
        feedback.pushInfo("─" * 60)

        result = {self.OUTPUT: dest_id}
        if out_outlet_id:
            result[self.OUTPUT_OUTLET] = out_outlet_id
        if download_mode and out_dem_path:
            result[self.OUTPUT_DEM] = out_dem_path
        return result

    # ── MERIT-Hydro delineation ─────────────────────────────────────────────────
    def _run_merit(self, parameters, context, feedback):
        import tempfile
        from pathlib import Path

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        pt_wgs = self.parameterAsPoint(parameters, self.CLICK_POINT, context, wgs84)
        lon, lat = pt_wgs.x(), pt_wgs.y()
        include_rivers = self.parameterAsBool(parameters, self.INCLUDE_RIVERS, context)

        data_dir = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()), "merit_data")

        feedback.pushInfo("─" * 60)
        feedback.pushInfo("  Method      : MERIT-Hydro / delineator")
        feedback.pushInfo(f"  Click WGS84 : lon {lon:.5f}, lat {lat:.5f}")
        feedback.pushInfo(f"  Rivers      : {'Yes' if include_rivers else 'No'}")
        feedback.pushInfo(f"  Cache dir   : {data_dir}")
        feedback.pushInfo("─" * 60)

        t0 = time.time()
        watershed_gdf, rivers_gdf, outlet_gdf = _delineate_merit(
            lat=lat, lng=lon, include_rivers=include_rivers,
            data_dir=data_dir, feedback=feedback)

        # Write to temp GPKG then sink into QGIS output
        tmp_gpkg = os.path.join(tempfile.gettempdir(), "merit_result.gpkg")
        for _f in (tmp_gpkg,):
            if os.path.exists(_f):
                try:
                    os.remove(_f)
                except Exception:
                    pass

        watershed_gdf.to_file(tmp_gpkg, driver="GPKG", layer="watershed")
        poly_layer = QgsVectorLayer(f"{tmp_gpkg}|layername=watershed", "merit_ws", "ogr")
        poly_layer.setCrs(wgs84)

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            poly_layer.fields(), QgsWkbTypes.MultiPolygon, wgs84)

        for feat in poly_layer.getFeatures():
            if feedback.isCanceled():
                break
            sink.addFeature(feat, QgsFeatureSink.FastInsert)

        result = {self.OUTPUT: dest_id}

        # Rivers output (optional)
        if (include_rivers and rivers_gdf is not None
                and parameters.get(self.OUTPUT_RIVERS)):
            rivers_gdf.to_file(tmp_gpkg, driver="GPKG", layer="rivers", mode="a")
            rivers_layer = QgsVectorLayer(
                f"{tmp_gpkg}|layername=rivers", "merit_rivers", "ogr")
            rivers_layer.setCrs(wgs84)
            (rsink, rivers_dest_id) = self.parameterAsSink(
                parameters, self.OUTPUT_RIVERS, context,
                rivers_layer.fields(), QgsWkbTypes.MultiLineString, wgs84)
            for feat in rivers_layer.getFeatures():
                rsink.addFeature(feat, QgsFeatureSink.FastInsert)
            result[self.OUTPUT_RIVERS] = rivers_dest_id

        area_km2 = float(watershed_gdf["area_km2"].iloc[0]) \
            if "area_km2" in watershed_gdf.columns else 0.0

        feedback.setProgress(100)
        feedback.pushInfo("─" * 60)
        feedback.pushInfo(f"  Area        : {area_km2:.1f} km²")
        feedback.pushInfo(f"  CRS         : EPSG:4326")
        feedback.pushInfo(f"  Total time  : {time.time() - t0:.1f}s")
        feedback.pushInfo("─" * 60)
        return result

    # ── boundary-touch test → expanded coverage or None ────────────────────────
    def _expand_if_touching(self, watershed_v, target_crs, context, feedback, bounds):
        w_t, e_t, s_t, n_t = bounds
        poly = QgsVectorLayer(watershed_v, "wtmp", "ogr")
        poly.setCrs(target_crs)
        bb = None
        for f in poly.getFeatures():
            g = f.geometry()
            if not g or g.type() != QgsWkbTypes.PolygonGeometry:
                continue
            bb = g.boundingBox() if bb is None else bb.combineExtentWith(g.boundingBox())
        if bb is None:
            return None

        xform = QgsCoordinateTransform(
            target_crs, QgsCoordinateReferenceSystem("EPSG:4326"),
            context.transformContext())
        corners = [
            xform.transform(QgsPointXY(bb.xMinimum(), bb.yMinimum())),
            xform.transform(QgsPointXY(bb.xMaximum(), bb.yMinimum())),
            xform.transform(QgsPointXY(bb.xMinimum(), bb.yMaximum())),
            xform.transform(QgsPointXY(bb.xMaximum(), bb.yMaximum())),
        ]
        minlon = min(c.x() for c in corners); maxlon = max(c.x() for c in corners)
        minlat = min(c.y() for c in corners); maxlat = max(c.y() for c in corners)

        eps = self.EPS_DEG
        nw, ne, ns, nn = w_t, e_t, s_t, n_t
        touched = []
        if maxlon >= (e_t + 1) - eps: ne = e_t + 1; touched.append("E")
        if minlon <= w_t + eps:       nw = w_t - 1; touched.append("W")
        if maxlat >= (n_t + 1) - eps: nn = n_t + 1; touched.append("N")
        if minlat <= s_t + eps:       ns = s_t - 1; touched.append("S")

        if not touched:
            return None
        feedback.pushInfo(f"  Catchment touches border(s): {', '.join(touched)} — "
                          f"adding adjacent tile(s).")
        return [nw, ne, ns, nn]
