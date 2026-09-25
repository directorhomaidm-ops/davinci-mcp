"""In-memory stand-in for DaVinci Resolve's scripting API, so the tools can be tested without Resolve."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


UI = {"page": "edit"}

KNOWN_SPACES = {"Rec.709 Gamma 2.4", "Rec.2100 ST2084", "Rec.2100 HLG", "DaVinci WG/Intermediate", "ARRI LogC4"}
SETTING_ENUMS = {
    "colorScienceMode": {"davinciYRGB", "davinciYRGBColorManaged", "davinciYRGBColorManagedv2", "acescc", "acescct"},
    "rcmPresetMode": {"SDR", "HDR"},
    "hdrDolbyVersion": {"2.9", "4.0"},
    "hdrDolbyAnalysisTuning": {"Legacy", "Most Mapping", "More Mapping", "Balanced", "Less Mapping", "Least Mapping"},
}


def color_set(store, key, value):
    """Resolve-like color setting rules: enums, mode-dependent locks, and a lie while RCM is automatic."""
    managed = store.get("colorScienceMode", "davinciYRGB").startswith("davinciYRGBColorManaged")
    if key.startswith("colorSpace"):
        if store.get("isAutoColorManage") == "1":
            return True  # locked by automatic color management: reports success, applies nothing
        if not managed:
            return False
        if not key.endswith("Gamma") and value not in KNOWN_SPACES:
            return False
    if key == "rcmPresetMode" and store.get("isAutoColorManage") != "1":
        return False
    if key in SETTING_ENUMS and value not in SETTING_ENUMS[key]:
        return False
    store[key] = value
    return True


class Clip:
    def __init__(self, name, frames=100, path=None):
        self.name, self.frames = name, frames
        self.props, self.transcribed_with = {}, None
        self.path = path or f"/media/{name}"
        self.metadata, self.color, self.flags, self.markers, self.proxy = {}, "", [], {}, None

    def GetMetadata(self, key=None):
        return self.metadata if key is None else self.metadata.get(key, "")

    def SetMetadata(self, values):
        # Like live Resolve with automatic reel naming: True, but Reel Name is not kept.
        self.metadata.update({k: v for k, v in values.items() if k != "Reel Name"})
        return True

    def GetClipColor(self):
        return self.color

    def SetClipColor(self, color):
        self.color = color
        return True

    def ClearClipColor(self):
        self.color = ""
        return True

    def AddFlag(self, color):
        if color not in ("Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple"):
            return False
        self.flags.append(color)
        return True

    def GetFlagList(self):
        return self.flags

    def ClearFlags(self, color):
        self.flags = [] if color == "All" else [f for f in self.flags if f != color]
        return True

    def GetMarkers(self):
        return self.markers

    def LinkProxyMedia(self, path):
        self.proxy = path
        return True

    def UnlinkProxyMedia(self):
        had, self.proxy = self.proxy, None
        return had is not None

    def ReplaceClip(self, path):
        self.path = path
        return True

    def GetName(self):
        return self.name

    def SetClipProperty(self, key, value):
        if key == "Input Color Space" and value not in KNOWN_SPACES:
            return False
        self.props[key] = value
        return True

    def GetClipProperty(self, key=None):
        allp = {"Frames": str(self.frames), "File Path": self.path, "FPS": "24", "Resolution": "1920x1080",
                "Start TC": "01:00:00:00",
                "Proxy Media Path": self.proxy or "", "Reel Name": "", **self.props}
        return allp if key is None else allp.get(key)

    def PerformAudioClassification(self):
        kind = "Music" if "music" in self.name else "Effects" if "sfx" in self.name else "Dialogue"
        self.props["Category"] = kind
        self.metadata["Subcategory"] = {"Music": "Score", "Effects": "Whoosh", "Dialogue": "Interview"}[kind]
        return True

    def TranscribeAudio(self, speaker_detection=None):
        if self.name.endswith(".wav") or self.name.endswith(".mov"):
            self.transcribed_with = speaker_detection
            self.props["Transcription"] = "hello and welcome..."
            return True
        return False


class Folder:
    def __init__(self, name, clips=(), subfolders=()):
        self.name, self.clips, self.subfolders = name, list(clips), list(subfolders)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subfolders

    def PerformAudioClassification(self):
        for c in self.clips:
            c.PerformAudioClassification()
        for f in self.subfolders:
            f.PerformAudioClassification()
        return True

    def GetIsFolderStale(self):
        return getattr(self, "stale", False)


import os


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
        if not path.endswith(".cube") or os.path.isabs(path):
            return False  # measured: only paths inside the master LUT folder resolve
        if path.startswith("davinci-mcp/") and path.split("/", 1)[1] not in self.installed():
            return False
        self.luts[n] = path
        return True

    def installed(self):
        folder = os.environ.get("RESOLVE_LUT_DIR", "")
        return os.listdir(os.path.join(folder, "davinci-mcp")) if folder and os.path.isdir(os.path.join(folder, "davinci-mcp")) else []

    def GetToolsInNode(self, n):
        return ["Primaries", "Curves"] if n == 1 else ["LUT"]

    def GetNodeCacheMode(self, n):
        return 0

    def SetNodeEnabled(self, n, on):
        self.disabled = getattr(self, "disabled", set())
        (self.disabled.discard if on else self.disabled.add)(n)
        return UI["page"] == "color"

    def ResetAllGrades(self):
        self.reset = UI["page"] == "color"
        return self.reset

    def ApplyGradeFromDRX(self, path, mode):
        if UI["page"] != "color":
            return False
        self.drx = (path, mode)
        return True

    def ApplyArriCdlLut(self):
        return UI["page"] == "color" and getattr(self, "arri", False)


class ColorGroup:
    def __init__(self, name):
        self.name, self.members = name, []
        self.pre, self.post = Graph(("Group Pre",)), Graph(("Group Post",))

    def GetName(self):
        return self.name

    def GetClipsInTimeline(self, timeline=None):
        return list(self.members)

    def GetPreClipNodeGraph(self):
        return self.pre

    def GetPostClipNodeGraph(self):
        return self.post


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

    def GetType(self):
        return getattr(self, "kind", "video")

    def SetUseTimelineForOutputBlanking(self, use):
        self.use_timeline_blanking = use
        return True

    def SetOutputBlanking(self, bounds):
        if getattr(self, "use_timeline_blanking", True):
            return False  # measured: refused while the clip inherits the timeline's blanking
        self.blanking = bounds
        return True

    def GetSourceStartTime(self):
        return getattr(self, "source_start", 0.0)

    def GetClipEnabled(self):
        return self.enabled

    def SetClipEnabled(self, enabled):
        self.enabled = enabled
        return True

    def Stabilize(self):
        self.stabilized = self.media is not None
        return self.stabilized

    def GetFades(self):
        return getattr(self, "fades", {"FadeIn": 0.0, "FadeOut": 0.0})

    def SetFades(self, fades):
        if any(v > self.end - self.start for v in fades.values()):
            return False
        self.fades = {**self.GetFades(), **{k: float(v) for k, v in fades.items()}}
        return True

    def GetSpeed(self):
        return getattr(self, "speed", {"Percentage": 100.0})

    def SetSpeed(self, options):
        self.speed_options = options
        pct = options["Percentage"]
        self.speed = {"Percentage": float(pct)}
        if pct:
            self.end = self.start + round((self.end - self.start) * 100 / pct)
        return True

    def CreateMagicMask(self, mode):
        if mode not in ("F", "B", "BI"):
            return False  # long spellings are rejected on live Resolve
        self.magic_mask = mode if getattr(self, "mask_clicked", False) else None
        return self.magic_mask is not None

    def RegenerateMagicMask(self):
        return getattr(self, "magic_mask", None) is not None

    def PerformMulticamSmartSwitch(self, settings):
        self.smart_switch = settings
        return self.name.startswith("Multicam")

    def FlattenMulticam(self, grade):
        if not self.name.startswith("Multicam"):
            return False
        self.flattened, self.name = grade, self.name.replace("Multicam", "Angle 1")
        return True

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
        tr.kind = "transition"
        items = self.timeline.tracks[("video", 1)]
        items.insert(items.index(self) + (1 if options["position"] == "end" else 0), tr)
        return tr

    def GetNodeGraph(self, layer=1):
        return self.graph

    def GetColorGroup(self):
        return self.color_group

    def SetCDL(self, cdl):
        if UI["page"] != "color":
            return False  # grade writes are refused off the Color page
        self.cdl = cdl
        return True

    def CopyGrades(self, items):
        if UI["page"] != "color":
            return False
        self.copied_to = items
        return True

    def AssignToColorGroup(self, group):
        if UI["page"] != "color":
            return False
        self.RemoveFromColorGroup()
        group.members.append(self)
        self.color_group = group
        return True

    def RemoveFromColorGroup(self):
        if self.color_group:
            self.color_group.members.remove(self)
        self.color_group = None
        return True

    def SetColorOutputCache(self, on):
        self.color_cache = on
        return True

    def GetCurrentVersion(self):
        return self.version

    def GetVersionNameList(self, kind):
        return self.versions[kind]

    def AddVersion(self, name, kind):
        if UI["page"] != "color" or name in self.versions[kind]:
            return False
        self.versions[kind].append(name)
        self.version = {"versionName": name, "versionType": kind}
        return True

    def LoadVersionByName(self, name, kind):
        if UI["page"] != "color" or name not in self.versions[kind]:
            return False
        self.version = {"versionName": name, "versionType": kind}
        return True

    def ExportLUT(self, kind, path):
        if UI["page"] != "color":
            return False  # measured: True only from the Color page
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
    "Transform": {"Input": "Image", "Size": "Number", "Center": "Point", "Angle": "Number"},
    "Merge": {"Background": "Image", "Foreground": "Image", "Blend": "Number"},
    "TextPlus": {"StyledText": "Text", "Font": "Text", "Style": "Text", "Size": "Number", "Center": "Point",
                 "Red1": "Number", "Green1": "Number", "Blue1": "Number"},
    "EllipseMask": {"Width": "Number", "Height": "Number", "SoftEdge": "Number", "Invert": "Number", "Center": "Point"},
    "BrightnessContrast": {"Input": "Image", "Gain": "Number", "EffectMask": "Mask"},
    "SoftGlow": {"Input": "Image", "Gain": "Number", "Threshold": "Number"},
    "Tracker": {"Input": "Image", "PatternCenter1": "Point", "TrackedCenter1": "Point", "TrackedCenter2": "Point"},
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

    def SetExpression(self, expr, time=None):
        self.tool.comp.value_write()
        self.expression = expr


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
        inp = self.inputs[inp_id]
        if time is not None and inp.keys:
            return inp.keys.get(int(time), inp.keys.get(time, inp.value))
        return inp.value

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
        if inp and src is None:  # disconnects anything, including an animation spline
            if inp.animated() and inp.keys:
                inp.value = inp.keys[max(inp.keys)]
            inp.source, inp.keys = None, {}
            return True
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
    def __init__(self, name="Stills 1"):
        self.name, self.stills, self.exported = name, [], []

    def GetStills(self):
        return self.stills

    def GetLabel(self, still):
        return getattr(still, "label", "")

    def ImportStills(self, paths):
        for pth in paths:
            st = Still()
            st.label = os.path.basename(pth)
            self.stills.append(st)
        return True

    def ExportStills(self, stills, folder, prefix, fmt):
        self.exported.append((stills, folder, prefix, fmt))
        return True


class Gallery:
    def __init__(self):
        self.album = Album()
        self.powergrades = []

    def GetCurrentStillAlbum(self):
        return self.album

    def GetAlbumName(self, album):
        return album.name

    def GetGalleryStillAlbums(self):
        return [self.album]

    def GetGalleryPowerGradeAlbums(self):
        return self.powergrades

    def CreateGalleryPowerGradeAlbum(self):
        album = Album(f"PowerGrade {len(self.powergrades) + 1}")
        self.powergrades.append(album)
        return album


class Timeline:
    def __init__(self, name, start=86400):
        self.name, self.start = name, start
        self.tracks = {("video", 1): [], ("audio", 1): []}
        self.markers = {}
        self.playhead_item, self.page_is_color, self.drx = None, True, None
        self.playhead, self.settings = "01:00:00:00", {"timelineFrameRate": "24", "timelineResolutionWidth": "1920",
                                                         "timelineResolutionHeight": "1080", "timelineDropFrameTimecode": "0",
                                                         "useCustomSettings": "0"}
        self.deleted, self.scene_cuts = None, False
        self.track_names, self.track_formats, self.track_locked, self.track_enabled = {}, {("audio", 1): "stereo"}, {}, {}
        self.voice, self.normalized, self.captioned_with = {}, None, None
        self.add_track_takes_dict = True

    def AddTrack(self, kind, sub=None):
        if kind == "audio" and isinstance(sub, dict) and not self.add_track_takes_dict:
            raise TypeError("AddTrack() takes a sub-type string on this build")
        n = self.GetTrackCount(kind) + 1
        self.tracks[(kind, n)] = []
        if kind == "audio":
            self.track_formats[(kind, n)] = sub["audioType"] if isinstance(sub, dict) else (sub or "stereo")
        return True

    def DeleteTrack(self, kind, n):
        del self.tracks[(kind, n)]
        return True

    def SetTrackName(self, kind, n, name):
        self.track_names[(kind, n)] = name
        return True

    def SetTrackEnable(self, kind, n, on):
        self.track_enabled[(kind, n)] = on
        return True

    def SetTrackLock(self, kind, n, on):
        self.track_locked[(kind, n)] = on
        return True

    def GetIsTrackLocked(self, kind, n):
        return self.track_locked.get((kind, n), False)

    def GetTrackSubType(self, kind, n):
        return self.track_formats.get((kind, n), "")

    def GetVoiceIsolationState(self, n):
        return self.voice.get(n, {"isEnabled": False, "amount": 0})

    def SetVoiceIsolationState(self, n, state):
        self.voice[n] = state
        return True

    def GetNormalizeAudioModes(self):
        return ["Sample Peak Program", "True Peak Program", "ITU-R BS.1770-4"]

    def NormalizeAudioLevel(self, items, options):
        self.normalized = (items, options)
        return True

    def ConvertTimelineToStereo(self):
        self.stereo = True
        return True

    def CreateSubtitlesFromAudio(self, settings):
        self.captioned_with = settings
        if not self.has_dialogue:
            return True  # Resolve's flag is unreliable: reports success, creates nothing
        n = self.GetTrackCount("subtitle") + 1
        self.tracks[("subtitle", n)] = [Item("caption", 0, 10), Item("caption", 10, 20)]
        return False  # ...and can report failure after creating the track

    has_dialogue = True

    def GetSetting(self, key):
        return self.settings.get(key)

    def SetSetting(self, key, value):
        if key == "useCustomSettings":
            self.settings[key] = value
            return True
        return color_set(self.settings, key, value)

    def AnalyzeDolbyVision(self, items=None, analysis=None):
        self.dolby_analyzed = (items, analysis)
        return True

    def GetTrackCount(self, kind):
        return len([k for k in self.tracks if k[0] == kind])

    def GetTrackName(self, kind, n):
        return self.track_names.get((kind, n), f"{kind[0].upper()}{n}")

    def GetIsTrackEnabled(self, kind, n):
        return self.track_enabled.get((kind, n), True)

    def GetCurrentTimecode(self):
        return self.playhead

    def SetCurrentTimecode(self, tc):
        if len(tc.split(":")) != 4:
            return False
        self.playhead = tc
        return True

    def GetMarkers(self):
        return {float(f): m for f, m in self.markers.items()}

    def Export(self, path, kind, sub):
        if kind is None or isinstance(kind, str):
            return False  # plain strings are rejected on live Resolve
        Path(path).write_text(f"{kind}/{sub}")
        self.exported = (path, kind, sub)
        return True

    def DeleteClips(self, items, ripple):
        if UI["page"] == "fairlight":
            return False  # measured: False on the Fairlight page, every time
        self.deleted = (items, ripple)
        for it in items:
            for track in self.tracks.values():
                if it in track:
                    track.remove(it)
        return True

    def AutoAlignClips(self, items, options):
        self.aligned = (items, options)
        return True

    def SetOutputBlanking(self, bounds):
        self.blanking = bounds
        return True

    def DetectSceneCuts(self):
        self.scene_cuts = True
        return True

    def GetCurrentVideoItem(self):
        return self.playhead_item

    def GrabStill(self):
        return Still() if self.page_is_color else None

    def ApplyGradeFromDRX(self, path, mode, items):
        if UI["page"] != "color":
            return False
        self.drx = (path, mode, items)
        return True

    def GetNodeGraph(self):
        self.timeline_graph = getattr(self, "timeline_graph", None) or Graph(("Timeline",))
        return self.timeline_graph

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

    def AddMarker(self, frame, color, name, note, duration, customData=""):
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note, "duration": duration,
                               "customData": customData or ""}
        return True

    def DeleteMarkerAtFrame(self, frame):
        return self.markers.pop(frame, None) is not None

    def DeleteMarkersByColor(self, color):
        self.markers = {} if color == "All" else {f: m for f, m in self.markers.items() if m["color"] != color}
        return True

    def SetName(self, name):
        if any(t.name == name for t in self.project.timelines):
            return False
        self.name = name
        return True

    def DuplicateTimeline(self, name):
        copy = Timeline(name, self.start)
        copy.project = self.project
        self.project.timelines.append(copy)
        self.project.current = copy  # measured: the copy silently becomes current
        return copy


class MediaPool:
    def __init__(self, project):
        self.project = project
        self.root = Folder("Master")
        self.appended = []

    def GetRootFolder(self):
        return self.root

    def DeleteTimelines(self, timelines):
        for t in timelines:
            self.project.timelines.remove(t)
        if self.project.current in timelines:
            self.project.current = self.project.timelines[0] if self.project.timelines else None
        return True

    def CreateMulticamClip(self, clips, options):
        self.multicam = (clips, options)
        mc = Clip(options.get("name") or f"Multicam {clips[0].name}")
        self.GetCurrentFolder().clips.append(mc)
        return [mc]

    def RefreshFolders(self):
        for f in self._folders():
            f.stale = False if not getattr(f, "stays_stale", False) else True
        return True

    def AutoSyncAudio(self, clips, settings):
        self.synced_with = (clips, settings)
        for c in clips:
            c.props["Synced Audio"] = "sound.wav" if self.can_sync else ""
        return False  # unreliable flag, as measured on live Resolve

    can_sync = True

    def GetCurrentFolder(self):
        return getattr(self, "current", self.root)

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True

    def _folders(self, folder=None):
        folder = folder or self.root
        yield folder
        for f in folder.subfolders:
            yield from self._folders(f)

    def ImportMedia(self, items):
        clips = []
        for it in items:
            if isinstance(it, dict):
                stem = Path(it["FilePath"]).name.split("%")[0]
                clips.append(Clip(f"{stem}[{it['StartIndex']}-{it['EndIndex']}]", it["EndIndex"] - it["StartIndex"] + 1))
            else:
                clips.append(Clip(Path(it).name, path=it))
        self.GetCurrentFolder().clips.extend(clips)
        self.imported_into = self.GetCurrentFolder().name
        return clips

    def AddSubFolder(self, parent, name):
        f = Folder(name)
        parent.subfolders.append(f)
        return f

    def MoveClips(self, clips, target):
        for c in clips:
            for f in self._folders():
                if c in f.clips:
                    f.clips.remove(c)
        target.clips.extend(clips)
        return True

    def DeleteClips(self, clips):
        for c in clips:
            for f in self._folders():
                if c in f.clips:
                    f.clips.remove(c)
        return True

    def RelinkClips(self, clips, folder):
        for c in clips:
            c.path = str(Path(folder) / c.name)
        return True

    def ExportMetadata(self, path, clips):
        rows = clips or [c for f in self._folders() for c in f.clips]
        Path(path).write_text("\n".join(c.name for c in rows))
        return True

    def ImportTimelineFromFile(self, path, options):
        name = options.get("timelineName") or Path(path).stem
        if path.endswith(".xml"):
            name = "Sequence 1"  # FCP7 XML: the file's own sequence name wins
        existing = next((t for t in self.project.timelines if t.name == name), None)
        if existing:
            return existing if path.endswith(".xml") else None
        tl = Timeline(name)
        tl.project = self.project
        self.project.timelines.append(tl)
        self.imported_options = options
        return tl

    def CreateEmptyTimeline(self, name):
        if any(t.name == name for t in self.project.timelines):
            return None
        tl = Timeline(name)
        tl.project = self.project
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
                tl._append(c["mediaPoolItem"].name, frames, c.get("trackIndex", 1), c["mediaPoolItem"])
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
        self.color = {"colorScienceMode": "davinciYRGB", "hdrMasteringOn": "0", "hdrDolbyControlsOn": "0"}
        self.gallery = Gallery()
        self.color_groups = []

    def GetGallery(self):
        return self.gallery

    def GetColorGroupsList(self):
        return self.color_groups

    def AddColorGroup(self, name):
        group = ColorGroup(name)
        self.color_groups.append(group)
        return group

    def DeleteColorGroup(self, group):
        for m in list(group.members):
            m.RemoveFromColorGroup()
        self.color_groups.remove(group)
        return True

    def RefreshLUTList(self):
        self.luts_refreshed = True
        return True

    def GetSetting(self, key):
        return self.color.get(key, "")

    def SetSetting(self, key, value):
        return color_set(self.color, key, value)

    def ApplyFairlightPresetToCurrentTimeline(self, name):
        self.fairlight_preset = name
        return name in ("Dialogue Mix", "Podcast")

    def InsertAudioToCurrentTrackAtPlayhead(self, path, offset, duration):
        self.inserted_audio = (path, offset, duration)
        return True

    def GenerateSpeech(self, settings):
        self.speech = settings
        if getattr(self, "speech_missing_extras", False):
            return "Required Package, 'AI Speech Generator' is not Installed."  # measured: a string, not False
        clip = Clip(settings.get("Filename") or "Speech 1.wav")
        self.pool.GetCurrentFolder().clips.append(clip)
        return clip

    def ExportCurrentFrameAsStill(self, path):
        if not Path(path).parent.is_dir():
            return False
        Path(path).write_bytes(PNG + self.current.playhead.encode())
        self.exported_frame = path
        return True

    def GetRenderFormats(self):
        return {"QuickTime": "mov", "MP4": "mp4", "Wave": "wav"}

    def GetRenderCodecs(self, fmt):
        # Only ids are accepted; "wav" has no codecs, as measured.
        return {"mov": {"Apple ProRes 422 HQ": "ProRes422HQ", "H.264": "H264"}, "mp4": {"H.264": "H264"}}.get(fmt, {})

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):
        if codec not in self.GetRenderCodecs(fmt).values():
            return False
        self.format_codec = (fmt, codec)
        return True

    def SetCurrentRenderMode(self, mode):
        self.render_mode = mode
        return True

    def GetRenderJobList(self):
        return [{"JobId": j, "TargetDir": self.render_settings.get("TargetDir", ""),
                 "OutputFilename": f"{self.render_settings.get('CustomName', 'Main')}.mov"} for j in self.jobs]

    def DeleteRenderJob(self, job):
        return self.jobs.pop(job, None) is not None

    def DeleteAllRenderJobs(self):
        self.jobs.clear()
        return True

    def SaveAsNewRenderPreset(self, name):
        if name in self.presets:
            return False
        self.presets.append(name)
        return True

    def IsRenderingInProgress(self):
        return self.rendering

    def StopRendering(self):
        self.rendering = False

    def GetName(self):
        return self.name

    def SetName(self, name):
        if name in self.manager.projects:
            return False
        self.manager.projects[name] = self.manager.projects.pop(self.name)
        self.name = name
        return True

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

    def StartRendering(self, jobs=None, isInteractiveMode=False):
        jobs = list(self.jobs) if jobs is None else jobs
        if not jobs or any(j not in self.jobs for j in jobs):
            return False
        self.rendering = True
        for j in jobs:
            self.jobs[j] = {"JobStatus": "Rendering", "CompletionPercentage": 0}
        return True

    def GetRenderJobStatus(self, job):
        return self.jobs.get(job, {})


class ProjectManager:
    def __init__(self):
        self.projects = {}
        self._add(Project("Demo"))
        self.current = None
        self.folder_path, self.folders = [], {(): ["Clients"], ("Clients",): ["Acme"], ("Clients", "Acme"): []}
        self.databases = [{"DbType": "Disk", "DbName": "Local Database"},
                          {"DbType": "PostgreSQL", "DbName": "Studio", "IpAddress": "10.0.0.5"}]
        self.db = self.databases[0]
        self.recently_open, self.saves, self.cloud = set(), 0, {}

    def _add(self, proj):
        proj.manager = self
        self.projects[proj.name] = proj
        return proj

    def GetCurrentDatabase(self):
        return self.db

    def GetDatabaseList(self):
        return self.databases

    def SetCurrentDatabase(self, info):
        self.db, self.current = info, None
        return True

    def GotoRootFolder(self):
        self.folder_path = []
        return True

    def OpenFolder(self, name):
        if name not in self.folders.get(tuple(self.folder_path), []):
            return False
        self.folder_path.append(name)
        return True

    def GetCurrentFolder(self):
        return self.folder_path[-1] if self.folder_path else ""

    def GetFolderListInCurrentFolder(self):
        return self.folders.get(tuple(self.folder_path), [])

    def CreateFolder(self, name):
        here = self.folders.setdefault(tuple(self.folder_path), [])
        if name in here:
            return False
        here.append(name)
        self.folders[tuple(self.folder_path) + (name,)] = []
        return True

    def DeleteProject(self, name):
        if self.current and self.current.name == name:
            return False
        if name in self.recently_open:
            self.recently_open.discard(name)  # measured: flaky first attempt
            return False
        return self.projects.pop(name, None) is not None

    def CreateCloudProject(self, settings):
        if not settings.get("cloud_name"):
            return None
        self.cloud_settings = settings
        self.current = self._add(Project(settings["cloud_name"]))
        return self.current

    def LoadCloudProject(self, settings):
        self.cloud_settings = settings
        proj = self.projects.get(settings.get("cloud_name"))
        if proj:
            self.current = proj
        return proj

    def GetCurrentProject(self):
        return self.current

    def GetProjectListInCurrentFolder(self):
        return list(self.projects)

    def LoadProject(self, name):
        if self.current:
            self.recently_open.add(self.current.name)
        self.current = self.projects.get(name)
        return self.current

    def SaveProject(self):
        if self.current is None or self.current.name == "Untitled Project":
            return False
        self.saved = True
        self.saves += 1
        return True

    def ExportProject(self, name, path, with_stills):
        Path(path).write_bytes(b"drp")
        self.exported_project = (name, path, with_stills)
        return name in self.projects

    def ImportProject(self, path, name=None):
        name = name or Path(path).stem
        if name in self.projects:
            return False
        self._add(Project(name))
        return True

    def CreateProject(self, name):
        if name in self.projects:
            return None
        self.current = self._add(Project(name))
        return self.current


class MediaStorage:
    def GetMountedVolumeList(self):
        return ["/Volumes/RAID", "/Volumes/SSD"]

    def GetSubFolderList(self, path):
        return sorted(str(p) for p in Path(path).iterdir() if p.is_dir())

    def GetFileList(self, path):
        return sorted(str(p) for p in Path(path).iterdir() if p.is_file())


class Resolve:
    EXPORT_LUT_17PTCUBE, EXPORT_LUT_33PTCUBE, EXPORT_LUT_65PTCUBE = 0, 1, 2
    EXPORT_NONE, EXPORT_AAF_NEW, EXPORT_AAF_EXISTING, EXPORT_CDL, EXPORT_SDL, EXPORT_MISSING_CLIPS = 0, 1, 2, 3, 4, 5
    EXPORT_AAF, EXPORT_DRT, EXPORT_EDL, EXPORT_FCP_7_XML, EXPORT_OTIO = 10, 11, 12, 13, 14
    EXPORT_FCPXML_1_8, EXPORT_FCPXML_1_9, EXPORT_FCPXML_1_10 = 18, 19, 20
    EXPORT_TEXT_CSV, EXPORT_TEXT_TAB = 30, 31
    NORMALIZE_AUDIO_SET_LEVEL_RELATIVE, NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT = 0, 1
    AUDIO_SYNC_MODE, AUDIO_SYNC_CHANNEL_NUMBER = "mode", "channel"
    AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO, AUDIO_SYNC_RETAIN_VIDEO_METADATA = "embedded", "metadata"
    AUDIO_SYNC_WAVEFORM, AUDIO_SYNC_TIMECODE = 10, 11
    AUDIO_SYNC_CHANNEL_AUTOMATIC, AUDIO_SYNC_CHANNEL_MIX = -1, -2
    SUBTITLE_LANGUAGE, SUBTITLE_CAPTION_PRESET, SUBTITLE_LINE_BREAK = "lang", "preset", "linebreak"
    SUBTITLE_CHARS_PER_LINE, SUBTITLE_GAP = "cpl", "gap"
    AUTO_CAPTION_AUTO, AUTO_CAPTION_ENGLISH, AUTO_CAPTION_FRENCH = 100, 101, 102
    AUTO_CAPTION_SUBTITLE_DEFAULT, AUTO_CAPTION_TELETEXT, AUTO_CAPTION_NETFLIX = 200, 201, 202
    AUTO_CAPTION_LINE_SINGLE, AUTO_CAPTION_LINE_DOUBLE = 300, 301
    DLB_BLEND_SHOTS = 400
    KEYFRAME_MODE_ALL, KEYFRAME_MODE_COLOR, KEYFRAME_MODE_SIZING = 0, 1, 2
    MULTICAM_ANGLE_SYNC_IN, MULTICAM_ANGLE_SYNC_OUT, MULTICAM_ANGLE_SYNC_TIMECODE = 600, 601, 602
    MULTICAM_ANGLE_SYNC_AUDIO, MULTICAM_ANGLE_SYNC_MARKER = 603, 604
    MULTICAM_AUDIO_ADAPTIVE, MULTICAM_AUDIO_SOURCE, MULTICAM_AUDIO_REFERENCE, MULTICAM_AUDIO_ALL = 610, 611, 612, 613
    MULTICAM_ANGLE_NAME_SEQUENTIAL, MULTICAM_ANGLE_NAME_ANGLE, MULTICAM_ANGLE_NAME_CAMERA = 620, 621, 622
    MULTICAM_ANGLE_NAME_CLIP, MULTICAM_ANGLE_NAME_FILE = 623, 624
    MULTICAM_DETECT_NONE, MULTICAM_DETECT_BY_CAMERA_NUMBER, MULTICAM_DETECT_BY_ANGLE = 630, 631, 632
    MULTICAM_DETECT_BY_REEL_NUMBER, MULTICAM_DETECT_BY_REEL_NAME, MULTICAM_DETECT_BY_ROLL_CARD = 633, 634, 635
    AUTO_ALIGN_CLIPS_USING_TIMECODE, AUTO_ALIGN_CLIPS_USING_WAVEFORM = 640, 641
    AUTO_ALIGN_CLIPS_WAVEFORM_TRACK_MIX, AUTO_ALIGN_CLIPS_WAVEFORM_TRACK_AUTOMATIC = -2, -1
    SMART_SWITCH_WIDE_ANGLE_FREQ_LOW, SMART_SWITCH_WIDE_ANGLE_FREQ_MEDIUM, SMART_SWITCH_WIDE_ANGLE_FREQ_HIGH = 650, 651, 652
    SMART_SWITCH_QUALITY_FASTER, SMART_SWITCH_QUALITY_BETTER = 660, 661
    SMART_SWITCH_ANALYSIS_MODE_NONE, SMART_SWITCH_ANALYSIS_MODE_DETECT_WIDE_ANGLE = 670, 671
    SMART_SWITCH_ANALYSIS_MODE_AUDIO_ONLY = 672
    FLATTEN_MULTICAM_COPY_GRADE, FLATTEN_MULTICAM_RETAIN_GRADE_FROM_ANGLE = 680, 681

    def ValidateDCTL(self, source):
        if "transform" not in source:
            return "cannot find main DCTL function."
        if "\n" not in source.strip():
            return "DCTL Error: main DCTL function does not have return value."  # measured single-line misread
        return None

    def SetKeyframeMode(self, mode):
        self.keyframe_mode = (mode, UI["page"])
        return True
    CLOUD_SETTING_PROJECT_NAME, CLOUD_SETTING_PROJECT_MEDIA_PATH = "cloud_name", "cloud_media"
    CLOUD_SETTING_IS_COLLAB, CLOUD_SETTING_SYNC_MODE, CLOUD_SETTING_IS_CAMERA_ACCESS = "cloud_collab", "cloud_sync", "cloud_cam"
    CLOUD_SYNC_NONE, CLOUD_SYNC_PROXY_ONLY, CLOUD_SYNC_PROXY_AND_ORIG = 500, 501, 502

    def GetFairlightPresets(self):
        return ["Dialogue Mix", "Podcast"]

    def __init__(self):
        self.pm = ProjectManager()
        UI["page"] = "edit"
        self.pages_visited = []

    @property
    def page(self):
        return UI["page"]

    def OpenPage(self, page):
        UI["page"] = page
        self.pages_visited.append(page)
        return True

    def GetMediaStorage(self):
        return MediaStorage()

    def GetProjectManager(self):
        return self.pm

    def GetProductName(self):
        return "DaVinci Resolve"

    def GetVersionString(self):
        return "19.0.0"

    def GetCurrentPage(self):
        return UI["page"]


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
