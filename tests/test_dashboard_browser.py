"""The dashboard in a real browser (headless Chromium, through Playwright), served by Athena's own services as in
test_dashboard.py: every tab loads without errors and fits a phone, and agents, their DAGs, runs and files work.

CI installs the browser (`playwright install --only-shell chromium`), and there these tests must run. Elsewhere
they're skipped without one, or ATHENA_TEST_BROWSER can name a Chromium to use.
"""

import asyncio
import json
import os

import pytest
from conftest import completion
from test_dashboard import PASSWORD, athena  # noqa: F401 - athena is a fixture

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - it's a dev dependency
    async_playwright = None

TABS = ["status", "agents", "memory", "skills", "tools"]
LOOKS = [(360, 800, "light"), (1280, 900, "dark")]  # a small phone, and a desktop in dark mode


@pytest.fixture
async def browser():
    if async_playwright is None:
        pytest.skip("needs Playwright")
    async with async_playwright() as p:
        try:
            chromium = await p.chromium.launch(executable_path=os.environ.get("ATHENA_TEST_BROWSER") or None)
        except PlaywrightError as exc:
            if os.environ.get("CI"):
                raise  # CI installs it: not finding it there is a failure, not a reason to skip
            pytest.skip(f"no browser for Playwright: {str(exc).splitlines()[0]}")
        yield chromium
        await chromium.close()


async def signed_in(browser, services, width=1280, height=900, scheme="light"):
    """A page signed in to the dashboard, and the errors it hits from then on."""
    context = await browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = await context.new_page()
    base = f"http://127.0.0.1:{services.dashboard.port}"
    await page.goto(f"{base}/")
    await page.fill("#password", PASSWORD)
    await page.click("#sign-in-form button[type=submit]")
    await page.wait_for_selector("#app:not([hidden])")
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"page error: {exc}"))
    page.on("console", lambda msg: msg.type == "error" and not expected(msg.text) and errors.append(msg.text))
    return page, base, errors


async def at(page, address):
    """Wait for the page to be at an address: as wait_for_function would, if the page's Content-Security-Policy let
    it evaluate a string, which it doesn't, once it has to wait."""
    for _ in range(200):
        if await page.evaluate("location.hash") == address:
            return
        await page.wait_for_timeout(25)
    raise AssertionError(f"The page is at {await page.evaluate('location.hash')}, not {address}")


def expected(console_error: str) -> bool:
    """The browser logs every 4xx as an error. A 409 is the API saying something's switched off, such as the
    board without Projects for Claude, which the page shows as such."""
    return "Failed to load resource" in console_error and "409 (Conflict)" in console_error


JOBS = """from athena import DAG, Task, TaskGroup, trigger

with DAG(schedule="0 9 * * 1-5", triggers=["job-emails"]):
    with TaskGroup("each_job", for_each=trigger) as each_job:
        fits = Task(prompt="Does this job fit my CV? {item}", return_type=bool)
        apply = Task(prompt="Apply to {item}", run_if=fits, alert=True)
        fits >> apply
    report = Task(prompt="Say what was applied for.")
    each_job >> report
"""


