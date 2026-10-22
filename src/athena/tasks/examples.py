"""Tasks to start from, on the dashboard."""

from athena.tasks.scheduler import NOTHING

EXAMPLES = (
    {
        "title": "Morning briefing",
        "schedule": "30 7 * * 1-5",
        "instructions": (
            "Brief me for the day ahead: what's on my calendar and reminders today, anything I've said is due, "
            "and the main news. Short, most important first."
        ),
    },
    {
        "title": "MCP servers worth adding",
        "schedule": "0 10 * * sun",
        "instructions": (
            "Look through what I've asked you about lately (search_memory, in conversations) for things you "
            "couldn't do, or did badly, for want of a tool. Search the MCP Registry for servers that would help. "
            "Suggest at most three you haven't suggested before: what each would let you do for me, who publishes "
            "it, how it runs and whether it needs a key. Prefer known publishers and PyPI packages. If none are "
            f"worth it, say {NOTHING}."
        ),
    },
    {
        "title": "Skills from habits",
        "schedule": "0 18 * * fri",
        "instructions": (
            "Look at what you've done this week with recent_work. Find things you did the same way three or more "
            "times: the same kind of request, handled with the same steps. For each, if none of your skills covers "
            "it, write one with save_skill: when to use it, and the steps that worked, short. If one covers it but "
            "you did it differently or better, improve it. Tell me what you suggested and why. If nothing's worth "
            f"a skill, say {NOTHING}."
        ),
    },
    {
        "title": "Research",
        "schedule": "now",
        "instructions": "Find out … and tell me what you learn, with your sources.",
    },
)
