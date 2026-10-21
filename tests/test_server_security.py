"""Security updates: checking the MCP servers' dependencies every day, fixing what can be, and saying so."""

from types import SimpleNamespace

from pi_assistant.config import SecurityUpdatesConfig
from pi_assistant.server_envs import Audit, Finding, Fixed
from pi_assistant.server_security import ServerSecurity

CERTIFI = Finding("certifi", "2026.7.22", ("GHSA-6q48", "PYSEC-4320"), "certifi bundles emSign roots", "2026.10.19")
IDNA = Finding("idna", "2.7", ("PYSEC-1",), "Denial of service", None)
VULNERABLE = Audit(False, "Found 2 known vulnerabilities in 33 packages", ["certifi 2026.7.22"], [CERTIFI])
CLEAN = Audit(True, "Found no known vulnerabilities in 33 packages")


class Envs:
    """The servers' environments: time and fetch have certifi to fix; sec has idna, with no fix yet."""

    def __init__(self, broken=False):
        self.audits = {
            "time": VULNERABLE,
            "fetch": VULNERABLE,
            "sec": Audit(False, "Found 1 known vulnerability", ["idna 2.7"], [IDNA]),
        }
        self.updated: list[str] = []
        self.broken = broken

    async def audit(self, name):
        return self.audits[name]

    async def fix(self, name):
        before = self.audits[name]
        if self.broken and name == "time":
            return Fixed(before, before, error="Couldn't update certifi 2026.10.19: no network")
        if not any(f.fixed for f in before.findings):
            return Fixed(before, before)
        self.updated.append(name)
        self.audits[name] = CLEAN
        return Fixed(before, CLEAN, [("certifi", "2026.7.22", "2026.10.19")])

    def fixed(self, name):
        return ["certifi 2026.7.22 -> 2026.10.19"] if name in self.updated else []


def make(automatic=True, broken=False):
    statuses = [
        SimpleNamespace(name="time", lock="reviewed"),
        SimpleNamespace(name="fetch", lock="first use"),
        SimpleNamespace(name="sec", lock="reviewed"),
        SimpleNamespace(name="apple", lock=None),  # over ssh: nothing Athena locks
    ]
    mcp = SimpleNamespace(status=lambda: statuses, envs=Envs(broken))
    said, restarted = [], []

    async def tell(text):
        said.append(text)

    async def restart(names):
        restarted.append(names)

    security = ServerSecurity(SecurityUpdatesConfig(automatic=automatic), mcp, tell, restart)
    return security, mcp.envs, said, restarted


async def test_whatever_can_be_fixed_is_and_you_hear_what_and_why():
    security, envs, said, restarted = make()
    assert security.locked() == ["time", "fetch", "sec"]
    await security.check()
    assert envs.updated == ["time", "fetch"] and restarted == [{"time", "fetch"}]  # restarted together, once
    assert said == [
        "🛡️ I updated certifi 2026.7.22 → 2026.10.19 for the “time” server, to fix known vulnerabilities "
        "(GHSA-6q48, PYSEC-4320: certifi bundles emSign roots). It's restarted.",
        "🛡️ I updated certifi 2026.7.22 → 2026.10.19 for the “fetch” server, to fix known vulnerabilities "
        "(GHSA-6q48, PYSEC-4320: certifi bundles emSign roots). It's restarted.",
        "⚠️ The “sec” server uses idna 2.7, which has a known vulnerability with no fixed version yet "
        "(PYSEC-1: Denial of service). Switch the server off on the Tools tab if that worries you.",
    ]
    assert security.results["time"]["clean"] is True and security.results["time"]["fixed"] == [
        "certifi 2026.7.22 -> 2026.10.19"
    ]
    assert security.results["sec"]["findings"][0]["fixed"] is None

    said.clear()
    await security.check()  # the next day: nothing new to say, and nothing to restart
    assert said == [] and restarted == [{"time", "fetch"}]


async def test_when_its_not_automatic_you_hear_once_and_press_fix():
    security, envs, said, restarted = make(automatic=False)
    await security.check()
    assert envs.updated == [] and restarted == []
    assert said[0] == (
        "⚠️ The “time” server uses certifi 2026.7.22, which has a known vulnerability (GHSA-6q48, PYSEC-4320: "
        "certifi bundles emSign roots). 2026.10.19 fixes it: press Fix on the server in the dashboard's Tools tab."
    )
    assert len(said) == 3
    await security.check()
    assert len(said) == 3  # said once

    await security.check(["time"], fix=True)  # Fix, on the dashboard
    assert envs.updated == ["time"] and restarted == [{"time"}]
    assert said[-1].startswith("🛡️ I updated certifi 2026.7.22 → 2026.10.19 for the “time” server")


async def test_a_fix_that_fails_is_said_once_and_tried_again_next_time():
    security, envs, said, restarted = make(broken=True)
    await security.check(["time"])
    assert said == [
        "⚠️ The “time” server has a known vulnerability, and I couldn't update it: "
        "Couldn't update certifi 2026.10.19: no network"
    ]
    assert restarted == [] and security.results["time"]["error"].endswith("no network")
    await security.check(["time"])
    assert len(said) == 1
    envs.broken = False
    await security.check(["time"])
    assert envs.updated == ["time"] and restarted == [{"time"}]


async def test_nothing_is_checked_without_locked_servers():
    security, envs, said, restarted = make()
    security.mcp.envs = None
    await security.check()
    assert said == [] and security.results == {}