def seed(services):
    """The master agent, with a DAG that finds job emails; an agent that applies for each, with guidance, what its
    last run kept, a DAG that's run for two jobs, with its steps, and two proposals; and another that's paused."""
    dags, history = services.dags, services.history
    master = history.master.id
    dags.save(
        master,
        "with DAG(schedule='@hourly'):\n    job_emails = Task(prompt='New emails about jobs?', return_type=list,"
        " outlets=['job-emails'])\n",
    )
    jobs = history.new_session("Job hunting").id
    dags.save(jobs, JOBS)
    dags.store.update(jobs, guidance="Only remote jobs, or in London.", context="CV: Python, data, five years.")
    store = dags.store
    run = store.start_run(jobs, "event:job-emails", "From the master agent.", JOBS, ["Job A", "Job B"])
    long = '{"query": "data engineer", "where": ' + '"London", ' * 20 + "}"  # one long line
    steps = [
        {"tool": "search_web", "given": long, "outcome": "ran", "said": "10 results"},
        {
            "tool": "fetch",
            "given": '{"url": "https://jobs.example/1"}',
            "outcome": "failed",
            "said": "Error: timed out.",
        },
    ]
    for i, (job, fit) in enumerate([("Job A", True), ("Job B", False)]):
        store.finish_task(store.add_task(run.id, "fits", i, "running", job, "trigger"), "success",
                          "**Yes**: Python." if fit else "No.", steps if fit else [], fit)  # fmt: skip
        applied = store.add_task(run.id, "apply", i, "running" if fit else "skipped", job, "trigger")
        store.finish_task(applied, "success" if fit else "skipped", "Applied." if fit else "“fits” said no.", [])
    store.finish_task(store.add_task(run.id, "report", 0, "running"), "success", "Applied to A.", [])
    for end in ("review_workflow", "review_tools", "review_skills", "save_context", "summary"):
        store.finish_task(
            store.add_task(run.id, end, 0, "running"),
            "skipped" if end == "review_skills" else "success",
            f"{end} said.",
            [],
        )
    store.finish_run(run, "success", "Applied to one job of two. **One didn't fit.**")
    better = JOBS.replace("Say what was applied for.", "List what was applied for, with links.")
    store.propose(jobs, run.id, "workflow", "The report should link to each job.", JOBS, source=better)
    store.propose(jobs, run.id, "tools", "fetch timed out.", JOBS, changes={"hide": ["read_news"]})
    other = history.new_session("Briefing").id
    dags.save(other, "with DAG(schedule='30 7 * * 1-5'):\n    brief = Task(prompt='Brief me for the day.')\n")
    dags.pause(other)
    return jobs


VIEWS = ["#agents/2", "#agents/2/dag", "#agents/2/runs", "#agents/2/context", "#agents/2/guidance", "#agents/1/dag"]
VIEWS += ["#agents/all"]


@pytest.mark.parametrize(("width", "height", "scheme"), LOOKS)
async def test_every_tab_and_view_loads_without_errors_and_fits_the_screen(athena, browser, width, height, scheme):  # noqa: F811
    seed(athena)
    page, base, errors = await signed_in(browser, athena, width, height, scheme)
    for where in [*(f"#{tab}" for tab in TABS), *VIEWS]:
        await page.goto(f"{base}/{where}")
        tab = where[1:].split("/")[0]
        await page.wait_for_selector(f"#tab-{tab} section, #tab-{tab} .card")
        await page.wait_for_timeout(300)  # anything it loads after
        await page.evaluate("document.querySelectorAll('details').forEach((d) => (d.open = true))")
        wide = await page.evaluate("document.documentElement.scrollWidth")
        assert wide <= width, f"{where} is {wide}px wide on a {width}px screen"
    assert errors == []


async def test_writing_a_dag_and_seeing_it_drawn(athena, browser):  # noqa: F811
    jobs = athena.history.new_session("Job hunting").id
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#agents/{jobs}/dag")
    await page.wait_for_selector(".dag-summary >> text=Not saved yet")
    await page.wait_for_selector(".dag-graph .graph-node >> text=check")  # the file it starts with, drawn
    await page.fill(".code-text", JOBS)
    await page.wait_for_selector(".dag-graph .graph-node >> text=fits")
    assert await page.locator(".dag-graph .graph-node").count() == 4  # and what started it, for each_job
    assert await page.locator(".dag-graph .graph-chip").count() == 5  # the tasks every DAG ends with
    assert await page.inner_text(".dag-graph .graph-group-label") == "each_job"
    assert await page.locator(".dag-graph .graph-node[data-each]").count() == 2  # it and apply, for each item
    assert await page.inner_text(".dag-graph .graph-edge-label.yes") == "yes"
    # A mistake is said, where it is, and the drawing waits for it to be put right.
    await page.fill(".code-text", JOBS.replace("each_job >> report", "each_job >> nowhere"))
    await page.wait_for_selector(".code-problem >> text=There's no task called “nowhere”")
    assert (await page.inner_text(".code-lines .bad")).strip() == "9"
    assert await page.is_disabled(".dag-editor button:text-is('Save')")
    await page.fill(".code-text", JOBS)
    await page.wait_for_selector(".code-problem:empty")
    await page.click(".dag-editor button:text-is('Save')")
    await page.wait_for_selector(".dag-summary >> text=Runs every weekday at 09:00")
    assert athena.dags.store.get(jobs).dag == JOBS
    # What starts it, and where from.
    await page.wait_for_selector(".dag-trigger >> text=job-emails")
    await page.wait_for_selector(".dag-trigger >> text=once TRIGGERS_TOKEN is set")  # no token in the tests
    await page.click(".dag-summary >> text=Pause")
    await page.wait_for_selector(".dag-summary >> text=paused")
    assert athena.dags.store.get(jobs).paused
    await page.click(".dag-summary >> text=Resume")
    await page.wait_for_selector(".dag-summary >> text=next")
    # How it's written: every argument.
    await page.click("text=How to write it")
    for argument in ("return_type", "for_each", "run_if", "allowed_tools", "outlets", "triggers"):
        await page.wait_for_selector(f"dialog dt >> text={argument}")
    await page.click("dialog >> text=Close")
    assert errors == []


