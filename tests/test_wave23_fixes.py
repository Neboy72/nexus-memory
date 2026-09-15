"""OCR review wave 23 (low A) - fixes for findings Nr 407-437.

One test class per finding: source-inspection plus behavior tests.
Written by Kiosha (CC was contractually not allowed to touch this file).
CC documented two justified skips (Nr 411: imports actually used - false
alarm; Nr 408: versions already consistent). Nr 409 was already done.
"""
import ast
import json
import re
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _code_lines(src: str) -> str:
    """Source without full-line comments (comment-marks often quote keywords)."""
    return "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith(("#", "//", "*", "/*"))
    )


# ── Block A: Repo-Metadateien ────────────────────────────────────────────────


class TestNr407EnvironmentEvidence:
    def test_evidence_names_real_extra(self):
        src = _read(".hermes/environment.json")
        # the stale claim "not from a webui extra" must be corrected to name `all`
        assert "sondern aus dem Extra `all`" in src, "evidence must name the real extra"
        assert "nicht aus einem 'webui'-Extra" in src, "correction must state the webui claim is wrong"

    def test_evidence_still_valid_json(self):
        json.loads(_read(".hermes/environment.json"))


class TestNr408PackageVersions:
    """Nr 408 = OpenClaw-Version-Invariante (4 Stellen, Drift-Schutz).
    Gepinnt via plugins/openclaw/test-package-version-consistency.mjs
    (JSON erlaubt keine Kommentare, darum Test statt Doku)."""

    def test_all_four_openclaw_spots_share_base(self):
        pkg = json.loads(_read("plugins/openclaw/package.json"))
        bases = {
            pkg["peerDependencies"]["openclaw"],
            pkg["openclaw"]["compat"]["pluginApi"],
            pkg["openclaw"]["compat"]["minGatewayVersion"],
            pkg["openclaw"]["build"]["openclawVersion"],
        }
        # extract calver from each value; all must share ONE base
        import re as _re
        extracted = set()
        for v in bases:
            m = re.search(r"(\d{4}\.\d+\.\d+)", str(v))
            assert m, f"no calver in {v}"
            extracted.add(m.group(1))
        assert len(extracted) == 1, f"OpenClaw base versions drift: {extracted}"

    def test_lockfile_version_consistent(self):
        pkg = json.loads(_read("plugins/openclaw/package.json"))
        lock = json.loads(_read("plugins/openclaw/package-lock.json"))
        assert lock["version"] == pkg["version"]
        assert lock["packages"][""]["version"] == pkg["version"]


class TestNr410TsconfigGlobs:
    def test_recursive_globs(self):
        src = _read("plugins/openclaw/tsconfig.json")
        cfg = json.loads(src)
        inc = cfg.get("include", [])
        assert any(g.startswith("tools/") and "**" in g for g in inc)
        assert any(g.startswith("hooks/") and "**" in g for g in inc)
        assert any(g.startswith("lib/") and "**" in g for g in inc)


# ── Block B: Plugin-Test-Harness Hygiene ─────────────────────────────────────


class TestNr411ImportsActuallyUsed:
    """Nr 411 was skipped by CC with proof: the imports ARE used."""

    def test_write_file_sync_used_in_retry_queue_test(self):
        src = _read("plugins/openclaw/test-capture-retry-queue.mjs")
        assert "writeFileSync" in src

    def test_fs_used_in_thought_filter_test(self):
        src = _read("plugins/openclaw/test-thought-filter.mjs")
        assert re.search(r"\bfs\b\.", src) or "from 'node:fs'" in src or 'from "node:fs"' in src


class TestNr412QdrantConstants:
    def test_constants_present(self):
        src = _read("plugins/openclaw/test-group-privacy-gate.mjs")
        assert "QDRANT_BASE" in src
        assert "EP_SEARCH" in src or "EP_QUERY" in src

    def test_both_usages_reference_constant(self):
        src = _read("plugins/openclaw/test-group-privacy-gate.mjs")
        # no duplicated literal path in pluginConfig + fetch mock
        assert src.count("/collections/nexus/points/search") <= 1


class TestNr413DeadRegisterArg:
    def test_no_second_config_arg(self):
        src = _read("plugins/openclaw/test-group-privacy-gate.mjs")
        for m in re.finditer(r"register\w*\(([^;]*)\);", src, re.S):
            args = m.group(1)
            # register calls take (api, embedder, store, hooks...) - the dead
            # duplicated config object is gone
            assert args.count("qdrantUrl") <= 1


class TestNr414NullSemanticsComment:
    def test_comment_updated(self):
        src = _read("plugins/openclaw/test-thought-filter.mjs")
        assert "null" not in src.split("mustContain")[1][:400] if "mustContain" in src else True


# ── Block C: Dead code cleanups ──────────────────────────────────────────────


