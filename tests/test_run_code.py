"""Running Python the model writes: in the strict sandbox, within its limits, keeping only its workspace.

The sandbox itself needs Linux and bubblewrap: those tests are skipped elsewhere, but run on CI, where a
sandbox that can't be made fails them (test_sandbox.py's REAL)."""

import ast
import os
import sys
from pathlib import Path

import pytest
from test_sandbox import REAL, needs_bwrap

from athena.builtin import Unavailable
from athena.run_code import CodeConfig, CodeRunner, Ran, SandboxedPython
from athena.sandbox import Sandbox
from athena.tools import ToolError, Turn, begin_turn
from athena.workspace import MADE, clear_workspace, workspace_of


def runner(tmp_path, **settings) -> CodeRunner:
    cfg = CodeConfig.model_construct(**{**CodeConfig().model_dump(), **settings})  # limits below the usual minimums
    return CodeRunner(SandboxedPython(cfg, REAL, tmp_path / "workspace"))


async def ran(tmp_path, code, **settings) -> Ran:
    return await runner(tmp_path, **settings).run(code)


# -- what it can do ---------------------------------------------------------------------------------------


@needs_bwrap
async def test_it_runs_python_and_says_what_it_printed(tmp_path):
    result = await ran(tmp_path, "import statistics\nprint(statistics.mean([2, 4, 9]))\nprint('done')")
    assert result.code == 0 and result.output == "5\ndone\n"
    assert result.describe().startswith("Finished in ") and result.describe().endswith("5\ndone")
    failed = await ran(tmp_path, "1 / 0")
    assert failed.code == 1 and "ZeroDivisionError" in failed.output and failed.describe().startswith("Failed (exit")


@needs_bwrap
async def test_athenas_packages_are_there_within_the_memory_limit(tmp_path):
    result = await ran(tmp_path, "import numpy, pandas\nprint(pandas.DataFrame({'a': [1, 2, 3]})['a'].sum())")
    assert result.output.strip() == "6", result.output


@needs_bwrap
async def test_its_folder_keeps_files_between_runs_and_says_what_changed(tmp_path):
    first = await ran(tmp_path, "open('sums.csv', 'w').write('a,b\\n1,2\\n')\nimport os\nos.makedirs('out')")
    assert first.changed == ["sums.csv (8 bytes, new)"] and first.made == ["sums.csv"]
    second = await ran(tmp_path, "print(open('sums.csv').read().splitlines()[1])\nimport os\nos.remove('sums.csv')")
    assert second.output == "1,2\n" and second.changed == ["sums.csv (deleted)"] and second.made == []
    assert (tmp_path / "workspace").stat().st_mode & 0o777 == 0o700


@needs_bwrap
async def test_the_files_its_code_writes_are_noted_to_be_sent_if_the_answer_names_them(tmp_path):
    code = "import os\nopen('chart.csv', 'w').write('a')\nos.mkdir('out')\nopen('out/t.txt', 'w').write('b')"
    turn = Turn()
    end = begin_turn(turn)
    try:
        await runner(tmp_path).tools()[0].handler({"code": code})
    finally:
        end()
    assert turn.notes[MADE] == ["chart.csv", "out/t.txt"]


# -- what it can't ----------------------------------------------------------------------------------------


@needs_bwrap
async def test_it_has_no_network(tmp_path):
    code = "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 53), 3)\n"
    code += "except OSError as e:\n    print('no', e)"
    assert (await ran(tmp_path, code)).output.startswith("no")