async def test_approving_and_rejecting_what_a_run_proposes(athena, browser):  # noqa: F811
    jobs = seed(athena)
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#agents/{jobs}/dag")
    assert await page.inner_text(".agent-view-link[aria-current] .view-count") == "2"
    workflow = page.locator(".proposal", has_text="A change to its tasks")
    await workflow.locator(".diff-line.added >> text=with links").wait_for()
    await workflow.locator("text=Approve").click()
    await page.wait_for_selector("#toast >> text=Approved")
    assert "with links" in athena.dags.store.get(jobs).dag
    assert "with links" in await page.input_value(".code-text")  # the file shown is the file saved
    await page.locator(".proposal", has_text="Its tools").locator("text=Reject").click()
    await page.wait_for_selector(".proposal", state="detached")
    assert athena.dags.store.proposals(jobs) == []
    assert errors == []


async def test_a_runs_history(athena, browser):  # noqa: F811
    jobs = seed(athena)
    page, base, errors = await signed_in(browser, athena, 1280, 900)
    await page.goto(f"{base}/#agents/{jobs}/runs")
    await page.wait_for_selector(".run-row >> text=#1")
    await page.wait_for_selector(".run-head >> text=Applied to one job of two.")
    await page.click(".run-input summary")
    await page.wait_for_selector(".run-items >> text=Job B")  # what started it came with two
    assert await page.get_attribute(".graph-node[aria-label^='fits']", "data-state") == "success"
    assert "×2" in await page.inner_text(".graph-node[aria-label^='fits']")  # once for each
    assert "1 done, 1 skipped" in await page.get_attribute(".graph-node[aria-label^='apply']", "aria-label")
    assert await page.get_attribute(".graph-chip:text-is('review skills')", "data-state") == "skipped"
    await page.click(".graph-node[aria-label^='fits']")
    fits = page.locator(".run-task[data-task='fits']")
    assert await fits.count() == 2
    first = fits.first
    await first.locator("text=Said yes").wait_for()
    await first.locator(".step-io pre >> text=Job A").wait_for()  # its item
    assert await first.locator(".run-output strong").inner_text() == "Yes"  # what it said, as Markdown
    await first.locator("li.step[data-outcome=failed] summary").click()
    assert await page.is_visible("text=Error: timed out.")
    applied = page.locator(".run-task[data-task='apply']")
    assert await applied.nth(1).get_attribute("data-state") == "skipped"
    # Its proposals are here too.
    assert await page.locator(".run-detail .proposal").count() == 2
    assert errors == []


