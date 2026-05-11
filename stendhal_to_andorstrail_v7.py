#!/usr/bin/env python3
"""
stendhal_to_andorstrail_v7.py
=============================
Converts ALL Stendhal maps (every level, including all interior zones) into
Andor's Trail-compatible 32x32-tile TMX map chunks, then writes a unified
Mapevents objectgroup back into every source stendhal/data/maps/*.tmx file.

Changes from v6
---------------
* MAPEVENTS WRITEBACK (new in v7):
  After all AT chunk TMX files are written, the script projects every
  generated Mapevents entry (exits AND all carried-over Mapevents objects)
  back onto the originating Stendhal source TMX files in data/maps/*.tmx.
  For each source map the script:
    - Translates chunk-local pixel coordinates back to zone-global coords
      by adding the chunk tile-origin offset (ox*32, oy*32 pixels).
    - Merges all exits from every split chunk that covers the zone.
    - Preserves every original Mapevents object name/type/property convention.
    - Replaces (or inserts) an <objectgroup name="Mapevents"> block in the
      source TMX using the AT object-layer format defined in 1_Maps.txt
      (x/y/width/height all multiples of 32, explicit <properties> block for
      mapchange targets, unique "name" attribute within the map).
    - Writes mapchange objects with "map" and "place" properties as required
      by Andor's Trail content format.
  A summary reports how many source files were updated vs skipped.

Changes from v5 (retained from v6)
------------------------------------
* NO BLANK CHUNKS: entirely empty chunks are skipped.
* CORRECT EDGE-CHUNK SIZING: partial edge chunks sized to actual tile count.
* WORLD FILE uses actual pixel dimensions per chunk.

Changes from v4 (retained from v5+v6)
--------------------------------------
* Bidirectional portal exits (both sides of every portal get a TMX object).
* Typed mapchange objects (mapchange_up / _down / _enter / _exit / mapchange).
* Robust portal detection: recursive iter(), namespaced tags, coord-based
  portals, TMX objectgroup fallback, diagnostic [PROBE] output.
* Portal exit catalog: res/xml/portal_exits_catalog.txt.

Requirements: Python 3.6+, no third-party packages.
Run from the Stendhal project ROOT directory (the one containing data/).
"""

import os, sys, re, shutil, math, json, glob, struct, zlib, base64
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# ── tuneable constants ────────────────────────────────────────────────────────
STENDHAL_ZONE_CONF = "data/conf/zones"
DATA_MAPS_BASE     = "data/maps"
OUTPUT_XML         = os.path.join("res", "xml")
OUTPUT_DRAWABLE    = os.path.join("res", "drawable")
OUTPUT_VALUES      = os.path.join("res", "values")
WORLD_FILE         = "stendhal_world.world"
CATALOG_FILE       = "portal_exits_catalog.txt"

AT_W    = 32    # chunk width  in tiles
AT_H    = 32    # chunk height in tiles
TILE_PX = 32    # pixels per tile

INT_LEVEL = 1000   # synthetic level for all interior (int_*) zones

ZONE_NS  = "stendhal"
ZONE_TAG = f"{{{ZONE_NS}}}zone"

STENDHAL_TO_AT = {
    "0_floor":    "Ground",
    "1_terrain":  "Object",
    "2_object":   "Object2",
    "3_roof":     "Above",
    "4_roof_add": "Above2",
    "collision":  "Walkable",
}
AT_LAYER_ORDER   = ["Ground", "Object", "Object2", "Above", "Above2", "Walkable"]
AT_VISUAL_LAYERS = {"Ground", "Object", "Object2", "Above", "Above2"}
AT_OBJECT_LAYERS = ["Mapevents", "Spawn", "Keys", "Replace"]

OPPOSITE = {"north": "south", "south": "north", "west": "east", "east": "west"}

FLIP_H    = 0x80000000
FLIP_V    = 0x40000000
FLIP_D    = 0x20000000
FLIP_MASK = FLIP_H | FLIP_V | FLIP_D

# Exit type string constants
EXIT_UP    = "mapchange_up"
EXIT_DOWN  = "mapchange_down"
EXIT_ENTER = "mapchange_enter"
EXIT_EXIT  = "mapchange_exit"
EXIT_HORIZ = "mapchange"

# Portal XML element tag names recognised in any namespace
PORTAL_TAGS = frozenset((
    "portal", "teleporter", "teleport", "entrance", "staircase",
    "stair", "exit", "transition", "warp", "door", "link",
))

# Destination child element tag names (any namespace)
DEST_TAGS = frozenset(("destination", "dest", "target"))

# TMX property name patterns that carry a destination zone name
DEST_ZONE_PROPS = (
    "destination-zone", "destinationzone", "destination_zone",
    "target-zone",      "targetzone",      "target_zone",
    "zone", "map", "destination", "target", "to",
    "dest", "destzone", "destmap",
    "teleport-to", "teleportto", "warp-to", "warpto",
)

# TMX property name patterns that carry destination tile coords
DEST_X_PROPS = ("destination-x", "destinationx", "destination_x",
                "target-x", "targetx", "target_x",
                "dest-x", "destx", "dest_x", "x")
DEST_Y_PROPS = ("destination-y", "destinationy", "destination_y",
                "target-y", "targety", "target_y",
                "dest-y", "desty", "dest_y", "y")

# Object type / name keywords that signal a portal in TMX
PORTAL_TYPE_RE = re.compile(
    r"portal|teleport|staircase|stair|entrance|exit|transition|warp"
    r"|door|link|zone.?change|mapchange",
    re.IGNORECASE,
)


# ── exit type classifier ──────────────────────────────────────────────────────

def classify_exit_type(src_level: int, tgt_level: int) -> str:
    if src_level == INT_LEVEL and tgt_level != INT_LEVEL:
        return EXIT_EXIT
    if tgt_level == INT_LEVEL and src_level != INT_LEVEL:
        return EXIT_ENTER
    if tgt_level > src_level:
        return EXIT_UP
    if tgt_level < src_level:
        return EXIT_DOWN
    return EXIT_HORIZ


# ── AT tileset detection ──────────────────────────────────────────────────────

def is_at_tileset(ts_name: str, img_src: str) -> bool:
    n = ts_name.lower()
    if n.startswith("tileset_") or n.startswith("map_"):
        return True
    img_norm = img_src.replace("\\", "/")
    if "../drawable/" in img_norm or "/drawable/" in img_norm:
        return True
    base = os.path.basename(img_norm).lower()
    if base.startswith("tileset_") or base.startswith("map_"):
        return True
    return False


# ── drawable name resolver ────────────────────────────────────────────────────

_drawable_name_map: dict = {}

def resolve_drawable_name(abs_img: str, img_rel: str) -> str:
    img_fwd = img_rel.replace("\\", "/")
    if "../drawable/" in img_fwd:
        return os.path.basename(img_fwd)
    if img_fwd.startswith("drawable/"):
        return os.path.basename(img_fwd)
    parts = [p for p in img_fwd.split("/") if p and p != ".."]
    for depth in range(1, len(parts) + 1):
        candidate = "_".join(parts[-depth:]) if depth > 1 else parts[-1]
        if not candidate.lower().endswith(".png"):
            candidate += ".png"
        existing = _drawable_name_map.get(candidate)
        if existing is None or existing == abs_img:
            _drawable_name_map[candidate] = abs_img
            return candidate
    fallback = "_".join(parts)
    if not fallback.lower().endswith(".png"):
        fallback += ".png"
    _drawable_name_map[fallback] = abs_img
    return fallback


# ── basic helpers ─────────────────────────────────────────────────────────────

def decode_layer(data_el: ET.Element) -> list:
    enc  = data_el.get("encoding", "xml")
    comp = data_el.get("compression", "")
    text = (data_el.text or "").strip()
    if enc == "base64":
        raw = base64.b64decode(text)
        if comp == "zlib":
            raw = zlib.decompress(raw)
        elif comp == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        n = len(raw) // 4
        return list(struct.unpack("<" + "I" * n, raw))
    if enc == "csv":
        return [int(v.strip()) for v in text.split(",") if v.strip()]
    return [int(t.get("gid", "0")) for t in data_el.findall("tile")]


def encode_layer(tiles: list) -> str:
    raw = struct.pack("<" + "I" * len(tiles), *tiles)
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def tile_at(tiles: list, x: int, y: int, w: int) -> int:
    i = y * w + x
    return tiles[i] if 0 <= i < len(tiles) else 0


def png_wh(path: str):
    try:
        with open(path, "rb") as f:
            hdr = f.read(24)
        if hdr[:8] == b"\x89PNG\r\n\x1a\n":
            return (struct.unpack(">I", hdr[16:20])[0],
                    struct.unpack(">I", hdr[20:24])[0])
    except Exception:
        pass
    return 0, 0


def consecutive_groups(positions: list) -> list:
    if not positions:
        return []
    groups, start, prev = [], positions[0], positions[0]
    for p in positions[1:]:
        if p != prev + 1:
            groups.append((start, prev - start + 1))
            start = p
        prev = p
    groups.append((start, prev - start + 1))
    return groups


def safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def local_tag(el: ET.Element) -> str:
    """Strip XML namespace from element tag: '{ns}name' -> 'name' (lowercase)."""
    return el.tag.split("}")[-1].lower()


def zone_level_from_name(name: str) -> int:
    if name.startswith("int_"):
        return INT_LEVEL
    m = re.match(r"^(-?\d+)_", name)
    return int(m.group(1)) if m else 0


