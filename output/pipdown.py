#!/usr/bin/env python3
"""Download Python package distributions from PyPI-compatible simple indexes.

The command line interface deliberately keeps the download destination flat:
files are written to the current working directory, never to a package-specific
subdirectory.  The module also exposes a small typed API for tests and embedding.

Selection policy
----------------
* Parse PEP 691 JSON and PEP 503 HTML simple indexes.
* Respect ``Requires-Python`` and PEP 440 version specifiers.
* Choose the newest version that has a usable artifact.  Within that version,
  prefer ``.tar.gz``, then ``.zip``, then ``.tar.bz2`` source distributions.
* If that version has no supported source distribution, fall back to the best
  wheel whose tags match the target Python/Linux platform.
* Verify published hashes and archive integrity before publishing a final file.

Only the requested distributions are downloaded; dependency resolution is not
performed.  Python 3.10+ is required.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import logging
import platform
import re
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Protocol, cast
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import (
    Tag,
    compatible_tags,
    cpython_tags,
    generic_tags,
    interpreter_name,
    sys_tags,
)
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

__version__ = "1.0.0"

PYPI_INDEX = "https://pypi.org/simple/"
USER_AGENT = f"pipdown/{__version__}"
CHUNK_SIZE = 1024 * 1024
SOURCE_SUFFIXES = (".tar.gz", ".zip", ".tar.bz2")
WHEEL_SUFFIX = ".whl"
SUPPORTED_SUFFIXES = (*SOURCE_SUFFIXES, WHEEL_SUFFIX)
JSON_CONTENT_TYPES = frozenset({"application/vnd.pypi.simple.v1+json", "application/json"})
HTML_CONTENT_TYPES = frozenset({"application/vnd.pypi.simple.v1+html", "text/html"})
ACCEPT_HEADER = (
    "application/vnd.pypi.simple.v1+json, "
    "application/vnd.pypi.simple.v1+html; q=0.1, "
    "text/html; q=0.01"
)
HASH_PREFERENCE = ("sha256", "sha512", "sha384", "sha224", "sha1", "md5")
HASH_ALGOS = frozenset(HASH_PREFERENCE)
NO_TAG_PRIORITY = 10**9

BackendName = Literal["httpx", "requests", "pycurl"]
ArtifactKind = Literal["sdist", "wheel"]
ResultStatus = Literal["downloaded", "cached", "dry-run", "failed"]
ProgressCallback = Callable[[int, int | None], None]
logger = logging.getLogger("pipdown")


class PipdownError(Exception):
    """Base class for expected pipdown failures."""


class InputError(PipdownError):
    """Invalid user input or configuration."""


class ResolutionError(PipdownError):
    """No compatible distribution could be resolved."""


class BackendError(PipdownError):
    """An HTTP backend is unavailable or failed unexpectedly."""


class NetworkError(PipdownError):
    """A transport-level HTTP failure occurred."""


class HttpStatusError(PipdownError):
    """The server returned an HTTP error status."""

    def __init__(self, message: str, *, status_code: int, url: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class IntegrityError(PipdownError):
    """A downloaded artifact failed hash or archive validation."""


class FileTooLarge(PipdownError):
    """An artifact exceeds the configured size limit."""


class DownloadError(PipdownError):
    """All candidate artifacts for a requirement failed."""


@dataclass(frozen=True, slots=True)
class Link:
    """A distribution link from a simple index or find-links source."""

    url: str
    filename: str
    requires_python: str | None = None
    yanked: bool = False
    hashes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        hashes = {key.lower(): value for key, value in self.hashes.items()}
        fragment = urlparse(self.url).fragment
        for algorithm, values in parse_qs(fragment).items():
            if algorithm.lower() in HASH_ALGOS and values:
                hashes.setdefault(algorithm.lower(), values[0])
        object.__setattr__(self, "hashes", hashes)

    @property
    def clean_url(self) -> str:
        """URL without a PEP 503 hash fragment."""
        return self.url.split("#", 1)[0]

    @property
    def is_file(self) -> bool:
        """Whether this link points at a local file."""
        return urlparse(self.url).scheme == "file"

    @property
    def local_path(self) -> Path:
        """Local filesystem path for a ``file://`` link."""
        parsed = urlparse(self.url)
        if parsed.scheme != "file":
            raise ValueError(f"not a file URL: {self.url}")
        return Path(unquote(parsed.path))


@dataclass(frozen=True, slots=True)
class Candidate:
    """A distribution that satisfies a requirement and target environment."""

    requirement: Requirement
    version: Version
    link: Link
    kind: ArtifactKind
    source_rank: int
    tag_priority: int

    @property
    def sort_key(self) -> tuple[Version, bool, int, int, str]:
        """Sort key for descending preference order."""
        return (
            self.version,
            self.kind == "sdist",
            self.source_rank,
            -self.tag_priority,
            self.link.filename.lower(),
        )


@dataclass(frozen=True, slots=True)
class TargetPython:
    """Python/platform tags used for wheel compatibility.

    The default target is the running interpreter on Linux.  When the script is
    run on a non-Linux host without explicit platform overrides, conservative
    manylinux2014 tags for the current machine architecture are used instead.
    """

    python_version: tuple[int, int] = field(
        default_factory=lambda: (sys.version_info.major, sys.version_info.minor)
    )
    implementation: str = field(default_factory=interpreter_name)
    abis: tuple[str, ...] | None = None
    platforms: tuple[str, ...] | None = None

    @property
    def version_str(self) -> str:
        """Major.minor version string used by ``Requires-Python``."""
        return f"{self.python_version[0]}.{self.python_version[1]}"

    def supported_tags(self) -> tuple[Tag, ...]:
        """Return supported wheel tags in preference order."""
        if (
            self.platforms is None
            and self.abis is None
            and self.implementation == interpreter_name()
            and self.python_version == (sys.version_info.major, sys.version_info.minor)
            and sys.platform.startswith("linux")
        ):
            return tuple(sys_tags())

        platforms = self.platforms or _default_linux_platforms()
        interpreter = f"{self.implementation}{self.python_version[0]}{self.python_version[1]}"
        tags: list[Tag] = []
        if self.implementation == "cp":
            tags.extend(
                cpython_tags(
                    python_version=self.python_version,
                    abis=self.abis,
                    platforms=platforms,
                )
            )
        else:
            tags.extend(
                generic_tags(
                    interpreter=interpreter,
                    abis=self.abis,
                    platforms=platforms,
                )
            )
        tags.extend(
            compatible_tags(
                python_version=self.python_version,
                interpreter=interpreter,
                platforms=platforms,
            )
        )
        return tuple(dict.fromkeys(tags))


