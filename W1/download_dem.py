"""
Download DEM — QGIS Processing Toolbox Script

Load in QGIS:
  Processing Toolbox → Scripts → ⋮ → Add Script from File → pick this file

Downloads a DEM for any area from free public sources and reprojects it to
your target CRS — ready to feed directly into Create Streams.

Available sources  (no login required):
  • Copernicus DEM 30 m  — ESA/EU · best quality · ~40 MB per 1°×1° tile
  • Copernicus DEM 90 m  — ESA/EU · faster download · ~5 MB per tile
  • SRTM 30 m            — NASA · classic global reference
  • SRTM 90 m            — NASA · fastest download
  • ALOS World 3D 30 m   — JAXA · good in mountainous terrain
  • NASADEM 30 m         — NASA · improved SRTM reprocessing

  Note: SRTM / ALOS / NASADEM require a free OpenTopography API key.
        Get one at: https://portal.opentopography.org/requestApiKey
        Copernicus downloads require no key at all.

Tiles are downloaded in parallel, mosaicked, clipped, and reprojected
to the target CRS in one step. Auto-installs 'requests' if missing.
"""

import os, sys, math, time, concurrent.futures
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterString,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
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


# ══════════════════════════════════════════════════════════════════════════════

class DownloadDEMAlgorithm(QgsProcessingAlgorithm):

    SOURCE          = "SOURCE"
    EXTENT          = "EXTENT"
    BUFFER_KM       = "BUFFER_KM"
    CLIP_TO_EXTENT  = "CLIP_TO_EXTENT"
    TARGET_CRS      = "TARGET_CRS"
    OT_API_KEY      = "OT_API_KEY"
    OUTPUT          = "OUTPUT"

    # index 0-1: Copernicus (no key) | 2-5: OpenTopography (key required)
    SOURCES = [
        "Copernicus DEM 30 m  (ESA · no key · best quality)",
        "Copernicus DEM 90 m  (ESA · no key · faster download)",
        "SRTM 30 m            (NASA · OpenTopography key required)",
        "SRTM 90 m            (NASA · OpenTopography key required)",
        "ALOS World 3D 30 m   (JAXA · OpenTopography key required)",
        "NASADEM 30 m         (NASA · OpenTopography key required)",
    ]

    _OT_DEMTYPE = {2: "SRTMGL1", 3: "SRTMGL3", 4: "AW3D30", 5: "NASADEM"}

    # ── metadata ──────────────────────────────────────────────────────────────

    def tr(self, s):
        return QCoreApplication.translate("Processing", s)

    def createInstance(self):
        return DownloadDEMAlgorithm()

    def name(self):
        return "download_dem"

    def displayName(self):
        return self.tr("Download DEM  [Copernicus / SRTM / ALOS]")

    def group(self):
        return self.tr("Hydrology")

    def groupId(self):
        return "hydrology"

    def shortHelpString(self):
        return self.tr(
            "<b>Download DEM</b><br><br>"
            "Downloads a DEM for any area and reprojects it to your project CRS — "
            "ready to feed directly into <i>Create Streams</i>.<br><br>"
            "<b>Sources (no login needed):</b><br>"
            "• <b>Copernicus 30m</b> — best vertical accuracy, recommended<br>"
            "• <b>Copernicus 90m</b> — same quality, much faster download<br><br>"
            "<b>Sources (free OpenTopography API key needed):</b><br>"
            "• SRTM 30m / 90m, ALOS World 3D 30m, NASADEM 30m<br>"
            "Get a free key at: <i>portal.opentopography.org/requestApiKey</i><br><br>"
            "<b>Tip:</b> add 2–5 km buffer to avoid edge artefacts in flow analysis. "
            "Leave Target CRS blank for automatic UTM zone selection."
        )

    # ── parameters ────────────────────────────────────────────────────────────

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterEnum(
                self.SOURCE,
                self.tr("DEM source"),
                options=self.SOURCES,
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterExtent(
                self.EXTENT,
                self.tr("Area of interest  (any CRS — converted to WGS84 automatically)"),
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUFFER_KM,
                self.tr("Buffer around extent (km)  — recommended ≥ 2 km"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=2.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CLIP_TO_EXTENT,
                self.tr("Clip output to selected extent + buffer  (uncheck to keep full tiles)"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.TARGET_CRS,
                self.tr("Target CRS  (blank = auto UTM zone)"),
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.OT_API_KEY,
                self.tr("OpenTopography API key  (only for SRTM / ALOS / NASADEM)"),
                optional=True,
                defaultValue="",
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
                self.tr("Output DEM"),
            )
        )

    # ── main ──────────────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):
        from osgeo import gdal
        gdal.UseExceptions()

        source_idx     = self.parameterAsEnum(parameters, self.SOURCE, context)
        buffer_km      = self.parameterAsDouble(parameters, self.BUFFER_KM, context)
        clip_to_extent = self.parameterAsBoolean(parameters, self.CLIP_TO_EXTENT, context)
        ot_key         = self.parameterAsString(parameters, self.OT_API_KEY, context).strip()
        output_path    = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        # ── Extent in WGS84 ───────────────────────────────────────────────────
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        ext   = self.parameterAsExtent(parameters, self.EXTENT, context, wgs84)
        buf   = buffer_km / 111.0
        west  = max(-180.0, ext.xMinimum() - buf)
        south = max( -90.0, ext.yMinimum() - buf)
        east  = min( 180.0, ext.xMaximum() + buf)
        north = min(  90.0, ext.yMaximum() + buf)

        # ── Target CRS ────────────────────────────────────────────────────────
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)
        if not target_crs.isValid():
            cx   = (west + east)   / 2
            cy   = (south + north) / 2
            zone = int((cx + 180) / 6) + 1
            epsg = (32600 if cy >= 0 else 32700) + zone
            target_crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
            feedback.pushInfo(f"  Auto CRS: {target_crs.authid()}")

        feedback.pushInfo("─" * 54)
        feedback.pushInfo(f"  Source : {self.SOURCES[source_idx]}")
        feedback.pushInfo(f"  Extent : W{west:.4f} S{south:.4f} E{east:.4f} N{north:.4f}")
        feedback.pushInfo(f"  Buffer : {buffer_km} km")
        feedback.pushInfo(f"  Clip   : {'yes — trimmed to extent + buffer' if clip_to_extent else 'no — full tiles kept'}")
        feedback.pushInfo(f"  CRS    : {target_crs.authid()}")
        feedback.pushInfo("─" * 54)

        temp_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dem_download")
        os.makedirs(temp_dir, exist_ok=True)

        t0 = time.time()

        # ── Download ──────────────────────────────────────────────────────────
        if source_idx in (0, 1):
            tiles = self._download_copernicus(
                west, south, east, north, temp_dir,
                resolution=30 if source_idx == 0 else 90,
                feedback=feedback,
            )
        else:
            if not ot_key:
                raise QgsProcessingException(
                    "An OpenTopography API key is required for this source.\n"
                    "Get a free key at: https://portal.opentopography.org/requestApiKey\n"
                    "Then paste it into the 'OpenTopography API key' parameter."
                )
            tiles = self._download_opentopography(
                west, south, east, north, temp_dir,
                demtype=self._OT_DEMTYPE[source_idx],
                api_key=ot_key,
                feedback=feedback,
            )

        if not tiles:
            raise QgsProcessingException(
                "No DEM tiles were downloaded.\n"
                "Check the extent is correct and your internet connection is working."
            )

        feedback.pushInfo(f"  {len(tiles)} tile(s) ready  ({time.time()-t0:.0f}s)")
        feedback.setProgress(72)

        # ── Mosaic + reproject ─────────────────────────────────────────────────
        feedback.pushInfo(f"Reprojecting to {target_crs.authid()} ...")
        feedback.pushInfo(f"  Clip to extent: {clip_to_extent}")

        vrt = gdal.BuildVRT("/vsimem/mosaic.vrt", tiles)
        if vrt is None:
            raise QgsProcessingException("Failed to build VRT from downloaded tiles.")

        # Step 1: full warp to target CRS (no clipping yet)
        warp_opts = gdal.WarpOptions(
            srcSRS="EPSG:4326",
            dstSRS=target_crs.authid(),
            resampleAlg="bilinear",
            outputType=gdal.GDT_Float32,
            srcNodata=-9999,
            dstNodata=-9999,
            # No TILED / PREDICTOR — WhiteboxTools' internal TIFF reader requires
            # striped, predictor-free rasters; tiling or PREDICTOR=2 causes WBT to
            # misread elevation values and routes all flow to the left edge.
            creationOptions=["COMPRESS=LZW", "BIGTIFF=IF_SAFER"],
            multithread=True,
            warpOptions=["NUM_THREADS=ALL_CPUS"],
        )

        warp_target = "/vsimem/warped_full.tif" if clip_to_extent else output_path
        ds = gdal.Warp(warp_target, vrt, options=warp_opts)
        if ds is None:
            raise QgsProcessingException("gdal.Warp reprojection failed.")
        ds.FlushCache()
        ds = None
        vrt = None

        # Step 2: clip to extent + buffer (in target CRS) using gdal.Translate
        if clip_to_extent:
            xform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                target_crs,
                context.transformContext(),
            )
            corners = [
                xform.transform(QgsPointXY(west,  south)),
                xform.transform(QgsPointXY(east,  south)),
                xform.transform(QgsPointXY(west,  north)),
                xform.transform(QgsPointXY(east,  north)),
            ]
            xmin = min(c.x() for c in corners)
            ymin = min(c.y() for c in corners)
            xmax = max(c.x() for c in corners)
            ymax = max(c.y() for c in corners)
            feedback.pushInfo(f"  Clip bounds: {xmin:.0f},{ymin:.0f} → {xmax:.0f},{ymax:.0f}")

            ds = gdal.Translate(
                output_path,
                warp_target,
                projWin=[xmin, ymax, xmax, ymin],   # ulx, uly, lrx, lry
                creationOptions=["COMPRESS=LZW", "BIGTIFF=IF_SAFER"],
            )
            if ds is None:
                raise QgsProcessingException("gdal.Translate clip failed.")
            ds.FlushCache()
            ds = None
            gdal.Unlink("/vsimem/warped_full.tif")

        elapsed = time.time() - t0
        feedback.setProgress(100)
        feedback.pushInfo("─" * 54)
        feedback.pushInfo(f"  CRS    : {target_crs.authid()}")
        feedback.pushInfo(f"  Output : {output_path}")
        feedback.pushInfo(f"  Total  : {elapsed:.0f}s")
        feedback.pushInfo("─" * 54)

        return {self.OUTPUT: output_path}

    # ── Copernicus tile download ───────────────────────────────────────────────

    def _download_copernicus(self, west, south, east, north, temp_dir, resolution, feedback):
        requests = _ensure_requests(feedback)

        if resolution == 30:
            bucket, code = "copernicus-dem-30m", "COG_10"
        else:
            bucket, code = "copernicus-dem-90m", "COG_30"

        # 1°×1° tiles named by NW corner (top-left)
        lat_range = range(int(math.floor(south)), int(math.ceil(north)))
        lon_range = range(int(math.floor(west)),  int(math.ceil(east)))

        jobs = []
        for lat in lat_range:
            for lon in lon_range:
                ns  = "N" if lat >= 0 else "S"
                ew  = "E" if lon >= 0 else "W"
                tag = f"Copernicus_DSM_{code}_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"
                url = f"https://{bucket}.s3.amazonaws.com/{tag}/{tag}.tif"
                out = os.path.join(temp_dir, f"{tag}.tif")
                jobs.append((url, out))

        feedback.pushInfo(f"Downloading {len(jobs)} Copernicus {resolution}m tile(s) in parallel ...")

        def _fetch(job):
            url, out = job
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out          # reuse cached tile
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
        max_w = min(8, len(jobs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as ex:
            futures = {ex.submit(_fetch, j): j for j in jobs}
            for i, fut in enumerate(concurrent.futures.as_completed(futures)):
                if feedback.isCanceled():
                    break
                result = fut.result()
                if result:
                    downloaded.append(result)
                    feedback.pushInfo(f"  ✓ {os.path.basename(result)}"
                                      f"  ({os.path.getsize(result)/1e6:.1f} MB)")
                feedback.setProgress(10 + int(60 * (i + 1) / len(jobs)))

        return downloaded

    # ── OpenTopography download ────────────────────────────────────────────────

    def _download_opentopography(self, west, south, east, north, temp_dir,
                                  demtype, api_key, feedback):
        requests = _ensure_requests(feedback)

        out = os.path.join(temp_dir, f"ot_{demtype}_{south:.2f}_{west:.2f}.tif")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            feedback.pushInfo(f"  Reusing cached: {os.path.basename(out)}")
            return [out]

        url = (
            "https://portal.opentopography.org/API/globaldem"
            f"?demtype={demtype}"
            f"&south={south}&north={north}&west={west}&east={east}"
            f"&outputFormat=GTiff&API_Key={api_key}"
        )
        feedback.pushInfo(f"Downloading {demtype} from OpenTopography ...")
        feedback.setProgress(10)

        r = requests.get(url, stream=True, timeout=300)
        if r.status_code != 200:
            raise QgsProcessingException(
                f"OpenTopography returned HTTP {r.status_code}.\n"
                f"Check your API key and that the extent is within the DEM coverage."
            )

        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)

        feedback.pushInfo(f"  Downloaded: {os.path.getsize(out)/1e6:.1f} MB")
        feedback.setProgress(70)
        return [out]