async def test_an_agents_guidance_files_and_what_it_kept(athena, browser):  # noqa: F811
    jobs = seed(athena)
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#agents/{jobs}/guidance")
    assert await page.input_value(".text-view") == "Only remote jobs, or in London."
    await page.fill(".text-view", "Only remote jobs.")
    await page.click(".text-card >> text=Save")
    await page.wait_for_selector("#toast >> text=Saved.")
    assert athena.dags.store.get(jobs).guidance == "Only remote jobs."
    # Files with its guidance: added, listed, and deleted.
    await page.set_input_files(
        ".files-card input[type=file]", files=[{"name": "cv.md", "mimeType": "text/markdown", "buffer": b"# CV"}]
    )
    await page.wait_for_selector(".file-row >> text=cv.md")
    assert await page.get_attribute(".file-name", "href") == f"/api/agents/{jobs}/files/cv.md"
    assert f"agents/{jobs}/cv.md" in athena.dags.intro_for(jobs)
    await page.click(".file-remove")
    await page.wait_for_selector(".file-row", state="detached")
    await page.click(".agent-view-link >> text=Context")
    await page.wait_for_selector(".text-card >> text=Kept")
    assert await page.input_value(".text-view") == "CV: Python, data, five years."
    # What you're writing stays while you look at another view.
    await page.fill(".text-view", "CV: Python, data, six years.")
    await page.click(".agent-view-link >> text=Chat")
    await page.click(".agent-view-link >> text=Context")
    assert await page.input_value(".text-view") == "CV: Python, data, six years."
    assert errors == []


async def test_the_master_agent_comes_first_and_stays(athena, browser):  # noqa: F811
    seed(athena)
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#agents")
    await at(page, "#agents/1")  # the master agent, at its own address
    first = page.locator(".session").first
    assert (
        await first.get_attribute("data-master") == ""
        and await first.locator(".session-title").inner_text() == "Master"
    )
    await page.wait_for_selector(".chat-sub >> text=Telegram, Siri and the terminal talk to it")
    await page.click(".chat-more")
    await page.wait_for_selector("dialog >> text=The master agent")
    assert await page.locator("dialog >> text=Delete agent").count() == 0  # it can't be
    await page.click("dialog button:text-is('Cancel')")
    # What its DAG finds starts another's: it says so.
    await page.goto(f"{base}/#agents/1/dag")
    await page.wait_for_selector(".dag-trigger.outlet >> text=the DAG of Job hunting waits for it")
    assert errors == []


# -- chat -------------------------------------------------------------------------------------------------


@pytest.fixture
def talking(athena):  # noqa: F811
    """Athena with a model that asks for send_note, which asks first, when a message says "use send_note"."""
    from conftest import FakeLLMServer
    from test_chat import Model

    from athena.tools import Tool

    let_go = asyncio.Event()

    async def send_note(args):
        return "Sent."

    async def wait_here(args):
        await let_go.wait()
        return "Waited."

    params = {"type": "object", "properties": {}}
    athena.tools.add(Tool("send_note", "Sends a note.", params, send_note, needs_confirmation=True))
    athena.tools.add(Tool("wait_here", "Waits.", params, wait_here))
    athena.llm = athena.agent.llm = FakeLLMServer(Model()).client(athena.config.llm)
    athena.let_go = let_go
    return athena


async def test_chatting_approving_and_talking_to_another_agent(talking, browser):
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#agents")
    await page.fill(".composer textarea", "Hello **there**")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.theirs >> text=Hello there.")
    assert await page.input_value(".composer textarea") == ""
    assert await page.inner_text(".msg.mine .msg-text") == "Hello **there**"  # your words, as typed

    # It asks before send_note: Allow, here.
    await page.fill(".composer textarea", "Please use send_note")
    await page.click(".chat-send")
    await page.wait_for_selector("li.approval >> text=send_note")
    assert await page.inner_text('.tab[data-tab="agents"] .tab-count') == "1"
    await page.click("li.approval button:text-is('Allow')")
    await page.wait_for_selector(".msg.theirs >> text=Done: Sent.")
    await page.wait_for_selector('.tab[data-tab="agents"] .tab-count', state="detached")

    # A command, from the suggestions, says what it did.
    await page.fill(".composer textarea", "/he")
    await page.wait_for_selector(".chat-suggest:not([hidden]) >> text=/help")
    await page.press(".composer textarea", "Tab")
    assert await page.input_value(".composer textarea") == "/help "
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.note >> text=Just send me a message.")

    # A new agent, then back to the master, which is still the one Telegram talks to.
    await page.click(".chat-sessions-head >> text=New agent")
    await page.fill("dialog input[aria-label='Its name']", "Second")
    await page.click("dialog >> text=Make it")
    await page.wait_for_selector(".chat-empty")
    assert await page.inner_text(".chat-title") == "Second"
    await page.fill(".composer textarea", "Second conversation")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.theirs >> text=Hello there.")
    await page.click(".session >> text=Master")
    await page.wait_for_selector(".chat-title:text-is('Master')")
    await page.fill(".composer textarea", "Back again")
    await page.wait_for_selector(".chat-send:enabled")  # once the page knows the last answer's finished
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.mine >> text=Back again")
    assert talking.history.master.id == 1 and len(talking.history.messages(2)[0]) == 2
    assert await page.inner_text(".session[aria-current] .current") == "Telegram"
    assert errors == []


