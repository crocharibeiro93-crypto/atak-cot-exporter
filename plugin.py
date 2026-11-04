
import os
import re
import json
import uuid
import zipfile
import hashlib
from datetime import datetime, timedelta, timezone
from . import safe_xml as ET
from qgis.core import QgsMessageLog, Qgis

from qgis.PyQt.QtCore import (
    Qt, QSettings, QSize, QBuffer, QIODevice
)
from qgis.PyQt.QtWidgets import (
    QAction, QApplication, QDialog, QVBoxLayout,
    QHBoxLayout, QFormLayout, QLabel, QComboBox,
    QLineEdit, QPushButton, QCheckBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QFileDialog,
    QMessageBox, QDialogButtonBox, QProgressDialog
)
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsGeometry,
    QgsWkbTypes, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRenderContext,
    QgsExpressionContextUtils, QgsSymbolLayerUtils
)

VERSION = "2.1.1"
SETTINGS_ROOT = "ATAKCoTExporterV2/"

_WARNINGS_LOGGED = set()


def _log_optional(exc):
    message = "{}: {}".format(
        type(exc).__name__, exc
    )

    if message not in _WARNINGS_LOGGED:
        _WARNINGS_LOGGED.add(message)
        QgsMessageLog.logMessage(
            message,
            "ATAK CoT Exporter",
            Qgis.MessageLevel.Warning
        )




def setting(key, default=None):
    return QSettings().value(SETTINGS_ROOT + key, default)


def save_setting(key, value):
    QSettings().setValue(SETTINGS_ROOT + key, value)


def flag(key, default=False):
    return str(setting(key, default)).lower() in ("true", "1")


def slug(value):
    return re.sub(
        r"[^A-Za-z0-9_-]+", "_", str(value)
    ).strip("_") or "ITEM"


def xml_bytes(root):
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="utf-8")
    )


