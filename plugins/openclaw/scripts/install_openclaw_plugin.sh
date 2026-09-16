#!/usr/bin/env bash
#
# install_openclaw_plugin.sh — One-command install for the Nexus Memory OpenClaw plugin.
#
# Symlinks (or copies) the plugin into the OpenClaw plugins directory and
# prints configuration instructions.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Discover OpenClaw state directory
OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-$HOME/.openclaw}"
PLUGINS_DIR="$OPENCLAW_STATE_DIR/plugins"
TARGET_DIR="$PLUGINS_DIR/nexus-memory"

# Safety guards for the destructive rm -rf further down: never operate on a
# root/empty plugins dir and never remove a target that is not exactly the
# dedicated nexus-memory seat inside it.
case "$PLUGINS_DIR" in
  "/"|""|"//"*) echo "❌ ERROR: root PLUGINS_DIR '$PLUGINS_DIR' — refusing to install"; exit 1;;
esac
if [ "$TARGET_DIR" != "$PLUGINS_DIR/nexus-memory" ] || [ "$(basename "$TARGET_DIR")" != "nexus-memory" ]; then
  echo "❌ ERROR: unexpected target directory '$TARGET_DIR' — refusing to install"; exit 1
fi
case "$TARGET_DIR" in
  "$PLUGINS_DIR"/*) ;;
  *) echo "❌ ERROR: target '$TARGET_DIR' is not under '$PLUGINS_DIR' — refusing to install"; exit 1;;
esac

# The install is remove-then-create: an interrupted run must not silently
# leave a half-created plugin seat behind.
trap 'echo "❌ ERROR: install incomplete — re-run this script (target: $TARGET_DIR)"; exit 1' ERR

# Sanity: PLUGIN_DIR is derived from the script location — verify it really
# holds the OpenClaw plugin manifest before touching the target.
if [ ! -f "$PLUGIN_DIR/openclaw.plugin.json" ]; then
  echo "❌ ERROR: $PLUGIN_DIR does not look like the Nexus Memory OpenClaw plugin"
  echo "   (missing openclaw.plugin.json) — refusing to install"
  exit 1
fi

echo "╔══════════════════════════════════════════════════════╗"
echo "║   Nexus Memory — OpenClaw Plugin Installer           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Plugin source:  $PLUGIN_DIR"
echo "OpenClaw dir:   $OPENCLAW_STATE_DIR"
echo "Target:         $TARGET_DIR"
echo ""

# Check if OpenClaw state directory exists
if [ ! -d "$OPENCLAW_STATE_DIR" ]; then
  echo "❌ ERROR: OpenClaw state directory not found at $OPENCLAW_STATE_DIR"
  echo "   aborting (set OPENCLAW_STATE_DIR oder installiere OpenClaw)"
  exit 1
fi

# Create plugins directory if it doesn't exist
mkdir -p "$PLUGINS_DIR"

# Remove existing target if present
if [ -L "$TARGET_DIR" ]; then
  echo "Removing existing symlink at $TARGET_DIR"
  rm "$TARGET_DIR"
elif [ -d "$TARGET_DIR" ]; then
  echo "Removing existing plugin directory at $TARGET_DIR"
  rm -rf "$TARGET_DIR"
fi

# Try symlink first (preferred — stays in sync with repo)
if ln -s "$PLUGIN_DIR" "$TARGET_DIR"; then
  echo "✅ Symlinked: $TARGET_DIR → $PLUGIN_DIR"
else
  err=$?
  echo "⚠️  symlink failed (exit $err) — falling back to copy"
  # A leftover target would make `cp -r` nest the copy inside it — refuse.
  if [ -e "$TARGET_DIR" ]; then
    echo "❌ ERROR: target already exists after failed symlink — aborting"
    exit 1
  fi
  cp -r "$PLUGIN_DIR" "$TARGET_DIR"
  echo "✅ Copied: $PLUGIN_DIR → $TARGET_DIR"
fi

echo ""
echo "┌──────────────────────────────────────────────────────┐"
echo "│  Configuration                                       │"
echo "└──────────────────────────────────────────────────────┘"
echo ""
echo "Add the following to your OpenClaw config"
echo "(~/.openclaw/openclaw.json):"
echo ""
echo "Least-privilege defaults are used below (prompt injection and conversation"
echo "access disabled, accessLevel \"private\"). Raise them only for an isolated/"
echo "trusted backend."
echo ""
echo "NOTE: embedding config below is a Voyage EXAMPLE - adjust provider/"
echo "model/apiKey if you use OpenAI/Google/Jina/Ollama (see prerequisites)."
echo "OpenClaw interpolates \${VOYAGE_API_KEY} from the process env."
echo ""
echo '{'
echo '  "plugins": {'
echo '    "slots": {'
echo '      "memory": "nexus-memory"'
echo '    },'
echo '    "entries": {'
echo '      "nexus-memory": {'
echo '        "enabled": true,'
echo '        "hooks": {'
echo '          "allowPromptInjection": false,'
echo '          "allowConversationAccess": false'
echo '        },'
echo '        "config": {'
echo '          "qdrantUrl": "http://localhost:6333",'
echo '          "collection": "nexus",'
echo '          "embedding": {'
echo '            "provider": "voyage",'
echo '            "model": "voyage-4",'
echo '            "apiKey": "${VOYAGE_API_KEY}"'
echo '          },'
echo '          "autoRecall": true,'
echo '          "autoCapture": true,'
echo '          "maxRecallResults": 10,'
echo '          "accessLevel": "private"'
echo '        }'
echo '      }'
echo '    }'
echo '  }'
echo '}'
echo ""
echo "┌──────────────────────────────────────────────────────┐"
echo "│  Prerequisites                                       │"
echo "└──────────────────────────────────────────────────────┘"
echo ""
echo "1. Qdrant running at http://localhost:6333"
echo "   Quick start: docker run -p 6333:6333 qdrant/qdrant"
echo ""
echo "2. Embedding provider — set one of:"
echo "   export VOYAGE_API_KEY=\"vo-...\"     (voyage-4, 1024d)"
echo "   export OPENAI_API_KEY=\"sk-...\"     (text-embedding-3-small, 1536d)"
echo "   export GOOGLE_API_KEY=\"AIza...\"    (text-embedding-004, 768d)"
echo "   export JINA_API_KEY=\"jina_...\"     (jina-embeddings-v3, 1024d)"
echo "   # Ollama: no key needed (nomic-embed-text, 768d)"
echo ""
echo "3. Restart OpenClaw gateway:"
echo "   openclaw gateway restart"
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅  Installation complete!                         ║"
echo "╚══════════════════════════════════════════════════════╝"