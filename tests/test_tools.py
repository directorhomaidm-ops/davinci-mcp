import asyncio
import json
import logging
import math
import os
import subprocess
from datetime import datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import davinci_mcp as d
from conftest import Clip, Folder, render_look


def test_status_without_project(resolve):
    assert d.status() == {
        "product": "DaVinci Resolve",
        "version": "19.0.0",
        "page": "edit",
        "project": None,
        "timeline": None,
        "start_frame": None,
        "end_frame": None,
    }


def test_status_with_timeline(project):
    s = d.status()
    assert (s["project"], s["timeline"], s["start_frame"]) == ("Demo", "Main", 86400)


def test_not_running(monkeypatch, resolve):
    monkeypatch.setattr(d.sys.modules["DaVinciResolveScript"], "scriptapp", lambda app: None)
    with pytest.raises(ToolError, match="is DaVinci Resolve running"):
        d.status()


def test_not_installed(monkeypatch):
    # No fake module: the real import fails the way it does on a machine without Resolve.
    monkeypatch.delitem(d.sys.modules, "DaVinciResolveScript", raising=False)
    monkeypatch.setenv("RESOLVE_SCRIPT_API", "/nonexistent/Scripting")
    with pytest.raises(ToolError, match="is DaVinci Resolve installed"):
        d.status()


def test_no_project_open(resolve):
    with pytest.raises(ToolError, match="no project open"):
        d.list_clips()


def test_no_timeline_open(resolve):
    resolve.pm.LoadProject("Demo")
    with pytest.raises(ToolError, match="no timeline open"):
        d.list_items()


def test_projects(resolve):
    assert d.list_projects() == ["Demo"]
    assert d.create_project("New") == "created: New"
    assert resolve.pm.current.name == "New"
    with pytest.raises(ToolError, match="name taken"):
        d.create_project("New")
    assert d.open_project("Demo") == "opened: Demo"
    with pytest.raises(ToolError, match="cannot open project: Nope"):
        d.open_project("Nope")


def test_import_media(project, tmp_path):
    f = tmp_path / "shot.mov"
    f.write_bytes(b"")
    assert d.import_media([str(f)]) == ["shot.mov"]
    with pytest.raises(ToolError, match="not found"):
        d.import_media([str(tmp_path / "missing.mov")])


def test_list_clips_recurses(project):
    assert d.list_clips() == [
        {"folder": "/", "name": "a.mov", "frames": "100"},
        {"folder": "B-roll/", "name": "b.mov", "frames": "50"},
    ]


def test_timelines(project):
    assert d.create_timeline("Alt") == "created: Alt"
    assert d.list_timelines() == [{"name": "Main", "current": False}, {"name": "Alt", "current": True}]
    assert d.switch_timeline("Main") == "switched: Main"
    assert project.current.name == "Main"
    with pytest.raises(ToolError, match="timeline not found"):
        d.switch_timeline("Nope")
    with pytest.raises(ToolError, match="cannot create timeline"):
        d.create_timeline("Main")


def test_append_whole_clips(project):
    assert d.append_clips(["a.mov", "b.mov"]) == "appended 2 clip(s) to 'Main'"
    assert [(i["name"], i["duration"]) for i in d.list_items()] == [("a.mov", 100), ("b.mov", 50)]


def test_append_subclips(project):
    d.append_clips(["a.mov"], start_frame=10, end_frame=19, track=1)
    (entry,) = project.pool.appended[-1]
    assert (entry["startFrame"], entry["endFrame"]) == (10, 20)  # end_frame inclusive, Resolve's endFrame exclusive
    assert d.list_items()[0]["duration"] == 10
    assert "trackIndex" not in entry  # the form Blackmagic's example uses, which renders


def test_append_subclips_other_track(project):
    d.append_clips(["a.mov"], start_frame=0, end_frame=9, track=2)
    (entry,) = project.pool.appended[-1]
    assert (entry["trackIndex"], entry["mediaType"]) == (2, 1)  # without mediaType the item renders black


def test_append_subclip_defaults_end_to_last_frame(project):
    d.append_clips(["b.mov"], start_frame=5)
    (entry,) = project.pool.appended[-1]
    assert (entry["startFrame"], entry["endFrame"]) == (5, 50)  # through the last frame (49) of a 50-frame clip


def test_append_subclip_counts_from_the_clips_first_frame(project):
    project.pool.root.clips[0].props["Start"] = "1"  # image sequences number their frames from 1
    d.append_clips(["a.mov"], start_frame=0, end_frame=23)
    (entry,) = project.pool.appended[-1]
    assert (entry["startFrame"], entry["endFrame"]) == (1, 25)
    assert d.list_items()[0]["duration"] == 24


def test_append_unknown_clip(project):
    with pytest.raises(ToolError, match="clips not in media pool: nope.mov"):
        d.append_clips(["a.mov", "nope.mov"])


def test_list_items_indexes_from_one(project):
    d.append_clips(["a.mov"])
    assert d.list_items() == [{"index": 1, "name": "a.mov", "start": 86400, "end": 86500, "duration": 100}]
    assert d.list_items(track=9) == []


def test_set_item_properties(project):
    d.append_clips(["a.mov"])
    assert d.set_item_properties(1, {"ZoomX": 1.5, "Opacity": 50}) == {
        "item": "a.mov",
        "set": {"ZoomX": 1.5, "Opacity": 50},
    }
    for bad in (0, 2):
        with pytest.raises(ToolError, match=f"item {bad} not found"):
            d.set_item_properties(bad, {"ZoomX": 1.0})
    with pytest.raises(ToolError, match="SetProperty failed for: Bogus"):
        d.set_item_properties(1, {"ZoomX": 1.0, "Bogus": 1})


def test_insert_title(project):
    assert d.insert_title() == {"title": "Text", "start": 86400, "end": 86520, "text_set": False}
    with pytest.raises(ToolError, match="could not insert title: Nope"):
        d.insert_title("Nope")


def test_insert_fusion_title_sets_text(project):
    out = d.insert_title("Text+", fusion=True, text="Hello")
    assert out["text_set"] is True
    item = project.current.tracks[("video", 1)][-1]
    assert item.comps[0].FindTool("Template").inputs["StyledText"].value == "Hello"


def test_add_marker(project):
    assert d.add_marker(24, note="cut here", color="Red") == "marker @ 24 (Red)"
    assert project.current.markers[24] == {"color": "Red", "name": "cut here", "note": "cut here", "duration": 1,
                                           "customData": ""}
    d.add_marker(48)
    assert project.current.markers[48]["name"] == "frame 48"
    with pytest.raises(ToolError, match="duplicate frame"):
        d.add_marker(24)


def test_render(project, tmp_path):
    assert d.list_render_presets() == ["H.264 Master", "YouTube 1080p"]
    out = d.render(str(tmp_path), preset="YouTube 1080p", file_name="final")
    assert out == {"job": "job-1", "target_dir": str(tmp_path), "started": True}
    assert project.loaded_preset == "YouTube 1080p"
    assert project.render_settings == {"TargetDir": str(tmp_path), "CustomName": "final", "SelectAllFrames": True}
    assert project.render_mode == 1  # single clip
    assert d.render_status("job-1")["JobStatus"] == "Rendering"
    with pytest.raises(ToolError, match="unknown render preset"):
        d.render(str(tmp_path), preset="Nope")


def test_all_tools_registered():
    names = {t.name for t in asyncio.run(d.mcp.list_tools())}
    assert names == {
        "status", "list_projects", "open_project", "create_project", "import_media", "list_clips",
        "list_timelines", "create_timeline", "switch_timeline", "append_clips", "list_items",
        "set_item_properties", "insert_title", "add_marker", "list_render_presets", "render", "render_status",
        "list_render_formats", "stop_render", "open_page", "color_info", "apply_lut", "set_cdl", "copy_grade",
        "apply_drx", "add_color_version", "load_color_version", "export_lut", "grab_still",
        "fusion_comps", "add_fusion_comp", "export_fusion_comp", "insert_fusion", "create_fusion_clip",
        "fusion_nodes", "fusion_inputs", "add_fusion_node", "connect_fusion_nodes", "delete_fusion_node",
        "set_fusion_input", "timeline_overview", "view_frame", "add_transition", "delete_items",
        "set_clip_enabled", "stabilize", "smart_reframe", "detect_scene_cuts", "dynamic_zoom",
        "insert_fusion_effect", "fairlight_info", "apply_fairlight_preset", "add_track", "set_track",
        "delete_track", "voice_isolation", "normalize_audio", "set_fades", "set_speed", "convert_to_stereo",
        "insert_audio", "sync_audio", "transcribe_audio", "create_subtitles", "magic_mask", "link_mask_to_tracker",
        "render_queue", "start_render", "delete_render_jobs", "save_render_preset", "browse_storage", "create_bin",
        "move_clips", "delete_clips", "import_image_sequence", "clip_info", "tag_clips", "relink_clips", "link_proxy",
        "replace_clip", "export_metadata", "export_timeline", "import_timeline", "save_project", "export_project",
        "import_project", "color_management_info", "set_color_management", "apply_color_preset", "set_hdr",
        "set_clip_color_space", "analyze_dolby_vision", "project_browser", "create_project_folder",
        "rename_project", "delete_project", "list_databases", "switch_database", "create_cloud_project",
        "load_cloud_project", "refresh_collaboration", "duplicate_timeline", "rename_timeline", "delete_timelines",
        "review_notes", "add_review_note", "resolve_review_note", "delete_markers", "export_review_notes",
        "get_transcript", "export_transcript", "write_subtitles", "list_titles", "set_title_text",
        "animate_clip", "list_keyframes", "clear_keyframes", "set_color_keyframe_mode", "create_multicam",
        "auto_align_clips", "smart_switch", "flatten_multicam", "generate_voiceover", "classify_audio",
        "find_audio", "generate_sound", "detect_beats", "mark_beats", "list_transitions", "transition_all_cuts",
        "remove_transitions", "letterbox", "picture_in_picture", "split_screen", "vignette", "camera_shake",
        "node_graph", "set_node_lut", "set_node_enabled", "reset_grade", "apply_drx_to", "color_groups",
        "create_color_group", "delete_color_group", "assign_color_group", "apply_arri_cdl_lut", "color_cache",
        "gallery_albums", "import_stills", "validate_dctl", "super_scale", "ai_slow_motion", "remove_silences",
        "cut_by_transcript", "social_platforms", "social_timeline", "social_render", "social_export",
        "animated_title", "lower_third", "save_template", "list_templates", "apply_template", "batch_titles",
        "analyze_color", "auto_color", "shot_match", "auto_organize", "color_code", "find_unused",
        "find_duplicates", "find_offline", "clean_bins",
    }


def test_call_through_mcp(project):
    result = asyncio.run(d.mcp.call_tool("add_marker", {"frame": 10, "color": "Green"}))
    assert result.structured_content == {"result": "marker @ 10 (Green)"}


def test_tool_call_is_logged(project, caplog):
    with caplog.at_level(logging.INFO, logger="davinci_mcp"):
        d.add_marker(12, note="x")
    (rec,) = caplog.records
    assert rec.fields["tool"] == "add_marker"
    assert rec.fields["args"] == {"frame": 12, "note": "x"}
    assert rec.fields["outcome"] == "ok"
    assert rec.fields["duration_ms"] >= 0


def test_tool_error_is_logged(project, caplog):
    d.add_marker(12)
    with caplog.at_level(logging.INFO, logger="davinci_mcp"), pytest.raises(ToolError):
        d.add_marker(12)
    (rec,) = caplog.records
    assert rec.levelname == "WARNING"
    assert rec.fields["outcome"] == "error"
    assert "duplicate frame" in rec.fields["error"]


def test_crash_is_logged(project, caplog, monkeypatch):
    monkeypatch.setattr(project, "GetRenderPresetList", lambda: 1 / 0)
    with caplog.at_level(logging.INFO, logger="davinci_mcp"), pytest.raises(ZeroDivisionError):
        d.list_render_presets()
    (rec,) = caplog.records
    assert rec.levelname == "ERROR"
    assert rec.fields["outcome"] == "crash"
    assert rec.exc_info


def test_json_formatter():
    rec = logging.LogRecord("davinci_mcp", logging.WARNING, __file__, 1, "tool call failed", None, None)
    rec.fields = {"tool": "render", "args": {"target_dir": "/out"}, "outcome": "error"}
    entry = json.loads(d._JsonFormatter().format(rec))
    assert entry["level"] == "WARNING"
    assert entry["msg"] == "tool call failed"
    assert entry["tool"] == "render"
    assert entry["args"] == {"target_dir": "/out"}
    assert entry["ts"].endswith("+00:00")


def test_setup_logging_writes_json_to_stderr(monkeypatch, capsys):
    monkeypatch.setenv("DAVINCI_MCP_LOG_LEVEL", "debug")
    monkeypatch.setattr(d.log, "handlers", [])
    monkeypatch.setattr(d.log, "propagate", True)
    monkeypatch.setattr(d.log, "level", logging.NOTSET)
    d._setup_logging()
    d.log.debug("hello", extra={"fields": {"k": 1}})
    out, err = capsys.readouterr()
    assert out == ""
    entry = json.loads(err)
    assert (entry["level"], entry["msg"], entry["k"]) == ("DEBUG", "hello", 1)


def test_setup_logging_ignores_bad_level(monkeypatch):
    monkeypatch.setenv("DAVINCI_MCP_LOG_LEVEL", "verbose")
    monkeypatch.setattr(d.log, "handlers", [])
    monkeypatch.setattr(d.log, "propagate", True)
    monkeypatch.setattr(d.log, "level", logging.NOTSET)
    d._setup_logging()
    assert d.log.level == logging.INFO


# --- color grading ---


def _two_items(project):
    d.append_clips(["a.mov", "b.mov"])
    return project.current.tracks[("video", 1)]


def test_open_page(resolve):
    assert d.open_page("color") == "page: color"
    assert resolve.page == "color"
    with pytest.raises(ToolError, match="unknown page"):
        d.open_page("colour")


def test_color_info(project):
    a, _ = _two_items(project)
    a.graph.luts[2] = "Film/Kodak.cube"
    assert d.color_info(1) == {
        "item": "a.mov",
        "nodes": [{"index": 1, "label": "Primary", "lut": None}, {"index": 2, "label": "Look", "lut": "Film/Kodak.cube"}],
        "version": {"versionName": "Version 1", "versionType": 0},
        "local_versions": ["Version 1"],
        "remote_versions": [],
        "color_group": None,
    }


def test_color_info_defaults_to_playhead_item(project):
    _, b = _two_items(project)
    from conftest import ColorGroup

    b.color_group = ColorGroup("Interview")
    project.current.playhead_item = b
    info = d.color_info()
    assert (info["item"], info["color_group"]) == ("b.mov", "Interview")


def test_color_info_nothing_under_playhead(project):
    with pytest.raises(ToolError, match="no video item under the playhead"):
        d.color_info()


def test_color_info_pre_19_item_without_graph(project, monkeypatch):
    a, _ = _two_items(project)
    # Before Resolve 19 there is no GetNodeGraph/GetColorGroup; node calls live on the item.
    monkeypatch.delattr(type(a), "GetNodeGraph")
    monkeypatch.delattr(type(a), "GetColorGroup")
    a.GetNumNodes = lambda: 1
    a.GetNodeLabel = lambda n: "Old"
    a.GetLUT = lambda n: ""
    info = d.color_info(1)
    assert info["nodes"] == [{"index": 1, "label": "Old", "lut": None}]
    assert info["color_group"] is None


def test_apply_lut(project):
    a, _ = _two_items(project)
    assert d.apply_lut(1, "Film/Kodak.cube", node=2) == "LUT on node 2 of 'a.mov': Film/Kodak.cube"
    assert a.graph.luts == {2: "Film/Kodak.cube"}
    with pytest.raises(ToolError, match=r"node 3 out of range \(item has 2 node"):
        d.apply_lut(1, "Film/Kodak.cube", node=3)
    with pytest.raises(ToolError, match="SetLUT failed"):
        d.apply_lut(1, "nope.txt")


def test_set_cdl(project):
    a, _ = _two_items(project)
    d.set_cdl(1, slope=[1.1, 1, 0.9], offset=[0, 0, 0.02], power=[1, 1, 1], saturation=0.8, node=2)
    assert a.cdl == {
        "NodeIndex": "2",
        "Slope": "1.1 1.0 0.9",
        "Offset": "0.0 0.0 0.02",
        "Power": "1.0 1.0 1.0",
        "Saturation": "0.8",
    }
    with pytest.raises(ToolError, match="slope needs 3 values"):
        d.set_cdl(1, slope=[1, 1])


def test_set_cdl_defaults_are_identity(project):
    a, _ = _two_items(project)
    d.set_cdl(1)
    assert (a.cdl["Slope"], a.cdl["Offset"], a.cdl["Power"], a.cdl["Saturation"]) == (
        "1.0 1.0 1.0", "0.0 0.0 0.0", "1.0 1.0 1.0", "1.0",
    )


def test_copy_grade(project):
    a, b = _two_items(project)
    assert d.copy_grade(1, [2]) == "copied grade of 'a.mov' to 1 item(s)"
    assert a.copied_to == [b]
    with pytest.raises(ToolError, match="item 5 not found"):
        d.copy_grade(1, [5])
    with pytest.raises(ToolError, match="no targets"):
        d.copy_grade(1, [])


def test_apply_drx(project, tmp_path):
    a, b = _two_items(project)
    drx = tmp_path / "look.drx"
    drx.write_text("")
    assert d.apply_drx(str(drx), [1, 2], keyframes="source_timecode") == "applied look.drx to 2 item(s)"
    assert project.current.drx == (str(drx), 1, [a, b])
    with pytest.raises(ToolError, match="unknown keyframes mode"):
        d.apply_drx(str(drx), [1], keyframes="tc")
    with pytest.raises(ToolError, match="file not found"):
        d.apply_drx(str(tmp_path / "missing.drx"), [1])


def test_color_versions(project):
    a, _ = _two_items(project)
    assert d.add_color_version(1, "Warm") == "added local version 'Warm' to 'a.mov'"
    assert a.version == {"versionName": "Warm", "versionType": 0}
    with pytest.raises(ToolError, match="name taken"):
        d.add_color_version(1, "Warm")
    d.add_color_version(1, "Client", remote=True)
    assert a.versions[1] == ["Client"]
    assert d.load_color_version(1, "Version 1") == "loaded version 'Version 1' on 'a.mov'"
    assert a.version["versionName"] == "Version 1"
    with pytest.raises(ToolError, match="version not found"):
        d.load_color_version(1, "Client")  # it is remote, not local


def test_export_lut(project, tmp_path, resolve):
    a, _ = _two_items(project)
    out = tmp_path / "grade.cube"
    d.export_lut(1, str(out), size=65)
    assert a.exported_lut == (resolve.EXPORT_LUT_65PTCUBE, str(out))
    with pytest.raises(ToolError, match="unsupported LUT size"):
        d.export_lut(1, str(out), size=32)


def test_grab_still(project):
    assert d.grab_still() == "still grabbed into the current gallery album"
    project.current.page_is_color = False
    with pytest.raises(ToolError, match="is the Color page open"):
        d.grab_still()


# --- render formats ---


def test_list_render_formats_keyed_by_id(project):
    # Resolve returns {name: id} and {description: id}; only ids are accepted, so the listing is keyed by id.
    assert d.list_render_formats() == {
        "mov": {"name": "QuickTime", "codecs": {"ProRes422HQ": "Apple ProRes 422 HQ", "H264": "H.264"}},
        "mp4": {"name": "MP4", "codecs": {"H264": "H.264"}},
        "wav": {"name": "Wave", "codecs": {}},
    }


