# Verifier 三态决策实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 GUI-Actor-3B Verifier 的 pass/reject/uncertain 三态决策，替换当前的纯分数重排序，使代理能拒绝错误检测并适当地重试或请求人工干预。

**Architecture:** 在 `UIVerifier.verify()` 中为每个候选标注新增 `verdict` 字段（`pass`/`reject`/`uncertain`）。`UIDetector.annotate()` 默认过滤掉 `reject` 候选，在 `Perception.som_annotations` 中新增 `blocked_count` 记录被过滤数量。`AgentOrchestrator` 在全否决场景下触发反射循环，`uncertain` 候选在 DesktopInteract 工具中追加确认检查。

**Tech Stack:** Python 3.12, PIL/Pillow, GUI-Actor-3B (Transformers), pytest

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `ui_detector/verifier.py` | 新增 `VerifierVerdict` 枚举；`verify()` 返回带 `verdict` 字段的标注；新增 `from_verify_score()` 判定逻辑 |
| `ui_detector/detector.py` | `annotate()` 过滤 reject 候选，统计 blocked_count |
| `agent/perception.py` | `Perception` 新增 `blocked_count: int` 字段 |
| `agent/orchestrator.py` | 全否决时触发反射；`DesktopInteract` 对 uncertain 候选追加确认 |
| `tests/test_ui_detector.py` | Verifier 三态输出的单元测试 |
| `tests/test_orchestrator.py` | 全否决反射路径测试 |

---

### Task 1: Verifier 三态判定逻辑

**Files:**
- Modify: `ui_detector/verifier.py`
- Test: `tests/test_ui_detector.py`

- [ ] **Step 1: 新增 `VerifierVerdict` 枚举和阈值常量**

```python
# ui_detector/verifier.py — 在文件顶部、UIVerifier 类之前添加

import enum


class VerifierVerdict(enum.StrEnum):
    PASS = "pass"
    REJECT = "reject"
    UNCERTAIN = "uncertain"


# Thresholds for verdict classification.
# verify_score >= PASS_THRESHOLD → pass
# verify_score <= REJECT_THRESHOLD → reject
# otherwise → uncertain
PASS_THRESHOLD = 0.55
REJECT_THRESHOLD = 0.25
```

- [ ] **Step 2: 新增 `from_verify_score` 静态方法**

```python
# ui_detector/verifier.py — 在 UIVerifier 类中新增

@staticmethod
def classify(verify_score: float, pass_threshold: float = PASS_THRESHOLD, reject_threshold: float = REJECT_THRESHOLD) -> VerifierVerdict:
    """Classify a verify_score into pass / reject / uncertain."""
    if verify_score >= pass_threshold:
        return VerifierVerdict.PASS
    if verify_score <= reject_threshold:
        return VerifierVerdict.REJECT
    return VerifierVerdict.UNCERTAIN
```

- [ ] **Step 3: 修改 `verify()` 返回带 `verdict` 的标注**

修改 `verify()` 方法：在给每个 annotation 附加 `verify_score` 的同时，计算并附加 `verdict` 字段。

```python
# ui_detector/verifier.py — 修改 verify() 方法签名和逻辑

def verify(
    self,
    image: Image.Image,
    instruction: str,
    annotations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return annotations with verify_score and verdict, sorted by score.
    
    Each returned annotation is augmented with:
      - verify_score: float — combined verification score [0, 1]
      - verdict: str — "pass", "reject", or "uncertain"
    
    Annotations with verdict "reject" are still returned (sorted last) so
    callers can inspect them or count blocked candidates.
    """
    if not self.enabled or not annotations:
        # When disabled, all candidates pass by default.
        return [
            {**a, "verify_score": 1.0, "verdict": VerifierVerdict.PASS}
            for a in sorted(annotations, key=lambda a: a.get("score", 0.0), reverse=True)
        ]

    if self.detector is None:
        return [
            {**a, "verify_score": float(a.get("score", 0.0)), "verdict": VerifierVerdict.UNCERTAIN}
            for a in sorted(annotations, key=lambda a: a.get("score", 0.0), reverse=True)
        ]

    scored: list[dict[str, Any]] = []
    for ann in annotations:
        score = self._verify_one(image, instruction, ann)
        verdict = self.classify(score)
        scored.append({**ann, "verify_score": score, "verdict": verdict})

    # Sort: pass first (by verify_score desc), then uncertain, then reject.
    order = {VerifierVerdict.PASS: 0, VerifierVerdict.UNCERTAIN: 1, VerifierVerdict.REJECT: 2}
    scored.sort(key=lambda a: (order.get(a["verdict"], 2), -a["verify_score"]))
    return scored
```

- [ ] **Step 4: 编写测试**

