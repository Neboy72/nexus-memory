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
  echo "⚠️  OpenClaw state directory not found at $OPENCLAW_STATE_DIR"
  echo "   Make sure OpenClaw is installed. Creating plugins directory anyway..."
  mkdir -p "$PLUGINS_DIR"
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
if ln -s "$PLUGIN_DIR" "$TARGET_DIR" 2>/dev/null; then
  echo "✅ Symlinked: $TARGET_DIR → $PLUGIN_DIR"
else
  echo "Symlink failed, copying instead..."
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
echo '{'
echo '  "plugins": {'
echo '    "slots": {'
echo '      "memory": "nexus-memory"'
echo '    },'
echo '    "entries": {'
echo '      "nexus-memory": {'
echo '        "enabled": true,'
echo '        "hooks": {'
echo '          "allowPromptInjection": true,'
echo '          "allowConversationAccess": true'
echo '        },'
echo '        "config": {'
echo '          "qdrantUrl": "http://localhost:6333",'
echo '          "collection": "nexus",'
echo '          "embedding": {'
echo '            "provider": "voyage",'
echo '            "model": "voyage-3-large",'
echo '            "apiKey": "${VOYAGE_API_KEY}"'
echo '          },'
echo '          "autoRecall": true,'
echo '          "autoCapture": true,'
echo '          "maxRecallResults": 10,'
echo '          "accessLevel": "public"'
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
echo "   export VOYAGE_API_KEY=\"vo-...\"     (voyage-3-large, 1024d)"
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