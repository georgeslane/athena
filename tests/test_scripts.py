"""The scripts that come with Athena (resources/scripts/), run with stand-ins for sudo, systemctl, ollama,
curl and athena itself, which record what they were asked to do instead of changing the machine. The Mac's
scripts are in test_mac_scripts.py."""

import shutil
import subprocess

import pytest

from athena.resources import SCRIPTS

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

RECORD = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
STUBS = {
    # Ollama's installer, piped to sh, "updates" Ollama: it stops being too old.
    "curl": RECORD + 'case "$*" in *ollama.com/install.sh*) echo "rm -f \'$STUB_FILES/ollama-too-old\'";; esac\n',
    # While $STUB_FILES/ollama-too-old exists, pulls fail.
    "ollama": RECORD
    + 'if [ "$1" = pull ] && [ -e "$STUB_FILES/ollama-too-old" ]; then\n'
    + '  echo "Error: pull model manifest: 412: this model requires a newer version of Ollama"; exit 1\nfi\n'
    + 'if [ "$1" = pull ] && [ "$2" = missing ]; then echo "Error: file does not exist"; exit 1; fi\n',
    # `athena embeddings where` says EMBEDDINGS_FROM, Ollama on the Pi unless it's set.
    "athena": RECORD
    + 'if [ "$2" = where ]; then echo "${EMBEDDINGS_FROM:-ollama}"; exit 0; fi\nexit "${ATHENA_EXIT:-0}"\n',
    "sudo": RECORD,
}


@pytest.fixture
def sandbox(tmp_path):
    stubs, files, home = (tmp_path / d for d in ("stubs", "written", "home"))
    for d in (stubs, files, home):
        d.mkdir()
    for name, script in STUBS.items():
        (stubs / name).write_text(script)
        (stubs / name).chmod(0o755)
    log = tmp_path / "log"
    log.touch()
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "HOME": str(home),
        "STUB_LOG": str(log),
        "STUB_FILES": str(files),
        "ATHENA": str(stubs / "athena"),  # as `athena embeddings switch` sets it
    }

    def run(script, *args, answer="", **extra_env):
        result = subprocess.run(
            ["bash", str(SCRIPTS / script), *args],
            cwd=tmp_path,
            env={**env, **extra_env},
            input=answer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result, log.read_text().splitlines()

    return files, run


# -- switch-embeddings.sh --------------------------------------------------------------------------------


def test_switching_the_embeddings_model(sandbox):
    _, run = sandbox
    result, calls = run("switch-embeddings.sh", "new-model")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "athena embeddings where",
        "ollama pull new-model",
        "sudo systemctl stop athena",
        "athena embeddings use new-model",
        "sudo systemctl restart ollama",  # lets go of the old model
        "sudo systemctl start athena",
    ]


def test_athena_starts_again_if_switching_fails(sandbox):
    _, run = sandbox
    result, calls = run("switch-embeddings.sh", "new-model", ATHENA_EXIT="1")
    assert result.returncode == 1
    assert calls[-2:] == ["athena embeddings use new-model", "sudo systemctl start athena"]
    assert "sudo systemctl restart ollama" not in calls


def test_a_model_that_wont_download_changes_nothing(sandbox):
    _, run = sandbox
    result, calls = run("switch-embeddings.sh", "missing")
    assert result.returncode == 1 and "nothing was changed" in result.stderr
    assert calls == ["athena embeddings where", "ollama pull missing"]  # Athena wasn't stopped


@pytest.mark.parametrize("answer", ["y\n", "n\n", ""])
def test_an_ollama_too_old_for_the_model_is_updated_if_you_say_so(sandbox, answer):
    files, run = sandbox
    (files / "ollama-too-old").touch()
    result, calls = run("switch-embeddings.sh", "new-model", answer=answer)
    if answer == "y\n":
        assert result.returncode == 0, result.stderr
        assert calls[1:4] == [
            "ollama pull new-model",
            "curl -fsSL https://ollama.com/install.sh",
            "curl -sf http://127.0.0.1:11434/api/version",
        ]
        assert calls[4:6] == ["ollama pull new-model", "sudo systemctl stop athena"]
    else:
        assert result.returncode == 1 and "Nothing was changed" in result.stderr
        assert calls == ["athena embeddings where", "ollama pull new-model"]


def test_switching_to_a_model_on_the_mac(sandbox):
    _, run = sandbox
    result, calls = run("switch-embeddings.sh", "embeddinggemma-2-bf16", "--on-mac")
    assert result.returncode == 0, result.stderr
    assert calls == [  # nothing to download on the Pi, and Ollama there is left as it is
        "sudo systemctl stop athena",
        "athena embeddings use embeddinggemma-2-bf16 --on-mac",
        "sudo systemctl start athena",
    ]


def test_switching_models_on_the_mac(sandbox):
    """Embeddings already from the Mac, such as bf16 to 8-bit there: nothing for Ollama to download."""
    _, run = sandbox
    result, calls = run("switch-embeddings.sh", "embeddinggemma-2-8bit", EMBEDDINGS_FROM="server")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "athena embeddings where",
        "sudo systemctl stop athena",
        "athena embeddings use embeddinggemma-2-8bit",
        "sudo systemctl start athena",
    ]


def test_switching_needs_one_model(sandbox):
    _, run = sandbox
    for args in [(), ("a", "b"), ("--on-mac",), ("a", "--other")]:
        result, calls = run("switch-embeddings.sh", *args)
        assert result.returncode == 2 and "Usage" in result.stderr and calls == []
