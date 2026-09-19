#!/usr/bin/env python3
"""Render frame-accurate filler-word cuts without a large split/concat graph.

The video path is decoded and encoded once.  A single ``select`` expression
drops half-open frame ranges, then ``setpts`` closes the gaps.  Audio is first
decoded once to stereo PCM, copied by exact frame-aligned sample ranges, and
given a tiny linear fade on both sides of every edit before one AAC encode.

The command is dry-run by default.  Pass --execute to create the output.
Supported input is an ordinary, non-fragmented MP4 with one progressive,
square-pixel, 8-bit BT.709 limited-range H.264 video track and one mono/stereo
audio track, both starting at zero. The whole presentation timeline is checked
for CFR; --fps is an assertion, never a conversion. VFR, HDR, rotation, and
unknown color metadata require an explicit, separately reviewed conversion.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from array import array
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Sequence


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrameRange:
    """Half-open frame range [start, end)."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Mp4VideoInfo:
    frame_count: int
    fps: Fraction | None


@dataclass(frozen=True)
class Box:
    kind: bytes
    payload_start: int
    end: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove half-open cut ranges from one MP4. Dry-run by default; "
            "pass --execute to render."
        )
    )
    parser.add_argument("--cuts", required=True, type=Path, help="Approved cuts JSON")
    parser.add_argument("--source", required=True, type=Path, help="Source MP4")
    parser.add_argument("--output", type=Path, help="Destination MP4 (required with --execute)")
    parser.add_argument("--work-dir", type=Path, help="Directory for temporary PCM/filter files")
    parser.add_argument("--ffmpeg", type=Path, help="FFmpeg executable")
    parser.add_argument("--ffprobe", type=Path, help="FFprobe executable (default: beside FFmpeg)")
    parser.add_argument("--fps", type=str, help="Assert source FPS, e.g. 30 or 30000/1001")
    parser.add_argument("--frame-count", type=int, help="Assert exact input video frame count")
    parser.add_argument(
        "--quantize",
        choices=("outward", "nearest", "inward"),
        default="outward",
        help="How second-only ranges are mapped to frames (default: outward)",
    )
    parser.add_argument(
        "--merge-gap-frames",
        type=int,
        default=0,
        help="Also remove gaps this many frames or shorter (default: 0; touching only)",
    )
    parser.add_argument("--pad-before-ms", type=Decimal, default=Decimal("0"))
    parser.add_argument("--pad-after-ms", type=Decimal, default=Decimal("0"))
    parser.add_argument(
        "--fade-ms",
        type=Decimal,
        default=Decimal("3"),
        help="Linear audio fade on each side of an edit (default: 3 ms)",
    )
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument(
        "--encoder",
        choices=("auto", "libx264", "h264_nvenc"),
        default="auto",
        help="Video encoder (auto prefers libx264, then NVENC)",
    )
    parser.add_argument("--quality", type=int, default=18, help="CRF/CQ quality value")
    parser.add_argument("--audio-bitrate", default="256k")
    parser.add_argument("--execute", action="store_true", help="Actually render the destination")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--plan-out", type=Path, help="Optional normalized plan JSON output")
    return parser.parse_args()


