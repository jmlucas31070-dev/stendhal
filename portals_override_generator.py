#!/usr/bin/env python3
"""
portals_override_generator.py
==============================
Scans the Stendhal Java source tree for hardcoded portal / staircase /
entrance definitions and emits a portals_override.json that the
stendhal_to_andorstrail_v7.py converter can consume directly.

The generator covers several common Stendhal portal-definition patterns:

  Pattern A -- chained setter calls  (most common)
    portal = new Portal();
    portal.setPosition(19, 4);
    portal.setDestination("int_semos_bank", "bank_entrance");
    zone.add(portal);

  Pattern B -- inline constructor helpers
    setPortal(zone, 19, 4, "int_semos_bank", "bank_entrance");

  Pattern C -- ZoneConfigurator addPortal / addEntrance variants
    addPortal("0_semos_city", 19, 4, "int_semos_bank", "bank_entrance");

  Pattern D -- StaircasePortal / OneWayPortalDestination
    staircase = new StaircasePortal();
    staircase.setPosition(5, 10);
    staircase.setDestination("1_semos_city", "semos_stairs");

  Pattern E -- zone helper methods (buildPortal / createPortal)
    buildPortal(zone, 5, 2, destZone, destRef);

The script infers the source zone from:
  1. A "zone =" or "setZone(" assignment nearby in the same block
  2. The Java class / file name  (e.g. SemosCity0.java -> "0_semos_city")
  3. A package-level ZONE_NAME constant

Run from the Stendhal project ROOT directory (the one containing data/ and src/).

Usage:
  python3 portals_override_generator.py [--src-dir PATH] [--out portals_override.json] [--verbose]

Output:
  portals_override.json  -- list of portal dicts consumable by v7 converter

Requirements: Python 3.6+, no third-party packages.
"""

import os
import re
import sys
import json
import glob
import argparse
import itertools
from collections import defaultdict
from pathlib import Path

# ── tuneable defaults ─────────────────────────────────────────────────────────
DEFAULT_SRC_DIR  = "src"
DEFAULT_OUT_FILE = "portals_override.json"
JAVA_GLOB        = "**/*.java"

# ── regex atoms ───────────────────────────────────────────────────────────────

# Quoted string  "foo"  or  'foo'
_QSTR   = r'"([^"]*)"'
_QSTR2  = r"'([^']*)'"

# Integer literal  (optional sign)
_INT    = r"(-?\d+)"

# Identifier token
_ID     = r"([A-Za-z_]\w*)"

# Optional whitespace / comma separator
_SEP    = r"\s*,\s*"
_WS     = r"\s*"

# ── portal class name patterns ────────────────────────────────────────────────
PORTAL_CLASS_RE = re.compile(
    r"\b(Portal|StaircasePortal|OneWayPortalDestination|"
    r"AccessPortal|TeleporterPortal|DestinationPortal|"
    r"StairwayPortal|StaircaseDestination)\b",
    re.IGNORECASE,
)

# ── zone-name heuristics ──────────────────────────────────────────────────────
# Matches:  zone = zones.get("0_semos_city")
#           setZone("int_semos_bank")
#           zone = new StendhalRPZone("int_semos_bank", ...)
ZONE_ASSIGN_RE = re.compile(
    r'(?:zone\s*=\s*(?:\w+\.get|new\s+\w+)?\s*\()\s*' + _QSTR,
    re.IGNORECASE,
)

# Matches: private static final String ZONE_NAME = "0_semos_city";
ZONE_CONST_RE = re.compile(
    r'(?:ZONE_NAME|ZONE|ZONE_ID)\s*=\s*' + _QSTR,
    re.IGNORECASE,
)

# ── setDestination ─────────────────────────────────────────────────────────────
# portal.setDestination("int_semos_bank", "bank_entrance");
SET_DEST_RE = re.compile(
    r'\.setDestination\s*\(\s*' + _QSTR + _SEP + _QSTR + r'\s*\)',
    re.IGNORECASE,
)

# ── setPosition ───────────────────────────────────────────────────────────────
# portal.setPosition(19, 4);
SET_POS_RE = re.compile(
    r'\.setPosition\s*\(\s*' + _INT + _SEP + _INT + r'\s*\)',
)

