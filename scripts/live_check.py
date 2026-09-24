#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2"]
# ///
"""Check davinci-mcp's tools against a running DaVinci Resolve and write a report.

Run from the repository root, with Resolve open:
    uv run scripts/live_check.py

What it touches:
- The project open in Resolve is SAVED (as the server does before switching projects), then left alone.
- A scratch project "davinci-mcp live check <time>" is created, used, and deleted at the end.
- Test media (PNG image sequences and a WAV tone, generated here) and all outputs go to a new folder,
  printed at the start and kept, with report.md / report.json inside.
Studio-only calls (stabilize, smart reframe, scene cuts, voice isolation, captions, Dolby Vision) run only with
--studio: on the free edition Resolve opens an upgrade dialog that blocks every later call.
"""
import argparse
import json
import math
import os
import struct
import sys
import tempfile
import time
import traceback
import wave
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import davinci_mcp as d  # noqa: E402

d.log.disabled = True  # this script reports every result itself

RESULTS = []


def step(name, fn, *, needs=True, note=""):
    """Run one check; PASS/FAIL/SKIP with the result or error. Returns the value, or None."""
    if needs is not True:
        RESULTS.append({"check": name, "status": "SKIP", "detail": needs or "prerequisite failed"})
        print(f"  SKIP  {name}: {needs or 'prerequisite failed'}")
        return None
    try:
        value = fn()
    except Exception as e:  # noqa: BLE001 - every failure is a finding
        detail = f"{type(e).__name__}: {e}"
        RESULTS.append({"check": name, "status": "FAIL", "detail": detail,
                        "trace": traceback.format_exc(limit=3) if not isinstance(e, d.ToolError) else None})
        print(f"  FAIL  {name}: {detail}")
        return None
    RESULTS.append({"check": name, "status": "PASS", "detail": _short(value), "note": note})
    print(f"  PASS  {name}: {_short(value)}")
    return value if value is not None else True


def _short(v, n=300):
    text = json.dumps(v, default=str, ensure_ascii=False) if not isinstance(v, str) else v
    return text if len(text) <= n else text[:n] + "…"


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --- test media -------------------------------------------------------------------------------------------------


def write_png(path, w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_wav(path, seconds=4, rate=48000, freq=440.0, level_db=-12.0):
    amp = 32767 * 10 ** (level_db / 20)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
                               for i in range(rate * seconds)))


def make_media(folder, frames=48):
    for name, rgb in (("red", (200, 30, 30)), ("blue", (30, 60, 200))):
        (folder / name).mkdir()
        for i in range(1, frames + 1):
            write_png(folder / name / f"{name}_{i:04d}.png", 640, 360, rgb)
    write_wav(folder / "tone.wav")


# --- main -------------------------------------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="do not ask before starting")
    ap.add_argument("--studio", action="store_true", help="also run Studio-only calls (never on the free edition)")
    ap.add_argument("--render-timeout", type=int, default=180, help="seconds to wait for the test render")
    args = ap.parse_args()

    st = step("connect: status", d.status)
    if not st:
        print("\nCannot reach Resolve: is it running, with Preferences > System > General > External scripting "
              "set to Local?")
        return write_report(None, {})
    pm = d._resolve().GetProjectManager()
    original = st.get("project")
    work = Path(tempfile.mkdtemp(prefix="davinci_mcp_live_"))
    scratch = f"davinci-mcp live check {datetime.now():%Y%m%d-%H%M%S}"

    print(f"\nResolve {st.get('product')} {st.get('version')}")
    print(f"Open project: {original!r} — it will be SAVED, then a scratch project {scratch!r} is created and "
          f"deleted at the end.\nWork folder (kept): {work}\n")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1

    version = tuple(int(x) for x in (st.get("version") or "0").split(".")[:2] if x.isdigit())
    is_211 = version >= (21, 1)
    info = {"product": st.get("product"), "version": st.get("version"), "work": str(work), "original": original}

    step("setup: generate test media (PNG sequences, WAV)", lambda: make_media(work) or "ok")
    created = step("projects: create_project (saves the open project first)", lambda: d.create_project(scratch))
    if not created:
        return write_report(info, {"work": work})
    try:
        run_checks(args, work, is_211)
    finally:
        print("\nCleaning up")
        if original and original != d.UNTITLED:
            step("cleanup: reopen your project", lambda: d.open_project(original))
            step("cleanup: delete the scratch project", lambda: d.delete_project(scratch))
        else:
            RESULTS.append({"check": "cleanup", "status": "SKIP",
                            "detail": f"no named project was open before; delete {scratch!r} by hand"})
    return write_report(info, {"work": work})