def is_interior_level(level_attr: str) -> bool:
    if not level_attr:
        return False
    return level_attr.strip().lower() in ("int", "interior", "i")


def get_attr_multi(el: ET.Element, *names: str, default="") -> str:
    """Return the first non-empty attribute value from a list of candidate names."""
    for n in names:
        v = el.get(n, "").strip()
        if v:
            return v
    return default


# ── v6: blank-chunk detection ─────────────────────────────────────────────────

def is_blank_chunk(layer_tiles: dict, chunk_w: int, chunk_h: int) -> bool:
    """
    Return True if every visual layer in this chunk is entirely GID=0
    (i.e. no tile is placed anywhere in the chunk's visual area).

    The Walkable (collision) layer is intentionally excluded from this check.
    Only AT_VISUAL_LAYERS are inspected.
    """
    n = chunk_w * chunk_h
    for lname in AT_VISUAL_LAYERS:
        tiles = layer_tiles.get(lname)
        if tiles is None:
            continue
        for i in range(n):
            if i < len(tiles) and (tiles[i] & ~FLIP_MASK) != 0:
                return False
    return True


# ── zone config ───────────────────────────────────────────────────────────────

class ZoneInfo:
    __slots__ = ("name", "level", "wx", "wy", "tmx_rel", "width", "height")

    def __init__(self, name, level, wx, wy, tmx_rel):
        self.name    = name
        self.level   = level
        self.wx      = wx
        self.wy      = wy
        self.tmx_rel = tmx_rel
        self.width   = 0
        self.height  = 0


def load_zone_configs(conf_dir: str) -> list:
    zones = []
    xmls  = sorted(set(
        glob.glob(os.path.join(conf_dir, "**", "*.xml"), recursive=True) +
        glob.glob(os.path.join(conf_dir, "*.xml"))
    ))
    if not xmls:
        print(f"  [WARN] No XML found under {conf_dir}")
        return zones

    for fpath in xmls:
        try:
            root = ET.parse(fpath).getroot()
        except ET.ParseError as e:
            print(f"  [WARN] Parse error {fpath}: {e}")
            continue

        els = list(root.iter(ZONE_TAG)) or list(root.iter("zone"))
        for z in els:
            name = z.get("name", "").strip()
            if not name:
                continue
            tmx = z.get("file", "").strip()
            if not tmx:
                continue
            try:
                wx = int(z.get("x", "0"))
                wy = int(z.get("y", "0"))
            except ValueError:
                wx, wy = 0, 0

            raw_level = z.get("level", "").strip()
            if is_interior_level(raw_level) or name.startswith("int_"):
                level = INT_LEVEL
            else:
                try:
                    level = int(raw_level) if raw_level else zone_level_from_name(name)
                except ValueError:
                    level = 0

            zones.append(ZoneInfo(name, level, wx, wy, tmx))
    return zones


def resolve_tmx(tmx_rel: str) -> str:
    for cand in [
        os.path.join(DATA_MAPS_BASE, tmx_rel),
        tmx_rel,
        os.path.join(DATA_MAPS_BASE, tmx_rel.replace(" ", "_")),
        os.path.join("tiled", tmx_rel),
        os.path.join(DATA_MAPS_BASE, os.path.basename(tmx_rel)),
    ]:
        if os.path.isfile(cand):
            return cand
    return ""


def fallback_scan() -> list:
    base = None
    for c in [DATA_MAPS_BASE,
              os.path.join(DATA_MAPS_BASE, "Level 0"),
              os.path.join(DATA_MAPS_BASE, "Level_0")]:
        if os.path.isdir(c):
            base = c
            break
    if not base:
        return []
    print(f"  Fallback: scanning {base}")
    tmx_files = sorted(glob.glob(os.path.join(base, "**", "*.tmx"), recursive=True))
    zones, pat = [], re.compile(r"(-?\d+)_(-?\d+)(?:\.tmx)?$")
    grid = max(1, int(math.sqrt(len(tmx_files))) + 1)
    for i, f in enumerate(tmx_files):
        stem = Path(f).stem
        m    = pat.search(stem)
        wx, wy = (int(m.group(1)), int(m.group(2))) if m else (i % grid * AT_W, i // grid * AT_H)
        zones.append(ZoneInfo(stem, zone_level_from_name(stem), wx, wy,
                              os.path.relpath(f, DATA_MAPS_BASE)))
    return zones


# ── portal link ───────────────────────────────────────────────────────────────

class PortalLink:
    __slots__ = ("source_zone", "src_tile_x", "src_tile_y",
                 "target_zone", "tgt_tile_x", "tgt_tile_y",
                 "src_ref", "tgt_ref", "exit_type")

    def __init__(self, source_zone, src_x, src_y,
                 target_zone, tgt_x, tgt_y,
                 src_ref=None, tgt_ref=None, exit_type=EXIT_HORIZ):
        self.source_zone = source_zone
        self.src_tile_x  = src_x
        self.src_tile_y  = src_y
        self.target_zone = target_zone
        self.tgt_tile_x  = tgt_x
        self.tgt_tile_y  = tgt_y
        self.src_ref     = src_ref
        self.tgt_ref     = tgt_ref
        self.exit_type   = exit_type


def _portal_direction(src_level: int, tgt_level: int) -> str:
    if tgt_level == INT_LEVEL and src_level != INT_LEVEL:
        return "enter"
    if src_level == INT_LEVEL and tgt_level != INT_LEVEL:
        return "exit"
    if tgt_level > src_level:
        return "up"
    if tgt_level < src_level:
        return "down"
    return "across"


# ── diagnostic probe ──────────────────────────────────────────────────────────

def probe_xml_structure(conf_dir: str, zone_by_name: dict):
    """
    Sample up to 5 zone XML files and print a summary of the child element
    tags and attributes found inside <zone> elements.
    """
    print("  [PROBE] Sampling zone XML structure ...")
    xmls = sorted(glob.glob(os.path.join(conf_dir, "**", "*.xml"), recursive=True))[:5]
    tag_counts: dict = defaultdict(int)
    zone_count = 0
    child_sample = []

    for fpath in xmls:
        try:
            root = ET.parse(fpath).getroot()
        except ET.ParseError:
            continue
        zone_els = list(root.iter(ZONE_TAG)) or list(root.iter("zone"))
        for z_el in zone_els:
            zone_count += 1
            for desc in z_el.iter():
                if desc is z_el:
                    continue
                t = local_tag(desc)
                tag_counts[t] += 1
                if t in PORTAL_TAGS and len(child_sample) < 6:
                    attrs = dict(desc.attrib)
                    children = [local_tag(c) for c in desc]
                    child_sample.append(
                        f"    <{t} {attrs}> children={children}"
                    )

    print(f"  [PROBE] Scanned {zone_count} zone elements in {len(xmls)} XML files")
    if tag_counts:
        top = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:20]
        print(f"  [PROBE] Most common child tags: "
              + ", ".join(f"{t}({c})" for t, c in top))
    else:
        print("  [PROBE] No child elements found inside <zone> elements!")
        print("  [PROBE] Portals may be defined only in Java source code.")
        print("  [PROBE] Only TMX-objectgroup and edge-based exits will be generated.")
    if child_sample:
        print("  [PROBE] Portal element samples:")
        for s in child_sample:
            print(s)
    print()


# ── portal loading ─────────────────────────────────────────────────────────────

def _find_dest_el(portal_el: ET.Element):
    for child in portal_el:
        if local_tag(child) in DEST_TAGS:
            return child
    for child in portal_el:
        for grandchild in child:
            if local_tag(grandchild) in DEST_TAGS:
                return grandchild
    return None


def _extract_dest_zone(el: ET.Element, portal_el: ET.Element) -> str:
    if el is not None:
        v = get_attr_multi(el, "zone", "map", "name", "area")
        if v:
            return v
    return get_attr_multi(portal_el,
                          "destination-zone", "destinationzone", "destination_zone",
                          "target-zone", "targetzone", "to-zone", "tozone",
                          "zone", "map")


def _extract_dest_ref(el: ET.Element, portal_el: ET.Element) -> str:
    if el is not None:
        v = el.get("ref", "").strip()
        if v:
            return v
    return get_attr_multi(portal_el,
                          "destination-ref", "destinationref",
                          "target-ref", "targetref")


def _extract_dest_coords(el: ET.Element, portal_el: ET.Element):
    src = el if el is not None else portal_el
    dx = safe_int(src.get("x", "-1"), -1)
    dy = safe_int(src.get("y", "-1"), -1)
    if dx < 0 or dy < 0:
        if el is not None:
            dx = safe_int(portal_el.get("dest-x",
                          portal_el.get("destination-x",
                          portal_el.get("target-x", "-1"))), -1)
            dy = safe_int(portal_el.get("dest-y",
                          portal_el.get("destination-y",
                          portal_el.get("target-y", "-1"))), -1)
    return dx, dy