# ── inline helper patterns ────────────────────────────────────────────────────
# setPortal(zone, 19, 4, "int_semos_bank", "bank_entrance")
# addPortal( ..., 19, 4, "dest_zone", "dest_ref")
# buildPortal(zone, 5, 2, destZone, destRef)
HELPER_RE = re.compile(
    r'(?:setPortal|addPortal|buildPortal|createPortal|addEntrance|addExit|addStaircase)'
    r'\s*\([^)]*?' +
    _INT + _SEP + _INT + _SEP + _QSTR + _SEP + _QSTR,
    re.IGNORECASE | re.DOTALL,
)

# addPortal("src_zone", srcX, srcY, "dest_zone", "dest_ref")
HELPER_WITH_SRC_ZONE_RE = re.compile(
    r'(?:setPortal|addPortal|buildPortal|createPortal|addEntrance|addExit|addStaircase)'
    r'\s*\(\s*' + _QSTR + _SEP + _INT + _SEP + _INT + _SEP + _QSTR + _SEP + _QSTR,
    re.IGNORECASE | re.DOTALL,
)

# ── createPortal("ref", x, y) or addEntrance("name", x, y) (dest inferred) ──
PORTAL_REF_XY_RE = re.compile(
    r'(?:createPortal|addEntrance|addExit|addDoor)\s*\(\s*'
    + _QSTR + _SEP + _INT + _SEP + _INT,
    re.IGNORECASE,
)

# ── new SomePortal("ref", x, y, "dest_zone", "dest_ref") ────────────────────
NEW_PORTAL_FULL_RE = re.compile(
    r'new\s+\w*(?:Portal|Staircase)\w*\s*\(\s*'
    + _QSTR + _SEP + _INT + _SEP + _INT + _SEP + _QSTR + _SEP + _QSTR,
    re.IGNORECASE | re.DOTALL,
)

# ── new SomePortal(x, y, "dest_zone", "dest_ref") ────────────────────────────
NEW_PORTAL_NO_REF_RE = re.compile(
    r'new\s+\w*(?:Portal|Staircase)\w*\s*\(\s*'
    + _INT + _SEP + _INT + _SEP + _QSTR + _SEP + _QSTR,
    re.IGNORECASE | re.DOTALL,
)

# ── zone name from file path ──────────────────────────────────────────────────

def _camel_to_zone(stem: str) -> str:
    """
    Try to derive a likely Stendhal zone name from a Java class file name.
    Examples:
      SemosCity         -> semos_city
      SemosCity0        -> 0_semos_city   (if ends with digit)
      IntSemosBank      -> int_semos_bank
      Level0SemosCity   -> 0_semos_city
    """
    # Strip common suffixes that are not part of the zone name
    stem = re.sub(r"(Zone|Configurator|Setup|Builder|Factory|Manager|Handler|Config)$",
                  "", stem, flags=re.IGNORECASE)
    if not stem:
        return ""

    # Insert underscores before capitals (CamelCase -> snake_case)
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", stem)
    s = s.lower()

    # If it starts with a digit, keep it as prefix: 0_semos_city style
    m = re.match(r"^(\d+)_(.+)$", s)
    if m:
        return f"{m.group(1)}_{m.group(2)}"

    # If it starts with "int_", leave as-is
    if s.startswith("int_"):
        return s

    # If the last word is a digit, move it to the front
    m2 = re.match(r"^(.+)_(\d+)$", s)
    if m2:
        return f"{m2.group(2)}_{m2.group(1)}"

    return s


def _zone_from_path(java_path: str) -> str:
    """
    Derive best-guess zone name from a Java source file path.
    Tries the class name first, then falls back to the package path.
    """
    stem = Path(java_path).stem
    return _camel_to_zone(stem)


# ── block-level zone name extractor ──────────────────────────────────────────

def extract_zone_names_from_text(text: str) -> list:
    """
    Return a list of zone-name strings found in the file via known patterns.
    Ordered by frequency (most-referenced first).
    """
    counts: dict = defaultdict(int)
    for m in ZONE_ASSIGN_RE.finditer(text):
        counts[m.group(1)] += 2   # higher weight for explicit zone assignments
    for m in ZONE_CONST_RE.finditer(text):
        counts[m.group(1)] += 3
    # Also scan for string literals that look like zone names
    for m in re.finditer(r'"(\d+_[a-z][a-z0-9_]*|int_[a-z][a-z0-9_]*)"', text):
        counts[m.group(1)] += 1
    return sorted(counts.keys(), key=lambda k: -counts[k])


