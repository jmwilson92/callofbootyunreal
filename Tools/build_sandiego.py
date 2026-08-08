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
    sample              compare the level against the file, per tile
    load                load every World Partition actor first
    wp-api              list the World Partition bindings this build has
    templates           list the level templates this engine ships
"""

import json
import os

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
        proj_hm = os.path.normpath(os.path.join(proj, "Tools", "Heightmaps"))
        if os.path.normpath(HEIGHTMAP_DIR) != proj_hm:
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
            if extent.x <= 1.0:                 # the geometry-less parent
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
        warn("no landscape found — putting the sea on the origin")

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
    "sample": sample,
    "load": load_all,
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
