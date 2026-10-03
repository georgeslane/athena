"""Render the images that are made from other files:

- src/pi_assistant/assets/athena.png, the shield the status board draws, from athena.svg
- docs/status-board.png, the board in each state, for the README

    uv run --with resvg-py scripts/render_images.py

Re-run it after editing the SVG or the board's design. resvg is only needed here,
which keeps it off the Pi.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "pi_assistant" / "assets"
ICON_SIZE = 256  # the board scales it down from here


def render_icon() -> Path:
    png = resvg_py.svg_to_bytes(svg_path=str(ASSETS / "athena.svg"), width=ICON_SIZE, height=ICON_SIZE)
    (ASSETS / "athena.png").write_bytes(png)
    return ASSETS / "athena.png"


def render_board() -> Path:
    from pi_assistant.board import HEIGHT, WIDTH, Board
    from pi_assistant.display import DEMO_SECONDS, demo

    # The --demo examples, at a fixed time so the picture only changes when the design does.
    start = datetime(2026, 10, 3, 14, 30, tzinfo=ZoneInfo("Europe/London")).timestamp()
    clock = [start]
    read = demo(lambda: clock[0])
    board = Board("Athena", "Europe/London")
    frames = []
    for i in range(6):
        clock[0] = start + i * DEMO_SECONDS + 2.6
        frames.append(board.render(read(), clock[0]))

    gap, columns = 12, 3
    sheet = Image.new("RGB", (columns * (WIDTH + gap) + gap, 2 * (HEIGHT + gap) + gap), (40, 34, 28))
    for i, frame in enumerate(frames):
        sheet.paste(frame, (gap + (i % columns) * (WIDTH + gap), gap + (i // columns) * (HEIGHT + gap)))
    path = ROOT / "docs" / "status-board.png"
    path.parent.mkdir(exist_ok=True)
    sheet.save(path, optimize=True)
    return path


if __name__ == "__main__":
    print("Wrote", render_icon())
    print("Wrote", render_board())
