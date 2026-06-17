#!/usr/bin/env python3
"""Build all OpsDB plugin packages into distributable zip files.

Usage:
    python3 build_plugins.py

Output:
    dist/{plugin_id}-{version}.zip

Each zip contains:
    manifest.json
    collectors.py
    icon.svg         (if present in the plugin directory)
"""

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"


def build_plugin(plugin_dir: Path) -> Path:
    manifest_path = plugin_dir / "manifest.json"
    collectors_path = plugin_dir / "collectors.py"

    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest.json in {plugin_dir}")
    if not collectors_path.exists():
        raise FileNotFoundError(f"Missing collectors.py in {plugin_dir}")

    manifest = json.loads(manifest_path.read_text())
    plugin_id = plugin_dir.name
    version = manifest.get("version", "1.0.0")

    DIST.mkdir(parents=True, exist_ok=True)
    zip_path = DIST / f"{plugin_id}-{version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(manifest_path, "manifest.json")
        zf.write(collectors_path, "collectors.py")
        icon_path = plugin_dir / "icon.svg"
        if icon_path.exists():
            zf.write(icon_path, "icon.svg")

    return zip_path


def main() -> None:
    plugins = sorted(
        d for d in ROOT.iterdir()
        if d.is_dir() and not d.name.startswith("_") and d.name != "dist"
    )

    if not plugins:
        print("No plugin directories found.", file=sys.stderr)
        sys.exit(1)

    print(f"Building {len(plugins)} plugin(s) -> {DIST}/")
    for plugin_dir in plugins:
        try:
            zip_path = build_plugin(plugin_dir)
            size_kb = zip_path.stat().st_size // 1024
            print(f"  ok  {plugin_dir.name:40s}  {zip_path.name}  ({size_kb} KB)")
        except Exception as exc:
            print(f"  FAIL  {plugin_dir.name:40s}  ERROR: {exc}", file=sys.stderr)

    print(f"\nDone. Upload zip files from dist/ to the Admin Platform.")


if __name__ == "__main__":
    main()
