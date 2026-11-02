# ATAK CoT Exporter

QGIS plugin for exporting vector layers to Cursor on Target
(CoT) events and ATAK Data Packages.

## Features

- Export points, lines and polygons
- Export multiple QGIS layers into one Data Package
- Generate an individual package for each layer
- Update a selected layer in an existing Data Package
- Convert compatible QGIS symbol colors and line styles
- Use attribute values for CoT callsigns
- Transform layer coordinates to EPSG:4326
- Generate MANIFEST/manifest.xml and CoT entries
- Include optional PNG previews of point symbols
- Remember export settings between sessions
- Optional experimental polygon-hole decomposition

## Installation

In QGIS:

1. Open Plugins > Manage and Install Plugins
2. Select Install from ZIP
3. Choose the downloaded plugin ZIP
4. Install and enable ATAK CoT Exporter

## Usage

1. Open vector layers in QGIS.
2. Apply the intended QGIS symbology.
3. Open Plugins > ATAK CoT > ATAK CoT Exporter.
4. Select the layers and label fields.
5. Choose the export mode.
6. Choose an output folder.
7. Export and test the resulting ZIP in ATAK.

## Export modes

### Combined package
Export several selected layers to one ZIP.

### Separate packages
Create one ZIP per selected layer.

### Update package
Replace recognized CoT entries for one selected layer
inside a new copy of an existing Data Package.

The original ZIP is preserved.

## Known limitations

QGIS symbology cannot always be reproduced identically
in ATAK. SVG symbols, complex multi-layer symbols, patterns
and labeling expressions may not have direct CoT equivalents.

Exported PNG symbol previews are not automatically installed
as ATAK iconsets.

Multipart lines may require splitting to avoid artificial
connections between disconnected segments.

Polygon-hole decomposition is experimental. Verify exported
geometry against the original QGIS data.

The update mechanism depends on recognizing existing CoT
entries for the selected layer. Always review the replacement
count before confirming.

## Compatibility

Built for QGIS 3.x with PyQGIS and the bundled PyQt library.
The declared minimum is QGIS 3.16; runtime compatibility
still requires testing.

## Privacy

The exporter processes layer information locally.
Review Data Packages before distributing them, especially
when they contain sensitive geographical information.

## License

MIT License. See LICENSE.

## Support

Use the project's GitHub Issues page to report problems.