async def test_allowing_a_tool_for_a_message_or_the_session(talking, browser):
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#agents")
    await page.click(".chat-tools")
    row = page.locator(".allow-tool", has_text="send_note")
    await row.locator("text=Next message").click()
    await page.click("dialog >> text=Done")
    assert await page.inner_text(".chat-allowed .chip") == "send_note"  # for the next message only
    await page.fill(".composer textarea", "Please use send_note")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.theirs >> text=Done: Sent.")  # without asking
    await page.wait_for_selector(".chat-allowed", state="hidden")

    await page.click(".chat-tools")
    await page.locator(".allow-tool", has_text="send_note").locator("text=Always here").click()
    await page.click("dialog >> text=Done")
    await page.wait_for_selector(".chat-tools >> text=1 allowed")
    assert talking.history.master.allowed_tools == ("send_note",)
    assert errors == []


async def test_on_a_phone_the_tabs_are_a_row_of_their_own(talking, browser):
    page, base, errors = await signed_in(browser, talking, 375, 760)
    await page.wait_for_selector("#tabs")
    assert await page.locator(".tab").count() == 5
    await at(page, "#agents/1")  # the master agent, at its own address
    await page.click('.tab[data-tab="memory"]')
    await page.wait_for_selector("#tab-memory:not([hidden])")
    await page.click('.tab[data-tab="agents"]')
    await page.wait_for_selector('.tab[data-tab="agents"][aria-current=page]')
    tabs = await page.evaluate("[...document.querySelectorAll('.tab')].map((t) => t.getBoundingClientRect().right)")
    assert max(tabs) <= 375  # all of them, on the screen

    # A conversation, or the list of them, one at a time.
    await page.wait_for_selector(".chat-main")
    assert await page.is_hidden(".chat-sessions")
    await page.click(".chat-back")
    await page.wait_for_selector(".chat-sessions")
    assert await page.is_hidden(".chat-main")
    await page.click(".session")
    await page.wait_for_selector(".chat-main")
    assert errors == []


async def test_while_athena_answers_enter_doesnt_stop_it_and_the_button_does(talking, browser):
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#agents")
    await page.fill(".composer textarea", "Please use wait_here")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".typing >> text=Using wait_here")
    assert await page.get_attribute(".chat-send", "aria-label") == "Stop"
    await page.fill(".composer textarea", "And another thing")
    await page.press(".composer textarea", "Enter")  # nothing: one message at a time
    await page.wait_for_timeout(300)
    assert talking.chat.replying is not None and await page.input_value(".composer textarea") == "And another thing"
    assert await page.is_disabled(".chat-sessions-head >> text=New agent")

    await page.click(".chat-send")  # Stop
    await page.wait_for_selector(".msg.failed >> text=Didn't get an answer: You stopped it.")
    talking.let_go.set()
    await page.click(".msg.failed >> text=Try again")
    await page.wait_for_selector(".msg.theirs >> text=Done: Waited.")
    await page.wait_for_selector(".msg.failed", state="detached")
    assert errors == []


