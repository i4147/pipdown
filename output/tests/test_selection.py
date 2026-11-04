"""Tests for simple-index parsing and distribution selection."""

from __future__ import annotations

import json

from packaging.requirements import Requirement

from pipdown import (
    Link,
    TargetPython,
    parse_html_page,
    parse_json_page,
    select_candidates,
)

TARGET = TargetPython(
    python_version=(3, 11),
    implementation="cp",
    platforms=("linux_x86_64", "manylinux2014_x86_64", "manylinux_2_17_x86_64"),
)


def link(
    filename: str,
    *,
    requires_python: str | None = None,
    yanked: bool = False,
    hashes: dict[str, str] | None = None,
) -> Link:
    """Create a link with a deterministic example URL."""
    return Link(
        url=f"https://example.com/{filename}",
        filename=filename,
        requires_python=requires_python,
        yanked=yanked,
        hashes=hashes if hashes is not None else {},
    )


def test_parse_json_page_with_hashes_and_metadata() -> None:
    content = json.dumps(
        {
            "files": [
                {
                    "filename": "demo-1.0.tar.gz",
                    "url": "https://files.example.com/demo-1.0.tar.gz#sha256=abc",
                    "requires-python": ">=3.8",
                    "yanked": False,
                    "hashes": {"sha256": "def"},
                }
            ]
        }
    ).encode()
    links = parse_json_page(content, "https://example.com/simple/demo/")
    assert len(links) == 1
    assert links[0].filename == "demo-1.0.tar.gz"
    assert links[0].requires_python == ">=3.8"
    assert links[0].hashes["sha256"] == "def"


def test_parse_html_page_with_base_and_yanked() -> None:
    content = """
    <html><base href="https://files.example.com/">
    <a href="demo-1.0.tar.gz#sha256=abc" data-requires-python=">=3.8">demo</a>
    <a href="demo-0.9.tar.gz" data-yanked>old</a>
    </html>
    """
    links = parse_html_page(content, "https://example.com/simple/demo/")
    assert links[0].url == "https://files.example.com/demo-1.0.tar.gz#sha256=abc"
    assert links[0].hashes["sha256"] == "abc"
    assert links[0].requires_python == ">=3.8"
    assert links[1].yanked is True


def test_source_preferred_within_newest_version() -> None:
    links = [
        link("demo-2.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
        link("demo-2.0.tar.gz"),
        link("demo-1.0.tar.gz"),
    ]
    candidates = select_candidates(links, Requirement("demo"), TARGET)
    assert candidates[0].link.filename == "demo-2.0.tar.gz"
    assert candidates[0].kind == "sdist"


def test_newer_wheel_preferred_over_older_source() -> None:
    links = [
        link("demo-2.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
        link("demo-1.0.tar.gz"),
    ]
    candidates = select_candidates(links, Requirement("demo"), TARGET)
    assert candidates[0].link.filename == "demo-2.0-cp311-cp311-manylinux_2_17_x86_64.whl"
    assert candidates[0].kind == "wheel"


def test_source_extension_preference() -> None:
    links = [
        link("demo-1.0.tar.bz2"),
        link("demo-1.0.zip"),
        link("demo-1.0.tar.gz"),
    ]
    candidates = select_candidates(links, Requirement("demo==1.0"), TARGET)
    assert [candidate.link.filename for candidate in candidates] == [
        "demo-1.0.tar.gz",
        "demo-1.0.zip",
        "demo-1.0.tar.bz2",
    ]


def test_incompatible_wheels_are_rejected() -> None:
    links = [
        link("demo-1.0-cp310-cp310-manylinux_2_17_x86_64.whl"),
        link("demo-1.0-cp311-cp311-win_amd64.whl"),
        link("demo-1.0-cp311-cp311-macosx_11_0_arm64.whl"),
    ]
    assert select_candidates(links, Requirement("demo"), TARGET) == []


def test_pure_and_abi3_wheels_are_compatible() -> None:
    links = [
        link("demo-1.0-py3-none-any.whl"),
        link("demo-1.0-cp39-abi3-manylinux_2_17_x86_64.whl"),
    ]
    candidates = select_candidates(links, Requirement("demo"), TARGET)
    assert len(candidates) == 2


def test_requires_python_and_yanked_filtering() -> None:
    links = [
        link("demo-2.0.tar.gz", requires_python=">=3.12"),
        link("demo-1.0.tar.gz", yanked=True),
        link("demo-0.9.tar.gz", requires_python=">=3.8"),
    ]
    candidates = select_candidates(links, Requirement("demo"), TARGET)
    assert [candidate.link.filename for candidate in candidates] == ["demo-0.9.tar.gz"]


def test_prerelease_policy() -> None:
    links = [link("demo-2.0rc1.tar.gz"), link("demo-1.0.tar.gz")]
    stable = select_candidates(links, Requirement("demo"), TARGET)
    assert stable[0].link.filename == "demo-1.0.tar.gz"
    pre = select_candidates(links, Requirement("demo"), TARGET, pre=True)
    assert pre[0].link.filename == "demo-2.0rc1.tar.gz"
    pinned = select_candidates(links, Requirement("demo==2.0rc1"), TARGET)
    assert pinned[0].link.filename == "demo-2.0rc1.tar.gz"


def test_binary_policy_flags() -> None:
    links = [link("demo-1.0.tar.gz"), link("demo-1.0-py3-none-any.whl")]
    no_binary = select_candidates(links, Requirement("demo"), TARGET, no_binary=True)
    assert [candidate.kind for candidate in no_binary] == ["sdist"]
    only_binary = select_candidates(links, Requirement("demo"), TARGET, only_binary=True)
    assert [candidate.kind for candidate in only_binary] == ["wheel"]


def test_unsafe_filenames_are_rejected() -> None:
    links = [
        link("../demo-1.0.tar.gz"),
        link("demo-1.0.tar.gz/evil"),
        link(".demo-1.0.tar.gz"),
    ]
    assert select_candidates(links, Requirement("demo"), TARGET) == []


def test_wrong_project_names_are_rejected() -> None:
    links = [link("other-1.0.tar.gz"), link("other-1.0-py3-none-any.whl")]
    assert select_candidates(links, Requirement("demo"), TARGET) == []