def _default_linux_platforms() -> tuple[str, ...]:
    """Return Linux platform tags for the current machine.

    On Linux, reuse the platform tags detected by ``packaging`` for the running
    environment.  On other hosts, fall back to conservative manylinux2014 tags.
    """
    if sys.platform.startswith("linux"):
        detected = tuple(dict.fromkeys(tag.platform for tag in sys_tags() if tag.platform != "any"))
        if detected:
            return detected
    machine = platform.machine().lower()
    aliases = {
        "amd64": "x86_64",
        "arm64": "aarch64",
        "i386": "i686",
        "i486": "i686",
        "i586": "i686",
        "i686": "i686",
    }
    arch = aliases.get(machine, machine)
    manylinux_arches = {"x86_64", "aarch64", "ppc64le", "s390x"}
    if arch in manylinux_arches:
        return (f"linux_{arch}", f"manylinux2014_{arch}", f"manylinux_2_17_{arch}")
    return (f"linux_{arch}",)


@dataclass(frozen=True, slots=True)
class Options:
    """Runtime options for resolution and downloading."""

    backend: BackendName = "httpx"
    jobs: int = 4
    timeout: float = 60.0
    retries: int = 2
    retry_delay: float = 1.0
    progress_delay: float = 15.0
    pre: bool = False
    no_binary: bool = False
    only_binary: bool = False
    overwrite: bool = False
    dry_run: bool = False
    max_size: int | None = None
    indexes: tuple[str, ...] = (PYPI_INDEX,)
    find_links: tuple[str, ...] = ()
    target: TargetPython = field(default_factory=TargetPython)


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Outcome for one requested requirement."""

    requirement: str
    status: ResultStatus
    candidate: Candidate | None = None
    path: Path | None = None
    size: int | None = None
    error: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        data: dict[str, object] = {
            "requirement": self.requirement,
            "status": self.status,
            "path": str(self.path) if self.path is not None else None,
            "size": self.size,
            "error": self.error,
            "attempts": self.attempts,
        }
        if self.candidate is not None:
            candidate = self.candidate
            data.update(
                {
                    "name": canonicalize_name(candidate.requirement.name),
                    "version": str(candidate.version),
                    "filename": candidate.link.filename,
                    "url": candidate.link.clean_url,
                    "kind": candidate.kind,
                    "requires_python": candidate.link.requires_python,
                    "yanked": candidate.link.yanked,
                    "hashes": candidate.link.hashes,
                }
            )
        return data


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Concrete non-streaming HTTP response."""

    content: bytes
    headers: dict[str, str]
    url: str

    @property
    def content_type(self) -> str:
        """Lowercase media type without parameters."""
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


class ChunkSink(Protocol):
    """Writable binary sink used by streaming backends."""

    def write(self, data: bytes) -> int: ...


