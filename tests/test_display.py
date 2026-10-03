import itertools
import sys
import types
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pi_assistant.board import LED_OFF
from pi_assistant.cli import main
from pi_assistant.display import DisplayError, DisplayHATMini, PreviewScreen, demo, run_board
from pi_assistant.status import Snapshot, State, StatusFile, StatusTracker

REPO = Path(__file__).resolve().parents[1]
BLUE = (False, False, True)
COLOURS = {
    State.IDLE: (0, 128, 0),
    State.WORKING: (0, 0, 255),
    State.APPROVAL: (255, 165, 0),
    State.OFFLINE: (99, 99, 99),
}


class SpyBoard:
    """Draws each state as a plain colour and remembers what it was asked to draw."""

    def __init__(self):
        self.seen: list[Snapshot] = []

    def render(self, s, now):
        self.seen.append(s)
        return Image.new("RGB", (4, 4), COLOURS[s.state])

    def led(self, s, now):
        return BLUE if s.state is State.WORKING else LED_OFF


class Stop(Exception):
    pass


class FakeScreen:
    def __init__(self, presses=()):
        self.presses = list(presses)  # what each wait() returns; it raises Stop when they run out
        self.log = []

    def show(self, image):
        self.log.append(("show", image.getpixel((0, 0))))

    def set_led(self, *rgb):
        self.log.append(("led", rgb))

    def set_backlight(self, on):
        self.log.append(("backlight", on))

    def wait(self, seconds):
        self.log.append(("wait", seconds))
        if not self.presses:
            raise Stop
        return self.presses.pop(0)

    def close(self):
        self.log.append(("close",))

    def calls(self, kind):
        return [args[0] for name, *args in self.log if name == kind]


def run(states, presses=(), board=None):
    screen = FakeScreen(presses)
    reads = iter(Snapshot(state) if isinstance(state, State) else state for state in states)
    with pytest.raises(Stop):
        run_board(board or SpyBoard(), lambda: next(reads), screen, clock=itertools.count(1000).__next__)
    return screen


def test_frames_are_only_sent_when_they_change_and_the_led_follows():
    screen = run([State.IDLE, State.IDLE, State.WORKING, State.WORKING, State.IDLE], presses=[False] * 4)
    assert screen.calls("show") == [COLOURS[State.IDLE], COLOURS[State.WORKING], COLOURS[State.IDLE]]
    assert screen.calls("led") == [LED_OFF, BLUE, LED_OFF]
    assert screen.calls("wait") == [1.0, 1.0, 1 / 6, 1 / 6, 1.0]  # faster while something moves


def test_a_button_turns_the_screen_off_and_activity_wakes_it():
    screen = run(
        [State.IDLE, State.IDLE, State.WORKING, State.APPROVAL, State.IDLE], presses=[True, False, False, False]
    )
    assert screen.calls("backlight") == [True, False, True, False]
    assert screen.calls("show") == [COLOURS[State.IDLE], COLOURS[State.WORKING], COLOURS[State.APPROVAL]]


def test_offline_since_is_when_the_board_first_noticed():
    board = SpyBoard()
    run([State.OFFLINE, State.OFFLINE, State.IDLE, State.OFFLINE], presses=[False] * 3, board=board)
    assert [s.started for s in board.seen if s.state is State.OFFLINE] == [1000, 1000, 1003]


def test_the_board_reads_what_the_assistant_publishes(tmp_path):
    path = tmp_path / "status.json"
    board, screen, reader = SpyBoard(), FakeScreen(), StatusFile(path)
    agent, tracker = StatusFile(path), StatusTracker()

    def frame():
        run_board(board, reader.read, screen, once=True)
        return board.seen[-1]

    assert frame().state is State.OFFLINE
    agent.publish(tracker)
    assert frame().state is State.IDLE
    tracker.begin("What's the weather?")
    assert (frame().state, frame().task) == (State.WORKING, "What's the weather?")
    agent.close()
    assert frame().state is State.OFFLINE


def test_demo_cycles_through_examples():
    clock = SimpleNamespace(now=0.0)
    read = demo(lambda: clock.now)
    states = []
    for t in range(0, 35, 5):
        clock.now = t
        states.append(read().state)
    assert states == [State.IDLE, State.WORKING, State.WORKING, State.APPROVAL, State.IDLE, State.OFFLINE, State.IDLE]


@pytest.mark.parametrize("extra", [[], ["--demo"]])
def test_cli_draws_a_preview(tmp_path, extra):
    config = tmp_path / "config.toml"
    config.write_text((REPO / "config.example.toml").read_text())
    out = tmp_path / "board.png"
    with pytest.raises(SystemExit) as exited:
        main(["--config", str(config), "display", "--preview", str(out), "--once", *extra])
    assert exited.value.code == 0
    with Image.open(out) as image:
        assert image.size == (320, 240)


