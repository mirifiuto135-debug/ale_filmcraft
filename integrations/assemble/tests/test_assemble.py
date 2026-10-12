"""End-to-end test: synthetic PNG clips + a script -> an MP4, through filmcraft-cli.

    python3 -m unittest discover -s integrations/assemble/tests
Needs `cargo build --release -p filmcraft-cli` (or FILMCRAFT_CLI); Pillow for the synthetic frames.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("filmcraft_assemble", HERE.parent / "filmcraft_assemble.py")
fa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fa)

try:
    CLI = fa.find_cli()
except fa.AssembleError:
    CLI = None

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None


def write_frames(d: Path, name: str, n: int, color, size=(320, 180)):
    """A numbered PNG sequence `<name>_0001.png`… with a moving bar, so frames differ."""
    d.mkdir(parents=True, exist_ok=True)
    first = None
    for i in range(n):
        im = Image.new("RGB", size, color)
        dr = ImageDraw.Draw(im)
        x = int(i / max(1, n - 1) * (size[0] - 20))
        dr.rectangle([x, 40, x + 20, size[1] - 40], fill=(255, 255, 255))
        p = d / f"{name}_{i + 1:04d}.png"
        im.save(p)
        first = first or p
    return first


def probe(path: Path) -> dict:
    out = subprocess.run([CLI, "probe", str(path), "--compact"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise AssertionError(f"probe failed: {out.stderr}")
    return json.loads(out.stdout.strip().splitlines()[-1])


class SpecTests(unittest.TestCase):
    def test_load_and_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a.png").write_bytes(b"x")
            (d / "scenes.json").write_text(json.dumps({"title": "T", "scenes": [{"clip": "a.png", "narration": "hi", "title": "One"}]}))
            s = fa.load_spec(d / "scenes.json")
            self.assertEqual(s["output"], str((d / "scenes.mp4").resolve()))
            self.assertEqual(s["fps"], 24)
            self.assertEqual(s["transition"]["effect"], "Cross Dissolve")
            text = "\n".join(fa.plan(s))
            self.assertIn("title card 'T'", text)
            self.assertIn("a.png", text)

    def test_bad_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "s.json").write_text('{"scenes": []}')
            with self.assertRaises(fa.AssembleError):
                fa.load_spec(d / "s.json")
            (d / "s.json").write_text('{"scenes": [{"clip": "missing.mp4"}]}')
            with self.assertRaises(fa.AssembleError):
                fa.load_spec(d / "s.json")
            (d / "s.json").write_text("not json")
            with self.assertRaises(fa.AssembleError):
                fa.load_spec(d / "s.json")


@unittest.skipUnless(CLI and Image, "filmcraft-cli not built or Pillow missing")
class AssembleTests(unittest.TestCase):
    def test_movie_from_png_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            fps = 24
            a = write_frames(d / "s1", "s1", 2 * fps, (30, 60, 160))      # 2 s
            b = write_frames(d / "s2", "s2", 3 * fps, (160, 60, 30))      # 3 s
            still = d / "still.png"
            Image.new("RGB", (320, 180), (40, 160, 60)).save(still)
            scenes = {
                "output": "movie.mp4",
                "project": "movie.fcproj",
                "width": 320,
                "height": 180,
                "fps": fps,
                "title": "Test Book",
                "title_seconds": 1.0,
                "end_title": "Fin",
                "end_title_seconds": 1.0,
                "transition": {"effect": "Cross Dissolve", "frames": 6},
                "narration": {"voice": None},
                "captions": "burn",
                "fit": "speed",
                "export": {"format": "h264"},
                "scenes": [
                    {"clip": str(a), "narration": "Chapter one. A short line.", "title": "One", "title_seconds": 1.0},
                    {"clip": str(b), "caption": "A silent scene"},
                    {"clip": str(still), "image_sequence": False, "duration": 1.5},
                ],
            }
            (d / "scenes.json").write_text(json.dumps(scenes))
            spec = fa.load_spec(d / "scenes.json")
            r = fa.assemble(spec, CLI, log=lambda m: print(m, file=sys.stderr))
            out = Path(r["output"])
            self.assertTrue(out.is_file(), r)
            self.assertGreater(out.stat().st_size, 10_000)
            self.assertTrue((d / "movie.fcproj").is_file())
            self.assertEqual(len(r["scenes"]), 3)
            # scene 1 is 2 s of picture; its narration is spoken, so the scene lasts max(clip, speech)
            self.assertGreaterEqual(r["scenes"][0]["end"] - r["scenes"][0]["start"], 2.0 - 1e-6)
            self.assertGreater(r["scenes"][0]["narration"], 0.5)
            self.assertAlmostEqual(r["scenes"][2]["end"] - r["scenes"][2]["start"], 1.5, places=2)
            info = probe(out)
            dur = info.get("duration") or info.get("durationSeconds") or info.get("video", {}).get("duration")
            self.assertIsNotNone(dur, info)
            dur = float(dur)
            if dur > 1e6:  # ticks
                dur /= fa.TICKS_PER_SECOND
            self.assertAlmostEqual(dur, r["duration"], delta=0.25)

    def test_cuts_without_narration_or_captions(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            a = write_frames(d / "s1", "s1", 12, (90, 90, 90))
            scenes = {"width": 320, "height": 180, "fps": 24, "transition": None, "narration": None, "captions": False, "scenes": [{"clip": str(a)}, {"clip": str(a)}]}
            (d / "scenes.json").write_text(json.dumps(scenes))
            r = fa.assemble(fa.load_spec(d / "scenes.json"), CLI)
            self.assertTrue(Path(r["output"]).is_file())
            self.assertAlmostEqual(r["duration"], 1.0, places=2)

    def test_command_errors_are_reported(self):
        with fa.FilmCraft(CLI) as fc:
            with self.assertRaises(fa.AssembleError):
                fc.run("no.such.command", {})
            voices = fc.run("tts.voices", {})
            self.assertTrue(voices)


if __name__ == "__main__":
    unittest.main()
