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
    "clip properties, transitions, titles, markers, color grading and management, HDR, Fusion compositing, audio, "
    "media management, projects and review notes, "
    "interchange export and rendering. "
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


UNTITLED = "Untitled Project"


def _save_current(pm):
    """Save the current project before anything that replaces or closes it. The default Untitled Project is
    skipped: SaveProject cannot succeed on it (False in the GUI, an endless hang headless)."""
    proj = pm.GetCurrentProject()
    if proj and proj.GetName() != UNTITLED and not pm.SaveProject():
        raise ToolError(f"could not save the current project '{proj.GetName()}'; nothing was switched")


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


@contextlib.contextmanager
def _on_page(resolve, page):
    """Run a page-gated call on `page`, then return to where the user was."""
    prev = resolve.GetCurrentPage()
    if prev != page:
        resolve.OpenPage(page)
    try:
        yield
    finally:
        if prev and prev != page:
            resolve.OpenPage(prev)


def _bin(proj, path):
    """Media-pool folder by path: "/" (root), "Footage" or "Footage/Day 1"."""
    folder = proj.GetMediaPool().GetRootFolder()
    for part in [p for p in (path or "").strip("/").split("/") if p]:
        folder = next((f for f in folder.GetSubFolderList() or [] if f.GetName() == part), None)
        if folder is None:
            raise ToolError(f"bin not found: {path} (see list_clips)")
    return folder


@contextlib.contextmanager
def _in_bin(pool, folder):
    """ImportMedia only targets the current folder, so switch to `folder` and back."""
    prev = pool.GetCurrentFolder()
    pool.SetCurrentFolder(folder)
    try:
        yield
    finally:
        if prev:
            pool.SetCurrentFolder(prev)


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
    """Open a project by name. The current project is saved first."""
    pm = _resolve().GetProjectManager()
    _save_current(pm)
    if not pm.LoadProject(name):
        raise ToolError(f"cannot open project: {name}")
    return f"opened: {name}"


@_tool
def create_project(name: str) -> str:
    """Create and open a new project. The current project is saved first: CreateProject replaces it, and an
    unsaved one would be lost."""
    pm = _resolve().GetProjectManager()
    _save_current(pm)
    if not pm.CreateProject(name):
        raise ToolError(f"cannot create project: {name} (name taken, or the current project blocks the switch)")
    return f"created: {name}"


@_tool
def import_media(paths: list[str], bin: str | None = None) -> list[str]:
    """Import media files or folders into the media pool, into `bin` (e.g. "Footage/Day 1"; default: the current
    bin). Returns imported clip names."""
    _, proj = _project()
    paths = [os.path.abspath(p) for p in paths]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise ToolError("file(s) not found: " + ", ".join(missing))
    pool = proj.GetMediaPool()
    with _in_bin(pool, _bin(proj, bin) if bin else pool.GetCurrentFolder()):
        items = pool.ImportMedia(paths)
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
    mark_in: int | None = None,
    mark_out: int | None = None,
    width: int | None = None,
    height: int | None = None,
    frame_rate: float | None = None,
    quality: int | str | None = None,
    video: bool | None = None,
    audio: bool | None = None,
    individual_clips: bool = False,
    settings: dict | None = None,
    start: bool = True,
) -> dict:
    """Queue a render of the current timeline and start it (start=False only queues it).
    format/codec are ids from list_render_formats (e.g. "mov" + "ProRes422HQ"), both or neither.
    mark_in/mark_out: absolute timeline frames (as in list_items), both inclusive; default whole timeline.
    quality: 0 auto, a bitrate, or Least/Low/Medium/High/Best. individual_clips=True renders one file per clip.
    settings: any other SetRenderSettings key (AudioCodec, AudioBitDepth, AudioSampleRate, ColorSpaceTag, GammaTag,
    ExportAlpha, AlphaMode, EncodingProfile, MultiPassEncode, NetworkOptimization, PixelAspectRatio...).
    Unset values inherit the Deliver page's current state, so pass a `preset` to start from a known base."""
    if (format is None) != (codec is None):
        raise ToolError("format and codec must be given together")
    if (mark_in is None) != (mark_out is None):
        raise ToolError("mark_in and mark_out must be given together")
    proj, tl = _timeline()
    if mark_in is not None:
        first, last = int(tl.GetStartFrame()), int(tl.GetEndFrame())
        # Resolve silently clamps frames below the start instead of refusing them.
        if not first <= mark_in <= mark_out <= last:
            raise ToolError(f"mark_in/mark_out must be absolute frames with {first} <= in <= out <= {last}")
    if preset and not proj.LoadRenderPreset(preset):
        raise ToolError(f"unknown render preset: {preset}")
    if format:
        if not (proj.GetRenderCodecs(format) or {}):
            raise ToolError(f"unknown render format id or one without selectable codecs: {format} "
                            "(see list_render_formats)")
        if not proj.SetCurrentRenderFormatAndCodec(format, codec):
            raise ToolError(f"unsupported format/codec: {format}/{codec} (see list_render_formats)")
    if not proj.SetCurrentRenderMode(0 if individual_clips else 1):
        raise ToolError("could not set render mode")
    values = dict(settings or {})
    values["TargetDir"] = os.path.abspath(target_dir)
    for key, value in (("CustomName", file_name), ("FormatWidth", width), ("FormatHeight", height),
                       ("FrameRate", frame_rate), ("VideoQuality", quality), ("ExportVideo", video),
                       ("ExportAudio", audio)):
        if value is not None:
            values[key] = value
    if mark_in is not None:
        values.update(SelectAllFrames=False, MarkIn=mark_in, MarkOut=mark_out)
    elif "MarkIn" not in values:
        values["SelectAllFrames"] = True
    if not proj.SetRenderSettings(values):
        raise ToolError(f"Resolve rejected the render settings: {sorted(values)}")
    job = proj.AddRenderJob()
    if not job:
        raise ToolError("could not add render job")
    if start and not proj.StartRendering([job], isInteractiveMode=False):
        raise ToolError(f"job {job} queued but rendering did not start")
    return {"job": job, "target_dir": values["TargetDir"], "started": start}


@_tool
def render_status(job: str) -> dict:
    """Progress of a render job. `done` is judged from CompletionPercentage and Error (JobStatus is a translated
    display string), and a finished job also reports whether its output file exists."""
    _, proj = _project()
    status = dict(proj.GetRenderJobStatus(job) or {})
    if not status:
        raise ToolError(f"render job not found: {job} (see render_queue)")
    done = float(status.get("CompletionPercentage") or 0) >= 100 and not status.get("Error")
    status["done"] = done
    info = next((j for j in (proj.GetRenderJobList() or []) if j.get("JobId") == job), None)
    if info and info.get("TargetDir") and info.get("OutputFilename"):
        status["output"] = os.path.join(info["TargetDir"], info["OutputFilename"])
        if done:
            status["output_exists"] = os.path.exists(status["output"])
    return status


@_tool
def render_queue() -> list[dict]:
    """All jobs in the render queue with their settings and progress."""
    _, proj = _project()
    out = []
    for j in proj.GetRenderJobList() or []:
        row = {k: _plain(v) for k, v in j.items()}
        row["status"] = _plain(proj.GetRenderJobStatus(j.get("JobId")))
        out.append(row)
    return out


@_tool
def start_render(jobs: list[str] | None = None) -> str:
    """Start rendering the given queued jobs, or the whole queue."""
    _, proj = _project()
    ok = proj.StartRendering(jobs, isInteractiveMode=False) if jobs else proj.StartRendering(isInteractiveMode=False)
    if not ok:
        raise ToolError("rendering did not start (empty queue, unknown job id, or a render already running)")
    return f"rendering {len(jobs)} job(s)" if jobs else "rendering the whole queue"


@_tool
def delete_render_jobs(jobs: list[str] | None = None) -> str:
    """Remove the given jobs from the render queue, or all of them. Refused while a render is running."""
    _, proj = _project()
    if proj.IsRenderingInProgress():
        raise ToolError("a render is running: stop_render first")
    if jobs is None:
        if not proj.DeleteAllRenderJobs():
            raise ToolError("DeleteAllRenderJobs failed")
        return "render queue cleared"
    failed = [j for j in jobs if not proj.DeleteRenderJob(j)]
    if failed:
        raise ToolError(f"unknown render job(s): {', '.join(failed)}")
    return f"deleted {len(jobs)} render job(s)"


@_tool
def save_render_preset(name: str) -> str:
    """Save the Deliver page's current render settings as a new render preset."""
    _, proj = _project()
    if not proj.SaveAsNewRenderPreset(name):
        raise ToolError(f"cannot save render preset (name taken?): {name}")
    return f"saved render preset '{name}'"


