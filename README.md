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

`status` · `list_projects` · `open_project` · `create_project` · `import_media` · `list_clips` · `list_timelines` · `create_timeline` · `switch_timeline` · `append_clips` · `list_items` · `set_item_properties` · `insert_title` · `add_marker` · `list_render_presets` · `render` · `render_status`

Set `RESOLVE_SCRIPT_API` / `RESOLVE_SCRIPT_LIB` if Resolve is installed in a non-default location.

## License

MIT