@needs_bwrap
async def test_it_sees_nothing_of_yours_or_athenas(tmp_path, monkeypatch):
    canary = Path.home() / f".athena-code-check-{os.getpid()}"
    canary.write_text("secret")
    monkeypatch.setenv("ATHENA_TEST_SECRET", "hunter2")
    try:
        code = f"""
import os
print(os.path.exists({str(canary)!r}), os.path.exists({str(Path(__file__).parent)!r}))
print(sorted(os.listdir(os.path.expanduser('~'))))
print('ATHENA_TEST_SECRET' in os.environ, sorted(os.environ))
print([p for p in ('/var/log', '/mnt', '/media', '/srv', '/opt', '/root', '/run') if os.path.exists(p)])
print(len([p for p in os.listdir('/proc') if p.isdigit()]) < 5)
"""
        lines = (await ran(tmp_path, code)).output.splitlines()
    finally:
        canary.unlink()
    assert lines[0] == "False False"  # your home folder's files, or Athena's code
    # Only the way to Athena's Python, if it's in your home folder, as on CI: what's alongside isn't there.
    home = Path.home()
    ways = {
        ".local",
        *(Path(p).relative_to(home).parts[0] for p in (sys.prefix, sys.base_prefix) if home in Path(p).parents),
    }
    assert set(ast.literal_eval(lines[1])) <= ways, lines[1]
    assert lines[2] == "False ['HOME', 'LANG', 'MPLBACKEND', 'PATH', 'PWD', 'PYTHONDONTWRITEBYTECODE']"  # PWD: bwrap's
    assert lines[3] == "[]"  # the system's logs, drives and the like aren't there at all
    assert lines[4] == "True"  # only its own processes


@needs_bwrap
async def test_only_athenas_python_is_there_not_whats_beside_it(tmp_path):
    """On the Pi, Athena's Python is in ~/athena/.venv, beside config.toml and .env: the folder's path is in the
    sandbox, as the way to .venv, but nothing else in it is."""
    beside = Path(sys.prefix).parent / f".athena-beside-check-{os.getpid()}"  # where config.toml would be
    beside.write_text("secret")
    try:
        code = f"import os\nprint(os.path.exists({str(beside)!r}), sorted(os.listdir({str(beside.parent)!r})))"
        said = (await ran(tmp_path, code)).output.strip()
    finally:
        beside.unlink()
    assert said == f"False [{Path(sys.prefix).name!r}]"


@needs_bwrap
async def test_doctor_checks_athenas_own_files_arent_there(config):
    """Even where its folder is the way to its Python, as on the Pi."""
    config.base_dir = Path(sys.prefix).parent
    reported = []
    await CodeRunner.check(config, lambda mark, msg: reported.append(msg))
    assert "Code can't see Athena's files" in reported, reported


@needs_bwrap
async def test_it_can_write_only_in_its_folder_and_its_own_tmp_which_doesnt_last(tmp_path):
    code = """
for path in ('/usr/athena', '/etc/athena', '/tmp/x'):
    try:
        open(path, 'w').write('x')
        print(path, 'written')
    except OSError as e:
        print(path, type(e).__name__)
"""
    assert (await ran(tmp_path, code)).output.splitlines() == [
        "/usr/athena OSError",
        "/etc/athena OSError",
        "/tmp/x written",
    ]
    assert (await ran(tmp_path, "import os\nprint(os.path.exists('/tmp/x'))")).output == "False\n"


@needs_bwrap
async def test_a_link_it_makes_to_something_outside_is_listed_not_followed(tmp_path):
    result = await ran(tmp_path, "import os\nos.symlink('/etc/passwd', 'passwd')")
    assert result.changed[0].startswith("passwd (") and (tmp_path / "workspace" / "passwd").is_symlink()


# -- its limits -------------------------------------------------------------------------------------------


@needs_bwrap
async def test_it_is_stopped_when_it_runs_too_long(tmp_path):
    result = await ran(tmp_path, "while True:\n    pass", timeout_seconds=1.5)
    assert result.timed_out and result.describe().startswith("Stopped after 1.5 seconds, its limit.")
    assert 1.4 < result.seconds < 10


@needs_bwrap
async def test_it_cant_use_more_than_its_memory_or_write_a_file_too_big(tmp_path):
    memory = await ran(tmp_path, "x = bytearray(400 * 1024 * 1024)\nprint('allocated')", memory_mb=256)
    assert "MemoryError" in memory.output and "allocated" not in memory.output
    big = await ran(tmp_path, "open('big', 'wb').write(b'x' * 2 * 1024 * 1024)", max_file_mb=1)
    assert "File too large" in big.output  # an error it can handle, not killed without a word


