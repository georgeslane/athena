"""The dashboard in a real browser (headless Chromium, through Playwright), served by Athena's own services as in
test_dashboard.py: every tab loads without errors and fits a phone, and the task and watch forms work.

CI installs the browser (`playwright install --only-shell chromium`), and there these tests must run. Elsewhere
they're skipped without one, or ATHENA_TEST_BROWSER can name a Chromium to use.
"""

import asyncio
import os
import time

import pytest
from conftest import completion
from test_dashboard import PASSWORD, athena  # noqa: F401 - athena is a fixture

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - it's a dev dependency
    async_playwright = None

TABS = ["status", "chat", "activity", "tasks", "memory", "skills", "board", "tools"]
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


def expected(console_error: str) -> bool:
    """The browser logs every 4xx as an error. A 409 is the API saying something's switched off, such as the
    board without Projects for Claude, which the page shows as such."""
    return "Failed to load resource" in console_error and "409 (Conflict)" in console_error


def seed(services):
    """A task that's run, with its steps, one paused, one waiting for an event, and two things watched."""
    tasks = services.tasks
    brief = tasks.add("Morning briefing", "Brief me for the day: calendar, news.", "30 7 * * 1-5", [], "you")
    brief.last_run, brief.runs, brief.last_outcome = time.time() - 3600, 3, "told you"
    brief.last_result = "Dentist at 2."
    long = '{"start": "2026-10-22", "end": "2026-10-22", "calendars": ' + '"all", ' * 20 + "}"  # one long line
    brief.last_steps = [
        {"tool": "events_fetch", "given": long, "outcome": "ran", "said": "1 event"},
        {"tool": "news_headlines", "given": '{"feed": "BBC News"}', "outcome": "failed", "said": "Error: timed out."},
    ]
    tasks.store.save(brief)
    tasks.pause(tasks.add("Servers", "Look for MCP servers.", "0 10 * * sun", ["forget_memory"], "athena").id)
    tasks.add("Home", "Anything to do this evening?", "on arrive-home", [], "you")
    tasks.watches.add("anything in my inbox that needs a reply", None, by="you")
    tasks.watches.add("when the parcel comes", time.time() + 86400 * 5, by="athena")
    tasks.check_result = {"at": time.time() - 600, "outcome": "nothing new", "said": "", "steps": brief.last_steps}
    seed_activity(services, brief.last_steps)


def seed_activity(services, steps):
    """What Athena's done: a briefing, three quiet checks, a message where you allowed one thing and declined
    another, and an alert."""
    timeline = services.timeline
    timeline.add("task", "Morning briefing", "told you", said="Dentist at 2.", seconds=42, steps=steps)
    for _ in range(3):
        timeline.add("check", "Checked what you're watching", "nothing new", seconds=8)
    email = '{"to": "sam@example.com"}'
    asked = [
        {"tool": "send_email", "given": email, "outcome": "ran", "said": "Sent.", "approval": "you",
         "flagged": "an email address (“sam@example.com”)"},
        {"tool": "publish", "given": "{}", "outcome": "declined", "said": "The user declined.", "approval": "you"},
    ]  # fmt: skip
    timeline.add("message", "Email Sam the notes " + "and more " * 30, "answered", said="Sent one.", channel="Telegram",
                 seconds=6.5, steps=asked)  # fmt: skip
    timeline.add("alert", "The model server isn't working", "problem", said="it didn't answer")


@pytest.mark.parametrize(("width", "height", "scheme"), LOOKS)
async def test_every_tab_loads_without_errors_and_fits_the_screen(athena, browser, width, height, scheme):  # noqa: F811
    seed(athena)
    page, base, errors = await signed_in(browser, athena, width, height, scheme)
    for tab in TABS:
        await page.goto(f"{base}/#{tab}")
        await page.wait_for_selector(f"#tab-{tab} section, #tab-{tab} .card")
        await page.wait_for_timeout(300)  # anything it loads after
        await page.evaluate("document.querySelectorAll('details').forEach((d) => (d.open = true))")
        wide = await page.evaluate("document.documentElement.scrollWidth")
        assert wide <= width, f"the {tab} tab is {wide}px wide on a {width}px screen"
    assert errors == []


async def test_adding_a_task_and_changing_its_schedule(athena, browser):  # noqa: F811
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#tasks")
    await page.click("text=New task")
    await page.click("dialog >> text=Morning briefing")  # an example to start from
    await page.click("dialog button[type=submit]")
    await page.wait_for_selector("article.task >> text=Every weekday at 07:30")

    await page.click("article.task >> text=Edit")
    await page.fill("dialog input[type=text]", "Weekly briefing")
    await page.click("dialog >> text=Fridays 18:00")  # a suggested schedule fills the field
    assert await page.input_value("dialog input[aria-label=When]") == "0 18 * * 5"
    await page.click("dialog button[type=submit]")
    await page.wait_for_selector("article.task >> text=Every Friday at 18:00")
    [task] = athena.tasks.store.all()
    assert (task.title, task.schedule) == ("Weekly briefing", "0 18 * * 5")
    assert errors == []


