"""Files in the workspace: reading, writing and sending them without anything there leading Athena outside it, PDFs
read and made in the sandbox, and files coming and going in Telegram."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_sandbox import REAL, needs_bwrap
from test_telegram import ME, PhotoBot, asking, make_bot

from athena import pdfs, workspace
from athena.agent import AgentResult
from athena.files import NOT_SEEN, SENT, Files, FilesConfig, unsent
from athena.run_code import CodeConfig, SandboxedPython
from athena.tools import ToolError, Turn, begin_turn


@pytest.fixture
def ws(tmp_path):
    folder = tmp_path / "workspace"
    folder.mkdir()
    return folder


@pytest.fixture
def secret(tmp_path):
    """A file outside the workspace, as Athena's .env is."""
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=secret")
    return path


# -- the workspace: nothing there leads outside it ------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "../.env", "a/../../.env", "", "  ", "x\x00y", "/workspace/../.env", "/workspace", "/workspaces/a"],
)
def test_only_paths_within_the_workspace_are_taken(name):
    with pytest.raises(ToolError):
        workspace.parts(name)


def test_paths_are_taken_as_given_within_it():
    assert workspace.parts("./reports/march.pdf") == ("reports", "march.pdf")
    assert workspace.parts("/workspace/reports/march.pdf") == ("reports", "march.pdf")  # where code sees it
    assert workspace.parts("inbox/a b.md") == ("inbox", "a b.md")
    assert workspace.parts("a/./b") == ("a", "b")


def test_a_link_to_outside_is_never_followed(ws, secret):
    (ws / "env").symlink_to(secret)
    (ws / "outside").symlink_to(secret.parent, target_is_directory=True)
    for name in ("env", "outside/.env"):
        with pytest.raises(ToolError):
            workspace.read(ws, name, 1000)
    for name in ("env", "outside/new.md"):
        with pytest.raises(ToolError):
            workspace.write(ws, name, b"overwritten")
    assert secret.read_text() == "TELEGRAM_BOT_TOKEN=secret" and not (secret.parent / "new.md").exists()
    assert workspace.kind(ws, "env") == ("other", 0) and workspace.listing(ws) == []  # links aren't listed
    workspace.remove(ws, "env")
    workspace.remove(ws, "outside")
    assert secret.exists() and not (ws / "env").is_symlink() and not (ws / "outside").exists()


def test_a_named_pipe_is_refused_rather_than_waited_on(ws):
    os.mkfifo(ws / "pipe")
    with pytest.raises(ToolError, match="isn't an ordinary file"):
        workspace.read(ws, "pipe", 1000)  # returns at once, rather than waiting for a writer that never comes
    with pytest.raises(ToolError):
        workspace.write(ws, "pipe", b"x")


def test_files_are_written_privately_with_their_folders_and_read_within_a_limit(ws):
    assert workspace.write(ws, "reports/2026/march.md", b"# March") == "reports/2026/march.md"
    assert (ws / "reports/2026/march.md").stat().st_mode & 0o777 == 0o600
    assert (ws / "reports").stat().st_mode & 0o777 == 0o700
    assert workspace.read(ws, "reports/2026/march.md", 1000) == b"# March"
    with pytest.raises(ToolError, match="more than the 0 MB"):
        workspace.read(ws, "reports/2026/march.md", 3)
    assert workspace.listing(ws, "reports") == [("2026", None)] and workspace.listing(ws, "reports/2026") == [
        ("march.md", 7)
    ]


def test_files_kept_from_outside_get_safe_names_and_dont_replace_any(ws):
    assert workspace.safe_name("../../etc/passwd") == "passwd"
    assert workspace.safe_name("..hidden") == "hidden" and workspace.safe_name("") == "file"
    assert workspace.safe_name('a<b>:c"d|e?f*.pdf') == "a_b__c_d_e_f_.pdf"
    assert workspace.save_new(ws, "inbox", "bill.pdf", b"1") == "inbox/bill.pdf"
    assert workspace.save_new(ws, "inbox", "bill.pdf", b"2") == "inbox/bill (2).pdf"
    assert workspace.save_new(ws, "inbox", "../bill", b"3") == "inbox/bill"
    assert (ws / "inbox/bill.pdf").read_bytes() == b"1"


