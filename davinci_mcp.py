#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2"]
# ///
"""davinci-mcp — MCP server that drives DaVinci Resolve through its bundled scripting API.

Run (Resolve must be open):
    uv run davinci_mcp.py

Register with Claude Code:
    claude mcp add davinci -- uv run /path/to/davinci_mcp.py

Override API locations with RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB if Resolve is installed elsewhere.
Logs one JSON object per line to stderr; set DAVINCI_MCP_LOG_LEVEL (default INFO) to change verbosity.
"""
import contextlib
import functools
import inspect
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from typing import Any
from datetime import datetime, timezone

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DEFAULTS = {
    "darwin": (
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
        "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    ),
    "win32": (
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
    ),
    "linux": (
        "/opt/resolve/Developer/Scripting",
        "/opt/resolve/libs/Fusion/fusionscript.so",
    ),
}

mcp = MCPServer(
    "davinci",
    instructions="Controls the running DaVinci Resolve instance: projects, media pool, timelines, "
    "clip properties, transitions, titles, markers, color grading, Fusion compositing and rendering. "
    "Start with timeline_overview to see the edit and view_frame to see the picture. "
    "Frames are absolute timeline frames.",
)

log = logging.getLogger("davinci_mcp")


class _JsonFormatter(logging.Formatter):
    """One JSON object per line. Structured fields come from `extra={"fields": {...}}`."""

    def format(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _setup_logging():
    # stdout carries the MCP stdio protocol, so logs must go to stderr.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    log.addHandler(handler)
    level = os.environ.get("DAVINCI_MCP_LOG_LEVEL", "INFO").upper()
    log.setLevel(level if isinstance(logging.getLevelName(level), int) else logging.INFO)
    log.propagate = False


def _tool(fn):
    """Register `fn` as an MCP tool and log each call with its arguments, duration and outcome."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        fields = {"tool": fn.__name__, "args": dict(sig.bind(*args, **kwargs).arguments)}
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except ToolError as e:
            fields.update(outcome="error", error=str(e), duration_ms=round((time.perf_counter() - start) * 1000, 1))
            log.warning("tool call failed", extra={"fields": fields})
            raise
        except Exception:
            fields.update(outcome="crash", duration_ms=round((time.perf_counter() - start) * 1000, 1))
            log.exception("tool call crashed", extra={"fields": fields})
            raise
        fields.update(outcome="ok", duration_ms=round((time.perf_counter() - start) * 1000, 1))
        log.info("tool call", extra={"fields": fields})
        return result

    return mcp.tool()(wrapper)


def _resolve():
    api, lib = DEFAULTS.get(sys.platform, DEFAULTS["linux"])
    os.environ.setdefault("RESOLVE_SCRIPT_API", api)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib)
    mods = os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules")
    if mods not in sys.path:
        sys.path.insert(0, mods)
    try:
        import DaVinciResolveScript as dvr
    except ImportError as e:
        raise ToolError(
            f"cannot load Resolve scripting module from {mods} ({e}) — is DaVinci Resolve installed? "
            "Set RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB if it is installed elsewhere."
        ) from e

    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        raise ToolError("cannot connect — is DaVinci Resolve running?")
    return resolve


def _project():
    resolve = _resolve()
    pm = resolve.GetProjectManager()
    proj = pm.GetCurrentProject()
    if not proj:
        raise ToolError("no project open")
    return pm, proj


def _timeline():
    _, proj = _project()
    tl = proj.GetCurrentTimeline()
    if not tl:
        raise ToolError("no timeline open")
    return proj, tl


def _walk(folder, prefix=""):
    for c in folder.GetClipList() or []:
        yield prefix, c
    for f in folder.GetSubFolderList() or []:
        yield from _walk(f, prefix + f.GetName() + "/")


def _clips_by_name(proj):
    return {c.GetName(): c for _, c in _walk(proj.GetMediaPool().GetRootFolder())}


def _item(tl, item, track):
    items = tl.GetItemListInTrack("video", track) or []
    if not 1 <= item <= len(items):
        raise ToolError(f"item {item} not found on video track {track}")
    return items[item - 1]


def _graph(it):
    """Node graph of a timeline item. Resolve 19+ exposes it via GetNodeGraph(); older versions on the item itself."""
    try:
        graph = it.GetNodeGraph()
    except AttributeError:
        graph = None
    return graph or it


def _method(obj, name, version):
    """A method only newer Resolve versions have (missing ones resolve to None through the bridge)."""
    fn = getattr(obj, name, None)
    if not callable(fn):
        raise ToolError(f"{name} needs DaVinci Resolve {version} or later")
    return fn


def _opt(obj, name, *args):
    """Result of an optional read, or None where this Resolve version lacks the method."""
    fn = getattr(obj, name, None)
    return fn(*args) if callable(fn) else None


def _check_node(it, node):
    count = int(_graph(it).GetNumNodes() or 0)
    if not 1 <= node <= count:
        raise ToolError(f"node {node} out of range (item has {count} node(s))")


@_tool
def status() -> dict:
    """Connection check: Resolve version, current project and timeline."""
    resolve = _resolve()
    proj = resolve.GetProjectManager().GetCurrentProject()
    tl = proj.GetCurrentTimeline() if proj else None
    return {
        "product": resolve.GetProductName(),
        "version": resolve.GetVersionString(),
        "page": resolve.GetCurrentPage(),
        "project": proj.GetName() if proj else None,
        "timeline": tl.GetName() if tl else None,
        "start_frame": tl.GetStartFrame() if tl else None,
        "end_frame": tl.GetEndFrame() if tl else None,
    }


@_tool
def list_projects() -> list[str]:
    """List projects in the current project-manager folder."""
    return list(_resolve().GetProjectManager().GetProjectListInCurrentFolder())


@_tool
def open_project(name: str) -> str:
    """Open a project by name."""
    if not _resolve().GetProjectManager().LoadProject(name):
        raise ToolError(f"cannot open project: {name}")
    return f"opened: {name}"


@_tool
def create_project(name: str) -> str:
    """Create and open a new project."""
    if not _resolve().GetProjectManager().CreateProject(name):
        raise ToolError(f"cannot create project (already exists?): {name}")
    return f"created: {name}"


@_tool
def import_media(paths: list[str]) -> list[str]:
    """Import media files into the media pool. Returns imported clip names."""
    _, proj = _project()
    paths = [os.path.abspath(p) for p in paths]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise ToolError("file(s) not found: " + ", ".join(missing))
    items = proj.GetMediaPool().ImportMedia(paths)
    if not items:
        raise ToolError("import failed")
    return [it.GetName() for it in items]


@_tool
def list_clips() -> list[dict]:
    """List all media-pool clips (recursive) with folder path and frame count."""
    _, proj = _project()
    return [
        {"folder": prefix or "/", "name": c.GetName(), "frames": c.GetClipProperty("Frames")}
        for prefix, c in _walk(proj.GetMediaPool().GetRootFolder())
    ]


@_tool
def list_timelines() -> list[dict]:
    """List timelines in the current project; `current` marks the active one."""
    _, proj = _project()
    cur = proj.GetCurrentTimeline()
    cur_name = cur.GetName() if cur else None
    out = []
    for i in range(1, int(proj.GetTimelineCount()) + 1):
        tl = proj.GetTimelineByIndex(i)
        out.append({"name": tl.GetName(), "current": tl.GetName() == cur_name})
    return out


@_tool
def create_timeline(name: str) -> str:
    """Create an empty timeline and make it current."""
    _, proj = _project()
    if not proj.GetMediaPool().CreateEmptyTimeline(name):
        raise ToolError(f"cannot create timeline: {name}")
    return f"created: {name}"


@_tool
def switch_timeline(name: str) -> str:
    """Make the named timeline current."""
    _, proj = _project()
    for i in range(1, int(proj.GetTimelineCount()) + 1):
        tl = proj.GetTimelineByIndex(i)
        if tl.GetName() == name:
            if not proj.SetCurrentTimeline(tl):
                raise ToolError(f"cannot switch to timeline: {name}")
            return f"switched: {name}"
    raise ToolError(f"timeline not found: {name}")


@_tool
def append_clips(
    names: list[str], start_frame: int | None = None, end_frame: int | None = None, track: int = 1
) -> str:
    """Append media-pool clips (by name, in order) to the end of the current timeline.
    start_frame/end_frame (clip-relative, both inclusive) make a subclip of each clip."""
    proj, tl = _timeline()
    pool = proj.GetMediaPool()
    by_name = _clips_by_name(proj)
    missing = [n for n in names if n not in by_name]
    if missing:
        raise ToolError(f"clips not in media pool: {', '.join(missing)} (see list_clips)")
    clips = [by_name[n] for n in names]
    if start_frame is None and end_frame is None:
        ok = pool.AppendToTimeline(clips)
    else:
        ok = pool.AppendToTimeline([
            {
                "mediaPoolItem": c,
                "startFrame": start_frame or 0,
                "endFrame": end_frame if end_frame is not None else int(float(c.GetClipProperty("Frames") or 0)) - 1,
                "trackIndex": track,
            }
            for c in clips
        ])
    if not ok:
        raise ToolError("append failed")
    return f"appended {len(clips)} clip(s) to '{tl.GetName()}'"


@_tool
def list_items(track: int = 1, track_type: str = "video") -> list[dict]:
    """List items on a timeline track (track_type: video|audio|subtitle). Index is 1-based."""
    _, tl = _timeline()
    return [
        {"index": i, "name": it.GetName(), "start": it.GetStart(), "end": it.GetEnd(), "duration": it.GetDuration()}
        for i, it in enumerate(tl.GetItemListInTrack(track_type, track) or [], 1)
    ]


# Resolve's enum-valued clip properties, in constant order (the index is the value Resolve takes).
PROPERTY_ENUMS = {
    "DynamicZoomEase": ["linear", "in", "out", "in_and_out"],
    "CompositeMode": [
        "normal", "add", "subtract", "diff", "multiply", "screen", "overlay", "hardlight", "softlight",
        "darken", "lighten", "color_dodge", "color_burn", "exclusion", "hue", "saturate", "colorize",
        "luma_mask", "divide", "linear_dodge", "linear_burn", "linear_light", "vivid_light", "pin_light",
        "hard_mix", "lighter_color", "darker_color", "foreground", "alpha", "inverted_alpha", "lum",
        "inverted_lum",
    ],
    "RetimeProcess": ["project", "nearest", "frame_blend", "optical_flow"],
    "MotionEstimation": ["project", "standard_faster", "standard_better", "enhanced_faster", "enhanced_better", "speed_warp"],
    "Scaling": ["project", "crop", "fit", "fill", "stretch"],
    "ResizeFilter": [
        "project", "sharper", "smoother", "bicubic", "bilinear", "bessel", "box", "catmull_rom", "cubic",
        "gaussian", "lanczos", "mitchell", "nearest_neighbor", "quadratic", "sinc", "linear",
    ],
}


def _prop_value(key, value):
    names = PROPERTY_ENUMS.get(key)
    if not names or not isinstance(value, str):
        return value
    norm = value.strip().lower().replace(" ", "_").replace("-", "_")
    if norm not in names:
        raise ToolError(f"{key} must be one of: {', '.join(names)} (got {value!r})")
    return names.index(norm)


@_tool
def set_item_properties(item: int, properties: dict, track: int = 1) -> dict:
    """Set properties on a video timeline item (1-based index from list_items).
    Transform: Pan Tilt ZoomX ZoomY ZoomGang RotationAngle AnchorPointX AnchorPointY Pitch Yaw FlipX FlipY.
    Crop: CropLeft CropRight CropTop CropBottom CropSoftness CropRetain. Composite: Opacity (0-100), CompositeMode.
    Other: Distortion, DynamicZoomEase, RetimeProcess, MotionEstimation, Scaling, ResizeFilter.
    Enum keys take a name (e.g. CompositeMode "screen", RetimeProcess "optical_flow", Scaling "fill") or the number."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    properties = {k: _prop_value(k, v) for k, v in properties.items()}
    failed = [k for k, v in properties.items() if not it.SetProperty(k, v)]
    if failed:
        raise ToolError(f"SetProperty failed for: {', '.join(failed)} (bad key or out-of-range value?)")
    return {"item": it.GetName(), "set": properties}


@_tool
def insert_title(name: str = "Text", fusion: bool = False, text: str | None = None) -> dict:
    """Insert a title template at the playhead of the current timeline.
    fusion=True uses a Fusion title (required for `text` to be applied)."""
    _, tl = _timeline()
    insert = tl.InsertFusionTitleIntoTimeline if fusion else tl.InsertTitleIntoTimeline
    it = insert(name)
    if not it:
        raise ToolError(f"could not insert title: {name}")
    out = {"title": name, "start": it.GetStart(), "end": it.GetEnd(), "text_set": False}
    if text:
        comp = it.GetFusionCompByIndex(1)
        tool = comp.FindTool("Template") if comp else None
        if tool:
            tool.SetInput("StyledText", text)
            out["text_set"] = True
    return out


@_tool
def add_marker(frame: int, note: str = "", color: str = "Blue", duration: int = 1) -> str:
    """Add a marker on the current timeline. `frame` is relative to the timeline start."""
    _, tl = _timeline()
    if not tl.AddMarker(frame, color, note or f"frame {frame}", note, duration):
        raise ToolError("marker failed (duplicate frame?)")
    return f"marker @ {frame} ({color})"


@_tool
def list_render_presets() -> list[str]:
    """List available render presets."""
    _, proj = _project()
    return list(proj.GetRenderPresetList())


@_tool
def render(
    target_dir: str,
    preset: str | None = None,
    file_name: str | None = None,
    format: str | None = None,
    codec: str | None = None,
) -> dict:
    """Queue and start rendering the current timeline. Returns the job id; progress is visible in Resolve.
    format/codec (see list_render_formats) override the preset's; both must be given together."""
    if (format is None) != (codec is None):
        raise ToolError("format and codec must be given together")
    _, proj = _project()
    if preset and not proj.LoadRenderPreset(preset):
        raise ToolError(f"unknown render preset: {preset}")
    if format and not proj.SetCurrentRenderFormatAndCodec(format, codec):
        raise ToolError(f"unsupported format/codec: {format}/{codec} (see list_render_formats)")
    settings = {"TargetDir": os.path.abspath(target_dir)}
    if file_name:
        settings["CustomName"] = file_name
    proj.SetRenderSettings(settings)
    job = proj.AddRenderJob()
    if not job:
        raise ToolError("could not add render job")
    proj.StartRendering([job], isInteractiveMode=False)
    return {"job": job, "target_dir": settings["TargetDir"]}


@_tool
def render_status(job: str) -> dict:
    """Progress of a render job started with `render`."""
    _, proj = _project()
    return proj.GetRenderJobStatus(job)


@_tool
def list_render_formats() -> dict:
    """Render formats with their file extension and codecs ({codec name: description}), for `render`."""
    _, proj = _project()
    return {
        fmt: {"extension": ext, "codecs": {name: desc for desc, name in (proj.GetRenderCodecs(fmt) or {}).items()}}
        for fmt, ext in (proj.GetRenderFormats() or {}).items()
    }


@_tool
def stop_render() -> str:
    """Stop any render in progress."""
    _, proj = _project()
    if not proj.IsRenderingInProgress():
        return "nothing rendering"
    proj.StopRendering()
    return "stopped"


PAGES = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")


@_tool
def open_page(page: str) -> str:
    """Switch Resolve to a page: media, cut, edit, fusion, color, fairlight or deliver."""
    if page not in PAGES:
        raise ToolError(f"unknown page: {page} (one of {', '.join(PAGES)})")
    if not _resolve().OpenPage(page):
        raise ToolError(f"cannot open page: {page}")
    return f"page: {page}"


@_tool
def color_info(item: int | None = None, track: int = 1) -> dict:
    """Grade state of a video item (1-based index from list_items; default: the item under the playhead):
    nodes with label and LUT, current and available color versions, and color group."""
    _, tl = _timeline()
    it = _item(tl, item, track) if item is not None else tl.GetCurrentVideoItem()
    if not it:
        raise ToolError("no video item under the playhead")
    graph = _graph(it)
    try:
        group = it.GetColorGroup()  # Resolve 18+
    except AttributeError:
        group = None
    return {
        "item": it.GetName(),
        "nodes": [
            {"index": n, "label": graph.GetNodeLabel(n) or "", "lut": graph.GetLUT(n) or None}
            for n in range(1, int(graph.GetNumNodes() or 0) + 1)
        ],
        "version": it.GetCurrentVersion(),
        "local_versions": list(it.GetVersionNameList(0) or []),
        "remote_versions": list(it.GetVersionNameList(1) or []),
        "color_group": group.GetName() if group else None,
    }


@_tool
def apply_lut(item: int, lut_path: str, node: int = 1, track: int = 1) -> str:
    """Set a LUT on a node (1-based) of a video item. lut_path is absolute, or relative to Resolve's LUT folders;
    Resolve only accepts LUTs it has already discovered (Project Settings → Color Management → Update Lists)."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    _check_node(it, node)
    if not _graph(it).SetLUT(node, lut_path):
        raise ToolError(f"SetLUT failed for {lut_path} (unknown to Resolve?)")
    return f"LUT on node {node} of '{it.GetName()}': {lut_path}"


def _rgb(name, v):
    if len(v) != 3:
        raise ToolError(f"{name} needs 3 values (R G B), got {len(v)}")
    return " ".join(str(float(x)) for x in v)


@_tool
def set_cdl(
    item: int,
    slope: list[float] = [1.0, 1.0, 1.0],
    offset: list[float] = [0.0, 0.0, 0.0],
    power: list[float] = [1.0, 1.0, 1.0],
    saturation: float = 1.0,
    node: int = 1,
    track: int = 1,
) -> dict:
    """Apply an ASC CDL (slope/offset/power per R G B, plus saturation) to a node (1-based) of a video item."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    _check_node(it, node)
    cdl = {
        "NodeIndex": str(node),
        "Slope": _rgb("slope", slope),
        "Offset": _rgb("offset", offset),
        "Power": _rgb("power", power),
        "Saturation": str(float(saturation)),
    }
    if not it.SetCDL(cdl):
        raise ToolError("SetCDL failed")
    return {"item": it.GetName(), "cdl": cdl}


@_tool
def copy_grade(source: int, targets: list[int], track: int = 1) -> str:
    """Copy the grade of one video item to others (1-based indexes on the same track)."""
    _, tl = _timeline()
    src = _item(tl, source, track)
    dst = [_item(tl, t, track) for t in targets]
    if not dst:
        raise ToolError("no targets given")
    if not src.CopyGrades(dst):
        raise ToolError("CopyGrades failed")
    return f"copied grade of '{src.GetName()}' to {len(dst)} item(s)"


DRX_MODES = {"none": 0, "source_timecode": 1, "start_frames": 2}


@_tool
def apply_drx(path: str, items: list[int], keyframes: str = "none", track: int = 1) -> str:
    """Apply a grade from a .drx still file to video items (1-based indexes).
    keyframes: none | source_timecode | start_frames (how keyframes in the still are aligned)."""
    _, tl = _timeline()
    if keyframes not in DRX_MODES:
        raise ToolError(f"unknown keyframes mode: {keyframes} (one of {', '.join(DRX_MODES)})")
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ToolError(f"file not found: {path}")
    targets = [_item(tl, i, track) for i in items]
    if not targets:
        raise ToolError("no items given")
    if not tl.ApplyGradeFromDRX(path, DRX_MODES[keyframes], targets):
        raise ToolError("ApplyGradeFromDRX failed")
    return f"applied {os.path.basename(path)} to {len(targets)} item(s)"


@_tool
def add_color_version(item: int, name: str, remote: bool = False, track: int = 1) -> str:
    """Add a named color version to a video item (local by default) and make it current."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    if not it.AddVersion(name, int(remote)):
        raise ToolError(f"cannot add version (name taken?): {name}")
    return f"added {'remote' if remote else 'local'} version '{name}' to '{it.GetName()}'"


@_tool
def load_color_version(item: int, name: str, remote: bool = False, track: int = 1) -> str:
    """Make a named color version of a video item current."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    if not it.LoadVersionByName(name, int(remote)):
        raise ToolError(f"version not found: {name} (see color_info)")
    return f"loaded version '{name}' on '{it.GetName()}'"


@_tool
def export_lut(item: int, path: str, size: int = 33, track: int = 1) -> str:
    """Export a video item's grade as a .cube LUT (size 17, 33 or 65 points). Needs Resolve 18 or later."""
    resolve = _resolve()
    _, tl = _timeline()
    it = _item(tl, item, track)
    kind = {17: "EXPORT_LUT_17PTCUBE", 33: "EXPORT_LUT_33PTCUBE", 65: "EXPORT_LUT_65PTCUBE"}.get(size)
    if not kind:
        raise ToolError(f"unsupported LUT size: {size} (17, 33 or 65)")
    path = os.path.abspath(path)
    if not it.ExportLUT(getattr(resolve, kind), path):
        raise ToolError(f"ExportLUT failed: {path}")
    return f"exported {size}-point LUT of '{it.GetName()}' to {path}"


@_tool
def grab_still() -> str:
    """Grab a still of the frame under the playhead into the current gallery album, as a grade reference
    (Color page must be open; see open_page). To get the image itself use view_frame."""
    _, tl = _timeline()
    if not tl.GrabStill():
        raise ToolError("GrabStill failed (is the Color page open?)")
    return "still grabbed into the current gallery album"



# --- Fusion ---
#
# Measured on live Resolve (Studio 19.1.3) by other Resolve automation projects and followed here:
# - Structural edits (AddTool, ConnectInput, Delete) run under comp.Lock().
# - Value writes and keyframes must NOT: under the lock they read back fine but the render ignores them.
#   They run inside StartUndo/EndUndo instead, so each call is one undo step in Resolve.
# - Assigning a value at a frame only creates a keyframe once a spline modifier is attached
#   (BezierSpline for numbers, Path for points); otherwise it silently sets a static value.
# - Point inputs (e.g. Center) take [x, y] or {1: x, 2: y} depending on the build.

ANIMATION_MODIFIERS = {"BezierSpline", "PolyPath", "Path", "XYPath"}


@contextlib.contextmanager
def _locked(comp):
    comp.Lock()
    try:
        yield
    finally:
        comp.Unlock()


@contextlib.contextmanager
def _undo(comp, name):
    comp.StartUndo(name)
    try:
        yield
    finally:
        comp.EndUndo(True)


def _comp(item, comp, track):
    _, tl = _timeline()
    it = _item(tl, item, track)
    count = int(it.GetFusionCompCount() or 0)
    if not 1 <= comp <= count:
        raise ToolError(f"comp {comp} not found on '{it.GetName()}' ({count} comp(s); see add_fusion_comp)")
    return it, it.GetFusionCompByIndex(comp)


def _node(comp, name):
    tool = comp.FindTool(name)
    if not tool:
        raise ToolError(f"node not found: {name} (see fusion_nodes)")
    return tool


def _tool_attrs(tool):
    attrs = tool.GetAttrs() or {}
    return attrs.get("TOOLS_Name", ""), attrs.get("TOOLS_RegID", "")


def _source(inp):
    """(name, type) of the tool feeding an input, or (None, None)."""
    out = inp.GetConnectedOutput()
    return _tool_attrs(out.GetTool()) if out else (None, None)


def _plain(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return str(v)


def _write(assign, value, point):
    """Write a value through `assign`, trying the point encodings the bridge may want."""
    if not point:
        assign(value)
        return
    if isinstance(value, dict):
        candidates = [value]
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        candidates = [list(value), {1: value[0], 2: value[1]}]
    else:
        raise ToolError(f"point input needs [x, y], got {value!r}")
    for i, cand in enumerate(candidates):
        try:
            assign(cand)
            return
        except Exception:
            if i == len(candidates) - 1:
                raise


@_tool
def fusion_comps(item: int, track: int = 1) -> list[str]:
    """Fusion compositions on a video item (1-based index from list_items), in comp index order."""
    _, tl = _timeline()
    return list(_item(tl, item, track).GetFusionCompNameList() or [])


@_tool
def add_fusion_comp(item: int, import_path: str | None = None, track: int = 1) -> dict:
    """Add a Fusion composition to a video item: empty (MediaIn → MediaOut), or imported from a .comp file."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    if import_path:
        import_path = os.path.abspath(import_path)
        if not os.path.exists(import_path):
            raise ToolError(f"file not found: {import_path}")
        comp = it.ImportFusionComp(import_path)
    else:
        comp = it.AddFusionComp()
    if not comp:
        raise ToolError("could not add Fusion composition")
    names = list(it.GetFusionCompNameList() or [])
    return {"item": it.GetName(), "comp": len(names), "comps": names}


@_tool
def export_fusion_comp(item: int, path: str, comp: int = 1, track: int = 1) -> str:
    """Save a video item's Fusion composition as a .comp file (reusable template)."""
    it, _ = _comp(item, comp, track)
    path = os.path.abspath(path)
    if not it.ExportFusionComp(path, comp):
        raise ToolError(f"export failed: {path}")
    return f"exported comp {comp} of '{it.GetName()}' to {path}"


@_tool
def insert_fusion(kind: str = "composition", name: str | None = None) -> dict:
    """Insert at the playhead a Fusion composition clip (kind=composition) or a Fusion generator by name
    (kind=generator). Titles go through insert_title(fusion=True)."""
    _, tl = _timeline()
    if kind == "composition":
        it = tl.InsertFusionCompositionIntoTimeline()
    elif kind == "generator":
        if not name:
            raise ToolError("a generator name is required")
        it = tl.InsertFusionGeneratorIntoTimeline(name)
    else:
        raise ToolError(f"unknown kind: {kind} (composition or generator)")
    if not it:
        raise ToolError(f"could not insert Fusion {kind}" + (f": {name}" if name else ""))
    return {"name": it.GetName(), "start": it.GetStart(), "end": it.GetEnd()}


@_tool
def create_fusion_clip(items: list[int], track: int = 1) -> dict:
    """Combine video items (1-based indexes) into one Fusion clip, to composite them together."""
    _, tl = _timeline()
    targets = [_item(tl, i, track) for i in items]
    if not targets:
        raise ToolError("no items given")
    it = tl.CreateFusionClip(targets)
    if not it:
        raise ToolError("CreateFusionClip failed")
    return {"name": it.GetName(), "start": it.GetStart(), "end": it.GetEnd()}


@_tool
def fusion_nodes(item: int, comp: int = 1, track: int = 1) -> list[dict]:
    """Nodes of a Fusion composition with type and wiring: `inputs` maps each connected input to its source
    node; `animated` lists inputs driven by a spline or path."""
    _, c = _comp(item, comp, track)
    out = []
    for tool in (c.GetToolList(False) or {}).values():
        name, kind = _tool_attrs(tool)
        if kind in ANIMATION_MODIFIERS:
            continue
        inputs, animated = {}, []
        for inp in (tool.GetInputList() or {}).values():
            src, src_kind = _source(inp)
            if src is None:
                continue
            inp_id = (inp.GetAttrs() or {}).get("INPS_ID", "")
            if src_kind in ANIMATION_MODIFIERS:
                animated.append(inp_id)
            else:
                inputs[inp_id] = src
        out.append({"name": name, "type": kind, "inputs": inputs, "animated": animated})
    return out


@_tool
def fusion_inputs(item: int, node: str, filter: str | None = None, comp: int = 1, track: int = 1) -> list[dict]:
    """Inputs of a Fusion node: id, name, type, current value, and source node or animation.
    `filter` keeps only inputs whose id or name contains it (case-insensitive); Text+ has hundreds."""
    _, c = _comp(item, comp, track)
    tool = _node(c, node)
    rows = []
    for inp in (tool.GetInputList() or {}).values():
        attrs = inp.GetAttrs() or {}
        inp_id, inp_name = attrs.get("INPS_ID", ""), attrs.get("INPS_Name", "")
        if filter and filter.lower() not in f"{inp_id} {inp_name}".lower():
            continue
        kind = attrs.get("INPS_DataType", "")
        src, src_kind = _source(inp)
        rows.append({
            "id": inp_id,
            "name": inp_name,
            "type": kind,
            "value": None if kind in ("Image", "Mask") else _plain(tool.GetInput(inp_id)),
            "animated": src_kind in ANIMATION_MODIFIERS,
            "source": src if src_kind not in ANIMATION_MODIFIERS else None,
        })
    return rows


@_tool
def add_fusion_node(
    item: int,
    tool_type: str,
    name: str | None = None,
    connect_from: str | None = None,
    input: str = "Input",
    comp: int = 1,
    track: int = 1,
) -> dict:
    """Add a node by registry id (e.g. Blur, Transform, TextPlus, Merge, Glow, ColorCorrector, EllipseMask,
    RectangleMask, Background, FastNoise). Optionally rename it and feed `connect_from`'s output into `input`."""
    _, c = _comp(item, comp, track)
    src = _node(c, connect_from) if connect_from else None
    if name and c.FindTool(name):
        raise ToolError(f"a node named {name} already exists")
    with _locked(c):
        tool = c.AddTool(tool_type, -1, -1)
        if not tool:
            raise ToolError(f"unknown tool type: {tool_type}")
        if name:
            tool.SetAttrs({"TOOLS_Name": name})
        connected = bool(tool.ConnectInput(input, src)) if src else False
    if src and not connected:
        raise ToolError(f"added {_tool_attrs(tool)[0]} but could not connect {connect_from} to its {input}")
    final, kind = _tool_attrs(tool)
    return {"name": final, "type": kind, "connected": {input: connect_from} if connected else {}}


@_tool
def connect_fusion_nodes(
    item: int, target: str, source: str | None, input: str = "Input", comp: int = 1, track: int = 1
) -> str:
    """Feed `source`'s main output into `target`'s `input` (e.g. Input, Background, Foreground, EffectMask).
    source=null disconnects the input."""
    _, c = _comp(item, comp, track)
    dst = _node(c, target)
    src = _node(c, source) if source else None
    with _locked(c):
        ok = dst.ConnectInput(input, src)
    if not ok:
        raise ToolError(f"cannot connect {source} to {target}.{input} (see fusion_inputs for valid inputs)")
    return f"{source} → {target}.{input}" if source else f"disconnected {target}.{input}"


@_tool
def delete_fusion_node(item: int, node: str, comp: int = 1, track: int = 1) -> str:
    """Delete a node from a Fusion composition."""
    _, c = _comp(item, comp, track)
    tool = _node(c, node)
    with _locked(c):
        tool.Delete()
    return f"deleted {node}"


@_tool
def set_fusion_input(
    item: int,
    node: str,
    input: str,
    value: Any = None,
    keyframes: dict[int, Any] | None = None,
    comp: int = 1,
    track: int = 1,
) -> dict:
    """Set a Fusion node input to a static `value`, or animate it with `keyframes` ({frame: value}, frames
    relative to the comp). Numbers get a Bezier spline, points ([x, y], e.g. Center) a path; text is static only."""
    if (value is None) == (keyframes is None):
        raise ToolError("give either value or keyframes")
    _, c = _comp(item, comp, track)
    return _set_input(c, _node(c, node), node, input, value, keyframes)


def _set_input(c, tool, node, input, value=None, keyframes=None):
    inp = tool[input]
    if not inp:
        raise ToolError(f"{node} has no input {input} (see fusion_inputs)")
    kind = (inp.GetAttrs() or {}).get("INPS_DataType", "")
    point = kind == "Point"
    with _undo(c, f"{node}.{input}"):
        if keyframes is None:
            _write(lambda v: tool.SetInput(input, v), value, point)
            return {"node": node, "input": input, "value": _plain(tool.GetInput(input))}
        if _source(inp)[1] not in ANIMATION_MODIFIERS:
            tool.AddModifier(input, "Path" if point else "BezierSpline")
            if _source(tool[input])[1] not in ANIMATION_MODIFIERS:
                raise ToolError(f"{node}.{input} ({kind or 'unknown type'}) cannot be animated")
        for frame, v in sorted(keyframes.items()):
            _write(lambda x, f=frame: tool[input].__setitem__(f, x), v, point)
    frames = sorted(float(f) for f in (tool[input].GetKeyFrames() or {}).values())
    return {"node": node, "input": input, "keyframes": frames}


def _insert_before_output(c, tool_type, name):
    """Add a node between MediaOut1 and whatever feeds it (MediaIn1 in a fresh comp)."""
    out = _node(c, "MediaOut1")
    src_name = _source(out["Input"])[0] or "MediaIn1"
    src = _node(c, src_name)
    if name and c.FindTool(name):
        raise ToolError(f"a node named {name} already exists")
    with _locked(c):
        tool = c.AddTool(tool_type, -1, -1)
        if not tool:
            raise ToolError(f"unknown tool type: {tool_type}")
        if name:
            tool.SetAttrs({"TOOLS_Name": name})
        if not (tool.ConnectInput("Input", src) and out.ConnectInput("Input", tool)):
            raise ToolError(f"could not wire {tool_type} between {src_name} and MediaOut1")
    return tool


def _clip_comp(it):
    """The item's first Fusion comp, created if it has none."""
    if int(it.GetFusionCompCount() or 0) == 0 and not it.AddFusionComp():
        raise ToolError(f"could not add a Fusion composition to '{it.GetName()}'")
    return it.GetFusionCompByIndex(1)


# --- Editing ---


def _kind(items, i):
    """clip, transition (straddles a cut) or other (title, generator, Fusion composition)."""
    it = items[i]
    if _opt(it, "GetMediaPoolItem"):
        return "clip"
    start, end = it.GetStart(), it.GetEnd()
    before = i > 0 and items[i - 1].GetEnd() > start
    after = i + 1 < len(items) and items[i + 1].GetStart() < end
    return "transition" if before or after else "other"


@_tool
def timeline_overview() -> dict:
    """The whole current timeline in one call: format, playhead, every track with its items (kind, source clip,
    position, enabled, Fusion comps) and markers. Item indexes match list_items; transitions count as items."""
    _, tl = _timeline()
    tracks = {}
    for kind in ("video", "audio", "subtitle"):
        rows = []
        for n in range(1, int(tl.GetTrackCount(kind) or 0) + 1):
            items = list(tl.GetItemListInTrack(kind, n) or [])
            entries = []
            for i, it in enumerate(items):
                mpi = _opt(it, "GetMediaPoolItem")
                entry = {
                    "index": i + 1,
                    "kind": _kind(items, i),
                    "name": it.GetName(),
                    "source": mpi.GetName() if mpi else None,
                    "start": it.GetStart(),
                    "end": it.GetEnd(),
                    "duration": it.GetDuration(),
                    "enabled": _opt(it, "GetClipEnabled"),
                }
                if kind == "video":
                    entry["fusion_comps"] = int(_opt(it, "GetFusionCompCount") or 0)
                entries.append(entry)
            rows.append({
                "index": n,
                "name": _opt(tl, "GetTrackName", kind, n),
                "enabled": _opt(tl, "GetIsTrackEnabled", kind, n),
                "items": entries,
            })
        tracks[kind] = rows
    return {
        "timeline": tl.GetName(),
        "fps": _opt(tl, "GetSetting", "timelineFrameRate"),
        "resolution": [_opt(tl, "GetSetting", "timelineResolutionWidth"), _opt(tl, "GetSetting", "timelineResolutionHeight")],
        "start_frame": tl.GetStartFrame(),
        "end_frame": tl.GetEndFrame(),
        "playhead": _opt(tl, "GetCurrentTimecode"),
        "tracks": tracks,
        "markers": {str(int(f)) if float(f).is_integer() else str(f): m for f, m in (_opt(tl, "GetMarkers") or {}).items()},
    }


def _timecode(tl, frame):
    if str(_opt(tl, "GetSetting", "timelineDropFrameTimecode")) in ("1", "True"):
        raise ToolError("timeline uses drop-frame timecode; pass `timecode` instead of `frame`")
    fps = round(float(_opt(tl, "GetSetting", "timelineFrameRate") or 24))
    s, f = divmod(int(frame), fps)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


@_tool
def view_frame(timecode: str | None = None, frame: int | None = None, save_to: str | None = None) -> list:
    """See the picture: move the playhead (to an absolute `timecode`, or absolute timeline `frame` as in
    list_items) and return that frame as an image, graded and composited as Resolve shows it.
    save_to also keeps the file (.png/.jpg/.tif/.dpx...). Works on pages with a viewer (edit, cut, color...)."""
    proj, tl = _timeline()
    if timecode is not None and frame is not None:
        raise ToolError("give timecode or frame, not both")
    tc = timecode if timecode is not None else (_timecode(tl, frame) if frame is not None else None)
    if tc and not tl.SetCurrentTimecode(tc):
        raise ToolError(f"cannot move playhead to {tc}")
    tmp = None if save_to else tempfile.mkdtemp(prefix="davinci_mcp_")
    path = os.path.abspath(save_to) if save_to else os.path.join(tmp, "frame.png")
    try:
        if not proj.ExportCurrentFrameAsStill(path) or not os.path.exists(path):
            raise ToolError("ExportCurrentFrameAsStill failed (is a page with a viewer open?)")
        ext = os.path.splitext(path)[1].lower()
        note = f"frame at {_opt(tl, 'GetCurrentTimecode') or tc}" + (f", saved to {path}" if save_to else "")
        if ext not in (".png", ".jpg", ".jpeg"):
            return [note + f" ({ext} is not viewable inline)"]
        with open(path, "rb") as f:
            data = f.read()
        return [Image(data=data, format="jpeg" if ext != ".png" else "png"), note]
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


TRANSITION_CATEGORIES = ("simple", "fusion", "ofx", "audio")


@_tool
def add_transition(
    item: int,
    type: str = "Cross Dissolve",
    position: str = "end",
    alignment: str = "center",
    duration: int | None = None,
    category: str = "simple",
    track: int = 1,
    track_type: str = "video",
) -> dict:
    """Add a transition at the start or end of an item (Resolve 21.1+). type is the name as in Resolve's
    Effects library (Cross Dissolve, Dip To Color Dissolve, Smooth Cut, Push, Slide, Wipe...); category is
    simple, fusion, ofx or audio. Needs unused source media (handles) past the cut. Transitions become items
    of their own, so indexes after it shift by one."""
    if position not in ("start", "end"):
        raise ToolError("position must be start or end")
    if alignment not in ("left", "center", "right"):
        raise ToolError("alignment must be left, center or right")
    if category not in TRANSITION_CATEGORIES:
        raise ToolError(f"category must be one of: {', '.join(TRANSITION_CATEGORIES)}")
    if duration is not None and duration < 1:
        raise ToolError("duration must be a positive number of frames")
    _, tl = _timeline()
    items = tl.GetItemListInTrack(track_type, track) or []
    if not 1 <= item <= len(items):
        raise ToolError(f"item {item} not found on {track_type} track {track}")
    it = items[item - 1]
    options = {"type": type, "category": category, "position": position, "alignment": alignment}
    if duration is not None:
        options["duration"] = duration
    tr = _method(it, "AddTransition", "21.1")(options)
    if not tr:
        raise ToolError(
            f"no transition created — check the name matches an installed {category} transition and that "
            "both clips have handles (unused media) past the cut"
        )
    return {"name": tr.GetName(), "start": tr.GetStart(), "end": tr.GetEnd(), "duration": tr.GetDuration()}


@_tool
def delete_items(items: list[int], ripple: bool = False, track: int = 1, track_type: str = "video") -> str:
    """Delete items (1-based indexes, clips or transitions) from a track. ripple=True closes the gaps."""
    _, tl = _timeline()
    all_items = tl.GetItemListInTrack(track_type, track) or []
    bad = [i for i in items if not 1 <= i <= len(all_items)]
    if bad or not items:
        raise ToolError(f"items not found on {track_type} track {track}: {bad or 'none given'}")
    if not tl.DeleteClips([all_items[i - 1] for i in items], ripple):
        raise ToolError("DeleteClips failed")
    return f"deleted {len(items)} item(s)" + (" (ripple)" if ripple else "")


@_tool
def set_clip_enabled(item: int, enabled: bool, track: int = 1, track_type: str = "video") -> str:
    """Enable or disable an item (a disabled clip is skipped in playback and render)."""
    _, tl = _timeline()
    items = tl.GetItemListInTrack(track_type, track) or []
    if not 1 <= item <= len(items):
        raise ToolError(f"item {item} not found on {track_type} track {track}")
    if not items[item - 1].SetClipEnabled(enabled):
        raise ToolError("SetClipEnabled failed")
    return f"{'enabled' if enabled else 'disabled'} '{items[item - 1].GetName()}'"


@_tool
def stabilize(item: int, track: int = 1) -> str:
    """Run Resolve's stabilizer on a video item with its current stabilization settings. Analysis can continue
    in the background; check the result with view_frame."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    if not _method(it, "Stabilize", "18")():
        raise ToolError(f"Stabilize failed for '{it.GetName()}' (unsupported clip, or a limitation of this edition)")
    return f"stabilized '{it.GetName()}'"


@_tool
def smart_reframe(item: int, track: int = 1) -> str:
    """Reframe a video item for the timeline's aspect ratio (e.g. 16:9 to 9:16) by tracking its subject.
    Studio only: on the free edition Resolve shows an upgrade dialog that blocks later calls until dismissed."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    if not _method(it, "SmartReframe", "18")():
        raise ToolError(f"SmartReframe failed for '{it.GetName()}' (Studio only; set the timeline aspect first)")
    return f"reframed '{it.GetName()}'"


@_tool
def detect_scene_cuts() -> str:
    """Split the current timeline's clips at detected scene cuts (Studio). Item indexes change afterwards."""
    _, tl = _timeline()
    if not _method(tl, "DetectSceneCuts", "18.5")():
        raise ToolError("DetectSceneCuts failed (Studio only)")
    return "scene cuts detected; re-read the timeline with timeline_overview"


@_tool
def dynamic_zoom(
    item: int,
    start_zoom: float = 1.0,
    end_zoom: float = 1.2,
    start_center: list[float] = [0.5, 0.5],
    end_center: list[float] = [0.5, 0.5],
    track: int = 1,
) -> dict:
    """Ken Burns move over the whole clip: animate zoom (1.0 = full frame) and center ([x, y], 0-1, image
    center 0.5, 0.5) from start to end. Built as a keyframed Fusion Transform named DynamicZoom in the clip's
    comp, so it renders everywhere and can be refined with set_fusion_input."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    if start_zoom <= 0 or end_zoom <= 0:
        raise ToolError("zoom must be positive")
    c = _clip_comp(it)
    if c.FindTool("DynamicZoom"):
        raise ToolError("clip already has a DynamicZoom node; delete_fusion_node it first")
    tool = _insert_before_output(c, "Transform", "DynamicZoom")
    attrs = c.GetAttrs() or {}
    first = int(attrs.get("COMPN_RenderStart", 0))
    last = int(attrs.get("COMPN_RenderEnd", first + int(it.GetDuration()) - 1))
    _set_input(c, tool, "DynamicZoom", "Size", keyframes={first: start_zoom, last: end_zoom})
    if list(start_center) != list(end_center) or list(start_center) != [0.5, 0.5]:
        _set_input(c, tool, "DynamicZoom", "Center", keyframes={first: start_center, last: end_center})
    return {"item": it.GetName(), "node": "DynamicZoom", "frames": [first, last], "zoom": [start_zoom, end_zoom],
            "center": [list(start_center), list(end_center)]}


@_tool
def insert_fusion_effect(item: int, tool_type: str, settings: dict | None = None, name: str | None = None, track: int = 1) -> dict:
    """Apply a filter to a video item: insert a Fusion node right before MediaOut1 of the clip's comp (created
    if missing) and set its inputs. Fusion tools (Blur, SoftGlow, Glow, FilmGrain, Sharpen, ColorCorrector,
    DirectionalBlur, Defocus, ...) or ResolveFX by their Fusion id (see fusion_inputs to find input names)."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    c = _clip_comp(it)
    tool = _insert_before_output(c, tool_type, name)
    node = _tool_attrs(tool)[0]
    try:
        applied = {k: _set_input(c, tool, node, k, value=v)["value"] for k, v in (settings or {}).items()}
    except ToolError as e:
        raise ToolError(f"{node} was added to the chain but a setting failed: {e}") from e
    return {"item": it.GetName(), "node": node, "type": tool_type, "settings": applied}


if __name__ == "__main__":
    _setup_logging()
    log.info("starting", extra={"fields": {"platform": sys.platform, "pid": os.getpid()}})
    mcp.run()
