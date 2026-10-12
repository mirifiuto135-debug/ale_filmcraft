# FilmCraft assembler: clips + script → movie

`filmcraft_assemble.py` turns a folder of generated clips and a scene list into a finished short
film: title cards, the clips in order, cross dissolves, spoken narration, captions, and an H.264
MP4, plus the FilmCraft project so the cut can be opened and fixed in the app. It is the last
stage of a book → script → generated-clips pipeline (ComfyUI or anything else makes the clips).

It needs only Python 3.10+ and `filmcraft-cli`: the script drives a headless FilmCraft session
(`filmcraft-cli mcp`, JSON-RPC over stdio), so every engine command is available and nothing else
is installed. No ffmpeg: FilmCraft decodes and encodes with its own codecs.

```sh
cargo build --release -p filmcraft-cli
python3 integrations/assemble/filmcraft_assemble.py scenes.json           # -> movie.mp4
python3 integrations/assemble/filmcraft_assemble.py scenes.json --dry-run # print the plan
```

The binary is found through `--cli`, the `FILMCRAFT_CLI` environment variable, `bin/` next to the
script, the repository's `target/release`, or the PATH.

## scenes.json

```jsonc
{
  "output": "movie.mp4",              // default: <scenes>.mp4 next to the file
  "project": "movie.fcproj",          // optional: also save the FilmCraft project
  "width": 1920, "height": 1080, "fps": 24,
  "title": "The Little Prince",       // opening title card (black, centred text), optional
  "title_seconds": 3,
  "end_title": "The End",             // closing card, optional
  "transition": {"effect": "Cross Dissolve", "frames": 12},   // null = straight cuts
  "narration": {"voice": "Heart", "pace": 1.0, "pitch": "default"},   // null = no speech
  "captions": "burn",                 // "burn" into the frames, "track" (sidecar only), false
  "fit": "speed",                     // clip shorter than its narration: "speed" slows the clip
                                      // to match, "hold" freezes its last frame, "none" = black
  "gap": 0.0,                         // seconds of black between scenes
  "export": {"format": "h264", "quality": 80},
  "scenes": [
    {
      "clip": "scene01.mp4",          // a video, the first frame of a numbered PNG/JPEG
                                      // sequence (frame_0001.png), or a single still
      "duration": 6.0,                // optional: trim the clip (a single still needs it)
      "narration": "Once upon a time…",
      "caption": "…",                 // optional; default: the narration text
      "title": "Chapter 1",           // overlay at the start of the scene, optional
      "title_seconds": 2.5
    }
  ]
}
```

Paths are relative to the scenes file. The result JSON names the output, the project and each
scene's start, end and narration length.

How a scene is timed: the clip is placed on V1 at the scene start; its narration starts at the
same moment on a free audio track; the scene ends when both have finished (frame-aligned). With
`"fit": "speed"` a clip shorter than its line is slowed down so the picture lasts as long as the
speech. Captions run one frame short of the scene so they never collide. Cross dissolves are
applied at the head of every clip after the first.

Narration: FilmCraft's built-in voices (`basic-female`, `basic-male`) need no download and are
robotic; the natural Kokoro voices (`Heart`, `Bella`, `Michael`, …; 336 MB, one-time download via
the app's Text to Speech panel or the `tts.downloadVoices` command) sound good. English only.
`filmcraft-cli exec tts.voices` lists what is installed.

## Windows (PowerShell)

```powershell
# build the CLI (from the FilmCraft checkout)
cd C:\src\ale_filmcraft
cargo build --release -p filmcraft-cli

# point the assembler at it for every future session
[Environment]::SetEnvironmentVariable("FILMCRAFT_CLI", "C:\src\ale_filmcraft\target\release\filmcraft-cli.exe", "User")

# assemble (scenes.json next to the clips)
python integrations\assemble\filmcraft_assemble.py C:\work\book1\scenes.json
```

## From Python

```python
from filmcraft_assemble import load_spec, assemble
report = assemble(load_spec("scenes.json"))      # {"output": ..., "project": ..., "scenes": [...]}
```

`FilmCraft` (the session class) runs any command: `with FilmCraft() as fc: fc.run("tts.voices")`.

## Tests

```sh
cargo build --release -p filmcraft-cli
python3 -m unittest discover -s integrations/assemble/tests
```

The end-to-end test draws PNG sequences and a still with Pillow, assembles them with a title card,
narration, captions, dissolves and an end card, exports the MP4, saves the project and probes the
file's duration.