class HttpClient(Protocol):
    """Minimal HTTP interface implemented by every backend."""

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse: ...

    def transfer(
        self,
        url: str,
        headers: Mapping[str, str],
        sink: ChunkSink,
        progress: ProgressCallback | None,
    ) -> None: ...

    def close(self) -> None: ...


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Normalize header names to lowercase."""
    return {key.lower(): value for key, value in headers.items()}


def _content_length(headers: Mapping[str, str]) -> int | None:
    """Parse a usable Content-Length for progress totals."""
    if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        return None
    value = headers.get("content-length")
    if value is None or not value.isdigit():
        return None
    return int(value)


def _http_status_error(url: str, status_code: int) -> HttpStatusError:
    """Create a uniform HTTP status error."""
    return HttpStatusError(f"HTTP {status_code} for {url}", status_code=status_code, url=url)


class HttpxBackend:
    """HTTP backend powered by httpx (the default)."""

    def __init__(self, timeout: float) -> None:
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            headers={"User-Agent": USER_AGENT},
        )

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        """Fetch a complete response body."""
        try:
            response = self._client.get(url, headers=dict(headers))
            response.raise_for_status()
            return HttpResponse(
                content=response.content,
                headers=_lower_headers(response.headers),
                url=str(response.url),
            )
        except httpx.HTTPStatusError as exc:
            raise _http_status_error(url, exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"network error for {url}: {exc}") from exc

    def transfer(
        self,
        url: str,
        headers: Mapping[str, str],
        sink: ChunkSink,
        progress: ProgressCallback | None,
    ) -> None:
        """Stream a response body into ``sink``."""
        try:
            with self._client.stream("GET", url, headers=dict(headers)) as response:
                response.raise_for_status()
                response_headers = _lower_headers(response.headers)
                total = _content_length(response_headers)
                if progress is not None and total is not None:
                    progress(0, total)
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    if chunk:
                        sink.write(chunk)
                        if progress is not None:
                            progress(len(chunk), total)
        except httpx.HTTPStatusError as exc:
            raise _http_status_error(url, exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"network error for {url}: {exc}") from exc

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()


class RequestsBackend:
    """HTTP backend powered by requests."""

    def __init__(self, timeout: float) -> None:
        self._module: ModuleType = _import_optional("requests")
        self._timeout = (min(10.0, timeout), timeout)
        self._session = self._module.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        """Fetch a complete response body."""
        try:
            response = self._session.get(url, headers=dict(headers), timeout=self._timeout)
            response.raise_for_status()
            return HttpResponse(
                content=response.content,
                headers=_lower_headers(response.headers),
                url=str(response.url),
            )
        except self._module.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            raise _http_status_error(url, status) from exc
        except self._module.RequestException as exc:
            raise NetworkError(f"network error for {url}: {exc}") from exc

    def transfer(
        self,
        url: str,
        headers: Mapping[str, str],
        sink: ChunkSink,
        progress: ProgressCallback | None,
    ) -> None:
        """Stream a response body into ``sink``."""
        try:
            with self._session.get(
                url, headers=dict(headers), timeout=self._timeout, stream=True
            ) as response:
                response.raise_for_status()
                response_headers = _lower_headers(response.headers)
                total = _content_length(response_headers)
                if progress is not None and total is not None:
                    progress(0, total)
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        sink.write(chunk)
                        if progress is not None:
                            progress(len(chunk), total)
        except self._module.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            raise _http_status_error(url, status) from exc
        except self._module.RequestException as exc:
            raise NetworkError(f"network error for {url}: {exc}") from exc

    def close(self) -> None:
        """Close the underlying session."""
        self._session.close()


class PycurlBackend:
    """HTTP backend powered by pycurl/libcurl."""

    def __init__(self, timeout: float) -> None:
        self._module: ModuleType = _import_optional("pycurl")
        self._timeout = timeout

    def _new_curl(self, url: str, headers: Mapping[str, str]) -> Any:
        """Create a configured curl handle."""
        curl = self._module.Curl()
        curl.setopt(curl.URL, url)
        curl.setopt(curl.FOLLOWLOCATION, True)
        curl.setopt(curl.CONNECTTIMEOUT, max(1, int(min(10.0, self._timeout))))
        curl.setopt(curl.TIMEOUT, max(1, int(self._timeout)))
        curl.setopt(curl.USERAGENT, USER_AGENT)
        curl.setopt(curl.ACCEPT_ENCODING, "")
        curl.setopt(curl.HTTPHEADER, [f"{key}: {value}" for key, value in headers.items()])
        return curl

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        """Fetch a complete response body."""
        buffer = io.BytesIO()
        response_headers: dict[str, str] = {}
        curl = self._new_curl(url, headers)

        def header_callback(raw: bytes) -> int:
            line = raw.decode("latin-1").strip()
            if line.startswith("HTTP/"):
                response_headers.clear()
            elif ":" in line:
                key, value = line.split(":", 1)
                response_headers[key.lower()] = value.strip()
            return len(raw)

        try:
            curl.setopt(curl.WRITEDATA, buffer)
            curl.setopt(curl.HEADERFUNCTION, header_callback)
            curl.perform()
            status = int(curl.getinfo(curl.RESPONSE_CODE))
            effective_url = str(curl.getinfo(curl.EFFECTIVE_URL))
            if status >= 400:
                raise _http_status_error(url, status)
            return HttpResponse(
                content=buffer.getvalue(),
                headers=dict(response_headers),
                url=effective_url,
            )
        except self._module.error as exc:
            status = int(curl.getinfo(curl.RESPONSE_CODE))
            if status >= 400:
                raise _http_status_error(url, status) from exc
            raise NetworkError(f"network error for {url}: {exc}") from exc
        finally:
            curl.close()

    def transfer(
        self,
        url: str,
        headers: Mapping[str, str],
        sink: ChunkSink,
        progress: ProgressCallback | None,
    ) -> None:
        """Stream a response body into ``sink``."""
        curl = self._new_curl(url, headers)
        total: int | None = None
        response_headers: dict[str, str] = {}
        write_error: BaseException | None = None

        def header_callback(raw: bytes) -> int:
            nonlocal total
            line = raw.decode("latin-1").strip()
            if line.startswith("HTTP/"):
                response_headers.clear()
                total = None
            elif ":" in line:
                key, value = line.split(":", 1)
                response_headers[key.lower()] = value.strip()
                if key.lower() == "content-length":
                    total = _content_length(response_headers)
                    if progress is not None and total is not None:
                        progress(0, total)
            return len(raw)

        def write_callback(data: bytes) -> int:
            nonlocal write_error
            try:
                sink.write(data)
            except BaseException as exc:
                write_error = exc
                return -1
            if progress is not None:
                progress(len(data), total)
            return len(data)

        try:
            curl.setopt(curl.FAILONERROR, True)
            curl.setopt(curl.HEADERFUNCTION, header_callback)
            curl.setopt(curl.WRITEFUNCTION, write_callback)
            curl.perform()
            status = int(curl.getinfo(curl.RESPONSE_CODE))
            if status >= 400:
                raise _http_status_error(url, status)
        except self._module.error as exc:
            if write_error is not None:
                raise write_error from exc
            status = int(curl.getinfo(curl.RESPONSE_CODE))
            if status >= 400:
                raise _http_status_error(url, status) from exc
            raise NetworkError(f"network error for {url}: {exc}") from exc
        finally:
            curl.close()

    def close(self) -> None:
        """Pycurl handles are per-request, so there is nothing to close."""


def _import_optional(name: str) -> ModuleType:
    """Import an optional backend dependency with a helpful error."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise BackendError(
            f"backend {name!r} requires the {name!r} package; install it with: pip install {name}"
        ) from exc


def make_backend(name: BackendName, timeout: float) -> HttpClient:
    """Construct an HTTP backend by name."""
    if name == "httpx":
        return HttpxBackend(timeout)
    if name == "requests":
        return RequestsBackend(timeout)
    if name == "pycurl":
        return PycurlBackend(timeout)
    raise InputError(f"unsupported backend: {name}")