def test_render_with_format_and_codec(project, tmp_path):
    d.render(str(tmp_path), format="mov", codec="ProRes422HQ")
    assert project.format_codec == ("mov", "ProRes422HQ")
    with pytest.raises(ToolError, match="must be given together"):
        d.render(str(tmp_path), preset="YouTube 1080p", format="mov")
    assert project.loaded_preset is None  # rejected before touching the project
    with pytest.raises(ToolError, match="unsupported format/codec"):
        d.render(str(tmp_path), format="mp4", codec="ProRes422HQ")
    with pytest.raises(ToolError, match="unknown render format id or one without selectable codecs: QuickTime"):
        d.render(str(tmp_path), format="QuickTime", codec="ProRes422HQ")
    with pytest.raises(ToolError, match="without selectable codecs: wav"):
        d.render(str(tmp_path), format="wav", codec="")


def test_stop_render(project, tmp_path):
    assert d.stop_render() == "nothing rendering"
    d.render(str(tmp_path))
    assert d.stop_render() == "stopped"
    assert project.rendering is False


# --- Fusion ---


@pytest.fixture
def fusion(project):
    """Item 1 on V1 is a Fusion composition clip with an empty MediaIn -> MediaOut comp."""
    d.insert_fusion()
    item = project.current.tracks[("video", 1)][0]
    return item.comps[0]


def _assert_lock_rules(comp):
    # Live Resolve ignores value writes made under Lock() at render; structural edits need it.
    assert comp.value_writes_under_lock == 0
    assert comp.unlocked_structural_edits == 0
    assert comp.undo_open == 0


def test_insert_fusion(project):
    assert d.insert_fusion() == {"name": "Fusion Composition", "start": 86400, "end": 86550}
    assert d.insert_fusion("generator", "Contours")["name"] == "Contours"
    with pytest.raises(ToolError, match="generator name is required"):
        d.insert_fusion("generator")
    with pytest.raises(ToolError, match="could not insert Fusion generator: Nope"):
        d.insert_fusion("generator", "Nope")
    with pytest.raises(ToolError, match="unknown kind"):
        d.insert_fusion("title")


def test_fusion_comps_add_import_export(project, tmp_path):
    d.append_clips(["a.mov"])
    assert d.fusion_comps(1) == []
    assert d.add_fusion_comp(1) == {"item": "a.mov", "comp": 1, "comps": ["Composition 1"]}
    tpl = tmp_path / "glow_template.comp"
    tpl.write_text("")
    # live 21.1 imports into the active comp, keeping its name; a comp of its own keeps Composition 1 intact
    assert d.add_fusion_comp(1, import_path=str(tpl))["comps"] == ["Composition 1", "Composition 2"]
    assert d.fusion_comps(1) == ["Composition 1", "Composition 2"]
    with pytest.raises(ToolError, match="file not found"):
        d.add_fusion_comp(1, import_path=str(tmp_path / "missing.comp"))

    out = tmp_path / "out.comp"
    d.export_fusion_comp(1, str(out), comp=2)
    assert project.current.tracks[("video", 1)][0].exported_comp == (str(out), 2)
    with pytest.raises(ToolError, match=r"comp 3 not found on 'a.mov' \(2 comp"):
        d.export_fusion_comp(1, str(out), comp=3)


def test_create_fusion_clip(project):
    d.append_clips(["a.mov", "b.mov"])
    assert d.create_fusion_clip([1, 2]) == {"name": "Fusion Clip 1", "start": 86400, "end": 86550}
    assert [i.name for i in project.current.fusion_clip_of] == ["a.mov", "b.mov"]
    with pytest.raises(ToolError, match="no items"):
        d.create_fusion_clip([])


def test_build_blur_chain(fusion):
    # MediaIn1 -> Blur -> MediaOut1, the basic VFX insert.
    assert d.add_fusion_node(1, "Blur", name="Soften", connect_from="MediaIn1") == {
        "name": "Soften",
        "type": "Blur",
        "connected": {"Input": "MediaIn1"},
    }
    assert d.connect_fusion_nodes(1, "MediaOut1", "Soften") == "Soften → MediaOut1.Input"
    nodes = {n["name"]: n for n in d.fusion_nodes(1)}
    assert nodes["Soften"]["inputs"] == {"Input": "MediaIn1"}
    assert nodes["MediaOut1"]["inputs"] == {"Input": "Soften"}
    _assert_lock_rules(fusion)


def test_add_fusion_node_errors(fusion):
    with pytest.raises(ToolError, match="unknown tool type: Blurr"):
        d.add_fusion_node(1, "Blurr")
    with pytest.raises(ToolError, match="node not found: Nope"):
        d.add_fusion_node(1, "Blur", connect_from="Nope")
    with pytest.raises(ToolError, match="already exists"):
        d.add_fusion_node(1, "Blur", name="MediaIn1")
    with pytest.raises(ToolError, match="could not connect MediaIn1 to its Size"):
        d.add_fusion_node(1, "Transform", connect_from="MediaIn1", input="Size")
    _assert_lock_rules(fusion)


def test_auto_names(fusion):
    assert d.add_fusion_node(1, "Blur")["name"] == "Blur1"
    assert d.add_fusion_node(1, "Blur")["name"] == "Blur2"


def test_mask_and_disconnect(fusion):
    d.add_fusion_node(1, "Blur", connect_from="MediaIn1")
    d.add_fusion_node(1, "EllipseMask", name="Vignette")
    d.connect_fusion_nodes(1, "Blur1", "Vignette", input="EffectMask")
    assert {n["name"]: n for n in d.fusion_nodes(1)}["Blur1"]["inputs"] == {"Input": "MediaIn1", "EffectMask": "Vignette"}
    assert d.connect_fusion_nodes(1, "Blur1", None, input="EffectMask") == "disconnected Blur1.EffectMask"
    with pytest.raises(ToolError, match="cannot connect Vignette to Blur1.XBlurSize"):
        d.connect_fusion_nodes(1, "Blur1", "Vignette", input="XBlurSize")
    _assert_lock_rules(fusion)


def test_delete_fusion_node(fusion):
    d.add_fusion_node(1, "Blur")
    assert d.delete_fusion_node(1, "Blur1") == "deleted Blur1"
    assert [n["name"] for n in d.fusion_nodes(1)] == ["MediaIn1", "MediaOut1"]
    with pytest.raises(ToolError, match="node not found"):
        d.delete_fusion_node(1, "Blur1")
    _assert_lock_rules(fusion)


def test_set_static_values(fusion):
    d.add_fusion_node(1, "TextPlus", name="Title")
    assert d.set_fusion_input(1, "Title", "StyledText", "Breaking News")["value"] == "Breaking News"
    assert d.set_fusion_input(1, "Title", "Size", 0.12)["value"] == 0.12
    # Point: this build rejects [x, y], so the {1: x, 2: y} encoding is used.
    assert d.set_fusion_input(1, "Title", "Center", [0.5, 0.2])["value"] == {"1": 0.5, "2": 0.2}
    _assert_lock_rules(fusion)
    assert fusion.undo_steps == 3


def test_set_input_errors(fusion):
    d.add_fusion_node(1, "Transform")
    with pytest.raises(ToolError, match="either value or keyframes"):
        d.set_fusion_input(1, "Transform1", "Size")
    with pytest.raises(ToolError, match="either value or keyframes"):
        d.set_fusion_input(1, "Transform1", "Size", 1.0, keyframes={0: 1.0})
    with pytest.raises(ToolError, match="Transform1 has no input Sise"):
        d.set_fusion_input(1, "Transform1", "Sise", 1.0)
    with pytest.raises(ToolError, match=r"point input needs \[x, y\]"):
        d.set_fusion_input(1, "Transform1", "Center", 0.5)


def test_keyframe_number_attaches_spline(fusion):
    d.add_fusion_node(1, "Transform", connect_from="MediaIn1")
    out = d.set_fusion_input(1, "Transform1", "Size", keyframes={0: 1.0, 75: 1.4})
    assert out == {"node": "Transform1", "input": "Size", "keyframes": [0.0, 75.0]}
    tool = fusion.FindTool("Transform1")
    assert tool.inputs["Size"].keys == {0: 1.0, 75: 1.4}
    # Adding more keys reuses the existing spline instead of attaching another.
    d.set_fusion_input(1, "Transform1", "Size", keyframes={150: 1.0})
    assert sum(t.kind == "BezierSpline" for t in fusion.tools) == 1
    node = {n["name"]: n for n in d.fusion_nodes(1)}["Transform1"]
    assert node["animated"] == ["Size"]
    assert "BezierSpline1" not in [n["name"] for n in d.fusion_nodes(1)]
    _assert_lock_rules(fusion)


def test_keyframe_point_uses_path(fusion):
    d.add_fusion_node(1, "Transform")
    d.set_fusion_input(1, "Transform1", "Center", keyframes={0: [0.2, 0.5], 48: [0.8, 0.5]})
    tool = fusion.FindTool("Transform1")
    assert tool.inputs["Center"].source.tool.kind == "PolyPath"
    assert tool.inputs["Center"].keys == {0: {1: 0.2, 2: 0.5}, 48: {1: 0.8, 2: 0.5}}
    _assert_lock_rules(fusion)


def test_keyframe_text_is_rejected(fusion):
    d.add_fusion_node(1, "TextPlus")
    with pytest.raises(ToolError, match=r"TextPlus1.StyledText \(Text\) cannot be animated"):
        d.set_fusion_input(1, "TextPlus1", "StyledText", keyframes={0: "a"})
    # Nothing was written as a static value by mistake.
    assert fusion.FindTool("TextPlus1").inputs["StyledText"].value == ""


def test_keyframes_through_mcp_with_json_keys(fusion):
    d.add_fusion_node(1, "Transform")
    args = {"item": 1, "node": "Transform1", "input": "Size", "keyframes": {"0": 1.0, "24": 2.0}}
    result = asyncio.run(d.mcp.call_tool("set_fusion_input", args))
    assert json.loads(result.content[0].text)["keyframes"] == [0.0, 24.0]


def test_fusion_inputs(fusion):
    d.add_fusion_node(1, "Blur", connect_from="MediaIn1")
    d.set_fusion_input(1, "Blur1", "XBlurSize", keyframes={0: 0.0, 24: 20.0})
    assert d.fusion_inputs(1, "Blur1") == [
        {"id": "Input", "name": "Input", "type": "Image", "value": None, "animated": False, "source": "MediaIn1"},
        {"id": "XBlurSize", "name": "XBlurSize", "type": "Number", "value": 0.0, "animated": True, "source": None},
        {"id": "EffectMask", "name": "EffectMask", "type": "Mask", "value": None, "animated": False, "source": None},
    ]
    assert [r["id"] for r in d.fusion_inputs(1, "Blur1", filter="blur")] == ["XBlurSize"]


def test_fusion_needs_a_comp(project):
    d.append_clips(["a.mov"])
    with pytest.raises(ToolError, match=r"comp 1 not found on 'a.mov' \(0 comp\(s\); see add_fusion_comp\)"):
        d.fusion_nodes(1)


# --- editing ---


def test_set_item_properties_enum_names(project):
    a, _ = _two_items(project)
    d.set_item_properties(1, {"Opacity": 80})
    a.SetProperty = lambda k, v: a.props.__setitem__(k, v) or True
    d.set_item_properties(1, {"CompositeMode": "Screen", "RetimeProcess": "optical flow", "Scaling": "fill",
                              "ResizeFilter": "lanczos", "DynamicZoomEase": "in-and-out", "ZoomX": 1.2})
    assert a.props == {"Opacity": 80, "CompositeMode": 5, "RetimeProcess": 3, "Scaling": 3, "ResizeFilter": 10,
                       "DynamicZoomEase": 3, "ZoomX": 1.2}
    d.set_item_properties(1, {"CompositeMode": 2})  # raw constants still pass through
    assert a.props["CompositeMode"] == 2
    with pytest.raises(ToolError, match="CompositeMode must be one of: normal, add"):
        d.set_item_properties(1, {"CompositeMode": "glow"})


def test_timeline_overview(project):
    d.append_clips(["a.mov", "b.mov"])
    d.add_marker(24, note="beat")
    a = project.current.tracks[("video", 1)][0]
    a.AddTransition({"type": "Cross Dissolve", "category": "simple", "position": "end", "alignment": "center", "duration": 12})
    ov = d.timeline_overview()
    assert (ov["timeline"], ov["fps"], ov["resolution"], ov["playhead"]) == ("Main", "24", ["1920", "1080"], "01:00:00:00")
    v1 = ov["tracks"]["video"][0]
    assert (v1["index"], v1["name"], v1["enabled"]) == (1, "V1", True)
    assert [(i["index"], i["kind"], i["name"], i["source"]) for i in v1["items"]] == [
        (1, "clip", "a.mov", "a.mov"),
        (2, "transition", "Cross Dissolve", None),
        (3, "clip", "b.mov", "b.mov"),
    ]
    assert v1["items"][1]["duration"] == 12
    assert v1["items"][0]["fusion_comps"] == 0
    assert ov["tracks"]["audio"][0]["items"] == []
    assert ov["markers"]["24"]["note"] == "beat"


def test_overview_title_is_other(project):
    d.append_clips(["a.mov"])
    d.insert_title()
    kinds = [i["kind"] for i in d.timeline_overview()["tracks"]["video"][0]["items"]]
    assert kinds == ["clip", "other"]


def test_view_frame_returns_image(project, tmp_path):
    out = d.view_frame(frame=86424)
    image, note = out
    assert isinstance(image, d.Image)
    assert image.data.startswith(b"\x89PNG") and image.data.endswith(b"01:00:01:00")
    assert note == "frame at 01:00:01:00"
    assert not os.path.exists(project.exported_frame)  # temp file cleaned up

    saved = tmp_path / "shot.jpg"
    _, note = d.view_frame(timecode="01:00:02:00", save_to=str(saved))
    assert saved.exists() and note.endswith(f"saved to {saved}")
    (only,) = d.view_frame(save_to=str(tmp_path / "shot.dpx"))
    assert "not viewable inline" in only


def test_view_frame_errors(project, tmp_path):
    with pytest.raises(ToolError, match="not both"):
        d.view_frame(timecode="01:00:00:00", frame=86400)
    with pytest.raises(ToolError, match="cannot move playhead"):
        d.view_frame(timecode="bogus")
    project.current.settings["timelineDropFrameTimecode"] = "1"
    with pytest.raises(ToolError, match="drop-frame"):
        d.view_frame(frame=86400)
    with pytest.raises(ToolError, match="ExportCurrentFrameAsStill failed"):
        d.view_frame(save_to=str(tmp_path / "missing_dir" / "f.png"))


def test_view_frame_cleans_temp_on_failure(project, monkeypatch):
    import glob
    import tempfile

    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "davinci_mcp_*")))
    monkeypatch.setattr(project, "ExportCurrentFrameAsStill", lambda path: False)
    with pytest.raises(ToolError):
        d.view_frame()
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "davinci_mcp_*"))) == before


def test_view_frame_through_mcp_is_image_content(project):
    result = asyncio.run(d.mcp.call_tool("view_frame", {}))
    kinds = [c.type for c in result.content]
    assert kinds == ["image", "text"]
    assert result.content[0].mime_type == "image/png"


def test_add_transition(project):
    a, b = _two_items(project)
    # timing comes from the track, not from the object AddTransition returns
    assert d.add_transition(1, duration=24) == {"name": "Cross Dissolve", "index": 2, "start": 86488, "end": 86512,
                                                "duration": 24}
    assert a.transition_options == {"type": "Cross Dissolve", "category": "simple", "position": "end",
                                    "alignment": "center", "duration": 24}
    assert [i["name"] for i in d.list_items()] == ["a.mov", "Cross Dissolve", "b.mov"]


def test_add_transition_errors(project, monkeypatch):
    a, _ = _two_items(project)
    for kwargs, msg in [({"position": "middle"}, "position must be"), ({"alignment": "top"}, "alignment must be"),
                        ({"category": "3d"}, "category must be"), ({"duration": 0}, "positive")]:
        with pytest.raises(ToolError, match=msg):
            d.add_transition(1, **kwargs)
    with pytest.raises(ToolError, match="handles"):
        d.add_transition(1, type="Page Curl")
    a.name = "a_nohandles"
    with pytest.raises(ToolError, match="handles"):
        d.add_transition(1)
    monkeypatch.delattr(type(a), "AddTransition")
    with pytest.raises(ToolError, match="AddTransition needs DaVinci Resolve 21.1 or later"):
        d.add_transition(1)


def test_delete_items(project):
    a, b = _two_items(project)
    assert d.delete_items([1], ripple=True) == "deleted 1 item(s) (ripple)"
    assert project.current.deleted == ([a], True)
    assert [i["name"] for i in d.list_items()] == ["b.mov"]
    with pytest.raises(ToolError, match="items not found"):
        d.delete_items([5])
    with pytest.raises(ToolError, match="none given"):
        d.delete_items([])


def test_set_clip_enabled(project):
    a, _ = _two_items(project)
    assert d.set_clip_enabled(1, False) == "disabled 'a.mov'"
    assert a.enabled is False
    assert d.timeline_overview()["tracks"]["video"][0]["items"][0]["enabled"] is False


def test_stabilize_and_reframe(project, monkeypatch):
    a, _ = _two_items(project)
    assert d.stabilize(1) == "stabilized 'a.mov'"
    assert a.stabilized
    assert d.smart_reframe(1) == "reframed 'a.mov'"
    d.insert_title()
    with pytest.raises(ToolError, match="Stabilize failed for 'Text'"):
        d.stabilize(3)
    monkeypatch.delattr(type(a), "SmartReframe")
    with pytest.raises(ToolError, match="SmartReframe needs DaVinci Resolve 18"):
        d.smart_reframe(1)


def test_detect_scene_cuts(project):
    assert "scene cuts detected" in d.detect_scene_cuts()
    assert project.current.scene_cuts


def test_dynamic_zoom(project):
    a, _ = _two_items(project)
    out = d.dynamic_zoom(1, start_zoom=1.0, end_zoom=1.3, start_center=[0.4, 0.5], end_center=[0.6, 0.5])
    assert out["frames"] == [0, 99]
    comp = a.comps[0]
    zoom = comp.FindTool("DynamicZoom")
    assert zoom.inputs["Size"].keys == {0: 1.0, 99: 1.3}
    assert zoom.inputs["Center"].keys == {0: {1: 0.4, 2: 0.5}, 99: {1: 0.6, 2: 0.5}}
    nodes = {n["name"]: n for n in d.fusion_nodes(1)}
    assert nodes["DynamicZoom"]["inputs"] == {"Input": "MediaIn1"}
    assert nodes["MediaOut1"]["inputs"] == {"Input": "DynamicZoom"}
    _assert_lock_rules(comp)
    with pytest.raises(ToolError, match="already has a DynamicZoom"):
        d.dynamic_zoom(1)


def test_dynamic_zoom_centered_skips_path(project):
    a, _ = _two_items(project)
    d.dynamic_zoom(1)
    zoom = a.comps[0].FindTool("DynamicZoom")
    assert zoom.inputs["Center"].source is None
    with pytest.raises(ToolError, match="zoom must be positive"):
        d.dynamic_zoom(2, end_zoom=0)


def test_insert_fusion_effect_chains(project):
    a, _ = _two_items(project)
    out = d.insert_fusion_effect(1, "SoftGlow", settings={"Gain": 0.6, "Threshold": 0.8})
    assert out == {"item": "a.mov", "node": "SoftGlow1", "type": "SoftGlow", "settings": {"Gain": 0.6, "Threshold": 0.8}}
    d.insert_fusion_effect(1, "Blur", settings={"XBlurSize": 2.0}, name="Soft")
    nodes = {n["name"]: n for n in d.fusion_nodes(1)}
    # MediaIn1 -> SoftGlow1 -> Soft -> MediaOut1
    assert nodes["SoftGlow1"]["inputs"] == {"Input": "MediaIn1"}
    assert nodes["Soft"]["inputs"] == {"Input": "SoftGlow1"}
    assert nodes["MediaOut1"]["inputs"] == {"Input": "Soft"}
    _assert_lock_rules(a.comps[0])
    with pytest.raises(ToolError, match="unknown tool type"):
        d.insert_fusion_effect(1, "NoSuchFx")
    with pytest.raises(ToolError, match="SoftGlow1 was added to the chain but a setting failed: SoftGlow1 has no input Strength"):
        d.insert_fusion_effect(2, "SoftGlow", settings={"Strength": 1})


