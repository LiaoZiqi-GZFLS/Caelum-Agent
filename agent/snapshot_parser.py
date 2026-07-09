"""Accessibility tree parsing helpers for Windows-MCP and Playwright snapshots."""

from __future__ import annotations

import re
from typing import Any


class UIElement:
    def __init__(
        self,
        element_id: str | None,
        role: str,
        name: str,
        bounds: tuple[int, int, int, int] | None = None,
        children: list[UIElement] | None = None,
    ) -> None:
        self.element_id = element_id
        self.role = role
        self.name = name
        self.bounds = bounds
        self.children = children or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.element_id,
            "role": self.role,
            "name": self.name,
            "bounds": self.bounds,
            "children": [c.to_dict() for c in self.children],
        }

    @property
    def is_interactive(self) -> bool:
        return self.role.lower() in {
            "button",
            "edit",
            "hyperlink",
            "menuitem",
            "tabitem",
            "treeitem",
            "listitem",
            "combo",
            "check",
            "radio",
            "link",
            "text",
        }

    def __repr__(self) -> str:
        return f"UIElement({self.element_id}, {self.role}, {self.name!r})"


def parse_windows_snapshot(text: str) -> UIElement:
    """Parse Windows-MCP Snapshot YAML-ish text into a UIElement tree.

    Snapshot lines typically look like:
      [1] Button 'OK' (x=100, y=200, w=80, h=30)
        [2] Edit ''
    """
    lines = text.splitlines()
    root = UIElement(element_id="root", role="window", name="desktop")
    stack: list[tuple[int, UIElement]] = [(-1, root)]

    pattern = re.compile(
        r"^(\s*)\[(?P<id>[^\]]+)\]\s+(?P<role>\w+)\s+'(?P<name>.*?)'"
        r"(?:\s*\(x=(?P<x>\d+),\s*y=(?P<y>\d+),\s*w=(?P<w>\d+),\s*h=(?P<h>\d+)\))?"
    )

    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        element_id = match.group("id")
        role = match.group("role")
        name = match.group("name")
        bounds = None
        if match.group("x"):
            bounds = (
                int(match.group("x")),
                int(match.group("y")),
                int(match.group("w")),
                int(match.group("h")),
            )
        element = UIElement(element_id=element_id, role=role, name=name, bounds=bounds)

        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            stack[-1][1].children.append(element)
        stack.append((indent, element))

    return root


def _parse_playwright_yaml_node(node: dict[str, Any]) -> UIElement | None:
    element_id = node.get("ref") or node.get("id") or node.get("target")
    role = node.get("role", "")
    name = node.get("name", "") or ""
    if not role and not name:
        return None
    children = [
        child
        for child in (_parse_playwright_yaml_node(c) for c in node.get("children", []))
        if child
    ]
    return UIElement(element_id=element_id, role=role, name=name, children=children)


def parse_playwright_snapshot(text: str) -> UIElement:
    """Parse Playwright browser_snapshot YAML output into a UIElement tree."""
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception:
        # Fallback: treat the whole text as an unstructured root.
        return UIElement(element_id="root", role="document", name=text[:500])

    if isinstance(data, dict):
        root = _parse_playwright_yaml_node(data)
        if root:
            return root
    return UIElement(element_id="root", role="document", name="browser")


def summarize_tree(root: UIElement, max_depth: int = 3) -> str:
    """Convert a UIElement tree into a compact text description."""
    lines: list[str] = []

    def walk(node: UIElement, depth: int) -> None:
        if depth > max_depth:
            return
        if node.is_interactive or depth == 0:
            prefix = "  " * depth
            id_part = f"[{node.element_id}]" if node.element_id else ""
            name_part = f" {node.name!r}" if node.name else ""
            lines.append(f"{prefix}{id_part} {node.role}{name_part}")
        for child in node.children:
            walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines)