def cot_time(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def argb(color, opacity=1.0):
    if color is None or not color.isValid():
        return -16776961

    a = max(0, min(255, round(color.alpha() * opacity)))
    value = (
        (a << 24)
        | (color.red() << 16)
        | (color.green() << 8)
        | color.blue()
    )
    return value - 2**32 if value >= 2**31 else value


def style_for_symbol(symbol, geometry_type, context):
    result = {
        "stroke": -16776961,
        "fill": 0,
        "point": -16776961,
        "weight": "3",
        "line_style": "solid"
    }

    if symbol is None:
        return result

    try:
        opacity = float(symbol.opacity())
    except Exception:
        opacity = 1.0

    try:
        value = argb(symbol.color(), opacity)
        result["point"] = value
        result["stroke"] = value
        if geometry_type == QgsWkbTypes.GeometryType.PolygonGeometry:
            result["fill"] = value
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        _log_optional(exc)

    if symbol.symbolLayerCount() == 0:
        return result

    symbol_layer = symbol.symbolLayer(0)

    try:
        color = symbol_layer.color()
        if color.isValid():
            value = argb(color, opacity)
            result["point"] = value
            if geometry_type == QgsWkbTypes.GeometryType.LineGeometry:
                result["stroke"] = value
            elif geometry_type == QgsWkbTypes.GeometryType.PolygonGeometry:
                result["fill"] = value
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        _log_optional(exc)

    try:
        color = symbol_layer.strokeColor()
        if color.isValid():
            result["stroke"] = argb(color, opacity)
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        _log_optional(exc)

    if geometry_type != QgsWkbTypes.GeometryType.PointGeometry:
        if geometry_type == QgsWkbTypes.GeometryType.LineGeometry:
            width_attr = "width"
            unit_attr = "widthUnit"
            style_attr = "penStyle"
        else:
            width_attr = "strokeWidth"
            unit_attr = "strokeWidthUnit"
            style_attr = "strokeStyle"

        try:
            width = float(getattr(symbol_layer, width_attr)())
            unit = getattr(symbol_layer, unit_attr)()
            pixels = context.convertToPainterUnits(width, unit)
            result["weight"] = str(
                round(max(1.0, min(16.0, pixels)), 2)
            )
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            _log_optional(exc)

        try:
            pen = getattr(symbol_layer, style_attr)()
            if pen == Qt.PenStyle.DotLine:
                result["line_style"] = "dotted"
            elif pen in (
                Qt.PenStyle.DashLine, Qt.PenStyle.DashDotLine,
                Qt.PenStyle.DashDotDotLine
            ):
                result["line_style"] = "dashed"
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            _log_optional(exc)

    return result


def symbol_png(symbol):
    if symbol is None:
        return None

    try:
        pixmap = QgsSymbolLayerUtils.symbolPreviewPixmap(
            symbol, QSize(64, 64)
        )
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        success = pixmap.save(buffer, "PNG")
        data = bytes(buffer.data())
        buffer.close()
        return data if success and data else None
    except Exception:
        return None


def label_for(feature, layer, field):
    candidates = [field] if field else [
        "Name", "name", "nome"
    ]
    available = layer.fields().names()

    for candidate in candidates:
        if not candidate:
            continue
        for actual in available:
            if actual.lower() != candidate.lower():
                continue
            value = feature[actual]
            if value is not None:
                text = str(value).strip()
                if text and text.upper() not in ("NULL", "NONE"):
                    return text

    return "{} {}".format(layer.name(), feature.id())


def same_point(a, b):
    return a.x() == b.x() and a.y() == b.y()


def polygon_parts(poly):
    if poly.isMultipart():
        return poly.asMultiPolygon()
    return [poly.asPolygon()]


def polygon_without_holes(poly):
    result = []
    for rings in polygon_parts(poly):
        if not rings:
            continue
        if len(rings) > 1:
            raise RuntimeError(
                "A decomposicao deixou um anel interior."
            )
        if len(rings[0]) >= 4:
            result.append(list(rings[0]))
    return result


def decompose_holes(rings):
    """
    Triangula os vertices do poligono e intersecta as
    pecas com a geometria original. Nao exporta pecas
    que ainda apresentem aneis interiores.
    """
    original = QgsGeometry.fromPolygonXY(rings)

    if not original.isGeosValid():
        raise RuntimeError(
            "Poligono com buracos invalido. "
            "Executa Corrigir Geometrias no QGIS."
        )

    triangles = original.delaunayTriangulation()

    if triangles.isEmpty():
        raise RuntimeError("Falha na triangulacao.")

    try:
        triangle_geometries = triangles.asGeometryCollection()
    except Exception:
        triangle_geometries = []

    if not triangle_geometries:
        triangle_geometries = [
            QgsGeometry.fromPolygonXY(p)
            for p in triangles.asMultiPolygon()
        ]

    results = []
    area_total = 0.0

    for triangle in triangle_geometries:
        if triangle.isEmpty():
            continue

        clipped = triangle.intersection(original)
        if clipped.isEmpty():
            continue

        parts = polygon_without_holes(clipped)

        for part in parts:
            piece = QgsGeometry.fromPolygonXY([part])
            area_total += piece.area()
            results.append(part)

    if not results:
        raise RuntimeError(
            "Nao foi possivel decompor o poligono."
        )

    expected = original.area()
    if expected > 0:
        difference = abs(area_total - expected) / expected
        if difference > 0.001:
            raise RuntimeError(
                "A decomposicao nao preservou a area "
                "(erro relativo {:.2%}).".format(difference)
            )

    return results


def geometry_parts(geometry, split_lines, fix_holes):
    kind = QgsWkbTypes.geometryType(geometry.wkbType())

    if kind == QgsWkbTypes.GeometryType.PointGeometry:
        points = (
            geometry.asMultiPoint()
            if geometry.isMultipart()
            else [geometry.asPoint()]
        )
        return [("point", [p]) for p in points], 0, 0

    if kind == QgsWkbTypes.GeometryType.LineGeometry:
        lines = (
            geometry.asMultiPolyline()
            if geometry.isMultipart()
            else [geometry.asPolyline()]
        )
        lines = [list(p) for p in lines if len(p) >= 2]

        if split_lines or len(lines) <= 1:
            return [("line", p) for p in lines], 0, 0

        joined = list(lines[0])
        disconnected = 0

        for part in lines[1:]:
            if same_point(joined[-1], part[-1]):
                part.reverse()

            if same_point(joined[-1], part[0]):
                joined.extend(part[1:])
            else:
                joined.extend(part)
                disconnected += 1

        return [("line", joined)], disconnected, 0

    if kind == QgsWkbTypes.GeometryType.PolygonGeometry:
        result = []
        hole_count = 0

        for rings in polygon_parts(geometry):
            if not rings:
                continue

            holes = max(0, len(rings) - 1)
            hole_count += holes

            if holes and fix_holes:
                pieces = decompose_holes(rings)
                result.extend(
                    ("polygon", part) for part in pieces
                )
            else:
                outer = list(rings[0])
                if not same_point(outer[0], outer[-1]):
                    outer.append(outer[0])
                if len(outer) >= 4:
                    result.append(("polygon", outer))

        return result, 0, hole_count

    return [], 0, 0


def make_cot(uid, name, points, kind, style):
    now = datetime.now(timezone.utc)
    stale = now + timedelta(days=7)

    lat = sum(p.y() for p in points) / len(points)
    lon = sum(p.x() for p in points) / len(points)

    event = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": "a-u-G" if kind == "point" else "u-d-f",
        "time": cot_time(now),
        "start": cot_time(now),
        "stale": cot_time(stale),
        "how": "h-e",
        "access": "Undefined"
    })

    ET.SubElement(event, "point", {
        "lat": "{:.9f}".format(lat),
        "lon": "{:.9f}".format(lon),
        "hae": "9999999",
        "ce": "9999999",
        "le": "9999999"
    })

    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {"callsign": name})

    remarks = ET.SubElement(detail, "remarks")
    remarks.text = name

    if kind == "point":
        ET.SubElement(detail, "archive")
        ET.SubElement(detail, "usericon", {
            "iconsetpath": "COT_MAPPING_2525C/a-u/a-u-G"
        })
        ET.SubElement(detail, "color", {
            "argb": str(style["point"])
        })

    else:
        ET.SubElement(detail, "strokeColor", {
            "value": str(style["stroke"])
        })
        ET.SubElement(detail, "fillColor", {
            "value": str(
                style["fill"] if kind == "polygon" else 0
            )
        })
        ET.SubElement(detail, "clamped", {"value": "True"})
        ET.SubElement(detail, "height", {"value": "0.00"})
        ET.SubElement(detail, "height_unit", {"value": "1"})
        ET.SubElement(detail, "archive")
        ET.SubElement(detail, "strokeStyle", {
            "value": style["line_style"]
        })

        for point in points:
            ET.SubElement(detail, "link", {
                "point": "{:.9f},{:.9f}".format(
                    point.y(), point.x()
                )
            })

        ET.SubElement(detail, "strokeWeight", {
            "value": style["weight"]
        })

    return (
        b"<?persistorRestore True?>\n"
        + ET.tostring(event, encoding="utf-8")
    )


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def qualified(root, name):
    if "}" in root.tag:
        return root.tag.split("}")[0] + "}" + name
    return name


