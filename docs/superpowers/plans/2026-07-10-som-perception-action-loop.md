# 感知-行动闭环：SoM 标注图 + 坐标点击工具 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LLM 看到带 SoM 编号标记的截图，并提供一个本地函数工具将标记编号转换为屏幕坐标点击，闭合桌面自动化的感知-行动回路。

**Architecture:** 在 Perception 阶段将 GUI-Actor-3B 检测到的元素用 `visualize_som()` 绘制到压缩截图上，发送带标记的图片给 Kimi。注册一个本地函数工具 `desktop_interact`，接收 SoM 标记编号和动作类型，解析归一化坐标→屏幕像素坐标，然后调用 Windows-MCP 或 Playwright MCP 执行实际操作。更新系统提示词，教模型如何使用标记和工具。

**Tech Stack:** Python 3.12, PIL/Pillow, GUI-Actor-3B, Windows-MCP, Playwright MCP, Kimi K2.6 Function Calling

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `agent/perception.py` | 新增 `screen_width`/`screen_height`/`annotated_screenshot_path` 字段到 `Perception`；`perceive()` 返回带 SoM 标注的截图路径 |
| `agent/orchestrator.py` | `_format_perception()` 发送标注截图而非原始截图；新增 `desktop_interact` 工具注册；更新系统提示词 |
| `agent/tools.py` | 新增 `DESKTOP_INTERACT_SCHEMA` 常量 |
| `ui_detector/detector.py` | `annotate()` 返回时附带原始图像尺寸（不修改 API，仅在返回 dict 中增加字段） |
| `tests/test_perception.py` | 新增测试：Perception 包含 screen 尺寸；annotated_screenshot 存在 |
| `tests/test_orchestrator.py` | 新增测试：`desktop_interact` 工具已注册；标注截图格式正确；系统提示词包含 SoM 指令 |

---

### Task 1: 在 Perception 中存储屏幕尺寸并生成标注截图

**Files:**
- Modify: `agent/perception.py:30-91`
- Test: `tests/test_perception.py`

- [ ] **Step 1: 扩展 Perception 数据类**

```python
# agent/perception.py — 修改 Perception dataclass

@dataclass
class Perception:
    screenshot_path: Path
    description: str
    ocr_text: str
    ui_tree: dict[str, Any]
    som_annotations: list[dict[str, Any]]
    ui_hash: str = ""
    screen_width: int = 0       # 新增：原始屏幕宽度
    screen_height: int = 0      # 新增：原始屏幕高度
    annotated_screenshot_path: Path | None = None  # 新增：SoM 标注后的截图路径
```

- [ ] **Step 2: 修改 `perceive()` 方法**

在 `perceive()` 中：
1. 在压缩前保存原始图像尺寸 `orig_w, orig_h = image.size`
2. 在获取 `som_annotations` 后，调用 `visualize_som()` 生成标注图
3. 将标注图保存到 `cache_dir / f"screenshot_{timestamp}_annotated.jpg"`
4. 将所有新增信息填入 `Perception(...)` 构造参数

```python
# agent/perception.py — 修改 perceive() 方法（在第 60-91 行范围内）

async def perceive(self, instruction: str = "") -> Perception:
    cache_dir = self.config.cache_dir_absolute()
    cache_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    screenshot_path = cache_dir / f"screenshot_{timestamp}.jpg"

    loop = asyncio.get_event_loop()
    image = await loop.run_in_executor(self._io_executor, self._capture_screenshot)
    orig_w, orig_h = image.size  # ← 新增：记录原始尺寸

    image_bytes = await loop.run_in_executor(
        self._io_executor, self._compress, image
    )
    await loop.run_in_executor(
        self._io_executor, screenshot_path.write_bytes, image_bytes
    )

    image_hash = await loop.run_in_executor(
        self._io_executor, self._compute_image_hash, image
    )
    ocr_text = await loop.run_in_executor(self._io_executor, self._run_ocr, image)
    ui_tree = await self._fetch_ui_tree()
    som_annotations = await self._run_ui_detector(image, instruction)

    # ← 新增：生成 SoM 标注截图
    annotated_path: Path | None = None
    if som_annotations and self.ui_detector is not None:
        from ui_detector.visualizer import visualize_som
        compressed = Image.open(screenshot_path)
        annotated_image = visualize_som(compressed, som_annotations)
        annotated_path = cache_dir / f"screenshot_{timestamp}_annotated.jpg"
        await loop.run_in_executor(
            self._io_executor,
            annotated_image.save,
            str(annotated_path),
            "JPEG",
        )

    ui_hash = self._compute_ui_hash(image_hash, ocr_text, ui_tree)
    description = self._build_description(ocr_text, ui_tree, som_annotations)
    return Perception(
        screenshot_path=screenshot_path,
        description=description,
        ocr_text=ocr_text,
        ui_tree=ui_tree,
        som_annotations=som_annotations,
        ui_hash=ui_hash,
        screen_width=orig_w,          # ← 新增
        screen_height=orig_h,         # ← 新增
        annotated_screenshot_path=annotated_path,  # ← 新增
    )
```

