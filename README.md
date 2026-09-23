# FlowCut

Natural talking-head editing, guided by meaning—not a word blacklist.

A reusable AI skill for polishing Chinese talking-head videos: choose complete retakes, remove redundant filler words, shorten no-information recording waits, and smooth jump cuts while preserving meaning, voice, and subtitles.

[中文说明](README.zh-CN.md) · [Skill instructions](skills/flowcut/SKILL.md)

## What it does

- Reviews filler words such as **就是、然后、啊、呃、我觉得** in context. Meaningful transitions and subjective qualifiers stay.
- Resolves false starts and repeated takes by complete semantic units instead of matching repeated words.
- Reviews the whole retained timeline for no-information waits, including leading/trailing space and pauses away from retake points.
- Refines pauses and breathing without clipping consonants, word endings, or intentional emphasis. Playback speed remains unchanged unless the current request explicitly asks for a separate speed change.
- Improves cut decisions using neighboring speech, movement, framing, and subtitle state. Authorized B-roll may cover difficult cuts.
- Preserves the original source, produces a separate edited version, and checks the actual export.

FlowCut is an **agent workflow with optional local helpers**, not a standalone editor or a one-click transcription service. It does not include a speech model, FFmpeg, private media, or API credentials. Dubbing, translation, fabricated evidence, and publishing are not part of its default workflow.

## Use the skill

The installable folder is [`skills/flowcut`](skills/flowcut). Keep `SKILL.md`, `references/`, and `scripts/` together.

**Naming:** FlowCut is the public project and package name. In a talking-head editing context, “口播Skill” and `koubo-skill` are natural-language aliases for this workflow. This repository's canonical skill ID remains `flowcut`, so explicit invocation after installing this package is `$flowcut`. FlowCut Studio requests for intro/outro modules are outside this skill.

1. Download or clone this repository.
2. Install the `flowcut` folder using your agent's supported skill-loading mechanism.
3. Give the agent a source video and your editing preferences. Review its proposed cuts before export.

Example request:

> Use FlowCut to polish this Chinese talking-head video. Keep the final complete version of repeated takes, review redundant 就是、然后、啊、呃、我觉得 in context, and shorten no-information recording waits without clipping speech. Preserve my meaning, voice, subtitles, original speed, and original file. Explain uncertain cuts and verify the exported result.

中文示例：

> 用 FlowCut（口播Skill / koubo-skill）精剪这段口播。保留重复录制中最后一次完整表达，逐项复核口头语，并清理全片无信息空等；默认保持原速，保护字头字尾，不覆盖原片，导出后检查接点和字幕。

### Host compatibility

The core instructions are not tied to an AI provider. A host still needs local file access, command execution, suitable speech-analysis tools, and a way to review media.

- The workflow was developed and used with **Codex on Windows**.
- Other agent hosts and operating systems have **not been end-to-end verified**.
- `agents/openai.yaml` is optional, product-specific display metadata. It is not required by the core workflow.
- For a Codex repository-scoped setup, copy the folder to `.agents/skills/flowcut`; then invoke `$flowcut`. See the [official skill documentation](https://learn.chatgpt.com/docs/build-skills) for current discovery locations and installation options.

## Optional local helpers

### Render reviewed cuts

[`render_cuts.py`](skills/flowcut/scripts/render_cuts.py) applies an already-reviewed cut plan. It does **not** decide which words to remove, generate B-roll, or rewrite subtitles.

Requirements: Python **3.10+**, FFmpeg and FFprobe. The renderer uses only Python's standard library. FFmpeg **9.0.1** was tested; older versions are not claimed compatible (the command uses `-/filter_complex`). Pass explicit executable paths or put FFmpeg on `PATH`; FFprobe is expected beside FFmpeg unless supplied separately.

Supported input is intentionally narrow: a non-fragmented CFR MP4, one progressive square-pixel 8-bit BT.709 limited-range H.264 video track, and one mono/stereo audio track, both starting at zero. VFR, HDR, rotation, unknown color metadata, and extra tracks require a separately reviewed workflow. Burned-in subtitles are carried with the video, not regenerated.

Create an approved JSON plan using **source-frame** half-open intervals `[start_frame, end_frame)`. The numbers below are illustrative, not recommended cuts for your video:

```json
{
  "fps": 30,
  "cuts": [
    {"start_frame": 300, "end_frame": 306, "reason": "Reviewed redundant filler"}
  ]
}
```

```sh
python skills/flowcut/scripts/render_cuts.py --help
python skills/flowcut/scripts/render_cuts.py --source input.mp4 --cuts approved-cuts.json --output edited.mp4 --sample-rate 48000 --channels 2
```

The second command is a **dry run**. Review the result, then add `--execute` to render a new output file. Use the source's actual sample rate and channel count: `48000` and `2` above are examples; the script defaults are `44100` and `2`. `--fps` asserts a rate; it does not convert VFR. Do not use padding or gap merging to expand approved edits without review.

### Scan acoustic candidates

[`scan_ctc.py`](skills/flowcut/scripts/scan_ctc.py) is an **optional, model-specific** scanner, not a general ASR backend. It requires NumPy, ONNX Runtime, `kaldi-native-fbank`, and the exact locally available model/token fingerprints documented in the script. Model files and runtime packages are not distributed here or downloaded automatically.

It emits **candidates, not approved cuts**. Do not transfer its 50 ms calibration to another model. Without the matching model, use another authorized speech-analysis tool and independently review timing. See [local tools and limitations](skills/flowcut/references/local-tools.md).

### Scan low-energy pause candidates

[`scan_pauses.py`](skills/flowcut/scripts/scan_pauses.py) scans each channel of a PCM16 WAV without mixing down and reports long low-energy regions for review. It requires Python 3.10+ and NumPy, but no speech model. Thresholds and durations are starting points for the current recording—not automatic cut rules.

```sh
mkdir -p work
python skills/flowcut/scripts/scan_pauses.py --audio review.wav --out work/pause-candidates.json --fps 30000/1001 --min-pause 1.2 --rms-dbfs -40 --peak-dbfs -40
```

The report deliberately contains no approved `cuts`. It records the resolved source path, so keep it in the ignored private `work/` directory and do not publish it. Check the neighboring words, breaths, semantics, image, and timeline mapping before creating a separate render plan. Re-scan the actual exported file, not only the pre-render analysis audio.

## Validation

Run the tests from the repository root:

```sh
python -m unittest discover -s tests -v
```

Pause-scanner tests run when NumPy is installed. The synthetic-media integration test needs FFmpeg/FFprobe; use `FLOWCUT_FFMPEG` if FFmpeg is not on `PATH`. Tests cannot certify that a real speech edit sounds natural. Each exported user video still needs semantic, acoustic, visual, and synchronization checks.

## Privacy and scope

The included helpers run locally and contain no upload logic. A host or optional transcription service may have its own data handling; obtain permission before sending private media to an external service. Keep recordings, transcripts, credentials, generated output, and proprietary model files out of public commits. The included `.gitignore` helps prevent accidental additions; it is not a substitute for reviewing staged files.

The referenced [影视飓风 tutorial](https://www.bilibili.com/video/BV1qDAbeGETw/) is credited in [reference notes](skills/flowcut/references/reference-video.md). This repository contains summarized editing principles, not the video or its transcript. No affiliation or endorsement is implied.

No license has been selected for this repository yet. Third-party dependencies and models are not included; consult their own terms separately.