```python
# tests/test_ui_detector.py — 新增测试

from ui_detector.verifier import UIVerifier, VerifierVerdict, PASS_THRESHOLD, REJECT_THRESHOLD


def test_classify_pass():
    assert UIVerifier.classify(0.9) == VerifierVerdict.PASS
    assert UIVerifier.classify(PASS_THRESHOLD) == VerifierVerdict.PASS
    assert UIVerifier.classify(0.55) == VerifierVerdict.PASS


def test_classify_reject():
    assert UIVerifier.classify(0.0) == VerifierVerdict.REJECT
    assert UIVerifier.classify(REJECT_THRESHOLD) == VerifierVerdict.REJECT
    assert UIVerifier.classify(0.1) == VerifierVerdict.REJECT


def test_classify_uncertain():
    assert UIVerifier.classify(0.4) == VerifierVerdict.UNCERTAIN
    assert UIVerifier.classify(0.26) == VerifierVerdict.UNCERTAIN
    assert UIVerifier.classify(0.54) == VerifierVerdict.UNCERTAIN


def test_verify_disabled_marks_all_pass():
    verifier = UIVerifier(enabled=False)
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.3},
        {"label": 2, "center_x": 0.1, "center_y": 0.9, "score": 0.1},
    ]
    result = verifier.verify(None, "click the button", annotations)
    assert all(a["verdict"] == VerifierVerdict.PASS for a in result)


def test_verify_no_detector_marks_all_uncertain():
    verifier = UIVerifier(detector=None, enabled=True)
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.8},
    ]
    result = verifier.verify(None, "click the button", annotations)
    assert all(a["verdict"] == VerifierVerdict.UNCERTAIN for a in result)


def test_verify_sort_order_pass_before_reject():
    """Even low-confidence pass candidates sort before high-confidence rejects."""
    verifier = UIVerifier(enabled=True)
    verifier._verify_one = lambda img, instr, ann: 0.55 if ann["label"] == 1 else 0.1
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.3},  # will be pass
        {"label": 2, "center_x": 0.1, "center_y": 0.9, "score": 0.9},  # will be reject
    ]
    result = verifier.verify(None, "click the button", annotations)
    assert result[0]["verdict"] == VerifierVerdict.PASS
    assert result[0]["label"] == 1
```

- [ ] **Step 5: 运行测试验证**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ui_detector.py -v`
Expected: 新增 6 个测试 PASS，现有测试继续通过

- [ ] **Step 6: Commit**

```bash
git add ui_detector/verifier.py tests/test_ui_detector.py
git commit -m "feat: add VerifierVerdict three-state classification to UIVerifier"
```

---

### Task 2: 在 Detector 和 Orchestrator 中消费 Verdict

**Files:**
- Modify: `ui_detector/detector.py:121-148`
- Modify: `agent/perception.py:30-38`
- Modify: `agent/orchestrator.py` — `_desktop_interact_impl` 方法
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: `annotate()` 过滤 reject + 统计 blocked_count**

修改 `UIDetector.annotate()` 使其在返回前过滤 reject 候选并记录统计信息：

```python
# ui_detector/detector.py — 修改 annotate() 返回类型

async def annotate(
    self, image: Image.Image, instruction: str
) -> tuple[list[dict[str, Any]], int]:
    """Return (annotations, blocked_count).
    
    Annotations with verdict "reject" are excluded from the returned list.
    blocked_count is the number of rejected candidates.
    """
    pred = await self.predict_async(image, instruction)
    annotations = []
    points = pred.get("topk_points") or []
    values = pred.get("topk_values") or []
    for idx, (point_group, score) in enumerate(zip(points, values), start=1):
        if not point_group:
            continue
        xs = [p[0] for p in point_group]
        ys = [p[1] for p in point_group]
        annotations.append({
            "label": idx,
            "center_x": sum(xs) / len(xs),
            "center_y": sum(ys) / len(ys),
            "score": score,
            "normalized": True,
        })
    loop = asyncio.get_event_loop()
    verified = await loop.run_in_executor(
        self._executor, self.verifier.verify, image, instruction, annotations
    )
    # Filter out rejected candidates.
    passed = [a for a in verified if a.get("verdict") != "reject"]
    blocked = len(verified) - len(passed)
    return passed, blocked
```

- [ ] **Step 2: Perception 数据类新增 `blocked_count`**

```python
# agent/perception.py — 在 Perception dataclass 中新增字段

@dataclass
class Perception:
    screenshot_path: Path
    description: str
    ocr_text: str
    ui_tree: dict[str, Any]
    som_annotations: list[dict[str, Any]]
    ui_hash: str = ""
    screen_width: int = 0
    screen_height: int = 0
    annotated_screenshot_path: Path | None = None
    blocked_count: int = 0  # 新增：被 Verifier 拒绝的候选数量
```

- [ ] **Step 3: 更新 `PerceptionModule.perceive()` 处理新返回类型**

```python
# agent/perception.py — 在 perceive() 中 _run_ui_detector 的调用处

som_annotations = await self._run_ui_detector(image, instruction)
# _run_ui_detector 现在可能返回 tuple，需要适配
```

修改 `_run_ui_detector` 以解包新的 tuple 返回：

```python
# agent/perception.py — 修改 _run_ui_detector

async def _run_ui_detector(
    self, image: Image.Image, instruction: str
) -> tuple[list[dict[str, Any]], int]:
    if self.ui_detector is None or not self.config.ui_detector.enabled:
        return [], 0
    try:
        annotations, blocked = await self.ui_detector.annotate(image, instruction)
        return annotations, blocked
    except Exception as exc:
        return [{"error": str(exc)}], 0
