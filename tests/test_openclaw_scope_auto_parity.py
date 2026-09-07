"""Tests for the OpenClaw TS scope-auto port (parity with Python scope_auto).

Runs the actual scope-auto.ts logic via esbuild-bundled import (tsx-free:
node with ts stripping via --experimental-strip-types is fragile; instead
we test through the built dist bundle's exported behavior is not possible —
so we validate the math rules by mirroring them in Python and asserting the
TS file content consistency + run node directly on the TS via esbuild).

Simpler and honest: node can import .ts since 22.6 with --experimental-strip-types.
"""
import json
import subprocess
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "openclaw"

NODE_SCRIPT = """
import { inferScope, prefetchFilterScopes } from "./plugins/openclaw/lib/scope-auto.ts";
const cents = { voice: [1.0, 0.0], haushalt: [0.0, 1.0] };
const out = {
  clear: inferScope([0.98, 0.2], cents),
  ambiguous: inferScope([0.71, 0.71], cents),
  empty: inferScope([], cents),
  allowedClear: [...prefetchFilterScopes([0.98, 0.2], cents, "")].sort(),
  allowedManual: [...prefetchFilterScopes([0.98, 0.2], cents, "openclaw-maint")].sort(),
  allowedAmbiguous: prefetchFilterScopes([0.71, 0.71], cents, ""),
};
console.log(JSON.stringify(out));
"""


def test_ts_scope_auto_parity():
    r = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", NODE_SCRIPT],
        capture_output=True, text=True, cwd=PLUGIN.parent.parent, timeout=60,
    )
    assert r.returncode == 0, f"node failed: {r.stderr[-500:]}"
    out = json.loads(r.stdout.strip())
    assert out["clear"] == "voice"
    assert out["ambiguous"] == "default"
    assert out["empty"] == "default"
    assert out["allowedClear"] == ["default", "voice"]
    assert out["allowedManual"] == ["default", "openclaw-maint", "voice"]
    assert out["allowedAmbiguous"] is None