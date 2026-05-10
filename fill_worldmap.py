#!/usr/bin/env python3
"""
fill_worldmap.py — Stendhal Level 0 World Filler
=================================================
Run this script from the root of the Stendhal source directory (the directory
that contains the 'data/' and 'src/' subdirectories).

What it does
------------
1. Copies data/maps/Level 0/ados/coast_se.tmx into every blank slot listed
   below, creating subdirectories as needed.
2. Patches the <map name="…"> attribute and the internal zone-name property
   inside every copied TMX so the game loads them as distinct zones.
3. Registers every new zone in data/conf/zones.xml so the Stendhal server
   actually adds them to the world at start-up.

Usage
-----
    python3 fill_worldmap.py [--dry-run] [--stendhal-dir PATH]

Options
-------
--dry-run          Print what would be done without writing any files.
--stendhal-dir     Explicit path to the Stendhal source root.
                   Defaults to the current working directory.
"""

import argparse
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Zone definitions
# Format: (area_subdir, map_filename_without_extension)
# Zone ID will be  "0_{area}_{map_filename}"  (Stendhal naming convention)
# ---------------------------------------------------------------------------
NEW_ZONES: List[Tuple[str, str]] = [
    # Row 1 — ocean east of ados (next to ados/ocean_e.tmx)
    ("amazon",  "ocean_s"),

    # Row 2 — deep ocean south-east of ados (next to ados/ocean_se.tmx)
    ("ados",    "ocean_deep"),

    # Row 3 — northern athor ocean (next to ados/coast_s.tmx)
    ("athor",   "ocean_n"),
    ("athor",   "ocean_ne"),

    # Row 4 — andor coast (next to kirneh/river_w.tmx, heading east)
    ("andor",   "coast_w3"),
    ("andor",   "coast_w2"),
    ("andor",   "coast_w"),
    ("andor",   "coast"),
    ("andor",   "dock"),
    ("andor",   "coast_e"),
    ("andor",   "coast_e2"),
    ("andor",   "coast_e3"),

    # Row 5 — andor town/city (next to fado/forest_s_e3, heading east)
    ("andor",   "andor_w3"),
    ("andor",   "andor_w2"),
    ("andor",   "andor_w"),
    ("andor",   "town"),
    ("andor",   "city"),
    ("andor",   "andor_e"),
    ("andor",   "andor_e2"),
    ("andor",   "andor_e3"),

    # Row 6 — andor forest (next to kalavan/forest_e2, heading east)
    ("andor",   "andor_forest_w3"),
    ("andor",   "andor_forest_w2"),
    ("andor",   "andor_forest_w"),
    ("andor",   "andor_forest"),
    ("andor",   "andor_clearing"),
    ("andor",   "andor_forest_e"),
    ("andor",   "andor_forest_e2"),
    ("andor",   "andor_forest_e3"),
]

TEMPLATE_REL  = Path("data/maps/Level 0/ados/coast_se.tmx")
MAPS_BASE_DIR = Path("data/maps/Level 0")
ZONES_XML     = Path("data/conf/zones.xml")

# The zone-name property key used inside Stendhal TMX files
TMX_ZONE_PROPERTY_NAME = "zone"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def zone_id(area: str, mapname: str) -> str:
    """Return the Stendhal zone ID for a level-0 zone."""
    return f"0_{area}_{mapname}"


def tmx_relative_path(area: str, mapname: str) -> str:
    """Return the path Stendhal expects in zones.xml (relative to data/maps)."""
    return f"Level 0/{area}/{mapname}.tmx"


def patch_tmx(src_path: Path, dst_path: Path, new_name: str, dry_run: bool) -> None:
    """
    Copy src_path → dst_path and update the internal zone identifiers:
      • <map name="…">     attribute on the root element
      • <property name="zone" value="…"> inside <properties>
    """
    if dry_run:
        print(f"  [DRY-RUN] Would copy {src_path} → {dst_path}")
        print(f"            and set zone name to '{new_name}'")
        return

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dst_path)

    # Parse and patch
    ET.register_namespace("", "")  # keep existing namespaces intact
    tree = ET.parse(dst_path)
    root = tree.getroot()

    # 1. Patch the <map name="…"> attribute
    if "name" in root.attrib:
        root.set("name", new_name)

    # 2. Patch any <property name="zone" value="…"> inside <properties>
    for props in root.findall("properties"):
        for prop in props.findall("property"):
            if prop.get("name") == TMX_ZONE_PROPERTY_NAME:
                prop.set("value", new_name)

    # Write back, preserving the XML declaration
    tree.write(
        dst_path,
        encoding="UTF-8",
        xml_declaration=True,
        short_empty_elements=True,
    )
    print(f"  Patched  {dst_path}  (zone='{new_name}')")