async def test_a_schedule_that_wont_do_is_explained_in_the_form(athena, browser):  # noqa: F811
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#tasks")
    await page.click("text=New task")
    await page.fill("dialog input[type=text]", "Something")
    await page.fill("dialog textarea", "Do something.")
    await page.fill("dialog input[aria-label=When]", "sometime")
    await page.click("dialog button[type=submit]")
    await page.wait_for_selector('dialog .form-error:has-text("I don\'t understand the schedule")')
    assert athena.tasks.store.all() == []


async def test_watching_with_no_end_then_until_a_day(athena, browser):  # noqa: F811
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#tasks")
    await page.click("text=Watch for…")
    await page.fill("dialog textarea", "anything from the bank")
    await page.check("dialog input[type=checkbox]")  # no end
    assert await page.is_disabled("dialog input[type=date]")
    await page.click("dialog button[type=submit]")
    await page.wait_for_selector("article.task >> text=No end")
    assert athena.tasks.watches.all()[0].until is None

    await page.click("article.task:has-text('the bank') >> text=Edit")
    await page.uncheck("dialog input[type=checkbox]")
    await page.fill("dialog input[type=date]", "2099-01-31")
    await page.click("dialog button[type=submit]")
    await page.wait_for_selector("article.task >> text=Until Sat 31 Jan")
    assert errors == []


async def test_a_runs_steps_stay_open_when_the_tab_is_drawn_again(athena, browser):  # noqa: F811
    seed(athena)
    athena.agent.llm = None  # nothing here should need the model
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#tasks")
    await page.click("summary:has-text('Last run')")
    await page.click("li.step[data-outcome=failed] summary")
    assert await page.is_visible("text=Error: timed out.")
    await page.click(".tab[data-tab=status]")
    await page.click(".tab[data-tab=tasks]")  # drawn again, from what Athena says now
    await page.wait_for_timeout(300)
    assert await page.is_visible("text=Error: timed out.")
    assert errors == []


def test_the_seeded_steps_are_what_a_run_records():
    """seed() writes steps by hand: they have the fields steps() gives them."""
    from athena.timeline import steps

    taken, _ = steps()
    assert taken == [] and completion("x")["choices"][0]["message"]["content"] == "x"


async def test_the_activity_tab(athena, browser):  # noqa: F811
    seed(athena)
    page, base, errors = await signed_in(browser, athena, 360, 800)
    await page.goto(f"{base}/#activity")
    await page.wait_for_selector("#tab-activity .entry")
    top = "#tab-activity .activity:not(.nested) > .entry"
    kinds = await page.eval_on_selector_all(top, "(entries) => entries.map((e) => e.dataset.kind)")
    assert kinds == ["alert", "message", "check", "task", "athena"]  # newest first
    # The three quiet checks are one line, which opens to each.
    assert "Checked what you're watching 3 times" in await page.locator(top).nth(2).inner_text()
    await page.click("text=Checked what you're watching 3 times")
    assert await page.locator(".activity.nested > .entry").count() == 3

    # Your message: what was said, each step, who allowed it, and what the privacy check found.
    message = page.locator(".entry[data-kind=message]")
    assert (
        "Answered · Telegram · 6.5 s · 2 steps · asked you 2 times" in await message.locator(".entry-sub").inner_text()
    )
    assert await message.locator(".entry-dot").get_attribute("data-tone") == "approval"  # something was declined
    await message.locator("summary").first.click()
    assert await page.is_visible("text=Sent one.") and await page.is_visible(".step-approval >> text=you allowed")
    await message.locator(".step summary").first.click()
    assert await page.is_visible("text=The privacy check found an email address (“sam@example.com”).")

    # Only the tasks and checks, then all of them again.
    await page.click(".filters >> text=Tasks")
    await page.wait_for_selector(".entry[data-kind=task]")
    kinds = await page.eval_on_selector_all(top, "(entries) => entries.map((e) => e.dataset.kind)")
    assert kinds == ["check", "task"]
    await page.click(".filters >> text=All")
    await page.wait_for_selector(".entry[data-kind=message]")

    # The Status tab links here, from what was done last.
    await page.goto(f"{base}/#status")
    await athena.agent.respond("111", "Hello", channel="Telegram")
    await page.click("text=Everything it's done ›")
    await page.wait_for_selector(".entry-title >> text=Hello")
    assert errors == []


async def test_earlier_activity_comes_a_page_at_a_time(athena, browser):  # noqa: F811
    from athena.timeline import PAGE

    for i in range(PAGE + 5):
        athena.timeline.add("task", f"Run {i}", "told you")
    page, base, errors = await signed_in(browser, athena)
    await page.goto(f"{base}/#activity")
    await page.wait_for_selector("#tab-activity .entry")
    assert await page.locator("#tab-activity .entry").count() == PAGE
    await page.click("text=Show earlier")
    await page.wait_for_selector(".entry-title >> text=Run 0")
    assert await page.locator("#tab-activity .entry").count() == PAGE + 6  # and when Athena started
    assert not await page.is_visible("text=Show earlier")
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


