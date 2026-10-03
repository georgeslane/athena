from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from pi_assistant.board import (
    ASSETS,
    COLORS,
    EYE_RADIUS,
    EYES,
    LED_OFF,
    LEFT,
    RIGHT,
    Board,
    ago,
    duration,
    wrap,
)
from pi_assistant.status import Snapshot, State

NOW = datetime(2026, 10, 3, 14, 30, 10, tzinfo=ZoneInfo("Europe/London")).timestamp()
TASK = "Check the weather in London and, if it's going to rain, put 'Take umbrella' in my calendar for 8am"

EXAMPLES = {
    "idle": Snapshot(State.IDLE, last_task="What's on tomorrow?", last_finished=NOW - 720),
    "idle, nothing yet": Snapshot(State.IDLE),
    "failed": Snapshot(
        State.IDLE, last_task="Call the café", last_error="Couldn't reach the model server", last_finished=NOW
    ),
    "working": Snapshot(State.WORKING, task=TASK, step="Using fetch", tools=["fetch"], started=NOW - 42),
    "approval": Snapshot(
        State.APPROVAL,
        task=TASK,
        tool="create_calendar_event",
        channel="Telegram",
        started=NOW - 60,
        deadline=NOW + 251,
    ),
    "hidden task": Snapshot(State.WORKING, channel="Telegram", started=NOW),
    "offline": Snapshot(State.OFFLINE, started=NOW - 3600),
}


@pytest.fixture(scope="module")
def board():
    return Board("Athena", "Europe/London")


@pytest.mark.parametrize("name", EXAMPLES)
def test_every_state_draws_a_full_frame(board, name):
    s = EXAMPLES[name]
    frame = board.render(s, NOW)
    assert (frame.size, frame.mode) == ((320, 240), "RGB")
    assert frame.getpixel((LEFT + 12, 62)) == COLORS[s.state]  # the dot in the status pill
    assert frame.crop((12, 170, 308, 236)).getbbox() is not None or name == "idle, nothing yet"


def test_text_fits_its_space(board):
    font = board.fonts["body"]
    lines = wrap("see https://example.com/" + "a" * 120 + " for " + "more words " * 30, font, 296, 2)
    assert len(lines) == 2
    assert all(font.getlength(line) <= 296 for line in lines)
    assert lines[-1].endswith("…")
    assert wrap("", font, 296, 2) == []
    assert wrap("fits", font, 296, 2) == ["fits"]


def test_long_headlines_shrink_before_they_are_cut(board):
    font, text = board._fitted("Approve in Telegram", RIGHT - LEFT, 19, 600)
    assert text == "Approve in Telegram" and font.size < 19
    font, text = board._fitted("Using " + "very_long_tool_name_" * 4, RIGHT - LEFT, 19, 600)
    assert text.endswith("…") and font.getlength(text) <= RIGHT - LEFT


def test_characters_the_font_lacks_are_dropped(board):
    assert board._clean("Rain ☔ tomorrow 🙂\x07 in Zürich, café Ωmega") == "Rain tomorrow in Zürich, café Ωmega"


def test_long_names_shrink_to_fit():
    board = Board("Pallas Athena Parthenos")
    assert board.fonts["title"].getlength(board.title) <= RIGHT - LEFT


def test_working_animates_and_idle_stays_still(board):
    working = EXAMPLES["working"]
    assert board.render(working, NOW).tobytes() != board.render(working, NOW + 0.25).tobytes()
    idle = EXAMPLES["idle"]
    assert board.render(idle, NOW).tobytes() == board.render(idle, NOW + 0.5).tobytes()


def test_led_is_blue_while_working_and_flashes_amber_for_approval(board):
    assert board.led(EXAMPLES["working"], NOW) == (False, False, True)
    assert board.led(EXAMPLES["approval"], 100.0) == (True, True, False)
    assert board.led(EXAMPLES["approval"], 100.5) == LED_OFF
    assert board.led(EXAMPLES["idle"], NOW) == LED_OFF
    assert board.led(EXAMPLES["offline"], NOW) == LED_OFF


def test_icon_has_the_owl_eyes_where_the_board_lights_them():
    icon = Image.open(ASSETS / "athena.png").convert("RGBA")
    scale = icon.width / 512
    assert icon.getpixel((0, 0))[3] == 0 and icon.getpixel((icon.width // 2, icon.height // 2))[3] == 255
    for x, y in EYES:  # a point on each gold iris, between the pupil and the edge
        r, g, b, _ = icon.getpixel((round((x + EYE_RADIUS * 0.7) * scale), round(y * scale)))
        assert r > 200 and g > 130 and b < 110, (r, g, b)


def test_time_formats():
    assert duration(42) == "0:42"
    assert duration(3725) == "1:02:05"
    assert duration(-5) == "0:00"
    assert ago(30) == "just now"
    assert ago(720) == "12 min ago"
    assert ago(7300) == "2 h ago"
    assert ago(86400 * 3) == "3 days ago"
