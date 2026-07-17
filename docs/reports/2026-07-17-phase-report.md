# Caelum-Agent 项目阶段性报告

**日期**: 2026-07-11 ~ 2026-07-17
**分支**: main
**测试状态**: 681 passed, 0 failed

---

## 一、总体进展

本周（7天）共提交 **80+ commits**，变更 **26 个文件、+4700 行、-223 行**。核心主线是**视觉感知系统从 GUI-Actor 切换到 YOLO-SoM**，并围绕它建立了完整的 6 级视觉定位降级链、工具基础设施和质量保障体系。

## 二、核心技术突破

### 2.1 YOLO-SoM 视觉感知（替代 GUI-Actor）

| 组件 | 文件 | 功能 |
|------|------|------|
| YoloDetector | `ui_detector/yolo_detector.py` | OmniParser icon_detect YOLOv8，~50ms/帧 GPU，自动 CUDA→CPU 降级 |
| fuse_annotations | `ui_detector/fusion.py` | OCR 文字框 + YOLO 图标框 IoU 融合（>15% 合并，>5% 去重），统一归一化坐标 |
| IconCaptioner | `ui_detector/icon_captioner.py` | Florence-2 base fine-tune，为纯图标标记生成语义描述（`"magnifier"`, `"close button"`） |
| visualize_som | `ui_detector/visualizer.py` | 归一化坐标→图像像素渲染，红色编号标记 |

**感知流水线**: 截图 → 逆DPI归一化 → RapidOCR(DirectML GPU) + UIA树 → YOLO自动补偿（UIA空时）→ 融合 → Florence-2标注 → 双图输出（干净图+标注图）

### 2.2 6 级视觉定位降级链

```
UIA Tree (Snapshot + Click)
  └─ 失败 →
  [1] DesktopInteract(label=N)     ← YOLO SoM 自动触发
  [2] NearbyLabels(label|loc)      ← 纯几何三角定位，零开销
  [3] ZoomRegion(size, label|loc)  ← 局部原生分辨率重感知
  [4] UpgradeVision                ← 全局原生分辨率切换
  [5] CaptureWindow(title)         ← PrintWindow 穿透遮挡
  [6] PreviewPoints([[x,y],...])   ← 最终兜底：坐标猜测+可视化确认
```

2 次连续失败自动强制推进（`_FAILURE_ESCALATION_NOTICE`），任何成功重置计数器。

### 2.3 坐标系统归一化

全部模型面朝坐标统一为 **normalized [0,1]**，`(0,0)=左上角`，`(1,1)=右下角`：

- 内部换算简化为一公式：`screen = origin + norm × screen_size`
- 删除了 `screenshot_width/height` 参与坐标转换的旧路径
- 4 位小数精度，所有工具描述同步更新

### 2.4 截图编码与分辨率优化

- **编码**: JPEG Q=60 → **PNG 无损**（LLM 需要读文字）
- **分层降档**: 逆 DPI 归一化后
  - `max_dim > 3840` → 4K (3840×2160)
  - `max_dim > 2560` → 2K (2560×1440)
  - `max_dim > 1920` → 1080p (1920×1080)
  - `≤ 1920` → 保持原样

### 2.5 OCR GPU 加速

RapidOCR 通过 `onnxruntime-directml` 在 GPU 上运行（~5.5x 加速，4.3s→0.8s @2560×1440，RTX 4090 Laptop）。为避免 DirectML 并发崩溃（两路 ONNX 会话同时抢 D3D12 设备导致 `0xc0000005`），ChromaDB 向量模型锁死在 CPU（`ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])`）。

## 三、工具和功能清单

### 新增本地工具（10 个）

| 工具 | 功能 |
|------|------|
| DesktopInteract | 基于 SoM 标注的视觉点击（label=N），支持 click/type/scroll |
| NearbyLabels | k 近邻标注查询，归一化坐标欧氏距离排序 |
| ZoomRegion | 局部区域原生分辨率重感知（3 档: 480/960/1680px） |
| UpgradeVision | 全局切换原生分辨率截图 |
| PreviewPoints | 坐标猜测可视化确认（1-3 个候选点） |
| CaptureWindow | PrintWindow 穿透遮挡窗口捕获 |
| SelfWindow | 隐藏/恢复 Agent 自身控制台窗口 |
| FocusGuard | 前台窗口焦点守护（AttachThreadInput 方案） |
| ReadDocument | Kimi Files API 文档提取（PDF/DOCX/PPTX/EPUB/XLSX） |
| ViewMedia | 本地图片/视频上传并原生渲染（ms:// 引用） |

### 子代理（2 个）

| 子代理 | 功能 |
|--------|------|
| DraftContent | 内容写作（persona + Partial Mode 预填充），最多 5 轮修订 |
| GenerateImage | SVG 生成 + CairoSVG 渲染 + LLM 视觉自检，最多 5 轮 |

### 学习机制

| 组件 | 功能 |
|------|------|
| SkillLearner | 成功任务轨迹 → SKILL.md 自动生成/合并（ChromaDB 向量相似度 ≥0.85 合并） |
| ReflectionEngine | 失败任务 → Kimi rethink + SQLite 记录 → 系统提示注入 |
| LearningSettler | 中断任务延迟判定（启动时 LLM 判断完成度 → 路由到 Skill 或 Reflection），最多 3 次尝试 |

## 四、工程质量

### 测试
- **681 个测试**，0 失败，pytest-xdist 并行执行
- 新增测试覆盖：融合逻辑、图标标注、YOLO 检测器、学习结算、坐标缩放

### 上游问题处理
- 定位并归档 `windows-mcp` 的 `UnboundLocalError: tree_node` 上游 bug（`docs/windows_mcp/upstream-tree-node-issue.md`）
- `_UpstreamNoiseFilter` 在 MCP 客户端层抑制已知噪声（含 traceback 感知、rich tool-error 块过滤）

### 稳定性
- LLM 瞬态错误重试 + WARNING 日志
- 过期 UIA 标签自动刷新 Snapshot
- API 断路器（5 次连续失败 trip）
- 循环预算 + 反思检查点（初始 10 轮，最多 50 轮）

## 五、文档

- `docs/handover/caelum-agent-pro-handover.md` — 全面交接文档（930 行）
- `docs/kimi_api/kimi_api_usage.md` — Kimi API 使用手册（562 行）
- CLAUDE.md 持续更新

## 六、下阶段计划

1. **端到端测试**: 完善真实桌面场景的自动化 E2E 测试
2. **WeChat/QQ 适配**: 针对 Qt 无 UIA 应用优化视觉定位准确率
3. **性能优化**: 减少感知延迟（OCR+YOLO+Florence 串行→流水线）
4. **多模态记忆**: 截图关键帧存入 ChromaDB 辅助长期任务记忆
5. **安全审计**: 文件系统操作权限细粒度控制
