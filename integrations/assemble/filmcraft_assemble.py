#!/usr/bin/env python3
"""Assemble a short movie from generated clips and a script, with FilmCraft.

Input: a ``scenes.json`` (format below). Output: an MP4 (and optionally the ``.fcproj`` project,
so the cut can be opened in the FilmCraft app). Everything runs through ``filmcraft-cli mcp``
(headless engine, JSON-RPC over stdio), so the assembler needs only Python 3.10+ and the binary.

    python3 filmcraft_assemble.py scenes.json            # writes the output named in the file
    python3 filmcraft_assemble.py scenes.json -o out.mp4 --project cut.fcproj
    python3 filmcraft_assemble.py scenes.json --dry-run  # print the command plan, run nothing

scenes.json::

    {
      "output": "movie.mp4",            // default: <scenes>.mp4 next to the file
      "project": "movie.fcproj",        // optional: also save the FilmCraft project
      "width": 1920, "height": 1080, "fps": 24,
      "title": "The Little Prince",     // optional opening title card (black, centred text)
      "title_seconds": 3,
      "end_title": "The End",           // optional closing card
      "transition": {"effect": "Cross Dissolve", "frames": 12},   // between scenes; null for cuts
      "narration": {"voice": "Heart", "pace": 1.0, "pitch": "default"},  // null = no speech
      "captions": "burn",               // "burn" (drawn into the frames), "track" (sidecar track
                                        //  in the project), false
      "fit": "speed",                   // clip shorter than its narration: "speed" slows the clip
                                        //  to match, "hold" freezes its last frame, "none" leaves
                                        //  a black gap
      "gap": 0.0,                       // seconds of black between scenes
      "export": {"format": "h264", "quality": 80},
      "scenes": [
        {
          "clip": "scene01.mp4",        // a video, or the first frame of a numbered PNG/JPEG
                                        //  sequence (frame_0001.png), or a single still
          "duration": 6.0,              // optional: trim the clip to this (a still needs it)
          "narration": "Once upon a time…",   // optional line, spoken by the narration voice
          "caption": "…",               // optional; default: the narration text
          "title": "Chapter 1",         // optional overlay at the start of the scene
          "title_seconds": 2.5
        }
      ]
    }

Paths are relative to the scenes file. Every scene needs a clip; narration, captions and titles
are optional. Narration is English only (FilmCraft's built-in and Kokoro voices; ``tts.voices``
lists them; the neural voices are a one-time download the app or ``tts.downloadVoices`` makes).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

TICKS_PER_SECOND = 254_016_000_000
STILL_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".exr", ".dpx"}
BINARY = "filmcraft-cli.exe" if sys.platform.startswith("win") else "filmcraft-cli"
HERE = Path(__file__).resolve().parent


class AssembleError(RuntimeError):
    """A command failed or the input is wrong; the message says what to fix."""


def ticks(seconds: float) -> int:
    return int(round(seconds * TICKS_PER_SECOND))


def seconds(t: int) -> float:
    return t / TICKS_PER_SECOND


# ----------------------------------------------------------------------------- the CLI


def find_cli(explicit: str | None = None) -> str:
    """Path of ``filmcraft-cli``: the argument, ``FILMCRAFT_CLI``, ``bin/`` next to this file, the
    repository's release build, or the PATH."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("FILMCRAFT_CLI")
    if env:
        candidates.append(Path(env))
    candidates.append(HERE / "bin" / BINARY)
    candidates.append(HERE.parents[1] / "target" / "release" / BINARY)
    on_path = shutil.which("filmcraft-cli")
    if on_path:
        candidates.append(Path(on_path))
    for c in candidates:
        if c.is_file():
            return str(c)
    raise AssembleError(
        "filmcraft-cli not found: build it with `cargo build --release -p filmcraft-cli`, then pass "
        "--cli, set FILMCRAFT_CLI, or put the binary on the PATH"
    )


