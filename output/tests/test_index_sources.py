"""Tests for HTML indexes, mirror fallback, and direct find-links URLs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipdown import DelayedProgress, Options, download_packages
from tests.conftest import LocalServer, Route


def quiet_reporter() -> DelayedProgress:
    return DelayedProgress(1_000_000)


def test_html_simple_index_end_to_end(
    http_server: LocalServer, tmp_path: Path, sdist_bytes: bytes
) -> None:
    digest = hashlib.sha256(sdist_bytes).hexdigest()
    html = (
        '<html><body><a href="/demo-1.0.tar.gz#sha256='
        + digest
        + '" data-requires-python="&gt;=3.8">demo-1.0.tar.gz</a></body></html>'
    )
    http_server.routes["/simple/demo/"] = Route(body=html.encode(), content_type="text/html")
    http_server.routes["/demo-1.0.tar.gz"] = Route(body=sdist_bytes)
    results = download_packages(
        ["demo==1.0"],
        Options(indexes=(f"{http_server.url}/simple/",), jobs=1, retries=0),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "downloaded"
    assert results[0].path is not None
    assert results[0].path.read_bytes() == sdist_bytes


def test_mirror_fallback_after_index_error(
    http_server: LocalServer, tmp_path: Path, sdist_bytes: bytes
) -> None:
    digest = hashlib.sha256(sdist_bytes).hexdigest()
    index = {
        "files": [
            {
                "filename": "demo-1.0.tar.gz",
                "url": f"{http_server.url}/demo-1.0.tar.gz",
                "hashes": {"sha256": digest},
            }
        ]
    }
    http_server.routes["/bad/simple/demo/"] = Route(status=500)
    http_server.routes["/good/simple/demo/"] = Route(
        body=json.dumps(index).encode(),
        content_type="application/vnd.pypi.simple.v1+json",
    )
    http_server.routes["/demo-1.0.tar.gz"] = Route(body=sdist_bytes)
    results = download_packages(
        ["demo==1.0"],
        Options(
            indexes=(f"{http_server.url}/bad/simple/", f"{http_server.url}/good/simple/"),
            jobs=1,
            retries=0,
        ),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "downloaded"


def test_remote_direct_find_links_artifact(
    http_server: LocalServer, tmp_path: Path, sdist_bytes: bytes
) -> None:
    http_server.routes["/demo-1.0.tar.gz"] = Route(body=sdist_bytes)
    results = download_packages(
        ["demo==1.0"],
        Options(find_links=(f"{http_server.url}/demo-1.0.tar.gz",), indexes=(), jobs=1),
        destination=tmp_path,
        reporter_factory=quiet_reporter,
    )
    assert results[0].status == "downloaded"
    assert results[0].path is not None
    assert results[0].path.name == "demo-1.0.tar.gz"
