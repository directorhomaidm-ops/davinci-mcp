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
- `set_item_properties` works on video tracks only. Typical keys: `ZoomX` `ZoomY` `Pan` `Tilt` `RotationAngle` `Opacity` `CropLeft` `CropRight` `CropTop` `CropBottom` `FlipX` `FlipY` `CompositeMode`.
- `insert_title` inserts at the playhead. `text` is applied only when `fusion=True` and the Fusion title has a `Template` tool; `text_set` says whether it was.
- `add_marker` takes `frame` relative to the timeline start, not an absolute frame. `note` is used as the marker name too (or `frame <n>` when empty).

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
| `grab_still` | `export_dir: str \| None`, `prefix: str = "still"`, `format: str = "png"` | `{grabbed, exported_to}` | Color page not open; unsupported format; export failed |

Notes:

- `set_cdl` defaults are the identity grade (slope 1, offset 0, power 1, saturation 1), so pass only what you change.
- `apply_lut` only accepts LUTs Resolve has already indexed. After copying a new `.cube` into a LUT folder, run *Project Settings → Color Management → Update Lists* first.
- `grab_still` needs the Color page open (`open_page("color")`). The still goes into the current gallery album; with `export_dir` it is also written as an image (`dpx` `cin` `tif` `jpg` `png` `ppm` `bmp` `xpm`).
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