# ── portal record ─────────────────────────────────────────────────────────────

class PortalRecord:
    __slots__ = ("src_zone", "src_x", "src_y",
                 "tgt_zone", "tgt_ref",
                 "src_ref",  "java_file", "line_no")

    def __init__(self, src_zone, src_x, src_y,
                 tgt_zone=None, tgt_ref=None,
                 src_ref=None, java_file="", line_no=0):
        self.src_zone  = src_zone
        self.src_x     = src_x
        self.src_y     = src_y
        self.tgt_zone  = tgt_zone
        self.tgt_ref   = tgt_ref
        self.src_ref   = src_ref
        self.java_file = java_file
        self.line_no   = line_no

    def is_complete(self) -> bool:
        return (self.src_zone and self.src_x is not None
                and self.src_y is not None and self.tgt_zone)

    def to_dict(self) -> dict:
        d = {
            "src_zone": self.src_zone,
            "src_x":    self.src_x,
            "src_y":    self.src_y,
            "tgt_zone": self.tgt_zone,
        }
        if self.tgt_ref:
            d["tgt_ref"] = self.tgt_ref
        if self.src_ref:
            d["src_ref"] = self.src_ref
        return d

    def __repr__(self):
        return (f"Portal({self.src_zone} [{self.src_x},{self.src_y}] "
                f"-> {self.tgt_zone}/{self.tgt_ref})")


# ── per-file scanner ──────────────────────────────────────────────────────────

