"""
Build the San Diego level from data, inside the Unreal editor.

HOW TO RUN IT
    Output Log (Window -> Output Log). In the command box at the bottom, with
    the dropdown left of it on "Cmd", type:

        py "C:/Users/laugh/projects/callofbootyunreal/callofbooty/Tools/build_sandiego.py"

    That runs report() — a read-only survey. The other entry points take
    arguments, so run them from the same box in Python mode, or append a call
    to the bottom of this file and re-run it.

WHAT IS SCRIPTED AND WHAT IS NOT
The geography is traced data (callofbooty repo, src/world/geo/SanDiegoGeo.js)
and the whole point of tracing it was that it can be rebuilt at any scale, any
number of times, without redoing it by hand.

The one step that cannot be scripted is the heightmap import itself. Unreal has
never exposed ALandscape::Import to Python — confirmed on this build, where the
landscape editor types are absent from the `unreal` module. So the import is a
dialog, and this script's job is to hand you the exact numbers for it and then
fix up the transform afterwards, because the transform is where a typo silently
puts the whole city 60 m under the sea.

    report()            what engine, what level, what landscape, is it right
    make_open_world()   create the World Partition map to import into
    place_landscape()   apply the correct scale + origin to the imported terrain
    import_recipe()     print the numbers to type into the import dialog
"""

import json
import os

import unreal


HEIGHTMAP_DIR = os.path.join(unreal.Paths.project_dir(), "Tools", "Heightmaps")

# Where the open world map lives. Content-relative, as Unreal package paths.
MAP_PACKAGE = "/Game/Maps/Lvl_SanDiego"
OPEN_WORLD_TEMPLATE = "/Engine/Maps/Templates/OpenWorld"


def log(msg):
    unreal.log("[sandiego] {}".format(msg))


def warn(msg):
    unreal.log_warning("[sandiego] {}".format(msg))


def load_meta():
    """The exporter's sidecar, or None with a reason logged."""
    meta_path = os.path.join(HEIGHTMAP_DIR, "sandiego.json")
    raw_path = os.path.join(HEIGHTMAP_DIR, "sandiego.r16")
    if not os.path.exists(meta_path) or not os.path.exists(raw_path):
        warn("MISSING heightmap. Expected: {}".format(raw_path))
        return None
    with open(meta_path, "r") as handle:
        meta = json.load(handle)
    res = meta["resolution"]
    actual = os.path.getsize(raw_path)
    expected = res * res * 2
    if actual != expected:
        warn("heightmap is {} bytes, expected {} — re-export or re-pull".format(
            actual, expected))
        return None
    meta["_raw_path"] = os.path.abspath(raw_path)
    return meta


# ---------------------------------------------------------------- geometry --

def component_layout(res):
    """Split (res - 1) quads into the section/component grid Unreal wants.

    Landscape is not free to be any size: it is components, each of 1x1 or 2x2
    sections, each section a square of 7/15/31/63/127 quads. Getting this wrong
    is what makes the import dialog silently resample and blur the coastline.
    Prefer the layout with the fewest components, which streams best.
    """
    quads = res - 1
    best = None
    for section_quads in (127, 63, 31, 15, 7):
        for sections in (2, 1):
            per_component = section_quads * sections
            if quads % per_component:
                continue
            n = quads // per_component
            total = n * n
            if best is None or total < best["components"]:
                best = {
                    "sectionQuads": section_quads,
                    "sectionsPerComponent": sections,
                    "componentsX": n,
                    "componentsY": n,
                    "components": total,
                    "quadsPerComponent": per_component,
                }
    return best


def transform_for(meta):
    """Scale and origin that put the traced world where it belongs.

    Two facts drive all of this:

      - Unreal stores landscape height as a 16-bit value about a midpoint of
        32768, and one unit of Z scale spans 512 m of that range. So the scale
        that reproduces an N-metre range is (N * 100) / 512 cm.
      - Landscape-local Z=0 is that midpoint, not sea level. Our range is not
        centred on zero, so the actor has to be lifted to put the waterline at
        world Z=0 — otherwise every coastline in the map is 60 m underwater and
        it looks, convincingly and wrongly, like the export was bad.
    """
    res = meta["resolution"]
    lo = meta["heightRangeMetres"]["min"]
    hi = meta["heightRangeMetres"]["max"]
    scale = meta["unrealLandscapeScale"]

    # Elevation that lands on the 16-bit midpoint, i.e. on landscape-local Z=0.
    mid_metres = lo + (32768.0 / 65535.0) * (hi - lo)
    z_offset_uu = mid_metres * 100.0

    span_uu = (res - 1) * scale["x"]
    return {
        "scale": (scale["x"], scale["y"], scale["z"]),
        # Centre the map on the world origin, sea level at Z=0
        "location": (-span_uu / 2.0, -span_uu / 2.0, z_offset_uu),
        "spanUU": span_uu,
        "spanKM": span_uu / 100000.0,
        "seaLevelMidMetres": mid_metres,
    }


