"""
Create Streams — WhiteboxTools  (large-DEM optimised)
QGIS Processing Toolbox Script

Load in QGIS:
  Processing Toolbox → Scripts → ⋮ → Add Script from File → pick this file

Designed to be as fast as possible on any DEM size — from a single watershed
to a whole country.  Strategy is chosen automatically at runtime:

  DEM size class  │ Cells        │ Fill algorithm          │ Threads
  ────────────────┼──────────────┼─────────────────────────┼────────────────
  Small           │ < 50 M       │ breach_lc  (accurate)   │ all – 2
  Medium          │ 50 – 500 M   │ breach_lc  if RAM OK,   │ all – 2
                  │              │ else fill_wang_liu       │
  Large           │ 500 M – 2 B  │ fill_single_cell_pits   │ all cores
                  │              │ + fill_wang_liu          │
  Huge            │ > 2 B        │ fill_single_cell_pits   │ all cores
                  │              │ + fill_wang_liu          │

  ► All sizes: GDAL_CACHEMAX = 40 % of available RAM
  ► Large / Huge: intermediates always compressed to save disk space
  ► Vectorisation always runs in parallel tiles (one WBT instance per tile)
    → tile boundaries share exact coordinates; no topology repair needed
    → STRM_VAL (Strahler order) is preserved through the merge
"""

import os, sys, glob, time, ctypes, concurrent.futures
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterVectorDestination,
    QgsProcessingException,
    QgsFeatureSink,
    QgsWkbTypes,
    QgsProcessing,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

def _get_wbt_site():
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


# ── size-class thresholds (cells) ─────────────────────────────────────────────
_SMALL  =   50_000_000   # < 50 M   →  breach_lc if RAM allows
_MEDIUM =  500_000_000   # 50–500 M →  breach_lc if RAM allows, else wang_liu
_LARGE  = 2_000_000_000  # 500 M–2B →  wang_liu, all cores
                          # > 2 B   →  same + forced compress