class FilmCraft:
    """One headless FilmCraft session: ``filmcraft-cli mcp`` driven over JSON-RPC lines."""

    def __init__(self, cli: str | None = None, timeout: float = 3600.0, log=None):
        self.cli = find_cli(cli)
        self.timeout = timeout
        self.log = log
        self._id = 0
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(
            [self.cli, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._stderr: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "filmcraft-assemble", "version": "0.1"}})
        self._notify("notifications/initialized", {})

    def _drain_stderr(self):
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())
            if len(self._stderr) > 200:
                del self._stderr[0]

    def _send(self, msg: dict):
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise AssembleError(f"filmcraft-cli exited: {' | '.join(self._stderr[-5:])}") from e

    def _notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: dict) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            assert self.proc.stdout is not None
            while True:
                if time.monotonic() > deadline:
                    raise AssembleError(f"{method} timed out after {self.timeout:.0f}s")
                line = self.proc.stdout.readline()
                if not line:
                    raise AssembleError(f"filmcraft-cli exited during {method}: {' | '.join(self._stderr[-5:])}")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise AssembleError(f"{method}: {msg['error'].get('message', msg['error'])}")
                    return msg.get("result", {})
                # progress notifications and anything else: ignore

    def run(self, cmd: str, params: dict | None = None):
        """Run one engine command; returns its JSON result or raises `AssembleError`."""
        params = params or {}
        if self.log:
            self.log(f"  {cmd} {json.dumps(params)[:160]}")
        r = self._rpc("tools/call", {"name": "command_run", "arguments": {"id": cmd, "params": params}})
        text = "".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        if r.get("isError"):
            raise AssembleError(f"{cmd}: {text.strip()}")
        try:
            return json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            return {"text": text}

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                if f:
                    f.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ----------------------------------------------------------------------------- the spec


DEFAULTS = {
    "width": 1920,
    "height": 1080,
    "fps": 24,
    "title": None,
    "title_seconds": 3.0,
    "end_title": None,
    "end_title_seconds": 3.0,
    "title_size": 96,
    "title_font": "Inter",
    "title_font_style": "SemiBold",
    "title_color": "#ffffff",
    "transition": {"effect": "Cross Dissolve", "frames": 12},
    "narration": {"voice": None, "pace": 1.0, "pitch": "default"},
    "captions": "burn",
    "fit": "speed",
    "gap": 0.0,
    "still_seconds": 5.0,
    "export": {"format": "h264"},
}


def load_spec(path: str | Path) -> dict:
    """The scenes file with defaults filled in and paths made absolute."""
    p = Path(path).resolve()
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise AssembleError(f"{p}: {e}") from e
    if not isinstance(spec, dict) or not isinstance(spec.get("scenes"), list) or not spec["scenes"]:
        raise AssembleError(f"{p}: needs a non-empty \"scenes\" list")
    base = p.parent
    out = dict(DEFAULTS)
    for k, v in spec.items():
        if k in ("transition", "narration", "export") and isinstance(v, dict) and isinstance(DEFAULTS[k], dict):
            out[k] = {**DEFAULTS[k], **v}
        else:
            out[k] = v
    out["output"] = str((base / spec.get("output", p.stem + ".mp4")).resolve())
    out["project"] = str((base / spec["project"]).resolve()) if spec.get("project") else None
    out["_base"] = str(base)
    if out["fit"] not in ("speed", "hold", "none"):
        raise AssembleError('"fit" must be "speed", "hold" or "none"')
    if out["captions"] not in ("burn", "track", False, None):
        raise AssembleError('"captions" must be "burn", "track" or false')
    scenes = []
    for i, s in enumerate(spec["scenes"]):
        if not isinstance(s, dict) or not s.get("clip"):
            raise AssembleError(f"scene {i + 1}: needs a \"clip\" path")
        clip = (base / s["clip"]).resolve()
        if not clip.is_file():
            raise AssembleError(f"scene {i + 1}: clip not found: {clip}")
        sc = dict(s)
        sc["clip"] = str(clip)
        sc["_index"] = i
        if sc.get("duration") is not None and float(sc["duration"]) <= 0:
            raise AssembleError(f"scene {i + 1}: duration must be positive")
        scenes.append(sc)
    out["scenes"] = scenes
    return out


