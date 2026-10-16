"""Keeps what identifies you from leaving: checks what a tool would send for personal details.

Before a tool runs, Athena looks through what it would send for:

- your private words: ones you list on the dashboard (Memory), such as your full name, address, family's
  names or employer, and your name as config.toml has it ([agent] user_name)
- things that identify anyone: email addresses, phone numbers, postcodes, card and bank account numbers,
  National Insurance numbers, street addresses and precise coordinates
- Athena's secrets: the values in .env, and anything shaped like an API key or token

Secrets stop any tool. Personal details stop the tools in [privacy] block, which send where nothing about
you should go, such as Claude's projects: the model is told to take them out and try again. Other tools
that ask first say what they found in the approval message, so you can see it before you allow it.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from pi_assistant.config import PrivacyConfig
from pi_assistant.tools import matches_any

log = logging.getLogger(__name__)

MIN_TERM_CHARS = 3  # shorter private words would match too much
MAX_TERM_CHARS = 200
MAX_TERMS = 200
MIN_SECRET_CHARS = 12  # shorter values in .env, such as a port, aren't secrets
NOT_A_NAME = {"", "the user", "you", "me"}  # [agent] user_name when it isn't set


@dataclass(frozen=True)
class Finding:
    kind: str  # what it is, in words: "your name", "an email address"
    text: str  # what matched
    secret: bool = False  # one of Athena's secrets, which stops any tool

    def describe(self) -> str:
        return self.kind if self.secret else f"{self.kind} (“{self.text}”)"  # a secret isn't repeated


@dataclass(frozen=True)
class Verdict:
    findings: tuple[Finding, ...] = ()
    refused: bool = False  # the tool mustn't run

    def describe(self) -> str:
        return describe(self.findings)


def describe(findings: tuple[Finding, ...] | list[Finding]) -> str:
    """'your name (“Sam”) and an email address (“sam@mail.com”)'"""
    said = list(dict.fromkeys(f.describe() for f in findings))
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]


# -- what identifies anyone ---------------------------------------------------------------------

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])")
# Addresses made for examples, which are fine anywhere: RFC 2606 and 6761.
_EXAMPLE_DOMAINS = re.compile(r"(^|\.)(example\.(com|org|net)|example|test|invalid|localhost)$", re.I)
# A run of digits with spaces, hyphens or brackets between, that starts with +, 00 or 0: a phone number
# if there are enough digits. Dots aren't allowed between, so 0.1234567890 isn't one.
_PHONE = re.compile(r"(?<![\w+.])(?:\+|00|0)\d(?:[ ()-]{0,2}\d){7,15}(?![\w.]|-\d)")
_POSTCODE = re.compile(r"\b(?:[A-Z]{1,2}\d[A-Z\d]?|GIR) ?\d[A-Z]{2}\b", re.I)  # in the UK
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
_NI_NUMBER = re.compile(r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b")
_COORDINATES = re.compile(r"(?<![\d.])-?\d{1,2}\.\d{4,},\s*-?\d{1,3}\.\d{4,}(?![\d.])")
_STREET = re.compile(
    r"\b\d{1,4}[A-Za-z]?,? (?:[A-Z][a-z]+ ){1,3}"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Close|Drive|Dr|Way|Crescent|Gardens|Place|Terrace|Court|Grove|"
    r"Square|Hill|Row|Mews|Walk|Park|Rise|View)\b"
)
# Shaped like a key or token: OpenAI and Anthropic, GitHub, AWS, Slack, Telegram bots and private keys.
_KEY = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|"
    r"xox[abpr]-[A-Za-z0-9-]{10,}|\d{8,10}:[A-Za-z0-9_-]{35})\b|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _iban(text: str) -> bool:
    s = text.replace(" ", "")
    s = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


def _phones(text: str) -> Iterator[str]:
    for m in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if m.group().startswith(("+", "00")):
            if 10 <= len(digits) <= 15:
                yield m.group()
        elif len(digits) in (10, 11) and digits[1] != "0":
            yield m.group()  # like a UK number: 07700 900123, 020 7946 0958


def identifying(text: str) -> list[Finding]:
    """Things in ``text`` that identify anyone, whoever they belong to."""
    found = [
        Finding("an email address", m.group()) for m in _EMAIL.finditer(text) if not _EXAMPLE_DOMAINS.search(m.group(1))
    ]
    found += [Finding("a phone number", p) for p in _phones(text)]
    found += [
        Finding("a postcode", m.group())
        for m in _POSTCODE.finditer(text)
        if m.group()[-2:].lower() not in ("am", "pm")  # "S2 3pm" is a time
    ]
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append(Finding("a card number", m.group()))
    found += [Finding("a bank account number (IBAN)", m.group()) for m in _IBAN.finditer(text) if _iban(m.group())]
    found += [Finding("a National Insurance number", m.group()) for m in _NI_NUMBER.finditer(text)]
    found += [Finding("a street address", m.group()) for m in _STREET.finditer(text)]
    found += [Finding("precise coordinates", m.group()) for m in _COORDINATES.finditer(text)]
    return found


def term_pattern(term: str) -> re.Pattern[str]:
    """``term`` as a whole word or words, in any case, with any spacing between its words."""
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


def strings(value: Any) -> Iterator[str]:
    """Every string (and number) in a tool's arguments, which is what it would send."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from strings(v)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)


# -- your private words ---------------------------------------------------------------------------


