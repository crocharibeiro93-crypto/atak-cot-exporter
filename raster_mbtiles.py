
import os
import math
from pathlib import Path

from osgeo import gdal

from qgis.core import QgsProject, QgsRasterLayer

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QComboBox,
    QSpinBox,
    QLabel,
    QMessageBox,
    QFileDialog,
    QDialogButtonBox,
    QProgressDialog
)

BASE_RESOLUTION = 156543.03392804097


def progress_callback(dialog, start, end):
    def callback(fraction, message, data):
        position = start + int(
            fraction * (end - start)
        )

        dialog.setValue(position)
        QApplication.processEvents()

        return 0 if dialog.wasCanceled() else 1

    return callback


def prepare_raster(source):
    """
    Prepara raster RGB/RGBA de 8 bits.

    Suporta:
    - RGB Byte
    - RGBA Byte
    - Grayscale Byte
    - Paletted Byte

    Nao faz escalonamento automatico de rasters
    cientificos de 16/32 bits.
    """

    count = source.RasterCount

    if count not in (1, 3, 4):
        raise RuntimeError(
            "Raster com {} bandas. "
            "Prepara primeiro uma composicao RGB."
            .format(count)
        )

    for i in range(1, count + 1):
        band = source.GetRasterBand(i)

        if band.DataType != gdal.GDT_Byte:
            raise RuntimeError(
                "O raster nao e de 8 bits. "
                "Converte primeiro para RGB Byte, "
                "controlando os valores e a simbologia."
            )

    if count == 1:
        band = source.GetRasterBand(1)

        if band.GetColorTable() is not None:
            options = gdal.TranslateOptions(
                format="VRT",
                rgbExpand="rgb"
            )
        else:
            options = gdal.TranslateOptions(
                format="VRT",
                bandList=[1, 1, 1]
            )

        result = gdal.Translate(
            "",
            source,
            options=options
        )

        if result is None:
            raise RuntimeError(
                "Falha na conversao para RGB."
            )

        return result

    return source


