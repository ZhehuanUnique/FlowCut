# 本地分析与校验

## 先找到已有工具

不要假定 `python`、`ffmpeg`、`ffprobe` 已在 PATH。先只读检查：

```powershell
Get-Command ffmpeg, ffprobe, python -ErrorAction SilentlyContinue | Select-Object Name, Source
```

Windows 的 `python.exe` 可能只是商店占位项，必须用 `--version` 验证；占位项不可作为可用运行时。若宿主提供运行时发现功能，可用它获取已有 Python 路径；例如 Codex 桌面的 `load_workspace_dependencies`（可选，不是通用依赖）。若机器已有 VideoNote，优先检查它的配置或已知安装目录中的 FFmpeg，不扫描整个磁盘、不自动重装。后续命令使用已核实的绝对路径。找不到依赖时说明缺口，请用户选择安装或只做已有证据能支持的分析。

对输入素材做只读探测，并将真实帧率、音轨、色彩信息用于后续判断：

```powershell
& 'VERIFIED_FFPROBE.exe' -v error -show_streams -show_format -of json 'ABSOLUTE_SOURCE.mp4'
```

这里的占位路径必须替换为本次实际核实的路径；平均帧率本身不能证明 CFR，渲染器会另外审计全部视频时间戳。

## 声学候选扫描

`scripts/scan_ctc.py` 只支持经指纹验证的本机剪映 CTC 模型配置 `jianying-local-ctc-795f5b13`。模型文件名为 `asr-model-encoder.onnx` 和 `asr-model-token.txt`；具体 SHA-256 写在脚本中。它们不包含在 Skill 中，不要随 Skill 打包或擅自从未知来源下载。

先无变速地将音频转为单声道 16 kHz PCM16 WAV。运行参数：

```text
python scripts/scan_ctc.py --audio INPUT.wav --out ANALYSIS.json --runtime LOCAL_RUNTIME_DIR --model-dir LOCAL_MODEL_DIR --profile jianying-local-ctc-795f5b13 --fps 30 --targets 就是 然后 啊 呃 我觉得 嗯 哈 哎
```

`--runtime` 指向已有隔离目录，含 `onnxruntime` 和 `kaldi_native_fbank`；Python 还需 NumPy。开发环境在 Windows 上曾验证 ONNX Runtime 1.20.1；这不是对其他环境的兼容承诺。不要为此改系统 Python、驱动或其他项目环境。

扫描采用 40 秒核心窗口和两侧上下文，输出逐字峰及匹配项。CTC 常会漏掉半个词或把语气词识别成同音字；输出中的中点区间仅供复核，不直接送去渲染。没有该本地模型时，可采用已有支持细粒度定位的转写工具，仍须核对语音边界，不套用此脚本的校准。

## 批准计划与渲染

简单剪切计划示例，所有帧号指原片，区间左闭右开：

```json
{
  "source": "ABSOLUTE_SOURCE.mp4",
  "fps": 30,
  "cuts": [
    {"start_frame": 300, "end_frame": 306, "reason": "已复核的冗余语气词"}
  ]
}
```

`scripts/render_cuts.py --help` 显示当前支持范围。先 dry-run 检查，再用 `--execute`。显式传入 FFmpeg、工作目录及不同的输出文件；不要直接复用示例帧号。

此脚本使用 Python 3.10+ 标准库，开发环境已验证 FFmpeg 9.0.1；旧版本是否支持 `-/filter_complex` 等选项需另行确认。先根据原片探测结果显式传入 `--sample-rate` 与 `--channels`，不要依赖脚本的 44100 Hz / 双声道默认值。分析 JSON、文本逐字稿和标准化剪切计划可能包含完整转写及本机绝对路径，应放在私有工作目录，不能直接提交到公开仓库。

该渲染器仅处理已批准的同步删剪及短音频增益渐变，不负责识别口头语、选择气口、添加 B-roll 或重新生成字幕。烧录字幕随画面同步保留；独立字幕轨或字幕重排、HDR、多音轨等超出其支持范围时选择其他合适流程。

## 参考链接

已有 VideoNote 时可用 `inspect_video` 确认身份，再用 `prepare_note_material` 获取文字和画面，`task` 查询结果；已完成的素材任务可直接复用。默认由当前 AI 助手分析，不必另配付费大模型接口。需要登录的内容由用户走正常登录流程，不索取粘贴 Cookie，不绕过权限。