def test_an_inbox_made_into_a_link_isnt_saved_through(ws, secret):
    (ws / "inbox").symlink_to(secret.parent, target_is_directory=True)
    with pytest.raises(ToolError):
        workspace.save_new(ws, "inbox", "planted.md", b"x")
    assert not (secret.parent / "planted.md").exists()


# -- the tools -------------------------------------------------------------------------------------------


def files(ws, python=None, **settings) -> Files:
    return Files(FilesConfig(**settings), ws, python)


async def test_reading_and_writing_text_a_part_at_a_time(ws):
    f = files(ws)
    assert await f.write("notes/plan.md", "# Plan\n" + "x" * 6000, "") == f"Wrote notes/plan.md (5.9 KB). {NOT_SEEN}"
    first = await f.read("notes/plan.md", 0)
    assert first.startswith("[notes/plan.md: characters 0-5000 of 6007; to read on, start=5000]\n# Plan\n")
    rest = await f.read("notes/plan.md", 5000)
    assert rest.startswith("[notes/plan.md: characters 5000-6007 of 6007]\n")
    assert await f.read("", 0) == "In your folder:\nnotes/"
    assert await f.read("notes", 0) == "In notes:\nplan.md (5.9 KB)"
    (ws / "data.bin").write_bytes(b"\x00\x01binary")
    assert await f.read("data.bin", 0) == "[data.bin: 8 bytes, not text: run_python can look at it, if it's data]"
    with pytest.raises(ToolError, match="There's no file nope.md"):
        await f.read("nope.md", 0)


async def test_nothing_more_is_written_once_the_folder_is_full(ws):
    (ws / "big").write_bytes(b"x" * 2 * 1024 * 1024)
    f = Files(FilesConfig(), ws, None, workspace_mb=1)
    with pytest.raises(ToolError, match="holds more than 1 MB"):
        await f.write("more.md", "x", "")


async def test_without_a_sandbox_pdfs_cant_be_read_or_made_but_other_files_can(ws):
    f = files(ws)
    (ws / "a.pdf").write_bytes(b"%PDF-1.4")
    for job in (f.read("a.pdf", 0), f.write("b.pdf", "# B", "")):
        with pytest.raises(ToolError, match="needs a sandbox"):
            await job


def test_sending_a_file_notes_it_for_the_answer(ws, secret):
    f = files(ws, max_send_mb=1)
    (ws / "report.pdf").write_bytes(b"%PDF")
    (ws / "big.csv").write_bytes(b"x" * (1024 * 1024 + 1))
    (ws / "env").symlink_to(secret)
    turn = Turn()
    end = begin_turn(turn)
    try:
        assert f.send("report.pdf", "March") == "report.pdf will be sent to the user with your answer."
        for name, why in (("nope.pdf", "There's no file"), ("env", "There's no file"), ("big.csv", "over 1 MB")):
            with pytest.raises(ToolError, match=why):
                f.send(name, "")
    finally:
        end()
    assert [json.loads(n) for n in turn.notes[SENT]] == [{"path": "report.pdf", "caption": "March"}]


# -- PDFs, as pdfs.py makes and reads them (here, outside the sandbox) -----------------------------------


def test_markdown_becomes_a_pdf_that_reads_back(tmp_path):
    md = "# Budget\n\nSpent **£1,240** — less than “before”.\n\n| Item | Cost |\n|---|---:|\n| Rent | £900 |\n\n- one\n"
    made = pdfs.make_pdf(md, str(tmp_path / "b.pdf"), "Budget")
    assert made["pages"] == 1
    read = pdfs.read_pdf(str(tmp_path / "b.pdf"), 0, 1000)
    assert read["pages"] == 1 and not read["empty"]
    assert "Budget" in read["text"] and "£1,240" in read["text"] and "Rent" in read["text"]
    assert read["text"].startswith("[Page 1]\n")
    assert pdfs.read_pdf(str(tmp_path / "b.pdf"), 5, 3)["text"] == read["text"][5:8]