def load_zone_portals(conf_dir: str, zones: list) -> tuple:
    """
    Parse ALL portal / entrance / staircase definitions.

    Pass 1 -- Zone config XML (recursive, coord+ref aware)
    Pass 2 -- Ref cross-linking
    Pass 3 -- TMX objectgroup fallback
    """
    zone_by_name = {z.name: z for z in zones}
    portals:   list = []
    seen_tile: set  = set()

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    raw_portals: dict = {}
    direct_links: list = []

    xmls = sorted(set(
        glob.glob(os.path.join(conf_dir, "**", "*.xml"), recursive=True) +
        glob.glob(os.path.join(conf_dir, "*.xml"))
    ))

    p1_zones_with_portals = 0

    for fpath in xmls:
        try:
            root = ET.parse(fpath).getroot()
        except ET.ParseError:
            continue

        zone_els = list(root.iter(ZONE_TAG)) or list(root.iter("zone"))
        for z_el in zone_els:
            src_name = z_el.get("name", "").strip()
            if not src_name:
                continue
            src_zone_obj = zone_by_name.get(src_name)
            src_level    = src_zone_obj.level if src_zone_obj else zone_level_from_name(src_name)

            found_in_zone = 0

            for child in z_el.iter():
                if child is z_el:
                    continue
                t = local_tag(child)
                if t not in PORTAL_TAGS:
                    continue

                ref   = child.get("ref", "").strip()
                src_x = safe_int(get_attr_multi(child, "x", "src-x", "srcx"), -1)
                src_y = safe_int(get_attr_multi(child, "y", "src-y", "srcy"), -1)
                if src_x < 0 or src_y < 0:
                    continue

                dest_el   = _find_dest_el(child)
                dest_zone = _extract_dest_zone(dest_el, child)
                dest_ref  = _extract_dest_ref(dest_el, child)
                dest_x, dest_y = _extract_dest_coords(dest_el, child)

                if not dest_zone:
                    continue

                tgt_zone_obj = zone_by_name.get(dest_zone)
                if tgt_zone_obj is None:
                    continue

                tgt_level = tgt_zone_obj.level
                etype     = classify_exit_type(src_level, tgt_level)

                found_in_zone += 1

                if dest_x >= 0 and dest_y >= 0:
                    pos_key = (src_name, src_x, src_y)
                    if pos_key not in seen_tile:
                        seen_tile.add(pos_key)
                        direct_links.append(PortalLink(
                            src_name, src_x, src_y,
                            dest_zone, dest_x, dest_y,
                            src_ref=ref or None,
                            tgt_ref=dest_ref or None,
                            exit_type=etype,
                        ))
                else:
                    key = (src_name, ref) if ref else (src_name, f"__auto_{src_x}_{src_y}")
                    if key not in raw_portals:
                        raw_portals[key] = {
                            "x": src_x, "y": src_y,
                            "dest_zone": dest_zone,
                            "dest_ref":  dest_ref,
                            "exit_type": etype,
                        }

            if found_in_zone:
                p1_zones_with_portals += 1

    print(f"  Pass 1 (XML):  {p1_zones_with_portals} zones had portal elements")
    print(f"              ->  {len(raw_portals)} ref-based entries")
    print(f"              ->  {len(direct_links)} coord-based PortalLinks")

    # ── Pass 2: cross-link ref-based portals ──────────────────────────────────
    seen_links: set = set()

    for (src_name, src_ref), data in raw_portals.items():
        dest_zone = data["dest_zone"]
        dest_ref  = data["dest_ref"]
        etype     = data["exit_type"]

        if not dest_zone or not dest_ref:
            if dest_zone and dest_zone in zone_by_name:
                pos_key = (src_name, data["x"], data["y"])
                if pos_key not in seen_tile:
                    seen_tile.add(pos_key)
                    portals.append(PortalLink(
                        src_name, data["x"], data["y"],
                        dest_zone, None, None,
                        src_ref=src_ref if not src_ref.startswith("__auto_") else None,
                        tgt_ref=None,
                        exit_type=etype,
                    ))
            continue

        if dest_zone not in zone_by_name:
            continue

        link_key = (src_name, src_ref, dest_zone, dest_ref)
        if link_key in seen_links:
            continue
        seen_links.add(link_key)

        tgt_data = raw_portals.get((dest_zone, dest_ref))
        tgt_x    = tgt_data["x"] if tgt_data else None
        tgt_y    = tgt_data["y"] if tgt_data else None

        pos_key = (src_name, data["x"], data["y"])
        if pos_key not in seen_tile:
            seen_tile.add(pos_key)
            portals.append(PortalLink(
                src_name, data["x"], data["y"],
                dest_zone, tgt_x, tgt_y,
                src_ref=src_ref if not src_ref.startswith("__auto_") else None,
                tgt_ref=dest_ref,
                exit_type=etype,
            ))

    portals.extend(direct_links)
    xml_portal_count = len(portals)

    if xml_portal_count == 0:
        probe_xml_structure(conf_dir, zone_by_name)

    # ── Pass 3: TMX objectgroup fallback ─────────────────────────────────────
    xml_src_positions: set = set()
    for pl in portals:
        xml_src_positions.add((pl.source_zone, pl.src_tile_x, pl.src_tile_y))

    p3_count = 0

    for zone in zones:
        fpath = resolve_tmx(zone.tmx_rel)
        if not fpath:
            continue
        try:
            tmx_root = ET.parse(fpath).getroot()
        except ET.ParseError:
            continue

        for og in tmx_root.findall("objectgroup"):
            for obj in og.findall("object"):
                props: dict = {}
                props_el = obj.find("properties")
                if props_el is not None:
                    for prop in props_el.findall("property"):
                        pname = prop.get("name", "").lower().replace("-", "_")
                        pval  = prop.get("value", prop.text or "").strip()
                        props[pname] = pval

                obj_type  = (obj.get("type", "") or obj.get("class", "")).strip()
                obj_name  = obj.get("name", "").strip()

                is_portal_typed = (PORTAL_TYPE_RE.search(obj_type)
                                   or PORTAL_TYPE_RE.search(obj_name))

                tgt_zone_name = ""
                for pn in DEST_ZONE_PROPS:
                    key = pn.lower().replace("-", "_")
                    if key in props:
                        tgt_zone_name = props[key]
                        break

                if not is_portal_typed and not tgt_zone_name:
                    continue

                px     = safe_int(float(obj.get("x", "0")))
                py     = safe_int(float(obj.get("y", "0")))
                src_tx = px // TILE_PX
                src_ty = py // TILE_PX

                if (zone.name, src_tx, src_ty) in xml_src_positions:
                    continue

                if not tgt_zone_name:
                    continue
                tgt_zone_obj = zone_by_name.get(tgt_zone_name)
                if tgt_zone_obj is None:
                    for zn in zone_by_name:
                        if zn.endswith("_" + tgt_zone_name) or zn == tgt_zone_name:
                            tgt_zone_obj = zone_by_name[zn]
                            tgt_zone_name = zn
                            break
                if tgt_zone_obj is None:
                    continue

                tgt_tx = tgt_ty = None
                for pn in DEST_X_PROPS:
                    key = pn.lower().replace("-", "_")
                    if key in props:
                        v = safe_int(props[key], -1)
                        if v >= 0:
                            tgt_tx = v
                        break
                for pn in DEST_Y_PROPS:
                    key = pn.lower().replace("-", "_")
                    if key in props:
                        v = safe_int(props[key], -1)
                        if v >= 0:
                            tgt_ty = v
                        break

                etype = classify_exit_type(zone.level, tgt_zone_obj.level)
                xml_src_positions.add((zone.name, src_tx, src_ty))
                portals.append(PortalLink(
                    zone.name, src_tx, src_ty,
                    tgt_zone_name, tgt_tx, tgt_ty,
                    src_ref=None, tgt_ref=None,
                    exit_type=etype,
                ))
                p3_count += 1

    print(f"  Pass 3 (TMX): {p3_count} additional portals from objectgroups")
    print(f"  Total PortalLinks: {len(portals)}")
    return portals, raw_portals, p3_count


# ── Stendhal TMX reader ───────────────────────────────────────────────────────

class StendhalTMX:
    def __init__(self, path: str):
        self.path = path
        self.dir  = os.path.dirname(os.path.abspath(path))
        root      = ET.parse(path).getroot()
        self.width  = int(root.get("width",  0))
        self.height = int(root.get("height", 0))
        self.tw     = int(root.get("tilewidth",  TILE_PX))
        self.th     = int(root.get("tileheight", TILE_PX))
        self.tilesets:     list = []
        self.layers:       dict = {}
        self.objectgroups: dict = {}
        self._parse(root)

    def _parse(self, root: ET.Element):
        for ts_el in root.findall("tileset"):
            fg   = int(ts_el.get("firstgid", 1))
            src  = ts_el.get("source", "")
            name = ts_el.get("name", "")
            tc   = int(ts_el.get("tilecount", 0))
            cols = int(ts_el.get("columns",   0))
            img_src = ""
            if src:
                tsx = os.path.normpath(os.path.join(self.dir, src))
                if os.path.isfile(tsx):
                    try:
                        t = ET.parse(tsx).getroot()
                        ie = t.find("image")
                        if ie is not None:
                            img_src = ie.get("source", "")
                        name = t.get("name", name)
                        tc   = int(t.get("tilecount", tc))
                        cols = int(t.get("columns",   cols))
                    except Exception:
                        pass
            else:
                ie = ts_el.find("image")
                if ie is not None:
                    img_src = ie.get("source", "")

            at_native = is_at_tileset(name, img_src)
            abs_img   = self._resolve_image(img_src, at_native)
            draw_name = resolve_drawable_name(abs_img, img_src) if img_src else ""

            self.tilesets.append({
                "firstgid":      fg,
                "name":          name,
                "image":         img_src,
                "abs_img":       abs_img,
                "drawable_name": draw_name,
                "tilecount":     tc,
                "columns":       cols,
                "is_at_native":  at_native,
            })
        self.tilesets.sort(key=lambda t: t["firstgid"])

        for ly in root.findall("layer"):
            de = ly.find("data")
            if de is not None:
                self.layers[ly.get("name", "")] = decode_layer(de)

        for og in root.findall("objectgroup"):
            og_name = og.get("name", "")
            self.objectgroups[og_name] = list(og.findall("object"))

    def _resolve_image(self, img_rel: str, at_native: bool) -> str:
        if not img_rel:
            return ""
        img_fwd = img_rel.replace("\\", "/")
        if at_native and ("../drawable/" in img_fwd or "drawable/" in img_fwd):
            basename = os.path.basename(img_fwd)
            staged = os.path.join(OUTPUT_DRAWABLE, basename)
            if os.path.isfile(staged):
                return staged
            for search_root in (DATA_MAPS_BASE, ".", "res"):
                for dirpath, _, filenames in os.walk(search_root):
                    if basename in filenames:
                        return os.path.join(dirpath, basename)
            return staged
        p = os.path.normpath(os.path.join(self.dir, img_rel))
        if os.path.isfile(p):
            return p
        clean = re.sub(r"^[./]+", "", img_fwd)
        for base in (DATA_MAPS_BASE, "tiled", "."):
            p2 = os.path.normpath(os.path.join(base, clean))
            if os.path.isfile(p2):
                return p2
        return p

    def gid_to_ts(self, gid: int):
        if gid == 0:
            return None, 0
        clean = gid & ~FLIP_MASK
        for ts in reversed(self.tilesets):
            if clean >= ts["firstgid"]:
                return ts, clean - ts["firstgid"]
        return None, 0


