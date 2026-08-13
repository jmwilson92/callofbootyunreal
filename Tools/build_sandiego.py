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
    overview            camera 12 km up, looking straight down
    water               lay a sea surface at Z=0 across the map
    material            colour the terrain by height and slope
    roads               lay the freeways along the traced routes (legacy)
    clear-roads         delete those legacy freeway actors
    cull                per-kind draw distances on the city instances
    interiors [m] [at]  hollow the buildings near a place and give them floors
    actors [match]      list what is in this level, grouped, with counts
    drop <label stem>   delete every actor whose label starts with that
    city [what]         lay the surface streets and buildings from the plan
    city-report         what the city plan contains, without touching the level
    sample              compare the level against the file, per tile
    load                load every World Partition actor first
    wp-api              list the World Partition bindings this build has
    templates           list the level templates this engine ships
"""

import json
import math
import os
import sys

import unreal


def _heightmap_dir():
    """Where the heightmaps actually are, which is not always the open project.

    This used to be project_dir()/Tools/Heightmaps. Unreal copies a project
    when you open it under a different engine version — "callofbooty 5.8 - 2"
    beside "callofbooty" — and the copy carries a snapshot of whatever the
    heightmaps were at the time. Every export and every git pull went to the
    original; every import read the copy. The terrain therefore could not
    change no matter how many times it was re-imported, and nothing in the log
    said why, because both folders contained a perfectly valid 4033 heightmap.

    So: prefer the copy sitting beside this script, since the script is run by
    full path out of the git working tree and that is the one being updated.
    """
    candidates = []
    try:
        candidates.append(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "Heightmaps"))
    except NameError:                       # exec() with no __file__
        pass
    candidates.append(os.path.join(unreal.Paths.project_dir(), "Tools", "Heightmaps"))
    for c in candidates:
        if os.path.exists(os.path.join(c, "sandiego.json")):
            return c
    return candidates[0] if candidates else ""


HEIGHTMAP_DIR = _heightmap_dir()

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
    ("cabrillo", 0.160, 0.864), ("airport", 0.370, 0.337),
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

    # uv_to_world assumes the map is centred on the origin, which is where the
    # transform puts it — but only if the import went in at the right location.
    # Read the landscape's real corner and work from that instead, so this aims
    # at the right place even when the level does not match the intent.
    groups = [g for g in _landscape_groups()
              if abs(g["res"] - meta["resolution"]) <= max(2, meta["resolution"] * 0.02)]
    if groups:
        span = (meta["resolution"] - 1) * meta["unrealLandscapeScale"]["x"]
        cx, cy = groups[0]["minCorner"]
        shift_x = cx - (-span / 2.0)
        shift_y = cy - (-span / 2.0)
        if abs(shift_x) > 1000 or abs(shift_y) > 1000:
            warn("landscape corner is ({:.0f}, {:.0f}), not ({:.0f}, {:.0f}) — "
                 "aiming at where it actually is".format(cx, cy, -span / 2, -span / 2))
        p["x"] += shift_x
        p["y"] += shift_y

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
    proj = unreal.Paths.project_dir()
    log("project dir    : {}".format(proj))
    log("heightmap dir  : {}".format(HEIGHTMAP_DIR))
    # These two disagreeing is not cosmetic: it means imports and exports are
    # looking at different files, which is invisible unless something says so.
    try:
        # project_dir() comes back relative to the engine binary — a string of
        # "../../.." that never string-compares equal to an absolute path even
        # when it names the same folder. Resolve both before comparing, or this
        # warns on a perfectly correct setup and teaches everyone to ignore it.
        proj_hm = os.path.realpath(os.path.join(proj, "Tools", "Heightmaps"))
        if os.path.realpath(HEIGHTMAP_DIR) != proj_hm:
            warn("the open project is NOT where these heightmaps live.")
            warn("  reading  : {}".format(HEIGHTMAP_DIR))
            warn("  project  : {}".format(proj_hm))
            warn("Unreal copies a project when the engine version changes, and")
            warn("the copy keeps an old heightmap. Work in the git checkout, or")
            warn("copy Tools/Heightmaps across, or the import cannot change.")
    except Exception:                                            # noqa: BLE001
        pass

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
    # landCoverage was dropped from the sidecar; report what is there rather
    # than crashing on what is not. Anything optional gets the same treatment.
    if "landCoverage" in meta:
        log("  land cover   : {:.1f}%".format(meta["landCoverage"] * 100))
    if "playableMetres" in meta:
        log("  playable     : {:.2f} x {:.2f} km inside a {:.0f} m ring".format(
            meta["playableMetres"]["width"] / 1000.0,
            meta["playableMetres"]["height"] / 1000.0,
            meta.get("outOfBoundsRingMetres", 0)))
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
    log("  5. Location       : X 0  Y 0  Z {:.0f}".format(lz))
    log("     The dialog CENTRES the landscape on this point, so 0,0 is what")
    log("     spans {:.0f}..{:.0f}. Entering the corner ({:.0f}) puts the whole".format(
        lx, -lx, lx))
    log("     map one full width to the south-west instead.")
    log("     The finished actor's own Location reads {:.0f}, {:.0f} — that one".format(lx, ly))
    log("     is the corner. Two different conventions, one field name.")
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


def _bounds(a):
    """(origin, extent) across engine versions, or None."""
    for args in ((False,), (False, False)):
        try:
            return a.get_actor_bounds(*args)
        except Exception:                                        # noqa: BLE001
            continue
    return None


def _is_proxy(a):
    """Is this a streaming fragment rather than the landscape itself?"""
    cls = getattr(unreal, "LandscapeStreamingProxy", None)
    if cls is not None:
        return isinstance(a, cls)
    return "StreamingProxy" in type(a).__name__


def _landscape_groups():
    """Collect landscape actors into logical landscapes.

    A World Partition landscape is not one actor. It is a parent ALandscape
    carrying no geometry of its own — its bounds measure about zero — plus one
    LandscapeStreamingProxy per region, each holding a fragment. Measuring
    actors one at a time therefore reports a correct 4033 import as a single
    ~1x1 actor and 256 ~253x253 ones, and concludes that none of them is the
    landscape that was just imported. Resolution is a property of the group,
    so group before measuring.
    """
    actors = _find_landscapes()
    parents = [a for a in actors if not _is_proxy(a)]
    proxies = [a for a in actors if _is_proxy(a)]

    groups = {}
    for p in parents:
        groups[p.get_name()] = {"parent": p, "members": [p]}

    for q in proxies:
        owner = None
        try:                                    # the proxy names its parent
            la = q.get_editor_property("landscape_actor")
            if la:
                owner = la.get_name()
        except Exception:                                        # noqa: BLE001
            owner = None
        if owner in groups:
            groups[owner]["members"].append(q)
        elif len(parents) == 1:
            groups[parents[0].get_name()]["members"].append(q)
        else:
            groups.setdefault("_orphans", {"parent": None, "members": []})
            groups["_orphans"]["members"].append(q)

    out = []
    for g in groups.values():
        lo_x = lo_y = float("inf")
        hi_x = hi_y = float("-inf")
        scale = None
        for a in g["members"]:
            b = _bounds(a)
            if not b:
                continue
            origin, extent = b
            # The parent of a streamed landscape carries no geometry and
            # measures about zero; skip it so it does not drag the bounds. But
            # a landscape imported at World Partition Grid Size 0 has no
            # proxies and IS its own single actor, so there is nothing to skip.
            if len(g["members"]) > 1 and extent.x <= 1.0:
                continue
            lo_x = min(lo_x, origin.x - extent.x)
            lo_y = min(lo_y, origin.y - extent.y)
            hi_x = max(hi_x, origin.x + extent.x)
            hi_y = max(hi_y, origin.y + extent.y)
            scale = a.get_actor_scale3d()
        if scale is None or lo_x == float("inf"):
            continue
        quads = (hi_x - lo_x) / scale.x if scale.x else 0.0
        anchor = g["parent"] or g["members"][0]
        out.append({
            "parent": g["parent"],
            "members": g["members"],
            "label": anchor.get_actor_label(),
            "res": int(round(quads)) + 1,
            "scale": scale,
            "minCorner": (lo_x, lo_y),
        })
    return out


def identify():
    """List each logical landscape with the resolution its geometry implies."""
    meta = _meta_for_level()
    want = meta["resolution"] if meta else None
    groups = _landscape_groups()
    if not groups:
        log("no landscape actors loaded. If the Outliner shows one greyed out, "
            "right-click it -> Load, then re-run.")
        return []
    log("=" * 64)
    for g in groups:
        tag = ""
        if want:
            tag = "  <- ours" if abs(g["res"] - want) <= max(2, want * 0.02) \
                else "  <- NOT ours"
        log("{:<16} ~{} x {}  scale {:.3f}  {} actors  corner ({:.0f}, {:.0f}){}"
            .format(g["label"], g["res"], g["res"], g["scale"].x,
                    len(g["members"]), g["minCorner"][0], g["minCorner"][1], tag))
    log("=" * 64)
    return groups


def place_landscape(delete_others="yes"):
    """Scale and position the imported landscape, streaming proxies included.

    Setting the parent actor's transform is the intended move — the landscape
    propagates it to its proxies. That is verified rather than assumed: a proxy
    position is read before and after, and if nothing moved, every member is
    transformed directly, preserving the grid by scaling each offset from the
    landscape's own corner. A landscape half-moved is worse than one not moved
    at all, and the two are indistinguishable from the log line.
    """
    meta = load_meta()
    if not meta:
        return False
    groups = _landscape_groups()
    if not groups:
        warn("no landscape loaded in this level.")
        warn("If the Outliner shows one greyed out it is unloaded and invisible "
             "to Python — right-click it -> Load, then re-run.")
        return False

    want = meta["resolution"]
    tol = max(2, want * 0.02)
    ours = [g for g in groups if abs(g["res"] - want) <= tol]
    others = [g for g in groups if abs(g["res"] - want) > tol]

    if not ours:
        warn("none of the {} landscape(s) here look like a {} import:".format(
            len(groups), want))
        for g in groups:
            warn("  {} reads as ~{} x {} across {} actors".format(
                g["label"], g["res"], g["res"], len(g["members"])))
        return False
    g = ours[0]

    if others and str(delete_others).lower() not in ("no", "false", "0"):
        sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for o in others:
            log("deleting {} (~{} x {}, {} actors, not our import)".format(
                o["label"], o["res"], o["res"], len(o["members"])))
            for a in o["members"]:
                try:
                    sub.destroy_actor(a)
                except Exception as exc:                         # noqa: BLE001
                    warn("  could not delete {} ({})".format(
                        a.get_actor_label(), exc))

    tr = transform_for(meta)
    sx, sy, sz = tr["scale"]
    lx, ly, lz = tr["location"]

    witness = next((a for a in g["members"] if _is_proxy(a)), None)
    witness_x = witness.get_actor_location().x if witness else None

    target = g["parent"] or g["members"][0]
    target.set_actor_scale3d(unreal.Vector(sx, sy, sz))
    target.set_actor_location(unreal.Vector(lx, ly, lz), False, False)

    if witness is not None and abs(witness.get_actor_location().x - witness_x) < 1.0:
        # The parent did not carry its proxies, and moving them here is not the
        # answer. A LandscapeStreamingProxy is not a free-standing actor: its
        # transform is bound to the landscape's section layout, so setting all
        # 257 by hand spaces them correctly while their geometry stays at the
        # old scale — islands of terrain on a grid four times too coarse, with
        # flat gaps between. It looks like the same tile stamped over and over,
        # which is exactly what it is.
        warn("the parent transform did not reach the {} streaming proxies."
             .format(len(g["members"]) - 1))
        warn("Do NOT let anything move them individually — that breaks the")
        warn("section layout and tiles the terrain. Re-import instead, with the")
        warn("scale and location typed into the New Landscape panel:")
        warn("  Scale    X {}  Y {}  Z {}".format(sx, sy, sz))
        warn("  Location X 0  Y 0  Z {:.0f}  (the dialog centres on this)".format(lz))
        warn("Run 'clear-landscape', then import with those in the dialog. A")
        warn("landscape built at the right transform never needs moving.")
        return False

    log("placed {} (~{} x {}, {} actors): scale ({}, {}, {}) at "
        "({:.0f}, {:.0f}, {:.0f})".format(
            g["label"], g["res"], g["res"], len(g["members"]),
            sx, sy, sz, lx, ly, lz))
    log("  {:.2f} km square, sea level at Z=0, centred on the origin".format(
        tr["spanKM"]))
    log("  Save with Ctrl+S, then: ... build_sandiego.py look downtown")
    return True


OCEAN_LABEL = "SanDiegoOcean"
OCEAN_MATERIAL = "/Game/Materials/M_Ocean"


def _ocean_material():
    """A flat sea material, created once and reused.

    Deliberately not the Water plugin. A WaterBodyOcean needs the plugin
    enabled, a WaterZone actor, and a landscape set up to receive its terrain
    carving — three more things to go wrong on a level that has already been
    imported four times. A plane with a low-roughness material reads as ocean
    from any altitude you would actually look at a 17.6 km map from, and it
    cannot fail in a way that takes a screenshot to diagnose.
    """
    if unreal.EditorAssetLibrary.does_asset_exist(OCEAN_MATERIAL):
        return unreal.EditorAssetLibrary.load_asset(OCEAN_MATERIAL)
    try:
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        mat = tools.create_asset(
            OCEAN_MATERIAL.rsplit("/", 1)[1], OCEAN_MATERIAL.rsplit("/", 1)[0],
            unreal.Material, unreal.MaterialFactoryNew())
        mel = unreal.MaterialEditingLibrary

        col = mel.create_material_expression(
            mat, unreal.MaterialExpressionVectorParameter, -420, 0)
        col.set_editor_property("parameter_name", "WaterColour")
        # Deep water is much darker than people expect; most of what you see on
        # real ocean is reflected sky, which the roughness below provides.
        col.set_editor_property("default_value",
                                unreal.LinearColor(0.010, 0.048, 0.098, 1.0))
        mel.connect_material_property(col, "", unreal.MaterialProperty.MP_BASE_COLOR)

        rough = mel.create_material_expression(
            mat, unreal.MaterialExpressionScalarParameter, -420, 220)
        rough.set_editor_property("parameter_name", "Roughness")
        rough.set_editor_property("default_value", 0.055)
        mel.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)

        spec = mel.create_material_expression(
            mat, unreal.MaterialExpressionScalarParameter, -420, 340)
        spec.set_editor_property("parameter_name", "Specular")
        spec.set_editor_property("default_value", 1.0)
        mel.connect_material_property(spec, "", unreal.MaterialProperty.MP_SPECULAR)

        mel.recompile_material(mat)
        unreal.EditorAssetLibrary.save_asset(OCEAN_MATERIAL)
        log("created {}".format(OCEAN_MATERIAL))
        return mat
    except Exception as exc:                                     # noqa: BLE001
        warn("could not build the water material ({}) — the plane will be "
             "default grey, which still shows you the coastline".format(exc))
        return None


def water():
    """Lay a sea surface at Z=0 across the whole map.

    Sea level is world Z=0 by construction — that is the entire reason the
    landscape gets lifted 6000 uu on import — so the plane needs no fitting.
    It is sized and centred from the landscape's real bounds rather than from
    the intended ones, because those two have not always agreed.
    """
    meta = _meta_for_level()
    if not meta:
        return False

    span = (meta["resolution"] - 1) * meta["unrealLandscapeScale"]["x"]
    cx = cy = 0.0
    groups = [g for g in _landscape_groups()
              if abs(g["res"] - meta["resolution"]) <= max(2, meta["resolution"] * 0.02)]
    if groups:
        cx = groups[0]["minCorner"][0] + span / 2.0
        cy = groups[0]["minCorner"][1] + span / 2.0
        log("centring the sea on the landscape at ({:.0f}, {:.0f})".format(cx, cy))
    else:
        found = _find_landscapes()
        if found:
            loc = found[0].get_actor_location()
            cx = loc.x + span / 2.0
            cy = loc.y + span / 2.0
            log("single landscape actor (no streaming proxies); centring the "
                "sea on ({:.0f}, {:.0f})".format(cx, cy))
        else:
            warn("no landscape in this level — putting the sea on the origin")

    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in sub.get_all_level_actors():
        try:
            if a.get_actor_label() == OCEAN_LABEL:
                sub.destroy_actor(a)
        except Exception:                                        # noqa: BLE001
            continue

    mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Plane")
    if not mesh:
        warn("/Engine/BasicShapes/Plane is missing — cannot build the sea")
        return False

    actor = sub.spawn_actor_from_class(
        unreal.StaticMeshActor, unreal.Vector(cx, cy, 0.0), unreal.Rotator(0, 0, 0))
    actor.set_actor_label(OCEAN_LABEL)
    comp = actor.static_mesh_component
    comp.set_static_mesh(mesh)
    mat = _ocean_material()
    if mat:
        comp.set_material(0, mat)
    # The engine plane is 100 uu square. Overhang the map by 15% so there is
    # open water past the coastline instead of a visible edge to the world.
    s = (span * 1.15) / 100.0
    actor.set_actor_scale3d(unreal.Vector(s, s, 1.0))
    try:
        comp.set_editor_property("cast_shadow", False)
    except Exception:                                            # noqa: BLE001
        pass

    log("sea laid at Z=0, {:.1f} km across, centred on ({:.0f}, {:.0f})".format(
        span * 1.15 / 100000.0, cx, cy))
    log("Save with Ctrl+S. Then: ... build_sandiego.py overview")
    return True


BOUNDS_TAG = "SanDiegoOutOfBounds"


def _cube_builder():
    """A CubeBuilder, however this engine version is willing to give one.

    5.8 does not expose unreal.CubeBuilder as a generated Python type, so the
    direct constructor raises AttributeError and every out-of-bounds volume
    comes out with no brush and no collision. The class is still in the Engine
    module; it just has to be loaded by path.
    """
    try:
        return unreal.CubeBuilder()
    except AttributeError:
        pass
    for path in ("/Script/Engine.CubeBuilder", "/Script/Engine.Default__CubeBuilder"):
        try:
            cls = unreal.load_class(None, path)
            if cls:
                return unreal.new_object(cls)
        except Exception:                                        # noqa: BLE001
            continue
    return None


def _size_volume(vol, world, label, sx, sy, sz):
    """Make a volume the size asked for, by brush if possible and by scale if not.

    The fallback does not need to know how big the default brush is: it measures
    what it got and scales by the ratio. That is the only version of this that
    survives an engine upgrade, because the thing it depends on -- that a spawned
    volume has SOME brush -- is the part that does not change.
    """
    cube = _cube_builder()
    if cube is not None:
        for prop, value in (("x", sx), ("y", sy), ("z", sz)):
            try:
                cube.set_editor_property(prop, float(value))
            except Exception as exc:                             # noqa: BLE001
                warn("  CubeBuilder.{} did not take ({})".format(prop, exc))
        try:
            vol.set_editor_property("brush_builder", cube)
            cube.build(world, vol)
        except Exception as exc:                                 # noqa: BLE001
            warn("  brush build failed for {} ({})".format(label, exc))

    try:
        _, extent = vol.get_actor_bounds(False)
        got = (extent.x * 2.0, extent.y * 2.0, extent.z * 2.0)
        want = (sx, sy, sz)
        if all(abs(g - w) < max(200.0, w * 0.05) for g, w in zip(got, want)):
            return True
        if min(got) <= 1.0:
            warn("  {} has no brush at all to scale".format(label))
            return False
        vol.set_actor_scale3d(unreal.Vector(
            *[w / g for w, g in zip(want, got)]))
        log("  {} sized by scaling its default brush "
            "(CubeBuilder unavailable on this engine)".format(label))
        return True
    except Exception as exc:                                     # noqa: BLE001
        warn("  could not size {} ({})".format(label, exc))
        return False
BOUNDS_DAMAGE_PER_SEC = 12.0
BOUNDS_CEILING_M = 400.0
BOUNDS_FLOOR_M = -60.0


def bounds():
    """Ring the playable area with volumes that hurt.

    The map is the capture, 13.2 x 11.8 km, sitting in a 17.19 km frame. The
    2 km of terrain around it is real ground you can walk onto — that was the
    point, no invisible wall and no cliff at the edge — but staying there has
    to kill you.

    PainCausingVolume does exactly that with no gameplay code behind it: stand
    in one and it applies damage on an interval. Four of them, one per side,
    make a ring rather than a box, so the playable middle is untouched.

    Volumes spawned from Python come with no brush geometry, so the cube has to
    be built onto each one. That is verified by reading the actor's bounds back:
    a volume with an unbuilt brush reports a point, has no collision, and is
    indistinguishable in the Outliner from one that works.
    """
    meta = _meta_for_level()
    if not meta:
        return False
    play = meta.get("playableMetres")
    if not play:
        warn("this sidecar has no playableMetres — it predates the capture "
             "import. Re-pull, or re-run tools/maps3d-terrain.mjs.")
        return False

    span = (meta["resolution"] - 1) * meta["unrealLandscapeScale"]["x"]
    half = span / 2.0
    px = float(play["width"]) * 100.0 / 2.0
    py = float(play["height"]) * 100.0 / 2.0
    lo = BOUNDS_FLOOR_M * 100.0
    hi = BOUNDS_CEILING_M * 100.0
    cz = (lo + hi) / 2.0
    tall = hi - lo

    # (label, centre x, centre y, size x, size y). The east and west bands run
    # the full height; the north and south ones fill what is left between them.
    bands = [
        ("West", -(half + px) / 2.0, 0.0, half - px, span),
        ("East", (half + px) / 2.0, 0.0, half - px, span),
        ("North", 0.0, -(half + py) / 2.0, px * 2.0, half - py),
        ("South", 0.0, (half + py) / 2.0, px * 2.0, half - py),
    ]

    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    removed = 0
    for a in sub.get_all_level_actors():
        try:
            if a.actor_has_tag(BOUNDS_TAG):
                sub.destroy_actor(a)
                removed += 1
        except Exception:                                        # noqa: BLE001
            continue
    if removed:
        log("removed {} existing out-of-bounds volume(s)".format(removed))

    world = None
    try:
        world = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
    except Exception as exc:                                     # noqa: BLE001
        warn("could not reach the editor world ({})".format(exc))

    made = 0
    for label, cx, cy, sx, sy in bands:
        if sx <= 0 or sy <= 0:
            warn("{} band has no width — playable area is as big as the frame"
                 .format(label))
            continue
        vol = sub.spawn_actor_from_class(
            unreal.PainCausingVolume, unreal.Vector(cx, cy, cz),
            unreal.Rotator(0, 0, 0))
        if not vol:
            warn("could not spawn a PainCausingVolume for {}".format(label))
            continue
        vol.set_actor_label("{}_{}".format(BOUNDS_TAG, label))
        try:
            vol.set_editor_property("tags", [BOUNDS_TAG])
        except Exception as exc:                                 # noqa: BLE001
            warn("  tag did not take ({})".format(exc))

        _size_volume(vol, world, label, sx, sy, tall)

        for prop, value in (("pain_causing", True),
                            ("damage_per_sec", BOUNDS_DAMAGE_PER_SEC),
                            ("pain_interval", 1.0),
                            ("entry_pain", False)):
            try:
                vol.set_editor_property(prop, value)
            except Exception as exc:                             # noqa: BLE001
                warn("  {}.{} did not take ({})".format(label, prop, exc))

        # Read it back. A volume whose brush never built reports a zero extent
        # and has no collision, and nothing else says so.
        try:
            _, extent = vol.get_actor_bounds(False)
            got = (extent.x * 2, extent.y * 2, extent.z * 2)
            want = (sx, sy, tall)
            ok = all(abs(g - w) < max(200.0, w * 0.05)
                     for g, w in zip(got, want))
            log("  {:<5} {:.0f} x {:.0f} m at ({:.0f}, {:.0f}) {}".format(
                label, sx / 100.0, sy / 100.0, cx, cy,
                "" if ok else "-- BRUSH DID NOT BUILD, extent {:.0f} x {:.0f}"
                .format(got[0] / 100.0, got[1] / 100.0)))
            if not ok:
                warn("  {} has no usable volume. Place a PainCausingVolume by "
                     "hand from the Place Actors panel and re-run.".format(label))
        except Exception as exc:                                 # noqa: BLE001
            warn("  could not measure {} ({})".format(label, exc))
        made += 1

    log("{} out-of-bounds volume(s) around a {:.2f} x {:.2f} km playable area"
        .format(made, play["width"] / 1000.0, play["height"] / 1000.0))
    log("{:.0f} damage a second, from {:.0f} m below sea level to {:.0f} m up, "
        "so it catches aircraft too.".format(
            BOUNDS_DAMAGE_PER_SEC, -BOUNDS_FLOOR_M, BOUNDS_CEILING_M))
    log("The terrain out there is walkable on purpose — the edge of the world "
        "should be a warning, not a wall.")
    log("Save with Ctrl+S.")
    return made > 0


def overview():
    """Put the camera high enough to see the whole map at once.

    Judging a 17.6 km coastline from 1.5 km up, at an angle, is how four
    adjacent streaming proxies come to look like the same tile repeated. From
    directly above the whole thing, the silhouette is either San Diego or it
    is not, and no amount of flying around settles it faster.
    """
    meta = _meta_for_level()
    if not meta:
        return False
    span = (meta["resolution"] - 1) * meta["unrealLandscapeScale"]["x"]
    cx = cy = 0.0
    groups = [g for g in _landscape_groups()
              if abs(g["res"] - meta["resolution"]) <= max(2, meta["resolution"] * 0.02)]
    if groups:
        cx = groups[0]["minCorner"][0] + span / 2.0
        cy = groups[0]["minCorner"][1] + span / 2.0

    eye = unreal.Vector(cx, cy, span * 0.72)
    rot = unreal.Rotator(0.0, -89.0, -90.0)   # roll, pitch, yaw — looking down
    try:
        unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\
            .set_level_viewport_camera_info(eye, rot)
    except Exception as exc:                                     # noqa: BLE001
        warn("could not move the viewport ({})".format(exc))
        return False
    log("looking straight down from {:.1f} km over ({:.0f}, {:.0f})".format(
        span * 0.72 / 100000.0, cx, cy))
    log("Point Loma hooks south on the west side, Coronado closes the bay")
    log("behind it, and the mesas sit north and east. If that is not what you")
    log("see, the terrain is wrong rather than the camera.")
    return True


def _shade(h):
    """One character per elevation band, so a map fits in the log."""
    if h is None:
        return " "
    if h < -1:
        return "~"
    if h < 8:
        return "."
    if h < 30:
        return ":"
    if h < 60:
        return "+"
    if h < 95:
        return "#"
    return "@"


def sample(unused=None):
    """Compare the level's terrain against the file, per streaming proxy.

    The first version fired rays down and every single one missed: editor-world
    line traces do not hit landscape collision, so it measured nothing while
    looking like it had. This uses actor bounds instead — the same read that
    identify already gets right — so it cannot silently measure air.

    Each streaming proxy covers one tile of the map and its bounds carry the
    highest ground in that tile. Printing the level's tile maxima beside the
    file's answers the question directly: if the level is the top quarter
    repeated, its rows repeat and the file's do not.
    """
    meta = _meta_for_level()
    if not meta:
        return False
    groups = [g for g in _landscape_groups()
              if abs(g["res"] - meta["resolution"]) <= max(2, meta["resolution"] * 0.02)]
    if not groups:
        warn("no landscape in this level to compare")
        return False
    g = groups[0]

    proxies = [a for a in g["members"] if _is_proxy(a)]
    if not proxies:
        warn("no streaming proxies — nothing to tile-compare")
        return False

    x0, y0 = g["minCorner"]
    span = (meta["resolution"] - 1) * meta["unrealLandscapeScale"]["x"]

    # Work the grid pitch out from the proxies themselves rather than assuming.
    xs = sorted({round(a.get_actor_location().x) for a in proxies})
    pitch = min((b - a) for a, b in zip(xs, xs[1:])) if len(xs) > 1 else span
    n = int(round(span / pitch))
    if n < 2 or n > 64:
        warn("proxy grid looks wrong ({} across) — not comparing".format(n))
        return False

    level = [[None] * n for _ in range(n)]
    for a in proxies:
        b = _bounds(a)
        if not b:
            continue
        origin, extent = b
        loc = a.get_actor_location()
        i = int(round((loc.x - x0) / pitch))
        j = int(round((loc.y - y0) / pitch))
        if 0 <= i < n and 0 <= j < n:
            level[j][i] = (origin.z + extent.z) / 100.0     # highest ground, metres

    res = meta["resolution"]
    per = (res - 1) // n
    log("=" * 64)
    log("LEVEL (proxy bounds)      FILE (sandiego.r16)   highest ground per tile")
    log("~ sea  . 0-8  : 8-30  + 30-60  # 60-95  @ 95+   ' ' proxy not loaded")
    rows_seen = {}
    for j in range(n):
        left = "".join(_shade(level[j][i]) for i in range(n))
        right = []
        for i in range(n):
            hi = None
            # Corners and centre of the tile is enough to catch its maximum
            # without reading a quarter of a million samples per tile.
            for cc, rr in ((0, 0), (per, 0), (0, per), (per, per), (per // 2, per // 2)):
                v = _sample_metres(meta, min(res - 1, i * per + cc), min(res - 1, j * per + rr))
                if v is not None and (hi is None or v > hi):
                    hi = v
            right.append(_shade(hi))
        right = "".join(right)
        # Uniform rows — the sea bands top and bottom — repeat legitimately and
        # would otherwise report every correct map as tiled.
        if len(set(left.strip() or " ")) > 1:
            rows_seen.setdefault(left, []).append(j)
        log("{}    {}".format(left, right))
    log("=" * 64)

    blank = sum(1 for j in range(n) for i in range(n) if level[j][i] is None)
    if blank:
        log("{} of {} proxies unloaded (blank on the left).".format(blank, n * n))
    dupes = {k: v for k, v in rows_seen.items() if len(v) > 1 and k.strip()}
    if dupes:
        worst = max(dupes.values(), key=len)
        log("rows {} are identical — the level repeats itself, so the import "
            "read part of the file and tiled it.".format(
                ", ".join(str(x) for x in worst)))
    else:
        log("no repeated rows: the level is not tiled.")
    return True


def _all_metas():
    """Every heightmap sidecar in the folder, best-resolution-first.

    There is more than one now — 4033 and 2017 — and a command that always
    reads the first one compares the level against a file it was not built
    from, then reports the mismatch as a broken import. Which has happened.
    """
    out = []
    try:
        names = sorted(os.listdir(HEIGHTMAP_DIR))
    except Exception:                                            # noqa: BLE001
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        base = os.path.join(HEIGHTMAP_DIR, name[:-5])
        try:
            with open(base + ".json", "r") as fh:
                meta = json.load(fh)
            res = meta["resolution"]
        except Exception:                                        # noqa: BLE001
            continue
        raw = base + ".r16"
        if not os.path.exists(raw) or os.path.getsize(raw) != res * res * 2:
            continue
        meta["_raw_path"] = os.path.abspath(raw)
        png = base + ".png"
        meta["_png_path"] = os.path.abspath(png) if os.path.exists(png) else None
        out.append(meta)
    return out


def _meta_for_level():
    """The sidecar matching the landscape actually in the level.

    Falls back to the default when nothing matches, but says so — comparing a
    2017 level against the 4033 file produces a confident, wrong answer.
    """
    metas = _all_metas()
    if not metas:
        return load_meta()
    groups = _landscape_groups()
    for g in groups:
        for m in metas:
            if abs(g["res"] - m["resolution"]) <= max(2, m["resolution"] * 0.02):
                return m
    # Bounds can come back degenerate on a single-actor landscape. Scale is
    # unambiguous either way: 436.508 is the 4033 map, 873.016 is the 2017 one.
    for a in _find_landscapes():
        try:
            sx = a.get_actor_scale3d().x
        except Exception:                                        # noqa: BLE001
            continue
        for m in metas:
            if abs(sx - m["unrealLandscapeScale"]["x"]) < 0.5:
                return m
    if groups:
        warn("the landscape here reads as ~{} x {}, which matches none of the "
             "heightmaps present ({}).".format(
                 groups[0]["res"], groups[0]["res"],
                 ", ".join(str(m["resolution"]) for m in metas)))
    return load_meta()


def load_all():
    """Load every World Partition actor, so the level in memory is the level.

    This is the missing step behind most of a day's confusion. Unreal streams a
    partitioned level in the editor too, and `get_all_level_actors` returns only
    what is loaded — so a landscape that imported perfectly reports as absent,
    reads back with stale bounds, and renders as a few tiles floating in space.
    Every one of those looks exactly like a broken import.

    The APIs for this move between versions, so try them in turn and say which
    one worked rather than assuming.
    """
    loaded = False

    # 1. The subsystem, if this build exposes a loading call on it.
    try:
        sub = unreal.get_editor_subsystem(unreal.WorldPartitionSubsystem)
        for name in ("load_all_cells", "load_all_actors", "load_all"):
            fn = getattr(sub, name, None)
            if callable(fn):
                fn()
                log("loaded via WorldPartitionSubsystem.{}()".format(name))
                loaded = True
                break
    except Exception:                                            # noqa: BLE001
        pass

    # 2. The blueprint library, which is where the actor-descriptor calls live.
    if not loaded:
        lib = getattr(unreal, "WorldPartitionBlueprintLibrary", None)
        if lib is not None:
            try:
                descs = None
                for name in ("get_intersecting_actor_descs", "get_all_actor_descs"):
                    fn = getattr(lib, name, None)
                    if callable(fn):
                        descs = fn() if name == "get_all_actor_descs" else None
                        break
                if descs:
                    lib.load_actors(descs)
                    log("loaded {} actors via WorldPartitionBlueprintLibrary"
                        .format(len(descs)))
                    loaded = True
            except Exception as exc:                             # noqa: BLE001
                warn("WorldPartitionBlueprintLibrary did not take it ({})".format(exc))

    n = len(_find_landscapes())
    log("landscape actors visible to Python: {}".format(n))
    if n:
        log("{} actors are loaded — commands that read the level will see them."
            .format(n))
        for g in _landscape_groups():
            log("  {} reads as ~{} x {} across {} actors".format(
                g["label"], g["res"], g["res"], len(g["members"])))
    if not loaded and not n:
        warn("could not load regions from script on this build. Do it in the UI:")
        warn("  Window -> World Partition -> World Partition Editor")
        warn("  drag a box over the whole minimap, right-click -> Load Region")
        warn("Then re-run this. Until the actors are loaded, every other command")
        warn("here is reading a fraction of the level and reporting it as fact.")
    elif n == 0:
        warn("still nothing — the level may genuinely have no landscape.")
    return loaded


def wp_api():
    """List what this build exposes for World Partition, for when load fails."""
    names = sorted(n for n in dir(unreal) if "WorldPartition" in n)
    if not names:
        log("no WorldPartition symbols in this build's Python bindings")
        return names
    for n in names:
        obj = getattr(unreal, n, None)
        calls = []
        try:
            calls = [m for m in dir(obj)
                     if ("load" in m.lower() or "cell" in m.lower())
                     and not m.startswith("_")][:8]
        except Exception:                                        # noqa: BLE001
            pass
        log("  {}{}".format(n, ("  -> " + ", ".join(calls)) if calls else ""))
    return names


LAND_MATERIAL = "/Game/Materials/M_SanDiego_Land"
SURFACE_TEXTURE = "/Game/Textures/T_SanDiego_Surfaces"


def _import_surface_texture():
    """Bring sandiego-surfaces.png in as a texture asset, or None with a reason.

    This is a data map, not a picture: R is a road class, G a land cover class,
    B water. So sRGB is off — an sRGB curve would bend every code on the way in
    and the thresholds below would land between classes — and the compression is
    the uncompressed one. Block compression averages 4x4 blocks, and a two-pixel
    wide alley encoded as "160" next to bare ground encoded as "0" comes back as
    neither. 4096 x 4096 RGBA8 is 67 MB resident, which is a fair price for the
    whole city's ground reading correctly.
    """
    png = os.path.join(HEIGHTMAP_DIR, "sandiego-surfaces.png")
    if not os.path.exists(png):
        warn("MISSING {} — pull the repo, or re-run tools/maps3d-surfaces.mjs"
             .format(png))
        return None

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", png)
    task.set_editor_property("destination_path", SURFACE_TEXTURE.rsplit("/", 1)[0])
    task.set_editor_property("destination_name", SURFACE_TEXTURE.rsplit("/", 1)[1])
    task.set_editor_property("automated", True)          # no import dialog
    task.set_editor_property("replace_existing", True)   # re-import, don't stack
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    tex = unreal.EditorAssetLibrary.load_asset(SURFACE_TEXTURE)
    if not tex:
        warn("the import produced no asset at {}".format(SURFACE_TEXTURE))
        return None
    for prop, value in (
            ("srgb", False),
            ("compression_settings",
             unreal.TextureCompressionSettings.TC_VECTOR_DISPLACEMENTMAP),
            ("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD),
    ):
        try:
            tex.set_editor_property(prop, value)
        except Exception as exc:                                 # noqa: BLE001
            warn("  texture.{} did not take ({})".format(prop, exc))
    unreal.EditorAssetLibrary.save_asset(SURFACE_TEXTURE)
    try:
        log("imported {} ({} x {})".format(
            SURFACE_TEXTURE, tex.blueprint_get_size_x(), tex.blueprint_get_size_y()))
    except Exception:                                            # noqa: BLE001
        log("imported {}".format(SURFACE_TEXTURE))
    return tex


def _expr(mat, class_name, x, y, **props):
    """Create a material node by class name, or None with a reason."""
    cls = getattr(unreal, class_name, None)
    if cls is None:
        warn("this build has no unreal.{}".format(class_name))
        return None
    node = unreal.MaterialEditingLibrary.create_material_expression(mat, cls, x, y)
    for k, v in props.items():
        try:
            node.set_editor_property(k, v)
        except Exception as exc:                                 # noqa: BLE001
            warn("  {}.{} did not take ({})".format(class_name, k, exc))
    return node


_WIRE_FAILURES = []


def _wire(a, a_out, b, b_in):
    """Connect two nodes, and notice when it does not happen.

    connect_material_expressions returns False for an unknown pin name rather
    than raising, and the first version ignored that. Three Clamp nodes went
    unconnected, the material failed to compile, every landscape fell back to
    the default grey — and the script reported success. Silent failures have
    cost more today than loud ones.
    """
    # b_in may be several candidate pin names. Unreal names a node's single
    # unnamed input "" rather than "Input" — which is why Clamp failed, and
    # then ComponentMask failed the same way one commit later. Trying the
    # plausible names beats discovering them one compile error at a time.
    names = (b_in,) if isinstance(b_in, str) else tuple(b_in)
    outs = (a_out,) if isinstance(a_out, str) else tuple(a_out)
    for out in outs:
        for name in names:
            try:
                if unreal.MaterialEditingLibrary.connect_material_expressions(
                        a, out, b, name):
                    return True
            except Exception as exc:                             # noqa: BLE001
                warn("connect {} -> {} raised ({})".format(out or "out", name, exc))
    _WIRE_FAILURES.append("{} -> {}".format(
        "/".join(o or "out" for o in outs), "/".join(names)))
    return False


def _saturate(mat, src, x, y):
    """Clamp a scalar to 0..1, built from Min and Max.

    Not MaterialExpressionClamp: its first pin is not called "Input" on this
    build, and a mis-named pin connects to nothing without complaining. Min and
    Max take A and B, which the rest of this graph already proves work.
    """
    hi = _expr(mat, "MaterialExpressionMax", x, y, const_b=0.0)
    lo = _expr(mat, "MaterialExpressionMin", x + 130, y, const_b=1.0)
    if not (hi and lo):
        return None
    _wire(src, "", hi, "A")
    _wire(hi, "", lo, "A")
    return lo


def _ramp(mat, src, src_out, lo, hi, x, y):
    """saturate((src - lo) / (hi - lo)) — 0 at lo, 1 at hi, flat outside."""
    sub = _expr(mat, "MaterialExpressionSubtract", x, y, const_b=float(lo))
    div = _expr(mat, "MaterialExpressionDivide", x + 130, y,
                const_b=float(hi) - float(lo))
    if not (sub and div):
        return None
    _wire(src, src_out, sub, "A")
    _wire(sub, "", div, "A")
    return _saturate(mat, div, x + 260, y)


# The land cover palette, in the order the codes run. maps3d-surfaces.mjs writes
# one code per class into G; reading them back as a chain of blends up the same
# order means a pixel that landed exactly on a code gets exactly that colour,
# and a pixel the mip chain averaged between two codes gets a blend of the two
# neighbours rather than whatever class happens to sit at the average.
COVER_BANDS = [
    (30,  "CoverOther",    (0.235, 0.216, 0.180)),
    (50,  "CoverUrban",    (0.268, 0.256, 0.240)),
    (80,  "CoverRock",     (0.196, 0.174, 0.150)),
    (110, "CoverSand",     (0.505, 0.446, 0.322)),
    (140, "CoverWetland",  (0.148, 0.163, 0.122)),
    (165, "CoverFarmland", (0.243, 0.238, 0.130)),
    (190, "CoverGrass",    (0.196, 0.216, 0.110)),
    (220, "CoverWood",     (0.098, 0.128, 0.072)),
]


def _land_material():
    """Build the landscape material: colour from height and slope.

    No painted layers. Weightmaps would mean hand-painting 17.6 km of coastline,
    and the heightfield already knows where the beaches and the mesa tops are —
    height and surface normal reproduce San Diego's banding well enough that
    painting would only be refining it.

    Everything is a named parameter, so the colours can be pushed around in the
    material editor without coming back here.
    """
    # Always rebuild. A material that failed to compile is still an asset, and
    # reusing it silently reinstates the bug it was rebuilt to fix.
    if unreal.EditorAssetLibrary.does_asset_exist(LAND_MATERIAL):
        log("replacing the existing {}".format(LAND_MATERIAL))
        try:
            unreal.EditorAssetLibrary.delete_asset(LAND_MATERIAL)
        except Exception as exc:                                 # noqa: BLE001
            warn("could not delete it ({}) — rewiring in place".format(exc))

    del _WIRE_FAILURES[:]
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    folder, name = LAND_MATERIAL.rsplit("/", 1)
    mat = tools.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())
    if not mat:
        warn("could not create {}".format(LAND_MATERIAL))
        return None

    # --- height, in metres above sea level -------------------------------
    wp = _expr(mat, "MaterialExpressionWorldPosition", -1500, -200)
    z = _expr(mat, "MaterialExpressionComponentMask", -1300, -200,
              r=False, g=False, b=True, a=False)
    metres = _expr(mat, "MaterialExpressionDivide", -1150, -200, const_b=100.0)
    if not (wp and z and metres):
        return mat
    _wire(wp, "", z, ("", "Input"))
    _wire(z, "", metres, "A")

    # Beach -> scrub over the first few metres of dry land.
    beach_h = _expr(mat, "MaterialExpressionScalarParameter", -1150, -80,
                    parameter_name="BeachTopMetres", default_value=11.0)
    t_beach = _expr(mat, "MaterialExpressionDivide", -950, -200)
    _wire(metres, "", t_beach, "A")
    _wire(beach_h, "", t_beach, "B")
    c_beach = _saturate(mat, t_beach, -810, -200)

    # Scrub -> mesa top. Offset first so the transition starts off the coast.
    mesa_lo = _expr(mat, "MaterialExpressionScalarParameter", -1150, 60,
                    parameter_name="MesaStartMetres", default_value=45.0)
    mesa_sp = _expr(mat, "MaterialExpressionScalarParameter", -1150, 140,
                    parameter_name="MesaSpanMetres", default_value=55.0)
    off = _expr(mat, "MaterialExpressionSubtract", -950, 20)
    t_mesa = _expr(mat, "MaterialExpressionDivide", -800, 20)
    _wire(metres, "", off, "A")
    _wire(mesa_lo, "", off, "B")
    _wire(off, "", t_mesa, "A")
    _wire(mesa_sp, "", t_mesa, "B")
    c_mesa = _saturate(mat, t_mesa, -660, 20)

    # --- slope, 0 flat to 1 vertical -------------------------------------
    nrm = _expr(mat, "MaterialExpressionVertexNormalWS", -1500, 300)
    nz = _expr(mat, "MaterialExpressionComponentMask", -1300, 300,
               r=False, g=False, b=True, a=False)
    inv = _expr(mat, "MaterialExpressionSubtract", -1150, 300, const_a=1.0)
    gain = _expr(mat, "MaterialExpressionScalarParameter", -1150, 400,
                 parameter_name="SlopeGain", default_value=3.2)
    steep = _expr(mat, "MaterialExpressionMultiply", -950, 300)
    c_slope = None
    if nrm and nz and inv and steep:
        _wire(nrm, "", nz, ("", "Input"))
        _wire(nz, "", inv, "B")
        _wire(inv, "", steep, "A")
        _wire(gain, "", steep, "B")
        c_slope = _saturate(mat, steep, -810, 300)

    # --- the palette ------------------------------------------------------
    def colour(label, pos_y, rgb):
        node = _expr(mat, "MaterialExpressionVectorParameter", -650, pos_y,
                     parameter_name=label)
        if node:
            node.set_editor_property(
                "default_value", unreal.LinearColor(rgb[0], rgb[1], rgb[2], 1.0))
        return node

    # Southern California, not Ireland: the scrub is olive-grey and burnt off
    # for most of the year, and the mesa tops are drier still.
    sand = colour("Sand", -420, (0.470, 0.412, 0.290))
    scrub = colour("Scrub", -300, (0.166, 0.180, 0.104))
    mesa = colour("MesaTop", -180, (0.250, 0.230, 0.148))
    rock = colour("Rock", -60, (0.196, 0.174, 0.150))

    mix1 = _expr(mat, "MaterialExpressionLinearInterpolate", -420, 120)
    mix2 = _expr(mat, "MaterialExpressionLinearInterpolate", -260, 120)
    mix3 = _expr(mat, "MaterialExpressionLinearInterpolate", -100, 120)
    if not (sand and scrub and mesa and rock and mix1 and mix2 and mix3):
        warn("palette incomplete — the material will be partly unwired")
        return mat
    _wire(sand, "", mix1, "A")
    _wire(scrub, "", mix1, "B")
    _wire(c_beach, "", mix1, "Alpha")

    _wire(mix1, "", mix2, "A")
    _wire(mesa, "", mix2, "B")
    _wire(c_mesa, "", mix2, "Alpha")

    _wire(mix2, "", mix3, "A")
    _wire(rock, "", mix3, "B")
    if c_slope:
        _wire(c_slope, "", mix3, "Alpha")

    # --- the capture's own surfaces, sampled by world position -------------
    #
    # Everything above is inference: height says beach, slope says cliff. This
    # part is not inference. The capture knows where the parks, the scrub, the
    # sand, the water and the paving actually are, and maps3d-surfaces.mjs baked
    # that into one RGB map covering exactly the landscape's footprint. Sampling
    # it by world position puts each class back on the ground it came off.
    #
    # It composites over the height/slope colour rather than replacing it, so
    # any pixel the capture had nothing to say about still gets a sensible
    # coast-to-mesa reading instead of a hole.
    ground = mix3
    surf_alpha = None
    tex = _import_surface_texture()
    meta = load_meta()
    if tex and meta:
        span_uu = transform_for(meta)["spanUU"]
        tex_res = 4096
        side = os.path.join(HEIGHTMAP_DIR, "sandiego-surfaces.json")
        if os.path.exists(side):
            with open(side, "r") as handle:
                tex_res = int(json.load(handle)["resolution"])
        # Pixel 0 of the map is the frame's minimum corner and pixel res-1 the
        # maximum, so the sampled range is res-1 texels wide, offset by half a
        # texel. Skipping that shifts the whole city two metres north-west.
        edge = float(tex_res - 1) / float(tex_res)
        uv_scale = edge / span_uu                    # per centimetre of world
        uv_bias = 0.5 * edge + 0.5 / float(tex_res)

        wp2 = _expr(mat, "MaterialExpressionWorldPosition", -1500, 700)
        xy = _expr(mat, "MaterialExpressionComponentMask", -1300, 700,
                   r=True, g=True, b=False, a=False)
        uvm = _expr(mat, "MaterialExpressionMultiply", -1150, 700,
                    const_b=uv_scale)
        uva = _expr(mat, "MaterialExpressionAdd", -1000, 700, const_b=uv_bias)
        samp = _expr(mat, "MaterialExpressionTextureSampleParameter2D",
                     -850, 700, parameter_name="SurfaceMap", texture=tex)
        try:
            samp.set_editor_property(
                "sampler_type",
                unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
        except Exception as exc:                                 # noqa: BLE001
            warn("  sampler_type did not take ({})".format(exc))

        if wp2 and xy and uvm and uva and samp:
            _wire(wp2, "", xy, ("", "Input"))
            _wire(xy, "", uvm, "A")
            _wire(uvm, "", uva, "A")
            _wire(uva, "", samp, ("UVs", "UV", ""))

            # Land cover, blended up the code order.
            cover = None
            y = 560
            for i, (code, label, rgb) in enumerate(COVER_BANDS):
                col = _expr(mat, "MaterialExpressionVectorParameter", -560, y,
                            parameter_name=label)
                if col:
                    col.set_editor_property(
                        "default_value",
                        unreal.LinearColor(rgb[0], rgb[1], rgb[2], 1.0))
                if cover is None:
                    cover = col                      # the floor of the chain
                else:
                    prev = COVER_BANDS[i - 1][0] / 255.0
                    a = _ramp(mat, samp, "G", prev, code / 255.0, -380, y)
                    mix = _expr(mat, "MaterialExpressionLinearInterpolate",
                                60, y)
                    if cover and col and a and mix:
                        _wire(cover, "", mix, "A")
                        _wire(col, "", mix, "B")
                        _wire(a, "", mix, "Alpha")
                        cover = mix
                y += 110

            # Any non-zero code counts as cover; the lowest is 30/255.
            cov_gain = _expr(mat, "MaterialExpressionMultiply", -560, 1460,
                             const_b=20.0)
            cov_a = None
            if cov_gain:
                _wire(samp, "G", cov_gain, "A")
                cov_a = _saturate(mat, cov_gain, -400, 1460)

            # Paving. Sidewalks, paths and rail read as concrete; parking and
            # up as asphalt. The road decks are separate geometry sitting above
            # this, so what the ground layer is really for is everything that
            # never got a deck.
            pale = _expr(mat, "MaterialExpressionVectorParameter", -560, 1560,
                         parameter_name="PavePale")
            dark = _expr(mat, "MaterialExpressionVectorParameter", -560, 1670,
                         parameter_name="PaveAsphalt")
            if pale:
                pale.set_editor_property(
                    "default_value", unreal.LinearColor(0.400, 0.392, 0.372, 1.0))
            if dark:
                dark.set_editor_property(
                    "default_value", unreal.LinearColor(0.052, 0.051, 0.055, 1.0))
            pave_t = _ramp(mat, samp, "R", 0.28, 0.46, -380, 1620)
            pave = _expr(mat, "MaterialExpressionLinearInterpolate", 60, 1620)
            road_gain = _expr(mat, "MaterialExpressionMultiply", -560, 1790,
                              const_b=14.0)
            road_a = None
            if road_gain:
                _wire(samp, "R", road_gain, "A")
                road_a = _saturate(mat, road_gain, -400, 1790)
            if pale and dark and pave_t and pave:
                _wire(pale, "", pave, "A")
                _wire(dark, "", pave, "B")
                _wire(pave_t, "", pave, "Alpha")

            # Water last, because that is the order it was baked in: a bridge
            # deck's surface polygon lies over the channel it crosses, and the
            # channel is the thing you want to see.
            bed = _expr(mat, "MaterialExpressionVectorParameter", -560, 1900,
                        parameter_name="WaterBed")
            if bed:
                bed.set_editor_property(
                    "default_value", unreal.LinearColor(0.036, 0.070, 0.078, 1.0))
            wat_gain = _expr(mat, "MaterialExpressionMultiply", -560, 2010,
                             const_b=4.0)
            wat_a = None
            if wat_gain:
                _wire(samp, "B", wat_gain, "A")
                wat_a = _saturate(mat, wat_gain, -400, 2010)

            for layer, alpha, y_at in ((cover, cov_a, 300),
                                       (pave, road_a, 420),
                                       (bed, wat_a, 540)):
                if not (layer and alpha):
                    continue
                over = _expr(mat, "MaterialExpressionLinearInterpolate", 300, y_at)
                if not over:
                    continue
                _wire(ground, "", over, "A")
                _wire(layer, "", over, "B")
                _wire(alpha, "", over, "Alpha")
                ground = over
            surf_alpha = wat_a
        else:
            warn("surface sampling incomplete — falling back to height and slope")
    elif not tex:
        warn("no surface texture, so the terrain is height and slope only")

    unreal.MaterialEditingLibrary.connect_material_property(
        ground, "", unreal.MaterialProperty.MP_BASE_COLOR)

    rough = _expr(mat, "MaterialExpressionScalarParameter", -260, 300,
                  parameter_name="Roughness", default_value=0.88)
    rough_out = rough
    if rough and surf_alpha:
        # Wet ground is not 0.88 rough. Free, since the mask already exists.
        wet = _expr(mat, "MaterialExpressionScalarParameter", -260, 380,
                    parameter_name="RoughnessWater", default_value=0.10)
        blend = _expr(mat, "MaterialExpressionLinearInterpolate", 300, 660)
        if wet and blend:
            _wire(rough, "", blend, "A")
            _wire(wet, "", blend, "B")
            _wire(surf_alpha, "", blend, "Alpha")
            rough_out = blend
    if rough_out:
        unreal.MaterialEditingLibrary.connect_material_property(
            rough_out, "", unreal.MaterialProperty.MP_ROUGHNESS)

    if _WIRE_FAILURES:
        warn("{} connection(s) did not take: {}".format(
            len(_WIRE_FAILURES), ", ".join(_WIRE_FAILURES)))
        warn("The material will not compile with unconnected inputs, and the "
             "landscape would fall back to default grey. Not saving.")
        return None

    unreal.MaterialEditingLibrary.recompile_material(mat)
    unreal.EditorAssetLibrary.save_asset(LAND_MATERIAL)
    log("built {}, every connection verified".format(LAND_MATERIAL))
    return mat


def material():
    """Build the landscape material and put it on the terrain."""
    mat = _land_material()
    if not mat:
        return False

    targets = _find_landscapes()
    if not targets:
        warn("no landscape in this level to assign it to")
        return False

    done = 0
    for a in targets:
        try:
            a.set_editor_property("landscape_material", mat)
            done += 1
        except Exception as exc:                                 # noqa: BLE001
            warn("could not assign to {} ({})".format(a.get_actor_label(), exc))
    log("assigned to {} of {} landscape actor(s)".format(done, len(targets)))
    log("Sand below {} m, scrub above it, mesa tops from ~{} m, rock on the "
        "steep faces.".format(11, 45))
    log("Over that: the capture's own land cover, paving and water, read out of "
        "{} by world position.".format(SURFACE_TEXTURE))
    log("Every colour and threshold is a named parameter — open "
        "{} to push them around.".format(LAND_MATERIAL))
    log("Save with Ctrl+S. Shaders will compile for a minute first.")
    return True


ROAD_MATERIAL = "/Game/Materials/M_Road"
ROAD_TAG = "SanDiegoFreeway"


def actors(pattern=""):
    """List what is actually in this level, grouped, with counts and extents.

    Guessing at an actor's tag from a screenshot is how the legacy freeways
    survived three re-imports. This prints what is really there: the label stem,
    how many, what class, and where the group sits -- enough to say "those grey
    slabs over the bay are these" and then remove exactly those.
    """
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    groups = {}
    for a in sub.get_all_level_actors():
        try:
            label = a.get_actor_label()
            if pattern and pattern.lower() not in label.lower():
                continue
            stem = label.rstrip("0123456789_") or label
            cls = a.get_class().get_name()
            key = (stem, cls)
            g = groups.setdefault(key, {"n": 0, "tags": set(), "z": []})
            g["n"] += 1
            try:
                for t in a.get_editor_property("tags") or []:
                    g["tags"].add(str(t))
            except Exception:                                    # noqa: BLE001
                pass
            g["z"].append(a.get_actor_location().z)
        except Exception:                                        # noqa: BLE001
            continue
    if not groups:
        log("no actors matched '{}'".format(pattern))
        return 0
    log("=" * 72)
    log("{:<34} {:>6}  {:<22} {}".format("label stem", "count", "class", "tags"))
    for (stem, cls), g in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
        log("{:<34} {:>6}  {:<22} {}".format(
            stem[:34], g["n"], cls[:22], ",".join(sorted(g["tags"])) or "-"))
    log("=" * 72)
    log("Remove a group with: ... build_sandiego.py drop <label stem>")
    return len(groups)


def drop(stem=""):
    """Delete every actor whose label starts with `stem`. Say what went."""
    if not stem:
        warn("drop needs a label stem -- run `actors` first to see them")
        return 0
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    removed = 0
    for a in sub.get_all_level_actors():
        try:
            if a.get_actor_label().startswith(stem):
                sub.destroy_actor(a)
                removed += 1
        except Exception:                                        # noqa: BLE001
            continue
    log("dropped {} actor(s) whose label starts with '{}'".format(removed, stem))
    if removed:
        log("Save with Ctrl+S.")
    return removed


def clear_roads():
    """Remove the freeways laid by the legacy `roads` command.

    Road geometry comes through the packed buffer now -- road_deck, line_white,
    line_yellow and kerb, built by tools/maps3d-roadmesh.mjs from traced
    centrelines. The old freeway actors are from the invented plan that
    preceded it, and nothing removed them: `clear-landscape` only deletes
    landscapes and `city` only clears what `city` placed, so they survived
    every re-import and sat on top of the new streets.
    """
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    removed = 0
    for a in sub.get_all_level_actors():
        try:
            if a.actor_has_tag(ROAD_TAG) or a.get_actor_label().startswith(ROAD_TAG):
                sub.destroy_actor(a)
                removed += 1
        except Exception:                                        # noqa: BLE001
            continue
    log("removed {} legacy freeway actor(s)".format(removed))
    if removed:
        log("Save with Ctrl+S.")
    return removed

# The real corridors, traced from the reference map, in the same normalised
# (u, v) as everything else. Widths are metres.
# The real corridors, re-traced off a Google Maps capture and registered onto
# this frame with three landmarks: Cabrillo, downtown and National City. That
# registration checks out — National City lands on u 0.972, which is where the
# PLACES data independently puts it.
#
# (id, width_m, is_bridge, points). Widths are metres.
FREEWAYS = [
    # I-5: down from Old Town past the airport, along the east side of
    # downtown, then south-east to National City.
    ("i5", 26, False, [
        (0.445, 0.000), (0.454, 0.082), (0.465, 0.217), (0.485, 0.298),
        (0.513, 0.365), (0.547, 0.419), (0.582, 0.473), (0.616, 0.514),
        (0.661, 0.567), (0.713, 0.615), (0.764, 0.655), (0.821, 0.702),
        (0.878, 0.742), (0.935, 0.783), (0.992, 0.850)]),
    # I-8 through Mission Valley, running east from Ocean Beach along the
    # river. It starts further west than the old trace had it.
    ("i8", 24, False, [
        (0.149, 0.153), (0.206, 0.153), (0.297, 0.136), (0.388, 0.116),
        (0.490, 0.103), (0.582, 0.089), (0.690, 0.069), (0.809, 0.055),
        (0.923, 0.042), (1.000, 0.035)]),
    # I-805, parallel to I-5 but inland. It leaves the eastern edge of the
    # frame around National City's latitude, which is where it really goes.
    ("i805", 22, False, [
        (0.809, 0.000), (0.832, 0.082), (0.855, 0.177), (0.889, 0.285),
        (0.923, 0.378), (0.958, 0.460), (0.986, 0.541), (1.000, 0.585)]),
    # SR-163 south out of Mission Valley, through Balboa Park to downtown.
    ("sr163", 18, False, [
        (0.650, 0.095), (0.659, 0.163), (0.665, 0.210), (0.670, 0.284),
        (0.670, 0.365), (0.661, 0.433), (0.644, 0.487), (0.627, 0.514)]),
    # SR-94 east out of downtown. The old trace had it running south-east;
    # it actually climbs slightly north of east toward Lemon Grove.
    ("sr94", 18, False, [
        (0.650, 0.554), (0.718, 0.548), (0.809, 0.527), (0.901, 0.507),
        (0.992, 0.498), (1.000, 0.497)]),
    # The Coronado bridge, and then SR-75 continuing down the Silver Strand.
    # Split in two because only the first half is a bridge — the old version
    # held the whole route at 62 m, including the part on dry sand.
    ("sr75_bridge", 16, True, [
        (0.678, 0.597), (0.650, 0.618), (0.616, 0.641), (0.582, 0.664),
        (0.547, 0.682), (0.519, 0.696)]),
    ("sr75_strand", 16, False, [
        (0.519, 0.696), (0.536, 0.783), (0.570, 0.864), (0.593, 0.945),
        (0.616, 1.000)]),
    # I-15 is deliberately absent: it runs east of this frame's edge, and a
    # stub drawn along the boundary would be a line, not a freeway.
]

SEGMENT_METRES = 160.0
# Steepest grade a route may climb. Interstates top out near 6%; this is a
# little over that, which reads as a road without needing real earthworks.
MAX_GRADE = 0.08


def _road_material():
    """Dark asphalt. Deliberately duller than the ground so the routes read."""
    if unreal.EditorAssetLibrary.does_asset_exist(ROAD_MATERIAL):
        return unreal.EditorAssetLibrary.load_asset(ROAD_MATERIAL)
    try:
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        folder, name = ROAD_MATERIAL.rsplit("/", 1)
        mat = tools.create_asset(name, folder, unreal.Material,
                                 unreal.MaterialFactoryNew())
        col = _expr(mat, "MaterialExpressionVectorParameter", -420, 0)
        col.set_editor_property("parameter_name", "Asphalt")
        col.set_editor_property("default_value",
                                unreal.LinearColor(0.030, 0.029, 0.031, 1.0))
        unreal.MaterialEditingLibrary.connect_material_property(
            col, "", unreal.MaterialProperty.MP_BASE_COLOR)
        rough = _expr(mat, "MaterialExpressionScalarParameter", -420, 200)
        rough.set_editor_property("parameter_name", "Roughness")
        rough.set_editor_property("default_value", 0.72)
        unreal.MaterialEditingLibrary.connect_material_property(
            rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
        unreal.MaterialEditingLibrary.recompile_material(mat)
        unreal.EditorAssetLibrary.save_asset(ROAD_MATERIAL)
        log("built {}".format(ROAD_MATERIAL))
        return mat
    except Exception as exc:                                     # noqa: BLE001
        warn("could not build the road material ({})".format(exc))
        return None


def _resample(pts, step_uv):
    """Walk a polyline at a fixed spacing, so segments follow the ground."""
    out = []
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        span = math.hypot(bx - ax, by - ay)
        n = max(1, int(math.ceil(span / step_uv)))
        for k in range(n):
            t = k / float(n)
            out.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    out.append(pts[-1])
    return out


def roads():
    """Lay the freeways on the terrain, following the heightmap.

    Heights come from the .r16 rather than from a trace: editor-world line
    traces do not hit landscape collision, which is why the first version of
    `sample` measured nothing. Reading the file gives the same answer without
    depending on the editor at all.
    """
    meta = _meta_for_level()
    if not meta:
        return False

    res = meta["resolution"]
    span_uu = (res - 1) * meta["unrealLandscapeScale"]["x"]
    x0 = y0 = -span_uu / 2.0
    groups = [g for g in _landscape_groups()
              if abs(g["res"] - meta["resolution"]) <= max(2, meta["resolution"] * 0.02)]
    if groups:
        x0, y0 = groups[0]["minCorner"]
    else:
        found = _find_landscapes()
        if found:
            loc = found[0].get_actor_location()
            x0, y0 = loc.x, loc.y

    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    removed = 0
    for a in sub.get_all_level_actors():
        try:
            if a.get_actor_label().startswith(ROAD_TAG):
                sub.destroy_actor(a)
                removed += 1
        except Exception:                                        # noqa: BLE001
            continue
    if removed:
        log("cleared {} existing road pieces".format(removed))

    mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
    if not mesh:
        warn("/Engine/BasicShapes/Cube is missing — cannot build roads")
        return False
    mat = _road_material()

    frame_w = float(meta["frameMetres"]["width"])
    step_uv = SEGMENT_METRES / frame_w
    placed = 0

    for name, width_m, bridge, pts in FREEWAYS:
        walk = _resample(pts, step_uv)
        # A bridge holds its deck; everything else follows the ground. The
        # flag is per-route because SR-75 is both: a bridge over the channel,
        # then an ordinary road down the Silver Strand.
        deck = 62.0 if bridge else None     # metres above sea level

        world = []
        for u, v in walk:
            p = uv_to_world(meta, u, v)
            h = _sample_metres(meta, p["col"], p["row"])
            if h is None:
                h = 0.0
            z = deck if bridge else max(h, 0.0) + 1.6
            world.append((x0 + (p["x"] + span_uu / 2.0),
                          y0 + (p["y"] + span_uu / 2.0),
                          z * 100.0))

        # Smooth the profile. Laid straight onto the heightfield the routes
        # climb mesa escarpments at 27 degrees, because that is what the ground
        # does there — but a freeway cuts and fills rather than following a
        # cliff. A few averaging passes bring the worst grades under control
        # without needing real earthworks.
        if not bridge and len(world) > 2:
            for _ in range(6):
                smoothed = list(world)
                for k in range(1, len(world) - 1):
                    z = (world[k - 1][2] + world[k][2] * 2.0 + world[k + 1][2]) / 4.0
                    smoothed[k] = (world[k][0], world[k][1], z)
                world = smoothed

            # Averaging alone still leaves 40% where a route crosses a mesa
            # escarpment: smoothing spreads a cliff out, it does not flatten
            # one. Walk the profile in both directions and cap each step, which
            # bounds the grade outright instead of hoping.
            for pass_dir in (1, -1):
                order = range(1, len(world)) if pass_dir > 0 \
                    else range(len(world) - 2, -1, -1)
                prev = 0 if pass_dir > 0 else len(world) - 1
                for k in order:
                    px, py, pz = world[prev]
                    cx, cy, cz = world[k]
                    run = math.hypot(cx - px, cy - py)
                    limit = run * MAX_GRADE
                    if cz - pz > limit:
                        world[k] = (cx, cy, pz + limit)
                    elif pz - cz > limit:
                        world[k] = (cx, cy, pz - limit)
                    prev = k

        for i in range(len(world) - 1):
            ax, ay, az = world[i]
            bx, by, bz = world[i + 1]
            dx, dy, dz = bx - ax, by - ay, bz - az
            length = math.sqrt(dx * dx + dy * dy + dz * dz)
            if length < 1.0:
                continue
            yaw = math.degrees(math.atan2(dy, dx))
            pitch = math.degrees(math.asin(max(-1.0, min(1.0, dz / length))))

            actor = sub.spawn_actor_from_class(
                unreal.StaticMeshActor,
                unreal.Vector((ax + bx) / 2.0, (ay + by) / 2.0, (az + bz) / 2.0),
                unreal.Rotator(0.0, pitch, yaw))
            actor.set_actor_label("{}_{}_{}".format(ROAD_TAG, name, i))
            comp = actor.static_mesh_component
            comp.set_static_mesh(mesh)
            if mat:
                comp.set_material(0, mat)
            # The engine cube is 100 uu on a side.
            actor.set_actor_scale3d(unreal.Vector(
                (length + 200.0) / 100.0,       # overlap so joints do not gap
                (width_m * 100.0) / 100.0,
                0.6))
            placed += 1

    log("laid {} road pieces across {} freeways".format(placed, len(FREEWAYS)))
    log("I-5 down the coast, I-8 through Mission Valley, SR-75 over the bay to")
    log("Coronado at 62 m. Save with Ctrl+S, then: ... build_sandiego.py look downtown")
    return True


# -------------------------------------------------------------------- city --
#
# The city fabric — streets and buildings — generated in the callofbooty repo by
# tools/export-city.mjs and read here. Nothing about the layout is decided in
# this file; the browser build and this one have to agree on where every
# building stands, and they only do that if one of them is the author and the
# other is the reader.
#
# Everything goes into instanced static meshes. There are on the order of
# 150,000 buildings: as StaticMeshActors that is 150,000 entries in the outliner
# and an editor that will not move. As instances on a handful of hierarchical
# components it is a few dozen actors and the renderer batches the rest.

CITY_TAG = "SanDiegoCity"

# How far each kind is worth drawing, in metres. Nothing streams in a
# non-partitioned level, so every one of 745,926 instances is resident and
# considered every frame unless it is told not to be. HISM culls per instance,
# which makes this the cheapest large win available here: a kerb two kilometres
# away costs nothing if it is never submitted.
#
# The numbers are what the thing is actually legible at. Lane paint stops
# reading at a few hundred metres; a tower is the skyline and has to draw from
# anywhere on the map. Anything absent from this list draws everywhere and is
# named in the log rather than silently given a default -- the same rule the
# rest of this pipeline follows.
CULL_M = {
    "building": 0, "pad": 6000, "water": 0, "pier": 4000,
    "road_deck": 4000, "kerb": 900, "line_white": 700, "line_yellow": 700,
    "path": 700, "sign": 450, "sign_post": 450,
    "lamp": 1200, "lamp_post": 1200,
    "tree": 3000, "tree_trunk": 3000, "shrub": 700, "rock": 700,
    "runway": 8000, "taxiway": 8000,
    "runway_centreline": 1800, "runway_threshold": 1800, "runway_light": 1200,
}


def cull():
    """Apply per-kind draw distances to the city HISMs, without rebuilding.

    Separate from `city` on purpose: `city` takes long enough that nobody
    should have to re-run it to change a draw distance.
    """
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    prefix = CITY_TAG + "_Buildings_"
    done = 0
    missing = []
    for a in sub.get_all_level_actors():
        try:
            label = a.get_actor_label()
            if not label.startswith(prefix):
                continue
            kind = label[len(prefix):]
            end_m = CULL_M.get(kind)
            if end_m is None:
                missing.append(kind)
                continue
            for comp in a.get_components_by_class(
                    unreal.HierarchicalInstancedStaticMeshComponent):
                if end_m <= 0:
                    comp.set_editor_property("instance_start_cull_distance", 0)
                    comp.set_editor_property("instance_end_cull_distance", 0)
                else:
                    comp.set_editor_property(
                        "instance_start_cull_distance", int(end_m * 100 * 0.75))
                    comp.set_editor_property(
                        "instance_end_cull_distance", int(end_m * 100))
                done += 1
            log("  {:<18} {}".format(
                kind, "always drawn" if end_m <= 0 else "{} m".format(end_m)))
        except Exception:                                        # noqa: BLE001
            continue
    if missing:
        warn("no cull distance for {} — those draw everywhere. Add them to "
             "CULL_M.".format(", ".join(sorted(set(missing)))))
    log("set draw distances on {} component(s)".format(done))
    if done:
        log("Save with Ctrl+S.")
    return done
CITY_JSON = "city.json"
CITY_BIN = "city-buildings.bin"

# Street centrelines are walked at this spacing so they follow the ground.
# Coarser than the freeways because a residential street has no grade standard
# to meet — it goes where the hill goes.
STREET_STEP_M = 34.0

# How far a building is sunk into the ground. Footprints sit on sloping lots and
# a box placed exactly on the sampled height at its centre floats at one corner
# and buries at the other; dropping it a little turns "floating" into "cut into
# the slope", which is what a real foundation does anyway.
BUILDING_SINK_M = 1.8

# Base colour and roughness per building kind. Deliberately flat and desaturated
# — this is massing, not architecture, and saturated boxes read as toys.
CITY_PALETTE = {
    "house":      ((0.402, 0.360, 0.302), 0.88),
    "rowhouse":   ((0.436, 0.386, 0.332), 0.86),
    "midrise":    ((0.352, 0.340, 0.322), 0.80),
    "tower":      ((0.240, 0.256, 0.278), 0.42),
    "commercial": ((0.372, 0.360, 0.340), 0.76),
    "industrial": ((0.330, 0.334, 0.336), 0.82),
    "military":   ((0.288, 0.300, 0.276), 0.84),
    "campus":     ((0.360, 0.350, 0.330), 0.80),
    "park":       ((0.318, 0.330, 0.300), 0.86),
    "parking":    ((0.088, 0.086, 0.090), 0.72),
    "runway":     ((0.318, 0.316, 0.308), 0.66),
    # The road kit. Asphalt is nearly black; the paint has to be bright enough
    # to read at speed, which is the whole reason it is that colour in reality.
    "building":   ((0.402, 0.386, 0.358), 0.86),
    "road_deck":  ((0.052, 0.051, 0.055), 0.78),
    "line_white": ((0.880, 0.880, 0.860), 0.55),
    "line_yellow":((0.880, 0.680, 0.130), 0.55),
    "kerb":       ((0.560, 0.556, 0.540), 0.80),
    # Decomposed granite, which is what San Diego's park and canyon paths are.
    "path":       ((0.512, 0.470, 0.398), 0.94),
    # Footprints the capture gives no height for. Concrete, so they read as a
    # slab rather than as a building that failed to grow.
    "pad":        ((0.430, 0.424, 0.412), 0.84),
    "pier":       ((0.520, 0.514, 0.500), 0.86),
    # Signs had no entry at all, so 2,520 assemblies were coming out in the
    # default grey. US guide blades are green; the posts are galvanised.
    "sign":       ((0.055, 0.235, 0.145), 0.62),
    "sign_post":  ((0.550, 0.560, 0.570), 0.70),
    "lamp_post":  ((0.470, 0.478, 0.486), 0.62),
    "lamp":       ((0.780, 0.760, 0.700), 0.40),
    # The airfield kit. `runway` had an entry from the old plan and the four
    # pieces laid on top of it did not, so 741 parts imported as default grey —
    # markings and edge lights the same colour as the asphalt they sit on.
    # Runway paint is whiter and flatter than road paint; edge lights are amber
    # glass; taxiway asphalt is a shade lighter than the strip it serves.
    "runway_centreline": ((0.900, 0.900, 0.880), 0.50),
    "runway_threshold":  ((0.920, 0.920, 0.900), 0.50),
    "runway_light":      ((0.760, 0.700, 0.320), 0.30),
    "taxiway":           ((0.105, 0.102, 0.098), 0.76),
    "tree":       ((0.118, 0.170, 0.086), 0.92),
    "tree_trunk": ((0.128, 0.104, 0.078), 0.94),
    "palm":       ((0.150, 0.196, 0.104), 0.90),
    "shrub":      ((0.176, 0.190, 0.116), 0.94),
    "rock":       ((0.310, 0.288, 0.252), 0.88),
    "water":      ((0.036, 0.114, 0.130), 0.08),
    # Named commercial uses. Different enough from each other to read as a
    # strip rather than as one long shed: the filling-station canopy is near
    # white, the supermarket beige, the cinema almost black.
    "gas":        ((0.640, 0.628, 0.596), 0.60),
    "restaurant": ((0.404, 0.296, 0.244), 0.82),
    "grocery":    ((0.470, 0.436, 0.372), 0.80),
    "pharmacy":   ((0.520, 0.512, 0.492), 0.78),
    "strip":      ((0.416, 0.386, 0.336), 0.82),
    "theatre":    ((0.148, 0.142, 0.152), 0.74),
    "motel":      ((0.512, 0.468, 0.396), 0.84),
    "hotel":      ((0.362, 0.372, 0.386), 0.62),
    "office":     ((0.268, 0.290, 0.316), 0.50),
    "bank":       ((0.470, 0.458, 0.428), 0.72),
}


def _city_paths():
    """Where the export lands. Beside this script first, as with the heightmap:
    an engine-upgrade copy of the project carries a stale one."""
    return (os.path.join(HEIGHTMAP_DIR, CITY_JSON),
            os.path.join(HEIGHTMAP_DIR, CITY_BIN))


def _load_city():
    """The city.json sidecar, or None with a reason logged."""
    json_path, bin_path = _city_paths()
    if not os.path.exists(json_path):
        warn("MISSING city plan. Expected: {}".format(json_path))
        warn("Generate it in the callofbooty repo:  node tools/export-city.mjs")
        warn("then copy out/city.json and out/city-buildings.bin into Tools/Heightmaps.")
        return None
    with open(json_path, "r") as handle:
        city = json.load(handle)
    city["_json_path"] = json_path
    city["_bin_path"] = bin_path if os.path.exists(bin_path) else None
    if city["_bin_path"]:
        stride = int(city.get("buildingStride", 9))
        expect = int(city.get("buildingCount", 0)) * stride * 4
        actual = os.path.getsize(bin_path)
        if actual != expect:
            warn("{} is {} bytes, expected {} — the plan and the buffer are "
                 "from different exports. Re-copy both.".format(
                     CITY_BIN, actual, expect))
            city["_bin_path"] = None
    else:
        warn("no {} beside {} — streets will build, buildings will not".format(
            CITY_BIN, CITY_JSON))
    return city


_HEIGHTS = {}


def _height_field(meta):
    """The whole .r16 in memory, as unsigned shorts.

    `_sample_metres` opens the file per sample. That is fine for a few thousand
    freeway points and ruinous for 150,000 buildings — it is 150,000 opens, and
    the command never finishes. The file is 32 MB; hold it.
    """
    key = meta["_raw_path"]
    if key in _HEIGHTS:
        return _HEIGHTS[key]
    import array
    data = array.array("H")
    with open(key, "rb") as fh:
        data.fromfile(fh, meta["resolution"] * meta["resolution"])
    if sys.byteorder != "little":
        data.byteswap()
    _HEIGHTS[key] = data
    return data


def _fast_metres(meta, field, col, row):
    """One height out of the cached field, in metres above sea level."""
    res = meta["resolution"]
    if col < 0:
        col = 0
    elif col >= res:
        col = res - 1
    if row < 0:
        row = 0
    elif row >= res:
        row = res - 1
    lo = meta["heightRangeMetres"]["min"]
    hi = meta["heightRangeMetres"]["max"]
    return lo + (field[row * res + col] / 65535.0) * (hi - lo)


def _level_origin(meta):
    """Where the landscape's minimum corner actually sits, in world cm.

    The import dialog's Location is a centre and the finished actor's Location
    is a corner, so the only trustworthy answer comes from measuring the
    landscape that is in the level rather than from recomputing what it should
    have been.
    """
    res = meta["resolution"]
    span_uu = (res - 1) * meta["unrealLandscapeScale"]["x"]
    x0 = y0 = -span_uu / 2.0
    groups = [g for g in _landscape_groups()
              if abs(g["res"] - meta["resolution"]) <= max(2, meta["resolution"] * 0.02)]
    if groups:
        x0, y0 = groups[0]["minCorner"]
    else:
        found = _find_landscapes()
        if found:
            loc = found[0].get_actor_location()
            x0, y0 = loc.x, loc.y
        else:
            warn("no landscape found — placing the city about the world origin")
    return x0, y0, span_uu


# The engine's basic shapes, by what the thing actually is. A cube makes a
# passable building and a very poor tree — a sphere for a crown and a cylinder
# for a trunk cost the same instance and read correctly from any distance. Swap
# any of these for real assets and nothing else has to change.
CITY_MESH = {
    "tree":       "/Engine/BasicShapes/Sphere",
    "palm":       "/Engine/BasicShapes/Sphere",
    "shrub":      "/Engine/BasicShapes/Sphere",
    "rock":       "/Engine/BasicShapes/Sphere",
    "tree_trunk": "/Engine/BasicShapes/Cylinder",
    "lamp_post":  "/Engine/BasicShapes/Cylinder",
}
CITY_MESH_DEFAULT = "/Engine/BasicShapes/Cube"


def _mesh_for(kind):
    path = CITY_MESH.get(kind, CITY_MESH_DEFAULT)
    mesh = unreal.EditorAssetLibrary.load_asset(path)
    if mesh:
        return mesh
    warn("{} is missing — falling back to the cube".format(path))
    return unreal.EditorAssetLibrary.load_asset(CITY_MESH_DEFAULT)


def _city_material(kind):
    """A flat colour per building kind, built once and reused."""
    path = "/Game/Materials/M_City_{}".format(kind)
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        return unreal.EditorAssetLibrary.load_asset(path)
    rgb, rough = CITY_PALETTE.get(kind, ((0.36, 0.35, 0.33), 0.85))
    try:
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        folder, name = path.rsplit("/", 1)
        mat = tools.create_asset(name, folder, unreal.Material,
                                 unreal.MaterialFactoryNew())
        col = _expr(mat, "MaterialExpressionVectorParameter", -420, 0)
        col.set_editor_property("parameter_name", "Base")
        col.set_editor_property("default_value",
                                unreal.LinearColor(rgb[0], rgb[1], rgb[2], 1.0))
        unreal.MaterialEditingLibrary.connect_material_property(
            col, "", unreal.MaterialProperty.MP_BASE_COLOR)
        rn = _expr(mat, "MaterialExpressionScalarParameter", -420, 200)
        rn.set_editor_property("parameter_name", "Roughness")
        rn.set_editor_property("default_value", rough)
        unreal.MaterialEditingLibrary.connect_material_property(
            rn, "", unreal.MaterialProperty.MP_ROUGHNESS)
        if kind == "tower":
            # Glass towers are the one thing that reads wrong as pure diffuse:
            # a downtown of matte grey boxes looks like a model, not a skyline.
            sp = _expr(mat, "MaterialExpressionScalarParameter", -420, 320)
            sp.set_editor_property("parameter_name", "Metallic")
            sp.set_editor_property("default_value", 0.55)
            unreal.MaterialEditingLibrary.connect_material_property(
                sp, "", unreal.MaterialProperty.MP_METALLIC)
        unreal.MaterialEditingLibrary.recompile_material(mat)
        unreal.EditorAssetLibrary.save_asset(path)
        return mat
    except Exception as exc:                                     # noqa: BLE001
        warn("could not build {} ({})".format(path, exc))
        return None


def _add_ism_component(actor, name):
    """Attach a hierarchical instanced static mesh component that survives save.

    Three routes, tried in order, because which ones exist moves between engine
    versions and a silent failure here looks exactly like "the city did not
    generate". The subobject subsystem is the sanctioned UE5 path; the other two
    are there so a version mismatch degrades instead of stopping.
    """
    cls = unreal.HierarchicalInstancedStaticMeshComponent

    try:
        subsys = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
        handles = subsys.k2_gather_subobject_data_for_instance(actor)
        if handles:
            params = unreal.AddNewSubobjectParams(
                parent_handle=handles[0], new_class=cls, blueprint_context=None)
            handle, fail = subsys.add_new_subobject(params)
            # `fail` is the reason text, empty when it worked.
            if hasattr(fail, "is_empty") and not fail.is_empty():
                raise RuntimeError(str(fail))
            subsys.rename_subobject(handle, name)
            data = subsys.k2_find_subobject_data_from_handle(handle)
            comp = unreal.SubobjectDataBlueprintFunctionLibrary.get_object(data)
            if comp:
                return comp
    except Exception as exc:                                     # noqa: BLE001
        warn("subobject route did not take ({})".format(exc))

    try:
        comp = actor.add_component_by_class(cls, False, unreal.Transform(), False)
        if comp:
            return comp
    except Exception as exc:                                     # noqa: BLE001
        warn("add_component_by_class did not take ({})".format(exc))

    warn("could not attach an instanced mesh component to {}".format(
        actor.get_actor_label()))
    return None


def _ism_holder(label, mesh, material):
    """An empty actor carrying one instanced mesh, ready to be filled."""
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor = sub.spawn_actor_from_class(
        unreal.Actor, unreal.Vector(0.0, 0.0, 0.0), unreal.Rotator())
    if not actor:
        return None, None
    actor.set_actor_label(label)
    comp = _add_ism_component(actor, "Instances")
    if not comp:
        sub.destroy_actor(actor)
        return None, None
    comp.set_static_mesh(mesh)
    if material:
        comp.set_material(0, material)
    try:
        comp.set_editor_property("mobility", unreal.ComponentMobility.STATIC)
    except Exception:                                            # noqa: BLE001
        pass
    return actor, comp


INSTANCE_CHUNK = 10000


def _add_instances(comp, transforms):
    """Batch if this build can, one at a time if it cannot.

    Chunked rather than handed over in one call. A single component here takes
    over a hundred thousand instances, and a batch that large gives the editor
    no way to say how far it has got — the difference between "working" and
    "hung" is a progress line. Chunking also means a failure part way through
    is a partial result that can be reported, not a total loss.
    """
    added = 0
    total = len(transforms)
    for start in range(0, total, INSTANCE_CHUNK):
        chunk = transforms[start:start + INSTANCE_CHUNK]
        try:
            comp.add_instances(chunk, False)
            added += len(chunk)
        except Exception:                                        # noqa: BLE001
            for t in chunk:
                try:
                    comp.add_instance(t)
                    added += 1
                except Exception:                                # noqa: BLE001
                    warn("instancing stopped after {} of {}".format(added, total))
                    return added
        if total > INSTANCE_CHUNK:
            log("    {} / {}".format(added, total))
    return added


def _clear_city(prefix):
    """Remove anything this command placed before, so it is safe to re-run."""
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    removed = 0
    for a in sub.get_all_level_actors():
        try:
            if a.get_actor_label().startswith(prefix):
                sub.destroy_actor(a)
                removed += 1
        except Exception:                                        # noqa: BLE001
            continue
    if removed:
        log("cleared {} existing {} actor(s)".format(removed, prefix))
    return removed


def city_buildings(limit="0"):
    """Place every building in the plan as an instance, grouped by kind."""
    meta = _meta_for_level()
    city = _load_city()
    if not meta or not city or not city["_bin_path"]:
        return False

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0

    mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
    if not mesh:
        warn("/Engine/BasicShapes/Cube is missing — cannot place buildings")
        return False

    import array
    stride = int(city.get("buildingStride", 9))
    count = int(city.get("buildingCount", 0))
    data = array.array("f")
    with open(city["_bin_path"], "rb") as fh:
        data.fromfile(fh, count * stride)
    if sys.byteorder != "little":
        data.byteswap()

    kinds = city.get("kinds", [])
    field = _height_field(meta)
    x0, y0, span_uu = _level_origin(meta)
    res = meta["resolution"]
    fm = meta["frameMetres"]
    band = float(fm["height"]) / float(fm["width"])
    v_off = (1.0 - band) / 2.0
    scale_x = meta["unrealLandscapeScale"]["x"]

    _clear_city(CITY_TAG + "_Buildings")
    # Placed by versions of this script that no longer exist, and removed by
    # nothing: the landmark boxes and the legacy street grid. They were built
    # against a landscape with a different scale and Z lift, so they do not even
    # land in the right place any more.
    for stale in (CITY_TAG + "_Streets", "SanDiegoLandmark"):
        _clear_city(stale)

    # Group by kind first, then fill one component per kind. Grouping keeps the
    # instance buffers homogeneous, which is the whole point: one draw call per
    # kind instead of one per building.
    n = count if limit <= 0 else min(count, limit)

    # One kind at a time, rather than every transform up front.
    #
    # The plan is 657,438 parts now. Building all of their unreal.Transform
    # objects before placing any of them means the peak is the whole plan
    # resident at once; doing it a kind at a time makes the peak the largest
    # single kind, which is 155,000. The cost is one extra scan of a packed
    # float array per kind, which is nothing next to losing an editor session.
    present = {}
    for i in range(n):
        k = kinds[int(data[i * stride + 6])] if kinds else "house"
        present[k] = present.get(k, 0) + 1
    log("{} parts across {} kinds: {}".format(
        n, len(present),
        ", ".join("{} {}".format(v, k) for k, v in sorted(present.items()))))

    # A kind with no palette entry is not an error, it is grey geometry and a
    # silent one — CITY_PALETTE.get falls back and says nothing. That is the
    # same family of bug that has cost this project six defects on the tools
    # side, so the last place it can still hide gets to complain out loud.
    no_colour = [k for k in sorted(present) if k not in CITY_PALETTE]
    if no_colour:
        warn("no palette entry for {} — {} parts will be the default grey"
             .format(", ".join(no_colour),
                     sum(present[k] for k in no_colour)))
        warn("Add them to CITY_PALETTE rather than letting them import as grey.")
    default_mesh = [k for k in sorted(present) if k not in CITY_MESH]
    if default_mesh:
        log("taking the cube for: {}".format(", ".join(default_mesh)))

    placed = 0
    culled = 0
    cleared = 0
    pitched = 0
    for want_kind in sorted(present):
        transforms = []
        for i in range(n):
            o = i * stride
            if (kinds[int(data[o + 6])] if kinds else "house") != want_kind:
                continue
            u = data[o]
            v = data[o + 1]
            rot = data[o + 2]
            w = data[o + 3]
            d = data[o + 4]
            h = data[o + 5]
            flags = int(data[o + 7]) if stride > 7 else 0
            base = data[o + 8] if stride > 8 else 0.0
            # Field 9, added when the format went to stride 10: how far the part
            # leans along its own length. Without it a road deck on a slope sits
            # flat and the next one starts higher -- a metre of step at this
            # city's 90th-percentile grade. A part's length runs along local X
            # and Unreal's positive pitch raises +X, so the sign carries over
            # from atan2(rise, run) unchanged.
            pitch = data[o + 9] if stride > 9 else 0.0
            if pitch != pitch:                 # NaN, from a producer one short
                pitch = 0.0
            on_water = bool(flags & 2)
            # Flag 4: built over water on purpose. Bridge piers stand on the bed of
            # a bay that is 9 m below the waterline, and the sea test below would
            # throw every one of them away — 3,330 parts, most of a bridge — while
            # reporting a perfectly healthy-looking count.
            structure = bool(flags & 4)
            # Flag 8: standing on runway or taxiway pavement. maps3d-airfields.mjs
            # flags rather than deletes these, because deleting one would shift
            # every index after it and the structure record addresses parts by
            # index. A building on a runway is wrong however it got there, and the
            # pavement is the only geometry on the map placed on purpose.
            if flags & 8:
                cleared += 1
                continue

            row_f = v_off + v * band
            col = int(round(u * (res - 1)))
            row = int(round(row_f * (res - 1)))
            ground = _fast_metres(meta, field, col, row)
            if on_water:
                # The Midway and the carrier alongside North Island are moored.
                # Their ground is the waterline, and rejecting them for standing
                # where the heightmap says sea would throw away the two most
                # recognisable objects on the bay.
                ground = 0.0
            elif ground is None:
                continue                       # off the edge of the heightmap
            elif ground < 0.6 and not structure:
                continue                       # the plan says land, the terrain says sea

            # A part stacked above its own base — a flight deck, a roof pavilion —
            # is not sunk into anything; only what stands on the ground is. And the
            # sink is capped at a quarter of the part's own height, because a fixed
            # 1.8 m buries anything shorter than that outright: a car park is a
            # 12 cm pad, and sinking it by the full amount put every acre of asphalt
            # on the map two metres underground.
            if abs(base) > 0.01 or on_water or structure:
                # Negative bases exist too: a lake surface is placed relative to
                # the bed under it, and sinking it would drop the water below the
                # level every other slab in the same lake is holding.
                sink = 0.0
            else:
                sink = min(BUILDING_SINK_M, h * 0.25)

            x = x0 + (u * span_uu)
            y = y0 + (row_f * span_uu)
            z = (ground + base - sink + h / 2.0) * 100.0

            if abs(pitch) > 0.01:
                pitched += 1
            transforms.append(unreal.Transform(
                unreal.Vector(x, y, z),
                unreal.Rotator(0.0, pitch, rot),
                unreal.Vector(w, d, h + sink)))

        dropped = present[want_kind] - len(transforms)
        culled += dropped
        if not transforms:
            warn("{}: all {} parts were culled".format(
                want_kind, present[want_kind]))
            continue
        label = "{}_Buildings_{}".format(CITY_TAG, want_kind)
        actor, comp = _ism_holder(label, _mesh_for(want_kind),
                                  _city_material(want_kind))
        if not comp:
            warn("skipped {} ({} parts)".format(want_kind, len(transforms)))
            continue
        added = _add_instances(comp, transforms)
        placed += added
        log("  {:<11} {:>7} instances{}".format(
            want_kind, added,
            "" if not dropped
            else "  ({} culled below the waterline)".format(dropped)))
        if added != len(transforms):
            warn("  {} of {} did not take".format(
                len(transforms) - added, len(transforms)))
        del transforms

    log("placed {} parts of {} in the plan, {} culled below the waterline"
        .format(placed, count, culled))
    if stride > 9:
        log("{} parts carry a pitch (stride {}); if the roads lean the wrong "
            "way up a hill, that is the sign convention and not the data"
            .format(pitched, stride))
    else:
        warn("this buffer is stride {} and carries no pitch — road decks will "
             "step up slopes instead of lying on them. Re-export.".format(stride))
    if cleared:
        log("{} parts skipped for standing on runway or taxiway pavement"
            .format(cleared))
    if placed == 0:
        warn("nothing was placed. If the subobject route failed above, this")
        warn("engine build cannot attach components from Python — say so rather")
        warn("than assuming the plan is wrong.")
        return False
    log("Save with Ctrl+S. Then: ... build_sandiego.py look downtown")
    return True


def city_streets():
    """Lay streets from the legacy plan arrays, if there are any.

    There are not, in the maps3d plan, and that is the point of this note. Road
    geometry now arrives through the packed buffer as road_deck, line_white,
    line_yellow and kerb parts, built by tools/maps3d-roadmesh.mjs with real
    widths, markings and junction boxes — 1,832 km of it. `city.json` carries
    `arterials: []` and `streets: []` because nothing writes them any more.

    So this command does nothing on the current plan, and the danger is that it
    LOOKS like the thing that lays the roads. It says so instead of silently
    placing zero and returning success.
    """
    meta = _meta_for_level()
    city = _load_city()
    if not meta or not city:
        return False

    # Clear BEFORE deciding there is nothing to do. This returned early on the
    # maps3d plan and so never reached its own _clear_city below, which meant
    # the street grid from the invented plan survived every single re-import --
    # thousands of instances of it, sitting on top of the real roads, and
    # nothing in any log mentioning them.
    _clear_city(CITY_TAG + "_Streets")

    legacy = len(city.get("arterials", [])) + len(city.get("streets", []))
    if not legacy:
        log("no streets in the plan's legacy arrays — nothing for this command "
            "to do.")
        log("Road geometry comes through the packed buffer now: run "
            "'city buildings', which places road_deck, line_white, "
            "line_yellow and kerb along with everything else.")
        return True

    mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
    if not mesh:
        warn("/Engine/BasicShapes/Cube is missing — cannot lay streets")
        return False

    field = _height_field(meta)
    x0, y0, span_uu = _level_origin(meta)
    res = meta["resolution"]
    fm = meta["frameMetres"]
    frame_w = float(fm["width"])
    band = float(fm["height"]) / frame_w
    v_off = (1.0 - band) / 2.0
    step_uv = STREET_STEP_M / frame_w

    _clear_city(CITY_TAG + "_Streets")

    def to_world(u, v):
        row_f = v_off + v * band
        col = int(round(u * (res - 1)))
        row = int(round(row_f * (res - 1)))
        ground = _fast_metres(meta, field, col, row)
        return (x0 + u * span_uu, y0 + row_f * span_uu, ground)

    # Arterials sit slightly higher than the residential streets they cross, so
    # a junction reads as the arterial running through rather than as two
    # surfaces fighting over the same z.
    groups = [("arterial", city.get("arterials", []), 0.26),
              ("street", city.get("streets", []), 0.18)]

    total = 0
    for group_name, items, lift in groups:
        transforms = []
        for item in items:
            width_m = float(item.get("w", 10))
            pts = item.get("pts", [])
            if len(pts) < 2:
                continue
            walk = _resample([(p[0], p[1]) for p in pts], step_uv)
            world = [to_world(u, v) for (u, v) in walk]
            for i in range(len(world) - 1):
                ax, ay, az = world[i]
                bx, by, bz = world[i + 1]
                if az is None or bz is None:
                    continue
                if az < 0.4 and bz < 0.4:
                    continue                      # the run crossed water
                dx, dy = bx - ax, by - ay
                dz = (bz - az) * 100.0
                run = math.sqrt(dx * dx + dy * dy)
                length = math.sqrt(run * run + dz * dz)
                if length < 1.0:
                    continue
                yaw = math.degrees(math.atan2(dy, dx))
                pitch = math.degrees(math.asin(max(-1.0, min(1.0, dz / length))))
                cz = ((az + bz) / 2.0 + lift) * 100.0
                transforms.append(unreal.Transform(
                    unreal.Vector((ax + bx) / 2.0, (ay + by) / 2.0, cz),
                    unreal.Rotator(0.0, pitch, yaw),
                    # Overlap along the run so joints do not gap on a curve.
                    unreal.Vector((length + 120.0) / 100.0, width_m, 0.5)))

        if not transforms:
            continue
        label = "{}_Streets_{}".format(CITY_TAG, group_name)
        actor, comp = _ism_holder(label, mesh, _road_material())
        if not comp:
            warn("skipped {} ({} segments)".format(group_name, len(transforms)))
            continue
        added = _add_instances(comp, transforms)
        total += added
        log("  {:<9} {:>7} segments".format(group_name, added))

    log("laid {} street segments".format(total))
    if total == 0:
        warn("nothing was laid — see the component warnings above")
        return False
    log("Save with Ctrl+S.")
    return True


def city(what="all", limit="0"):
    """Streets then buildings. `city streets`, `city buildings [limit]`, `city`."""
    what = (what or "all").strip().lower()
    ok_streets = ok_buildings = True
    if what in ("all", "streets"):
        ok_streets = city_streets()
    if what in ("all", "buildings"):
        ok_buildings = city_buildings(limit)
    return ok_streets and ok_buildings


def city_report():
    """What the plan contains, without touching the level."""
    city_plan = _load_city()
    if not city_plan:
        return False
    log("plan: {}".format(city_plan["_json_path"]))
    log("frame {} x {} m".format(city_plan["frameMetres"]["width"],
                                 city_plan["frameMetres"]["height"]))
    log("{} districts, {} arterials, {} streets, {} buildings".format(
        len(city_plan.get("districts", [])),
        len(city_plan.get("arterials", [])),
        len(city_plan.get("streets", [])),
        city_plan.get("buildingCount", 0)))
    log("kinds: {}".format(", ".join(city_plan.get("kinds", []))))
    log("")
    for s in city_plan.get("stats", []):
        log("  {:<20} {:>5} streets {:>7} buildings".format(
            s.get("id", "?"), s.get("streets", 0), s.get("buildings", 0)))
    return True


# ----------------------------------------------------------------- interiors --
#
# The first piece of the architecture in docs/07-map-architecture.md that
# actually exists in the engine: a building that is a shell you can walk into
# rather than a solid cube.
#
# Deliberately bounded by a radius. Tier C alone is 6.10 M instances across the
# map and this level does not stream, so "all of them" is not a thing to ask for
# yet. A radius makes it a thing you can stand in tonight and measure.
#
# This is a PROTOTYPE of the layout, not the reference implementation. The
# reference is tools/interior-c.mjs in the callofbooty repo, and the eventual
# C++ has to match THAT index for index because element indices are what damage
# state is addressed by. What this proves is the shape: read the record, hollow
# the box, put floors and walls and a door in it, walk in.

INTERIOR_TAG = "SanDiegoInterior"
WALL_T = 20.0            # wall thickness, cm
FLOOR_T = 20.0
DOOR_W = 110.0
DOOR_H = 220.0


def _load_structures():
    """city-structures.bin as a list of dicts, or None with a reason."""
    city = _load_city()
    if not city:
        return None, None
    meta = city.get("structures")
    if not meta:
        warn("city.json has no structures block — re-export from the callofbooty "
             "repo (maps3d-city.mjs then maps3d-doors.mjs).")
        return None, None
    path = os.path.join(HEIGHTMAP_DIR, meta.get("file", "city-structures.bin"))
    if not os.path.exists(path):
        warn("MISSING {}".format(path))
        return None, None
    stride = int(meta["stride"])
    count = int(meta["count"])
    expect = stride * count * 4
    actual = os.path.getsize(path)
    if actual != expect:
        warn("{} is {} bytes, expected {} — plan and record are from different "
             "exports".format(os.path.basename(path), actual, expect))
        return None, None
    import struct as _struct
    with open(path, "rb") as fh:
        raw = fh.read()
    fields = meta["fields"]
    idx = {name: i for i, name in enumerate(fields)}
    out = []
    for i in range(count):
        vals = _struct.unpack_from("<{}f".format(stride), raw, i * stride * 4)
        out.append(vals)
    return {"meta": meta, "idx": idx, "rows": out}, city


def interiors(radius_m="600", centre="downtown"):
    """Hollow out every building within `radius_m` and give it floors and a door.

    Removes those buildings' solid instances from the city HISM and replaces
    them with a shell: four walls with a doorway in the street-facing one, a
    slab per storey, and a stair opening. What you get is a building you can
    walk into and climb.
    """
    try:
        radius = float(radius_m) * 100.0
    except (TypeError, ValueError):
        warn("radius must be a number of metres")
        return False

    data, city = _load_structures()
    if not data:
        return False
    idx, rows = data["idx"], data["rows"]
    meta = _meta_for_level()
    if not meta:
        return False

    span_uu = (meta["resolution"] - 1) * meta["unrealLandscapeScale"]["x"]
    half = span_uu / 2.0

    # PLACES is a list of (name, u, v), not a dict. Looked up as a dict this
    # silently fell through to the origin every time and the radius searched the
    # middle of the bay.
    spot = next((p for p in PLACES if p[0] == str(centre).lower()), None)
    if spot:
        cx = spot[1] * span_uu - half
        cy = spot[2] * span_uu - half
    else:
        warn("no place called '{}' — searching from the origin. Known: {}"
             .format(centre, ", ".join(p[0] for p in PLACES)))
        cx = cy = 0.0

    def world(u, v):
        return (u * span_uu - half, v * span_uu - half)

    cleared = _clear_city(INTERIOR_TAG)

    picked = []
    for r in rows:
        if int(r[idx["flags"]]) & 8:
            continue                       # cleared for airfield pavement
        st = int(round(r[idx["storeys"]]))
        if st < 1:
            continue                       # a pad has no inside
        x, y = world(r[idx["u"]], r[idx["v"]])
        if (x - cx) ** 2 + (y - cy) ** 2 > radius * radius:
            continue
        picked.append((r, x, y))

    if not picked:
        warn("no buildings within {} m of {}".format(radius_m, centre))
        return False
    log("hollowing {} buildings within {} m of {}".format(
        len(picked), radius_m, centre))

    mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
    if not mesh:
        warn("/Engine/BasicShapes/Cube is missing")
        return False

    parts = {"wall": [], "floor": [], "partition": []}
    for r, x, y in picked:
        w = r[idx["widthM"]] * 100.0
        d = r[idx["depthM"]] * 100.0
        st = int(round(r[idx["storeys"]]))
        fh = r[idx["floorHeightM"]] * 100.0 or 300.0
        rot = r[idx["rotDeg"]]
        base = r[idx["groundMaxM"]] * 100.0 - 15.0
        side = int(round(r[idx["doorSide"]]))

        rr = unreal.Rotator(0.0, 0.0, rot)

        def place(bucket, lx, ly, lz, sx, sy, sz):
            """Local offset in the footprint's own frame -> world transform."""
            th = math.radians(rot)
            wx = x + lx * math.cos(th) - ly * math.sin(th)
            wy = y + lx * math.sin(th) + ly * math.cos(th)
            parts[bucket].append(unreal.Transform(
                unreal.Vector(wx, wy, base + lz),
                rr,
                unreal.Vector(sx / 100.0, sy / 100.0, sz / 100.0)))

        for k in range(st + 1):
            place("floor", 0.0, 0.0, k * fh, w, d, FLOOR_T)

        # Four walls, full height, with the street-facing one split round a door.
        wall_h = st * fh
        for s in range(4):
            if s in (0, 2):
                lx = (w / 2.0 - WALL_T / 2.0) * (1 if s == 0 else -1)
                if s == side:
                    seg = (d - DOOR_W) / 2.0
                    for sgn in (-1, 1):
                        place("wall", lx, sgn * (DOOR_W / 2.0 + seg / 2.0),
                              wall_h / 2.0, WALL_T, seg, wall_h)
                    place("wall", lx, 0.0, DOOR_H + (wall_h - DOOR_H) / 2.0,
                          WALL_T, DOOR_W, wall_h - DOOR_H)
                else:
                    place("wall", lx, 0.0, wall_h / 2.0, WALL_T, d, wall_h)
            else:
                ly = (d / 2.0 - WALL_T / 2.0) * (1 if s == 1 else -1)
                if s == side:
                    seg = (w - DOOR_W) / 2.0
                    for sgn in (-1, 1):
                        place("wall", sgn * (DOOR_W / 2.0 + seg / 2.0), ly,
                              wall_h / 2.0, seg, WALL_T, wall_h)
                    place("wall", 0.0, ly, DOOR_H + (wall_h - DOOR_H) / 2.0,
                          DOOR_W, WALL_T, wall_h - DOOR_H)
                else:
                    place("wall", 0.0, ly, wall_h / 2.0, w, WALL_T, wall_h)

        # One cross partition a floor, so it is rooms rather than a shoebox.
        for k in range(st):
            place("partition", 0.0, 0.0, k * fh + fh / 2.0,
                  WALL_T, d * 0.55, fh - FLOOR_T)

    total = 0
    for kind, transforms in parts.items():
        if not transforms:
            continue
        # _ism_holder returns (actor, component), not a component.
        actor, comp = _ism_holder("{}_{}".format(INTERIOR_TAG, kind), mesh,
                                  _city_material("building"))
        if not comp:
            continue
        total += _add_instances(comp, transforms)
        log("  {:<10} {} instances".format(kind, len(transforms)))

    log("{} interior parts across {} buildings".format(total, len(picked)))
    log("The solid city boxes are still in place around these, so the shells sit "
        "inside them. Hide SanDiegoCity_Buildings_building in the outliner to "
        "walk in -- removing those instances properly is the next step.")
    log("Save with Ctrl+S.")
    return True