def ensure_zone_in_xml(zones_xml_path: Path, new_zones: List[Tuple[str, str]], dry_run: bool) -> None:
    """
    Add missing <zone> entries to data/conf/zones.xml.

    Stendhal uses a zones.xml that contains one or more <zones> groups.
    Each zone entry looks like:
        <zone name="0_ados_coast_se"
              file="Level 0/ados/coast_se.tmx"
              title="Coast" />
    or may carry additional attributes.  We append each missing entry to the
    first <zones> group whose 'level' attribute is "0" (or the first <zones>
    group if none has a level attribute at all).
    """
    if not zones_xml_path.exists():
        print(f"\n[WARNING] zones.xml not found at {zones_xml_path}")
        print("          Skipping XML registration.  See README for manual steps.")
        return

    tree = ET.parse(zones_xml_path)
    root = tree.getroot()

    # Collect existing zone names so we don't duplicate
    existing = {z.get("name") for z in root.iter("zone")}

    # Find the best parent <zones> group to append to
    group = None
    for g in root.iter("zones"):
        if g.get("level") == "0":
            group = g
            break
    if group is None:
        # Fall back to the first <zones> element
        group = root.find("zones")
    if group is None:
        # zones.xml has an unexpected structure — append directly to root
        group = root

    added = []
    for area, mapname in new_zones:
        zid  = zone_id(area, mapname)
        fpath = tmx_relative_path(area, mapname)
        if zid in existing:
            print(f"  [SKIP] {zid} already registered in zones.xml")
            continue
        elem = ET.SubElement(group, "zone")
        elem.set("name",  zid)
        elem.set("file",  fpath)
        # Use a human-readable title derived from the map name
        elem.set("title", mapname.replace("_", " ").title())
        added.append(zid)

    if not added:
        print("  No new zones needed in zones.xml (all already present).")
        return

    if dry_run:
        print(f"  [DRY-RUN] Would add {len(added)} zone(s) to {zones_xml_path}:")
        for z in added:
            print(f"            • {z}")
        return

    # Pretty-print the XML (Python's ElementTree doesn't indent by default)
    _indent_xml(root)
    tree.write(
        zones_xml_path,
        encoding="UTF-8",
        xml_declaration=True,
        short_empty_elements=True,
    )
    print(f"\n  Registered {len(added)} new zone(s) in {zones_xml_path}:")
    for z in added:
        print(f"    • {z}")


def _indent_xml(elem: ET.Element, level: int = 0) -> None:
    """Add pretty-print indentation in-place (Python <3.9 compatibility)."""
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            _indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent
    if not level:
        elem.tail = "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without writing files.")
    parser.add_argument("--stendhal-dir", default=".",
                        help="Path to the Stendhal source root (default: CWD).")
    args = parser.parse_args()

    stendhal_dir = Path(args.stendhal_dir).resolve()
    os.chdir(stendhal_dir)

    print(f"Stendhal source root : {stendhal_dir}")
    print(f"Dry-run mode         : {args.dry_run}")
    print()

    # Sanity check
    template = stendhal_dir / TEMPLATE_REL
    if not template.exists():
        sys.exit(
            f"ERROR: Template file not found:\n  {template}\n"
            "Make sure you are running this script from the Stendhal source root\n"
            "and that data/maps/Level 0/ados/coast_se.tmx exists."
        )

    print(f"Template             : {template}")
    print(f"Zones to create      : {len(NEW_ZONES)}")
    print()

    # ── Step 1: Copy + patch TMX files ──────────────────────────────────────
    print("=== Step 1: Creating TMX files ===")
    for area, mapname in NEW_ZONES:
        dst = stendhal_dir / MAPS_BASE_DIR / area / f"{mapname}.tmx"
        if dst.exists():
            print(f"  [SKIP] {dst.relative_to(stendhal_dir)} already exists.")
            continue
        new_name = zone_id(area, mapname)
        patch_tmx(template, dst, new_name, dry_run=args.dry_run)

    # ── Step 2: Register zones in zones.xml ─────────────────────────────────
    print("\n=== Step 2: Updating zones.xml ===")
    zones_xml = stendhal_dir / ZONES_XML
    ensure_zone_in_xml(zones_xml, NEW_ZONES, dry_run=args.dry_run)

    print()
    print("Done.")
    print()
    print("Next steps")
    print("----------")
    print("1. If Stendhal uses separate Java ZoneConfigurator classes for each")
    print("   zone, stub classes are NOT created by this script (ocean/filler")
    print("   zones typically don't need them — but verify in your project).")
    print("2. Rebuild the project:  ant  (or  mvn package  depending on your")
    print("   build system), then restart the server.")
    print("3. The new zones will appear in the world as copies of the Ados")
    print("   coast_se terrain.  Use the map editor (Tiled) to customise each")
    print("   TMX file afterwards.")


if __name__ == "__main__":
    main()