def child_named(parent, name):
    for child in parent:
        if local_name(child.tag) == name:
            return child
    return None


def new_manifest(name):
    root = ET.Element("MissionPackageManifest", {
        "version": "2"
    })
    config = ET.SubElement(root, "Configuration")

    for key, value in (
        ("name", name),
        ("uid", str(uuid.uuid4())),
        ("onReceiveImport", "true"),
        ("onReceiveDelete", "false")
    ):
        ET.SubElement(config, "Parameter", {
            "name": key,
            "value": value
        })

    ET.SubElement(root, "Contents")
    return root


def add_entries(root, entries):
    contents = child_named(root, "Contents")
    if contents is None:
        raise RuntimeError(
            "Manifest sem secao Contents."
        )

    for entry in entries:
        node = ET.SubElement(
            contents, qualified(root, "Content"), {
                "ignore": "false",
                "zipEntry": entry["path"]
            }
        )

        for key, value in (
            ("visible", "true"),
            ("qgis_layer_name", entry["layer"]),
            ("qgis_feature_id", entry["feature"]),
            ("qgis_exporter_version", VERSION)
        ):
            ET.SubElement(
                node, qualified(root, "Parameter"), {
                    "name": key,
                    "value": str(value)
                }
            )


def unique_path(path):
    if not os.path.exists(path):
        return path

    stem, extension = os.path.splitext(path)
    index = 2
    while os.path.exists(
        "{}_{}{}".format(stem, index, extension)
    ):
        index += 1

    return "{}_{}{}".format(stem, index, extension)