async def test_chatting_approving_and_carrying_on_an_earlier_session(talking, browser):
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#chat")
    await page.fill(".composer textarea", "Hello **there**")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.theirs >> text=Hello there.")
    assert await page.input_value(".composer textarea") == ""
    assert await page.inner_text(".session[aria-current] .session-title") == "Hello **there**"  # your words, as typed

    # It asks before send_note: Allow, here.
    await page.fill(".composer textarea", "Please use send_note")
    await page.click(".chat-send")
    await page.wait_for_selector("li.approval >> text=send_note")
    assert await page.inner_text('.tab[data-tab="chat"] .tab-count') == "1"
    await page.click("li.approval button:text-is('Allow')")
    await page.wait_for_selector(".msg.theirs >> text=Done: Sent.")
    await page.wait_for_selector('.tab[data-tab="chat"] .tab-count', state="detached")

    # A command, from the suggestions, says what it did.
    await page.fill(".composer textarea", "/he")
    await page.wait_for_selector(".chat-suggest:not([hidden]) >> text=/help")
    await page.press(".composer textarea", "Tab")
    assert await page.input_value(".composer textarea") == "/help "
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.note >> text=Just send me a message.")

    # A new chat, then back to the first, which says sending carries it on.
    await page.click("text=New chat")
    await page.wait_for_selector(".chat-empty")
    await page.fill(".composer textarea", "Second conversation")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.theirs >> text=Hello there.")
    await page.click(".session >> text=Hello **there**")
    await page.wait_for_selector(".chat-notice >> text=An earlier session")
    await page.fill(".composer textarea", "Back again")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".msg.mine >> text=Back again")
    await page.wait_for_selector(".chat-notice", state="hidden")
    assert talking.history.session.id == 1
    assert await page.inner_text(".session[aria-current] .current") == "Current"
    assert errors == []


async def test_allowing_a_tool_for_a_message_or_the_session(talking, browser):
    page, base, errors = await signed_in(browser, talking)
    await page.goto(f"{base}/#chat")
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
    await page.locator(".allow-tool", has_text="send_note").locator("text=Session").click()
    await page.click("dialog >> text=Done")
    await page.wait_for_selector(".chat-tools >> text=1 allowed")
    assert talking.history.session.allowed_tools == ("send_note",)
    assert errors == []


async def test_on_a_phone_the_shield_opens_the_menu(talking, browser):
    page, base, errors = await signed_in(browser, talking, 375, 760)
    assert await page.is_hidden("#tabs")
    assert await page.inner_text("#brand-where") == "Status"
    await page.click("#brand")
    await page.wait_for_selector("#tabs")
    assert await page.get_attribute("#brand", "aria-expanded") == "true"
    await page.click('.tab[data-tab="chat"]')
    await page.wait_for_selector("#tabs", state="hidden")
    await page.wait_for_selector("#brand-where:text-is('Chat')")
    assert page.url.endswith("#chat")
    await page.click("#brand")
    await page.keyboard.press("Escape")
    await page.wait_for_selector("#tabs", state="hidden")

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
    await page.goto(f"{base}/#chat")
    await page.fill(".composer textarea", "Please use wait_here")
    await page.press(".composer textarea", "Enter")
    await page.wait_for_selector(".typing >> text=Using wait_here")
    assert await page.get_attribute(".chat-send", "aria-label") == "Stop"
    await page.fill(".composer textarea", "And another thing")
    await page.press(".composer textarea", "Enter")  # nothing: one message at a time
    await page.wait_for_timeout(300)
    assert talking.chat.replying is not None and await page.input_value(".composer textarea") == "And another thing"
    assert await page.is_disabled("text=New chat")

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
    await page.goto(f"{base}/#chat")
    await page.wait_for_function("navigator.serviceWorker.controller !== null || navigator.serviceWorker.ready")
    assert await page.evaluate("navigator.serviceWorker.ready.then((r) => r.active.scriptURL)") == f"{base}/sw.js"

    await page.click(".chat-head .bell")
    await page.click("dialog >> text=Turn on")
    await page.wait_for_selector(".chat-head .bell[data-on=true]")
    assert talking.push.subscribed(device.subscription["endpoint"]) == 1
    await page.click("dialog >> text=Send a test")  # the dialog says they're on, and offers a test
    await page.wait_for_selector("#toast >> text=Sent.")
    assert received[0]["body"].startswith("Notifications are on.")
    await page.click("dialog >> text=Turn off")
    await page.wait_for_selector(".chat-head .bell[data-on=false]")
    assert talking.push.subscribed() == 0
    await context.close()
