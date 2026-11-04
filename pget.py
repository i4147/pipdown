import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests
from packaging.requirements import Requirement
from pyget2.constants import DEFAULT_TIMEOUT, MAX_WORKERS, PYPI_INDEX


PYPI_INDEX = "https://pypi.org/pypi"
PYPI_SIMPLE = "https://pypi.org/simple"
DEFAULT_TIMEOUT = 100
MAX_WORKERS = 4


class PackageNotFound(Exception):
    pass


class Downloader:
    def __init__(self, output_dir: str = ".", timeout: int = DEFAULT_TIMEOUT, max_workers: int = MAX_WORKERS):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.max_workers = max_workers
        self.session = requests.Session()
    def get_package_info(self, package: str, version: Optional[str] = None) -> dict:
        url = f"{PYPI_INDEX}/{package}/json"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise PackageNotFound(f"Package '{package}' not found: {e}")
        data = resp.json()
        if version and version not in data["releases"]:
            raise PackageNotFound(f"Version {version} not found for {package}")
        return data
    def get_releases(self, package: str, version: Optional[str] = None) -> list[dict]:
        info = self.get_package_info(package, version)
        if version:
            releases = info["releases"].get(version, [])
        else:
            latest = info["info"]["version"]
            releases = info["releases"].get(latest, [])
        return releases
    def _download_file(self, url: str, filename: str) -> tuple[str, bool]:
        filepath = self.output_dir / filename
        try:
            resp = self.session.get(url, timeout=self.timeout, stream=True)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            return str(filepath), True
        except Exception as e:
            return str(filepath), False
    def download(self, package: str, version: Optional[str] = None, parallel: bool = True) -> list[str]:
        releases = self.get_releases(package, version)
        if not releases:
            raise PackageNotFound(f"No distributions found for {package}")
        filtered = self._filter_releases(releases)
        if not filtered:
            filtered = releases
        if parallel and len(filtered) > 1:
            return self._parallel_download(filtered)
        return self._serial_download(filtered)
    def _filter_releases(self, releases: list[dict]) -> list[dict]:
        targz = [r for r in releases if r["filename"].endswith(".tar.gz")]
        return targz
    def _serial_download(self, releases: list[dict]) -> list[str]:
        results = []
        for release in releases:
            path, success = self._download_file(release["url"], release["filename"])
            if success:
                results.append(path)
        return results
    def _parallel_download(self, releases: list[dict]) -> list[str]:
        results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._download_file, r["url"], r["filename"]): r for r in releases}
            for future in as_completed(futures):
                path, success = future.result()
                if success:
                    results.append(path)
        return results


class DependencyResolver:
    def __init__(self, downloader: Downloader):
        self.downloader = downloader
        self.resolved: set[str] = set()
    def resolve(self, package: str, version: str | None = None, max_depth: int = 3) -> set[str]:
        if max_depth == 0 or package in self.resolved:
            return self.resolved
        self.resolved.add(package)
        try:
            info = self.downloader.get_package_info(package, version)
            requires = info["info"].get("requires_dist") or []
        except Exception:
            return self.resolved
        for req_str in requires:
            try:
                req = Requirement(req_str)
                self.resolve(req.name, max_depth=max_depth - 1)
            except Exception:
                pass
        return self.resolved


def main():
    parser = argparse.ArgumentParser(description="Download Python packages from PyPI (.tar.gz preferred)", prog="pyget")
    parser.add_argument("packages", nargs="+", help="Package name(s)")
    parser.add_argument("-v", "--version", help="Specific version")
    parser.add_argument("-o", "--output", default=".", help="Output directory")
    parser.add_argument("-d", "--deps", action="store_true", help="Resolve dependencies")
    parser.add_argument("-w", "--workers", type=int, default=8, help="Parallel workers")
    parser.add_argument("-t", "--timeout", type=int, default=100, help="Request timeout (s)")
    args = parser.parse_args()
    try:
        downloader = Downloader(output_dir=args.output, timeout=args.timeout, max_workers=args.workers)
        all_packages = []
        if args.deps:
            resolver = DependencyResolver(downloader)
            for pkg in args.packages:
                packages = resolver.resolve(pkg, args.version)
                all_packages.extend(packages)
            all_packages = list(dict.fromkeys(all_packages))
            print(f"Resolved {len(all_packages)} packages: {', '.join(sorted(all_packages))}")
        else:
            all_packages = args.packages
        all_files = []
        for pkg in all_packages:
            files = downloader.download(pkg, version=args.version, parallel=True)
            all_files.extend(files)
            print(f"Downloaded {pkg}: {len(files)} file(s)")
        print(f"\nTotal: {len(all_files)} file(s) → {Path(args.output).resolve()}")
    except PackageNotFound as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
