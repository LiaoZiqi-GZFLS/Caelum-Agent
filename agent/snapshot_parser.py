"""Accessibility tree parsing helpers for Windows-MCP and Playwright snapshots."""

from __future__ import annotations

import json
import logging
import re
from typing import Any


logger = logging.getLogger("caelum.snapshot")


class UIElement:
    def __init__(
        self,
        element_id: str | None,
        role: str,
        name: str,
        bounds: tuple[int, int, int, int] | None = None,
        children: list[UIElement] | None = None,
        center: tuple[int, int] | None = None,
        action: str | None = None,
    ) -> None:
        self.element_id = element_id
        self.role = role
        self.name = name
        self.bounds = bounds
        self.children = children or []
        self.center = center
        self.action = action

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.element_id,
            "role": self.role,
            "name": self.name,
            "bounds": self.bounds,
            "children": [c.to_dict() for c in self.children],
        }
        if self.center is not None:
            d["center"] = self.center
        if self.action is not None:
            d["action"] = self.action
        return d

    @property
    def is_interactive(self) -> bool:
        # Box-format snapshots mark interactivity with a trailing [action: ...].
        if self.action:
            return True
        # Legacy `[id] Role 'Name'` format uses English role names.
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
    """Parse a Windows-MCP Snapshot into a UIElement tree.

    Two formats are recognised:

    * **Box format** (current windows-mcp): the Snapshot body is a tree drawn
      with Unicode box-drawing characters, e.g.
      ``├── (905,332) 按钮 "关闭"  [action: click]`` under a ``UI Tree:``
      section that follows window/header tables.
    * **Legacy format**: ``[1] Button 'OK' (x=100, y=200, w=80, h=30)`` with
      indentation for nesting.
    """
    if any(ch in text for ch in "├└│"):
        return _parse_windows_box(text)
    return _parse_windows_legacy(text)


def unwrap_windows_snapshot(content: str) -> str:
    """Return the inner snapshot text from a windows-mcp Snapshot content block.

    Current windows-mcp returns the Snapshot as a JSON-encoded single-element
    array (``["<snapshot text>"]``) with embedded newlines/quotes escaped. Feed
    that straight to the parser and nothing matches. If the content looks like
    such a wrapper, decode it and return the first string element; otherwise
    return it unchanged.
    """
    stripped = content.lstrip()
    if not stripped.startswith("["):
        return content
    try:
        decoded = json.loads(content)
    except (ValueError, TypeError):
        return content
    if isinstance(decoded, list) and decoded and isinstance(decoded[0], str):
        return decoded[0]
    return content


def _parse_windows_legacy(text: str) -> UIElement:
    """Parse the legacy ``[id] Role 'Name'`` Snapshot text into a tree."""
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


# Branch connectors used by the windows-mcp box-drawing tree.
_BOX_BRANCH = "├└"
# Payload (after the leading tree-drawing prefix) of a box-format node:
#   (x,y) 角色 "名称"  [action: click]  [toggle: off]
# The coordinate, name and tags are all optional (containers may have none).
_BOX_PAYLOAD = re.compile(
    r"^(?:\((?P<x>\d+),\s*(?P<y>\d+)\)\s*)?"
    r"(?P<role>[^\s\"][^\"]*?)?"
    r"(?:\s*\"(?P<name>.*?)\")?"
    r"\s*(?P<tags>(?:\[[^\]]+\]\s*)*)$"
)
_BOX_PREFIX = re.compile(r"^[\s│├└─]*")


def _parse_windows_box(text: str) -> UIElement:
    """Parse the current windows-mcp box-drawing tree into a UIElement tree.

    Depth is derived from the leading box-drawing prefix: each ancestor level
    contributes one ``│`` and the node's own branch (``├``/``└``) adds one.
    Window/table headers above ``UI Tree:`` are ignored for the tree (window
    titles are still captured from ``window "..."`` nodes).
    """
    root = UIElement(element_id="root", role="window", name="desktop")
    stack: list[tuple[int, UIElement]] = [(0, root)]
    in_tree = False

    for raw in text.splitlines():
        if not in_tree:
            # The tree begins at the "desktop" root line following "UI Tree:".
            if raw.strip() == "desktop":
                in_tree = True
            continue

        stripped = raw.strip()
        if not stripped:
            continue

        # Depth comes from the COLUMN of the branch connector (├/└): each tree
        # level indents by 4 columns, and a level where the ancestor is the last
        # child uses spaces (not ``│``), so counting ``│`` undercounts depth.
        branch_col = -1
        for ch in _BOX_BRANCH:
            idx = raw.find(ch)
            if idx != -1 and (branch_col == -1 or idx < branch_col):
                branch_col = idx
        if branch_col < 0:
            # No connector inside the tree region (shouldn't happen); skip.
            continue
        depth = (branch_col - 4) // 4 + 1

        payload = _BOX_PREFIX.sub("", raw)
        match = _BOX_PAYLOAD.match(payload)
        if not match:
            continue

        role = (match.group("role") or "").strip()
        name = (match.group("name") or "").replace("\r\n", " ").replace("\n", " ").strip()
        if not role and not name:
            continue

        center = None
        if match.group("x"):
            center = (int(match.group("x")), int(match.group("y")))
        action = None
        tags = match.group("tags") or ""
        action_match = re.search(r"\[action:\s*([^\]]+)\]", tags)
        if action_match:
            action = action_match.group(1).strip()

        element = UIElement(
            element_id=None,
            role=role or "unknown",
            name=name,
            center=center,
            action=action,
        )

        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else root
        parent.children.append(element)
        stack.append((depth, element))

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
    except Exception as exc:
        logger.debug("Failed to parse Playwright snapshot as YAML: %s", exc)
        # Fallback: treat the whole text as an unstructured root.
        return UIElement(element_id="root", role="document", name=text[:500])

    if isinstance(data, dict):
        root = _parse_playwright_yaml_node(data)
        if root:
            return root
    return UIElement(element_id="root", role="document", name="browser")


def summarize_tree(root: UIElement, max_depth: int = 8) -> str:
    """Convert a UIElement tree into a compact text description.

    Includes interactive elements, the root, and ``window`` nodes (so the
    focused/opened window titles stay visible for targeting even though windows
    are not themselves interactive).
    """
    lines: list[str] = []

    def walk(node: UIElement, depth: int) -> None:
        if depth > max_depth:
            return
        if node.is_interactive or depth == 0 or node.role == "window":
            prefix = "  " * depth
            id_part = f"[{node.element_id}]" if node.element_id else ""
            name_part = f" {node.name!r}" if node.name else ""
            center_part = f" @{node.center[0]},{node.center[1]}" if node.center else ""
            action_part = f" ({node.action})" if node.action else ""
            lines.append(
                f"{prefix}{id_part} {node.role}{name_part}{center_part}{action_part}"
            )
        for child in node.children:
            walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines)
