"""Draws the status board: a 320x240 picture of what the assistant is doing.

Only drawing happens here; display.py puts the pictures on the screen. The shield is
assets/athena.png, rendered from assets/athena.svg by scripts/render_images.py.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from pi_assistant.status import Snapshot, State

WIDTH, HEIGHT = 320, 240
ASSETS = Path(__file__).parent / "assets"

Color = tuple[int, int, int]

# Black-figure pottery: a dark ground with bronze and cream figures.
BACKGROUND = (14, 11, 8)
TEXT = (243, 232, 211)
MUTED = (158, 142, 116)
DIVIDER = (96, 72, 40)
OWL = (29, 19, 10)
DONE = (156, 180, 106)
FAILED = (240, 108, 78)
COLORS: dict[State, Color] = {
    State.IDLE: (156, 180, 106),  # olive
    State.WORKING: (92, 178, 255),  # Aegean blue
    State.APPROVAL: (255, 182, 72),  # amber
    State.OFFLINE: (128, 120, 108),  # stone
}
LABELS = {State.IDLE: "IDLE", State.WORKING: "WORKING", State.APPROVAL: "NEEDS YOU", State.OFFLINE: "OFFLINE"}

# The RGB LED as (red, green, blue) on/off: blue while working, amber (flashing) when it needs you.
LED_OFF = (False, False, False)
LEDS = {State.WORKING: (False, False, True), State.APPROVAL: (True, True, False)}

SHIELD_SIZE = 112
SHIELD_CENTER = (68, 76)
EYES = ((222, 232), (290, 232))  # the owl's eyes in athena.svg, on its 512x512 canvas,
EYE_RADIUS = 24  # and their radius
HALO = SHIELD_SIZE + 36  # the square the shield's glow and spinner are drawn in
SPINNER_STEPS = 18

LEFT, RIGHT = 140, WIDTH - 12  # the text column beside the shield
MARGIN = 12


@lru_cache(maxsize=32)
def _font(name: str, size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(ASSETS / "fonts" / f"{name}.ttf"), size)
    axes = [axis["name"] for axis in font.get_variation_axes()]
    font.set_variation_by_axes([min(max(size, 14), 32) if axis == b"Optical size" else weight for axis in axes])
    return font


def wrap(text: str, font: ImageFont.FreeTypeFont, width: float, max_lines: int) -> list[str]:
    """Word-wrap ``text`` to ``width`` pixels; the last line ends in "…" if it doesn't all fit."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        while font.getlength(word) > width:  # a word longer than a line (e.g. a URL): break it
            if line:
                lines.append(line)
                line = ""
            cut = max(1, next((i for i in range(len(word), 0, -1) if font.getlength(word[:i]) <= width), 1))
            lines.append(word[:cut])
            word = word[cut:]
        candidate = f"{line} {word}" if line else word
        if font.getlength(candidate) <= width:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and font.getlength(last + "…") > width:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,.;:") + "…"
    return lines


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return f"{days} day{'' if days == 1 else 's'} ago"