def run_checks(args, work, is_211):
    print("\nMedia")
    step("media: create_bin", lambda: d.create_bin("Live Check"))
    red = step("media: import_image_sequence red", lambda: d.import_image_sequence(
        str(work / "red" / "red_%04d.png"), 1, 48, bin="Live Check"))
    blue = step("media: import_image_sequence blue", lambda: d.import_image_sequence(
        str(work / "blue" / "blue_%04d.png"), 1, 48, bin="Live Check"))
    step("media: import_media wav", lambda: d.import_media([str(work / "tone.wav")], bin="Live Check"))
    clips = step("media: list_clips", d.list_clips) or []
    names = {c["name"] for c in clips}
    seq = sorted(n for n in names if n.lower().startswith(("red", "blue")))
    wav = next((n for n in names if n.lower().startswith("tone")), None)
    have_media = True if len(seq) == 2 else f"image sequences not found in media pool: {sorted(names)}"
    step("media: clip_info + tag_clips", lambda: (d.tag_clips([seq[0]], color="Teal", metadata={"Scene": "1"}),
                                                    d.clip_info(seq[0])["metadata"])[1], needs=have_media)

    print("\nEdit")
    step("edit: create_timeline", lambda: d.create_timeline("Check"))
    appended = step("edit: append_clips subclips 0-23 (leaves handles)",
                    lambda: d.append_clips(seq, start_frame=0, end_frame=23), needs=have_media)
    if wav:
        step("edit: append wav", lambda: d.append_clips([wav]))
    ov = step("edit: timeline_overview", d.timeline_overview)
    have_items = True if appended else "append failed"

    def durations():
        items = d.list_items()
        expect([i["duration"] for i in items[:2]] == [24, 24], f"expected two 24-frame items (end_frame inclusive), got {items}")
        return items

    step("edit: end_frame is inclusive (24 frames each)", durations, needs=have_items)
    step("edit: view_frame returns an image", lambda: _frame(work / "frame_edit.png"), needs=have_items)

    print("\nRender (checks format ids, absolute range, output file, and that the append renders frames)")
    fmts = step("render: list_render_formats keyed by id", lambda: _formats(d.list_render_formats()))
    start = int(ov["start_frame"]) if ov else None
    job = step("render: mp4/H264 frames start..start+23", lambda: d.render(
        str(work), format="mp4", codec="H264", mark_in=start, mark_out=start + 23, file_name="range_check"),
        needs=True if (have_items and fmts and "mp4" in fmts) else "no mp4 format or no items")
    if job:
        step("render: render_status done + output_exists", lambda: _wait_render(job["job"], args.render_timeout))

    print("\nInterchange")
    for fmt, ext in (("fcpxml", "fcpxml"), ("fcp7_xml", "xml"), ("otio", "otio"), ("edl", "edl"), ("aaf", "aaf"),
                     ("drt", "drt"), ("csv", "csv")):
        step(f"export_timeline {fmt}", lambda f=fmt, e=ext: d.export_timeline(str(work / f"check.{e}"), f),
             needs=have_items)
    step("export_metadata", lambda: d.export_metadata(str(work / "metadata.csv")))
    step("export_project .drp", lambda: d.export_project(str(work / "check")))

    print("\nColor")
    step("color: color_management_info", d.color_management_info)
    step("color: apply_color_preset rcm_custom", lambda: d.apply_color_preset("rcm_custom"))
    step("color: set spaces 'DaVinci WG/Intermediate' / 'Rec.709 Gamma 2.4'", lambda: d.set_color_management(
        {"colorSpaceTimeline": "DaVinci WG/Intermediate", "colorSpaceOutput": "Rec.709 Gamma 2.4"}),
        note="confirms the exact space names")
    step("color: color_management_info after", d.color_management_info)
    step("color: apply_color_preset rcm_sdr", lambda: d.apply_color_preset("rcm_sdr"))
    step("color: spaces locked under automatic RCM are reported", lambda: _expect_tool_error(
        lambda: d.set_color_management({"colorSpaceOutput": "Rec.2100 ST2084"})))
    step("color: set_hdr mastering 1000 then off", lambda: (d.set_hdr(mastering_nits=1000), d.set_hdr(mastering_nits=0)))
    step("color: set_cdl on item 1", lambda: d.set_cdl(1, slope=[1.1, 1.0, 0.9]), needs=have_items)
    step("color: color_info item 1", lambda: d.color_info(1), needs=have_items)
    step("color: export_lut (switches to Color page and back)", lambda: _lut(work), needs=have_items)
    step("color: set_clip_color_space Rec.709", lambda: d.set_clip_color_space([seq[0]], color_space="Rec.709"),
         needs=have_media, note="needs a color-managed project; rcm_sdr is")

    print("\nFusion")
    step("fusion: insert_fusion_effect Blur", lambda: d.insert_fusion_effect(1, "Blur", {"XBlurSize": 8.0}),
         needs=have_items)
    step("fusion: fusion_nodes", lambda: d.fusion_nodes(1), needs=have_items)
    step("fusion: dynamic_zoom item 2 (comp range on a media clip)", lambda: d.dynamic_zoom(2, end_zoom=1.3),
         needs=have_items, note="frames should cover the clip: 24 frames")
    step("fusion: Tracker point input names", lambda: _tracker_inputs(), needs=have_items,
         note="link_mask_to_tracker assumes TrackedCenter1")
    step("fusion: view_frame after effects", lambda: _frame(work / "frame_fusion.png"), needs=have_items)

    print("\nReview and timelines")
    step("review: add/resolve/export notes", lambda: _notes(work))
    step("timeline: duplicate_timeline keeps current", lambda: _dup())
    step("edit: delete_items from the Fairlight page", lambda: _delete_from_fairlight(), needs=have_items)

    print("\nResolve 21.1")
    need211 = True if is_211 else "needs Resolve 21.1"
    step("21.1: add_transition Cross Dissolve 12f", lambda: d.add_transition(1, duration=12), needs=need211)
    step("21.1: set_fades on item 1", lambda: d.set_fades(1, fade_in=6, track_type="video"), needs=need211)
    step("21.1: set_speed 50% on last item", lambda: d.set_speed(len(d.list_items()), 50), needs=need211)
    step("21.1: normalize_audio -16 LKFS", lambda: d.normalize_audio([1], loudness=-16),
         needs=need211 if wav else "no audio item")

    print("\nAudio")
    step("audio: fairlight_info", d.fairlight_info)
    step("audio: add_track 5.1", lambda: d.add_track(format="5.1", name="Surround"))

    print("\nStudio only")
    studio = True if args.studio else "run with --studio (the free edition blocks on an upgrade dialog)"
    step("studio: stabilize item 1", lambda: d.stabilize(1), needs=studio)
    step("studio: voice_isolation A1", lambda: d.voice_isolation(1, amount=50), needs=studio if wav else "no audio")
    step("studio: set_hdr dolby 4.0 + analyze", lambda: (d.set_hdr(dolby_vision="4.0"), d.analyze_dolby_vision()),
         needs=studio)