def test_preview_screen_replaces_the_file_whole(tmp_path):
    screen = PreviewScreen(tmp_path / "board.png")
    screen.show(Image.new("RGB", (320, 240), "red"))
    screen.show(Image.new("RGB", (320, 240), "blue"))
    assert Image.open(tmp_path / "board.png").getpixel((0, 0)) == (0, 0, 255)
    assert [p.name for p in tmp_path.iterdir()] == ["board.png"]


# -- the Display HAT Mini, with stand-ins for the hardware libraries -----------------------


def fake_drivers(monkeypatch, lcd_error=None):
    hardware = SimpleNamespace(lcd=None, requests=[])

    class LCD:
        def __init__(self, **kwargs):
            if lcd_error:
                raise lcd_error
            self.kwargs, self.frames, self.backlight = kwargs, [], []
            hardware.lcd = self

        def display(self, image):
            self.frames.append(image)

        def set_backlight(self, on):
            self.backlight.append(on)

    class Request:
        def __init__(self, consumer, config):
            self.consumer, self.config = consumer, config
            self.values, self.edges, self.timeout, self.released = [], [], None, False

        def set_values(self, values):
            self.values.append(values)

        def wait_edge_events(self, timeout):
            self.timeout = timeout
            return bool(self.edges)

        def read_edge_events(self):
            events, self.edges = self.edges, []
            return events

        def release(self):
            self.released = True

    class Chip:
        def request_lines(self, consumer, config):
            hardware.requests.append(Request(consumer, config))
            return hardware.requests[-1]

    line = types.ModuleType("gpiod.line")
    line.Value = SimpleNamespace(ACTIVE="on", INACTIVE="off")
    line.Direction = SimpleNamespace(INPUT="in", OUTPUT="out")
    line.Bias = SimpleNamespace(PULL_UP="pull-up")
    line.Edge = SimpleNamespace(FALLING="falling")
    gpiod = types.ModuleType("gpiod")
    gpiod.line = line
    gpiod.LineSettings = lambda **settings: settings
    gpiodevice = types.ModuleType("gpiodevice")
    gpiodevice.find_chip_by_platform = Chip
    st7789 = types.ModuleType("st7789")
    st7789.ST7789 = LCD
    for name, module in {"gpiod": gpiod, "gpiod.line": line, "gpiodevice": gpiodevice, "st7789": st7789}.items():
        monkeypatch.setitem(sys.modules, name, module)
    return hardware


def test_display_hat_mini_uses_the_right_pins(monkeypatch):
    hardware = fake_drivers(monkeypatch)
    hat = DisplayHATMini()

    assert hardware.lcd.kwargs == {
        "port": 0,
        "cs": 1,
        "dc": 9,
        "backlight": 13,
        "width": 320,
        "height": 240,
        "rotation": 180,
        "spi_speed_hz": 60_000_000,
    }
    led, buttons = hardware.requests
    assert led.config == {(17, 27, 22): {"direction": "out", "active_low": True, "output_value": "off"}}
    button_settings = buttons.config[(5, 6, 16, 24)]
    assert (button_settings["direction"], button_settings["bias"], button_settings["edge_detection"]) == (
        "in",
        "pull-up",
        "falling",
    )

    hat.set_led(True, False, True)
    assert led.values[-1] == {17: "on", 27: "off", 22: "on"}
    assert hat.wait(0.5) is False and buttons.timeout == timedelta(seconds=0.5)
    buttons.edges = ["pressed"]
    assert hat.wait(0.5) is True

    hat.show(Image.new("RGB", (320, 240), "red"))
    hat.close()
    assert led.values[-1] == {17: "off", 27: "off", 22: "off"}
    assert hardware.lcd.frames[-1].getbbox() is None  # cleared to black, not left showing old news
    assert hardware.lcd.backlight[-1] is False
    assert led.released and buttons.released


def test_hardware_problems_explain_the_fix(monkeypatch):
    monkeypatch.setitem(sys.modules, "st7789", None)  # not installed
    with pytest.raises(DisplayError, match="install.sh --display"):
        DisplayHATMini()

    fake_drivers(monkeypatch, lcd_error=FileNotFoundError(2, "No such file or directory: '/dev/spidev0.1'"))
    with pytest.raises(DisplayError, match="raspi-config nonint do_spi 0"):
        DisplayHATMini()

    fake_drivers(monkeypatch, lcd_error=PermissionError(13, "Permission denied: '/dev/spidev0.1'"))
    with pytest.raises(DisplayError, match="usermod -aG spi,gpio"):
        DisplayHATMini()