class _HTMLIndexParser(HTMLParser):
    """Collect anchors and base URL from a PEP 503 HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url: str | None = None
        self.anchors: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "base" and self.base_url is None:
            self.base_url = attributes.get("href")
        elif tag == "a":
            self.anchors.append(attributes)


def _filename_from_url(url: str) -> str:
    """Extract a decoded filename from a URL path."""
    return unquote(urlparse(url).path.rsplit("/", 1)[-1])


def _link_from_fields(
    *,
    url: str,
    base_url: str,
    filename: str | None,
    requires_python: object,
    yanked: object,
    hashes: object,
) -> Link | None:
    """Build a Link from JSON/HTML fields, skipping malformed entries."""
    if not url:
        return None
    absolute = urljoin(base_url, url)
    name = filename or _filename_from_url(absolute)
    parsed_hashes: dict[str, str] = {}
    if isinstance(hashes, dict):
        for key, value in hashes.items():
            if isinstance(key, str) and isinstance(value, str):
                parsed_hashes[key.lower()] = value
    is_yanked = True if isinstance(yanked, str) else bool(yanked)
    return Link(
        url=absolute,
        filename=name,
        requires_python=(requires_python if isinstance(requires_python, str) else None),
        yanked=is_yanked,
        hashes=parsed_hashes,
    )


def parse_json_page(content: bytes, base_url: str) -> list[Link]:
    """Parse a PEP 691 JSON simple page."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ResolutionError(f"invalid JSON simple page at {base_url}: {exc}") from exc
    if not isinstance(data, dict):
        raise ResolutionError(f"invalid JSON simple page at {base_url}")
    files = data.get("files", [])
    if not isinstance(files, list):
        raise ResolutionError(f"invalid JSON simple page at {base_url}")
    links: list[Link] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str):
            continue
        filename = item.get("filename")
        link = _link_from_fields(
            url=url,
            base_url=base_url,
            filename=filename if isinstance(filename, str) else None,
            requires_python=item.get("requires-python"),
            yanked=item.get("yanked"),
            hashes=item.get("hashes"),
        )
        if link is not None:
            links.append(link)
    return links


def parse_html_page(content: str, base_url: str) -> list[Link]:
    """Parse a PEP 503 HTML simple page."""
    parser = _HTMLIndexParser()
    parser.feed(content)
    effective_base = urljoin(base_url, parser.base_url) if parser.base_url else base_url
    links: list[Link] = []
    for attributes in parser.anchors:
        href = attributes.get("href")
        if not href:
            continue
        link = _link_from_fields(
            url=href,
            base_url=effective_base,
            filename=None,
            requires_python=attributes.get("data-requires-python"),
            yanked="data-yanked" in attributes,
            hashes=None,
        )
        if link is not None:
            links.append(link)
    return links


def fetch_page(client: HttpClient, url: str) -> list[Link]:
    """Fetch and parse a JSON or HTML simple page."""
    response = client.get(url, {"Accept": ACCEPT_HEADER})
    content_type = response.content_type
    if content_type in JSON_CONTENT_TYPES:
        return parse_json_page(response.content, response.url)
    if content_type in HTML_CONTENT_TYPES:
        return parse_html_page(response.content.decode("utf-8", "replace"), response.url)
    if response.content.lstrip().startswith(b"{"):
        return parse_json_page(response.content, response.url)
    raise ResolutionError(f"unsupported simple index content-type {content_type!r} at {url}")


def _safe_filename(filename: str) -> bool:
    """Whether a distribution filename is safe to write into a flat directory."""
    if not filename or filename in {".", ".."} or filename.startswith("."):
        return False
    if "/" in filename or "\\" in filename:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in filename):
        return False
    return Path(filename).name == filename


def _source_rank(filename: str) -> int:
    """Preference rank for supported source archive extensions."""
    lower = filename.lower()
    if lower.endswith(".tar.gz"):
        return 3
    if lower.endswith(".zip"):
        return 2
    if lower.endswith(".tar.bz2"):
        return 1
    return 0


def _parse_sdist_filename(filename: str) -> tuple[str, Version] | None:
    """Parse supported source distribution filenames, including ``.tar.bz2``."""
    lower = filename.lower()
    for suffix in SOURCE_SUFFIXES:
        if lower.endswith(suffix):
            stem = filename[: -len(suffix)]
            name, separator, version_text = stem.rpartition("-")
            if not separator or not name or not version_text:
                return None
            try:
                return canonicalize_name(name), Version(version_text)
            except InvalidVersion:
                return None
    return None


def _python_matches(requires_python: str | None, target: TargetPython) -> bool:
    """Whether ``Requires-Python`` matches the target interpreter."""
    if not requires_python:
        return True
    try:
        specifier = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return False
    return specifier.contains(target.version_str, prereleases=True)


def select_candidates(
    links: Iterable[Link],
    requirement: Requirement,
    target: TargetPython,
    *,
    pre: bool = False,
    no_binary: bool = False,
    only_binary: bool = False,
) -> list[Candidate]:
    """Filter and sort distribution links for a requirement."""
    priorities = {tag: index for index, tag in enumerate(target.supported_tags())}
    canonical_name = canonicalize_name(requirement.name)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for link in links:
        if link.yanked or link.clean_url in seen:
            continue
        if not _safe_filename(link.filename):
            continue
        if not _python_matches(link.requires_python, target):
            continue
        filename = link.filename
        lower = filename.lower()
        if lower.endswith(WHEEL_SUFFIX):
            if no_binary:
                continue
            try:
                name, version, _build, tags = parse_wheel_filename(filename)
            except (InvalidWheelFilename, InvalidVersion):
                continue
            if canonicalize_name(name) != canonical_name:
                continue
            if not requirement.specifier.contains(
                version, prereleases=True if pre else bool(requirement.specifier.prereleases)
            ):
                continue
            matching = [priorities[tag] for tag in tags if tag in priorities]
            if not matching:
                continue
            candidates.append(
                Candidate(
                    requirement=requirement,
                    version=version,
                    link=link,
                    kind="wheel",
                    source_rank=0,
                    tag_priority=min(matching),
                )
            )
            seen.add(link.clean_url)
        elif lower.endswith(SOURCE_SUFFIXES):
            if only_binary:
                continue
            parsed = _parse_sdist_filename(filename)
            if parsed is None:
                continue
            sdist_name, sdist_version = parsed
            if sdist_name != canonical_name:
                continue
            if not requirement.specifier.contains(
                sdist_version, prereleases=True if pre else bool(requirement.specifier.prereleases)
            ):
                continue
            candidates.append(
                Candidate(
                    requirement=requirement,
                    version=sdist_version,
                    link=link,
                    kind="sdist",
                    source_rank=_source_rank(filename),
                    tag_priority=NO_TAG_PRIORITY,
                )
            )
            seen.add(link.clean_url)
    candidates.sort(key=lambda candidate: candidate.sort_key, reverse=True)
    return candidates


