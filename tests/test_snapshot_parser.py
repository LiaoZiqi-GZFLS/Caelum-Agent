"""Tests for accessibility snapshot parsers."""

from agent.snapshot_parser import (
    UIElement,
    parse_playwright_snapshot,
    parse_windows_snapshot,
    summarize_tree,
    unwrap_windows_snapshot,
)


def test_parse_windows_snapshot():
    text = """
[1] Window 'Notepad'
  [2] Button 'OK' (x=100, y=200, w=80, h=30)
  [3] Edit '' (x=100, y=240, w=400, h=30)
"""
    root = parse_windows_snapshot(text)
    assert root.role == "window"
    assert len(root.children) == 1
    notepad = root.children[0]
    assert notepad.name == "Notepad"
    assert len(notepad.children) == 2
    assert notepad.children[0].role == "Button"
    assert notepad.children[0].bounds == (100, 200, 80, 30)


def test_summarize_tree_includes_interactive():
    root = UIElement(
        element_id="root",
        role="window",
        name="app",
        children=[
            UIElement(element_id="b1", role="button", name="Click me"),
            UIElement(element_id="d1", role="group", name="container"),
        ],
    )
    text = summarize_tree(root)
    assert "Click me" in text
    assert "button" in text


def test_parse_playwright_snapshot_yaml():
    text = """
role: document
name: Example
ref: root
children:
  - role: button
    name: Submit
    ref: e1
"""
    root = parse_playwright_snapshot(text)
    assert root.role == "document"
    assert len(root.children) == 1
    assert root.children[0].element_id == "e1"


def test_parse_playwright_snapshot_fallback():
    root = parse_playwright_snapshot("not yaml at all")
    assert root.role == "document"


def test_parse_windows_snapshot_preserves_cjk_names():
    # Regression guard: window/element names with non-ASCII (Chinese) must
    # survive parsing unchanged, with no U+FFFD replacement chars introduced.
    text = """
[1] Window '测试文档-中文.txt - Notepad'
  [2] Edit '编辑' (x=10, y=20, w=100, h=20)
"""
    root = parse_windows_snapshot(text)
    window = root.children[0]
    edit = window.children[0]

    assert window.name == "测试文档-中文.txt - Notepad"
    assert edit.name == "编辑"
    assert "�" not in window.name
    assert "�" not in edit.name


def test_summarize_tree_preserves_cjk_names():
    # summarize_tree only emits interactive elements, so the CJK window is
    # dropped by design; the interactive CJK button must survive unchanged.
    root = UIElement(
        element_id="root",
        role="window",
        name="desktop",
        children=[
            UIElement(element_id="w1", role="window", name="测试文档-中文.txt"),
            UIElement(element_id="b1", role="button", name="关闭标签页"),
        ],
    )
    text = summarize_tree(root)
    assert "关闭标签页" in text
    assert "�" not in text


# ---------------------------------------------------------------------------
# Current windows-mcp box-drawing format
# ---------------------------------------------------------------------------

BOX_SAMPLE = """
    Cursor Position: (0, 0)

    Focused Window:
    Name      Depth  Status      Width    Height    Handle
------  -------  --------  -------  --------  --------
任务栏           0  Normal       2560        48    131510

    UI Tree:
    desktop
    ├── window "任务栏"
    │   └── 窗格 "任务栏"
    │       ├── (728,1416) 按钮 "开始"  [action: click]  [toggle:off]
    │       └── (2554,1416) 按钮 "显示桌面"  [action: click]
    ├── window ""
    └── window "设置"
"""


def test_unwrap_windows_snapshot_decodes_json_array():
    wrapped = '["    desktop\\n    ├── window \\"任务栏\\""]'
    inner = unwrap_windows_snapshot(wrapped)
    assert inner.splitlines()[0] == "    desktop"
    assert 'window "任务栏"' in inner


def test_unwrap_windows_snapshot_passthrough():
    assert unwrap_windows_snapshot("plain text") == "plain text"
    assert unwrap_windows_snapshot('["not valid json') == '["not valid json'


def test_parse_windows_box_format_structure():
    root = parse_windows_snapshot(BOX_SAMPLE)

    windows = root.children
    assert [w.name for w in windows] == ["任务栏", "", "设置"]
    assert all(w.role == "window" for w in windows)

    taskbar = windows[0]
    pane = taskbar.children[0]
    assert pane.role == "窗格"
    assert pane.name == "任务栏"
    assert not pane.is_interactive  # container: no [action: ...]

    buttons = pane.children
    assert len(buttons) == 2
    start = buttons[0]
    assert start.role == "按钮"
    assert start.name == "开始"
    assert start.center == (728, 1416)
    assert start.action == "click"
    assert start.is_interactive

    show_desktop = buttons[1]
    assert show_desktop.center == (2554, 1416)
    assert show_desktop.action == "click"


def test_summarize_tree_box_includes_windows_and_actions():
    root = parse_windows_snapshot(BOX_SAMPLE)
    text = summarize_tree(root)
    # window titles are kept for targeting
    assert "任务栏" in text
    assert "设置" in text
    # interactive leaves and their actions/coords surface
    assert "开始" in text
    assert "显示桌面" in text
    assert "click" in text
    assert "728,1416" in text
