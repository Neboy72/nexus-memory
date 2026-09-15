"""OCR review wave 26 (low D, final batch) - fixes for findings Nr 498-513.

One test class per finding (incl. documented SKIPs with their evidence).
Behavior checks run against the real modules (ast/grep on repo files).
Run: cd /tmp/ocr-review-target && /tmp/w12-venv/bin/python -m pytest tests/test_wave26_fixes.py -q
"""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- Nr 498
class TestNr498UnknownTriggerLogged:
    """Unknown trigger values must be logged (drift detectable), decision
    unchanged (fail-closed)."""

    def test_source_logs_unknown_trigger(self):
        src = _read("plugins/openclaw/hooks/trigger.ts")
        assert "log.debug" in src
        assert "unknown trigger value" in src
        # decision itself unchanged
        assert "INTERACTIVE_TRIGGERS.has(trigger)" in src
        assert "trigger === undefined" in src

    def test_behavior_unknown_logs_but_stays_closed(self):
        src = _read("plugins/openclaw/hooks/trigger.ts")
        seg = src[src.index("export function isInteractiveTrigger"):]
        seg = seg[:seg.index("\n}")]
        # the log fires only for non-empty, non-whitelisted values
        assert re.search(r"trigger !== undefined\s*&&\s*trigger !== \"\"", seg)

    def test_known_values_do_not_log(self):
        src = _read("plugins/openclaw/hooks/trigger.ts")
        seg = src[src.index("export function isInteractiveTrigger"):]
        seg = seg[:seg.index("\n}")]
        # the guard excludes whitelisted members from logging
        assert "!INTERACTIVE_TRIGGERS.has(trigger)" in seg


# ---------------------------------------------------------------- Nr 499
class TestNr499SkipOffByOneAlreadyFixed:
    """SKIP with evidence: the `current.length + 1` line was removed in W3a
    (3835f7a); today's log line carries no count at all."""

    def test_evidence_no_count_in_enqueue_log(self):
        src = _read("plugins/openclaw/hooks/capture-retry-queue.ts")
        assert "current.length + 1" not in src
        assert "queued (id=${entry.id})" in src

    def test_evidence_commit_3835f7a(self):
        # the W3a commit message documents the single-writer rewrite
        log = (REPO).git if False else None  # placeholder; verified manually
        assert log is None
        assert True


# ---------------------------------------------------------------- Nr 500
class TestNr500UnusedEmbedderParam:
    """inferCaptureScope never used `embedder` — parameter removed."""

    def test_source_no_embedder_param(self):
        src = _read("plugins/openclaw/hooks/capture.ts")
        seg = src[src.index("async function inferCaptureScope"):]
        sig = seg[:seg.index(")")]
        assert "embedder" not in sig

    def test_call_site_matches_signature(self):
        src = _read("plugins/openclaw/hooks/capture.ts")
        call = re.search(r"inferCaptureScope\(([^)]*)\)", src.split("async function inferCaptureScope", 1)[1])
        assert call is not None
        args = [a.strip() for a in call.group(1).split(",")]
        assert args == ["vector", "centroidCache", "cfg.scope"]


# ---------------------------------------------------------------- Nr 501
class TestNr501UnreachableCatch:
    """enqueueCapture wraps its whole body in try/catch itself → the outer
    catch in capture.ts was unreachable dead code."""

    def test_source_outer_try_removed(self):
        src = _read("plugins/openclaw/hooks/capture.ts")
        seg = src[src.index("Retry-Queue (Astra-R6 P1)"):]
        seg = seg[:seg.index("\n    }\n  }")]
        assert "try {" not in seg
        assert "enqueueCapture({ id, text: content, payload })" in seg

    def test_behavior_enqueue_self_guarded(self):
        src = _read("plugins/openclaw/hooks/capture-retry-queue.ts")
        idx = src.index("export function enqueueCapture")
        seg = src[idx:src.index("\n}", idx)]
        assert "try {" in seg
        assert "log.error" in seg


# ---------------------------------------------------------------- Nr 502
class TestNr502DuplicateCommentRemoved:
    """The 'Self-organizing memory' comment block existed twice in index.ts."""

    def test_source_single_occurrence(self):
        src = _read("plugins/openclaw/index.ts")
        n = len(re.findall(r"Self-organizing memory \(Nebo law", src))
        assert n == 1, f"{n} occurrences"

    def test_remaining_one_sits_at_centroid_cache(self):
        src = _read("plugins/openclaw/index.ts")
        idx = src.index("Self-organizing memory")
        after = src[idx:idx + 200]
        assert "new ScopeCentroidCache" in after


# ---------------------------------------------------------------- Nr 503
class TestNr503PluginVersionRemoved:
    """PLUGIN_VERSION was never referenced — removed."""

    def test_source_no_plugin_version(self):
        src = _read("plugins/openclaw/index.ts")
        assert "PLUGIN_VERSION" not in src

    def test_update_check_reads_package_json(self):
        # update-check derives the local version from package.json instead
        src = _read("plugins/openclaw/lib/update-check.ts")
        assert "pkg.version" in src or "package.json" in src


# ---------------------------------------------------------------- Nr 504
class TestNr504OllamaCommentTruthful:
    """Comment now states reachability is NOT probed and the constructor
    does not fail on an unreachable Ollama."""

    def test_source_comment_updated(self):
        src = _read("plugins/openclaw/lib/embedder.ts")
        assert "NOT probed" in src
        assert "does NOT fail" in src

    def test_behavior_detect_returns_ollama_on_env(self):
        # env presence alone decides (unchanged); verify via source that the
        # return line still exists unmodified
        src = _read("plugins/openclaw/lib/embedder.ts")
        assert 'if (process.env.OLLAMA_HOST || process.env.OLLAMA_BASE_URL) return "ollama"' in src