def export_layer(
    layer, field, package, entries, config, stats, tick,
    icon_cache
):
    if not layer.crs().isValid():
        raise RuntimeError(
            "{}: camada sem CRS valido.".format(layer.name())
        )

    target = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = None

    if layer.crs() != target:
        transform = QgsCoordinateTransform(
            layer.crs(), target,
            QgsProject.instance().transformContext()
        )

    context = QgsRenderContext.fromMapSettings(
        config["iface"].mapCanvas().mapSettings()
    )
    expression = context.expressionContext()
    expression.appendScope(
        QgsExpressionContextUtils.layerScope(layer)
    )
    expression.setFields(layer.fields())

    renderer = layer.renderer()
    if renderer is None:
        raise RuntimeError(
            "{} sem simbologia.".format(layer.name())
        )

    renderer.startRender(context, layer.fields())

    try:
        for feature in layer.getFeatures():
            tick(layer.name())
            geometry_original = feature.geometry()

            if (
                geometry_original is None
                or geometry_original.isNull()
                or geometry_original.isEmpty()
            ):
                stats["skipped"] += 1
                continue

            expression.setFeature(feature)

            try:
                symbol = renderer.symbolForFeature(
                    feature, context
                )
            except Exception:
                symbol = None

            geom_type = QgsWkbTypes.geometryType(
                geometry_original.wkbType()
            )

            style = style_for_symbol(
                symbol, geom_type, context
            )

            geometry = QgsGeometry(geometry_original)

            if transform is not None:
                result = geometry.transform(transform)
                if result != 0:
                    raise RuntimeError(
                        "Falha na reprojecao de {} / feature {}"
                        .format(layer.name(), feature.id())
                    )

            parts, disconnected, holes = geometry_parts(
                geometry,
                config["split_lines"],
                config["fix_holes"]
            )

            stats["disconnected"] += disconnected
            stats["holes"] += holes

            name = label_for(feature, layer, field)

            if not parts:
                stats["skipped"] += 1
                continue

            if config["icons"] and geom_type == QgsWkbTypes.GeometryType.PointGeometry:
                try:
                    symbol_desc = symbol.dump() if symbol else ""
                    symbol_key = hashlib.sha256(
                        (layer.name() + symbol_desc
                         + str(style["point"])).encode("utf-8")
                    ).hexdigest()[:16]

                    icon_path = (
                        "qgis_symbol_previews/"
                        + slug(layer.name())
                        + "__" + symbol_key + ".png"
                    )

                    if icon_path not in icon_cache:
                        png = symbol_png(symbol)
                        if png:
                            package.writestr(icon_path, png)
                            icon_cache.add(icon_path)
                            stats["icons"] += 1
                except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
                    _log_optional(exc)

            for index, (kind, points) in enumerate(parts):
                if len(points) < (1 if kind == "point" else 2):
                    stats["skipped"] += 1
                    continue

                if not all(
                    -90 <= p.y() <= 90 and -180 <= p.x() <= 180
                    for p in points
                ):
                    stats["skipped"] += 1
                    continue

                label = name
                if len(parts) > 1:
                    label = "{} - parte {}".format(
                        name, index + 1
                    )

                identity = "|".join([
                    layer.source(),
                    str(feature.id()),
                    kind,
                    str(index)
                ])

                uid = str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "ATAKCoTExporterV2|" + identity
                ))

                filename = "{}__{}__{}.cot".format(
                    slug(layer.name()), kind, uid
                )
                path = "cot/" + filename

                package.writestr(
                    path, make_cot(
                        uid, label, points, kind, style
                    )
                )

                entries.append({
                    "path": path,
                    "layer": layer.name(),
                    "feature": str(feature.id())
                })
                stats["cot"] += 1

    finally:
        renderer.stopRender(context)