# --- audio / Fairlight ---


@pytest.fixture
def audio(project):
    """Two dialogue items on A1."""
    from conftest import Item

    tl = project.current
    items = [Item("vo_1.wav", 86400, 86500, media=object(), timeline=tl), Item("vo_2.wav", 86500, 86600, media=object(), timeline=tl)]
    tl.tracks[("audio", 1)] = items
    return items


def test_fairlight_info(project, audio):
    project.current.voice[1] = {"isEnabled": True, "amount": 40}
    info = d.fairlight_info()
    assert info["tracks"] == [{"index": 1, "name": "A1", "format": "stereo", "enabled": True, "locked": False,
                               "voice_isolation": {"isEnabled": True, "amount": 40}, "items": 2}]
    assert info["fairlight_presets"] == ["Dialogue Mix", "Podcast"]
    assert "ITU-R BS.1770-4" in info["normalize_modes"]


def test_fairlight_info_on_older_resolve(project, resolve, monkeypatch):
    monkeypatch.delattr(type(resolve), "GetFairlightPresets")
    monkeypatch.delattr(type(project.current), "GetNormalizeAudioModes")
    info = d.fairlight_info()
    assert (info["fairlight_presets"], info["normalize_modes"]) == (None, None)


def test_apply_fairlight_preset(project, monkeypatch):
    assert d.apply_fairlight_preset("Podcast") == "applied Fairlight preset 'Podcast'"
    with pytest.raises(ToolError, match="cannot apply Fairlight preset: Nope"):
        d.apply_fairlight_preset("Nope")
    monkeypatch.delattr(type(project), "ApplyFairlightPresetToCurrentTimeline")
    with pytest.raises(ToolError, match="needs DaVinci Resolve 20.2.2"):
        d.apply_fairlight_preset("Podcast")


def test_add_track(project):
    assert d.add_track(format="5.1", name="Music") == {"track_type": "audio", "index": 2, "name": "Music", "format": "5.1"}
    assert d.add_track("video")["index"] == 2
    with pytest.raises(ToolError, match="unknown audio format: quad"):
        d.add_track(format="quad")
    with pytest.raises(ToolError, match="unknown track type"):
        d.add_track("midi")


def test_add_track_older_string_signature(project):
    project.current.add_track_takes_dict = False
    assert d.add_track(format="mono")["format"] == "mono"
    assert project.current.GetTrackCount("audio") == 2  # exactly one track added


def test_set_and_delete_track(project, audio):
    out = d.set_track("audio", 1, name="Dialogue", enabled=False, locked=True)
    assert out == {"track_type": "audio", "index": 1, "name": "Dialogue", "enabled": False, "locked": True}
    with pytest.raises(ToolError, match="nothing to change"):
        d.set_track("audio", 1)
    with pytest.raises(ToolError, match=r"audio track 3 not found \(1 audio track"):
        d.set_track("audio", 3, enabled=True)
    assert d.delete_track("audio", 1) == "deleted audio track 1 (2 item(s))"
    assert project.current.GetTrackCount("audio") == 0


def test_voice_isolation(project, audio):
    assert d.voice_isolation(1, amount=70) == {"track": 1, "state": {"isEnabled": True, "amount": 70}}
    with pytest.raises(ToolError, match="0-100"):
        d.voice_isolation(1, amount=150)
    with pytest.raises(ToolError, match="audio track 2 not found"):
        d.voice_isolation(2)


def test_normalize_audio(project, audio):
    assert d.normalize_audio([1, 2], loudness=-14) == "normalized 2 item(s) to -14 LKFS"
    items, options = project.current.normalized
    assert items == audio
    assert options == {"setLevelMode": 0, "targetLoudness": -14.0}
    d.normalize_audio([1], level=-1, mode="True Peak Program", independent=True)
    assert project.current.normalized[1] == {"setLevelMode": 1, "normalizationMode": "True Peak Program", "targetLevel": -1.0}


def test_normalize_audio_errors(project, audio, monkeypatch):
    with pytest.raises(ToolError, match="give a target"):
        d.normalize_audio([1])
    with pytest.raises(ToolError, match="unknown mode: Loud"):
        d.normalize_audio([1], loudness=-23, mode="Loud")
    with pytest.raises(ToolError, match="item 3 not found on audio track 1"):
        d.normalize_audio([3], loudness=-23)
    monkeypatch.delattr(type(project.current), "NormalizeAudioLevel")
    with pytest.raises(ToolError, match="NormalizeAudioLevel needs DaVinci Resolve 21.1"):
        d.normalize_audio([1], loudness=-23)


def test_set_fades(project, audio):
    assert d.set_fades(1, fade_in=12, fade_out=24) == {"item": "vo_1.wav", "fades": {"FadeIn": 12.0, "FadeOut": 24.0}}
    d.set_fades(1, fade_out=0)
    assert audio[0].fades == {"FadeIn": 12.0, "FadeOut": 0.0}
    with pytest.raises(ToolError, match="give fade_in and/or fade_out"):
        d.set_fades(1)
    with pytest.raises(ToolError, match="0 or more"):
        d.set_fades(1, fade_in=-1)
    with pytest.raises(ToolError, match="fade longer than the clip"):
        d.set_fades(1, fade_in=500)


def test_set_fades_video(project):
    _two_items(project)
    assert d.set_fades(1, fade_in=24, track_type="video")["fades"]["FadeIn"] == 24.0


def test_set_speed(project):
    a, _ = _two_items(project)
    out = d.set_speed(1, 50, pitch_correction=True, ripple=True)
    assert out == {"item": "a.mov", "speed": {"Percentage": 50.0}, "duration": 200}
    assert a.speed_options == {"Percentage": 50, "RippleTimeline": True, "PitchCorrection": True}
    with pytest.raises(ToolError, match="0 or more"):
        d.set_speed(1, -10)


def test_set_speed_needs_21_1(project, monkeypatch):
    a, _ = _two_items(project)
    monkeypatch.delattr(type(a), "SetSpeed")
    with pytest.raises(ToolError, match="SetSpeed needs DaVinci Resolve 21.1"):
        d.set_speed(1, 50)


def test_convert_to_stereo_and_insert_audio(project, tmp_path):
    assert d.convert_to_stereo() == "timeline converted to stereo"
    f = tmp_path / "sfx.wav"
    f.write_bytes(b"")
    assert d.insert_audio(str(f), duration=48000) == "inserted sfx.wav at the playhead"
    assert project.inserted_audio == (str(f), 0, 48000)
    with pytest.raises(ToolError, match="file not found"):
        d.insert_audio(str(tmp_path / "missing.wav"))


def test_sync_audio_verifies_by_readback(project):
    from conftest import Clip

    project.pool.root.clips.append(Clip("sound.wav"))
    out = d.sync_audio(["a.mov", "sound.wav"], channel="mix", retain_embedded_audio=True)
    # Resolve returned False, but the clips are linked: the readback decides.
    assert out == {"resolve_reported": False, "synced_audio": {"a.mov": "sound.wav", "sound.wav": "sound.wav"}}
    clips, settings = project.pool.synced_with
    assert settings == {"mode": 10, "channel": -2, "embedded": True, "metadata": False}
    project.pool.can_sync = False
    with pytest.raises(ToolError, match="no clip was synced"):
        d.sync_audio(["a.mov", "sound.wav"], method="timecode")


def test_sync_audio_errors(project):
    with pytest.raises(ToolError, match="waveform or timecode"):
        d.sync_audio(["a.mov", "b.mov"], method="ear")
    with pytest.raises(ToolError, match="at least one video and one audio"):
        d.sync_audio(["a.mov"])
    with pytest.raises(ToolError, match="clips not in media pool: nope.wav"):
        d.sync_audio(["a.mov", "nope.wav"])
    with pytest.raises(ToolError, match="channel must be"):
        d.sync_audio(["a.mov", "b.mov"], channel="left")


def test_transcribe_audio(project):
    out = d.transcribe_audio(["a.mov"], speaker_detection=True)
    assert out == [{"clip": "a.mov", "transcribed": True, "preview": "hello and welcome..."}]


def test_create_subtitles_verifies_by_track_count(project):
    out = d.create_subtitles(language="english", preset="netflix", lines=2, chars_per_line=42, gap=2)
    # Resolve reported False, but a subtitle track appeared: that is what counts.
    assert out == {"resolve_reported": False, "subtitle_track": 1, "captions": 2}
    assert project.current.captioned_with == {"lang": 101, "preset": 202, "linebreak": 301, "cpl": 42, "gap": 2}
    project.current.has_dialogue = False
    with pytest.raises(ToolError, match="no subtitle track was created"):
        d.create_subtitles()


def test_create_subtitles_errors(project):
    for kwargs, msg in [({"language": "arabic"}, "unsupported caption language: arabic"), ({"preset": "bbc"}, "unknown preset"),
                        ({"lines": 3}, "1 or 2"), ({"chars_per_line": 80}, "1-60"), ({"gap": 11}, "0-10")]:
        with pytest.raises(ToolError, match=msg):
            d.create_subtitles(**kwargs)


# --- mask tracking ---


def test_magic_mask_needs_a_click(project):
    a, _ = _two_items(project)
    with pytest.raises(ToolError, match="the API cannot click the subject"):
        d.magic_mask(1)
    with pytest.raises(ToolError, match="no Magic Mask to regenerate"):
        d.magic_mask(1, regenerate=True)


def test_magic_mask_tracks_seeded_mask(project):
    a, _ = _two_items(project)
    a.mask_clicked = True
    assert d.magic_mask(1, "forward") == "tracked Magic Mask forward on 'a.mov'"
    assert a.magic_mask == "F"
    d.magic_mask(1)
    assert a.magic_mask == "BI"
    assert d.magic_mask(1, regenerate=True) == "regenerated Magic Mask on 'a.mov'"
    with pytest.raises(ToolError, match="direction must be"):
        d.magic_mask(1, "sideways")


def test_magic_mask_needs_newer_resolve(project, monkeypatch):
    a, _ = _two_items(project)
    monkeypatch.delattr(type(a), "CreateMagicMask")
    with pytest.raises(ToolError, match="CreateMagicMask needs DaVinci Resolve 18.5"):
        d.magic_mask(1)


def _tracked(fusion, name="Tracker1", n=1):
    trk = fusion.FindTool(name)
    path = fusion.AddTool("PolyPath", -1, -1, locked_ok=True)
    trk.inputs[f"TrackedCenter{n}"].source = path.output
    return trk


def test_link_mask_to_tracker(fusion):
    d.add_fusion_node(1, "Blur", connect_from="MediaIn1")
    d.add_fusion_node(1, "EllipseMask", name="Face")
    d.connect_fusion_nodes(1, "Blur1", "Face", input="EffectMask")
    d.add_fusion_node(1, "Tracker", connect_from="MediaIn1")
    _tracked(fusion)
    assert d.link_mask_to_tracker(1, "Face", "Tracker1") == {"node": "Face", "expression": "Tracker1.TrackedCenter1"}
    center = fusion.FindTool("Face").inputs["Center"]
    assert center.expression == "Tracker1.TrackedCenter1"
    out = d.link_mask_to_tracker(1, "Face", "Tracker1", offset=[0.05, -0.1])
    assert out["expression"] == "Point(Tracker1.TrackedCenter1.X + 0.05, Tracker1.TrackedCenter1.Y + -0.1)"
    assert d.link_mask_to_tracker(1, "Face", "Tracker1", unlink=True) == {"node": "Face", "expression": None}
    assert center.expression is None
    _assert_lock_rules(fusion)


def test_link_mask_second_tracker(fusion):
    d.add_fusion_node(1, "EllipseMask")
    d.add_fusion_node(1, "Tracker")
    _tracked(fusion, n=2)
    assert d.link_mask_to_tracker(1, "EllipseMask1", "Tracker1", tracker_index=2)["expression"] == "Tracker1.TrackedCenter2"


def test_link_mask_errors(fusion):
    d.add_fusion_node(1, "EllipseMask")
    d.add_fusion_node(1, "Tracker")
    d.add_fusion_node(1, "Blur")
    with pytest.raises(ToolError, match="run Track Forward on it in the Fusion page first"):
        d.link_mask_to_tracker(1, "EllipseMask1", "Tracker1")
    with pytest.raises(ToolError, match="Tracker1 has no TrackedCenter3; its point inputs are: PatternCenter1, TrackedCenter1, TrackedCenter2"):
        d.link_mask_to_tracker(1, "EllipseMask1", "Tracker1", tracker_index=3)
    with pytest.raises(ToolError, match="Blur1 is a Blur, not a Tracker"):
        d.link_mask_to_tracker(1, "EllipseMask1", "Blur1")
    with pytest.raises(ToolError, match="Blur1 has no Center point input"):
        d.link_mask_to_tracker(1, "Blur1", "Tracker1")
    _tracked(fusion)
    with pytest.raises(ToolError, match=r"offset needs \[dx, dy\]"):
        d.link_mask_to_tracker(1, "EllipseMask1", "Tracker1", offset=[0.1])


# --- page-gated calls ---


def test_export_lut_switches_to_color_and_back(project, tmp_path, resolve):
    _two_items(project)
    resolve.OpenPage("edit")
    d.export_lut(1, str(tmp_path / "g.cube"))
    assert resolve.pages_visited[-2:] == ["color", "edit"]
    assert resolve.page == "edit"


def test_delete_items_from_fairlight_page(project, resolve):
    _two_items(project)
    resolve.OpenPage("fairlight")
    assert d.delete_items([1]) == "deleted 1 item(s)"
    assert resolve.page == "fairlight"  # put back where the user was


# --- advanced render ---


def test_render_range_and_settings(project, tmp_path):
    d.append_clips(["a.mov", "b.mov"])
    d.render(str(tmp_path), mark_in=86424, mark_out=86471, width=3840, height=2160, frame_rate=25, quality="Best",
             audio=False, individual_clips=True, settings={"ExportAlpha": True, "AudioSampleRate": 48000}, start=False)
    assert project.render_settings == {
        "ExportAlpha": True, "AudioSampleRate": 48000, "TargetDir": str(tmp_path), "FormatWidth": 3840,
        "FormatHeight": 2160, "FrameRate": 25, "VideoQuality": "Best", "ExportAudio": False,
        "SelectAllFrames": False, "MarkIn": 86424, "MarkOut": 86471,
    }
    assert project.render_mode == 0
    assert project.rendering is False  # start=False only queues


def test_render_range_must_be_absolute(project, tmp_path):
    d.append_clips(["a.mov"])
    # Relative frames (as GetMarkInOut reports them) would be clamped silently by Resolve.
    with pytest.raises(ToolError, match="absolute frames with 86400 <= in <= out <= 86500"):
        d.render(str(tmp_path), mark_in=0, mark_out=24)
    with pytest.raises(ToolError, match="given together"):
        d.render(str(tmp_path), mark_in=86400)


def test_render_status_done_checks_file(project, tmp_path):
    d.render(str(tmp_path), file_name="final")
    project.jobs["job-1"] = {"JobStatus": "Concluso", "CompletionPercentage": 100}  # localized status string
    st = d.render_status("job-1")
    assert (st["done"], st["output"], st["output_exists"]) == (True, str(tmp_path / "final.mov"), False)
    (tmp_path / "final.mov").write_bytes(b"x")
    assert d.render_status("job-1")["output_exists"] is True
    project.jobs["job-1"] = {"JobStatus": "Failed", "CompletionPercentage": 100, "Error": "disk full"}
    assert d.render_status("job-1")["done"] is False
    with pytest.raises(ToolError, match="render job not found"):
        d.render_status("job-9")


def test_render_queue_start_delete(project, tmp_path):
    d.render(str(tmp_path), start=False)
    d.render(str(tmp_path), start=False)
    assert [j["JobId"] for j in d.render_queue()] == ["job-1", "job-2"]
    assert d.start_render(["job-2"]) == "rendering 1 job(s)"
    with pytest.raises(ToolError, match="stop_render first"):
        d.delete_render_jobs(["job-1"])
    d.stop_render()
    assert d.delete_render_jobs(["job-1"]) == "deleted 1 render job(s)"
    with pytest.raises(ToolError, match="unknown render job"):
        d.delete_render_jobs(["job-1"])
    assert d.delete_render_jobs() == "render queue cleared"
    with pytest.raises(ToolError, match="rendering did not start"):
        d.start_render()


def test_save_render_preset(project):
    assert d.save_render_preset("Client 4K") == "saved render preset 'Client 4K'"
    with pytest.raises(ToolError, match="name taken"):
        d.save_render_preset("Client 4K")


# --- media management ---


def test_browse_storage(resolve, tmp_path):
    (tmp_path / "Day1").mkdir()
    (tmp_path / "a.mov").write_bytes(b"")
    assert d.browse_storage() == {"volumes": ["/Volumes/RAID", "/Volumes/SSD"]}
    assert d.browse_storage(str(tmp_path)) == {"path": str(tmp_path), "folders": [str(tmp_path / "Day1")],
                                               "files": [str(tmp_path / "a.mov")]}
    with pytest.raises(ToolError, match="folder not found"):
        d.browse_storage(str(tmp_path / "nope"))


def test_bins_move_and_import(project, tmp_path):
    assert d.create_bin("Footage") == "created bin /Footage"
    assert d.create_bin("Day 1", parent="Footage") == "created bin Footage/Day 1"
    with pytest.raises(ToolError, match="bin already exists"):
        d.create_bin("Footage")
    with pytest.raises(ToolError, match="bin not found: Nope"):
        d.create_bin("x", parent="Nope")
    assert d.move_clips(["a.mov"], "Footage/Day 1") == "moved 1 clip(s) to Footage/Day 1"
    assert {c["folder"]: c["name"] for c in d.list_clips()}["Footage/Day 1/"] == "a.mov"
    f = tmp_path / "c.mov"
    f.write_bytes(b"")
    d.import_media([str(f)], bin="Footage")
    assert project.pool.imported_into == "Footage"
    assert project.pool.GetCurrentFolder() is project.pool.root  # restored


def test_delete_clips(project):
    assert d.delete_clips(["a.mov"]) == "deleted 1 clip(s) from the media pool"
    assert "a.mov" not in [c["name"] for c in d.list_clips()]
    with pytest.raises(ToolError, match="clips not in media pool"):
        d.delete_clips(["a.mov"])


def test_import_image_sequence(project, tmp_path):
    (tmp_path / "shot_0001.exr").write_bytes(b"")
    assert d.import_image_sequence(str(tmp_path / "shot_%04d.exr"), 1, 120) == "imported shot_[1-120]"
    with pytest.raises(ToolError, match="first frame not found"):
        d.import_image_sequence(str(tmp_path / "shot_%04d.exr"), 5, 120)
    with pytest.raises(ToolError, match="end must be"):
        d.import_image_sequence(str(tmp_path / "shot_%04d.exr"), 10, 1)


def test_clip_info_and_tagging(project):
    out = d.tag_clips(["a.mov"], color="Teal", flag="Green", metadata={"Scene": "12", "Take": 3})
    assert out == [{"clip": "a.mov", "color": "Teal", "flags": ["Green"]}]
    info = d.clip_info("a.mov")
    assert info["metadata"] == {"Scene": "12", "Take": "3"}
    assert (info["color"], info["flags"]) == ("Teal", ["Green"])
    assert info["properties"]["File Path"] == "/media/a.mov"
    assert "Reel Name" not in info["properties"]  # empty values are dropped
    d.tag_clips(["a.mov"], color="", clear_flags=True)
    assert (d.clip_info("a.mov")["color"], d.clip_info("a.mov")["flags"]) == (None, [])