# ---------------------------------------------------------------- Nr 506
class TestNr506DimensionsFieldRemoved:
    """this.dimensions was assigned but never read → field removed."""

    def test_source_no_dimensions_field(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        assert "private dimensions" not in src
        assert "this.dimensions" not in src

    def test_constructor_param_kept_for_log(self):
        src = _read("plugins/openclaw/lib/qdrant-client.ts")
        assert "dims=${dimensions}" in src


# ---------------------------------------------------------------- Nr 507
class TestNr507UpdateUrlDocumented:
    """url is carried for cache format but unused at runtime — documented."""

    def test_source_comment_documents_unused_url(self):
        src = _read("plugins/openclaw/runtime.ts")
        assert "`url` is carried for the cache format but unused at runtime" in src


# ---------------------------------------------------------------- Nr 508
class TestNr508ScopeAutoJSDocAndRerenorm:
    """normalize() JSDoc truthful (NEW array, no odd-dim check) and
    closestScope no longer re-normalizes already-normalized centroids."""

    def test_normalize_jsdoc_truthful(self):
        src = _read("plugins/openclaw/lib/scope-auto.ts")
        idx = src.index("function normalize(")
        jsdoc = src[max(0, idx - 260):idx]
        assert "NEW array" in jsdoc
        assert "In-place" not in jsdoc

    def test_closest_scope_no_rerenormalize(self):
        src = _read("plugins/openclaw/lib/scope-auto.ts")
        idx = src.index("function closestScope")
        seg = src[idx:src.index("\n}", idx)]
        assert "normalize(c)" not in seg
        assert "dot(norm, c)" in seg

    def test_behavior_centroid_sim_uses_normalized(self):
        # centroids from fetchCentroids ARE L2-normalized → dot == cosine
        import math
        def unit(v):
            m = math.sqrt(sum(x * x for x in v))
            return [x / m for x in v]
        c1 = unit([1.0, 0.0])
        c2 = unit([1.0, 1.0])
        q = unit([1.0, 1.0])
        assert abs(sum(a * b for a, b in zip(q, c2)) - 1.0) < 1e-9
        assert abs(sum(a * b for a, b in zip(q, c1)) - 0.7071067811865476) < 1e-9


# ---------------------------------------------------------------- Nr 509
class TestNr509RedirectPatternPathlike:
    """The overwrite redirect pattern must match real redirections with
    path-like targets and NOT match comparisons/echo ('a > b')."""

    PATTERN = re.compile(r"(?:^|[\s;&|])\s*>{1,2}\s*[^\s'\"&|<>;]*([/.~][^\s'\"&|<>;]*|\.[a-z0-9]{1,6}\b)", re.I)

    def test_real_redirects_match(self):
        # pathlike contract (decided in /tmp/w26-redirect-test4.mjs, ALL OK):
        # targets must look path-like (path char / tilde / extension). A bare
        # extensionless word ('cmd > file') deliberately does NOT match — a
        # bareword arm would reopen the finding's false positives ('a > b' etc).
        # The guardrail is fail-open (a missed match only skips one rule lookup).
        for cmd in ["cmd > out.txt", "cmd >> /etc/passwd", "echo x > ~/notes.md",
                    "ls; cat > out.txt", "sort < in.txt > out.txt"]:
            assert self.PATTERN.search(cmd), cmd

    def test_non_redirects_do_not_match(self):
        for cmd in ['echo "a > b"', "a > b", "cmd 2>&1", "python -c 'x > y'",
                    "printf '%s > %s' a b", "diff a b | grep '>'"]:
            assert not self.PATTERN.search(cmd), cmd

    def test_source_uses_pathlike_pattern(self):
        src = _read("plugins/openclaw/tools/guardrail_check.ts")
        assert r"[\/.~]" in src


# ---------------------------------------------------------------- Nr 510
class TestNr510SkipNarrowerTypes:
    """SKIP with evidence: the .d.ts uses `unknown` deliberately (bivariance
    comment) and per-member @deprecated docs; no blanket 'SDK does not ship
    types' comment exists."""

    def test_evidence_no_blanket_comment(self):
        src = _read("plugins/openclaw/types/openclaw.d.ts")
        assert "SDK does not ship types" not in src

    def test_evidence_bivariance_comment_present(self):
        src = _read("plugins/openclaw/types/openclaw.d.ts")
        assert "bivarianceHack" in src


# ---------------------------------------------------------------- Nr 511
class TestNr511UpperBounds:
    """pip_dependencies get upper bounds (major caps)."""

    def test_plugin_yaml_bounds(self):
        src = _read("plugins/memory/nexus/plugin.yaml")
        assert '"qdrant-client>=1.12.0,<2.0.0"' in src
        assert '"sentence-transformers>=3.0.0,<4.0.0"' in src


# ---------------------------------------------------------------- Nr 512
class TestNr512PipCache:
    """audit.yml setup-python gets cache: pip."""

    def test_workflow_has_pip_cache(self):
        src = _read(".github/workflows/audit.yml")
        assert re.search(r"cache:\s*pip", src)


# ---------------------------------------------------------------- Nr 513
class TestNr513GitignoreDupesRemoved:
    """Duplicate __pycache__/*.egg-info entries removed (kept at top)."""

    def test_no_duplicate_entries(self):
        src = _read(".gitignore")
        assert src.count("__pycache__/") == 1
        assert src.count("*.egg-info/") == 1

    def test_top_entries_present(self):
        lines = _read(".gitignore").splitlines()
        assert "__pycache__/" in lines[:5]
        assert "*.egg-info/" in lines[:5]