@_tool
def list_render_formats() -> dict:
    """Render formats by id (the value `render` takes, e.g. mov, mp4, mxf_op1a) with their display name and codecs
    ({codec id: description}). A format with no codecs cannot be selected through the API (e.g. wav)."""
    _, proj = _project()
    # GetRenderFormats is {display name: id} and GetRenderCodecs {description: id}; Resolve only accepts the ids.
    return {
        fmt_id: {"name": name, "codecs": {cid: desc for desc, cid in (proj.GetRenderCodecs(fmt_id) or {}).items()}}
        for name, fmt_id in (proj.GetRenderFormats() or {}).items()
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
    """Export a video item's grade as a .cube LUT (size 17, 33 or 65 points). Needs Resolve 18 or later.
    Switches to the Color page for the export (Resolve refuses it elsewhere) and back."""
    resolve = _resolve()
    _, tl = _timeline()
    it = _item(tl, item, track)
    kind = {17: "EXPORT_LUT_17PTCUBE", 33: "EXPORT_LUT_33PTCUBE", 65: "EXPORT_LUT_65PTCUBE"}.get(size)
    if not kind:
        raise ToolError(f"unsupported LUT size: {size} (17, 33 or 65)")
    path = os.path.abspath(path)
    with _on_page(resolve, "color"):  # ExportLUT returns False on every other page
        ok = it.ExportLUT(getattr(resolve, kind), path)
    if not ok:
        raise ToolError(f"ExportLUT failed: {path}")
    return f"exported {size}-point LUT of '{it.GetName()}' to {path}"


MAGIC_MASK_DIRECTIONS = {"forward": "F", "backward": "B", "both": "BI"}


@_tool
def magic_mask(item: int, direction: str = "both", regenerate: bool = False, track: int = 1) -> str:
    """Track the Magic Mask of a video item through the shot: forward, backward or both from the current frame;
    regenerate=True re-tracks an existing mask. Studio only.
    The API cannot place the subject clicks Magic Mask needs, so it only tracks a mask already seeded in the UI:
    Color page → select the clip's node → Magic Mask palette → click the subject; then call this.
    Check the result with view_frame."""
    if direction not in MAGIC_MASK_DIRECTIONS:
        raise ToolError(f"direction must be one of: {', '.join(MAGIC_MASK_DIRECTIONS)}")
    _, tl = _timeline()
    it = _item(tl, item, track)
    if regenerate:
        if not _method(it, "RegenerateMagicMask", "18.5")():
            raise ToolError(f"'{it.GetName()}' has no Magic Mask to regenerate: click the subject in the Color page "
                            "Magic Mask palette first")
        return f"regenerated Magic Mask on '{it.GetName()}'"
    if not _method(it, "CreateMagicMask", "18.5")(MAGIC_MASK_DIRECTIONS[direction]):
        raise ToolError(f"no Magic Mask tracked on '{it.GetName()}': the API cannot click the subject. In the Color "
                        "page select the clip, open the Magic Mask palette, click the subject, then call magic_mask "
                        "again (Studio only)")
    return f"tracked Magic Mask {direction} on '{it.GetName()}'"


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
    """Delete items (1-based indexes, clips or transitions) from a track. ripple=True closes the gaps.
    Linked audio is not deleted with its video: pass the audio items too (track_type="audio")."""
    resolve = _resolve()
    _, tl = _timeline()
    all_items = tl.GetItemListInTrack(track_type, track) or []
    bad = [i for i in items if not 1 <= i <= len(all_items)]
    if bad or not items:
        raise ToolError(f"items not found on {track_type} track {track}: {bad or 'none given'}")
    with _on_page(resolve, "edit"):  # DeleteClips returns False on the Fairlight page, every time
        ok = tl.DeleteClips([all_items[i - 1] for i in items], ripple)
    if not ok:
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



# --- Audio / Fairlight ---
#
# The API has no per-parameter mixer: clip/track volume, pan, EQ, automation and FairlightFX cannot be read or
# set (SetProperty('Volume'|'Level'|'Gain') returns False on live 21.0). What it does offer is below: whole-mix
# Fairlight presets (20.2.2+), loudness normalization, fades and speed (21.1+), track management, voice
# isolation, audio sync, transcription and auto captions. AutoSyncAudio and CreateSubtitlesFromAudio take
# enum constants and report success unreliably, so both are verified by reading the result back.


def _track_items(tl, track_type, track):
    if track_type not in ("video", "audio", "subtitle"):
        raise ToolError(f"unknown track type: {track_type} (video, audio or subtitle)")
    return list(tl.GetItemListInTrack(track_type, track) or [])


def _pick(tl, item, track, track_type):
    items = _track_items(tl, track_type, track)
    if not 1 <= item <= len(items):
        raise ToolError(f"item {item} not found on {track_type} track {track}")
    return items[item - 1]


def _check_track(tl, track_type, index):
    count = int(tl.GetTrackCount(track_type) or 0) if track_type in ("video", "audio", "subtitle") else 0
    if not 1 <= index <= count:
        raise ToolError(f"{track_type} track {index} not found ({count} {track_type} track(s))")


def _constant(resolve, name):
    value = getattr(resolve, name, None)
    if value is None:
        raise ToolError(f"this Resolve version has no {name}")
    return value


def _pool_clips(proj, names):
    by_name = _clips_by_name(proj)
    missing = [n for n in names if n not in by_name]
    if missing:
        raise ToolError(f"clips not in media pool: {', '.join(missing)} (see list_clips)")
    return [by_name[n] for n in names]


@_tool
def fairlight_info() -> dict:
    """Audio state of the current timeline: each audio track's name, format (mono, stereo, 5.1...), enabled,
    locked and voice isolation, plus the Fairlight presets and loudness-normalization modes this Resolve offers."""
    resolve = _resolve()
    _, tl = _timeline()
    tracks = []
    for n in range(1, int(tl.GetTrackCount("audio") or 0) + 1):
        tracks.append({
            "index": n,
            "name": _opt(tl, "GetTrackName", "audio", n),
            "format": _opt(tl, "GetTrackSubType", "audio", n),
            "enabled": _opt(tl, "GetIsTrackEnabled", "audio", n),
            "locked": _opt(tl, "GetIsTrackLocked", "audio", n),
            "voice_isolation": _opt(tl, "GetVoiceIsolationState", n),
            "items": len(tl.GetItemListInTrack("audio", n) or []),
        })
    return {
        "tracks": tracks,
        "fairlight_presets": _opt(resolve, "GetFairlightPresets"),
        "normalize_modes": _opt(tl, "GetNormalizeAudioModes"),
    }


@_tool
def apply_fairlight_preset(name: str) -> str:
    """Apply a saved Fairlight preset (a whole mix: levels, EQ, dynamics, bussing) to the current timeline
    (Resolve 20.2.2+). Save the preset once in the Fairlight page; names are listed by fairlight_info."""
    _, proj = _project()
    _timeline()
    if not _method(proj, "ApplyFairlightPresetToCurrentTimeline", "20.2.2")(name):
        raise ToolError(f"cannot apply Fairlight preset: {name} (see fairlight_info)")
    return f"applied Fairlight preset '{name}'"


AUDIO_FORMATS = ("mono", "stereo", "5.1", "7.1") + tuple(f"adaptive{n}" for n in range(1, 37))


@_tool
def add_track(track_type: str = "audio", format: str = "stereo", name: str | None = None) -> dict:
    """Add a track at the end. Audio tracks take a format: mono, stereo, 5.1, 7.1 or adaptive1..adaptive36."""
    _, tl = _timeline()
    if track_type not in ("video", "audio", "subtitle"):
        raise ToolError(f"unknown track type: {track_type} (video, audio or subtitle)")
    if track_type == "audio" and format not in AUDIO_FORMATS:
        raise ToolError(f"unknown audio format: {format} (mono, stereo, 5.1, 7.1, adaptive1..adaptive36)")
    before = int(tl.GetTrackCount(track_type) or 0)
    if track_type != "audio":
        tl.AddTrack(track_type)
    else:
        # Newer builds take {audioType}; older ones a plain sub-type string.
        try:
            tl.AddTrack("audio", {"audioType": format})
        except TypeError:
            pass
        if int(tl.GetTrackCount("audio") or 0) == before:
            tl.AddTrack("audio", format)
    index = int(tl.GetTrackCount(track_type) or 0)
    if index != before + 1:
        raise ToolError(f"could not add {track_type} track")
    if name and not tl.SetTrackName(track_type, index, name):
        raise ToolError(f"added {track_type} track {index} but could not name it")
    return {"track_type": track_type, "index": index, "name": _opt(tl, "GetTrackName", track_type, index),
            "format": _opt(tl, "GetTrackSubType", track_type, index) if track_type == "audio" else None}


@_tool
def set_track(
    track_type: str, index: int, name: str | None = None, enabled: bool | None = None, locked: bool | None = None
) -> dict:
    """Rename, enable/disable (a disabled audio track is muted in playback and render) or lock/unlock a track."""
    _, tl = _timeline()
    _check_track(tl, track_type, index)
    if name is None and enabled is None and locked is None:
        raise ToolError("nothing to change: give name, enabled or locked")
    if name is not None and not tl.SetTrackName(track_type, index, name):
        raise ToolError("SetTrackName failed")
    if enabled is not None and not tl.SetTrackEnable(track_type, index, enabled):
        raise ToolError("SetTrackEnable failed")
    if locked is not None and not tl.SetTrackLock(track_type, index, locked):
        raise ToolError("SetTrackLock failed")
    return {
        "track_type": track_type,
        "index": index,
        "name": _opt(tl, "GetTrackName", track_type, index),
        "enabled": _opt(tl, "GetIsTrackEnabled", track_type, index),
        "locked": _opt(tl, "GetIsTrackLocked", track_type, index),
    }


@_tool
def delete_track(track_type: str, index: int) -> str:
    """Delete a track and everything on it. Tracks after it move up one index."""
    _, tl = _timeline()
    _check_track(tl, track_type, index)
    count = len(tl.GetItemListInTrack(track_type, index) or [])
    if not tl.DeleteTrack(track_type, index):
        raise ToolError("DeleteTrack failed")
    return f"deleted {track_type} track {index} ({count} item(s))"


@_tool
def voice_isolation(track: int, enabled: bool = True, amount: int = 50) -> dict:
    """Turn Voice Isolation on an audio track on or off, with strength 0-100 (removes background noise, music
    and room sound behind dialogue). Studio feature."""
    if not 0 <= amount <= 100:
        raise ToolError("amount must be 0-100")
    _, tl = _timeline()
    _check_track(tl, "audio", track)
    if not _method(tl, "SetVoiceIsolationState", "18.5")(track, {"isEnabled": enabled, "amount": amount}):
        raise ToolError("SetVoiceIsolationState failed (Studio only)")
    return {"track": track, "state": _opt(tl, "GetVoiceIsolationState", track)}


@_tool
def normalize_audio(
    items: list[int],
    loudness: float | None = None,
    level: float | None = None,
    mode: str | None = None,
    independent: bool = False,
    track: int = 1,
) -> str:
    """Normalize audio items (1-based indexes on an audio track) to a target (Resolve 21.1+): `loudness` in
    LKFS/LUFS (e.g. -14 web/YouTube, -16 podcasts, -23 EBU R128 broadcast, -24 ATSC) or peak `level` in dBFS.
    mode is one of fairlight_info's normalize_modes (Resolve's default when omitted). independent=True
    normalizes each item on its own instead of keeping their relative levels."""
    if loudness is None and level is None:
        raise ToolError("give a target: loudness (LKFS) or level (dBFS)")
    resolve = _resolve()
    _, tl = _timeline()
    normalize = _method(tl, "NormalizeAudioLevel", "21.1")
    targets = [_pick(tl, i, track, "audio") for i in items]
    if not targets:
        raise ToolError("no items given")
    modes = _opt(tl, "GetNormalizeAudioModes")
    if mode is not None and modes and mode not in modes:
        raise ToolError(f"unknown mode: {mode} (one of {', '.join(map(str, modes))})")
    options = {"setLevelMode": _constant(resolve, "NORMALIZE_AUDIO_SET_LEVEL_" + ("INDEPENDENT" if independent else "RELATIVE"))}
    if mode is not None:
        options["normalizationMode"] = mode
    if loudness is not None:
        options["targetLoudness"] = float(loudness)
    if level is not None:
        options["targetLevel"] = float(level)
    if not normalize(targets, options):
        raise ToolError("NormalizeAudioLevel failed")
    return f"normalized {len(targets)} item(s)" + (f" to {loudness} LKFS" if loudness is not None else f" to {level} dBFS")


@_tool
def set_fades(
    item: int, fade_in: int | None = None, fade_out: int | None = None, track: int = 1, track_type: str = "audio"
) -> dict:
    """Set an item's fade-in/fade-out length in frames (0 removes the fade). Works on audio and video items
    (Resolve 21.1+)."""
    if fade_in is None and fade_out is None:
        raise ToolError("give fade_in and/or fade_out")
    if any(v is not None and v < 0 for v in (fade_in, fade_out)):
        raise ToolError("fades must be 0 or more frames")
    _, tl = _timeline()
    it = _pick(tl, item, track, track_type)
    fades = {k: v for k, v in (("FadeIn", fade_in), ("FadeOut", fade_out)) if v is not None}
    if not _method(it, "SetFades", "21.1")(fades):
        raise ToolError("SetFades failed (fade longer than the clip?)")
    return {"item": it.GetName(), "fades": _opt(it, "GetFades")}


@_tool
def set_speed(
    item: int,
    percent: float,
    pitch_correction: bool | None = None,
    stretch_keyframes: bool | None = None,
    ripple: bool = False,
    track: int = 1,
    track_type: str = "video",
) -> dict:
    """Change an item's speed (Resolve 21.1+): 50 = half speed, 200 = double, 0 = freeze frame. ripple=True
    moves later items to fit; pitch_correction keeps voices natural. Set RetimeProcess (e.g. optical_flow)
    with set_item_properties for smooth slow motion."""
    if percent < 0:
        raise ToolError("percent must be 0 or more")
    _, tl = _timeline()
    it = _pick(tl, item, track, track_type)
    options = {"Percentage": percent, "RippleTimeline": ripple}
    if pitch_correction is not None:
        options["PitchCorrection"] = pitch_correction
    if stretch_keyframes is not None:
        options["StretchKeyframesToFit"] = stretch_keyframes
    if not _method(it, "SetSpeed", "21.1")(options):
        raise ToolError("SetSpeed failed")
    return {"item": it.GetName(), "speed": _opt(it, "GetSpeed"), "duration": it.GetDuration()}


@_tool
def convert_to_stereo() -> str:
    """Convert the whole current timeline's audio to stereo."""
    _, tl = _timeline()
    if not tl.ConvertTimelineToStereo():
        raise ToolError("ConvertTimelineToStereo failed")
    return "timeline converted to stereo"


@_tool
def insert_audio(path: str, start_offset: int = 0, duration: int = 0) -> str:
    """Insert an audio file at the playhead on the selected track of the Fairlight page (open_page("fairlight")
    first). start_offset/duration are in samples into the file; duration 0 inserts to the end."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ToolError(f"file not found: {path}")
    _, proj = _project()
    _timeline()
    if not proj.InsertAudioToCurrentTrackAtPlayhead(path, start_offset, duration):
        raise ToolError("insert failed (is the Fairlight page open with a track selected?)")
    return f"inserted {os.path.basename(path)} at the playhead"


SYNC_CHANNELS = {"auto": "AUDIO_SYNC_CHANNEL_AUTOMATIC", "mix": "AUDIO_SYNC_CHANNEL_MIX"}


@_tool
def sync_audio(
    clips: list[str],
    method: str = "waveform",
    channel: str | int = "auto",
    retain_embedded_audio: bool = False,
    retain_video_metadata: bool = False,
) -> dict:
    """Sync separately recorded audio to video in the media pool (at least one video and one audio clip, by
    name). method: waveform or timecode; channel: auto, mix or a channel number. Checks each clip afterwards,
    since Resolve's own success flag is unreliable."""
    if method not in ("waveform", "timecode"):
        raise ToolError("method must be waveform or timecode")
    if len(clips) < 2:
        raise ToolError("need at least one video and one audio clip")
    resolve = _resolve()
    _, proj = _project()
    targets = _pool_clips(proj, clips)
    if isinstance(channel, str):
        if channel not in SYNC_CHANNELS:
            raise ToolError("channel must be auto, mix or a channel number")
        channel = _constant(resolve, SYNC_CHANNELS[channel])
    settings = {
        _constant(resolve, "AUDIO_SYNC_MODE"): _constant(resolve, "AUDIO_SYNC_" + method.upper()),
        _constant(resolve, "AUDIO_SYNC_CHANNEL_NUMBER"): channel,
        _constant(resolve, "AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO"): retain_embedded_audio,
        _constant(resolve, "AUDIO_SYNC_RETAIN_VIDEO_METADATA"): retain_video_metadata,
    }
    returned = bool(proj.GetMediaPool().AutoSyncAudio(targets, settings))
    synced = {c.GetName(): c.GetClipProperty("Synced Audio") or None for c in targets}
    if not any(synced.values()):
        raise ToolError("no clip was synced (check the clips overlap in timecode or share audible sound)")
    return {"resolve_reported": returned, "synced_audio": synced}


@_tool
def transcribe_audio(clips: list[str], speaker_detection: bool = False) -> list[dict]:
    """Transcribe media-pool clips (by name) so text-based editing and captions can use them (Studio).
    speaker_detection labels who is speaking."""
    _, proj = _project()
    out = []
    for c in _pool_clips(proj, clips):
        ok = bool(c.TranscribeAudio(speaker_detection))
        out.append({"clip": c.GetName(), "transcribed": ok, "preview": c.GetClipProperty("Transcription") or None})
    if not any(r["transcribed"] for r in out):
        raise ToolError("transcription failed (Studio only)")
    return out


CAPTION_LANGUAGES = (
    "auto", "danish", "dutch", "english", "french", "german", "italian", "japanese", "korean",
    "mandarin_simplified", "mandarin_traditional", "norwegian", "portuguese", "russian", "spanish", "swedish",
)
CAPTION_PRESETS = {"default": "AUTO_CAPTION_SUBTITLE_DEFAULT", "teletext": "AUTO_CAPTION_TELETEXT", "netflix": "AUTO_CAPTION_NETFLIX"}


@_tool
def create_subtitles(
    language: str = "auto",
    preset: str = "default",
    lines: int = 1,
    chars_per_line: int | None = None,
    gap: int | None = None,
) -> dict:
    """Generate a subtitle track from the timeline's dialogue (Studio). language: auto or one of Resolve's
    caption languages; preset: default, teletext or netflix; lines: 1 or 2 per caption; chars_per_line 1-60;
    gap 0-10 frames between captions. Verified by the subtitle track count."""
    if language not in CAPTION_LANGUAGES:
        raise ToolError(f"unsupported caption language: {language} (one of {', '.join(CAPTION_LANGUAGES)})")
    if preset not in CAPTION_PRESETS:
        raise ToolError(f"unknown preset: {preset} (default, teletext or netflix)")
    if lines not in (1, 2):
        raise ToolError("lines must be 1 or 2")
    if chars_per_line is not None and not 1 <= chars_per_line <= 60:
        raise ToolError("chars_per_line must be 1-60")
    if gap is not None and not 0 <= gap <= 10:
        raise ToolError("gap must be 0-10 frames")
    resolve = _resolve()
    _, tl = _timeline()
    settings = {
        _constant(resolve, "SUBTITLE_LANGUAGE"): _constant(resolve, "AUTO_CAPTION_" + language.upper()),
        _constant(resolve, "SUBTITLE_CAPTION_PRESET"): _constant(resolve, CAPTION_PRESETS[preset]),
        _constant(resolve, "SUBTITLE_LINE_BREAK"): _constant(resolve, "AUTO_CAPTION_LINE_" + ("SINGLE" if lines == 1 else "DOUBLE")),
    }
    if chars_per_line is not None:
        settings[_constant(resolve, "SUBTITLE_CHARS_PER_LINE")] = chars_per_line
    if gap is not None:
        settings[_constant(resolve, "SUBTITLE_GAP")] = gap
    before = int(tl.GetTrackCount("subtitle") or 0)
    returned = bool(tl.CreateSubtitlesFromAudio(settings))
    after = int(tl.GetTrackCount("subtitle") or 0)
    if after <= before:
        raise ToolError("no subtitle track was created (Studio only; the timeline needs audible dialogue)")
    return {"resolve_reported": returned, "subtitle_track": after,
            "captions": len(tl.GetItemListInTrack("subtitle", after) or [])}



@_tool
def link_mask_to_tracker(
    item: int,
    mask: str,
    tracker: str,
    tracker_index: int = 1,
    offset: list[float] = [0.0, 0.0],
    unlink: bool = False,
    comp: int = 1,
    track: int = 1,
) -> dict:
    """Make a Fusion node's Center (a mask, Text+, Transform...) follow a Tracker node's tracked point, through a
    Fusion expression. offset ([dx, dy], 0-1 image space) shifts it from the tracked point; unlink=True removes
    the link. The track itself must be run in the Fusion page (Track Forward); the API cannot start it.
    EXPERIMENTAL: not yet confirmed on a live Resolve."""
    _, c = _comp(item, comp, track)
    target, trk = _node(c, mask), _node(c, tracker)
    if _tool_attrs(trk)[1] != "Tracker":
        raise ToolError(f"{tracker} is a {_tool_attrs(trk)[1]}, not a Tracker")
    center = target["Center"]
    if not center or (center.GetAttrs() or {}).get("INPS_DataType") != "Point":
        raise ToolError(f"{mask} has no Center point input")
    with _undo(c, f"{mask}.Center"):
        if unlink:
            center.SetExpression(None)
            return {"node": mask, "expression": None}
        path_id = f"TrackedCenter{tracker_index}"
        path = trk[path_id]
        if not path:
            points = [(i.GetAttrs() or {}).get("INPS_ID") for i in (trk.GetInputList() or {}).values()
                      if (i.GetAttrs() or {}).get("INPS_DataType") == "Point"]
            raise ToolError(f"{tracker} has no {path_id}; its point inputs are: {', '.join(points) or 'none'}")
        if _source(path)[1] not in ANIMATION_MODIFIERS:
            raise ToolError(f"{tracker} has no tracked data for tracker {tracker_index}: run Track Forward on it in the "
                            "Fusion page first")
        dx, dy = (float(v) for v in _xy(offset))
        ref = f"{tracker}.{path_id}"
        expr = ref if dx == dy == 0 else f"Point({ref}.X + {dx}, {ref}.Y + {dy})"
        center.SetExpression(expr)
    return {"node": mask, "expression": expr}


def _xy(v):
    if len(v) != 2:
        raise ToolError(f"offset needs [dx, dy], got {v!r}")
    return v



# --- Media management ---
#
# Clips are addressed by name, as everywhere else in this server; bins by path ("Footage/Day 1").
# There is no API to generate proxies or optimized media, only to link existing proxy files.

CLIP_COLORS = (
    "Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy",
    "Blue", "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate",
)


@_tool
def browse_storage(path: str | None = None) -> dict:
    """Browse disks as Resolve sees them (Media Storage): without `path`, the mounted volumes; with a folder path,
    its subfolders and files (image sequences are listed as one entry)."""
    storage = _resolve().GetMediaStorage()
    if not path:
        return {"volumes": list(storage.GetMountedVolumeList() or [])}
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise ToolError(f"folder not found: {path}")
    return {"path": path, "folders": list(storage.GetSubFolderList(path) or []),
            "files": list(storage.GetFileList(path) or [])}


@_tool
def create_bin(name: str, parent: str = "/") -> str:
    """Create a media-pool bin under `parent` ("/" is the root, or a path like "Footage")."""
    _, proj = _project()
    folder = _bin(proj, parent)
    if any(f.GetName() == name for f in folder.GetSubFolderList() or []):
        raise ToolError(f"bin already exists: {name}")
    if not proj.GetMediaPool().AddSubFolder(folder, name):
        raise ToolError(f"could not create bin: {name}")
    return f"created bin {parent.rstrip('/')}/{name}"


@_tool
def move_clips(clips: list[str], bin: str) -> str:
    """Move media-pool clips (by name) into a bin (path, e.g. "Footage/Day 1")."""
    _, proj = _project()
    targets, folder = _pool_clips(proj, clips), _bin(proj, bin)
    if not proj.GetMediaPool().MoveClips(targets, folder):
        raise ToolError("MoveClips failed")
    return f"moved {len(targets)} clip(s) to {bin}"


@_tool
def delete_clips(clips: list[str]) -> str:
    """Delete clips from the media pool (by name). Their uses on timelines go offline; files on disk are kept."""
    _, proj = _project()
    targets = _pool_clips(proj, clips)
    if not proj.GetMediaPool().DeleteClips(targets):
        raise ToolError("DeleteClips failed")
    return f"deleted {len(targets)} clip(s) from the media pool"


@_tool
def import_image_sequence(pattern: str, start: int, end: int, bin: str | None = None) -> str:
    """Import an image sequence as one clip. pattern is printf-style, e.g. /renders/shot_%04d.exr, with the first and
    last frame numbers."""
    if end < start:
        raise ToolError("end must be >= start")
    pattern = os.path.abspath(pattern)
    first = pattern % start if "%" in pattern else None
    if not first or not os.path.exists(first):
        raise ToolError(f"first frame not found: {first or pattern} (pattern needs a %0Nd placeholder)")
    _, proj = _project()
    pool = proj.GetMediaPool()
    with _in_bin(pool, _bin(proj, bin) if bin else pool.GetCurrentFolder()):
        items = pool.ImportMedia([{"FilePath": pattern, "StartIndex": start, "EndIndex": end}])
    if not items:
        raise ToolError("import failed")
    return f"imported {items[0].GetName()}"


@_tool
def clip_info(clip: str) -> dict:
    """Everything Resolve knows about a media-pool clip: clip properties (resolution, fps, codec, duration, file
    path, reel, timecode, proxy...), metadata, clip color, flags and markers."""
    _, proj = _project()
    (c,) = _pool_clips(proj, [clip])
    props = {k: v for k, v in (c.GetClipProperty() or {}).items() if v not in ("", None)}
    return {
        "name": c.GetName(),
        "properties": props,
        "metadata": {k: v for k, v in (c.GetMetadata() or {}).items() if v not in ("", None)},
        "color": c.GetClipColor() or None,
        "flags": list(c.GetFlagList() or []),
        "markers": {str(k): v for k, v in (c.GetMarkers() or {}).items()},
    }


@_tool
def tag_clips(
    clips: list[str],
    color: str | None = None,
    flag: str | None = None,
    clear_flags: bool = False,
    metadata: dict | None = None,
) -> list[dict]:
    """Organize media-pool clips: set a clip color (Orange, Apricot, Yellow, Lime, Olive, Green, Teal, Navy, Blue,
    Purple, Violet, Pink, Tan, Beige, Brown, Chocolate; "" clears it), add a flag, clear flags, and write metadata
    (e.g. {"Scene": "12", "Take": "3", "Keywords": "interview", "Comments": "..."}). Metadata is read back and a
    value Resolve did not keep is an error."""
    if color and color not in CLIP_COLORS:
        raise ToolError(f"unknown clip color: {color} (one of {', '.join(CLIP_COLORS)})")
    if color is None and flag is None and not clear_flags and not metadata:
        raise ToolError("nothing to change")
    _, proj = _project()
    out = []
    for c in _pool_clips(proj, clips):
        if color == "":
            c.ClearClipColor()
        elif color and not c.SetClipColor(color):
            raise ToolError(f"SetClipColor failed on {c.GetName()}")
        if clear_flags:
            c.ClearFlags("All")
        if flag and not c.AddFlag(flag):
            raise ToolError(f"unknown flag color: {flag}")
        if metadata:
            c.SetMetadata({k: str(v) for k, v in metadata.items()})
            # Some fields (e.g. Reel Name under automatic reel naming) return True but are not kept.
            lost = [k for k, v in metadata.items() if str(c.GetMetadata(k) or "") != str(v)]
            if lost:
                raise ToolError(f"Resolve did not keep {', '.join(lost)} on {c.GetName()} (check project settings)")
        out.append({"clip": c.GetName(), "color": c.GetClipColor() or None, "flags": list(c.GetFlagList() or [])})
    return out


@_tool
def relink_clips(clips: list[str], folder: str) -> str:
    """Relink offline media-pool clips to files in `folder` (searched by file name)."""
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ToolError(f"folder not found: {folder}")
    _, proj = _project()
    if not proj.GetMediaPool().RelinkClips(_pool_clips(proj, clips), folder):
        raise ToolError(f"RelinkClips failed (no matching files in {folder}?)")
    return f"relinked {len(clips)} clip(s) to {folder}"


@_tool
def link_proxy(clip: str, proxy_path: str | None = None) -> str:
    """Attach an existing proxy file to a media-pool clip, or detach it (proxy_path omitted). Resolve cannot generate
    proxies through the API; render them in the UI or externally."""
    _, proj = _project()
    (c,) = _pool_clips(proj, [clip])
    if proxy_path is None:
        if not c.UnlinkProxyMedia():
            raise ToolError(f"{clip} has no proxy to unlink")
        return f"unlinked proxy of {clip}"
    proxy_path = os.path.abspath(proxy_path)
    if not os.path.exists(proxy_path):
        raise ToolError(f"file not found: {proxy_path}")
    if not c.LinkProxyMedia(proxy_path):
        raise ToolError("LinkProxyMedia failed (proxy must match the clip's duration and frame rate)")
    return f"linked proxy {os.path.basename(proxy_path)} to {clip}"


@_tool
def replace_clip(clip: str, path: str) -> str:
    """Swap a media-pool clip's underlying file for another (e.g. a VFX shot's new version); every use on timelines
    follows."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ToolError(f"file not found: {path}")
    _, proj = _project()
    (c,) = _pool_clips(proj, [clip])
    if not c.ReplaceClip(path):
        raise ToolError("ReplaceClip failed")
    return f"{clip} now uses {os.path.basename(path)}"


@_tool
def export_metadata(path: str, clips: list[str] | None = None) -> str:
    """Write clip metadata to a CSV file: the given clips, or the whole media pool."""
    _, proj = _project()
    path = os.path.abspath(path)
    targets = _pool_clips(proj, clips) if clips else []
    if not proj.GetMediaPool().ExportMetadata(path, targets) or not os.path.exists(path):
        raise ToolError(f"ExportMetadata failed: {path}")
    return f"metadata of {len(targets) or 'all'} clip(s) written to {path}"


# --- Interchange and project ---

# name -> (exportType, exportSubtype). Constants are resolved on the live handle: plain strings are silently rejected.
TIMELINE_EXPORTS = {
    "aaf": ("EXPORT_AAF", "EXPORT_AAF_NEW"),
    "aaf_existing": ("EXPORT_AAF", "EXPORT_AAF_EXISTING"),
    "drt": ("EXPORT_DRT", "EXPORT_NONE"),
    "edl": ("EXPORT_EDL", "EXPORT_NONE"),
    "edl_cdl": ("EXPORT_EDL", "EXPORT_CDL"),
    "edl_sdl": ("EXPORT_EDL", "EXPORT_SDL"),
    "edl_missing_clips": ("EXPORT_EDL", "EXPORT_MISSING_CLIPS"),
    "fcp7_xml": ("EXPORT_FCP_7_XML", "EXPORT_NONE"),
    "otio": ("EXPORT_OTIO", "EXPORT_NONE"),
    "csv": ("EXPORT_TEXT_CSV", "EXPORT_NONE"),
    "tab": ("EXPORT_TEXT_TAB", "EXPORT_NONE"),
    "hdr10_a": ("EXPORT_HDR_10_PROFILE_A", "EXPORT_NONE"),
    "hdr10_b": ("EXPORT_HDR_10_PROFILE_B", "EXPORT_NONE"),
    "dolby_vision_2_9": ("EXPORT_DOLBY_VISION_VER_2_9", "EXPORT_NONE"),
    "dolby_vision_4_0": ("EXPORT_DOLBY_VISION_VER_4_0", "EXPORT_NONE"),
    "dolby_vision_5_1": ("EXPORT_DOLBY_VISION_VER_5_1", "EXPORT_NONE"),
}
FCPXML_VERSIONS = [f"1_{n}" for n in range(3, 12)]
TIMELINE_EXPORTS.update({f"fcpxml_{v}": (f"EXPORT_FCPXML_{v}", "EXPORT_NONE") for v in FCPXML_VERSIONS})


@_tool
def export_timeline(path: str, format: str) -> dict:
    """Export the current timeline for another app or a conform: aaf / aaf_existing (Avid, Pro Tools), fcpxml
    (newest version this Resolve writes) or fcpxml_1_3..fcpxml_1_11, fcp7_xml (Premiere), otio (OpenTimelineIO),
    edl / edl_cdl / edl_sdl / edl_missing_clips, drt (Resolve timeline), csv / tab (edit list), hdr10_a / hdr10_b,
    dolby_vision_2_9 / 4_0 / 5_1. Checks the file was written."""
    resolve = _resolve()
    _, tl = _timeline()
    if format == "fcpxml":
        newest = next((v for v in reversed(FCPXML_VERSIONS) if getattr(resolve, f"EXPORT_FCPXML_{v}", None) is not None),
                      None)
        if not newest:
            raise ToolError("this Resolve version exports no FCPXML")
        format = f"fcpxml_{newest}"
    if format not in TIMELINE_EXPORTS:
        raise ToolError(f"unknown format: {format} (one of fcpxml, {', '.join(TIMELINE_EXPORTS)})")
    kind, sub = (_constant(resolve, name) for name in TIMELINE_EXPORTS[format])
    path = os.path.abspath(path)
    if not os.path.isdir(os.path.dirname(path)):
        raise ToolError(f"folder not found: {os.path.dirname(path)}")
    if not tl.Export(path, kind, sub):
        raise ToolError(f"Timeline.Export failed for {format}")
    if not os.path.exists(path):
        raise ToolError(f"Resolve reported success but wrote no file at {path}")
    return {"timeline": tl.GetName(), "format": format, "path": path, "bytes": os.path.getsize(path)}


@_tool
def import_timeline(
    path: str, name: str | None = None, import_source_clips: bool = True, source_clips_path: str | None = None
) -> dict:
    """Import a timeline from AAF, EDL, FCP7 XML, FCPXML, OTIO or DRT and make it current. source_clips_path is where
    to look for media missing from its original location. The file's own sequence name wins for FCP7 XML and
    DRT, so the returned name can differ from `name`."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ToolError(f"file not found: {path}")
    _, proj = _project()
    options = {"importSourceClips": import_source_clips}
    if name:
        if any(proj.GetTimelineByIndex(i).GetName() == name for i in range(1, int(proj.GetTimelineCount()) + 1)):
            raise ToolError(f"a timeline named {name} already exists")
        options["timelineName"] = name
    if source_clips_path:
        options["sourceClipsPath"] = os.path.abspath(source_clips_path)
    before = int(proj.GetTimelineCount())
    tl = proj.GetMediaPool().ImportTimelineFromFile(path, options)
    if not tl:
        raise ToolError("ImportTimelineFromFile failed (unsupported file, or its timeline name already exists)")
    # FCP7 XML returns the EXISTING timeline when its internal sequence name is already taken.
    if int(proj.GetTimelineCount()) == before:
        raise ToolError(f"no new timeline: the file's sequence name matches the existing timeline '{tl.GetName()}'")
    proj.SetCurrentTimeline(tl)
    return {"timeline": tl.GetName(), "renamed_by_file": bool(name) and tl.GetName() != name}


@_tool
def save_project() -> str:
    """Save the current project."""
    pm = _resolve().GetProjectManager()
    _, proj = _project()
    if proj.GetName() == UNTITLED:
        raise ToolError("the default Untitled Project cannot be saved from a script (Resolve needs a Save As "
                        "dialog); use create_project to work in a named project")
    if not pm.SaveProject():
        raise ToolError("SaveProject failed")
    return "project saved"


@_tool
def export_project(path: str, with_stills_and_luts: bool = True) -> dict:
    """Export the current project as a .drp file (for backup or moving to another database). Media is not included.
    Save first with save_project to include recent changes."""
    pm = _resolve().GetProjectManager()
    _, proj = _project()
    path = os.path.abspath(path)
    if not path.lower().endswith(".drp"):
        path += ".drp"
    if not pm.ExportProject(proj.GetName(), path, with_stills_and_luts) or not os.path.exists(path):
        raise ToolError(f"ExportProject failed: {path}")
    return {"project": proj.GetName(), "path": path, "bytes": os.path.getsize(path)}


@_tool
def import_project(path: str, name: str | None = None) -> str:
    """Import a .drp project file into the current project-manager folder (it is not opened; use open_project)."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ToolError(f"file not found: {path}")
    pm = _resolve().GetProjectManager()
    ok = pm.ImportProject(path, name) if name else pm.ImportProject(path)
    if not ok:
        raise ToolError("ImportProject failed (name already in use?)")
    return f"imported project {name or os.path.splitext(os.path.basename(path))[0]}"



# --- Color management and HDR ---
#
# Keys and documented values come from Resolve's typed API stub (ProjectSettings / TimelineSettings). Values are
# strings. Writes go in dependency order (color science, then RCM mode, then color spaces, then HDR) and every
# key is read back: Resolve can return True, or False, without applying a value, and some keys are read-only in
# some modes (e.g. color spaces while automatic color management is on).

COLOR_KEYS = (
    "colorScienceMode", "acesVersion", "isAutoColorManage", "rcmPresetMode", "separateColorSpaceAndGamma",
    "colorAcesIDT", "colorAcesODT", "colorAcesGamutCompressType", "colorAcesNodeLUTProcessingSpace", "colorAcesMidGray",
    "colorSpaceInput", "colorSpaceInputGamma", "colorSpaceTimeline", "colorSpaceTimelineGamma",
    "colorSpaceOutput", "colorSpaceOutputGamma", "timelineWorkingLuminanceMode", "timelineWorkingLuminance",
    "inputDRT", "outputDRT", "useInverseDRT", "colorSpaceOutputToneMapping", "colorSpaceOutputToneLuminanceMax",
    "colorSpaceOutputGamutMapping", "colorSpaceOutputGamutLimit", "colorSpaceOutputGamutSaturationKnee",
    "colorSpaceOutputGamutSaturationMax", "inputDRTSatRolloffStart", "inputDRTSatRolloffLimit",
    "outputDRTSatRolloffStart", "outputDRTSatRolloffLimit", "imageResizingGamma", "graphicsWhiteLevel",
    "useCATransform", "disableFusionToneMapping", "useColorSpaceAwareGradingTools",
)
HDR_KEYS = (
    "hdrMasteringOn", "hdrMasteringLuminanceMax", "hdrDolbyControlsOn", "hdrDolbyVersion", "hdrDolbyAnalysisTuning",
    "hdrDolbyMasterDisplay", "hdrDolbyUseExternalCMU", "hdr10PlusControlsOn", "hdrVividControlsOn",
    "hdrVividMasterDisplay",
)
SETTINGS_ORDER = (
    "colorScienceMode", "acesVersion", "isAutoColorManage", "rcmPresetMode", "separateColorSpaceAndGamma",
    "colorAcesIDT", "colorAcesNodeLUTProcessingSpace", "colorAcesODT", "colorSpaceInput", "colorSpaceInputGamma",
    "colorSpaceTimeline", "colorSpaceTimelineGamma", "colorSpaceOutput", "colorSpaceOutputGamma",
    "timelineWorkingLuminanceMode", "timelineWorkingLuminance", "hdrMasteringOn", "hdrMasteringLuminanceMax",
    "hdrDolbyControlsOn", "hdrDolbyVersion",
)
# Only values documented in the stub, so the presets hold on any 18+ build.
COLOR_PRESETS = {
    "yrgb": {"colorScienceMode": "davinciYRGB"},
    "rcm_sdr": {"colorScienceMode": "davinciYRGBColorManagedv2", "isAutoColorManage": "1", "rcmPresetMode": "SDR"},
    "rcm_hdr": {"colorScienceMode": "davinciYRGBColorManagedv2", "isAutoColorManage": "1", "rcmPresetMode": "HDR"},
    "rcm_custom": {"colorScienceMode": "davinciYRGBColorManagedv2", "isAutoColorManage": "0"},
    "aces_cct": {"colorScienceMode": "acescct"},
    "aces_cc": {"colorScienceMode": "acescc"},
}


def _setting_str(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _settings_target(timeline):
    """The project, or the current timeline switched to its own settings."""
    proj, tl = _timeline() if timeline else (_project()[1], None)
    if not timeline:
        return proj, "project"
    if str(tl.GetSetting("useCustomSettings")) != "1" and not tl.SetSetting("useCustomSettings", "1"):
        raise ToolError(f"cannot give timeline '{tl.GetName()}' its own settings")
    return tl, f"timeline '{tl.GetName()}'"


def _apply_settings(target, label, values):
    unknown = [k for k in values if k not in COLOR_KEYS + HDR_KEYS]
    if unknown:
        raise ToolError(f"not a color/HDR setting: {', '.join(unknown)} (see color_management_info)")
    order = [k for k in SETTINGS_ORDER if k in values] + [k for k in values if k not in SETTINGS_ORDER]
    applied, rejected = {}, {}
    for key in order:
        want = _setting_str(values[key])
        target.SetSetting(key, want)
        got = target.GetSetting(key)
        if str(got) == want:
            applied[key] = want
        else:
            rejected[key] = {"wanted": want, "is": got}
    if rejected:
        detail = "; ".join(f"{k}: wanted {v['wanted']!r}, is {v['is']!r}" for k, v in rejected.items())
        raise ToolError(f"{label}: Resolve did not apply {detail}. Applied: {applied or 'nothing'}. Names must match "
                        "Project Settings > Color Management exactly, and some keys are locked by the current mode.")
    return {"scope": label, "applied": applied}


@_tool
def color_management_info(timeline: bool = False) -> dict:
    """Color management and HDR settings of the project, or of the current timeline (timeline=True; a timeline
    without its own settings reports the project's). Empty values are omitted."""
    if timeline:
        _, tl = _timeline()
        custom = str(tl.GetSetting("useCustomSettings")) == "1"
        source, label = (tl if custom else _project()[1]), f"timeline '{tl.GetName()}'"
    else:
        source, label, custom = _project()[1], "project", None
    read = lambda keys: {k: v for k in keys if (v := source.GetSetting(k)) not in (None, "")}
    out = {"scope": label, "color": read(COLOR_KEYS), "hdr": read(HDR_KEYS)}
    if timeline:
        out["uses_own_settings"] = custom
    return out


@_tool
def set_color_management(settings: dict, timeline: bool = False) -> dict:
    """Set color management keys on the project, or on the current timeline only (timeline=True switches it to its
    own settings). Keys: colorScienceMode (davinciYRGB, davinciYRGBColorManaged, davinciYRGBColorManagedv2, acescc,
    acescct), isAutoColorManage, rcmPresetMode (SDR/HDR when automatic), separateColorSpaceAndGamma,
    colorSpaceInput/Timeline/Output (+Gamma), timelineWorkingLuminanceMode (e.g. "SDR 100", "HDR 1000"),
    inputDRT/outputDRT (None, Simple, Luminance Mapping, DaVinci, Saturation Preserving, RED IPP2), useInverseDRT,
    colorSpaceOutputGamutMapping, graphicsWhiteLevel, colorAcesIDT/ODT and the other keys color_management_info lists.
    Color space names are exactly as in Project Settings. Every key is read back; any not applied is an error."""
    if not settings:
        raise ToolError("no settings given")
    target, label = _settings_target(timeline)
    return _apply_settings(target, label, settings)


@_tool
def apply_color_preset(preset: str, timeline: bool = False) -> dict:
    """Switch the project (or current timeline) to a color workflow: yrgb (unmanaged DaVinci YRGB), rcm_sdr / rcm_hdr
    (DaVinci color managed, automatic SDR or HDR), rcm_custom (color managed, spaces set by hand with
    set_color_management), aces_cct, aces_cc."""
    if preset not in COLOR_PRESETS:
        raise ToolError(f"unknown preset: {preset} (one of {', '.join(COLOR_PRESETS)})")
    target, label = _settings_target(timeline)
    return {"preset": preset, **_apply_settings(target, label, COLOR_PRESETS[preset])}


DOLBY_TUNINGS = ("Legacy", "Most Mapping", "More Mapping", "Balanced", "Less Mapping", "Least Mapping")


@_tool
def set_hdr(
    mastering_nits: int | None = None,
    dolby_vision: str | None = None,
    dolby_tuning: str | None = None,
    dolby_master_display: str | None = None,
    hdr10_plus: bool | None = None,
    timeline: bool = False,
) -> dict:
    """HDR mastering for the project (or current timeline): mastering_nits enables HDR mastering at that peak
    (e.g. 1000, 4000; 0 turns it off); dolby_vision "2.9", "4.0" or "off" (Studio); dolby_tuning Legacy, Most
    Mapping, More Mapping, Balanced, Less Mapping or Least Mapping; dolby_master_display as named in Resolve;
    hdr10_plus on/off. Set the output color space (e.g. an ST2084 or HLG space) with set_color_management."""
    values = {}
    if mastering_nits is not None:
        if mastering_nits < 0:
            raise ToolError("mastering_nits must be 0 or more")
        values["hdrMasteringOn"] = mastering_nits > 0
        if mastering_nits:
            values["hdrMasteringLuminanceMax"] = mastering_nits
    if dolby_vision is not None:
        if dolby_vision not in ("2.9", "4.0", "off"):
            raise ToolError('dolby_vision must be "2.9", "4.0" or "off"')
        values["hdrDolbyControlsOn"] = dolby_vision != "off"
        if dolby_vision != "off":
            values["hdrDolbyVersion"] = dolby_vision
    if dolby_tuning is not None:
        if dolby_tuning not in DOLBY_TUNINGS:
            raise ToolError(f"dolby_tuning must be one of: {', '.join(DOLBY_TUNINGS)}")
        values["hdrDolbyAnalysisTuning"] = dolby_tuning
    if dolby_master_display is not None:
        values["hdrDolbyMasterDisplay"] = dolby_master_display
    if hdr10_plus is not None:
        values["hdr10PlusControlsOn"] = hdr10_plus
    if not values:
        raise ToolError("nothing to change")
    target, label = _settings_target(timeline)
    return _apply_settings(target, label, values)


@_tool
def set_clip_color_space(
    clips: list[str], color_space: str | None = None, gamma: str | None = None, idt: str | None = None
) -> list[dict]:
    """Tag media-pool clips (by name) with their source color space and gamma for color-managed projects (e.g. a
    log camera clip), or their ACES IDT in ACES projects. Names exactly as in Resolve's Input Color Space menu.
    Read back after writing."""
    props = {k: v for k, v in (("Input Color Space", color_space), ("Input Gamma", gamma), ("IDT", idt)) if v}
    if not props:
        raise ToolError("give color_space, gamma or idt")
    _, proj = _project()
    out = []
    for c in _pool_clips(proj, clips):
        for key, value in props.items():
            c.SetClipProperty(key, value)
            if c.GetClipProperty(key) != value:
                raise ToolError(f"{c.GetName()}: Resolve did not apply {key} = {value!r} (is {c.GetClipProperty(key)!r}; "
                                "the project must be color managed for Input Color Space, ACES for IDT)")
        out.append({"clip": c.GetName(), **{k: c.GetClipProperty(k) for k in props}})
    return out


@_tool
def analyze_dolby_vision(items: list[int] | None = None, blend_shots: bool = False, track: int = 1) -> str:
    """Run Dolby Vision analysis (Studio) on the whole current timeline, or on video items (1-based indexes).
    blend_shots=True analyzes the given items together, as one blended shot. Dolby Vision must be on
    (set_hdr(dolby_vision="4.0")). Analysis can continue after this returns."""
    resolve = _resolve()
    proj, tl = _timeline()
    source = tl if str(tl.GetSetting("useCustomSettings")) == "1" else proj
    if str(source.GetSetting("hdrDolbyControlsOn")) != "1":
        raise ToolError('Dolby Vision is off: turn it on with set_hdr(dolby_vision="4.0")')
    analyze = _method(tl, "AnalyzeDolbyVision", "18")
    if blend_shots and not items:
        raise ToolError("blend_shots needs the items to blend")
    if items:
        targets = [_item(tl, i, track) for i in items]
        ok = analyze(targets, _constant(resolve, "DLB_BLEND_SHOTS")) if blend_shots else analyze(targets, None)
    else:
        ok = analyze()
    if not ok:
        raise ToolError("AnalyzeDolbyVision failed (Studio only)")
    return f"Dolby Vision analysis started on {f'{len(items)} item(s)' if items else 'the whole timeline'}"



# --- Projects, databases and Blackmagic Cloud ---


@_tool
def project_browser(folder: str | None = None) -> dict:
    """The project manager: current database and project, and the folders and projects in a project-manager folder.
    `folder` is a path from the root ("Clients/Acme"); "/" is the root; omitted, the current folder. Navigating makes
    that folder current, which is where list_projects, create_project and import_project act."""
    pm = _resolve().GetProjectManager()
    if folder is not None:
        pm.GotoRootFolder()
        for part in [p for p in folder.strip("/").split("/") if p]:
            if not pm.OpenFolder(part):
                pm.GotoRootFolder()  # never leave the project manager half-way down a wrong path
                raise ToolError(f"project folder not found: {part} (in {folder}); now at the root folder")
    cur = pm.GetCurrentProject()
    return {
        "database": pm.GetCurrentDatabase(),
        "folder": pm.GetCurrentFolder(),
        "folders": list(pm.GetFolderListInCurrentFolder() or []),
        "projects": list(pm.GetProjectListInCurrentFolder() or []),
        "current_project": cur.GetName() if cur else None,
    }


@_tool
def create_project_folder(name: str) -> str:
    """Create a folder in the current project-manager folder (see project_browser)."""
    pm = _resolve().GetProjectManager()
    if not pm.CreateFolder(name):
        raise ToolError(f"cannot create project folder (already exists?): {name}")
    return f"created project folder {name}"


@_tool
def rename_project(new_name: str) -> str:
    """Rename the current project."""
    _, proj = _project()
    old = proj.GetName()
    if not proj.SetName(new_name):
        raise ToolError(f"cannot rename to {new_name} (name taken?)")
    return f"renamed project {old} → {new_name}"


@_tool
def delete_project(name: str) -> str:
    """Delete a project in the current project-manager folder. The open project cannot be deleted: open another
    first. Irreversible."""
    pm = _resolve().GetProjectManager()
    cur = pm.GetCurrentProject()
    if cur and cur.GetName() == name:
        raise ToolError(f"{name} is the open project: open another project first")
    if name not in (pm.GetProjectListInCurrentFolder() or []):
        raise ToolError(f"project not found in the current folder: {name} (see project_browser)")
    # The first attempt is flaky on live Resolve; one retry.
    if not (pm.DeleteProject(name) or pm.DeleteProject(name)):
        raise ToolError(f"Resolve refused to delete {name} (it may still be held from being open earlier)")
    return f"deleted project {name}"


@_tool
def list_databases() -> dict:
    """Project databases known to Resolve (local disk and PostgreSQL servers) and the current one."""
    pm = _resolve().GetProjectManager()
    return {"current": pm.GetCurrentDatabase(), "databases": list(pm.GetDatabaseList() or [])}


@_tool
def switch_database(name: str, db_type: str | None = None) -> dict:
    """Switch to another project database by name (db_type "Disk" or "PostgreSQL" when names repeat). Resolve
    closes the open project; it is saved first."""
    pm = _resolve().GetProjectManager()
    matches = [db for db in (pm.GetDatabaseList() or []) if db.get("DbName") == name
               and (db_type is None or db.get("DbType") == db_type)]
    if not matches:
        raise ToolError(f"database not found: {name} (see list_databases)")
    if len(matches) > 1:
        raise ToolError(f"several databases are named {name}: pass db_type")
    _save_current(pm)
    if not pm.SetCurrentDatabase(matches[0]) or (pm.GetCurrentDatabase() or {}).get("DbName") != name:
        raise ToolError(f"could not switch to database {name}")
    return {"database": pm.GetCurrentDatabase(), "projects": list(pm.GetProjectListInCurrentFolder() or [])}


CLOUD_SYNC = {"none": "CLOUD_SYNC_NONE", "proxy_only": "CLOUD_SYNC_PROXY_ONLY", "proxy_and_original": "CLOUD_SYNC_PROXY_AND_ORIG"}


def _cloud_settings(resolve, name, media_path, sync, collaboration=None, camera_access=None):
    if sync not in CLOUD_SYNC:
        raise ToolError(f"sync must be one of: {', '.join(CLOUD_SYNC)}")
    media_path = os.path.abspath(media_path)
    if not os.path.isdir(media_path):
        raise ToolError(f"media folder not found: {media_path}")
    # Enum-keyed: plain string keys are silently rejected.
    settings = {
        _constant(resolve, "CLOUD_SETTING_PROJECT_NAME"): name,
        _constant(resolve, "CLOUD_SETTING_PROJECT_MEDIA_PATH"): media_path,
        _constant(resolve, "CLOUD_SETTING_SYNC_MODE"): _constant(resolve, CLOUD_SYNC[sync]),
    }
    if collaboration is not None:
        settings[_constant(resolve, "CLOUD_SETTING_IS_COLLAB")] = collaboration
    if camera_access is not None:
        settings[_constant(resolve, "CLOUD_SETTING_IS_CAMERA_ACCESS")] = camera_access
    return settings


@_tool
def create_cloud_project(
    name: str, media_path: str, collaboration: bool = True, sync: str = "proxy_only", camera_access: bool = False
) -> str:
    """Create a Blackmagic Cloud project and open it (the current project is saved first). collaboration=True turns
    on multi-user collaboration; sync: none, proxy_only or proxy_and_original; media_path is this machine's local
    media folder. Signed-in Blackmagic Cloud account needed. Inviting collaborators is only possible in the UI."""
    resolve = _resolve()
    pm = resolve.GetProjectManager()
    settings = _cloud_settings(resolve, name, media_path, sync, collaboration, camera_access)
    _save_current(pm)
    proj = _method(pm, "CreateCloudProject", "18")(settings)
    if not proj:
        raise ToolError(f"could not create cloud project {name} (signed in to Blackmagic Cloud? name taken?)")
    return f"created and opened cloud project {proj.GetName()}"


@_tool
def load_cloud_project(name: str, media_path: str, sync: str = "proxy_only") -> str:
    """Open an existing Blackmagic Cloud project (the current project is saved first). media_path is where its media
    lives or syncs to on this machine."""
    resolve = _resolve()
    pm = resolve.GetProjectManager()
    settings = _cloud_settings(resolve, name, media_path, sync)
    _save_current(pm)
    proj = _method(pm, "LoadCloudProject", "18")(settings)
    if not proj:
        raise ToolError(f"cloud project not found or not shared with this account: {name}")
    return f"opened cloud project {proj.GetName()}"


@_tool
def refresh_collaboration() -> dict:
    """In a collaboration project, pull other editors' changes into the media pool and report bins that are still
    out of date."""
    _, proj = _project()
    pool = proj.GetMediaPool()
    if not pool.RefreshFolders():
        raise ToolError("RefreshFolders failed (is this a collaboration project?)")
    stale = []

    def walk(folder, path):
        if _opt(folder, "GetIsFolderStale"):
            stale.append(path or "/")
        for f in folder.GetSubFolderList() or []:
            walk(f, f"{path}/{f.GetName()}".lstrip("/"))

    walk(pool.GetRootFolder(), "")
    return {"refreshed": True, "stale_bins": stale}


# --- Timelines ---


def _find_timeline(proj, name):
    for i in range(1, int(proj.GetTimelineCount()) + 1):
        tl = proj.GetTimelineByIndex(i)
        if tl.GetName() == name:
            return tl
    raise ToolError(f"timeline not found: {name} (see list_timelines)")


@_tool
def duplicate_timeline(new_name: str, timeline: str | None = None) -> dict:
    """Copy a timeline (default: the current one) under a new name, e.g. to keep a version before big changes. The
    current timeline stays current."""
    proj, cur = _timeline()
    source = _find_timeline(proj, timeline) if timeline else cur
    if any(proj.GetTimelineByIndex(i).GetName() == new_name for i in range(1, int(proj.GetTimelineCount()) + 1)):
        raise ToolError(f"a timeline named {new_name} already exists")
    copy = source.DuplicateTimeline(new_name)
    # DuplicateTimeline silently makes the copy current; put the user's timeline back.
    if not proj.SetCurrentTimeline(cur):
        raise ToolError(f"duplicated to {new_name}, but could not make '{cur.GetName()}' current again")
    if not copy:
        raise ToolError("DuplicateTimeline failed")
    return {"copy": copy.GetName(), "of": source.GetName(), "current": cur.GetName()}


@_tool
def rename_timeline(new_name: str, timeline: str | None = None) -> str:
    """Rename a timeline (default: the current one)."""
    proj, cur = _timeline()
    tl = _find_timeline(proj, timeline) if timeline else cur
    old = tl.GetName()
    if not tl.SetName(new_name):
        raise ToolError(f"cannot rename to {new_name} (name taken?)")
    return f"renamed timeline {old} → {new_name}"


@_tool
def delete_timelines(names: list[str]) -> str:
    """Delete timelines by name. Irreversible; duplicate_timeline first to keep a copy."""
    _, proj = _project()
    targets = [_find_timeline(proj, n) for n in names]
    if not targets:
        raise ToolError("no timelines given")
    if len(targets) == int(proj.GetTimelineCount()):
        raise ToolError("refusing to delete every timeline in the project")
    if not proj.GetMediaPool().DeleteTimelines(targets):
        raise ToolError("DeleteTimelines failed")
    return f"deleted {len(targets)} timeline(s)"


# --- Review notes (timeline markers) ---
#
# Review notes are timeline markers whose customData carries {"author", "status"}; they show up in Resolve's
# marker index for everyone on the project, collaboration projects included.

MARKER_COLORS = ("Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky",
                 "Mint", "Lemon", "Sand", "Cocoa", "Cream")


def _note_rows(tl):
    start = int(tl.GetStartFrame())
    rows = []
    for frame, m in sorted((tl.GetMarkers() or {}).items()):
        try:
            data = json.loads(m.get("customData") or "{}")
        except ValueError:
            data = {}
        try:
            tc = _timecode(tl, start + int(frame))
        except ToolError:
            tc = None
        rows.append({"frame": int(frame), "timecode": tc, "color": m.get("color"), "name": m.get("name"),
                     "note": m.get("note"), "duration": int(m.get("duration") or 1),
                     "author": data.get("author"), "status": data.get("status")})
    return rows


def _marker_at(tl, frame):
    markers = {int(f): m for f, m in (tl.GetMarkers() or {}).items()}
    if frame not in markers:
        raise ToolError(f"no marker at frame {frame} (see review_notes)")
    return markers[frame]


@_tool
def review_notes(status: str | None = None) -> list[dict]:
    """All markers on the current timeline as review notes: frame (relative to the timeline start), timecode, color,
    name, note, duration, and author/status for notes made with add_review_note. status filters: open, resolved."""
    _, tl = _timeline()
    rows = _note_rows(tl)
    return [r for r in rows if status is None or r["status"] == status]


@_tool
def add_review_note(frame: int, note: str, author: str | None = None, color: str = "Red", duration: int = 1) -> dict:
    """Leave a review note at `frame` (relative to the timeline start) as a marker, marked open, with its author."""
    if color not in MARKER_COLORS:
        raise ToolError(f"unknown marker color: {color} (one of {', '.join(MARKER_COLORS)})")
    if duration < 1:
        raise ToolError("duration must be at least 1 frame")
    _, tl = _timeline()
    data = json.dumps({"author": author, "status": "open"})
    name = f"{author}: {note}" if author else note
    if not tl.AddMarker(frame, color, name[:60], note, duration, data):
        raise ToolError(f"cannot add a note at frame {frame} (a marker is already there?)")
    return next(r for r in _note_rows(tl) if r["frame"] == frame)


@_tool
def resolve_review_note(frame: int, reopen: bool = False) -> dict:
    """Mark the review note at `frame` resolved (it turns green), or reopen it with its original color."""
    _, tl = _timeline()
    m = _marker_at(tl, frame)
    try:
        data = json.loads(m.get("customData") or "{}")
    except ValueError:
        data = {}
    if reopen:
        data["status"], color = "open", data.pop("open_color", m.get("color"))
    else:
        data.setdefault("open_color", m.get("color"))
        data["status"], color = "resolved", "Green"
    # A marker's color can only change by replacing it.
    if not tl.DeleteMarkerAtFrame(frame):
        raise ToolError(f"cannot update the marker at frame {frame}")
    if not tl.AddMarker(frame, color, m.get("name", ""), m.get("note", ""), int(m.get("duration") or 1), json.dumps(data)):
        tl.AddMarker(frame, m.get("color"), m.get("name", ""), m.get("note", ""), int(m.get("duration") or 1),
                     m.get("customData") or "")
        raise ToolError(f"could not rewrite the marker at frame {frame}; the original was put back")
    return next(r for r in _note_rows(tl) if r["frame"] == frame)


@_tool
def delete_markers(frame: int | None = None, color: str | None = None) -> str:
    """Delete the marker at `frame`, or every marker of a `color` ("All" for every marker) on the current timeline."""
    if (frame is None) == (color is None):
        raise ToolError("give frame or color")
    _, tl = _timeline()
    if frame is not None:
        _marker_at(tl, frame)
        if not tl.DeleteMarkerAtFrame(frame):
            raise ToolError(f"cannot delete the marker at frame {frame}")
        return f"deleted marker at frame {frame}"
    if color != "All" and color not in MARKER_COLORS:
        raise ToolError(f"unknown marker color: {color}")
    before = len(tl.GetMarkers() or {})
    if not tl.DeleteMarkersByColor(color):
        raise ToolError("DeleteMarkersByColor failed")
    return f"deleted {before - len(tl.GetMarkers() or {})} marker(s)"


@_tool
def export_review_notes(path: str, status: str | None = None) -> dict:
    """Write the current timeline's review notes to a .csv (spreadsheets, other editors) or .md (sharing with a
    client) file, in timeline order. status filters: open, resolved."""
    import csv

    _, tl = _timeline()
    rows = [r for r in _note_rows(tl) if status is None or r["status"] == status]
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".csv", ".md"):
        raise ToolError("path must end in .csv or .md")
    if not os.path.isdir(os.path.dirname(path)):
        raise ToolError(f"folder not found: {os.path.dirname(path)}")
    cols = ["timecode", "frame", "status", "author", "color", "note"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        if ext == ".csv":
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        else:
            f.write(f"# Review notes: {tl.GetName()}\n\n| Timecode | Status | Author | Note |\n|---|---|---|---|\n")
            for r in rows:
                note = (r["note"] or "").replace("|", "\\|").replace("\n", " ")
                f.write(f"| {r['timecode'] or r['frame']} | {r['status'] or ''} | {r['author'] or ''} | {note} |\n")
    return {"timeline": tl.GetName(), "path": path, "notes": len(rows)}


if __name__ == "__main__":
    _setup_logging()
    log.info("starting", extra={"fields": {"platform": sys.platform, "pid": os.getpid()}})
    mcp.run()