def test_tag_clips_errors(project):
    with pytest.raises(ToolError, match="unknown clip color: Red"):
        d.tag_clips(["a.mov"], color="Red")
    with pytest.raises(ToolError, match="unknown flag color"):
        d.tag_clips(["a.mov"], flag="Beige")
    with pytest.raises(ToolError, match="nothing to change"):
        d.tag_clips(["a.mov"])
    # Resolve returns True for Reel Name but does not keep it under automatic reel naming.
    with pytest.raises(ToolError, match="did not keep Reel Name on a.mov"):
        d.tag_clips(["a.mov"], metadata={"Reel Name": "A001"})


def test_relink_proxy_replace(project, tmp_path):
    (tmp_path / "proxy.mov").write_bytes(b"")
    (tmp_path / "v2.mov").write_bytes(b"")
    assert d.relink_clips(["a.mov"], str(tmp_path)) == f"relinked 1 clip(s) to {tmp_path}"
    assert d.clip_info("a.mov")["properties"]["File Path"] == str(tmp_path / "a.mov")
    assert d.link_proxy("a.mov", str(tmp_path / "proxy.mov")) == "linked proxy proxy.mov to a.mov"
    assert d.clip_info("a.mov")["properties"]["Proxy Media Path"] == str(tmp_path / "proxy.mov")
    assert d.link_proxy("a.mov") == "unlinked proxy of a.mov"
    with pytest.raises(ToolError, match="no proxy to unlink"):
        d.link_proxy("a.mov")
    assert d.replace_clip("a.mov", str(tmp_path / "v2.mov")) == "a.mov now uses v2.mov"
    with pytest.raises(ToolError, match="file not found"):
        d.replace_clip("a.mov", str(tmp_path / "v3.mov"))
    with pytest.raises(ToolError, match="folder not found"):
        d.relink_clips(["a.mov"], str(tmp_path / "missing"))


def test_export_metadata(project, tmp_path):
    out = tmp_path / "meta.csv"
    assert d.export_metadata(str(out)) == f"metadata of 2 clip(s) written to {out}"  # never an empty list
    assert out.read_text().split("\n") == ["a.mov", "b.mov"]
    d.export_metadata(str(out), ["b.mov"])
    assert out.read_text() == "b.mov"


# --- interchange and project ---


def test_export_timeline_formats(project, tmp_path, resolve):
    out = d.export_timeline(str(tmp_path / "cut.xml"), "fcpxml")
    assert (out["format"], out["timeline"]) == ("fcpxml_1_10", "Main")  # newest this build exposes
    assert project.current.exported[1:] == (resolve.EXPORT_FCPXML_1_10, resolve.EXPORT_NONE)
    d.export_timeline(str(tmp_path / "cut.aaf"), "aaf")
    assert project.current.exported[1:] == (resolve.EXPORT_AAF, resolve.EXPORT_AAF_NEW)
    d.export_timeline(str(tmp_path / "cut.edl"), "edl_cdl")
    assert project.current.exported[1:] == (resolve.EXPORT_EDL, resolve.EXPORT_CDL)
    assert d.export_timeline(str(tmp_path / "cut.otio"), "otio")["bytes"] > 0


def test_export_timeline_errors(project, tmp_path):
    with pytest.raises(ToolError, match="unknown format: premiere"):
        d.export_timeline(str(tmp_path / "x"), "premiere")
    with pytest.raises(ToolError, match="this Resolve version has no EXPORT_HDR_10_PROFILE_A"):
        d.export_timeline(str(tmp_path / "x"), "hdr10_a")
    with pytest.raises(ToolError, match="folder not found"):
        d.export_timeline(str(tmp_path / "no" / "x.aaf"), "aaf")


def test_export_timeline_detects_missing_file(project, tmp_path, monkeypatch):
    monkeypatch.setattr(project.current, "Export", lambda path, kind, sub: True)
    with pytest.raises(ToolError, match="reported success but wrote no file"):
        d.export_timeline(str(tmp_path / "cut.aaf"), "aaf")


def test_import_timeline(project, tmp_path):
    aaf = tmp_path / "from_avid.aaf"
    aaf.write_text("")
    out = d.import_timeline(str(aaf), name="Avid Cut", source_clips_path=str(tmp_path))
    assert out == {"timeline": "Avid Cut", "renamed_by_file": False}
    assert project.current.name == "Avid Cut"
    assert project.pool.imported_options == {"importSourceClips": True, "timelineName": "Avid Cut",
                                             "sourceClipsPath": str(tmp_path)}
    with pytest.raises(ToolError, match="already exists"):
        d.import_timeline(str(aaf), name="Avid Cut")


def test_import_timeline_fcp7_name_from_file(project, tmp_path):
    xml = tmp_path / "premiere.xml"
    xml.write_text("")
    assert d.import_timeline(str(xml), name="From Premiere") == {"timeline": "Sequence 1", "renamed_by_file": True}
    # Importing again: FCP7 XML hands back the EXISTING timeline instead of failing.
    with pytest.raises(ToolError, match="matches the existing timeline 'Sequence 1'"):
        d.import_timeline(str(xml))


def test_project_save_export_import(project, resolve, tmp_path):
    assert d.save_project() == "project saved"
    out = d.export_project(str(tmp_path / "backup"))
    assert out == {"project": "Demo", "path": str(tmp_path / "backup.drp"), "bytes": 3}
    assert resolve.pm.exported_project == ("Demo", str(tmp_path / "backup.drp"), True)
    assert d.import_project(out["path"], name="Demo copy") == "imported project Demo copy"
    assert "Demo copy" in d.list_projects()
    with pytest.raises(ToolError, match="name already in use"):
        d.import_project(out["path"], name="Demo")


# --- color management and HDR ---


def test_color_management_info(project):
    info = d.color_management_info()
    assert info == {"scope": "project", "color": {"colorScienceMode": "davinciYRGB"},
                    "hdr": {"hdrMasteringOn": "0", "hdrDolbyControlsOn": "0"}}
    tl_info = d.color_management_info(timeline=True)
    assert (tl_info["scope"], tl_info["uses_own_settings"]) == ("timeline 'Main'", False)
    assert tl_info["color"] == info["color"]  # falls back to the project


def test_set_color_management_orders_keys(project):
    # Given in the "wrong" order: spaces before the color science that unlocks them.
    out = d.set_color_management({"colorSpaceOutput": "Rec.2100 ST2084", "colorSpaceTimeline": "DaVinci WG/Intermediate",
                                  "isAutoColorManage": False, "colorScienceMode": "davinciYRGBColorManagedv2",
                                  "graphicsWhiteLevel": 203})
    assert out["applied"] == {"colorScienceMode": "davinciYRGBColorManagedv2", "isAutoColorManage": "0",
                              "colorSpaceTimeline": "DaVinci WG/Intermediate", "colorSpaceOutput": "Rec.2100 ST2084",
                              "graphicsWhiteLevel": "203"}
    assert list(out["applied"])[0] == "colorScienceMode"


def test_set_color_management_reports_what_was_not_applied(project):
    d.apply_color_preset("rcm_sdr")
    # Automatic RCM locks the spaces: Resolve returns True but keeps the old value; the read-back catches it.
    with pytest.raises(ToolError, match="did not apply colorSpaceOutput: wanted 'Rec.709 Gamma 2.4', is ''"):
        d.set_color_management({"colorSpaceOutput": "Rec.709 Gamma 2.4"})
    d.apply_color_preset("rcm_custom")
    with pytest.raises(ToolError, match="did not apply colorSpaceOutput: wanted 'Rec 709'"):
        d.set_color_management({"colorSpaceOutput": "Rec 709"})
    with pytest.raises(ToolError, match="not a color/HDR setting: timelineFrameRate"):
        d.set_color_management({"timelineFrameRate": "25"})
    with pytest.raises(ToolError, match="no settings"):
        d.set_color_management({})


def test_timeline_scope_leaves_project_alone(project):
    d.apply_color_preset("aces_cct", timeline=True)
    assert project.current.settings["useCustomSettings"] == "1"
    assert project.current.settings["colorScienceMode"] == "acescct"
    assert project.color["colorScienceMode"] == "davinciYRGB"
    info = d.color_management_info(timeline=True)
    assert (info["uses_own_settings"], info["color"]["colorScienceMode"]) == (True, "acescct")


def test_apply_color_preset(project):
    assert d.apply_color_preset("rcm_hdr")["applied"] == {"colorScienceMode": "davinciYRGBColorManagedv2",
                                                          "isAutoColorManage": "1", "rcmPresetMode": "HDR"}
    with pytest.raises(ToolError, match="unknown preset"):
        d.apply_color_preset("filmic")


def test_set_hdr(project):
    out = d.set_hdr(mastering_nits=1000, dolby_vision="4.0", dolby_tuning="Balanced", hdr10_plus=True)
    assert out["applied"] == {"hdrMasteringOn": "1", "hdrMasteringLuminanceMax": "1000", "hdrDolbyControlsOn": "1",
                              "hdrDolbyVersion": "4.0", "hdrDolbyAnalysisTuning": "Balanced", "hdr10PlusControlsOn": "1"}
    assert d.set_hdr(mastering_nits=0, dolby_vision="off")["applied"] == {"hdrMasteringOn": "0", "hdrDolbyControlsOn": "0"}


def test_set_hdr_errors(project):
    for kwargs, msg in [({"dolby_vision": "5"}, "dolby_vision must be"), ({"dolby_tuning": "Max"}, "dolby_tuning must be"),
                        ({"mastering_nits": -1}, "0 or more"), ({}, "nothing to change")]:
        with pytest.raises(ToolError, match=msg):
            d.set_hdr(**kwargs)


def test_set_clip_color_space(project):
    out = d.set_clip_color_space(["a.mov"], color_space="ARRI LogC4", gamma="ARRI LogC4")
    assert out == [{"clip": "a.mov", "Input Color Space": "ARRI LogC4", "Input Gamma": "ARRI LogC4"}]
    with pytest.raises(ToolError, match="did not apply Input Color Space = 'LogC'"):
        d.set_clip_color_space(["a.mov"], color_space="LogC")
    with pytest.raises(ToolError, match="give color_space"):
        d.set_clip_color_space(["a.mov"])


def test_analyze_dolby_vision(project, resolve):
    a, b = _two_items(project)
    with pytest.raises(ToolError, match="Dolby Vision is off"):
        d.analyze_dolby_vision()
    d.set_hdr(dolby_vision="4.0")
    assert d.analyze_dolby_vision() == "Dolby Vision analysis started on the whole timeline"
    assert project.current.dolby_analyzed == (None, None)
    d.analyze_dolby_vision([1, 2], blend_shots=True)
    assert project.current.dolby_analyzed == ([a, b], resolve.DLB_BLEND_SHOTS)
    with pytest.raises(ToolError, match="needs the items"):
        d.analyze_dolby_vision(blend_shots=True)


def test_analyze_dolby_vision_uses_timeline_settings(project):
    _two_items(project)
    d.set_hdr(dolby_vision="4.0", timeline=True)  # on for this timeline only
    assert project.color["hdrDolbyControlsOn"] == "0"
    assert "whole timeline" in d.analyze_dolby_vision()


# --- projects and databases ---


def test_switching_projects_saves_first(resolve):
    resolve.pm.LoadProject("Demo")
    d.create_project("Next")
    assert resolve.pm.saves == 1  # Demo saved before CreateProject replaced it
    d.open_project("Demo")
    assert resolve.pm.saves == 2


def test_untitled_project_is_never_saved(resolve):
    from conftest import Project

    resolve.pm.current = resolve.pm._add(Project("Untitled Project"))
    with pytest.raises(ToolError, match="Untitled Project cannot be saved"):
        d.save_project()
    d.create_project("Named")  # switching away from Untitled skips the (impossible) save
    assert resolve.pm.saves == 0


def test_failed_save_blocks_switch(resolve, monkeypatch):
    resolve.pm.LoadProject("Demo")
    monkeypatch.setattr(resolve.pm, "SaveProject", lambda: False)
    with pytest.raises(ToolError, match="could not save the current project 'Demo'; nothing was switched"):
        d.create_project("Next")
    assert "Next" not in resolve.pm.projects


def test_project_browser_and_folders(resolve):
    assert d.project_browser("/Clients/Acme")["folder"] == "Acme"
    assert d.create_project_folder("2026") == "created project folder 2026"
    assert d.project_browser()["folders"] == ["2026"]
    root = d.project_browser("/")
    assert (root["folders"], root["projects"], root["database"]["DbName"]) == (["Clients"], ["Demo"], "Local Database")
    with pytest.raises(ToolError, match="project folder not found: Nope .*now at the root folder"):
        d.project_browser("Clients/Nope")
    assert d.project_browser()["folder"] == ""
    with pytest.raises(ToolError, match="already exists"):
        d.create_project_folder("Clients")


def test_rename_and_delete_project(resolve):
    resolve.pm.LoadProject("Demo")
    d.create_project("Old")
    assert d.rename_project("Archive") == "renamed project Old → Archive"
    with pytest.raises(ToolError, match="is the open project"):
        d.delete_project("Archive")
    d.open_project("Demo")
    # Archive was open a moment ago: the first DeleteProject fails on live Resolve, the retry succeeds.
    assert d.delete_project("Archive") == "deleted project Archive"
    with pytest.raises(ToolError, match="project not found"):
        d.delete_project("Archive")


def test_databases(resolve):
    resolve.pm.LoadProject("Demo")
    assert [db["DbName"] for db in d.list_databases()["databases"]] == ["Local Database", "Studio"]
    out = d.switch_database("Studio")
    assert out["database"]["DbType"] == "PostgreSQL"
    assert resolve.pm.saves == 1
    with pytest.raises(ToolError, match="database not found"):
        d.switch_database("Nope")


def test_cloud_projects(resolve, tmp_path):
    resolve.pm.LoadProject("Demo")
    out = d.create_cloud_project("Series S01", str(tmp_path), sync="proxy_and_original")
    assert out == "created and opened cloud project Series S01"
    assert resolve.pm.cloud_settings == {"cloud_name": "Series S01", "cloud_media": str(tmp_path), "cloud_sync": 502,
                                         "cloud_collab": True, "cloud_cam": False}
    assert d.load_cloud_project("Series S01", str(tmp_path)) == "opened cloud project Series S01"
    with pytest.raises(ToolError, match="not found or not shared"):
        d.load_cloud_project("Other", str(tmp_path))
    with pytest.raises(ToolError, match="sync must be"):
        d.create_cloud_project("X", str(tmp_path), sync="all")
    with pytest.raises(ToolError, match="media folder not found"):
        d.create_cloud_project("X", str(tmp_path / "missing"))


def test_refresh_collaboration(project):
    from conftest import Folder

    stuck = Folder("Locked")
    stuck.stays_stale = True
    project.pool.root.subfolders.append(stuck)
    project.pool.root.stale = True
    assert d.refresh_collaboration() == {"refreshed": True, "stale_bins": ["Locked"]}


# --- timelines ---


def test_duplicate_timeline_keeps_current(project):
    out = d.duplicate_timeline("Main v2")
    assert out == {"copy": "Main v2", "of": "Main", "current": "Main"}
    assert project.current.name == "Main"  # DuplicateTimeline moved it; the tool moved it back
    with pytest.raises(ToolError, match="already exists"):
        d.duplicate_timeline("Main v2")
    assert d.duplicate_timeline("Old copy", timeline="Main v2")["of"] == "Main v2"


def test_rename_and_delete_timelines(project):
    d.duplicate_timeline("Scratch")
    assert d.rename_timeline("Scratch 1", timeline="Scratch") == "renamed timeline Scratch → Scratch 1"
    assert d.delete_timelines(["Scratch 1"]) == "deleted 1 timeline(s)"
    assert [t["name"] for t in d.list_timelines()] == ["Main"]
    with pytest.raises(ToolError, match="refusing to delete every timeline"):
        d.delete_timelines(["Main"])
    with pytest.raises(ToolError, match="timeline not found"):
        d.rename_timeline("X", timeline="Nope")


# --- review notes ---


def test_review_notes_workflow(project, tmp_path):
    note = d.add_review_note(48, "Logo too small", author="Sara")
    assert note == {"frame": 48, "timecode": "01:00:02:00", "color": "Red", "name": "Sara: Logo too small",
                    "note": "Logo too small", "duration": 1, "author": "Sara", "status": "open"}
    d.add_marker(12, note="plain marker")
    assert [n["frame"] for n in d.review_notes()] == [12, 48]
    assert [n["frame"] for n in d.review_notes(status="open")] == [48]

    resolved = d.resolve_review_note(48)
    assert (resolved["status"], resolved["color"], resolved["author"]) == ("resolved", "Green", "Sara")
    reopened = d.resolve_review_note(48, reopen=True)
    assert (reopened["status"], reopened["color"]) == ("open", "Red")

    out = d.export_review_notes(str(tmp_path / "notes.csv"))
    lines = (tmp_path / "notes.csv").read_text().splitlines()
    assert out["notes"] == 2
    assert lines[0] == "timecode,frame,status,author,color,note"
    assert lines[2] == "01:00:02:00,48,open,Sara,Red,Logo too small"

    d.export_review_notes(str(tmp_path / "notes.md"), status="open")
    md = (tmp_path / "notes.md").read_text()
    assert "| 01:00:02:00 | open | Sara | Logo too small |" in md and "plain marker" not in md


def test_review_note_errors(project, tmp_path):
    with pytest.raises(ToolError, match="unknown marker color"):
        d.add_review_note(10, "x", color="Orange")
    d.add_review_note(10, "x")
    with pytest.raises(ToolError, match="marker is already there"):
        d.add_review_note(10, "y")
    with pytest.raises(ToolError, match="no marker at frame 11"):
        d.resolve_review_note(11)
    with pytest.raises(ToolError, match=r"\.csv or \.md"):
        d.export_review_notes(str(tmp_path / "notes.txt"))


def test_resolve_note_restores_marker_on_failure(project, monkeypatch):
    d.add_review_note(10, "keep me", author="Ali")
    real_add = project.current.AddMarker
    calls = []

    def flaky_add(*args):
        calls.append(args)
        return False if len(calls) == 1 else real_add(*args)

    monkeypatch.setattr(project.current, "AddMarker", flaky_add)
    with pytest.raises(ToolError, match="the original was put back"):
        d.resolve_review_note(10)
    assert project.current.markers[10]["note"] == "keep me"
    assert json.loads(project.current.markers[10]["customData"])["status"] == "open"


def test_delete_markers(project):
    d.add_marker(1, color="Red")
    d.add_marker(2, color="Red")
    d.add_marker(3, color="Blue")
    assert d.delete_markers(color="Red") == "deleted 2 marker(s)"
    assert d.delete_markers(frame=3) == "deleted marker at frame 3"
    with pytest.raises(ToolError, match="give frame or color"):
        d.delete_markers()
    with pytest.raises(ToolError, match="no marker at frame 3"):
        d.delete_markers(frame=3)


def test_notes_timecode_on_drop_frame(project):
    project.current.settings["timelineDropFrameTimecode"] = "1"
    d.add_review_note(10, "df")
    assert d.review_notes()[0]["timecode"] is None  # frames stay exact; no guessed drop-frame timecode


# --- transcripts, subtitle files, titles ---

TRANSCRIPT = {
    "language": "en",
    "segments": [
        {"start": "01:00:00:12", "end": "01:00:02:00", "text": "Welcome to the show", "speaker": "Sara",
         "words": [{"start": "01:00:00:12", "end": "01:00:01:00", "text": "Welcome"}]},
        {"start": "01:00:02:00", "end": "01:00:03:00", "text": "(...)", "speaker": None, "words": []},
        {"start": "01:00:03:00", "end": "01:00:05:12", "text": "Today we talk about color", "speaker": None, "words": []},
    ],
}


