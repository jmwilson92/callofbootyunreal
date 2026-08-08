"""
Build the San Diego level from data, inside the Unreal editor.

HOW TO RUN IT
    Output Log (Window -> Output Log). In the command box at the bottom, with
    the dropdown left of it on "Cmd", type:

        py "C:/Users/laugh/projects/callofbootyunreal/callofbooty/Tools/build_sandiego.py"

    That runs report — a read-only survey. Every other entry point is a
    trailing argument on the same line, e.g. `... build_sandiego.py look zoo`.

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

    report              what engine, what level, what landscape, is it right
    open-world          create the World Partition map to import into
    identify            list each landscape with the resolution it really is
    clear-landscape     delete every landscape here, proxies included
    recipe              print the numbers to type into the import dialog
    place               transform our import, delete any impostor beside it
    probe               read elevations at 21 landmarks straight from the file
    look <place>        point the viewport at one of them
    templates           list the level templates this engine ships
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
    png_path = os.path.join(HEIGHTMAP_DIR, "sandiego.png")
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

    # Prefer the PNG. A headerless .r16 makes Unreal go looking for a sidecar
    # declaring width/height/bpp, and it looks for it at sandiego.json — the same
    # path this project already used for its own metadata. When those fields were
    # missing the import did not complain: it reported "(Invalid)" resolution and
    # offered a default 505x505 landscape, which is a much worse failure than an
    # error. PNG carries its own header and sidesteps the whole question.
    meta["_png_path"] = os.path.abspath(png_path) if os.path.exists(png_path) else None
    if not meta["_png_path"]:
        warn("no sandiego.png — re-run tools/export-heightmap.mjs and re-pull. "
             "The .r16 will work, but only because the sidecar now declares "
             "width/height/bpp.")
    if "width" not in meta or "bpp" not in meta:
        warn("sidecar has no width/bpp fields: an .r16 import from this folder "
             "will silently fall back to a default landscape. Import the .png.")
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


# ------------------------------------------------------------------ places --

# Traced landmarks, in the same normalised (u, v) the geography is authored in:
# u east, v south, both 0..1 over the reference frame. Kept in step with
# src/world/geo/SanDiegoGeo.js in the callofbooty repo.
PLACES = [
    ("missionbeach", 0.070, 0.026), ("oceanbeach", 0.099, 0.213),
    ("sunsetcliffs", 0.101, 0.426), ("pointloma", 0.140, 0.620),
    ("cabrillo", 0.150, 0.870), ("airport", 0.370, 0.337),
    ("oldtown", 0.508, 0.136), ("missionvalley", 0.658, 0.048),
    ("hillcrest", 0.589, 0.227), ("balboa", 0.710, 0.354),
    ("downtown", 0.611, 0.500), ("littleitaly", 0.569, 0.436),
    ("northisland", 0.349, 0.644), ("coronado", 0.497, 0.772),
    ("nationalcity", 0.972, 0.820), ("northpark", 0.802, 0.245),
    ("cityheights", 0.968, 0.294), ("kearnymesa", 0.520, 0.040),
    ("clairemont", 0.300, 0.130), ("mcrd", 0.395, 0.285),
    ("zoo", 0.700, 0.310),
]


def uv_to_world(meta, u, v):
    """Normalised (u, v) -> world centimetres, and the heightmap sample it hits.

    The frame is wider than it is tall and the heightmap is square, so the
    export letterboxed: v is sampled over the frame's real aspect and the
    remainder left at sea. Undo exactly that here, or every landmark lands about
    a kilometre north of where it belongs.
    """
    res = meta["resolution"]
    fm = meta["frameMetres"]
    band = float(fm["height"]) / float(fm["width"])     # image fraction the frame fills
    v_off = (1.0 - band) / 2.0
    row_f = v_off + v * band                            # 0..1 down the image

    span_uu = (res - 1) * meta["unrealLandscapeScale"]["x"]
    return {
        "x": (u - 0.5) * span_uu,
        "y": (row_f - 0.5) * span_uu,
        "col": int(round(u * (res - 1))),
        "row": int(round(row_f * (res - 1))),
    }


def _sample_metres(meta, col, row):
    """Read one height straight out of the .r16. No image library needed."""
    res = meta["resolution"]
    if not (0 <= col < res and 0 <= row < res):
        return None
    lo = meta["heightRangeMetres"]["min"]
    hi = meta["heightRangeMetres"]["max"]
    with open(meta["_raw_path"], "rb") as fh:
        fh.seek((row * res + col) * 2)
        b = fh.read(2)
    if len(b) != 2:
        return None
    raw = b[0] | (b[1] << 8)                            # little-endian
    return lo + (raw / 65535.0) * (hi - lo)


def probe():
    """Print the elevation the heightmap holds at every named landmark.

    This is the check worth doing before dressing anything: terrain can look
    entirely convincing and still be the wrong terrain. Downtown and the airport
    should read near sea level, Point Loma and the mesas well above it, and the
    bay below zero. If those disagree, the import is wrong in a way no amount of
    flying around will reveal.
    """
    meta = load_meta()
    if not meta:
        return
    log("=" * 64)
    log("{:<14} {:>10} {:>12} {:>12}".format("place", "height m", "world X", "world Y"))
    for name, u, v in PLACES:
        p = uv_to_world(meta, u, v)
        h = _sample_metres(meta, p["col"], p["row"])
        log("{:<14} {:>10} {:>12.0f} {:>12.0f}".format(
            name, "n/a" if h is None else "{:.1f}".format(h), p["x"], p["y"]))
    log("=" * 64)
    log("Sanity: downtown/airport/coronado near 0, pointloma and the mesas high,")
    log("open water negative. Anything wildly off means a bad import, not a bad trace.")


def look(place="downtown"):
    """Point the editor viewport at a landmark.

    Flying 17.6 km by hand to find out whether the coastline is right is how you
    end up not checking.
    """
    meta = load_meta()
    if not meta:
        return
    key = str(place).strip().lower()
    match = [p for p in PLACES if p[0] == key]
    if not match:
        warn("unknown place '{}'. Known: {}".format(
            key, ", ".join(p[0] for p in PLACES)))
        return
    _, u, v = match[0]
    p = uv_to_world(meta, u, v)
    h = _sample_metres(meta, p["col"], p["row"]) or 0.0

    # Stand off to the south and look north, high enough to take in the coast.
    dist = 250000.0
    eye = unreal.Vector(p["x"], p["y"] + dist, h * 100.0 + 150000.0)
    rot = unreal.Rotator(0.0, -32.0, -90.0)   # roll, pitch, yaw: -Y is north
    try:
        unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\
            .set_level_viewport_camera_info(eye, rot)
    except Exception as exc:                                     # noqa: BLE001
        warn("could not move the viewport ({}). Fly to X {:.0f} Y {:.0f} "
             "by hand.".format(exc, p["x"], p["y"]))
        return
    log("looking at {}: world ({:.0f}, {:.0f}), ground {:.1f} m".format(
        key, p["x"], p["y"], h))


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
    src = meta["_png_path"] or meta["_raw_path"]
    log("heightmap      : {} x {}  ({:.1f} MB, {})".format(
        res, res, os.path.getsize(src) / 1048576.0, os.path.basename(src)))
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
    log("  2. Heightmap File : {}".format(meta["_png_path"] or meta["_raw_path"]))
    if meta["_png_path"]:
        log("     (the .png, not the .r16 — it carries its own resolution)")
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

def list_templates():
    """Print the level templates this engine actually ships.

    Hardcoding a template path is how you get a script that works on one
    engine version. 5.8 may name these differently to 5.3; ask it.
    """
    found = []
    for folder in ("/Engine/Maps/Templates", "/Engine/Maps"):
        try:
            for path in unreal.EditorAssetLibrary.list_assets(folder, recursive=True):
                if "template" in path.lower() or "openworld" in path.lower():
                    found.append(path.split(".")[0])
        except Exception:                                        # noqa: BLE001
            continue
    for p in sorted(set(found)):
        log("  template: {}".format(p))
    if not found:
        warn("no templates listed — use File -> New Level by hand")
    return sorted(set(found))


def make_open_world(map_package=MAP_PACKAGE):
    """Create the World Partition map to import the landscape into.

    Lvl_FirstPerson is a template level and not partitioned; a 17.6 km landscape
    in a non-partitioned level loads all 1024 components at once, which is how
    you get an editor that will not open the map you just made.

    Prefers an *empty* open world. The plain Open World template ships with a
    default Landscape, and deleting one in a partitioned level is fiddlier than
    it sounds — the terrain is split across streaming proxies and the Outliner
    only lists the ones currently loaded, so a manual select-and-delete tends to
    leave pieces behind. Starting empty means there is nothing to delete.
    """
    sub = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if unreal.EditorAssetLibrary.does_asset_exist(map_package):
        warn("{} already exists — opening it instead of overwriting".format(map_package))
        sub.load_level(map_package)
        return map_package

    candidates = [
        "/Engine/Maps/Templates/OpenWorld_Empty",
        "/Engine/Maps/Templates/Template_OpenWorld_Empty",
        "/Engine/Maps/Templates/OpenWorld",
    ]
    for template in candidates:
        if not unreal.EditorAssetLibrary.does_asset_exist(template):
            continue
        log("creating {} from {}".format(map_package, template))
        if sub.new_level_from_template(map_package, template):
            sub.save_current_level()
            n = len(_find_landscapes())
            if n:
                log("created, with {} landscape actor(s) from the template — "
                    "run the 'clear-landscape' command next.".format(n))
            else:
                log("created, no landscape present. Import ours straight in.")
            return map_package
        warn("{} did not take, trying the next one".format(template))

    warn("no usable template. Do it by hand: File -> New Level -> Empty Open "
         "World, then save as {}".format(map_package))
    list_templates()
    return None


def clear_landscape():
    """Delete every landscape actor in the open level, proxies included.

    This exists because doing it by hand is genuinely awkward. A World Partition
    landscape is one Landscape actor plus a LandscapeStreamingProxy per region,
    and the Outliner only shows actors that are currently loaded — so selecting
    what you can see and pressing Delete leaves the unloaded proxies in place,
    and the leftovers only turn up later as terrain fighting the imported map.
    """
    lands = _find_landscapes()
    if not lands:
        log("no landscape actors loaded in this level — nothing to delete.")
        log("If you can see terrain anyway, its proxies are unloaded: open "
            "Window -> World Partition, select all in the minimap, right-click "
            "-> Load Region, then run this again.")
        return 0

    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    labels = [a.get_actor_label() for a in lands]
    removed = 0
    for a in lands:
        try:
            sub.destroy_actor(a)
            removed += 1
        except Exception as exc:                                 # noqa: BLE001
            warn("could not delete {} ({})".format(a.get_actor_label(), exc))
    log("deleted {} of {} landscape actor(s): {}".format(
        removed, len(lands), ", ".join(labels[:6]) + ("..." if len(labels) > 6 else "")))
    left = len(_find_landscapes())
    if left:
        warn("{} still loaded — re-run, or they are unloaded proxies (see "
             "World Partition note above)".format(left))
    else:
        log("level is clear. Save it (Ctrl+S), then import the heightmap.")
    return removed


def _landscape_info(a):
    """Infer a landscape's heightmap resolution from its bounds and scale.

    Needed because "the landscape in this level" stopped being a safe
    assumption. The Open World template ships one, the import adds a second
    called Landscape2, and picking whichever the actor list returned first
    applied the 17.6 km transform to a 505x505 template default while the real
    terrain sat untouched. Bounds discriminate them by a factor of eight, and
    unlike a name they cannot be wrong.
    """
    try:
        origin, extent = a.get_actor_bounds(False)
    except Exception:                                            # noqa: BLE001
        try:
            origin, extent = a.get_actor_bounds(False, False)
        except Exception as exc:                                 # noqa: BLE001
            warn("no bounds for {} ({})".format(a.get_actor_label(), exc))
            return None
    sc = a.get_actor_scale3d()
    quads = (extent.x * 2.0) / sc.x if sc.x else 0.0
    return {
        "actor": a,
        "label": a.get_actor_label(),
        "res": int(round(quads)) + 1,
        "scale": sc,
        "location": a.get_actor_location(),
    }


def identify():
    """List every landscape with the resolution its geometry implies."""
    meta = load_meta()
    want = meta["resolution"] if meta else None
    infos = [i for i in (_landscape_info(a) for a in _find_landscapes()) if i]
    if not infos:
        log("no landscape actors loaded. If the Outliner shows one greyed out, "
            "right-click it -> Load, then re-run.")
        return []
    log("=" * 64)
    for i in infos:
        tag = ""
        if want:
            tag = "  <- ours" if abs(i["res"] - want) <= max(2, want * 0.02) \
                else "  <- NOT ours (template default?)"
        log("{:<16} ~{} x {}  scale {:.1f}  at ({:.0f}, {:.0f}, {:.0f}){}".format(
            i["label"], i["res"], i["res"], i["scale"].x,
            i["location"].x, i["location"].y, i["location"].z, tag))
    log("=" * 64)
    return infos


def place_landscape(delete_others="yes"):
    """Transform the imported landscape, and remove any impostor beside it.

    Picks by resolution rather than by order: the level can hold the template's
    505x505 default and our 4033 import at the same time, and only one of them
    should be 17.6 km across.
    """
    meta = load_meta()
    if not meta:
        return False
    infos = [i for i in (_landscape_info(a) for a in _find_landscapes()) if i]
    if not infos:
        warn("no landscape loaded in this level.")
        warn("If the Outliner shows one greyed out it is unloaded and invisible "
             "to Python — right-click it -> Load, then re-run.")
        return False

    want = meta["resolution"]
    tol = max(2, want * 0.02)
    ours = [i for i in infos if abs(i["res"] - want) <= tol]
    others = [i for i in infos if abs(i["res"] - want) > tol]

    if not ours:
        warn("none of the {} landscape(s) here look like a {} import:".format(
            len(infos), want))
        for i in infos:
            warn("  {} reads as ~{} x {}".format(i["label"], i["res"], i["res"]))
        warn("Import the heightmap first, or right-click any unloaded landscape "
             "in the Outliner -> Load and re-run.")
        return False
    if len(ours) > 1:
        warn("{} landscapes match {} — using {}".format(
            len(ours), want, ours[0]["label"]))

    if others and str(delete_others).lower() not in ("no", "false", "0"):
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for i in others:
            log("deleting {} (~{} x {}, not our import)".format(
                i["label"], i["res"], i["res"]))
            try:
                sub.destroy_actor(i["actor"])
            except Exception as exc:                             # noqa: BLE001
                warn("  could not delete it ({})".format(exc))
    elif others:
        warn("leaving {} other landscape(s) in place".format(len(others)))

    tr = transform_for(meta)
    a = ours[0]["actor"]
    sx, sy, sz = tr["scale"]
    lx, ly, lz = tr["location"]
    a.set_actor_scale3d(unreal.Vector(sx, sy, sz))
    a.set_actor_location(unreal.Vector(lx, ly, lz), False, False)
    log("placed {} (~{} x {}): scale ({}, {}, {}) at ({:.0f}, {:.0f}, {:.0f})".format(
        ours[0]["label"], ours[0]["res"], ours[0]["res"], sx, sy, sz, lx, ly, lz))
    log("  {:.2f} km square, sea level at Z=0, centred on the origin".format(
        tr["spanKM"]))
    log("  Save with Ctrl+S, then: ... build_sandiego.py look downtown")
    return True


COMMANDS = {
    "report": report,
    "recipe": import_recipe,
    "open-world": make_open_world,
    "clear-landscape": clear_landscape,
    "place": place_landscape,
    "identify": identify,
    "probe": probe,
    "look": look,
    "templates": list_templates,
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
    fn(*argv[1:])


_dispatch()
