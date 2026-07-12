# YOLO-SoM 替换 GUI-Actor：实施计划

> 设计文档：`docs/superpowers/specs/2026-07-13-yolo-som-replace-gui-actor-design.md`
> 基线：main @ 15b5e64，非 smoke 571 passed
> 纪律：每任务 TDD（红→绿），任务间全套非 smoke 保持绿，逐任务提交

## Task 1：YoloDetector + YoloConfig（纯新增，不破坏现有）

**文件**：新建 `ui_detector/yolo_detector.py`、`tests/test_yolo_detector.py`；改 `agent/config.py`

- [ ] 写失败测试（mock ultralytics）：
  - `detect()` 返回 `[{label, center_x, center_y, bbox, score}]`，坐标归一化 [0,1]，
    label 从 1 开始；按 conf 降序编号
  - 懒加载：构造不调 YOLO()，首次 detect 才加载
  - device 回退：cuda 加载/推理抛异常 → 自动以 cpu 重建一次并推理成功
  - YoloConfig 默认值解析（enabled/model_path/device/conf/imgsz/auto_compensate）
- [ ] 实现 `YoloDetector`：`YOLO(path)` 懒加载；`predict(image, imgsz, conf, device, verbose=False)`；
  xyxy 像素框 → 归一化中心 + 归一化 bbox + conf
- [ ] config.py 新增 `YoloConfig` + `Config.yolo`（UIDetectorConfig 暂留，Task 4 删）
- [ ] 绿 → 全套非 smoke → 提交 `feat(ui_detector): add YoloDetector for OmniParser icon detection`

## Task 2：visualizer 支持画框 + perception 接入 YOLO + 双图发送

**文件**：`ui_detector/visualizer.py`、`agent/perception.py`、`agent/orchestrator.py`（_format_perception）、`tests/fakes.py`、`tests/test_perception*.py`

- [ ] 写失败测试：
  - visualize_som：标注含 `bbox`（归一化 xyxy）时画矩形框 + 左上角编号标签；
    无 bbox 时保持圆点（PreviewPoints 兼容）
  - perceive：UIA 空 + OCR 有字 → 调用 detector.detect → Perception.som_annotations 非空、
    annotated_screenshot_path 存在；`yolo.auto_compensate=false` 时不跑
  - detector=None 或 detect 抛异常 → 跳过标注，任务不炸（告警日志）
  - _format_perception：有标注图时 content 含**两张** image_url（原图在前、标注图在后）
    + 文本引导（DesktopInteract label=N）
- [ ] 实现：
  - visualizer：bbox 分支（红框 + 标签 pill）
  - perception：`ui_detector` 参数改名 `detector`；`_run_ui_detector` → `_run_yolo`；
    自动补偿判定不变（`not ui_tree and ocr_text.strip()`）；
    删除 `with_vision` 参数与 `perceive_with_vision`；blocked_count 恒 0
  - orchestrator._format_perception：双图分支 + 文案（删 target= 引导）
  - fakes.py：`FakeYoloDetector`（canned annotations + calls 计数）
- [ ] 绿 → 全套 → 提交 `feat(perception): YOLO auto-annotation with dual-image perception`

## Task 3：DesktopInteract 改为 label-only

**文件**：`agent/orchestrator.py`、`agent/tools.py`、`tests/test_orchestrator.py`

- [ ] 写失败测试：
  - label=N → 取 _last_perception.som_annotations[N] 中心 → windows__Click(loc=屏幕像素)
  - double_click → clicks=2；right_click → button=right；type → focus+Type
  - label 缺省/超界/无标注 → 友好 [error]（提示 Snapshot label 或 PreviewPoints）
  - schema 无 target；label 为 required
- [ ] 实现：删除 target= 分支、verifier 歧义逻辑（AMBIGUITY_SCORE_MARGIN）、
  `_pending_som_followup` 与 `_format_som_followup` 及其在 _think_and_act 的消费；
  工具 description 与系统提示词改写（UIA label → YOLO label → PreviewPoints 链）
- [ ] 绿 → 全套 → 提交 `refactor(agent): DesktopInteract label-only on YOLO annotations`

## Task 4：删除 GUI-Actor 残余

**文件**：删 `ui_detector/detector.py`、`ui_detector/verifier.py`、`ui_detector/gui_actor/`、
`tests/test_ui_detector*.py`；改 `ui_detector/__init__.py`、`agent/config.py`、
`agent/orchestrator.py`（initialize）、`tests/conftest.py`（如引用）

- [ ] config.py 删 UIDetectorConfig + Config.ui_detector + model_path_absolute
- [ ] __init__.py 导出 YoloDetector / visualize_som
- [ ] orchestrator.initialize()：`yolo.enabled` 时构造 YoloDetector 注入 perception
  （不 preload——YOLO 加载仅 ~200ms）
- [ ] 全仓 grep `ui_detector|UIDetector|gui_actor|with_vision|perceive_with_vision`
  清零（含测试）
- [ ] 全套 → 提交 `refactor(ui_detector): remove GUI-Actor model, verifier and config`

## Task 5：setup.py + requirements + config.yaml.example

**文件**：`setup.py`、`requirements.txt`、`config.yaml.example`、`tests/test_setup.py`

- [ ] 写失败测试：下载器幂等（已存在跳过/大小不符重下）、zip 解压 model.pt 落位、
  下载失败 best-effort 报错
- [ ] setup.py：删 GUI-Actor 分卷下载/合并/`--weights-source`；
  新增 `--download-weights` 从
  `.../omniparser-weights/releases/download/v2.0/icon_detect.zip` 下载解压到
  `models/omniparser/icon_detect/`；smoke 改为 YOLO 加载+推理（mock 可跳过）
- [ ] requirements.txt：+`ultralytics`；grep 确认 transformers/accelerate 无其他使用者后删除
- [ ] config.yaml.example：`ui_detector:` 段替换为 `yolo:` 段
- [ ] 全套 → 提交 `chore(setup): download OmniParser YOLO weights; drop transformers`

## Task 6：CLAUDE.md + 终验

**文件**：`CLAUDE.md`

- [ ] 更新：技术栈表（UI detection 行）、项目结构（ui_detector 说明）、
  感知融合段、DesktopInteract/PreviewPoints 工具行、Python 约束说明、
  setup 权重下载命令、降级链描述
- [ ] 全套非 smoke + 编译检查 + spike 复跑
- [ ] 提交 `docs: update CLAUDE.md for YOLO-SoM perception`

## 验证

- 每任务：`pytest tests/ -q -m "not smoke"` 全绿
- 终验：`scripts/spike_yolo_omniparser.py` 复跑；人工跑一次
  `python main.py --task "..." --yes` 观察 UIA 缺乏场景（可选）

## 不在范围

- `docs/designs/desktop_agent_v8.agent.final.md` spec 同步（另行处理）
- Florence-2 captioner（Kimi 直接读标注图）
- UIA「贫瘠」（非空但元素极少）触发扩展（保留现有空树启发式，实践后按需加）
