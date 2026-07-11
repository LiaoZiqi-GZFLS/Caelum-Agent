"""Interactive up/down choice menu on the raw Windows console.

The agent asks the human a question (see ``RequestHumanHelp`` in
``agent/orchestrator.py``); this module renders the choices and reads the
answer. The last row is always a free-text ``type something`` entry the
user can type into directly. Only rendering + the key loop live here;
callers own TTY checks and spinner suspension. Keys are read through an
injectable ``getch`` so tests can script key sequences.
"""

from __future__ import annotations

from typing import Callable

from rich.console import Console

_TYPE_ROW_LABEL = "type something: "
_CURSOR = "█"


def _default_getch() -> str:
    import msvcrt  # Windows-only project; imported lazily for importability

    return msvcrt.getwch()


def ask_choice(
    question: str,
    options: list[str],
    console: Console,
    getch: Callable[[], str] | None = None,
) -> str | None:
    """Show an up/down menu and return the chosen text, or None on cancel.

    ``options`` are the model-provided choices; a free-text row is appended
    automatically as the last entry. Typing anywhere jumps to that row.
    Enter on an empty type row does nothing. ESC / Ctrl+C return None.
    """
    getch = getch or _default_getch
    write = console.file.write
    flush = console.file.flush

    selected = 0
    typed = ""
    n = len(options)

    def frame_lines() -> list[str]:
        lines = [
            f"需要你的帮助：{question}",
            "  (↑↓ 选择，回车确认；在最后一条直接打字，ESC 取消)",
            "",
        ]
        for i, opt in enumerate(options):
            marker = "❯ " if i == selected else "  "
            lines.append(f"{marker}{opt}")
        marker = "❯ " if selected == n else "  "
        cursor = _CURSOR if selected == n else ""
        lines.append(f"{marker}{_TYPE_ROW_LABEL}{typed}{cursor}")
        return lines

    def render(first: bool = False) -> None:
        lines = frame_lines()
        if not first:
            # Cursor sits len(lines) rows below the frame top; move back up.
            write(f"\x1b[{len(lines)}A")
        for line in lines:
            write("\x1b[2K" + line + "\n")
        flush()

    render(first=True)
    while True:
        ch = getch()
        if ch in ("\xe0", "\x00"):
            code = getch()
            if code == "H":  # up
                selected = max(0, selected - 1)
            elif code == "P":  # down
                selected = min(n, selected + 1)
            render()
            continue
        if ch == "\r":
            if selected < n:
                return options[selected]
            if typed:
                return typed
            continue  # empty type row: enter does nothing
        if ch == "\x08":
            if typed:
                typed = typed[:-1]
            render()
            continue
        if ch in ("\x1b", "\x03"):
            return None
        if ch.isprintable():
            typed += ch
            selected = n
            render()
            continue
        render()
