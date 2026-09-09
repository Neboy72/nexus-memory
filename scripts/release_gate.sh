#!/usr/bin/env bash
# Release-Gate: verhindert, dass ein gepusheter Stand ohne Tag/Release bleibt.
# Prüft: pyproject-version == neuester GitHub-Tag (sortiert, nicht alphabetisch!).
# Exit 1 + Klartext bei Mismatch. Alarm-Only — grün = still.
set -euo pipefail
REPO="${NEXUS_REPO:-Neboy72/nexus-memory}"
REPO_DIR="${NEXUS_REPO_DIR:-$HOME/nexus-memory}"

LOCAL_VER=$(grep -m1 '^version' "$REPO_DIR/pyproject.toml" | sed 's/version = "\(.*\)"/\1/')
REMOTE_TAG=$(gh api "repos/$REPO/tags" --paginate --jq '.[].name' | sort -V | tail -1)
REMOTE_TAG_VER="${REMOTE_TAG#v}"

if [ "$LOCAL_VER" = "$REMOTE_TAG_VER" ]; then
    echo "GATE-GRUEN: pyproject=$LOCAL_VER == Tag=$REMOTE_TAG"
else
    echo "GATE-ROT: Release fehlt! pyproject=$LOCAL_VER aber neuester Tag=$REMOTE_TAG"
    echo "AKTION: git tag v$LOCAL_VER <commit> && git push origin v$LOCAL_VER && gh release create v$LOCAL_VER --generate-notes"
    exit 1
fi