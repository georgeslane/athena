import httpx
import openai

from pi_assistant.status import Snapshot, State, StatusTracker, describe_error, shorten


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def tracker(**kwargs) -> tuple[StatusTracker, list[Snapshot], Clock]:
    clock = Clock()
    t = StatusTracker(clock=clock, **kwargs)
    seen: list[Snapshot] = []
    t.listeners.append(seen.append)
    return t, seen, clock


def test_a_task_from_start_to_finish():
    t, seen, clock = tracker()
    t.channel, t.approval_timeout = "Telegram", 300

    task = t.begin("  Add 'Dentist' to my\ncalendar  ")
    assert seen[-1].state is State.WORKING
    assert (seen[-1].task, seen[-1].step, seen[-1].started) == ("Add 'Dentist' to my calendar", "Thinking", 1000.0)

    clock.now = 1005
    task.using("search_memory")
    assert (seen[-1].step, seen[-1].tools) == ("Searching memory", ["search_memory"])

    clock.now = 1010
    with task.approval("create_event"):
        waiting = seen[-1]
        assert waiting.state is State.APPROVAL
        assert (waiting.tool, waiting.channel, waiting.deadline) == ("create_event", "Telegram", 1310.0)
    assert seen[-1].state is State.WORKING

    task.using("create_event")
    clock.now = 1020
    task.finish()
    done = seen[-1]
    assert done.state is State.IDLE
    assert (done.last_task, done.last_error, done.last_finished) == ("Add 'Dentist' to my calendar", "", 1020.0)
    assert done.updated == 1020.0


def test_unchanged_status_is_not_published_again():
    t, seen, _ = tracker()
    task = t.begin("hi")
    task.thinking()
    task.thinking()
    assert len(seen) == 1


def test_failures_and_hidden_tasks():
    t, seen, _ = tracker(show_task=False)
    task = t.begin("Something private")
    assert seen[-1].task == ""
    task.finish(openai.APIConnectionError(request=httpx.Request("POST", "http://mac/v1")))
    assert seen[-1].last_error == "Couldn't reach the model server"
    assert seen[-1].last_task == ""


def test_a_task_waiting_for_approval_is_shown_before_newer_ones():
    t, seen, _ = tracker()
    first = t.begin("first")
    with first.approval("send_email"):
        t.begin("second")
        assert (seen[-1].state, seen[-1].task) == (State.APPROVAL, "first")
    assert (seen[-1].state, seen[-1].task) == (State.WORKING, "second")


def test_shorten_and_describe_error():
    assert shorten("word " * 100, limit=22) == "word word word word…"
    assert shorten("short") == "short"
    assert (
        describe_error(openai.APITimeoutError(request=httpx.Request("POST", "http://mac"))) == "The model took too long"
    )
    assert describe_error(ValueError("x")) == "Something went wrong (ValueError)"


async def test_the_status_api_and_the_dashboard_can_both_follow_it():
    from pi_assistant.status import StatusFeed

    t, seen, _ = tracker()
    board, dashboard = StatusFeed(t), StatusFeed(t)
    board.start()
    dashboard.start()
    hello = t.begin("hello")
    assert board.snapshot.state is dashboard.snapshot.state is State.WORKING
    assert board.version != dashboard.version  # each has its own, so neither can be confused with the other
    board.stop()
    hello.finish()
    t.begin("again").finish()
    assert board.snapshot.task == "hello" and dashboard.snapshot.last_task == "again"
    assert len(t.listeners) == 2  # the dashboard and the test's own
