# Stendhal Level 0 World Filler

Fills blank spots on the Stendhal Level 0 worldmap by copying `ados/coast_se.tmx`
as a neutral ocean/terrain placeholder into every missing grid slot, then
registers every new zone so the server loads them at start-up.

---

## Quick start

```bash
# From the Stendhal source root (the directory that contains data/ and src/)
python3 fill_worldmap.py
```

That's it. The script is idempotent — running it more than once is safe; it
skips any file or zone registration that already exists.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.6 or newer | Uses only the standard library — no `pip install` needed |
| Stendhal source tree | The script must be able to find `data/maps/Level 0/ados/coast_se.tmx` |

---

## Command-line options

```
python3 fill_worldmap.py [--dry-run] [--stendhal-dir PATH]
```

| Flag | Description |
|---|---|
| `--dry-run` | Print everything that would be done without writing a single file. Useful for reviewing the changes before committing. |
| `--stendhal-dir PATH` | Explicit path to the Stendhal source root. Defaults to the current working directory. |

### Examples

```bash
# Preview changes without touching any files
python3 fill_worldmap.py --dry-run

# Run from a different directory
python3 fill_worldmap.py --stendhal-dir /home/user/stendhal
```

---

## What the script does

### Step 1 — Create TMX map files

For each zone listed below the script:

1. Copies `data/maps/Level 0/ados/coast_se.tmx` to the destination path.
2. Opens the copied file and patches two things:
   - The `name` attribute on the root `<map>` element.
   - Any `<property name="zone" value="…">` element inside `<properties>`.
   Both are set to the Stendhal zone ID for the new map
   (e.g. `0_andor_coast_w3`).

Directories are created automatically if they don't already exist.

### Step 2 — Register zones in `data/conf/zones.xml`

Appends a `<zone>` entry for each new map to the level-0 group inside
`zones.xml`, for example:

```xml
<zone name="0_andor_coast_w3"
      file="Level 0/andor/coast_w3.tmx"
      title="Coast W3" />
```

Zones that are already present in `zones.xml` are skipped.

---

## Maps added