def run(cmd: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    printable = subprocess.list2cmdline([str(x) for x in cmd])
    print(f"+ {printable}", file=sys.stderr)
    try:
        return subprocess.run(
            [str(x) for x in cmd],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
    except subprocess.CalledProcessError as exc:
        detail = f"\n{exc.stdout}" if capture and exc.stdout else ""
        raise RenderError(f"Command failed with exit code {exc.returncode}: {printable}{detail}") from exc
    except OSError as exc:
        raise RenderError(f"Cannot run {cmd[0]}: {exc}") from exc


def find_ffmpeg(explicit: Path | None) -> Path:
    if explicit:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise RenderError(f"FFmpeg not found: {path}")
        return path
    on_path = shutil.which("ffmpeg")
    if on_path:
        return Path(on_path).resolve()
    raise RenderError("No FFmpeg executable found on PATH; pass --ffmpeg explicitly")


def find_ffprobe(explicit: Path | None, ffmpeg: Path) -> Path:
    path = explicit.expanduser().resolve() if explicit else ffmpeg.with_name(
        "ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe"
    )
    if not path.is_file():
        raise RenderError(f"FFprobe not found: {path}; pass --ffprobe explicitly")
    return path


def same_file(left: Path, right: Path) -> bool:
    """Resolve symlinks and detect hard links, including Windows case aliases."""
    if left.resolve() == right.resolve():
        return True
    return left.exists() and right.exists() and os.path.samefile(left, right)


def validate_paths(args: argparse.Namespace) -> None:
    named = [("source", args.source), ("cuts", args.cuts)]
    if args.output is not None:
        named.append(("output", args.output))
    if args.plan_out is not None:
        named.append(("plan-out", args.plan_out))
    for index, (name, path) in enumerate(named):
        for other_name, other in named[:index]:
            if same_file(path, other):
                raise RenderError(f"--{name} and --{other_name} must be different files: {path}")
    for name, path in named[2:]:
        if path.exists() and not path.is_file():
            raise RenderError(f"--{name} is not a regular file: {path}")
        if path.exists() and not args.overwrite:
            raise RenderError(f"--{name} already exists (pass --overwrite): {path}")
    if args.source.suffix.lower() != ".mp4":
        raise RenderError("Only .mp4 source files are supported")
    if args.output is not None and args.output.suffix.lower() != ".mp4":
        raise RenderError("--output must end in .mp4")
    if args.plan_out is not None and args.plan_out.suffix.lower() != ".json":
        raise RenderError("--plan-out must end in .json")
    if args.execute and args.output is None:
        raise RenderError("--output is required with --execute")


def ffprobe_json(ffprobe: Path, path: Path, *options: str) -> dict[str, object]:
    result = run([str(ffprobe), "-v", "error", *options, "-of", "json", str(path)], capture=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"FFprobe returned invalid JSON for {path}") from exc


def audit_source(ffprobe: Path, source: Path) -> Mp4VideoInfo:
    """Prove the supported format and exact presentation cadence; never infer 30 fps."""
    table_info = probe_mp4_video(source)
    if not table_info or not table_info.fps:
        raise RenderError("Cannot prove constant MP4 sample timing (VFR/fragmented/unknown input); preconvert explicitly")
    info = ffprobe_json(ffprobe, source, "-show_streams", "-show_format")
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(streams) != 2 or len(video) != 1 or len(audio) != 1:
        raise RenderError("Input must have exactly one video and one audio stream; select/convert other tracks explicitly")
    v, a = video[0], audio[0]
    if v.get("codec_name") != "h264" or v.get("pix_fmt") != "yuv420p":
        raise RenderError("Only 8-bit yuv420p H.264 input is supported; no implicit HDR/bit-depth conversion")
    required = {"color_range": "tv", "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709"}
    if any(v.get(key) != value for key, value in required.items()):
        raise RenderError("Explicit BT.709 limited-range SDR color tags are required; unknown/HDR colors must be reviewed separately")
    if v.get("field_order") != "progressive" or v.get("sample_aspect_ratio") != "1:1":
        raise RenderError("Only progressive, square-pixel input is supported")
    for side_data in v.get("side_data_list", []):
        if any(term in str(side_data).lower() for term in ("display matrix", "rotation", "mastering", "content light", "dovi", "hdr")):
            raise RenderError("Rotation/HDR side data is unsupported; preconvert explicitly")
    if "rotate" in v.get("tags", {}):
        raise RenderError("Rotation metadata is unsupported; preconvert explicitly")
    if a.get("channels") not in (1, 2):
        raise RenderError("Only mono/stereo audio is supported")
    for label, stream in (("video", v), ("audio", a)):
        if str(stream.get("start_pts")) != "0":
            raise RenderError(f"{label} must start at timestamp zero; offset timelines need explicit normalization")
    fps = table_info.fps
    if parse_fps(v.get("avg_frame_rate")) != fps or parse_fps(v.get("r_frame_rate")) != fps:
        raise RenderError("MP4 and FFprobe disagree about FPS; VFR/ambiguous input is unsupported")
    time_base = Fraction(v["time_base"])
    step = Fraction(1, 1) / fps / time_base
    if step.denominator != 1:
        raise RenderError("Frame period is not exactly representable in the video time base")
    packets = ffprobe_json(ffprobe, source, "-select_streams", "v:0", "-show_packets", "-show_entries", "packet=pts,duration").get("packets", [])
    if len(packets) != table_info.frame_count or any("pts" not in packet for packet in packets):
        raise RenderError("Packet/frame count mismatch or missing PTS; cannot prove a frame-accurate timeline")
    presentation = sorted(int(packet["pts"]) for packet in packets)
    if any(pts != index * step.numerator for index, pts in enumerate(presentation)):
        raise RenderError("Nonuniform presentation timestamps (VFR/gaps/duplicates) are unsupported; preconvert explicitly")
    if any(int(packet.get("duration", 0)) != step.numerator for packet in packets):
        raise RenderError("Nonuniform/missing packet duration is unsupported")
    return table_info


def iter_boxes(fp: BinaryIO, start: int, end: int) -> Iterator[Box]:
    pos = start
    while pos + 8 <= end:
        fp.seek(pos)
        header = fp.read(8)
        if len(header) != 8:
            return
        size32, kind = struct.unpack(">I4s", header)
        header_size = 8
        if size32 == 1:
            ext = fp.read(8)
            if len(ext) != 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size32 == 0:
            size = end - pos
        else:
            size = size32
        if size < header_size or pos + size > end:
            return
        yield Box(kind, pos + header_size, pos + size)
        pos += size


def first_box(fp: BinaryIO, start: int, end: int, kind: bytes) -> Box | None:
    return next((box for box in iter_boxes(fp, start, end) if box.kind == kind), None)


def parse_mdhd_timescale(fp: BinaryIO, mdhd: Box) -> int | None:
    fp.seek(mdhd.payload_start)
    head = fp.read(24)
    if len(head) < 16:
        return None
    version = head[0]
    offset = 20 if version == 1 else 12
    if len(head) < offset + 4:
        return None
    return struct.unpack_from(">I", head, offset)[0]


def parse_stts_fps(fp: BinaryIO, stts: Box, timescale: int) -> Fraction | None:
    fp.seek(stts.payload_start)
    header = fp.read(8)
    if len(header) != 8:
        return None
    entry_count = struct.unpack_from(">I", header, 4)[0]
    if entry_count == 0 or stts.payload_start + 8 + entry_count * 8 > stts.end:
        return None
    entries: list[tuple[int, int]] = []
    for _ in range(entry_count):
        raw = fp.read(8)
        if len(raw) != 8:
            return None
        entries.append(struct.unpack(">II", raw))
    if any(count <= 0 or delta <= 0 for count, delta in entries):
        return None
    deltas = {delta for count, delta in entries}
    if len(deltas) != 1:
        return None
    return Fraction(timescale, deltas.pop())


def parse_sample_count(fp: BinaryIO, stbl: Box) -> int | None:
    stsz = first_box(fp, stbl.payload_start, stbl.end, b"stsz")
    if stsz:
        fp.seek(stsz.payload_start)
        header = fp.read(12)
        if len(header) == 12:
            return struct.unpack_from(">I", header, 8)[0]
    stz2 = first_box(fp, stbl.payload_start, stbl.end, b"stz2")
    if stz2:
        fp.seek(stz2.payload_start)
        header = fp.read(12)
        if len(header) == 12:
            return struct.unpack_from(">I", header, 8)[0]
    return None


def probe_mp4_video(path: Path) -> Mp4VideoInfo | None:
    """Read the video track's MP4 sample count without decoding the movie."""
    try:
        file_size = path.stat().st_size
        with path.open("rb") as fp:
            moov = first_box(fp, 0, file_size, b"moov")
            if not moov:
                return None
            for trak in iter_boxes(fp, moov.payload_start, moov.end):
                if trak.kind != b"trak":
                    continue
                mdia = first_box(fp, trak.payload_start, trak.end, b"mdia")
                if not mdia:
                    continue
                hdlr = first_box(fp, mdia.payload_start, mdia.end, b"hdlr")
                if not hdlr:
                    continue
                fp.seek(hdlr.payload_start + 8)
                if fp.read(4) != b"vide":
                    continue
                minf = first_box(fp, mdia.payload_start, mdia.end, b"minf")
                mdhd = first_box(fp, mdia.payload_start, mdia.end, b"mdhd")
                if not minf:
                    continue
                stbl = first_box(fp, minf.payload_start, minf.end, b"stbl")
                if not stbl:
                    continue
                frame_count = parse_sample_count(fp, stbl)
                if not frame_count:
                    continue
                fps = None
                if mdhd:
                    timescale = parse_mdhd_timescale(fp, mdhd)
                    stts = first_box(fp, stbl.payload_start, stbl.end, b"stts")
                    if timescale and stts:
                        fps = parse_stts_fps(fp, stts, timescale)
                return Mp4VideoInfo(frame_count, fps)
    except (OSError, struct.error):
        return None
    return None


def decimal_fraction(value: Decimal | str | int | float) -> Fraction:
    if isinstance(value, Decimal):
        return Fraction(value)
    return Fraction(str(value))


def parse_fps(value: object) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise RenderError(f"Invalid FPS value: {value!r}")
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise RenderError(f"Invalid FPS value: {value!r}") from exc
    if result <= 0:
        raise RenderError("FPS must be positive")
    return result


def exact_integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise RenderError(f"{label} must be an exact integer, not a boolean")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise RenderError(f"{label} must be an exact integer: {value!r}") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise RenderError(f"{label} must be an exact integer: {value!r}")
    return int(number)


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"Cannot read cuts JSON {path}: {exc}") from exc


def get_time_seconds(item: dict[str, object], prefix: str) -> Decimal:
    direct_keys = (prefix, f"{prefix}_s", f"{prefix}_seconds")
    for key in direct_keys:
        if key in item:
            value = Decimal(str(item[key]))
            if not value.is_finite():
                raise RenderError(f"{key} must be finite")
            return value
    ms_key = f"{prefix}_ms"
    if ms_key in item:
        value = Decimal(str(item[ms_key])) / Decimal(1000)
        if not value.is_finite():
            raise RenderError(f"{ms_key} must be finite")
        return value
    raise RenderError(f"Cut is missing {prefix}/{prefix}_frame: {item!r}")


def quantize_frame(seconds: Decimal, fps: Fraction, mode: str, is_end: bool) -> int:
    exact = seconds * Decimal(fps.numerator) / Decimal(fps.denominator)
    if mode == "nearest":
        rounding = ROUND_HALF_UP
    elif mode == "outward":
        rounding = ROUND_CEILING if is_end else ROUND_FLOOR
    else:  # inward
        rounding = ROUND_FLOOR if is_end else ROUND_CEILING
    return int(exact.to_integral_value(rounding=rounding))


def raw_cut_items(document: object) -> tuple[list[object], dict[str, object]]:
    if isinstance(document, list):
        return document, {}
    if not isinstance(document, dict):
        raise RenderError("Cuts JSON must be a list or an object containing cuts[]")
    cuts = document.get("cuts")
    if not isinstance(cuts, list):
        raise RenderError("Cuts JSON object must contain a cuts[] list")
    return cuts, document


def normalize_cuts(
    raw_items: Iterable[object],
    *,
    fps: Fraction,
    frame_count: int,
    quantize: str,
    pad_before_ms: Decimal,
    pad_after_ms: Decimal,
    merge_gap_frames: int,
) -> tuple[list[FrameRange], int]:
    if merge_gap_frames < 0:
        raise RenderError("--merge-gap-frames cannot be negative")
    if not pad_before_ms.is_finite() or not pad_after_ms.is_finite():
        raise RenderError("Padding must be finite")
    if pad_before_ms < 0 or pad_after_ms < 0:
        raise RenderError("Padding cannot be negative")
    ranges: list[FrameRange] = []
    raw_count = 0
    before_s = pad_before_ms / Decimal(1000)
    after_s = pad_after_ms / Decimal(1000)
    for raw in raw_items:
        raw_count += 1
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            item: dict[str, object] = {"start": raw[0], "end": raw[1]}
        elif isinstance(raw, dict):
            item = raw
        else:
            raise RenderError(f"Invalid cut entry #{raw_count}: {raw!r}")

        if "start_frame" in item or "end_frame" in item:
            if "start_frame" not in item or "end_frame" not in item:
                raise RenderError(f"Cut #{raw_count} needs both start_frame and end_frame")
            start = exact_integer(item["start_frame"], f"Cut #{raw_count} start_frame")
            end = exact_integer(item["end_frame"], f"Cut #{raw_count} end_frame")
            if before_s:
                start -= quantize_frame(before_s, fps, "outward", True)
            if after_s:
                end += quantize_frame(after_s, fps, "outward", True)
        else:
            start_s = get_time_seconds(item, "start") - before_s
            end_s = get_time_seconds(item, "end") + after_s
            duration = Fraction(frame_count, 1) / fps
            if start_s < 0 or Fraction(end_s) > duration:
                raise RenderError(f"Cut #{raw_count} seconds/padding exceed the source timeline")
            if end_s <= start_s:
                raise RenderError(f"Cut #{raw_count} has empty/reversed second boundaries")
            start = quantize_frame(start_s, fps, quantize, False)
            end = quantize_frame(end_s, fps, quantize, True)

        if start < 0 or end > frame_count:
            raise RenderError(f"Cut #{raw_count} [{start}, {end}) exceeds [0, {frame_count}); no silent clipping is allowed")
        if end <= start:
            raise RenderError(
                f"Cut #{raw_count} becomes empty/reversed after quantization: [{start}, {end})"
            )
        ranges.append(FrameRange(start, end))

    ranges.sort(key=lambda r: (r.start, r.end))
    merged: list[FrameRange] = []
    for current in ranges:
        if not merged or current.start > merged[-1].end + merge_gap_frames:
            merged.append(current)
        else:
            previous = merged[-1]
            merged[-1] = FrameRange(previous.start, max(previous.end, current.end))
    if merged and sum(item.length for item in merged) >= frame_count:
        raise RenderError("The cut plan removes the entire video")
    return merged, raw_count


def complement(cuts: Sequence[FrameRange], frame_count: int) -> list[FrameRange]:
    keep: list[FrameRange] = []
    cursor = 0
    for cut in cuts:
        if cursor < cut.start:
            keep.append(FrameRange(cursor, cut.start))
        cursor = cut.end
    if cursor < frame_count:
        keep.append(FrameRange(cursor, frame_count))
    return keep


def frame_to_seconds(frame: int, fps: Fraction) -> str:
    return f"{float(Fraction(frame, 1) / fps):.6f}"


def make_plan(
    *,
    source: Path,
    fps: Fraction,
    frame_count: int,
    cuts: Sequence[FrameRange],
    keep: Sequence[FrameRange],
    raw_count: int,
    sample_rate: int,
    fade_ms: Decimal,
) -> dict[str, object]:
    kept_frames = sum(item.length for item in keep)
    removed_frames = frame_count - kept_frames
    samples_per_frame = Fraction(sample_rate, 1) / fps
    expected_samples = frame_to_sample(kept_frames, sample_rate, fps)
    return {
        "source": str(source),
        "fps": str(fps) if fps.denominator != 1 else fps.numerator,
        "frame_count": frame_count,
        "input_duration_seconds": frame_to_seconds(frame_count, fps),
        "raw_cut_count": raw_count,
        "merged_cut_count": len(cuts),
        "cuts": [
            {
                "start_frame": item.start,
                "end_frame": item.end,
                "start": frame_to_seconds(item.start, fps),
                "end": frame_to_seconds(item.end, fps),
            }
            for item in cuts
        ],
        "keep_range_count": len(keep),
        "kept_frames": kept_frames,
        "removed_frames": removed_frames,
        "removed_seconds": frame_to_seconds(removed_frames, fps),
        "output_duration_seconds": frame_to_seconds(kept_frames, fps),
        "sample_rate": sample_rate,
        "samples_per_frame": (
            samples_per_frame.numerator
            if samples_per_frame.denominator == 1
            else str(samples_per_frame)
        ),
        "expected_output_samples": expected_samples,
        "audio_fade_ms": str(fade_ms),
        "audio_rounding": "round-half-up on cumulative kept frames; at most one sample repeated/dropped per keep range",
        "validated_input": "H.264 8-bit yuv420p, progressive square-pixel, BT.709 limited SDR, zero-start single audio/video, audited CFR PTS",
    }


def encoder_inventory(ffmpeg: Path) -> str:
    return run([str(ffmpeg), "-hide_banner", "-encoders"], capture=True).stdout or ""


def choose_encoder(requested: str, inventory: str) -> str:
    available = {line.split()[1] for line in inventory.splitlines() if len(line.split()) >= 2}
    if requested != "auto":
        if requested not in available:
            raise RenderError(f"Requested encoder is unavailable: {requested}")
        return requested
    if "libx264" in available:
        return "libx264"
    if "h264_nvenc" in available:
        return "h264_nvenc"
    raise RenderError("Neither libx264 nor h264_nvenc is available in this FFmpeg build")


def video_encoder_args(encoder: str, quality: int) -> list[str]:
    if not 0 <= quality <= 51:
        raise RenderError("--quality must be between 0 and 51")
    if encoder == "libx264":
        return [
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            str(quality),
            "-profile:v",
            "high",
        ]
    return [
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p6",
        "-tune",
        "hq",
        "-rc",
        "vbr",
        "-cq",
        str(quality),
        "-b:v",
        "12M",
        "-maxrate:v",
        "24M",
        "-bufsize:v",
        "24M",
        "-multipass",
        "fullres",
        "-spatial_aq",
        "1",
        "-temporal_aq",
        "1",
        "-aq-strength",
        "8",
        "-profile:v",
        "high",
    ]


def make_video_filter(cuts: Sequence[FrameRange], fps: Fraction) -> str:
    if cuts:
        terms = [f"between(n\\,{cut.start}\\,{cut.end - 1})" for cut in cuts]
        select = "not(" + "+".join(terms) + ")"
    else:
        select = "1"
    if fps.denominator == 1:
        pts = f"N/({fps.numerator}*TB)"
    else:
        pts = f"N*{fps.denominator}/({fps.numerator}*TB)"
    return f"[0:v:0]select='{select}',setpts={pts}[v]\n"


def frame_to_sample(frame: int, sample_rate: int, fps: Fraction) -> int:
    exact = Fraction(frame * sample_rate * fps.denominator, fps.numerator)
    quotient, remainder = divmod(exact.numerator, exact.denominator)
    return quotient + (1 if remainder * 2 >= exact.denominator else 0)


def apply_gain(samples: array, channels: int, frame_index: int, gain: float) -> None:
    base = frame_index * channels
    for channel in range(channels):
        samples[base + channel] = int(round(samples[base + channel] * gain))


def copy_pcm_with_fades(
    source_wav: Path,
    output_wav: Path,
    keep: Sequence[FrameRange],
    *,
    fps: Fraction,
    sample_rate: int,
    channels: int,
    fade_ms: Decimal,
) -> int:
    requested_fade = int(
        (fade_ms * Decimal(sample_rate) / Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP)
    )
    chunk_frames = 65536
    total_written = 0
    kept_frames_so_far = 0
    with wave.open(str(source_wav), "rb") as src:
        if src.getsampwidth() != 2:
            raise RenderError(f"Expected 16-bit PCM, got {src.getsampwidth() * 8}-bit")
        if src.getframerate() != sample_rate or src.getnchannels() != channels:
            raise RenderError(
                f"Unexpected PCM format: {src.getframerate()} Hz, {src.getnchannels()} channels"
            )
        required_samples = frame_to_sample(keep[-1].end, sample_rate, fps)
        if src.getnframes() < required_samples:
            raise RenderError(
                f"Decoded audio is too short: {src.getnframes()} samples; need {required_samples}"
            )

        with wave.open(str(output_wav), "wb") as dst:
            dst.setnchannels(channels)
            dst.setsampwidth(2)
            dst.setframerate(sample_rate)

            for segment_index, frame_range in enumerate(keep):
                sample_start = frame_to_sample(frame_range.start, sample_rate, fps)
                sample_end = frame_to_sample(frame_range.end, sample_rate, fps)
                source_segment_samples = sample_end - sample_start
                kept_frames_so_far += frame_range.length
                segment_samples = frame_to_sample(kept_frames_so_far, sample_rate, fps) - total_written
                if segment_samples <= 0 or abs(segment_samples - source_segment_samples) > 1:
                    raise RenderError("Unsupported sample quantization difference at a cut")
                fade_in = requested_fade if segment_index > 0 or frame_range.start > 0 else 0
                fade_out = requested_fade if segment_index < len(keep) - 1 else 0
                if fade_in and fade_out:
                    fade_in = fade_out = min(requested_fade, segment_samples // 2)
                elif fade_in:
                    fade_in = min(fade_in, segment_samples)
                elif fade_out:
                    fade_out = min(fade_out, segment_samples)

                src.setpos(sample_start)
                copied = 0
                last_sample = b""
                while copied < segment_samples:
                    count = min(chunk_frames, segment_samples - copied)
                    # Cumulative output rounding prevents fractional-rate cuts
                    # from accumulating sync drift. Never read a deleted sample:
                    # duplicate the last retained sample if this range needs +1.
                    read_count = min(count, max(0, source_segment_samples - copied))
                    raw = src.readframes(read_count)
                    if len(raw) != read_count * channels * 2:
                        raise RenderError("Unexpected end of decoded PCM")
                    if raw:
                        last_sample = raw[-channels * 2:]
                    if read_count < count:
                        if count - read_count != 1 or not last_sample:
                            raise RenderError("Unexpected PCM quantization padding")
                        raw += last_sample
                    values = array("h")
                    values.frombytes(raw)
                    if sys.byteorder != "little":
                        values.byteswap()

                    local_start = copied
                    local_end = copied + count
                    if fade_in > 0 and local_start < fade_in:
                        stop = min(local_end, fade_in)
                        denominator = max(1, fade_in - 1)
                        for absolute in range(local_start, stop):
                            apply_gain(values, channels, absolute - local_start, absolute / denominator)
                    fade_out_start = segment_samples - fade_out
                    if fade_out > 0 and local_end > fade_out_start:
                        start = max(local_start, fade_out_start)
                        denominator = max(1, fade_out - 1)
                        for absolute in range(start, local_end):
                            gain = (segment_samples - 1 - absolute) / denominator
                            apply_gain(values, channels, absolute - local_start, max(0.0, gain))

                    if sys.byteorder != "little":
                        values.byteswap()
                    dst.writeframesraw(values.tobytes())
                    copied += count
                total_written += segment_samples
    return total_written


def safe_remove_temp(temp_dir: Path, work_parent: Path) -> None:
    """Only remove the exact unique direct child created by this invocation."""
    resolved = temp_dir.resolve()
    parent = work_parent.resolve()
    if resolved == parent or resolved.parent != parent or not resolved.name.startswith("koubo-render-"):
        raise RenderError(f"Refusing unsafe temporary cleanup outside the chosen work parent: {resolved}")
    if temp_dir.is_symlink() or (hasattr(temp_dir, "is_junction") and temp_dir.is_junction()):
        raise RenderError(f"Refusing to recursively remove a redirected temporary directory: {temp_dir}")
    if resolved.exists():
        shutil.rmtree(resolved)


def verify_render(ffprobe: Path, output: Path, *, fps: Fraction, frames: int, samples: int, sample_rate: int) -> None:
    observed = audit_source(ffprobe, output)
    if observed.frame_count != frames or observed.fps != fps:
        raise RenderError(f"Output frame verification failed: expected {frames} frames at {fps}, got {observed}")
    streams = ffprobe_json(ffprobe, output, "-show_streams")["streams"]
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    if int(audio["sample_rate"]) != sample_rate:
        raise RenderError("Output audio sample rate changed unexpectedly")
    declared_samples = Fraction(int(audio["duration_ts"])) * Fraction(audio["time_base"]) * sample_rate
    # AAC padding is real at the packet/decoder level, but the MP4 effective
    # audio duration must reflect the edited PCM, not those padded AAC samples.
    if abs(declared_samples - samples) > 1:
        raise RenderError(f"Output effective audio duration mismatch: {declared_samples} vs {samples} samples")


def render(
    *,
    args: argparse.Namespace,
    ffmpeg: Path,
    ffprobe: Path,
    encoder: str,
    fps: Fraction,
    frame_count: int,
    cuts: Sequence[FrameRange],
    keep: Sequence[FrameRange],
) -> None:
    if args.output is None:
        raise RenderError("--output is required with --execute")
    output = args.output.expanduser().resolve()
    source = args.source.expanduser().resolve()
    if output == source:
        raise RenderError("Output must not overwrite the source")
    if output.exists() and not args.overwrite:
        raise RenderError(f"Output already exists (pass --overwrite): {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    work_parent = args.work_dir.expanduser().resolve() if args.work_dir else output.parent
    work_parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix="koubo-render-", dir=str(work_parent))
    temp_dir = Path(temporary).resolve()
    pcm_source = temp_dir / "source_pcm.wav"
    pcm_edited = temp_dir / "edited_pcm.wav"
    filter_script = temp_dir / "video_filter.txt"
    try:
        run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "pcm_s16le",
                "-ar",
                str(args.sample_rate),
                "-ac",
                str(args.channels),
                str(pcm_source),
            ]
        )
        written_samples = copy_pcm_with_fades(
            pcm_source,
            pcm_edited,
            keep,
            fps=fps,
            sample_rate=args.sample_rate,
            channels=args.channels,
            fade_ms=args.fade_ms,
        )
        expected_samples = frame_to_sample(sum(item.length for item in keep), args.sample_rate, fps)
        if written_samples != expected_samples:
            raise RenderError(
                f"Internal sample-count mismatch: wrote {written_samples}, expected {expected_samples}"
            )

        filter_script.write_text(make_video_filter(cuts, fps), encoding="utf-8")
        fps_text = str(fps.numerator) if fps.denominator == 1 else f"{fps.numerator}/{fps.denominator}"
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-y" if args.overwrite else "-n",
            "-i",
            str(source),
            "-i",
            str(pcm_edited),
            "-/filter_complex",
            str(filter_script),
            "-map",
            "[v]",
            "-map",
            "1:a:0",
            *video_encoder_args(encoder, args.quality),
            "-pix_fmt",
            "yuv420p",
            "-r",
            fps_text,
            "-fps_mode:v",
            "cfr",
            "-color_range:v",
            "tv",
            "-colorspace:v",
            "bt709",
            "-color_primaries:v",
            "bt709",
            "-color_trc:v",
            "bt709",
            "-c:a",
            "aac",
            "-b:a",
            args.audio_bitrate,
            "-ar",
            str(args.sample_rate),
            "-ac",
            str(args.channels),
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            "-movie_timescale",
            str(args.sample_rate),
            str(output),
        ]
        run(command)
        verify_render(
            ffprobe, output, fps=fps, frames=sum(item.length for item in keep),
            samples=written_samples, sample_rate=args.sample_rate,
        )
        print(
            json.dumps(
                {
                    "status": "rendered",
                    "output": str(output),
                    "encoder": encoder,
                    "kept_frames": sum(item.length for item in keep),
                    "audio_samples": written_samples,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        if args.keep_temp:
            print(f"Temporary files kept at: {temp_dir}", file=sys.stderr)
        else:
            safe_remove_temp(temp_dir, work_parent)


def main() -> int:
    args = parse_args()
    try:
        for name in ("source", "cuts", "output", "plan_out", "work_dir"):
            value = getattr(args, name)
            if value is not None:
                setattr(args, name, value.expanduser().resolve())
        source = args.source
        cuts_path = args.cuts
        validate_paths(args)
        if not source.is_file():
            raise RenderError(f"Source not found: {source}")
        if not cuts_path.is_file():
            raise RenderError(f"Cuts JSON not found: {cuts_path}")
        if not 8000 <= args.sample_rate <= 192000 or args.channels not in (1, 2):
            raise RenderError("Supported output audio is 8000–192000 Hz, mono/stereo")
        if not args.fade_ms.is_finite() or not Decimal(0) <= args.fade_ms <= Decimal(20):
            raise RenderError("--fade-ms must be finite and between 0 and 20 ms")
        if not 0 <= args.quality <= 51:
            raise RenderError("--quality must be between 0 and 51")

        document = load_json(cuts_path)
        raw_items, metadata = raw_cut_items(document)
        ffmpeg = find_ffmpeg(args.ffmpeg)
        ffprobe = find_ffprobe(args.ffprobe, ffmpeg)
        mp4_info = audit_source(ffprobe, source)
        fps = mp4_info.fps
        frame_count = mp4_info.frame_count
        if fps is None:
            raise RenderError("Source FPS is unknown; an override cannot establish CFR")
        if frame_count <= 0:
            raise RenderError("Frame count must be positive")
        if fps > 240:
            raise RenderError("Input FPS above 240 is outside the supported scope")
        for label, value in (("--fps", args.fps), ("plan fps", metadata.get("fps"))):
            if value is not None and parse_fps(value) != fps:
                raise RenderError(f"FPS mismatch: source is {fps}, {label} says {value}")
        if args.frame_count is not None and args.frame_count != frame_count:
            raise RenderError(
                f"Frame-count mismatch: source has {frame_count}, --frame-count says {args.frame_count}"
            )
        if "frame_count" in metadata and exact_integer(metadata["frame_count"], "plan frame_count") != frame_count:
            raise RenderError("Plan frame_count does not match the source")
        if "source" in metadata:
            planned_source = Path(str(metadata["source"]))
            if not same_file(planned_source, source):
                raise RenderError(f"Plan source does not match --source: {planned_source} != {source}")

        cuts, raw_count = normalize_cuts(
            raw_items,
            fps=fps,
            frame_count=frame_count,
            quantize=args.quantize,
            pad_before_ms=args.pad_before_ms,
            pad_after_ms=args.pad_after_ms,
            merge_gap_frames=args.merge_gap_frames,
        )
        keep = complement(cuts, frame_count)
        plan = make_plan(
            source=source,
            fps=fps,
            frame_count=frame_count,
            cuts=cuts,
            keep=keep,
            raw_count=raw_count,
            sample_rate=args.sample_rate,
            fade_ms=args.fade_ms,
        )
        plan_text = json.dumps(plan, ensure_ascii=False, indent=2)
        print(plan_text)
        if args.plan_out:
            args.plan_out.parent.mkdir(parents=True, exist_ok=True)
            args.plan_out.write_text(plan_text + "\n", encoding="utf-8")

        if not args.execute:
            print("Dry-run only. Pass --execute to render.", file=sys.stderr)
            return 0

        encoder = choose_encoder(args.encoder, encoder_inventory(ffmpeg))
        render(
            args=args,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            encoder=encoder,
            fps=fps,
            frame_count=frame_count,
            cuts=cuts,
            keep=keep,
        )
        return 0
    except (RenderError, OSError, ValueError, InvalidOperation, ZeroDivisionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
