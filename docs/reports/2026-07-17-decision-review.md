# Caelum-Agent 决策与回退全景 + 项目审查

> 审查日期: 2026-07-17 | 测试: 681 passed, 0 failed (排除 10 个环境相关失败) | 源文件: ~7700 行

---

## 一、Agent 全部决策点与回退链

### 1.1 主循环状态机

```
IDLE → PLANNING → [EXECUTING ⇄ VERIFYING] → COMPLETED/ERROR/STUCK
                       ↓
                  WAITING_HUMAN ⟲
                       ↓
                    REFLECT
```

**触发条件与回退：**

| 事件 | 触发 | 行为 | 回退 |
|------|------|------|------|
| Kill Switch 触发 | Ctrl+C 或 SIGINT | 设 `_cancel_event`，记录中断轨迹到 `pending_learning` | 下次启动时 LearningSettler 判定完成度 |
| API 断路器 trip | 连续 5 次 LLM 调用失败 | 转 `WAITING_HUMAN`，抛 `APIBreakerTripped` | 返回 "switched to local-only mode" |
| 瞬态 API 错误 | 单次 LLM 调用失败 | WARNING 日志 + reflection 记录，转 `PLANNING` 重试 | `continue` 主循环 |
| 连续 2 次 UI 操作失败 | `_register_action_failure` 计数 ≥ 2 | 注入降级提示："do NOT retry — move one step down this chain" | 任何成功重置计数器 |
| 同一 UI 连续 3 轮不变 | `_is_same_ui_loop` hash 相同 ≥3 次 | 转 `STUCK`，记录 reflection | 返回 "stuck" 消息 |
| 循环预算耗尽 | `loop ≥ loop_limit`（初始 10，最多 50） | LLM 反思检查点："is the approach sound? YES/NO" | YES → +10 循环; NO → STUCK |
| 验证失败 | LLM 判断 action 未成功 或 UI 未变化 | `_reflect()` 生成反思，转 `PLANNING` 重试 | 最多无限重试直到预算耗尽 |
| CompleteTask | 模型调用 CompleteTask | 跳过验证和 final_answer，直接返回 | — |

### 1.2 工具执行决策树

每个工具调用经过 ~12 个决策点：

```
工具调用
  ├─ 重复批次检测？ → 跳过（返回缓存结果）
  ├─ Kill Switch 触发？ → [error] 取消
  ├─ 本地工具？ → 安全门（CodeRunner 需 write_risky 审批）
  │   ├─ 审批拒绝 → [blocked]
  │   └─ 审批通过 → 执行
  ├─ MCP 工具？ → 解析 server
  │   ├─ 未知工具 → [error] 找不到
  │   ├─ windows 定位工具无 loc/label → [error] 需 Snapshot
  │   ├─ 有 loc → _rescale_loc_args(norm→screen px)
  │   ├─ 安全门（read/write_risky/destructive）
  │   └─ 执行 → 成功/失败
  │       ├─ 失败 + 过期标签 → 自动刷新 Snapshot + 追加标签上下文
  │       └─ 失败计数 → 触发降级提示（2次）
  └─ 审计日志 + 事件发射
```

### 1.3 安全三级门控

| 级别 | 匹配规则 | 默认行为 | --yes | --yes-all |
|------|---------|---------|-------|-----------|
| `read` | 默认（Click/Snapshot/Screenshot 除外） | 自动执行 | 同 | 同 |
| `write_risky` | click/type/write/edit/browser_*/app/filesystem | **需人工确认** | 自动批准 | 自动批准 |
| `destructive` | delete/remove/format/registry/powershell | **需键入确认**（重新输入操作摘要） | 仍需确认 | 自动批准 |

---

## 二、全部回退/降级链

### 2.1 视觉定位降级（6 级，从精确到兜底）

```
[0] UIA Tree → windows__Snapshot + Click(label=<id>)
     │ 失败（UIA 空/不可靠/标签过期/点错位置）
     ▼
[1] DesktopInteract(label=N)  ← YOLO SoM 自动触发（UIA空+OCR有字时）
     │ 失败（没有 marker 覆盖目标 / label 不存在）
     ▼
[2] NearbyLabels(label|loc, k=6)  ← 纯几何三角定位，零开销
     │ 失败（附近无可用的 marker）
     ▼
[3] ZoomRegion(size, label|loc)  ← 局部原生分辨率重感知（3档: 480/960/1680px）
     │ 失败（还是看不清 / 区域内无目标）
     ▼
[4] UpgradeVision  ← 全局切原生分辨率，持久生效
     │ 失败（窗口被遮挡 / mss 拍不到）
     ▼
[5] CaptureWindow(title)  ← PrintWindow(PW_RENDERFULLCONTENT) 穿透遮挡
     │ 失败（找不到窗口标题 / PrintWindow 返回空）
     ▼
[6] PreviewPoints([[x,y], ...])  ← ★ 最终兜底：坐标盲猜 + 可视化确认 + 调整重试
```