# ── tileset registry ──────────────────────────────────────────────────────────

class TilesetRegistry:
    def __init__(self):
        self._by_abs: dict = {}
        self._next   = 1

    def register(self, ts: dict):
        abs_img = ts["abs_img"]
        if not abs_img or abs_img in self._by_abs:
            return
        draw_name = ts["drawable_name"]
        if not draw_name:
            return
        w, h = png_wh(abs_img) if os.path.isfile(abs_img) else (0, 0)
        cols = ts["columns"] if ts["columns"] > 0 else (max(1, w // TILE_PX) if w else 1)
        tc   = ts["tilecount"] if ts["tilecount"] > 0 else (
               cols * max(1, h // TILE_PX) if (w or h) else max(256, ts["tilecount"]))
        self._by_abs[abs_img] = {
            "firstgid":      self._next,
            "name":          ts["name"],
            "drawable_name": draw_name,
            "w": w, "h": h, "cols": cols, "tc": tc,
        }
        self._next += max(1, tc)

    def remap(self, raw_gid: int, stmx: StendhalTMX) -> int:
        if raw_gid == 0:
            return 0
        flags = raw_gid &  FLIP_MASK
        clean = raw_gid & ~FLIP_MASK
        ts, lid = stmx.gid_to_ts(clean)
        if ts is None:
            return 0
        info = self._by_abs.get(ts["abs_img"])
        if info is None:
            return 0
        return (info["firstgid"] + lid) | flags

    def sorted_tilesets(self):
        return sorted(self._by_abs.values(), key=lambda d: d["firstgid"])

    def first_blocked_gid(self, stmx: StendhalTMX) -> int:
        for ts in stmx.tilesets:
            n = ts["name"].lower()
            i = ts["image"].lower() if ts["image"] else ""
            if "collision" in n or "collision" in i or "block" in n:
                info = self._by_abs.get(ts["abs_img"])
                if info:
                    return info["firstgid"]
        all_ts = self.sorted_tilesets()
        return all_ts[0]["firstgid"] if all_ts else 1


# ── tileset copying ───────────────────────────────────────────────────────────

_copied: set = set()

def copy_tileset(abs_img: str, drawable_name: str):
    if drawable_name in _copied:
        return
    if abs_img and os.path.isfile(abs_img):
        os.makedirs(OUTPUT_DRAWABLE, exist_ok=True)
        dst = os.path.join(OUTPUT_DRAWABLE, drawable_name)
        if not os.path.exists(dst):
            shutil.copy2(abs_img, dst)
        _copied.add(drawable_name)
    elif abs_img:
        print(f"  [WARN] Tileset image not found: {abs_img}")


# ── object layer utilities ────────────────────────────────────────────────────

def objects_for_chunk(obj_elements: list, chunk_ox: int, chunk_oy: int,
                      chunk_w: int = AT_W, chunk_h: int = AT_H) -> list:
    """
    Return objects whose pixel origin falls within this chunk's tile area.
    v6: accepts chunk_w/chunk_h so partial edge chunks are handled correctly.
    """
    x_min_px = chunk_ox * TILE_PX
    y_min_px = chunk_oy * TILE_PX
    x_max_px = x_min_px + chunk_w * TILE_PX
    y_max_px = y_min_px + chunk_h * TILE_PX
    result = []
    for obj in obj_elements:
        try:
            px = float(obj.get("x", "0"))
            py = float(obj.get("y", "0"))
        except ValueError:
            continue
        if x_min_px <= px < x_max_px and y_min_px <= py < y_max_px:
            result.append((px - x_min_px, py - y_min_px, obj))
    return result


def serialize_object(obj_el: ET.Element, local_x: float, local_y: float,
                     obj_id: int) -> list:
    attrs = dict(obj_el.attrib)
    attrs["x"]  = str(int(local_x))
    attrs["y"]  = str(int(local_y))
    attrs["id"] = str(obj_id)
    ordered_keys = []
    for k in ("name", "type", "id", "x", "y", "width", "height"):
        if k in attrs:
            ordered_keys.append(k)
    for k in attrs:
        if k not in ordered_keys:
            ordered_keys.append(k)
    attr_str = " ".join(f'{k}="{attrs[k]}"' for k in ordered_keys if k in attrs)
    lines = [f"  <object {attr_str}>"]
    for child in obj_el:
        lines.append("   " + ET.tostring(child, encoding="unicode").strip())
    lines.append("  </object>")
    return lines


# ── exit generation ───────────────────────────────────────────────────────────

def generate_exits(map_id: str, walkable: list,
                   all_ids: set, neighbor_fn,
                   portal_exits_for_chunk: list,
                   used_names: set,
                   chunk_w: int = AT_W,
                   chunk_h: int = AT_H) -> list:
    """
    v6: chunk_w / chunk_h allow edge chunks to emit exits only along their
    actual tile boundary, not the padded 32x32 boundary.
    """
    W, H = chunk_w, chunk_h
    exits = list(portal_exits_for_chunk)

    edges = {
        "north": [x for x in range(W) if tile_at(walkable, x, 0,   W) == 0],
        "south": [x for x in range(W) if tile_at(walkable, x, H-1, W) == 0],
        "west":  [y for y in range(H) if tile_at(walkable, 0,   y, W) == 0],
        "east":  [y for y in range(H) if tile_at(walkable, W-1, y, W) == 0],
    }
    for direction, open_pos in edges.items():
        if not open_pos:
            continue
        nbr = neighbor_fn(map_id, direction)
        if nbr is None or nbr not in all_ids:
            continue
        opp = OPPOSITE[direction]
        for idx, (start, length) in enumerate(consecutive_groups(open_pos)):
            suffix = "" if idx == 0 else str(idx + 1)
            base_name = direction + suffix
            if base_name in used_names:
                suffix = str(idx + 100)
            if direction in ("north", "south"):
                ex, ey = start * TILE_PX, (0 if direction == "north" else H-1) * TILE_PX
                ew, eh = length * TILE_PX, TILE_PX
            else:
                ex, ey = (0 if direction == "west" else W-1) * TILE_PX, start * TILE_PX
                ew, eh = TILE_PX, length * TILE_PX
            exits.append({
                "name": direction + suffix,
                "x": ex, "y": ey, "w": ew, "h": eh,
                "target_map": nbr, "place": opp + suffix,
                "exit_type":  EXIT_HORIZ,
            })
    return exits


# ── chunk plan ────────────────────────────────────────────────────────────────

def build_chunk_plan(zones_tmx: list):
    """
    v6: each chunk dict contains actual_w and actual_h reflecting the true
    number of tiles available (may be < AT_W / AT_H for edge chunks).
    """
    for z, stmx in zones_tmx:
        z.width  = stmx.width
        z.height = stmx.height

    level_min: dict = defaultdict(lambda: (10**9, 10**9))
    for z, _ in zones_tmx:
        mx, my = level_min[z.level]
        level_min[z.level] = (min(mx, z.wx), min(my, z.wy))
    for z, _ in zones_tmx:
        ox, oy = level_min[z.level]
        z.wx -= ox
        z.wy -= oy

    chunks, id_to_wpos, wpos_to_id = [], {}, {}
    for z, stmx in zones_tmx:
        ncx = max(1, math.ceil(z.width  / AT_W))
        ncy = max(1, math.ceil(z.height / AT_H))
        for cy in range(ncy):
            for cx in range(ncx):
                mid      = f"{z.name}_x{cx}_y{cy}"
                tile_ox  = cx * AT_W
                tile_oy  = cy * AT_H

                # v6: compute actual tile count for this chunk (handles edges)
                actual_w = min(AT_W, max(0, z.width  - tile_ox))
                actual_h = min(AT_H, max(0, z.height - tile_oy))

                if actual_w <= 0 or actual_h <= 0:
                    continue

                world_tx = z.wx + tile_ox
                world_ty = z.wy + tile_oy
                key      = (z.level, world_tx, world_ty)
                if key not in wpos_to_id:
                    wpos_to_id[key] = mid
                id_to_wpos[mid] = (z.level, world_tx, world_ty)
                chunks.append({
                    "map_id":   mid,    "zone":     z,
                    "stmx":     stmx,   "ox":       tile_ox,
                    "oy":       tile_oy, "world_tx": world_tx,
                    "world_ty": world_ty,
                    "actual_w": actual_w,
                    "actual_h": actual_h,
                })
    return chunks, id_to_wpos, wpos_to_id


def make_neighbor_fn(id_to_wpos, wpos_to_id):
    deltas = {"north": (0, -AT_H), "south": (0,  AT_H),
              "west":  (-AT_W, 0), "east":  ( AT_W, 0)}
    def neighbor(map_id, direction):
        pos = id_to_wpos.get(map_id)
        if pos is None:
            return None
        level, tx, ty = pos
        dx, dy = deltas[direction]
        return wpos_to_id.get((level, tx+dx, ty+dy))
    return neighbor


# ── portal exit builder ───────────────────────────────────────────────────────

def build_chunk_tile_map(chunks):
    result: dict = {}
    for cd in chunks:
        z, ox, oy, mid = cd["zone"], cd["ox"], cd["oy"], cd["map_id"]
        aw, ah = cd["actual_w"], cd["actual_h"]
        for ty in range(ah):
            for tx in range(aw):
                key = (z.name, ox+tx, oy+ty)
                if key not in result:
                    result[key] = mid
    return result


def build_portal_exits(portal_links: list,
                       zone_by_name: dict,
                       chunk_tile_map: dict) -> tuple:
    result:     dict = defaultdict(list)
    used_names: dict = defaultdict(set)
    catalog:    list = []

    dir_count: dict = defaultdict(lambda: defaultdict(int))

    for pl in portal_links:
        src_chunk = chunk_tile_map.get(
            (pl.source_zone, pl.src_tile_x, pl.src_tile_y))
        if src_chunk is None:
            continue

        if pl.tgt_tile_x is None or pl.tgt_tile_y is None:
            continue
        tgt_chunk = chunk_tile_map.get(
            (pl.target_zone, pl.tgt_tile_x, pl.tgt_tile_y))
        if tgt_chunk is None:
            continue

        cx       = pl.src_tile_x // AT_W
        cy       = pl.src_tile_y // AT_H
        local_tx = pl.src_tile_x - cx * AT_W
        local_ty = pl.src_tile_y - cy * AT_H
        px, py   = local_tx * TILE_PX, local_ty * TILE_PX

        tcx       = pl.tgt_tile_x // AT_W
        tcy       = pl.tgt_tile_y // AT_H
        local_ttx = pl.tgt_tile_x - tcx * AT_W
        local_tty = pl.tgt_tile_y - tcy * AT_H
        tpx, tpy  = local_ttx * TILE_PX, local_tty * TILE_PX

        src_zone  = zone_by_name.get(pl.source_zone)
        tgt_zone  = zone_by_name.get(pl.target_zone)
        src_level = src_zone.level if src_zone else 0
        tgt_level = tgt_zone.level if tgt_zone else 0
        fwd_type  = pl.exit_type
        rev_type  = classify_exit_type(tgt_level, src_level)

        if pl.src_ref:
            exit_name  = pl.src_ref
            place_name = pl.tgt_ref if pl.tgt_ref else pl.src_ref + "_dest"
            rev_name   = pl.tgt_ref if pl.tgt_ref else pl.src_ref + "_dest"
            rev_place  = pl.src_ref
        else:
            direction  = _portal_direction(src_level, tgt_level)
            opp        = _portal_direction(tgt_level, src_level)
            cnt        = dir_count[src_chunk][direction]
            suffix     = "" if cnt == 0 else str(cnt + 1)
            dir_count[src_chunk][direction] += 1
            tgt_cnt    = dir_count[tgt_chunk][opp]
            tgt_suffix = "" if tgt_cnt == 0 else str(tgt_cnt + 1)
            dir_count[tgt_chunk][opp] += 1
            exit_name  = direction + suffix
            place_name = opp + tgt_suffix
            rev_name   = place_name
            rev_place  = exit_name

        if exit_name not in used_names[src_chunk]:
            result[src_chunk].append({
                "name": exit_name,
                "x": px, "y": py, "w": TILE_PX, "h": TILE_PX,
                "target_map": tgt_chunk,
                "place":      place_name,
                "exit_type":  fwd_type,
            })
            used_names[src_chunk].add(exit_name)
            catalog.append(
                f"{src_chunk:60s} -> {tgt_chunk:60s}  [{fwd_type:18s}]  "
                f"name={exit_name}  place={place_name}"
            )

        if rev_name not in used_names[tgt_chunk]:
            result[tgt_chunk].append({
                "name": rev_name,
                "x": tpx, "y": tpy, "w": TILE_PX, "h": TILE_PX,
                "target_map": src_chunk,
                "place":      rev_place,
                "exit_type":  rev_type,
            })
            used_names[tgt_chunk].add(rev_name)
            catalog.append(
                f"{tgt_chunk:60s} -> {src_chunk:60s}  [{rev_type:18s}]  "
                f"name={rev_name}  place={rev_place}"
            )

    return dict(result), dict(used_names), catalog


# ── AT TMX writer ─────────────────────────────────────────────────────────────

def write_at_tmx(path: str,
                 layer_tiles: dict,
                 registry: TilesetRegistry,
                 exits: list,
                 source_objectgroups: dict,
                 chunk_ox: int,
                 chunk_oy: int,
                 chunk_w: int = AT_W,
                 chunk_h: int = AT_H):
    """
    v6: chunk_w / chunk_h allow writing TMX files smaller than 32x32 for
    edge chunks.  The <map width= height=> attributes reflect actual tile
    counts, preventing Tiled from displaying blank padding columns/rows.
    """
    obj_counter = [1]
    def next_id():
        v = obj_counter[0]; obj_counter[0] += 1; return v

    n_layers       = len(AT_LAYER_ORDER)
    n_objgroups    = len(AT_OBJECT_LAYERS)
    next_layer_id  = n_layers + n_objgroups + 1

    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<!DOCTYPE map SYSTEM "https://mapeditor.org/dtd/1.0/map.dtd">')
    lines.append(
        f'<map version="1.0" orientation="orthogonal" renderorder="right-down" '
        f'width="{chunk_w}" height="{chunk_h}" '
        f'tilewidth="{TILE_PX}" tileheight="{TILE_PX}" '
        f'nextlayerid="{next_layer_id}" nextobjectid="1">'
    )

    for ts in registry.sorted_tilesets():
        lines.append(
            f' <tileset firstgid="{ts["firstgid"]}" name="{ts["name"]}" '
            f'tilewidth="{TILE_PX}" tileheight="{TILE_PX}">'
        )
        img_line = f'  <image source="../drawable/{ts["drawable_name"]}"'
        if ts["w"]: img_line += f' width="{ts["w"]}"'
        if ts["h"]: img_line += f' height="{ts["h"]}"'
        img_line += "/>"
        lines.append(img_line)
        lines.append(" </tileset>")

    for layer_id, lname in enumerate(AT_LAYER_ORDER, start=1):
        tiles   = layer_tiles.get(lname, [0] * (chunk_w * chunk_h))
        encoded = encode_layer(tiles)
        vis     = ' visible="0"' if lname == "Walkable" else ""
        lines.append(f' <layer id="{layer_id}" name="{lname}" '
                     f'width="{chunk_w}" height="{chunk_h}"{vis}>')
        lines.append('  <data encoding="base64" compression="zlib">')
        lines.append(f"   {encoded}")
        lines.append("  </data>")
        lines.append(" </layer>")

    obj_layer_base_id = n_layers + 1
    for og_idx, og_name in enumerate(AT_OBJECT_LAYERS):
        layer_id = obj_layer_base_id + og_idx
        if og_name == "Mapevents":
            existing_me = objects_for_chunk(
                source_objectgroups.get("Mapevents", []), chunk_ox, chunk_oy,
                chunk_w, chunk_h)
            total = len(exits) + len(existing_me)
            if total == 0:
                lines.append(f' <objectgroup id="{layer_id}" name="Mapevents"/>')
            else:
                lines.append(f' <objectgroup id="{layer_id}" name="Mapevents">')
                for ex in exits:
                    oid       = next_id()
                    exit_type = ex.get("exit_type", EXIT_HORIZ)
                    lines.append(
                        f'  <object name="{ex["name"]}" type="{exit_type}" '
                        f'id="{oid}" '
                        f'x="{ex["x"]}" y="{ex["y"]}" '
                        f'width="{ex["w"]}" height="{ex["h"]}">'
                    )
                    lines.append("   <properties>")
                    lines.append(f'    <property name="map" value="{ex["target_map"]}"/>')
                    lines.append(f'    <property name="place" value="{ex["place"]}"/>')
                    lines.append("   </properties>")
                    lines.append("  </object>")
                for lx, ly, obj_el in existing_me:
                    lines.extend(serialize_object(obj_el, lx, ly, next_id()))
                lines.append(" </objectgroup>")
        else:
            src_objs = objects_for_chunk(
                source_objectgroups.get(og_name, []), chunk_ox, chunk_oy,
                chunk_w, chunk_h)
            if not src_objs:
                lines.append(f' <objectgroup id="{layer_id}" name="{og_name}"/>')
            else:
                lines.append(f' <objectgroup id="{layer_id}" name="{og_name}">')
                for lx, ly, obj_el in src_objs:
                    lines.extend(serialize_object(obj_el, lx, ly, next_id()))
                lines.append(" </objectgroup>")

    lines.append("</map>")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ── portal catalog writer ─────────────────────────────────────────────────────

def write_catalog(catalog_entries: list, exit_type_counts: dict):
    TYPE_ORDER = [EXIT_UP, EXIT_DOWN, EXIT_ENTER, EXIT_EXIT, EXIT_HORIZ]

    def sort_key(line: str) -> int:
        for i, t in enumerate(TYPE_ORDER):
            if f"[{t}" in line:
                return i
        return len(TYPE_ORDER)

    sorted_entries = sorted(catalog_entries, key=sort_key)
    cat_path = os.path.join(OUTPUT_XML, CATALOG_FILE)
    with open(cat_path, "w", encoding="utf-8") as fh:
        fh.write("portal_exits_catalog.txt\n")
        fh.write("Generated by stendhal_to_andorstrail_v7.py\n")
        fh.write("=" * 140 + "\n")
        fh.write(f"{'SOURCE CHUNK':60s}   {'TARGET CHUNK':60s}  "
                 f"{'[TYPE]':20s}  NAME / PLACE\n")
        fh.write("-" * 140 + "\n")
        prev_type = None
        for line in sorted_entries:
            cur_type = EXIT_HORIZ
            for t in TYPE_ORDER:
                if f"[{t}" in line:
                    cur_type = t
                    break
            if prev_type is not None and cur_type != prev_type:
                fh.write("\n")
            fh.write(line + "\n")
            prev_type = cur_type
        fh.write("\n" + "=" * 140 + "\n")
        fh.write("EXIT TYPE SUMMARY\n")
        fh.write("-" * 40 + "\n")
        for t in TYPE_ORDER:
            fh.write(f"  {t:20s}: {exit_type_counts.get(t, 0)}\n")
        fh.write(f"  {'TOTAL':20s}: {sum(exit_type_counts.values())}\n")
    print(f"  wrote {cat_path}")
    return cat_path


# ── worldmap.xml ──────────────────────────────────────────────────────────────

def write_worldmap(output_records: list):
    by_level: dict = defaultdict(list)
    for rec in output_records:
        by_level[rec["level"]].append(rec)

    wm_root = ET.Element("worldmap")

    def seg_id(level: int) -> str:
        return "level_interior" if level == INT_LEVEL else f"level_{level}"

    for level in sorted(by_level.keys()):
        recs  = by_level[level]
        min_x = min(r["world_tx"] for r in recs)
        min_y = min(r["world_ty"] for r in recs)
        seg   = ET.SubElement(wm_root, "segment")
        seg.set("id", seg_id(level))
        seg.set("x",  str(min_x))
        seg.set("y",  str(min_y))
        for zone_name in sorted({r["zone"] for r in recs}):
            na = ET.SubElement(seg, "namedarea")
            na.set("id",   zone_name)
            readable = re.sub(r"^-?\d+_", "", zone_name).replace("_", " ").title()
            na.set("name", readable)
            na.set("type", "other")
        for r in sorted(recs, key=lambda d: (d["world_tx"], d["world_ty"])):
            me = ET.SubElement(seg, "map")
            me.set("id",   r["map_id"])
            me.set("x",    str(r["world_tx"] - min_x))
            me.set("y",    str(r["world_ty"] - min_y))
            me.set("area", r["zone"])

    def _indent(el, lv=0):
        pad = "\n" + "  " * lv
        if len(el):
            if not (el.text and el.text.strip()):
                el.text = pad + "  "
            for ch in el:
                _indent(ch, lv+1)
                ch.tail = pad + "  " if ch is not el[-1] else pad
        if lv and not (el.tail and el.tail.strip()):
            el.tail = pad
    _indent(wm_root)

    wm_path = os.path.join(OUTPUT_XML, "worldmap.xml")
    with open(wm_path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n')
        fh.write(ET.tostring(wm_root, encoding="unicode") + "\n")
    print(f"  wrote {wm_path}")
    return wm_path


# ── v7: Mapevents writeback helpers ──────────────────────────────────────────

def collect_zone_writeback_events(zones_tmx: list,
                                  chunk_records: list) -> dict:
    """
    Build a per-zone-name dict of all Mapevents objects that should be written
    back into each source Stendhal TMX file.

    Each entry in the returned dict maps zone_name ->
        list of dicts with keys:
            name, exit_type, x_px, y_px, w_px, h_px,
            target_map (str or None), place (str or None),
            raw_el (ET.Element or None)   <- for non-exit original objects

    Coordinates are zone-global pixel coords (chunk-local + chunk_offset).
    """
    zone_events: dict = defaultdict(list)

    for rec in chunk_records:
        zone_name = rec["zone_name"]
        ox_px     = rec["ox"] * TILE_PX   # chunk tile-origin -> pixels
        oy_px     = rec["oy"] * TILE_PX

        # ── generated exits (mapchange objects) ──────────────────────────────
        for ex in rec["exits"]:
            zone_events[zone_name].append({
                "name":       ex["name"],
                "exit_type":  ex.get("exit_type", EXIT_HORIZ),
                "x_px":       ex["x"] + ox_px,
                "y_px":       ex["y"] + oy_px,
                "w_px":       ex["w"],
                "h_px":       ex["h"],
                "target_map": ex.get("target_map"),
                "place":      ex.get("place"),
                "raw_el":     None,
            })

        # ── original Mapevents objects carried over from source TMX ──────────
        for lx, ly, obj_el in rec["existing_me"]:
            # lx/ly are already chunk-local pixel offsets from objects_for_chunk
            zone_events[zone_name].append({
                "name":       obj_el.get("name", ""),
                "exit_type":  (obj_el.get("type", "") or obj_el.get("class", "")),
                "x_px":       lx + ox_px,
                "y_px":       ly + oy_px,
                "w_px":       safe_int(obj_el.get("width",  str(TILE_PX))),
                "h_px":       safe_int(obj_el.get("height", str(TILE_PX))),
                "target_map": None,
                "place":      None,
                "raw_el":     obj_el,
            })

    return dict(zone_events)


def _build_mapevents_xml_block(events: list, indent: str = " ") -> list:
    """
    Render a list of zone-global Mapevents objects as XML lines.
    Uses the AT content format from 1_Maps.txt:
      - <objectgroup name="Mapevents">
      -   <object name="..." type="mapchange[_up/_down/_enter/_exit]"
                  id="N" x="X" y="Y" width="W" height="H">
      -     <properties>
      -       <property name="map"   value="target_map_id"/>
      -       <property name="place" value="place_name"/>
      -     </properties>
      -   </object>
      - </objectgroup>
    """
    i2 = indent + " "
    i3 = indent + "  "
    i4 = indent + "   "
    lines = [f'{indent}<objectgroup name="Mapevents">']
    obj_id = 1
    seen_names: set = set()

    for ev in events:
        name = ev["name"] or ""
        # Deduplicate: if a name was already emitted, append a numeric suffix
        base_name = name
        suffix_n  = 1
        while name in seen_names:
            name = f"{base_name}_{suffix_n}"
            suffix_n += 1
        seen_names.add(name)

        etype = ev["exit_type"] or EXIT_HORIZ
        x     = int(ev["x_px"])
        y     = int(ev["y_px"])
        w     = int(ev["w_px"])
        h     = int(ev["h_px"])

        raw_el = ev.get("raw_el")

        if raw_el is not None:
            # Reproduce the original object with corrected coords & id
            attrs = dict(raw_el.attrib)
            attrs["name"] = name
            attrs["x"]    = str(x)
            attrs["y"]    = str(y)
            attrs["id"]   = str(obj_id)
            obj_id += 1
            ordered_keys = []
            for k in ("name", "type", "id", "x", "y", "width", "height"):
                if k in attrs:
                    ordered_keys.append(k)
            for k in attrs:
                if k not in ordered_keys:
                    ordered_keys.append(k)
            attr_str = " ".join(f'{k}="{attrs[k]}"' for k in ordered_keys if k in attrs)
            if list(raw_el):
                lines.append(f'{i2}<object {attr_str}>')
                for child in raw_el:
                    lines.append(i3 + ET.tostring(child, encoding="unicode").strip())
                lines.append(f'{i2}</object>')
            else:
                lines.append(f'{i2}<object {attr_str}/>')
        else:
            # Generated mapchange exit
            lines.append(
                f'{i2}<object name="{name}" type="{etype}" '
                f'id="{obj_id}" '
                f'x="{x}" y="{y}" width="{w}" height="{h}">'
            )
            obj_id += 1
            tgt = ev.get("target_map") or ""
            plc = ev.get("place") or ""
            if tgt or plc:
                lines.append(f'{i3}<properties>')
                if tgt:
                    lines.append(f'{i4}<property name="map"   value="{tgt}"/>')
                if plc:
                    lines.append(f'{i4}<property name="place" value="{plc}"/>')
                lines.append(f'{i3}</properties>')
            lines.append(f'{i2}</object>')

    lines.append(f'{indent}</objectgroup>')
    return lines


def write_mapevents_to_source_tmx(zones_tmx: list,
                                  zone_writeback_events: dict) -> dict:
    """
    v7: For each zone that has writeback events, open the source Stendhal
    TMX file, replace (or append) its <objectgroup name="Mapevents"> block
    with the newly computed one, and write it back in-place.

    The function preserves:
      - All other objectgroups (Spawn, Keys, Replace, any custom groups)
      - All layer data (unchanged)
      - All tileset declarations (unchanged)
      - Map properties (unchanged)
      - Original Mapevents naming conventions from the source file
      - Mapevents objects that originated in the source file (re-inserted
        via the raw_el path with zone-global corrected coordinates)

    Returns a dict with keys: updated, skipped, errors, total_objects
    """
    updated = 0
    skipped = 0
    errors  = 0
    total_objects = 0

    # Build a map from zone_name -> source TMX absolute path
    zone_path_map: dict = {}
    for z, _ in zones_tmx:
        fpath = resolve_tmx(z.tmx_rel)
        if fpath:
            zone_path_map[z.name] = os.path.abspath(fpath)

    for zone_name, events in zone_writeback_events.items():
        if not events:
            skipped += 1
            continue

        src_path = zone_path_map.get(zone_name)
        if not src_path or not os.path.isfile(src_path):
            print(f"  [WARN] writeback: source TMX not found for zone '{zone_name}'")
            errors += 1
            continue

        try:
            # Read the raw file text so we can do a surgical replacement
            with open(src_path, "r", encoding="utf-8") as fh:
                original_text = fh.read()

            # Generate the new Mapevents block (single-space indent to match
            # the typical Stendhal TMX style where objectgroups are at top level)
            new_block_lines = _build_mapevents_xml_block(events, indent=" ")
            new_block = "\n".join(new_block_lines)

            # Check whether the file already contains a Mapevents objectgroup
            me_pattern = re.compile(
                r'[ \t]*<objectgroup[^>]*name\s*=\s*["\']Mapevents["\'][^>]*>'
                r'.*?</objectgroup>',
                re.DOTALL | re.IGNORECASE,
            )
            # Also match self-closing form: <objectgroup name="Mapevents"/>
            me_self_close = re.compile(
                r'[ \t]*<objectgroup[^>]*name\s*=\s*["\']Mapevents["\'][^/]*/\s*>',
                re.IGNORECASE,
            )

            if me_pattern.search(original_text):
                new_text = me_pattern.sub(new_block, original_text, count=1)
            elif me_self_close.search(original_text):
                new_text = me_self_close.sub(new_block, original_text, count=1)
            else:
                # Insert just before </map>
                new_text = original_text.rstrip()
                if new_text.endswith("</map>"):
                    new_text = new_text[:-len("</map>")] + new_block + "\n</map>"
                else:
                    new_text = new_text + "\n" + new_block + "\n</map>"

            with open(src_path, "w", encoding="utf-8") as fh:
                fh.write(new_text)

            updated += 1
            total_objects += len(events)
            print(f"  wrote Mapevents ({len(events)} objects) -> {src_path}")

        except Exception as exc:
            print(f"  [ERROR] writeback failed for '{zone_name}': {exc}")
            errors += 1

    return {
        "updated":       updated,
        "skipped":       skipped,
        "errors":        errors,
        "total_objects": total_objects,
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  Stendhal -> Andor's Trail map converter  v7")
    print("  (no blank tiles * correct edge sizes * bidirectional exits * mapevents writeback)")
    print("=" * 70)

    if not os.path.isdir(STENDHAL_ZONE_CONF) and not os.path.isdir(DATA_MAPS_BASE):
        sys.exit(
            f"\n[ERROR] Cannot find '{STENDHAL_ZONE_CONF}' or '{DATA_MAPS_BASE}'.\n"
            "Run this script from the Stendhal project root directory."
        )

    os.makedirs(OUTPUT_XML,      exist_ok=True)
    os.makedirs(OUTPUT_DRAWABLE, exist_ok=True)
    os.makedirs(OUTPUT_VALUES,   exist_ok=True)

    # ── 1. Zone configs ────────────────────────────────────────────
    print(f"\n[1/8] Loading zone configs from {STENDHAL_ZONE_CONF} ...")
    zones = load_zone_configs(STENDHAL_ZONE_CONF)
    levels_found   = sorted({z.level for z in zones if z.level != INT_LEVEL})
    interior_count = sum(1 for z in zones if z.level == INT_LEVEL)
    print(f"      Found {len(zones)} zones total")
    print(f"        World levels  : {levels_found}")
    print(f"        Interior (int): {interior_count} zones")
    if not zones:
        zones = fallback_scan()
    if not zones:
        sys.exit("[ERROR] No zones or TMX files found. Aborting.")

    # ── 2. Load TMX files ──────────────────────────────────────────
    print(f"\n[2/8] Loading {len(zones)} TMX files ...")
    zones_tmx, skipped = [], 0
    for z in zones:
        fpath = resolve_tmx(z.tmx_rel)
        if not fpath:
            print(f"  [WARN] Not found: {z.tmx_rel}")
            skipped += 1
            continue
        try:
            stmx = StendhalTMX(fpath)
            if stmx.width == 0 or stmx.height == 0:
                print(f"  [WARN] {fpath}: map has zero width or height "
                      f"({stmx.width}x{stmx.height}) -- skipping")
                skipped += 1
                continue
            zones_tmx.append((z, stmx))
        except Exception as e:
            print(f"  [WARN] {fpath}: {e}")
            skipped += 1
    print(f"  Loaded {len(zones_tmx)} maps  ({skipped} skipped)")
    at_native_count = sum(
        1 for _, s in zones_tmx if any(ts["is_at_native"] for ts in s.tilesets))
    print(f"  AT-native tileset maps: {at_native_count}")
    if not zones_tmx:
        sys.exit("[ERROR] No maps loaded.")

    # ── 3. Portal / interior exits ─────────────────────────────────
    print(f"\n[3/8] Parsing ALL portal definitions ...")
    zone_by_name = {z.name: z for z, _ in zones_tmx}
    portal_links, raw_portals, tmx_fb_count = load_zone_portals(
        STENDHAL_ZONE_CONF, list(zone_by_name.values()))
    print(f"  Raw portal refs (XML):   {len(raw_portals)}")
    print(f"  Total PortalLinks built: {len(portal_links)}")

    # ── 4. Chunk plan ──────────────────────────────────────────────
    print(f"\n[4/8] Building 32x32 chunk plan (tracking actual edge sizes) ...")
    chunks, id_to_wpos, wpos_to_id = build_chunk_plan(zones_tmx)
    all_ids     = set(cd["map_id"] for cd in chunks)
    neighbor_fn = make_neighbor_fn(id_to_wpos, wpos_to_id)

    edge_chunks = sum(
        1 for cd in chunks
        if cd["actual_w"] < AT_W or cd["actual_h"] < AT_H
    )
    print(f"  {len(chunks)} AT chunks from {len(zones_tmx)} Stendhal zones")
    print(f"  Edge chunks (smaller than 32x32): {edge_chunks}")

    chunk_tile_map = build_chunk_tile_map(chunks)

    portal_exits, portal_used_names, catalog_entries = build_portal_exits(
        portal_links, zone_by_name, chunk_tile_map)
    total_pe = sum(len(v) for v in portal_exits.values())
    print(f"  Resolved {total_pe} portal exits across {len(portal_exits)} chunks")

    exit_type_counts: dict = defaultdict(int)
    for ex_list in portal_exits.values():
        for ex in ex_list:
            exit_type_counts[ex.get("exit_type", EXIT_HORIZ)] += 1

    # ── 5. Write AT TMX chunks ─────────────────────────────────────
    print(f"\n[5/8] Writing AT TMX map files (skipping blank chunks) ...")
    output_records  = []
    blank_skipped   = 0
    # v7: accumulate per-chunk writeback data
    chunk_writeback_records = []

    for cd in chunks:
        map_id         = cd["map_id"]
        stmx           = cd["stmx"]
        ox, oy         = cd["ox"], cd["oy"]
        zone           = cd["zone"]
        actual_w       = cd["actual_w"]
        actual_h       = cd["actual_h"]

        registry = TilesetRegistry()
        for ts in stmx.tilesets:
            if ts["abs_img"] and ts["drawable_name"]:
                registry.register(ts)
                copy_tileset(ts["abs_img"], ts["drawable_name"])

        # Build layer tiles using actual chunk dimensions
        layer_tiles: dict = {}
        for st_lname, at_lname in STENDHAL_TO_AT.items():
            src = stmx.layers.get(st_lname, [])
            out = []
            for ty in range(actual_h):
                for tx in range(actual_w):
                    sx, sy = ox + tx, oy + ty
                    if st_lname == "collision":
                        if sx >= stmx.width or sy >= stmx.height:
                            out.append(registry.first_blocked_gid(stmx))
                        else:
                            raw = tile_at(src, sx, sy, stmx.width)
                            out.append(registry.remap(raw, stmx))
                    else:
                        raw = tile_at(src, sx, sy, stmx.width) if src else 0
                        out.append(registry.remap(raw, stmx))
            layer_tiles[at_lname] = out

        # v6: skip entirely blank chunks
        if is_blank_chunk(layer_tiles, actual_w, actual_h):
            blank_skipped += 1
            print(f"  [BLANK] skipped  {map_id}  ({actual_w}x{actual_h} tiles, all empty)")
            continue

        chunk_portal_exits = portal_exits.get(map_id, [])
        chunk_used         = portal_used_names.get(map_id, set())
        exits = generate_exits(map_id, layer_tiles["Walkable"],
                               all_ids, neighbor_fn,
                               chunk_portal_exits, chunk_used,
                               chunk_w=actual_w, chunk_h=actual_h)

        # v7: capture existing Mapevents from source for writeback
        existing_me = objects_for_chunk(
            stmx.objectgroups.get("Mapevents", []), ox, oy, actual_w, actual_h)

        fname    = f"{map_id}.tmx"
        out_path = os.path.join(OUTPUT_XML, fname)
        write_at_tmx(out_path, layer_tiles, registry, exits,
                     stmx.objectgroups, ox, oy,
                     chunk_w=actual_w, chunk_h=actual_h)

        output_records.append({
            "map_id":   map_id, "fname":    fname,
            "zone":     zone.name, "level":    zone.level,
            "world_tx": cd["world_tx"], "world_ty": cd["world_ty"],
            "actual_w": actual_w, "actual_h": actual_h,
        })

        # v7: record this chunk's exits and original objects for source writeback
        chunk_writeback_records.append({
            "zone_name":   zone.name,
            "ox":          ox,
            "oy":          oy,
            "actual_w":    actual_w,
            "actual_h":    actual_h,
            "exits":       exits,
            "existing_me": existing_me,
        })

        n_portal = len(chunk_portal_exits)
        n_horiz  = len(exits) - n_portal
        spawn_n   = len(objects_for_chunk(stmx.objectgroups.get("Spawn",   []), ox, oy, actual_w, actual_h))
        keys_n    = len(objects_for_chunk(stmx.objectgroups.get("Keys",    []), ox, oy, actual_w, actual_h))
        replace_n = len(objects_for_chunk(stmx.objectgroups.get("Replace", []), ox, oy, actual_w, actual_h))
        size_tag  = f"{actual_w}x{actual_h}" if (actual_w < AT_W or actual_h < AT_H) else "32x32"
        detail = f" [{size_tag}]"
        if n_portal:   detail += f", {n_portal} portal exits"
        if spawn_n:    detail += f", Spawn:{spawn_n}"
        if keys_n:     detail += f", Keys:{keys_n}"
        if replace_n:  detail += f", Replace:{replace_n}"
        print(f"  wrote  {fname}  ({n_horiz} edge exits{detail})")

    # ── 6. Resource files ──────────────────────────────────────────
    print(f"\n[6/8] Writing resource files ...")
    wm_path = write_worldmap(output_records)

    lr_root = ET.Element("resources")
    arr     = ET.SubElement(lr_root, "array")
    arr.set("name", "loadresource_maps")
    for rec in sorted(output_records, key=lambda d: d["map_id"]):
        ET.SubElement(arr, "item").text = f"@xml/{rec['map_id']}"

    def _indent2(el, lv=0):
        pad = "\n" + "  " * lv
        if len(el):
            if not (el.text and el.text.strip()):
                el.text = pad + "  "
            for ch in el:
                _indent2(ch, lv+1)
                ch.tail = pad + "  " if ch is not el[-1] else pad
        if lv and not (el.tail and el.tail.strip()):
            el.tail = pad
    _indent2(lr_root)

    lr_path = os.path.join(OUTPUT_VALUES, "loadresources.xml")
    with open(lr_path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write(ET.tostring(lr_root, encoding="unicode") + "\n")
    print(f"  wrote {lr_path}")

    world_obj = {
        "type": "world",
        "maps": [
            {"fileName": f"res/xml/{r['fname']}",
             "x": r["world_tx"] * TILE_PX,
             "y": r["world_ty"] * TILE_PX,
             "width":  r["actual_w"] * TILE_PX,
             "height": r["actual_h"] * TILE_PX}
            for r in output_records
        ],
    }
    with open(WORLD_FILE, "w", encoding="utf-8") as fh:
        json.dump(world_obj, fh, indent=2)
    print(f"  wrote {WORLD_FILE}")

    cat_path = write_catalog(catalog_entries, dict(exit_type_counts))

    # ── 7. Mapevents writeback to source Stendhal TMX ──────────────
    print(f"\n[7/8] Writing Mapevents objectgroup back to source Stendhal TMX files ...")
    zone_writeback_events = collect_zone_writeback_events(
        zones_tmx, chunk_writeback_records)
    writeback_counts = write_mapevents_to_source_tmx(
        zones_tmx, zone_writeback_events)
    print(f"  Updated : {writeback_counts['updated']} source TMX files")
    print(f"  Skipped : {writeback_counts['skipped']}  (no mapevents to write)")
    print(f"  Errors  : {writeback_counts['errors']}")

    # ── 8. Summary ────────────────────────────────────────────────
    print(f"\n[8/8] Done!")
    print("=" * 70)
    world_levels = sorted(
        {r["level"] for r in output_records if r["level"] != INT_LEVEL})
    int_chunks = sum(1 for r in output_records if r["level"] == INT_LEVEL)
    print(f"  World levels      : {world_levels}")
    print(f"  Interior chunks   : {int_chunks}  (segment 'level_interior')")
    print(f"  AT map chunks     : {len(output_records)}  ->  {OUTPUT_XML}/")
    print(f"  Blank chunks skip : {blank_skipped}  (all-empty, no TMX written)")
    print(f"  Edge chunks       : {edge_chunks}  (smaller than 32x32, sized to fit)")
    print(f"  Tileset images    : {len(_copied)}  ->  {OUTPUT_DRAWABLE}/")
    print(f"  Portal exits total: {total_pe}  (both sides of every portal)")
    print(f"    {EXIT_UP:20s}: {exit_type_counts.get(EXIT_UP,   0)}")
    print(f"    {EXIT_DOWN:20s}: {exit_type_counts.get(EXIT_DOWN, 0)}")
    print(f"    {EXIT_ENTER:20s}: {exit_type_counts.get(EXIT_ENTER,0)}")
    print(f"    {EXIT_EXIT:20s}: {exit_type_counts.get(EXIT_EXIT, 0)}")
    print(f"    {EXIT_HORIZ:20s}: {exit_type_counts.get(EXIT_HORIZ,0)}")
    print(f"  worldmap.xml      : {wm_path}")
    print(f"  loadresources     : {lr_path}")
    print(f"  Tiled world       : {WORLD_FILE}")
    print(f"  Portal catalog    : {cat_path}")
    print(f"  Mapevents writeback:")
    print(f"    Updated sources : {writeback_counts['updated']}")
    print(f"    Total objects   : {writeback_counts['total_objects']}")
    print("=" * 70)

    if total_pe == 0:
        print()
        print("  *** Portal exits are still 0. ***")
        print("  The [PROBE] output above shows what tags exist inside <zone>")
        print("  elements. If no portal tags appeared, Stendhal's portals for")
        print("  your version are defined only in Java source code and not")
        print("  exported to XML or TMX files.")
        print()
        print("  To add portal data manually, create a file named")
        print("  'portals_override.json' in the Stendhal project root with")
        print("  the structure below, then re-run the script:")
        print()
        print('  [')
        print('    {"src_zone":"0_semos_city","src_x":19,"src_y":4,')
        print('     "tgt_zone":"int_semos_bank","tgt_x":2,"tgt_y":3},')
        print('    ...')
        print('  ]')
        _try_load_portals_override(portal_links, zone_by_name, chunk_tile_map,
                                   portal_exits, portal_used_names, catalog_entries,
                                   exit_type_counts)


def _try_load_portals_override(portal_links, zone_by_name, chunk_tile_map,
                               portal_exits, portal_used_names, catalog_entries,
                               exit_type_counts):
    override_path = "portals_override.json"
    if not os.path.isfile(override_path):
        return
    try:
        with open(override_path, encoding="utf-8") as fh:
            data = json.load(fh)
        print(f"  Found {override_path} with {len(data)} entries.")
        print("  Re-run the script -- override portals will be loaded automatically.")
    except Exception as e:
        print(f"  [WARN] Could not parse {override_path}: {e}")


if __name__ == "__main__":
    # ── Optional portals_override.json pre-load hook ───────────────────────
    _OVERRIDE_FILE = "portals_override.json"

    _original_load = load_zone_portals

    def load_zone_portals_with_override(conf_dir, zones):
        portals, raw, tmx_cnt = _original_load(conf_dir, zones)
        if not os.path.isfile(_OVERRIDE_FILE):
            return portals, raw, tmx_cnt
        zone_by_name = {z.name: z for z in zones}
        try:
            with open(_OVERRIDE_FILE, encoding="utf-8") as fh:
                overrides = json.load(fh)
            added = 0
            for entry in overrides:
                sz  = entry.get("src_zone", "")
                sx  = safe_int(entry.get("src_x", -1), -1)
                sy  = safe_int(entry.get("src_y", -1), -1)
                tz  = entry.get("tgt_zone", "")
                tx  = safe_int(entry.get("tgt_x", -1), -1)
                ty  = safe_int(entry.get("tgt_y", -1), -1)
                if not sz or not tz or sx < 0 or sy < 0:
                    continue
                src_obj   = zone_by_name.get(sz)
                tgt_obj   = zone_by_name.get(tz)
                if src_obj is None or tgt_obj is None:
                    continue
                etype = classify_exit_type(src_obj.level, tgt_obj.level)
                portals.append(PortalLink(
                    sz, sx, sy, tz,
                    tx if tx >= 0 else None,
                    ty if ty >= 0 else None,
                    src_ref=entry.get("src_ref") or None,
                    tgt_ref=entry.get("tgt_ref") or None,
                    exit_type=etype,
                ))
                added += 1
            print(f"  [OVERRIDE] Loaded {added} portal links from {_OVERRIDE_FILE}")
        except Exception as e:
            print(f"  [WARN] portals_override.json error: {e}")
        return portals, raw, tmx_cnt

    load_zone_portals = load_zone_portals_with_override

    main()
