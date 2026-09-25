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
import math
import random
import struct
import subprocess
import wave
from array import array
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
    "media management, projects and review notes, transcripts, subtitle files and titles, keyframes, multicam, "
    "sound effects and music (voiceover, test tones, beat detection), visual effects and transitions, node graphs, "
    "color groups, PowerGrades and DCTL, "
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


MASTER_LUT_DIRS = {
    "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
    "win32": r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\LUT",
    "linux": "/opt/resolve/LUT",
}


def _set_lut(proj, graph, node, lut_path):
    """SetLUT, installing an outside file into the master LUT folder when Resolve cannot resolve it."""
    if graph.SetLUT(node, lut_path):
        return lut_path
    if not (os.path.isabs(lut_path) and os.path.isfile(lut_path)):
        raise ToolError(f"SetLUT failed for {lut_path}: not a LUT Resolve knows (give a path relative to its LUT "
                        "folders, or an absolute path to the file)")
    master = os.environ.get("RESOLVE_LUT_DIR") or MASTER_LUT_DIRS.get(sys.platform, MASTER_LUT_DIRS["linux"])
    dest = os.path.join(master, "davinci-mcp")
    try:
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(lut_path, os.path.join(dest, os.path.basename(lut_path)))
    except OSError as e:
        raise ToolError(f"Resolve only reads LUTs from its master LUT folder, and copying there failed ({e}); copy "
                        f"the file into {dest} yourself or set RESOLVE_LUT_DIR") from e
    proj.RefreshLUTList()
    rel = f"davinci-mcp/{os.path.basename(lut_path)}"
    if not graph.SetLUT(node, rel):
        raise ToolError(f"SetLUT failed even after installing the LUT as {rel} (not a valid LUT file?)")
    return rel


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
    while track > int(tl.GetTrackCount("video") or 0):  # appending to a missing track lands the clips elsewhere
        if not tl.AddTrack("video"):
            raise ToolError(f"could not add video track {track}")
    before = len(tl.GetItemListInTrack("video", track) or [])
    if start_frame is None and end_frame is None:
        ok = pool.AppendToTimeline(clips)
    else:
        # endFrame is exclusive: live Resolve 21.1 made 23-frame items from startFrame 0, endFrame 23.
        infos = [
            {
                "mediaPoolItem": c,
                "startFrame": start_frame or 0,
                "endFrame": end_frame + 1 if end_frame is not None else int(float(c.GetClipProperty("Frames") or 0)),
            }
            for c in clips
        ]
        # Only pass trackIndex when asked: appends carrying it were measured to read back fine but render almost
        # nothing on live Resolve 21.0.4, while Blackmagic's own example (no trackIndex) renders normally.
        if track != 1:
            for info in infos:
                info["trackIndex"] = track
        ok = pool.AppendToTimeline(infos)
    if not ok:
        raise ToolError("append failed")
    if track != 1 and len(tl.GetItemListInTrack("video", track) or []) == before:
        raise ToolError(f"Resolve reported success but nothing landed on video track {track}")
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


SUBTITLE_DELIVERY = {"burn_in": "BurnIn", "separate_file": "SeparateFile", "embedded": "EmbeddedCaptions"}


def _major_version():
    try:
        return int(str(_resolve().GetVersionString()).split(".")[0])
    except (TypeError, ValueError):
        return 0


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
    subtitles: str | None = None,
    start: bool = True,
) -> dict:
    """Queue a render of the current timeline and start it (start=False only queues it).
    format/codec are ids from list_render_formats (e.g. "mov" + "ProRes422HQ"), both or neither.
    mark_in/mark_out: absolute timeline frames (as in list_items), both inclusive; default whole timeline.
    quality: 0 auto, a bitrate, or Least/Low/Medium/High/Best. individual_clips=True renders one file per clip.
    settings: any other SetRenderSettings key (AudioCodec, AudioBitDepth, AudioSampleRate, ColorSpaceTag, GammaTag,
    ExportAlpha, AlphaMode, EncodingProfile, MultiPassEncode, NetworkOptimization, PixelAspectRatio...).
    Unset values inherit the Deliver page's current state, so pass a `preset` to start from a known base.
    subtitles: burn_in, separate_file or embedded delivers the timeline's subtitle track (Resolve 21+; on 19.x these
    settings were measured to have no effect, so they are refused there). Check the output for the subtitles."""
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
    if subtitles is not None:
        if subtitles not in SUBTITLE_DELIVERY:
            raise ToolError(f"subtitles must be one of: {', '.join(SUBTITLE_DELIVERY)}")
        if _major_version() < 21:
            raise ToolError("subtitle delivery through the API needs Resolve 21 (inert on earlier versions); "
                            "use the Deliver page's Subtitle Settings")
        values.update(ExportSubtitle=True, SubtitleFormat=SUBTITLE_DELIVERY[subtitles])
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
    """Set a LUT on a node (1-based) of a video item. lut_path is relative to Resolve's LUT folders (e.g.
    "Blackmagic Design/Rec709 ..."), or any absolute path to a .cube/.dat file: Resolve only resolves LUTs in its master
    LUT folder, so a file elsewhere is copied into its "davinci-mcp" subfolder and the LUT list refreshed."""
    resolve = _resolve()
    proj, tl = _timeline()
    it = _item(tl, item, track)
    _check_node(it, node)
    with _on_page(resolve, "color"):
        used = _set_lut(proj, _graph(it), node, lut_path)
    return f"LUT on node {node} of '{it.GetName()}': {used}"


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
    with _on_page(_resolve(), "color"):
        ok = it.SetCDL(cdl)
    if not ok:
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
    with _on_page(_resolve(), "color"):
        ok = src.CopyGrades(dst)
    if not ok:
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
    with _on_page(_resolve(), "color"):
        ok = tl.ApplyGradeFromDRX(path, DRX_MODES[keyframes], targets)
    if not ok:
        raise ToolError("ApplyGradeFromDRX failed")
    return f"applied {os.path.basename(path)} to {len(targets)} item(s)"


@_tool
def add_color_version(item: int, name: str, remote: bool = False, track: int = 1) -> str:
    """Add a named color version to a video item (local by default) and make it current."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    with _on_page(_resolve(), "color"):
        ok = it.AddVersion(name, int(remote))
    if not ok:
        raise ToolError(f"cannot add version (name taken?): {name}")
    return f"added {'remote' if remote else 'local'} version '{name}' to '{it.GetName()}'"


@_tool
def load_color_version(item: int, name: str, remote: bool = False, track: int = 1) -> str:
    """Make a named color version of a video item current."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    with _on_page(_resolve(), "color"):
        ok = it.LoadVersionByName(name, int(remote))
    if not ok:
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
            # SetInput with a time keys an animated input. Not tool[input].__setitem__: the Resolve bridge
            # returns None for attributes it does not know ('NoneType' object is not callable on live 21.1).
            _write(lambda x, f=frame: tool.SetInput(input, x, f), v, point)
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
            # Refused on some pages (live 21.1: after a render); the Edit page's viewer always works.
            with _on_page(_resolve(), "edit"):
                ok = proj.ExportCurrentFrameAsStill(path) and os.path.exists(path)
            if not ok:
                raise ToolError("ExportCurrentFrameAsStill failed, also from the Edit page")
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
    # Found by position, next to the clip: live 21.1 reports transition timing that does not bracket the cut
    # (start 24, end 22), so a timing search misses it.
    items = tl.GetItemListInTrack(track_type, track) or []
    i = item if position == "end" else item - 1  # 0-based slot the transition takes
    if not (0 <= i < len(items) and _is_transition(items, i)):
        return {"name": tr.GetName(), "note": "added, but not found next to the clip"}
    out = {"name": items[i].GetName(), "index": i + 1}
    start, end = items[i].GetStart(), items[i].GetEnd()
    if start < end:
        out.update(start=start, end=end, duration=end - start)
    else:
        out["note"] = f"Resolve reports start {start}, end {end} for it; timing not usable"
    return out


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
    first, last = _comp_range(c, it)
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
        why = "" if _opt(it, "GetMediaPoolItem") else f" ('{it.GetName()}' is a title, generator or transition)"
        raise ToolError("SetSpeed failed" + why)
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
    # An empty list means "nothing" to live Resolve 21.1 (the export fails), so the whole pool is listed.
    targets = _pool_clips(proj, clips) if clips else [c for _, c in _walk(proj.GetMediaPool().GetRootFolder())]
    if not targets:
        raise ToolError("the media pool is empty")
    if not proj.GetMediaPool().ExportMetadata(path, targets) or not os.path.exists(path):
        raise ToolError(f"ExportMetadata failed: {path}")
    return f"metadata of {len(targets)} clip(s) written to {path}"


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



# --- Transcripts, subtitle files and titles ---
#
# Resolve's API cannot read or edit subtitle items (text or timing), import an SRT onto a subtitle track, or style
# subtitles. So subtitles are made as files: from a clip's transcript (Resolve 21.1 GetTranscription, with word
# timing and speakers), or from captions the model writes or translates, e.g. into Arabic, which Resolve's
# auto-captions do not support. The file is then imported in Resolve (File > Import > Subtitle).

SILENCE = "(...)"


def _tc_frames(tc, fps):
    """Frames in a "HH:MM:SS:FF" (or ;FF) timecode at the nominal rate of `fps`."""
    parts = str(tc).replace(";", ":").split(":")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise ToolError(f"not a timecode: {tc!r}")
    h, m, sec, f = (int(p) for p in parts)
    return ((h * 60 + m) * 60 + sec) * round(fps) + f


