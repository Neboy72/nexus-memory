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
    LOCAL_VER=$(grep -m1 '^version' "$PYPROJECT" | sed 's/version = "\(.*\)"/\1/')
fi

if [ -z "$LOCAL_VER" ]; then
    echo "GATE-ROT: no version found in $PYPROJECT"
    exit 1
fi
if ! printf '%s' "$LOCAL_VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "GATE-ROT: unparsable version '$LOCAL_VER' in $PYPROJECT (expected X.Y.Z)"
    exit 1
fi

# --- Remote tag. No tags at all is NOT the same as "release missing". ---
REMOTE_TAG=$(gh api "repos/$REPO/tags" --paginate --jq '.[].name' 2>/dev/null | sort -V | tail -1 || true)
if [ -z "$REMOTE_TAG" ]; then
    echo "GATE-ROT: keine Tags in $REPO gefunden (Repository hat noch kein Release)"
    exit 1
fi
REMOTE_TAG_VER="${REMOTE_TAG#v}"

if [ "$LOCAL_VER" = "$REMOTE_TAG_VER" ]; then
    echo "GATE-GRUEN: pyproject=$LOCAL_VER == Tag=$REMOTE_TAG"
else
    echo "GATE-ROT: Release fehlt! pyproject=$LOCAL_VER aber neuester Tag=$REMOTE_TAG"
    echo "AKTION: git tag v$LOCAL_VER <commit> && git push origin v$LOCAL_VER && gh release create v$LOCAL_VER --generate-notes"
    exit 1
fi
