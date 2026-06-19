"""
Delineate Catchments — WhiteboxTools
QGIS Processing Toolbox Script

Load in QGIS:
  Processing Toolbox → Scripts → ⋮ → Add Script from File → pick this file

Reuses fill / flow-direction / stream rasters cached by the Create Streams tool
so if streams were already extracted the delineation completes in seconds.

Pour points: supply a point layer, click a single point on the map canvas, or both.
Each point becomes one watershed polygon. A snapped outlets layer is also produced
so you can verify exactly where each pour point landed on the stream.
"""

import os, sys, glob, csv, time, ctypes
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterPoint,
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
        m = _MEM()
        m.dwLength = ctypes.sizeof(_MEM)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 2**30, m.ullAvailPhys / 2**30
    except Exception:
        pass
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total / 2**30, vm.available / 2**30
    except Exception:
        return 8.0, 4.0


# ══════════════════════════════════════════════════════════════════════════════
class DelineateCatchmentsAlgorithm(QgsProcessingAlgorithm):

    INPUT_DEM       = "INPUT_DEM"
    INPUT_POINTS    = "INPUT_POINTS"
    CLICK_POINT     = "CLICK_POINT"
    SNAP_DISTANCE   = "SNAP_DISTANCE"
    THRESHOLD_KM2   = "THRESHOLD_KM2"
    FORCE_REPROCESS = "FORCE_REPROCESS"
    OUTPUT          = "OUTPUT"
    OUTPUT_OUTLETS  = "OUTPUT_OUTLETS"

    # ── metadata ─────────────────────────────────────────────────────────────

    def tr(self, s):
        return QCoreApplication.translate("Processing", s)

    def createInstance(self):
        return DelineateCatchmentsAlgorithm()

    def name(self):
        return "delineate_catchments_wbt"

    def displayName(self):
        return self.tr("Delineate Catchments  [WhiteboxTools]")

    def group(self):
        return self.tr("Hydrology")

    def groupId(self):
        return "hydrology"

    def shortHelpString(self):
        return self.tr(
            "<b>Delineate Catchments — WhiteboxTools</b><br><br>"
            "Delineates watershed polygons for one or more pour points using the "
            "WhiteboxTools multi-core Rust engine.<br><br>"
            "<b>Pour points:</b> supply a point layer, click a single point on "
            "the map canvas (Single pour point parameter), or both — they are "
            "combined automatically.<br><br>"
            "<b>Snap tolerance:</b> each pour point is automatically moved to "
            "the nearest stream cell within the specified radius. The snapped "
            "outlet layer shows exactly where each pour point landed.<br><br>"
            "<b>Speed:</b> if the Create Streams tool was already run for the "
            "same DEM the fill / flow-direction / stream rasters are reused "
            "and delineation completes in seconds."
        )

    # ── parameters ───────────────────────────────────────────────────────────

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_DEM,
                self.tr("Input DEM"),
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_POINTS,
                self.tr("Pour points layer  (point shapefile or loaded layer)"),
                types=[QgsProcessing.TypeVectorPoint],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterPoint(
                self.CLICK_POINT,
                self.tr("Single pour point  (click on map canvas)"),
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SNAP_DISTANCE,
                self.tr("Snap distance (m)  — pour point moves to nearest stream within this radius"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=200.0,
                minValue=1.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.THRESHOLD_KM2,
                self.tr("Stream threshold (km²)  — only used when stream rasters must be rebuilt"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.5,
                minValue=0.001,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FORCE_REPROCESS,
                self.tr("Force re-run stream pipeline  (ignore cached rasters)"),
                defaultValue=False,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr("Output catchment polygons"),
                type=QgsProcessing.TypeVectorPolygon,
            )
        )
        param = QgsProcessingParameterVectorDestination(
            self.OUTPUT_OUTLETS,
            self.tr("Output snapped outlets  (QA — shows where pour points landed on stream)"),
            type=QgsProcessing.TypeVectorPoint,
            optional=True,
            createByDefault=True,
        )
        self.addParameter(param)

    # ── main ─────────────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):

        # ── Read inputs ───────────────────────────────────────────────────────
        dem_layer       = self.parameterAsRasterLayer(parameters, self.INPUT_DEM, context)
        snap_dist       = self.parameterAsDouble(parameters, self.SNAP_DISTANCE, context)
        threshold_km2   = self.parameterAsDouble(parameters, self.THRESHOLD_KM2, context)
        force_reprocess = self.parameterAsBoolean(parameters, self.FORCE_REPROCESS, context)
        dem_path        = dem_layer.source()

        cell_size_x = dem_layer.rasterUnitsPerPixelX()
        cell_size_y = dem_layer.rasterUnitsPerPixelY()
        threshold_cells = int(threshold_km2 * 1e6 / (cell_size_x * cell_size_y))
        crs  = dem_layer.crs()
        epsg = crs.postgisSrid()

        # ── WBT init ──────────────────────────────────────────────────────────
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

        # Suppress CMD console windows
        if sys.platform == "win32":
            import subprocess as _sp
            _orig_popen = _sp.Popen.__init__
            def _no_window_popen(self_popen, args, **kwargs):
                si = kwargs.pop("startupinfo", None) or _sp.STARTUPINFO()
                si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000
                _orig_popen(self_popen, args, **kwargs)
            _sp.Popen.__init__ = _no_window_popen

        total_ram, avail_ram = _get_ram_gb()
        cpu_cores   = os.cpu_count() or 4
        wbt_threads = max(1, cpu_cores - 2) if cpu_cores > 4 else cpu_cores

        import io as _io
        if sys.stdout is None:
            sys.stdout = _io.StringIO()

        wbt = whitebox.WhiteboxTools()
        wbt.set_max_procs(wbt_threads)
        wbt.set_verbose_mode(False)

        temp_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "wbt_streams")
        os.makedirs(temp_dir, exist_ok=True)

        # ── Convert DEM to WBT-safe format ────────────────────────────────────
        # WhiteboxTools' internal TIFF reader cannot handle PREDICTOR=2 or tiled
        # GeoTIFFs (such as those written by download_dem.py before the fix).
        # Use a separate stamp so this check is independent of the pipeline cache.
        from osgeo import gdal as _gdal
        dem_wbt         = os.path.join(temp_dir, "dem_wbt.tif")
        dem_wbt_stamp   = os.path.join(temp_dir, "dem_wbt_source.txt")
        _dem_src_sig    = f"{dem_path}|{os.path.getmtime(dem_path):.0f}"
        _cached_wbt_sig = open(dem_wbt_stamp).read().strip() if os.path.exists(dem_wbt_stamp) else ""
        if not os.path.exists(dem_wbt) or _cached_wbt_sig != _dem_src_sig:
            feedback.pushInfo("Converting DEM to WBT-compatible format (striped, LZW) ...")
            _gdal.UseExceptions()
            _ds = _gdal.Translate(dem_wbt, dem_path,
                                   creationOptions=["COMPRESS=LZW", "BIGTIFF=IF_SAFER"])
            if _ds is None:
                raise QgsProcessingException("Failed to convert DEM for WhiteboxTools.")
            _ds.FlushCache(); _ds = None
            with open(dem_wbt_stamp, "w") as _f: _f.write(_dem_src_sig)
        dem_path = dem_wbt     # all WBT calls use the converted copy

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

        # Invalidate cache when the input DEM changes
        dem_sig = f"{dem_path}|{os.path.getmtime(dem_path):.0f}"
        cached_sig = ""
        if os.path.exists(dem_stamp):
            with open(dem_stamp) as _f:
                cached_sig = _f.read().strip()
        if cached_sig != dem_sig:
            force_reprocess = True
            feedback.pushInfo("Input DEM changed — cached stream rasters will be rebuilt.")

        t0 = time.time()

        def _step(label, fn, prog):
            if feedback.isCanceled():
                raise QgsProcessingException("Cancelled by user.")
            feedback.pushInfo(f"{label} ...")
            feedback.setProgress(prog)
            t = time.time()
            fn()
            feedback.pushInfo(f"  done  {time.time()-t:.1f}s")

        # ── Collect pour point coordinates ────────────────────────────────────
        coords = []

        # From points layer
        points_source = self.parameterAsSource(parameters, self.INPUT_POINTS, context)
        if points_source:
            for feat in points_source.getFeatures():
                geom = feat.geometry()
                if geom.isNull():
                    continue
                if geom.isMultipart():
                    for pt in geom.asMultiPoint():
                        coords.append((pt.x(), pt.y()))
                else:
                    pt = geom.asPoint()
                    coords.append((pt.x(), pt.y()))

        # From single canvas click
        try:
            if parameters.get(self.CLICK_POINT):
                click_pt = self.parameterAsPoint(parameters, self.CLICK_POINT, context, crs)
                coords.append((click_pt.x(), click_pt.y()))
        except Exception:
            pass

        if not coords:
            raise QgsProcessingException(
                "No pour points provided.\n"
                "Fill in 'Pour points layer' (a loaded point layer or shapefile) "
                "or click on the map canvas using 'Single pour point'."
            )

        feedback.pushInfo("─" * 54)
        feedback.pushInfo(f"  Pour points : {len(coords)}")
        feedback.pushInfo(f"  Snap dist   : {snap_dist} m")
        feedback.pushInfo(f"  CPU threads : {wbt_threads}  (total: {cpu_cores})")
        feedback.pushInfo(f"  RAM avail   : {avail_ram:.1f} GB / {total_ram:.1f} GB total")
        feedback.pushInfo("─" * 54)

        # ── Write pour points CSV → vector ────────────────────────────────────
        with open(pour_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["X", "Y", "CAT_ID"])
            for i, (x, y) in enumerate(coords, 1):
                w.writerow([f"{x:.3f}", f"{y:.3f}", i])

        _step("Prepare pour points → vector",
              lambda: wbt.csv_points_to_vector(pour_csv, pour_v,
                                                xfield=0, yfield=1, epsg=epsg),
              2)

        # ── Check / rebuild stream rasters ────────────────────────────────────
        needs_streams = force_reprocess or not all(
            os.path.exists(f) for f in [filled, flowdir, streams_r]
        )

        if needs_streams:
            feedback.pushInfo("─" * 54)
            feedback.pushInfo("  Stream pipeline  (building missing rasters)")
            feedback.pushInfo("─" * 54)

            dem_cells  = dem_layer.width() * dem_layer.height()
            dem_gb_est = dem_cells * 4 * 5 / 2**30
            fill_method = (
                "breach_lc"
                if avail_ram >= dem_gb_est * 1.5 and total_ram >= 16
                else "fill_wang_liu"
            )

            if not os.path.exists(filled) or force_reprocess:
                if fill_method == "breach_lc":
                    _step("[1/4] Breach depressions (least-cost, accurate)",
                          lambda: wbt.breach_depressions_least_cost(dem_path, filled,
                                                                     dist=5, fill=True), 5)
                else:
                    _step("[1/4] Fill depressions — Wang & Liu (memory-efficient)",
                          lambda: wbt.fill_depressions_wang_and_liu(dem_path, filled), 5)
            else:
                feedback.pushInfo("  reusing filled.tif")

            if not os.path.exists(flowdir) or force_reprocess:
                _step("[2/4] D8 flow direction",
                      lambda: wbt.d8_pointer(filled, flowdir), 20)
            else:
                feedback.pushInfo("  reusing flowdir.tif")

            if not os.path.exists(flowacc) or force_reprocess:
                _step("[3/4] D8 flow accumulation",
                      lambda: wbt.d8_flow_accumulation(filled, flowacc, out_type="cells"), 30)

            if not os.path.exists(streams_r) or force_reprocess:
                _step(f"[4/4] Extract streams  ({threshold_cells:,} cells = {threshold_km2} km²)",
                      lambda: wbt.extract_streams(flowacc, streams_r,
                                                   threshold=threshold_cells), 42)
        else:
            feedback.pushInfo("  Reusing cached fill / flowdir / streams — skipping stream pipeline.")
            feedback.setProgress(42)

        # ── Catchment delineation ─────────────────────────────────────────────
        feedback.pushInfo("─" * 54)
        feedback.pushInfo("  Catchment delineation")
        feedback.pushInfo("─" * 54)

        _step(f"[1/4] Snap pour points to stream  (tolerance {snap_dist} m)",
              lambda: wbt.jenson_snap_pour_points(pour_v, streams_r, snapped_v,
                                                   snap_dist=snap_dist), 50)

        if not os.path.exists(snapped_v):
            raise QgsProcessingException(
                "Snapping failed — WBT produced no snapped points output.\n"
                "Check that pour points lie within the DEM extent and within "
                f"{snap_dist} m of a stream."
            )

        _step("[2/4] Rasterize snapped pour points",
              lambda: wbt.vector_points_to_raster(snapped_v, pour_r,
                                                   field="CAT_ID", nodata=True,
                                                   cell_size=None, base=filled), 62)

        _step("[3/4] Delineate watersheds",
              lambda: wbt.watershed(flowdir, pour_r, watershed_r), 74)

        feedback.pushInfo("[4/4] Vectorise catchment polygons ...")
        feedback.setProgress(82)
        t = time.time()
        for f in glob.glob(watershed_v.replace(".shp", ".*")):
            try: os.remove(f)
            except: pass
        wbt.raster_to_vector_polygons(watershed_r, watershed_v)
        if not os.path.exists(watershed_v):
            raise QgsProcessingException("WBT did not produce polygon output.")
        feedback.pushInfo(f"  done  {time.time()-t:.1f}s")
        feedback.setProgress(88)

        # ── Write catchment polygons to QGIS output ───────────────────────────
        feedback.pushInfo("Writing catchment polygons ...")

        poly_layer = QgsVectorLayer(watershed_v, "catchments_tmp", "ogr")
        if not poly_layer.isValid():
            raise QgsProcessingException("Could not load catchment polygon output.")
        poly_layer.setCrs(crs)

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            poly_layer.fields(),
            QgsWkbTypes.Polygon,
            crs,
        )

        poly_feats = [f for f in poly_layer.getFeatures()
                      if f.geometry() and
                      f.geometry().type() == QgsWkbTypes.PolygonGeometry]

        for feat in poly_feats:
            if feedback.isCanceled():
                break
            sink.addFeature(feat, QgsFeatureSink.FastInsert)

        feedback.setProgress(94)

        # ── Write snapped outlets (optional, on by default) ───────────────────
        out_outlets_id = None
        try:
            if parameters.get(self.OUTPUT_OUTLETS):
                feedback.pushInfo("Writing snapped outlet points ...")
                outlet_layer = QgsVectorLayer(snapped_v, "outlets_tmp", "ogr")
                if outlet_layer.isValid():
                    outlet_layer.setCrs(crs)
                    (outlet_sink, out_outlets_id) = self.parameterAsSink(
                        parameters, self.OUTPUT_OUTLETS, context,
                        outlet_layer.fields(),
                        QgsWkbTypes.Point,
                        crs,
                    )
                    for feat in outlet_layer.getFeatures():
                        if feedback.isCanceled():
                            break
                        outlet_sink.addFeature(feat, QgsFeatureSink.FastInsert)
        except Exception:
            pass

        elapsed = time.time() - t0
        feedback.setProgress(100)
        feedback.pushInfo("─" * 54)
        feedback.pushInfo(f"  Catchments : {len(poly_feats)}")
        feedback.pushInfo(f"  CRS        : {crs.authid()}")
        feedback.pushInfo(f"  Total      : {elapsed:.1f}s")
        feedback.pushInfo("─" * 54)

        result = {self.OUTPUT: dest_id}
        if out_outlets_id:
            result[self.OUTPUT_OUTLETS] = out_outlets_id
        return result
