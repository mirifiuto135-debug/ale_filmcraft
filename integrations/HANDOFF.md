# Handoff note: from Claude (cloud session, 2026-10-12) to Claude at home

This note is for the Claude Code session running on the owner's own computer, in their home
project (the book → script → short-film pipeline). The owner asked me to write down what I did
and why, because they are not deeply familiar with their own stack and will not be able to
explain it to you. So: read this first, then the READMEs it points to. The same note is in
`integrations/HANDOFF.md` of three forks: `ale_photocraft`, `ale_filmcraft`, `ale_lightcraft`.

## The stack, in one paragraph

The owner forked five apps by storytold (`ale_photocraft`, `ale_vectorcraft`, `ale_filmcraft`,
`ale_lightcraft`, `ale_artcraft`). They are clean-room, pure-Rust clones of Photoshop,
Illustrator, Premiere, Lightroom and a painting app. Every one has the same shape: a desktop app
(egui), a web build, and a **headless command-line binary `<app>-cli`** that exposes the same
**command registry** the UI uses (every menu item is a command with an id and JSON params) plus
an **MCP server** (`<app>-cli mcp`). That is why they are easy to automate: nothing is UI-only.
Each repo has an `AGENTS.md` with strict rules (no panics, no `unsafe`, Rust only in the app, clean
room, tests for everything). I did not change any Rust. Everything I added is Python glue under
`integrations/` that spawns the CLI, so the apps' own rules stay intact.

## What I built (three branches, all named `claude/admiring-ritchie-6iuoee`)

1. **ale_photocraft → `integrations/comfyui/ComfyUI-PhotoCraft/`**: ComfyUI custom nodes.
   Image Size, Scale By, Canvas Size, Crop, Trim, Rotate/Flip, Remove Background (Select Subject
   + edge refinement, returns an alpha mask), and a run-any-command node. Bridge:
   tensor → PNG → `photocraft-cli run in.png --cmd id --params json --out out.png` → tensor.
   Convention: an optional `alpha` MASK input/output with 1 = visible (opposite of ComfyUI's
   Load Image mask; put an Invert Mask between). 14 tests, all green.

2. **ale_filmcraft → `integrations/assemble/filmcraft_assemble.py`**: the assembly stage of the
   film pipeline, replacing the owner's ffmpeg step. Input `scenes.json` (clips, per-scene
   narration text, titles, captions); output an MP4 plus the `.fcproj` project. It drives
   `filmcraft-cli mcp` over JSON-RPC lines (class `FilmCraft`, method `run(cmd, params)`):
   new project/sequence → import each clip → `timeline.place` on V1 → `tts.create` narration on
   an audio track → `clip.speedDuration` when the clip is shorter than the speech → `captions.add`
   → `graphics.newText` title cards (centred with `graphics.set` + `graphics.align`) →
   `sequence.applyVideoTransition` cross dissolves → `file.saveAs` → `file.exportMedia`
   (`wait: true`, `burnCaptions`). Two things that bit me, now handled: scene starts must be
   frame-aligned (clips snap to frames) and PNG sequences import at 29.97 fps unless
   `prefs.set media.indeterminateTimebase` matches the sequence. 5 tests green; I rendered frames
   from the saved project and checked them by eye.

3. **ale_lightcraft → `integrations/comfyui/ComfyUI-LightCraft/`**: ComfyUI nodes for
   "relighting" in the raw-developer sense (tone and colour, not moving a light source).
   Develop (global sliders), Masked Develop (local sliders inside a mask), Preset, Preset List,
   Command. The important trick: a ComfyUI MASK becomes a LightCraft local-adjustment mask
   without any AI model, by encoding it in LightCraft's stored segmentation format (`SegMask`:
   512×512 int8 logits, raw deflate, base64; see `seg_from_mask`) and passing it to
   `mask.add {kind: "object", seg}`. Sky/subject/background/luminance/linear/radial masks are
   LightCraft's own and need no model; text-prompt masks need a build with
   `--features lightcraft-engine/sam` plus the SAM 3 weights. 8 tests green; I looked at the
   before/after renders.

Each integration has a README with the exact PowerShell install steps (the owner is on Windows
and asked for paste-ready PowerShell with the save location). Each needs its own
`cargo build --release -p <app>-cli` (5 to 7 minutes each on 4 cores here).

## How it should fit the home pipeline

book → (model) script → `scenes.json` + visual prompts → ComfyUI generates one clip per scene
(optionally PhotoCraft/LightCraft nodes for sizing, cutouts, grading) → `filmcraft_assemble.py
scenes.json` → MP4. The script stage only has to emit `scenes.json` (format in
`integrations/assemble/README.md`): per scene a `clip` path, `narration` text, optional `title`,
`caption`, `duration`. The assembler is importable (`assemble(load_spec(path))`) if you prefer to
call it from Python, and could be wrapped as one ComfyUI node in an hour.

## What I could not verify here, and what is left

- Windows builds and paths: everything was built and tested on Linux. The Python is portable
  and the CLIs are cross-platform, but check `.exe` discovery (`FILMCRAFT_CLI`,
  `LIGHTCRAFT_CLI`, `PHOTOCRAFT_CLI` env vars) first.
- Real generated clips (MP4 from a video model): I tested with PNG sequences, stills and
  FilmCraft colour mattes. `file.import` reads MP4/MOV/WebM etc. through FilmCraft's own decoders;
  if a clip is refused, `filmcraft-cli probe clip.mp4` says why.
- Natural narration voices (Kokoro, 336 MB) were not downloaded here; the built-in voice is
  robotic. Download once through the FilmCraft app's Text to Speech panel, then set
  `"narration": {"voice": "Heart"}`.
- Precision: images cross the ComfyUI bridges as 8-bit PNG. Fine for cutouts and sizing;
  a 16-bit path is the first improvement if colour grading is chained through several nodes.
- Not done: VectorCraft (image trace to SVG, render SVG) and ArtCraft nodes. Same pattern,
  `vectorcraft-cli run --in file --cmd id --params json --export out.png`.
- The owner's git identity for commits: I used their GitHub account email from the session.
  `contributors/people.toml` in each repo is where they can add a display name (AGENTS.md
  explains; I did not add one because I did not ask them).

Be kind about the knowledge gap: the owner built this by forking and asking, which is a fine way
to work. Explain terms when they come up (a "command id" is just the name of a menu action; a
"headless" binary is the app without its window; MCP is the protocol that lets you call those
actions). When in doubt, `<app>-cli commands` lists everything each app can do.
