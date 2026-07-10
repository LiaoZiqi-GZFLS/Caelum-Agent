# Upstream issue draft: `UnboundLocalError: tree_node` in `tree/service.py`

> File this against `CursorTouch/Windows-MCP`. This is a local draft; nothing here
> is submitted automatically. To file it: see the command at the bottom.

## Title

`UnboundLocalError: tree_node in tree/service.py when an interactive element has an empty name`

## Body

**Environment**
- windows-mcp 0.8.2
- Windows 11
- Python 3.12

**Symptom**

Every `Snapshot` call prints a burst of errors to stderr:

```
Error in tree_traversal: cannot access local variable 'tree_node' where it is not associated with a value
UnboundLocalError: cannot access local variable 'tree_node' where it is not associated with a value
Error getting nodes for handle <hwnd>: cannot access local variable 'tree_node' ...
Error in processing window '<name>' (handle <hwnd>), retry attempt 4/3
Task failed completely for handle <hwnd> after 3 retries
[Tree] 1 window(s) failed to capture — UI services may be loading
```

The traceback ends at:

```
windows_mcp\tree\service.py:598 in tree_traversal
    control_type=tree_node.control_type,
UnboundLocalError: cannot access local variable 'tree_node' where it is not associated with a value
```

**Root cause**

In `windows_mcp/tree/service.py`, `tree_node` is only assigned inside the
`if name:` branch, but the `if current_semantic_node is not None:` block that
uses `tree_node` is a sibling of `if name:`, not nested inside it. When an
interactive element has an empty `name` and a non-None semantic parent,
`tree_node` is referenced without being bound.

```python
# service.py ~586-605 (current)
if name:
    tree_node = TreeElementNode(**{...})
    interactive_nodes.append(tree_node)
if current_semantic_node is not None:          # sibling of `if name:` — BUG
    current_semantic_node.add_child(SemanticNode(
        control_type=tree_node.control_type,   # tree_node undefined when name == ""
        ...
    ))
```

**Suggested fix**

Nest the `current_semantic_node` block inside `if name:` (or guard the
`tree_node` use with `if name:`):

```diff
                                 if name:
                                     tree_node=TreeElementNode(**{
                                         'name':name,
                                         'control_type':localized_control_type.title(),
                                         'bounding_box':bounding_box,
                                         'center':center,
                                         'window_name':window_name,
                                         'metadata':metadata
                                     })
                                     interactive_nodes.append(tree_node)
-                                if current_semantic_node is not None:
-                                    current_semantic_node.add_child(SemanticNode(
-                                        control_type=tree_node.control_type,
-                                        element_type='interactive',
-                                        name=tree_node.name,
-                                        window_name=tree_node.window_name,
-                                        center=tree_node.center,
-                                        bounding_box=tree_node.bounding_box,
-                                        metadata=dict(tree_node.metadata),
-                                    ))
-                                    semantic_added = True
+                                if current_semantic_node is not None:
+                                    current_semantic_node.add_child(SemanticNode(
+                                        control_type=tree_node.control_type,
+                                        element_type='interactive',
+                                        name=tree_node.name,
+                                        window_name=tree_node.window_name,
+                                        center=tree_node.center,
+                                        bounding_box=tree_node.bounding_box,
+                                        metadata=dict(tree_node.metadata),
+                                    ))
+                                    semantic_added = True
```

**Impact**

The exception is caught inside `Snapshot`, so only the affected window's
subtree is dropped; the overall UI tree is still returned. The main practical
effect is stderr noise on every `Snapshot`, plus missing nodes for the specific
windows that hit the branch (e.g. the agent's own host window, transient
unnamed windows).

---

## How to file (manual)

```powershell
gh issue create --repo CursorTouch/Windows-MCP `
  --title "UnboundLocalError: tree_node in tree/service.py when an interactive element has an empty name" `
  --body-file docs/windows_mcp/upstream-tree-node-issue.md
```

(Strip the "How to file" section before posting, or pass a body that starts at the Title.)
