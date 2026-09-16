/**
 * Thought-Filter — message_sending Hook (29.08.2026)
 *
 * Kontext: GLM-5.3 (und ähnliche Cloud-Modelle) emittieren Chain-of-Thought
 * ALS PLAIN TEXT im message.content (kein natives reasoning-Feld, keine
 * <think>-Tags). OpenClaws thinking-tag-Strip greift nicht (GitHub #42062,
 * #33242) — das Denken wandert als sichtbare Nachricht in den Chat.
 *
 * Dieser Hook greift im Deliver-Pfad VOR dem Senden und entfernt nur
 * eindeutige Reasoning-Leak-Blöcke. Der Rest bleibt unangetastet.
 *
 * Design (Karpathy):
 * - Surgical: Nur Muster, die als Leak verifiziert sind.
 * - Fail-open: Im Zweifel geht die Message unverändert raus — nie legitime
 *   Antworten verschlucken.
 * - 0ms-Klasse: reine Regex-Matches, keine LLM-Calls.
 */

import { log } from "../logger.ts"

// Below MIN_LEN_PROCESS chars a message cannot carry a reasoning leak (pure
// noise) → skip the scan entirely. Fail-open policy stays: only blocks matched
// as leaks are ever dropped.
const MIN_LEN_PROCESS = 24
// all-Leak remainder below this is noise, not send
const MIN_LEN_SEND = 12
// W29-7: collateral cap — at most this many consecutive continuation blocks
// (numbered plan steps) are dropped after a verified leak block. Without the
// cap a single leak could eat an unbounded numbered tail.
const MAX_CONTINUATION = 2