**强制推进**：连续 2 次失败后系统自动注入降级警告，阻止模型反复撞同一堵墙。

### 2.2 感知流水线退化

| 步骤 | 正常路径 | 退化 |
|------|---------|------|
| 截图捕获 | mss 全屏 | → `ImageGrab.grab()` 兜底 |
| 活动窗口裁剪 | `win32gui.GetForegroundWindow()` | → 裁剪失败返回原图 |
| OCR | RapidOCR (DirectML GPU) | → DML 不可用时 RapidOCR 内部 CPU 回退 |
| 图片压缩 | 逆DPI归一化 + 分层降档 | → `UpgradeVision` 时跳过全部缩放 |
| UI 树获取 | windows-mcp Snapshot | → playwright browser_snapshot → `{}` |
| YOLO 检测 | GPU 推理 | → 自动一次 CUDA→CPU 降级 → 失败返回 `[]` |
| SoM 融合 | OCR+YOLO IoU 合并 | → 任一为空则仅保留另一方 |
| 图标标注 | Florence-2 GPU | → 自动一次 CUDA→CPU 降级 → 失败静默跳过 |
| 标注过滤 | 过滤无 center_x/y 的标记 | → INFO 日志记录被过滤数量 |

### 2.3 MCP 连接退化

| 场景 | 行为 |
|------|------|
| 单次连接失败 | 指数退避重试（1s→2s→4s→8s→16s，最多 5 次） |
| 连接全部耗尽 | `_connected=False`，ERROR 日志 |
| 调用时发现断连 | 自动尝试一次 `reconnect()` |
| reconnect 失败 | 返回 `ToolResult(success=False)` |
| 健康监控 | 后台每 `health_interval` 秒 ping，不健康则自动重连 |
| 噪声过滤 | `_UpstreamNoiseFilter` 抑制 windows-mcp tree_node stderr 噪声，周期性汇总 |

### 2.4 学习机制退化

| 步骤 | 正常路径 | 退化 |
|------|---------|------|
| Skill 生成 | LLM JSON Mode | → 确定性模板 fallback |
| Skill 合并 | 向量相似度 ≥ 0.85 | → 低于阈值则新建独立 SKILL.md |
| Reflection 记录 | Kimi `rethink` Formula 工具 | → SQLite 直接写入 |
| 中断任务结算 | LLM 判定完成度 | → 3 次失败后纯文本 reflection fallback |
| Memory 读写 | Kimi `memory` Formula 工具 | → SQLite fallback |
| ChromaDB 向量 | ONNXMiniLM_L6_V2 CPU | → 旧集合自动删除重建（EF 冲突时） |

### 2.5 子代理退化

| 子代理 | 正常路径 | 退化 |
|--------|---------|------|
| DraftContent | persona + Partial Mode 预填充 | → 无 Partial Mode 时仍可生成；`doc_ref` 不存在时报错 |
| GenerateImage | SVG → CairoSVG → PNG → LLM 视觉自检（5 轮） | → CairoSVG 不可用时跳过（`ModuleNotFoundError` 降级）；5 轮后返回最佳尝试 |
| GenerateImage LLM 自检 | JSON Mode 视觉审查 | → JSON 解析失败时重试；review 失败时携带反馈重新生成 |

---

## 三、项目审查

### 3.1 测试覆盖

**总体**: 681 passed, 0 failed (排除环境问题后)

| 模块 | 源文件行数 | 测试行数 | 覆盖比 |
|------|----------|---------|--------|
| orchestrator | 2116 | 3321 | 1.57x |
| perception | 709 | 1381 | 1.95x |
| mcp_client | 583 | 793 | 1.36x |
| yolo_detector | 104 | — | (已集成到 perception 测试) |
| fusion | 131 | 227 | 1.73x |
| icon_captioner | 212 | 327 | 1.54x |
| tools | 546 | 299 | 0.55x ⚠️ |
| skills | 401 | 323 | 0.81x |
| memory | 294 | 265 | 0.90x |
| snapshot_parser | 291 | 170 | 0.58x ⚠️ |

