# SoundRadar360 · 单耳聋玩家的《暗区突围：无限》声音方向雷达

<h1 align="center">SoundRadar360</h1>

<p align="center">
  <b>看得见你听不见的。</b><br>
  一款把游戏声音变成实时视觉雷达的无障碍工具，专为单耳聋（SSD）玩家打造，
  基于 <a href="https://github.com/mike-s-zaugg/VisualAudioOverlay">mike-s-zaugg/VisualAudioOverlay</a>
  （FSL-1.1-MIT 许可）深度改造，针对《暗区突围：无限》（Arena Breakout: Infinite）校准。
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-0078D6">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776AB">
  <img alt="License" src="https://img.shields.io/badge/license-FSL--1.1--MIT-blue">
</p>

---

## 这是什么

单耳聋玩家在射击游戏里最大的痛点不是"听不见"，而是**无法判断声音来源方向**。
SoundRadar360 监听 Windows 音频回环（纯监听，**绝不改动游戏声音**），把周围的声音
实时画成一个 360° 雷达：**方向、距离、上下、类型**，一眼可读。

> 核心设计原则：**方向精度永远优先于声音类型**——单耳玩家自己能听出枪声和脚步声，
> 雷达的价值在于告诉他声音**从哪个方向来**。

## 功能总览（v0.7.x）

### 🧭 方向系统（核心）

- **左右方向精确**：双耳强度差（ILD）+ 双耳时间差（ITD）融合——脚步声这类低频声用
  ITD（低频的真实方位线索），枪声用 ILD，带一致性保护；
- **四方位标定**：用训练场"前/后/左/右"实测样本验证，左右误差接近 0°；
- **前后判定三件套**：
  1. 亮声（枪声）的 HRTF 频谱线索自动判前后；
  2. **注意力机制**：每种声音独立跟踪 1 秒（持续发声自动续期、消失后 1 秒到期），
     你转身时标记稳稳贴在声源上，不同声音互不干扰；
  3. **侧头修正系统**：转身时前方声源侧向角缩小、后方声源侧向角放大——多声源同向
     偏移时雷达**自动判定前后**；单声源时显示**偏移弧+度数**供你自己读；
  4. **中线穿越相位跟踪**：锚定后（多源转身判定或枪声线索），旋转过程中每次穿越
     中线都自动标注前后，后方声源钉在 180°；
- 无法判定前后时如实显示**正反两弧**（中线附近等强度），绝不武断指错方向；
- **上下方向**：`+` = 声音在你上方（楼上/坡上），`-` = 下方，无标记 = 同层。

### 🎨 四类声音一眼可辨

| 颜色 | 类别 | 判定 |
|------|------|------|
| 🔵 蓝 | 脚步声 | 规律节奏（容忍漏步）或离轴孤立闷响（敌人单步/跳跃/开门） |
| 🔴 红 | 战斗声 | 爆裂特征 / 巨响通道 / 连发持续轰鸣（枪、手雷、爆炸） |
| 🟣 紫+❗ | 静步威胁 | 极轻的脚步声（含你自己的静步，实时显示） |
| 🟡 黄 | 杂音 | 正中闷响（开门/舔包/换弹/被击中）、机器噪声等 |

- 你自己的跑步/行走脚步与枪声 → 圆心白色小脉冲（屏蔽确认，不占方向）；
- 环境风声/引擎声 → 起音后不回落，完全不显示；
- 快出现、慢消失 + 敌情残影（熄灭后保留最近声音的方向标记）。

### 📐 距离与显示

- 光弧离圆心越近 = 越近，三条虚线参考环；
- 快攻慢放（首帧即亮、缓慢淡出 3 秒+）；
- 老化标记自动变宽/虚线（方向随时间不确定的诚实表示）。

### 📦 自动化校准

所有参数基于**真实游戏录音**校准，无需手动调参：

- 16 把枪的第一人称枪声库（SCAR/ACE/AK12/AN94/FAMAS/95/191/H416/A545/ZC807/FAL/
  RPK16/AKM/AUG/AEK，含消音对比）；
- 六批步态样本（软/硬地 × 跑/走/静步）；
- 训练场机器声样本（中频金属环守卫，排除机器噪声）；
- 原地自转 360° 标定（方位平滑与跟踪参数）。

## 安装与使用

1. 安装到 `D:\Program Files\SoundRadar360\`（或任意目录），运行 `SoundRadar360.exe`；
2. 游戏音频输出选你的**耳机**（保持立体声/耳机模式，不要开游戏内单声道）；
3. 雷达仪表盘：选显示器 → **开始** → 可选：把雷达窗口放到副屏/游戏上方；
4. 你的脚步声/枪声应只在圆心闪白脉冲；静步显示紫色。

> 软件为纯监听：不修改、不混音、不输出任何音频到游戏设备。

## 开发

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m unittest discover -s tests    # 57 项测试
python main.py                          # 源码运行

# 打包（one-dir）
python -m PyInstaller AudioRadarFolder.spec --noconfirm --clean
```

- `direction.py` — DSP 核心（分类、方位、ITD/ILD、注意力数据源）；
- `overlay.py` — 雷达渲染（注意力机制、前后修正、+/- 显示）；
- `audio_capture.py` — WASAPI 回环/进程捕获 + 录音；
- `tests/` — 57 项回归测试（每个实测 bug 都有对应测试）。

## 许可证与致谢

- 上游项目：[mike-s-zaugg/VisualAudioOverlay](https://github.com/mike-s-zaugg/VisualAudioOverlay)（FSL-1.1-MIT）；
- 本仓库为同一许可下的改造分支，详见 `LICENSE` 与 `THIRD-PARTY-NOTICES.md`。