@needs_bwrap
async def test_it_cant_raise_its_own_limits(tmp_path):
    code = "import resource\nresource.setrlimit(resource.RLIMIT_AS, (-1, -1))\nprint('raised')"
    result = await ran(tmp_path, code, memory_mb=512)
    assert "raised" not in result.output and ("ValueError" in result.output or "Permission" in result.output)


@needs_bwrap
async def test_what_it_prints_is_cut_short(tmp_path):
    result = await ran(tmp_path, "print('x' * 100_000)\nprint('end')", max_output_chars=1000)
    assert result.cut and len(result.output) == 1000
    assert "[cut short: only the first 1,000 characters are shown]" in result.describe()


@needs_bwrap
async def test_a_full_folder_can_only_be_tidied(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old.bin").write_bytes(b"x" * 2 * 1024 * 1024)
    full = await ran(tmp_path, "open('new', 'w').write('x')", workspace_mb=1)
    assert full.full and "File too large" in full.output and "delete files that aren't needed" in full.describe()
    tidied = await ran(tmp_path, "import os\nos.remove('old.bin')\nos.remove('new')", workspace_mb=1)
    assert tidied.code == 0 and tidied.changed[0] == "new (deleted)"
    assert not (await ran(tmp_path, "open('new', 'w').write('x')", workspace_mb=1)).full


@needs_bwrap
async def test_doctor_checks_it(config):
    reported = []
    config.code.enabled = True
    await CodeRunner.check(config, lambda mark, msg: reported.append(msg))
    assert reported == ["Code runs Python", "Code can't connect out", "Code can't see Athena's files"]


# -- without a sandbox, and the rest ---------------------------------------------------------------------


def test_without_a_sandbox_there_is_no_code_tool(config, monkeypatch):
    monkeypatch.setattr(Sandbox, "detect", classmethod(lambda cls: Sandbox(None, Path.home())))
    assert CodeRunner.wanted(config) is (sys.platform == "linux")
    with pytest.raises(Unavailable, match="needs a sandbox"):
        CodeRunner.start(config)


async def test_empty_or_huge_code_isnt_run(tmp_path):
    code_runner = CodeRunner(SandboxedPython(CodeConfig(), Sandbox(None, Path.home()), tmp_path))
    for code, why in (("  ", "Give the code"), ("x" * 50_001, "more than 50,000 characters")):
        with pytest.raises(ToolError, match=why):
            await code_runner.run(code)


def test_the_limits_are_set_before_the_code_runs(tmp_path):
    launcher = SandboxedPython(
        CodeConfig(memory_mb=300, max_file_mb=2), Sandbox(None, Path.home()), tmp_path
    )._launcher()
    assert "(resource.RLIMIT_AS, 314572800)" in launcher and "(resource.RLIMIT_FSIZE, 2097152)" in launcher
    assert launcher.index("setrlimit") < launcher.index("exec(")
    assert "(resource.RLIMIT_FSIZE, 0)" in SandboxedPython(CodeConfig(), None, tmp_path)._launcher(writes=False)


def test_how_a_run_is_described():
    cfg = CodeConfig()
    assert Ran("", None, True, False, 30.2, [], False, cfg).describe().startswith("Stopped after 30 seconds")
    killed = Ran("", -9, False, False, 1.0, [], False, cfg).describe()
    assert killed.startswith("Stopped by signal 9: it may have run out of memory or CPU time.")
    said = Ran("", 0, False, False, 0.3, ["a.csv (8 bytes, new)"], False, cfg).describe()
    assert said == "Finished in 0.3s.\n(it printed nothing)\nFiles in its folder that changed: a.csv (8 bytes, new)"


def test_forgetting_everything_empties_the_workspace(config):
    workspace = workspace_of(config)
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "notes.csv").write_text("private")
    clear_workspace(config)
    assert not workspace.exists()