**⚠️ 覆盖薄弱**: `tools.py` (0.55x) 和 `snapshot_parser.py` (0.58x) 测试相对较少。

### 3.2 代码质量

**强项**:
- ✅ 全部 `from __future__ import annotations`，类型标注完整
- ✅ 零 TODO/FIXME/HACK/XXX 注释
- ✅ 统一 logger 命名 `caelum.<module>`
- ✅ 清晰模块职责（除 orchestrator 外）
- ✅ 异常处理带具体类型，退化路径显式

**关注点**:
- ⚠️ `orchestrator.py` 2116 行——"上帝模块"，导入全部 17 个 agent 子模块
- ⚠️ `cairosvg` 未安装导致 7 个 image_gen 测试失败（环境问题）
- ⚠️ `rapidocr-onnxruntime` 包结构变化导致 3 个 perception 测试失败（版本不兼容）
- ⚠️ 3 处 Pillow `getdata()` 废弃警告（将在 Pillow 14 中移除）

### 3.3 架构评估

```
main.py → orchestrator (hub) → 17 个 agent 子模块
                ↓
         perception → ui_detector
         reflection → memory/kimi_memory
         skills → memory
         security → config
```

**Star-shaped architecture**: orchestrator 是唯一的 hub，其他模块单向依赖。优点是不存在循环依赖；弱点是 orchestrator 承担过多职责（ReAct 循环 + 工具注册 + 状态持久化 + 学习触发 + 生命周期管理）。

**建议拆分** (非紧急)：
- 工具注册 → 独立 `ToolRegistry` 类
- 坐标缩放逻辑 → 独立模块
- 状态持久化 → 已在 `state_machine.py` 中，可进一步分离

### 3.4 安全评估

- ✅ 三级门控 (read/write_risky/destructive)，无绕过路径
- ✅ `--yes-all` 有明确 WARNING 日志
- ✅ destructive 操作需**重新键入**操作摘要（不是简单按 y）
- ✅ CodeRunner 有 AST 白名单验证 + 子进程沙箱
- ✅ 非 TTY 环境默认拒绝 risky 操作
- ⚠️ 文件系统 MCP 允许目录由配置决定，默认无限制

### 3.5 依赖评估

20 个依赖，ML 栈最重（torch + torchvision + transformers + timm + einops + ultralytics + chromadb 合计数 GB）。核心功能（UIA 自动化 + LLM 调用）可以轻量运行；视觉感知（YOLO + OCR + Florence-2）和向量记忆（ChromaDB）是重量级可选项。

---

## 四、决策全景速查

| 场景 | Agent 决策 | 失败后 |
|------|-----------|--------|
| 桌面 UIA 可用 | Snapshot + Click(label) | → DesktopInteract (SoM) |
| Qt/Electron 无 UIA | YOLO 自动补偿 → DesktopInteract | → NearbyLabels |
| 没有 marker 覆盖目标 | NearbyLabels 三角定位 | → ZoomRegion |
| 文字太小看不清 | ZoomRegion 局部重感知 | → UpgradeVision |
| 全局看不清 | UpgradeVision 原生分辨率 | → CaptureWindow |
| 窗口被遮挡 | CaptureWindow PrintWindow 穿透 | → PreviewPoints |
| 一切定位失败 | PreviewPoints 盲猜 + 可视化验证 | → 调整重试 |
| LLM 调用失败 | 瞬态重试 (WARNING 日志) | → 5 次失败 trip 断路器 |
| 断路器 trip | 转 WAITING_HUMAN | → 中断轨迹结算 |
| 连续 2 次 UI 失败 | 系统强制注入降级提示 | → 任何成功重置 |
| 同一 UI 3 轮不变 | 转 STUCK | → reflection 记录 |
| 循环预算耗尽 | LLM 反思检查点 | → YES 延 10 轮 / NO 转 STUCK |
| Kill Switch 触发 | 取消当前操作，记录轨迹 | → 下次启动 LearningSettler |
| 人类请求帮助 | 暂停循环，msvcrt 菜单 | → 返回答案继续 |
| 模型说 CompleteTask | 跳过验证直接返回 | → 后台学习 skill |
| 验证判断失败 | `_reflect()` + 转 PLANNING | → 继续循环 |
