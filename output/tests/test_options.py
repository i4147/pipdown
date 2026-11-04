"""Tests for runtime option validation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from pipdown import BackendName, InputError, Options, download_packages


@pytest.mark.parametrize(
    "options",
    [
        Options(jobs=0),
        Options(timeout=0),
        Options(retries=-1),
        Options(retry_delay=-1),
        Options(progress_delay=-1),
        Options(max_size=0),
        Options(backend=cast(BackendName, "wget")),
        Options(no_binary=True, only_binary=True),
    ],
)
def test_invalid_options_are_rejected(options: Options, tmp_path: Path) -> None:
    with pytest.raises(InputError):
        download_packages(["demo"], options, destination=tmp_path)


def test_empty_requirements_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="no package requirements"):
        download_packages([], Options(), destination=tmp_path)


def test_missing_destination_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="destination is not a directory"):
        download_packages(["demo"], Options(), destination=tmp_path / "missing")