def convert_raster(
    layer,
    output,
    min_zoom,
    max_zoom,
    tile_format,
    quality,
    parent
):
    gdal.UseExceptions()

    source_path = layer.source().split("|")[0]

    if not os.path.isfile(source_path):
        raise RuntimeError(
            "O raster tem de ser um ficheiro local."
        )

    if not layer.crs().isValid():
        raise RuntimeError(
            "A camada nao possui CRS valido."
        )

    if min_zoom > max_zoom:
        raise RuntimeError(
            "Zoom minimo superior ao zoom maximo."
        )

    source = gdal.Open(
        source_path,
        gdal.GA_ReadOnly
    )

    if source is None:
        raise RuntimeError(
            "Nao foi possivel abrir o TIFF."
        )

    if source.RasterCount == 4 and tile_format == "JPEG":
        raise RuntimeError(
            "O raster possui quatro bandas. "
            "Seleciona PNG para preservar o canal alfa."
        )

    progress = QProgressDialog(
        "A preparar raster...",
        "Cancelar",
        0,
        100,
        parent
    )

    progress.setWindowModality(
        Qt.WindowModality.WindowModal
    )
    progress.setMinimumDuration(0)
    progress.show()

    temporary = output + ".writing.mbtiles"

    if os.path.exists(temporary):
        os.remove(temporary)

    prepared = None
    preview = None
    warped = None
    mbtiles = None

    try:
        prepared = prepare_raster(source)

        # ----------------------------------------------
        # ESTIMAR RESOLUCAO NATIVA
        # ----------------------------------------------

        preview = gdal.Warp(
            "",
            prepared,
            options=gdal.WarpOptions(
                format="VRT",
                srcSRS=layer.crs().toWkt(),
                dstSRS="EPSG:3857",
                resampleAlg="bilinear"
            )
        )

        if preview is None:
            raise RuntimeError(
                "Falha ao calcular resolucao."
            )

        transform = preview.GetGeoTransform()

        native_resolution = max(
            abs(transform[1]),
            abs(transform[5])
        )

        if native_resolution <= 0:
            raise RuntimeError(
                "Resolucao raster invalida."
            )

        natural_zoom = math.floor(
            math.log2(
                BASE_RESOLUTION / native_resolution
            )
        )

        natural_zoom = max(
            0,
            min(22, natural_zoom)
        )

        effective_max = min(
            max_zoom,
            natural_zoom
        )

        effective_min = min(
            min_zoom,
            effective_max
        )

        target_resolution = (
            BASE_RESOLUTION / (2 ** effective_max)
        )

        preview = None

        # ----------------------------------------------
        # REPROJETAR PARA WEB MERCATOR
        # ----------------------------------------------

        progress.setLabelText(
            "A reprojetar para EPSG:3857..."
        )

        palette = (
            source.RasterCount == 1
            and source.GetRasterBand(1).GetColorTable()
            is not None
        )

        warped = gdal.Warp(
            "",
            prepared,
            options=gdal.WarpOptions(
                format="VRT",
                srcSRS=layer.crs().toWkt(),
                dstSRS="EPSG:3857",
                xRes=target_resolution,
                yRes=target_resolution,
                targetAlignedPixels=True,
                resampleAlg=(
                    "near" if palette else "bilinear"
                ),
                callback=progress_callback(
                    progress, 0, 25
                )
            )
        )

        if warped is None:
            raise RuntimeError(
                "Falha na reprojecao."
            )

        if progress.wasCanceled():
            raise RuntimeError(
                "Operacao cancelada."
            )

        estimated_tiles = (
            math.ceil(warped.RasterXSize / 256)
            * math.ceil(warped.RasterYSize / 256)
        )

        if estimated_tiles > 100000:
            raise RuntimeError(
                "Exportacao muito grande: aproximadamente "
                "{} tiles no zoom maximo. "
                "Reduz o zoom ou recorta o TIFF."
                .format(estimated_tiles)
            )

        # ----------------------------------------------
        # GERAR MBTILES
        # ----------------------------------------------

        progress.setLabelText(
            "A gerar mosaicos MBTiles..."
        )

        creation_options = [
            "TILE_FORMAT=" + tile_format
        ]

        if tile_format == "JPEG":
            creation_options.append(
                "QUALITY=" + str(quality)
            )

        mbtiles = gdal.Translate(
            temporary,
            warped,
            options=gdal.TranslateOptions(
                format="MBTiles",
                creationOptions=creation_options,
                callback=progress_callback(
                    progress, 25, 80
                )
            )
        )

        if mbtiles is None:
            raise RuntimeError(
                "Falha ao criar MBTiles."
            )

        mbtiles = None
        warped = None
        prepared = None

        # ----------------------------------------------
        # CRIAR NIVEIS DE ZOOM INFERIORES
        # ----------------------------------------------

        progress.setLabelText(
            "A criar piramide de zoom..."
        )

        levels = effective_max - effective_min

        if levels > 0:
            factors = [
                2 ** i
                for i in range(1, levels + 1)
            ]

            mbtiles = gdal.Open(
                temporary,
                gdal.GA_Update
            )

            if mbtiles is None:
                raise RuntimeError(
                    "Nao foi possivel abrir MBTiles "
                    "para gerar overviews."
                )

            status = mbtiles.BuildOverviews(
                "AVERAGE",
                factors,
                callback=progress_callback(
                    progress, 80, 100
                )
            )

            if status != 0:
                raise RuntimeError(
                    "Falha na criacao de overviews."
                )

            mbtiles = None

        if progress.wasCanceled():
            raise RuntimeError(
                "Operacao cancelada."
            )

        if not os.path.isfile(temporary):
            raise RuntimeError(
                "MBTiles nao foi produzido."
            )

        os.replace(temporary, output)

        progress.setValue(100)

        return {
            "size": os.path.getsize(output)
                    / (1024 * 1024),
            "min_zoom": effective_min,
            "max_zoom": effective_max
        }

    finally:
        mbtiles = None
        warped = None
        preview = None
        prepared = None
        source = None

        progress.close()

        if os.path.exists(temporary):
            os.remove(temporary)


