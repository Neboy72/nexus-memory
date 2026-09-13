#!/usr/bin/env python3
"""Lost-in-the-Middle (LitM) benchmark harness for Nexus Memory.

Measures whether reordering the auto-recall (prefetch) injection block moves
needle hits that are "lost in the middle" back into the model's attention.

Spec: benches/litm/SPEC.md
Only fixture data (synthetic) is processed. No other provider than the local
Ollama OpenAI-compatible endpoint is called. Nothing outside benches/litm/ is
touched.

Usage:
    python3 benches/litm/run_bench.py --selftest
    python3 benches/litm/run_bench.py --dry-run
    python3 benches/litm/run_bench.py --varianten base,b1 --model glm-5.3-flash:cloud
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Constants (mirror production plugin behaviour)
# --------------------------------------------------------------------------- #

BENCH_DIR = Path(__file__).resolve().parent
FIXTURES_PATH = BENCH_DIR / "fixtures.jsonl"
RESULTS_PATH = BENCH_DIR / "results.json"
PARTIAL_PATH = BENCH_DIR / "results_partial.json"

# plugins/memory/nexus/__init__.py:378 — NEXUS_PREFETCH_CHARS default
PREFETCH_BUDGET_CHARS = 2400
# plugins/memory/nexus/__init__.py:216 — block head line in the plugin format
BLOCK_HEADER = "Nexus Memory active. Relevant memories are automatically injected."
# plugins/memory/nexus/__init__.py:416 — per-item text slice
ITEM_TEXT_LIMIT = 500
# preview length for B2 when a fixture carries no summary (task spec: first 120)
PREVIEW_FALLBACK_CHARS = 120
# minimum remaining budget before a partial item is written (plugin behaviour)
MIN_TAIL_CHARS = 80
# plugins/memory/nexus/__init__.py:421 — truncation marker
TRUNCATION_MARKER = " …"

ANSWER_INSTRUCTION = (
    "Antworte NUR mit der Needle-ID wenn im Kontext vorhanden, sonst NUR mit UNBEKANT"
)

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
DEFAULT_MODEL = "glm-5.3-flash:cloud"
VALID_VARIANTS = ("base", "b1", "b2")
DEFAULT_VARIANTS = "base,b1,b2"
DEFAULT_MAX_CALLS = 465
CHECKPOINT_EVERY = 10

NEEDLE_ID_RE = re.compile(r"\bN-\d{3,6}\b", re.IGNORECASE)
ITEM_RE = re.compile(r"^\[(?P<category>[^\]]+)\]\s+score=(?P<score>\d+\.\d{2}):\s(?P<text>.*)$", re.DOTALL)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Memory:
    category: str
    score: float
    text: str
    summary: Optional[str] = None
    # graph-like items (production graph-boost) are pinned to the block tail
    is_graph: bool = False


@dataclass
class Session:
    session_id: str
    needle_id: str
    needle_text: str
    needle_topic: str
    needle_position: str  # normalized: anfang | mitte | ende
    memories: List[Memory] = field(default_factory=list)
    question: str = ""


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def normalize_position(raw: str) -> str:
    """Map fixture position labels to anfang/mitte/ende (fail-open to mitte)."""
    value = (raw or "").strip().lower()
    if value in ("anfang", "begin", "beginning", "start", "top", "front"):
        return "anfang"
    if value in ("ende", "end", "bottom", "back", "last"):
        return "ende"
    return "mitte"


def is_graph_like(category: str, text: str) -> bool:
    """Graph-boost items are tagged [graph:rel] by the production plugin."""
    cat = (category or "").strip().lower()
    if cat == "graph" or cat.startswith("graph:"):
        return True
    return (text or "").lstrip().lower().startswith("[graph:")


def parse_session(raw: Dict[str, Any]) -> Session:
    needle = raw.get("needle") or {}
    memories: List[Memory] = []
    for m in raw.get("memories") or []:
        category = str(m.get("category") or "fact")
        text = str(m.get("text") or "")
        summary = m.get("summary")
        memories.append(
            Memory(
                category=category,
                score=float(m.get("score") or 0.0),
                text=text,
                summary=str(summary) if summary else None,
                is_graph=is_graph_like(category, text),
            )
        )
    return Session(
        session_id=str(raw.get("session_id") or ""),
        needle_id=str(needle.get("id") or ""),
        needle_text=str(needle.get("text") or ""),
        needle_topic=str(needle.get("topic") or ""),
        needle_position=normalize_position(str(needle.get("position") or "")),
        memories=memories,
        question=str(raw.get("question") or ""),
    )


def load_fixtures(path: Path = FIXTURES_PATH) -> List[Session]:
    """Load JSONL fixtures; one session per line."""
    if not path.exists():
        raise FileNotFoundError(
            f"Fixtures not found: {path}. Expected {FIXTURES_PATH.name} "
            "(built by haus-betrieb, see SPEC.md section 2.1)."
        )
    sessions: List[Session] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            sessions.append(parse_session(raw))
    return sessions


# --------------------------------------------------------------------------- #
# Block construction (plugin format, budget 2400 chars)
# --------------------------------------------------------------------------- #

def render_item(memory: Memory, *, preview: bool) -> str:
    """Render one injection item in the production format.

    ``preview=True`` (B2) emits the one-line summary instead of the full text.
    """
    if preview:
        body = memory.summary or memory.text[:PREVIEW_FALLBACK_CHARS]
    else:
        body = memory.text[:ITEM_TEXT_LIMIT]
    return f"[{memory.category}] score={memory.score:.2f}: {body}"


def apply_budget(items: List[str], budget: int = PREFETCH_BUDGET_CHARS) -> List[str]:
    """Whole-item slicing identical to plugins/memory/nexus/__init__.py:418-422."""
    kept: List[str] = []
    total = 0
    for item in items:
        if total >= budget:
            break
        if total + len(item) > budget:
            if budget - total < MIN_TAIL_CHARS:
                break
            item = item[: budget - total].rstrip() + TRUNCATION_MARKER
        kept.append(item)
        total += len(item)
    return kept


def build_items(session: Session, variant: str) -> List[str]:
    """Return the ordered, budget-trimmed item list for a variant (no header)."""
    variant = variant.lower()
    memories = session.memories

    if variant == "base":
        rendered = [render_item(m, preview=False) for m in memories]
        return apply_budget(rendered)

    if variant == "b1":
        # Edge placement: best item first, second-best at the block end, the
        # rest in the middle. Graph-like tail items stay pinned at the back.
        # Assumption (SPEC ambiguity): the *single* best goes to the front and
        # the second-best to the end — items are never duplicated (tokens must
        # equal BASE per SPEC section 5).
        non_graph = [(i, m) for i, m in enumerate(memories) if not m.is_graph]
        graph = [m for m in memories if m.is_graph]
        ranked = sorted(non_graph, key=lambda pair: (-pair[1].score, pair[0]))
        if len(ranked) < 2:
            ordered = [m for _, m in ranked] + graph
        else:
            best_front = ranked[0][1]
            best_back = ranked[1][1]
            ordered = [best_front] + [m for _, m in ranked[2:]] + [best_back] + graph
        rendered = [render_item(m, preview=False) for m in ordered]
        return apply_budget(rendered)

    if variant == "b2":
        # Summary layer: one-line preview per item, full text only for the
        # top-2 by score. Arrangement stays as delivered (BASE order).
        order = sorted(range(len(memories)), key=lambda i: (-memories[i].score, i))
        top2 = set(order[:2])
        rendered = [
            render_item(m, preview=(idx not in top2))
            for idx, m in enumerate(memories)
        ]
        return apply_budget(rendered)

    raise ValueError(f"Unknown variant: {variant}")


def build_block(session: Session, variant: str) -> str:
    """Full injection block: header line + items (header not counted in budget,
    matching the production plugin where the header lives in system_prompt_block)."""
    items = build_items(session, variant)
    if not items:
        return BLOCK_HEADER
    return BLOCK_HEADER + "\n" + "\n".join(items)


def build_prompt(session: Session, block: str) -> str:
    return f"{session.question}\n\n{block}\n\n{ANSWER_INSTRUCTION}"


def estimate_tokens(text: str) -> int:
    """Cheap char/4 token estimate (no tokenizer dependency)."""
    return max(1, round(len(text) / 4))


# --------------------------------------------------------------------------- #
# Hit detection
# --------------------------------------------------------------------------- #

def is_hit(answer: str, needle_id: str) -> bool:
    """Hit = needle id appears in the answer, case-insensitive (SPEC section 2.4)."""
    if not needle_id:
        return False
    return needle_id.strip().lower() in (answer or "").lower()


# --------------------------------------------------------------------------- #
# Ollama call
# --------------------------------------------------------------------------- #

def call_ollama(model: str, prompt: str, *, timeout: float = 120.0) -> Tuple[str, float]:
    """POST to the OpenAI-compatible Ollama endpoint. Returns (answer, ms)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        # Reasoning-Modelle (glm-5.3-flash) verbrauchen vom Budget zuerst Denk-
        # Token im Feld 'reasoning' (content bleibt bei finish=length leer).
        # 1024 gibt Raum fuer Thinking + kurze ID-Antwort; Benchmark-Kosten
        # bleiben Cent-Bereich.
        "max_tokens": 1024,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000.0
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"Ollama returned no choices: {str(body)[:200]}")
    answer = (choices[0].get("message") or {}).get("content") or ""
    return answer.strip(), elapsed_ms