def test_raw_html_and_pictures_in_the_markdown_are_only_text(tmp_path):
    md = '<img src="file:///etc/passwd"><script>x()</script>\n\n![x](http://example.com/a.png)'
    pdfs.make_pdf(md, str(tmp_path / "h.pdf"))
    text = pdfs.read_pdf(str(tmp_path / "h.pdf"), 0, 1000)["text"]
    assert "<script>" in text  # shown, not acted on


def test_a_pdf_with_no_text_or_a_password_says_so(tmp_path):
    from fpdf import FPDF
    from pypdf import PdfReader, PdfWriter

    scan = FPDF()
    scan.add_page()
    scan.rect(20, 20, 50, 50, style="F")  # a picture, and no text
    scan.output(str(tmp_path / "scan.pdf"))
    assert pdfs.read_pdf(str(tmp_path / "scan.pdf"), 0, 100)["empty"]

    pdfs.make_pdf("# Secret", str(tmp_path / "plain.pdf"))
    writer = PdfWriter(clone_from=PdfReader(str(tmp_path / "plain.pdf")))
    writer.encrypt("hunter2")
    writer.write(str(tmp_path / "locked.pdf"))
    assert pdfs.read_pdf(str(tmp_path / "locked.pdf"), 0, 100) == {
        "problem": "It's protected with a password, so it can't be read."
    }


# -- PDFs in the sandbox, for real -----------------------------------------------------------------------


def sandboxed(ws) -> Files:
    return files(ws, SandboxedPython(CodeConfig(), REAL, ws))


@needs_bwrap
async def test_a_pdf_is_made_and_read_in_the_sandbox(ws):
    f = sandboxed(ws)
    said = await f.write("out/summary.pdf", "# Summary\n\nThe council tax band is D.\n\n- one\n- two", "Summary")
    assert said.startswith("Made out/summary.pdf (1 page, ") and (ws / "out/summary.pdf").stat().st_size > 500
    read = await f.read("out/summary.pdf", 0)
    assert read.startswith("[out/summary.pdf, a PDF of 1 pages: characters 0-") and "council tax band is D" in read


@needs_bwrap
async def test_a_broken_pdf_is_an_error_not_a_crash(ws):
    (ws / "broken.pdf").write_bytes(b"%PDF-1.7\n" + os.urandom(2000))
    with pytest.raises(ToolError, match="couldn't be done"):
        await sandboxed(ws).read("broken.pdf", 0)


@needs_bwrap
async def test_a_pdf_that_takes_too_long_is_stopped(ws):
    hurried = CodeConfig.model_construct(**{**CodeConfig().model_dump(), "timeout_seconds": 0.01})
    f = Files(FilesConfig(), ws, SandboxedPython(hurried, REAL, ws))
    with pytest.raises(ToolError):
        await f.write("slow.pdf", "# x\n" * 10_000, "")


@needs_bwrap
async def test_doctor_checks_pdfs(config):
    reported = []
    await Files.check(config, lambda mark, msg: reported.append(msg))
    assert reported and reported[0].startswith("PDFs can be made and read"), reported
    assert not (workspace.workspace_of(config) / ".athena-check").exists()


# -- Telegram: files you send, and files sent to you -----------------------------------------------------


class FileBot(PhotoBot):
    def __init__(self, files):
        super().__init__(files)
        self.documents = []

    async def send_document(self, chat_id, document, caption=None):
        self.documents.append((chat_id, document.filename, document.input_file_content, caption))


def file_update(name, file_id="f1", size=10, caption=None, mime="application/pdf"):
    document = SimpleNamespace(file_name=name, file_id=file_id, file_size=size, mime_type=mime)
    message = SimpleNamespace(document=document, caption=caption, text=None)
    return SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=ME), message=message)


async def test_a_file_you_send_goes_into_the_inbox_and_the_model_is_told_where(config):
    asked, respond = asking("It's your gas bill.")
    bot = make_bot(config, respond)
    tg = FileBot({"f1": b"%PDF-1.7 bill"})
    await bot.on_file(file_update("../Gas bill.pdf", caption=" What's due? "), SimpleNamespace(bot=tg))
    assert asked == [("[File: inbox/Gas bill.pdf] What's due?", None)]
    assert (workspace.workspace_of(config) / "inbox/Gas bill.pdf").read_bytes() == b"%PDF-1.7 bill"
    assert tg.sent[-1].text.startswith("It's your gas bill.")