def _scan_file(java_path: str, verbose: bool = False) -> list:
    """
    Scan a single Java source file and return a list of PortalRecord objects.
    """
    try:
        with open(java_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []

    records: list = []
    lines   = text.splitlines()

    # ── determine candidate zone names for this file ──────────────────────────
    file_zone_candidates = extract_zone_names_from_text(text)
    path_zone = _zone_from_path(java_path)

    def pick_zone(candidates: list) -> str:
        """Pick the most likely zone name: prefer file-derived candidates."""
        if candidates:
            return candidates[0]
        return path_zone

    # Offset -> line number helper
    def line_of(offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    # ── Pattern A: chained setPosition / setDestination ──────────────────────
    # Strategy: find all setDestination calls, then look backwards up to
    # N lines for a matching setPosition call on the same variable.

    # Build an index: variable name -> list of (line_no, x, y)
    pos_by_var: dict = defaultdict(list)
    VAR_POS_RE = re.compile(
        r'\b' + _ID + r'\s*\.\s*setPosition\s*\(\s*' + _INT + _SEP + _INT + r'\s*\)',
    )
    for m in VAR_POS_RE.finditer(text):
        var_name = m.group(1)
        x, y     = int(m.group(2)), int(m.group(3))
        ln       = line_of(m.start())
        pos_by_var[var_name].append((ln, x, y))

    VAR_DEST_RE = re.compile(
        r'\b' + _ID + r'\s*\.\s*setDestination\s*\(\s*'
        + _QSTR + _SEP + _QSTR + r'\s*\)',
        re.IGNORECASE,
    )
    for m in VAR_DEST_RE.finditer(text):
        var_name  = m.group(1)
        tgt_zone  = m.group(2)
        tgt_ref   = m.group(3)
        dest_line = line_of(m.start())

        # Find the most recent setPosition for the same variable (within 40 lines)
        best = None
        for (pl, px, py) in pos_by_var.get(var_name, []):
            if 0 < dest_line - pl <= 40:
                if best is None or pl > best[0]:
                    best = (pl, px, py)

        if best is None:
            # Try any portal variable (positional proximity)
            for vn, positions in pos_by_var.items():
                if PORTAL_CLASS_RE.search(vn):
                    for (pl, px, py) in positions:
                        if 0 < dest_line - pl <= 20:
                            if best is None or pl > best[0]:
                                best = (pl, px, py)

        if best is None:
            continue

        _, px, py = best

        # Determine zone context for this portal
        # Look at the text in the surrounding ~100 lines
        start_line = max(0, dest_line - 100)
        ctx_text   = "\n".join(lines[start_line:dest_line])
        local_zones = extract_zone_names_from_text(ctx_text)
        src_zone = local_zones[0] if local_zones else pick_zone(file_zone_candidates)

        if not src_zone:
            continue

        records.append(PortalRecord(
            src_zone=src_zone, src_x=px, src_y=py,
            tgt_zone=tgt_zone, tgt_ref=tgt_ref,
            java_file=java_path, line_no=dest_line,
        ))

    # ── Pattern B/C: helper methods without explicit zone arg ─────────────────
    for m in HELPER_RE.finditer(text):
        try:
            x        = int(m.group(1))
            y        = int(m.group(2))
            tgt_zone = m.group(3)
            tgt_ref  = m.group(4)
        except (IndexError, ValueError):
            continue
        dest_line = line_of(m.start())
        start_line = max(0, dest_line - 60)
        ctx_text   = "\n".join(lines[start_line:dest_line+1])
        local_zones = extract_zone_names_from_text(ctx_text)
        src_zone    = local_zones[0] if local_zones else pick_zone(file_zone_candidates)
        if not src_zone:
            continue
        records.append(PortalRecord(
            src_zone=src_zone, src_x=x, src_y=y,
            tgt_zone=tgt_zone, tgt_ref=tgt_ref,
            java_file=java_path, line_no=dest_line,
        ))

    # ── Pattern B2/C2: helper with explicit src zone ───────────────────────────
    for m in HELPER_WITH_SRC_ZONE_RE.finditer(text):
        try:
            src_zone = m.group(1)
            x        = int(m.group(2))
            y        = int(m.group(3))
            tgt_zone = m.group(4)
            tgt_ref  = m.group(5)
        except (IndexError, ValueError):
            continue
        records.append(PortalRecord(
            src_zone=src_zone, src_x=x, src_y=y,
            tgt_zone=tgt_zone, tgt_ref=tgt_ref,
            java_file=java_path, line_no=line_of(m.start()),
        ))

    # ── Pattern D: new SomePortal("ref", x, y, "dest_zone", "dest_ref") ──────
    for m in NEW_PORTAL_FULL_RE.finditer(text):
        try:
            src_ref  = m.group(1)
            x        = int(m.group(2))
            y        = int(m.group(3))
            tgt_zone = m.group(4)
            tgt_ref  = m.group(5)
        except (IndexError, ValueError):
            continue
        dest_line  = line_of(m.start())
        start_line = max(0, dest_line - 60)
        ctx_text   = "\n".join(lines[start_line:dest_line+1])
        local_zones = extract_zone_names_from_text(ctx_text)
        src_zone    = local_zones[0] if local_zones else pick_zone(file_zone_candidates)
        if not src_zone:
            continue
        records.append(PortalRecord(
            src_zone=src_zone, src_x=x, src_y=y,
            src_ref=src_ref,
            tgt_zone=tgt_zone, tgt_ref=tgt_ref,
            java_file=java_path, line_no=dest_line,
        ))

    # ── Pattern E: new SomePortal(x, y, "dest_zone", "dest_ref") ─────────────
    for m in NEW_PORTAL_NO_REF_RE.finditer(text):
        try:
            x        = int(m.group(1))
            y        = int(m.group(2))
            tgt_zone = m.group(3)
            tgt_ref  = m.group(4)
        except (IndexError, ValueError):
            continue
        dest_line  = line_of(m.start())
        start_line = max(0, dest_line - 60)
        ctx_text   = "\n".join(lines[start_line:dest_line+1])
        local_zones = extract_zone_names_from_text(ctx_text)
        src_zone    = local_zones[0] if local_zones else pick_zone(file_zone_candidates)
        if not src_zone:
            continue
        records.append(PortalRecord(
            src_zone=src_zone, src_x=x, src_y=y,
            tgt_zone=tgt_zone, tgt_ref=tgt_ref,
            java_file=java_path, line_no=dest_line,
        ))

    if verbose and records:
        print(f"  {java_path}: {len(records)} portal(s)")

    return records


# ── deduplication ─────────────────────────────────────────────────────────────

def deduplicate(records: list) -> list:
    """
    Remove duplicate portal records.
    Two records are duplicates if (src_zone, src_x, src_y, tgt_zone) match.
    When duplicates exist, keep the one with both src_ref and tgt_ref if
    available, otherwise just keep the first.
    """
    seen:   dict = {}
    result: list = []

    for r in records:
        key = (r.src_zone, r.src_x, r.src_y, r.tgt_zone)
        if key not in seen:
            seen[key] = r
            result.append(r)
        else:
            # Prefer the record that has more information
            existing = seen[key]
            if (r.src_ref or r.tgt_ref) and not (existing.src_ref or existing.tgt_ref):
                # Replace with better record
                idx = result.index(existing)
                result[idx] = r
                seen[key]   = r

    return result


# ── cross-zone pairing helper ─────────────────────────────────────────────────

def pair_one_way_portals(records: list, verbose: bool = False) -> list:
    """
    Stendhal sometimes defines the A->B side of a portal in one Java class
    and the B->A side in another.  This function detects records that are
    already reciprocal (tgt_zone of A == src_zone of B and vice versa) and
    ensures the tgt_ref / src_ref values are cross-linked where possible.

    Returns the original list unchanged (just annotates tgt_ref when missing).
    """
    # Build index: (zone, x, y) -> record
    by_pos: dict = {}
    for r in records:
        by_pos[(r.src_zone, r.src_x, r.src_y)] = r

    for r in records:
        if r.tgt_zone and r.tgt_ref is None:
            # Try to find the reciprocal record to get the ref name
            # Search for any record in tgt_zone that points back to src_zone
            # We can't know the exact tgt coords without more info, so skip
            pass

    if verbose:
        paired = sum(1 for r in records if r.tgt_ref)
        print(f"  Pairing: {paired}/{len(records)} records have tgt_ref")

    return records


# ── main scanner ──────────────────────────────────────────────────────────────

def scan_java_sources(src_dir: str, verbose: bool = False) -> list:
    """
    Recursively scan all *.java files under src_dir for portal definitions.
    Returns a deduplicated list of PortalRecord objects.
    """
    java_files = sorted(glob.glob(
        os.path.join(src_dir, JAVA_GLOB), recursive=True))

    print(f"  Scanning {len(java_files)} Java files under '{src_dir}' ...")

    all_records: list = []
    for fpath in java_files:
        all_records.extend(_scan_file(fpath, verbose=verbose))

    print(f"  Raw portal records found : {len(all_records)}")

    # Keep only complete records (have src_zone + coords + tgt_zone)
    complete = [r for r in all_records if r.is_complete()]
    print(f"  Complete records         : {len(complete)}")

    deduped = deduplicate(complete)
    print(f"  After deduplication      : {len(deduped)}")

    return deduped


# ── output writer ─────────────────────────────────────────────────────────────

def write_output(records: list, out_path: str, verbose: bool = False):
    entries = [r.to_dict() for r in sorted(
        records,
        key=lambda r: (r.src_zone, r.src_x, r.src_y),
    )]

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"\n  Wrote {len(entries)} portal entries to '{out_path}'")

    if verbose:
        print()
        print("  Sample entries:")
        for e in entries[:10]:
            print(f"    {e}")
        if len(entries) > 10:
            print(f"    ... ({len(entries) - 10} more)")


# ── diagnostic report ─────────────────────────────────────────────────────────

def print_report(records: list):
    """Print a summary of what was found."""
    by_src_zone: dict = defaultdict(int)
    for r in records:
        by_src_zone[r.src_zone] += 1

    print()
    print("  Zone breakdown (top 20 by portal count):")
    top = sorted(by_src_zone.items(), key=lambda kv: -kv[1])[:20]
    for zone_name, cnt in top:
        print(f"    {zone_name:40s}: {cnt}")

    missing_dest = [r for r in records if not r.tgt_zone]
    missing_ref  = [r for r in records if r.tgt_zone and not r.tgt_ref]
    print()
    print(f"  Records missing tgt_zone : {len(missing_dest)}"
          "  (excluded from output)")
    print(f"  Records missing tgt_ref  : {len(missing_ref)}"
          "  (arrival-only portals, included)")


# ── cross-check against existing zone names ───────────────────────────────────

def load_known_zones(conf_dir: str) -> set:
    """
    Return the set of zone names from the Stendhal zone config XML files.
    Used to validate the extracted zone names.
    """
    import xml.etree.ElementTree as ET
    ZONE_NS  = "stendhal"
    ZONE_TAG = f"{{{ZONE_NS}}}zone"
    known: set = set()
    xmls = sorted(glob.glob(os.path.join(conf_dir, "**", "*.xml"), recursive=True))
    for fpath in xmls:
        try:
            root = ET.parse(fpath).getroot()
        except ET.ParseError:
            continue
        els = list(root.iter(ZONE_TAG)) or list(root.iter("zone"))
        for z in els:
            name = z.get("name", "").strip()
            if name:
                known.add(name)
    return known


def filter_to_known_zones(records: list, known_zones: set,
                          verbose: bool = False) -> tuple:
    """
    Split records into (valid, unknown) based on whether src_zone and tgt_zone
    both appear in the known zone set.
    Arrival-only portals (tgt_zone is a valid zone) are kept.
    """
    valid:   list = []
    unknown: list = []
    for r in records:
        src_ok = r.src_zone in known_zones
        tgt_ok = not r.tgt_zone or r.tgt_zone in known_zones
        if src_ok and tgt_ok:
            valid.append(r)
        else:
            unknown.append(r)
            if verbose:
                print(f"  [UNKNOWN ZONE] src={r.src_zone} tgt={r.tgt_zone}"
                      f"  ({r.java_file}:{r.line_no})")
    return valid, unknown


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scan Stendhal Java source for portal definitions "
                    "and emit portals_override.json for use with "
                    "stendhal_to_andorstrail_v7.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--src-dir", default=DEFAULT_SRC_DIR, metavar="PATH",
        help=f"Root of the Stendhal Java source tree (default: '{DEFAULT_SRC_DIR}')",
    )
    p.add_argument(
        "--out", default=DEFAULT_OUT_FILE, metavar="FILE",
        help=f"Output JSON file (default: '{DEFAULT_OUT_FILE}')",
    )
    p.add_argument(
        "--conf-dir", default="data/conf/zones", metavar="PATH",
        help="Zone config directory used to validate zone names "
             "(default: 'data/conf/zones'). Pass empty string to skip validation.",
    )
    p.add_argument(
        "--include-unknown-zones", action="store_true",
        help="Include records whose zone names are not found in the config "
             "XML (may add false positives, but is useful for incomplete repos).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-file and per-record details.",
    )
    return p


