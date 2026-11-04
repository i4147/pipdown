"""Tests for the command line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipdown import main
from tests.conftest import LocalServer


def test_cli_input_file_downloads_to_current_directory(
    local_find_links: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    input_file = tmp_path / "requirements.txt"
    input_file.write_text("# demo\n demo==1.0  # pinned\n", encoding="utf-8")
    exit_code = main(["-i", str(input_file), "--find-links", str(local_find_links), "--json"])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output[0]["status"] == "downloaded"
    assert output[0]["filename"] == "demo-1.0.tar.gz"
    assert (tmp_path / "demo-1.0.tar.gz").is_file()


def test_cli_dry_run_does_not_download(
    local_find_links: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code = main(["demo", "--find-links", str(local_find_links), "--dry-run"])
    assert exit_code == 0
    assert "DRY demo" in capsys.readouterr().out
    assert not (tmp_path / "demo-1.0.tar.gz").exists()


def test_cli_missing_package_returns_failure(
    http_server: LocalServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code = main(["missing", "--index-url", f"{http_server.url}/simple/", "--json"])
    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output[0]["status"] == "failed"


def test_cli_invalid_requirement_returns_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["requests[security]"]) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_no_requirements_returns_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_cli_mutually_exclusive_binary_flags() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["demo", "--no-binary", "--only-binary"])
    assert exc_info.value.code == 2
