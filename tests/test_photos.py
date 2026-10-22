"""photos.py, and photos through the history and the agent: what the model is shown, and what's kept."""

import struct
import zlib

from conftest import FakeLLMServer, completion
from test_agent import make_agent

from athena.history import ConversationStore
from athena.photos import IN_CONTEXT, Photo, PhotoStore, can_see, label, mime_of, red_square, with_photos

JPEG = b"\xff\xd8\xff\xe0" + b"a photo of a cat" * 4
PNG = b"\x89PNG\r\n\x1a\n" + b"a diagram"


def images(content):
    """The data: URLs of the photos in a message's content."""
    return [p["image_url"]["url"] for p in content if p["type"] == "image_url"] if isinstance(content, list) else []


def text_of(content):
    return content if isinstance(content, str) else next(p["text"] for p in content if p["type"] == "text")


def test_a_photo_is_an_image_part_the_model_reads():
    photo = Photo(JPEG)
    assert photo.part()["type"] == "image_url"
    assert photo.part()["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/")
    assert with_photos("hi", []) == "hi"
    assert with_photos("What's this?", [photo]) == [{"type": "text", "text": "What's this?"}, photo.part()]
    assert Photo(JPEG).name == Photo(JPEG).name != Photo(PNG, "image/png").name
    assert Photo(PNG, "image/png").name.endswith(".png")
    assert (label(1), label(3)) == ("[Photo]", "[3 photos]")


def test_only_images_the_model_can_take_are_recognised():
    assert mime_of(JPEG) == "image/jpeg" and mime_of(PNG) == "image/png"
    assert mime_of(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp" and mime_of(b"GIF89a...") == "image/gif"
    assert mime_of(b"%PDF-1.7") is None and mime_of(b"") is None


def test_photos_are_kept_once_privately_and_never_outside_their_folder(tmp_path):
    store = PhotoStore(tmp_path / "photos")
    name = store.keep(Photo(JPEG))
    assert store.keep(Photo(JPEG)) == name and len(list((tmp_path / "photos").iterdir())) == 1
    assert (tmp_path / "photos" / name).stat().st_mode & 0o777 == 0o600
    assert store.load(name) == Photo(JPEG)
    (tmp_path / "secret.jpg").write_bytes(JPEG)
    assert store.load("../secret.jpg") is None and store.load(".hidden") is None and store.load("gone.jpg") is None
    store.clear()
    assert list((tmp_path / "photos").iterdir()) == []


def test_the_test_square_is_a_real_red_png():
    data = red_square(8).data
    assert mime_of(data) == "image/png"
    (length,) = struct.unpack(">I", data[8:12])
    width, height, depth, kind = struct.unpack(">IIBB", data[16:26])
    assert (width, height, depth, kind) == (8, 8, 8, 2)
    start = data.index(b"IDAT") + 4
    (idat_length,) = struct.unpack(">I", data[start - 8 : start - 4])
    pixels = zlib.decompress(data[start : start + idat_length])
    assert pixels == (b"\x00" + b"\xff\x00\x00" * 8) * 8  # every pixel red


async def test_doctor_can_tell_whether_the_model_sees(config):
    sees = FakeLLMServer([completion("Red.")])
    assert await can_see(sees.client(config.llm)) == (True, "Red.")
    sent = sees.requests[0]["messages"][0]["content"]
    assert images(sent)[0].startswith("data:image/png;base64,")
    blind = FakeLLMServer([completion("I can't see images.")])
    assert await can_see(blind.client(config.llm)) == (False, "I can't see images.")


# -- in the history -------------------------------------------------------------------------------------


def test_the_last_few_messages_with_photos_keep_them(tmp_path):
    history = ConversationStore(tmp_path / "a.db")
    photos = [Photo(JPEG + bytes([i])) for i in range(IN_CONTEXT + 1)]
    for i, photo in enumerate(photos):
        history.append_exchange("c", f"[Photo] photo {i}", f"answer {i}", photos=[photo])
    history.append_exchange("c", "and in words?", "words")
    loaded = history.load("c")
    users = [m["content"] for m in loaded if m["role"] == "user"]
    assert [text_of(c) for c in users] == [*(f"[Photo] photo {i}" for i in range(IN_CONTEXT + 1)), "and in words?"]
    assert users[0] == "[Photo] photo 0"  # the oldest is just its text now
    assert [len(images(c)) for c in users] == [0, *([1] * IN_CONTEXT), 0]
    assert images(users[-2]) == [photos[-1].part()["image_url"]["url"]]


def test_a_photo_thats_gone_leaves_its_text(tmp_path):
    history = ConversationStore(tmp_path / "a.db")
    history.append_exchange("c", "[Photo] look", "nice", photos=[Photo(JPEG)])
    history.photos.clear()
    assert history.load("c")[0] == {"role": "user", "content": "[Photo] look"}


def test_retry_gets_the_photos_back_and_forgetting_deletes_them(tmp_path):
    history = ConversationStore(tmp_path / "a.db")
    history.append_exchange("c", "[2 photos] which?", "the left", photos=[Photo(JPEG), Photo(PNG, "image/png")])
    taken = history.take_last("c")
    assert taken.question == "[2 photos] which?" and len(taken.photos) == 2
    assert [history.photos.load(n) for n in taken.photos] == [Photo(JPEG), Photo(PNG, "image/png")]
    history.append_exchange("c", "[Photo] again", "ok", photos=[Photo(JPEG)])
    history.clear()
    assert list((tmp_path / "photos").iterdir()) == []


# -- through the agent ----------------------------------------------------------------------------------


async def test_the_model_sees_your_photo_and_can_be_asked_about_it_again(config, memory):
    server = FakeLLMServer([completion("A cat on a mat."), completion("Ginger.")])
    agent = make_agent(config, memory, server)
    result = await agent.respond("chat1", "What's this?", photos=[Photo(JPEG)])
    assert result.text == "A cat on a mat."
    sent = server.requests[0]["messages"][-1]["content"]
    assert text_of(sent).endswith("\n\n[Photo] What's this?")
    assert images(sent) == [Photo(JPEG).part()["image_url"]["url"]]

    await agent.respond("chat1", "What colour is it?")
    history = server.requests[1]["messages"]
    assert (
        history[1]["role"] == "user"
        and images(history[1]["content"])
        and text_of(history[1]["content"]) == ("[Photo] What's this?")
    )
    assert history[-1]["content"].endswith("What colour is it?") and not images(history[-1]["content"])


async def test_a_photo_without_a_caption_and_a_retried_one_are_labelled_once(config, memory):
    server = FakeLLMServer([completion("A cat."), completion("Still a cat.")])
    agent = make_agent(config, memory, server)
    await agent.respond("chat1", "", photos=[Photo(JPEG), Photo(PNG, "image/png")])
    assert text_of(server.requests[0]["messages"][-1]["content"]).endswith("\n\n[2 photos]")
    taken = agent.history.take_last("chat1")
    photos = [agent.history.photos.load(n) for n in taken.photos]
    await agent.respond("chat1", taken.question, photos=photos)  # as /retry does
    assert text_of(server.requests[1]["messages"][-1]["content"]).endswith("\n\n[2 photos]")
    assert len(images(server.requests[1]["messages"][-1]["content"])) == 2
