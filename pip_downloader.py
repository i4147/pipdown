#!/data/data/com.termux/files/usr/bin/env python

import sys
from pathlib import Path
from urllib.parse import quote

import httpx

PREFERRED_EXTENSIONS = (".tar.gz", ".tar.bz2", ".zip")


def main():
    if len(sys.argv) != 2:
        msg = f"Usage: python {sys.argv[0]} <package-list-file>"
        raise SystemExit(msg)
    package_file = Path(sys.argv[1])
    output_dir = Path("downloads")
    output_dir.mkdir(exist_ok=True)
    packages = [
        line.strip()
        for line in package_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    with httpx.Client(
        follow_redirects=True,
        timeout=60.0,
        headers={"User-Agent": "pypi-package-downloader/1.0"},
    ) as client:
        for package in packages:
            try:
                api_url = f"https://pypi.org/pypi/{quote(package, safe='')}/json"
                response = client.get(api_url)
                response.raise_for_status()
                data = response.json()
                files = data.get("urls", [])
                selected = None
                for extension in PREFERRED_EXTENSIONS:
                    selected = next(
                        (item for item in files if item.get("filename", "").lower().endswith(extension)),
                        None,
                    )
                    if selected:
                        break
                if selected is None:
                    selected = next(
                        (item for item in files if item.get("filename", "").lower().endswith(".whl")),
                        None,
                    )
                if selected is None:
                    print(f"SKIP {package}: no supported archive or wheel for latest release")
                    continue
                filename = Path(selected["filename"]).name
                destination = output_dir / filename
                with client.stream("GET", selected["url"]) as download:
                    download.raise_for_status()
                    with destination.open("wb") as file:
                        for chunk in download.iter_bytes():
                            file.write(chunk)
                print(f"Downloaded {package} {data['info']['version']}: {destination}")
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                print(f"ERROR {package}: {exc}")


if __name__ == "__main__":
    main()