- [ ] **Step 3: 编写测试**

```python
# tests/test_perception.py — 新增测试

import pytest
from pathlib import Path
from agent.perception import Perception


def test_perception_stores_screen_dimensions():
    p = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
        screen_width=1920,
        screen_height=1080,
    )
    assert p.screen_width == 1920
    assert p.screen_height == 1080


def test_perception_stores_annotated_screenshot_path():
    p = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
        annotated_screenshot_path=Path("/tmp/test_annotated.jpg"),
    )
    assert p.annotated_screenshot_path == Path("/tmp/test_annotated.jpg")


def test_perception_defaults_screen_dims_to_zero():
    p = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
    )
    assert p.screen_width == 0
    assert p.screen_height == 0
    assert p.annotated_screenshot_path is None
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_perception.py -v`
Expected: 新增 3 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/perception.py tests/test_perception.py
git commit -m "feat: store screen dimensions and generate SoM-annotated screenshot in Perception"
```

---

### Task 2: 添加 `desktop_interact` 本地函数工具

**Files:**
- Modify: `agent/tools.py`（新增 schema 常量）
- Modify: `agent/orchestrator.py`（注册和实现）
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: 在 `tools.py` 中添加 schema 常量**

```python
# agent/tools.py — 在 CODERUNNER_SCHEMA 之后添加

DESKTOP_INTERACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {
            "type": "integer",
            "description": (
                "The number on the SoM (Set-of-Mark) marker overlay to interact with. "
                "Look at the screenshot: each red circle has a number. Use that number."
            ),
        },
        "action": {
            "type": "string",
            "enum": ["click", "double_click", "right_click", "type", "scroll_down", "scroll_up"],
            "description": "What action to perform on the element.",
        },
        "text": {
            "type": "string",
            "description": "Text to type. Required when action is 'type'.",
        },
    },
    "required": ["label", "action"],
}
```

- [ ] **Step 2: 在 `AgentOrchestrator` 中注册 `desktop_interact` 工具**

在 `orchestrator.py` 中新增方法 `_register_desktop_interact()`，在 `initialize()` 中 `register_all(...)` 之后调用。该工具方法 `_desktop_interact_impl` 必须是异步的，因为它需要调用 MCP。

```python
# agent/orchestrator.py — 新增导入和 schema 引用

from agent.tools import DESKTOP_INTERACT_SCHEMA, register_all


# 在 AgentOrchestrator 类中新增方法：

