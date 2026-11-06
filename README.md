<h1 align="center">Visual Audio Overlay</h1>

<p align="center">
  <b>See what you can't hear.</b><br>
  An accessibility overlay that turns in-game sound into a real-time visual radar,
  built for gamers with single-sided deafness (SSD) or hearing loss.
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-0078D6">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776AB">
  <img alt="License" src="https://img.shields.io/badge/license-FSL--1.1--MIT-blue">
</p>

---

# 🎯 SSD 单耳聋改进版（本分支）

本分支基于 [mike-s-zaugg/VisualAudioOverlay](https://github.com/mike-s-zaugg/VisualAudioOverlay)
（FSL-1.1-MIT 许可），针对**单耳聋玩家在《暗区突围：无限》(Arena Breakout: Infinite)**
的实际痛点做的改进：

## 1. 脚步声明显可见（自适应噪声门 + 瞬态增强）

原版用一个固定的绝对阈值和 50ms 整块 RMS 判声，环境音（雨、风、载具声）稍微一响，
安静脚步就被淹没；显示强度还是线性映射，弱声音几乎不可见。

本分支：

| 机制 | 说明 |
|------|------|
| **自适应噪声门 (Adaptive Gate)** | 用 0.8 秒滑动窗口的最小电平持续估计环境噪声底。雨声等持续环境音会自动"变成背景"，只有高于噪声底的**事件**才点亮雷达；安静时噪声底回落，轻脚步依然醒目。 |
| **瞬态（起音）增强** | 脚步声是短促冲击。分块电平上升越快，额外加的分贝越高（最多 +7dB），脚步声"踢踏"感会明显弹出。 |
| **对数亮度曲线** | 强度按信噪比 dB 映射并开方压缩：高于噪声底 6dB 的轻脚步已清晰可见，27dB 拉满。 |
| **峰值保持 + 残影** | 每个光弧保留约 1.15 秒，强事件带一道内圈残影，连续脚步看起来是"脉动轨迹"而不是一闪而过。 |

## 2. 身后声音能看见了（立体声 HRTF 全 360° 方位）

原版在立体声耳机下只输出 ±90°（纯左右），身后根本不存在；本分支从立体声重建完整 360°：

- **左右**：中/低频段双耳电平差（ILD），按头部遮蔽物理模型（ILD∝sinθ 的反正弦逆映射）；
- **前后**：每个事件 3.5–8kHz / 120–700Hz 能量比与**持续学习的环境基线**对比——
  比环境亮=前方，暗=后方（HRTF 头部遮蔽）。强线索 150ms 翻转，弱线索慢漂移防闪烁。

> **重要前提**：游戏需输出 HRTF/双耳立体声（游戏内选"耳机/3D 音频/空间音频"）。
> 纯平面立体声摆位下前后不可判定，请把 Mode 设为 `Stereo L/R only`。

## 3. 上下方向（高度）

- 6–9.5kHz 陷波带 vs 9.5–14kHz 明亮带之比（对比自适应基线）估计高度，低信噪比自动降置信度；
- 显示：上方=▲箭头+暖色，下方=▼箭头+冷色；真实 7.1.4 回环输出精确高度。

## 4. 战斗声与脚步声区分（物理声学特征）

"战斗声" = 枪声 + 手雷/爆炸类（进攻手雷、燃烧瓶、震撼弹；烟雾弹除外，归杂音）。
战斗声和脚步物理上截然不同，无需游戏素材文件即可稳健区分（游戏 pak 是加密的自定义
封装，无法合法解包，故采用物理特征 + 实机日志校准）：

- 战斗声：波峰因数（峰值/自身 RMS）>14.5dB 且 4kHz 以上高频占比 >0.22；
- 脚步：波峰因数 ≤14dB 且（低频占比 >0.3 或高频占比 ≤0.25）；
- 超过 max_amp 的巨响（爆炸等）无条件判为战斗声类事件；
- 显示：战斗声=**红色宽闪光**（可关闭），脚步=蓝色弧，其他=淡白弧（烟雾弹、被击中、喘息、换弹）。

**Event log**：开启后在程序目录写 `event_log.csv`（每个事件的 snr/波峰/频带占比/
方位/距离），并且把每个枪声/脚步事件的**原始音频片段存成 WAV** 到 `recordings\`
（每局一个 session 文件夹，最多 500 条）。用这些真实录音即可校准阈值。

## 5. 距离显示

- 距离 = f(信噪比)：比环境噪声底响 34dB≈贴脸，6dB≈雷达外环，每 4dB 翻倍；
- 起音越陡越近（近处声音更"干"），明显偏暗的声音推远一点；
- 显示：**光弧到圆心的距离**=远近（中心=近，外环=远），三条虚线为近/中/远参考环。

## 6. 屏蔽玩家自己的脚步声/战斗声

- 自己的战斗声（枪声/手雷投掷）：正前方（±15°）+ 极响（SNR>30dB 或超过 max_amp）→ 屏蔽；
- 自己的脚步：正前方 + 响亮（SNR>24dB）+ **节奏稳定**（连续 4 步、间隔 0.25–0.9s、
  抖动 <35%）→ 屏蔽（节奏检测防止误杀正前方的敌人脚步）；
- 被屏蔽的声音在**圆心闪一个白色小脉冲**作为确认，不占方向显示。

---

# 雷达怎么看（头朝 N）

```
         ▲ 前进方向
        N
   NW ／  ＼ NE        · 光弧方位 = 声音方向（N=前，E=右，S=后，W=左）
  W ｜  你  ｜ E        · 离圆心越近 = 越近（距离）
   SW ＼  ／ SE        · 蓝色弧=脚步 红色闪光=战斗声 淡白弧=其他(含烟雾弹)
        S              · 内圈+▲/暖色=上方  外圈+▼/冷色=下方
                       · 圆心白色脉冲 = 自己的声音（已屏蔽）
```

# 快速上手（暗区突围）

1. 游戏音频设置：耳机输出 + **开启耳机/3D 音频/空间音频**（关键！）。
2. 启动程序 → Preset 选 **Footsteps - ABI**（90–7000Hz 脚步带通 + 增强 2.6×）。
3. RADAR 卡选游戏进程（或 All）→ Start → 拖动雷达到合适位置。

## 推荐调节顺序

| 症状 | 调节 |
|------|------|
| 脚步太暗/太亮 | **Footstep Boost**（先调，默认 2.0x） |
| 环境音也一直亮 | 确认 **Adaptive Gate** 开；等 1–2 秒收敛 |
| 前后方向反了 | **Front/Back Bias**（-1…+1） |
| 枪声不显示/太抢眼 | SOUND TYPES：Gunshots 开关 / 换色 |
| 自己的脚步没被屏蔽 | 确认 Own footsteps 开；Sensitivity 别太低 |
| 想只用左右模式 | Mode → Stereo L/R only |

**单耳听音**：保持 Windows"单声道音频"关闭，用本程序 HARDWARE 卡的 **Mono Output**
（VB-CABLE）把游戏合成单声道送进健康耳，雷达方向不受影响。

# 从源码运行

```bash
pip install -r requirements.txt
python main.py          # Windows 10 19041+ / Python 3.10+
```

# 打包 exe

```bash
pip install pyinstaller
pyinstaller AudioRadarFolder.spec   # 目录版（推荐，避免 QtWebEngine 白屏）
```

# 测试

```bash
python -m unittest discover -s tests      # 41 项全过
python tools/simulate_game.py             # 12 秒合成对局端到端验证（雨+绕圈脚步+枪声+自身脚步/枪声）
python tools/render_overlay_test.py       # 离屏渲染雷达效果图
```

---

# 上游原文（Visual Audio Overlay）

## What it is

Visual Audio Overlay listens to your PC's audio and draws a sleek, transparent,
Fortnite-style circular radar on top of your game. When a sound happens, an arc
lights up in the direction it came from, so you can *see* footsteps, gunshots,
reloads, and ability cues the instant they play.

It runs as a two-part app: a control panel you keep on a second monitor, and the
radar overlay that floats over your game on your main screen.

## Who it's for

- Players with **single-sided deafness** or **hearing loss** who lose directional audio.
- Anyone gaming with one earbud in, or in a quiet household, who still wants positional awareness.
- Competitive players who want footsteps and other key cues filtered out from background noise.

## Features

- **Directional radar overlay.** Transparent, always-on-top, click-through. Arcs
  fade in and out in the direction of each sound.
- **Per-application capture.** Pick a single program (your game) so other apps
  like Discord voice chat are ignored. Uses the Windows WASAPI process-loopback
  API; pick **All (system audio)** to capture everything as before.
- **Game-specific presets.** Built-in frequency band-pass filters (CS2, Valorant,
  Fortnite, and more) isolate footsteps and ignore useless low-end rumble.
- **Mono output for single-sided listeners.** Optionally play a mono down-mix of
  the game to your headphones so you hear everything through your working ear,
  while the overlay still shows full left/right direction. Needs a one-time
  virtual-cable setup; see the Mono Output panel in the app.
- **Smart audio boost.** Amplifies quiet, distant sounds so faint cues still register.
- **Stereo and surround.** 7.1 / multi-channel headsets unlock 360 degree front/back
  detection; stereo headsets run in left/right mode automatically.
- **Saveable presets.** Store a full per-game setup - sensitivities, filters, the
  captured program, monitor, mono output, and radar look - and switch in one click.
- **Full customization.** Pick the radar's accent color and line thickness, with a
  live preview that matches exactly what shows up in game.
- **Non-intrusive by design.** It does not inject into game memory or modify any game
  files. It simply reads the standard Windows audio output (WASAPI loopback).

## How it works

The app captures your system's audio output (the same signal going to your
headphones) and analyzes the balance between channels to estimate the direction
of each sound. That direction is drawn as an arc on the radar. Because it only
reads audio you are already playing, there is no interaction with the game itself.

## Disclaimer

This tool reads only standard Windows audio output and does not read or modify
game memory or files. It is intended as an accessibility aid. Anti-cheat policies
vary between games, so use it at your own discretion.

## License

Copyright (c) 2026 Mike Zaugg.

This project is **source-available**, not open source. It is licensed under the
**Functional Source License, Version 1.1, MIT Future License (FSL-1.1-MIT)**. In
short: you are free to use, modify, and contribute to the code for any purpose
except building a competing product. Each release automatically becomes available
under the permissive MIT license two years after it ships.

See the [LICENSE](LICENSE) for the full terms.