def _find_links_url(source: str) -> str:
    """Normalize a find-links source to a URL or local path string."""
    if source.startswith(("http://", "https://", "file://")):
        return source
    return str(Path(source).expanduser().resolve())


def collect_find_links(client: HttpClient, source: str) -> list[Link]:
    """Collect links from a local file/directory or remote find-links page."""
    normalized = _find_links_url(source)
    parsed = urlparse(normalized)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        if path.is_file():
            return [Link(url=path.resolve().as_uri(), filename=path.name)]
        if path.is_dir():
            return [
                Link(url=item.resolve().as_uri(), filename=item.name)
                for item in sorted(path.iterdir())
                if item.is_file()
            ]
        raise ResolutionError(f"find-links path does not exist: {path}")
    if parsed.scheme in {"http", "https"}:
        if urlparse(normalized).path.lower().endswith(SUPPORTED_SUFFIXES):
            return [Link(url=normalized, filename=_filename_from_url(normalized))]
        return fetch_page(client, normalized)
    path = Path(normalized)
    if path.is_file():
        return [Link(url=path.resolve().as_uri(), filename=path.name)]
    if path.is_dir():
        return [
            Link(url=item.resolve().as_uri(), filename=item.name)
            for item in sorted(path.iterdir())
            if item.is_file()
        ]
    raise ResolutionError(f"find-links path does not exist: {path}")


def resolve_requirement(
    client: HttpClient,
    requirement: Requirement,
    options: Options,
) -> list[Candidate]:
    """Resolve a requirement from find-links sources and simple indexes."""
    sources: list[tuple[str, str]] = [("find-links", source) for source in options.find_links]
    sources.extend(("index", index) for index in options.indexes)
    errors: list[str] = []
    for kind, source in sources:
        try:
            if kind == "index":
                url = urljoin(source.rstrip("/") + "/", canonicalize_name(requirement.name) + "/")
                links = fetch_page(client, url)
            else:
                links = collect_find_links(client, source)
            candidates = select_candidates(
                links,
                requirement,
                options.target,
                pre=options.pre,
                no_binary=options.no_binary,
                only_binary=options.only_binary,
            )
            if candidates:
                return candidates
        except (PipdownError, OSError, ValueError) as exc:
            errors.append(f"{kind} {source}: {exc}")
            logger.debug("source failed: %s %s: %s", kind, source, exc)
            continue
    detail = f"; last errors: {'; '.join(errors[-3:])}" if errors else ""
    raise ResolutionError(f"no compatible distribution found for {requirement}{detail}")


def choose_hash(hashes: Mapping[str, str]) -> tuple[str, str] | None:
    """Choose the strongest published hash available."""
    for algorithm in HASH_PREFERENCE:
        value = hashes.get(algorithm)
        if value:
            return algorithm, value.lower()
    return None


def verify_file_hash(path: Path, hashes: Mapping[str, str]) -> None:
    """Verify a file against the strongest published hash, if any."""
    chosen = choose_hash(hashes)
    if chosen is None:
        return
    algorithm, expected = chosen
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    if digest.hexdigest().lower() != expected:
        raise IntegrityError(
            f"{algorithm} mismatch for {path.name}: expected {expected}, got {digest.hexdigest()}"
        )


def validate_archive(path: Path, filename: str | None = None) -> None:
    """Validate a source archive or wheel without extracting it.

    ``filename`` may be supplied when ``path`` is a temporary file whose name
    does not carry the original distribution extension.
    """
    display_name = filename if filename is not None else path.name
    lower = display_name.lower()
    try:
        if lower.endswith((".zip", WHEEL_SUFFIX)):
            with zipfile.ZipFile(path) as archive:
                bad_member = archive.testzip()
            if bad_member is not None:
                raise IntegrityError(f"corrupt zip member {bad_member!r} in {display_name}")
            return
        if lower.endswith((".tar.gz", ".tar.bz2")):
            with tarfile.open(path, mode="r:*") as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    stream = archive.extractfile(member)
                    if stream is None:
                        continue
                    with stream:
                        while stream.read(CHUNK_SIZE):
                            pass
            return
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise IntegrityError(f"invalid archive {display_name}: {exc}") from exc
    raise IntegrityError(f"unsupported archive type: {display_name}")


class _DigestWriter:
    """Binary sink that hashes and size-checks streamed bytes.

    Progress is reported by the HTTP backend (or local copy loop), not here, to
    avoid counting the same bytes twice.
    """

    def __init__(
        self,
        stream: io.BufferedWriter,
        hashes: Mapping[str, str],
        max_size: int | None,
    ) -> None:
        self._stream = stream
        self._max_size = max_size
        self.size = 0
        chosen = choose_hash(hashes)
        self._algorithm = chosen[0] if chosen is not None else None
        self._expected = chosen[1] if chosen is not None else None
        self._digest = hashlib.new(self._algorithm) if self._algorithm else None

    def write(self, data: bytes) -> int:
        """Write bytes, updating integrity and size state."""
        self.size += len(data)
        if self._max_size is not None and self.size > self._max_size:
            raise FileTooLarge(f"file exceeds size limit of {self._max_size} bytes")
        if self._digest is not None:
            self._digest.update(data)
        return self._stream.write(data)

    def verify(self) -> None:
        """Raise if the accumulated digest does not match the published hash."""
        if self._digest is None or self._expected is None or self._algorithm is None:
            return
        actual = self._digest.hexdigest().lower()
        if actual != self._expected:
            raise IntegrityError(
                f"{self._algorithm} mismatch: expected {self._expected}, got {actual}"
            )


def _atomic_temp(destination: Path, filename: str) -> Path:
    """Create a temporary file in the destination directory, not a subdirectory."""
    with tempfile.NamedTemporaryFile(
        prefix=f".{filename}.", suffix=".part", dir=destination, delete=False
    ) as stream:
        return Path(stream.name)