# ----------------------------------------------------------------------------- the assembly


def _find_clip(seq: dict, clip_id: int) -> dict | None:
    """The track item with ``clip_id`` in a ``sequence.inspect`` result."""
    for key in ("video", "audio"):
        for track in seq.get(key) or []:
            for it in track.get("items") or []:
                if it.get("clip") == clip_id or it.get("id") == clip_id:
                    return it
    return None


def _span(seq: dict, clip_id: int) -> tuple[int, int]:
    """``(start, duration)`` in ticks of a placed clip."""
    it = _find_clip(seq, clip_id)
    if it is None:
        raise AssembleError(f"clip {clip_id} is not in the sequence")
    start = it.get("start", it.get("time", it.get("at")))
    dur = it.get("duration")
    if dur is None and it.get("end") is not None:
        dur = it["end"] - start
    if start is None or dur is None:
        raise AssembleError(f"cannot read the span of clip {clip_id}: {json.dumps(it)[:200]}")
    return int(start), int(dur)


def _text_clip(fc: FilmCraft, spec: dict, text: str, at: int, dur_s: float, track: int, size: float | None = None, style: dict | None = None):
    """A centred text clip of ``dur_s`` seconds at ``at`` on video track ``track`` (0 = V1)."""
    size = size or spec["title_size"]
    r = fc.run("graphics.newText", {"text": text, "seconds": dur_s, "time": at, "track": track, "size": size})
    props = {"align": "center", "font": spec.get("title_font") or "Inter", "font_style": spec.get("title_font_style") or "SemiBold", "fill_color": spec.get("title_color") or "#ffffff"}
    props.update(style or {})
    fc.run("graphics.set", {"clip": r["clip"], "layer": r.get("layer", 0), "props": props})
    for how in ("hcenter", "vcenter"):
        fc.run("graphics.align", {"clip": r["clip"], "layers": [r.get("layer", 0)], "align": how, "to": "frame"})
    return r


