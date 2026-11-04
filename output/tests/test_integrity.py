"""Tests for hash and archive integrity validation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pipdown import IntegrityError, choose_hash, validate_archive, verify_file_hash
from tests.conftest import make_tar_bz2, make_tar_gz, make_zip


def test_choose_hash_prefers_sha256() -> None:
    assert choose_hash({"md5": "a", "sha256": "b", "sha512": "c"}) == ("sha256", "b")
    assert choose_hash({"md5": "a"}) == ("md5", "a")
    assert choose_hash({}) is None


def test_verify_file_hash_success_and_failure(tmp_path: Path) -> None:
    path = tmp_path / "demo-1.0.tar.gz"
    data = make_tar_gz()
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    verify_file_hash(path, {"sha256": digest})
    with pytest.raises(IntegrityError, match="sha256 mismatch"):
        verify_file_hash(path, {"sha256": "0" * 64})


def test_validate_supported_archives(tmp_path: Path) -> None:
    archives = {
        "demo-1.0.tar.gz": make_tar_gz(),
        "demo-1.0.tar.bz2": make_tar_bz2(),
        "demo-1.0.zip": make_zip(),
        "demo-1.0-py3-none-any.whl": make_zip(wheel=True),
    }
    for filename, data in archives.items():
        path = tmp_path / filename
        path.write_bytes(data)
        validate_archive(path)


def test_validate_rejects_corrupt_archives(tmp_path: Path) -> None:
    bad_zip = tmp_path / "demo-1.0-py3-none-any.whl"
    bad_zip.write_bytes(b"not a zip file")
    with pytest.raises(IntegrityError):
        validate_archive(bad_zip)
    bad_tar = tmp_path / "demo-1.0.tar.gz"
    bad_tar.write_bytes(b"not a tar file")
    with pytest.raises(IntegrityError):
        validate_archive(bad_tar)


def test_validate_uses_supplied_filename_for_temp_paths(tmp_path: Path) -> None:
    temp_path = tmp_path / ".demo-1.0.tar.gz.abc.part"
    temp_path.write_bytes(make_tar_gz())
    validate_archive(temp_path, "demo-1.0.tar.gz")
    with pytest.raises(IntegrityError, match="unsupported archive type"):
        validate_archive(temp_path)
