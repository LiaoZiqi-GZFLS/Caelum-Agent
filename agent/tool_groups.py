"""Tool groups for K3 dynamic tool loading.

K3 supports ``tool_choice`` and injecting tools via system messages. To save
tokens, tools are partitioned into groups; only core tools are always visible,
and other groups are activated on demand or automatically (e.g., perception
tools after a screenshot).

Group membership is matched by tool name prefix or exact name. ``*`` wildcard
in a group matches any tool whose name starts with the given prefix.
"""

from __future__ import annotations

# Core tools always visible to the model.
CORE_TOOLS: set[str] = {
    "CompleteTask",
    "DesktopInteract",
    "CodeRunner",
    "RequestHumanHelp",
    "SelfWindow",
}

# Groups activated on demand or by heuristics.
TOOL_GROUPS: dict[str, set[str]] = {
    "perception": {
        "ZoomRegion",
        "NearbyLabels",
        "PreviewPoints",
        "UpgradeVision",
        "CaptureWindow",
        "Wait",
    },
    "browser": {"playwright__*"},
    "desktop": {"windows__*"},
    "filesystem": {"filesystem__*"},
    "writing": {
        "DraftContent",
        "GenerateImage",
        "ReadDocument",
        "ViewMedia",
    },
    "task": {
        "UpdateTaskList",
        "FocusGuard",
    },
}

# All Formula tools are in one group (they share a common prefix pattern:
# their names come from Formula URIs, not predictable prefixes).
# They're included when "formula" group is active.
FORMULA_GROUP = "formula"


def _matches(tool_name: str, group_names: set[str]) -> bool:
    """Check if a tool belongs to a group, respecting ``*`` wildcards."""
    for name in group_names:
        if name.endswith("__*"):
            if tool_name.startswith(name[:-2]):  # strip trailing *
                return True
        elif tool_name == name:
            return True
    return False


def tool_names_for_groups(
    active_groups: set[str],
    all_tool_names: set[str],
    formula_names: set[str] | None = None,
) -> set[str]:
    """Return the set of tool names that should be visible given the active
    groups. Core tools are always included. ``None`` active_groups means
    ALL tools."""
    if active_groups is None:
        return set(all_tool_names)
    allowed = set(CORE_TOOLS)
    for group in active_groups:
        if group == FORMULA_GROUP and formula_names:
            allowed |= formula_names
        group_patterns = TOOL_GROUPS.get(group, set())
        allowed |= {n for n in all_tool_names if _matches(n, group_patterns)}
    return allowed