async def test_turning_notifications_on_testing_them_and_off(talking, browser):
    import json

    import httpx
    from test_push import Device

    device, received = Device(), []
    talking.push.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: received.append(device.read(r.content)) or httpx.Response(201))
    )
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    # The browser's push service, which headless Chromium hasn't: a subscription as Safari would give, once you've
    # allowed notifications, which headless Chromium can't ask you.
    sub = json.dumps(device.subscription)
    await context.add_init_script(
        f"""
        const sub = {sub};
        let current = null;
        const made = {{ endpoint: sub.endpoint, options: {{}}, toJSON: () => sub,
                        unsubscribe: async () => {{ current = null; return true; }} }};
        PushManager.prototype.subscribe = async function () {{ return (current = made); }};
        PushManager.prototype.getSubscription = async function () {{ return current; }};
        Object.defineProperty(Notification, "permission", {{ get: () => "granted" }});
        Notification.requestPermission = async () => "granted";
        """
    )
    page = await context.new_page()
    base = f"http://127.0.0.1:{talking.dashboard.port}"
    await page.goto(f"{base}/")
    await page.fill("#password", PASSWORD)
    await page.click("#sign-in-form button[type=submit]")
    await page.goto(f"{base}/#agents")
    await page.wait_for_function("navigator.serviceWorker.controller !== null || navigator.serviceWorker.ready")
    assert await page.evaluate("navigator.serviceWorker.ready.then((r) => r.active.scriptURL)") == f"{base}/sw.js"

    # The Chat tab offers them, until they're on.
    await page.click(".chat-banner >> text=Turn on")
    await page.wait_for_selector(".chat-banner", state="hidden")
    assert talking.push.subscribed(device.subscription["endpoint"]) == 1
    # The Status tab's bell says they're on, and offers a test, or turning them off.
    await page.goto(f"{base}/#status")
    await page.click("#tab-status .bell[data-on=true]")
    await page.click("dialog >> text=Send a test")
    await page.wait_for_selector("#toast >> text=Sent.")
    assert received[0]["body"].startswith("Notifications are on.")
    await page.click("dialog >> text=Turn off")
    await page.wait_for_selector("#tab-status .bell[data-on=false]")
    assert talking.push.subscribed() == 0
    await page.keyboard.press("Escape")
    await page.goto(f"{base}/#agents")
    await page.wait_for_selector(".chat-banner >> text=Turn on")  # offered again
    await page.click(".chat-banner [aria-label='Not on this device']")
    await page.wait_for_selector(".chat-banner", state="hidden")
    await page.reload()
    await page.wait_for_selector(".chat-main")
    assert await page.is_hidden(".chat-banner")  # and it stays that way
    await context.close()


async def test_the_tools_button_says_what_it_is_and_never_null(talking, browser):
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#agents")
    await page.wait_for_selector(".chat-main")
    assert await page.inner_text(".chat-tools") == "Tools"
    assert "null" not in await page.inner_text("#tab-agents")
    assert errors == []


async def test_commands_from_their_button(talking, browser):
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#agents")
    await page.click(".chat-icon[aria-label=Commands]")
    await page.wait_for_selector(".chat-suggest:not([hidden]) >> text=/remember")
    await page.click(".chat-suggest >> text=/help")  # needs nothing more: it runs
    await page.wait_for_selector(".msg.note >> text=Just send me a message.")
    await page.wait_for_selector(".chat-suggest", state="hidden")
    await page.click(".chat-icon[aria-label=Commands]")
    await page.click(".chat-suggest >> text=/remember")  # needs what to remember: it's typed for you
    assert await page.input_value(".composer textarea") == "/remember "
    await page.click(".chat-icon[aria-label=Commands]")
    await page.click(".chat-log")  # anywhere else closes it
    await page.wait_for_selector(".chat-suggest", state="hidden")
    assert errors == []


async def test_sending_photos_and_files(talking, browser):
    from athena.photos import red_square

    photo = red_square()
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#agents")
    await page.wait_for_selector(".chat-main")
    await page.set_input_files(
        ".composer input[type=file]",
        files=[
            {"name": "square.png", "mimeType": "image/png", "buffer": photo.data},
            {"name": "notes.md", "mimeType": "text/markdown", "buffer": b"# Notes"},
        ],
    )
    await page.wait_for_selector(".attached >> text=notes.md")
    assert await page.locator(".attached img").count() == 1
    assert await page.is_enabled(".chat-send")  # a photo needs nothing written with it
    await page.click('.attached-remove[aria-label="Don\'t send notes.md"]')
    await page.wait_for_selector(".attached >> text=notes.md", state="detached")
    notes = {"name": "notes.md", "mimeType": "text/markdown", "buffer": b"# Notes"}
    await page.set_input_files(".composer input[type=file]", files=[notes])
    await page.wait_for_selector(".attached >> text=notes.md")
    await page.fill(".composer textarea", "What are these?")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.theirs >> text=Hello there.")
    await page.wait_for_selector(".chat-attached", state="hidden")
    mine = page.locator(".msg.mine").last
    assert await mine.locator("img").get_attribute("src") == f"/api/photos/{photo.name}"
    assert await mine.locator(".file-chip").inner_text() == "notes.md"
    assert await mine.locator(".msg-text").inner_text() == "What are these?"
    assert await page.evaluate("document.querySelector('.msg.mine img').naturalWidth") == 64  # it's shown
    assert errors == []