def patrol(shots_file=None, out_dir=None):
    """Fly the viewport through a list of waypoints and screenshot each one.

    The reason this exists: tools/flyover.mjs can render the shipped bytes from
    any camera, but it renders the DATA. Materials, lighting, LOD popping, HISM
    cull distances and collision belong to the engine, and nothing outside the
    engine can photograph them. So this is the other half — the editor's own
    view, from the same waypoints, saved as files that can be looked at
    together.

        py "Tools/build_sandiego.py" patrol
        py "Tools/build_sandiego.py" patrol Tools/patrol.json Saved/Patrol

    The waypoint file is the same shape flyover.mjs takes, so one list drives
    both: [{"name","u","v","eye","look","tilt"}, ...]. Without one, it walks a
    default circuit of the places defects have actually turned up in.

    A screenshot is asynchronous — the request is queued and served on a later
    frame — so a plain for-loop produces one image, or none, and reports
    success. This drives a state machine off the editor's own tick instead, one
    waypoint at a time, and only advances once the file has appeared on disk.
    """
    meta = load_meta()
    if not meta:
        return False

    root = unreal.Paths.project_dir()
    out = out_dir or os.path.join(root, "Saved", "Patrol")
    out = os.path.abspath(out)
    if not os.path.isdir(out):
        os.makedirs(out)

    shots = None
    if shots_file:
        path = shots_file if os.path.isabs(shots_file) else os.path.join(root, shots_file)
        try:
            with open(path, "r") as fh:
                shots = json.load(fh)
        except Exception as exc:
            warn("could not read {} ({}) — using the default circuit".format(path, exc))
    if not shots:
        # Every one of these is a place a defect has actually turned up in, and
        # the two airfields are first because their geometry was missing from a
        # shipped buffer for a week.
        shots = [
            {"name": "ksan",        "u": 0.5196, "v": 0.3626, "eye": 260, "look": 100, "tilt": -22},
            {"name": "ksan-low",    "u": 0.5196, "v": 0.3626, "eye": 12,  "look": 100, "tilt": -2},
            {"name": "northisland", "u": 0.3932, "v": 0.5737, "eye": 300, "look": 121, "tilt": -24},
            {"name": "downtown",    "u": 0.6755, "v": 0.4716, "eye": 140, "look": 210, "tilt": -14},
            {"name": "pointloma",   "u": 0.2373, "v": 0.7514, "eye": 40,  "look": 20,  "tilt": -8},
            {"name": "mcrd",        "u": 0.4744, "v": 0.3152, "eye": 120, "look": 90,  "tilt": -18},
            {"name": "coronado",    "u": 0.7523, "v": 0.6273, "eye": 90,  "look": 300, "tilt": -10},
            {"name": "zoo",         "u": 0.7414, "v": 0.3455, "eye": 80,  "look": 180, "tilt": -12},
        ]

    res_x = int(os.environ.get("PATROL_W", "1920"))
    res_y = int(os.environ.get("PATROL_H", "1080"))

    try:
        ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    except Exception as exc:
        warn("no UnrealEditorSubsystem ({}) — cannot drive the viewport".format(exc))
        return False

    state = {"i": 0, "waited": 0, "shot": False, "done": [], "handle": None}

    def finish():
        if state["handle"] is not None:
            unreal.unregister_slate_post_tick_callback(state["handle"])
            state["handle"] = None
        log("{} of {} frames written to {}".format(
            len(state["done"]), len(shots), out))
        for name in state["done"]:
            log("  {}".format(name))
        if len(state["done"]) < len(shots):
            warn("some frames never appeared. The viewport must be visible and "
                 "not minimised for a screenshot to be served.")
        log("Nothing here changed the level, so there is nothing to save.")

    def tick(_delta):
        i = state["i"]
        if i >= len(shots):
            finish()
            return
        s = shots[i]
        name = s.get("name", "shot{}".format(i))
        path = os.path.join(out, "{:02d}-{}.png".format(i, name))

        if not state["shot"]:
            # Place the camera. `eye` is metres above the ground under the
            # waypoint, not an absolute height, so a shot list reads the same
            # way whether it is over the bay or over Point Loma.
            p = uv_to_world(meta, float(s["u"]), float(s["v"]))
            ground = _sample_metres(meta, p["col"], p["row"]) or 0.0
            z = (ground + float(s.get("eye", 60))) * 100.0
            # Unreal yaw is measured the same way maps3d writes a heading:
            # atan2(dy, dx) with +y running down the image, so a bearing here
            # matches a bearing in flyover.mjs and the two views line up.
            rot = unreal.Rotator(0.0, float(s.get("tilt", -12)), float(s.get("look", 0)))
            ues.set_level_viewport_camera_info(unreal.Vector(p["x"], p["y"], z), rot)
            if os.path.exists(path):
                os.remove(path)
            unreal.AutomationLibrary.take_high_res_screenshot(res_x, res_y, path)
            state["shot"] = True
            state["waited"] = 0
            return

        # Wait for the file. The request is served on a later frame, and how
        # many depends on what the renderer is doing, so this watches for the
        # artefact rather than counting frames and hoping.
        state["waited"] += 1
        if os.path.exists(path) and os.path.getsize(path) > 0:
            state["done"].append(os.path.basename(path))
            state["i"] += 1
            state["shot"] = False
        elif state["waited"] > 600:              # ~10 s at 60 fps
            warn("{} never appeared — skipping".format(os.path.basename(path)))
            state["i"] += 1
            state["shot"] = False

    log("patrolling {} waypoints at {}x{} into {}".format(
        len(shots), res_x, res_y, out))
    log("Leave the editor alone and visible until it reports done — a "
        "screenshot cannot be served to a minimised viewport.")
    state["handle"] = unreal.register_slate_post_tick_callback(tick)
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
    "overview": overview,
    "water": water,
    "bounds": bounds,
    "material": material,
    "roads": roads,
    "clear-roads": clear_roads,
    "actors": actors,
    "cull": cull,
    "interiors": interiors,
    "drop": drop,
    "city": city,
    "city-report": city_report,
    "sample": sample,
    "load": load_all,
    "patrol": patrol,
    "wp-api": wp_api,
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