def _seconds(value, fps=None):
    """Seconds from a number, "HH:MM:SS,mmm" / "HH:MM:SS.mmm", or a "HH:MM:SS:FF" timecode (needs fps)."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.count(":") == 3:
        if not fps:
            raise ToolError(f"{text!r} is a frame timecode: give fps")
        return _tc_frames(text, fps) / float(fps)
    try:
        h, m, rest = text.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    except ValueError:
        raise ToolError(f"not a time: {value!r} (seconds, HH:MM:SS,mmm or HH:MM:SS:FF)") from None


def _stamp(sec, sep):
    ms = round(sec * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s_, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s_:02d}{sep}{ms:03d}"


RLM = "\u200f"


def _write_captions(path, captions, rtl=False):
    """Write [{start, end, text}] (seconds) as .srt or .vtt."""
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".srt", ".vtt"):
        raise ToolError("path must end in .srt or .vtt")
    if not os.path.isdir(os.path.dirname(path)):
        raise ToolError(f"folder not found: {os.path.dirname(path)}")
    blocks = []
    for n, c in enumerate(captions, 1):
        lines = str(c["text"]).strip().splitlines()
        if rtl:
            lines = [RLM + line for line in lines]  # keeps punctuation on the right side in Arabic/Hebrew
        sep = "," if ext == ".srt" else "."
        head = [str(n)] if ext == ".srt" else []
        blocks.append("\n".join(head + [f"{_stamp(c['start'], sep)} --> {_stamp(c['end'], sep)}"] + lines))
    body = ("WEBVTT\n\n" if ext == ".vtt" else "") + "\n\n".join(blocks) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def _transcript(clip):
    """(transcript dict, complete) for a media-pool clip: full on 21.1+, else the truncated preview property."""
    get = getattr(clip, "GetTranscription", None)
    if callable(get):
        t = get(False) or {}
        if not t.get("segments"):
            raise ToolError(f"{clip.GetName()} has no transcription: run transcribe_audio first (Studio)")
        return t, True
    preview = clip.GetClipProperty("Transcription") or ""
    if not preview:
        raise ToolError(f"{clip.GetName()} has no transcription: run transcribe_audio first (Studio)")
    return {"language": None, "segments": [], "preview": preview}, not preview.endswith(("…", "..."))


def _clip_timing(clip):
    fps = float(clip.GetClipProperty("FPS") or 0) or 24.0
    start_tc = clip.GetClipProperty("Start TC")
    return fps, (_tc_frames(start_tc, fps) if start_tc else 0)


@_tool
def get_transcript(clip: str, query: str | None = None, words: bool = False) -> dict:
    """A media-pool clip's transcript (after transcribe_audio): segments with start/end timecodes, seconds from the
    clip start, speaker and text; words=True adds per-word timing. query keeps only segments containing it
    (case-insensitive), to find where something is said. Full on Resolve 21.1+; earlier versions only expose a
    preview, marked complete=False when Resolve cut it short."""
    _, proj = _project()
    (c,) = _pool_clips(proj, [clip])
    t, complete = _transcript(c)
    if not t["segments"]:
        return {"clip": c.GetName(), "complete": complete, "preview": t["preview"], "segments": []}
    fps, origin = _clip_timing(c)
    rows = []
    for seg in t["segments"]:
        if query and query.lower() not in (seg.get("text") or "").lower():
            continue
        row = {"start": seg["start"], "end": seg["end"],
               "start_seconds": round((_tc_frames(seg["start"], fps) - origin) / fps, 3),
               "end_seconds": round((_tc_frames(seg["end"], fps) - origin) / fps, 3),
               "speaker": seg.get("speaker"), "text": seg.get("text", "")}
        if words:
            row["words"] = seg.get("words", [])
        rows.append(row)
    return {"clip": c.GetName(), "language": t.get("language"), "complete": True, "segments": rows}


@_tool
def export_transcript(clip: str, path: str, speakers: bool = True, rtl: bool = False) -> dict:
    """Write a clip's transcript (Resolve 21.1+) as subtitles (.srt, .vtt) or a document (.txt, .json), timed from
    the clip start. speakers=True prefixes each line with the speaker when Resolve detected one; silences are
    skipped. Import the .srt in Resolve with File > Import > Subtitle."""
    t = get_transcript(clip)
    if not t.get("language") and not t["segments"]:
        raise ToolError("this Resolve only exposes a transcript preview; exporting needs Resolve 21.1")
    segs = [s_ for s_ in t["segments"] if s_["text"].strip() and s_["text"].strip() != SILENCE]
    label = (lambda s_: f"{s_['speaker']}: {s_['text']}" if speakers and s_.get("speaker") else s_["text"])
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".srt", ".vtt"):
        _write_captions(path, [{"start": s_["start_seconds"], "end": s_["end_seconds"], "text": label(s_)} for s_ in segs], rtl)
    elif ext in (".txt", ".json"):
        if not os.path.isdir(os.path.dirname(path)):
            raise ToolError(f"folder not found: {os.path.dirname(path)}")
        with open(path, "w", encoding="utf-8") as f:
            if ext == ".json":
                json.dump(t, f, ensure_ascii=False, indent=2)
            else:
                f.write("\n".join(f"[{_stamp(s_['start_seconds'], '.')[:8]}] {label(s_)}" for s_ in segs) + "\n")
    else:
        raise ToolError("path must end in .srt, .vtt, .txt or .json")
    return {"clip": t["clip"], "path": path, "segments": len(segs), "language": t.get("language")}


@_tool
def write_subtitles(path: str, captions: list[dict], fps: float | None = None, rtl: bool = False) -> dict:
    """Write captions as an .srt or .vtt file, e.g. a translation of a transcript (any language, UTF-8).
    captions: [{"start", "end", "text"}] with times in seconds, "HH:MM:SS,mmm", or "HH:MM:SS:FF" timecodes (these
    need fps; default: the current timeline's). rtl=True marks lines right-to-left (Arabic, Hebrew, Persian) so
    punctuation lands on the correct side. Import in Resolve with File > Import > Subtitle."""
    if not captions:
        raise ToolError("no captions given")
    if fps is None and any(str(c.get("start", "")).count(":") == 3 for c in captions):
        _, tl = _timeline()
        fps = float(tl.GetSetting("timelineFrameRate") or 24)
    rows, prev_end = [], 0.0
    for n, c in enumerate(captions, 1):
        if not str(c.get("text", "")).strip():
            raise ToolError(f"caption {n} has no text")
        start, end = _seconds(c.get("start"), fps), _seconds(c.get("end"), fps)
        if end <= start:
            raise ToolError(f"caption {n} ends before it starts")
        if start < prev_end:
            raise ToolError(f"caption {n} starts before caption {n - 1} ends")
        rows.append({"start": start, "end": end, "text": c["text"]})
        prev_end = end
    return {"path": _write_captions(path, rows, rtl), "captions": len(rows)}


def _text_nodes(it):
    comp = it.GetFusionCompByIndex(1) if int(_opt(it, "GetFusionCompCount") or 0) else None
    return comp, (list((comp.GetToolList(False, "TextPlus") or {}).values()) if comp else [])


@_tool
def list_titles() -> list[dict]:
    """Every Fusion title (Text+) on the current timeline's video tracks, with its text: the titles set_title_text
    can edit. Resolve's standard (non-Fusion) titles are not editable through the API."""
    _, tl = _timeline()
    out = []
    for n in range(1, int(tl.GetTrackCount("video") or 0) + 1):
        for i, it in enumerate(tl.GetItemListInTrack("video", n) or [], 1):
            _, nodes = _text_nodes(it)
            if nodes:
                out.append({"track": n, "item": i, "name": it.GetName(), "start": it.GetStart(), "end": it.GetEnd(),
                            "texts": {_tool_attrs(t)[0]: t.GetInput("StyledText") for t in nodes}})
    return out


@_tool
def set_title_text(
    item: int,
    text: str | None = None,
    font: str | None = None,
    style: str | None = None,
    size: float | None = None,
    color: list[float] | None = None,
    node: str | None = None,
    track: int = 1,
) -> dict:
    """Edit a Fusion title (Text+) on the timeline: its text (any language), font family and style (e.g. "Bold"),
    size (0-1 of frame width, Text+ default about 0.08), fill color [r, g, b] in 0-1. node picks the Text+ node when a
    title has several (see list_titles). Insert new Fusion titles with insert_title(fusion=True)."""
    _, tl = _timeline()
    it = _item(tl, item, track)
    comp, nodes = _text_nodes(it)
    if not nodes:
        raise ToolError(f"'{it.GetName()}' is not a Fusion title (no Text+ node); see list_titles")
    if node:
        nodes = [t for t in nodes if _tool_attrs(t)[0] == node]
        if not nodes:
            raise ToolError(f"no Text+ node named {node} in '{it.GetName()}'")
    elif len(nodes) > 1:
        raise ToolError(f"'{it.GetName()}' has several Text+ nodes: pass node= one of "
                        f"{', '.join(_tool_attrs(t)[0] for t in nodes)}")
    tool = nodes[0]
    name = _tool_attrs(tool)[0]
    values = {k: v for k, v in (("StyledText", text), ("Font", font), ("Style", style), ("Size", size)) if v is not None}
    if color is not None:
        if len(color) != 3 or not all(0 <= float(v) <= 1 for v in color):
            raise ToolError("color needs [r, g, b] with values 0-1")
        values.update(Red1=float(color[0]), Green1=float(color[1]), Blue1=float(color[2]))
    if not values:
        raise ToolError("nothing to change")
    applied = {k: _set_input(comp, tool, name, k, value=v)["value"] for k, v in values.items()}
    return {"item": it.GetName(), "node": name, "set": applied}



# --- Keyframes ---
#
# Clip animation goes through the clip's Fusion comp (a Transform node), the route measured to render on live
# Resolve; the Edit-page keyframe methods are not in Resolve's current API reference. Frames here are relative to
# the clip's first frame and converted to the comp's own frame numbers.

MOTION_INPUTS = {"zoom": "Size", "position": "Center", "rotation": "Angle"}


def _comp_range(c, it):
    """(first, last) comp frames of the clip. The length comes from the timeline item: live Resolve 21.1 reported
    a render range one frame short (0-21 on a 23-frame clip), which would refuse keyframes on the last frame."""
    first = int((c.GetAttrs() or {}).get("COMPN_RenderStart", 0))
    return first, first + int(it.GetDuration()) - 1


@_tool
def animate_clip(
    item: int,
    zoom: dict[int, float] | None = None,
    position: dict[int, list[float]] | None = None,
    rotation: dict[int, float] | None = None,
    track: int = 1,
) -> dict:
    """Keyframe a video clip's motion: zoom (1.0 = full frame), position ([x, y], 0-1, frame center 0.5, 0.5) and
    rotation (degrees, counter-clockwise), each as {frame: value} with frames counted from the clip's first frame
    (0). Built on a Fusion Transform node named Motion in the clip's comp (created if needed); calling again adds or
    replaces keyframes. Refine with set_fusion_input(node="Motion"), inspect with list_keyframes."""
    moves = {k: v for k, v in (("zoom", zoom), ("position", position), ("rotation", rotation)) if v}
    if not moves:
        raise ToolError("give zoom, position and/or rotation keyframes")
    _, tl = _timeline()
    it = _item(tl, item, track)
    c = _clip_comp(it)
    first, last = _comp_range(c, it)
    for name, keys in moves.items():
        bad = [f for f in keys if not 0 <= int(f) <= last - first]
        if bad:
            raise ToolError(f"{name} keyframes outside the clip (0-{last - first}): {bad}")
        if name == "zoom" and any(v <= 0 for v in keys.values()):
            raise ToolError("zoom must be positive")
    tool = c.FindTool("Motion") or _insert_before_output(c, "Transform", "Motion")
    out = {}
    for name, keys in moves.items():
        comp_keys = {first + int(f): v for f, v in keys.items()}
        frames = _set_input(c, tool, "Motion", MOTION_INPUTS[name], keyframes=comp_keys)["keyframes"]
        out[name] = [int(f) - first for f in frames]
    return {"item": it.GetName(), "node": "Motion", "keyframes": out}


@_tool
def list_keyframes(item: int, comp: int = 1, track: int = 1) -> list[dict]:
    """Every animated input in a clip's Fusion comp with its keyframes: [{node, input, keyframes: [{frame, value}]}],
    frames counted from the clip's first frame."""
    _, c = _comp(item, comp, track)
    _, tl = _timeline()
    first, _ = _comp_range(c, _item(tl, item, track))
    out = []
    for tool in (c.GetToolList(False) or {}).values():
        name, kind = _tool_attrs(tool)
        if kind in ANIMATION_MODIFIERS:
            continue
        for inp in (tool.GetInputList() or {}).values():
            if _source(inp)[1] not in ANIMATION_MODIFIERS:
                continue
            inp_id = (inp.GetAttrs() or {}).get("INPS_ID", "")
            frames = sorted(float(f) for f in (tool[inp_id].GetKeyFrames() or {}).values())
            out.append({"node": name, "input": inp_id,
                        "keyframes": [{"frame": int(f) - first, "value": _plain(tool.GetInput(inp_id, f))} for f in frames]})
    return out


@_tool
def clear_keyframes(item: int, node: str, input: str, comp: int = 1, track: int = 1) -> dict:
    """Remove the animation from one Fusion input (e.g. node="Motion", input="Size"); it keeps a single static value."""
    _, c = _comp(item, comp, track)
    tool = _node(c, node)
    inp = tool[input]
    if not inp:
        raise ToolError(f"{node} has no input {input} (see fusion_inputs)")
    if _source(inp)[1] not in ANIMATION_MODIFIERS:
        raise ToolError(f"{node}.{input} is not animated")
    with _locked(c):
        tool.ConnectInput(input, None)
    if _source(tool[input])[1] in ANIMATION_MODIFIERS:
        raise ToolError(f"could not remove the animation from {node}.{input}")
    return {"node": node, "input": input, "value": _plain(tool.GetInput(input))}


KEYFRAME_MODES = {"all": "KEYFRAME_MODE_ALL", "color": "KEYFRAME_MODE_COLOR", "sizing": "KEYFRAME_MODE_SIZING"}


@_tool
def set_color_keyframe_mode(mode: str) -> str:
    """Color page keyframe mode: which parameters a keyframe in the Color page's keyframe editor records: all,
    color (grade only) or sizing (sizing only)."""
    if mode not in KEYFRAME_MODES:
        raise ToolError(f"mode must be one of: {', '.join(KEYFRAME_MODES)}")
    resolve = _resolve()
    value = _constant(resolve, KEYFRAME_MODES[mode])
    with _on_page(resolve, "color"):
        ok = _method(resolve, "SetKeyframeMode", "18")(value)
    if not ok:
        raise ToolError("SetKeyframeMode failed")
    return f"color keyframe mode: {mode}"


# --- Multicam (Resolve 21.1) ---

MULTICAM_SYNC = {"timecode": "TIMECODE", "in": "IN", "out": "OUT", "audio": "AUDIO", "marker": "MARKER"}
MULTICAM_AUDIO = {"source": "SOURCE", "adaptive": "ADAPTIVE", "reference": "REFERENCE", "all": "ALL"}
MULTICAM_NAMES = {"sequential": "SEQUENTIAL", "angle": "ANGLE", "camera": "CAMERA", "clip": "CLIP", "file": "FILE"}
MULTICAM_DETECT = {"none": "MULTICAM_DETECT_NONE", "camera_number": "MULTICAM_DETECT_BY_CAMERA_NUMBER",
                   "angle": "MULTICAM_DETECT_BY_ANGLE", "reel_number": "MULTICAM_DETECT_BY_REEL_NUMBER",
                   "reel_name": "MULTICAM_DETECT_BY_REEL_NAME", "roll_card": "MULTICAM_DETECT_BY_ROLL_CARD"}


def _choice(value, table, what):
    if value not in table:
        raise ToolError(f"{what} must be one of: {', '.join(table)}")
    return table[value]


@_tool
def create_multicam(
    clips: list[str],
    name: str | None = None,
    sync: str = "timecode",
    audio_mode: str = "source",
    angle_names: str = "sequential",
    audio_channel: int | str | None = None,
    split_at_gaps: bool | None = None,
    use_full_extents: bool | None = None,
    create_bin: bool | None = None,
    same_camera: str | None = None,
    start_timecode: str | None = None,
    frame_rate: float | None = None,
) -> list[str]:
    """Build a multicam clip from media-pool clips (by name), one angle per camera (Resolve 21.1+).
    sync: timecode, in, out, audio (waveform) or marker. audio_mode: source, adaptive, reference or all.
    angle_names: sequential, angle, camera, clip or file. audio_channel (sync=audio): 1-8, "auto" or "mix".
    same_camera groups clips from one camera into one angle: none, camera_number, angle, reel_number, reel_name,
    roll_card. Resolve moves the source clips into a new bin unless create_bin=False. Append the result with
    append_clips, then smart_switch or cut angles in the UI."""
    resolve = _resolve()
    _, proj = _project()
    sources = _pool_clips(proj, clips)
    if len(sources) < 2:
        raise ToolError("a multicam clip needs at least two clips")
    opts = {
        "angleSyncMode": _constant(resolve, "MULTICAM_ANGLE_SYNC_" + _choice(sync, MULTICAM_SYNC, "sync")),
        "multicamAudioMode": _constant(resolve, "MULTICAM_AUDIO_" + _choice(audio_mode, MULTICAM_AUDIO, "audio_mode")),
        "angleNameMode": _constant(resolve, "MULTICAM_ANGLE_NAME_" + _choice(angle_names, MULTICAM_NAMES, "angle_names")),
    }
    if audio_channel is not None:
        if sync != "audio":
            raise ToolError("audio_channel only applies to sync=audio")
        if isinstance(audio_channel, str):
            audio_channel = _constant(resolve, _choice(audio_channel, SYNC_CHANNELS, "audio_channel"))
        elif not 1 <= audio_channel <= 8:
            raise ToolError("audio_channel must be 1-8, auto or mix")
        opts["channelConfig"] = audio_channel
    if split_at_gaps is not None:
        if sync != "audio":
            raise ToolError("split_at_gaps only applies to sync=audio")
        opts["splitAtGaps"] = split_at_gaps
    for key, value in (("useFullClipExtents", use_full_extents), ("createBinForSourceClips", create_bin),
                       ("name", name), ("startTimecode", start_timecode), ("frameRate", frame_rate)):
        if value is not None:
            opts[key] = value
    if same_camera is not None:
        opts["detectSameCameraClipsMode"] = _constant(resolve, _choice(same_camera, MULTICAM_DETECT, "same_camera"))
    created = _method(proj.GetMediaPool(), "CreateMulticamClip", "21.1")(sources, opts)
    if not created:
        raise ToolError("CreateMulticamClip made nothing (do the clips overlap in the chosen sync?)")
    return [c.GetName() for c in created]


@_tool
def auto_align_clips(
    video_items: list[int] | None = None,
    audio_items: list[int] | None = None,
    sync: str = "timecode",
    waveform_track: int | str | None = None,
    video_track: int = 1,
    audio_track: int = 1,
) -> str:
    """Line up timeline clips from different cameras/recorders by timecode or audio waveform (Resolve 21.1+). Items
    are 1-based indexes on video_track / audio_track. Include the linked audio of every video clip: Resolve moves only
    what is selected, and waveform alignment of video items alone fails. waveform_track: the audio track to compare,
    or "mix" / "auto"."""
    if sync not in ("timecode", "waveform"):
        raise ToolError("sync must be timecode or waveform")
    if sync == "waveform" and not audio_items:
        raise ToolError("waveform alignment needs the audio items (with their linked video items)")
    resolve = _resolve()
    _, tl = _timeline()
    items = [_pick(tl, i, video_track, "video") for i in video_items or []]
    items += [_pick(tl, i, audio_track, "audio") for i in audio_items or []]
    if len(items) < 2:
        raise ToolError("give at least two items to align")
    opts = {"SyncUsing": _constant(resolve, "AUTO_ALIGN_CLIPS_USING_" + sync.upper())}
    if waveform_track is not None:
        if sync != "waveform":
            raise ToolError("waveform_track only applies to sync=waveform")
        if isinstance(waveform_track, str):
            named = {"mix": "AUTO_ALIGN_CLIPS_WAVEFORM_TRACK_MIX", "auto": "AUTO_ALIGN_CLIPS_WAVEFORM_TRACK_AUTOMATIC"}
            waveform_track = _constant(resolve, _choice(waveform_track, named, "waveform_track"))
        opts["UseTrack"] = waveform_track
    if not _method(tl, "AutoAlignClips", "21.1")(items, opts):
        raise ToolError("AutoAlignClips failed (include linked video and audio; clips must share timecode or sound)")
    return f"aligned {len(items)} item(s) by {sync}"


SS_FREQ = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}
SS_ANALYSIS = {"none": "NONE", "detect_wide_angle": "DETECT_WIDE_ANGLE", "audio_only": "AUDIO_ONLY"}