// Muster, die eindeutig internes Reasoning markieren (verifizierte Leaks).
const REASONING_MARKERS: RegExp[] = [
  // Reflex-/Übergangs-Adverbien am Blockanfang (Miosha-leak-typisch)
  // "So"-False-Positive geschärft (08.09.): Nur "So, …" mit Komma = Leak-Marker,
  // "So gehen wir vor:" / "So läuft's" bleibt legitime Antwort.
  // W29-6: hier stehen nur noch die STARKEN Reflex-Formen. Die schwachen
  // Adverbien (now/then/okay/alright/actually/ok) sind nach
  // WEAK_ADVERB_MARKER unten verschoben — sie allein sind kein Leak.
  /^(so,|wait|hmm)\b[,.\s]/i,
  // DE/EN Selbststart-Marker
  /^\s*(let me (parse|think|work through|analyze|check|consider)|hmm[,.]|okay,? let'?s|i should|i need to)\b/i,
  /^(the user|nebo|miosha|kiosha)\s+(asks|is asking|wrote|sent)\b/i,
  // Englische Analyse-Blöcke über dem deutschen Final (Miosha-Realität):
  /^(he'?s|she'?s|he is|she is)\s+(asking|wondering|reacting)\b/i,
  /^(this|that|it) (is|was)\s+a (personal|warm|curious|natural|joke|meta)\b/i,
  /^wait[,\s—-]/i,
  /^(naja|eigentlich|wohl|vermutlich|probably|maybe|perhaps)\b.*\?$/i,
  // Analyse-Struktur-Marker
  /^\(a\)\s|\n\s*\([ab]\)\s+(A |The )?/i,
  // Draft-/Antwortkonstruktions-Marker
  /^-{0,3}\s*How should I respond/i,
  /\b(Draft|Final answer version|Response draft)\s*:\s*$/im,
  // Selbstdiskussion über Antwortformat
  /^(draft|response|final)(\s+\d)?:\s/im,
  // Runtime-context-/Replay-Selbstverortung (02.09.-Leak, "The runtime context is just a replay…")
  /^the (runtime )?context (is just|re-delivery)/i,
  /^runtime context (re-delivery|is just)/i,
  // Recovery-/Bearing-Marker ("Let me get my bearings for the current turn.")
  /^let me get my bearings\b/i,
  /^i(?:'| a)m (still|now) (processing|waiting|in)\b/i,
  // Plan-Aufzählungs-Blöcke ("Where I am:", "What do I know:", "Plan for this turn")
  /^(where i am|what do i know|my plan|the plan|plan for this turn|current state|now \(|critical honesty)\s*:?\s*$/i,
  /^what(?:'s| is) (missing|next|left)\b/i,
  /^next (steps?|up)\s*:/i,
  // Recovery-Ehrlichkeits-Deklarationen ("Critical honesty requirement: …")
  /^(critical honesty|important honesty|honesty requirement|note to self)\b/i,
  // System-Kontext-Wiedergabe ("The last system message: …" / "The last system message was a retry.")
  /^the (last |previous )?(system|incoming|visible) (message|context)\b/i,
  // Re-Orientierungs-Blöcke (Leak-Welle 08.09.: "Let me (very carefully) re-orient on what is REAL/right now…")
  /^let me (?:very |carefully )*re-?orient\b/i,
  // Re-Orientierungs-Varianten (08.09., geschärft): nur die echten Leak-Formen —
  // ein generisches "This message contains X" in einer LEGITIMEN Antwort darf
  // nicht gefiltert werden (False-Positive-Gefahr des ersten Entwurfs).
  /^(the current|this) (user )?message is (an internal|an? context|nebo|miosha)\b/i,
  /^(the current|this) (user )?message contains\s*(:|$)/im,
  /^the current user message\s*:/i,
  /^(the current|this) turn (is|contains|says)\b/i,
  // Zitat-/Verweis-Öffner ("The last message: Nebo's message at …", "THE CURRENT USER MESSAGE: …")
  /^the (last|current) (user )?message\b/i,
  // Cron-/Heartbeat-Selbstplanung (Release Tracker, Memory-Cron — 08.09.-Leak-Welle)
  /^let me (parse this heartbeat|work through this task|analyze what i got|start by fetching)\b/i,
  /^(steps?|my steps)\s*:\s*$/im,
  /^(\d+\.\s*)?(daily memory file exists|memory file exists)\b/i,
]

// W29-6: schwache Übergangs-Adverbien. Sie eröffnen genauso häufig legitime
// Antworten ("Now, here is the update", "Okay, erledigt") wie Leaks, darum
// gelten sie NUR zusammen mit einem zusätzlichen Reasoning-Signal (oder direkt
// nach einem verifizierten Leak-Block) als Leak. Fail-open (Karpathy-Regel im
// Datei-Kopf): im Zweifel bleibt der Block stehen.
const WEAK_ADVERB_MARKER = /^(now|then|okay|alright|actually|ok)\b[,.\s]/i
// Zusätzlicher Beweis, dass ein Schwach-Adverb-Block wirklich Reasoning ist.
const REASONING_SIGNAL =
  /\b(i'?ll|i need to|i should|let'?s|let me|first,|next,|step \d|my plan|plan for)\b/i

/** true = Block besteht (sehr wahrscheinlich) NUR aus internem Reasoning. */
export function isPureReasoningBlock(
  text: string,
  prevWasLeak = false,
  continuationCount = 0,
): boolean {
  const trimmed = text.trim()
  if (trimmed.length === 0) return false
  // Fortsetzungen eines Leak-Blocks: nummerierte Plan-Struktur direkt nach
  // verifiziertem Reasoning ("Steps:\n\n1. Fetch …").
  // W29-7: nur noch die nummerierte Fortsetzung UND nur bis MAX_CONTINUATION
  // aufeinanderfolgende Fortsetzungs-Blocks. Die alte Dash-Regel und die
  // Bold-Regel sind RAUS — sie löschten zu oft die eigentliche Antwort
  // ("**Status:** alles grün", "- Update installiert").
  // Nur NACH einem Leak-Block aktiv — legitime Antworten mit Aufzählungen
  // ohne Leak-Vorgänger bleiben unangetastet.
  if (prevWasLeak && continuationCount < MAX_CONTINUATION) {
    if (/^\d+\.\s/.test(trimmed)) return true
  }
  // W29-6: schwache Adverbien nur mit zusätzlichem Reasoning-Signal bzw.
  // innerhalb eines laufenden Leak-Blocks.
  if (WEAK_ADVERB_MARKER.test(trimmed) && (REASONING_SIGNAL.test(trimmed) || prevWasLeak)) {
    return true
  }
  return REASONING_MARKERS.some((re) => re.test(trimmed))
}

/**
 * message_sending-Handler: modifiziert den Outbound-Text.
 * Merge-Regel (Doku): "message_sending uses the last returned content".
 */
export function buildThoughtFilterHandler() {
  return async (ctx: { message?: string; content?: string; text?: string }) => {
    // raw VOR dem try deklarieren: der Fail-open-catch muss den ORIGINAL-Text
    // zurückgeben können. ctx.message ist undefined, wenn die Payload in
    // content/text lag — die Message würde sonst gedroppt statt durchzugehen.
    let raw: string | undefined
    try {
      raw = ctx?.message ?? ctx?.content ?? ctx?.text
      if (typeof raw !== "string" || raw.trim().length < MIN_LEN_PROCESS) return { message: raw }

      const blocks = raw.split(/\n{2,}/)
      let prevWasLeak = false
      let continuationCount = 0
      let droppedLines = 0
      const kept: string[] = []
      for (const b of blocks) {
        // W29-8: Marker-Check auf ZEILEN-Ebene. Reasoning + Antwort ohne
        // Leerzeile landen im selben Block ("Let me think…\nHier die Antwort")
        // — der alte Block-Check löschte den ganzen Block inkl. Antwort.
        // Jetzt werden nur die FÜHRENDEN Leak-Zeilen gedroppt, der Rest bleibt.
        const lines = b.split("\n")
        let cut = 0
        while (
          cut < lines.length &&
          isPureReasoningBlock(lines[cut], prevWasLeak || cut > 0, continuationCount)
        ) {
          cut++
        }
        const isLeakBlock = cut > 0
        if (isLeakBlock) {
          prevWasLeak = true
          continuationCount++
          droppedLines += cut
        } else {
          prevWasLeak = false
          continuationCount = 0
        }
        const remaining = lines.slice(cut)
        if (remaining.length > 0) kept.push(remaining.join("\n"))
      }
      if (droppedLines === 0) return { message: raw }

      const out = kept.join("\n\n").trim()
      log.warn(
        `thought-filter: reasoning-Leak entfernt (${droppedLines} Zeile(n), ${raw.length} -> ${out.length} Zeichen)`
      )
      // W29-9: { message: undefined } is INTENTIONAL for fully-suppressed leaks —
      // message_sending merges the last returned content; undefined here means
      // "nothing to send" (drop). Verified in production since 29.08.2026.
      if (out.length < MIN_LEN_SEND) return { message: undefined } // alles war Leak → nicht senden
      return { message: out }
    } catch {
      return { message: raw } // Fail-open: Original unverändert durchlassen
    }
  }
}