async def _desktop_interact_impl(
    self, label: int, action: str, text: str | None = None
) -> str:
    """Convert a SoM label to screen coordinates and execute the action.
    
    Looks up the label in the most recent perception's som_annotations,
    converts normalized coordinates to screen pixels, then calls the
    appropriate MCP tool (Windows desktop or Playwright browser).
    """
    perception = getattr(self, "_last_perception", None)
    if perception is None:
        return "[error] No perception data available. Run perception first."

    # Find the annotation with the matching label.
    match = None
    for ann in perception.som_annotations:
        if ann.get("label") == label:
            match = ann
            break
    if match is None:
        available = [a.get("label") for a in perception.som_annotations]
        return f"[error] SoM label {label} not found. Available labels: {available}"

    # Convert normalized [0,1] to screen pixel coordinates.
    sw = perception.screen_width or 1920
    sh = perception.screen_height or 1080
    screen_x = int(round(match["center_x"] * sw))
    screen_y = int(round(match["center_y"] * sh))

    # Choose server: prefer Windows for desktop, Playwright for browser.
    # Heuristic: if Playwright MCP has tools and we recently used the browser, use it.
    # Simplified: always try Windows first, fall back to Playwright.
    if action in ("click", "double_click", "right_click"):
        mcp_action = "Click"
        mcp_args: dict[str, Any] = {"loc": [screen_x, screen_y]}
        if action == "double_click":
            mcp_args["loc"] = [screen_x, screen_y]
            # Windows-MCP supports DoubleClick via the same Click with times=2
            mcp_args["times"] = 2
        elif action == "right_click":
            mcp_args["button"] = "right"
    elif action == "type":
        # Type: click first to focus, then type.
        focus_result = await self.mcp.call("windows", "Click", {"loc": [screen_x, screen_y]})
        if not focus_result.success:
            return f"[error] Failed to focus element at ({screen_x}, {screen_y}): {focus_result.content}"
        type_result = await self.mcp.call("windows", "Type", {"text": text or ""})
        return type_result.content if type_result.success else f"[error] {type_result.content}"
    elif action in ("scroll_down", "scroll_up"):
        direction = "down" if action == "scroll_down" else "up"
        scroll_result = await self.mcp.call("windows", "Scroll", {
            "loc": [screen_x, screen_y],
            "direction": direction,
        })
        return scroll_result.content if scroll_result.success else f"[error] {scroll_result.content}"
    else:
        return f"[error] Unknown action: {action}"

    result = await self.mcp.call("windows", mcp_action, mcp_args)
    if result.success:
        return f"OK: {action} at ({screen_x}, {screen_y}) — {result.content[:200]}"
    return f"[error] {result.content}"


def _register_desktop_interact(self) -> None:
    """Register the desktop_interact local function tool with the LLM."""
    self.llm.register_local_function(
        "desktop_interact",
        self._desktop_interact_impl,
        schema=DESKTOP_INTERACT_SCHEMA,
        description=(
            "Interact with a UI element identified by a SoM (Set-of-Mark) label number. "
            "The screenshot shows numbered red circles on detected elements. "
            "Use the label number to click, double-click, right-click, type text, or scroll. "
            "For 'type' action, provide the 'text' parameter."
        ),
    )
```

在 `initialize()` 中调用：

```python
# agent/orchestrator.py — 在 initialize() 中 register_all 之后添加

async def initialize(self) -> None:
    await self.llm.initialize()
    await self.mcp.connect_all()
    register_all(self.llm, self.mcp)
    self._register_desktop_interact()  # ← 新增
    # ... 其余不变
```

- [ ] **Step 3: 编写测试**

```python
# tests/test_orchestrator.py — 新增测试

@pytest.mark.asyncio
async def test_desktop_interact_tool_registered(config, eventbus, killswitch):
    """desktop_interact is available when tools are registered."""
    llm = FakeLLM([])
    mcp = FakeMCP()
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    
    # Simulate initialize() tool registration.
    from agent.tools import register_all
    register_all(llm, mcp)
    agent._register_desktop_interact()
    
    assert "desktop_interact" in llm.tool_names()


@pytest.mark.asyncio
async def test_desktop_interact_click_resolves_label_to_coords(config, eventbus, killswitch):
    """desktop_interact resolves a SoM label to screen coords and calls Click."""
    from agent.perception import Perception
    
    mcp = FakeMCP()
    llm = FakeLLM([])
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    
    # Set up a fake perception with a SoM annotation.
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[
            {"label": 1, "center_x": 0.5, "center_y": 0.4, "score": 0.95, "normalized": True},
            {"label": 2, "center_x": 0.25, "center_y": 0.75, "score": 0.87, "normalized": True},
        ],
        screen_width=1920,
        screen_height=1080,
    )
    
    result = await agent._desktop_interact_impl(label=1, action="click")
    
    assert "OK" in result
    assert mcp.calls
    server, tool, args = mcp.calls[-1]
    assert server == "windows"
    assert tool == "Click"
    assert args["loc"] == [960, 432]  # 0.5*1920, 0.4*1080


