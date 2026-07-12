# YOLO-SoM 替换 GUI-Actor：设计文档

**日期**：2026-07-13
**状态**：已批准（用户逐题确认 + spike 验证通过）

## 背景

GUI-Actor-3B（指令条件 pointing + verifier 重排）在实践中价值不足：
模型加载数秒、推理慢、精度收益不明显。用 OmniParser v2 的定制
YOLOv8 图标检测模型（`icon_detect/model.pt`，40MB）替换视觉定位：
检测快（实测 2560×1440 上 ~42ms/帧，RTX 4090 Laptop），覆盖桌面图标、
任务栏、窗口控件、托盘等可交互元素（spike：
`scripts/spike_yolo_omniparser.py`）。

## 用户决策（确认记录）

1. **触发方式**：逐帧自动——该帧 UIA 树为空但 OCR 有文字时跑 YOLO；
   UIA 恢复即停（沿用现有 `_needs_vision_compensation` 启发式）。
2. **执行方式**：DesktopInteract 保留 `label=N`（点 YOLO 框中心），
   删除 `target=` 自然语言定位。
3. **权重来源**：GitHub Release 镜像
   `LiaoZiqi-GZFLS/omniparser-weights`（release v2.0，已存在），
   只需 `icon_detect.zip`（37MB）；Florence-2 captioner 不需要
   （Kimi 直接看标注图）。

## 删除清单

| 项 | 内容 |
|---|---|
| `ui_detector/detector.py`、`verifier.py`、`gui_actor/` | GUI-Actor 包装、verifier 重排、本地 patched 模型源码 |
| DesktopInteract `target=` 模式 | 自然语言定位 + top-k 候选 + verifier 歧义/重排逻辑（含 `_pending_som_followup`、`AMBIGUITY_SCORE_MARGIN`） |
| config `ui_detector` 段 | 全部键 → 新 `yolo` 段 |
| setup.py | GUI-Actor 分卷下载/合并 → 单 zip 下载 |
| requirements.txt | +`ultralytics`；−`transformers`/`accelerate`（验证无其他使用者后删）；`torch` 保留 |
| CLAUDE.md | 技术栈行、感知融合、Python <3.13 约束（随 GUI-Actor 消失；保留 3.12 因 windows-mcp >=3.12） |

**保留**：`ui_detector/visualizer.py`（`visualize_som` 画框，YOLO 标注与
PreviewPoints 共用）、`preview_points.py` 兜底工具、`UpgradeVision`、
`CaptureWindow`。

## 新增：`ui_detector/yolo_detector.py`

```python
class YoloDetector:
    def __init__(self, model_path: Path, device: str = "cuda:0",
                 conf: float = 0.25, imgsz: int = 1280): ...
    def detect(self, image: Image.Image) -> list[dict]: ...
```

- ultralytics `YOLO(model_path)` 懒加载（首次 detect 时加载；非 UI 任务零成本）
- device 默认 `cuda:0`，加载/推理失败自动回退 `cpu` 一次（之后保持 cpu）
- 输出沿用现有 SoM 标注契约：`{label, center_x, center_y, bbox, score}`，
  坐标归一化到 [0,1]（对压缩图归一化，与模型所见坐标系一致）
- 检测输入 = 压缩后的模型可见图（1440p），框与标注图像素一致，无需二次换算
- 单元测试全 mock ultralytics（FakeYoloDetector 喂假框）

## 感知流程（perceive）

```
截图 → 压缩(反DPI) → OCR → UIA树
   └── 逐帧判定：UIA树为空 且 OCR有文字（_needs_vision_compensation，不变）
       └── YOLO detect → SoM 标注 → 标注图（visualize_som，不变）
传给 Kimi（_format_perception）：
   有标注 = 未标注图 + 标注图（两张）+ 文字描述（含 label 列表）
   无标注 = 未标注图（现状）
```

- 删除 `with_vision` 参数与 `perceive_with_vision`（GUI-Actor 时代的
  按需视觉入口）；YOLO 只走自动补偿
- `_run_ui_detector` → `_run_yolo`；`auto_compensate` 移入 `yolo` 配置段

## DesktopInteract（改造后）

- schema：`label`（必填 int）、`action`（click/double_click/right_click/type）、
  `text`；删除 `target`
- `label=N` → 最近一次感知的 YOLO 标注中取第 N 号框中心 →
  归一化坐标 × 屏幕分辨率 → windows__Click/Type（执行路径与现状一致，
  含 `clicks=2`、`button=right`、type 先 focus）
- 最近感知无 YOLO 标注 → `[error] 当前帧没有 YOLO 标注（UIA 树可用时
  不跑检测）；请用 windows__Snapshot 的 label，或 PreviewPoints 兜底`
- 无视觉模型可用（模型文件缺失等）→ 自动补偿跳过并日志告警（不炸任务）

## 配置

```yaml
yolo:
  enabled: true
  model_path: ./models/omniparser/icon_detect/model.pt
  device: cuda:0          # 加载/推理失败自动回退 cpu
  conf: 0.25
  imgsz: 1280
  auto_compensate: true   # UIA 空+OCR 有字时自动标注
```

`config.yaml` 里旧的 `ui_detector:` 段被 pydantic 忽略（extra=ignore），
可手动删除。

## setup.py

- 删除 GUI-Actor 分卷下载/合并/校验逻辑与 `--weights-source` 参数
- `--download-weights`：从
  `https://github.com/LiaoZiqi-GZFLS/omniparser-weights/releases/download/v2.0/icon_detect.zip`
  下载并解压 `model.pt` 到 `models/omniparser/icon_detect/`（幂等：
  已存在且大小匹配则跳过）
- smoke test：YOLO 加载 + 一次推理（替代 GUI-Actor load smoke）

## 降级链（更新后）

```
UIA label (windows__Snapshot/Click)
  → YOLO 自动标注 + DesktopInteract(label=N)   ← 本次替换
  → UpgradeVision（原画）/ CaptureWindow
  → PreviewPoints 反复标注视觉伺服              ← 兜底，不动
```

## 测试策略

- TDD：每个任务先写失败测试
- 单测全 mock YOLO；`FakeYoloDetector` 放入 tests/fakes.py（仿现有模式）
- 删除/改写：test_ui_detector*（删）、DesktopInteract 相关测试、
  perception 视觉测试、config/setup 测试
- `scripts/spike_yolo_omniparser.py` 保留为检测质量验证脚本
- 全套非 smoke 保持绿（当前基线 571 passed）

## 已知代价与备注

- UIA 缺乏帧双图传入，图像 token 约 2×（1440p×2），用户已确认接受
- YOLO 对壁纸花纹有误检框（spike 实测），Kimi 看图可忽略；conf 可调
- `docs/designs/desktop_agent_v8.agent.final.md`（设计 spec）的同步
  更新不在本次范围，另行处理；CLAUDE.md 本次更新
- 坐标契约不变：模型给压缩图坐标系，orchestrator `_rescale_loc_args`
  换算屏幕像素