# --- helpers for individual checks ------------------------------------------------------------------------------


def _frame(path):
    out = d.view_frame(save_to=str(path))
    expect(path.exists() and path.stat().st_size > 0, f"no frame written at {path}")
    return {"note": out[-1], "bytes": path.stat().st_size}


def _formats(fmts):
    expect(fmts and all(v.get("name") for v in fmts.values()), "unexpected shape")
    expect(any(v["codecs"] for v in fmts.values()), "no format lists any codec: format ids are not being used")
    return {k: v["name"] for k, v in fmts.items()}


def _wait_render(job, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = d.render_status(job)
        if st.get("done") or st.get("Error"):
            break
        time.sleep(2)
    expect(st.get("done"), f"render not done: {st}")
    expect(st.get("output_exists"), f"render done but no output file: {st}")
    size = os.path.getsize(st["output"])
    expect(size > 10_000, f"output is only {size} bytes: the appended clips may render empty")
    return {"output": st["output"], "bytes": size}


def _lut(work):
    before = d._resolve().GetCurrentPage()
    d.export_lut(1, str(work / "grade.cube"))
    expect((work / "grade.cube").exists(), "no .cube written")
    expect(d._resolve().GetCurrentPage() == before, "page not restored")
    return (work / "grade.cube").stat().st_size


def _tracker_inputs():
    d.add_fusion_node(1, "Tracker", connect_from="MediaIn1")
    ids = [r["id"] for r in d.fusion_inputs(1, "Tracker1") if r["type"] == "Point"]
    return {"point_inputs": ids, "has_TrackedCenter1": "TrackedCenter1" in ids}


def _notes(work):
    d.add_review_note(12, "Check note", author="live-check")
    d.resolve_review_note(12)
    notes = d.review_notes()
    expect(notes and notes[0]["status"] == "resolved", f"unexpected notes: {notes}")
    d.export_review_notes(str(work / "notes.md"))
    return notes


def _dup():
    out = d.duplicate_timeline("Check v2")
    expect(d.status()["timeline"] == "Check", f"current timeline changed: {d.status()['timeline']}")
    return out


def _delete_from_fairlight():
    d.switch_timeline("Check v2")
    before = len(d.list_items())
    d.open_page("fairlight")
    d.delete_items([1])
    expect(len(d.list_items()) == before - 1, "nothing deleted")
    expect(d._resolve().GetCurrentPage() == "fairlight", "page not restored")
    d.open_page("edit")
    d.switch_timeline("Check")
    return "deleted 1 item from the Fairlight page"


def _expect_tool_error(fn):
    try:
        fn()
    except d.ToolError as e:
        return f"reported as expected: {e}"
    raise AssertionError("Resolve accepted a write that should be locked, or the lock was not detected")


# --- report -----------------------------------------------------------------------------------------------------


def write_report(info, paths):
    counts = {s: sum(r["status"] == s for r in RESULTS) for s in ("PASS", "FAIL", "SKIP")}
    work = paths.get("work")
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    if work:
        (work / "report.json").write_text(json.dumps({"info": info, "counts": counts, "results": RESULTS},
                                                     indent=2, default=str, ensure_ascii=False))
        lines = [f"# davinci-mcp live check\n", f"{json.dumps(info, ensure_ascii=False)}\n",
                 f"**{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped**\n",
                 "| Check | Result | Detail |", "|---|---|---|"]
        for r in RESULTS:
            detail = str(r.get("detail", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['check']} | {r['status']} | {detail} |")
        (work / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Report: {work / 'report.md'}  (send this file back)")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