class TermStore:
    """The private words you list on the dashboard, in Athena's database."""

    def __init__(self, db_path: Path):
        from pi_assistant.memory import connect_db  # memory imports config, which imports this module's config

        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS private_terms (term TEXT PRIMARY KEY COLLATE NOCASE)")

    def all(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT term FROM private_terms ORDER BY term COLLATE NOCASE").fetchall()
        return [r[0] for r in rows]

    def replace(self, terms: list[str]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM private_terms")
            self._conn.executemany("INSERT OR IGNORE INTO private_terms (term) VALUES (?)", [(t,) for t in terms])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def check_terms(raw: Any) -> list[str]:
    """Your private words, tidied, from the dashboard. Raises ValueError, saying why, if they won't do."""
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise ValueError('Send the words as a list: {"terms": ["..."]}.')
    terms = list(dict.fromkeys(" ".join(t.split()) for t in raw if t.strip()))
    short = [t for t in terms if len(t) < MIN_TERM_CHARS]
    if short:
        raise ValueError(
            f"“{short[0]}” is too short: private words need at least {MIN_TERM_CHARS} characters, "
            "or they'd match parts of everyday words."
        )
    if any(len(t) > MAX_TERM_CHARS for t in terms):
        raise ValueError(f"Each one can be up to {MAX_TERM_CHARS} characters.")
    if len(terms) > MAX_TERMS:
        raise ValueError(f"That's more than {MAX_TERMS}.")
    return terms


# -- the check ------------------------------------------------------------------------------------


class Privacy:
    def __init__(
        self,
        cfg: PrivacyConfig,
        store: TermStore,
        *,
        user_name: Callable[[], str] = lambda: "",
        env_path: Path | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.user_name = user_name  # [agent] user_name, as config.toml has it now
        self.env_path = env_path  # .env, whose values are Athena's secrets
        self._terms: list[tuple[str, str, re.Pattern[str]]] | None = None  # (kind, term, pattern), built when needed
        self._name = ""

    def automatic(self) -> list[str]:
        """Private words you don't have to list: your name."""
        name = " ".join(self.user_name().split())
        return [name] if name.lower() not in NOT_A_NAME and len(name) >= MIN_TERM_CHARS else []

    def set_terms(self, raw: Any) -> list[str]:
        terms = check_terms(raw)
        self.store.replace(terms)
        self._terms = None
        log.info("Private words: now %d", len(terms))
        return self.store.all()

    def _patterns(self) -> list[tuple[str, str, re.Pattern[str]]]:
        name = self.user_name()
        if self._terms is None or name != self._name:
            self._name = name
            self._terms = [("your name", t, term_pattern(t)) for t in self.automatic()]
            self._terms += [("one of your private words", t, term_pattern(t)) for t in self.store.all()]
        return self._terms

    def _secrets(self) -> list[str]:
        if not self.env_path or not self.env_path.exists():
            return []
        values = []
        for name, value in dotenv_values(self.env_path, interpolate=False).items():
            for v in (value, os.environ.get(name)):  # as it's set now, which may not yet be what's in the file
                if v and len(v.strip()) >= MIN_SECRET_CHARS:
                    values.append(v.strip())
        return list(dict.fromkeys(values))

    def find(self, texts: list[str], *, personal: bool = True) -> list[Finding]:
        """Everything in ``texts`` that shouldn't leave without you knowing: secrets first, then, if
        ``personal``, personal details."""
        found = []
        secrets = self._secrets()
        for text in texts:
            found += [Finding("one of Athena's secrets, from .env", s, secret=True) for s in secrets if s in text]
            found += [
                Finding("something shaped like an API key or token", m.group(), secret=True)
                for m in _KEY.finditer(text)
            ]
        for text in texts if personal else ():
            for kind, term, pattern in self._patterns():
                if pattern.search(text):
                    found.append(Finding(kind, term))
            found += identifying(text)
        return list(dict.fromkeys(found))

    def check(self, tool: str, args: Any, *, asks: bool) -> Verdict:
        """What's in a call to ``tool`` that shouldn't leave, and whether that stops it. ``asks``: it asks
        you first, so you can be told what it found; tools that don't ask are stopped only by a secret."""
        blocking = matches_any(tool, self.cfg.block)
        found = self.find(list(strings(args)), personal=blocking or asks)
        refused = any(f.secret for f in found) or (blocking and bool(found))
        if found:
            log.warning(
                "%s %s: it had %s in it",
                "Stopped" if refused else "Asking about",
                tool,
                ", ".join(dict.fromkeys(f.kind for f in found)),  # what kind, never what
            )
        return Verdict(tuple(found), refused=refused)

    def close(self) -> None:
        self.store.close()


def refusal(verdict: Verdict) -> str:
    """What the model is told when a call is stopped."""
    if any(f.secret for f in verdict.findings):
        return (
            f"this would send {verdict.describe()}, which must never leave Athena. Don't put secrets in what a "
            "tool sends."
        )
    return (
        f"this would send personal details: {verdict.describe()}. Take them out, keeping only what anyone could "
        "ask, and try again. If it can't be done without them, don't send it: tell the user why."
    )


def warning(verdict: Verdict) -> str:
    """The first line of the approval message, when a call that asks has personal details in it."""
    return f"⚠️ This has personal details in it: {verdict.describe()}. Allow it only if they're meant to go."
