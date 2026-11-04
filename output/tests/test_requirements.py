"""Tests for requirement parsing and input-file loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement

from pipdown import InputError, load_requirements, parse_requirement


def test_parse_pinned_requirement() -> None:
    requirement = parse_requirement("requests==2.32.3")
    assert requirement.name == "requests"
    assert str(requirement.specifier) == "==2.32.3"


def test_parse_unpinned_and_range() -> None:
    assert parse_requirement("six").name == "six"
    requirement = parse_requirement("idna>=3,<4")
    assert str(requirement.specifier) == "<4,>=3"


@pytest.mark.parametrize(
    "spec",
    [
        "requests[security]==2.32.3",
        "requests; python_version >= '3.8'",
        "https://example.com/requests.whl",
        "not a requirement",
    ],
)
def test_reject_unsupported_requirement_forms(spec: str) -> None:
    with pytest.raises(InputError):
        parse_requirement(spec)


def test_load_requirements_combines_and_deduplicates(tmp_path: Path) -> None:
    input_file = tmp_path / "requirements.txt"
    input_file.write_text(
        "# comment\nsix==1.16.0\n\nidna==3.7  # inline comment\nSix == 1.16.0\n",
        encoding="utf-8",
    )
    requirements = load_requirements(["requests==2.32.3", "six==1.16.0"], input_file)
    assert [str(requirement) for requirement in requirements] == [
        "requests==2.32.3",
        "six==1.16.0",
        "idna==3.7",
    ]


def test_load_requirements_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="cannot read input file"):
        load_requirements([], tmp_path / "missing.txt")


def test_load_requirements_reports_file_line(tmp_path: Path) -> None:
    input_file = tmp_path / "bad.txt"
    input_file.write_text("six\nrequests[security]\n", encoding="utf-8")
    with pytest.raises(InputError, match=r"bad\.txt:2"):
        load_requirements([], input_file)


def test_requirement_type_is_packaging_requirement() -> None:
    assert isinstance(parse_requirement("six"), Requirement)
