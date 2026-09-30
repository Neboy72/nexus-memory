/**
 * Self-Check-Chat-Warnung (message_sending Hook, 30.09.2026)
 *
 * lib/self-check.ts feedet die Kaputt-Diagnose bereits in den Prompt
 * (buildSelfCheckWarning) — aber ein Prompt-Warntext erreicht den Operator
 * nur, wenn der Agent ihn freiwillig weitererzählt. Dieses Hook hängt eine
 * kurze, user-visible Warnung an OUTGOING-Nachrichten an, damit ein kaputtes
 * Nexus Memory im CHAT nicht unsichtbar bleibt (max. 1 Warnung pro Session,
 * Throttle auf ctx.sessionKey, bounded bookkeeping).
 *
 * ── SICHERHEITSANALYSE: Host-Merge-Kette (bewiesen, nicht geglaubt) ──
 * Quelle: /opt/homebrew/lib/node_modules/openclaw/dist/hooks-CKanWLsK.mjs
 * (runModifyingHook 837 bis 901, runMessageSending 1259 bis 1276) und
 * deliver-prepare-Dgf7HbVt.mjs (applyMessageSendingHook 197 bis 274,
 * legacy-Pfad 147 bis 173):
 *
 * 1. Reihenfolge + Merge: Handler laufen SEQUENZIELL nach Priorität
 *    (getHooksForName: toSorted((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
 *    toSorted ist stabil → ohne priority gilt Registrierungsreihenfolge).
 *    Gemerged wird pro Feld:
 *      content: lastDefined(acc?.content, next.content) — der LETZTE
 *               definierte content gewinnt ALLEIN (keine Verkettung),
 *      cancel:  stickyTrue — einmal true bleibt true,
 *    und die Kette STOPPT, sobald das Merge-Ergebnis cancel === true ist
 *    (reste Handler laufen dann nie).
 * 2. Jeder Handler kriegt DASSELBE ORIGINAL-Event: für message_sending gibt
 *    es in runModifyingHook weder policy.eventForHandler noch
 *    isolateEventPerHandler → handlerEvent === dispatchEvent. Ein Handler
 *    kann das Ergebnis des Vorgängers weder sehen noch darauf aufbauen —
 *    er kann es nur ÜBERSCHREIBEN oder (via cancel) verwerfen.
 * 3. Der Host liest vom Ergebnis AUSSCHLIESSLICH `.content`
 *    (deliver-prepare: `text: sendingResult.content`, legacy: `text:
 *    result.content`; Typ-Vertrag PluginHookMessageSendingResult =
 *    {content?, cancel?, cancelReason?, metadata?}). Ein Key `message` — wie
 *    der thought-filter ihn zurückgibt — wird vom echten Host ignoriert;
 *    dessen Drop ({message: undefined}) ist dort ein NO-OP.
 *
 * Warum dieser Hook damit sicher by construction ist:
 *   (a) Clobber: [{content: bereinigt}, danach mein {content: …}] würde den
 *       bereinigten Text ÜBERSCHREIBEN (lastDefined) — und weil JEDER Handler
 *       das ORIGINAL-Event sieht, hinge ich an den UNGEFILTERTEN Text und
 *       würde den Reasoning-Leak damit wieder ausliefern (Kompositions-Probe
 *       oc-composition-probe.mjs S2: "ORIGINAL-TEXT+WARN"). Zwei Abwehren:
 *       (1) Registrierung ALS LETZTER message_sending-Handler, (2) die harte
 *       Wache `hasReasoningLeak(raw)` unten — trägt der Text einen Leak, gibt
 *       dieser Hook `undefined` zurück (keine Meinung), der bereinigte Text
 *       des Filters bleibt unangetastet. Der Anhang entsteht nur auf Text,
 *       den der Filter NICHT verändert (kein Leak) — dann gibt es auch nichts
 *       zu überschreiben.
 *   (b) Resurrect einer unterdrückten Nachricht: Suppression läuft über
 *       cancel === true (Host-Doku: terminal; Delivery-Outcome
 *       "cancelled_by_message_sending_hook"). Liefert der Filter cancel
 *       (reiner Reasoning-Text), stoppt die Kette VOR diesem Hook. Ein
 *       Content-Drop über `undefined` existiert im Vertrag nicht — der
 *       frühere `{message: undefined}`-Drop des Filters war ein NO-OP am
 *       Host, deshalb prüft die Wache (2) zusätzlich auf den GANZEN Leak.
 *   (c) Cron-Form-Gate nie brechen: unbeaufsichtigte Sessions (cron /
 *       heartbeat via isUnattendedSession aus hooks/cron-form-gate.ts)
 *       werden komplett übersprungen — keine Anhänge auf unattended Sends.
 *   (d) Fail-open: der komplette Handler liegt im try/catch; im Fehler
 *       return undefined (keine Meinung, Verkette unangetastet) + sichtbarer
 *       Log (Lektion W31-14: stille Fail-opens bleiben unsichtbar).
 *   (e) Dieses Hook cancelt NIE und setzt niemals cancel: false — es gibt
 *       ausschließlich undefined (keine Meinung) oder {content} (Anhang).
 */
import { log } from "../logger.ts"
import { buildSelfCheckWarning } from "../lib/self-check.ts"
import { isUnattendedSession } from "./cron-form-gate.ts"
import { hasReasoningLeak } from "./thought-filter.ts"

