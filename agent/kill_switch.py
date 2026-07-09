"""Kill switch via global keyboard listener and SIGINT handler."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

from pynput import keyboard

from eventbus import EventBus
from eventbus.events import KillSwitchTriggered


class KillSwitch:
    def __init__(self, eventbus: EventBus) -> None:
        self.eventbus = eventbus
        self._listener: keyboard.Listener | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pressed: set[keyboard.Key | keyboard.KeyCode] = set()
        self._triggered = asyncio.Event()
        self._original_sigint: signal.Handlers | None = None
        self._armed = asyncio.Event()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()
        self._original_sigint = signal.signal(signal.SIGINT, self._on_sigint)

    def stop(self) -> None:
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)
            self._original_sigint = None
        if self._listener:
            self._listener.stop()
            self._listener = None

    def is_triggered(self) -> bool:
        return self._triggered.is_set()

    def reset(self) -> None:
        # asyncio.Event is not thread-safe; marshal clear() to the loop if it is
        # running. reset() should otherwise be called from the loop thread.
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._triggered.clear)
            self._loop.call_soon_threadsafe(self._armed.clear)
        else:
            self._triggered.clear()
            self._armed.clear()

    def _on_sigint(self, signum: int, frame: Any) -> None:
        self._trigger("sigint")

    def _on_press(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        self._pressed.add(key)
        if self._is_ctrl_c(key):
            self._trigger("ctrl+c")

    def _on_release(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        self._pressed.discard(key)

    def _is_ctrl_c(self, key: keyboard.Key | keyboard.KeyCode) -> bool:
        # pynput represents Ctrl+C as Key.ctrl_l held and a 'c' character.
        if not hasattr(key, "char") or key.char != "c":
            return False
        ctrl_keys = {keyboard.Key.ctrl_l, keyboard.Key.ctrl_r, keyboard.Key.ctrl}
        return bool(ctrl_keys & self._pressed)

    def _trigger(self, reason: str) -> None:
        # Serialise the armed check/set and triggered set on the asyncio loop.
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._handle_trigger(reason),
                self._loop,
            )
        else:
            # No running loop to debounce on; fall back to marking triggered.
            self._triggered.set()

    async def _handle_trigger(self, reason: str) -> None:
        if self._armed.is_set():
            return
        self._armed.set()
        self._triggered.set()
        await self.eventbus.emit(KillSwitchTriggered(reason=reason))
        await asyncio.sleep(0.1)
        self._armed.clear()
