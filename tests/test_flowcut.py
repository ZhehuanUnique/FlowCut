"""Portable checks: python -m unittest discover -s tests -v.

All unit tests use only the standard library. The synthetic media smoke test
runs when FFmpeg (with libx264) and FFprobe are available on PATH or through
FLOWCUT_FFMPEG / FLOWCUT_FFPROBE. It never reads personal media or models.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "flowcut" / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


render = load_script("render_cuts")
scan = load_script("scan_ctc")


class TemporaryFilesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flowcut-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, name, data=b"input must remain unchanged"):
        path = self.root / name
        path.write_bytes(data)
        return path


class ExecutableTests(TemporaryFilesTest):
    def test_explicit_ffmpeg_takes_precedence_over_path(self):
        executable = self.file("custom ffmpeg.exe")
        with patch.object(render.shutil, "which") as which:
            self.assertEqual(render.find_ffmpeg(executable), executable.resolve())
        which.assert_not_called()

    def test_ffmpeg_is_resolved_from_path(self):
        executable = self.file("ffmpeg")
        with patch.object(render.shutil, "which", return_value=str(executable)) as which:
            self.assertEqual(render.find_ffmpeg(None), executable.resolve())
        which.assert_called_once_with("ffmpeg")

    def test_missing_explicit_ffmpeg_is_an_actionable_error(self):
        with self.assertRaisesRegex(render.RenderError, "FFmpeg not found"):
            render.find_ffmpeg(self.root / "missing")

    def test_missing_path_ffmpeg_is_an_actionable_error(self):
        with patch.object(render.shutil, "which", return_value=None):
            with self.assertRaisesRegex(render.RenderError, "pass --ffmpeg explicitly"):
                render.find_ffmpeg(None)

    def test_ffprobe_sibling_and_explicit_override(self):
        for suffix in ("", ".exe"):
            with self.subTest(suffix=suffix):
                ffmpeg = self.file("ffmpeg" + suffix)
                ffprobe = self.file("ffprobe" + suffix)
                self.assertEqual(render.find_ffprobe(None, ffmpeg), ffprobe.resolve())
        custom = self.file("custom probe")
        self.assertEqual(render.find_ffprobe(custom, ffmpeg), custom.resolve())

    def test_missing_ffprobe_is_an_actionable_error(self):
        with self.assertRaisesRegex(render.RenderError, "pass --ffprobe explicitly"):
            render.find_ffprobe(None, self.root / "ffmpeg.exe")

    def test_cli_missing_ffmpeg_fails_without_traceback_or_output(self):
        source = self.file("source.mp4")
        cuts = self.file("cuts.json", b"[]")
        output = self.root / "output.mp4"
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "render_cuts.py"),
             "--source", str(source), "--cuts", str(cuts),
             "--output", str(output), "--execute", "--ffmpeg", str(self.root / "absent")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("FFmpeg not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), b"input must remain unchanged")


class CutRangeTests(unittest.TestCase):
    def normalize(self, cuts, **overrides):
        options = dict(fps=Fraction(30), frame_count=60, quantize="outward",
                       pad_before_ms=Decimal(0), pad_after_ms=Decimal(0), merge_gap_frames=0)
        options.update(overrides)
        return render.normalize_cuts(cuts, **options)

    def test_overlapping_and_touching_ranges_merge_but_gaps_remain(self):
        cuts, raw_count = self.normalize([
            {"start_frame": 15, "end_frame": 20},
            {"start_frame": 10, "end_frame": 17},
            {"start_frame": 20, "end_frame": 22},
            {"start_frame": 23, "end_frame": 25},
        ])
        self.assertEqual(cuts, [render.FrameRange(10, 22), render.FrameRange(23, 25)])
        self.assertEqual(raw_count, 4)
        keep = render.complement(cuts, 60)
        self.assertEqual(keep, [render.FrameRange(0, 10), render.FrameRange(22, 23),
                                render.FrameRange(25, 60)])
        self.assertEqual(sum(item.length for item in cuts + keep), 60)

    def test_gap_merging_requires_explicit_option(self):
        cuts, _ = self.normalize([
            {"start_frame": 10, "end_frame": 20},
            {"start_frame": 21, "end_frame": 25},
        ], merge_gap_frames=1)
        self.assertEqual(cuts, [render.FrameRange(10, 25)])

    def test_second_quantization_modes(self):
        expected = {"outward": (1, 4), "nearest": (2, 3), "inward": (2, 3)}
        for mode, boundaries in expected.items():
            with self.subTest(mode=mode):
                cuts, _ = self.normalize([["0.05", "0.11"]], quantize=mode)
                self.assertEqual(cuts, [render.FrameRange(*boundaries)])

    def test_rational_fps_and_millisecond_boundaries(self):
        cuts, _ = self.normalize([{"start_ms": "100.1", "end_ms": "200.2"}],
                                 fps=Fraction(30000, 1001))
        self.assertEqual(cuts, [render.FrameRange(3, 6)])
        self.assertEqual(render.frame_to_sample(30, 44100, Fraction(30000, 1001)), 44144)

    def test_empty_plan_preserves_all_frames(self):
        cuts, raw_count = self.normalize([])
        self.assertEqual((cuts, raw_count), ([], 0))
        self.assertEqual(render.complement(cuts, 60), [render.FrameRange(0, 60)])

    def test_invalid_boundaries_are_rejected_without_clipping(self):
        entries = [
            {"start_frame": -1, "end_frame": 2},
            {"start_frame": 58, "end_frame": 61},
            {"start_frame": 10, "end_frame": 10},
            {"start_frame": 11, "end_frame": 10},
            {"start_frame": True, "end_frame": 3},
            {"start_frame": "1.5", "end_frame": 3},
            {"start_frame": 1},
            {"start": "-0.01", "end": "0.1"},
            {"start": "1.9", "end": "2.01"},
            {"start": "0.1", "end": "Infinity"},
        ]
        for entry in entries:
            with self.subTest(entry=entry), self.assertRaises(render.RenderError):
                self.normalize([entry])

    def test_entire_video_cannot_be_removed(self):
        with self.assertRaisesRegex(render.RenderError, "entire video"):
            self.normalize([{"start_frame": 0, "end_frame": 60}])

    def test_padding_cannot_silently_extend_past_source(self):
        with self.assertRaisesRegex(render.RenderError, "no silent clipping"):
            self.normalize([{"start_frame": 0, "end_frame": 3}], pad_before_ms=Decimal(1))

    def test_invalid_adjustments_are_rejected(self):
        for option in ({"merge_gap_frames": -1}, {"pad_before_ms": Decimal(-1)},
                       {"pad_after_ms": Decimal("NaN")}):
            with self.subTest(option=option), self.assertRaises(render.RenderError):
                self.normalize([], **option)

    def test_plan_counts_use_merged_ranges(self):
        cuts, raw_count = self.normalize([{"start_frame": 10, "end_frame": 20}])
        plan = render.make_plan(source=Path("synthetic.mp4"), fps=Fraction(30), frame_count=60,
                                cuts=cuts, keep=render.complement(cuts, 60), raw_count=raw_count,
                                sample_rate=44100, fade_ms=Decimal(3))
        self.assertEqual(plan["kept_frames"], 50)
        self.assertEqual(plan["removed_frames"], 10)
        self.assertEqual(plan["expected_output_samples"], 73500)


class PathProtectionTests(TemporaryFilesTest):
    def setUp(self):
        super().setUp()
        self.source = self.file("source.mp4")
        self.cuts = self.file("cuts.json", b"[]")

    def args(self, **overrides):
        values = dict(source=self.source, cuts=self.cuts, output=self.root / "output.mp4",
                      plan_out=self.root / "plan.json", overwrite=False, execute=True)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_source_cannot_be_output_even_with_overwrite(self):
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                with self.assertRaisesRegex(render.RenderError, "must be different files"):
                    render.validate_paths(self.args(output=self.source, overwrite=overwrite))
        self.assertEqual(self.source.read_bytes(), b"input must remain unchanged")

    def test_plan_cannot_overwrite_cuts(self):
        with self.assertRaisesRegex(render.RenderError, "must be different files"):
            render.validate_paths(self.args(plan_out=self.cuts, overwrite=True))

    def test_resolved_path_alias_is_rejected(self):
        alias = self.root / "subfolder" / ".." / "source.mp4"
        with self.assertRaisesRegex(render.RenderError, "must be different files"):
            render.validate_paths(self.args(output=alias, overwrite=True))

    def test_hard_link_alias_is_rejected(self):
        alias = self.root / "linked.mp4"
        try:
            os.link(self.source, alias)
        except OSError as error:
            self.skipTest(f"Hard links unavailable: {error}")
        with self.assertRaisesRegex(render.RenderError, "must be different files"):
            render.validate_paths(self.args(output=alias, overwrite=True))

    def test_existing_output_needs_explicit_overwrite(self):
        output = self.file("output.mp4", b"existing result")
        with self.assertRaisesRegex(render.RenderError, "already exists"):
            render.validate_paths(self.args(output=output))
        self.assertEqual(output.read_bytes(), b"existing result")
        render.validate_paths(self.args(output=output, overwrite=True))

    def test_execute_requires_output(self):
        with self.assertRaisesRegex(render.RenderError, "required with --execute"):
            render.validate_paths(self.args(output=None))

    def test_invalid_output_extensions_are_rejected(self):
        for options in ({"output": self.root / "output.mov"},
                        {"plan_out": self.root / "plan.txt"}):
            with self.subTest(options=options), self.assertRaises(render.RenderError):
                render.validate_paths(self.args(**options))


class CandidateTests(unittest.TestCase):
    def test_candidates_are_bounded_and_not_approved_edits(self):
        spans = [
            {"token": "▁就", "center": .08, "start": .06, "end": .10},
            {"token": "是", "center": .14, "start": .12, "end": .16},
            {"token": "好", "center": .30, "start": .28, "end": .32},
        ]
        transcript, candidates = scan.find_candidates(spans, ["就是", "呃"], .4, 30)
        self.assertEqual(transcript, "就是好")
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["term"], "就是")
        self.assertEqual(candidate["status"], "candidate_only_semantic_and_acoustic_review_required")
        self.assertGreaterEqual(candidate["candidate_start_frame"], 0)
        self.assertLessEqual(candidate["candidate_end_frame"], 12)
        self.assertNotIn("start_frame", candidate)
        self.assertNotIn("end_frame", candidate)

    def test_invalid_fps_is_rejected(self):
        self.assertAlmostEqual(scan.parse_fps("30000/1001"), 30000 / 1001)
        for value in ("0", "-1", "NaN", "1/0"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                scan.parse_fps(value)


class SyntheticMediaSmokeTests(TemporaryFilesTest):
    def setUp(self):
        super().setUp()
        configured = os.environ.get("FLOWCUT_FFMPEG")
        executable = configured or shutil.which("ffmpeg")
        if not executable:
            self.skipTest("FFmpeg unavailable; set FLOWCUT_FFMPEG or add it to PATH")
        self.ffmpeg = Path(executable).expanduser().resolve()
        if not self.ffmpeg.is_file():
            self.fail("FLOWCUT_FFMPEG does not identify an existing file")
        configured_probe = os.environ.get("FLOWCUT_FFPROBE")
        sibling = self.ffmpeg.with_name("ffprobe.exe" if self.ffmpeg.suffix.lower() == ".exe" else "ffprobe")
        probe = configured_probe or (str(sibling) if sibling.is_file() else shutil.which("ffprobe"))
        if not probe:
            self.skipTest("FFprobe unavailable; set FLOWCUT_FFPROBE or add it to PATH")
        self.ffprobe = Path(probe).expanduser().resolve()
        if not self.ffprobe.is_file():
            self.fail("FLOWCUT_FFPROBE does not identify an existing file")
        inventory = self.run_command([self.ffmpeg, "-hide_banner", "-encoders"])
        if "libx264" not in inventory.stdout:
            self.skipTest("Synthetic smoke test requires FFmpeg's libx264 encoder")

    def run_command(self, command, expected=0):
        result = subprocess.run([str(item) for item in command], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=90)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_synthetic_dry_run_execute_and_source_protection(self):
        source = self.root / "synthetic source.mp4"
        cuts = self.root / "cuts.json"
        output = self.root / "edited.mp4"
        plan_path = self.root / "plan.json"
        self.run_command([
            self.ffmpeg, "-hide_banner", "-v", "error", "-n",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-vf", "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
            "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-c:a", "aac", "-ac", "1", "-ar", "48000",
            "-movflags", "+faststart", source,
        ])
        source_digest = hashlib.sha256(source.read_bytes()).digest()
        cuts.write_text(json.dumps({"fps": 30, "frame_count": 60, "cuts": [
            {"start_frame": 10, "end_frame": 20},
            {"start_frame": 40, "end_frame": 45},
        ]}), encoding="utf-8")
        cuts_digest = hashlib.sha256(cuts.read_bytes()).digest()
        command = [sys.executable, SCRIPTS / "render_cuts.py", "--source", source,
                   "--cuts", cuts, "--output", output, "--ffmpeg", self.ffmpeg,
                   "--ffprobe", self.ffprobe, "--encoder", "libx264",
                   "--sample-rate", "48000", "--channels", "1"]
        dry_run = self.run_command(command + ["--plan-out", plan_path])
        self.assertIn("Dry-run only", dry_run.stderr)
        self.assertFalse(output.exists())
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["kept_frames"], 45)
        self.assertEqual(plan["removed_frames"], 15)
        self.assertEqual(plan["expected_output_samples"], 72000)
        executed = self.run_command(command + ["--execute"])
        self.assertIn('"status": "rendered"', executed.stdout)
        self.assertTrue(output.is_file())
        probe = self.run_command([self.ffprobe, "-v", "error", "-show_streams", "-of", "json", output])
        streams = json.loads(probe.stdout)["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        self.assertEqual(int(video["nb_frames"]), 45)
        self.assertEqual(Fraction(video["avg_frame_rate"]), Fraction(30))
        self.assertEqual(audio["sample_rate"], "48000")
        self.assertEqual(audio["channels"], 1)
        samples = Fraction(audio["duration_ts"]) * Fraction(audio["time_base"]) * 48000
        self.assertLessEqual(abs(samples - 72000), 1)
        existing_digest = hashlib.sha256(output.read_bytes()).digest()
        rejected = self.run_command(command + ["--execute"], expected=2)
        self.assertIn("already exists", rejected.stderr)
        self.assertEqual(hashlib.sha256(output.read_bytes()).digest(), existing_digest)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), source_digest)
        self.assertEqual(hashlib.sha256(cuts.read_bytes()).digest(), cuts_digest)


if __name__ == "__main__":
    unittest.main()
