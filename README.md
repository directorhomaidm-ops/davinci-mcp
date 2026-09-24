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

All frames are absolute timeline frames unless noted. Item and track indexes are 1-based. A failed call returns an MCP tool error with the reason. Common ones: `cannot connect — is DaVinci Resolve running?`, `no project open`, `no timeline open`.

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

- `append_clips` appends to the end of the current timeline in the order given. Without `start_frame`/`end_frame` whole clips are used and `track` is ignored. With either set, each clip is trimmed to `start_frame`–`end_frame` (clip-relative; defaults `0` and the clip's last frame, passed to Resolve as `startFrame`/`endFrame`) and placed on `track`.
- `set_item_properties` works on video tracks only. Typical keys: `ZoomX` `ZoomY` `Pan` `Tilt` `RotationAngle` `Opacity` `CropLeft` `CropRight` `CropTop` `CropBottom` `FlipX` `FlipY` `CompositeMode`.
- `insert_title` inserts at the playhead. `text` is applied only when `fusion=True` and the Fusion title has a `Template` tool; `text_set` says whether it was.
- `add_marker` takes `frame` relative to the timeline start, not an absolute frame. `note` is used as the marker name too (or `frame <n>` when empty).

### Rendering

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `list_render_presets` | — | Preset names | |
| `render` | `target_dir: str`, `preset: str \| None`, `file_name: str \| None` | `{job, target_dir}`; rendering starts immediately | Unknown preset; job could not be queued |
| `render_status` | `job: str` (from `render`) | Resolve's job status dict, e.g. `{JobStatus, CompletionPercentage}` | |

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
