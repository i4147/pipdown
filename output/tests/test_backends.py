"""End-to-end backend tests against a local HTTP server."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import cast

import pytest

from pipdown import (
    BackendName,
    DelayedProgress,
    HttpStatusError,
    Options,
    download_packages,
    make_backend,
)
from tests.conftest import LocalServer, Route


@pytest.mark.parametrize("backend", ["httpx", "requests", "pycurl"])
def test_backend_downloads_from_json_index(
    backend: str,
    http_server: LocalServer,
    tmp_path: Path,
    sdist_bytes: bytes,
) -> None:
    if backend != "httpx":
        pytest.importorskip(backend)
    digest = hashlib.sha256(sdist_bytes).hexdigest()
    index = {
        "files": [
            {
                "filename": "demo-1.0.tar.gz",
                "url": f"{http_server.url}/artifacts/demo-1.0.tar.gz",
                "hashes": {"sha256": digest},
                "requires-python": ">=3.8",
            }
        ]
    }
    http_server.routes["/simple/demo/"] = RouteFactory.json(index)
    http_server.routes["/artifacts/demo-1.0.tar.gz"] = RouteFactory.binary(sdist_bytes)
    options = Options(
        backend=cast(BackendName, backend),
        indexes=(f"{http_server.url}/simple/",),
        jobs=1,
        retries=0,
    )
    results = download_packages(
        ["demo==1.0"],
        options,
        destination=tmp_path,
        reporter_factory=lambda: DelayedProgress(1_000_000),
    )
    assert len(results) == 1
    assert results[0].status == "downloaded"
    assert results[0].path is not None
    assert results[0].path.read_bytes() == sdist_bytes


@pytest.mark.parametrize("backend", ["httpx", "requests", "pycurl"])
def test_backend_reports_http_errors(backend: str, http_server: LocalServer) -> None:
    if backend != "httpx":
        pytest.importorskip(backend)
    client = make_backend(cast(BackendName, backend), timeout=5)
    try:
        with pytest.raises(HttpStatusError) as exc_info:
            client.get(f"{http_server.url}/missing", {})
        assert exc_info.value.status_code == 404
    finally:
        client.close()


@pytest.mark.parametrize("backend", ["httpx", "requests", "pycurl"])
def test_backend_transfer_progress_totals(
    backend: str, http_server: LocalServer, sdist_bytes: bytes
) -> None:
    if backend != "httpx":
        pytest.importorskip(backend)
    http_server.routes["/artifact"] = RouteFactory.binary(sdist_bytes)
    client = make_backend(cast(BackendName, backend), timeout=5)
    sink = io.BytesIO()
    updates: list[tuple[int, int | None]] = []

    def progress(advance: int, total: int | None) -> None:
        updates.append((advance, total))

    try:
        client.transfer(f"{http_server.url}/artifact", {}, sink, progress)
    finally:
        client.close()
    assert sink.getvalue() == sdist_bytes
    assert sum(advance for advance, _ in updates) == len(sdist_bytes)
    assert any(total == len(sdist_bytes) for _, total in updates)


class RouteFactory:
    """Small helpers for constructing test-server routes."""

    @staticmethod
    def json(data: object) -> Route:
        return Route(
            body=json.dumps(data).encode(),
            content_type="application/vnd.pypi.simple.v1+json",
        )

    @staticmethod
    def binary(data: bytes) -> Route:
        return Route(body=data)