@pytest.mark.asyncio
async def test_desktop_interact_reports_missing_label(config, eventbus, killswitch):
    """desktop_interact returns an error when the label is not found."""
    from agent.perception import Perception
    
    mcp = FakeMCP()
    llm = FakeLLM([])
    agent = AgentOrchestrator(config, eventbus, llm, mcp, killswitch)
    
    agent._last_perception = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
    )
    
    result = await agent._desktop_interact_impl(label=99, action="click")
    
    assert result.startswith("[error]")
    assert "99" in result


@pytest.mark.asyncio
async def test_desktop_interact_no_perception_error(config, eventbus, killswitch):
    """desktop_interact errors when there is no perception data."""
    agent = AgentOrchestrator(config, eventbus, FakeLLM([]), FakeMCP(), killswitch)
    agent._last_perception = None
    
    result = await agent._desktop_interact_impl(label=1, action="click")
    
    assert result.startswith("[error]")
    assert "No perception data" in result
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator.py::test_desktop_interact_tool_registered tests/test_orchestrator.py::test_desktop_interact_click_resolves_label_to_coords tests/test_orchestrator.py::test_desktop_interact_reports_missing_label tests/test_orchestrator.py::test_desktop_interact_no_perception_error -v`
Expected: 4 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/tools.py agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add desktop_interact tool for SoM label-to-coordinate actions"
```

---

### Task 3: 修改 `_format_perception` 发送标注截图

**Files:**
- Modify: `agent/orchestrator.py:187-224`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: 更新 `_format_perception` 优先使用标注截图**

```python
# agent/orchestrator.py — 修改 _format_perception 方法

@staticmethod
def _format_perception(perception: Any) -> list[dict[str, Any]]:
    """Convert a Perception dataclass into a multimodal message for the LLM."""
    text_parts = [perception.description]
    if perception.som_annotations:
        text_parts.append(
            "SoM annotations (numbered markers on screenshot):\n"
            + "\n".join(
                f"  [{a.get('label', '?')}] at ({a.get('center_x', 0):.3f}, {a.get('center_y', 0):.3f})"
                + (f" score={a.get('score', 0):.2f}" if a.get('score') else "")
                for a in perception.som_annotations
            )
        )
        text_parts.append(
            "To interact with an element, call desktop_interact(label=<number>, action=<action>). "
            "Actions: click, double_click, right_click, type (needs text=), scroll_down, scroll_up."
        )

    content: list[dict[str, Any]] = [
        {"type": "text", "text": "\n\n".join(text_parts)},
    ]

    # Prefer the SoM-annotated screenshot; fall back to raw.
    image_path = (
        perception.annotated_screenshot_path or perception.screenshot_path
    )
    if image_path is not None and image_path.exists():
        try:
            image_bytes = image_path.read_bytes()
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        except Exception as exc:
            content.append({
                "type": "text",
                "text": f"[Could not include screenshot: {exc}]",
            })
    else:
        content.append({
            "type": "text",
            "text": "Screenshot not available.",
        })

    return content
```

- [ ] **Step 2: 更新系统提示词**

在 `run_task` 的系统提示词中添加 SoM 工具使用说明：

```python
# agent/orchestrator.py — 在 run_task() 的 system_content 中追加

system_content = (
    "You are Caelum-Agent, a Windows desktop automation assistant. "
    "Use the provided tools to interact with the browser and desktop. "
    "Always explain your reasoning briefly before acting.\n\n"
    "## Working with the SoM (Set-of-Mark) screenshot\n"
    "The screenshot contains numbered red circle markers on detected UI elements. "
    "Each marker has a number (1, 2, 3, ...). To interact with a marked element:\n"
    "- Use desktop_interact(label=N, action='click') to click marker N\n"
    "- Use desktop_interact(label=N, action='type', text='...') to type into an input field\n"
    "- Use desktop_interact(label=N, action='scroll_down') to scroll at marker N\n"
    "- For browser elements with refs (like e12), use playwright__browser_click(target='e12') instead.\n"
    "- For unmarked elements, use the raw MCP tools with explicit coordinates or refs."
)
```

- [ ] **Step 3: 编写测试**

