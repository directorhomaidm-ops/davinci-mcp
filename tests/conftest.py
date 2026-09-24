"""In-memory stand-in for DaVinci Resolve's scripting API, so the tools can be tested without Resolve."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class Clip:
    def __init__(self, name, frames=100):
        self.name, self.frames = name, frames

    def GetName(self):
        return self.name

    def GetClipProperty(self, key):
        return str(self.frames) if key == "Frames" else None


class Folder:
    def __init__(self, name, clips=(), subfolders=()):
        self.name, self.clips, self.subfolders = name, list(clips), list(subfolders)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subfolders


class Graph:
    """Resolve 19+ node graph."""

    def __init__(self, labels=("",)):
        self.labels, self.luts = list(labels), {}

    def GetNumNodes(self):
        return len(self.labels)

    def GetNodeLabel(self, n):
        return self.labels[n - 1]

    def GetLUT(self, n):
        return self.luts.get(n, "")

    def SetLUT(self, n, path):
        if not path.endswith(".cube"):
            return False
        self.luts[n] = path
        return True


class ColorGroup:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name


class Item:
    def __init__(self, name, start, end, media=None, timeline=None):
        self.name, self.start, self.end, self.props = name, start, end, {}
        self.media, self.timeline, self.enabled = media, timeline, True
        self.stabilized = self.reframed = False
        self.comps, self.exported_comp = [], None
        self.graph = Graph(("Primary", "Look"))
        self.cdl, self.color_group = None, None
        self.versions = {0: ["Version 1"], 1: []}
        self.version = {"versionName": "Version 1", "versionType": 0}
        self.copied_to, self.exported_lut = None, None

    def GetMediaPoolItem(self):
        return self.media

    def GetClipEnabled(self):
        return self.enabled

    def SetClipEnabled(self, enabled):
        self.enabled = enabled
        return True

    def Stabilize(self):
        self.stabilized = self.media is not None
        return self.stabilized

    def SmartReframe(self):
        self.reframed = True
        return True

    def AddTransition(self, options):
        """Resolve 21.1: needs handles; here clips named *_nohandles have none."""
        if self.name.endswith("_nohandles") or options["type"] not in ("Cross Dissolve", "Dip To Color Dissolve"):
            return None
        self.transition_options = options
        half = options.get("duration", 8) // 2
        cut = self.end if options["position"] == "end" else self.start
        tr = Item(options["type"], cut - half, cut + half)
        items = self.timeline.tracks[("video", 1)]
        items.insert(items.index(self) + (1 if options["position"] == "end" else 0), tr)
        return tr

    def GetNodeGraph(self, layer=1):
        return self.graph

    def GetColorGroup(self):
        return self.color_group

    def SetCDL(self, cdl):
        self.cdl = cdl
        return True

    def CopyGrades(self, items):
        self.copied_to = items
        return True

    def GetCurrentVersion(self):
        return self.version

    def GetVersionNameList(self, kind):
        return self.versions[kind]

    def AddVersion(self, name, kind):
        if name in self.versions[kind]:
            return False
        self.versions[kind].append(name)
        self.version = {"versionName": name, "versionType": kind}
        return True

    def LoadVersionByName(self, name, kind):
        if name not in self.versions[kind]:
            return False
        self.version = {"versionName": name, "versionType": kind}
        return True

    def ExportLUT(self, kind, path):
        self.exported_lut = (kind, path)
        return True

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetDuration(self):
        return self.end - self.start

    def SetProperty(self, key, value):
        if key not in {"ZoomX", "ZoomY", "Pan", "Tilt", "Opacity"}:
            return False
        self.props[key] = value
        return True

    def GetFusionCompCount(self):
        return len(self.comps)

    def GetFusionCompByIndex(self, i):
        return self.comps[i - 1] if 1 <= i <= len(self.comps) else None

    def GetFusionCompNameList(self):
        return [c.name for c in self.comps]

    def AddFusionComp(self):
        comp = FuComp(f"Composition {len(self.comps) + 1}")
        self.comps.append(comp)
        return comp

    def ImportFusionComp(self, path):
        if not path.endswith(".comp"):
            return None
        comp = FuComp(Path(path).stem)
        self.comps.append(comp)
        return comp

    def ExportFusionComp(self, path, index):
        self.exported_comp = (path, index)
        return 1 <= index <= len(self.comps)


PNG = b"\x89PNG\r\n\x1a\n"

TOOL_INPUTS = {
    "MediaIn": {},
    "MediaOut": {"Input": "Image"},
    "Blur": {"Input": "Image", "XBlurSize": "Number", "EffectMask": "Mask"},
    "Transform": {"Input": "Image", "Size": "Number", "Center": "Point"},
    "Merge": {"Background": "Image", "Foreground": "Image", "Blend": "Number"},
    "TextPlus": {"StyledText": "Text", "Size": "Number", "Center": "Point"},
    "EllipseMask": {"Width": "Number", "Center": "Point"},
    "SoftGlow": {"Input": "Image", "Gain": "Number", "Threshold": "Number"},
    "BezierSpline": {},
    "PolyPath": {},
}
DEFAULTS = {"Number": 0.0, "Point": {1: 0.5, 2: 0.5}, "Text": ""}


class FuOutput:
    def __init__(self, tool):
        self.tool = tool

    def GetTool(self):
        return self.tool


class FuInput:
    def __init__(self, tool, inp_id, kind):
        self.tool, self.id, self.kind = tool, inp_id, kind
        self.value, self.source, self.keys = DEFAULTS.get(kind), None, {}

    def GetAttrs(self):
        return {"INPS_ID": self.id, "INPS_Name": self.id, "INPS_DataType": self.kind}

    def GetConnectedOutput(self):
        return self.source

    def _store(self, value):
        self.tool.comp.value_write()
        if self.kind == "Point" and not isinstance(value, dict):
            raise TypeError("this build wants a {1: x, 2: y} table")  # exercises the fallback encoding
        return value

    def animated(self):
        return self.source is not None and self.source.tool.kind in ("BezierSpline", "PolyPath")

    def __setitem__(self, frame, value):
        value = self._store(value)
        if self.animated():
            self.keys[frame] = value
        else:
            self.value = value  # no spline: a timed write is just a static value

    def GetKeyFrames(self):
        return {i: float(f) for i, f in enumerate(sorted(self.keys), 1)}


class FuTool:
    def __init__(self, comp, kind, name):
        self.comp, self.kind, self.name = comp, kind, name
        self.inputs = {k: FuInput(self, k, t) for k, t in TOOL_INPUTS[kind].items()}
        self.output = FuOutput(self)

    def GetAttrs(self):
        return {"TOOLS_Name": self.name, "TOOLS_RegID": self.kind}

    def SetAttrs(self, attrs):
        self.comp.structural()
        self.name = attrs.get("TOOLS_Name", self.name)

    def __getitem__(self, inp_id):
        return self.inputs.get(inp_id)

    def GetInputList(self):
        return dict(enumerate(self.inputs.values(), 1))

    def SetInput(self, inp_id, value, time=None):
        inp = self.inputs[inp_id]
        inp.value = inp._store(value)

    def GetInput(self, inp_id, time=None):
        return self.inputs[inp_id].value

    def AddModifier(self, inp_id, modifier):
        inp = self.inputs.get(inp_id)
        wanted = {"Number": "BezierSpline", "Point": "Path"}.get(inp.kind if inp else None)
        if modifier != wanted:
            return False
        mod = self.comp.AddTool("BezierSpline" if modifier == "BezierSpline" else "PolyPath", -1, -1, locked_ok=True)
        inp.source = mod.output
        return True

    def ConnectInput(self, inp_id, src):
        self.comp.structural()
        inp = self.inputs.get(inp_id)
        if not inp or inp.kind not in ("Image", "Mask"):
            return False
        inp.source = src.output if src else None
        return True

    def Delete(self):
        self.comp.structural()
        self.comp.tools.remove(self)


class FuComp:
    def __init__(self, name, template=False):
        self.name, self.tools, self.locked = name, [], False
        self.value_writes_under_lock = self.unlocked_structural_edits = 0
        self.undo_open = self.undo_steps = 0
        self.attrs = {"COMPN_RenderStart": 0.0, "COMPN_RenderEnd": 99.0}
        self.AddTool("MediaIn", -1, -1, locked_ok=True)
        self.AddTool("MediaOut", -1, -1, locked_ok=True)
        if template:
            self.AddTool("TextPlus", -1, -1, locked_ok=True).name = "Template"

    def structural(self):
        if not self.locked:
            self.unlocked_structural_edits += 1

    def value_write(self):
        if self.locked:
            self.value_writes_under_lock += 1

    def GetAttrs(self):
        return self.attrs

    def Lock(self):
        self.locked = True

    def Unlock(self):
        self.locked = False

    def StartUndo(self, name):
        self.undo_open += 1

    def EndUndo(self, keep):
        self.undo_open -= 1
        self.undo_steps += 1

    def AddTool(self, kind, x, y, locked_ok=False):
        if kind not in TOOL_INPUTS:
            return None
        if not locked_ok:
            self.structural()
        n = 1 + sum(t.kind == kind for t in self.tools)
        tool = FuTool(self, kind, f"{kind}{n}")
        self.tools.append(tool)
        return tool

    def FindTool(self, name):
        return next((t for t in self.tools if t.name == name), None)

    def GetToolList(self, selected=False, kind=None):
        return dict(enumerate((t for t in self.tools if kind is None or t.kind == kind), 1))


class Still:
    pass


class Album:
    def __init__(self):
        self.stills, self.exported = [], []

    def ExportStills(self, stills, folder, prefix, fmt):
        self.exported.append((stills, folder, prefix, fmt))
        return True


class Gallery:
    def __init__(self):
        self.album = Album()

    def GetCurrentStillAlbum(self):
        return self.album


class Timeline:
    def __init__(self, name, start=86400):
        self.name, self.start = name, start
        self.tracks = {("video", 1): [], ("audio", 1): []}
        self.markers = {}
        self.playhead_item, self.page_is_color, self.drx = None, True, None
        self.playhead, self.settings = "01:00:00:00", {"timelineFrameRate": "24", "timelineResolutionWidth": "1920",
                                                         "timelineResolutionHeight": "1080", "timelineDropFrameTimecode": "0"}
        self.deleted, self.scene_cuts = None, False

    def GetSetting(self, key):
        return self.settings.get(key)

    def GetTrackCount(self, kind):
        return len([k for k in self.tracks if k[0] == kind])

    def GetTrackName(self, kind, n):
        return f"{kind[0].upper()}{n}"

    def GetIsTrackEnabled(self, kind, n):
        return True

    def GetCurrentTimecode(self):
        return self.playhead

    def SetCurrentTimecode(self, tc):
        if len(tc.split(":")) != 4:
            return False
        self.playhead = tc
        return True

    def GetMarkers(self):
        return {float(f): m for f, m in self.markers.items()}

    def DeleteClips(self, items, ripple):
        self.deleted = (items, ripple)
        for it in items:
            for track in self.tracks.values():
                if it in track:
                    track.remove(it)
        return True

    def DetectSceneCuts(self):
        self.scene_cuts = True
        return True

    def GetCurrentVideoItem(self):
        return self.playhead_item

    def GrabStill(self):
        return Still() if self.page_is_color else None

    def ApplyGradeFromDRX(self, path, mode, items):
        self.drx = (path, mode, items)
        return True

    def GetName(self):
        return self.name

    def GetStartFrame(self):
        return self.start

    def GetEndFrame(self):
        items = self.tracks[("video", 1)]
        return items[-1].end if items else self.start

    def GetItemListInTrack(self, track_type, index):
        return self.tracks.get((track_type, index))

    def _append(self, name, frames, track=1, media=None):
        items = self.tracks.setdefault(("video", track), [])
        start = items[-1].end if items else self.start
        item = Item(name, start, start + frames, media, self)
        items.append(item)
        return item

    def InsertTitleIntoTimeline(self, name):
        return self._append(name, 120) if name == "Text" else None

    def InsertFusionTitleIntoTimeline(self, name):
        item = self._append(name, 120)
        item.comps = [FuComp("Composition 1", template=True)]
        return item

    def InsertFusionCompositionIntoTimeline(self):
        item = self._append("Fusion Composition", 150)
        item.comps = [FuComp("Composition 1")]
        return item

    def InsertFusionGeneratorIntoTimeline(self, name):
        return self._append(name, 150) if name in ("Contours", "Noise Gradient") else None

    def CreateFusionClip(self, items):
        self.fusion_clip_of = items
        return Item("Fusion Clip 1", items[0].start, items[-1].end)

    def AddMarker(self, frame, color, name, note, duration):
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note, "duration": duration}
        return True


class MediaPool:
    def __init__(self, project):
        self.project = project
        self.root = Folder("Master")
        self.appended = []

    def GetRootFolder(self):
        return self.root

    def ImportMedia(self, paths):
        clips = [Clip(Path(p).name) for p in paths]
        self.root.clips.extend(clips)
        return clips

    def CreateEmptyTimeline(self, name):
        if any(t.name == name for t in self.project.timelines):
            return None
        tl = Timeline(name)
        self.project.timelines.append(tl)
        self.project.current = tl
        return tl

    def AppendToTimeline(self, clips):
        tl = self.project.current
        if tl is None:
            return False
        for c in clips:
            if isinstance(c, dict):
                frames = c["endFrame"] - c["startFrame"] + 1
                tl._append(c["mediaPoolItem"].name, frames, c["trackIndex"], c["mediaPoolItem"])
            else:
                tl._append(c.name, c.frames, media=c)
        self.appended.append(clips)
        return True


class Project:
    def __init__(self, name):
        self.name = name
        self.timelines, self.current = [], None
        self.pool = MediaPool(self)
        self.presets = ["H.264 Master", "YouTube 1080p"]
        self.loaded_preset, self.render_settings, self.jobs = None, {}, {}
        self.format_codec, self.rendering = None, False
        self.gallery = Gallery()

    def GetGallery(self):
        return self.gallery

    def ExportCurrentFrameAsStill(self, path):
        if not Path(path).parent.is_dir():
            return False
        Path(path).write_bytes(PNG + self.current.playhead.encode())
        self.exported_frame = path
        return True

    def GetRenderFormats(self):
        return {"QuickTime": "mov", "MP4": "mp4"}

    def GetRenderCodecs(self, fmt):
        return {"QuickTime": {"Apple ProRes 422 HQ": "ProRes422HQ", "H.264": "H264"}, "MP4": {"H.264": "H264"}}[fmt]

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):
        if codec not in self.GetRenderCodecs(fmt).values():
            return False
        self.format_codec = (fmt, codec)
        return True

    def IsRenderingInProgress(self):
        return self.rendering

    def StopRendering(self):
        self.rendering = False

    def GetName(self):
        return self.name

    def GetMediaPool(self):
        return self.pool

    def GetCurrentTimeline(self):
        return self.current

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, i):
        return self.timelines[i - 1]

    def SetCurrentTimeline(self, tl):
        self.current = tl
        return True

    def GetRenderPresetList(self):
        return self.presets

    def LoadRenderPreset(self, name):
        if name not in self.presets:
            return False
        self.loaded_preset = name
        return True

    def SetRenderSettings(self, settings):
        self.render_settings.update(settings)
        return True

    def AddRenderJob(self):
        job = f"job-{len(self.jobs) + 1}"
        self.jobs[job] = {"JobStatus": "Ready", "CompletionPercentage": 0}
        return job

    def StartRendering(self, jobs, isInteractiveMode=False):
        self.rendering = True
        for j in jobs:
            self.jobs[j] = {"JobStatus": "Rendering", "CompletionPercentage": 0}
        return True

    def GetRenderJobStatus(self, job):
        return self.jobs.get(job, {})


class ProjectManager:
    def __init__(self):
        self.projects = {"Demo": Project("Demo")}
        self.current = None

    def GetCurrentProject(self):
        return self.current

    def GetProjectListInCurrentFolder(self):
        return list(self.projects)

    def LoadProject(self, name):
        self.current = self.projects.get(name)
        return self.current

    def CreateProject(self, name):
        if name in self.projects:
            return None
        self.projects[name] = self.current = Project(name)
        return self.current


class Resolve:
    EXPORT_LUT_17PTCUBE, EXPORT_LUT_33PTCUBE, EXPORT_LUT_65PTCUBE = 0, 1, 2

    def __init__(self):
        self.pm = ProjectManager()
        self.page = "edit"

    def OpenPage(self, page):
        self.page = page
        return True

    def GetProjectManager(self):
        return self.pm

    def GetProductName(self):
        return "DaVinci Resolve"

    def GetVersionString(self):
        return "19.0.0"

    def GetCurrentPage(self):
        return self.page


@pytest.fixture
def resolve(monkeypatch):
    """A fresh fake Resolve with no project open, served by a fake DaVinciResolveScript module."""
    fake = Resolve()
    module = types.ModuleType("DaVinciResolveScript")
    module.scriptapp = lambda app: fake if app == "Resolve" else None
    monkeypatch.setitem(sys.modules, "DaVinciResolveScript", module)
    return fake


@pytest.fixture
def project(resolve):
    """The fake with project "Demo" open, two clips in the pool (one in a subfolder) and timeline "Main" current."""
    proj = resolve.pm.LoadProject("Demo")
    proj.pool.root.clips.append(Clip("a.mov", 100))
    proj.pool.root.subfolders.append(Folder("B-roll", [Clip("b.mov", 50)]))
    proj.pool.CreateEmptyTimeline("Main")
    return proj
