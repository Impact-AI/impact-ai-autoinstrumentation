# SPDX-License-Identifier: Apache-2.0

"""The documented steps for giving the key yourself (docs/install.md), run the way a person runs them: pasted into bash
in a terminal and the key pasted at the prompt.

The key must not reach the screen (the output of an install gets copied into chats and tickets), the shell's history,
or the arguments or the environment of a command, which other users of the machine can read. The page's block runs
here in an interactive bash on a pseudo-terminal, pasted the way a terminal without bracketed paste delivers it, line by
line. A stand-in for `helm` records what it was given, and nothing else is on the PATH, so the block can start no other
program.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import termios
import time

import pytest
from test_chart import CHART

README = (CHART.parents[1] / "docs" / "install.md").read_text()
BASH = shutil.which("bash")
KEY = "impact_test_PastedAtThePrompt_0123456789abcdefghijKLMNOPQ"
PROMPT = "impact.ai ingest key: "
PLACEHOLDERS = {"<product>": "prod_test", "<ingest origin, no path>": "https://ingest.example.test"}
TIMEOUT = 30.0
STAND_IN = """#!{python}
import json, os, sys
with open({record!r}, "w") as out:
    json.dump({{"arguments": sys.argv[1:], "stdin": sys.stdin.read(), "environment": dict(os.environ)}}, out)
print("helm: recorded")
"""


def key_block() -> str:
    """The page's shell block that asks for the key, with its placeholders filled in."""
    (block,) = [b for b in re.findall(r"```sh\n(.*?)```", README, re.S) if "read -rs" in b]
    for placeholder, value in PLACEHOLDERS.items():
        block = block.replace(placeholder, value)
    assert "<" not in block, "a placeholder the test does not fill in"
    return block


class Terminal:
    """An interactive bash on a pseudo-terminal: what is typed into it, and everything it shows."""

    def __init__(self, environment: dict[str, str]):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # the child: wide enough that no line of the block wraps, then bash
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 250, 0, 0))
                os.execve(BASH, [BASH, "--norc", "--noprofile", "-i"], environment)  # noqa: S606
            finally:
                os._exit(127)
        self.screen = ""

    def type(self, text: str) -> None:
        os.write(self.fd, text.encode())

    def read(self, timeout: float) -> bool:
        """Add what the terminal shows within the timeout to the screen; False once bash has gone."""
        if not select.select([self.fd], [], [], timeout)[0]:
            return True
        try:
            data = os.read(self.fd, 65536)
        except OSError:  # EIO: the other end is closed
            return False
        self.screen += data.decode(errors="replace")
        return bool(data)

    def wait_for(self, shown) -> None:
        deadline = time.monotonic() + TIMEOUT
        while not shown(self.screen):
            remaining = deadline - time.monotonic()
            assert remaining > 0 and self.read(remaining), f"the terminal did not show it:\n{self.screen[-3000:]}"

    def echoes(self) -> bool:
        """Whether the terminal shows what is typed into it (the master reads the settings of the other end)."""
        return bool(termios.tcgetattr(self.fd)[3] & termios.ECHO)

    def wait_until_silent(self) -> None:
        deadline = time.monotonic() + TIMEOUT
        while self.echoes():
            assert time.monotonic() < deadline, f"the terminal still shows what is typed:\n{self.screen[-3000:]}"
            self.read(0.01)

    def wait_for_exit(self) -> None:
        deadline = time.monotonic() + TIMEOUT
        while self.read(max(deadline - time.monotonic(), 0)):
            assert time.monotonic() < deadline, f"bash did not exit:\n{self.screen[-3000:]}"
        os.waitpid(self.pid, 0)
        self.pid = 0

    def close(self) -> None:
        if self.pid:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        os.close(self.fd)


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_the_key_is_read_at_a_prompt_that_shows_nothing_and_reaches_helm_on_standard_input_alone(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "helm.json"
    helm = bin_dir / "helm"
    helm.write_text(STAND_IN.format(python=sys.executable, record=str(record)))
    helm.chmod(0o755)
    history = tmp_path / "history"
    terminal = Terminal(
        {
            "PATH": str(bin_dir),  # the stand-in and nothing else: printf and read are bash's own
            "HOME": str(tmp_path),
            "HISTFILE": str(history),
            "INPUTRC": "/dev/null",
            "TERM": "dumb",
            "PS1": "$ ",
            "PS2": "> ",
        }
    )
    try:
        terminal.type(key_block())
        # Bash has read the whole block before it asks: the prompt comes after the echo of the block's last line.
        terminal.wait_for(lambda screen: screen.endswith(PROMPT) and screen.rfind("/dev/stdin") < screen.rfind(PROMPT))
        # The prompt waits with the terminal's echo off. (read -s turns it off just after the prompt is printed, so a
        # key typed in those microseconds would show: the test pastes it when a person can, once the prompt waits.)
        terminal.wait_until_silent()
        terminal.type(KEY + "\n")
        terminal.wait_for(lambda screen: "helm: recorded" in screen)
        terminal.type('echo "after the block the key is ${key-unset}"\n')
        terminal.wait_for(lambda screen: "after the block the key is unset" in screen)
        terminal.type("history -w\nexit\n")
        terminal.wait_for_exit()
    finally:
        terminal.close()

    assert KEY not in terminal.screen, "the key was shown on the screen"
    assert "command not found" not in terminal.screen, "the block starts a program other than helm"
    given = json.loads(record.read_text())
    assert given["stdin"] == KEY + "\n", "helm reads the key, and nothing else, on standard input"
    arguments = given["arguments"]
    assert arguments[arguments.index("--set-file") + 1] == "ingest.key=/dev/stdin"
    assert [a for a in arguments if KEY in a] == []
    assert [name for name, value in given["environment"].items() if KEY in value] == []
    kept = history.read_text()
    assert "read -rs key" in kept, "the history holds the block, so the next line can fail"
    assert KEY not in kept
