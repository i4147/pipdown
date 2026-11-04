"""Tests for the parallel downloader, caching, and fallback behavior."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipdown import DelayedProgress, Options, download_packages
from tests.conftest import LocalServer, Route, make_tar_gz, make_zip


def quiet_reporter() -> DelayedProgress:
    """Return a reporter that will not start during fast tests."""
    return DelayedProgress(1_000_000)


def test_local_find_links_prefers_source(local_find_links: Path, tmp_path: Path) -> None:
    results = download_packages(
        ["demo"],
        Options(find_links=(str(local_find_links),), indexes=(), jobs=1),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "downloaded"
    assert results[0].path is not None
    assert results[0].path.name == "demo-1.0.tar.gz"
    assert results[0].path.parent == tmp_path


def test_local_find_links_wheel_fallback(tmp_path: Path) -> None:
    links = tmp_path / "links"
    links.mkdir()
    (links / "demo-1.0-py3-none-any.whl").write_bytes(make_zip(wheel=True))
    results = download_packages(
        ["demo"],
        Options(find_links=(str(links),), indexes=(), jobs=1),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "downloaded"
    assert results[0].path is not None
    assert results[0].path.name == "demo-1.0-py3-none-any.whl"


def test_parallel_downloads_preserve_input_order(local_find_links: Path, tmp_path: Path) -> None:
    (local_find_links / "other-2.0.tar.gz").write_bytes(make_tar_gz("other", "2.0"))
    results = download_packages(
        ["demo", "other"],
        Options(find_links=(str(local_find_links),), indexes=(), jobs=2),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert [result.requirement for result in results] == ["demo", "other"]
    assert [result.status for result in results] == ["downloaded", "downloaded"]
    assert {path.name for path in tmp_path.iterdir() if path.is_file()} == {
        "demo-1.0.tar.gz",
        "other-2.0.tar.gz",
    }


def test_valid_existing_file_is_cached_and_overwrite_redownloads(
    local_find_links: Path, tmp_path: Path
) -> None:
    options = Options(find_links=(str(local_find_links),), indexes=(), jobs=1)
    first = download_packages(
        ["demo"], options, destination=tmp_path, reporter_factory=quiet_reporter
    )
    assert first[0].status == "downloaded"
    second = download_packages(
        ["demo"], options, destination=tmp_path, reporter_factory=quiet_reporter
    )
    assert second[0].status == "cached"
    third = download_packages(
        ["demo"],
        Options(find_links=(str(local_find_links),), indexes=(), jobs=1, overwrite=True),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert third[0].status == "downloaded"


def test_invalid_existing_file_is_replaced(local_find_links: Path, tmp_path: Path) -> None:
    (tmp_path / "demo-1.0.tar.gz").write_bytes(b"corrupt")
    results = download_packages(
        ["demo"],
        Options(find_links=(str(local_find_links),), indexes=(), jobs=1),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "downloaded"
    assert results[0].path is not None
    assert results[0].path.read_bytes() == make_tar_gz()


def test_dry_run_does_not_write_files(local_find_links: Path, tmp_path: Path) -> None:
    results = download_packages(
        ["demo"],
        Options(find_links=(str(local_find_links),), indexes=(), dry_run=True),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "dry-run"
    assert results[0].path is None
    assert list(tmp_path.iterdir()) == [local_find_links]


def test_failures_are_isolated(local_find_links: Path, tmp_path: Path) -> None:
    results = download_packages(
        ["demo", "missing"],
        Options(find_links=(str(local_find_links),), indexes=(), jobs=2),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert [result.status for result in results] == ["downloaded", "failed"]
    assert results[1].error is not None


def test_max_size_rejects_large_artifact(local_find_links: Path, tmp_path: Path) -> None:
    results = download_packages(
        ["demo"],
        Options(find_links=(str(local_find_links),), indexes=(), max_size=10),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "failed"
    assert results[0].error is not None
    assert "size limit" in results[0].error
    assert not (tmp_path / "demo-1.0.tar.gz").exists()


def test_hash_mismatch_falls_back_to_wheel(
    http_server: LocalServer, tmp_path: Path, sdist_bytes: bytes, wheel_bytes: bytes
) -> None:
    wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
    index = {
        "files": [
            {
                "filename": "demo-1.0.tar.gz",
                "url": f"{http_server.url}/demo-1.0.tar.gz",
                "hashes": {"sha256": "0" * 64},
            },
            {
                "filename": "demo-1.0-py3-none-any.whl",
                "url": f"{http_server.url}/demo-1.0-py3-none-any.whl",
                "hashes": {"sha256": wheel_digest},
            },
        ]
    }
    http_server.routes["/simple/demo/"] = Route(
        body=json.dumps(index).encode(),
        content_type="application/vnd.pypi.simple.v1+json",
    )
    http_server.routes["/demo-1.0.tar.gz"] = Route(body=sdist_bytes)
    http_server.routes["/demo-1.0-py3-none-any.whl"] = Route(body=wheel_bytes)
    results = download_packages(
        ["demo==1.0"],
        Options(indexes=(f"{http_server.url}/simple/",), jobs=1, retries=0),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "downloaded"
    assert results[0].candidate is not None
    assert results[0].candidate.kind == "wheel"
    assert not (tmp_path / "demo-1.0.tar.gz").exists()
    assert (tmp_path / "demo-1.0-py3-none-any.whl").read_bytes() == wheel_bytes


def test_hash_mismatch_leaves_no_final_or_partial_file(
    http_server: LocalServer, tmp_path: Path, sdist_bytes: bytes
) -> None:
    index = {
        "files": [
            {
                "filename": "demo-1.0.tar.gz",
                "url": f"{http_server.url}/demo-1.0.tar.gz",
                "hashes": {"sha256": "0" * 64},
            }
        ]
    }
    http_server.routes["/simple/demo/"] = Route(
        body=json.dumps(index).encode(),
        content_type="application/vnd.pypi.simple.v1+json",
    )
    http_server.routes["/demo-1.0.tar.gz"] = Route(body=sdist_bytes)
    results = download_packages(
        ["demo==1.0"],
        Options(indexes=(f"{http_server.url}/simple/",), jobs=1, retries=0),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "failed"
    assert list(tmp_path.iterdir()) == []
