import asyncio
import json
import logging
import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import davinci_mcp as d


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
    with pytest.raises(ToolError, match="already exists"):
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
    assert (entry["startFrame"], entry["endFrame"], entry["trackIndex"]) == (10, 19, 1)


def test_append_subclip_defaults_end_to_last_frame(project):
    d.append_clips(["b.mov"], start_frame=5)
    (entry,) = project.pool.appended[-1]
    assert (entry["startFrame"], entry["endFrame"]) == (5, 49)


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
    assert project.current.markers[24] == {"color": "Red", "name": "cut here", "note": "cut here", "duration": 1}
    d.add_marker(48)
    assert project.current.markers[48]["name"] == "frame 48"
    with pytest.raises(ToolError, match="duplicate frame"):
        d.add_marker(24)


def test_render(project, tmp_path):
    assert d.list_render_presets() == ["H.264 Master", "YouTube 1080p"]
    out = d.render(str(tmp_path), preset="YouTube 1080p", file_name="final")
    assert out == {"job": "job-1", "target_dir": str(tmp_path)}
    assert project.loaded_preset == "YouTube 1080p"
    assert project.render_settings == {"TargetDir": str(tmp_path), "CustomName": "final"}
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
        "insert_fusion_effect",
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


def test_list_render_formats(project):
    assert d.list_render_formats() == {
        "QuickTime": {"extension": "mov", "codecs": {"ProRes422HQ": "Apple ProRes 422 HQ", "H264": "H.264"}},
        "MP4": {"extension": "mp4", "codecs": {"H264": "H.264"}},
    }


def test_render_with_format_and_codec(project, tmp_path):
    d.render(str(tmp_path), format="QuickTime", codec="ProRes422HQ")
    assert project.format_codec == ("QuickTime", "ProRes422HQ")
    with pytest.raises(ToolError, match="must be given together"):
        d.render(str(tmp_path), preset="YouTube 1080p", format="QuickTime")
    assert project.loaded_preset is None  # rejected before touching the project
    with pytest.raises(ToolError, match="unsupported format/codec"):
        d.render(str(tmp_path), format="MP4", codec="ProRes422HQ")


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
    assert d.add_fusion_comp(1, import_path=str(tpl))["comps"] == ["Composition 1", "glow_template"]
    assert d.fusion_comps(1) == ["Composition 1", "glow_template"]
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
    assert d.add_transition(1, duration=24) == {"name": "Cross Dissolve", "start": 86488, "end": 86512, "duration": 24}
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