def _publish_temp(
    temp_path: Path,
    destination_file: Path,
    writer: _DigestWriter,
) -> None:
    """Verify and atomically move a completed temporary file into place."""
    writer.verify()
    validate_archive(temp_path, destination_file.name)
    temp_path.replace(destination_file)


def _download_once(
    client: HttpClient,
    candidate: Candidate,
    destination: Path,
    options: Options,
    progress: ProgressCallback | None,
) -> DownloadResult:
    """Download or reuse one candidate artifact."""
    link = candidate.link
    destination_file = destination / link.filename
    if destination_file.exists() and not options.overwrite:
        try:
            verify_file_hash(destination_file, link.hashes)
            validate_archive(destination_file)
            size = destination_file.stat().st_size
            if progress is not None:
                progress(0, size)
            return DownloadResult(
                requirement=str(candidate.requirement),
                status="cached",
                candidate=candidate,
                path=destination_file,
                size=size,
                attempts=1,
            )
        except (IntegrityError, OSError) as exc:
            logger.warning("existing file %s is invalid: %s", destination_file, exc)

    if link.is_file:
        source = link.local_path
        if not source.is_file():
            raise IntegrityError(f"local artifact does not exist: {source}")
        if source.resolve() == destination_file.resolve():
            verify_file_hash(source, link.hashes)
            validate_archive(source)
            size = source.stat().st_size
            if progress is not None:
                progress(0, size)
            return DownloadResult(
                requirement=str(candidate.requirement),
                status="cached",
                candidate=candidate,
                path=source,
                size=size,
                attempts=1,
            )
        size = source.stat().st_size
        if options.max_size is not None and size > options.max_size:
            raise FileTooLarge(f"file exceeds size limit of {options.max_size} bytes")
        temp_path = _atomic_temp(destination, link.filename)
        try:
            with (
                temp_path.open("wb") as temp_stream,
                source.open("rb") as source_stream,
            ):
                writer = _DigestWriter(temp_stream, link.hashes, options.max_size)
                if progress is not None:
                    progress(0, size)
                for chunk in iter(lambda: source_stream.read(CHUNK_SIZE), b""):
                    writer.write(chunk)
                    if progress is not None:
                        progress(len(chunk), size)
            _publish_temp(temp_path, destination_file, writer)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return DownloadResult(
            requirement=str(candidate.requirement),
            status="downloaded",
            candidate=candidate,
            path=destination_file,
            size=writer.size,
            attempts=1,
        )

    temp_path = _atomic_temp(destination, link.filename)
    try:
        with temp_path.open("wb") as temp_stream:
            writer = _DigestWriter(temp_stream, link.hashes, options.max_size)
            client.transfer(link.clean_url, {"Accept": "*/*"}, writer, progress)
        _publish_temp(temp_path, destination_file, writer)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return DownloadResult(
        requirement=str(candidate.requirement),
        status="downloaded",
        candidate=candidate,
        path=destination_file,
        size=writer.size,
        attempts=1,
    )


def _download_with_retries(
    client: HttpClient,
    candidate: Candidate,
    destination: Path,
    options: Options,
    progress: ProgressCallback | None,
) -> DownloadResult:
    """Retry transient failures for one candidate."""
    attempts = options.retries + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = _download_once(client, candidate, destination, options, progress)
            return DownloadResult(
                requirement=result.requirement,
                status=result.status,
                candidate=result.candidate,
                path=result.path,
                size=result.size,
                error=result.error,
                attempts=attempt,
            )
        except FileTooLarge:
            raise
        except (NetworkError, HttpStatusError, IntegrityError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                delay = options.retry_delay * attempt
                logger.debug(
                    "retrying %s in %.1fs after error: %s",
                    candidate.link.filename,
                    delay,
                    exc,
                )
                if delay > 0:
                    time.sleep(delay)
                continue
            raise
    raise DownloadError(f"download failed: {last_error}")


def _download_candidates(
    client: HttpClient,
    candidates: Sequence[Candidate],
    destination: Path,
    options: Options,
    progress: ProgressCallback | None,
) -> DownloadResult:
    """Try preferred artifacts from the newest usable version."""
    best_version = candidates[0].version
    errors: list[str] = []
    for candidate in candidates:
        if candidate.version != best_version:
            break
        try:
            return _download_with_retries(client, candidate, destination, options, progress)
        except (PipdownError, OSError) as exc:
            errors.append(f"{candidate.link.filename}: {exc}")
            logger.debug("candidate failed: %s: %s", candidate.link.filename, exc)
            continue
    raise DownloadError(f"all candidates for version {best_version} failed: {'; '.join(errors)}")


class ProgressLike(Protocol):
    """Subset of Rich Progress used by the delayed reporter."""

    def start(self) -> None: ...

    def add_task(
        self, description: str, *, total: int | None = None, start: bool = True
    ) -> TaskID: ...

    def update(
        self,
        task_id: TaskID,
        *,
        total: int | None = None,
        completed: int | None = None,
        advance: int | None = None,
        description: str | None = None,
    ) -> None: ...

    def stop_task(self, task_id: TaskID) -> None: ...

    def stop(self) -> None: ...


def _rich_progress() -> Progress:
    """Create a Rich progress bar that does not start immediately."""
    return Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=Console(stderr=True),
        transient=True,
    )


