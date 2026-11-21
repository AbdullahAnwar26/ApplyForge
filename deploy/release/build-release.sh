#!/usr/bin/env bash
# Build an ApplyForge release from a clean, committed tree (deploy/README.md section 4).
#
#   deploy/release/build-release.sh [--out DIR] [--skip-frontend-tests]
#
# Produces, in DIR (default: ./release-out):
#   applyforge-<sha>.tar.gz        backend, deploy tooling and docs (git archive: tracked files only)
#   applyforge-<sha>-dist.tar.gz   the frontend build from the same commit
#   applyforge-<sha>.sha256        checksums of both
# Runs on the admin workstation (Linux, macOS, or Git Bash on Windows). Needs git, node/npm, tar, sha256sum.
set -euo pipefail

OUT=release-out
FRONTEND_TESTS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift 2 ;;
    --skip-frontend-tests) FRONTEND_TESTS=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "build-release: $*" >&2; exit 1; }
for t in git tar sha256sum npm node; do command -v "$t" >/dev/null || die "$t not found on PATH"; done
TOP=$(git rev-parse --show-toplevel) || die "not inside a git repository"
cd "$TOP"
[ -z "$(git status --porcelain)" ] || die "working tree is not clean; commit or stash first"
SHA=$(git rev-parse HEAD)
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
case "$OUT/" in "$TOP"/*) git check-ignore -q "$OUT" || die "--out inside the repository must be git-ignored (or use a path outside it)";; esac

# Vite inlines every VITE_* variable into the public bundle: refuse any frontend env file that could set one.
for f in frontend/.env frontend/.env.*; do
  [ -e "$f" ] || continue
  case "$f" in *.example) continue ;; esac
  die "$f exists; it could inline VITE_* values into the bundle. Move it away for the build."
done
[ -z "${VITE_API_BASE_URL:-}" ] || die "VITE_API_BASE_URL is set in the environment; unset it (same-origin /api/v1 is required)"

TAR="$OUT/applyforge-$SHA.tar.gz"
DIST="$OUT/applyforge-$SHA-dist.tar.gz"
[ ! -e "$TAR" ] && [ ! -e "$DIST" ] || die "release $SHA already exists in $OUT"

WORK=$(mktemp -d)
DONE=0
# A failed build leaves no partial release behind (a later run would otherwise refuse or ship it).
trap 'rm -rf "$WORK"; [ $DONE = 1 ] || rm -f "$TAR" "$DIST" "$OUT/applyforge-$SHA.sha256"' EXIT

echo "== git archive $SHA"
git archive --format=tar.gz --prefix="$SHA/" -o "$TAR" "$SHA"
if tar -tzf "$TAR" | grep -E '(^|/)\.env$|db\.sqlite3$' >/dev/null; then die "archive contains .env or a database"; fi

echo "== frontend build (clean checkout of $SHA)"
git archive --format=tar "$SHA" frontend | tar -xf - -C "$WORK"
(
  cd "$WORK/frontend"
  npm ci --no-audit --no-fund
  if [ "$FRONTEND_TESTS" = 1 ]; then npm run lint; npm run typecheck; npm test; fi
  npm run build
  [ -f dist/index.html ] || { echo "dist/index.html missing" >&2; exit 1; }
  tar -czf "$DIST" dist
)

echo "== checksums"
( cd "$OUT" && sha256sum "applyforge-$SHA.tar.gz" "applyforge-$SHA-dist.tar.gz" > "applyforge-$SHA.sha256" )
cat "$OUT/applyforge-$SHA.sha256"
DONE=1
echo "release $SHA ready in $OUT"