@pytest.mark.parametrize(
    ("name", "size", "said"),
    [
        ("song.mp3", 10, "I can read PDFs and text files"),
        ("huge.pdf", 21 * 1024 * 1024, "That's over 20 MB"),
    ],
)
async def test_files_athena_cant_take_are_turned_down(config, name, size, said):
    asked, respond = asking()
    bot = make_bot(config, respond)
    tg = FileBot({"f1": b"x"})
    await bot.on_file(file_update(name, size=size), SimpleNamespace(bot=tg))
    assert asked == [] and tg.fetched == [] and tg.sent[-1].text.startswith(said)


async def test_files_switched_off_are_turned_down(config):
    config.workspace.enabled = False
    asked, respond = asking()
    bot = make_bot(config, respond)
    tg = FileBot({"f1": b"x"})
    await bot.on_file(file_update("a.md"), SimpleNamespace(bot=tg))
    assert asked == [] and "Files are switched off" in tg.sent[-1].text


async def test_files_the_answer_sends_come_after_it_and_a_link_isnt_sent(config, tmp_path):
    ws = workspace.workspace_of(config)
    workspace.write(ws, "out/report.pdf", b"%PDF report")
    (ws / "out/env").symlink_to(tmp_path / ".env")
    (tmp_path / ".env").write_text("secret")
    sent = [json.dumps({"path": "out/report.pdf", "caption": "March"}), json.dumps({"path": "out/env", "caption": ""})]

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        return AgentResult(text="Here's the report.", saved=True, notes={SENT: sent})

    bot = make_bot(config, respond)
    tg = FileBot({})
    message = SimpleNamespace(text="report?", reply_to_message=None)
    await bot.on_text(SimpleNamespace(effective_chat=SimpleNamespace(id=1), message=message), SimpleNamespace(bot=tg))
    assert [m.text for m in tg.sent][0].startswith("Here's the report.")
    assert tg.documents == [(1, "report.pdf", b"%PDF report", "March")]
    assert tg.sent[-1].text.startswith("I couldn't send you env:")  # the link, which isn't followed


async def test_a_dag_task_sends_the_files_it_made(config, memory):
    """A task that writes a file and sends it, in a DAG run."""
    from conftest import FakeLLMServer, completion
    from test_dags import FakeChannel

    from athena.agent import Agent
    from athena.config import TasksConfig
    from athena.dags.service import Dags
    from athena.dags.store import AgentStore
    from athena.history import ConversationStore
    from athena.tools import ToolRegistry

    tools = ToolRegistry()
    for tool in Files(FilesConfig(), workspace.workspace_of(config), None).tools():
        tools.add(tool)
    server = FakeLLMServer(
        [
            completion(None, [("write_file", {"path": "summary.md", "content": "# Summary"})]),
            completion(None, [("send_file", {"path": "summary.md"})]),
            completion("Here's the summary."),
        ]
        + [completion("NO CHANGES")] * 6
    )
    agent = Agent(config.agent, server.client(config.llm), tools, ConversationStore(config.db_path), memory)
    channel = FakeChannel()
    dags = Dags(TasksConfig(), AgentStore(config.db_path), agent, timezone="Europe/London",
                channel=lambda: channel, state=config.db_path.with_name("dags.db"))  # fmt: skip
    dags.save(1, "with Dag('files', prompt='p'):\n    AgenticTask('summary_file', prompt='Write and send it.')\n")
    await dags.start()
    try:
        run = await dags.run_now(1)
        await dags.engine.wait(run.run_id, timeout=10, stop_on_pause=False)
    finally:
        await dags.stop()
    assert channel.files == [json.dumps({"path": "summary.md", "caption": ""})]


def test_the_files_builtin_is_there_and_kept_apart_from_an_old_files_server():
    assert Files.entry.id == "workspace" and Files.section == "workspace"


def test_its_pdf_module_needs_nothing_of_athenas():
    source = Path(pdfs.__file__).read_text()
    assert "athena" not in source  # its source is all that's in the sandbox


