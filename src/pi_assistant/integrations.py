"""The built-in integrations, in the order the dashboard shows them. To add one, see builtin.py."""

from __future__ import annotations

from pi_assistant.builtin import Builtin
from pi_assistant.databases import Databases
from pi_assistant.news import NewsReader
from pi_assistant.trading212 import Trading212

BUILTINS: tuple[type[Builtin], ...] = (NewsReader, Trading212, Databases)