def show_dialog(iface):
    layers = sorted(
        [
            layer
            for layer in QgsProject.instance().mapLayers().values()
            if (
                isinstance(layer, QgsRasterLayer)
                and layer.isValid()
                and layer.source().split("|")[0].lower()
                    .endswith((".tif", ".tiff"))
            )
        ],
        key=lambda layer: layer.name().lower()
    )

    if not layers:
        QMessageBox.warning(
            iface.mainWindow(),
            "ATAK MBTiles",
            "Abre primeiro um GeoTIFF no QGIS."
        )
        return

    dialog = QDialog(iface.mainWindow())

    dialog.setWindowTitle(
        "ATAK CoT Exporter 2.1.1 - MBTiles"
    )

    dialog.resize(460, 300)

    layout = QVBoxLayout(dialog)
    form = QFormLayout()

    raster_combo = QComboBox()

    for layer in layers:
        raster_combo.addItem(
            layer.name(),
            layer.id()
        )

    active = iface.activeLayer()

    if isinstance(active, QgsRasterLayer):
        index = raster_combo.findData(active.id())

        if index >= 0:
            raster_combo.setCurrentIndex(index)

    form.addRow("Raster:", raster_combo)

    min_zoom = QSpinBox()
    min_zoom.setRange(0, 22)
    min_zoom.setValue(9)

    max_zoom = QSpinBox()
    max_zoom.setRange(0, 22)
    max_zoom.setValue(16)

    form.addRow("Zoom minimo:", min_zoom)
    form.addRow("Zoom maximo:", max_zoom)

    tile_format = QComboBox()

    tile_format.addItem(
        "JPEG - compacto", "JPEG"
    )
    tile_format.addItem(
        "PNG - sem perdas", "PNG"
    )

    form.addRow("Formato:", tile_format)

    quality = QSpinBox()
    quality.setRange(50, 100)
    quality.setValue(80)
    quality.setSuffix("%")

    form.addRow("Qualidade JPEG:", quality)

    tile_format.currentIndexChanged.connect(
        lambda _: quality.setEnabled(
            tile_format.currentData() == "JPEG"
        )
    )

    layout.addLayout(form)

    note = QLabel(
        "Aceita rasters de imagem Byte (8 bits). "
        "O zoom maximo respeita a resolucao original. "
        "Esta conversao utiliza as bandas do TIFF "
        "e nao necessariamente a simbologia do QGIS."
    )
    note.setWordWrap(True)

    layout.addWidget(note)

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel
    )

    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)

    layout.addWidget(buttons)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return

    layer = QgsProject.instance().mapLayer(
        raster_combo.currentData()
    )

    if layer is None:
        return

    source_path = Path(
        layer.source().split("|")[0]
    )

    suggested = str(
        source_path.with_name(
            source_path.stem + "_ATAK.mbtiles"
        )
    )

    output, _ = QFileDialog.getSaveFileName(
        iface.mainWindow(),
        "Guardar MBTiles",
        suggested,
        "MBTiles (*.mbtiles)"
    )

    if not output:
        return

    if not output.lower().endswith(".mbtiles"):
        output += ".mbtiles"

    try:
        result = convert_raster(
            layer,
            output,
            min_zoom.value(),
            max_zoom.value(),
            tile_format.currentData(),
            quality.value(),
            iface.mainWindow()
        )

        QMessageBox.information(
            iface.mainWindow(),
            "Exportacao concluida",
            (
                "MBTiles criado:\n{}\n\n"
                "Tamanho: {:.1f} MB\n"
                "Zoom aproximado: {} a {}\n\n"
                "Confirma a importacao no ATAK."
            ).format(
                output,
                result["size"],
                result["min_zoom"],
                result["max_zoom"]
            )
        )

    except (RuntimeError, ValueError, OSError) as error:
        QMessageBox.critical(
            iface.mainWindow(),
            "Erro na exportacao MBTiles",
            str(error)
        )