class DelayedProgress:
    """Start Rich progress only after a run has been active for a delay."""

    def __init__(
        self,
        delay: float = 15.0,
        *,
        progress_factory: Callable[[], ProgressLike] = _rich_progress,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._delay = delay
        self._progress = progress_factory()
        self._clock = clock
        self._started_at = clock()
        self._started = False
        self._completed: dict[TaskID, int] = {}
        self._totals: dict[TaskID, int] = {}

    def add_task(self, description: str) -> TaskID:
        """Register a task without displaying it yet."""
        task_id = self._progress.add_task(description, total=None)
        self._completed[task_id] = 0
        return task_id

    def update(self, task_id: TaskID, advance: int, total: int | None) -> None:
        """Advance a task and optionally set its total byte count."""
        self._completed[task_id] = self._completed.get(task_id, 0) + advance
        if total is not None:
            self._totals[task_id] = total
        self._progress.update(task_id, advance=advance, total=total)

    def finish(self, task_id: TaskID, description: str) -> None:
        """Mark a task complete."""
        completed = self._completed.get(task_id, 0)
        total = self._totals.get(task_id, completed)
        self._progress.update(
            task_id,
            completed=completed,
            total=max(0, total),
            description=description,
        )
        self._progress.stop_task(task_id)

    def tick(self, pending: int) -> None:
        """Start the display if the delay has elapsed and work remains."""
        if self._started or pending <= 0:
            return
        if self._clock() - self._started_at >= self._delay:
            self._progress.start()
            self._started = True

    def close(self) -> None:
        """Stop the display if it was started."""
        if self._started:
            self._progress.stop()
            self._started = False


class ProgressReporter(Protocol):
    """Interface used by the parallel orchestrator."""

    def add_task(self, description: str) -> TaskID: ...

    def update(self, task_id: TaskID, advance: int, total: int | None) -> None: ...

    def finish(self, task_id: TaskID, description: str) -> None: ...

    def tick(self, pending: int) -> None: ...

    def close(self) -> None: ...


def parse_requirement(spec: str) -> Requirement:
    """Parse a name/version requirement, rejecting URLs, extras, and markers."""
    try:
        requirement = Requirement(spec.strip())
    except InvalidRequirement as exc:
        raise InputError(f"invalid requirement {spec!r}: {exc}") from exc
    if requirement.url:
        raise InputError(f"direct URL requirements are not supported: {spec}")
    if requirement.extras:
        raise InputError(f"extras are not supported: {spec}")
    if requirement.marker is not None:
        raise InputError(f"environment markers are not supported: {spec}")
    return requirement


def load_requirements(specs: Sequence[str], input_file: Path | None = None) -> list[Requirement]:
    """Combine CLI specs and an optional input file, preserving order."""
    raw_specs: list[tuple[str, str | None]] = [(spec, None) for spec in specs]
    if input_file is not None:
        try:
            lines = input_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise InputError(f"cannot read input file {input_file}: {exc}") from exc
        for line_number, line in enumerate(lines, start=1):
            stripped = line.split("#", 1)[0].strip()
            if stripped:
                raw_specs.append((stripped, f"{input_file}:{line_number}"))
    requirements: list[Requirement] = []
    seen: set[tuple[str, str]] = set()
    for spec, origin in raw_specs:
        try:
            requirement = parse_requirement(spec)
        except InputError as exc:
            if origin is not None:
                raise InputError(f"{origin}: {exc}") from exc
            raise
        key = (canonicalize_name(requirement.name), str(requirement.specifier))
        if key not in seen:
            seen.add(key)
            requirements.append(requirement)
    return requirements


def _process_requirement(
    requirement: Requirement,
    options: Options,
    destination: Path,
    reporter: ProgressReporter,
    task_id: TaskID,
) -> DownloadResult:
    """Resolve and download one requirement, isolating failures."""
    result: DownloadResult
    try:
        client = make_backend(options.backend, options.timeout)
        try:
            candidates = resolve_requirement(client, requirement, options)
            best = candidates[0]
            logger.debug(
                "selected %s %s (%s)",
                best.link.filename,
                best.version,
                best.kind,
            )
            if options.dry_run:
                result = DownloadResult(
                    requirement=str(requirement),
                    status="dry-run",
                    candidate=best,
                )
            else:

                def progress(advance: int, total: int | None) -> None:
                    reporter.update(task_id, advance, total)

                result = _download_candidates(client, candidates, destination, options, progress)
        finally:
            client.close()
    except Exception as exc:
        logger.debug("requirement failed: %s", requirement, exc_info=True)
        result = DownloadResult(
            requirement=str(requirement),
            status="failed",
            error=str(exc),
        )
    reporter.finish(task_id, f"{requirement} [{result.status}]")
    return result


def download_packages(
    specs: Sequence[str],
    options: Options | None = None,
    *,
    destination: Path | None = None,
    reporter_factory: Callable[[], ProgressReporter] | None = None,
) -> list[DownloadResult]:
    """Download multiple requirements in parallel and return ordered results."""
    resolved_options = options if options is not None else Options()
    requirements = load_requirements(specs)
    if not requirements:
        raise InputError("no package requirements were provided")
    if resolved_options.jobs < 1:
        raise InputError("jobs must be at least 1")
    if resolved_options.backend not in {"httpx", "requests", "pycurl"}:
        raise InputError(f"unsupported backend: {resolved_options.backend}")
    if resolved_options.timeout <= 0:
        raise InputError("timeout must be positive")
    if resolved_options.retries < 0:
        raise InputError("retries must be nonnegative")
    if resolved_options.retry_delay < 0:
        raise InputError("retry-delay must be nonnegative")
    if resolved_options.progress_delay < 0:
        raise InputError("progress-delay must be nonnegative")
    if resolved_options.max_size is not None and resolved_options.max_size <= 0:
        raise InputError("max-size must be positive")
    if resolved_options.no_binary and resolved_options.only_binary:
        raise InputError("--no-binary and --only-binary are mutually exclusive")
    target_directory = (
        (destination if destination is not None else Path.cwd()).expanduser().resolve()
    )
    if not target_directory.is_dir():
        raise InputError(f"destination is not a directory: {target_directory}")

    reporter = (
        reporter_factory()
        if reporter_factory is not None
        else DelayedProgress(resolved_options.progress_delay)
    )
    results: list[DownloadResult | None] = [None] * len(requirements)
    try:
        with ThreadPoolExecutor(max_workers=resolved_options.jobs) as executor:
            futures: dict[Future[DownloadResult], int] = {}
            for index, requirement in enumerate(requirements):
                task_id = reporter.add_task(str(requirement))
                future = executor.submit(
                    _process_requirement,
                    requirement,
                    resolved_options,
                    target_directory,
                    reporter,
                    task_id,
                )
                futures[future] = index
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:
                        results[index] = DownloadResult(
                            requirement=str(requirements[index]),
                            status="failed",
                            error=str(exc),
                        )
                reporter.tick(len(pending))
    finally:
        reporter.close()
    return [result for result in results if result is not None]


def _positive_int(value: str) -> int:
    """Argparse type for positive integers."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _nonnegative_int(value: str) -> int:
    """Argparse type for nonnegative integers."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _positive_float(value: str) -> float:
    """Argparse type for positive floats."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_float(value: str) -> float:
    """Argparse type for nonnegative floats."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _python_version(value: str) -> tuple[int, int]:
    """Argparse type for ``X.Y`` Python versions."""
    match = re.fullmatch(r"(\d+)\.(\d+)", value)
    if match is None:
        raise argparse.ArgumentTypeError(f"invalid Python version: {value!r}")
    return int(match.group(1)), int(match.group(2))


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="pipdown",
        description=(
            "Download Python package distributions into the current directory. "
            "Source archives are preferred; compatible Linux wheels are used as fallback."
        ),
        epilog=(
            "Examples: pipdown requests==2.32.3 six==1.16.0 | "
            "pipdown -i packages.txt -b requests -j 8"
        ),
    )
    parser.add_argument("packages", nargs="*", metavar="PKG", help="package name or name==version")
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        metavar="FILE",
        help="read requirements from FILE, one per line",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=("httpx", "requests", "pycurl"),
        default="httpx",
        help="HTTP backend (default: httpx)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=_positive_int,
        default=4,
        metavar="N",
        help="parallel package downloads (default: 4)",
    )
    parser.add_argument(
        "--index-url",
        action="append",
        metavar="URL",
        help="simple index URL; repeatable, tried in order",
    )
    parser.add_argument(
        "--find-links",
        action="append",
        default=[],
        metavar="PATH_OR_URL",
        help="local directory/file or remote find-links page; repeatable",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=60.0,
        metavar="SECONDS",
        help="HTTP timeout (default: 60)",
    )
    parser.add_argument(
        "--retries",
        type=_nonnegative_int,
        default=2,
        metavar="N",
        help="retries per artifact (default: 2)",
    )
    parser.add_argument(
        "--retry-delay",
        type=_nonnegative_float,
        default=1.0,
        metavar="SECONDS",
        help="base retry delay (default: 1)",
    )
    parser.add_argument(
        "--progress-delay",
        type=_nonnegative_float,
        default=15.0,
        metavar="SECONDS",
        help="show Rich progress only after this many seconds (default: 15)",
    )
    parser.add_argument(
        "--max-size",
        type=_positive_float,
        metavar="MIB",
        help="skip artifacts larger than this many MiB",
    )
    parser.add_argument("--pre", action="store_true", help="allow prerelease versions")
    binary_group = parser.add_mutually_exclusive_group()
    binary_group.add_argument(
        "--no-binary", action="store_true", help="download source distributions only"
    )
    binary_group.add_argument("--only-binary", action="store_true", help="download wheels only")
    parser.add_argument(
        "--python-version",
        type=_python_version,
        metavar="X.Y",
        help="target Python version (default: current)",
    )
    parser.add_argument(
        "--platform",
        action="append",
        default=[],
        metavar="TAG",
        help="target platform tag; repeatable",
    )
    parser.add_argument(
        "--abi",
        action="append",
        default=[],
        metavar="TAG",
        help="target ABI tag; repeatable",
    )
    parser.add_argument(
        "--implementation",
        metavar="IMPL",
        help="target Python implementation, e.g. cp or pp",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="redownload even if a valid file already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and print selections without downloading",
    )
    parser.add_argument("--json", action="store_true", help="print JSON results")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _options_from_args(args: argparse.Namespace) -> Options:
    """Convert parsed CLI arguments into runtime options."""
    target = TargetPython(
        python_version=args.python_version or (sys.version_info.major, sys.version_info.minor),
        implementation=args.implementation or interpreter_name(),
        abis=tuple(args.abi) if args.abi else None,
        platforms=tuple(args.platform) if args.platform else None,
    )
    return Options(
        backend=cast(BackendName, args.backend),
        jobs=args.jobs,
        timeout=args.timeout,
        retries=args.retries,
        retry_delay=args.retry_delay,
        progress_delay=args.progress_delay,
        pre=args.pre,
        no_binary=args.no_binary,
        only_binary=args.only_binary,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        max_size=int(args.max_size * 1024 * 1024) if args.max_size is not None else None,
        indexes=tuple(args.index_url) if args.index_url else (PYPI_INDEX,),
        find_links=tuple(args.find_links),
        target=target,
    )