@pytest.fixture
def transcribed(project):
    """a.mov carries a Resolve 21.1 transcript."""
    clip = next(c for c in project.pool.root.clips if c.name == "a.mov")
    clip.GetTranscription = lambda nested=False: TRANSCRIPT
    return clip


def test_get_transcript_full(transcribed):
    t = d.get_transcript("a.mov")
    assert (t["language"], t["complete"], len(t["segments"])) == ("en", True, 3)
    first = t["segments"][0]
    assert (first["start_seconds"], first["end_seconds"], first["speaker"]) == (0.5, 2.0, "Sara")  # from clip start
    assert "words" not in first
    assert d.get_transcript("a.mov", words=True)["segments"][0]["words"][0]["text"] == "Welcome"
    found = d.get_transcript("a.mov", query="COLOR")["segments"]
    assert [s_["start"] for s_ in found] == ["01:00:03:00"]


def test_get_transcript_preview_before_21_1(project):
    clip = next(c for c in project.pool.root.clips if c.name == "a.mov")
    clip.props["Transcription"] = "Welcome to the show today we…"
    t = d.get_transcript("a.mov")
    assert (t["complete"], t["preview"], t["segments"]) == (False, "Welcome to the show today we…", [])
    with pytest.raises(ToolError, match="exporting needs Resolve 21.1"):
        d.export_transcript("a.mov", "/tmp/x.srt")


def test_get_transcript_missing(project):
    with pytest.raises(ToolError, match="has no transcription: run transcribe_audio"):
        d.get_transcript("a.mov")


def test_export_transcript_srt_vtt_txt(transcribed, tmp_path):
    out = d.export_transcript("a.mov", str(tmp_path / "a.srt"))
    assert (out["segments"], out["language"]) == (2, "en")  # the silence is skipped
    assert (tmp_path / "a.srt").read_text(encoding="utf-8") == (
        "1\n00:00:00,500 --> 00:00:02,000\nSara: Welcome to the show\n\n"
        "2\n00:00:03,000 --> 00:00:05,500\nToday we talk about color\n"
    )
    d.export_transcript("a.mov", str(tmp_path / "a.vtt"), speakers=False)
    vtt = (tmp_path / "a.vtt").read_text(encoding="utf-8")
    assert vtt.startswith("WEBVTT\n\n00:00:00.500 --> 00:00:02.000\nWelcome to the show")
    d.export_transcript("a.mov", str(tmp_path / "a.txt"))
    assert (tmp_path / "a.txt").read_text(encoding="utf-8").splitlines()[0] == "[00:00:00] Sara: Welcome to the show"
    d.export_transcript("a.mov", str(tmp_path / "a.json"))
    assert json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))["language"] == "en"
    with pytest.raises(ToolError, match="must end in"):
        d.export_transcript("a.mov", str(tmp_path / "a.doc"))


def test_write_subtitles_arabic_rtl(project, tmp_path):
    captions = [{"start": 0.5, "end": 2, "text": "مرحبًا بكم في البرنامج!"},
                {"start": "00:00:03,000", "end": "00:00:05,500", "text": "اليوم نتحدث عن الألوان."}]
    out = d.write_subtitles(str(tmp_path / "ar.srt"), captions, rtl=True)
    assert out["captions"] == 2
    text = (tmp_path / "ar.srt").read_text(encoding="utf-8")
    assert text == ("1\n00:00:00,500 --> 00:00:02,000\n\u200fمرحبًا بكم في البرنامج!\n\n"
                    "2\n00:00:03,000 --> 00:00:05,500\n\u200fاليوم نتحدث عن الألوان.\n")


def test_write_subtitles_timecodes_use_timeline_fps(project, tmp_path):
    d.write_subtitles(str(tmp_path / "tc.vtt"), [{"start": "00:00:01:12", "end": "00:00:02:00", "text": "Hi"}])
    assert "00:00:01.500 --> 00:00:02.000" in (tmp_path / "tc.vtt").read_text()  # 24 fps timeline


def test_write_subtitles_errors(project, tmp_path):
    path = str(tmp_path / "x.srt")
    for captions, msg in [([], "no captions"), ([{"start": 2, "end": 1, "text": "a"}], "ends before it starts"),
                          ([{"start": 0, "end": 1, "text": " "}], "has no text"),
                          ([{"start": 0, "end": 2, "text": "a"}, {"start": 1, "end": 3, "text": "b"}], "starts before caption 1 ends"),
                          ([{"start": "soon", "end": 1, "text": "a"}], "not a time")]:
        with pytest.raises(ToolError, match=msg):
            d.write_subtitles(path, captions)
    with pytest.raises(ToolError, match=".srt or .vtt"):
        d.write_subtitles(str(tmp_path / "x.ass"), [{"start": 0, "end": 1, "text": "a"}])


def test_list_and_set_titles(project):
    _two_items(project)
    d.insert_title("Text+", fusion=True, text="Hello")
    assert d.list_titles() == [{"track": 1, "item": 3, "name": "Text+", "start": 86550, "end": 86670,
                                "texts": {"Template": "Hello"}}]
    out = d.set_title_text(3, text="أهلًا وسهلًا", font="Noto Naskh Arabic", style="Bold", size=0.1, color=[1, 0.8, 0])
    assert out["set"] == {"StyledText": "أهلًا وسهلًا", "Font": "Noto Naskh Arabic", "Style": "Bold", "Size": 0.1,
                          "Red1": 1.0, "Green1": 0.8, "Blue1": 0.0}
    assert d.list_titles()[0]["texts"] == {"Template": "أهلًا وسهلًا"}
    comp = project.current.tracks[("video", 1)][2].comps[0]
    _assert_lock_rules(comp)


def test_set_title_text_errors(project):
    _two_items(project)
    with pytest.raises(ToolError, match="is not a Fusion title"):
        d.set_title_text(1, text="x")
    d.insert_title("Text+", fusion=True)
    d.add_fusion_node(3, "TextPlus", name="Subtitle")
    with pytest.raises(ToolError, match="several Text\\+ nodes: pass node= one of Template, Subtitle"):
        d.set_title_text(3, text="x")
    assert d.set_title_text(3, text="lower third", node="Subtitle")["node"] == "Subtitle"
    with pytest.raises(ToolError, match="color needs"):
        d.set_title_text(3, color=[2, 0, 0], node="Subtitle")
    with pytest.raises(ToolError, match="nothing to change"):
        d.set_title_text(3, node="Subtitle")


def test_render_subtitles_needs_21(project, tmp_path, resolve, monkeypatch):
    with pytest.raises(ToolError, match="needs Resolve 21"):
        d.render(str(tmp_path), subtitles="burn_in")
    monkeypatch.setattr(type(resolve), "GetVersionString", lambda self: "21.0.2")
    d.render(str(tmp_path), subtitles="separate_file")
    assert (project.render_settings["ExportSubtitle"], project.render_settings["SubtitleFormat"]) == (True, "SeparateFile")
    with pytest.raises(ToolError, match="subtitles must be"):
        d.render(str(tmp_path), subtitles="srt")


# --- keyframes ---


def test_animate_clip(project):
    a, _ = _two_items(project)
    a.comps.append(__import__("conftest").FuComp("Composition 1"))
    a.comps[0].attrs = {"COMPN_RenderStart": 1000.0, "COMPN_RenderEnd": 1099.0}  # comps number their own frames
    out = d.animate_clip(1, zoom={0: 1.0, 99: 1.2}, position={0: [0.4, 0.5], 99: [0.6, 0.5]}, rotation={50: 0, 99: 15})
    assert out == {"item": "a.mov", "node": "Motion",
                   "keyframes": {"zoom": [0, 99], "position": [0, 99], "rotation": [50, 99]}}
    motion = a.comps[0].FindTool("Motion")
    assert motion.inputs["Size"].keys == {1000: 1.0, 1099: 1.2}
    assert motion.inputs["Center"].keys == {1000: {1: 0.4, 2: 0.5}, 1099: {1: 0.6, 2: 0.5}}
    nodes = {n["name"]: n for n in d.fusion_nodes(1)}
    assert nodes["MediaOut1"]["inputs"] == {"Input": "Motion"}
    # Calling again adds keys to the same node and spline.
    d.animate_clip(1, zoom={50: 1.5})
    assert motion.inputs["Size"].keys == {1000: 1.0, 1050: 1.5, 1099: 1.2}
    assert sum(t.name.startswith("Motion") for t in a.comps[0].tools) == 1
    _assert_lock_rules(a.comps[0])


def test_animate_clip_errors(project):
    _two_items(project)
    with pytest.raises(ToolError, match="give zoom"):
        d.animate_clip(1)
    with pytest.raises(ToolError, match=r"zoom keyframes outside the clip \(0-99\): \[100\]"):
        d.animate_clip(1, zoom={0: 1, 100: 2})
    with pytest.raises(ToolError, match="zoom must be positive"):
        d.animate_clip(1, zoom={0: 0})


def test_list_and_clear_keyframes(project):
    _two_items(project)
    d.animate_clip(1, zoom={0: 1.0, 48: 1.3})
    assert d.list_keyframes(1) == [{"node": "Motion", "input": "Size",
                                    "keyframes": [{"frame": 0, "value": 1.0}, {"frame": 48, "value": 1.3}]}]
    assert d.clear_keyframes(1, "Motion", "Size") == {"node": "Motion", "input": "Size", "value": 1.3}
    assert d.list_keyframes(1) == []
    _assert_lock_rules(project.current.tracks[("video", 1)][0].comps[0])  # the disconnect is structural: locked
    with pytest.raises(ToolError, match="is not animated"):
        d.clear_keyframes(1, "Motion", "Size")
    with pytest.raises(ToolError, match="has no input Zoom"):
        d.clear_keyframes(1, "Motion", "Zoom")


def test_set_color_keyframe_mode(project, resolve):
    assert d.set_color_keyframe_mode("sizing") == "color keyframe mode: sizing"
    assert resolve.keyframe_mode == (resolve.KEYFRAME_MODE_SIZING, "color")  # set on the Color page
    assert resolve.page == "edit"  # and back
    with pytest.raises(ToolError, match="mode must be"):
        d.set_color_keyframe_mode("position")


# --- multicam ---


def test_create_multicam(project):
    names = d.create_multicam(["a.mov", "b.mov"], name="Interview", sync="audio", audio_channel="mix",
                              angle_names="clip", split_at_gaps=True, create_bin=False, same_camera="reel_name")
    assert names == ["Interview"]
    clips, opts = project.pool.multicam
    assert [c.name for c in clips] == ["a.mov", "b.mov"]
    assert opts == {"angleSyncMode": 603, "multicamAudioMode": 611, "angleNameMode": 623, "channelConfig": -2,
                    "splitAtGaps": True, "createBinForSourceClips": False, "name": "Interview",
                    "detectSameCameraClipsMode": 634}
    assert "Interview" in [c["name"] for c in d.list_clips()]


def test_create_multicam_errors(project, monkeypatch):
    for kwargs, msg in [({"clips": ["a.mov"]}, "at least two"), ({"sync": "gps"}, "sync must be"),
                        ({"audio_channel": 2}, "only applies to sync=audio"),
                        ({"sync": "audio", "audio_channel": 9}, "1-8"),
                        ({"split_at_gaps": True}, "only applies to sync=audio")]:
        with pytest.raises(ToolError, match=msg):
            d.create_multicam(**{"clips": ["a.mov", "b.mov"], **kwargs})
    monkeypatch.delattr(type(project.pool), "CreateMulticamClip")
    with pytest.raises(ToolError, match="CreateMulticamClip needs DaVinci Resolve 21.1"):
        d.create_multicam(["a.mov", "b.mov"])


def test_auto_align_clips(project, audio):
    a, b = _two_items(project)
    assert d.auto_align_clips(video_items=[1, 2], audio_items=[1, 2], sync="waveform", waveform_track="mix") == \
        "aligned 4 item(s) by waveform"
    items, opts = project.current.aligned
    assert items == [a, b] + audio
    assert opts == {"SyncUsing": 641, "UseTrack": -2}
    d.auto_align_clips(video_items=[1, 2])
    assert project.current.aligned[1] == {"SyncUsing": 640}


def test_auto_align_errors(project, audio):
    _two_items(project)
    with pytest.raises(ToolError, match="waveform alignment needs the audio items"):
        d.auto_align_clips(video_items=[1, 2], sync="waveform")
    with pytest.raises(ToolError, match="at least two"):
        d.auto_align_clips(video_items=[1])
    with pytest.raises(ToolError, match="only applies to sync=waveform"):
        d.auto_align_clips(video_items=[1, 2], waveform_track=2)


def test_smart_switch_and_flatten(project):
    d.create_multicam(["a.mov", "b.mov"], name="Multicam Interview")
    d.append_clips(["Multicam Interview"])
    item = project.current.tracks[("video", 1)][0]
    assert d.smart_switch(1, wide_angle="Angle 1", wide_frequency="low", analysis="audio_only") == \
        "Smart Switch cut 'Multicam Interview'"
    assert item.smart_switch == {"minEditDuration": 1.0, "editChangeDelay": 0.3, "wideAngleFrequency": 650,
                                 "isUseWideAngleForIntroOutro": True, "isUseWideAngleForSilence": True,
                                 "switchOnVideoOnly": False, "quality": 661, "isAutoDetectWideAngle": False,
                                 "wideAngleID": "Angle 1", "analysisMode": 672}
    d.smart_switch(1, wide_angle=None)
    assert (item.smart_switch["isAutoDetectWideAngle"], item.smart_switch["wideAngleID"]) == (False, "None")
    assert d.flatten_multicam(1, grade="angle") == "flattened 'Multicam Interview'"
    assert item.flattened == 681


def test_smart_switch_errors(project):
    _two_items(project)
    with pytest.raises(ToolError, match="min_edit_seconds"):
        d.smart_switch(1, min_edit_seconds=0.1)
    with pytest.raises(ToolError, match="change_delay_seconds"):
        d.smart_switch(1, change_delay_seconds=3)
    with pytest.raises(ToolError, match="Smart Switch failed on 'a.mov'"):
        d.smart_switch(1)
    with pytest.raises(ToolError, match="is it a multicam clip"):
        d.flatten_multicam(1)
    with pytest.raises(ToolError, match="grade must be"):
        d.flatten_multicam(1, grade="none")


# --- sound effects and music ---

import math as _math
import random as _random
import wave as _wave


def click_track(path, bpm, seconds, offset=0.25, rate=48000, width=3, channels=2):
    """A drum-like click on every beat, over light noise."""
    rnd = _random.Random(1)
    period, out = 60 / bpm, bytearray()
    top = 2 ** (8 * width - 1) - 1
    for i in range(int(seconds * rate)):
        t = i / rate
        k = (t - offset) % period if t >= offset else 1e9
        v = (0.8 * _math.exp(-k * 60) * _math.sin(2 * _math.pi * 1500 * k) if k < 0.05 else 0.0) + 0.02 * rnd.uniform(-1, 1)
        out += int(max(-1, min(1, v)) * top).to_bytes(width, "little", signed=True) * channels
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(out))
    return path


def read_wav(path):
    with _wave.open(str(path), "rb") as w:
        ch, width, rate, raw = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    vals = [int.from_bytes(raw[i:i + width], "little", signed=True) / (2 ** (8 * width - 1) - 1)
            for i in range(0, len(raw), width * ch)]
    return vals, rate, ch, width


def dbfs(x):
    return 20 * _math.log10(x)


@pytest.mark.parametrize("bpm,width", [(120, 3), (95, 2), (140, 4)])
def test_detect_beats_click_tracks(tmp_path, bpm, width):
    path = click_track(tmp_path / f"c{bpm}.wav", bpm, 12, width=width)
    r = d.detect_beats(path=str(path))
    assert abs(r["bpm"] - bpm) < 0.1
    true = [0.25 + k * 60 / bpm for k in range(len(r["beats"]))]
    assert max(abs(a - b) for a, b in zip(r["beats"], true)) < 0.006  # within 6 ms on every beat
    assert len(r["hits"]) >= len(r["beats"]) - 2


def test_detect_beats_errors(tmp_path):
    with pytest.raises(ToolError, match="give clip or path"):
        d.detect_beats()
    (tmp_path / "x.mp3").write_bytes(b"ID3 not a wav")
    with pytest.raises(ToolError, match="not a PCM WAV file"):
        d.detect_beats(path=str(tmp_path / "x.mp3"))
    short = click_track(tmp_path / "short.wav", 120, 1)
    with pytest.raises(ToolError, match="too short"):
        d.detect_beats(path=str(short))
    with pytest.raises(ToolError, match="min_bpm"):
        d.detect_beats(path=str(short), min_bpm=200, max_bpm=100)


def test_generate_sound_tone_level(project, tmp_path):
    out = d.generate_sound("tone", str(tmp_path / "tone.wav"), seconds=0.5, level_db=-20)
    vals, rate, ch, width = read_wav(tmp_path / "tone.wav")
    assert (rate, ch, width, len(vals)) == (48000, 2, 3, 24000)
    assert abs(dbfs(max(vals)) - -20) < 0.05  # peak at -20 dBFS
    assert out["seconds"] == 0.5