```

然后在 `perceive()` 中：

```python
# agent/perception.py — modify perceive()

som_annotations, blocked_count = await self._run_ui_detector(image, instruction)
# ... build description ...
return Perception(
    ...,
    som_annotations=som_annotations,
    blocked_count=blocked_count,  # ← 新增
)
```

- [ ] **Step 4: Orchestrator — 全否决时触发反射**

在 `run_task()` 中，感知完成后检查是否所有候选都被拒绝：

```python
# agent/orchestrator.py — 在 run_task() 中 perception 获取后添加

perception = await self.perception.perceive(instruction=self.current_instruction)

# Check for total rejection by verifier.
if perception.blocked_count > 0 and not perception.som_annotations:
    reason = f"Verifier rejected all {perception.blocked_count} candidates"
    await self.reflection.record(
        task_summary=user_input,
        failure_reason=reason,
        fix_action="Retry detection with different framing or ask for human guidance.",
    )
    self.history.append({
        "role": "user",
        "content": (
            f"{reason}. The UI may have changed or the target element may not be visible. "
            "Try a different approach or describe what you are looking for differently."
        ),
    })
    await self.state.transition("REFLECT", task_id=self.task_id)
    reflection = await self._reflect()
    # After reflecting, go back to planning (loop continues)
    await self.state.transition("PLANNING", task_id=self.task_id)
    continue  # skip to next loop iteration
```

- [ ] **Step 5: Orchestrator — DesktopInteract uncertain 候选确认**

修改 `_desktop_interact_impl`：如果匹配到的标注 `verdict == "uncertain"`，在返回消息中追加警告：

```python
# agent/orchestrator.py — 在 _desktop_interact_impl 中找到 match 后

match = None
for ann in perception.som_annotations:
    if ann.get("label") == label:
        match = ann
        break

# After finding the match, before executing:
is_uncertain = match.get("verdict") == "uncertain"
```

在成功返回消息中附带警告：

```python
if result.success:
    msg = f"OK: {action} at ({screen_x}, {screen_y}) — {result.content[:200]}"
    if is_uncertain:
        msg = "[uncertain] " + msg + " (Verifier was unsure about this element; verify the result.)"
    return msg
```

- [ ] **Step 6: 编写测试**

```python
# tests/test_orchestrator.py — 新增测试

@pytest.mark.asyncio
async def test_desktop_interact_warns_on_uncertain_verdict(config, eventbus, killswitch):
    """DesktopInteract appends a warning when the matched element has verdict=uncertain."""
    from agent.perception import Perception

    mcp = FakeMCP()
    llm = FakeLLM([])
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[
            {"label": 1, "center_x": 0.5, "center_y": 0.5, "verdict": "uncertain"},
        ],
    )

    result = await agent._desktop_interact_impl(label=1, action="click")

    assert result.startswith("[uncertain]")
    assert "OK" in result
```

- [ ] **Step 7: 运行测试验证**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ui_detector.py tests/test_orchestrator.py tests/test_perception.py -v`
Expected: 所有测试 PASS

- [ ] **Step 8: Commit**

```bash
git add ui_detector/detector.py agent/perception.py agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: consume VerifierVerdict in detector, perception, and orchestrator"
```

---

### Task 3: 回归测试与收尾

**Files:**
- 无新增/修改

- [ ] **Step 1: 运行完整测试套件**

Run: `.venv\Scripts\python.exe -m pytest tests/ -q`
Expected: 全部测试 PASS（预计 ≥164 个）

- [ ] **Step 2: 语法/导入检查**

Run:
```
.venv\Scripts\python.exe -m py_compile ui_detector/verifier.py ui_detector/detector.py agent/perception.py agent/orchestrator.py
.venv\Scripts\python.exe -c "import ui_detector; import agent; print('Imports OK')"
```
Expected: 无错误

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: finalize VerifierVerdict three-state integration"
```
(only if regression fixes were needed; otherwise skip)

---

## 自我审查

### 1. 规范覆盖
- ✅ Verifier 三态输出（pass/reject/uncertain）— Task 1
- ✅ reject → 重试（通过 orchestrator 全否决反射循环）— Task 2 Step 4
- ✅ uncertain → 警告（通过 DesktopInteract 的 [uncertain] 前缀）— Task 2 Step 5
- ✅ 规范图表：通过→执行、否决→重试、不确定→提醒用户

### 2. Placeholder 扫描
- 无 "TBD"、"TODO"、"implement later"
- 所有代码块都有具体实现
- 所有测试都有实际断言

### 3. 类型一致性
- `VerifierVerdict` 在 Task 1 定义，Task 2 引用时名称一致
- `blocked_count` 在 `Perception` dataclass (Task 2 Step 2)、`perceive()` (Task 2 Step 3)、orchestrator (Task 2 Step 4) 中使用一致
- `annotate()` 新返回类型 `tuple[list, int]` 在 `_run_ui_detector` 和 `perceive()` 中一致解包