@_tool
def smart_switch(
    item: int,
    min_edit_seconds: float = 1.0,
    change_delay_seconds: float = 0.3,
    wide_angle: str | None = "auto",
    wide_frequency: str = "medium",
    wide_for_intro_outro: bool = True,
    wide_for_silence: bool = True,
    video_only: bool = False,
    quality: str = "better",
    analysis: str | None = None,
    track: int = 1,
) -> str:
    """Let Resolve cut a multicam clip on the timeline automatically, switching to whoever is speaking (Resolve 21.1+,
    Studio). wide_angle: "auto" to detect the wide shot, an angle name, or None for no wide shot; wide_frequency low,
    medium or high; analysis (overrides auto wide detection): none, detect_wide_angle or audio_only.
    min_edit_seconds 0.5-10, change_delay_seconds 0-2. Experimental: not yet validated on a live Resolve.
    Check the result with timeline_overview / view_frame."""
    if not 0.5 <= min_edit_seconds <= 10:
        raise ToolError("min_edit_seconds must be 0.5-10")
    if not 0 <= change_delay_seconds <= 2:
        raise ToolError("change_delay_seconds must be 0-2")
    resolve = _resolve()
    _, tl = _timeline()
    it = _item(tl, item, track)
    settings = {
        "minEditDuration": float(min_edit_seconds),
        "editChangeDelay": float(change_delay_seconds),
        "wideAngleFrequency": _constant(resolve, "SMART_SWITCH_WIDE_ANGLE_FREQ_" + _choice(wide_frequency, SS_FREQ, "wide_frequency")),
        "isUseWideAngleForIntroOutro": wide_for_intro_outro,
        "isUseWideAngleForSilence": wide_for_silence,
        "switchOnVideoOnly": video_only,
        "quality": _constant(resolve, "SMART_SWITCH_QUALITY_" + _choice(quality, {"better": "BETTER", "faster": "FASTER"}, "quality")),
    }
    if wide_angle == "auto":
        settings["isAutoDetectWideAngle"] = True
    else:
        settings.update(isAutoDetectWideAngle=False, wideAngleID=wide_angle or "None")
    if analysis is not None:
        settings["analysisMode"] = _constant(resolve, "SMART_SWITCH_ANALYSIS_MODE_" + _choice(analysis, SS_ANALYSIS, "analysis"))
    if not _method(it, "PerformMulticamSmartSwitch", "21.1")(settings):
        raise ToolError(f"Smart Switch failed on '{it.GetName()}' (a multicam clip with speech is needed; Studio only)")
    return f"Smart Switch cut '{it.GetName()}'"


