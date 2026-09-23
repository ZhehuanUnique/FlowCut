#!/usr/bin/env python3
"""Locate low-energy pause candidates in PCM16 WAV; never approve deletions.

The input is analyzed per channel, without mixing down. Low-energy windows must
pass BOTH RMS and peak limits. Any above-limit window splits a run: even very
short transients, breaths, or soft speech are not bridged. Background music can
hide pauses, and speech quieter than both thresholds can look like silence.
Review semantics, phonetic boundaries, breaths and video before making cuts.
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import sys
import wave

try:
    import numpy as np
except ModuleNotFoundError:  # Keep --help and skill discovery usable without optional NumPy.
    np = None


def positive_fraction(value: str) -> Fraction:
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError("expected a positive number or ratio") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def dbfs(value: float) -> float | None:
    """JSON null means digital silence; avoid nonstandard -Infinity literals."""
    return round(20 * math.log10(value), 4) if value > 0 else None


def ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def ratio(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def window_levels(wav: wave.Wave_read, window_samples: int):
    """Yield nonoverlapping windows, retaining the possibly short final window."""
    channel_count = wav.getnchannels()
    position = 0
    while True:
        data = wav.readframes(window_samples * 4096)
        if not data:
            break
        samples = np.frombuffer(data, dtype="<i2").reshape(-1, channel_count)
        full_count, remainder = divmod(len(samples), window_samples)
        if full_count:
            normalized = samples[: full_count * window_samples].astype(np.float64) / 32768.0
            blocks = normalized.reshape(full_count, window_samples, channel_count)
            # Taking the maximum across channels protects speech in either side;
            # stereo cancellation must never turn speech into apparent silence.
            rms_levels = np.sqrt(np.mean(blocks * blocks, axis=1)).max(axis=1)
            peak_levels = np.abs(blocks).max(axis=(1, 2))
            for rms, peak in zip(rms_levels, peak_levels):
                yield position, position + window_samples, float(rms), float(peak)
                position += window_samples
        if remainder:
            tail = samples[-remainder:].astype(np.float64) / 32768.0
            rms = np.sqrt(np.mean(tail * tail, axis=0)).max()
            peak = np.abs(tail).max()
            yield position, position + remainder, float(rms), float(peak)
            position += remainder
    if position != wav.getnframes():
        raise ValueError(f"truncated WAV: expected {wav.getnframes()} samples, read {position}")


def scan_audio(
    audio: Path,
    fps: Fraction,
    *,
    rms_dbfs: float = -40.0,
    peak_dbfs: float | None = None,
    min_pause_seconds: Fraction = Fraction(6, 5),
    window_ms: Fraction = Fraction(10),
) -> dict:
    """Return a review-only report. All sample/time ranges are half-open."""
    if np is None:
        raise RuntimeError("NumPy is required for pause scanning; install numpy in the selected Python environment")
    audio = Path(audio).resolve(strict=True)
    fps = Fraction(fps)
    min_pause_seconds = Fraction(min_pause_seconds)
    window_ms = Fraction(window_ms)
    if fps <= 0 or min_pause_seconds <= 0 or window_ms <= 0:
        raise ValueError("fps, minimum pause length and window length must be positive")
    peak_dbfs = rms_dbfs if peak_dbfs is None else peak_dbfs
    if not all(math.isfinite(v) and v <= 0 for v in (rms_dbfs, peak_dbfs)):
        raise ValueError("RMS and peak thresholds must be finite dBFS values <= 0")
    rms_limit, peak_limit = 10 ** (rms_dbfs / 20), 10 ** (peak_dbfs / 20)

    with wave.open(str(audio), "rb") as wav:
        if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
            raise ValueError("only uncompressed signed PCM16 WAV is supported")
        rate, channels, sample_count = wav.getframerate(), wav.getnchannels(), wav.getnframes()
        if rate <= 0 or channels <= 0 or sample_count <= 0:
            raise ValueError("input must contain at least one valid audio sample")
        window_samples = max(1, round(Fraction(rate) * window_ms / 1000))
        minimum_samples = ceil_fraction(min_pause_seconds * rate)
        candidates: list[dict] = []
        run_start: int | None = None
        run_end = 0
        run_rms = run_peak = 0.0
        low_sample_count = window_count = 0
        longest_run = 0

        def finish_run():
            nonlocal run_start, run_rms, run_peak, longest_run
            if run_start is None:
                return
            length = run_end - run_start
            longest_run = max(longest_run, length)
            if length >= minimum_samples:
                start_time = Fraction(run_start, rate)
                end_time = Fraction(run_end, rate)
                # Inward rounding includes only complete video-frame intervals
                # contained in the acoustic candidate. These are NOT cut points.
                first_frame = ceil_fraction(start_time * fps)
                after_last_frame = (end_time * fps).numerator // (end_time * fps).denominator
                edge = "whole_file" if run_start == 0 and run_end == sample_count else (
                    "leading" if run_start == 0 else "trailing" if run_end == sample_count else "internal"
                )
                candidates.append({
                    "id": f"pause-{len(candidates) + 1:04d}",
                    "status": "unreviewed_candidate_not_an_approved_cut",
                    "edge": edge,
                    "start_sample": run_start,
                    "end_sample": run_end,
                    "start_seconds": float(start_time),
                    "end_seconds": float(end_time),
                    "duration_seconds": length / rate,
                    "start_time_exact": ratio(start_time),
                    "end_time_exact": ratio(end_time),
                    "frame_interval_inward": {
                        "start_frame": first_frame,
                        "end_frame": max(first_frame, after_last_frame),
                        "empty": after_last_frame <= first_frame,
                    },
                    "maximum_window_rms_dbfs": dbfs(run_rms),
                    "maximum_peak_dbfs": dbfs(run_peak),
                    "bridged_intervals": [],
                    "requires": ["speech_and_breath_boundary_review", "semantic_pause_review", "visual_join_review"],
                })
            run_start = None
            run_rms = run_peak = 0.0

        for start, end, rms, peak in window_levels(wav, window_samples):
            window_count += 1
            if rms <= rms_limit and peak <= peak_limit:
                low_sample_count += end - start
                if run_start is None:
                    run_start = start
                run_end = end
                run_rms, run_peak = max(run_rms, rms), max(run_peak, peak)
            else:
                finish_run()
        finish_run()

    return {
        "schema": "flowcut.pause-candidates.v1",
        "purpose": "review_only_not_a_render_plan",
        "source": str(audio),
        "duration_seconds": sample_count / rate,
        "duration_exact": ratio(Fraction(sample_count, rate)),
        "audio": {"sample_rate": rate, "channels": channels, "sample_width_bits": 16, "sample_count": sample_count},
        "settings": {
            "fps": ratio(fps),
            "rms_threshold_dbfs": rms_dbfs,
            "peak_threshold_dbfs": peak_dbfs,
            "min_pause_seconds": float(min_pause_seconds),
            "requested_window_ms": float(window_ms),
            "actual_window_samples": window_samples,
            "actual_window_ms": 1000 * window_samples / rate,
            "channel_policy": "all_channels_must_pass_both_limits_no_mixdown",
            "bridging": "disabled_any_above_limit_window_splits_a_run",
        },
        "coordinate_system": {
            "intervals": "half_open_start_inclusive_end_exclusive",
            "timeline": "relative_to_WAV_sample_zero_not_automatically_the_original_video",
            "frame_rounding": "ceil_start_floor_end_complete_frames_inside_candidate",
            "boundary_resolution_samples": window_samples,
        },
        "statistics": {
            "windows_scanned": window_count,
            "low_energy_sample_count": low_sample_count,
            "longest_low_energy_run_seconds": longest_run / rate,
            "candidate_count": len(candidates),
            "candidate_seconds": sum(item["duration_seconds"] for item in candidates),
        },
        "warnings": [
            "Energy detects candidates, not speech or approved deletions. Preserve word attacks, tails, breaths and meaningful emphasis.",
            "Soft speech below both limits can be classified as low energy; music or noise can hide real pauses. Tune thresholds for this recording.",
            "Leading and trailing candidates are included. Keep natural handles after review; never remove every candidate wholesale.",
            "Nonzero source audio offsets or excerpts require an explicit mapping to the original video before a separate approved cut plan is built.",
            "Thresholds and minimum duration are starting points, not universal editorial rules. No gaps are automatically bridged.",
        ],
        "pause_candidates": candidates,
    }


def write_report(report: dict, output: Path, source: Path, *, overwrite: bool = False):
    output = Path(output).resolve()
    source = Path(source).resolve(strict=True)
    if output == source or (output.exists() and output.samefile(source)):
        raise ValueError("output must never replace the input audio")
    # Exclusive creation by default avoids races with another task's output.
    with output.open("w" if overwrite else "x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True, help="local uncompressed PCM16 WAV (unchanged by this tool)")
    parser.add_argument("--out", type=Path, required=True, help="new review-only JSON; parent directory must exist")
    parser.add_argument("--fps", type=positive_fraction, required=True, help="exact source CFR, e.g. 60 or 30000/1001; not a VFR conversion")
    parser.add_argument("--rms-dbfs", type=float, default=-40.0, help="maximum window RMS (default -40; tune for this recording)")
    parser.add_argument("--peak-dbfs", type=float, default=None, help="maximum sample peak; defaults to the RMS limit for conservative scanning")
    parser.add_argument("--min-pause", type=positive_fraction, default=Fraction(6, 5), help="minimum candidate seconds (default 1.2; not a cut rule)")
    parser.add_argument("--window-ms", type=positive_fraction, default=Fraction(10), help="nonoverlapping analysis window milliseconds (default 10)")
    parser.add_argument("--overwrite", action="store_true", help="explicitly replace an existing report, never the input")
    args = parser.parse_args(argv)
    try:
        source = args.audio.resolve(strict=True)
        output = args.out.resolve()
        if source == output or (output.exists() and output.samefile(source)):
            raise ValueError("output must never replace the input audio")
        if output.exists() and not args.overwrite:
            raise FileExistsError(f"output already exists: {output}; choose a new name or pass --overwrite")
        report = scan_audio(source, args.fps, rms_dbfs=args.rms_dbfs, peak_dbfs=args.peak_dbfs,
                            min_pause_seconds=args.min_pause, window_ms=args.window_ms)
        write_report(report, output, source, overwrite=args.overwrite)
    except (OSError, ValueError, RuntimeError, wave.Error, EOFError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"Saved {report['statistics']['candidate_count']} unreviewed pause candidates to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