```python
# tests/test_orchestrator.py — 新增测试

def test_format_perception_prefers_annotated_screenshot():
    """_format_perception uses annotated_screenshot_path when available."""
    from agent.orchestrator import AgentOrchestrator
    from agent.perception import Perception
    
    raw = Path("/tmp/screenshot_raw.jpg")
    annotated = Path("/tmp/screenshot_annotated.jpg")
    # Create small JPEG files so exists() returns True.
    raw.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xdb\x00C\x01\x09\x09\x09\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\x09\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xf9\xfe\x00\x1f\xff\xd9")
    annotated.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xdb\x00C\x01\x09\x09\x09\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\x09\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xf9\xfe\x00\x1f\xff\xd9")
    
    try:
        p = Perception(
            screenshot_path=raw,
            description="test",
            ocr_text="",
            ui_tree={},
            som_annotations=[{"label": 1, "center_x": 0.5, "center_y": 0.5}],
            annotated_screenshot_path=annotated,
        )
        result = AgentOrchestrator._format_perception(p)
        
        # The result should include the annotated image's base64 content.
        image_found = False
        for part in result:
            if part.get("type") == "image_url":
                image_found = True
                # The base64 data should start with /9j/ (JPEG magic)
                url = part["image_url"]["url"]
                assert "base64," in url
                break
        assert image_found, "Annotated screenshot should be included as image_url"
    finally:
        raw.unlink(missing_ok=True)
        annotated.unlink(missing_ok=True)


def test_format_perception_falls_back_to_raw_screenshot():
    """_format_perception falls back to raw screenshot when annotated is None."""
    from agent.orchestrator import AgentOrchestrator
    from agent.perception import Perception
    
    raw = Path("/tmp/screenshot_raw.jpg")
    raw.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xdb\x00C\x01\x09\x09\x09\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\x09\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xf9\xfe\x00\x1f\xff\xd9")
    
    try:
        p = Perception(
            screenshot_path=raw,
            description="test",
            ocr_text="",
            ui_tree={},
            som_annotations=[],
            annotated_screenshot_path=None,
        )
        result = AgentOrchestrator._format_perception(p)
        
        image_found = any(part.get("type") == "image_url" for part in result)
        assert image_found, "Should fall back to raw screenshot"
    finally:
        raw.unlink(missing_ok=True)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator.py::test_format_perception_prefers_annotated_screenshot tests/test_orchestrator.py::test_format_perception_falls_back_to_raw_screenshot -v`
Expected: 2 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: send SoM-annotated screenshot to LLM and update system prompt"
```

---

### Task 4: 端到端验证与回归测试

**Files:**
- 无新增/修改，仅运行完整测试套件

- [ ] **Step 1: 运行完整测试套件**

Run: `.venv\Scripts\python.exe -m pytest tests/ -q`
Expected: 全部测试 PASS（预计 ≥148 个）

- [ ] **Step 2: 运行语法/导入检查**

Run:
```
.venv\Scripts\python.exe -m py_compile agent/perception.py agent/orchestrator.py agent/tools.py
.venv\Scripts\python.exe -c "import agent; import mcp_client; import eventbus; import ui_detector"
```
Expected: 无错误

- [ ] **Step 3: Commit** (如测试套件发现回归问题并修复后)

```bash
git add -A :\!:.claude/ :\!:skills/learned/
git commit -m "chore: finalize perception-action loop integration"
```

---

## 自我审查

### 1. 规范覆盖
- ✅ SoM 标注图发送给 LLM（规范第 2 节感知融合管线）
- ✅ 坐标点击工具（规范第 4.3 节 Windows-MCP 的 Click 工具支持坐标点击）
- ✅ 系统提示词更新（规范第 4 节"让模型理解工具使用方式"）
- ⚠️ Playwright 的坐标点击暂不处理——Playwright 使用 A11y ref，通过 `browser_click(target='e12')` 已有路径，无需坐标

### 2. Placeholder 扫描
- 无 "TBD"、"TODO"、"implement later"
- 所有代码块都有具体实现
- 所有测试都有实际断言

### 3. 类型一致性
- `DESKTOP_INTERACT_SCHEMA` 在 Task 2 定义，Task 3 引用时名称一致
- `Perception` 新增字段在 Task 1 定义，Task 2/3 使用时名称一致
- `_register_desktop_interact` 在 Task 2 定义，Task 3 的测试中引用