def test_generate_sound_pop_and_beeps(project, tmp_path):
    d.generate_sound("pop", str(tmp_path / "pop.wav"))  # 24 fps timeline: one frame = 2000 samples
    assert len(read_wav(tmp_path / "pop.wav")[0]) == 2000
    d.generate_sound("beeps", str(tmp_path / "beeps.wav"), count=3, fps=25, channels=1)
    vals, _, ch, _ = read_wav(tmp_path / "beeps.wav")
    assert (ch, len(vals)) == (1, 3 * 48000)
    loud = [i for i, v in enumerate(vals) if abs(v) > 0.01]
    assert {i // 48000 for i in loud} == {0, 1, 2}  # one beep per second
    assert all(i % 48000 < 1920 for i in loud)  # each one frame at 25 fps


def test_generate_sound_noise_and_silence(project, tmp_path):
    d.generate_sound("noise", str(tmp_path / "n.wav"), seconds=0.5, level_db=-30, channels=1)
    vals = read_wav(tmp_path / "n.wav")[0]
    rms = _math.sqrt(sum(v * v for v in vals) / len(vals))
    assert abs(dbfs(rms) - -30) < 0.5
    d.generate_sound("silence", str(tmp_path / "s.wav"), seconds=0.1)
    assert set(read_wav(tmp_path / "s.wav")[0]) == {0.0}


def test_generate_sound_imports(project, tmp_path):
    out = d.generate_sound("tone", str(tmp_path / "bars_tone.wav"), seconds=0.1, bin="/")
    assert out["clip"] == "bars_tone.wav"
    assert "bars_tone.wav" in [c["name"] for c in d.list_clips()]


def test_generate_sound_errors(project, tmp_path):
    for kwargs, msg in [({"kind": "laser"}, "kind must be"), ({"kind": "tone", "level_db": 3}, "level_db"),
                        ({"kind": "tone", "path": str(tmp_path / "a.mp3")}, "must end in .wav"),
                        ({"kind": "beeps", "count": 0, "fps": 24}, "count")]:
        with pytest.raises(ToolError, match=msg):
            d.generate_sound(**{"path": str(tmp_path / "a.wav"), **kwargs})


def test_generate_voiceover(project):
    out = d.generate_voiceover("Welcome back.", voice="Male 1", speed=1.5, file_name="vo_intro.wav",
                               add_to_timeline=True, audio_track=2)
    assert out == {"clip": "vo_intro.wav", "voice": "Male 1", "added_to_timeline": True}
    assert project.speech == {"TextInput": "Welcome back.", "VoiceModel": "Male 1", "Speed": 1.5, "Pitch": 0.0,
                              "AddToTimeline": True, "AudioTrack": 2, "Filename": "vo_intro.wav"}


def test_generate_voiceover_missing_extras_is_an_error(project):
    project.speech_missing_extras = True
    with pytest.raises(ToolError, match="AI Speech Generator' is not Installed"):
        d.generate_voiceover("Hello")


def test_generate_voiceover_errors(project, tmp_path):
    with pytest.raises(ToolError, match="350"):
        d.generate_voiceover("x" * 351)
    with pytest.raises(ToolError, match="speed must be"):
        d.generate_voiceover("Hi", speed=20)
    with pytest.raises(ToolError, match="custom_voice_file"):
        d.generate_voiceover("Hi", voice="Custom Voice")


def test_classify_and_find_audio(project):
    from conftest import Clip, Folder

    sfx = Folder("SFX", [Clip("sfx_whoosh.wav"), Clip("music_bed.wav")])
    project.pool.root.subfolders.append(sfx)
    assert d.classify_audio(bin="SFX") == [
        {"clip": "sfx_whoosh.wav", "category": "Effects", "subcategory": "Whoosh"},
        {"clip": "music_bed.wav", "category": "Music", "subcategory": "Score"},
    ]
    assert [r["clip"] for r in d.find_audio(category="music")] == ["music_bed.wav"]
    assert [r["clip"] for r in d.find_audio(name="WHOOSH")] == ["sfx_whoosh.wav"]
    assert d.find_audio(category="Music", subcategory="Score")[0]["bin"] == "SFX/"
    assert d.classify_audio(clips=["a.mov"])[0]["category"] == "Dialogue"
    with pytest.raises(ToolError, match="give clips or bin"):
        d.classify_audio()


def test_find_audio_treats_uncategorized_as_none(project):
    clip = project.pool.root.clips[0]
    clip.props["Category"] = "Uncategorized"  # what ClearAudioClassification leaves
    assert d.find_audio(name="a.mov")[0]["category"] is None


def test_mark_beats(project, tmp_path):
    from conftest import Clip, Item

    music = Clip("song.wav", path=str(click_track(tmp_path / "song.wav", 120, 12)))
    project.pool.root.clips.append(music)
    tl = project.current
    # On the timeline from frame 24, trimmed to start 2.0 s into the song, 5 s long.
    item = Item("song.wav", tl.start + 24, tl.start + 24 + 120, media=music, timeline=tl)
    item.source_start = 2.0
    tl.tracks[("audio", 1)] = [item]
    out = d.mark_beats(1)
    assert (out["bpm"], out["markers"]) == (120.0, 10)  # beats at 2.25, 2.75 ... 6.75 s
    frames = sorted(tl.markers)
    assert frames[0] == 24 + 6  # (2.25 - 2.0) s * 24 fps after the clip start
    assert all(b - a == 12 for a, b in zip(frames, frames[1:]))  # half a second apart
    assert json.loads(tl.markers[frames[0]]["customData"])["bpm"] == 120.0

    tl.markers.clear()
    assert d.mark_beats(1, every=4)["markers"] == 3
    again = d.mark_beats(1, every=4)
    assert (again["markers"], again["skipped"]) == (0, 3)  # existing markers are kept, not duplicated


def test_mark_beats_errors(project):
    from conftest import Item

    project.current.tracks[("audio", 1)] = [Item("tone", 0, 10, media=None, timeline=project.current)]
    with pytest.raises(ToolError, match="has no source clip"):
        d.mark_beats(1)
    with pytest.raises(ToolError, match="every must be"):
        d.mark_beats(1, every=0)
    with pytest.raises(ToolError, match="unknown marker color"):
        d.mark_beats(1, color="Orange")


# --- transitions ---


def _three_clips(project, names=("a.mov", "b.mov", "a.mov")):
    d.append_clips(list(names))
    return project.current.tracks[("video", 1)]


def test_transition_all_cuts(project):
    a, b, c = _three_clips(project)
    out = d.transition_all_cuts(duration=12)
    assert out == {"cuts": 2, "added": 2, "skipped_existing": 0, "failed_no_handles": []}
    trs = d.list_transitions()
    assert [(t["index"], t["name"], t["duration"]) for t in trs] == [(2, "Cross Dissolve", 12), (4, "Cross Dissolve", 12)]
    assert trs[0]["between"] == ["a.mov", "b.mov"]
    # Running again finds the cuts covered and adds nothing.
    again = d.transition_all_cuts(duration=12)
    assert (again["added"], again["skipped_existing"]) == (0, 2)


def test_transition_all_cuts_reports_missing_handles(project):
    a, b, c = _three_clips(project)
    b.name = "b_nohandles"
    out = d.transition_all_cuts()
    assert (out["added"], out["failed_no_handles"]) == (1, ["b_nohandles | a.mov"])


def test_transition_all_cuts_nothing_possible(project):
    a, b, c = _three_clips(project)
    with pytest.raises(ToolError, match="no transition added"):
        d.transition_all_cuts(type="Page Curl")
    with pytest.raises(ToolError, match="alignment must be"):
        d.transition_all_cuts(alignment="top")


def test_remove_transitions(project, resolve):
    _three_clips(project)
    d.transition_all_cuts()
    resolve.OpenPage("fairlight")
    assert d.remove_transitions(items=[2]) == "removed 1 transition(s)"
    assert resolve.page == "fairlight"
    assert [t["index"] for t in d.list_transitions()] == [3]
    with pytest.raises(ToolError, match="item 1 on video track 1 is not a transition"):
        d.remove_transitions(items=[1])
    assert d.remove_transitions() == "removed 1 transition(s)"
    assert d.remove_transitions() == "no transitions to remove"
    assert [i["name"] for i in d.list_items()] == ["a.mov", "b.mov", "a.mov"]


# --- visual effects ---


def test_letterbox_timeline_and_pillarbox(project):
    out = d.letterbox(2.39)  # 1920x1080
    assert out["bounds"] == {"Top": 138, "Bottom": 941, "Left": 0, "Right": 1920}  # 803 px = 1920 / 2.39
    assert project.current.blanking == out["bounds"]
    assert d.letterbox(4 / 3)["bounds"] == {"Top": 0, "Bottom": 1080, "Left": 240, "Right": 1680}
    assert d.letterbox(None)["bounds"] == {"Top": 0, "Bottom": 1080, "Left": 0, "Right": 1920}
    with pytest.raises(ToolError, match="aspect must be"):
        d.letterbox(50)


def test_letterbox_clip_override(project):
    a, _ = _two_items(project)
    out = d.letterbox(2.0, item=1)
    assert out["bounds"] == {"Top": 60, "Bottom": 1020, "Left": 0, "Right": 1920}
    assert (a.use_timeline_blanking, a.blanking) == (False, out["bounds"])  # inheritance turned off first
    assert d.letterbox(None, item=1) == {"target": "'a.mov'", "blanking": "timeline's"}
    assert a.use_timeline_blanking is True


def test_letterbox_needs_21_1(project, monkeypatch):
    monkeypatch.delattr(type(project.current), "SetOutputBlanking")
    with pytest.raises(ToolError, match="SetOutputBlanking needs DaVinci Resolve 21.1"):
        d.letterbox()


def test_picture_in_picture(project):
    d.append_clips(["a.mov"], track=2, start_frame=0, end_frame=49)
    item = project.current.tracks[("video", 2)][0]
    item.SetProperty = lambda k, v: item.props.__setitem__(k, v) or True
    d.picture_in_picture(1, scale=0.25, corner="bottom_left", margin=0.05)
    # x: 1920 * 0.75 / 2 - 96 = 624 to the left; y: 1080 * 0.75 / 2 - 54 = 351 down
    assert item.props == {"Scaling": 2, "ZoomX": 0.25, "ZoomY": 0.25, "Pan": -624.0, "Tilt": -351.0}
    with pytest.raises(ToolError, match="corner must be"):
        d.picture_in_picture(1, corner="middle")


def test_picture_in_picture_16x9_clip_in_a_vertical_timeline(project):
    # measured on live 21.1: a 640x360 clip fitted into 1080x1920 is placed 1080x607.5, and Tilt moves it
    # 607.5/1920 px per unit, so the corner offset in Tilt units is the pixel offset divided by that
    tl = project.current
    tl.settings.update(timelineResolutionWidth="1080", timelineResolutionHeight="1920")
    d.append_clips(["a.mov"], track=2, start_frame=0, end_frame=49)
    item = tl.tracks[("video", 2)][0]
    item.media.props["Resolution"] = "640x360"
    item.SetProperty = lambda k, v: item.props.__setitem__(k, v) or True
    d.picture_in_picture(1, scale=0.3, corner="bottom_right")
    # 324 x 182 px; right edge 43 px in, bottom edge 77 px up: 792 px down = 2503 Tilt units
    assert item.props == {"Scaling": 2, "ZoomX": 0.3, "ZoomY": 0.3, "Pan": 334.8, "Tilt": -2503.3}


def _split_items(project):
    d.append_clips(["a.mov"], track=2, start_frame=0, end_frame=49)
    d.append_clips(["b.mov"], start_frame=0, end_frame=49)
    left = project.current.tracks[("video", 2)][0]
    right = project.current.tracks[("video", 1)][0]
    for it in (left, right):
        it.SetProperty = (lambda i: lambda k, v: i.props.__setitem__(k, v) or True)(it)
    return left, right


def test_split_screen(project):
    left, right = _split_items(project)
    d.split_screen(1, 1, gap=0.02)
    # each clip keeps the middle 940.8 px of its 1920 px picture, centered on its half; 38.4 px gap in the middle
    same = {"Scaling": 2, "ZoomX": 1.0, "ZoomY": 1.0, "CropLeft": 489.6, "CropRight": 489.6, "CropTop": 0.0,
            "CropBottom": 0.0, "Tilt": 0.0}
    assert left.props == {**same, "Pan": -489.6}
    assert right.props == {**same, "Pan": 489.6}


def test_split_screen_16x9_clips_in_a_vertical_timeline(project):
    # measured on live 21.1: fitted 1080x607.5, zoomed 3.16 to cover a 540x1920 half. A timeline on project settings
    # in a vertical project fills, so Crop counts pixels of the filled 3413x1920 picture: 3.16x the fitted ones
    project.current.settings.update(timelineResolutionWidth="1080", timelineResolutionHeight="1920")
    left, right = _split_items(project)
    for it in (left, right):
        it.media.props["Resolution"] = "640x360"
    d.split_screen(1, 1)
    same = {"Scaling": 2, "ZoomX": 3.1605, "ZoomY": 3.1605, "CropLeft": 1436.7, "CropRight": 1436.7, "CropTop": 0.0,
            "CropBottom": 0.0, "Tilt": 0.0}
    assert left.props == {**same, "Pan": -270.0}
    assert right.props == {**same, "Pan": 270.0}
    # with its own settings the timeline follows its mode (fit here), and Crop counts fitted pixels
    project.current.settings.update(useCustomSettings="1", timelineInputResMismatchBehavior="scaleToFit")
    d.split_screen(1, 1)
    assert (left.props["CropLeft"], right.props["CropRight"]) == (454.6, 454.6)


def test_vignette(project):
    a, _ = _two_items(project)
    out = d.vignette(1, amount=0.4, size=0.9, softness=0.5)
    assert out["set"] == {"VignetteMask.Width": 0.9, "VignetteMask.Height": 0.9, "VignetteMask.SoftEdge": 0.5,
                          "VignetteMask.Invert": 1, "Vignette.Gain": 0.6}
    nodes = {n["name"]: n for n in d.fusion_nodes(1)}
    assert nodes["Vignette"]["inputs"] == {"Input": "MediaIn1", "EffectMask": "VignetteMask"}
    assert nodes["MediaOut1"]["inputs"] == {"Input": "Vignette"}
    _assert_lock_rules(a.comps[0])
    with pytest.raises(ToolError, match="already has a Vignette"):
        d.vignette(1)


def test_camera_shake(project):
    a, _ = _two_items(project)
    out = d.camera_shake(1, amount=0.01, every=10, seed=7)
    assert (out["keyframes"], out["zoom"]) == (11, 1.02)  # frames 0,10..90 and the last frame 99
    keys = a.comps[0].FindTool("Motion").inputs["Center"].keys
    assert sorted(keys) == list(range(0, 100, 10)) + [99]
    assert all(abs(v[1] - 0.5) <= 0.01 and abs(v[2] - 0.5) <= 0.01 for v in keys.values())
    again = d.camera_shake(1, amount=0.01, every=10, seed=7)
    assert a.comps[0].FindTool("Motion").inputs["Center"].keys == keys  # same seed, same shake
    _assert_lock_rules(a.comps[0])


def test_camera_shake_keeps_animated_zoom(project):
    a, _ = _two_items(project)
    d.animate_clip(1, zoom={0: 1.0, 99: 1.2})
    out = d.camera_shake(1, amount=0.02)
    assert out["zoom"].startswith("left as animated")
    assert a.comps[0].FindTool("Motion").inputs["Size"].keys == {0: 1.0, 99: 1.2}


# --- advanced grading ---


def test_grade_writes_switch_to_color_page(project, resolve, tmp_path):
    a, b = _two_items(project)
    resolve.OpenPage("edit")
    d.set_cdl(1, slope=[1.1, 1, 1])
    d.copy_grade(1, [2])
    d.add_color_version(1, "Look B")
    d.load_color_version(1, "Version 1")
    drx = tmp_path / "look.drx"
    drx.write_text("")
    d.apply_drx(str(drx), [1])
    assert resolve.page == "edit"  # every write went to the Color page and came back
    assert resolve.pages_visited.count("color") == 5


def test_apply_lut_installs_outside_file(project, resolve, tmp_path, monkeypatch):
    a, _ = _two_items(project)
    master = tmp_path / "LUT"
    master.mkdir()
    monkeypatch.setenv("RESOLVE_LUT_DIR", str(master))
    lut = tmp_path / "downloads" / "Teal Orange.cube"
    lut.parent.mkdir()
    lut.write_text("LUT_3D_SIZE 2")
    out = d.apply_lut(1, str(lut), node=2)
    # An absolute path outside the master folder is refused by Resolve, so it is installed and applied from there.
    assert out == "LUT on node 2 of 'a.mov': davinci-mcp/Teal Orange.cube"
    assert (master / "davinci-mcp" / "Teal Orange.cube").read_text() == "LUT_3D_SIZE 2"
    assert project.luts_refreshed and a.graph.luts[2] == "davinci-mcp/Teal Orange.cube"


def test_apply_lut_install_failure_explained(project, tmp_path, monkeypatch):
    _two_items(project)
    blocker = tmp_path / "LUT"
    blocker.write_text("not a folder")  # makedirs fails
    monkeypatch.setenv("RESOLVE_LUT_DIR", str(blocker))
    lut = tmp_path / "x.cube"
    lut.write_text("")
    with pytest.raises(ToolError, match="copying there failed"):
        d.apply_lut(1, str(lut))
    with pytest.raises(ToolError, match="not a LUT Resolve knows"):
        d.apply_lut(1, "Missing/Look.txt")


def test_node_graph_targets(project):
    a, _ = _two_items(project)
    g = d.node_graph(item=1)
    assert g == {"graph": "'a.mov'", "nodes": [
        {"index": 1, "label": "Primary", "tools": ["Primaries", "Curves"], "lut": None, "cache": 0},
        {"index": 2, "label": "Look", "tools": ["LUT"], "lut": None, "cache": 0}]}
    assert d.node_graph(timeline_grade=True)["nodes"][0]["label"] == "Timeline"
    d.create_color_group("Interview")
    assert d.node_graph(group="Interview", stage="post")["nodes"][0]["label"] == "Group Post"
    with pytest.raises(ToolError, match="exactly one of"):
        d.node_graph(item=1, group="Interview")
    with pytest.raises(ToolError, match="exactly one of"):
        d.node_graph()
    with pytest.raises(ToolError, match="stage must be"):
        d.node_graph(group="Interview", stage="middle")


def test_group_and_timeline_grades(project, tmp_path):
    _two_items(project)
    d.create_color_group("Night")
    assert d.set_node_lut(1, "Film/Kodak.cube", group="Night") == "LUT on node 1 of group 'Night' pre-clip: Film/Kodak.cube"
    assert project.color_groups[0].pre.luts == {1: "Film/Kodak.cube"}
    drx = tmp_path / "night.drx"
    drx.write_text("")
    assert d.apply_drx_to(str(drx), group="Night", stage="post") == "applied night.drx to group 'Night' post-clip"
    assert d.apply_drx_to(str(drx), timeline_grade=True, keyframes="start_frames").endswith("timeline 'Main'")
    assert project.current.timeline_graph.drx == (str(drx), 2)
    with pytest.raises(ToolError, match="node 2 out of range"):
        d.set_node_lut(2, "Film/Kodak.cube", group="Night")


def test_set_node_enabled_and_reset(project):
    a, _ = _two_items(project)
    assert "bypassed (not readable back" in d.set_node_enabled(2, False, item=1)
    assert a.graph.disabled == {2}
    assert d.reset_grade(item=1) == "grade of 'a.mov' reset"
    assert a.graph.reset is True


def test_color_groups_workflow(project):
    a, b = _two_items(project)
    assert d.create_color_group("Camera A") == "created color group Camera A"
    with pytest.raises(ToolError, match="already exists"):
        d.create_color_group("Camera A")
    assert d.assign_color_group([1, 2], "Camera A") == {"group": "Camera A", "items": ["a.mov", "b.mov"]}
    assert d.color_groups() == [{"group": "Camera A", "clips": [{"track": 1, "item": 1, "name": "a.mov"},
                                                                 {"track": 1, "item": 2, "name": "b.mov"}]}]
    assert d.color_info(1)["color_group"] == "Camera A"
    d.assign_color_group([2], None)
    assert [c["name"] for c in d.color_groups()[0]["clips"]] == ["a.mov"]
    assert d.delete_color_group("Camera A") == "deleted color group Camera A"
    assert d.color_groups() == [] and a.color_group is None
    with pytest.raises(ToolError, match="color group not found"):
        d.assign_color_group([1], "Nope")


def test_arri_and_cache(project):
    a, b = _two_items(project)
    a.graph.arri = True
    assert d.apply_arri_cdl_lut([1]) == {"applied": ["a.mov"]}
    with pytest.raises(ToolError, match="ApplyArriCdlLut failed on b.mov"):
        d.apply_arri_cdl_lut([2])
    assert d.color_cache([1, 2]) == {"items": ["a.mov", "b.mov"], "color_cache": True}
    assert a.color_cache is True


def test_gallery_albums_and_import_stills(project, tmp_path):
    looks = [tmp_path / "warm.drx", tmp_path / "cold.drx"]
    for f in looks:
        f.write_text("")
    assert d.import_stills([str(f) for f in looks]) == {"album": "Stills 1", "imported": 2}
    assert d.import_stills([str(looks[0])], powergrade=True) == {"album": "PowerGrade 1", "imported": 1}
    assert d.gallery_albums() == {"still_albums": [{"name": "Stills 1", "stills": ["warm.drx", "cold.drx"]}],
                                  "powergrade_albums": [{"name": "PowerGrade 1", "stills": ["warm.drx"]}]}
    assert d.import_stills([str(looks[1])], album="PowerGrade 1")["imported"] == 1
    with pytest.raises(ToolError, match="album not found"):
        d.import_stills([str(looks[0])], album="Nope")
    with pytest.raises(ToolError, match="not found"):
        d.import_stills([str(tmp_path / "missing.drx")])


def test_validate_dctl(resolve):
    good = "__DEVICE__ float3 transform(int p_Width, int p_Height, int p_X, int p_Y, float p_R, float p_G, float p_B)\n{\n    return make_float3(p_R, p_G, p_B);\n}\n"
    assert d.validate_dctl(good) == {"valid": True, "diagnostic": None}
    one_line = good.replace("\n", " ")
    assert d.validate_dctl(one_line) == {"valid": False,
                                         "diagnostic": "DCTL Error: main DCTL function does not have return value."}
    assert d.validate_dctl("float x;")["diagnostic"] == "cannot find main DCTL function."


# --- fixes from the live check on Resolve Studio 21.1.0.14 ---


def test_append_adds_missing_track_and_verifies(project, monkeypatch):
    d.append_clips(["a.mov"])
    assert project.current.GetTrackCount("video") == 1
    d.append_clips(["b.mov"], start_frame=0, end_frame=9, track=3)
    assert project.current.GetTrackCount("video") == 3
    assert [i["name"] for i in d.list_items(track=3)] == ["b.mov"]
    monkeypatch.setattr(project.pool, "AppendToTimeline", lambda clips: True)  # success reported, nothing placed
    with pytest.raises(ToolError, match="nothing landed on video track 3"):
        d.append_clips(["a.mov"], start_frame=0, end_frame=9, track=3)


def test_keyframes_use_timed_setinput(project):
    a, _ = _two_items(project)
    out = d.dynamic_zoom(1, end_zoom=1.3)
    assert out["frames"] == [0, 99]
    keys = a.comps[0].FindTool("DynamicZoom").inputs["Size"].keys
    assert keys == {0: 1.0, 99: 1.3}


def test_comp_range_follows_item_length(project):
    a, _ = _two_items(project)
    d.insert_fusion_effect(1, "Blur")
    a.comps[0].attrs["COMPN_RenderEnd"] = 97.0  # live 21.1 reported a range short of the clip
    out = d.animate_clip(1, zoom={0: 1.0, 99: 1.2})
    assert out["keyframes"]["zoom"] == [0, 99]
    with pytest.raises(ToolError, match=r"outside the clip \(0-99\): \[100\]"):
        d.animate_clip(1, zoom={100: 1.0})


def test_view_frame_retries_from_edit_page(project, resolve, monkeypatch, tmp_path):
    _two_items(project)
    real = project.ExportCurrentFrameAsStill
    monkeypatch.setattr(project, "ExportCurrentFrameAsStill",
                        lambda path: resolve.page == "edit" and real(path))
    resolve.OpenPage("deliver")
    out = d.view_frame(save_to=str(tmp_path / "f.png"))
    assert (tmp_path / "f.png").exists() and resolve.page == "deliver"
    monkeypatch.setattr(project, "ExportCurrentFrameAsStill", lambda path: False)
    with pytest.raises(ToolError, match="also from the Edit page"):
        d.view_frame()


def test_set_speed_explains_titles(project):
    _two_items(project)
    tl = project.current
    title = tl.tracks[("video", 1)][0]
    title.media = None
    title.SetSpeed = lambda options: False
    with pytest.raises(ToolError, match="is a title, generator or transition"):
        d.set_speed(1, 50)


def test_add_transition_found_by_position_when_timing_is_odd(project, monkeypatch):
    a, b = _two_items(project)
    real = a.AddTransition

    def odd(options):
        out = real(options)
        tr = project.current.tracks[("video", 1)][1]
        tr.start, tr.end = tr.end, tr.start  # live 21.1: timing that does not bracket the cut
        return out

    monkeypatch.setattr(a, "AddTransition", odd)
    out = d.add_transition(1, duration=24)
    assert (out["name"], out["index"]) == ("Cross Dissolve", 2) and "timing not usable" in out["note"]



# --- AI editing ---


def test_super_scale(project):
    clip = next(c for c in project.pool.root.clips if c.name == "a.mov")
    assert d.super_scale(["a.mov"], "3x") == [{"clip": "a.mov", "super_scale": "3", "enhanced": False}]
    out = d.super_scale(["a.mov"], "2x", sharpness=0.4, noise_reduction=0.2)
    assert out[0]["enhanced"] and clip.super_scale_args == (2, 0.4, 0.2)
    for kwargs, msg in [({"scale": "8x"}, "scale must be"), ({"scale": "3x", "sharpness": 0.5, "noise_reduction": 0},
                                                              "use scale='2x'"),
                        ({"sharpness": 0.5}, "needs both"), ({"sharpness": 2, "noise_reduction": 0}, "between 0 and 1")]:
        with pytest.raises(ToolError, match=msg):
            d.super_scale(["a.mov"], **kwargs)


def test_ai_slow_motion(project):
    a, _ = _two_items(project)
    out = d.ai_slow_motion(1, 50)
    assert a.speed == {"Percentage": 50.0}
    assert (a.props["RetimeProcess"], a.props["MotionEstimation"]) == (3, 5)  # optical_flow, speed_warp
    assert (out["retime"], out["motion_estimation"]) == ("optical_flow", "speed_warp")
    with pytest.raises(ToolError, match="below 100"):
        d.ai_slow_motion(1, 150)
    with pytest.raises(ToolError, match="engine must be"):
        d.ai_slow_motion(1, 50, engine="magic")


def _talk_wav(path, pattern, rate=16000):
    """pattern: [(seconds, loud)], a 300 Hz tone where loud, digital silence elsewhere."""
    import wave as _wave
    frames = bytearray()
    for sec_, loud in pattern:
        for i in range(int(sec_ * rate)):
            v = int(12000 * math.sin(2 * math.pi * 300 * i / rate)) if loud else 0
            frames += v.to_bytes(2, "little", signed=True)
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


@pytest.fixture
def talk(project, tmp_path):
    """talk.wav in the pool: 1 s speech, 1 s pause, 1 s speech, 0.3 s pause, 1 s speech (4.3 s at 24 fps)."""
    path = tmp_path / "talk.wav"
    _talk_wav(path, [(1, True), (1, False), (1, True), (0.3, False), (1, True)])
    clip = Clip("talk.wav", frames=0, path=str(path))
    project.pool.root.clips.append(clip)
    return clip


def test_remove_silences(project, talk):
    out = d.remove_silences("talk.wav", min_silence=0.5, padding=0.1)
    # only the 1 s pause is long enough; 0.1 s of it stays on each side
    assert (out["parts"], out["pauses_found"]) == (2, 1)
    assert out["kept"] == [[0.0, 1.12], [1.88, 4.29]]  # frames 27 and 45..103 of 103
    assert out["timeline"] == "talk.wav - no silences" and project.current.name == out["timeline"]
    (infos,) = project.pool.appended
    assert [(i["startFrame"], i["endFrame"]) for i in infos] == [(0, 27), (45, 103)]
    with pytest.raises(ToolError, match="may be taken"):
        d.remove_silences("talk.wav")  # same default name again
    with pytest.raises(ToolError, match="no pause"):
        d.remove_silences("talk.wav", min_silence=2, timeline="x")
    with pytest.raises(ToolError, match="padding must be"):
        d.remove_silences("talk.wav", padding=0.3)


def test_remove_silences_needs_wav_or_ffmpeg(project, monkeypatch, tmp_path):
    clip = next(c for c in project.pool.root.clips if c.name == "a.mov")
    clip.path = str(tmp_path / "a.mov")
    (tmp_path / "a.mov").write_bytes(b"\0")
    monkeypatch.setattr(d.shutil, "which", lambda name: None)
    with pytest.raises(ToolError, match="ffmpeg is not installed"):
        d.remove_silences("a.mov")


def test_remove_silences_through_ffmpeg(project, monkeypatch, tmp_path):
    clip = next(c for c in project.pool.root.clips if c.name == "a.mov")
    clip.path = str(tmp_path / "a.mov")
    (tmp_path / "a.mov").write_bytes(b"\0")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        _talk_wav(cmd[-1], [(1, True), (1, False), (1, True)])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(d.subprocess, "run", fake_run)
    out = d.remove_silences("a.mov")
    assert calls[0][:4] == ["/usr/bin/ffmpeg", "-v", "error", "-y"] and out["parts"] == 2
    assert not os.path.exists(calls[0][-1])  # the temporary WAV is removed


TALK = {
    "language": "en",
    "segments": [
        {"start": "01:00:00:00", "end": "01:00:02:00", "text": "So um welcome", "speaker": None,
         "words": [{"start": "01:00:00:00", "end": "01:00:00:06", "text": "So"},
                   {"start": "01:00:00:12", "end": "01:00:00:18", "text": "um,"},
                   {"start": "01:00:01:00", "end": "01:00:02:00", "text": "welcome"}]},
        {"start": "01:00:02:00", "end": "01:00:02:12", "text": "(...)", "speaker": None, "words": []},
        {"start": "01:00:02:12", "end": "01:00:03:00", "text": "Wrong take sorry", "speaker": None, "words": []},
        {"start": "01:00:03:00", "end": "01:00:04:00", "text": "Today we grade", "speaker": None,
         "words": [{"start": "01:00:03:00", "end": "01:00:03:12", "text": "Today"},
                   {"start": "01:00:03:12", "end": "01:00:04:00", "text": "we grade"}]},
    ],
}


def test_cut_by_transcript(project):
    clip = next(c for c in project.pool.root.clips if c.name == "a.mov")
    clip.GetTranscription = lambda nested=False: TALK
    out = d.cut_by_transcript("a.mov", remove_phrases=["wrong take"], padding=0.1)
    assert out["removed"] == {"fillers": 1, "segments": 1}
    (infos,) = project.pool.appended
    # 24 fps, padding 2.4 frames: "So" 0-6 -> 0-9; the filler (12-18) is cut; "welcome" 24-48 -> 21-51, padded
    # at most halfway back to the filler; the pause and the retake (48-72) are cut; "Today we grade" 72-96 -> 72-99,
    # not padded into the retake before it.
    assert [(i["startFrame"], i["endFrame"]) for i in infos] == [(0, 9), (21, 51), (72, 99)]
    assert out["timeline"] == "a.mov - edited"


def test_cut_by_transcript_keep_only_and_errors(project):
    clip = next(c for c in project.pool.root.clips if c.name == "a.mov")
    clip.GetTranscription = lambda nested=False: TALK
    out = d.cut_by_transcript("a.mov", keep_only=["today"], timeline="today only")
    assert out["parts"] == 1 and out["removed"]["segments"] == 2
    with pytest.raises(ToolError, match="nothing left"):
        d.cut_by_transcript("a.mov", keep_only=["absent"], timeline="none")
    clip.GetTranscription = None
    clip.props["Transcription"] = "So um welcome…"
    with pytest.raises(ToolError, match="needs Resolve 21.1"):
        d.cut_by_transcript("a.mov", timeline="old")


def test_cut_by_transcript_max_gap(project):
    clip = next(c for c in project.pool.root.clips if c.name == "a.mov")
    clip.GetTranscription = lambda nested=False: TALK
    # fillers kept; the 0.25 s gaps between "So", "um" and "welcome" exceed max_gap 0.2, so each word is a part
    out = d.cut_by_transcript("a.mov", remove_fillers=False, remove_phrases=["wrong take"], max_gap=0.2, padding=0)
    assert out["kept"][:3] == [[0.0, 0.25], [0.5, 0.75], [1.0, 2.0]]
    joined = d.cut_by_transcript("a.mov", remove_fillers=False, remove_phrases=["wrong take"], max_gap=0.3,
                                 padding=0, timeline="joined")
    assert joined["kept"][0] == [0.0, 2.0]



# --- social media delivery ---


def test_social_platforms():
    rows = {r["platform"]: r for r in d.social_platforms()}
    assert rows["tiktok"] == {"platform": "tiktok", "name": "TikTok", "resolution": [1080, 1920], "aspect": "9:16",
                              "bitrate_kbps": 12000}
    assert rows["instagram_feed"]["aspect"] == "4:5" and rows["youtube"]["aspect"] == "16:9"


def test_social_timeline_vertical_copy(project):
    _two_items(project)
    src = project.current
    out = d.social_timeline("tiktok", reframe=True)
    assert out == {"timeline": "Main - TikTok", "platform": "TikTok", "resolution": [1080, 1920], "fit": "fill",
                   "warnings": [], "reframed": 2}
    copy = project.current
    assert copy.name == "Main - TikTok" and copy is not src
    assert (copy.settings["timelineResolutionWidth"], copy.settings["timelineResolutionHeight"]) == ("1080", "1920")
    assert copy.settings["timelineInputResMismatchBehavior"] == "scaleToFill"
    assert all(i.reframed for i in copy.tracks[("video", 1)])
    assert src.settings["timelineResolutionWidth"] == "1920"  # the edit itself is untouched
    assert not any(i.reframed for i in src.tracks[("video", 1)])
    with pytest.raises(ToolError, match="already exists"):
        d.social_timeline("tiktok", timeline="Main")
    with pytest.raises(ToolError, match="unknown platform"):
        d.social_timeline("myspace")
    with pytest.raises(ToolError, match="fit must be"):
        d.social_timeline("youtube", fit="zoom")


def test_social_timeline_refused_resolution_and_fit(project, monkeypatch):
    _two_items(project)
    Timeline = type(project.current)
    real = Timeline.SetSetting
    monkeypatch.setattr(Timeline, "SetSetting",
                        lambda self, k, v: False if k == "timelineInputResMismatchBehavior" else real(self, k, v))
    out = d.social_timeline("instagram_feed")
    assert "fit 'fill' not applied" in out["warnings"][0]
    monkeypatch.setattr(Timeline, "SetSetting",
                        lambda self, k, v: False if k.startswith("timelineResolution") else real(self, k, v))
    with pytest.raises(ToolError, match="Resolve kept"):
        d.social_timeline("tiktok", timeline="Main")


def test_social_timeline_loudness(project):
    d.append_clips(["a.mov"])
    project.current.tracks[("audio", 1)] = [project.current.tracks[("video", 1)][0]]
    out = d.social_timeline("youtube_shorts", loudness=-14)
    assert out["loudness"] == {"lufs": -14, "audio_tracks": [1]}
    items, options = project.current.normalized
    assert options["targetLoudness"] == -14.0 and len(items) == 1


def test_social_render(project, tmp_path):
    _two_items(project)
    with pytest.raises(ToolError, match="run social_timeline\\('tiktok'\\) first"):
        d.social_render("tiktok", str(tmp_path))
    out = d.social_render("youtube", str(tmp_path))  # 1920x1080 already
    assert (out["platform"], out["bitrate_kbps"], out["started"]) == ("YouTube", 16000, True)
    name, settings, fc = project.job_made[out["job"]]
    assert fc == ("mp4", "H264")
    assert {k: settings[k] for k in ("FormatWidth", "FormatHeight", "VideoQuality", "CustomName", "ExportAudio")} == \
        {"FormatWidth": 1920, "FormatHeight": 1080, "VideoQuality": 16000, "CustomName": "Main - YouTube",
         "ExportAudio": True}
    with pytest.raises(ToolError, match="kbps"):
        d.social_render("youtube", str(tmp_path), bitrate=50)


def test_social_export_all(project, tmp_path):
    _two_items(project)
    d.social_timeline("tiktok")
    project.current.settings["hand_tweak"] = "kept"
    d.switch_timeline("Main")
    out = d.social_export(["tiktok", "youtube", "instagram_feed"], str(tmp_path))
    assert [(j["platform"], j["timeline"], j["timeline_was"]) for j in out["jobs"]] == [
        ("tiktok", "Main - TikTok", "reused"), ("youtube", "Main - YouTube", "created"),
        ("instagram_feed", "Main - Instagram Feed", "created")]
    made = [project.job_made[j["job"]] for j in out["jobs"]]
    assert [m[0] for m in made] == ["Main - TikTok", "Main - YouTube", "Main - Instagram Feed"]
    assert [(m[1]["FormatWidth"], m[1]["FormatHeight"]) for m in made] == [(1080, 1920), (1920, 1080), (1080, 1350)]
    assert [m[1]["CustomName"] for m in made] == ["Main - TikTok", "Main - YouTube", "Main - Instagram Feed"]
    assert project.current.name == "Main" and out["started"]
    assert all(project.jobs[j["job"]]["JobStatus"] == "Rendering" for j in out["jobs"])
    tiktok = next(t for t in project.timelines if t.name == "Main - TikTok")
    assert tiktok.settings["hand_tweak"] == "kept"
    with pytest.raises(ToolError, match="listed twice"):
        d.social_export(["x", "x"], str(tmp_path))
    with pytest.raises(ToolError, match="unknown platform"):
        d.social_export(["x", "vine"], str(tmp_path))


def test_social_timeline_reframe_skips_titles(project, monkeypatch):
    _two_items(project)
    Item = type(project.current.tracks[("video", 1)][0])
    monkeypatch.setattr(Item, "SmartReframe", lambda self: self.media is not None)  # titles have nothing to follow
    project.current.tracks[("video", 1)][1].media = None
    out = d.social_timeline("tiktok", reframe=True)
    assert (out["reframed"], out["warnings"]) == (1, [])



# --- animated titles and templates ---


def _keys(comp, node, inp):
    return comp.FindTool(node).inputs[inp].keys


def test_animated_title_pop_and_slide(project):
    _two_items(project)
    out = d.animated_title("Chapter 1", animation="pop", exit="slide_left", speed=10, font="Arial", size=0.1,
                           color=[1, 1, 0], position=[0.5, 0.3])
    assert (out["track"], out["item"], out["duration"], out["animation_frames"]) == (1, 3, 120, 10)
    title = project.current.tracks[("video", 1)][2]
    comp = title.comps[0]
    assert out["set"]["StyledText"] == "Chapter 1" and out["set"]["Center"] == {"1": 0.5, "2": 0.3}
    # Template -> TitleMotion -> MediaOut1
    nodes = {n["name"]: n for n in d.fusion_nodes(3)}
    assert nodes["TitleMotion"]["inputs"] == {"Input": "Template"} and nodes["MediaOut1"]["inputs"] == {"Input": "TitleMotion"}
    assert _keys(comp, "TitleMotion", "Size") == {0: 0.0, 7: 1.12, 10: 1.0}
    # exits continuing leftwards: centre to the left edge side over the last 10 frames (109..119)
    assert _keys(comp, "TitleMotion", "Center") == {109: {1: 0.5, 2: 0.5}, 119: {1: 0.25, 2: 0.5}}
    assert not getattr(title, "fades", None)
    _assert_lock_rules(comp)


def test_animated_title_fade_and_typewriter(project):
    out = d.animated_title("Hello world", animation="typewriter", exit="fade", speed=8)
    title = project.current.tracks[("video", 1)][0]
    assert _keys(title.comps[0], "Template", "End") == {0: 0.0, 22: 1.0}  # 2 frames per character
    assert title.fades["FadeOut"] == 8.0 and title.comps[0].FindTool("TitleMotion") is None
    assert out["exit"] == "fade"
    with pytest.raises(ToolError, match="animation must be one of"):
        d.animated_title("x", animation="explode")


def test_title_insert_that_moves_clips_is_refused(project, monkeypatch):
    a, b = _two_items(project)
    tl = project.current
    real = tl.InsertFusionTitleIntoTimeline

    def cutting_insert(name):  # what an insert into V1 mid-clip would do
        item = real(name)
        a.end -= 10
        return item

    monkeypatch.setattr(tl, "InsertFusionTitleIntoTimeline", cutting_insert)
    with pytest.raises(ToolError, match=r"changed existing clips on video track\(s\) \[1\]"):
        d.animated_title("x", frame=5)
    assert tl.playhead == "00:00:00:05"


def test_lower_third(project):
    out = d.lower_third("Sara Ahmed", "Colorist", side="right")
    assert out["set"]["StyledText"] == "Sara Ahmed\nColorist" and out["set"]["Center"] == {"1": 0.72, "2": 0.16}
    assert (out["animation"], out["exit"]) == ("slide_left", "fade")
    comp = project.current.tracks[("video", 1)][0].comps[0]
    assert _keys(comp, "TitleMotion", "Center")[0] == {1: 0.75, 2: 0.5}  # enters from the right
    with pytest.raises(ToolError, match="side must be"):
        d.lower_third("x", side="top")


@pytest.fixture
def templates(tmp_path, monkeypatch):
    folder = tmp_path / "templates"
    monkeypatch.setattr(d, "TEMPLATES_DIR", str(folder))
    return folder


def test_save_list_apply_template(project, templates):
    d.animated_title("Episode 1", animation="pop", exit="none")
    saved = d.save_template(1, "Episode Card")
    assert saved["texts"] == {"Template": "Episode 1"} and (templates / "Episode Card.comp").exists()
    with pytest.raises(ToolError, match="exists"):
        d.save_template(1, "Episode Card")
    with pytest.raises(ToolError, match="invalid template name"):
        d.save_template(1, "../x")
    listed = d.list_templates()
    assert [t["template"] for t in listed["saved"]] == ["Episode Card"] and listed["saved"][0]["duration"] == 120
    out = d.apply_template("Episode Card", text="Episode 2")
    assert (out["item"], out["text"]) == (2, "Episode 2")
    new = project.current.tracks[("video", 1)][1]
    assert [c.name for c in new.comps] == ["Composition 1"]  # the title's own comp, replaced by the template
    assert d.list_titles()[1]["texts"] == {"Template": "Episode 2"}


def test_apply_template_to_clip(project, templates):
    a, b = _two_items(project)
    d.insert_fusion_effect(1, "Blur")
    d.save_template(1, "Soft")
    d.insert_fusion_effect(2, "Blur")
    out = d.apply_template("Soft", item=2)
    # a new comp holds the template and is active; the clip's own comp and its Blur are kept
    assert out["item"] == 2 and [c.name for c in b.comps] == ["Composition 1", "Composition 2"]
    assert b.active_comp == "Composition 2" and b.comps[0].FindTool("Blur1")
    with pytest.raises(ToolError, match="no Text\\+ node"):
        d.apply_template("Soft", item=2, text="x")
    assert b.active_comp == "Composition 3"  # the second import, in a comp of its own
    with pytest.raises(ToolError, match="template not found"):
        d.apply_template("Nope")


def test_batch_titles(project, templates):
    made = d.batch_titles([{"frame": 200, "text": "Two"}, {"frame": 50, "text": "One"}], animation="zoom")
    assert [m["set"]["StyledText"] for m in made] == ["One", "Two"]  # in time order
    d.save_template(1, "Card")
    made = d.batch_titles([{"frame": 300, "text": "Three"}], template="Card")
    assert made[0]["text"] == "Three" and made[0]["template"] == "Card"
    with pytest.raises(ToolError, match="needs a frame and a text"):
        d.batch_titles([{"frame": 1}])
    with pytest.raises(ToolError, match="template not found"):
        d.batch_titles([{"frame": 1, "text": "x"}], template="Missing")


def test_animation_length_capped_at_a_third(project):
    out = d.animated_title("Long", animation="zoom", exit="none", speed=100)
    assert out["animation_frames"] == 40  # 120-frame title


# --- automatic color correction ---


def test_png_decoder_all_filters(tmp_path):
    class It:
        look, cdl = {"lo": 0.1, "hi": 0.8, "cast": (1.1, 1.0, 0.9)}, None
    path = tmp_path / "f.png"
    path.write_bytes(render_look(It()))
    w, h, px = d._png_pixels(str(path), samples=10 ** 6)
    assert (w, h, len(px)) == (48, 32, 48 * 32)
    # row 7 (filter 2, Up) and row 9 (filter 4, Paeth), last column: v = 0.8
    assert px[7 * 48 + 47] == pytest.approx((0.88, 0.8, 0.72), abs=0.003)
    assert px[9 * 48 + 47] == pytest.approx((0.88, 0.8, 0.72), abs=0.003)
    assert px[30 * 48 + 47] == pytest.approx((0.88, 0.32, 0.144), abs=0.003)  # the saturated rows
    # every pixel, including row 24 (Paeth) where the gray ramp turns into the colored rows
    for y in range(32):
        for x in range(48):
            v = 0.1 + 0.7 * x / 47
            base = (v, v, v) if y < 24 else (v, 0.4 * v, 0.2 * v)
            want = tuple(round(255 * min(1.0, b * g)) / 255 for b, g in zip(base, (1.1, 1.0, 0.9)))
            assert px[y * 48 + x] == pytest.approx(want, abs=1e-9), (x, y)


def _looks(project, a_look, b_look=None):
    a, b = _two_items(project)
    a.look, b.look = a_look, b_look or a_look
    return a, b


def test_analyze_color(project, resolve):
    a, b = _looks(project, {"lo": 0.2, "hi": 0.6, "cast": (1.12, 1.0, 0.85)})
    project.current.playhead = "01:00:00:10"
    (row,) = d.analyze_color([2])
    assert (row["item"], row["frame"]) == (2, 86525)  # b.mov: 50 frames from 86500
    assert "low contrast (flat)" in row["verdict"] and "warm cast" in row["verdict"]
    assert row["neutral_from"] == "neutral areas" and row["neutral"][0] > row["neutral"][2]
    assert project.current.playhead == "01:00:00:10"  # restored
    project.current.playhead = "01:00:00:20"
    (here,) = d.analyze_color()
    assert here["frame"] == "01:00:00:20" and here["pixels_sampled"] == 48 * 32


def _balanced(row, tol=0.02):
    after = row["after"]
    assert after["black"] == pytest.approx([0.03] * 3, abs=tol)
    assert after["white"] == pytest.approx([0.94] * 3, abs=tol)
    assert max(after["neutral"]) - min(after["neutral"]) < tol
    assert after["luma_mean"] == pytest.approx(0.42, abs=tol)


@pytest.mark.parametrize("gamma", [1.0, 1.6])  # 1.6: a display transform the loop has to see through
def test_auto_color_converges(project, resolve, gamma):
    a, _ = _looks(project, {"lo": 0.15, "hi": 0.65, "cast": (1.15, 1.0, 0.8), "gamma": gamma})
    resolve.OpenPage("edit")
    (row,) = d.auto_color([1])
    _balanced(row)
    assert row["error"] < 0.02 and row["iterations"] <= 4
    assert "warm cast" in row["before"]["verdict"] and row["after"]["verdict"] == ["balanced"]
    assert a.cdl["NodeIndex"] == "1" and resolve.page == "edit"


def test_solve_channel_too_flat_to_stretch_fully():
    # live 21.1, red of a flat warm frame: reaching 0.03-0.94 needs slope 5. With slope capped at 4, an offset solved
    # for the uncapped slope and clamped on its own mapped the black point to 0.992: the channel turned inside out
    lb, lw = 0.3686, 0.549
    s, o, p = d._solve_channel(lb, lw, 0.3818, 0.03, 0.94, 0.42, 1.0)
    black, white = d._cdl_apply(lb, s, o, p), d._cdl_apply(lw, s, o, p)
    assert black < lb and white > lw  # stretched both ways, as far as the limits allow


def test_auto_color_strength_and_parts(project):
    a, b = _looks(project, {"lo": 0.15, "hi": 0.65, "cast": (1.15, 1.0, 0.8)})
    half, = d.auto_color([1], strength=0.5)
    before = half["before"]
    assert half["after"]["black"][1] == pytest.approx((before["black"][1] + 0.03) / 2, abs=0.02)
    n = half["after"]["neutral"]
    spread0 = max(before["neutral"]) - min(before["neutral"])
    assert max(n) - min(n) == pytest.approx(spread0 / 2, abs=0.02)  # half the cast left, not compounded
    only_balance, = d.auto_color([2], levels=False, exposure=False)
    ob = only_balance["after"]
    assert max(ob["neutral"]) - min(ob["neutral"]) < 0.02
    assert ob["luma_mean"] == pytest.approx(only_balance["before"]["luma_mean"], abs=0.03)  # brightness kept
    for kwargs, msg in [({"strength": 0}, "strength"), ({"iterations": 0}, "iterations")]:
        with pytest.raises(ToolError, match=msg):
            d.auto_color([1], **kwargs)


def test_shot_match(project):
    a, b = _looks(project, {"lo": 0.05, "hi": 0.9, "cast": (1.0, 1.0, 1.05)},
                  {"lo": 0.2, "hi": 0.6, "cast": (1.2, 1.0, 0.8), "gamma": 1.3})
    out = d.shot_match(1, [2])
    ref, (row,) = out["reference"], out["matched"]
    assert a.cdl is None  # the reference is left alone
    for key in ("black", "white", "neutral"):
        assert row["after"][key] == pytest.approx(ref[key], abs=0.025)
    with pytest.raises(ToolError, match="cannot also be a target"):
        d.shot_match(1, [1, 2])



def test_auto_color_replaces_an_existing_cdl(project):
    a, _ = _looks(project, {"lo": 0.15, "hi": 0.65, "cast": (1.15, 1.0, 0.8)})
    d.set_cdl(1, slope=[2.0, 0.5, 1.0], offset=[0.1, 0.0, 0.0])  # an old grade on the node
    (row,) = d.auto_color([1])
    _balanced(row)
    assert "warm cast" in row["before"]["verdict"]  # measured from the neutral node, not the old grade


def test_auto_color_warns_without_neutral_areas(project):
    _looks(project, {"lo": 0.5, "hi": 0.55, "cast": (1.6, 0.6, 0.3)})  # one strong color filling the frame
    (row,) = d.auto_color([1])
    assert "no neutral areas" in row["warnings"][0]
    (row,) = d.auto_color([1], balance=False)
    assert row["warnings"] == []



# --- automatic media organization ---


@pytest.fixture
def media(project, tmp_path):
    """Real files for date/size checks: interview.mov (2026-01-02), music.wav, logo.png (a still), and a timeline."""
    files = {}
    for name, day in (("interview.mov", 2), ("music.wav", 3), ("logo.png", 3)):
        f = tmp_path / "card" / name
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"x" * 10)
        t = datetime(2026, 1, day, 12).timestamp()
        os.utime(f, (t, t))
        files[name] = f
    root = project.pool.root
    root.clips += [Clip("interview.mov", 200, str(files["interview.mov"])), Clip("music.wav", 0, str(files["music.wav"])),
                   Clip("logo.png", 1, str(files["logo.png"]))]
    tl = Clip("Main", 0, "")
    tl.type = "Timeline"
    root.clips.append(tl)
    return files