/** Max. Zeilen des Warnblocks: Kopf + Cause + Fix + Beruhigung. */
const MAX_WARNING_LINES = 4
/** Defensives Zeilenlimit pro übernommener Zeile (Config-Strings können lang sein). */
const MAX_LINE_CHARS = 240
/** Bounded bookkeeping: höchstens so viele Sessions werden gleichzeitig getrackt. */
const MAX_TRACKED_SESSIONS = 256

const WARNING_HEADER = "⚠️ Nexus Memory self-check: NOT WORKING"
const WARNING_FALLBACK_SAFE =
  "Stored memories are safe; only this agent's memory access is offline."
const WARNING_FALLBACK_CAUSE = "Cause: unknown (self-check reported a failure)"
const WARNING_FALLBACK_FIX = "Fix: check the memory backend in the OpenClaw plugin config"

/** Bereits gewarnte Sessions (Throttle: 1 Warnung pro Session). */
const warnedSessions = new Set<string>()

function clipLine(line: string): string {
  return line.length > MAX_LINE_CHARS ? `${line.slice(0, MAX_LINE_CHARS)} …` : line
}

/**
 * Kurzer Warnblock (max. 4 Zeilen) aus den Prompt-Zeilen von
 * buildSelfCheckWarning: Kopf, Cause, Fix, Beruhigung — je Zeile gekappt.
 * Die Cause/Fix-Zeilen werden an den exportierten Präfixen erkannt, damit
 * Änderungen am Prompt-Text nicht stillschweigend die Chat-Warnung leeren.
 */
function buildChatWarningBlock(): string {
  const lines = buildSelfCheckWarning()
  const cause = lines.find((line) => /^Cause:/.test(line)) ?? WARNING_FALLBACK_CAUSE
  const fix = lines.find((line) => /^Fix:/.test(line)) ?? "Fix: siehe OpenClaw-Plugin-Config"
  const safe = lines.find((line) => /^Your stored memories/.test(line)) ?? WARNING_FALLBACK_SAFE
  return [WARNING_HEADER, cause, fix, safe]
    .slice(0, MAX_WARNING_LINES)
    .map(clipLine)
    .join("\n")
}

/**
 * message_sending-Handler: hängt bei kaputtem Self-check EINMAL pro Session
 * einen kurzen Warnblock an den Outgoing-Text an.
 *
 * Vertrags-Treue: Der Handler gibt ausschließlich `undefined` (keine Meinung,
 * nichts wird im Merge überschrieben oder wiederbelebt) oder `{content}` mit
 * `original + "\n\n" + Warnblock` zurück — niemals cancel, niemals einen
 * nicht-stringigen Content.
 */
export function buildSelfCheckWarningHandler() {
  return async (
    event?: { content?: unknown } | null,
    ctx?: { sessionKey?: string },
  ): Promise<{ content: string } | undefined> => {
    try {
      // Nur `content` ist der vom Host gelesene Key (bewiesen, Dateikopf
      // Punkt 3) — message/text-Fallbacks bewusst NICHT hier: ein Anhang an
      // eine andere Payload-Form würde das Feld des Filters berühren.
      const raw = (event ?? ({} as { content?: unknown })).content
      if (typeof raw !== "string") return undefined // keine Meinung, nie etwas "retten"
      if (raw.trim().length === 0) return undefined
      // Gesund → sofort neutral, BEVOR der Leak-Scan läuft: der Normalfall
      // (Memory ok) darf keine zusätzliche Arbeit pro Nachricht kosten.
      if (buildSelfCheckWarning().length === 0) return undefined
      // Clobber-Wache: jeder Handler sieht das ORIGINAL-Event, der Host nimmt
      // aber den LETZTEN `content` — hängt dieser Hook an leak-tragenden Text
      // an, überschreibt er den bereinigten Text des thought-filter und der
      // Leak geht wieder raus (Probe S2). Kein Leak → kein Konflikt.
      if (hasReasoningLeak(raw)) return undefined
      const sessionKey =
        typeof ctx?.sessionKey === "string" && ctx.sessionKey ? ctx.sessionKey : ""
      if (isUnattendedSession(sessionKey)) return undefined // Cron/Heartbeat → unangetastet
      const key = sessionKey || "(ohne sessionKey)"
      if (warnedSessions.has(key)) return undefined
      // Bounded statt unendlich wachsen: ältesten Eintrag (Set = Insertion
      // Order) verwerfen, wenn das Limit erreicht ist.
      if (warnedSessions.size >= MAX_TRACKED_SESSIONS) {
        const oldest = warnedSessions.values().next().value
        if (oldest !== undefined) warnedSessions.delete(oldest)
      }
      warnedSessions.add(key)
      const out = `${raw}\n\n${buildChatWarningBlock()}`
      log.warn(
        `self-check-warning: Warnung an Nachricht angehängt (session=${sessionKey || "ohne"}, ${raw.length} Zeichen)`,
      )
      return { content: out }
    } catch (err) {
      // Fail-open (bewusst, aber NICHT still): ein Crash dieses Hooks darf
      // den Sendepfad nie berühren — die Nachricht geht unverändert raus.
      // Der Log liegt IM eigenen try: wirft sogar der Logger (kaputtes
      // Backend), stirbt der Fail-open-Pfad trotzdem nie nach außen.
      try {
        log.warn("self-check-warning: Fehler — fail-open (Senden unangetastet)", err)
      } catch {
        /* Logger selbst kaputt → trotzdem fail-open */
      }
      return undefined
    }
  }
}

/** Test-Rücksetzer: Throttle-Bookkeeping leeren (Self-check-Verdict separat via resetSelfCheckForTest). */
export function resetSelfCheckWarningStateForTest(): void {
  warnedSessions.clear()
}