def _format_size(size: int | None) -> str:
    """Format a byte count for human-readable output."""
    if size is None:
        return "unknown size"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    try:
        requirements = load_requirements(args.packages, args.input)
        if not requirements:
            parser.print_help(sys.stderr)
            return 2
        options = _options_from_args(args)
        results = download_packages([str(requirement) for requirement in requirements], options)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps([result.to_dict() for result in results], indent=2))
    else:
        for result in results:
            if result.status == "failed":
                print(f"FAIL {result.requirement}: {result.error}", file=sys.stderr)
            elif result.status == "dry-run":
                filename = result.candidate.link.filename if result.candidate else "?"
                print(f"DRY {result.requirement} -> {filename}")
            else:
                filename = result.path.name if result.path is not None else "?"
                label = "CACHED" if result.status == "cached" else "OK"
                print(f"{label} {result.requirement} -> {filename} ({_format_size(result.size)})")
        downloaded = sum(result.status == "downloaded" for result in results)
        cached = sum(result.status == "cached" for result in results)
        failed = sum(result.status == "failed" for result in results)
        dry = sum(result.status == "dry-run" for result in results)
        print(f"Done: {downloaded} downloaded, {cached} cached, {dry} dry-run, {failed} failed")
    return 1 if any(result.status == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
