# ComfyUI MCP Transport

Hermes Studio uses `comfyui-mcp` as its production ComfyUI control plane.
Raw REST remains only inside explicit legacy diagnostic runners.

## Live configuration

- Profile: `studio` only (the sole GPU queue owner)
- Server: `comfyui-mcp@0.52.61` (pinned)
- Transport: stdio via `npx`
- Target: `http://127.0.0.1:8188`
- ComfyUI root: `/home/sven/ComfyUI`
- Startup timeout: 120s
- Tool-call timeout: 660s (covers the server's bounded 600s batch wait)
- Completion watcher: 10,800s timeout, 2s fallback poll

The source-controlled example is
`hermes/profiles/studio/config.yaml.example`. Verify the live profile with:

```bash
hermes -p studio mcp list
hermes -p studio mcp test comfyui
```

Current server exposes 41 tools. Core Studio tools:

- `mcp_comfyui_upload_image`
- `mcp_comfyui_enqueue_workflow`
- `mcp_comfyui_queue`
- `mcp_comfyui_get_history`
- `mcp_comfyui_get_image`
- `mcp_comfyui_clear_vram`
- `mcp_comfyui_get_system_stats`

## Mandatory generation transaction

Only `studio` executes GPU jobs. Subagents prepare storyboards, prompts and
image handoffs.

1. Build/inspect an API-format graph locally (`run_h3.py --dry-run` or
   `krea2_image.py --dry-run`).
2. Upload references with MCP and patch the graph with returned filenames.
3. Submit one workflow with `mcp_comfyui_batch` `action:"submit"`,
   `workflows:[graph]`, and `disable_random_seed:true`; retain `batch_id` and
   `prompt_id`.
4. Wait with `mcp_comfyui_batch` `action:"wait"`, `timeout_s:600`. The server
   checks every two seconds and returns as soon as the prompt is terminal, so
   completion is not delayed by a fixed sleep. Repeat only when the bounded
   safety cap expires and the returned state is still pending/running. Never
   overlap jobs.
5. On success, archive MCP output into the project:

   ```bash
   python3 scripts/design_studio.py archive-output <project-id> <clip-id> \
     <filename-under-ComfyUI-output> --prompt-id <id> --kind <image|video>
   ```

6. In a finally-style cleanup, always call `mcp_comfyui_clear_vram` (defaults:
   `unload_models=true`, `free_memory=true`).
7. On error/timeout: cancel via `mcp_comfyui_queue`, verify stopped, then clear
   VRAM. Killing a Python wrapper does not cancel a ComfyUI job.

This intentionally unloads after every terminal job. It costs model reload
time on the next render, but guarantees an idle Studio does not retain most of
the 16GB GPU.

## No silent REST fallback

Normal Studio agent work must not use curl or raw `/prompt`, `/history`,
`/upload`, `/queue`, `/interrupt`, or `/free`. If MCP discovery/calls fail,
stop and report the MCP error.

The web backend has one narrow observability exception: it performs read-only
`GET /queue` and on-demand completed-job `GET /api/jobs` requests for the header
queue viewer. It reduces workflow graphs to an allowlist of recipe/mode, canvas,
approximate media length, frames, steps, accel, and seed; prompt text,
references, model paths, and raw graphs never cross the backend boundary. It has
no mutation path and is not an execution fallback. Phase 1 uses native ComfyUI
APIs only—there is no Studio ComfyUI extension and no whole-generation
percentage or ETA claim.

`design_studio.py generate`, `generate-image`, and standalone
`krea2_image.py` execution remain manual diagnostic fallbacks. They now clean
VRAM after terminal completion; timeout paths interrupt first, then unload.