def _bins(folder, prefix=""):
    out = {}
    for f in folder.subfolders:
        path = f"{prefix}/{f.name}"
        out[path] = sorted(c.name for c in f.clips)
        out.update(_bins(f, path))
    return out


def test_auto_organize_by_type(project, media):
    plan = d.auto_organize(dry_run=True)
    assert plan["plan"] == {"/Audio": ["music.wav"], "/Stills": ["logo.png"], "/Timelines": ["Main"],
                            "/Video": ["a.mov", "b.mov", "interview.mov"]}
    assert _bins(project.pool.root) == {"/B-roll": ["b.mov"]}  # nothing moved on a dry run
    out = d.auto_organize()
    assert out["moved"] == 6 and out["already_in_place"] == 0
    assert _bins(project.pool.root) == {"/B-roll": [], "/Audio": ["music.wav"], "/Stills": ["logo.png"],
                                        "/Timelines": ["Main"], "/Video": ["a.mov", "b.mov", "interview.mov"]}
    again = d.auto_organize()
    assert (again["already_in_place"], again["moved"], again["plan"]) == (6, 0, {})
    with pytest.raises(ToolError, match="by must be from"):
        d.auto_organize(by="mood")


def test_auto_organize_nested_into_bin(project, media):
    out = d.auto_organize(by=["type", "date"], bin="/", into="Organized", color=True)
    bins = _bins(project.pool.root)
    assert bins["/Organized/Video/2026-01-02"] == ["interview.mov"]
    assert bins["/Organized/Video/No date"] == ["a.mov", "b.mov"]  # no file on disk
    assert bins["/Organized/Audio/2026-01-03"] == ["music.wav"]
    assert out["colored"] == {"Video": 3, "Audio": 1, "Stills": 1, "Timelines": 1}