# ------------------------------------------------------------------ report --

def _find_landscapes():
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = sub.get_all_level_actors()
    except Exception as exc:                                     # noqa: BLE001
        warn("could not list actors ({})".format(exc))
        return []
    return [a for a in actors if isinstance(a, unreal.LandscapeProxy)]


def _partitioned():
    """Is the open level World Partition? Probe several bindings, not one.

    The first version of this asked World.get_world_partition() and reported
    "not enabled" when the attribute was missing — which says nothing about the
    level and everything about the Python bindings.
    """
    for name in ("WorldPartitionSubsystem", "WorldPartitionBlueprintLibrary"):
        if hasattr(unreal, name):
            return "likely (bindings present — check World Settings)"
    try:
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in sub.get_all_level_actors():
            if type(a).__name__ in ("WorldPartitionMiniMapVolume", "WorldDataLayers"):
                return "ENABLED"
    except Exception:                                            # noqa: BLE001
        pass
    return "no evidence in this level"


def report():
    """Read-only survey: engine, level, landscape, heightmap. Changes nothing."""
    log("=" * 64)
    log("engine version : {}".format(unreal.SystemLibrary.get_engine_version()))
    log("project dir    : {}".format(unreal.Paths.project_dir()))

    try:
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    except Exception:                                            # noqa: BLE001
        world = None
    log("current level  : {}".format(world.get_name() if world else "<none>"))
    log("world partition: {}".format(_partitioned()))

    lands = _find_landscapes()
    log("landscapes     : {}".format(len(lands)))
    for a in lands:
        loc = a.get_actor_location()
        sc = a.get_actor_scale3d()
        log("  {} @ ({:.0f}, {:.0f}, {:.0f})  scale ({:.3f}, {:.3f}, {:.3f})".format(
            a.get_actor_label(), loc.x, loc.y, loc.z, sc.x, sc.y, sc.z))

    log("-" * 64)
    meta = load_meta()
    if not meta:
        log("Fix the heightmap above before continuing.")
        log("=" * 64)
        return None

    res = meta["resolution"]
    lay = component_layout(res)
    tr = transform_for(meta)
    log("heightmap      : {} x {}  ({:.1f} MB)".format(
        res, res, os.path.getsize(meta["_raw_path"]) / 1048576.0))
    log("  frame        : {} x {} m".format(
        meta["frameMetres"]["width"], meta["frameMetres"]["height"]))
    log("  heights      : {}..{} m observed, {}..{} m encoded".format(
        meta["observedMetres"]["min"], meta["observedMetres"]["max"],
        meta["heightRangeMetres"]["min"], meta["heightRangeMetres"]["max"]))
    log("  land cover   : {:.1f}%".format(meta["landCoverage"] * 100))
    log("  in engine    : {:.2f} km square, {:.2f} m per quad".format(
        tr["spanKM"], tr["scale"][0] / 100.0))
    if lay:
        log("  components   : {} ({}x{} of {} quads)".format(
            lay["components"], lay["componentsX"], lay["componentsY"],
            lay["quadsPerComponent"]))
    else:
        warn("  {} does not factor into a legal landscape size — re-export at "
             "1009, 2017, 4033 or 8129".format(res))
    log("=" * 64)
    return meta


# ------------------------------------------------------------------ recipe --

