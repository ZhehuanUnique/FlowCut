"""Find Chinese filler candidates with a fingerprinted local acoustic model.

This program only analyzes mono PCM16/16 kHz WAV audio. It does not edit media.
The selected profile, including its 40 ms step and +50 ms calibration, is valid
only for the model/token hashes checked below. A CTC spike is not a phoneme edge.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
import wave
from fractions import Fraction
from pathlib import Path


PROFILE_NAME = "jianying-local-ctc-795f5b13"
PROFILE = {
    "encoder_name": "asr-model-encoder.onnx",
    "tokens_name": "asr-model-token.txt",
    "encoder_sha256": "795f5b130124d389a1f867cdb1370ea59607706eb5fd203af54410c744131c25",
    "tokens_sha256": "6907215aeb034f6926b26bf8abfd650f756781622480a2342ec1f29b2072cafe",
    "ctc_step_seconds": 0.04,
    "timestamp_offset_seconds": 0.05,
    "sample_rate": 16000,
    "sample_amplitude": "native PCM16 (not normalized to [-1,1])",
    "feature_config": {
        "bins": 80, "frame_length_ms": 25, "frame_shift_ms": 10,
        "window": "povey", "dither": 0, "snip_edges": True,
        "mel_scale": "Kaldi/HTK, not librosa or Slaney",
        "low_freq": 20, "high_freq": 0,
        "normalization": "model-embedded global CMVN",
    },
}
DEFAULT_TARGETS = ("就是", "然后", "啊", "呃", "我觉得")


def parse_fps(value):
    try:
        fps = float(Fraction(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError("fps must be a positive number or ratio") from exc
    if not math.isfinite(fps) or fps <= 0:
        raise argparse.ArgumentTypeError("fps must be positive and finite")
    return fps


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as reader:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def load_tokens(path: Path) -> list[str]:
    indexed = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        token, index = line.rsplit(maxsplit=1)
        indexed[int(index)] = token
    if set(indexed) != set(range(max(indexed) + 1)):
        raise ValueError("The token file must have contiguous zero-based IDs")
    return [indexed[index] for index in range(len(indexed))]


def compute_fbank(samples, sample_rate, np, knf):
    options = knf.FbankOptions()
    options.frame_opts.samp_freq = sample_rate
    options.frame_opts.frame_length_ms = 25.0
    options.frame_opts.frame_shift_ms = 10.0
    options.frame_opts.dither = 0.0
    options.frame_opts.snip_edges = True
    options.frame_opts.window_type = "povey"
    options.mel_opts.num_bins = 80
    options.mel_opts.low_freq = 20.0
    options.mel_opts.high_freq = 0.0
    options.mel_opts.is_librosa = False
    options.mel_opts.use_slaney_mel_scale = False
    options.mel_opts.norm = ""
    fbank = knf.OnlineFbank(options)
    fbank.accept_waveform(sample_rate, samples.astype(np.float32).tolist())
    fbank.input_finished()
    if fbank.num_frames_ready < 1:
        raise ValueError("Audio chunk is too short for the model frontend")
    return np.stack([fbank.get_frame(i) for i in range(fbank.num_frames_ready)]).astype(np.float32)


def collapse(logits, tokens, absolute_start):
    spans = []
    previous = None
    run_start = 0
    step = PROFILE["ctc_step_seconds"]
    offset = PROFILE["timestamp_offset_seconds"]
    for frame, token_id in enumerate(logits.argmax(axis=-1).tolist() + [None]):
        if token_id == previous:
            continue
        if previous is not None and previous != 0:
            spans.append({
                "id": previous, "token": tokens[previous],
                "start": round(absolute_start + run_start * step + offset, 6),
                "end": round(absolute_start + frame * step + offset, 6),
                "center": round(absolute_start + (run_start + frame) * .5 * step + offset, 6),
                "peak_log_score": float(logits[run_start:frame, previous].max()),
            })
        previous, run_start = token_id, frame
    return spans


def find_candidates(spans, targets, duration, fps):
    chars = [(character, i) for i, span in enumerate(spans)
             for character in span["token"].replace("▁", "")]
    stream = "".join(character for character, _ in chars)
    candidates = []
    for target in targets:
        cursor = 0
        while (position := stream.find(target, cursor)) >= 0:
            first_i, last_i = chars[position][1], chars[position + len(target) - 1][1]
            first, last = spans[first_i], spans[last_i]
            previous = spans[first_i - 1] if first_i else None
            following = spans[last_i + 1] if last_i + 1 < len(spans) else None
            start = max(0.0, first["center"] - .20,
                        (previous["center"] + first["center"]) * .5 if previous else 0.0)
            end = min(duration, last["center"] + .20,
                      (last["center"] + following["center"]) * .5 if following else duration)
            start_frame = max(0, math.floor(start * fps + 1e-8))
            end_frame = min(math.floor(duration * fps + 1e-6), math.ceil(end * fps - 1e-8))
            candidates.append({
                "term": target,
                "ctc_first_center": first["center"], "ctc_last_center": last["center"],
                "ctc_token_start": first["start"], "ctc_token_end": last["end"],
                "previous_token": previous, "next_token": following,
                "target_tokens": spans[first_i:last_i + 1],
                "midpoint_start": start, "midpoint_end": end,
                "candidate_start_frame": start_frame, "candidate_end_frame": end_frame,
                "candidate_start": start_frame / fps, "candidate_end": end_frame / fps,
                "context": "".join(s["token"] for s in spans[max(0, first_i-9):last_i+10]).replace("▁", " "),
                "status": "candidate_only_semantic_and_acoustic_review_required",
            })
            cursor = position + 1
    candidates.sort(key=lambda row: (row["ctc_first_center"], row["term"]))
    for index, row in enumerate(candidates, 1):
        row["id"] = index
    return stream, candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True, help="Mono PCM16 16 kHz WAV")
    parser.add_argument("--out", type=Path, required=True, help="Output JSON path; text sidecar is also written")
    parser.add_argument("--runtime", type=Path, required=True, help="Directory containing local onnxruntime and kaldi_native_fbank packages")
    parser.add_argument("--model-dir", type=Path, required=True, help="Directory containing the fingerprinted encoder and token files")
    parser.add_argument("--profile", choices=[PROFILE_NAME], required=True,
                        help="Explicitly select the verified model calibration; hashes are always checked")
    parser.add_argument("--fps", type=parse_fps, required=True, help="Actual source frame rate, e.g. 30 or 30000/1001; never guessed")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--targets", nargs="+", default=list(DEFAULT_TARGETS))
    parser.add_argument("--overwrite", action="store_true", help="Allow replacement of this JSON and its text sidecar")
    args = parser.parse_args()
    if not math.isfinite(args.fps) or args.fps <= 0 or args.threads <= 0:
        parser.error("fps and threads must be positive")
    if not args.runtime.is_dir() or not args.model_dir.is_dir():
        parser.error("runtime and model-dir must be existing local directories")
    if any(not target for target in args.targets):
        parser.error("targets cannot contain an empty string")
    args.out = args.out.resolve()
    if args.out.suffix.lower() != ".json":
        parser.error("out must have a .json extension")
    if (args.out.exists() or args.out.with_suffix(".txt").exists()) and not args.overwrite:
        parser.error("output already exists; select another path or pass --overwrite")
    model = args.model_dir / PROFILE["encoder_name"]
    token_path = args.model_dir / PROFILE["tokens_name"]
    protected = {args.audio.resolve(), model.resolve(), token_path.resolve()}
    if args.out.resolve() in protected or args.out.with_suffix(".txt").resolve() in protected:
        parser.error("analysis outputs must not overwrite audio or model inputs")
    fingerprints = {"encoder_sha256": sha256(model), "tokens_sha256": sha256(token_path)}
    for key, actual in fingerprints.items():
        if actual != PROFILE[key]:
            raise RuntimeError(f"Unverified model profile: {key}={actual}. Do not reuse +50 ms calibration on another model.")
    # Dependencies are isolated locally. No network use or global installation.
    sys.path.insert(0, str(args.runtime.resolve()))
    import numpy as np
    import kaldi_native_fbank as knf
    import onnxruntime as ort

    with wave.open(str(args.audio), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (16000, 1, 2):
            raise ValueError("Expected mono, 16000 Hz, PCM16 WAV. Convert audio without changing its speed first.")
        rate = reader.getframerate()
        audio = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16)
    duration = len(audio) / rate
    if duration < .05:
        raise ValueError("Audio must be at least 50 ms")
    tokens = load_tokens(token_path)
    options = ort.SessionOptions()
    options.intra_op_num_threads = args.threads
    session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
    inputs = {node.name: node.type for node in session.get_inputs()}
    if inputs != {"speech": "tensor(float)", "speech_lengths": "tensor(int32)"}:
        raise RuntimeError(f"Unexpected model input signature: {inputs}")
    all_tokens, chunks = [], []
    for core_start in np.arange(0, duration, 40.0):
        core_end = min(duration, core_start + 40.0)
        analysis_start = max(0.0, core_start - 2.0)
        analysis_end = min(duration, core_end + 2.0)
        features = compute_fbank(audio[round(analysis_start * rate):round(analysis_end * rate)], rate, np, knf)
        began = time.time()
        logits, lengths = session.run(None, {
            "speech": features[None, :, :],
            "speech_lengths": np.asarray([len(features)], dtype=np.int32),
        })
        spans = collapse(logits[0, :int(lengths[0])], tokens, analysis_start)
        accepted = [span for span in spans if core_start <= span["center"] < core_end]
        all_tokens.extend(accepted)
        text = "".join(span["token"] for span in accepted).replace("▁", " ")
        chunks.append({"core_start": float(core_start), "core_end": float(core_end),
                       "analysis_start": float(analysis_start), "analysis_end": float(analysis_end),
                       "inference_seconds": time.time()-began, "text": text,
                       "all_context_tokens": spans})
        print(f"{core_start:.1f}-{core_end:.1f}: {len(accepted)} acoustic tokens", flush=True)
    transcript, occurrences = find_candidates(all_tokens, args.targets, duration, args.fps)
    payload = {
        "schema_version": 1, "source_audio": str(args.audio.resolve()),
        "duration": duration, "sample_rate": rate, "fps": args.fps,
        "profile": args.profile, "calibration": PROFILE, "verified_fingerprints": fingerprints,
        "runtime": {"onnxruntime": ort.__version__, "kaldi_native_fbank": knf.__version__},
        "model": str(model.resolve()), "targets": args.targets,
        "counts": {term: sum(o["term"] == term for o in occurrences) for term in args.targets},
        "warning": "CTC peaks and midpoint frame ranges are review candidates, not edit approval. Zero complete matches do not prove absence of filler fragments. Preserve semantic qualifiers and verify every actual exported join.",
        "text": transcript, "occurrences": occurrences, "tokens": all_tokens, "chunks": chunks,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.out.with_suffix(".txt").write_text("\n".join(
        f"[{chunk['core_start']:.3f}-{chunk['core_end']:.3f}] {chunk['text']}" for chunk in chunks), encoding="utf-8")
    print(json.dumps({"duration": duration, "counts": payload["counts"], "output": str(args.out)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
