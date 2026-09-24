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
import functools
import inspect
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
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
    "clip properties, titles, markers and rendering. Frames are absolute timeline frames.",
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
    import DaVinciResolveScript as dvr

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
    start_frame/end_frame (clip-relative, end exclusive) make a subclip of each clip."""
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


@_tool
def set_item_properties(item: int, properties: dict, track: int = 1) -> dict:
    """Set effect properties on a video timeline item (1-based index from list_items).
    Keys: ZoomX ZoomY Pan Tilt RotationAngle Opacity CropLeft CropRight CropTop CropBottom FlipX FlipY CompositeMode ..."""
    _, tl = _timeline()
    items = tl.GetItemListInTrack("video", track) or []
    if not 1 <= item <= len(items):
        raise ToolError(f"item {item} not found on video track {track}")
    it = items[item - 1]
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
def render(target_dir: str, preset: str | None = None, file_name: str | None = None) -> dict:
    """Queue and start rendering the current timeline. Returns the job id; progress is visible in Resolve."""
    _, proj = _project()
    if preset and not proj.LoadRenderPreset(preset):
        raise ToolError(f"unknown render preset: {preset}")
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


if __name__ == "__main__":
    _setup_logging()
    log.info("starting", extra={"fields": {"platform": sys.platform, "pid": os.getpid()}})
    mcp.run()
