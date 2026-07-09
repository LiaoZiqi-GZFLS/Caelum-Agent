"""Tests for accessibility snapshot parsers."""

from agent.snapshot_parser import (
    UIElement,
    parse_playwright_snapshot,
    parse_windows_snapshot,
    summarize_tree,
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