def test_auto_organize_other_keys(project, media):
    plan = d.auto_organize(by="extension", dry_run=True)["plan"]
    assert plan["/MOV"] == ["a.mov", "b.mov", "interview.mov"] and plan["/WAV"] == ["music.wav"]
    plan = d.auto_organize(by="folder", bin="B-roll", dry_run=True)["plan"]
    assert plan == {"/media": ["b.mov"]}
    plan = d.auto_organize(by="fps", dry_run=True)["plan"]
    assert plan == {"/24 fps": ["Main", "a.mov", "b.mov", "interview.mov", "logo.png", "music.wav"]}
    clip = next(c for c in project.pool.root.clips if c.name == "interview.mov")
    clip.metadata.update({"Camera Manufacturer": "Blackmagic", "Camera Type": "Pocket 6K"})
    plan = d.auto_organize(by="camera", dry_run=True)["plan"]
    assert plan["/Blackmagic Pocket 6K"] == ["interview.mov"]


def test_color_code(project, media):
    out = d.color_code(colors={"Audio": "Teal"})
    assert out["colors"]["Audio"] == "Teal" and out["colored"]["Video"] == 3
    clip = next(c for c in project.pool.root.clips if c.name == "music.wav")
    assert clip.GetClipColor() == "Teal"
    with pytest.raises(ToolError, match="unknown clip colors"):
        d.color_code(colors={"Audio": "Gold"})


def test_find_unused(project, media):
    d.append_clips(["a.mov", "interview.mov"])
    out = d.find_unused(move_to="Unused")
    assert sorted(r["name"] for r in out["unused"]) == ["b.mov", "logo.png", "music.wav"]  # never the timeline
    assert _bins(project.pool.root)["/Unused"] == ["b.mov", "logo.png", "music.wav"]


def test_find_duplicates(project, media, tmp_path):
    root = project.pool.root
    first = next(c for c in root.clips if c.name == "interview.mov")
    again = Clip("interview.mov", 200, first.path)
    root.subfolders[0].clips.append(again)  # imported twice, into B-roll
    copy = tmp_path / "backup" / "music.wav"
    copy.parent.mkdir()
    copy.write_bytes(b"x" * 10)
    root.clips.append(Clip("music copy", 0, str(copy)))
    project.current = None
    d.create_timeline("Main 2")
    d.append_clips(["b.mov"])
    project.current.tracks[("video", 1)][0].media = again  # the B-roll copy is the one in use
    out = d.find_duplicates()
    assert out["same_file"] == [{"file": os.path.normcase(first.path),
                                 "clips": [{"name": "interview.mov", "bin": "/"}, {"name": "interview.mov", "bin": "B-roll"}]}]
    assert out["same_name_and_size"] == [{"files": sorted([str(media["music.wav"]), str(copy)])}]
    removed = d.find_duplicates(remove=True)["removed"]
    assert removed == [{"name": "interview.mov", "bin": "/"}]  # the used entry stays
    assert first not in root.clips and again in root.subfolders[0].clips


def test_find_offline_and_relink(project, tmp_path):
    root = project.pool.root
    root.clips.append(Clip("x.mov", 10, "/old drive/x.mov"))
    root.clips.append(Clip("gone.mov", 10, "/old drive/gone.mov"))
    out = d.find_offline(bin=None)
    assert [r["name"] for r in out["offline"]] == ["a.mov", "x.mov", "gone.mov", "b.mov"]
    (tmp_path / "new" / "day1").mkdir(parents=True)
    (tmp_path / "new" / "day1" / "x.mov").write_bytes(b"x")
    (tmp_path / "new" / "a.mov").write_bytes(b"x")
    out = d.find_offline(search=str(tmp_path / "new"))
    assert sorted(r["name"] for r in out["relinked"]) == ["a.mov", "x.mov"]
    assert sorted(out["still_offline"]) == ["b.mov", "gone.mov"]
    assert next(c for c in root.clips if c.name == "x.mov").path == str(tmp_path / "new" / "day1" / "x.mov")
    with pytest.raises(ToolError, match="folder not found"):
        d.find_offline(search=str(tmp_path / "nowhere"))


def test_clean_bins(project):
    root = project.pool.root
    empty = Folder("Empty", subfolders=[Folder("Also empty")])
    keep = Folder("Keep", subfolders=[Folder("Hollow")])
    keep.clips.append(Clip("k.mov"))
    root.subfolders += [empty, keep]
    out = d.clean_bins()
    assert out["deleted"] == ["/Empty", "/Keep/Hollow"]
    assert [f.name for f in root.subfolders] == ["B-roll", "Keep"] and keep.subfolders == []
    assert d.clean_bins("Keep") == {"deleted": []}



def test_media_kind_without_a_type_column(project):
    root = project.pool.root
    for name, frames, path in (("seq_[1-48].png", 48, "/x/seq_%04d.png"), ("logo.png", 1, "/x/logo.png"),
                               ("vo.wav", 0, "/x/vo.wav"), ("gen", 0, "")):
        c = Clip(name, frames, path)
        c.path = path  # "" for a clip without a file (a generator)
        c.type = "unknown"  # a Type Resolve reports that says nothing: fall back to the file
        root.clips.append(c)
    plan = d.auto_organize(dry_run=True)["plan"]
    assert "seq_[1-48].png" in plan["/Video"] and plan["/Stills"] == ["logo.png"]
    assert plan["/Audio"] == ["vo.wav"] and plan["/Other"] == ["gen"]


def test_find_unused_matches_by_unique_id(project, media, monkeypatch):
    import copy
    d.append_clips(["a.mov"])
    item = project.current.tracks[("video", 1)][0]
    monkeypatch.setattr(item, "GetMediaPoolItem", lambda: copy.copy(item.media))  # a new wrapper per call, as live
    assert "a.mov" not in [r["name"] for r in d.find_unused()["unused"]]


def test_find_duplicates_keeps_every_used_entry(project, media):
    root = project.pool.root
    first = next(c for c in root.clips if c.name == "interview.mov")
    second, third = Clip("interview.mov", 200, first.path), Clip("interview.mov", 200, first.path)
    root.clips += [second, third]
    d.create_timeline("T")
    d.append_clips(["a.mov", "b.mov"])
    items = project.current.tracks[("video", 1)]
    items[0].media, items[1].media = first, second  # two of the three copies are in use
    removed = d.find_duplicates(remove=True)["removed"]
    assert len(removed) == 1 and third not in root.clips and first in root.clips and second in root.clips


def test_clean_bins_keeps_the_bin_itself(project):
    project.pool.root.subfolders.append(Folder("Empty"))
    assert d.clean_bins("Empty") == {"deleted": []}
    assert "Empty" in [f.name for f in project.pool.root.subfolders]


def test_linsolve():
    assert d._linsolve([[2, 1, 0], [1, 3, 1], [0, 1, 4]], [3, 5, 5]) == pytest.approx([1, 1, 1])
    assert d._linsolve([[1, 2], [2, 4]], [1, 2]) is None  # singular


def test_levenberg_sees_through_a_channel_mixing_display():
    # a display that mixes channels (as color management does): each output channel takes 20% of the others, which
    # the per-channel model cannot describe; the model-free refinement must still reach the goal
    def display(cdl):
        levels = {}
        for key, scene in (("black", 0.2), ("white", 0.8), ("neutral", 0.5)):  # a goal within the CDL limits
            raw = [min(1.0, max(0.0, scene * cdl[0][c] + cdl[1][c])) ** cdl[2][c] for c in range(3)]
            levels[key] = [0.6 * raw[c] + 0.2 * raw[(c + 1) % 3] + 0.2 * raw[(c + 2) % 3] for c in range(3)]
        return {**levels, "crushed_share": [0.0] * 3, "clipped_share": [0.0] * 3}

    goal = {"black": [0.05] * 3, "white": [0.9] * 3, "neutral": [0.4, 0.42, 0.44], "mid": None, "cast": None}
    identity = ([1.0] * 3, [0.0] * 3, [1.0] * 3)
    (cdl, st), used = d._levenberg(display, identity, display(identity), goal, budget=48)
    assert d._error(st, goal) < 0.015 and used <= 48