class Board:
    def __init__(self, name: str = "Athena", timezone: str = "UTC"):
        self.title = name.upper()
        try:
            self.zone: ZoneInfo | None = ZoneInfo(timezone)
        except Exception:  # unknown name: use the system's
            self.zone = None
        title_size = 26
        while title_size > 14 and _font("Cinzel", title_size, 700).getlength(self.title) > RIGHT - LEFT:
            title_size -= 1
        self.title = (wrap(self.title, _font("Cinzel", title_size, 700), RIGHT - LEFT, 1) or [""])[0]
        self.fonts = {
            "title": _font("Cinzel", title_size, 700),
            "pill": _font("Inter", 10, 650),
            "time": _font("Inter", 15, 500),
            "sub": _font("Inter", 12, 400),
            "label": _font("Inter", 9, 650),
            "body": _font("Inter", 16, 400),
            "footer": _font("Inter", 11, 500),
        }
        icon = Image.open(ASSETS / "athena.png").convert("RGBA")
        self.shield = icon.resize((SHIELD_SIZE, SHIELD_SIZE), Image.Resampling.LANCZOS)
        self.shield_offline = _faded(self.shield)
        scale = SHIELD_SIZE / 512
        self.eyes = [(x * scale, y * scale) for x, y in EYES]
        self.eye_radius = EYE_RADIUS * scale
        self.background = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
        _meander(ImageDraw.Draw(self.background), MARGIN, 142, WIDTH - MARGIN, DIVIDER)
        self._glyphs: dict[str, bool] = {}
        self._notdef = self._bitmap("\uffff")  # a noncharacter, so it comes out as the "missing" glyph
        self._layers: dict[object, Image.Image] = {}
        self._static: Image.Image = self.background
        self._static_key: object = None

    def render(self, s: Snapshot, now: float) -> Image.Image:
        # Most of the picture only changes with the status, or once a minute for the clock, so it's
        # drawn once and reused. Each frame adds the moving parts: the shield, timer and headline.
        last = ago(now - s.last_finished) if s.last_finished else ""
        key = (s.to_json(), self._clock(now, "%H:%M"), last)
        if key != self._static_key:
            self._static, self._static_key = self._draw_static(s, now), key
        image = self._static.copy()
        self._draw_shield(image, s, now)
        draw = ImageDraw.Draw(image)
        draw.text((RIGHT, 67), self._time(s, now), font=self.fonts["time"], fill=MUTED, anchor="rs")
        font, headline = self._fitted(self._headline(s, now), RIGHT - LEFT, 19, 600)
        draw.text((LEFT, 101), headline, font=font, fill=TEXT, anchor="ls")
        return image

    def led(self, s: Snapshot, now: float) -> tuple[bool, bool, bool]:
        if s.state is State.APPROVAL and int(now * 2) % 2:  # flash once a second
            return LED_OFF
        return LEDS.get(s.state, LED_OFF)

    # -- what to say ------------------------------------------------------------------------

    def _draw_static(self, s: Snapshot, now: float) -> Image.Image:
        image = self.background.copy()
        draw = ImageDraw.Draw(image)
        f = self.fonts
        draw.text((LEFT, 40), self.title, font=f["title"], fill=TEXT, anchor="ls")
        self._pill(draw, LEFT, 52, LABELS[s.state], COLORS[s.state])
        sub, sub_color = self._sub(s, now)
        if sub:
            draw.text(
                (LEFT, 121), self._fitted(sub, RIGHT - LEFT, 12, 400)[1], font=f["sub"], fill=sub_color, anchor="ls"
            )
        label, body, footer, footer_color = self._details(s, now)
        if label:
            _tracked(draw, (MARGIN, 167), label, f["label"], MUTED, 1.5)
        for i, line in enumerate(wrap(self._clean(body), f["body"], WIDTH - 2 * MARGIN, 2)):
            draw.text((MARGIN, 188 + 20 * i), line, font=f["body"], fill=TEXT, anchor="ls")
        if footer:
            draw.text((MARGIN, 229), footer, font=f["footer"], fill=footer_color, anchor="ls")
        return image

    def _time(self, s: Snapshot, now: float) -> str:
        if s.state is State.WORKING:
            return duration(now - s.started)
        if s.state is State.APPROVAL:
            return f"{duration(s.deadline - now)} left" if s.deadline else duration(now - s.started)
        return self._clock(now, "%H:%M")

    def _headline(self, s: Snapshot, now: float) -> str:
        if s.state is State.WORKING:
            step = s.step or "Thinking"
            return step + "." * (int(now * 2) % 4) if step == "Thinking" else step
        if s.state is State.APPROVAL:
            return f"Approve in {s.channel or 'the chat'}"
        return "Not running" if s.state is State.OFFLINE else "Ready"

    def _sub(self, s: Snapshot, now: float) -> tuple[str, Color]:
        if s.state is State.APPROVAL:
            return s.tool, COLORS[State.APPROVAL]
        if s.state is State.OFFLINE:
            return f"since {self._clock(s.started or now, '%H:%M')}", MUTED
        if s.state is State.IDLE:
            return self._clock(now, "%A %-d %B"), MUTED
        return "", MUTED

    def _details(self, s: Snapshot, now: float) -> tuple[str, str, str, Color]:
        """The bottom half: a label, the task (two lines) and a footer line."""
        if s.state in (State.WORKING, State.APPROVAL):
            body = s.task or f"A request from {s.channel or 'you'}"
            return "TASK", body, self._trail(s.tools), MUTED
        if s.state is State.OFFLINE:
            return "", "The assistant isn't running. See its log with:", "journalctl -u pi-assistant -f", MUTED
        if not s.last_finished:
            return "", "Nothing yet. Send a message to get started.", "", MUTED
        when = ago(now - s.last_finished)
        if s.last_error:
            return "LAST TASK", s.last_task, f"✗  {s.last_error} · {when}", FAILED
        return "LAST TASK", s.last_task, f"✓  Done · {when}", DONE

    def _trail(self, tools: list[str]) -> str:
        """The tools used so far, newest last, trimmed from the front to fit."""
        font, width = self.fonts["footer"], WIDTH - 2 * MARGIN
        trail = " › ".join(tools)
        while tools and font.getlength(trail) > width:
            tools = tools[1:]
            trail = "… › " + " › ".join(tools)
        return trail

    def _clock(self, timestamp: float, fmt: str) -> str:
        return datetime.fromtimestamp(timestamp, self.zone).strftime(fmt)

    # -- drawing --------------------------------------------------------------------------

    def _draw_shield(self, image: Image.Image, s: Snapshot, now: float) -> None:
        color = COLORS[s.state]
        pulse = 0.5 - 0.5 * math.cos(math.pi * now)  # 0 to 1 and back every two seconds
        halo_at = (SHIELD_CENTER[0] - HALO // 2, SHIELD_CENTER[1] - HALO // 2)
        shield_at = (SHIELD_CENTER[0] - SHIELD_SIZE // 2, SHIELD_CENTER[1] - SHIELD_SIZE // 2)

        glow = {State.IDLE: 2, State.WORKING: 4, State.APPROVAL: 3 + round(6 * pulse)}.get(s.state, 0)
        if glow:
            layer = _glow(color, glow)
            image.paste(layer, halo_at, layer)
        shield = self.shield_offline if s.state is State.OFFLINE else self.shield
        image.paste(shield, shield_at, shield)

        if s.state is State.OFFLINE:
            eyes = self._closed_eyes()
        elif s.state is State.WORKING:
            eyes = self._eye_glow(color, 8)
        elif s.state is State.APPROVAL:
            eyes = self._eye_glow(color, 4 + round(6 * pulse))
        elif s.last_error:
            eyes = self._eye_glow(FAILED, 5)
        else:
            eyes = None
        if eyes:
            image.paste(eyes, shield_at, eyes)
        if s.state is State.WORKING:
            spinner = _spinner(color)[int(now * SPINNER_STEPS / 1.5) % SPINNER_STEPS]  # a turn every 1.5s
            image.paste(spinner, halo_at, spinner)

    def _eye_glow(self, color: Color, level: int) -> Image.Image:
        """The owl's eyes lit up in ``color``; ``level`` 0-10 sets how brightly."""
        key = (color, level)
        if key not in self._layers:
            r = self.eye_radius
            glow = Image.new("L", (SHIELD_SIZE, SHIELD_SIZE), 0)
            iris = Image.new("L", glow.size, 0)
            for x, y in self.eyes:
                ImageDraw.Draw(glow).ellipse([x - 2 * r, y - 2 * r, x + 2 * r, y + 2 * r], fill=10 * level)
                ImageDraw.Draw(iris).ellipse([x - r, y - r, x + r, y + r], fill=18 * level)
            layer = Image.new("RGBA", glow.size, color + (0,))
            layer.putalpha(ImageChops.lighter(glow.filter(ImageFilter.GaussianBlur(r)), iris))
            self._layers[key] = layer
        return self._layers[key]

    def _closed_eyes(self) -> Image.Image:
        if "closed" not in self._layers:
            layer = Image.new("RGBA", (SHIELD_SIZE, SHIELD_SIZE), (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer)
            r = self.eye_radius + 0.5
            for x, y in self.eyes:
                draw.ellipse([x - r, y - r, x + r, y + r], fill=OWL + (255,))
                draw.line([(x - r + 1, y + 1), (x + r - 1, y + 1)], fill=(120, 96, 64, 255), width=1)
            self._layers["closed"] = layer
        return self._layers["closed"]

    def _pill(self, draw: ImageDraw.ImageDraw, x: int, y: int, label: str, color: Color) -> None:
        font = self.fonts["pill"]
        width = 24 + _tracked_length(label, font, 1.0) + 9
        draw.rounded_rectangle([x, y, x + width, y + 20], radius=10, fill=_mix(color, 0.2), outline=_mix(color, 0.55))
        draw.ellipse([x + 9, y + 7, x + 15, y + 13], fill=color)
        _tracked(draw, (x + 22, y + 14), label, font, color, 1.0)

    def _fitted(self, text: str, width: float, size: int, weight: int) -> tuple[ImageFont.FreeTypeFont, str]:
        """``text`` on one line: in a smaller font if that makes it fit, else cut short."""
        text = self._clean(text)
        while size > 14 and _font("Inter", size, weight).getlength(text) > width:
            size -= 1
        font = _font("Inter", size, weight)
        return font, (wrap(text, font, width, 1) or [""])[0]

    def _clean(self, text: str) -> str:
        """Drop characters the font can't draw (emoji, control characters) rather than show boxes."""
        kept = []
        for ch in text:
            if ch.isspace():
                kept.append(" ")
            elif unicodedata.category(ch)[0] != "C" and self._has_glyph(ch):
                kept.append(ch)
        return " ".join("".join(kept).split())

    def _has_glyph(self, ch: str) -> bool:
        if ch not in self._glyphs:
            self._glyphs[ch] = self._bitmap(ch) != self._notdef
        return self._glyphs[ch]

    def _bitmap(self, ch: str) -> bytes:
        image = Image.new("L", (48, 48))
        ImageDraw.Draw(image).text((8, 8), ch, font=self.fonts["body"], fill=255)
        return image.tobytes()


def _mix(color: Color, amount: float) -> Color:
    """``amount`` of ``color`` over the background."""
    r, g, b = (round(c * amount + bg * (1 - amount)) for c, bg in zip(color, BACKGROUND, strict=True))
    return r, g, b


def _tracked_length(text: str, font: ImageFont.FreeTypeFont, tracking: float) -> float:
    return sum(font.getlength(ch) for ch in text) + tracking * (len(text) - 1)


def _tracked(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font, fill: Color, tracking: float) -> None:
    """Letter-spaced text (Pillow has no tracking option)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor="ls")
        x += font.getlength(ch) + tracking


def _meander(draw: ImageDraw.ImageDraw, left: int, top: int, right: int, color: Color) -> None:
    """A Greek key border, 8px tall."""
    unit, bottom = 12, top + 8
    units = (right - left) // unit
    start = left + (right - left - units * unit) // 2
    draw.line([(start, bottom), (start + units * unit, bottom)], fill=color)
    for x in range(start, start + units * unit, unit):
        spiral = [
            (x, bottom),
            (x, top),
            (x + 9, top),
            (x + 9, top + 6),
            (x + 3, top + 6),
            (x + 3, top + 3),
            (x + 6, top + 3),
        ]
        draw.line(spiral, fill=color)


def _faded(icon: Image.Image) -> Image.Image:
    """The shield in stone grey, for when the assistant isn't running."""
    rgb = ImageEnhance.Color(icon.convert("RGB")).enhance(0.1)
    rgb = ImageEnhance.Brightness(rgb).enhance(0.55)
    rgb.putalpha(icon.getchannel("A"))
    return rgb


@lru_cache(maxsize=64)
def _glow(color: Color, level: int) -> Image.Image:
    """A soft halo behind the shield; ``level`` 0-10 sets its strength."""
    layer = Image.new("RGBA", (HALO, HALO), color + (0,))
    mask = Image.new("L", (HALO, HALO), 0)
    centre, r = HALO / 2, SHIELD_SIZE / 2 + 5
    ImageDraw.Draw(mask).ellipse([centre - r, centre - r, centre + r, centre + r], fill=round(25.5 * level))
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(9)))
    return layer


@lru_cache(maxsize=4)
def _spinner(color: Color) -> tuple[Image.Image, ...]:
    """Frames of a light running round the rim, drawn 4x larger and scaled down for smooth edges."""
    k = 4
    size = HALO * k
    centre, r = size / 2, (SHIELD_SIZE / 2 + 6) * k
    box = [centre - r, centre - r, centre + r, centre + r]
    frames = []
    for step in range(SPINNER_STEPS):
        layer = Image.new("RGBA", (size, size), color + (0,))
        draw = ImageDraw.Draw(layer)
        head = step * 360 / SPINNER_STEPS - 90
        for i in range(12):  # a tail that fades out behind the head
            alpha = round(255 * (1 - i / 12) ** 1.6)
            draw.arc(box, head - (i + 1) * 9, head - i * 9 + 0.5, fill=color + (alpha,), width=3 * k)
        frames.append(layer.resize((HALO, HALO), Image.Resampling.LANCZOS))
    return tuple(frames)
