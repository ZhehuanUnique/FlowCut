"""Synthetic pause-scanner checks; no personal media or speech model required."""

from __future__ import annotations

from array import array
from fractions import Fraction
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "flowcut" / "scripts" / "scan_pauses.py"
SPEC = importlib.util.spec_from_file_location("flowcut_scan_pauses", SCRIPT)
pause = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pause
SPEC.loader.exec_module(pause)


@unittest.skipUnless(pause.np is not None, "NumPy unavailable; pause-scanner behavior tests skipped")
class PauseScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flowcut-pause-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rate = 8000

    def wav(self, values, name="input.wav", channels=1, sample_width=2):
        path = self.root / name
        samples = array("h", values)
        if sys.byteorder != "little":
            samples.byteswap()
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(self.rate)
            wav_file.writeframes(samples.tobytes())
        return path

    def silence(self, seconds, channels=1):
        return [0] * (round(seconds * self.rate) * channels)

    def tone(self, seconds, amplitude=8192):
        count = round(seconds * self.rate)
        return [round(amplitude * math.sin(2 * math.pi * 250 * index / self.rate)) for index in range(count)]

    @staticmethod
    def stereo(left, right):
        return [sample for pair in zip(left, right) for sample in pair]

    def test_normal_includes_leading_internal_trailing(self):
        source = self.wav(self.silence(1.3) + self.tone(.5) + self.silence(1.5)
                          + self.tone(.5) + self.silence(1.4))
        report = pause.scan_audio(source, Fraction(60))
        candidates = report["pause_candidates"]
        self.assertEqual(report["schema"], "flowcut.pause-candidates.v1")
        self.assertEqual([item["edge"] for item in candidates], ["leading", "internal", "trailing"])
        self.assertAlmostEqual(candidates[1]["start_seconds"], 1.8)
        self.assertAlmostEqual(candidates[1]["end_seconds"], 3.3)
        self.assertNotIn("cuts", report)
        self.assertTrue(all(item["status"].startswith("unreviewed") for item in candidates))

    def test_whole_file_silence_and_partial_final_window(self):
        source = self.wav(self.silence(1.2345))
        report = pause.scan_audio(source, Fraction(60))
        candidate, = report["pause_candidates"]
        self.assertEqual(candidate["edge"], "whole_file")
        self.assertEqual(candidate["end_sample"], 9876)
        self.assertIsNone(candidate["maximum_peak_dbfs"])
        json.dumps(report, allow_nan=False)

    def test_no_silence(self):
        self.assertEqual(pause.scan_audio(self.wav(self.tone(3)), Fraction(60))["pause_candidates"], [])

    def test_short_soft_sound_is_not_bridged(self):
        source = self.wav(self.silence(.9) + self.tone(.02, 580) + self.silence(.9))
        report = pause.scan_audio(source, Fraction(60))
        self.assertEqual(report["pause_candidates"], [])
        self.assertAlmostEqual(report["statistics"]["longest_low_energy_run_seconds"], .9)

    def test_single_peak_protects_when_rms_below_limit(self):
        sound = self.silence(2)
        sound[self.rate] = 500
        self.assertEqual(pause.scan_audio(self.wav(sound), Fraction(60))["pause_candidates"], [])

    def test_stereo_one_side_and_phase_cancellation_are_protected(self):
        tone = self.tone(2)
        silence = self.silence(2)
        one_side = pause.scan_audio(self.wav(self.stereo(silence, tone), channels=2), Fraction(60))
        self.assertEqual(one_side["audio"]["channels"], 2)
        self.assertEqual(one_side["pause_candidates"], [])
        opposite = [-sample for sample in tone]
        cancelled = pause.scan_audio(
            self.wav(self.stereo(tone, opposite), name="opposite.wav", channels=2), Fraction(60)
        )
        self.assertEqual(cancelled["pause_candidates"], [])

    def test_exact_fractional_fps_inward_bounds(self):
        source = self.wav(self.tone(.11) + self.silence(1.3) + self.tone(.2))
        report = pause.scan_audio(source, Fraction(30000, 1001))
        candidate, = report["pause_candidates"]
        self.assertEqual(report["settings"]["fps"], "30000/1001")
        self.assertEqual(candidate["frame_interval_inward"], {"start_frame": 4, "end_frame": 42, "empty": False})
        self.assertEqual(candidate["start_time_exact"], "11/100")
        self.assertEqual(candidate["end_time_exact"], "141/100")

    def test_source_and_existing_output_protection(self):
        source = self.wav(self.silence(2))
        original = source.read_bytes()
        report = pause.scan_audio(source, Fraction(60))
        with self.assertRaises(ValueError):
            pause.write_report(report, source, source, overwrite=True)
        self.assertEqual(source.read_bytes(), original)
        output = self.root / "report.json"
        pause.write_report(report, output, source)
        with self.assertRaises(FileExistsError):
            pause.write_report(report, output, source)
        pause.write_report(report, output, source, overwrite=True)
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["purpose"], "review_only_not_a_render_plan")

    def test_reject_non_pcm16_and_invalid_thresholds(self):
        eight_bit = self.root / "eight-bit.wav"
        with wave.open(str(eight_bit), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(1)
            wav_file.setframerate(self.rate)
            wav_file.writeframes(bytes([128]) * 16000)
        with self.assertRaises(ValueError):
            pause.scan_audio(eight_bit, Fraction(60))
        source = self.wav(self.silence(2), name="valid.wav")
        for invalid in (1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                pause.scan_audio(source, Fraction(60), rms_dbfs=invalid)


class PauseScannerDependencyTests(unittest.TestCase):
    def test_help_works_without_site_packages(self):
        result = subprocess.run(
            [sys.executable, "-S", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--min-pause", result.stdout)

    def test_missing_numpy_is_actionable(self):
        with tempfile.TemporaryDirectory(prefix="flowcut-pause-dependency-test-") as directory:
            source = Path(directory) / "silence.wav"
            output = Path(directory) / "report.json"
            with wave.open(str(source), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                wav_file.writeframes(bytes(16000))
            with patch.object(pause, "np", None), self.assertRaisesRegex(RuntimeError, "NumPy is required"):
                pause.scan_audio(source, Fraction(60))
            result = subprocess.run(
                [sys.executable, "-S", str(SCRIPT), "--audio", str(source),
                 "--out", str(output), "--fps", "60"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("NumPy is required", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