def assemble(spec: dict, cli: str | None = None, log=None) -> dict:
    """Build the sequence described by ``spec`` (see `load_spec`) and export it.

    Returns ``{"output", "project", "duration", "scenes": [{"clip", "start", "end", "narration"}]}``."""
    say = log or (lambda *_: None)
    out_path = Path(spec["output"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / (out_path.stem + "_filmcraft")
    work.mkdir(parents=True, exist_ok=True)
    narr = spec.get("narration")
    transition = spec.get("transition")
    captions = spec.get("captions")
    report = {"output": str(out_path), "project": spec.get("project"), "scenes": []}

    with FilmCraft(cli, log=say) as fc:
        say("new project and sequence")
        fc.run("file.newProject", {"name": out_path.stem})
        fc.run("file.newSequence", {"name": out_path.stem, "width": spec["width"], "height": spec["height"], "fps": spec["fps"], "video": 3, "audio": 3})
        if captions:
            fc.run("captions.newTrack", {"format": "Subtitle", "name": "Narration", "language": "en"})
        # Image sequences and stills take the sequence's frame rate (FilmCraft's default is 29.97)
        fps = float(spec["fps"])
        timebase = next((k for k, v in (("23.976", 23.976), ("24", 24), ("25", 25), ("29.97df", 29.97), ("30", 30), ("50", 50), ("59.94", 59.94), ("60", 60)) if abs(fps - v) < 0.01), None)
        if timebase:
            fc.run("prefs.set", {"key": "media.indeterminateTimebase", "value": timebase})
        else:
            say(f"  (fps {fps} has no still-image timebase; PNG sequences import at 29.97)")

        info = fc.run("sequence.inspect", {})
        fr = (info.get("settings") or {}).get("frame_rate") or {}
        frame = int(TICKS_PER_SECOND * int(fr.get("den", 1)) / max(1, int(fr.get("num", spec["fps"]))))

        def align(x: int) -> int:
            """``x`` rounded up to a frame boundary: clips snap to frames, so the next scene must too."""
            return ((x + frame - 1) // frame) * frame

        t = 0  # ticks: where the next scene starts (always on a frame)
        if spec.get("title"):
            dur = float(spec["title_seconds"])
            _text_clip(fc, spec, spec["title"], t, dur, 0)
            t = align(t + ticks(dur))

        placed: list[int] = []
        for sc in spec["scenes"]:
            n = sc["_index"] + 1
            say(f"scene {n}: {Path(sc['clip']).name}")
            clip = Path(sc["clip"])
            is_still = clip.suffix.lower() in STILL_EXTS
            imp = fc.run("file.import", {"paths": [str(clip)], "imageSequence": bool(is_still and sc.get("image_sequence", True))})
            if imp.get("errors"):
                raise AssembleError(f"scene {n}: import failed: {'; '.join(imp['errors'])}")
            items = imp.get("items") or []
            if not items:
                raise AssembleError(f"scene {n}: nothing imported from {clip}")
            item = items[0]
            place = {"item": item, "track": "V1", "time": t}
            dur_s = sc.get("duration")
            if dur_s is None and is_still and not (imp.get("imageSequences") or [{}])[0].get("frames", 0) > 1:
                dur_s = spec["still_seconds"]
            if dur_s is not None:
                place["duration"] = ticks(float(dur_s))
            r = fc.run("timeline.place", place)
            clip_ids = r.get("clips") or []
            if not clip_ids:
                raise AssembleError(f"scene {n}: the clip was not placed ({json.dumps(r)[:200]})")
            vclip = clip_ids[0]
            seq = fc.run("sequence.inspect", {})
            start, cdur = _span(seq, vclip)
            placed.append(vclip)

            ndur = 0
            line = (sc.get("narration") or "").strip()
            if line and narr:
                p = {"text": line, "time": t, "dir": str(work)}
                if narr.get("voice"):
                    p["voice"] = narr["voice"]
                if narr.get("pace") is not None:
                    p["pace"] = float(narr["pace"])
                if narr.get("pitch"):
                    p["pitch"] = narr["pitch"]
                nr = fc.run("tts.create", p)
                ndur = int(nr.get("duration") or nr.get("speechDuration") or 0)
                if ndur > cdur and spec["fit"] == "speed" and cdur > 0:
                    pct = 100.0 * cdur / ndur
                    fc.run("clip.speedDuration", {"clips": [vclip], "speed": pct, "ripple": True})
                    seq = fc.run("sequence.inspect", {})
                    start, cdur = _span(seq, vclip)
                elif ndur > cdur and spec["fit"] == "hold":
                    fc.run("timeline.select", {"clips": [vclip]})
                    fc.run("clip.frameHold", {"clips": [vclip], "time": start + cdur - 1})
                    seq = fc.run("sequence.inspect", {})
                    start, cdur = _span(seq, vclip)

            # the scene ends when both its picture (as FilmCraft placed it) and its speech have
            scene_end = align(max(start + cdur, t + ndur))
            scene_len = scene_end - t
            cap = sc.get("caption", line if line else None)
            if cap and captions:
                # one frame short of the scene, so it never overlaps the next scene's caption
                cap_s = max(0.5, seconds(scene_len - frame))
                fc.run("captions.add", {"text": cap, "time": t, "durationSeconds": cap_s})

            if sc.get("title"):
                _text_clip(fc, spec, sc["title"], t, float(sc.get("title_seconds", 2.5)), 1, sc.get("title_size"), sc.get("title_style"))

            report["scenes"].append({"clip": str(clip), "start": seconds(t), "end": seconds(scene_end), "narration": seconds(ndur)})
            t = align(scene_end + ticks(float(spec.get("gap") or 0.0)))

        if spec.get("end_title"):
            dur = float(spec["end_title_seconds"])
            _text_clip(fc, spec, spec["end_title"], t, dur, 0)
            t = align(t + ticks(dur))

        if transition and transition.get("effect") and len(placed) > 1 and not spec.get("gap"):
            say("transitions")
            for c in placed[1:]:
                try:
                    fc.run("sequence.applyVideoTransition", {"clip": c, "effect": transition["effect"], "frames": int(transition.get("frames", 12)), "edge": "in"})
                except AssembleError as e:
                    say(f"  (no transition on clip {c}: {e})")

        if spec.get("project"):
            Path(spec["project"]).parent.mkdir(parents=True, exist_ok=True)
            fc.run("file.saveAs", {"path": spec["project"]})

        say(f"export {out_path}")
        exp = dict(spec.get("export") or {})
        exp.update({"path": str(out_path), "wait": True})
        if captions == "burn":
            exp["burnCaptions"] = True
        er = fc.run("file.exportMedia", exp)
        job = er.get("job")
        if job is not None:
            jobs = fc.run("jobs.list", {})
            j = next((x for x in jobs if x.get("id") == job), None) if isinstance(jobs, list) else None
            if j and isinstance(j.get("result"), dict) and j["result"].get("error"):
                raise AssembleError(f"export failed: {j['result']['error']}")
        written = er.get("path") or str(out_path)
        report["output"] = written
        report["duration"] = seconds(t)
        if not Path(written).is_file():
            raise AssembleError(f"export reported no file at {written}")
    return report


def plan(spec: dict) -> list[str]:
    """A human-readable dry run of what `assemble` will do."""
    lines = [f"sequence {spec['width']}x{spec['height']} @ {spec['fps']} fps -> {spec['output']}"]
    t = 0.0
    if spec.get("title"):
        lines.append(f"{t:7.2f}s  title card {spec['title']!r} ({spec['title_seconds']}s)")
        t += float(spec["title_seconds"])
    for sc in spec["scenes"]:
        d = sc.get("duration")
        lines.append(f"{t:7.2f}s  clip {Path(sc['clip']).name}" + (f" trimmed to {d}s" if d else ""))
        if sc.get("narration") and spec.get("narration"):
            lines.append(f"          narration: {sc['narration'][:70]!r}")
        if sc.get("title"):
            lines.append(f"          title overlay: {sc['title']!r}")
        t += float(d or spec["still_seconds"])
    if spec.get("end_title"):
        lines.append(f"{t:7.2f}s  end card {spec['end_title']!r}")
    tr = spec.get("transition")
    lines.append(f"transitions: {tr['effect']} {tr.get('frames', 12)} frames" if tr and tr.get("effect") else "transitions: none (cuts)")
    lines.append(f"captions: {spec.get('captions') or 'none'}; narration voice: {(spec.get('narration') or {}).get('voice') or 'default'}; fit: {spec['fit']}")
    lines.append("(times assume each clip is as long as its narration; the real cut follows the clips and speech)")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assemble generated clips and a script into a movie with FilmCraft.")
    ap.add_argument("scenes", help="scenes.json (see the module docstring)")
    ap.add_argument("-o", "--output", help="output movie path (overrides the file)")
    ap.add_argument("--project", help="also save the FilmCraft project here (.fcproj)")
    ap.add_argument("--cli", help="path of filmcraft-cli")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    try:
        spec = load_spec(a.scenes)
        if a.output:
            spec["output"] = str(Path(a.output).resolve())
        if a.project:
            spec["project"] = str(Path(a.project).resolve())
        if a.dry_run:
            print("\n".join(plan(spec)))
            return 0
        log = None if a.quiet else (lambda m: print(m, file=sys.stderr, flush=True))
        r = assemble(spec, a.cli, log)
        print(json.dumps(r, indent=2))
        return 0
    except AssembleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