@_tool
def flatten_multicam(item: int, grade: str = "copy", track: int = 1) -> str:
    """Replace a multicam clip on the timeline with its active angle's source clip (Resolve 21.1+). grade: copy (keep
    the multicam clip's grade) or angle (keep the grade of the angle's own clip). Item indexes may change after."""
    options = {"copy": "FLATTEN_MULTICAM_COPY_GRADE", "angle": "FLATTEN_MULTICAM_RETAIN_GRADE_FROM_ANGLE"}
    resolve = _resolve()
    _, tl = _timeline()
    it = _item(tl, item, track)
    name = it.GetName()  # the item is replaced by the flatten, so read it first
    if not _method(it, "FlattenMulticam", "21.1")(_constant(resolve, _choice(grade, options, "grade"))):
        raise ToolError(f"FlattenMulticam failed on '{name}' (is it a multicam clip?)")
    return f"flattened '{name}'"



# --- Sound effects and music ---
#
# Resolve's API has no music generation, beat detection or ducking. It does have AI voiceover (Resolve 21
# GenerateSpeech) and audio classification. Test/sync sounds are synthesized here as 24-bit WAV, and beats are
# detected here from WAV audio (pure Python, no dependencies); both are then used through the normal tools.

@_tool
def generate_voiceover(
    text: str,
    voice: str = "Female 1",
    speed: float = 0.0,
    pitch: float = 0.0,
    variation: float | None = None,
    custom_voice_file: str | None = None,
    file_name: str | None = None,
    add_to_timeline: bool = False,
    audio_track: int = 0,
) -> dict:
    """Generate a spoken voiceover clip with Resolve's AI Speech Generator (Resolve 21+, needs the "AI Speech
    Generator" Extras package). text up to 350 characters (split longer scripts into several clips); voice e.g.
    "Female 1", "Male 1", or "Custom Voice" with custom_voice_file; speed -10..10, pitch -2..2, variation 0..1.
    add_to_timeline places it at the playhead on audio_track (0 = a new track)."""
    if not text.strip():
        raise ToolError("no text")
    if len(text) > 350:
        raise ToolError(f"text is {len(text)} characters; Resolve accepts up to 350 per clip")
    if not -10 <= speed <= 10 or not -2 <= pitch <= 2 or (variation is not None and not 0 <= variation <= 1):
        raise ToolError("speed must be -10..10, pitch -2..2, variation 0..1")
    settings = {"TextInput": text, "VoiceModel": voice, "Speed": float(speed), "Pitch": float(pitch),
                "AddToTimeline": add_to_timeline, "AudioTrack": audio_track}
    if voice == "Custom Voice":
        if not custom_voice_file or not os.path.exists(custom_voice_file):
            raise ToolError("Custom Voice needs an existing custom_voice_file")
        settings["CustomVoiceFile"] = os.path.abspath(custom_voice_file)
    if variation is not None:
        settings["Variation"] = float(variation)
    if file_name:
        settings["Filename"] = file_name
    _, proj = _project()
    result = _method(proj, "GenerateSpeech", "21")(settings)
    # With the Extras package missing Resolve returns an explanatory STRING, which is truthy.
    if isinstance(result, str):
        raise ToolError(f"GenerateSpeech: {result}")
    if not result:
        raise ToolError("GenerateSpeech failed")
    return {"clip": result.GetName(), "voice": voice, "added_to_timeline": add_to_timeline}


UNCLASSIFIED = ("", "Uncategorized")


def _audio_class(c):
    cat = c.GetClipProperty("Category") or ""
    return {"clip": c.GetName(), "category": None if cat in UNCLASSIFIED else cat,
            "subcategory": (c.GetMetadata("Subcategory") or None) if cat not in UNCLASSIFIED else None}


@_tool
def classify_audio(clips: list[str] | None = None, bin: str | None = None) -> list[dict]:
    """Let Resolve analyze and label audio clips by category (e.g. Dialogue, Music, Effects) and subcategory, for the
    given clips or every clip in a bin (and its sub-bins). Resolve 21+."""
    if (clips is None) == (bin is None):
        raise ToolError("give clips or bin")
    _, proj = _project()
    if clips is not None:
        targets = _pool_clips(proj, clips)
        failed = [c.GetName() for c in targets if not _method(c, "PerformAudioClassification", "21")()]
        if failed:
            raise ToolError(f"classification failed for {', '.join(failed)}")
    else:
        folder = _bin(proj, bin)
        if not _method(folder, "PerformAudioClassification", "21")():
            raise ToolError(f"classification failed for bin {bin}")
        targets = [c for _, c in _walk(folder)]
    return [_audio_class(c) for c in targets]


@_tool
def find_audio(category: str | None = None, subcategory: str | None = None, name: str | None = None,
               bin: str | None = None) -> list[dict]:
    """Search the media pool (or a bin) for audio by the category/subcategory classify_audio assigned and/or a name
    fragment (case-insensitive), e.g. category="Music" or name="whoosh"."""
    _, proj = _project()
    folder = _bin(proj, bin) if bin else proj.GetMediaPool().GetRootFolder()
    out = []
    for path, c in _walk(folder):
        row = _audio_class(c)
        if category and (row["category"] or "").lower() != category.lower():
            continue
        if subcategory and (row["subcategory"] or "").lower() != subcategory.lower():
            continue
        if name and name.lower() not in c.GetName().lower():
            continue
        out.append({"bin": path or "/", **row})
    return out


SOUND_KINDS = ("tone", "pop", "beeps", "silence", "noise")


def _write_wav24(path, samples, rate, channels):
    frames = bytearray()
    for v in samples:
        b = int(max(-1.0, min(1.0, v)) * 8388607).to_bytes(3, "little", signed=True)
        frames += b * channels
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(3)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


@_tool
def generate_sound(
    kind: str,
    path: str,
    seconds: float = 1.0,
    level_db: float = -20.0,
    frequency: float = 1000.0,
    count: int = 3,
    fps: float | None = None,
    channels: int = 2,
    sample_rate: int = 48000,
    bin: str | None = None,
) -> dict:
    """Synthesize a utility sound as a 24-bit WAV: tone (reference tone, e.g. 1 kHz at -20 dBFS for bars and tone),
    pop (a 2-pop: one frame of tone, placed 2 s before program start), beeps (a countdown: `count` one-frame beeps a
    second apart), silence, or noise (white noise at level_db RMS, e.g. a room-tone placeholder). fps sets the frame
    length for pop/beeps (default: the current timeline's). bin imports the file into that media-pool bin
    ("/" for the root) and returns the clip name."""
    if kind not in SOUND_KINDS:
        raise ToolError(f"kind must be one of: {', '.join(SOUND_KINDS)}")
    if level_db > 0 or not 20 <= frequency <= sample_rate / 2 or channels not in (1, 2) or seconds <= 0:
        raise ToolError("level_db must be <= 0, frequency 20 Hz..Nyquist, channels 1 or 2, seconds > 0")
    path = os.path.abspath(path)
    if not path.lower().endswith(".wav"):
        raise ToolError("path must end in .wav")
    if not os.path.isdir(os.path.dirname(path)):
        raise ToolError(f"folder not found: {os.path.dirname(path)}")
    if kind in ("pop", "beeps") and fps is None:
        fps = float(_timeline()[1].GetSetting("timelineFrameRate") or 24)
    amp = 10 ** (level_db / 20)
    tone = lambda i: amp * math.sin(2 * math.pi * frequency * i / sample_rate)  # noqa: E731
    if kind == "tone":
        samples = [tone(i) for i in range(int(seconds * sample_rate))]
    elif kind == "pop":
        samples = [tone(i) for i in range(round(sample_rate / fps))]
        seconds = len(samples) / sample_rate
    elif kind == "beeps":
        if count < 1:
            raise ToolError("count must be at least 1")
        frame = round(sample_rate / fps)
        samples = [tone(i) if i % sample_rate < frame else 0.0 for i in range(count * sample_rate)]
        seconds = float(count)
    elif kind == "silence":
        samples = [0.0] * int(seconds * sample_rate)
    else:
        rnd = random.Random(0)
        samples = [amp * math.sqrt(3) * rnd.uniform(-1, 1) for _ in range(int(seconds * sample_rate))]
    _write_wav24(path, samples, sample_rate, channels)
    out = {"path": path, "kind": kind, "seconds": round(seconds, 4), "level_db": level_db}
    if bin is not None:
        out["clip"] = import_media([path], bin=None if bin == "/" else bin)[0]
    return out


