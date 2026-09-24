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


class Item:
    def __init__(self, name, start, end):
        self.name, self.start, self.end, self.props = name, start, end, {}
        self.comp = None

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


class Timeline:
    def __init__(self, name, start=86400):
        self.name, self.start = name, start
        self.tracks = {("video", 1): [], ("audio", 1): []}
        self.markers = {}

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
    def __init__(self):
        self.pm = ProjectManager()

    def GetProjectManager(self):
        return self.pm

    def GetProductName(self):
        return "DaVinci Resolve"

    def GetVersionString(self):
        return "19.0.0"

    def GetCurrentPage(self):
        return "edit"


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
