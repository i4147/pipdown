"""Tests for delayed Rich progress reporting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from rich.progress import TaskID

from pipdown import DelayedProgress, Options, download_packages
from tests.conftest import LocalServer, Route


@dataclass
class FakeProgress:
    """In-memory stand-in for Rich Progress."""

    started: bool = False
    stopped: bool = False
    tasks: dict[TaskID, dict[str, object]] = field(default_factory=dict)
    finished: set[TaskID] = field(default_factory=set)

    def start(self) -> None:
        self.started = True

    def add_task(self, description: str, *, total: int | None = None, start: bool = True) -> TaskID:
        task_id = cast(TaskID, len(self.tasks))
        self.tasks[task_id] = {
            "description": description,
            "total": total,
            "start": start,
            "completed": 0,
        }
        return task_id

    def update(
        self,
        task_id: TaskID,
        *,
        total: int | None = None,
        completed: int | None = None,
        advance: int | None = None,
        description: str | None = None,
    ) -> None:
        task = self.tasks[task_id]
        if total is not None:
            task["total"] = total
        if completed is not None:
            task["completed"] = completed
        if advance is not None:
            task["completed"] = cast(int, task["completed"]) + advance
        if description is not None:
            task["description"] = description

    def stop_task(self, task_id: TaskID) -> None:
        self.finished.add(task_id)

    def stop(self) -> None:
        self.stopped = True


class FakeClock:
    """Controllable monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_progress_does_not_start_before_delay() -> None:
    progress = FakeProgress()
    clock = FakeClock()
    reporter = DelayedProgress(15.0, progress_factory=lambda: progress, clock=clock)
    task_id = reporter.add_task("demo")
    clock.advance(14.9)
    reporter.tick(1)
    assert progress.started is False
    reporter.update(task_id, 10, 100)
    reporter.finish(task_id, "demo [downloaded]")
    reporter.close()
    assert progress.started is False
    assert progress.stopped is False


def test_progress_starts_after_delay_with_pending_work() -> None:
    progress = FakeProgress()
    clock = FakeClock()
    reporter = DelayedProgress(15.0, progress_factory=lambda: progress, clock=clock)
    task_id = reporter.add_task("demo")
    clock.advance(15.0)
    reporter.tick(1)
    assert progress.started is True
    reporter.update(task_id, 50, 100)
    assert progress.tasks[task_id]["completed"] == 50
    reporter.finish(task_id, "demo [downloaded]")
    assert task_id in progress.finished
    reporter.close()
    assert progress.stopped is True


def test_progress_does_not_start_when_no_work_remains() -> None:
    progress = FakeProgress()
    clock = FakeClock()
    reporter = DelayedProgress(15.0, progress_factory=lambda: progress, clock=clock)
    reporter.add_task("demo")
    clock.advance(30.0)
    reporter.tick(0)
    assert progress.started is False
    reporter.close()


def test_zero_delay_starts_immediately_with_pending_work() -> None:
    progress = FakeProgress()
    clock = FakeClock()
    reporter = DelayedProgress(0.0, progress_factory=lambda: progress, clock=clock)
    reporter.add_task("demo")
    reporter.tick(1)
    assert progress.started is True
    reporter.close()


def _serve_demo_index(http_server: LocalServer, sdist_bytes: bytes, *, delay: float) -> None:
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
    http_server.routes["/simple/demo/"] = Route(
        body=json.dumps(index).encode(),
        content_type="application/vnd.pypi.simple.v1+json",
    )
    http_server.routes["/demo-1.0.tar.gz"] = Route(body=sdist_bytes, delay=delay)


def test_slow_download_starts_delayed_progress(
    http_server: LocalServer, tmp_path: Path, sdist_bytes: bytes
) -> None:
    _serve_demo_index(http_server, sdist_bytes, delay=0.4)
    progress = FakeProgress()
    reporter = DelayedProgress(0.05, progress_factory=lambda: progress)
    results = download_packages(
        ["demo==1.0"],
        Options(indexes=(f"{http_server.url}/simple/",), jobs=1, retries=0),
        destination=tmp_path,
        reporter_factory=lambda: reporter,
    )
    assert results[0].status == "downloaded"
    assert progress.started is True
    assert progress.stopped is True


def test_fast_download_does_not_start_delayed_progress(
    http_server: LocalServer, tmp_path: Path, sdist_bytes: bytes
) -> None:
    _serve_demo_index(http_server, sdist_bytes, delay=0.0)
    progress = FakeProgress()
    reporter = DelayedProgress(10.0, progress_factory=lambda: progress)
    results = download_packages(
        ["demo==1.0"],
        Options(indexes=(f"{http_server.url}/simple/",), jobs=1, retries=0),
        destination=tmp_path,
        reporter_factory=lambda: reporter,
    )
    assert results[0].status == "downloaded"
    assert progress.started is False
    assert progress.stopped is False
