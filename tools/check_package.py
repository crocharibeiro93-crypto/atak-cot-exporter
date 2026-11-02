from pathlib import Path
import ast
import configparser

root = Path(__file__).resolve().parents[1]
plugin = root / "atak_cot_exporter"

required = [
    "__init__.py",
    "plugin.py",
    "metadata.txt",
    "icon.png",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
]

for filename in required:
    assert (plugin / filename).is_file(), filename

for filename in ("plugin.py", "__init__.py"):
    ast.parse(
        (plugin / filename).read_text(encoding="utf-8")
    )

config = configparser.ConfigParser()
config.read(plugin / "metadata.txt", encoding="utf-8")

assert config.has_section("general")

for key in (
    "name", "version", "description",
    "qgisMinimumVersion", "author",
    "repository", "icon"
):
    assert config.get("general", key).strip(), key

for key in ("author", "email", "repository"):
    value = config.get("general", key)
    assert "PREENCHER" not in value, (
        "Metadados incompletos: " + key
    )

print("Verificação estática concluída.")
