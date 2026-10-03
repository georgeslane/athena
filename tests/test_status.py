import httpx
import openai

from pi_assistant.status import Snapshot, State, StatusFile, StatusTracker, describe_error, shorten


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def tracker(**kwargs) -> tuple[StatusTracker, list[Snapshot], Clock]:
    clock = Clock()
    t = StatusTracker(clock=clock, **kwargs)
    seen: list[Snapshot] = []
    t.on_change = seen.append
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


def test_status_file_says_offline_unless_an_agent_is_running(tmp_path):
    board = StatusFile(tmp_path / "data" / "status.json")
    assert board.read().state is State.OFFLINE  # nothing has run yet

    agent = StatusFile(tmp_path / "data" / "status.json")
    t = StatusTracker()
    agent.publish(t)
    assert board.read().state is State.IDLE

    t.begin("Turn on the heating")
    assert (board.read().state, board.read().task) == (State.WORKING, "Turn on the heating")

    agent.close()  # also what happens when the process exits or crashes
    assert board.read().state is State.OFFLINE


def test_two_agent_processes_can_share_the_board(tmp_path):
    path = tmp_path / "status.json"
    bot, chat = StatusFile(path), StatusFile(path)
    bot.publish(StatusTracker())
    chat.publish(StatusTracker())
    chat.close()
    assert StatusFile(path).read().state is State.IDLE  # the bot is still running
    bot.close()
    assert StatusFile(path).read().state is State.OFFLINE


def test_unwritable_status_file_does_not_stop_the_assistant(tmp_path, caplog):
    (tmp_path / "data").write_text("a file where the data folder should be")
    t = StatusTracker()
    StatusFile(tmp_path / "data" / "status.json").publish(t)
    assert t.on_change is None
    assert "Status board disabled" in caplog.text
    t.begin("still works").finish()


def test_snapshot_json_ignores_fields_it_does_not_know():
    text = Snapshot(State.WORKING, task="x").to_json().replace('"task"', '"future_field": 1, "task"')
    assert Snapshot.from_json(text) == Snapshot(State.WORKING, task="x")