All 28 new maps are copies of `ados/coast_se.tmx` used as ocean/terrain
placeholders. Customise them afterwards with [Tiled](https://www.mapeditor.org/).

| Zone ID | TMX file | Placed next to |
|---|---|---|
| `0_amazon_ocean_s` | `Level 0/amazon/ocean_s.tmx` | `ados/ocean_e.tmx` |
| `0_ados_ocean_deep` | `Level 0/ados/ocean_deep.tmx` | `ados/ocean_se.tmx` |
| `0_athor_ocean_n` | `Level 0/athor/ocean_n.tmx` | `ados/coast_s.tmx` |
| `0_athor_ocean_ne` | `Level 0/athor/ocean_ne.tmx` | `athor/ocean_n.tmx` |
| `0_andor_coast_w3` | `Level 0/andor/coast_w3.tmx` | `kirneh/river_w.tmx` |
| `0_andor_coast_w2` | `Level 0/andor/coast_w2.tmx` | — |
| `0_andor_coast_w` | `Level 0/andor/coast_w.tmx` | — |
| `0_andor_coast` | `Level 0/andor/coast.tmx` | — |
| `0_andor_dock` | `Level 0/andor/dock.tmx` | — |
| `0_andor_coast_e` | `Level 0/andor/coast_e.tmx` | — |
| `0_andor_coast_e2` | `Level 0/andor/coast_e2.tmx` | — |
| `0_andor_coast_e3` | `Level 0/andor/coast_e3.tmx` | — |
| `0_andor_andor_w3` | `Level 0/andor/andor_w3.tmx` | `fado/forest_s_e3.tmx` |
| `0_andor_andor_w2` | `Level 0/andor/andor_w2.tmx` | — |
| `0_andor_andor_w` | `Level 0/andor/andor_w.tmx` | — |
| `0_andor_town` | `Level 0/andor/town.tmx` | — |
| `0_andor_city` | `Level 0/andor/city.tmx` | — |
| `0_andor_andor_e` | `Level 0/andor/andor_e.tmx` | — |
| `0_andor_andor_e2` | `Level 0/andor/andor_e2.tmx` | — |
| `0_andor_andor_e3` | `Level 0/andor/andor_e3.tmx` | — |
| `0_andor_andor_forest_w3` | `Level 0/andor/andor_forest_w3.tmx` | `kalavan/forest_e2.tmx` |
| `0_andor_andor_forest_w2` | `Level 0/andor/andor_forest_w2.tmx` | — |
| `0_andor_andor_forest_w` | `Level 0/andor/andor_forest_w.tmx` | — |
| `0_andor_andor_forest` | `Level 0/andor/andor_forest.tmx` | — |
| `0_andor_andor_clearing` | `Level 0/andor/andor_clearing.tmx` | — |
| `0_andor_andor_forest_e` | `Level 0/andor/andor_forest_e.tmx` | — |
| `0_andor_andor_forest_e2` | `Level 0/andor/andor_forest_e2.tmx` | — |
| `0_andor_andor_forest_e3` | `Level 0/andor/andor_forest_e3.tmx` | — |

---

## After running the script

### 1. Verify the files

```bash
# Check that TMX files were created
find data/maps/Level\ 0/andor  -name "*.tmx" | sort
find data/maps/Level\ 0/amazon -name "*.tmx" | sort
find data/maps/Level\ 0/athor  -name "*.tmx" | sort

# Check zones.xml for new entries
grep "0_andor\|0_amazon\|0_athor_ocean" data/conf/zones.xml
```

### 2. Java ZoneConfigurator classes (ocean / filler zones)

Most ocean and filler zones in Stendhal **do not** need a dedicated Java
`ZoneConfigurator` class — the server loads them from `zones.xml` as bare
zones with no scripted contents.

If a zone does need scripted NPCs, items, or triggers you will need to create
a class under:

```
src/games/stendhal/server/maps/<area>/
```

that implements `ZoneConfigurator` and is referenced in `zones.xml` via the
`implementation` attribute.  For the plain ocean/terrain placeholders added by
this script, that step is **not required**.

### 3. Rebuild and start the server

```bash
# Ant build
ant

# or Maven build
mvn package -DskipTests

# Then start the server as usual
```

### 4. Customise the maps in Tiled

Open any of the new `.tmx` files in [Tiled](https://www.mapeditor.org/) to
paint proper terrain, add collision layers, place portals/warps, etc.  The
zone ID embedded in each file (`<property name="zone">`) must stay in sync
with the `name` attribute in `zones.xml`.

---

## Troubleshooting

### `ERROR: Template file not found`

You are not running the script from the Stendhal source root, or
`data/maps/Level 0/ados/coast_se.tmx` does not exist in your checkout.

**Fix:** `cd` into the Stendhal source root before running the script, or
pass `--stendhal-dir /path/to/stendhal`.

### `[WARNING] zones.xml not found`

The path `data/conf/zones.xml` doesn't exist in your checkout.  The script
will still create all the TMX files; you will need to add the zone entries to
wherever your build registers zones.

### The server starts but doesn't show the new zones

- Make sure you rebuilt after running the script (`ant` / `mvn package`).
- Check that the zone IDs in `zones.xml` exactly match the `name` attributes
  inside the TMX files.  Run `--dry-run` to see what the script would write
  without risking any corruption.

### XML encoding errors after patching

Python's `xml.etree.ElementTree` writes standard UTF-8 XML.  If the original
TMX used a BOM or a non-UTF-8 encoding, open the patched file in Tiled and
re-save it to normalise the encoding.

---

## Files in this package

| File | Description |
|---|---|
| `fill_worldmap.py` | The script — run this from the Stendhal source root |
| `README.md` | This file |

---

## License

This tooling script is released into the public domain (CC0).  The Stendhal
game itself is licensed under the GNU General Public License v2.