# ══════════════════════════════════════════════════════════════════════════════
# System helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_ram_gb():
    try:
        class _MEM(ctypes.Structure):
            _fields_ = [
                ("dwLength",                 ctypes.c_ulong),
                ("dwMemoryLoad",             ctypes.c_ulong),
                ("ullTotalPhys",             ctypes.c_ulonglong),
                ("ullAvailPhys",             ctypes.c_ulonglong),
                ("ullTotalPageFile",         ctypes.c_ulonglong),
                ("ullAvailPageFile",         ctypes.c_ulonglong),
                ("ullTotalVirtual",          ctypes.c_ulonglong),
                ("ullAvailVirtual",          ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        m = _MEM(); m.dwLength = ctypes.sizeof(_MEM)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 2**30, m.ullAvailPhys / 2**30
    except Exception:
        pass
    try:
        import psutil; vm = psutil.virtual_memory()
        return vm.total / 2**30, vm.available / 2**30
    except Exception:
        return 8.0, 4.0


def _set_gdal_cache(avail_ram_gb):
    """Set GDAL_CACHEMAX to 40 % of available RAM (in MB)."""
    mb = int(avail_ram_gb * 1024 * 0.40)
    os.environ["GDAL_CACHEMAX"] = str(mb)
    os.environ["GDAL_NUM_THREADS"] = "ALL_CPUS"
    return mb


def _profile_system(dem_layer):
    total_ram, avail_ram = _get_ram_gb()
    cpu          = os.cpu_count() or 4
    dem_cells    = dem_layer.width() * dem_layer.height()
    dem_gb_est   = dem_cells * 4 * 5 / 2**30   # 5 float32 intermediates

    if dem_cells < _SMALL:
        size_class = "small"
    elif dem_cells < _MEDIUM:
        size_class = "medium"
    elif dem_cells < _LARGE:
        size_class = "large"
    else:
        size_class = "huge"

    # Fill method
    if size_class in ("small", "medium") and avail_ram >= dem_gb_est * 1.5 and total_ram >= 16:
        fill_method  = "breach_lc"
        pre_pit_fill = False
    else:
        fill_method  = "fill_wang_liu"
        pre_pit_fill = True    # fast single-scan pre-pass removes trivial sinks

    # Thread count — leave headroom only for small/medium; use everything for large jobs
    if size_class in ("large", "huge"):
        wbt_threads = cpu
    else:
        wbt_threads = max(1, cpu - 2) if cpu > 4 else cpu

    compress = (size_class in ("large", "huge"))

    try:
        import shutil
        _, _, free = shutil.disk_usage(os.environ.get("TEMP", "C:\\"))
        disk_free_gb = free / 2**30
    except Exception:
        disk_free_gb = 999.0

    gdal_cache_mb = _set_gdal_cache(avail_ram)

    lines = [
        f"DEM size class    : {size_class.upper()}  ({dem_cells:,} cells  "
        f"{dem_layer.width()}×{dem_layer.height()})",
        f"CPU cores         : {cpu}  →  WBT threads: {wbt_threads}",
        f"RAM total / avail : {total_ram:.1f} GB / {avail_ram:.1f} GB",
        f"Est. tmp I/O      : {dem_gb_est:.1f} GB  (disk free: {disk_free_gb:.1f} GB)",
        f"GDAL cache        : {gdal_cache_mb} MB",
        f"Fill algorithm    : {'breach_depressions_least_cost  (accurate, RAM-resident)' if fill_method == 'breach_lc' else 'fill_single_cell_pits → fill_depressions_wang_and_liu  (tile-based, memory-safe)'}",
        f"Compress tmp      : {compress}",
    ]

    return dict(
        size_class=size_class, total_ram_gb=total_ram, avail_ram_gb=avail_ram,
        cpu_cores=cpu, wbt_threads=wbt_threads,
        dem_cells=dem_cells, dem_gb_estimate=dem_gb_est,
        fill_method=fill_method, pre_pit_fill=pre_pit_fill,
        compress_intermediates=compress,
        summary_lines=lines,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tile-parallel vectorisation
# ══════════════════════════════════════════════════════════════════════════════

def _vectorize_tiled(strahler_r, flowdir_r, streams_v, n_tiles,
                     cell_size_m, temp_dir, feedback):
    """
    Split strahler_r + flowdir_r into n_tiles horizontal strips and vectorise
    each strip in parallel (separate 1-thread WBT instance per strip).
    Tile boundaries share exact coordinates, so no topology repair is needed —
    and skipping it preserves the STRM_VAL (Strahler order) attribute.
    """
    from osgeo import gdal, ogr, osr
    import whitebox as _wbt_mod

    gdal.UseExceptions()
    ds = gdal.Open(strahler_r)
    W, H = ds.RasterXSize, ds.RasterYSize
    gt  = ds.GetGeoTransform()
    prj = ds.GetProjection()
    ds  = None

    strip_h = max(1, (H + n_tiles - 1) // n_tiles)
    actual_tiles = (H + strip_h - 1) // strip_h

    feedback.pushInfo(f"  Vectorisation: {actual_tiles} parallel tile(s)  "
                      f"({strip_h} rows each,  {W}×{H} total)")

    def _process_tile(idx):
        y0 = idx * strip_h
        rows = min(strip_h, H - y0)
        if rows <= 0:
            return None

        ts  = os.path.join(temp_dir, f"strahler_t{idx}.tif")
        tf  = os.path.join(temp_dir, f"flowdir_t{idx}.tif")
        tv  = os.path.join(temp_dir, f"streams_t{idx}.shp")

        # Write raster strips — adjust geotransform (top-left Y shifts down)
        for src, dst in [(strahler_r, ts), (flowdir_r, tf)]:
            src_ds = gdal.Open(src)
            drv = gdal.GetDriverByName("GTiff")
            out_ds = drv.Create(dst, W, rows, 1,
                                src_ds.GetRasterBand(1).DataType,
                                ["COMPRESS=LZW", "BIGTIFF=IF_SAFER"])
            new_gt = list(gt)
            new_gt[3] = gt[3] + y0 * gt[5]   # shift top-left Y
            out_ds.SetGeoTransform(new_gt)
            out_ds.SetProjection(prj)
            data = src_ds.GetRasterBand(1).ReadAsArray(0, y0, W, rows)
            out_ds.GetRasterBand(1).WriteArray(data)
            nd = src_ds.GetRasterBand(1).GetNoDataValue()
            if nd is not None:
                out_ds.GetRasterBand(1).SetNoDataValue(nd)
            out_ds.FlushCache(); out_ds = None; src_ds = None

        # Vectorise strip with its own single-threaded WBT instance
        import io as _io
        if sys.stdout is None:
            sys.stdout = _io.StringIO()
        w = _wbt_mod.WhiteboxTools()
        w.set_max_procs(1)
        w.set_verbose_mode(False)
        if sys.platform == "win32":
            import subprocess as _sp
            _orig = _sp.Popen.__init__
            def _nw(self_p, args, **kwargs):
                si = kwargs.pop("startupinfo", None) or _sp.STARTUPINFO()
                si.dwFlags |= _sp.STARTF_USESHOWWINDOW; si.wShowWindow = 0
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000
                _orig(self_p, args, **kwargs)
            _sp.Popen.__init__ = _nw

        w.raster_streams_to_vector(ts, tf, tv)
        return tv if os.path.exists(tv) else None

    # Run tiles in parallel
    results = []
    n_workers = min(actual_tiles, (os.cpu_count() or 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = {ex.submit(_process_tile, i): i for i in range(actual_tiles)}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            done += 1
            res = fut.result()
            if res:
                results.append(res)
                feedback.pushInfo(f"    tile {futs[fut]+1}/{actual_tiles} done")

    if not results:
        raise QgsProcessingException("Tile vectorisation produced no output.")

    # Merge all strips into one shapefile with OGR
    feedback.pushInfo("  Merging tile vectors ...")
    drv = ogr.GetDriverByName("ESRI Shapefile")
    for f in glob.glob(streams_v.replace(".shp", ".*")):
        try: os.remove(f)
        except: pass

    # Hold datasource reference — GetLayer() result is invalid once the DS is GC'd
    ref_ds   = ogr.Open(results[0])
    ref_lyr  = ref_ds.GetLayer(0)
    geom_type = ref_lyr.GetGeomType()   # use WBT's actual type (may be 25D/Multi)
    defn     = ref_lyr.GetLayerDefn()

    # Build SRS from the strahler raster projection (WBT rarely writes .prj for tiles)
    from osgeo import osr as _osr
    srs_obj = _osr.SpatialReference()
    srs_obj.ImportFromWkt(prj)

    out_ds  = drv.CreateDataSource(streams_v)
    out_lyr = out_ds.CreateLayer("streams", srs=srs_obj, geom_type=geom_type)
    for i in range(defn.GetFieldCount()):
        fd = defn.GetFieldDefn(i)
        new_fd = ogr.FieldDefn(fd.GetName(), fd.GetType())
        new_fd.SetWidth(fd.GetWidth())
        new_fd.SetPrecision(fd.GetPrecision())
        out_lyr.CreateField(new_fd)
    ref_ds = None   # safe to close now — schema already copied

    # Write .prj file explicitly — OGR CreateDataSource may omit it
    with open(streams_v.replace(".shp", ".prj"), "w") as _pf:
        _pf.write(prj)

    for shp in results:
        src_ds = ogr.Open(shp)
        if src_ds is None:
            continue
        lyr = src_ds.GetLayer(0)
        for feat in lyr:
            out_lyr.CreateFeature(feat.Clone())
        src_ds = None

    out_ds.FlushCache(); out_ds = None
    feedback.pushInfo(f"  Merged {len(results)} tile(s)  →  {streams_v}")
    return streams_v


# ══════════════════════════════════════════════════════════════════════════════
# WBT init helper
# ══════════════════════════════════════════════════════════════════════════════

def _init_wbt(n_threads, compress, feedback):
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

        # Clean up any leftover temp dirs
        for _n in os.listdir(WBT_SITE):
            if _n.startswith("_wbt_install_"):
                _sh.rmtree(os.path.join(WBT_SITE, _n), ignore_errors=True)

        # Install to temp dir (avoids Windows lock on existing whitebox/ directory)
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
            si.dwFlags |= _sp.STARTF_USESHOWWINDOW; si.wShowWindow = 0
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000
            _orig(self_popen, args, **kwargs)
        _sp.Popen.__init__ = _no_window

    import io as _io
    if sys.stdout is None:
        sys.stdout = _io.StringIO()

    wbt = whitebox.WhiteboxTools()
    wbt.set_max_procs(n_threads)
    wbt.set_verbose_mode(False)
    if compress:
        wbt.set_compress_rasters(True)
    return wbt


# ══════════════════════════════════════════════════════════════════════════════
# QGIS Processing Algorithm
# ══════════════════════════════════════════════════════════════════════════════

class CreateStreamsAlgorithm(QgsProcessingAlgorithm):

    INPUT_DEM       = "INPUT_DEM"
    THRESHOLD_KM2   = "THRESHOLD_KM2"
    VECT_TILES      = "VECT_TILES"
    SKIP_STRAHLER   = "SKIP_STRAHLER"
    FORCE_FILL      = "FORCE_FILL"
    FORCE_REPROCESS = "FORCE_REPROCESS"
    OUTPUT          = "OUTPUT"

    def tr(self, s):
        return QCoreApplication.translate("Processing", s)

    def createInstance(self):
        return CreateStreamsAlgorithm()

    def name(self):
        return "create_streams_wbt"

    def displayName(self):
        return self.tr("Create Streams  [WhiteboxTools]")

    def group(self):
        return self.tr("Hydrology")

    def groupId(self):
        return "hydrology"

    def shortHelpString(self):
        return self.tr(
            "<b>Create Streams — WhiteboxTools (large-DEM optimised)</b><br><br>"
            "Extracts a stream network from any DEM — from a small watershed to an "
            "entire country — using WhiteboxTools' multi-core Rust engine.<br><br>"
            "<b>Pipeline:</b> (optional pit-fill pre-pass →) depression fill → "
            "D8 flow direction → D8 flow accumulation → stream extraction → "
            "(optional Strahler order →) parallel tile vectorisation → merge<br><br>"
            "<b>Auto-optimisation:</b> fill algorithm, thread count, and compression "
            "are chosen automatically from DEM size, available RAM, and CPU count.<br><br>"
            "<b>Vectorisation tiles:</b> the stream raster is split into N horizontal "
            "strips vectorised in parallel, then merged. Tile boundaries share exact "
            "coordinates so no repair is needed — and STRM_VAL (Strahler order) is "
            "fully preserved. "
            "0 = auto (one tile per CPU core).<br><br>"
            "<b>Skip Strahler:</b> for country-scale DEMs where you just need the "
            "stream geometry and can skip the ordering step."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_DEM, self.tr("Input DEM")))
        self.addParameter(QgsProcessingParameterNumber(
            self.THRESHOLD_KM2, self.tr("Stream threshold (km²)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.5, minValue=0.001))
        self.addParameter(QgsProcessingParameterNumber(
            self.VECT_TILES,
            self.tr("Vectorisation tiles  (0 = auto: 1 per CPU core)"),
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=0, minValue=0, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SKIP_STRAHLER,
            self.tr("Skip Strahler order  (faster — stream geometry only)"),
            defaultValue=False, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FORCE_FILL,
            self.tr("Force Wang & Liu fill  (override RAM-based auto-selection)"),
            defaultValue=False, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FORCE_REPROCESS,
            self.tr("Force reprocess  (clear cached intermediate rasters)"),
            defaultValue=False, optional=True))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.OUTPUT, self.tr("Output stream network"),
            type=QgsProcessing.TypeVectorLine))

    # ── main ───────────────────────────────────────────────────────────────────
    def processAlgorithm(self, parameters, context, feedback):
        dem_layer       = self.parameterAsRasterLayer(parameters, self.INPUT_DEM, context)
        threshold_km2   = self.parameterAsDouble(parameters, self.THRESHOLD_KM2, context)
        vect_tiles      = self.parameterAsInt(parameters, self.VECT_TILES, context)
        skip_strahler   = self.parameterAsBoolean(parameters, self.SKIP_STRAHLER, context)
        force_fill      = self.parameterAsBoolean(parameters, self.FORCE_FILL, context)
        force_reprocess = self.parameterAsBoolean(parameters, self.FORCE_REPROCESS, context)
        dem_path        = dem_layer.source()

        cell_x  = dem_layer.rasterUnitsPerPixelX()
        cell_y  = dem_layer.rasterUnitsPerPixelY()
        cell_m  = (cell_x + cell_y) / 2
        n_cells = int(threshold_km2 * 1e6 / (cell_x * cell_y))

        plan = _profile_system(dem_layer)
        if force_fill:
            plan["fill_method"]  = "fill_wang_liu"
            plan["pre_pit_fill"] = True

        if vect_tiles == 0:
            vect_tiles = plan["cpu_cores"]

        feedback.pushInfo("═" * 60)
        feedback.pushInfo("  Create Streams — optimisation plan")
        feedback.pushInfo("═" * 60)
        for ln in plan["summary_lines"]:
            feedback.pushInfo("  " + ln)
        feedback.pushInfo(f"  Cell size         : {cell_x:.2f} × {cell_y:.2f} m")
        feedback.pushInfo(f"  Stream threshold  : {n_cells:,} cells = {threshold_km2} km²")
        feedback.pushInfo(f"  Strahler order    : {'skipped' if skip_strahler else 'yes'}")
        feedback.pushInfo(f"  Vectorise tiles   : {vect_tiles}")
        feedback.pushInfo("═" * 60)

        wbt = _init_wbt(plan["wbt_threads"], plan["compress_intermediates"], feedback)

        temp_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "wbt_streams")
        os.makedirs(temp_dir, exist_ok=True)

        # ── WBT-safe DEM copy ─────────────────────────────────────────────────
        from osgeo import gdal as _gdal
        _gdal.UseExceptions()
        dem_wbt       = os.path.join(temp_dir, "dem_wbt.tif")
        dem_wbt_stamp = os.path.join(temp_dir, "dem_wbt_source.txt")
        src_sig  = f"{dem_path}|{os.path.getmtime(dem_path):.0f}"
        prev_sig = open(dem_wbt_stamp).read().strip() if os.path.exists(dem_wbt_stamp) else ""
        if not os.path.exists(dem_wbt) or prev_sig != src_sig:
            feedback.pushInfo("Converting DEM to WBT-compatible format (striped, LZW) ...")
            _ds = _gdal.Translate(dem_wbt, dem_path,
                                  creationOptions=["COMPRESS=LZW", "BIGTIFF=IF_SAFER"])
            if _ds is None:
                raise QgsProcessingException("GDAL Translate failed for WBT DEM copy.")
            _ds.FlushCache(); _ds = None
            with open(dem_wbt_stamp, "w") as f: f.write(src_sig)
        dem_path = dem_wbt

        # Intermediate file paths
        pit_filled  = os.path.join(temp_dir, "pit_filled.tif")
        filled      = os.path.join(temp_dir, "filled.tif")
        flowdir     = os.path.join(temp_dir, "flowdir.tif")
        flowacc     = os.path.join(temp_dir, "flowacc.tif")
        streams_r   = os.path.join(temp_dir, "streams.tif")
        strahler_r  = os.path.join(temp_dir, "strahler.tif")
        streams_v   = os.path.join(temp_dir, "streams_vec.shp")
        dem_stamp   = os.path.join(temp_dir, "dem_source.txt")

        # Cache invalidation
        dem_sig    = f"{dem_path}|{os.path.getmtime(dem_path):.0f}"
        cached_sig = open(dem_stamp).read().strip() if os.path.exists(dem_stamp) else ""
        if cached_sig != dem_sig:
            force_reprocess = True
            feedback.pushInfo("DEM changed — clearing cached intermediates.")

        if force_reprocess:
            for f in [pit_filled, filled, flowdir, flowacc, streams_r, strahler_r]:
                if os.path.exists(f):
                    os.remove(f)
            for f in glob.glob(streams_v.replace(".shp", ".*")):
                try: os.remove(f)
                except: pass

        t0 = time.time()

        def _step(label, fn, out_path, prog):
            if feedback.isCanceled():
                raise QgsProcessingException("Cancelled by user.")
            if os.path.exists(out_path):
                feedback.pushInfo(f"{label} — using cache")
                feedback.setProgress(prog)
                return
            feedback.pushInfo(f"{label} ...")
            feedback.setProgress(max(0, prog - 10))
            t = time.time()
            fn()
            feedback.pushInfo(f"  done  {time.time()-t:.1f}s")
            feedback.setProgress(prog)

        # ── [0] Optional single-cell pit pre-fill ─────────────────────────────
        if plan["pre_pit_fill"]:
            _step("[0] Pre-fill single-cell pits  (fast single-scan)",
                  lambda: wbt.fill_single_cell_pits(dem_path, pit_filled),
                  pit_filled, 5)
            fill_input = pit_filled
        else:
            fill_input = dem_path

        # ── [1] Depression fill / breach ──────────────────────────────────────
        if plan["fill_method"] == "breach_lc":
            _step("[1/5] Breach depressions (least-cost, accurate)",
                  lambda: wbt.breach_depressions_least_cost(
                      fill_input, filled, dist=5, fill=True),
                  filled, 25)
        else:
            _step("[1/5] Fill depressions — Wang & Liu (tile-based, memory-safe)",
                  lambda: wbt.fill_depressions_wang_and_liu(fill_input, filled),
                  filled, 25)

        # ── [2] D8 flow direction ─────────────────────────────────────────────
        _step("[2/5] D8 flow direction",
              lambda: wbt.d8_pointer(filled, flowdir),
              flowdir, 38)

        # ── [3] D8 flow accumulation ──────────────────────────────────────────
        _step("[3/5] D8 flow accumulation",
              lambda: wbt.d8_flow_accumulation(filled, flowacc, out_type="cells"),
              flowacc, 52)

        # ── [4] Extract streams ───────────────────────────────────────────────
        _step(f"[4/5] Extract streams  ({n_cells:,} cells = {threshold_km2} km²)",
              lambda: wbt.extract_streams(flowacc, streams_r, threshold=n_cells),
              streams_r, 62)

        # ── [5] Strahler order (optional) ─────────────────────────────────────
        if not skip_strahler:
            _step("[5/5] Strahler stream order",
                  lambda: wbt.strahler_stream_order(flowdir, streams_r, strahler_r),
                  strahler_r, 70)
            vec_source = strahler_r
        else:
            feedback.pushInfo("[5/5] Strahler skipped — vectorising raw stream raster")
            feedback.setProgress(70)
            vec_source = streams_r

        # ── [6] Tile-parallel vectorisation ───────────────────────────────────
        feedback.pushInfo(f"[6/6] Parallel vectorisation ({vect_tiles} tile(s)) ...")
        feedback.setProgress(72)
        t = time.time()
        _vectorize_tiled(vec_source, flowdir, streams_v,
                         n_tiles=vect_tiles,
                         cell_size_m=cell_m,
                         temp_dir=temp_dir,
                         feedback=feedback)
        feedback.pushInfo(f"  vectorisation done  {time.time()-t:.1f}s")
        feedback.setProgress(90)

        with open(dem_stamp, "w") as f:
            f.write(dem_sig)

        # ── Write QGIS output sink ────────────────────────────────────────────
        feedback.pushInfo("Writing output layer ...")
        src = QgsVectorLayer(streams_v, "streams_tmp", "ogr")
        if not src.isValid():
            raise QgsProcessingException("Could not load vectorised stream output.")
        src.setCrs(dem_layer.crs())

        # WBT tiles each produce a 'FID' attribute field starting from 0.
        # After merging tiles these values are duplicated, which violates the
        # GPKG primary-key constraint (OUTPUT.fid).  Exclude it from the schema.
        from qgis.core import QgsFields, QgsFeature
        out_fields = QgsFields()
        fid_indices = set()
        for idx, f in enumerate(src.fields()):
            if f.name().upper() == 'FID':
                fid_indices.add(idx)
            else:
                out_fields.append(f)

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, src.wkbType(), dem_layer.crs())

        features = [f for f in src.getFeatures()
                    if f.geometry() and not f.geometry().isNull()]
        total = len(features)
        for i, feat in enumerate(features):
            if feedback.isCanceled():
                break
            out_feat = QgsFeature(out_fields)
            out_feat.setGeometry(feat.geometry())
            out_feat.setAttributes([v for j, v in enumerate(feat.attributes())
                                    if j not in fid_indices])
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
            if i % 1000 == 0:
                feedback.setProgress(90 + int(10 * i / max(total, 1)))

        elapsed = time.time() - t0
        feedback.setProgress(100)
        feedback.pushInfo("═" * 60)
        feedback.pushInfo(f"  Features  : {total:,}")
        feedback.pushInfo(f"  CRS       : {dem_layer.crs().authid()}")
        feedback.pushInfo(f"  Total     : {elapsed:.1f}s")
        feedback.pushInfo("═" * 60)

        self._dest_id = dest_id
        return {self.OUTPUT: dest_id}

    def postProcessAlgorithm(self, context, feedback):
        from qgis.core import (QgsProcessingUtils, QgsProject,
                               QgsSymbol, QgsLineSymbol,
                               QgsRuleBasedRenderer, QgsWkbTypes)
        from qgis.PyQt.QtGui import QColor

        lyr = QgsProcessingUtils.mapLayerFromString(self._dest_id, context)
        if not lyr:
            return {}

        # Strahler order rules: (label, filter, hex_color, width_mm)
        _RULES = [
            ("Order 1",  '"STRM_VAL" = 1',   "#9ecae1", 0.26),
            ("Order 2",  '"STRM_VAL" = 2',   "#6baed6", 0.46),
            ("Order 3",  '"STRM_VAL" = 3',   "#3182bd", 0.76),
            ("Order 4",  '"STRM_VAL" = 4',   "#08689c", 1.20),
            ("Order 5",  '"STRM_VAL" = 5',   "#084594", 1.80),
            ("Order 6",  '"STRM_VAL" = 6',   "#022864", 2.50),
            ("Order 7+", '"STRM_VAL" >= 7',  "#00143c", 3.50),
        ]

        root = QgsRuleBasedRenderer.Rule(None)
        for label, filt, color, width in _RULES:
            sym = QgsLineSymbol.createSimple({
                "color": color,
                "line_width": str(width),
                "capstyle": "round",
                "joinstyle": "round",
            })
            rule = QgsRuleBasedRenderer.Rule(sym)
            rule.setLabel(label)
            rule.setFilterExpression(filt)
            root.appendChild(rule)

        lyr.setRenderer(QgsRuleBasedRenderer(root))

        tree_lyr = QgsProject.instance().layerTreeRoot().findLayer(lyr.id())
        if tree_lyr:
            tree_lyr.setItemVisibilityChecked(True)
        lyr.triggerRepaint()
        return {}
