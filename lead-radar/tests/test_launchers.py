"""Guard the Windows launchers against the classic "不是内部或外部命令" breakages:
LF line endings, UTF-8 BOM / Chinese text in .bat files, and programs that rely on PATH."""
import re
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
BATS = sorted(ROOT.glob("*.bat"))
CMD_BUILTINS = {"@echo", "echo", "setlocal", "endlocal", "cd", "title", "set", "if", "exit", "pause",
                "goto", "rem", "pushd", "popd", "call"}


def test_expected_launchers_exist():
    assert {p.name for p in BATS} >= {"start.bat", "stop.bat", "run-server.bat", "install-browser.bat"}


@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_bat_bytes_are_safe_for_cmd(bat):
    data = bat.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "BOM makes cmd run '﻿@echo' as a command"
    assert all(b < 128 for b in data), "Chinese/UTF-8 text is misread under the GBK code page"
    assert not re.search(rb"(?<!\r)\n", data), "every line must end with CRLF"
    assert data.endswith(b"\r\n")


@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_bat_structure(bat):
    lines = bat.read_bytes().decode("ascii").split("\r\n")
    labels = {l.strip()[1:].lower() for l in lines if l.strip().startswith(":")}
    for target in re.findall(r"goto\s+:?(\w+)", "\n".join(lines), re.I):
        assert target.lower() in labels | {"eof"}, f"goto {target} has no label"
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith(":"):
            continue
        first = s.split()[0].lower()
        # external programs only through a quoted full path variable or the venv
        assert first in CMD_BUILTINS or first.startswith('"%') or first.startswith('".venv'), \
            f"line {n}: '{first}' relies on PATH"
        assert "echo." not in s.lower(), f"line {n}: 'echo.' breaks if a file named echo exists"
        if "%~dp0" in s:
            assert '"%~dp0' in s, f"line {n}: %~dp0 must be quoted (spaces, &, parentheses in folder names)"


def test_powershell_launcher_encoding():
    data = (ROOT / "start.ps1").read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), "Windows PowerShell 5.1 needs a BOM to read Chinese text"
    assert not re.search(rb"(?<!\r)\n", data)
    text = data.decode("utf-8-sig")
    for ps7_only in ("??", "?.", "&&", "||", "::new("):
        assert ps7_only not in text, f"{ps7_only!r} is not supported by Windows PowerShell 5.1"


def test_start_bat_calls_powershell_by_full_path():
    text = (ROOT / "start.bat").read_text(encoding="ascii")
    assert r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" in text
    assert '-ExecutionPolicy Bypass -File "%~dp0start.ps1"' in text