async def test_naming_muting_and_deleting_an_agent(talking, browser):
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#agents")
    await page.click(".chat-sessions-head >> text=New agent")
    await page.click("dialog >> text=Make it")
    await page.wait_for_selector(".chat-empty")
    await page.fill(".composer textarea", "Find me a job in data")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.theirs >> text=Hello there.")

    await page.click(".chat-more")
    await page.get_by_label("Name").fill("Job hunting")
    assert await page.input_value("dialog select[aria-label=Model]") == ""  # the default
    await page.click("dialog >> text=Save")
    await page.wait_for_selector(".chat-title:text-is('Job hunting')")
    await page.wait_for_selector(".session-title:text-is('Job hunting')")

    assert await page.get_attribute(".chat-head .bell", "aria-pressed") == "true"  # every agent notifies you
    await page.click(".chat-head .bell")
    await page.wait_for_selector(".chat-head .bell[aria-pressed=false]")
    await page.wait_for_selector(".session [aria-label=Muted]")
    assert talking.history.get_session(2).muted

    talking.dags.save(2, "with DAG(schedule='0 9 * * *'):\n    find = Task(prompt='Look for new postings.')\n")
    await page.click(".chat-more")
    await page.click("dialog >> text=Delete agent")
    await page.wait_for_selector("dialog >> text=and so are its DAG")
    await page.click("dialog button:text-is('Delete')")
    await page.wait_for_selector("#toast >> text=Deleted.")
    await page.wait_for_selector(".session-title:text-is('Job hunting')", state="detached")
    await at(page, "#agents/1")  # back to the master agent
    assert talking.history.get_session(2) is None and talking.dags.store.get(2).dag == ""
    assert errors == []


async def test_what_a_run_says_arrives_in_the_agents_chat_and_the_masters(talking, browser):
    from conftest import FakeLLMServer

    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#agents")
    await page.click(".chat-sessions-head >> text=New agent")
    await page.fill("dialog input[aria-label='Its name']", "Jobs")
    await page.click("dialog >> text=Make it")
    await page.wait_for_selector(".chat-empty")
    talking.dags.save(2, "with DAG():\n    find = Task(prompt='Look for new postings that fit my CV.')\n")
    await page.click(".agent-view-link >> text=DAG")
    await page.wait_for_selector(".dag-summary >> text=only when you run it")

    # It runs while you're looking at another agent: that one's marked as having something new.
    await page.click(".session >> text=Master")
    await page.wait_for_selector(".chat-main")
    talking.llm = talking.agent.llm = FakeLLMServer(
        lambda body: completion(
            "Two new jobs at Acme."
            if "Write George a short summary" in json.dumps(body["messages"][-1])
            else "NO CHANGES"
        )
    ).client(talking.config.llm)
    await talking.dags.runner.run(2, "manual")
    # The master agent's told too, where Telegram talks to you, with a link to the agent's runs.
    await page.wait_for_selector(".msg-task-link >> text=Jobs · run #1")
    await page.wait_for_selector(".msg.theirs >> text=Two new jobs at Acme.")
    await page.wait_for_selector(".session[data-unread=true] >> text=Jobs")
    await page.click(".session >> text=Jobs")
    await page.wait_for_selector(".msg-task-link >> text=Run #1: how it went")
    await page.wait_for_selector(".session[data-unread=true] >> text=Jobs", state="detached")
    await page.click(".msg-task-link")
    await page.wait_for_selector(".run-row >> text=#1")
    assert errors == []