# -- a file the answer names is sent, even if the model only said where it is ------------------------------


def made(*names, sent=()):
    return {workspace.MADE: list(names), SENT: [json.dumps({"path": n, "caption": "c"}) for n in sent]}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I've saved it as reports/march.pdf.", ["reports/march.pdf"]),
        ("It's in your folder, as `march.pdf`", ["reports/march.pdf"]),
        ("At /workspace/reports/march.pdf", ["reports/march.pdf"]),
        ("Here's the report.", []),
        ("I've saved march.pdfx and old-march.pdf", []),
    ],
)
def test_a_file_made_while_answering_that_the_answer_names_is_sent(text, expected):
    assert [json.loads(n)["path"] for n in unsent(made("reports/march.pdf"), text)] == expected


def test_a_file_already_sent_isnt_sent_twice_and_a_name_with_no_extension_isnt_any_word():
    assert unsent(made("a.md", "b.md", "a.md", sent=["a.md"]), "a.md and b.md") == [
        json.dumps({"path": "b.md", "caption": ""})
    ]
    assert unsent(made("out/report"), "Here's the report.") == []
    assert unsent(made("out/report"), "It's out/report.") != []


def agent_with_files(config, memory, replies):
    """An agent with the Files tools, and the model's replies, as completion()'s arguments."""
    from conftest import FakeLLMServer, completion

    from athena.agent import Agent
    from athena.history import ConversationStore
    from athena.tools import ToolRegistry

    tools = ToolRegistry()
    for tool in Files(FilesConfig(), workspace.workspace_of(config), None).tools():
        tools.add(tool)
    server = FakeLLMServer([completion(*reply) for reply in replies])
    return Agent(config.agent, server.client(config.llm), tools, ConversationStore(config.db_path), memory), server


WRITES = (None, [("write_file", {"path": "out/notes.md", "content": "# Notes"})])


async def test_a_file_the_answer_only_says_where_it_is_reaches_telegram(config, memory):
    agent, server = agent_with_files(config, memory, [WRITES, ("I've written it: it's out/notes.md in my folder.",)])
    bot = make_bot(config, agent.respond)
    tg = FileBot({})
    message = SimpleNamespace(text="Notes, please", reply_to_message=None)
    await bot.on_text(SimpleNamespace(effective_chat=SimpleNamespace(id=1), message=message), SimpleNamespace(bot=tg))
    assert tg.sent[0].text.startswith("I've written it")
    assert tg.documents == [(1, "notes.md", b"# Notes", None)]
    assert server.requests[1]["messages"][-1]["content"] == f"Wrote out/notes.md (7 bytes). {NOT_SEEN}"
    send_file = next(t for t in server.requests[0]["tools"] if t["function"]["name"] == "send_file")
    assert "rather than saying where it is" in send_file["function"]["description"]


@pytest.mark.parametrize(
    ("replies", "sent"),
    [
        (  # sent with send_file: once, as it was sent
            [WRITES, (None, [("send_file", {"path": "out/notes.md", "caption": "Notes"})]), ("Here: out/notes.md.",)],
            [{"path": "out/notes.md", "caption": "Notes"}],
        ),
        ([WRITES, ("Done.",)], []),  # not named: it may be Athena's own
    ],
)
async def test_a_file_is_sent_once_and_only_if_the_answer_names_it(config, memory, replies, sent):
    agent, _ = agent_with_files(config, memory, replies)
    result = await agent.respond(agent.history.master.chat_id, "Notes, please", confirm=None)
    assert [json.loads(n) for n in result.notes.get(SENT, [])] == sent


async def test_working_by_itself_a_file_it_names_isnt_sent(config, memory):
    from athena.agent import Background

    async def step(status, call):
        return await call()

    agent, _ = agent_with_files(config, memory, [WRITES, ("Saved out/notes.md.",)])
    background = Background(label="Testing", step=step, source="a test")
    result = await agent.follow_up([{"role": "system", "content": "Hi"}], "Write notes", background)
    assert result.notes[workspace.MADE] == ["out/notes.md"] and SENT not in result.notes