def _wav_envelope(path, hop_seconds=0.01):
    """RMS envelope (one value per hop) of a PCM WAV, via fast 16-bit views of the sample bytes."""
    try:
        w = wave.open(path, "rb")
    except (wave.Error, EOFError) as e:
        raise ToolError(f"not a PCM WAV file: {os.path.basename(path)} ({e}); render or convert the music to WAV") from e
    with w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        pcm = array("h", raw)
    elif width in (3, 4):  # keep the top two bytes of each sample: same envelope, C-speed slicing
        hi = bytearray(len(raw) // width * 2)
        hi[0::2], hi[1::2] = raw[width - 2::width], raw[width - 1::width]
        pcm = array("h", bytes(hi))
    else:
        raise ToolError(f"unsupported WAV sample width: {8 * width}-bit")
    if sys.byteorder != "little":
        pcm.byteswap()
    hop = max(1, int(rate * hop_seconds)) * ch
    step = max(1, ch * (rate // 8000))  # ~8 kHz is plenty for an energy envelope
    env = []
    for i in range(0, len(pcm) - hop + 1, hop):
        chunk = pcm[i:i + hop:step]
        env.append(math.sqrt(math.fsum(x * x for x in chunk) / max(1, len(chunk))) / 32768.0)
    return env, n / rate


def _fit_grid(onset, hop, duration, period, phase, floor):
    """Snap each grid beat to its onset peak (within +-40 ms, sub-hop by parabolic interpolation), then fit a
    straight line through (beat number, time). Averaging over every beat removes the drift that a 10 ms-resolution
    period would build up over a long track. Returns the refined (period, phase)."""
    pts = []
    for k in range(int((duration - phase) / period) + 1):
        c = int(round((phase + k * period) / hop))
        lo, hi = max(1, c - 4), min(len(onset) - 1, c + 5)
        if lo >= hi:
            continue
        i = max(range(lo, hi), key=onset.__getitem__)
        if onset[i] <= floor:
            continue
        a, b, c_ = onset[i - 1], onset[i], onset[i + 1]
        den = a - 2 * b + c_
        pts.append((k, (i + (0.5 * (a - c_) / den if den else 0.0)) * hop))
    if len(pts) < 4:
        return period, phase
    n = len(pts)
    mk, mt = sum(k for k, _ in pts) / n, sum(t for _, t in pts) / n
    var = sum((k - mk) ** 2 for k, _ in pts)
    if not var:
        return period, phase
    period = sum((k - mk) * (t - mt) for k, t in pts) / var
    return period, (mt - period * mk) % period


def _beats(path, sensitivity=1.4, min_bpm=60.0, max_bpm=200.0):
    hop = 0.01
    env, duration = _wav_envelope(path, hop)
    if len(env) < 200:
        raise ToolError("audio too short for beat detection (needs at least 2 s)")
    flux = [0.0] + [max(0.0, b - a) for a, b in zip(env, env[1:])]
    mean = math.fsum(flux) / len(flux)
    onset = [max(0.0, f - mean) for f in flux]
    # Tempo: autocorrelation of the onset curve over the allowed beat periods.
    lags = range(int(60 / max_bpm / hop), int(60 / min_bpm / hop) + 1)
    scores = {lag: math.fsum(a * b for a, b in zip(onset, onset[lag:])) / (len(onset) - lag) for lag in lags}
    lag = max(scores, key=scores.get)
    if scores[lag] <= 0:
        raise ToolError("no rhythmic pattern found")
    # Refine to a fractional period around the best integer lag.
    period = lag * hop
    if lag - 1 in scores and lag + 1 in scores:
        a, b, c = scores[lag - 1], scores[lag], scores[lag + 1]
        denom = a - 2 * b + c
        if denom:
            period = (lag + 0.5 * (a - c) / denom) * hop
    # Phase: the grid offset that lands on the most onset energy.
    steps = int(round(period / hop))
    phase = max(range(steps), key=lambda p: math.fsum(onset[p::steps])) * hop
    floor = math.fsum(onset) / len(onset)
    for _ in range(3):  # each pass tightens the grid, so later beats land inside the snapping window
        period, phase = _fit_grid(onset, hop, duration, period, phase, floor)
    beats = []
    t = phase
    while t < duration:
        beats.append(round(t, 3))
        t += period
    # Strong individual hits (accents, drops): local maxima well above their neighbourhood.
    w = int(0.5 / hop)
    hits = [round(i * hop, 3) for i in range(1, len(onset) - 1)
            if onset[i] > 0 and onset[i] >= onset[i - 1] and onset[i] > onset[i + 1]
            and onset[i] > sensitivity * math.fsum(onset[max(0, i - w):i + w]) / (2 * w)]
    return {"bpm": round(60 / period, 2), "beats": beats, "hits": hits, "duration": round(duration, 3)}


def _clip_file(proj, clip):
    (c,) = _pool_clips(proj, [clip])
    path = c.GetClipProperty("File Path")
    if not path or not os.path.exists(path):
        raise ToolError(f"{clip}'s file is not reachable: {path}")
    return path


@_tool
def detect_beats(clip: str | None = None, path: str | None = None, sensitivity: float = 1.4,
                 min_bpm: float = 60.0, max_bpm: float = 200.0) -> dict:
    """Find the tempo and beats of a music track (a media-pool clip, or a WAV path): bpm, a regular beat grid in
    seconds from the start of the file, and strong hits (accents, drops) above `sensitivity` x their surroundings.
    WAV (PCM 16/24/32-bit) only; for other formats, render an audio-only WAV first. Detection runs here, not in
    Resolve."""
    if (clip is None) == (path is None):
        raise ToolError("give clip or path")
    if not 20 <= min_bpm < max_bpm <= 300:
        raise ToolError("need 20 <= min_bpm < max_bpm <= 300")
    if clip is not None:
        path = _clip_file(_project()[1], clip)
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ToolError(f"file not found: {path}")
    out = _beats(path, sensitivity, min_bpm, max_bpm)
    return {"file": os.path.basename(path), **out}


@_tool
def mark_beats(item: int, track: int = 1, every: int = 1, color: str = "Yellow", hits: bool = False,
               sensitivity: float = 1.4, max_markers: int = 500) -> dict:
    """Put timeline markers on the beats of a music clip that is on the timeline (item: 1-based index on audio
    `track`), for cutting to the music. every=4 marks every 4th beat (one per bar in 4/4); hits=True marks the strong
    hits instead of the grid. Markers are placed only where the clip plays, using its trim; frames where a marker
    already exists are skipped."""
    if every < 1:
        raise ToolError("every must be at least 1")
    if color not in MARKER_COLORS:
        raise ToolError(f"unknown marker color: {color}")
    proj, tl = _timeline()
    it = _pick(tl, item, track, "audio")
    mpi = _opt(it, "GetMediaPoolItem")
    if not mpi:
        raise ToolError(f"'{it.GetName()}' has no source clip")
    found = _beats(os.path.abspath(_clip_file(proj, mpi.GetName())), sensitivity)
    fps = float(tl.GetSetting("timelineFrameRate") or 24)
    src_in = _opt(it, "GetSourceStartTime")  # seconds into the source; audio frame counts are unreliable
    if src_in is None:
        src_in = float(_opt(it, "GetLeftOffset") or 0) / fps
    length = it.GetDuration() / fps
    times = found["hits"] if hits else found["beats"][::every]
    base = it.GetStart() - tl.GetStartFrame()
    placed, skipped = [], 0
    for n, t in enumerate(times, 1):
        if not src_in <= t < src_in + length:
            continue
        frame = base + round((t - src_in) * fps)
        if len(placed) >= max_markers:
            break
        if tl.AddMarker(frame, color, f"beat {n}", "", 1, json.dumps({"beat": n, "bpm": found["bpm"]})):
            placed.append(frame)
        else:
            skipped += 1
    return {"item": it.GetName(), "bpm": found["bpm"], "markers": len(placed), "skipped": skipped,
            "first_frames": placed[:8]}



# --- Transitions ---


def _is_transition(items, i):
    kind = _opt(items[i], "GetType")  # 21.1: native lowercase type
    return kind == "transition" if kind else _kind(items, i) == "transition"


@_tool
def list_transitions(track: int = 1, track_type: str = "video") -> list[dict]:
    """Transitions on a track: item index, name (the transition type), start, end, duration, and the clips it joins."""
    _, tl = _timeline()
    items = _track_items(tl, track_type, track)
    out = []
    for i, it in enumerate(items):
        if not _is_transition(items, i):
            continue
        before = next((x.GetName() for x in reversed(items[:i]) if not _is_transition(items, items.index(x))), None)
        after = next((x.GetName() for x in items[i + 1:] if not _is_transition(items, items.index(x))), None)
        out.append({"index": i + 1, "name": it.GetName(), "start": it.GetStart(), "end": it.GetEnd(),
                    "duration": it.GetDuration(), "between": [before, after]})
    return out


@_tool
def transition_all_cuts(
    type: str = "Cross Dissolve",
    duration: int | None = 12,
    alignment: str = "center",
    category: str = "simple",
    track: int = 1,
    track_type: str = "video",
) -> dict:
    """Put the same transition on every cut of a track in one call (Resolve 21.1+): every place where a clip ends
    exactly where the next clip starts, skipping cuts that already have a transition. Cuts whose clips have no handles
    (unused media past the cut) are reported, not fatal."""
    if alignment not in ("left", "center", "right"):
        raise ToolError("alignment must be left, center or right")
    if category not in TRANSITION_CATEGORIES:
        raise ToolError(f"category must be one of: {', '.join(TRANSITION_CATEGORIES)}")
    if duration is not None and duration < 1:
        raise ToolError("duration must be a positive number of frames")
    _, tl = _timeline()
    items = _track_items(tl, track_type, track)
    clips = [it for i, it in enumerate(items) if not _is_transition(items, i)]
    cuts = [(a, b) for a, b in zip(clips, clips[1:]) if a.GetEnd() == b.GetStart()]
    options = {"type": type, "category": category, "position": "end", "alignment": alignment}
    if duration is not None:
        options["duration"] = duration
    added, failed, skipped = [], [], 0
    for a, b in cuts:  # clip objects stay valid while new transition items shift the indexes
        if _covered(tl, track_type, track, a.GetEnd()):
            skipped += 1
            continue
        tr = _method(a, "AddTransition", "21.1")(options)
        (added if tr else failed).append(f"{a.GetName()} | {b.GetName()}")
    if cuts and not added and failed:
        raise ToolError(f"no transition added: check '{type}' is an installed {category} transition and that the clips "
                        "have handles")
    return {"cuts": len(cuts), "added": len(added), "skipped_existing": skipped, "failed_no_handles": failed}


def _covered(tl, track_type, track, frame):
    items = _track_items(tl, track_type, track)
    return any(_is_transition(items, i) and it.GetStart() <= frame <= it.GetEnd() for i, it in enumerate(items))


@_tool
def remove_transitions(items: list[int] | None = None, track: int = 1, track_type: str = "video") -> str:
    """Remove transitions from a track: the given item indexes (see list_transitions), or all of them. The clips
    stay where they are."""
    resolve = _resolve()
    _, tl = _timeline()
    all_items = _track_items(tl, track_type, track)
    found = [it for i, it in enumerate(all_items) if _is_transition(all_items, i)]
    if items is not None:
        chosen = []
        for i in items:
            if not 1 <= i <= len(all_items) or not _is_transition(all_items, i - 1):
                raise ToolError(f"item {i} on {track_type} track {track} is not a transition")
            chosen.append(all_items[i - 1])
        found = chosen
    if not found:
        return "no transitions to remove"
    with _on_page(resolve, "edit"):
        ok = tl.DeleteClips(found, False)
    if not ok:
        raise ToolError("DeleteClips failed")
    return f"removed {len(found)} transition(s)"


# --- Visual effects ---


def _resolution(tl):
    try:
        return int(tl.GetSetting("timelineResolutionWidth")), int(tl.GetSetting("timelineResolutionHeight"))
    except (TypeError, ValueError):
        raise ToolError("cannot read the timeline resolution") from None


@_tool
def letterbox(aspect: float | None = 2.39, item: int | None = None, track: int = 1) -> dict:
    """Cinematic bars through Resolve's output blanking (Resolve 21.1+): black bars cropping the picture to `aspect`
    (e.g. 2.39, 2.0, 1.85; pillarbox bars when narrower than the frame), exact to the pixel. Applies to the whole
    timeline, or to one video item. aspect=None removes it (the item goes back to the timeline's blanking)."""
    _, tl = _timeline()
    w, h = _resolution(tl)
    if aspect is not None and not 0.2 <= aspect <= 10:
        raise ToolError("aspect must be between 0.2 and 10")
    target = _item(tl, item, track) if item is not None else tl
    label = f"'{target.GetName()}'" if item is not None else f"timeline '{tl.GetName()}'"
    if aspect is None:
        if item is not None:
            if not _method(target, "SetUseTimelineForOutputBlanking", "21.1")(True):
                raise ToolError("could not return the item to the timeline's blanking")
            return {"target": label, "blanking": "timeline's"}
        bounds = {"Top": 0, "Bottom": h, "Left": 0, "Right": w}
    elif w / h > aspect:  # pillarbox
        pic_w = round(h * aspect)
        bounds = {"Top": 0, "Bottom": h, "Left": (w - pic_w) // 2, "Right": (w - pic_w) // 2 + pic_w}
    else:
        pic_h = round(w / aspect)
        bounds = {"Top": (h - pic_h) // 2, "Bottom": (h - pic_h) // 2 + pic_h, "Left": 0, "Right": w}
    if item is not None:
        # A clip override only takes once the clip stops inheriting the timeline's blanking.
        if not _method(target, "SetUseTimelineForOutputBlanking", "21.1")(False):
            raise ToolError(f"could not give {label} its own blanking")
    if not _method(target, "SetOutputBlanking", "21.1")(bounds):
        raise ToolError(f"SetOutputBlanking failed on {label}")
    return {"target": label, "aspect": aspect, "resolution": [w, h], "bounds": bounds}


PIP_CORNERS = {"top_right": (1, 1), "top_left": (-1, 1), "bottom_right": (1, -1), "bottom_left": (-1, -1),
               "center": (0, 0)}


@_tool
def picture_in_picture(item: int, scale: float = 0.35, corner: str = "top_right", margin: float = 0.04,
                       track: int = 2) -> dict:
    """Shrink a clip on an upper track into a corner over the picture below: scale 0.05-1 of full frame, corner
    top_right, top_left, bottom_right, bottom_left or center, margin as a fraction of the frame. Uses the clip's
    Edit-page Zoom and Position, so it can be adjusted in the Inspector afterwards."""
    if not 0.05 <= scale <= 1:
        raise ToolError("scale must be 0.05-1")
    if not 0 <= margin <= 0.4:
        raise ToolError("margin must be 0-0.4")
    sx, sy = PIP_CORNERS.get(corner, (None, None))
    if sx is None:
        raise ToolError(f"corner must be one of: {', '.join(PIP_CORNERS)}")
    _, tl = _timeline()
    w, h = _resolution(tl)
    pan = sx * (w * (1 - scale) / 2 - margin * w)
    tilt = sy * (h * (1 - scale) / 2 - margin * h)
    props = {"ZoomX": scale, "ZoomY": scale, "Pan": round(pan, 1), "Tilt": round(tilt, 1)}
    return set_item_properties(item, props, track=track)


@_tool
def split_screen(left: int, right: int, left_track: int = 2, right_track: int = 1, gap: float = 0.0) -> dict:
    """Show two clips side by side: `left` (on left_track) and `right` (on right_track), each cropped to half the
    frame width and moved to its half, with an optional `gap` (fraction of the frame width) between them. Put the two
    clips on different tracks at the same time. Uses Edit-page Crop and Position."""
    if not 0 <= gap < 0.5:
        raise ToolError("gap must be 0-0.5")
    _, tl = _timeline()
    w, _ = _resolution(tl)
    half = w / 2
    crop = w / 4 + gap * w / 2  # a quarter of the picture off each side, plus half the gap on each clip
    shift = round(w / 4, 1)
    out = {
        "left": set_item_properties(left, {"CropLeft": round(crop, 1), "CropRight": round(crop, 1), "Pan": -shift},
                                    track=left_track),
        "right": set_item_properties(right, {"CropLeft": round(crop, 1), "CropRight": round(crop, 1), "Pan": shift},
                                     track=right_track),
    }
    return {"half_width": half, **out}


@_tool
def vignette(item: int, amount: float = 0.5, size: float = 0.85, softness: float = 0.35, track: int = 1) -> dict:
    """Darken the edges of a video clip: amount 0-1 (how dark the corners get), size 0.2-2 of the frame, softness
    0-1. Built in the clip's Fusion comp as an inverted ellipse mask driving a BrightnessContrast node named Vignette;
    refine with set_fusion_input. Experimental: input names not yet confirmed on a live Resolve."""
    if not 0 < amount <= 1 or not 0.2 <= size <= 2 or not 0 <= softness <= 1:
        raise ToolError("amount must be 0-1, size 0.2-2, softness 0-1")
    _, tl = _timeline()
    it = _item(tl, item, track)
    c = _clip_comp(it)
    if c.FindTool("Vignette"):
        raise ToolError("this clip already has a Vignette node; change it with set_fusion_input")
    darken = _insert_before_output(c, "BrightnessContrast", "Vignette")
    with _locked(c):
        mask = c.AddTool("EllipseMask", -1, -1)
        if not mask:
            raise ToolError("could not add EllipseMask")
        mask.SetAttrs({"TOOLS_Name": "VignetteMask"})
        if not darken.ConnectInput("EffectMask", mask):
            raise ToolError("could not connect the mask to Vignette")
    applied = {}
    for node, tool, key, value in (("VignetteMask", mask, "Width", size), ("VignetteMask", mask, "Height", size),
                                   ("VignetteMask", mask, "SoftEdge", softness), ("VignetteMask", mask, "Invert", 1),
                                   ("Vignette", darken, "Gain", 1 - amount)):
        applied[f"{node}.{key}"] = _set_input(c, tool, node, key, value=value)["value"]
    return {"item": it.GetName(), "nodes": ["Vignette", "VignetteMask"], "set": applied}


@_tool
def camera_shake(item: int, amount: float = 0.01, every: int = 2, seed: int = 1, track: int = 1) -> dict:
    """Handheld/impact camera shake on a video clip: random position jitter of `amount` (fraction of the frame,
    e.g. 0.005 subtle, 0.02 strong) keyframed every `every` frames, with a matching zoom so no edge shows. Same seed,
    same shake. Built on the clip's Fusion Transform node Motion (shared with animate_clip)."""
    if not 0 < amount <= 0.1 or every < 1:
        raise ToolError("amount must be 0-0.1 and every at least 1")
    _, tl = _timeline()
    it = _item(tl, item, track)
    c = _clip_comp(it)
    first, last = _comp_range(c, it)
    rnd = random.Random(seed)
    frames = list(range(0, last - first + 1, every))
    if frames[-1] != last - first:
        frames.append(last - first)
    center = {f: [round(0.5 + rnd.uniform(-amount, amount), 5), round(0.5 + rnd.uniform(-amount, amount), 5)]
              for f in frames}
    tool = c.FindTool("Motion") or _insert_before_output(c, "Transform", "Motion")
    _set_input(c, tool, "Motion", "Center", keyframes={first + f: v for f, v in center.items()})
    out = {"item": it.GetName(), "node": "Motion", "keyframes": len(frames), "seed": seed}
    if _source(tool["Size"])[1] in ANIMATION_MODIFIERS:
        out["zoom"] = "left as animated; keep it at least %.3f so no edge shows" % (1 + 2 * amount)
    else:
        out["zoom"] = _set_input(c, tool, "Motion", "Size", value=1 + 2 * amount)["value"]
    return out



# --- Advanced grading: node graphs, color groups, PowerGrades, DCTL ---
#
# Grade writes run on the Color page (measured: ApplyGradeFromDRX and AddVersion return False for every clip from
# the Edit page), switching there and back. The API cannot add, connect or tune nodes (lift/gamma/gain, curves,
# qualifiers, windows): grades are shaped with CDL, LUTs and .drx stills, and organised with groups and versions.

GROUP_STAGES = ("pre", "post")


def _find_group(proj, name):
    for g in proj.GetColorGroupsList() or []:
        if g.GetName() == name:
            return g
    raise ToolError(f"color group not found: {name} (see color_groups)")


def _graph_target(item, track, layer, group, stage, timeline_grade):
    """(graph, label) for a clip's layer, a color group's pre/post-clip grade, or the timeline grade."""
    if sum(x is not None and x is not False for x in (item, group, timeline_grade or None)) != 1:
        raise ToolError("give exactly one of item, group or timeline_grade=True")
    proj, tl = _timeline()
    if item is not None:
        it = _item(tl, item, track)
        graph = it.GetNodeGraph(layer) if layer != 1 else _graph(it)
        if not graph:
            raise ToolError(f"'{it.GetName()}' has no node layer {layer}")
        return graph, f"'{it.GetName()}'" + (f" layer {layer}" if layer != 1 else "")
    if group is not None:
        if stage not in GROUP_STAGES:
            raise ToolError("stage must be pre or post")
        g = _find_group(proj, group)
        graph = (g.GetPreClipNodeGraph if stage == "pre" else g.GetPostClipNodeGraph)()
        return graph, f"group '{group}' {stage}-clip"
    return _method(tl, "GetNodeGraph", "21.1")(), f"timeline '{tl.GetName()}'"


def _node_in(graph, node, label):
    count = int(graph.GetNumNodes() or 0)
    if not 1 <= node <= count:
        raise ToolError(f"node {node} out of range ({label} has {count} node(s))")


@_tool
def node_graph(item: int | None = None, group: str | None = None, stage: str = "pre", timeline_grade: bool = False,
               layer: int = 1, track: int = 1) -> dict:
    """The nodes of a grade: for a clip (item, node-stack layer), a color group (group, stage pre or post-clip), or the
    whole timeline (timeline_grade=True, Resolve 21.1+). Each node: index, label, the tools used in it (e.g.
    Primaries, Curves, Qualifier, Window), its LUT and cache mode."""
    graph, label = _graph_target(item, track, layer, group, stage, timeline_grade)
    nodes = []
    for n in range(1, int(graph.GetNumNodes() or 0) + 1):
        nodes.append({"index": n, "label": graph.GetNodeLabel(n) or "", "tools": list(_opt(graph, "GetToolsInNode", n) or []),
                      "lut": graph.GetLUT(n) or None, "cache": _opt(graph, "GetNodeCacheMode", n)})
    return {"graph": label, "nodes": nodes}


@_tool
def set_node_lut(node: int, lut_path: str, item: int | None = None, group: str | None = None, stage: str = "pre",
                 timeline_grade: bool = False, layer: int = 1, track: int = 1) -> str:
    """Put a LUT on a node of a clip, color group (pre/post-clip) or timeline grade. lut_path as in apply_lut: relative
    to Resolve's LUT folders, or an absolute file that gets installed into the master LUT folder."""
    graph, label = _graph_target(item, track, layer, group, stage, timeline_grade)
    _node_in(graph, node, label)
    with _on_page(_resolve(), "color"):
        used = _set_lut(_project()[1], graph, node, lut_path)
    return f"LUT on node {node} of {label}: {used}"


@_tool
def set_node_enabled(node: int, enabled: bool, item: int | None = None, group: str | None = None, stage: str = "pre",
                     timeline_grade: bool = False, layer: int = 1, track: int = 1) -> str:
    """Bypass (enabled=False) or re-enable one node of a clip, color group or timeline grade, e.g. to compare with and
    without a look. Resolve has no way to read a node's enabled state back; check with view_frame."""
    graph, label = _graph_target(item, track, layer, group, stage, timeline_grade)
    _node_in(graph, node, label)
    with _on_page(_resolve(), "color"):
        ok = graph.SetNodeEnabled(node, enabled)
    if not ok:
        raise ToolError(f"SetNodeEnabled failed on node {node} of {label}")
    return f"node {node} of {label} {'enabled' if enabled else 'bypassed'} (not readable back; verify with view_frame)"


@_tool
def reset_grade(item: int | None = None, group: str | None = None, stage: str = "pre", timeline_grade: bool = False,
                layer: int = 1, track: int = 1) -> str:
    """Reset every node of a clip's, color group's or timeline's grade to neutral. Save a version first
    (add_color_version) to be able to go back."""
    graph, label = _graph_target(item, track, layer, group, stage, timeline_grade)
    with _on_page(_resolve(), "color"):
        ok = graph.ResetAllGrades()
    if not ok:
        raise ToolError(f"ResetAllGrades failed on {label}")
    return f"grade of {label} reset"


@_tool
def apply_drx_to(path: str, group: str | None = None, stage: str = "pre", timeline_grade: bool = False,
                 keyframes: str = "none") -> str:
    """Apply a .drx grade still to a color group's pre/post-clip grade or to the timeline grade (for clips use
    apply_drx). keyframes: none, source_timecode or start_frames."""
    if keyframes not in DRX_MODES:
        raise ToolError(f"unknown keyframes mode: {keyframes} (one of {', '.join(DRX_MODES)})")
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ToolError(f"file not found: {path}")
    graph, label = _graph_target(None, 1, 1, group, stage, timeline_grade)
    with _on_page(_resolve(), "color"):
        ok = graph.ApplyGradeFromDRX(path, DRX_MODES[keyframes])
    if not ok:
        raise ToolError(f"ApplyGradeFromDRX failed on {label}")
    return f"applied {os.path.basename(path)} to {label}"


@_tool
def color_groups() -> list[dict]:
    """The project's color groups and, for each, its clips on the current timeline (video track and index)."""
    proj, tl = _timeline()
    # Matched by GetUniqueId: each API call hands back a new wrapper, so id() never matches (live 21.1).
    where = {}
    for n in range(1, int(tl.GetTrackCount("video") or 0) + 1):
        for i, it in enumerate(tl.GetItemListInTrack("video", n) or [], 1):
            where[_opt(it, "GetUniqueId") or id(it)] = (n, i)
    out = []
    for g in proj.GetColorGroupsList() or []:
        clips = []
        for c in g.GetClipsInTimeline(tl) or []:
            track, index = where.get(_opt(c, "GetUniqueId") or id(c), (None, None))
            clips.append({"track": track, "item": index, "name": c.GetName()})
        out.append({"group": g.GetName(), "clips": clips})
    return out


@_tool
def create_color_group(name: str) -> str:
    """Create a color group: clips in it share a pre-clip and post-clip grade (grade a scene or camera once)."""
    _, proj = _project()
    if any(g.GetName() == name for g in proj.GetColorGroupsList() or []):
        raise ToolError(f"a color group named {name} already exists")
    if not proj.AddColorGroup(name):
        raise ToolError(f"could not create color group {name}")
    return f"created color group {name}"


@_tool
def delete_color_group(name: str) -> str:
    """Delete a color group; its clips become ungrouped and keep their own clip grades."""
    _, proj = _project()
    if not proj.DeleteColorGroup(_find_group(proj, name)):
        raise ToolError(f"could not delete color group {name}")
    return f"deleted color group {name}"


@_tool
def assign_color_group(items: list[int], group: str | None, track: int = 1) -> dict:
    """Put video items (1-based indexes) into a color group, or take them out of theirs (group=None)."""
    proj, tl = _timeline()
    targets = [_item(tl, i, track) for i in items]
    if not targets:
        raise ToolError("no items given")
    g = _find_group(proj, group) if group is not None else None
    with _on_page(_resolve(), "color"):
        failed = [t.GetName() for t in targets if not (t.AssignToColorGroup(g) if g else t.RemoveFromColorGroup())]
    if failed:
        raise ToolError(f"could not {'assign' if g else 'ungroup'}: {', '.join(failed)}")
    return {"group": group, "items": [t.GetName() for t in targets]}


@_tool
def apply_arri_cdl_lut(items: list[int], track: int = 1) -> dict:
    """Apply the ARRI look embedded in ARRI camera clips (their CDL and LUT metadata) to their grades."""
    _, tl = _timeline()
    targets = [_item(tl, i, track) for i in items]
    if not targets:
        raise ToolError("no items given")
    with _on_page(_resolve(), "color"):
        failed = [t.GetName() for t in targets if not _graph(t).ApplyArriCdlLut()]
    if failed:
        raise ToolError(f"ApplyArriCdlLut failed on {', '.join(failed)} (ARRI clips with look metadata only)")
    return {"applied": [t.GetName() for t in targets]}


@_tool
def color_cache(items: list[int], enabled: bool = True, track: int = 1) -> dict:
    """Render-cache the color output of video items (Resolve's 'Render Cache Color Output'), for smooth playback of
    heavy grades; enabled=False turns it off."""
    _, tl = _timeline()
    targets = [_item(tl, i, track) for i in items]
    failed = [t.GetName() for t in targets if not t.SetColorOutputCache(enabled)]
    if failed:
        raise ToolError(f"SetColorOutputCache failed on {', '.join(failed)}")
    return {"items": [t.GetName() for t in targets], "color_cache": enabled}


@_tool
def gallery_albums() -> dict:
    """Gallery albums: still albums and PowerGrade albums (grades shared across projects), with the labels of their
    stills."""
    _, proj = _project()
    gallery = proj.GetGallery()

    def rows(albums):
        return [{"name": gallery.GetAlbumName(a), "stills": [a.GetLabel(st) for st in a.GetStills() or []]}
                for a in albums or []]

    return {"still_albums": rows(gallery.GetGalleryStillAlbums()),
            "powergrade_albums": rows(_opt(gallery, "GetGalleryPowerGradeAlbums"))}


@_tool
def import_stills(paths: list[str], album: str | None = None, powergrade: bool = False) -> dict:
    """Import grade stills (.drx, or .dpx with its .drx) into a gallery album, by name, or into a new PowerGrade
    album (powergrade=True with no album), to reuse looks across projects."""
    paths = [os.path.abspath(x) for x in paths]
    missing = [x for x in paths if not os.path.exists(x)]
    if missing or not paths:
        raise ToolError(f"file(s) not found: {', '.join(missing) or 'none given'}")
    _, proj = _project()
    gallery = proj.GetGallery()
    albums = list(gallery.GetGalleryStillAlbums() or []) + list(_opt(gallery, "GetGalleryPowerGradeAlbums") or [])
    if album is not None:
        target = next((a for a in albums if gallery.GetAlbumName(a) == album), None)
        if target is None:
            raise ToolError(f"album not found: {album} (see gallery_albums)")
    elif powergrade:
        target = _method(gallery, "CreateGalleryPowerGradeAlbum", "18")()
    else:
        target = gallery.GetCurrentStillAlbum()
    before = len(target.GetStills() or [])
    if not _method(target, "ImportStills", "18")(paths):
        raise ToolError("ImportStills failed")
    return {"album": gallery.GetAlbumName(target), "imported": len(target.GetStills() or []) - before}


@_tool
def validate_dctl(source: str) -> dict:
    """Check a DCTL shader's source with Resolve's own compiler front-end (Resolve 21.1+). Returns valid and
    Resolve's diagnostic verbatim. Keep the usual multi-line layout: the validator was measured to misread a whole
    function written on one line. Validation is not a rendered test."""
    result = _method(_resolve(), "ValidateDCTL", "21.1")(source)
    if result is not None and not isinstance(result, str):
        raise ToolError(f"unexpected ValidateDCTL result: {result!r}")
    return {"valid": result is None, "diagnostic": result}



# --- AI editing ---
#
# Resolve's neural features that the API reaches beyond the ones above (transcription, captions, Magic Mask, Smart
# Reframe, scene cuts, voice isolation, audio classification, speech generation): Super Scale and Speed Warp. Plus
# cut editing driven by analysis: pauses found in the audio here, words from Resolve's own transcript. Cut tools
# never touch the source: they build a new timeline from the kept ranges of the clip.

SUPER_SCALE = {"auto": 0, "none": 1, "2x": 2, "3x": 3, "4x": 4}
FILLER_WORDS = ("um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah", "hmm", "mm", "mhm",
                "ام", "امم", "اممم", "إمم", "مم", "ممم", "اه", "آه", "اها")


@_tool
def super_scale(clips: list[str], scale: str = "2x", sharpness: float | None = None,
                noise_reduction: float | None = None) -> list[dict]:
    """AI upscaling of media-pool clips (Studio): scale auto, none, 2x, 3x or 4x. Giving sharpness and
    noise_reduction (0-1, both) selects 2x Enhanced. It applies wherever the clip is used; set a higher timeline or
    render resolution to benefit. Returns the property as Resolve reads it back."""
    if scale not in SUPER_SCALE:
        raise ToolError(f"scale must be one of: {', '.join(SUPER_SCALE)}")
    enhanced = sharpness is not None or noise_reduction is not None
    if enhanced:
        if scale != "2x":
            raise ToolError("sharpness and noise_reduction select 2x Enhanced: use scale='2x'")
        if sharpness is None or noise_reduction is None:
            raise ToolError("2x Enhanced needs both sharpness and noise_reduction")
        if not (0 <= sharpness <= 1 and 0 <= noise_reduction <= 1):
            raise ToolError("sharpness and noise_reduction must be between 0 and 1")
    _, proj = _project()
    args = (SUPER_SCALE[scale],) + ((float(sharpness), float(noise_reduction)) if enhanced else ())
    out = []
    for c in _pool_clips(proj, clips):
        ok = c.SetClipProperty("Super Scale", *args)
        now = c.GetClipProperty("Super Scale")
        if not ok:
            raise ToolError(f"{c.GetName()}: Resolve refused Super Scale {scale} (Studio only); it reads {now!r}")
        out.append({"clip": c.GetName(), "super_scale": now, "enhanced": enhanced})
    return out


@_tool
def ai_slow_motion(item: int, percent: float = 50, engine: str = "speed_warp", ripple: bool = False,
                   track: int = 1) -> dict:
    """Smooth slow motion with interpolated frames (Resolve 21.1+): slows a video item to `percent` (below 100)
    and sets its retiming to optical flow with the given motion engine: speed_warp (neural, Studio),
    enhanced_better, enhanced_faster, standard_better or standard_faster."""
    engines = PROPERTY_ENUMS["MotionEstimation"][1:]
    if not 0 < percent < 100:
        raise ToolError("percent must be above 0 and below 100 for slow motion (use set_speed otherwise)")
    if engine not in engines:
        raise ToolError(f"engine must be one of: {', '.join(engines)}")
    out = set_speed(item, percent, ripple=ripple, track=track)
    _, tl = _timeline()
    it = _item(tl, item, track)
    props = {"RetimeProcess": _prop_value("RetimeProcess", "optical_flow"),
             "MotionEstimation": _prop_value("MotionEstimation", engine)}
    failed = [k for k, v in props.items() if not it.SetProperty(k, v)]
    if failed:
        raise ToolError(f"speed is now {percent}% but Resolve refused {', '.join(failed)}"
                        + (" (Speed Warp is Studio only)" if engine == "speed_warp" else ""))
    return {**out, "retime": "optical_flow", "motion_estimation": engine}


def _audio_wav(path):
    """(PCM WAV path, temp dir to delete or None): the file itself, or its audio converted by ffmpeg."""
    if path.lower().endswith((".wav", ".wave")):
        return path, None
    ff = shutil.which("ffmpeg")
    if not ff:
        raise ToolError(f"{os.path.basename(path)} is not a WAV and ffmpeg is not installed to read its audio "
                        "(install ffmpeg, or render the audio to WAV and import it)")
    tmp = tempfile.mkdtemp(prefix="davinci_mcp_")
    out = os.path.join(tmp, "audio.wav")
    try:
        r = subprocess.run([ff, "-v", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                            "-c:a", "pcm_s16le", out], capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ToolError(f"ffmpeg failed on {os.path.basename(path)}: {e}") from e
    if r.returncode or not os.path.exists(out):
        shutil.rmtree(tmp, ignore_errors=True)
        raise ToolError(f"ffmpeg could not read audio from {os.path.basename(path)}: {r.stderr.strip()[:300]}")
    return out, tmp


def _pauses(env, hop, duration, threshold_db, min_silence):
    """(start, end) seconds of runs quieter than threshold_db lasting at least min_silence."""
    floor = 10 ** (threshold_db / 20)
    out, i, n = [], 0, len(env)
    while i < n:
        if env[i] > floor:
            i += 1
            continue
        j = i
        while j < n and env[j] <= floor:
            j += 1
        if (j - i) * hop >= min_silence:
            out.append((i * hop, duration if j >= n else j * hop))
        i = j
    return out


def _keep_between(pauses, duration, padding):
    """Complement of the pauses over [0, duration], each pause shrunk by `padding` on the sides next to sound."""
    keep, t = [], 0.0
    for a, b in pauses:
        a2 = a if a <= 0 else a + padding
        b2 = b if b >= duration else b - padding
        if b2 <= a2:
            continue
        if a2 > t:
            keep.append((t, a2))
        t = b2
    if duration > t:
        keep.append((t, duration))
    return keep


def _clip_fps(proj, clip):
    try:
        return float(clip.GetClipProperty("FPS") or 0) or float(proj.GetSetting("timelineFrameRate"))
    except (TypeError, ValueError):
        raise ToolError(f"cannot read a frame rate for {clip.GetName()}") from None


def _cut_timeline(proj, clip, ranges, name):
    """New current timeline made of the clip's frame ranges ([start, end) in clip frames), in order."""
    if not ranges:
        raise ToolError("nothing left to keep")
    pool = proj.GetMediaPool()
    tl = pool.CreateEmptyTimeline(name)
    if not tl:
        raise ToolError(f"cannot create timeline {name!r} (the name may be taken; pass timeline=)")
    proj.SetCurrentTimeline(tl)
    # endFrame is exclusive (measured on 21.1), matching the [start, end) ranges.
    infos = [{"mediaPoolItem": clip, "startFrame": a, "endFrame": b} for a, b in ranges]
    if not pool.AppendToTimeline(infos):
        raise ToolError(f"timeline {name!r} was created but appending the kept parts failed")
    return tl


def _to_frames(ranges_s, fps, total):
    out = []
    for a, b in ranges_s:
        fa, fb = max(0, int(math.floor(a * fps))), min(total, int(math.ceil(b * fps)))
        if out and fa <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], fb))
        elif fb > fa:
            out.append((fa, fb))
    return out


def _cut_report(name, ranges, fps, total):
    kept = sum(b - a for a, b in ranges)
    return {"timeline": name, "parts": len(ranges), "kept_seconds": round(kept / fps, 2),
            "removed_seconds": round((total - kept) / fps, 2),
            "kept": [[round(a / fps, 2), round(b / fps, 2)] for a, b in ranges[:200]]}


@_tool
def remove_silences(clip: str, threshold_db: float = -40.0, min_silence: float = 0.5, padding: float = 0.1,
                    timeline: str | None = None) -> dict:
    """Jump-cut a talking clip: find the pauses in its audio (quieter than threshold_db for at least min_silence
    seconds) and build a new timeline from the parts between them, leaving `padding` seconds of each pause so
    words are not clipped. Raise threshold_db (e.g. -35) for noisy rooms. The clip and existing timelines are
    untouched. WAV is read directly, other formats through ffmpeg when installed; the analysis runs here."""
    if not -90 <= threshold_db <= -10:
        raise ToolError("threshold_db must be between -90 and -10")
    if min_silence < 0.1:
        raise ToolError("min_silence must be at least 0.1 seconds")
    if not 0 <= padding < min_silence / 2:
        raise ToolError("padding must be 0 or more and under half of min_silence")
    _, proj = _project()
    (c,) = _pool_clips(proj, [clip])
    wav, tmp = _audio_wav(_clip_file(proj, clip))
    try:
        env, duration = _wav_envelope(wav)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    hop = duration / len(env) if env else 0.01
    fps = _clip_fps(proj, c)
    total = int(float(c.GetClipProperty("Frames") or 0)) or int(round(duration * fps))
    pauses = _pauses(env, hop, duration, threshold_db, min_silence)
    if not pauses:
        raise ToolError(f"no pause of {min_silence}s under {threshold_db} dB found; nothing to cut")
    ranges = _to_frames(_keep_between(pauses, duration, padding), fps, total)
    name = timeline or f"{c.GetName()} - no silences"
    _cut_timeline(proj, c, ranges, name)
    return {**_cut_report(name, ranges, fps, total), "pauses_found": len(pauses)}


def _token(text):
    return (text or "").strip().strip(".,!?;:،؛؟…\"'()[]").lower()


@_tool
def cut_by_transcript(clip: str, remove_fillers: bool = True, fillers: list[str] | None = None,
                      remove_phrases: list[str] | None = None, keep_only: list[str] | None = None,
                      max_gap: float = 0.75, padding: float = 0.08, timeline: str | None = None) -> dict:
    """Text-based editing from Resolve's transcript (run transcribe_audio first; Resolve 21.1+). Builds a new
    timeline of the clip without filler words (um, uh, امم... or your own `fillers`), without the segments that
    contain any of remove_phrases (retakes, off-topic lines), and, with keep_only, with only the segments that
    contain one of those phrases. Pauses longer than max_gap seconds between kept words are cut too; padding
    seconds are kept around words where the neighbours allow. The clip and existing timelines are untouched."""
    if max_gap < 0 or padding < 0:
        raise ToolError("max_gap and padding must be 0 or more")
    _, proj = _project()
    (c,) = _pool_clips(proj, [clip])
    t, _ = _transcript(c)
    if not t["segments"]:
        raise ToolError("cutting by transcript needs Resolve 21.1 (earlier versions only expose a preview)")
    fps, origin = _clip_timing(c)
    sec = lambda tc: (_tc_frames(tc, fps) - origin) / fps  # noqa: E731
    filler_set = {_token(f) for f in (fillers if fillers is not None else FILLER_WORDS)} if remove_fillers else set()
    tokens, dropped = [], {"fillers": 0, "segments": 0}  # tokens: (start, end, keep) in time order
    for seg in t["segments"]:
        text = (seg.get("text") or "").strip()
        if not text or text == "(...)":
            continue  # a silence segment: its time is cut by max_gap
        low = text.lower()
        if (keep_only and not any(k.lower() in low for k in keep_only)) or \
                (remove_phrases and any(ph.lower() in low for ph in remove_phrases)):
            dropped["segments"] += 1
            tokens.append((sec(seg["start"]), sec(seg["end"]), False))
            continue
        words = seg.get("words") or []
        if not words:
            tokens.append((sec(seg["start"]), sec(seg["end"]), True))
            continue
        for w in words:
            filler = _token(w.get("text")) in filler_set
            dropped["fillers"] += filler
            tokens.append((sec(w["start"]), sec(w["end"]), not filler))
    tokens.sort()
    ranges, cur = [], None
    for i, (a, b, keep) in enumerate(tokens):
        if not keep:
            if cur:
                ranges.append(cur)
            cur = None
            continue
        prev_end = tokens[i - 1][1] if i else 0.0
        nxt = tokens[i + 1][0] if i + 1 < len(tokens) else None
        lo = a - min(padding, max(0.0, (a - prev_end) / 2) if i else padding)
        hi = b + (min(padding, max(0.0, (nxt - b) / 2)) if nxt is not None else padding)
        if cur and a - cur[2] <= max_gap:
            cur = (cur[0], hi, b)
        else:
            if cur:
                ranges.append(cur)
            cur = (max(0.0, lo), hi, b)
    if cur:
        ranges.append(cur)
    total = int(float(c.GetClipProperty("Frames") or 0)) or int(math.ceil(max((b for _, b, _ in tokens), default=0) * fps))
    frames = _to_frames([(a, b) for a, b, _ in ranges], fps, total)
    name = timeline or f"{c.GetName()} - edited"
    _cut_timeline(proj, c, frames, name)
    return {**_cut_report(name, frames, fps, total), "removed": dropped}

if __name__ == "__main__":
    _setup_logging()
    log.info("starting", extra={"fields": {"platform": sys.platform, "pid": os.getpid()}})
    mcp.run()
