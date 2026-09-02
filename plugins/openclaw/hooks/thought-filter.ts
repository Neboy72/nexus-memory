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

// Muster, die eindeutig internes Reasoning markieren (verifizierte Leaks).
const REASONING_MARKERS: RegExp[] = [
  // Reflex-/Übergangs-Adverbien am Blockanfang (Miosha-leak-typisch)
  /^(now|then|so|actually|wait|hmm|ok|okay|alright)\b[,\s]/i,
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
]

/** true = Block besteht (sehr wahrscheinlich) NUR aus internem Reasoning. */
function isPureReasoningBlock(text: string): boolean {
  const trimmed = text.trim()
  if (trimmed.length === 0) return false
  return REASONING_MARKERS.some((re) => re.test(trimmed))
}

type MessageSendingCtx = { message?: string; content?: string; text?: string } & Record<string, unknown>

/**
 * message_sending-Handler: modifiziert den Outbound-Text.
 * Merge-Regel (Doku): "message_sending uses the last returned content".
 */
export function buildThoughtFilterHandler() {
  return async (ctx: { message?: string; content?: string; text?: string }) => {
    try {
      const raw = ctx?.message ?? ctx?.content ?? ctx?.text
      if (typeof raw !== "string" || raw.trim().length < 24) return { message: raw }

      const blocks = raw.split(/\n{2,}/)
      const kept = blocks.filter((b) => !isPureReasoningBlock(b))
      if (kept.length === blocks.length) return { message: raw }

      const out = kept.join("\n\n").trim()
      log.warn(
        `thought-filter: reasoning-Leak entfernt (${blocks.length - kept.length} Block(s), ${raw.length} -> ${out.length} Zeichen)`
      )
      if (out.length < 12) return { message: undefined } // alles war Leak → nicht senden
      return { message: out }
    } catch {
      return { message: ctx?.message } // Fail-open
    }
  }
}