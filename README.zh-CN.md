# FlowCut · 口播精剪

让表达自然流畅，而不是删字最多。

FlowCut 是用于中文口播精剪的可复用 AI Skill：按完整语义处理重录与重说，清理冗余口头语和无信息空等，改善气口与跳切，同时保护原意、说话人的声音和字幕。

[English](README.md) · [Skill 指令](skills/flowcut/SKILL.md)

## 能做什么

- 逐项复核“就是、然后、啊、呃、我觉得”，不把连接词、强调或主观限定一刀切。
- 识别说错重来、局部重说和整段重录，按完整语义选段，而不是看到相同文字就直接去重。
- 检查整条保留时间轴，包括首尾、读稿等待以及远离重录点的无信息长停顿。
- 结合转写、声学证据和画面寻找切点，保护字头、字尾与必要停顿。
- 按构图、动作和字幕情况选择剪接方式；已有且获准使用的相关 B-roll 可以覆盖难以衔接的画面。
- 保留原片，另存成片，并核对实际导出结果，而不只看剪切计划。

它是“剪辑判断流程＋可选本地辅助脚本”，不是独立剪辑软件，也不是一键自动去口头语工具。默认不更换配音、不翻译、不改写观点、不发布视频。

## 安装与使用

可安装目录是 [`skills/flowcut`](skills/flowcut)。下载仓库后，把整个 `flowcut` 文件夹按你的 AI 工具支持的方式安装，保留其中的引用文件和脚本，不要只复制 `SKILL.md`。

**名称说明：**FlowCut 是仓库和对外项目名；在口播精剪语境中，“口播Skill”和 `koubo-skill` 是同一工作流的常用称呼。本仓库的正式技能标识仍是 `flowcut`，所以安装本仓库版本后的显式调用写作 `$flowcut`。若说的是 FlowCut Studio、片头片尾或模块组合，则不属于本技能。

示例请求：

> 用 FlowCut（口播Skill / koubo-skill）精剪这段口播。保留重复录制中最后一次完整表达，逐项复核口头语，并清理全片无信息空等；默认保持原速，保护字头字尾，不覆盖原片，导出后检查接点和字幕。

核心流程不绑定特定 AI 厂商，但宿主需要读写本地文件、执行命令、分析语音和检查画面的能力。流程曾在 **Windows 上配合 Codex** 使用；尚未对其他宿主和操作系统做完整验证。

Codex 项目级用法：将整个目录放到 `.agents/skills/flowcut`，再使用 `$flowcut`。具体发现路径参见[官方文档](https://learn.chatgpt.com/docs/build-skills)。`agents/openai.yaml` 只是可选的界面元数据，不是核心流程的依赖。

## 工具与限制

### 按已审核计划导出

`render_cuts.py` 需要 Python 3.10+、FFmpeg 和 FFprobe，本身只使用 Python 标准库。已验证 FFmpeg 9.0.1；没有承诺兼容旧版本。

```sh
python skills/flowcut/scripts/render_cuts.py --help
python skills/flowcut/scripts/render_cuts.py --source input.mp4 --cuts approved-cuts.json --output edited.mp4 --sample-rate 48000 --channels 2
```

默认只预演；核对结果后，添加 `--execute` 才导出。剪切计划格式见 [English README](README.md#render-reviewed-cuts)。帧号以原片为准，使用左闭右开区间。

音频参数应填写原片真实采样率和声道数，示例中的 48000 Hz / 双声道不适用于所有素材；脚本默认是 44100 Hz / 双声道。`--fps` 只校验帧率，不负责转换。可以通过参数指定 FFmpeg/FFprobe 路径，不依赖任何特定电脑的剪映安装目录。

该脚本支持范围较窄：非碎片化、恒定帧率 MP4；单条逐行扫描、方形像素、8-bit BT.709 limited-range H.264 视频轨；单条单声道或双声道音轨，音画均从零开始。VFR、HDR、旋转画面、未知色彩标记或额外轨道要另行处理。烧录字幕随画面保留，不会重新生成。

脚本只执行已审核的删剪，不自动批准候选词，不添加 B-roll，也不能替代听感验收。

### 可选声学扫描

`scan_ctc.py` 是特定本地模型的辅助工具，需要 NumPy、ONNX Runtime、`kaldi-native-fbank` 和匹配指纹的模型文件。仓库不附带模型或运行库，不自动下载。

没有匹配模型时，可以使用其他已获准的语音分析工具；不要挪用此模型的 50 ms 校准。扫描结果只是候选，不是自动删剪清单。详见[本地工具说明](skills/flowcut/references/local-tools.md)。

### 长停顿候选扫描

`scan_pauses.py` 逐声道扫描 PCM16 WAV 的长低能量区间，不会混成立体声，也不下载声学模型。它需要 Python 3.10+ 和 NumPy：

```sh
mkdir -p work
python skills/flowcut/scripts/scan_pauses.py --audio review.wav --out work/pause-candidates.json --fps 30000/1001 --min-pause 1.2 --rms-dbfs -40 --peak-dbfs -40
```

结果只用于定位，故意不包含获准的 `cuts`。报告会记录解析后的素材绝对路径，因此应留在已忽略的私有 `work/` 目录，不要公开提交。阈值和时长必须结合本次录音调整；建立删剪计划前仍要核对前后字音、呼吸、语义、画面与时间轴映射。实际成片也要重新解码复扫，不能只检查渲染前的分析音轨。

## 验证

```sh
python -m unittest discover -s tests -v
```

长停顿扫描测试在已安装 NumPy 时运行。合成素材集成测试需要 FFmpeg/FFprobe；FFmpeg 不在 `PATH` 时可以设置 `FLOWCUT_FFMPEG`。这些测试不等于真人口播听感通过。真实成片仍要逐处复核语义、声音边界、字幕和同步。

## 隐私与公开范围

辅助脚本在本地运行，没有上传逻辑。如果宿主或其他转写服务需要把私人素材发送到外部，必须另行取得用户授权。

不要把私人视频、截图、逐字稿、模型、凭据或生成结果提交到公开仓库。`.gitignore` 只提供防误加规则，提交前仍应检查文件清单。

[影视飓风参考视频](https://www.bilibili.com/video/BV1qDAbeGETw/) 的方法摘要与来源写在[参考说明](skills/flowcut/references/reference-video.md)中；仓库不包含该视频或逐字稿，也不代表得到其背书。

本仓库暂未选择许可证；第三方依赖和模型未随仓库分发。
