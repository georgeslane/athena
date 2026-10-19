"""Photos you send Athena: kept in its data folder, and shown to the model with your message.

The model reads them as OpenAI-style image parts (`image_url`, as a data: URL), which oMLX and other servers
for vision models take. Only the model server sees them: no tool is given a photo. The conversation history
notes each message's photos by name, and the last few stay in the model's context, so you can ask about
them again (history.py). Each is kept once, named by what's in it, and deleted with everything else when you
tell Athena to forget everything.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pi_assistant.llm import LLMClient

log = logging.getLogger(__name__)

TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
MAX_BYTES = 8_000_000  # an image sent as a file, which Telegram doesn't make smaller as it does photos
MAX_PER_MESSAGE = 5  # in an album; the rest are left out
IN_CONTEXT = 3  # messages with photos whose photos the model still sees; older ones are just "[Photo]"


@dataclass(frozen=True)
class Photo:
    data: bytes
    mime: str = "image/jpeg"

    @property
    def name(self) -> str:
        """Its file name: what's in it, so the same photo is kept once."""
        return f"{hashlib.sha256(self.data).hexdigest()[:32]}.{TYPES.get(self.mime, 'jpg')}"

    def part(self) -> dict[str, Any]:
        """The photo as part of a message to the model."""
        encoded = base64.b64encode(self.data).decode()
        return {"type": "image_url", "image_url": {"url": f"data:{self.mime};base64,{encoded}"}}


def label(count: int) -> str:
    """How a message's photos are noted in its text, for the history, memory and skills."""
    return "[Photo]" if count == 1 else f"[{count} photos]"


def with_photos(text: str, photos: list[Photo]) -> str | list[dict[str, Any]]:
    """A message's content: its text, and its photos if it has any."""
    if not photos:
        return text
    return [{"type": "text", "text": text}, *(photo.part() for photo in photos)]


def mime_of(data: bytes) -> str | None:
    """An image's type, from its first bytes. None if it isn't one the model can take."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


class PhotoStore:
    """Photos, as files in a folder of Athena's data, by name."""

    def __init__(self, folder: Path):
        self.folder = folder

    def keep(self, photo: Photo) -> str:
        path = self.folder / photo.name
        if not path.exists():
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(photo.data)
            tmp.chmod(0o600)
            tmp.replace(path)
        return photo.name

    def load(self, name: str) -> Photo | None:
        if "/" in name or name.startswith("."):
            return None
        try:
            data = (self.folder / name).read_bytes()
        except OSError:
            return None
        return Photo(data, mime_of(data) or "image/jpeg")

    def clear(self) -> None:
        """Delete every photo."""
        if self.folder.is_dir():
            for path in self.folder.iterdir():
                if path.is_file():
                    path.unlink()


# -- whether the model can see them, for doctor -----------------------------------------------------


def red_square(size: int = 64) -> Photo:
    """A plain red PNG, made here so doctor needs no image library."""
    row = b"\x00" + b"\xff\x00\x00" * size  # filter byte, then RGB pixels

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b"")
    )
    return Photo(png, "image/png")


async def can_see(llm: LLMClient) -> tuple[bool, str]:
    """Whether the model sees photos: it's shown a red square and asked its colour. Returns (yes, what it said)."""
    question = "What colour is this image? Answer with one word."
    reply = await llm.chat(
        [{"role": "user", "content": with_photos(question, [red_square()])}], max_tokens=300, thinking="off"
    )
    said = (reply.content or "").strip()
    return "red" in said.lower(), said
