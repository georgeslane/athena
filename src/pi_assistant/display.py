"""The status board on a Pimoroni Display HAT Mini.

``pi-assistant display`` runs this in its own process (and systemd service). It reads the
status the assistant publishes (status.py), draws it (board.py) and puts it on the screen.
Being separate means the board keeps going, and says so, when the assistant isn't running.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from PIL import Image

from pi_assistant.board import HEIGHT, LED_OFF, WIDTH, Board
from pi_assistant.config import Config
from pi_assistant.status import Snapshot, State, StatusFile

log = logging.getLogger(__name__)

ACTIVE_FPS = 6  # while something on screen is moving
IDLE_FPS = 1
DEMO_SECONDS = 5  # how long --demo shows each example


class DisplayError(Exception):
    """The screen can't be used. The message says how to fix it."""


class Screen(Protocol):
    def show(self, image: Image.Image) -> None: ...

    def set_led(self, red: bool, green: bool, blue: bool) -> None: ...

    def set_backlight(self, on: bool) -> None: ...

    def wait(self, seconds: float) -> bool:
        """Sleep for ``seconds``, returning early with True if a button is pressed."""
        ...

    def close(self) -> None: ...


class DisplayHATMini:
    """Pimoroni Display HAT Mini: a 320x240 ST7789 LCD, an RGB LED and four buttons.

    Pins (BCM) are from https://pinout.xyz/pinout/display_hat_mini and Pimoroni's own
    library. That library is built on RPi.GPIO, which doesn't work on a Pi 5, so this
    drives the hardware with st7789 and gpiod instead, which work on a Pi 4 and 5.
    """

    SPI_PORT, SPI_CS, DC, BACKLIGHT = 0, 1, 9, 13
    LED = (17, 27, 22)  # red, green, blue; lit when low
    BUTTONS = (5, 6, 16, 24)  # A, B, X, Y; low when pressed

    def __init__(self) -> None:
        try:
            import gpiod
            import gpiodevice
            import st7789
            from gpiod.line import Bias, Direction, Edge, Value
        except ImportError as exc:
            raise DisplayError(
                f"The display drivers aren't installed ({exc.name}). Run: bash scripts/install.sh --display"
            ) from exc
        self._on, self._off = Value.ACTIVE, Value.INACTIVE
        try:
            self._lcd = st7789.ST7789(
                port=self.SPI_PORT,
                cs=self.SPI_CS,
                dc=self.DC,
                backlight=self.BACKLIGHT,
                width=WIDTH,
                height=HEIGHT,
                rotation=180,
                spi_speed_hz=60_000_000,
            )
            chip = gpiodevice.find_chip_by_platform()
            self._led = chip.request_lines(
                consumer="pi-assistant-led",
                config={
                    self.LED: gpiod.LineSettings(direction=Direction.OUTPUT, active_low=True, output_value=self._off)
                },
            )
            self._buttons = chip.request_lines(
                consumer="pi-assistant-buttons",
                config={
                    self.BUTTONS: gpiod.LineSettings(
                        direction=Direction.INPUT,
                        bias=Bias.PULL_UP,
                        edge_detection=Edge.FALLING,
                        debounce_period=timedelta(milliseconds=30),
                    )
                },
            )
        except FileNotFoundError as exc:
            raise DisplayError(
                "SPI is turned off, so the screen can't be reached. Run: sudo raspi-config nonint do_spi 0"
            ) from exc
        except PermissionError as exc:
            raise DisplayError(
                f"Not allowed to use the screen ({exc}). Run: sudo usermod -aG spi,gpio $USER, then log in again"
            ) from exc
        except OSError as exc:
            raise DisplayError(
                f"Couldn't set up the Display HAT Mini's pins ({exc}). Is another status board running?"
            ) from exc

    def show(self, image: Image.Image) -> None:
        self._lcd.display(image)

    def set_led(self, red: bool, green: bool, blue: bool) -> None:
        self._led.set_values(
            {pin: self._on if lit else self._off for pin, lit in zip(self.LED, (red, green, blue), strict=True)}
        )

    def set_backlight(self, on: bool) -> None:
        self._lcd.set_backlight(on)

    def wait(self, seconds: float) -> bool:
        if not self._buttons.wait_edge_events(timedelta(seconds=seconds)):
            return False
        return bool(self._buttons.read_edge_events())

    def close(self) -> None:
        # Don't leave a stale picture on the screen (it keeps showing one while powered).
        self.set_led(False, False, False)
        self.show(Image.new("RGB", (WIDTH, HEIGHT)))
        self.set_backlight(False)
        self._led.release()
        self._buttons.release()