def make_new_package(path, layers, config, tick):
    temporary = path + ".writing"
    entries = []
    stats = {
        "cot": 0, "holes": 0,
        "disconnected": 0, "skipped": 0, "icons": 0
    }

    try:
        with zipfile.ZipFile(
            temporary, "w", zipfile.ZIP_DEFLATED
        ) as package:
            cache = set()

            for layer, field in layers:
                export_layer(
                    layer, field, package,
                    entries, config, stats, tick, cache
                )

            if not entries:
                raise RuntimeError(
                    "Nenhum CoT valido gerado."
                )

            manifest = new_manifest(
                os.path.splitext(
                    os.path.basename(path)
                )[0]
            )
            add_entries(manifest, entries)

            package.writestr(
                "MANIFEST/manifest.xml",
                xml_bytes(manifest)
            )

        os.replace(temporary, path)
        return stats

    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def old_layer_entries(manifest, layer_name):
    contents = child_named(manifest, "Contents")
    if contents is None:
        raise RuntimeError("Manifest sem Contents.")

    matches = set()
    clean = slug(layer_name).lower()

    for node in list(contents):
        if local_name(node.tag) != "Content":
            continue

        path = node.get("zipEntry", "")
        basename = path.replace("\\", "/").split("/")[-1]
        lower_name = basename.lower()

        tagged = False

        for param in node:
            if (
                local_name(param.tag) == "Parameter"
                and param.get("name") == "qgis_layer_name"
                and param.get("value", "").lower()
                    == layer_name.lower()
            ):
                tagged = True
                break

        legacy_match = (
            lower_name.startswith(clean + "_")
            and lower_name.endswith(".cot")
        )

        if tagged or legacy_match:
            matches.add(path)

    return matches


def update_package(
    original, destination, layer, field, config, tick,
    confirm
):
    if os.path.abspath(original) == os.path.abspath(destination):
        raise RuntimeError(
            "O ZIP original nao pode ser substituido diretamente."
        )

    temporary = destination + ".writing"
    stats = {
        "cot": 0, "holes": 0,
        "disconnected": 0, "skipped": 0, "icons": 0
    }

    try:
        with zipfile.ZipFile(original, "r") as source:
            manifest_name = next(
                (
                    name for name in source.namelist()
                    if name.upper() == "MANIFEST/MANIFEST.XML"
                ),
                None
            )

            if manifest_name is None:
                raise RuntimeError(
                    "MANIFEST/manifest.xml nao encontrado."
                )

            info = source.getinfo(manifest_name)

            if info.file_size > 32 * 1024 * 1024:
                raise RuntimeError(
                    "Manifest XML demasiado grande."
                )

            manifest = ET.fromstring(
                source.read(manifest_name)
            )

            old_entries = old_layer_entries(
                manifest, layer.name()
            )

            if not old_entries:
                raise RuntimeError(
                    "Nao foram identificados CoT antigos "
                    "desta camada. Atualizacao cancelada "
                    "para evitar duplicacoes."
                )

            if not confirm(len(old_entries)):
                raise RuntimeError(
                    "Atualizacao cancelada."
                )

            contents = child_named(manifest, "Contents")
            for content in list(contents):
                if content.get("zipEntry", "") in old_entries:
                    contents.remove(content)

            layer_prefix = (
                "qgis_symbol_previews/"
                + slug(layer.name()) + "__"
            )

            with zipfile.ZipFile(
                temporary, "w", zipfile.ZIP_DEFLATED
            ) as output:

                for item in source.infolist():
                    name = item.filename

                    if (
                        name == manifest_name
                        or name in old_entries
                        or name.startswith(layer_prefix)
                    ):
                        continue

                    output.writestr(
                        item, source.read(name)
                    )

                new_entries = []
                export_layer(
                    layer, field, output, new_entries,
                    config, stats, tick, set()
                )

                if not new_entries:
                    raise RuntimeError(
                        "Nenhum Road/elemento novo foi gerado."
                    )

                add_entries(manifest, new_entries)

                output.writestr(
                    manifest_name,
                    xml_bytes(manifest)
                )

        os.replace(temporary, destination)
        stats["removed"] = len(old_entries)
        return stats

    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


class ATAKPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None

    def initGui(self):
        self.action = QAction(
            "ATAK CoT Exporter 2.0",
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(
            "ATAK CoT", self.action
        )
        self.iface.addToolBarIcon(self.action)

        self.raster_action = QAction(
            "Exportar raster para MBTiles",
            self.iface.mainWindow()
        )

        self.raster_action.triggered.connect(
            self.export_mbtiles
        )

        self.iface.addPluginToMenu(
            "ATAK CoT",
            self.raster_action
        )


    def unload(self):
        if getattr(self, "raster_action", None):
            self.iface.removePluginMenu(
                "ATAK CoT", self.raster_action
            )

        if self.action:
            self.iface.removePluginMenu(
                "ATAK CoT", self.action
            )
            self.iface.removeToolBarIcon(self.action)

    def export_mbtiles(self):
        from .raster_mbtiles import show_dialog
        show_dialog(self.iface)

    def run(self):
        layers = sorted(
            [
                layer
                for layer in QgsProject.instance().mapLayers().values()
                if (
                    isinstance(layer, QgsVectorLayer)
                    and layer.isValid()
                    and layer.isSpatial()
                )
            ],
            key=lambda layer: layer.name().lower()
        )

        if not layers:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ATAK",
                "Abre primeiro uma camada vetorial."
            )
            return

        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle(
            "ATAK CoT Exporter 2.0"
        )
        dialog.resize(750, 550)

        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel(
            "Seleciona as camadas e o campo da label:"
        ))

        table = QTableWidget(len(layers), 3)
        table.setHorizontalHeaderLabels([
            "Exportar", "Label / callsign", "Geometria"
        ])
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        try:
            selected_before = json.loads(
                str(setting("selected", "[]"))
            )
        except Exception:
            selected_before = []

        try:
            labels_before = json.loads(
                str(setting("labels", "{}"))
            )
        except Exception:
            labels_before = {}

        field_boxes = []

        for row, layer in enumerate(layers):
            item = QTableWidgetItem(layer.name())
            item.setFlags(
                item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            )

            default_checked = (
                layer.name() in selected_before
                or (
                    not selected_before
                    and layer == self.iface.activeLayer()
                )
            )

            item.setCheckState(
                Qt.CheckState.Checked if default_checked else Qt.CheckState.Unchecked
            )
            table.setItem(row, 0, item)

            combo = QComboBox()
            combo.addItem("Automático", "")

            for field in layer.fields().names():
                combo.addItem(field, field)

            stored = labels_before.get(layer.name(), "")
            index = combo.findData(stored)
            if index >= 0:
                combo.setCurrentIndex(index)

            table.setCellWidget(row, 1, combo)
            field_boxes.append(combo)

            geometry_name = QgsWkbTypes.displayString(
                layer.wkbType()
            )
            table.setItem(
                row, 2, QTableWidgetItem(geometry_name)
            )

        layout.addWidget(table)

        select_row = QHBoxLayout()
        all_button = QPushButton("Selecionar todas")
        none_button = QPushButton("Limpar seleção")

        all_button.clicked.connect(
            lambda: [
                table.item(r, 0).setCheckState(Qt.CheckState.Checked)
                for r in range(table.rowCount())
            ]
        )
        none_button.clicked.connect(
            lambda: [
                table.item(r, 0).setCheckState(Qt.CheckState.Unchecked)
                for r in range(table.rowCount())
            ]
        )

        select_row.addWidget(all_button)
        select_row.addWidget(none_button)
        layout.addLayout(select_row)

        form = QFormLayout()

        mode = QComboBox()
        mode.addItem(
            "Várias camadas num único Data Package", "combined"
        )
        mode.addItem(
            "Um Data Package por camada", "separate"
        )
        mode.addItem(
            "Atualizar camada num Data Package existente", "update"
        )

        index = mode.findData(
            setting("mode", "combined")
        )
        if index >= 0:
            mode.setCurrentIndex(index)

        form.addRow("Modo:", mode)

        package_name = QLineEdit(
            str(setting("package_name", "ATAK_EXPORT"))
        )
        form.addRow("Nome do pacote:", package_name)

        folder = QLineEdit(
            str(setting(
                "folder",
                QgsProject.instance().homePath()
                or os.path.expanduser("~")
            ))
        )

        browse_folder = QPushButton("Procurar...")
        folder_row = QHBoxLayout()
        folder_row.addWidget(folder)
        folder_row.addWidget(browse_folder)

        def choose_folder():
            path = QFileDialog.getExistingDirectory(
                dialog, "Pasta de saída", folder.text()
            )
            if path:
                folder.setText(path)

        browse_folder.clicked.connect(choose_folder)
        form.addRow("Pasta de saída:", folder_row)

        existing = QLineEdit(
            str(setting("existing", ""))
        )
        browse_zip = QPushButton("Procurar...")
        zip_row = QHBoxLayout()
        zip_row.addWidget(existing)
        zip_row.addWidget(browse_zip)

        def choose_zip():
            path, _ = QFileDialog.getOpenFileName(
                dialog,
                "Selecionar Data Package existente",
                existing.text() or folder.text(),
                "ZIP (*.zip)"
            )
            if path:
                existing.setText(path)

        browse_zip.clicked.connect(choose_zip)
        form.addRow("ZIP existente:", zip_row)
        layout.addLayout(form)

        split_lines = QCheckBox(
            "Separar partes de linhas multipartes"
        )
        split_lines.setChecked(flag("split_lines", False))

        fix_holes = QCheckBox(
            "Decompor polígonos com buracos em partes simples"
        )
        fix_holes.setChecked(flag("fix_holes", False))

        icons = QCheckBox(
            "Incluir PNG de pré-visualização dos símbolos de pontos"
        )
        icons.setChecked(flag("icons", True))

        layout.addWidget(split_lines)
        layout.addWidget(fix_holes)
        layout.addWidget(icons)

        note = QLabel(
            "Os PNG são anexos de referência; não substituem "
            "um iconset ATAK. A decomposição de polígonos pode "
            "gerar muitos CoT adicionais."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        def mode_changed():
            is_update = mode.currentData() == "update"
            existing.setEnabled(is_update)
            browse_zip.setEnabled(is_update)
            package_name.setEnabled(not is_update)

        mode.currentIndexChanged.connect(
            lambda _: mode_changed()
        )
        mode_changed()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        chosen = []
        labels_saved = {}

        for row, layer in enumerate(layers):
            field = field_boxes[row].currentData()
            labels_saved[layer.name()] = field

            if table.item(row, 0).checkState() == Qt.CheckState.Checked:
                chosen.append((layer, field))

        if not chosen:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ATAK",
                "Seleciona pelo menos uma camada."
            )
            return

        selected_mode = mode.currentData()

        if selected_mode == "update" and len(chosen) != 1:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ATAK",
                "Para atualizar um pacote, seleciona "
                "apenas uma camada."
            )
            return

        if selected_mode != "update":
            names = [layer.name() for layer, _ in chosen]
            if len(names) != len(set(names)):
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "ATAK",
                    "Existem camadas selecionadas com nomes "
                    "iguais. Renomeia-as antes de exportar."
                )
                return

        directory = folder.text().strip()

        if not os.path.isdir(directory):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ATAK",
                "A pasta de saída não existe."
            )
            return

        original = existing.text().strip()

        if selected_mode == "update" and not os.path.isfile(original):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "ATAK",
                "Seleciona um ZIP existente."
            )
            return

        save_setting(
            "selected", json.dumps(
                [layer.name() for layer, _ in chosen]
            )
        )
        save_setting("labels", json.dumps(labels_saved))
        save_setting("mode", selected_mode)
        save_setting("folder", directory)
        save_setting("existing", original)
        save_setting("package_name", package_name.text())
        save_setting("split_lines", split_lines.isChecked())
        save_setting("fix_holes", fix_holes.isChecked())
        save_setting("icons", icons.isChecked())

        config = {
            "iface": self.iface,
            "split_lines": split_lines.isChecked(),
            "fix_holes": fix_holes.isChecked(),
            "icons": icons.isChecked()
        }

        total = sum(
            max(0, layer.featureCount())
            for layer, _ in chosen
        )

        progress = QProgressDialog(
            "A exportar para ATAK...",
            "Cancelar",
            0, max(1, total),
            self.iface.mainWindow()
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        state = {"processed": 0}

        def tick(name):
            state["processed"] += 1

            if state["processed"] % 50 == 0:
                progress.setLabelText(
                    "{}: {} features processadas"
                    .format(name, state["processed"])
                )
                progress.setValue(
                    min(state["processed"], progress.maximum())
                )
                QApplication.processEvents()

            if progress.wasCanceled():
                raise RuntimeError(
                    "Exportação cancelada pelo utilizador."
                )

        def confirm_removal(count):
            answer = QMessageBox.question(
                self.iface.mainWindow(),
                "Confirmar atualização",
                (
                    "Foram identificados {} CoT antigos "
                    "desta camada.\n\n"
                    "Queres substituí-los? O ZIP original "
                    "será preservado."
                ).format(count),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            return answer == QMessageBox.StandardButton.Yes

        reports = []

        try:
            if selected_mode == "combined":
                filename = slug(
                    package_name.text() or "ATAK_EXPORT"
                ) + ".zip"

                path = unique_path(
                    os.path.join(directory, filename)
                )

                stats = make_new_package(
                    path, chosen, config, tick
                )
                reports.append((path, stats))

            elif selected_mode == "separate":
                for layer, field in chosen:
                    path = unique_path(os.path.join(
                        directory,
                        slug(layer.name()) + "_ATAK.zip"
                    ))
                    stats = make_new_package(
                        path, [(layer, field)], config, tick
                    )
                    reports.append((path, stats))

            else:
                layer, field = chosen[0]
                base = os.path.splitext(
                    os.path.basename(original)
                )[0]

                path = unique_path(os.path.join(
                    directory, base + "_UPDATED.zip"
                ))

                stats = update_package(
                    original, path, layer, field,
                    config, tick, confirm_removal
                )
                reports.append((path, stats))

            progress.setValue(progress.maximum())

        except Exception as error:
            progress.close()
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Erro na exportação",
                str(error)
            )
            return

        finally:
            progress.close()

        lines = []

        for path, stats in reports:
            lines.append(
                "{}\n  CoT novos: {}\n"
                "  CoT antigos substituídos: {}\n"
                "  PNG: {}\n"
                "  Anéis interiores encontrados: {}\n"
                "  Ligações multipartes: {}\n"
                "  Elementos ignorados: {}"
                .format(
                    path,
                    stats["cot"],
                    stats.get("removed", 0),
                    stats["icons"],
                    stats["holes"],
                    stats["disconnected"],
                    stats["skipped"]
                )
            )

        QMessageBox.information(
            self.iface.mainWindow(),
            "ATAK — Exportação concluída",
            "\n\n".join(lines)
        )