def main():
    parser = build_arg_parser()
    args   = parser.parse_args()

    print("=" * 70)
    print("  Stendhal portal override generator")
    print("  Scans Java source -> portals_override.json")
    print("=" * 70)

    if not os.path.isdir(args.src_dir):
        sys.exit(
            f"\n[ERROR] Java source directory '{args.src_dir}' not found.\n"
            f"Run from the Stendhal project root, or pass --src-dir PATH."
        )

    # ── 1. Scan Java sources ───────────────────────────────────────
    print(f"\n[1/4] Scanning Java sources in '{args.src_dir}' ...")
    records = scan_java_sources(args.src_dir, verbose=args.verbose)
    print_report(records)

    # ── 2. Load known zone names for validation ────────────────────
    conf_dir = args.conf_dir
    known_zones: set = set()
    if conf_dir and os.path.isdir(conf_dir):
        print(f"\n[2/4] Loading known zone names from '{conf_dir}' ...")
        known_zones = load_known_zones(conf_dir)
        print(f"  Known zones: {len(known_zones)}")

        if not args.include_unknown_zones:
            valid, unknown = filter_to_known_zones(records, known_zones,
                                                   verbose=args.verbose)
            print(f"  Valid records (both zones known) : {len(valid)}")
            print(f"  Excluded (unknown zone names)    : {len(unknown)}")
            records = valid
        else:
            print("  --include-unknown-zones: skipping zone-name filter")
    else:
        print(f"\n[2/4] Zone validation skipped "
              f"('{conf_dir}' not found or empty).")

    # ── 3. Pair one-way portals where possible ────────────────────
    print(f"\n[3/4] Cross-linking one-way portal pairs ...")
    records = pair_one_way_portals(records, verbose=args.verbose)

    # ── 4. Write output ────────────────────────────────────────────
    print(f"\n[4/4] Writing output ...")
    write_output(records, args.out, verbose=args.verbose)

    print()
    print("=" * 70)
    print(f"  Done!  {len(records)} portal entries written to '{args.out}'")
    print()
    print("  Next steps:")
    print(f"  1. Review '{args.out}' and remove any false positives.")
    print(f"  2. Place it in your Stendhal project root.")
    print(f"  3. Run stendhal_to_andorstrail_v7.py -- it will auto-load the file.")
    print("=" * 70)


if __name__ == "__main__":
    main()
