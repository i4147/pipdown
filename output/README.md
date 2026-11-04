# pipdown — unified PyPI package downloader

`pipdown.py` is a single, fully type-annotated command line script that downloads
Python package distributions from PyPI or PyPI-compatible mirrors. It consolidates
the useful ideas from the repository's earlier scripts (`pipdown.py`, `pipget.py`,
`pget.py`, `simple_pipdown.py`, and `pip_downloader.py`) into one tested tool.

## Features

- **Backends:** `httpx` (default), `requests`, and `pycurl`.
- **Multiple requirements:** `pkg`, `pkg==version`, and version ranges such as
  `pkg>=1,<2`.
- **Input file:** `-i/--input FILE`, one requirement per line, with blank lines and
  `#` comments ignored. CLI requirements and file requirements are combined and
  deduplicated.
- **Flat destination:** downloaded files are saved directly into the current working
  directory. No package subdirectories are created.
- **Source-first selection:** for the newest usable version, prefer `.tar.gz`, then
  `.zip`, then `.tar.bz2`. If that version has no supported source archive, fall back
  to the best compatible wheel.
- **Linux/current-Python wheel tags:** wheel compatibility is computed with
  `packaging.tags`. On Linux the running interpreter's native tags are used. On
  non-Linux hosts, conservative manylinux2014 tags for the current machine
  architecture are used unless overridden.
- **Delayed Rich progress:** progress is shown only if the run is still active after
  15 seconds by default (`--progress-delay`). Short runs stay quiet.
- **Parallel downloads:** `-j/--jobs` controls concurrent package downloads.
- **Integrity:** published hashes are verified, archives are validated without
  extraction, and files are published atomically from temporary files.
- **Mirrors and offline sources:** repeatable `--index-url` and `--find-links`.
- **Extras:** dry-run, JSON output, retries, timeout, size limit, prerelease policy,
  source-only/wheel-only modes, target Python/platform overrides, caching, and
  overwrite.

This tool downloads only the requested distributions. It does **not** resolve or
download dependencies.

## Installation

Python 3.10+ is required.

```bash
cd output
python -m pip install -r requirements.txt

# Optional backends:
python -m pip install -r requirements-backends.txt

# Development/test dependencies:
python -m pip install -r requirements-dev.txt
```

`pycurl` may require system libcurl/OpenSSL development headers on platforms without
a suitable wheel.

## Quick start

Run from the directory where you want the package files to land:

```bash
python /path/to/output/pipdown.py requests==2.32.3 six==1.16.0
```

Read requirements from a file:

```bash
python /path/to/output/pipdown.py -i requirements.txt
```

Example `requirements.txt`:

```text
# comments are ignored
six==1.16.0
idna==3.7
requests==2.32.3
```

Choose a backend and parallelism:

```bash
python /path/to/output/pipdown.py -b requests -j 8 six==1.16.0 idna==3.7
python /path/to/output/pipdown.py -b pycurl -j 4 six==1.16.0
```

Use a mirror or additional simple indexes:

```bash
python /path/to/output/pipdown.py \
  --index-url https://pypi.org/simple/ \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple/ \
  six==1.16.0
```

Use local files/directories as offline sources:

```bash
python /path/to/output/pipdown.py --find-links ./wheelhouse --find-links ./sdists demo
```

Preview what would be downloaded:

```bash
python /path/to/output/pipdown.py --dry-run --json requests==2.32.3
```

Force wheels or source archives:

```bash
python /path/to/output/pipdown.py --only-binary six==1.16.0
python /path/to/output/pipdown.py --no-binary six==1.16.0
```

## Important CLI notes

- `-i` means **input requirement file**, not index URL. Use `--index-url` for indexes.
- The destination is always the current working directory. The `output/` folder in
  this repository contains the deliverable code, not runtime downloads.
- Indexes are tried in order. The first source that yields a compatible candidate is
  used. `--find-links` sources are searched before indexes.
- Existing valid files are reused as cached. Invalid existing files are replaced.
  Use `--overwrite` to force redownload of valid files.
- Exit codes: `0` success, `1` one or more package failures, `2` input/configuration
  error, `130` interrupted.

## Selection policy

1. Parse PEP 691 JSON or PEP 503 HTML simple pages.
2. Filter by project name, version specifier, `Requires-Python`, yanked status, and
   supported archive/wheel tags.
3. Choose the newest version that has at least one usable artifact.
4. Within that version, prefer source archives in this order: `.tar.gz`, `.zip`,
   `.tar.bz2`.
5. If no supported source archive exists for that version, choose the best compatible
   wheel by tag priority.
6. If the preferred artifact fails integrity checks or download, other artifacts from
   the same version are tried before failing the requirement.

Prereleases are excluded unless requested by an explicit prerelease pin or `--pre`.
Yanked releases are excluded.

## Progress behavior

Rich progress is delayed. If all work finishes before `--progress-delay` seconds, no
progress bar is printed. If work remains after the delay, a Rich progress display is
started on stderr and updated with byte counts, totals when known, transfer speed,
and estimated remaining time.

```bash
# Show progress immediately (useful for demos/tests):
python /path/to/output/pipdown.py --progress-delay 0 six==1.16.0
```

## Python API

```python
from pathlib import Path
from pipdown import Options, TargetPython, download_packages

results = download_packages(
    ["six==1.16.0", "idna==3.7"],
    Options(backend="httpx", jobs=4),
    destination=Path.cwd(),
)
for result in results:
    print(result.to_dict())
```

## Tests and quality checks

From the `output/` directory:

```bash
python -m pytest
python -m mypy
python -m ruff check .
python -m ruff format --check .
```

The test suite is offline: it uses local find-links directories and a local HTTP
server to exercise JSON indexes, all three backends, selection rules, integrity
checks, caching, fallback, parallelism, delayed progress, and the CLI.

## Demo

A live demo script is provided:

```bash
bash output/examples/demo.sh
```

It creates a temporary directory, changes into it, and demonstrates dry-run,
parallel pinned downloads, input files, backend switching, and wheel-only mode.
