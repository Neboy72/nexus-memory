#!/usr/bin/env bash
# Release-Gate: verhindert, dass ein gepusheter Stand ohne Tag/Release bleibt.
# Prüft: pyproject-version == neuester GitHub-Tag (sortiert, nicht alphabetisch!).
# Exit 1 + Klartext bei Mismatch. Alarm-Only — grün = still.
set -euo pipefail
REPO="${NEXUS_REPO:-Neboy72/nexus-memory}"
REPO_DIR="${NEXUS_REPO_DIR:-$HOME/nexus-memory}"
PYPROJECT="$REPO_DIR/pyproject.toml"

# --- Pre-flight: inputs and tools, with an explicit diagnosis each. ---
if [ ! -f "$PYPROJECT" ]; then
    echo "GATE-ERROR: pyproject.toml not found at $PYPROJECT (set NEXUS_REPO_DIR)"
    exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
    echo "GATE-ERROR: 'gh' CLI not installed — cannot read GitHub tags (install gh or skip this gate)"
    exit 1
fi

# --- Local version: tomllib preferred, grep/sed fallback when python3 is missing. ---
LOCAL_VER=""
if command -v python3 >/dev/null 2>&1; then
    LOCAL_VER=$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get("project", {}).get("version", ""))' "$PYPROJECT" 2>/dev/null || true)
fi
if [ -z "$LOCAL_VER" ]; then
    # `|| true`: a missing version line makes grep exit 1; under set -e the
    # substitution would abort the script without any GATE output.
    LOCAL_VER=$(grep -m1 '^version' "$PYPROJECT" | sed 's/version = "\(.*\)"/\1/' || true)
fi

if [ -z "$LOCAL_VER" ]; then
    echo "GATE-ROT: no version found in $PYPROJECT"
    exit 1
fi
if ! printf '%s' "$LOCAL_VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "GATE-ROT: unparsable version '$LOCAL_VER' in $PYPROJECT (expected X.Y.Z)"
    exit 1
fi

# --- Remote tag. No tags at all is NOT the same as "release missing", and a
# failed gh call (missing CLI, unauthenticated on CI) is NOT "no tags". ---
TAGS_LIST=""
if ! TAGS_LIST=$(gh api "repos/$REPO/tags" --paginate --jq '.[].name' 2>/dev/null); then
    echo "GATE-ERROR: 'gh api' failed (gh missing, unauthenticated, or network error) — cannot verify tags for $REPO"
    exit 1
fi
if [ -z "$TAGS_LIST" ]; then
    echo "GATE-ROT: keine Tags in $REPO gefunden (Repository hat noch kein Release)"
    exit 1
fi
# Only FINAL release tags (X.Y.Z, optional "v" prefix) take part — a stray
# non-version tag must not become the "latest release". Pre-release/build
# suffixed tags are deliberately excluded: `sort -V` ranks them *after* the
# plain release (1.0-rc1 > 1.0), so allowing them let e.g. v0.20.0-rc1 win
# the comparison while the normalized version below stripped the suffix —
# a false GATE-GRUEN although no final 0.20.0 release exists.
VERSION_TAGS=$(printf '%s\n' "$TAGS_LIST" | grep -E '^v?[0-9]+\.[0-9]+\.[0-9]+$' || true)
REMOTE_TAG=$(printf '%s\n' "$VERSION_TAGS" | sort -V | tail -1 || true)
if [ -z "$REMOTE_TAG" ]; then
    echo "GATE-ROT: kein finaler Release-Tag (X.Y.Z ohne Pre-Release-Suffix) in $REPO gefunden"
    exit 1
fi
REMOTE_TAG_VER="${REMOTE_TAG#v}"

if [ "$LOCAL_VER" = "$REMOTE_TAG_VER" ]; then
    echo "GATE-GRUEN: pyproject=$LOCAL_VER == Tag=$REMOTE_TAG"
    # HEAD-tagged check: alarm-only. A normal commit between releases is
    # untagged, so this must not turn the gate red (and a shallow clone / CI
    # without local tags simply yields the warning, never a failure).
    HEAD_TAG="$(git -C "$REPO_DIR" describe --tags --exact-match HEAD 2>/dev/null || true)"
    if [ -z "$HEAD_TAG" ]; then
        echo "GATE-WARN: HEAD ist untagged — der aktuelle Commit trägt kein Tag (Push ohne Version-Bump würde sonst still passieren)"
    fi
else
    echo "GATE-ROT: Release fehlt! pyproject=$LOCAL_VER aber neuester Tag=$REMOTE_TAG"
    echo "AKTION: git tag v$LOCAL_VER <commit> && git push origin v$LOCAL_VER && gh release create v$LOCAL_VER --generate-notes"
    exit 1
fi
