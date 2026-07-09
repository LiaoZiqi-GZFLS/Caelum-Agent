"""Package marker for spikes/.

Running ``python spikes/e2e_single_task.py`` directly starts Python with only
``spikes/`` on sys.path, so the ``agent``, ``eventbus``, ``mcp_client`` and
``ui_detector`` sibling packages are not importable.  This file detects that
situation and injects the project root onto ``sys.path`` before any sibling
imports are resolved.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SPIKES_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SPIKES_DIR.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

__all__: list[str] = []
