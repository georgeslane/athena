"""The MCP servers Athena recommends, which the dashboard's Tools tab offers to set up: what each does, its
settings and keys, and the section config.toml gets for it. config.example.toml has the same sections (tests
check they agree)."""

from __future__ import annotations

from athena.catalog import Field, Integration, Secret

_MAC_SETUP = "Runs on your Mac, which needs setting up first: see docs/mac.md."

RECOMMENDED: tuple[Integration, ...] = (
    Integration(
        "time",
        "Time",
        "Tells the time anywhere, and converts between time zones.",
        "mcp_servers.time",
        defaults={
            "command": "uvx",
            "args": ["mcp-server-time==2026.8.18", "--local-timezone=Europe/London"],
            "confirm": [],
            "network": False,
        },
        asks="Never asks: it's local and only reads the clock.",
    ),
    Integration(
        "fetch",
        "Web pages",
        "Reads web pages.",
        "mcp_servers.fetch",
        defaults={"command": "uvx", "args": ["mcp-server-fetch==2026.8.18"]},
        asks="Every fetch asks first: a URL can carry your data to any website.",
    ),
    Integration(
        "search",
        "Web search",
        "Searches the web through DuckDuckGo, with no account.",
        "mcp_servers.search",
        defaults={"enabled": False, "command": "uvx", "args": ["duckduckgo-mcp-server==0.7.0"], "include": ["search"]},
        asks="Every search asks first, because what you search for leaves your network.",
    ),
    Integration(
        "apple",
        "Calendar and Reminders",
        "Your Mac's Calendar and Reminders, through iMCP.",
        "mcp_servers.apple",
        defaults={
            "enabled": False,
            "command": "ssh",
            "args": ["athena-mac", "apple"],
            "include": [
                "calendars_list",
                "events_fetch",
                "events_create",
                "reminders_lists",
                "reminders_fetch",
                "reminders_create",
            ],
            "confirm": ["events_create", "reminders_create"],
            "about": "The user's own calendars and reminders, on their Mac.",
        },
        setup=_MAC_SETUP,
        asks="Adding events and reminders asks first.",
    ),
    Integration(
        "github",
        "GitHub",
        "Reads public GitHub repositories, through GitHub's own server.",
        "mcp_servers.github",
        secrets=(
            Secret(
                "GITHUB_TOKEN",
                "GitHub token",
                "A fine-grained token with only “Public repositories (read-only)” access, and an expiry date.",
                path="headers.Authorization",
                template="Bearer ${GITHUB_TOKEN}",
            ),
        ),
        defaults={
            "enabled": False,
            "url": "https://api.githubcopilot.com/mcp/readonly",
            "headers": {
                "Authorization": "Bearer ${GITHUB_TOKEN}",
                "X-MCP-Lockdown": "true",
                "X-MCP-Tools": "search_repositories,get_file_contents,get_latest_release,list_commits,search_issues",
            },
            "include": [
                "search_repositories",
                "get_file_contents",
                "get_latest_release",
                "list_commits",
                "search_issues",
            ],
            "confirm": [],
        },
        asks="Never asks: it only reads public repositories, and lookups go only to GitHub.",
    ),
    Integration(
        "sec",
        "Company filings",
        "Searches and reads filings to the SEC: annual reports, financial statements and insider trades.",
        "mcp_servers.sec",
        secrets=(
            Secret(
                "EDGAR_IDENTITY",
                "Your name and email",
                "The SEC asks who's asking, like: Jane Doe jane@example.com",
                path="env.EDGAR_IDENTITY",
                template="${EDGAR_IDENTITY}",
            ),
        ),
        defaults={
            "enabled": False,
            "command": "uvx",
            "args": ["--from", "edgartools[ai]==5.60.0", "edgartools-mcp"],
            "env": {"EDGAR_IDENTITY": "${EDGAR_IDENTITY}"},
            "include": ["edgar_company", "edgar_search", "edgar_filing", "edgar_read", "edgar_text_search"],
            "confirm": [],
        },
        setup=(
            'Install it on the Pi first, which takes a while: uvx --from "edgartools[ai]==5.60.0" edgartools-mcp --help'
        ),
        asks="Never asks: filings are public, and lookups go only to the SEC.",
    ),
    Integration(
        "email",
        "Email",
        "An email address of Athena's own. It reads that inbox, and sends only to addresses you allow.",
        "mcp_servers.email",
        fields=(
            Field("env.MCP_EMAIL_SERVER_EMAIL_ADDRESS", "Athena's address", required=True, example=True),
            Field("env.MCP_EMAIL_SERVER_USER_NAME", "Login", "Usually the address.", required=True, example=True),
            Field("env.MCP_EMAIL_SERVER_IMAP_HOST", "IMAP server", required=True, example=True),
            Field("env.MCP_EMAIL_SERVER_IMAP_PORT", "IMAP port"),
            Field("env.MCP_EMAIL_SERVER_SMTP_HOST", "SMTP server", required=True, example=True),
            Field("env.MCP_EMAIL_SERVER_SMTP_PORT", "SMTP port"),
            Field(
                "env.MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS",
                "Who it may email",
                "Comma-separated, and * matches anything. The server refuses anyone else, even if you approve.",
                required=True,
                example=True,
            ),
        ),
        secrets=(
            Secret(
                "EMAIL_PASSWORD",
                "App password",
                "An app password for Athena's own account, not your main password.",
                path="env.MCP_EMAIL_SERVER_PASSWORD",
                template="${EMAIL_PASSWORD}",
            ),
        ),
        defaults={
            "enabled": False,
            "command": "uvx",
            "args": ["mcp-email-server==1.11.0", "stdio"],
            "include": ["list_emails_metadata", "get_emails_content", "send_email"],
            "confirm": ["send_email"],
            "arguments": {"account_name": "athena"},
            "about": "Your own inbox, not the user's: they forward you mail they want you to read or keep an eye on.",
            "env": {
                "MCP_EMAIL_SERVER_ACCOUNT_NAME": "athena",
                "MCP_EMAIL_SERVER_FULL_NAME": "Athena",
                "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "athena@example.com",
                "MCP_EMAIL_SERVER_USER_NAME": "athena@example.com",
                "MCP_EMAIL_SERVER_PASSWORD": "${EMAIL_PASSWORD}",
                "MCP_EMAIL_SERVER_IMAP_HOST": "imap.example.com",
                "MCP_EMAIL_SERVER_IMAP_PORT": "993",
                "MCP_EMAIL_SERVER_IMAP_SSL": "true",
                "MCP_EMAIL_SERVER_SMTP_HOST": "smtp.example.com",
                "MCP_EMAIL_SERVER_SMTP_PORT": "465",
                "MCP_EMAIL_SERVER_SMTP_SSL": "true",
                "MCP_EMAIL_SERVER_ALLOWED_MUTATIONS": "send",
                "MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS": "you@example.com",
            },
        },
        asks="Sending asks first. It can never delete, move or flag mail.",
    ),
)
