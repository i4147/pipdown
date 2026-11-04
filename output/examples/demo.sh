#!/usr/bin/env bash
# Demonstrate pipdown against live PyPI in a temporary current directory.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

echo "== 1. Dry-run selection (no files written) =="
"$PYTHON" "$HERE/pipdown.py" --dry-run --json six==1.16.0

echo
echo "== 2. Parallel pinned downloads with the default httpx backend =="
"$PYTHON" "$HERE/pipdown.py" -j 2 six==1.16.0 idna==3.7

echo
echo "== 3. Read requirements from an input file (-i) =="
printf '# demo requirements\nsix==1.16.0\n' > requirements.txt
"$PYTHON" "$HERE/pipdown.py" -i requirements.txt

echo
echo "== 4. requests backend =="
"$PYTHON" "$HERE/pipdown.py" -b requests --overwrite six==1.16.0

echo
echo "== 5. pycurl backend (if installed) =="
if "$PYTHON" -c 'import pycurl' 2>/dev/null; then
  "$PYTHON" "$HERE/pipdown.py" -b pycurl --overwrite six==1.16.0
else
  echo "pycurl is not installed; skipping"
fi

echo
echo "== 6. Force wheel fallback with --only-binary =="
"$PYTHON" "$HERE/pipdown.py" --only-binary --overwrite six==1.16.0

echo
echo "== Files downloaded into the current directory =="
ls -lh