class PreviewScreen:
    """Draws to a PNG file instead of a screen, to try the board on any computer."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def show(self, image: Image.Image) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        image.save(tmp, format="PNG")
        os.replace(tmp, self.path)  # so an image viewer never reads half a file

    def set_led(self, red: bool, green: bool, blue: bool) -> None:
        pass

    def set_backlight(self, on: bool) -> None:
        pass

    def wait(self, seconds: float) -> bool:
        time.sleep(seconds)
        return False

    def close(self) -> None:
        pass


def run_board(
    board: Board,
    read: Callable[[], Snapshot],
    screen: Screen,
    *,
    led: bool = True,
    once: bool = False,
    clock: Callable[[], float] = time.time,
) -> None:
    """Keep ``screen`` showing the status from ``read()``. Runs until interrupted, or for one frame with ``once``.

    Any button turns the screen off or back on. It also comes on by itself while the
    assistant is working or waiting for you, so you never miss an approval.
    """
    shown: bytes | None = None
    lit: tuple[bool, bool, bool] | None = None
    backlight: bool | None = None
    wanted = True
    offline_since = 0.0
    last_press = 0.0
    while True:
        now = clock()
        s = read()
        if s.state is State.OFFLINE:
            offline_since = offline_since or now
            if not s.started:
                s = dataclasses.replace(s, started=offline_since)
        else:
            offline_since = 0.0
        active = s.state in (State.WORKING, State.APPROVAL)

        if (on := wanted or active) != backlight:
            screen.set_backlight(on)
            backlight, shown = on, None
        if on:
            frame = board.render(s, now)
            if (data := frame.tobytes()) != shown:  # unchanged frames aren't sent again
                screen.show(frame)
                shown = data
        if (colour := board.led(s, now) if led else LED_OFF) != lit:
            screen.set_led(*colour)
            lit = colour

        if once:
            return
        if screen.wait(1 / (ACTIVE_FPS if active else IDLE_FPS)) and now - last_press > 0.3:
            wanted, last_press = not wanted, now


def demo(clock: Callable[[], float] = time.time) -> Callable[[], Snapshot]:
    """Example states, a few seconds each, for checking the screen without the assistant."""
    start = clock()
    task = "Check the weather in London and, if it's going to rain, put 'Take umbrella' in my calendar for 8am"
    examples = [
        Snapshot(State.IDLE, last_task="What's on my calendar tomorrow?", last_finished=start - 720),
        Snapshot(State.WORKING, task=task, step="Thinking", channel="Telegram", started=start),
        Snapshot(State.WORKING, task=task, step="Using fetch", tools=["fetch"], channel="Telegram", started=start),
        Snapshot(
            State.APPROVAL,
            task=task,
            tools=["fetch"],
            tool="create_calendar_event",
            channel="Telegram",
            started=start,
            deadline=start + 300,
        ),
        Snapshot(State.IDLE, last_task=task, last_error="Couldn't reach the model server", last_finished=start - 60),
        Snapshot(State.OFFLINE, started=start),
    ]
    return lambda: examples[int((clock() - start) // DEMO_SECONDS) % len(examples)]


def run_display(cfg: Config, *, preview: str | None = None, demo_mode: bool = False, once: bool = False) -> None:
    board = Board(cfg.agent.assistant_name, cfg.agent.timezone)
    read = demo() if demo_mode else StatusFile(cfg.status_path).read
    screen: Screen = PreviewScreen(preview) if preview else DisplayHATMini()
    log.info("Status board running%s", f", drawing to {preview}" if preview else "")
    try:
        run_board(board, read, screen, led=cfg.display.led, once=once)
    finally:
        screen.close()
