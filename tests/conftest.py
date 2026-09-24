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
    def __init__(self, name, start, end):
        self.name, self.start, self.end, self.props = name, start, end, {}
        self.comp = None
        self.graph = Graph(("Primary", "Look"))
        self.cdl, self.color_group = None, None
        self.versions = {0: ["Version 1"], 1: []}
        self.version = {"versionName": "Version 1", "versionType": 0}
        self.copied_to, self.exported_lut = None, None

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

    def GetFusionCompByIndex(self, i):
        return self.comp


class FusionTool:
    def __init__(self):
        self.inputs = {}

    def SetInput(self, key, value):
        self.inputs[key] = value


class Comp:
    def __init__(self):
        self.template = FusionTool()

    def FindTool(self, name):
        return self.template if name == "Template" else None


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

    def _append(self, name, frames, track=1):
        items = self.tracks.setdefault(("video", track), [])
        start = items[-1].end if items else self.start
        item = Item(name, start, start + frames)
        items.append(item)
        return item

    def InsertTitleIntoTimeline(self, name):
        return self._append(name, 120) if name == "Text" else None

    def InsertFusionTitleIntoTimeline(self, name):
        item = self._append(name, 120)
        item.comp = Comp()
        return item

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
                tl._append(c["mediaPoolItem"].name, frames, c["trackIndex"])
            else:
                tl._append(c.name, c.frames)
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