def import_recipe():
    """Print exactly what to type into Landscape -> New -> Import from File."""
    meta = load_meta()
    if not meta:
        return
    lay = component_layout(meta["resolution"])
    tr = transform_for(meta)
    sx, sy, sz = tr["scale"]
    lx, ly, lz = tr["location"]

    log("=" * 64)
    log("LANDSCAPE IMPORT — type these, do not let the dialog guess")
    log("  1. Landscape mode (Ctrl+Shift+2) -> Manage -> New -> Import from File")
    log("  2. Heightmap File : {}".format(meta["_raw_path"]))
    log("  3. Section Size   : {0}x{0} quads".format(lay["sectionQuads"]))
    log("     Sections/Comp  : {0}x{0}".format(lay["sectionsPerComponent"]))
    log("     Component Count: {}x{}".format(lay["componentsX"], lay["componentsY"]))
    log("     (resolution should read {} x {})".format(
        meta["resolution"], meta["resolution"]))
    log("  4. Scale          : X {}  Y {}  Z {}".format(sx, sy, sz))
    log("  5. Location       : X {:.0f}  Y {:.0f}  Z {:.0f}".format(lx, ly, lz))
    log("     (or just run place_landscape() after the import — safer)")
    log("")
    log("  Z scale {} reproduces {}..{} m: one unit of Z scale spans 512 m of "
        "the 16-bit range, so scale = range_m * 100 / 512.".format(
            sz, meta["heightRangeMetres"]["min"], meta["heightRangeMetres"]["max"]))
    log("  Z location {:.0f} exists because landscape-local Z=0 sits at the "
        "16-bit midpoint, which is {:.1f} m of real elevation — not sea "
        "level.".format(lz, tr["seaLevelMidMetres"]))
    log("=" * 64)


# ------------------------------------------------------------------ actions --

def make_open_world(map_package=MAP_PACKAGE):
    """Create the World Partition map to import the landscape into.

    Lvl_FirstPerson is a template level and not partitioned; a 17.6 km landscape
    in a non-partitioned level loads all 1024 components at once, which is how
    you get an editor that will not open the map you just made.
    """
    sub = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if unreal.EditorAssetLibrary.does_asset_exist(map_package):
        warn("{} already exists — opening it instead of overwriting".format(map_package))
        sub.load_level(map_package)
        return map_package
    log("creating {} from {}".format(map_package, OPEN_WORLD_TEMPLATE))
    ok = sub.new_level_from_template(map_package, OPEN_WORLD_TEMPLATE)
    if not ok:
        warn("new_level_from_template failed. Do it by hand: File -> New Level "
             "-> Open World, then save as {}".format(map_package))
        return None
    sub.save_current_level()
    log("created. Delete the template's default Landscape before importing ours.")
    return map_package


def place_landscape():
    """Apply the computed scale and origin to the landscape in the open level.

    Run this after the import. It is the whole reason the import numbers do not
    have to be typed perfectly: get the resolution and file right in the dialog,
    and this fixes the rest.
    """
    meta = load_meta()
    if not meta:
        return False
    lands = _find_landscapes()
    if not lands:
        warn("no Landscape in this level — import it first, then re-run")
        return False
    if len(lands) > 1:
        warn("{} landscapes here; placing the first ({}). Delete the "
             "template's default one.".format(len(lands), lands[0].get_actor_label()))

    tr = transform_for(meta)
    a = lands[0]
    sx, sy, sz = tr["scale"]
    lx, ly, lz = tr["location"]
    a.set_actor_scale3d(unreal.Vector(sx, sy, sz))
    a.set_actor_location(unreal.Vector(lx, ly, lz), False, False)
    log("placed {}: scale ({}, {}, {}) at ({:.0f}, {:.0f}, {:.0f})".format(
        a.get_actor_label(), sx, sy, sz, lx, ly, lz))
    log("  {:.2f} km square, sea level at Z=0, centred on the origin".format(
        tr["spanKM"]))
    return True


COMMANDS = {
    "report": report,
    "recipe": import_recipe,
    "open-world": make_open_world,
    "place": place_landscape,
}


def _dispatch():
    """`py "<this file>" <command>` — so nothing here needs the Python console.

    Unreal's `py` console command forwards trailing arguments as sys.argv, which
    means every entry point is reachable from the Cmd prompt. That matters more
    than it looks: the dropdown defaulting to Cmd is what made the first attempt
    at running this fail as a "deprecated command".
    """
    import sys
    argv = [a for a in getattr(sys, "argv", [])[1:] if not a.lower().endswith(".py")]
    name = argv[0].strip().lower() if argv else "report"
    fn = COMMANDS.get(name)
    if not fn:
        warn("unknown command '{}'. Try: {}".format(name, ", ".join(COMMANDS)))
        return
    fn()


_dispatch()
