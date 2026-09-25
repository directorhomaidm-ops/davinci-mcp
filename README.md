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
| `open_project` | `name: str` | `"opened: <name>"` | Project cannot be opened; current project could not be saved |
| `create_project` | `name: str` | `"created: <name>"`; the new project becomes current | Name taken; current project could not be saved |

Notes:

- `open_project`, `create_project`, `switch_database` and the cloud tools save the current project first: `CreateProject` replaces the current project, and an unsaved one is lost without warning. The default "Untitled Project" is never saved (Resolve cannot save it from a script: `False` in the GUI, an endless hang headless), so work in a named project.

### Project management and collaboration

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `project_browser` | `folder: str \| None` (`"/"`, `"Clients/Acme"`) | `{database, folder, folders, projects, current_project}` | Folder not found (returns to the root) |
| `create_project_folder` | `name: str` | Confirmation string | Name taken |
| `rename_project` | `new_name: str` | Confirmation string | Name taken |
| `delete_project` | `name: str` | Confirmation string | It is the open project; not found; refused |
| `list_databases` | — | `{current, databases: [{DbType, DbName, IpAddress?}]}` | |
| `switch_database` | `name: str`, `db_type: "Disk" \| "PostgreSQL" \| None` | `{database, projects}` | Not found; ambiguous; save or switch failed |
| `create_cloud_project` | `name: str`, `media_path: str`, `collaboration: bool = True`, `sync: "none" \| "proxy_only" \| "proxy_and_original"`, `camera_access: bool = False` | Confirmation string | Folder missing; not signed in; name taken |
| `load_cloud_project` | `name: str`, `media_path: str`, `sync` | Confirmation string | Not found or not shared |
| `refresh_collaboration` | — | `{refreshed, stale_bins}` | Not a collaboration project |
| `duplicate_timeline` | `new_name: str`, `timeline: str \| None` | `{copy, of, current}` | Name taken; not found |
| `rename_timeline` | `new_name: str`, `timeline: str \| None` | Confirmation string | Name taken; not found |
| `delete_timelines` | `names: list[str]` | Confirmation string | Not found; would delete every timeline |
| `review_notes` | `status: "open" \| "resolved" \| None` | `[{frame, timecode, color, name, note, duration, author, status}]` | |
| `add_review_note` | `frame: int`, `note: str`, `author: str \| None`, `color: str = "Red"`, `duration: int = 1` | The note | Unknown color; a marker already there |
| `resolve_review_note` | `frame: int`, `reopen: bool = False` | The note | No marker there |
| `delete_markers` | `frame: int` **or** `color: str` (`"All"` for every marker) | Confirmation string | No marker there; unknown color |
| `export_review_notes` | `path: str` (`.csv` or `.md`), `status` | `{timeline, path, notes}` | Wrong extension; folder missing |

Notes:

- Review notes are ordinary timeline markers, so every editor sees them in Resolve's marker index, in collaboration projects too. `add_review_note` stores the author and an open/resolved status in the marker's hidden `customData`; resolving turns the marker green (a marker's color can only change by replacing it, and the original is put back if that fails). Frames are relative to the timeline start, like `add_marker`; timecodes are left empty on drop-frame timelines rather than guessed.
- `duplicate_timeline` keeps the current timeline current: Resolve's `DuplicateTimeline` silently switches to the copy.
- `delete_project` refuses the open project and retries once, because Resolve's first attempt on a recently open project fails.
- Blackmagic Cloud: only creating and opening cloud projects is scriptable. Listing cloud projects and inviting or removing collaborators can only be done in Resolve's UI.
- `project_browser` navigation changes the project manager's current folder (where `list_projects`, `create_project` and `import_project` act). A path that does not exist returns to the root rather than leaving you part-way down it.

### Media pool

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `browse_storage` | `path: str \| None` | Without `path`: `{volumes}`; with it: `{path, folders, files}` | Folder not found |
| `import_media` | `paths: list[str]` (relative paths resolve against the server's working directory), `bin: str \| None` | Imported clip names | Any path missing; bin not found; import rejected |
| `import_image_sequence` | `pattern: str` (e.g. `/renders/shot_%04d.exr`), `start: int`, `end: int`, `bin: str \| None` | Confirmation string with the clip name | First frame missing; `end < start` |
| `list_clips` | — | `[{folder, name, frames}]` for every clip, recursing into subfolders; `folder` is `/` for the root | |
| `clip_info` | `clip: str` | `{name, properties, metadata, color, flags, markers}` (empty values dropped) | Clip not found |
| `create_bin` | `name: str`, `parent: str = "/"` | Confirmation string | Parent not found; name taken |
| `move_clips` | `clips: list[str]`, `bin: str` | Confirmation string | Clip or bin not found |
| `delete_clips` | `clips: list[str]` | Confirmation string | Clip not found |
| `tag_clips` | `clips: list[str]`, `color: str \| None` (`""` clears), `flag: str \| None`, `clear_flags: bool`, `metadata: dict \| None` | `[{clip, color, flags}]` | Unknown color or flag; nothing to change; metadata not kept |
| `relink_clips` | `clips: list[str]`, `folder: str` | Confirmation string | Folder missing; no matching files |
| `link_proxy` | `clip: str`, `proxy_path: str \| None` (omit to unlink) | Confirmation string | File missing; proxy does not match the clip |
| `replace_clip` | `clip: str`, `path: str` | Confirmation string | File missing |
| `export_metadata` | `path: str` (.csv), `clips: list[str] \| None` (all when omitted) | Confirmation string | Export failed |

Notes:

- Clips are addressed by name, bins by path (`"Footage/Day 1"`). Resolve only imports into the current bin, so `bin` switches to it and back.
- `tag_clips` colors are Resolve's 16 clip colors: `Orange` `Apricot` `Yellow` `Lime` `Olive` `Green` `Teal` `Navy` `Blue` `Purple` `Violet` `Pink` `Tan` `Beige` `Brown` `Chocolate`. Metadata is read back after writing: some fields (e.g. `Reel Name` when the project derives reel names automatically) are accepted by Resolve and then dropped, and that is reported as an error.
- Proxies and optimized media cannot be generated through the API, only linked once they exist.

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
| `delete_items` | `items: list[int]`, `ripple: bool = False`, `track`, `track_type` | Confirmation string; switches to the Edit page for the delete and back | Index out of range |
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

### Sound effects and music

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `generate_voiceover` | `text: str` (≤ 350 chars), `voice: str = "Female 1"`, `speed` (−10…10), `pitch` (−2…2), `variation` (0…1), `custom_voice_file`, `file_name`, `add_to_timeline: bool = False`, `audio_track: int = 0` | `{clip, voice, added_to_timeline}` | Text too long; out-of-range values; Extras package missing; Resolve older than 21 |
| `classify_audio` | `clips: list[str]` **or** `bin: str` | `[{clip, category, subcategory}]` | Resolve older than 21 |
| `find_audio` | `category`, `subcategory`, `name`, `bin` (any of them) | `[{bin, clip, category, subcategory}]` | |
| `generate_sound` | `kind: "tone" \| "pop" \| "beeps" \| "silence" \| "noise"`, `path: str` (.wav), `seconds`, `level_db: float = -20`, `frequency: float = 1000`, `count: int = 3`, `fps`, `channels: 1 \| 2 = 2`, `sample_rate: int = 48000`, `bin: str \| None` | `{path, kind, seconds, level_db, clip?}` | Unknown kind; level above 0 dBFS; not .wav |
| `detect_beats` | `clip: str` **or** `path: str`, `sensitivity: float = 1.4`, `min_bpm: float = 60`, `max_bpm: float = 200` | `{file, bpm, beats, hits, duration}` (seconds) | Not a PCM WAV; shorter than 2 s; no rhythm found |
| `mark_beats` | `item: int` (on audio `track`), `track: int = 1`, `every: int = 1`, `color: str = "Yellow"`, `hits: bool = False`, `sensitivity`, `max_markers: int = 500` | `{item, bpm, markers, skipped, first_frames}` | No source clip; unknown color |

Notes:

- Resolve's API has no music generation, beat detection or ducking. Beats are detected by this server from WAV audio (PCM 16/24/32-bit, pure Python): an autocorrelation tempo estimate refined by fitting every detected beat, which on synthetic tracks lands within about 4 ms of each true beat over 3 minutes (1.4 s to analyse). For MP3/AAC/etc. render or export the music as WAV first.
- `mark_beats` workflow: put the music on an audio track, `mark_beats(item, every=4)` for one marker per bar in 4/4 (or `hits=True` for accents and drops), then cut on the markers. It reads the clip's trim through `GetSourceStartTime` (seconds), since audio items' frame counts follow the WAV's import-time rate.
- `generate_sound` writes 24-bit WAV: `tone` for bars and tone (1 kHz at −20 dBFS by default), `pop` for a 2-pop (one frame at the timeline rate, placed 2 s before program start), `beeps` for a countdown, `noise` for a room-tone placeholder at `level_db` RMS, `silence`.
- `generate_voiceover` needs Resolve 21 and the AI Speech Generator Extras package. When the package is missing Resolve returns an explanatory string instead of failing, which the tool reports as the error.
- `classify_audio` labels clips (e.g. Dialogue, Music, Effects, with subcategories) so `find_audio` can search a sound library; cleared clips read `Uncategorized` and are treated as unclassified.

### Rendering

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `list_render_presets` | — | Preset names | |
| `list_render_formats` | — | `{format id: {name, codecs: {codec id: description}}}` | |
| `render` | `target_dir: str`, `preset`, `file_name`, `format`, `codec`, `mark_in`, `mark_out`, `width`, `height`, `frame_rate`, `quality`, `video`, `audio`, `individual_clips: bool = False`, `settings: dict \| None`, `subtitles: "burn_in" \| "separate_file" \| "embedded" \| None`, `start: bool = True` | `{job, target_dir, started}` | Unknown preset; format without codecs; unsupported format/codec; range outside the timeline; settings rejected |
| `render_status` | `job: str` | Resolve's status plus `done`, `output` and (when done) `output_exists` | Job not found |
| `render_queue` | — | Every queued job with its settings and status | |
| `start_render` | `jobs: list[str] \| None` (whole queue when omitted) | Confirmation string | Nothing started |
| `stop_render` | — | `"stopped"` or `"nothing rendering"` | |
| `delete_render_jobs` | `jobs: list[str] \| None` (all when omitted) | Confirmation string | A render is running; unknown job |
| `save_render_preset` | `name: str` | Confirmation string | Name taken |

Notes:

- `format`/`codec` take the **ids** from `list_render_formats` (e.g. `mov` + `ProRes422HQ`, `mp4` + `H264`), not the names shown in the Deliver page: Resolve rejects the names. A format with no codecs (e.g. `wav`) cannot be selected through the API; render audio-only with `video=False` on a format that has codecs, or from a saved preset.
- `mark_in`/`mark_out` are absolute timeline frames, both inclusive. Resolve silently clamps values below the timeline start instead of refusing them, so the tool rejects them.
- Settings not passed are inherited from the Deliver page's current state (even from an audio-only preset used earlier). Pass a `preset` to start from a known base; `settings` takes any other documented key (`AudioCodec`, `AudioBitDepth`, `AudioSampleRate`, `ColorSpaceTag`, `GammaTag`, `ExportAlpha`, `AlphaMode`, `EncodingProfile`, `MultiPassEncode`, `NetworkOptimization`, `PixelAspectRatio`, `UniqueFilenameStyle`).
- `render_status` decides `done` from `CompletionPercentage` and `Error`, because `JobStatus` is a translated display string ("Complete", "Concluso", …), and then checks the output file exists.
- Do not close or delete a project while it is rendering: Resolve's render pipeline wedges until restart.

### Interchange and project

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `export_timeline` | `path: str`, `format: str` | `{timeline, format, path, bytes}` | Unknown format; format not in this Resolve; folder missing; no file written |
| `import_timeline` | `path: str`, `name: str \| None`, `import_source_clips: bool = True`, `source_clips_path: str \| None` | `{timeline, renamed_by_file}`; the new timeline becomes current | File missing; name taken; no new timeline created |
| `save_project` | — | Confirmation string | Untitled Project (cannot be saved from a script); save failed |
| `export_project` | `path: str` (.drp added if missing), `with_stills_and_luts: bool = True` | `{project, path, bytes}` | Export failed |
| `import_project` | `path: str`, `name: str \| None` | Confirmation string | File missing; name in use |

Notes:

- `export_timeline` formats: `aaf`, `aaf_existing` (Avid, Pro Tools), `fcpxml` (newest this Resolve writes) or `fcpxml_1_3` … `fcpxml_1_11`, `fcp7_xml` (Premiere), `otio`, `edl`, `edl_cdl`, `edl_sdl`, `edl_missing_clips`, `drt`, `csv`, `tab`, `hdr10_a`, `hdr10_b`, `dolby_vision_2_9`, `dolby_vision_4_0`, `dolby_vision_5_1`. Measured limits: Resolve's EDL carries video events only (no audio) with every source as reel `AX`; OTIO and DRT exports drop markers. For round trips with audio prefer `otio`, `aaf` or `fcp7_xml`.
- `import_timeline`: FCP7 XML and DRT name the timeline from the file, so `name` may be ignored; when an FCP7 XML's sequence name matches an existing timeline Resolve returns that timeline instead of a new one, which is reported as an error.
- Project archiving (`.dra` with media) is deliberately not offered: `ProjectManager.ArchiveProject` crashed Resolve in every measured call that included media and did nothing without it. Use `export_project` plus your own media copy.

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
| `magic_mask` | `item: int`, `direction: "forward" \| "backward" \| "both" = "both"`, `regenerate: bool = False`, `track: int = 1` | Confirmation string | No subject clicked in the UI yet; Resolve older than 18.5 |
| `grab_still` | — | Confirmation string | Color page not open |

Notes:

- `set_cdl` defaults are the identity grade (slope 1, offset 0, power 1, saturation 1), so pass only what you change.
- `apply_lut` and `set_node_lut` take a path relative to Resolve's LUT folders (`Film/Kodak.cube`) or an absolute path to any `.cube` file. Resolve only resolves LUTs inside its master LUT folder, so an outside file is copied to `<master LUT folder>/davinci-mcp/`, the LUT list is refreshed, and the returned path is the one Resolve uses. Set `RESOLVE_LUT_DIR` if your master folder is not the default.
- Grade writes (`set_cdl`, `copy_grade`, `apply_drx`, versions, LUTs, node bypass, reset, color groups) switch to the Color page and back: Resolve refuses them on other pages.
- `grab_still` needs the Color page open (`open_page("color")`) and stores the still in the current gallery album as a grade reference. To get the image itself use `view_frame`: Resolve's gallery export only works while the Gallery panel is visible, while the frame export `view_frame` uses works on any page with a viewer.
- `magic_mask` only tracks: Magic Mask needs clicks on the subject and the API cannot place them. Seed it once in the Color page (select the clip, open the Magic Mask palette, click the subject), then call `magic_mask`. Power Windows and their tracker have no API at all. Studio only.
- `export_lut` needs Resolve 18 or later and switches to the Color page for the export (Resolve refuses it on other pages), then back. Node reads/writes use Resolve 19's node graph when present and fall back to the older per-item calls on earlier versions.

### Advanced grading

Graph tools take one target: `item` (with `layer` and `track`), `group` (a color group, `stage` `"pre"` or `"post"` clip), or `timeline_grade=True` (the timeline-wide grade, Resolve 21.1+).

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `node_graph` | target | `{graph, nodes: [{index, label, tools, lut, cache}]}` | Not exactly one target; bad stage |
| `set_node_lut` | `node: int`, `lut_path: str`, target | Confirmation string with the path Resolve used | Node out of range; LUT unknown |
| `set_node_enabled` | `node: int`, `enabled: bool`, target | Confirmation string | Node out of range |
| `reset_grade` | target | Confirmation string | Reset refused |
| `apply_drx_to` | `path: str`, `group` + `stage` or `timeline_grade=True`, `keyframes = "none"` | Confirmation string | File missing |
| `color_groups` | — | `[{group, clips: [{track, item, name}]}]` | — |
| `create_color_group` / `delete_color_group` | `name: str` | Confirmation string | Name exists / not found |
| `assign_color_group` | `items: list[int]`, `group: str \| None` (None removes), `track: int = 1` | `{group, items}` | Group not found |
| `apply_arri_cdl_lut` | `items: list[int]`, `track: int = 1` | `{applied}` | Clip has no ARRI metadata |
| `color_cache` | `items: list[int]`, `enabled: bool = True`, `track: int = 1` | `{items, color_cache}` | — |
| `gallery_albums` | — | `{still_albums, powergrade_albums}` with still labels | — |
| `import_stills` | `paths: list[str]` (.drx/.dpx/…), `album: str \| None`, `powergrade: bool = False` | `{album, imported}` | File or album not found |
| `validate_dctl` | `source: str` | `{valid, diagnostic}` | Resolve older than 21.1 |

Notes:

- `set_node_enabled` cannot be read back: Resolve has no getter for a node's bypass state.
- `validate_dctl` misreads DCTL written on a single line; keep the normal multi-line layout.
- `powergrade=True` without `album` creates a new PowerGrade album; PowerGrades are shared across projects.

### Color management and HDR

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `color_management_info` | `timeline: bool = False` | `{scope, color, hdr}` (+ `uses_own_settings` for a timeline) | |
| `apply_color_preset` | `preset: str`, `timeline: bool = False` | `{preset, scope, applied}` | Unknown preset; a value not applied |
| `set_color_management` | `settings: dict`, `timeline: bool = False` | `{scope, applied}` | Not a color/HDR key; a value not applied |
| `set_hdr` | `mastering_nits: int`, `dolby_vision: "2.9" \| "4.0" \| "off"`, `dolby_tuning: str`, `dolby_master_display: str`, `hdr10_plus: bool`, `timeline: bool = False` (any of them) | `{scope, applied}` | Bad value; nothing to change; a value not applied |
| `set_clip_color_space` | `clips: list[str]`, `color_space`, `gamma`, `idt` (any of them) | `[{clip, <property>: value}]` | Value not applied |
| `analyze_dolby_vision` | `items: list[int] \| None` (whole timeline when omitted), `blend_shots: bool = False`, `track: int = 1` | Confirmation string | Dolby Vision off; Studio only |

Notes:

- Presets: `yrgb` (unmanaged DaVinci YRGB), `rcm_sdr` / `rcm_hdr` (DaVinci color managed, automatic SDR or HDR), `rcm_custom` (color managed, spaces set by hand), `aces_cct`, `aces_cc`. They use only values documented in Resolve's API stub.
- `set_color_management` takes Resolve's setting keys: `colorScienceMode`, `isAutoColorManage`, `rcmPresetMode`, `separateColorSpaceAndGamma`, `colorSpaceInput` / `colorSpaceTimeline` / `colorSpaceOutput` (and their `…Gamma`), `timelineWorkingLuminanceMode` (e.g. `"SDR 100"`, `"HDR 1000"`), `inputDRT` / `outputDRT` (`None`, `Simple`, `Luminance Mapping`, `DaVinci`, `Saturation Preserving`, `RED IPP2`), `useInverseDRT`, `colorSpaceOutputGamutMapping`, `graphicsWhiteLevel`, `colorAcesIDT` / `colorAcesODT` and the rest listed by `color_management_info`. Color space names must match Project Settings > Color Management exactly (e.g. `Rec.709 Gamma 2.4`, `Rec.2100 ST2084`, `DaVinci WG/Intermediate` when color space and gamma are combined).
- Writes are applied in dependency order (color science, then automatic/preset mode, then color spaces, then HDR), whatever order they are given in, and every key is read back: Resolve can report success without applying a value, and color spaces are locked while automatic color management is on (`isAutoColorManage` = 1). Anything not applied is an error that lists what was.
- `timeline=True` gives the current timeline its own settings (`useCustomSettings`) and changes only that timeline, e.g. an HDR deliverable timeline in an SDR project.
- A typical HDR10 setup: `apply_color_preset("rcm_custom")`, `set_color_management({"colorSpaceTimeline": "DaVinci WG/Intermediate", "colorSpaceOutput": "Rec.2100 ST2084", "timelineWorkingLuminanceMode": "HDR 1000"})`, `set_hdr(mastering_nits=1000)`, then `export_timeline(path, "hdr10_a")` for the metadata. Dolby Vision: `set_hdr(dolby_vision="4.0")`, `analyze_dolby_vision()`, `export_timeline(path, "dolby_vision_4_0")`.
- `set_clip_color_space` needs a color-managed project for `Input Color Space` / `Input Gamma`, and an ACES project for `IDT`.

### Transcripts, subtitles and titles

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `get_transcript` | `clip: str`, `query: str \| None`, `words: bool = False` | `{clip, language, complete, segments: [{start, end, start_seconds, end_seconds, speaker, text, words?}]}` | Not transcribed |
| `export_transcript` | `clip: str`, `path: str` (`.srt`, `.vtt`, `.txt`, `.json`), `speakers: bool = True`, `rtl: bool = False` | `{clip, path, segments, language}` | Not transcribed; before Resolve 21.1; wrong extension |
| `write_subtitles` | `path: str` (`.srt`, `.vtt`), `captions: [{start, end, text}]`, `fps: float \| None`, `rtl: bool = False` | `{path, captions}` | Empty text; end before start; overlapping captions; bad time |
| `list_titles` | — | `[{track, item, name, start, end, texts}]` | |
| `set_title_text` | `item: int`, `text`, `font`, `style`, `size`, `color: [r, g, b]` (0–1), `node: str \| None`, `track: int = 1` | `{item, node, set}` | Not a Fusion title; several Text+ nodes without `node`; bad color; nothing to change |

Notes:

- **What the API cannot do with subtitles:** read or change the text or timing of subtitle items, import an SRT onto a subtitle track, or style subtitles. Subtitles are therefore produced as files and imported in Resolve with *File > Import > Subtitle*.
- Subtitle workflow for any language, including Arabic (not one of Resolve's auto-caption languages): `transcribe_audio` → `get_transcript` → translate the segments → `write_subtitles(path, captions, rtl=True)` → import the file. `rtl=True` marks each line right-to-left so punctuation sits on the correct side.
- `get_transcript` needs Resolve 21.1 for the full transcript with word timing and speakers (`MediaPoolItem.GetTranscription`); earlier versions only expose a preview, reported with `complete: false` when Resolve truncated it. Times are given both as Resolve's source timecodes and as seconds from the clip start; `query` finds where something is said.
- `write_subtitles` times can be seconds, `"HH:MM:SS,mmm"`, or `"HH:MM:SS:FF"` timecodes (converted at `fps`, default the current timeline's).
- `set_title_text` edits Fusion titles (Text+), in any language; Resolve's standard titles have no scripting access. Color is Text+'s first shading element (`Red1`/`Green1`/`Blue1`).
- `render(..., subtitles="burn_in" | "separate_file" | "embedded")` delivers the timeline's subtitle track on Resolve 21+. It is refused on earlier versions, where these settings were measured to have no effect; check the output.

### Keyframes and multicam

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `animate_clip` | `item: int`, `zoom: {frame: float}`, `position: {frame: [x, y]}`, `rotation: {frame: degrees}`, `track: int = 1` | `{item, node, keyframes}` | Nothing given; frame outside the clip; zoom ≤ 0 |
| `list_keyframes` | `item: int`, `comp: int = 1`, `track: int = 1` | `[{node, input, keyframes: [{frame, value}]}]` | No comp |
| `clear_keyframes` | `item: int`, `node: str`, `input: str`, `comp`, `track` | `{node, input, value}` | Not animated; unknown input |
| `set_color_keyframe_mode` | `mode: "all" \| "color" \| "sizing"` | Confirmation string | Unknown mode |
| `create_multicam` | `clips: list[str]`, `name`, `sync: "timecode" \| "in" \| "out" \| "audio" \| "marker"`, `audio_mode`, `angle_names`, `audio_channel`, `split_at_gaps`, `use_full_extents`, `create_bin`, `same_camera`, `start_timecode`, `frame_rate` | Names of the multicam clips created | Fewer than 2 clips; option for another sync mode; Resolve older than 21.1 |
| `auto_align_clips` | `video_items`, `audio_items: list[int]`, `sync: "timecode" \| "waveform"`, `waveform_track: int \| "mix" \| "auto"`, `video_track`, `audio_track` | Confirmation string | Waveform without audio items; fewer than 2 items; Resolve older than 21.1 |
| `smart_switch` | `item: int`, `min_edit_seconds`, `change_delay_seconds`, `wide_angle: "auto" \| name \| None`, `wide_frequency`, `wide_for_intro_outro`, `wide_for_silence`, `video_only`, `quality`, `analysis`, `track` | Confirmation string | Out-of-range timing; not a multicam clip with speech; Studio only |
| `flatten_multicam` | `item: int`, `grade: "copy" \| "angle" = "copy"`, `track: int = 1` | Confirmation string | Not a multicam clip |

Notes:

- `animate_clip` frames count from the clip's first frame (0) and are converted to the clip's Fusion comp frame numbers. The motion lives in a Fusion `Transform` node named `Motion`, the route measured to render on live Resolve: Resolve's Edit-page keyframe methods are not in its current API reference. Calling it again adds keyframes to the same node. For any other animated value use `set_fusion_input(..., keyframes=...)`; `list_keyframes` shows everything animated in a clip.
- `set_color_keyframe_mode` sets what a Color page keyframe records (switching to the Color page and back): the grade keyframes themselves are made in the Color page, which the API cannot do.
- Multicam needs Resolve 21.1: `create_multicam` → `append_clips([name])` → `smart_switch` (automatic cuts to whoever is speaking) or cutting angles in the UI → optionally `flatten_multicam`. Creation and flattening were validated with renders on 21.1 by others; Smart Switch was not (it returned False on silent test cards), so treat it as experimental and check the result with `view_frame`. Switching a single cut to another angle has no API.
- `auto_align_clips` syncs clips already on the timeline (e.g. before building a multicam by hand). Select every video clip AND its linked audio: Resolve moves only what is selected, and waveform alignment of video alone returned False.

### Visual effects and transitions

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `list_transitions` | `track: int = 1`, `track_type: str = "video"` | `[{index, name, start, end, duration, between}]` | |
| `transition_all_cuts` | `type: str = "Cross Dissolve"`, `duration: int \| None = 12`, `alignment`, `category`, `track`, `track_type` | `{cuts, added, skipped_existing, failed_no_handles}` | Bad option; no transition could be added; Resolve older than 21.1 |
| `remove_transitions` | `items: list[int] \| None` (all when omitted), `track`, `track_type` | Confirmation string | An index that is not a transition |
| `letterbox` | `aspect: float \| None = 2.39`, `item: int \| None`, `track: int = 1` | `{target, aspect, resolution, bounds}` | Aspect outside 0.2–10; Resolve older than 21.1 |
| `picture_in_picture` | `item: int`, `scale: float = 0.35`, `corner: str = "top_right"`, `margin: float = 0.04`, `track: int = 2` | `{item, set}` | Out-of-range values; unknown corner |
| `split_screen` | `left: int`, `right: int`, `left_track: int = 2`, `right_track: int = 1`, `gap: float = 0` | `{half_width, left, right}` | Gap out of range |
| `vignette` | `item: int`, `amount: float = 0.5`, `size: float = 0.85`, `softness: float = 0.35`, `track: int = 1` | `{item, nodes, set}` | Out-of-range values; clip already has one |
| `camera_shake` | `item: int`, `amount: float = 0.01`, `every: int = 2`, `seed: int = 1`, `track: int = 1` | `{item, node, keyframes, seed, zoom}` | Out-of-range values |

Notes:

- `transition_all_cuts` finds every cut (a clip ending exactly where the next starts), skips cuts already covered by a transition, and reports cuts whose clips lack handles instead of stopping (Resolve 21.1 `AddTransition`). `remove_transitions` deletes transition items only, on the Edit page (switching to it and back); the clips stay in place.
- `letterbox` uses Resolve's own output blanking (21.1), exact to the pixel: bars for aspects wider than the frame, pillarbox bars for narrower ones. For one clip it first turns off the clip's use of the timeline's blanking, without which Resolve refuses the override (measured). `aspect=None` removes it.
- `picture_in_picture` and `split_screen` set the clips' Edit-page Zoom, Position and Crop, so they work on any version and stay adjustable in the Inspector. Put the inset or second clip on a track above.
- `vignette` and `camera_shake` are built in the clip's Fusion comp. `camera_shake` keyframes the `Motion` Transform (shared with `animate_clip`) with seeded random offsets plus a matching zoom so no edge shows; it leaves an already animated zoom alone. `vignette`'s node input names are not yet confirmed on a live Resolve; the live check covers them.

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
| `link_mask_to_tracker` | `item: int`, `mask: str`, `tracker: str`, `tracker_index: int = 1`, `offset: [dx, dy] = [0, 0]`, `unlink: bool = False`, `comp`, `track` | `{node, expression}` | Not a Tracker; no Center input; track not run yet; unknown tracker index |
| `set_fusion_input` | `item: int`, `node: str`, `input: str`, `value` **or** `keyframes: {frame: value}`, `comp`, `track` | `{node, input, value}` or `{node, input, keyframes}` | Neither or both given; unknown input; input cannot be animated |

Notes:

- `tool_type` is Fusion's registry id: `Blur`, `Glow`, `Transform`, `Merge`, `TextPlus`, `ColorCorrector`, `BrightnessContrast`, `Background`, `FastNoise`, `EllipseMask`, `RectangleMask`, `PolylineMask`, `Tracker`, `DeltaKeyer`, `Shape3D`, `Renderer3D`, and so on. Fusion names new nodes itself (`Blur1`, `Blur2`) unless you pass `name`.
- Common inputs: `Input` (main image), `Background`/`Foreground` (Merge), `EffectMask` (limits a node's effect to a mask).
- Frames in `keyframes` are relative to the composition, starting at 0. Numbers get a Bézier spline and points (`Center`, `[x, y]` in 0–1 image space) get a motion path. Adding more keyframes later extends the same curve. Text inputs can only be set statically.
- Every structural change (add, connect, delete) runs under `comp.Lock()`. Value and keyframe writes deliberately run outside it: Resolve renders ignore values written under the lock. Each write is one undo step in Resolve.

Tracking in Fusion: add a `Tracker` (`add_fusion_node(item=1, tool_type="Tracker", connect_from="MediaIn1")`), place its pattern on the feature and press Track Forward in the Fusion page (the API cannot run the tracker), then `link_mask_to_tracker(item=1, mask="Area", tracker="Tracker1")` makes the mask follow it. The link is the Fusion expression `Tracker1.TrackedCenter1`; it works for any node with a `Center` (masks, Text+, Transform). **Experimental:** not yet confirmed on a live Resolve; if the tracker's input has another name there, the error lists the tracker's actual point inputs.

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

### Automatic media organization

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `auto_organize` | `by: str \| list[str] = "type"` (type, date, camera, resolution, fps, extension, folder, category), `bin`, `into = "/"`, `dry_run = False`, `color = False` | `{by, clips, already_in_place, plan: {bin: [clips]}, moved, colored?}` | Unknown key; MoveClips refused |
| `color_code` | `bin`, `colors: dict` (type -> clip color) | `{colored, colors}` | Unknown color |
| `find_unused` | `bin`, `move_to: str \| None` | `{unused: [{name, bin, type}], moved_to?}` | — |
| `find_duplicates` | `bin`, `remove = False` | `{same_file, same_name_and_size, removed?}` | DeleteClips refused |
| `find_offline` | `search: str \| None` (folder), `bin`, `max_files = 200000` | `{offline, relinked?, still_offline?, files_searched?}` | Search folder missing |
| `clean_bins` | `bin = "/"` | `{deleted: [bins]}` | — |

Notes:

- `by=["type", "date"]` nests bins (`Video/2026-09-24`). `date` is the media file's date on disk, `folder` its folder on disk, `category` the audio classification (`classify_audio`). Clips already in the right bin stay; run with `dry_run=True` to see the plan first.
- Clips are handled by identity (`GetUniqueId`), not by name, so same-named clips from different cards are kept apart.
- `find_unused` scans every track of every timeline (not inside compound or multicam clips) and never lists timelines. `find_duplicates(remove=True)` only removes extra media-pool entries of the same file, never one used on a timeline; files on disk are never touched.
- `find_offline(search=...)` finds missing files by name under a folder and relinks them with Resolve's RelinkClips.

### Automatic color correction

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `analyze_color` | `items: list[int] \| None` (default: the frame under the playhead), `track = 1` | per frame: `verdict`, `black`/`white` (per channel, 0.5/99.5 percentiles), `mean`, `luma` percentiles, `neutral`, `saturation`, `clipped_pct`, `crushed_pct` | Frame export failed |
| `auto_color` | `items: list[int]`, `node = 1`, `levels = True`, `balance = True`, `exposure = True`, `strength = 1.0`, `iterations = 4`, `track = 1` | per item: `before`/`after` (verdict, black, white, luma_mean, neutral), `cdl`, `iterations`, `refine_measurements`, `error`, `display_exponent`, `warnings` | Bad strength/iterations; SetCDL refused |
| `shot_match` | `reference: int`, `targets: list[int]`, `node = 1`, `iterations = 4`, `track = 1` | `{reference, matched: [rows as auto_color]}` | Reference among targets |

Notes:

- Resolve's API has no Auto Color or Shot Match, so the picture is measured here: the frame is exported as PNG (what the viewer shows, graded and color managed) and decoded in pure Python. Each clip is measured at its middle frame; the playhead is put back afterwards.
- The correction is an ASC CDL on `node` (it replaces that node's CDL), found in a closed loop: solve, apply, export, measure. The loop fits how a gamma-like display curve (later nodes, a display LUT) bends the CDL's output from the measured black and white points; `display_exponent` reports that fit (1.0 = the CDL alone). On an unmanaged YRGB timeline that converges in one or two steps. When it does not meet the goal, a model-free refinement takes over (`refine_measurements` counts its exports, up to 48, about 25 s per clip on live 21.1) and the best measured CDL is left on the node. **Color-managed timelines improve but may not fully converge** (live 21.1, automatic SDR with S-Log3-decoded test frames: error about 0.06 for `auto_color` and 0.02-0.04 for `shot_match`, with clipping mostly removed; saturated areas pushed out of gamut stay crushed, which no single CDL can undo); check `error` in the result.
- Black and white points ignore colored clips (one channel clipped while another is not: out of gamut, not a level), and a clipped level counts the share of pixels at the clip in `error`. The neutral color is a mean weighted by how gray and mid-toned each pixel is.
- `levels` sets each channel's black and white points (0.03 / 0.94), `balance` makes neutral areas gray, `exposure` brings the mean brightness to 0.42. `strength` scales all three.
- Balance needs neutral areas (low-saturation midtones). Without them it assumes the frame averages to gray and says so in `warnings`; use `balance=False` or `shot_match` for frames dominated by one color.

### Animated titles and templates

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `animated_title` | `text: str`, `animation = "fade"`, `exit = "fade"`, `frame: int \| None` (default playhead), `speed = 12` (frames), `font`, `style`, `size`, `color: [r, g, b]`, `position: [x, y]` | `{track, item, title, node, set, animation, exit, animation_frames, duration}` | Unknown animation; insert moved existing clips |
| `lower_third` | `name: str`, `role: str \| None`, `frame`, `side: "left" \| "right" = "left"`, `animation`, `exit = "fade"`, `speed = 10`, `font`, `size = 0.045`, `color` | as `animated_title` + `side` | Bad side |
| `save_template` | `item: int`, `name: str`, `track = 1`, `overwrite = False` | `{template, path, source, duration, title, texts}` | Name exists; no Fusion comp |
| `list_templates` | — | `{folder, saved, installed_titles}` | — |
| `apply_template` | `name: str`, `item: int \| None` (None: new title), `frame`, `text`, `track = 1` | `{template, track, item, name, text?}` | Template not found; import failed |
| `batch_titles` | `entries: [{frame, text}]`, `template: str \| None`, `animation`, `exit`, `speed`, `font`, `size`, `color`, `position` | list of `animated_title` / `apply_template` results | Bad entry; missing template; stops at the first failure |

Animations: `none`, `fade`, `pop`, `zoom`, `slide_up`, `slide_down`, `slide_left`, `slide_right`, `typewriter`. An exit slide continues in the same direction as the entrance.

Notes:

- Motion is keyframed on a Transform named `TitleMotion` between the Text+ (`Template`) and `MediaOut1`; typing uses the Text+ write-on range; fades are the item's own fades (Resolve 21.1+). Refine with `set_fusion_input(node="TitleMotion")`, `set_title_text` and `list_keyframes`.
- Resolve inserts a title into the targeted track at the playhead. If that would move or cut existing clips, the tool reports it and asks you to undo in Resolve (Cmd/Ctrl+Z): put the playhead where the track is free, or target an empty track above the edit.
- Templates are Fusion comps saved in `DAVINCI_MCP_TEMPLATES` (default `~/Documents/DaVinci MCP Templates`) with a `.json` sidecar. `apply_template` on a clip (`item=`) adds the comp as the clip's active Fusion composition, e.g. a saved effect look. `installed_titles` lists the `.setting` titles in Resolve's Fusion Titles folder; insert those with `insert_title(name, fusion=True)`.

### Social media delivery

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `social_platforms` | — | `[{platform, name, resolution, aspect, bitrate_kbps}]` | — |
| `social_timeline` | `platform: str`, `timeline: str \| None` (default current), `name`, `fit: "fill" \| "fit" \| "crop" \| "stretch" = "fill"`, `reframe = False`, `loudness: float \| None` | `{timeline, platform, resolution, fit, warnings, reframed?, loudness?}` | Name taken; resolution refused |
| `social_render` | `platform: str`, `target_dir: str`, `file_name`, `bitrate` (kbps), `codec = "H264"`, `subtitles`, `start = True` | `render` result + platform, resolution, bitrate | Timeline not in the platform's shape |
| `social_export` | `platforms: list[str]`, `target_dir: str`, `timeline`, `fit`, `reframe`, `loudness`, `subtitles`, `start = True` | `{target_dir, jobs: [{platform, timeline, timeline_was, job}], started}` | Unknown or repeated platform |

Platforms: `youtube` (1920x1080), `youtube_4k` (3840x2160), `youtube_shorts`, `tiktok`, `instagram_reels` (1080x1920), `instagram_feed` (1080x1350, 4:5), `instagram_square` (1080x1080), `facebook`, `x`, `linkedin` (1920x1080).

Notes:

- `social_timeline` never reframes your edit: it copies the timeline as `<timeline> - <Platform>` at the platform's resolution and makes the copy current. Adjust it by hand (reframing, title positions), then render. `fit="fill"` scales and crops to fill the frame, the usual choice from 16:9 to 9:16. `reframe=True` adds Smart Reframe on every clip (Studio).
- `social_export` reuses an existing `<timeline> - <Platform>` copy, so hand adjustments survive re-exports. All jobs start together and the timeline that was current stays current.
- `loudness=-14` normalizes each audio track of the copy for streaming platforms (Resolve 21.1+).
- Bitrates are H.264 defaults (16 Mbps for 1080p YouTube, 12 Mbps for vertical, 45 Mbps for 4K); pass `bitrate` to change.

### AI editing

Resolve's AI features already covered elsewhere: `magic_mask`, `smart_reframe`, `stabilize`, `detect_scene_cuts`, `voice_isolation`, `transcribe_audio`, `create_subtitles`, `classify_audio`, `generate_voiceover`, `smart_switch`, `analyze_dolby_vision`.

| Tool | Parameters | Returns | Errors |
|---|---|---|---|
| `super_scale` | `clips: list[str]`, `scale: "auto" \| "none" \| "2x" \| "3x" \| "4x" = "2x"`, `sharpness`, `noise_reduction` (0-1, both: 2x Enhanced) | `[{clip, super_scale, enhanced}]` as Resolve reads it back | Refused (Studio only); bad scale |
| `ai_slow_motion` | `item: int`, `percent: float = 50` (under 100), `engine = "speed_warp"`, `ripple = False`, `track = 1` | `set_speed` result + retiming | Resolve older than 21.1; engine refused |
| `remove_silences` | `clip: str`, `threshold_db = -40`, `min_silence = 0.5`, `padding = 0.1`, `timeline: str \| None` | `{timeline, parts, kept_seconds, removed_seconds, kept, pauses_found}` | No pause found; not WAV and no ffmpeg |
| `cut_by_transcript` | `clip: str`, `remove_fillers = True`, `fillers`, `remove_phrases`, `keep_only`, `max_gap = 0.75`, `padding = 0.08`, `timeline` | `{timeline, parts, kept_seconds, removed_seconds, kept, removed: {fillers, segments}}` | No transcript; Resolve older than 21.1; nothing left |

Notes:

- `remove_silences` and `cut_by_transcript` never change the source: they build a new timeline (default names `<clip> - no silences` and `<clip> - edited`) from the kept ranges of the clip and make it current.
- `remove_silences` analyzes the audio here. WAV files are read directly; other formats need `ffmpeg` on the PATH. Raise `threshold_db` (e.g. `-35`) in noisy rooms.
- `cut_by_transcript` uses Resolve's word timing (run `transcribe_audio` first). The default fillers are English (um, uh, erm, hmm...) and Arabic (امم, اه...); pass `fillers` for others. A removed filler always cuts, even inside a short pause.
- `ai_slow_motion` sets `RetimeProcess` to optical flow and `MotionEstimation` to the engine. Speed Warp is Studio only.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RESOLVE_SCRIPT_API` | platform default | Resolve `Developer/Scripting` directory |
| `RESOLVE_SCRIPT_LIB` | platform default | Path to `fusionscript.so` / `fusionscript.dll` |
| `DAVINCI_MCP_TEMPLATES` | `~/Documents/DaVinci MCP Templates` | Where `save_template` keeps templates |
| `RESOLVE_LUT_DIR` | platform master LUT folder | Where outside LUTs are installed for `apply_lut` / `set_node_lut` |
| `DAVINCI_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

## Logging

The server writes one JSON object per line to stderr (stdout is the MCP protocol channel). Every tool call is logged with its arguments, duration and outcome:

```json
{"ts": "2026-09-24T14:39:30.637+00:00", "level": "INFO", "logger": "davinci_mcp", "msg": "tool call", "tool": "add_marker", "args": {"frame": 24, "color": "Red"}, "outcome": "ok", "duration_ms": 3.1}
```

`outcome` is `ok` (INFO), `error` (WARNING, with `error` holding the message returned to the client) or `crash` (ERROR, with the traceback in `exc`).

## Live check

`scripts/live_check.py` runs the tools against your running Resolve and writes a pass/fail report:

```bash
uv run scripts/live_check.py            # add --studio on Resolve Studio only
```

It saves the open project, works in a scratch project it creates and deletes at the end, generates its own test media (PNG image sequences and a WAV tone, no ffmpeg needed), and writes `report.md` / `report.json` plus all outputs to a temporary folder it prints. It checks the points the unit tests cannot: that renders contain frames, exact color space names, the Fusion Tracker's input names, the comp frame range on media clips, page switching, and the Resolve 21.1 calls when available.

## Development

The tests run against an in-memory fake of Resolve's scripting API, so Resolve does not need to be installed:

```bash
uv run --no-project --with "mcp>=2" --with pytest pytest
```

## License

MIT
