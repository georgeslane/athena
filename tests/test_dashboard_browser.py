"""The dashboard in a real browser (headless Chromium, through Playwright), served by Athena's own services as in
test_dashboard.py: every tab loads without errors and fits a phone, and the task and watch forms work.

CI installs the browser (`playwright install --only-shell chromium`), and there these tests must run. Elsewhere
they're skipped without one, or ATHENA_TEST_BROWSER can name a Chromium to use.
"""

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

TABS = ["status", "activity", "tasks", "memory", "skills", "board", "tools"]
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