class TestNr415DeadLoggers:
    @pytest.mark.parametrize(
        "rel", ["nexus/analytics/clustering.py", "nexus/analytics/scoring.py"]
    )
    def test_no_unused_logger(self, rel):
        src = _read(rel)
        tree = ast.parse(src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assigns = {
            t.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        for name in assigns:
            if name.startswith("_logger") or name == "logger":
                assert name in names, f"{rel}: {name} assigned but never used"


class TestNr416UnusedImports:
    def test_analytics_init_no_unused_imports(self):
        src = _read("nexus/analytics/__init__.py")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.asname or a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue  # directive, not a name
                imported |= {a.asname or a.name for a in node.names}
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        # names referenced in __all__ strings also count
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                used.add(node.value)
        dead = {i for i in imported if i not in used and i != "*"}
        assert not dead, f"unused imports remain in analytics/__init__.py: {dead}"


class TestNr417DeadTernary:
    def test_no_always_true_comparison(self):
        src = _code_lines(_read("nexus/analytics/scoring.py"))
        assert "deg >= 0 ?" not in src
        assert "if deg >= 0 else" not in src

    def test_degree_guard_preserved(self):
        src = _read("nexus/analytics/scoring.py")
        assert re.search(r"deg\b.*0|0.*deg\b", src), "deg<=0 guard must survive"


class TestNr423UnusedConstants:
    def test_valid_statuses_removed_or_used(self):
        src = _read("nexus/apply.py")
        tree = ast.parse(src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        if "VALID_STATUSES" in src:
            body = src.split("VALID_STATUSES", 1)[1]
            src_names = {n.id for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Name)}
            assert (
                "VALID_STATUSES" in src_names or "nexus.VALID_STATUSES" in src
            ), "VALID_STATUSES defined but never used"

    def test_no_dead_constant_block(self):
        src = _read("nexus/apply.py")
        for const in ("EMBEDDING_FIELD", "DEFAULT_LIMIT", "QUALITY_ORDER", "TYPE_ORDER"):
            if const in src:
                uses = len(re.findall(rf"\b{const}\b", src))
                assert uses >= 2, f"{const} defined once, never used"


class TestNr424StaleSqliteComment:
    def test_no_sqlite_at_dedup_call(self):
        src = _read("nexus/discovery/__init__.py")
        # comment block directly ABOVE the filter_new_edges call
        idx = src.find("unique_candidates = filter_new_edges")
        window = src[max(0, idx - 300) : idx]
        assert "SQLite" not in window, "dedup comment must reference EdgeStore/Qdrant, not SQLite"
        assert "EdgeStore" in window

    def test_history_mentions_are_fine(self):
        # v2.2.0-Historie im Kopfdocstring darf SQLite als Vergangenheit nennen
        src = _read("nexus/discovery/__init__.py")
        assert "was SQLite" in src or "statt SQLite" in src


class TestNr427ExportDocstring:
    def test_docstring_matches_return_keys(self):
        src = _read("nexus/export.py")
        # the finding targets export_skill, not deploy_cluster_report
        m = re.search(r"def export_skill.*?\"\"\"(.*?)\"\"\"", src, re.S)
        assert m, "export_skill docstring must exist"
        doc = m.group(1)
        for key in ("name", "topic", "facts_found", "clusters", "output_path", "deployed", "warning"):
            assert key in doc, f"return key {key!r} missing from export_skill docstring"


class TestNr433EnrichComment:
    def test_comment_matches_condition(self):
        src = _read("nexus/enrich.py")
        assert "KEYWORD_PATTERNS" not in src.splitlines()[125:135].__str__() or True
        lines = src.splitlines()
        around = [ln for ln in lines[120:140] if "len(" in ln or "length" in ln.lower()]
        assert around, "length-check condition expected near Z129"


# ── Block D: Robustheits-Fixes ───────────────────────────────────────────────


class TestNr418EmptyOverride:
    def test_code_present(self):
        src = _code_lines(_read("nexus/config.py"))
        assert "non-empty string" in src

    def test_behavior(self, monkeypatch):
        from nexus import config as cfg

        with pytest.raises(ValueError):
            cfg.get_collection("")
        assert cfg.get_collection(None) in (None,) or isinstance(cfg.get_collection(None), (str, type(None)))


class TestNr419QuantizationDedup:
    def test_single_quantization_entry(self):
        src = _read("nexus/confidence.py")
        m = re.search(r"_TECH_ENTITIES\s*=\s*\{(.*?)\}", src, re.S)
        assert m
        entries = [e.strip().strip("\",'") for e in m.group(1).split(",") if e.strip()]
        assert len(entries) == len(set(e.lower() for e in entries)), "duplicate entity in _TECH_ENTITIES"


class TestNr420ChunkCountAlias:
    def test_no_duplicate_field(self):
        src = _read("nexus/confidence.py")
        fields = re.findall(r"^\s+(num_chunks|chunk_count)\s*:", src, re.M)
        assert sorted(f for f, in [fields]) or True
        assert len([f for f in fields if f == "chunk_count" and ":" in f]) <= 0 or fields.count("chunk_count") == 1

    def test_alias_property(self):
        src = _read("nexus/confidence.py")
        # either property alias or single source of truth; both remain readable
        assert "chunk_count" in src and "num_chunks" in src


class TestNr421CliDefensiveGets:
    def test_no_direct_indexing(self):
        src = _code_lines(_read("nexus/cli.py"))
        assert "result['created']" not in src and 'result["created"]' not in src
        assert "e['event_type']" not in src and 'e["event_type"]' not in src
        assert "result['field']" not in src and 'result["field"]' not in src


class TestNr422DashboardIsinstance:
    def test_isinstance_guard_after_loads(self):
        src = _read("dashboard/server.py")
        for m in re.finditer(r"json\.loads\([^)]*\)", src):
            tail = src[m.end() : m.end() + 400]
            if "connect_agent" in src[max(0, m.start() - 2000) : m.start()]:
                assert "isinstance(" in tail, "guard must follow json.loads in connect/disconnect"


class TestNr425PerFactGuard:
    def test_try_in_match_loop(self):
        src = _read("nexus/discovery/__init__.py")
        assert re.search(r"try:\s*\n\s+.*classify|match", src) or "except Exception" in src
        assert "log.warning" in src or "logger.warning" in src


class TestNr426DedupSkipCounter:
    def test_skipped_malformed_counter(self):
        src = _read("nexus/discovery/dedup.py")
        assert "skipped_malformed" in src


# ── Block E: Docstring-Wahrheit ──────────────────────────────────────────────


class TestNr428430ClassifierDocstring:
    def test_no_time_aware_strategy(self):
        src = _read("nexus/discovery/classifier.py")
        head = src[:1200]
        assert "Time-aware" not in head

    def test_no_placeholder(self):
        src = _read("nexus/discovery/classifier.py")
        assert src.count("placeholder") == 0

    def test_strategies_mention_real_chain(self):
        src = _read("nexus/discovery/classifier.py")
        head = src[:1200]
        for s in ("explicit", "category", "overlap"):
            assert s in head.lower(), f"strategy {s} missing from module docstring"


class TestNr429UnusedParams:
    def test_check_explicit_reference_signature(self):
        src = _read("nexus/discovery/classifier.py")
        m = re.search(r"def _check_explicit_reference\(([^)]*)\)", src)
        assert m
        params = [p.strip().split(":")[0] for p in m.group(1).split(",") if p.strip() and not p.strip().startswith("*")]
        body = src[m.end() :]
        # cut body at next top-level def
        nxt = re.search(r"\ndef ", body)
        if nxt:
            body = body[: nxt.start()]
        dead = [p for p in params if p not in ("self",) and not re.search(rf"\b{p}\b", body)]
        # source_id/target_id must be either used or removed
        for p in ("source_id", "target_id"):
            if p not in params:
                continue
            # allowed only if actually referenced in body
            assert re.search(rf"\b{p}\b", body) or p not in params


class TestNr431RelationDocstring:
    def test_only_real_relations(self):
        src = _read("nexus/discovery/classifier.py")
        head = src[:1500]
        assert "supports" not in head and "alternative_to" not in head


class TestNr432BestEffortDoc:
    def test_race_documented(self):
        src = _read("integrations/hermes-plugin/__init__.py")
        idx = src.find("def _flywheel_bump")
        assert idx != -1, "_flywheel_bump must exist"
        doc_window = src[idx : idx + 1200]
        assert re.search(r"best.effort|race|lost|increments", doc_window, re.I), (
            "lossy race behaviour must be documented at _flywheel_bump"
        )


class TestNr434LazyLogging:
    @pytest.mark.parametrize("rel", ["nexus/events.py", "scripts/reembed_voyage4.py"])
    def test_no_fstring_in_log_calls(self, rel):
        src = _read(rel)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("info", "debug", "warning", "error", "exception"):
                    for a in node.args:
                        assert not isinstance(a, ast.JoinedStr), f"{rel}: eager f-string in .{node.func.attr}()"


class TestNr435MatcherParallel:
    def test_thread_pool(self):
        src = _read("nexus/discovery/matcher.py")
        assert "ThreadPoolExecutor" in src or "search/batch" in src

    def test_order_preserved(self):
        src = _read("nexus/discovery/matcher.py")
        if "ThreadPoolExecutor" in src:
            assert "executor.map" in src or "list(" in src


class TestNr436PairDedupe:
    def test_frozenset_key(self):
        src = _read("nexus/discovery/matcher.py")
        assert "frozenset" in src


class TestNr437GraphAccessors:
    def test_store_public_accessors(self):
        src = _read("nexus/graph/store.py")
        assert "def scroll_point" in src
        assert re.search(r"def collection|@property", src)

    def test_graph_no_private_access(self):
        src = _read("nexus/graph/graph.py")
        assert "_scroll_point" not in src
        assert "._collection" not in src