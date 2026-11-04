"""Shared pytest fixtures for offline pipdown tests."""

from __future__ import annotations

import io
import sys
import tarfile
import threading
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import pytest

OUTPUT_DIR = Path(__file__).resolve().parents[1]
if str(OUTPUT_DIR) not in sys.path:
    sys.path.insert(0, str(OUTPUT_DIR))


@dataclass(slots=True)
class Route:
    """A static HTTP route served by the test server."""

    body: bytes = b""
    content_type: str = "application/octet-stream"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


@dataclass(slots=True)
class LocalServer:
    """Handle for the local HTTP test server."""

    url: str
    routes: dict[str, Route]


class _Handler(BaseHTTPRequestHandler):
    """Serve routes from a class-level mapping."""

    routes: ClassVar[dict[str, Route]] = {}

    def do_GET(self) -> None:
        route = self.routes.get(urlparse(self.path).path)
        if route is None:
            self.send_error(404)
            return
        if route.delay > 0:
            time.sleep(route.delay)
        self.send_response(route.status)
        self.send_header("Content-Type", route.content_type)
        self.send_header("Content-Length", str(len(route.body)))
        for key, value in route.headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(route.body)

    def do_HEAD(self) -> None:
        route = self.routes.get(urlparse(self.path).path)
        if route is None:
            self.send_error(404)
            return
        self.send_response(route.status)
        self.send_header("Content-Type", route.content_type)
        self.send_header("Content-Length", str(len(route.body)))
        for key, value in route.headers.items():
            self.send_header(key, value)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Silence request logging during tests."""


@pytest.fixture
def http_server() -> Iterator[LocalServer]:
    """Start a local HTTP server whose routes can be mutated per test."""
    routes: dict[str, Route] = {}
    handler = type("BoundHandler", (_Handler,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
    try:
        yield LocalServer(url=f"http://{host_text}:{port}", routes=routes)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_tar_gz(name: str = "demo", version: str = "1.0") -> bytes:
    """Create a valid gzip source archive in memory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
        info = tarfile.TarInfo(name=f"{name}-{version}/PKG-INFO")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def make_tar_bz2(name: str = "demo", version: str = "1.0") -> bytes:
    """Create a valid bzip2 source archive in memory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as archive:
        content = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
        info = tarfile.TarInfo(name=f"{name}-{version}/PKG-INFO")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def make_zip(name: str = "demo", version: str = "1.0", *, wheel: bool = False) -> bytes:
    """Create a valid zip source archive or wheel in memory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        if wheel:
            archive.writestr(
                f"{name}-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
            archive.writestr(
                f"{name}-{version}.dist-info/WHEEL",
                "Wheel-Version: 1.0\n"
                "Generator: pipdown-tests\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n",
            )
        else:
            archive.writestr(
                f"{name}-{version}/PKG-INFO",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
    return buffer.getvalue()


@pytest.fixture
def sdist_bytes() -> bytes:
    """Valid demo source archive bytes."""
    return make_tar_gz()


@pytest.fixture
def wheel_bytes() -> bytes:
    """Valid demo pure wheel bytes."""
    return make_zip(wheel=True)


@pytest.fixture
def local_find_links(tmp_path: Path) -> Path:
    """Create a local find-links directory with a source archive and wheel."""
    directory = tmp_path / "links"
    directory.mkdir()
    (directory / "demo-1.0.tar.gz").write_bytes(make_tar_gz())
    (directory / "demo-1.0-py3-none-any.whl").write_bytes(make_zip(wheel=True))
    return directory