# --------------------------------------------------------------------------- #
# Checkpoint
# --------------------------------------------------------------------------- #

def load_partial(path: Path = PARTIAL_PATH) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("records", []) if isinstance(data, dict) else list(data)
    except (json.JSONDecodeError, OSError):
        return []


def save_partial(records: List[Dict[str, Any]], path: Path = PARTIAL_PATH) -> None:
    payload = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(records),
        "records": records,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def record_key(rec: Dict[str, Any]) -> Tuple[str, str, str]:
    return (rec.get("session_id", ""), rec.get("variant", ""), rec.get("model", ""))


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(ordered[int(k)])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def _rate(hits: int, total: int) -> Optional[float]:
    """Hit rate in percent, rounded to 1 decimal (None if no data)."""
    if total <= 0:
        return None
    return round(100.0 * hits / total, 1)


def summarize(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate records into one result row per (model, variant)."""
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for rec in records:
        groups.setdefault((rec.get("model", ""), rec.get("variant", "")), []).append(rec)

    rows: List[Dict[str, Any]] = []
    for (model, variant), recs in sorted(groups.items()):
        measured = [r for r in recs if not r.get("error")]
        errors = len(recs) - len(measured)
        pos = {"anfang": [0, 0], "mitte": [0, 0], "ende": [0, 0]}
        hits_total = 0
        latencies = [float(r.get("latency_ms") or 0.0) for r in measured]
        tokens = [int(r.get("tokens") or 0) for r in measured]

        for r in measured:
            position = normalize_position(str(r.get("position") or ""))
            bucket = pos.setdefault(position, [0, 0])
            bucket[1] += 1
            if r.get("hit"):
                hits_total += 1
                bucket[0] += 1

        hit_anfang = _rate(pos["anfang"][0], pos["anfang"][1])
        hit_mitte = _rate(pos["mitte"][0], pos["mitte"][1])
        litm_gap = (
            round(hit_anfang - hit_mitte, 1)
            if hit_anfang is not None and hit_mitte is not None
            else None
        )
        rows.append(
            {
                "model": model,
                "variant": variant,
                "n": len(measured),
                "errors": errors,
                "hit_gesamt": _rate(hits_total, len(measured)),
                "hit_anfang": hit_anfang,
                "hit_mitte": hit_mitte,
                "hit_ende": _rate(pos["ende"][0], pos["ende"][1]),
                "litm_gap": litm_gap,
                "tokens_mean": round(sum(tokens) / len(tokens), 1) if tokens else None,
                "latency_p50_ms": round(percentile(latencies, 0.50), 1) if latencies else None,
                "latency_p95_ms": round(percentile(latencies, 0.95), 1) if latencies else None,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def run(args: argparse.Namespace) -> int:
    variants = [v.strip().lower() for v in args.varianten.split(",") if v.strip()]
    bad = [v for v in variants if v not in VALID_VARIANTS]
    if bad:
        print(f"ERROR: unknown variant(s): {', '.join(bad)} (allowed: {', '.join(VALID_VARIANTS)})")
        return 2

    sessions = load_fixtures()
    if args.limit and args.limit > 0:
        sessions = sessions[: args.limit]
    if not sessions:
        print("ERROR: no sessions in fixtures.")
        return 2

    if args.dry_run:
        print(f"DRY-RUN: {len(sessions)} sessions x {len(variants)} variants "
              f"({', '.join(variants)}) — no calls made.")
        for session in sessions[: args.limit or 45]:
            for variant in variants:
                block = build_block(session, variant)
                print(f"\n=== {session.session_id} / {variant} "
                      f"(needle {session.needle_id} @ {session.needle_position}, "
                      f"{len(block)} chars, ~{estimate_tokens(block)} tok) ===")
                print(block)
        return 0

    records = load_partial()
    done = {record_key(r) for r in records if not r.get("error")}
    print(f"Resume: {len(done)} (session, variant, model) combos already measured.")

    calls = 0
    capped = False
    pending = 0

    for session in sessions:
        for variant in variants:
            key = (session.session_id, variant, args.model)
            if key in done:
                continue
            if calls >= args.max_calls:
                capped = True
                break
            block = build_block(session, variant)
            prompt = build_prompt(session, block)
            calls += 1
            rec: Dict[str, Any] = {
                "session_id": session.session_id,
                "variant": variant,
                "model": args.model,
                "position": session.needle_position,
                "needle_id": session.needle_id,
                "topic": session.needle_topic,
                "tokens": estimate_tokens(prompt),
                "block_chars": len(block),
            }
            try:
                answer, latency_ms = call_ollama(args.model, prompt)
                rec["answer"] = answer[:200]
                rec["latency_ms"] = round(latency_ms, 1)
                rec["hit"] = is_hit(answer, session.needle_id)
            except (urllib.error.URLError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
                rec["hit"] = False
                rec["latency_ms"] = 0.0
            records.append(rec)
            done.add(key)
            pending += 1
            print(f"[{calls:>4}] {session.session_id} {variant:>4} "
                  f"{'HIT ' if rec.get('hit') else 'MISS'} {rec.get('answer', rec.get('error',''))[:60]}")
            if pending % CHECKPOINT_EVERY == 0:
                save_partial(records)
                print(f"  ... checkpoint saved ({len(records)} records)")
        if capped:
            print(f"COST BRAKE: reached --max-calls={args.max_calls}. Stopping.")
            break

    if pending:
        save_partial(records)

    rows = summarize(records)
    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fixtures": str(FIXTURES_PATH),
        "variants": variants,
        "calls_this_run": calls,
        "capped": capped,
        "tokens_note": "estimated as round(chars/4) — no tokenizer available",
        "results": rows,
    }
    with RESULTS_PATH.open("w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    print("\n=== RESULTS (per model + variant) ===")
    header = f"{'model':<22} {'var':<5} {'n':>3} {'ges':>6} {'anf':>6} {'mit':>6} {'end':>6} {'gap':>6} {'tok':>7} {'p50ms':>8} {'p95ms':>8}"
    print(header)
    for row in rows:
        print(f"{row['model']:<22} {row['variant']:<5} {row['n']:>3} "
              f"{str(row['hit_gesamt']):>6} {str(row['hit_anfang']):>6} "
              f"{str(row['hit_mitte']):>6} {str(row['hit_ende']):>6} "
              f"{str(row['litm_gap']):>6} {str(row['tokens_mean']):>7} "
              f"{str(row['latency_p50_ms']):>8} {str(row['latency_p95_ms']):>8}")
    print(f"\nWrote {RESULTS_PATH}")
    return 0


# --------------------------------------------------------------------------- #
# Selftest (3 mini fixtures, no network calls)
# --------------------------------------------------------------------------- #

def _make_fixture() -> Session:
    """8 fake memories; needle first in the delivered order (position: anfang)."""
    specs = [
        ("fact", 0.91, "N-0042 Das Backup-Ziel der Homeinfra ist das NAS 'testnase'.", "Kurz: Backup-Ziel ist NAS testnase."),
        ("fact", 0.88, "DIST-1 Die Testsonde meldet Temperatur 21 Grad.", None),
        ("fact", 0.85, "DIST-2 Der Testknoten heisst 'testknoten-7'.", "Kurz: Testknoten heisst testknoten-7."),
        ("preference", 0.80, "DIST-3 Der Nutzer mag kurze Antworten.", None),
        ("fact", 0.76, "DIST-4 Das Testintervall betraegt 15 Minuten.", None),
        ("fact", 0.70, "DIST-5 Der Testkanal laeuft auf Port 9443.", None),
        ("fact", 0.64, "DIST-6 Die Testfarbe ist blau.", None),
        ("graph", 0.60, "[graph:runs_on] testknoten-7 laeuft auf der Testbasis.", None),
    ]
    memories = [
        Memory(category=c, score=s, text=t, summary=summ,
               is_graph=is_graph_like(c, t))
        for c, s, t, summ in specs
    ]
    return Session(
        session_id="SELFTEST-01",
        needle_id="N-0042",
        needle_text=memories[0].text,
        needle_topic="Homeinfra",
        needle_position="anfang",
        memories=memories,
        question="Wo liegt das Backup-Ziel?",
    )


def _item_texts(block: str) -> List[str]:
    lines = block.split("\n")
    return [ln for ln in lines[1:] if ln.startswith("[")]


def run_selftest() -> int:
    session = _make_fixture()
    checks: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    blocks = {v: build_block(session, v) for v in VALID_VARIANTS}

    # 1 — header line
    check("head-line present in all variants",
          all(b.startswith(BLOCK_HEADER) for b in blocks.values()),
          "")

    # 2 — item format [category] score=0.87: text
    fmt_ok = True
    for v, b in blocks.items():
        for item in _item_texts(b):
            m = ITEM_RE.match(item)
            if not m:
                fmt_ok = False
                check(f"item-format {v}", False, f"bad item: {item[:60]}")
    check("item-format matches plugin format", fmt_ok, "")

    # 3 — budget respected (items only, header excluded)
    budgets_ok = True
    for v, b in blocks.items():
        item_chars = sum(len(i) for i in _item_texts(b)) + max(0, len(_item_texts(b)) - 1)
        if item_chars > PREFETCH_BUDGET_CHARS:
            budgets_ok = False
            check(f"budget {v}", False, f"{item_chars} > {PREFETCH_BUDGET_CHARS}")
    check("budget <= NEXUS_PREFETCH_CHARS (2400)", budgets_ok, "")

    # 4 — BASE keeps delivered (score) order, graph item last
    base_items = _item_texts(blocks["base"])
    base_scores = [float(ITEM_RE.match(i).group("score")) for i in base_items if ITEM_RE.match(i)]
    check("base keeps delivered score order",
          base_scores == sorted(base_scores, reverse=True),
          f"scores={base_scores}")
    check("base keeps graph item at tail",
          base_items[-1].startswith("[graph") if base_items else False,
          base_items[-1][:40] if base_items else "")

    # 5 — B1 edge placement: best first, second-best at end, graph tail intact
    b1_items = _item_texts(blocks["b1"])
    best_text = session.memories[0].text[:ITEM_TEXT_LIMIT]
    second_text = session.memories[1].text[:ITEM_TEXT_LIMIT]
    check("b1 best item at block start",
          best_text in b1_items[0] if b1_items else False,
          b1_items[0][:50] if b1_items else "")
    check("b1 second-best before graph tail",
          len(b1_items) >= 3 and second_text in b1_items[-2],
          b1_items[-2][:50] if len(b1_items) >= 2 else "")
    check("b1 graph item stays at tail",
          b1_items[-1].startswith("[graph") if b1_items else False,
          b1_items[-1][:40] if b1_items else "")
    check("b1 keeps every item (no duplication/loss)",
          len(b1_items) == len(session.memories),
          f"{len(b1_items)} vs {len(session.memories)}")

    # 6 — B2 previews: non-top2 use summary, top-2 use full text
    b2_items = _item_texts(blocks["b2"])
    # DIST-2 (score 0.85) is outside the top-2 → preview must use its summary,
    # and the full DIST-2 text must NOT appear.
    check("b2 uses summary preview for non-top2",
          any("Kurz: Testknoten heisst testknoten-7." in i for i in b2_items)
          and not any("DIST-2 Der Testknoten heisst" in i for i in b2_items),
          b2_items[2][:60] if len(b2_items) > 2 else "")
    check("b2 top-2 keeps full text",
          any(session.memories[0].text[:ITEM_TEXT_LIMIT] in i for i in b2_items),
          "")

    # 6b — summary fallback = first 120 chars of text
    fallback_mem = Memory(category="fact", score=0.5, text="X" * 200, summary=None)
    preview = render_item(fallback_mem, preview=True)
    check("b2 fallback truncates to 120 chars",
          preview.endswith("X" * 120) and "X" * 121 not in preview,
          f"len={len(preview)}")

    # 7 — hit detection
    check("hit detection: exact id", is_hit("Die Antwort ist N-0042.", "N-0042"))
    check("hit detection: case-insensitive", is_hit("n-0042", "N-0042"))
    check("hit detection: unknown is a miss", not is_hit("UNBEKANT", "N-0042"))
    check("hit detection: other id is a miss", not is_hit("N-9999", "N-0042"))

    # 8 — metrics aggregation sanity
    recs = [
        {"model": "m", "variant": "base", "position": "anfang", "hit": True, "tokens": 100, "latency_ms": 10.0},
        {"model": "m", "variant": "base", "position": "mitte", "hit": False, "tokens": 100, "latency_ms": 20.0},
    ]
    row = summarize(recs)[0]
    check("metrics: litm_gap = anfang - mitte",
          row["litm_gap"] == 100.0 and row["hit_anfang"] == 100.0 and row["hit_mitte"] == 0.0,
          f"gap={row['litm_gap']}")

    print("=== SELFTEST (no network calls) ===")
    failed = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f"  [{detail}]" if detail and not ok else ""))
        if not ok:
            failed += 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks PASSED")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Lost-in-the-Middle benchmark harness for Nexus Memory.")
    p.add_argument("--varianten", default=DEFAULT_VARIANTS,
                   help="comma-separated variants: base,b1,b2 (default: all)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    p.add_argument("--limit", type=int, default=0, help="max sessions to process (0 = all)")
    p.add_argument("--dry-run", action="store_true", help="build blocks and print them, make no calls")
    p.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                   help=f"hard cap on LLM calls this run (default: {DEFAULT_MAX_CALLS})")
    p.add_argument("--selftest", action="store_true", help="run inline self-tests, no calls")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return run_selftest()
    try:
        return run(args)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
