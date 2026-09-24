# davinci-mcp

Single-file [MCP](https://modelcontextprotocol.io) server for DaVinci Resolve. Talks to the running Resolve instance through Blackmagic's bundled scripting API — no extra install beyond [uv](https://docs.astral.sh/uv/).

## Use

DaVinci Resolve must be open (Studio or free; scripting must be enabled under *Preferences → System → General → External scripting using*).

```bash
claude mcp add davinci -- uv run /path/to/davinci_mcp.py
```

Claude Desktop / any MCP client:

```json
{ "mcpServers": { "davinci": { "command": "uv", "args": ["run", "/path/to/davinci_mcp.py"] } } }
```

## Tools

All frames are absolute timeline frames unless noted. Item and track indexes are 1-based. A failed call returns an MCP tool error with the reason. Common ones: `cannot load Resolve scripting module … is DaVinci Resolve installed?`, `cannot connect — is DaVinci Resolve running?`, `no project open`, `no timeline open`.

### Projects

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `status` | — | `{product, version, page, project, timeline, start_frame, end_frame}`; project/timeline fields are `null` when none is open | Resolve not reachable |
| `list_projects` | — | Project names in the current project-manager folder | |
| `open_project` | `name: str` | `"opened: <name>"` | Project cannot be opened |
| `create_project` | `name: str` | `"created: <name>"`; the new project becomes current | Name already exists |

### Media pool

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `import_media` | `paths: list[str]` (relative paths resolve against the server's working directory) | Imported clip names | Any path missing; import rejected |
| `list_clips` | — | `[{folder, name, frames}]` for every clip, recursing into subfolders; `folder` is `/` for the root | |

### Timelines

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `list_timelines` | — | `[{name, current}]` | |
| `create_timeline` | `name: str` | `"created: <name>"`; the new timeline becomes current | Name taken |
| `switch_timeline` | `name: str` | `"switched: <name>"` | Timeline not found |
| `append_clips` | `names: list[str]`, `start_frame: int \| None`, `end_frame: int \| None`, `track: int = 1` | `"appended N clip(s) to '<timeline>'"` | Clip name not in media pool; append rejected |
| `list_items` | `track: int = 1`, `track_type: str = "video"` (`video`, `audio` or `subtitle`) | `[{index, name, start, end, duration}]` | |
| `set_item_properties` | `item: int`, `properties: dict`, `track: int = 1` | `{item, set}` | Item index out of range; any key rejected by Resolve |
| `insert_title` | `name: str = "Text"`, `fusion: bool = False`, `text: str \| None` | `{title, start, end, text_set}` | Template not found |
| `add_marker` | `frame: int`, `note: str = ""`, `color: str = "Blue"`, `duration: int = 1` | `"marker @ <frame> (<color>)"` | Marker already at that frame |

Notes:

- `append_clips` appends to the end of the current timeline in the order given. Without `start_frame`/`end_frame` whole clips are used and `track` is ignored. With either set, each clip is trimmed to `start_frame`–`end_frame` (clip-relative, both inclusive: `0`–`23` is the first 24 frames; defaults `0` and the clip's last frame) and placed on `track`.
- `set_item_properties` works on video tracks only. Keys and ranges (from Resolve's API reference):

  | Key | Value |
  |---|---|
  | `Pan`, `Tilt`, `AnchorPointX`, `AnchorPointY` | float, ±4 × frame width/height |
  | `ZoomX`, `ZoomY` | float, 0–100 (1 = 100 %); `ZoomGang` bool links them |
  | `RotationAngle` | float, −360–360 |
  | `Pitch`, `Yaw` | float, −1.5–1.5 |
  | `FlipX`, `FlipY`, `CropRetain` | bool |
  | `CropLeft`, `CropRight`, `CropTop`, `CropBottom` | float, 0–frame width/height |
  | `CropSoftness` | float, −100–100 |
  | `Opacity` | float, 0–100 |
  | `Distortion` | float, −1–1 |
  | `CompositeMode` | `normal` `add` `subtract` `diff` `multiply` `screen` `overlay` `hardlight` `softlight` `darken` `lighten` `color_dodge` `color_burn` `exclusion` `hue` `saturate` `colorize` `luma_mask` `divide` `linear_dodge` `linear_burn` `linear_light` `vivid_light` `pin_light` `hard_mix` `lighter_color` `darker_color` `foreground` `alpha` `inverted_alpha` `lum` `inverted_lum` |
  | `DynamicZoomEase` | `linear` `in` `out` `in_and_out` |
  | `RetimeProcess` | `project` `nearest` `frame_blend` `optical_flow` |
  | `MotionEstimation` | `project` `standard_faster` `standard_better` `enhanced_faster` `enhanced_better` `speed_warp` |
  | `Scaling` | `project` `crop` `fit` `fill` `stretch` |
  | `ResizeFilter` | `project` `sharper` `smoother` `bicubic` `bilinear` `bessel` `box` `catmull_rom` `cubic` `gaussian` `lanczos` `mitchell` `nearest_neighbor` `quadratic` `sinc` `linear` |

  Enum keys take the name (case, spaces and hyphens are ignored) or Resolve's number. `RetimeProcess`/`MotionEstimation` choose how a speed change is rendered; the speed itself is set with `set_speed` (Resolve 21.1+).
- `insert_title` inserts at the playhead. `text` is applied only when `fusion=True` and the Fusion title has a `Template` tool; `text_set` says whether it was.
- `add_marker` takes `frame` relative to the timeline start, not an absolute frame. `note` is used as the marker name too (or `frame <n>` when empty).

### Editing

Start with `timeline_overview` to see the edit and `view_frame` to see the picture.

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `timeline_overview` | — | `{timeline, fps, resolution, start_frame, end_frame, playhead, tracks: {video, audio, subtitle: [{index, name, enabled, items: [{index, kind, name, source, start, end, duration, enabled, fusion_comps}]}]}, markers}` | |
| `view_frame` | `timecode: str \| None` (absolute) **or** `frame: int \| None` (absolute), `save_to: str \| None` | The frame as an image, plus its timecode | Both given; playhead cannot move; drop-frame timeline with `frame`; export failed |
| `add_transition` | `item: int`, `type: str = "Cross Dissolve"`, `position: "start" \| "end" = "end"`, `alignment: "left" \| "center" \| "right" = "center"`, `duration: int \| None`, `category: "simple" \| "fusion" \| "ofx" \| "audio" = "simple"`, `track`, `track_type` | `{name, start, end, duration}` | Bad option; Resolve older than 21.1; no transition created |
| `delete_items` | `items: list[int]`, `ripple: bool = False`, `track`, `track_type` | Confirmation string | Index out of range |
| `set_clip_enabled` | `item: int`, `enabled: bool`, `track`, `track_type` | Confirmation string | Index out of range |
| `stabilize` | `item: int`, `track: int = 1` | Confirmation string | Unsupported clip; Resolve older than 18 |
| `smart_reframe` | `item: int`, `track: int = 1` | Confirmation string | Studio only; Resolve older than 18 |
| `detect_scene_cuts` | — | Confirmation string | Studio only |
| `dynamic_zoom` | `item: int`, `start_zoom: float = 1.0`, `end_zoom: float = 1.2`, `start_center`, `end_center: [x, y] = [0.5, 0.5]`, `track` | `{item, node, frames, zoom, center}` | Zoom ≤ 0; clip already has one |
| `insert_fusion_effect` | `item: int`, `tool_type: str`, `settings: dict \| None`, `name: str \| None`, `track` | `{item, node, type, settings}` | Unknown tool; bad setting (the node stays in the chain) |

Notes:

- `timeline_overview` classifies items as `clip` (has source media), `transition` (straddles a cut) or `other` (titles, generators, Fusion compositions). Transitions are items in Resolve, so they shift the indexes of everything after them.
- `view_frame` returns the frame as Resolve renders it (grade and Fusion included), so the model can check the result of an edit. Frames and timecodes are absolute timeline positions (a timeline usually starts at `01:00:00:00` = frame 86400 at 24 fps).
- `add_transition` needs Resolve 21.1+. It fails when either clip has no unused media (handles) past the cut, or when `type` is not the exact name of an installed transition.
- `stabilize` and `smart_reframe` can keep analysing after they return. `smart_reframe` and `detect_scene_cuts` are Studio features: on the free edition Resolve opens an upgrade dialog that blocks further API calls until it is closed.
- `dynamic_zoom` is a Ken Burns move built from a keyframed Fusion `Transform` over the clip's comp range, not Resolve's Dynamic Zoom checkbox (the API cannot switch that on). Refine it with `set_fusion_input(node="DynamicZoom", ...)`.
- `insert_fusion_effect` adds each effect before `MediaOut1`, so repeated calls build a chain in the order given. `tool_type` is a Fusion registry id (`SoftGlow`, `Glow`, `Blur`, `Sharpen`, `FilmGrain`, `ColorCorrector`, `DirectionalBlur`, `Defocus`, …). ResolveFX plugins are available in Fusion under their OFX id; check the id in Resolve's Fusion page.

### Audio / Fairlight

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `fairlight_info` | — | `{tracks: [{index, name, format, enabled, locked, voice_isolation, items}], fairlight_presets, normalize_modes}` | |
| `apply_fairlight_preset` | `name: str` | Confirmation string | Unknown preset; Resolve older than 20.2.2 |
| `add_track` | `track_type: str = "audio"`, `format: str = "stereo"` (`mono` `stereo` `5.1` `7.1` `adaptive1`…`adaptive36`), `name: str \| None` | `{track_type, index, name, format}` | Unknown type or format |
| `set_track` | `track_type: str`, `index: int`, `name`, `enabled`, `locked` (any of them) | `{track_type, index, name, enabled, locked}` | Track not found; nothing to change |
| `delete_track` | `track_type: str`, `index: int` | Confirmation string with the number of items removed | Track not found |
| `voice_isolation` | `track: int`, `enabled: bool = True`, `amount: int = 50` (0–100) | `{track, state}` | Track not found; Studio only |
| `normalize_audio` | `items: list[int]`, `loudness: float` (LKFS) **or** `level: float` (dBFS), `mode: str \| None`, `independent: bool = False`, `track: int = 1` | Confirmation string | No target; unknown mode; Resolve older than 21.1 |
| `set_fades` | `item: int`, `fade_in`, `fade_out: int` (frames), `track: int = 1`, `track_type: str = "audio"` | `{item, fades}` | Negative or longer than the clip; Resolve older than 21.1 |
| `set_speed` | `item: int`, `percent: float`, `pitch_correction`, `stretch_keyframes: bool \| None`, `ripple: bool = False`, `track`, `track_type: str = "video"` | `{item, speed, duration}` | Negative; Resolve older than 21.1 |
| `convert_to_stereo` | — | Confirmation string | |
| `insert_audio` | `path: str`, `start_offset: int = 0`, `duration: int = 0` (samples) | Confirmation string | File missing; Fairlight page not open |
| `sync_audio` | `clips: list[str]` (media-pool names), `method: "waveform" \| "timecode"`, `channel: "auto" \| "mix" \| int`, `retain_embedded_audio`, `retain_video_metadata` | `{resolve_reported, synced_audio: {clip: synced file}}` | Fewer than 2 clips; nothing synced |
| `transcribe_audio` | `clips: list[str]`, `speaker_detection: bool = False` | `[{clip, transcribed, preview}]` | Studio only |
| `create_subtitles` | `language: str = "auto"`, `preset: "default" \| "teletext" \| "netflix"`, `lines: 1 \| 2`, `chars_per_line` (1–60), `gap` (0–10 frames) | `{resolve_reported, subtitle_track, captions}` | Unsupported language; nothing created |

Notes:

- **The API has no per-parameter mixer.** Clip and track volume, pan, EQ, dynamics, automation and FairlightFX cannot be read or set by any script (verified on live Resolve 21.0: `SetProperty("Volume")` returns False). The scriptable route to a finished mix is: build it once in the Fairlight page, save it as a Fairlight preset, then `apply_fairlight_preset` on each timeline. Levels between clips are handled with `normalize_audio`.
- Loudness targets for `normalize_audio`: −14 LKFS (YouTube, Spotify), −16 (podcasts, Apple), −23 (EBU R128 broadcast), −24 (ATSC A/85). `mode` names come from `fairlight_info`; without it Resolve uses its default.
- Disabling an audio track with `set_track(enabled=False)` mutes it in playback and render.
- `sync_audio` and `create_subtitles` check the result themselves: Resolve's own success flag for both calls is unreliable in both directions, so `resolve_reported` is informational only.
- Caption languages are Resolve's: `auto` `danish` `dutch` `english` `french` `german` `italian` `japanese` `korean` `mandarin_simplified` `mandarin_traditional` `norwegian` `portuguese` `russian` `spanish` `swedish`. Arabic is not among them.
- `voice_isolation`, `transcribe_audio`, `create_subtitles` are Studio features; on the free edition Resolve opens an upgrade dialog that blocks further API calls until it is closed.

### Rendering

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `list_render_presets` | — | Preset names | |
| `list_render_formats` | — | `{format: {extension, codecs: {codec: description}}}` | |
| `render` | `target_dir: str`, `preset: str \| None`, `file_name: str \| None`, `format: str \| None`, `codec: str \| None` | `{job, target_dir}`; rendering starts immediately | Unknown preset; only one of `format`/`codec` given; unsupported format/codec; job could not be queued |
| `render_status` | `job: str` (from `render`) | Resolve's job status dict, e.g. `{JobStatus, CompletionPercentage}` | |
| `stop_render` | — | `"stopped"` or `"nothing rendering"` | |

`format`/`codec` take the keys from `list_render_formats` (e.g. `QuickTime` + `ProRes422HQ`) and are applied after `preset`, so they override it.

### Color grading

`item` is the 1-based index from `list_items` on video `track` (default 1). `node` is 1-based.

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `open_page` | `page: str` (`media`, `cut`, `edit`, `fusion`, `color`, `fairlight`, `deliver`) | `"page: <page>"` | Unknown page |
| `color_info` | `item: int \| None` (default: item under the playhead), `track: int = 1` | `{item, nodes: [{index, label, lut}], version, local_versions, remote_versions, color_group}` | Nothing under the playhead |
| `apply_lut` | `item: int`, `lut_path: str`, `node: int = 1`, `track: int = 1` | Confirmation string | Node out of range; LUT unknown to Resolve |
| `set_cdl` | `item: int`, `slope`, `offset`, `power: list[float]` (R G B), `saturation: float`, `node: int = 1`, `track: int = 1` | `{item, cdl}` | Not 3 values; node out of range |
| `copy_grade` | `source: int`, `targets: list[int]`, `track: int = 1` | Confirmation string | Item not found; empty `targets` |
| `apply_drx` | `path: str` (.drx still), `items: list[int]`, `keyframes: "none" \| "source_timecode" \| "start_frames" = "none"`, `track: int = 1` | Confirmation string | File missing; item not found |
| `add_color_version` | `item: int`, `name: str`, `remote: bool = False`, `track: int = 1` | Confirmation string; the new version becomes current | Name taken |
| `load_color_version` | `item: int`, `name: str`, `remote: bool = False`, `track: int = 1` | Confirmation string | Version not found |
| `export_lut` | `item: int`, `path: str`, `size: 17 \| 33 \| 65 = 33`, `track: int = 1` | Confirmation string | Unsupported size; export rejected |
| `grab_still` | — | Confirmation string | Color page not open |

Notes:

- `set_cdl` defaults are the identity grade (slope 1, offset 0, power 1, saturation 1), so pass only what you change.
- `apply_lut` only accepts LUTs Resolve has already indexed. After copying a new `.cube` into a LUT folder, run *Project Settings → Color Management → Update Lists* first.
- `grab_still` needs the Color page open (`open_page("color")`) and stores the still in the current gallery album as a grade reference. To get the image itself use `view_frame`: Resolve's gallery export only works while the Gallery panel is visible, while the frame export `view_frame` uses works on any page with a viewer.
- `export_lut` needs Resolve 18 or later. Node reads/writes use Resolve 19's node graph when present and fall back to the older per-item calls on earlier versions.

### Fusion (VFX and motion graphics)

`item` is the 1-based index from `list_items` on video `track` (default 1); `comp` is the 1-based composition index on that item (default 1). Nodes are addressed by name (`MediaIn1`, `Blur1`, …).

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `insert_fusion` | `kind: "composition" \| "generator" = "composition"`, `name: str \| None` (generator name) | `{name, start, end}` of the clip inserted at the playhead | Unknown kind; generator not found |
| `create_fusion_clip` | `items: list[int]`, `track: int = 1` | `{name, start, end}` of the new Fusion clip | Item not found; empty `items` |
| `fusion_comps` | `item: int`, `track: int = 1` | Composition names, in index order | |
| `add_fusion_comp` | `item: int`, `import_path: str \| None` (.comp file), `track: int = 1` | `{item, comp, comps}`; `comp` is the new index | File missing; add/import rejected |
| `export_fusion_comp` | `item: int`, `path: str`, `comp: int = 1`, `track: int = 1` | Confirmation string | Comp not found; export rejected |
| `fusion_nodes` | `item: int`, `comp: int = 1`, `track: int = 1` | `[{name, type, inputs: {input: source node}, animated: [input]}]` | Comp not found |
| `fusion_inputs` | `item: int`, `node: str`, `filter: str \| None`, `comp`, `track` | `[{id, name, type, value, animated, source}]` | Node not found |
| `add_fusion_node` | `item: int`, `tool_type: str`, `name: str \| None`, `connect_from: str \| None`, `input: str = "Input"`, `comp`, `track` | `{name, type, connected}` | Unknown type; name taken; source not found or not connectable |
| `connect_fusion_nodes` | `item: int`, `target: str`, `source: str \| None`, `input: str = "Input"`, `comp`, `track` | Confirmation string; `source: null` disconnects | Node not found; input not connectable |
| `delete_fusion_node` | `item: int`, `node: str`, `comp`, `track` | Confirmation string | Node not found |
| `set_fusion_input` | `item: int`, `node: str`, `input: str`, `value` **or** `keyframes: {frame: value}`, `comp`, `track` | `{node, input, value}` or `{node, input, keyframes}` | Neither or both given; unknown input; input cannot be animated |

Notes:

- `tool_type` is Fusion's registry id: `Blur`, `Glow`, `Transform`, `Merge`, `TextPlus`, `ColorCorrector`, `BrightnessContrast`, `Background`, `FastNoise`, `EllipseMask`, `RectangleMask`, `PolylineMask`, `Tracker`, `DeltaKeyer`, `Shape3D`, `Renderer3D`, and so on. Fusion names new nodes itself (`Blur1`, `Blur2`) unless you pass `name`.
- Common inputs: `Input` (main image), `Background`/`Foreground` (Merge), `EffectMask` (limits a node's effect to a mask).
- Frames in `keyframes` are relative to the composition, starting at 0. Numbers get a Bézier spline and points (`Center`, `[x, y]` in 0–1 image space) get a motion path. Adding more keyframes later extends the same curve. Text inputs can only be set statically.
- Every structural change (add, connect, delete) runs under `comp.Lock()`. Value and keyframe writes deliberately run outside it: Resolve renders ignore values written under the lock. Each write is one undo step in Resolve.

Example: a blur limited to an elliptical area, then an animated push-in:

```
add_fusion_comp(item=1)
add_fusion_node(item=1, tool_type="Blur", connect_from="MediaIn1")
add_fusion_node(item=1, tool_type="EllipseMask", name="Area")
connect_fusion_nodes(item=1, target="Blur1", source="Area", input="EffectMask")
add_fusion_node(item=1, tool_type="Transform", name="Push", connect_from="Blur1")
connect_fusion_nodes(item=1, target="MediaOut1", source="Push")
set_fusion_input(item=1, node="Blur1", input="XBlurSize", value=8)
set_fusion_input(item=1, node="Push", input="Size", keyframes={"0": 1.0, "120": 1.15})
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RESOLVE_SCRIPT_API` | platform default | Resolve `Developer/Scripting` directory |
| `RESOLVE_SCRIPT_LIB` | platform default | Path to `fusionscript.so` / `fusionscript.dll` |
| `DAVINCI_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

## Logging

The server writes one JSON object per line to stderr (stdout is the MCP protocol channel). Every tool call is logged with its arguments, duration and outcome:

```json
{"ts": "2026-09-24T14:39:30.637+00:00", "level": "INFO", "logger": "davinci_mcp", "msg": "tool call", "tool": "add_marker", "args": {"frame": 24, "color": "Red"}, "outcome": "ok", "duration_ms": 3.1}
```

`outcome` is `ok` (INFO), `error` (WARNING, with `error` holding the message returned to the client) or `crash` (ERROR, with the traceback in `exc`).

## Development

The tests run against an in-memory fake of Resolve's scripting API, so Resolve does not need to be installed:

```bash
uv run --no-project --with "mcp>=2" --with pytest pytest
```

## License

MIT
