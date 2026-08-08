"""
Build the San Diego level from data, inside the Unreal editor.

Run from the editor's Python console, or:
    Window -> Developer Tools -> Output Log, switch the dropdown to Python, then
    exec(open(r'<project>/Tools/build_sandiego.py').read())

What this does and why it is a script rather than clicks: the geography is
traced data (see the callofbooty repo, src/world/geo/SanDiegoGeo.js), and the
whole point of tracing it was that it can be rebuilt at any scale, any number of
times, without redoing it by hand. Anything that has to survive a re-import
belongs here rather than in someone's memory of which buttons they pressed.

Stage 1 only, for now: report the environment and check the heightmap is where
it should be. Landscape import follows once the engine version is confirmed —
the landscape API moved between 5.3, 5.4 and 5.5 and guessing produces scripts
that fail halfway through with a half-built level.
"""

import json
import os

import unreal


HEIGHTMAP_DIR = os.path.join(
    unreal.Paths.project_dir(), "Tools", "Heightmaps"
)


def log(msg):
    unreal.log("[sandiego] {}".format(msg))


def report_environment():
    """Everything a later stage needs to branch on, printed once."""
    log("engine version : {}".format(unreal.SystemLibrary.get_engine_version()))
    log("project dir    : {}".format(unreal.Paths.project_dir()))
    log("project name   : {}".format(unreal.Paths.get_base_filename(
        unreal.Paths.get_project_file_path())))

    # World Partition is what makes a 17.6 km map viable at all, so its state
    # decides whether we add a new open world map or convert an existing one.
    world = unreal.EditorLevelLibrary.get_editor_world()
    log("current level  : {}".format(world.get_name() if world else "<none>"))
    try:
        wp = world.get_world_partition() if world else None
        log("world partition: {}".format("ENABLED" if wp else "not enabled"))
    except Exception as exc:                                 # noqa: BLE001
        log("world partition: could not query ({})".format(exc))


def check_heightmap():
    """Confirm the traced geography arrived intact and report its import scale."""
    meta_path = os.path.join(HEIGHTMAP_DIR, "sandiego.json")
    raw_path = os.path.join(HEIGHTMAP_DIR, "sandiego.r16")

    if not os.path.exists(meta_path) or not os.path.exists(raw_path):
        log("MISSING heightmap. Expected: {}".format(raw_path))
        log("Git LFS may not have fetched it — run: git lfs pull")
        return None

    with open(meta_path, "r") as handle:
        meta = json.load(handle)

    res = meta["resolution"]
    expected_bytes = res * res * 2                # 16-bit
    actual_bytes = os.path.getsize(raw_path)

    log("heightmap      : {} x {}".format(res, res))
    log("  frame        : {} x {} m".format(
        meta["frameMetres"]["width"], meta["frameMetres"]["height"]))
    log("  heights      : {}..{} m".format(
        meta["observedMetres"]["min"], meta["observedMetres"]["max"]))
    log("  land cover   : {:.1f}%".format(meta["landCoverage"] * 100))

    if actual_bytes != expected_bytes:
        # An LFS pointer file is ~130 bytes, so this catches the most likely
        # failure by far: the asset was never fetched.
        log("  BAD SIZE     : {} bytes, expected {}".format(
            actual_bytes, expected_bytes))
        if actual_bytes < 1000:
            log("  This looks like a Git LFS pointer. Run: git lfs pull")
        return None

    scale = meta["unrealLandscapeScale"]
    log("  import scale : X {}  Y {}  Z {}".format(
        scale["x"], scale["y"], scale["z"]))
    log("  (Z reproduces the {}..{} m range: Unreal maps full 16-bit onto "
        "512 uu at Z=100, so scale = range_cm / 512)".format(
            meta["heightRangeMetres"]["min"], meta["heightRangeMetres"]["max"]))
    return meta


def main():
    log("=" * 60)
    report_environment()
    log("-" * 60)
    meta = check_heightmap()
    log("=" * 60)
    if meta:
        log("Ready. Paste this output back and the landscape import follows.")
    else:
        log("Fix the heightmap above before continuing.")


if __name__ == "__main__":
    main()
