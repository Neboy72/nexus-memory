## [0.22.79] - 2026-11-27

**Duplicate memories can now be merged without breaking the graph — and the scope index script no longer reports a failure that is not there.**

The memory held 59 groups of identical documents (139 points, 80 surplus copies), all of them imported more than once from Paperless. Merging them by hand was impossible: a point cannot simply disappear, because other points reference it.

- New `scripts/dedupe_content_groups.py`. Dry run by default; `--apply` needs a snapshot with name, size and checksum first. For every group it re-checks that the content is byte-identical, chooses a keeper by incoming references, reroutes the references of the copies onto it, and only then deletes — and only if a full re-scan of the collection finds no surviving point that still points at a copy.
- A keeper rule detail that the dry run made visible: ten points belong to two groups, so a per-group write would have overwritten the first group's rerouted edges with the stale snapshot. The tool now writes once per point, with all its assignments at the same time.
- `scripts/scope_payload_index.py` read the payload schema once, right after building the index, and reported `payload_schema does not name 'scope' as keyword` although the index was already there — Qdrant fills the schema asynchronously. It now waits in bounded steps (30 s, every 0.5 s) and says how long it waited.

Applied once on the live collection: 80 copies removed, 68.545 points left, 41 references rerouted, 11 carried over, and no reference left pointing at a deleted copy (verified over the whole collection). A recall through the running service answers normally.

## [0.22.78] - 2026-11-27

**The prefetch embedding no longer sends the whole turn — and every embed attempt now records how long it took.**

Prefetch (auto-recall) embeds the text of the turn before the next one starts. On a cold process it sent the full prompt: the failed turn of 2026-11-27 08:00 carried 13,750 characters, which is roughly 29 s of embedding against a 7.5 s deadline and two 15 s attempts. It could never arrive, and the memory was lost for that turn — not late, gone.

- The prefetch embedding text is now capped at 1000 characters (`NEXUS_PREFETCH_EMBED_CHARS`, clamped to 200–8000): the head and the tail, with a marker in between. A text that fits passes through unchanged. Only the prefetch path is capped; the write path and `nexus_recall` are untouched.
- Every embed attempt now writes one log line with its duration, character count, status and the thread that asked. The service logged nothing for `/embed` before, so an outlier could not be attributed to a caller.
- New script `scripts/scope_payload_index.py`: creates the missing payload index on the `scope` field (68,602 points, `payload_schema` was empty; a filtered query cost ~400 ms against ~5 ms unfiltered). Idempotent, requires a snapshot plus checksum before writing, dry run by default. It was not executed as part of this release.
- Four docstrings and comments described an older gate rule instead of the generation rule the code implements. They now say what the code does: a late run may still publish, unless a run that started later has already published.

Numbers: 19 new tests. On the previous commit six of them fail and the other three are counter-proofs that stay green (a short text passes unchanged, a late run may publish, a later publication wins). Full suite: 3500 passed, 1 pre-existing failure, 22 skipped.

## [0.22.77] - 2026-11-26

**A failed `/embed` now says why it failed — and the service writes down why it refused a request or gave no answer.**

A finding from live operation, 14:48:45: four POSTs in 0.13 s, `/healthz` answering, and the agent only got "answers /healthz but returned no vector after 2 attempt(s)". Why the fetch failed could not be determined, because `_post_embed` threw away every exception with `except Exception: return None, None`.

- **The reason is recorded** (`_post_embed`, thread-local): the kind of exception, an HTTP status together with the service's own error line, for a timeout the time actually waited next to the limit, and for a 200 without a vector the keys of the answer. No request text, no key — the line is fit for a log and for an answer. The retry line and the message to the agent carry it.
- **The service no longer stays silent.** `/embed` logs when the store cannot be built (the 503 that looks like a dead service from outside) and when the provider fails; a refused key names its kind (installation key or unknown key) and the caller's user agent.
- **The 14:48 case, put in order (not solved).** The service process ran through from 14:19:45 (PID 19114), `/healthz` answered, the access log holds not a single 5xx on `/embed`, and the service log has not been written to since 14:43. The four POSTs therefore did not reach the handler with an error status: either a 200 whose answer the plugin path could not use, or a connection error. Both are named by the new reason the next time. The background run did **not** starve the foreground: under six CPU burners the real service answered with 1024 dimensions in 0.09 s (unloaded 0.09 s warm, 0.47 s cold); a timeout would have taken 15 s, and a starved service would be slow, not finished in milliseconds.
- **The 772 x 401 in the access log.** 761 of them sit before the 44,686th successful `/embed` — according to the counter in commit `d9d919b` (16.11.2026 01:02) that is the point where agent keys were introduced and the installation key restricted to `/enroll`. That is the rollout window: callers still sending the installation key got "plugin update required"; that fallback is documented in such scripts (e.g. `paperless_nexus_common.get_service_key`, own key only since 22.11.). The remaining 11 are five small series (three to four 401, one 400, then 200) **without** a following `/enroll` — so not a plugin signing in, but a check of the auth gate. I could not attribute the process: the access log carries no timestamps, and the connections live for milliseconds. From now on the service names the kind of key and the user agent.

New checks in `tests/test_embed_grund.py` (8) and `tests/test_serve_embed_endpoint.py` (4) — at the stand `7b7c234b` all 12 are red.

## [0.22.76] - 2026-11-26

**A backup now says which process wrote it — the test run counts only its own, and after a start at most one backup per day is created.**

A fault in the test run of 26.11.2026.

- **The watchdog accused the wrong one.** At the end the test run checks whether it left backups in the real folder `~/.nexus-memory/backups`. During a run the service restarted and on start left two backups of 124 MB each there (13:56:50 and 13:56:52, version `0.22.75`) — the watchdog reported that as a fault of the test run, although the test had written nothing. Now every backup names its writer in the field `pid`; the watchdog counts only files of its own process id and keeps reporting when the field is missing (origin unknown). The rule itself is checked (`test_waechter_zaehlt_nur_die_eigenen_sicherungen`).
- **The installer test tipped over under load.** `test_ensure_runs_script_and_reports_installed` really started `bash` for that purpose; when the start failed under load, the test turned red without showing the reason. What is checked is the decision (script runs, output arrives, wait loop is bounded), and that is now checked without a real process start; the script's own output is covered by the other tests of that file.
- **The backup named an invented version.** The plugin wrote `"version": "0.4.0"` into every backup, the service its real one. Both now name their own version.
- **At most one backup per day after a start.** The service backs up 60 seconds after every start. Four restarts in one day (13:53:28, 13:54:09, 13:56:50, 13:56:52) therefore left four files of 124 MB each — not a timer, but four start backups. The start backup now asks whether a backup already exists for today (criterion: the date in the file name, which this code writes itself), and then writes no second one. The cadence after that stays untouched: the service keeps backing up every 6 hours, the plugin daily, and a day change triggers the next backup — a skipped start does not stop the cadence. Existing backups are not deleted, there is no cleanup routine.

At the old stand (`f605cf4`) all nine new checks in `tests/test_backup_isolation.py` fail, because the helpers do not exist there yet; in content the two day-change checks would already have been satisfied by the old behaviour — so they show that the cadence is preserved, not the fix. The installer test is written so that a failed process start no longer tips it over without a reason.

## [0.22.75] - 2026-11-26

**A single failed fetch at the embedder counted as "service gone" — now there is a short wait, one retry, and only a truly unreachable service is reported; a run that exceeds the deadline may still deliver its result.**

A finding from live operation, 18 times in the log.

- **The sentence contradicted itself.** "Prefetch failed: ⚠ local embedder unavailable: local embedder service answers on http://127.0.0.1:9122" — so the service answered while the plugin path declared it dead. Cause, shown from the timestamps: a single `POST /embed` ran into its 15-second limit on the hour (start 12:00:30, watchdog 12:00:38, error 12:00:45), was not repeated, and the error message took over the healthz text — the service was reachable, just not ready at that moment.
- **Now it is repeated.** After a failure the plugin path waits briefly and in bounded fashion, has `/healthz` confirm that the service is there, and tries **once** more; only then does it give up. If `/healthz` answers, the message reads "embed request failed" instead of "unavailable". A stale availability note (the service was not ready when the process started) is now verified by the prefetch before it publishes a note, instead of setting it from memory.
- **A late run may still deliver.** Until now a run that exceeded the deadline was discarded for good. Now it publishes its result as long as no **later-started** run has published something in the meantime; the old zombie rule is thus preserved — a fresher hit is never overwritten.
- **Measured** on the same sequence (the second `POST /embed` fails, the service lives): before 0 characters of memory block and the wrong note, now 652 characters and no note; the first turn still costs 0.00 s, because it does not wait.

New checks in `tests/test_embed_nach_start.py`, four of them red at the stand `b0d47b6`.

## [0.22.74] - 2026-11-26

**The first turn of a new agent no longer waits for memory, and the agent no longer loads a copy of the embedding model of its own — that was the reason the cold start was still not clean despite 0.22.73.**

Two findings, measured live today.

- **The deadline ran inside the turn, not in the background.** 0.22.73 had raised the deadline to 7.5 s — it was not enough: the log still read "Coldstart prefetch still running after 7.5s", the turn paid it and then got an empty memory block (10:43:03 and 10:47:15, six load operations that day). Now the turn starts the follow-up run and returns empty at once; the deadline only applies in the background, where a watchdog swaps the gate once it expires, so that a stuck run cannot wedge later fetches. The next turn takes the result, and a process-local mark says that a follow-up run is open. Measured on the same sequence with 3 s of work: first turn before 3.0 s (in operation above the deadline: 7.5 s and empty), now 0.00 s, hit in the following turn.
- **The agent loaded a second copy of the model.** The rule "exactly one process embeds" held for the plugin's vectors, but the graph store built its own embedder on first access: `EdgeStore.initialize()` called `EmbeddingProvider()` inside the agent process, although the host had long known the vector name. Consequence: three processes held the model file (checked 10:47), the agent itself with 3.3 GB, and the first graph access cost 2.4 s of model build-up in the middle of the turn. Now the host passes the vector name along; then no embedder is built. Anyone who does not pass a name keeps the old behaviour — embedder, binding and check of the collection; the four graph tools of the plugin pass it explicitly, because the collection belongs to the service and is created and checked there. Measured in the host interpreter: a process with the graph store had 1.44 GB RSS before, now 0.49 GB. The running agent releases its copy on the next restart.

At the old stand (`b01463c`) all four new checks in `tests/test_kaltstart_ohne_warten.py` are red.
## [0.22.73] - 2026-11-26

**The first turn after rebuilding an agent no longer waits five seconds for memory and then stands there without it anyway: the embedder service is now warmed up while the agent is built, and an empty result counts as a result.**

Four findings from live operation on 25/26.11.2026.

- **The deadline was exactly as long as the wait.** The catch-up in the first turn gave the service five seconds — exactly as long as starting the service takes anyway; no moment was left for building the model afterwards. Every cold start therefore ran into the deadline, the turn waited it out and got an empty memory block (on 26.11. twelve times in the log, e.g. 08:55:30 to 08:55:35). Now the deadline is derived from the parts of the cold start (start window plus model build-up) and stays below the host's deadline, so the host does not give up first; the warm-up itself runs while the agent is being built, where nobody is waiting on a turn. Measured on the cold service: first turn 5.00 s and empty, now 1.79 s and with a memory block; on the warm service 0.21 s, now 0.00 s.
- **An empty result was not a result.** If the prefetch found nothing, an empty string was published — on the next turn that looked like "nothing has run yet", and the catch-up searched again synchronously. Now there is a state of its own, "prepared", which every publication path sets, including the empty one; it is cleared on discard and on a gate swap. The same call twice in a row thus triggers exactly one search run.
- **An occupied prefetch was silent.** If a fetch was already running, the catch-up returned empty at once — with no wait and no line in the log. Now there is a short wait (0.5 to 1.0 s), whatever lands in that time is taken, and if nothing comes there is a line at DEBUG. Never silent again.
- **Things were deleted without a check.** Two abort sites cleared the result without looking at the gate, although the write site next to them names exactly that as the rule ("a zombie must not delete a fresher hit"). Both now pass their gate through; they were only reachable during shutdown, but wrong is wrong.

Nine new checks in `tests/test_prefetch_vorbereitung.py`, all nine red at the old stand (measured against the stand `6d21d78`).

## [0.22.72] - 2026-11-25

**The Windows check run was red because a new test did not account for the path form — it now carries the same exception as its neighbours.**

The test for the installer checks whether the start file points at the environment of its own checkout. Under Windows that branch runs in Git Bash, which writes paths in a different form than Python compares them; the test is therefore skipped there, exactly like the three tests of the same class that check the same thing. Nothing changes in behaviour.

## [0.22.71] - 2026-11-25

**A write into a room whose state nobody can prove is no longer let through; the graph add-on brings memories only and no more labels; Nexus Memory reads its own configuration first, and a test run no longer touches the real agent list.**

Four findings from 24/25.11.2026.

- **The refusal only applied with proof.** Until now an excessive level was rejected only when the room was provably shared. If the state was unclear — observation missing or stale — the write went through. That is the gap the security scan reported as "possible fail-open". Now: a call **without any room** (DS Bot, Hermes, Claude Code, DSH write that way) may still write `private`; a call **inside a room** whose state cannot be proven is refused — with the same identifier as in 0.22.69 and the reason `room_unknown`. Both paths, OpenClaw and Hermes, say the same thing.
- **The graph add-on brought labels instead of memories.** Measured over 20 real questions, 87 % of the add-ons consisted of entity labels ("service: Gateway (launchd_label=…)"); only 24 % hit the topic, while the next normal vector hits reached 56 %. Now only real memories enter the add-on (the same feed rule as in the write path), and the path goes through the **rare** entities first — a universally linked label leads to everything and therefore to nothing. After the change: 27 of 49 add-ons fitting (55 %). In addition the graph fetch now understands the old numeric identifiers in the store: as text the collection answered the batch with an error, and the add-on dropped out entirely in more than half of the fetches.
- **Nexus Memory hung off Hermes.** Reranker and consolidation read their settings from `~/.hermes/config.yaml` by default. Anyone without an agent installed got nothing there — and that looked like "switched off". Now the own file `~/.nexus-memory/config.json` applies first, Hermes is one possible further source; without Hermes both keep running normally.
- **A test run created a real agent.** Two test files used the test figure `eigner-bot`, but not the isolated data environment — on 24.11. at 07:31 that produced a real entry in `~/.nexus-memory/agents.json`. Both files now run in the isolated environment; the real list stays untouched.

## [0.22.70] - 2026-11-24

**The refusal from 0.22.69 now says "access level" instead of "access_level" — only the wording, the rule stays.**

The check that reports every firmly written `public` value in a write path had read the refusal itself as an assignment and turned the run red. In prose the level is therefore now called "access level"; the same thing is meant, and the machine fields (`code`, `reason`, `requested_level`, `max_level`) are unchanged. In a provably shared room an excessive level is still refused, not silently lowered — in both write paths, with the same wording.

## [0.22.69] - 2026-11-24

**In a shared room a note meant as confidential is no longer stored publicly without a word: the write is refused and says why.**

A finding from the review of the ClawHub package 1.22.68.

- **The level fell silently.** Anyone writing in a room where strangers write along and asking for `trusted` or `private` got `public` stored — and the answer reported "Stored: … (access_level: public)", as if that had been ordered. The caller did not learn that his note lay readable for everyone. The rule had been meant as a privacy correction since 13.11., but worked in the other direction: it changed the storage without asking.
- **Now it is refused.** In a provably shared room `public` is the upper limit. If a write asks for more, a refusal comes with the reason (the room), with the upper limit and with two ways out: deliberately ask for `public` if everyone may read it, or write in an own chat where the requested level applies. Nothing is stored then, not even in part, and nothing is prepared.
- **Both write paths say the same.** The rule sits in the OpenClaw plugin (tool for storing) and in the Hermes plugin; both name the same identifier (`shared-room-write-rejected`) and the same fields, so that an agent can read the answer independently of the host. On the Hermes path the automatic session log in a shared room is additionally not written at all, instead of storing it publicly — there is nobody there who could release a lower level.
- **Only with proof.** A call without a room, with an unknown room or with a stale mark stays untouched as before. The read side is unchanged: shared rooms keep delivering only `public`; the automatic session storage keeps capping at the configured level, because nobody asks for a level there.

Checked with real calls of both paths (`plugins/openclaw/test-store-room-rejection.mjs`, six cases, and the Python calls in the review) and with the source check `tests/test_raum_deckel_schreiben.py`, which covers both files.

## [0.22.68] - 2026-11-24

**An entry that is written anew no longer loses its fields, no writer can forget the merge any more, foreign entries can no longer be overwritten — and the answer says where the entry really lies.**

Four findings from live operation.

- **Fields fell back when overwriting.** Anyone rewriting a memory with the same identifier saw default values afterwards: `created_at`, `valid_from`, `use_count`, `salience`, `state_value`, `expiry_policy`, the scope and the edges were back at the start. The cause was the write itself — the collection replaces the whole payload. Now the existing entry is the base; only content, classification, author, the stricter access level, `provenance` and `updated_at` are overwritten. The origin additionally sits in `provenance_origin` and stays there even when it falls out of the trail after twenty previous versions; the trail itself is capped at twenty entries, and the total count stands next to it.
- **The merge hung off three call sites.** Anyone building a fourth could forget it — and a new writer would have deleted the fields of the same entry again. It now sits in `upsert_named`, the only place that writes into a collection with named vectors. A new point costs no additional read access; whoever has already read the payload passes it through; only restoration from a backup deliberately replaces raw.
- **A foreign entry could be overwritten.** The plugins' HTTP write path (`POST /write`) landed in `remember` and did not check the owner: an agent with the role `know` could overwrite a foreign point with a foreign identifier, and the point then carried its name. Now the same check applies there as in the modification path (`roles.is_owner`, resolved from the agent key, never from a field). A foreign writer gets a refusal with a reason, the point stays unchanged character by character, and the movement log holds exactly one line with the refusal — none claiming a write. `work` and `admin` may still change foreign entries; a new point with a fresh identifier stays allowed for every level.
- **The answer named the wrong scope.** A point in the scope `hermes`, rewritten without a scope, answered with `scope: "default"` — the point still said `hermes`, because the merge keeps the old value. What was wrong was the information: answer and movement log claimed a scope in which the entry did not lie. Both now name the stored value.

The rules sit in one place (`payload_merge.py`) instead of at every call site, and the tests cover the write paths: `test_overwrite_erhaelt_felder.py`, `test_upsert_named_zusammenfuehren.py`, `test_write_eigentum.py`, `test_restore_eigentum.py`, `test_scope_antwort.py`.

## [0.22.67] - 2026-11-22

**A test run no longer writes backups into the real home — and a backup is unreadable for other accounts.**

The finding from the release run of 0.22.66: `~/.nexus-memory/backups`
filled with seven files of 54.9 MB each during the test suite, and the log
held 16 lines of `Auto-backup failed: not enough values to unpack (expected 2, got
0)` — without a single red test.

- **Cause:** every `MemoryStore` and every Hermes provider starts a thread when built, which pulls the whole collection 60 s later. The storage location was fixed at
  `~/.nexus-memory/backups`; `NEXUS_DATA_DIR` was ignored. Against stand-ins the scroll failed, the loop caught it and wrote only a warning.
- **The folder follows the environment:** `NEXUS_BACKUP_DIR`, otherwise
  `<data directory>/backups`. Without environment variables the same path as before.
- **Switch:** `NEXUS_AUTO_BACKUP=0` keeps the thread out of the process altogether. In
  tests it is off; in operation the default stays on, and a backup that really failed
  keeps reporting as a warning.
- **Retention 7 -> 4:** one day at the service's 6-hour cadence, 220 instead of
  385 MB. The two old manual dumps in the folder are not counted, they stay.
- **Permissions of the plugin backup:** it was created with `open(..., "w")` and thus with
  the process's umask — at 0o022 every local account could read every private
  memory. Now like the service: folder 0o700, file 0o600, verification
  via `stat`, under Windows the ACL helper.
- **Tool description:** `backup` said "Includes payloads + vectors" and "every
  24h". Correct is: payloads only (the collection has named vectors, so a dump
  carries none, and restoring embeds anew) and every six hours.
- **Watchdog:** a session guard compares the REAL folder before and after the
  test run and lets the run fail if a new backup is added;
  `tests/test_backup_isolation.py` covers path, switch, retention, permissions and
  the persistent warning in eleven cases.

Python: 3404 tests green, 1 known red
(`tests/test_claude_code_write_path.py::test_leseweg_funktioniert`), 11
skipped — before 3394 green, the same red, 10 skipped. The one additional
skipped test is the reachability test of the pin chain: the pin points at
a commit that no remote ref contains yet.

> Entries from v0.22.67 onward are written in English, matching the published releases. Sections below v0.22.67 were written in German at the time and are kept unchanged as the historical record.

## [0.22.66] - 2026-11-22

**Jede Erinnerung nennt ihren Verfasser — oder sie wird nicht abgelegt.**

Bisher konnte ein Schreibvorgang ohne Agentenkennung durchgehen: der Punkt landete
mit dem Platzhalter `unidentified` oder ohne brauchbares Feld in der Sammlung, und
spaeter liess sich nicht mehr feststellen, wer ihn abgelegt hat. Im Bestand von
51.875 Punkten tragen 2.711 ueberhaupt ein `agent`-Feld (5,2 %), und davon sind
nur 4 Memories — 21.094 der 27.979 Memories (75,4 %) sind keinem Agenten
zuzuordnen. Genau diese Luecke schliesst die Aenderung an der Schreibstelle; der
Altbestand bleibt, wie er ist.

- **Die Kennung kommt aus dem Kontext, nicht aus einem Parameter.** `agent_detect.require_agent_id()`
  loest sie in dieser Reihenfolge auf: Agentenschluessel der Anfrage (MCP `x-nexus-agent-id`
  bzw. `actor` des Dienstes) → `actor`, den der Dienst durchreicht → `NEXUS_AGENT_ID`
  des Plugins. Ein Feld, das der Aufrufer behaupten kann, zaehlt nicht. Geprueft
  wird **vor** dem Einbetten — es entsteht kein halber Punkt.
- **Ohne Kennung wird abgewiesen, nicht still gespeichert.** `require_agent_id()`
  wirft `MissingAgentIdError`; die Wege antworten: MCP `{"status": "rejected",
  "reason": "No agent identity: …"}` plus eine `denied`-Zeile im Bewegungsprotokoll,
  HTTP `POST /write` mit `422` und demselben Grund, die DSH-Bruecke mit
  `{"stored": false, "reason": "no-agent-id"}` — der Turn des Menschen laeuft weiter.
  Lesen und Zaehlen verhalten sich unveraendert; `resolve_agent_id()` bleibt dort,
  wo der Platzhalter genuegt.
- **Ein eigenes Feld `writer` fuer die Verfahren.** Konsolidierung und Traeumen
  schreiben per direktem Upsert, nicht ueber `remember()`, und fuehren Aussagen
  mehrerer Agenten zusammen, deren Originale geloescht sind — jede Zuschreibung
  waere geraten. Ein konsolidierter Fakt traegt deshalb `writer: "consolidation"`,
  eine gelernte Lehre `writer: "dreaming"`, und `agent` bleibt dort leer. Abfragen
  koennen solche Punkte mit einem Payload-Filter ausschliessen, statt sie am Text
  zu erkennen. `agent` bleibt damit ein Personenschluessel, ohne Prozessnamen darin.
- **Altdaten werden nicht angefasst.** Keine Migration, kein `set_payload`, kein
  Nachtragen: 75,4 % der Memories waeren nicht belegbar zuzuordnen, und ein
  Sammelwert auf 21.000 Punkten ist ein Etikett ohne Wert. Punkte ohne `agent` und
  ohne `writer` sind normal und lesen sich wie bisher.

**Mit diesem Release ausgeliefert, nicht Teil dieser Aenderung.** Vier Commits
lagen schon vor heute auf `main` und landen mit 0.22.66 zum ersten Mal in einer
Fassung — sie stehen hier, damit niemand sie fuer neu haelt:

- **Vertrauensgrenze im eingebetteten Block** (`fd42257`, `a5ed519`): der
  Erinnerungsblock besteht aus gespeicherten Texten — Sitzungen, Dokumente,
  fremde Werkzeugausgaben — und eine Zeile darin kann eine Anweisung sein, die
  ein Mensch nie an diesen Agenten gerichtet hat. Hermes und Claude Code setzen
  jetzt dieselbe Kennzeichnung, die gespeicherte Texte als Daten markiert, bevor
  das Modell den ersten Eintrag liest (ClawHub-Befund "Persistent Prompt
  Injection Through Automatically Recalled Memories"). Sie zaehlt nicht gegen
  das Zeichenbudget: das Budget regelt Inhalte, nicht die Warnung davor.
- **DSH-Buendel von fuenf auf drei Eintraege** (`13865d2`): dieselbe Messung wie
  0.22.65 (fuenf Eintraege brachten 2,96 unpassende je Nachricht, drei 1,76),
  jetzt auch in der Buendel-Vorgabe des DSH-Plugins, mit Ausschluss von
  `session` und `entity` statt einer Erlaubnisliste. Die Mindestpunktzahl bleibt
  aus.
- **Claude-Code-Rueckfallversion auf 1.2.8** (`dd18878`): die Versionsangabe im
  Quelltext der Selbstauskunft lief der Manifest-Version hinterher.

Python auf der veroeffentlichten Linie: 3393 Tests gruen, 1 bekannter roter
(`tests/test_claude_code_write_path.py::test_leseweg_funktioniert`), 11
uebersprungen — vorher 3388 gruen, derselbe rote, 10 uebersprungen. Der eine
Uebersprungene mehr ist der Erreichbarkeitstest der Pin-Kette: der Pin zeigt auf
einen Commit, den noch kein entferntes Ref enthaelt.

## [0.22.65] - 2026-11-20

**Der Abruf liefert weniger, aber das Richtige, und jeder Lesezugriff steht im Protokoll.**

Gemessen an 25 echten Fragen: die automatische Erinnerung schickte im Schnitt
2,96 unpassende Eintraege pro Nachricht mit. Nicht weil die Suche schlecht war,
sondern weil ein fester Block von fuenf Eintraegen auch dann gefuellt wurde,
wenn nur zwei zur Frage passten.

- **Auswahl an einer Stelle:** neues Modul `src/nexus_memory/recall_policy.py`.
  Kategorien, Mindestpunktzahl und Anzahl werden dort entschieden, nicht in den
  Plugins; MCP und alle vier Wege reichen nur durch. Vorgabe: drei Eintraege,
  `session` und `entity` bleiben draussen.
- **Mindestpunktzahl standardmaessig aus.** Gemessen: 0.35 filtert nichts und
  kostet ab 0.45 passende Treffer (1,76 auf 1,84 Unpassende). Der Regler bleibt,
  die Vorgabe ist keine Schwelle.
- **Punktzahl richtig benannt:** `relevance` ist der Kosinus, nicht der
  normierte RRF-Rang. Der lag beim Spitzenreiter immer bei 1.0, weshalb die alte
  Schwelle wirkungslos war.
- **Lese-Protokoll:** `service_log.record_read()` schreibt eine Zeile je
  Lesevorgang in `read-YYYY-MM-DD.jsonl`: Zeit, Urheber, Anzahl Treffer,
  Kennungen der gelieferten Eintraege, Anfrage nur als Pruefsumme. Kein Inhalt,
  Rotation ueber dieselbe `RETENTION_DAYS = 90`, fail-open. Ein leerer Abruf
  schreibt nichts.
- **Schreibtor Regel 10:** ein diklierter Rest (Flickwoerter, Wortdopplungen,
  fehlendes Satzzeichen am Ende, langer Schachtelsatz, gewichtet ab fuenf
  Merkmalen) wird nicht verworfen, sondern als `session` abgelegt. Eine am
  Terminal bestaetigte Regel schlaegt die Heuristik. Trockenlauf ueber 2466
  Regeln: 10 Treffer, 0 Fehlalarme, einzeln gelesen.

Ergebnis der Messung: 2,96 auf 1,76 unpassende Eintraege je Nachricht, allein
durch drei statt fuenf. Der Kategorienfilter aendert auf dieser Stichprobe
nichts Messbares und kostet drei passende Treffer. Er bleibt trotzdem, weil
Sitzungsprotokolle und Entities dort nicht hingehoeren.

Python 3378 Tests gruen ausser den elf bekannten roten aus der Baseline.

## [0.22.64] - 2026-11-20

**Alle vier Wege melden jetzt: Claude Code und Hermes schliessen die Luecke.**

0.22.62 hat DSH und OpenClaw angeschlossen, 0.22.63 den Exit-Code eines Befehls
richtig gelesen. Offen waren die beiden anderen Wege, und damit war "universal"
noch nicht erreicht: nur die Haelfte der Agenten lieferte Vorfaelle.

- **Hermes:** `on_session_end` meldet die gescheiterten Werkzeugaufrufe der
  Sitzung (`exit_code != 0` oder gesetzter `error`) an `POST /events`. Neues
  Modul `plugins/memory/nexus/incident_report.py`, reine Funktion zum Bauen der
  Zeilen plus Melder. Kein Cron, kein Zeitplan: der Harness meldet ueber sich
  selbst, am Ende seiner eigenen Sitzung.
- **Claude Code:** neuer `Stop`-Hook `scripts/report_incidents.py`. Er liest das
  Transkript der Sitzung und nimmt die Werkzeug-Ergebnisse, die der Harness
  selbst mit `is_error` markiert hat. Kein Raten aus Text, kein zweiter
  Zeitplan.

Beide senden Werkzeugname, Fehlertext (gekappt auf 2000 Zeichen), Hinweis der
Umgebung und die Sitzungskennung; kein Gespraechsinhalt. Beide sind
abschaltbar (`NEXUS_REPORT_INCIDENTS=0`) und beide scheitern still: eine
Meldestelle, die schweigt, darf kein Sitzungsende kippen.

Sechs neue Tests: die Umwandlung aus Hermes-Nachrichten und aus einem
Claude-Code-Transkript, der Schalter, die Hook-Registrierung, und zwei
Rundlaeufe gegen einen winzigen lokalen Dienst, die Pfad, Schluessel und
Koerper pruefen. 14 Tests in dieser Datei, die Gesamtsuite bleibt gruen.

## [0.22.63] - 2026-11-20

**Ein Shell-Befehl mit Exit 1 ist ein Fehlschlag, kein Erfolg.**

0.22.62 liess den DSH-Plugin nur melden, was der Harness als Werkzeugfehler markiert
(`isError` oder ein `error`-Objekt). Ein Befehl, der laeuft und mit 1 endet, ist aus
Sicht des Harness ein gelungener Werkzeugaufruf — und damit ging die eigentliche
Fehlerquelle des Alltags nicht ins Journal. Gemessen nach dem Neustart: eine
gescheiterte Kommandozeile erzeugte null Meldungen.

Der Harness haengt sein eigenes Urteil an: `[exit code: N]` am Ende des Ergebnisses.
Der Reporter liest jetzt genau diesen Marker. `0` ist kein Fehlschlag; ein `null`
(Prozess ohne Exit-Code, etwa durch ein Signal) wird nicht geraten. Vier Testfaelle
halten die Regel fest, dazu einer, der den Marker im Quellbaum pinnt.

Das OpenClaw-Paket braucht keine neue Version: sein `exec`-Ergebnis traegt `exitCode`
als Feld, das der Reporter seit 0.22.62 schon liest.

## [0.22.62] - 2026-11-20

**Jeder Harness meldet seine Fehlschlaege jetzt selbst — die Regel ist in beiden Plugins dieselbe.**

0.22.61 hat die Quelle des naechtlichen Lernens umgedreht: Nexus liest keine fremde
Sitzungsablage mehr, sondern nimmt Vorfaelle unter `POST /events` an. Damit das nicht
nur fuer Hermes gilt, melden jetzt auch die beiden grossen Plugin-Pfade:

- **DSH-Plugin:** hoert auf `tool/result`, meldet nur, was die Plattform selbst als
  Fehler markiert (`isError` oder ein `error`-Objekt). Schalter `reportIncidents`
  (Standard an), Fehlertext auf `reportMaxChars` gekappt (2000), hoechstens
  `reportPerSessionMax` Meldungen je Sitzung (50). Nur der HTTP-Transport meldet;
  stdio hat keinen Endpunkt.
- **OpenClaw-Plugin:** neuer `after_tool_call`-Hook ueber `lib/incidents.ts`, dieselbe
  Regel und dieselben Grenzen. Das Plugin nennt keinen Agentennamen: der Dienst nimmt
  ihn aus dem Schluessel, der schreibt, damit keine Meldung eine fremde Herkunft
  vortaeuschen kann. Neue Einstellung `reportIncidents` im Manifest (das Schema hat
  `additionalProperties: false`, ein fehlender Schluessel waere ein Ladefehler) und in
  der Doku.

Beide melden nur die Fehlerzeichen der Plattform, nie aus dem Text geraten: die
Klassifikation (welche Fehlerklasse, ob daraus eine Lehre wird) bleibt in Nexus, an
einer Stelle und in einer Sprache. Ein Plugin, das nichts meldet, sieht von aussen
aus wie eines, das funktioniert — deshalb halten Tests die Meldung fest: neun
Faelle im DSH-Plugin (`test-report-incidents.mjs`), sieben im OpenClaw-Plugin
(`_test_incidents.mjs`), dazu sechs Python-Tests, die beide Quellbaeume pruefen.

Nichts davon sendet Gespraechsinhalt: gemeldet werden Werkzeugname, Fehlertext und
Sitzungskennung.

## [0.22.61] - 2026-11-20

**Das naechtliche Lernen haengt an keinem Harness mehr und legt keine Textfetzen ab.**

Bis 0.22.60 las das Traeumen die Sitzungsdatenbank von Hermes. Damit hing das
Langzeitgedaechtnis an einem bestimmten Agenten, und jede andere Installation hatte
dieses Lernen gar nicht. Dazu kam ein zweiter Fehler: es nahm jedes Vorkommen von
"fix", "fehler" oder "timeout" im Text, schnitt ein Fenster von 140 Zeichen heraus,
auch mitten im Satz einer langen Cron-Anweisung, und fragte ein Modell, ob das ein
wiederkehrendes Muster sei. Gespeichert wurde danach der Rohtext. Ergebnis im Bestand:
"Gelernt: must be recorded as failed, put [CRON_FAILURE] on the first line by itself
…", 24 solcher Eintraege in einer Nacht, alle spaeter zurueckgezogen.

Die Richtung ist jetzt umgedreht:

- **Neue Quelle `POST /events`:** jeder Agent meldet, was bei ihm gescheitert ist
  (`tool`, `ok`, `error`, `hint`, `session`, `agent`). Nexus liest keine fremde
  Sitzungsablage. Hermes, OpenClaw, Claude Code und DSH sind gleichberechtigte
  Zulieferer; wer nichts meldet, fehlt einfach.
- **Ablage ist Text:** `~/.nexus-memory/events/ereignisse.jsonl`, eine Zeile je
  Vorfall, gekappt auf 2000 Zeichen Fehlertext und 600 Zeichen Hinweis, Rotation bei
  20 MB. Kein Qdrant, kein Schema-Umbau.
- **Wiederkehr wird gezaehlt, nicht geschaetzt:** eine Stoerung zaehlt nur, wenn sie
  strukturell als Fehlschlag vorliegt (`exit_code != 0` oder gesetzter `error`) und in
  mindestens zwei verschiedenen Sitzungen auftritt. Kein Modell entscheidet mehr
  darueber. `llm_fn` wird nicht mehr befragt.
- **Gelernt wird nur mit Beleg:** der `hint` der Umgebung, oder eine benannte
  Umgebungsursache (fehlendes Paket, fehlendes Programm, HTTP-Code, Rechte). Ein
  Fehlerwort in einer erfolgreichen Ausgabe zaehlt nicht; ein namenloses "Timeout"
  auch nicht. Programmierfehler des Agenten (TypeError, KeyError, SyntaxError) sind
  kein Wissen.
- **Der Satz wird gebaut, nicht formuliert:** "Wiederkehrende Stoerung, 11 Sitzungen
  bis 20.11.2026: terminal scheitert mit ModuleNotFoundError (requests). Abhilfe laut
  Umgebung: …" Er geht durch dasselbe Schreibtor wie jeder Agent. Dieselbe Stoerung
  wird 30 Tage lang nicht erneut gelernt.
- **Optionale Zulieferer statt Kopplung:** `scripts/ereignisse-aus-hermes.py` liest
  eine Hermes-Ablage und meldet die Fehlschlaege per HTTP oder in die lokale Ablage.
  Wer ihn nicht aufruft, dessen Traum kennt nur die eigenen Meldungen.

Gemessen an 3300 gescheiterten Werkzeugaufrufen aus 30 Tagen Betrieb:

| | Ergebnis |
|---|---|
| Vorfaele im Journal | 3300 |
| erkannte Stoerungen mit Namen | 39 |
| wiederkehrend in mindestens zwei Sitzungen | 6 |
| davon als Lehre gespeichert (Beleg vorhanden) | 3 |

Nebenbei repariert: die Dublettenpruefung des Traeumens lief nie. Sie rief
`MemoryStore._embed` als synchrone Methode mit einer Liste auf, bekam ein
Coroutine-Objekt und warf im naechsten Schritt; der `except` verschluckte es, im
Dienst stand nur eine RuntimeWarning. Jetzt derselbe Einbettungsweg wie beim
Schreiben, mit Test.

`NEXUS_DREAM_STORE` bleibt im Code standardmaessig aus: wer das Lernen in den
Speicher lassen will, schaltet es bewusst ein. Die JSON-Datei unter
`learned-playbooks/` entsteht immer.

## [0.22.60] - 2026-11-20

**Nichts, was ein Modell beim Mitschreiben wegwirft, wird noch dauerhaftes Wissen.**

Gemessen an 300 Eintraegen, die von Hand als Muell eingestuft und aus dem Bestand
zurueckgezogen wurden (vier Monate Betrieb, 50.700 Punkte):

| Art | Anzahl |
|---|---|
| abgeschnittene Gespraechsreste ("User: …", "Assistant: …") | 130 |
| Zustandsmeldungen ("laeuft noch", "steht noch aus", "neu gestartet") | 99 |
| Rohsaetze, als Regel gespeichert ("Okay, was muss ich jetzt machen") | 25 |
| Gruss- und Zustimmungsfetzen des Assistenten | 24 |
| Platzhalter aus dem Kontextfenster ("Last conversation: [System: … truncated …]") | 22 |

Das Schreibtor (`nexus/write_gate.py`) kennt dafuer jetzt drei weitere Regeln, jede
mit Ablehnungsgrund in der Antwort:

- **Gespraechsrest:** eine Rollenmarke am Textanfang oder hinter einem Gedankenstrich.
  Bei einer Entitaet wird der Schwanz abgeschnitten und der Kopf behalten (dort stehen
  Rolle, Pfad, Preis); sonst faellt der Eintrag durch. `session` ist ausgenommen, weil
  dort ein Transkript die Sache selbst ist.
- **Frage und Fetzen:** ein Text, der ganz eine Frage ist, oder mit "Ok/Ja/Nein/Danke"
  beginnt, ist kein Wissen. Bei `rule`/`preference` gilt die strenge Form ohne Laengengrenze,
  damit eine handformulierte Regel nicht an einer Wendung mitten im Satz scheitert.
- **Zustandsmeldung:** "laeuft noch", "steht noch", "ausstehend", "erledigt" am Satzanfang
  ohne Laengengrenze. Wer eine solche Meldung halten will, schreibt sie als `temp`
  (sieben Tage), dann geht sie durch.
- Dazu die Platzhalter des Kontextfensters, die als laengster Text an jeder Laengengrenze
  vorbeigelaufen sind.

Gegentest in beide Richtungen, wie es sich fuer jede Regel gehoert:

| | Ergebnis |
|---|---|
| von 302 bekannten Muell-Eintraegen abgewiesen | 249 (83 %) |
| davon Entitaeten, bereinigt statt verworfen | 17 |
| von 13.955 behaltenen Eintraegen faelschlich getroffen | 25 (0,18 %) |

Die verbleibenden 34 sind lange Assistenten-Antworten und Vorfallberichte ueber rund
200 Zeichen in Kategorie `fact`. Jede Regel, die sie faengt, trifft auch echtes Wissen:
gemessen zwoelf Vorschlaege, darunter zwei aus einer Fremdberatung, kosten zwischen
0,85 und 2,95 Prozent des behaltenen Bestands. Sie sind deshalb nicht eingebaut;
`benchmarks/tor-schatten.py` misst jede kuenftige Regel gegen beide Seiten.

**Drei Wege schrieben bisher am Tor vorbei** und laufen jetzt durch dasselbe:

- `consolidation._store_fact` — der Verdichter baut Fakten aus rohen Gespraechs-Dumps,
  also genau dort, wo ein Modell Muell erzeugt. Ein abgewiesener Fakt wird uebersprungen.
- `dreaming._store_playbook` — gilt, sobald das Traeumen wieder anschreibt
  (`NEXUS_DREAM_STORE` ist derzeit 0).
- Der MCP-Weg gab eine Abweisung als `KeyError('id')` aus, weil das Tor kein `id` liefert.
  Jetzt kommt `{"status": "rejected", "reason": …}` beim Aufrufer an.

Zwei eigene Regel-Fehler hat die Messung ebenfalls gefunden und behoben: ein
Substring-Vergleich (`besteht aus` enthaelt `steht aus`) und eine Rollenmarke, die auch
`assistant:write` (OAuth-Scope) und `(user: paperless)` (Datenbankbenutzer) traf.

Dazu die Einblendung des DSH-Plugins: unter 30 Zeichen und vier Woertern wird nicht
nachgeschlagen — gemessen brachte "app neu gestartet" Treffer mit 1,0 und 0,80, eine
Scoreschwelle haette davon nichts gefiltert.

## [0.22.59] - 2026-11-19

**Zwei stille Ausfaelle in der Extraktion: der Schluessel `env:NAME` ging woertlich raus, und
ollama.com lieferte ueber `/v1` leere Antworten.**

Beide Male fiel die Extraktion auf die Heuristik zurueck, die keine Beziehungen findet — der
Graph wuchs nicht mehr, waehrend alles gesund aussah.

- `api_key: env:NAME` in `config.yaml`: der Aufloeser hatte keinen Zweig fuer diese Schreibweise
  und gab den Text unveraendert als Schluessel weiter. Jede Anfrage antwortete 401 (808 im
  Dienst-Log). Jetzt wird der Name ueber `_env_file_key` aufgeloest.
- `ollama.com`: ueber `/v1` wird `think: false` nicht durchgereicht, die Antwort kam leer zurueck.
  Der Extraktor geht fuer ollama.com jetzt denselben `/api/chat`-Weg wie fuer den lokalen Dienst
  und schickt den Bearer-Schluessel mit; andere Anbieter bleiben unberuehrt.

Tests: `tests/test_llm_env_indirection.py` neu, `tests/test_entity_extraction_path.py` um zwei
ollama.com-Faelle erweitert.

## [0.22.58] - 2026-11-18

**Die Fußzeile im README trug noch die alte Version, deshalb war der Audit von 0.22.57 rot.**

0.22.57 wurde veroeffentlicht, waehrend die Fußzeile im README noch `v0.22.56` sagte.
`test_readme_and_fallback_versions_are_current` vergleicht genau das und schlug an: der
Linux-Lauf brach nach 1801 bestandenen Tests mit diesem einen Fehler ab. Der Fehler lag
im Text, nicht im Motor. Hier sind Fußzeile und Badge nachgezogen, sonst aendert sich
nichts gegenueber 0.22.57.

Merke fuer den naechsten Schnitt: die Versionsnummer steht an fuenf Stellen und im README
an zwei, Badge und Fußzeile.

## [0.22.57] - 2026-11-18

**Der Engine-Pin zeigt wieder auf einen Commit, den `main` erreicht.**

Der Katalog-Eintrag pinnte die Engine auf `c41d832`, einen Commit aus der alten,
neu geschriebenen Kette. Der ist von `main` aus nicht erreichbar, ein
Katalog-Install haette also ins Leere oder auf einen fremden Stand gegriffen.
`8bee285` setzt den Pin auf den Release-Commit `09d5e9b` (0.22.56). Beide
Manifeste nennen dieselbe Kennung; `tests/test_pin_chain_is_closed.py` haelt das
fest.

Windows-Tests, vier Fehler, jeder mit Ursache im Test und nicht im Motor:

- `b7877de` — der Sandbox-Test schrieb ueber `HOME` statt `USERPROFILE` und
  landete auf einem gesperrten Pfad.
- `dc9b728` — der Node-Selbsttest wurde als Codepage gelesen; jetzt UTF-8 mit
  cp1252 als Rueckfall.
- `de529b0` — ein `join` auf einen Thread, den die Sperre nie startet.
- `669632d` — POSIX-Modusbits werden auf Windows nicht mehr geprueft, dort gibt
  es sie nicht.

Der Health-Audit-Daemon bleibt aus der Testsuite (`ce835b5`), der Motor ist
dafuer unveraendert geblieben; der erste Versuch griff in `src/` ein und wurde
zurueckgenommen.

Dazu die Kontaktadresse im Repo wiederhergestellt (`3390d6f`, `a14d08e`) und
die Benchmark-Werkzeuge mit ihren Messungen (`e5c5e73`, `2f942b4`, `da63c83`).

## [0.22.56] - 2026-11-17

**Der Quellen-Bonus hat die Relevanz ueberstimmt.**

Gemessen an 128 festgehaltenen Fragen ueber den laufenden Dienst, einmal mit und einmal ohne die
Aenderung: Der Bonus fuer vertrauenswuerdige Quellen wurde bisher mit dem Rangwert
**multipliziert** (bis Faktor 1,2). Das klingt harmlos, kostete aber 13 von 40 Platz-1-Treffern —
mehr als jede Aenderung an der Rangfusion je gebracht hat. Der Grund: Ein Fusionswert von 0,016
mal 1,2 sind 0,019, und damit springt ein Eintrag ueber mehrere Raenge, sobald er aus einer
bevorzugten Quelle stammt.

Jetzt entscheidet die Quelle nur noch bei **exakt gleichem** Rangwert. Gemessen: Als reiner
Gleichstands-Entscheider dieselben Treffer wie ganz ohne Bonus — der Sicherheitszweck bleibt also
erhalten, die Relevanz bleibt oben.

Vorher und nachher, dieselben 128 Fragen:

| Art | Platz 1 vorher | Platz 1 nachher | Top 5 vorher | Top 5 nachher |
|---|---|---|---|---|
| Zitat | 37 | 38 | 38 | 38 |
| Stichwort | 29 | 31 | 31 | 36 |
| Paraphrase | 13 | 16 | 22 | 29 |
| Kennung | 1 | 1 | 1 | 6 |
| **gesamt** | **80** | **86** | **92** | **109** |

Nicht gefunden: 20 vorher, 11 nachher. Keine Kategorie ist schlechter geworden.

Dazu ein Messwerkzeug, das diese Zahlen erzeugt (`benchmarks/messlatte.py`): eine festgehaltene
Liste von Fragen mit ihren Zieleintraegen, vier Arten, ueber den Dienst gemessen. Die Falldateien
bleiben lokal, weil sie Woertliches aus dem eigenen Bestand enthalten — im Repo liegt nur das
Werkzeug. Ebenso `benchmarks/diagnose_stufen.py` (Rang je Stufe der Kette) und
`benchmarks/fragen_bauen.py` (laesst ein Modell Paraphrase-Fragen bauen, mit Nachweis, wie
wortgleich sie sind).

Was NICHT gebaut wurde, obwohl es nahelag: ein Reranker, HyDE, ein Umschreiber bei jeder Abfrage,
oder ein anderer RRF-Wert. Der Befund zeigt, dass der Verlust nicht in der Fusion lag — die
Werkzeuge sind jetzt da, um das an echten Fragen zu belegen statt zu vermuten.

## [0.22.55] - 2026-11-16

**Wer den Wortlaut zitiert, findet ihn jetzt oben.**

Gemeldet und gemessen: Bei einer Abfrage, die den Text eines Eintrags woertlich zitierte, standen
zwei Eintraege ueber die Ollama-Ueberwachung auf Platz eins und drei — inhaltlich ohne jeden Bezug
zur Abfrage. Sie stehen in BEIDEN Trefferlisten (Wortlaut und Bedeutung), der zitierte Eintrag nur
in einer. Die Rangfusion belohnt das doppelt: wer in beiden Listen mittelmaessig steht, schlaegt
den einseitigen Spitzenreiter.

Die Rangfusion selbst bleibt unangetastet. Stattdessen ein **Ausloeser**: Deckt genau ein
sichtbarer Eintrag die Abfrage vollstaendig ab, gehoert er auf Platz 1. Deckt keiner oder mehr als
einer ab, aendert sich nichts — deshalb werden umformulierte Fragen nicht schlechter.

Drei Feinheiten, die beim Bauen aufgefallen sind und die den Unterschied machen:

1. **Schreibvarianten werden vorher geglaettet** — Kleinschreibung, Eszett zu ss, Umlaute zu
   ae/oe/ue, Satzzeichen weg, und zwar mit derselben Funktion fuer Abfrage und Eintraege. "scheiss"
   und "scheiß" sind derselbe Term. Das ist keine Tippfehler-Toleranz: ein echter Tippfehler deckt
   weiterhin nicht ab.
2. **Keine Stoppwortliste, sondern eine Schwelle aus dem eigenen Bestand.** Die Regel feuert nur,
   wenn die Abfrage mindestens zwei Terme enthaelt, die in hoechstens fuenf Prozent der Eintraege
   vorkommen. Damit fallen "der", "die", "und" von selbst heraus, ohne sprachabhaengige Listen —
   der Bestand ist deutsch und englisch gemischt.
3. **Zurueckgezogene Eintraege zaehlen nicht als sichtbarer Volltreffer.** Ein zusammengelegter
   Zwilling traegt denselben Text; ohne diese Unterscheidung findet die Regel immer zwei
   Volltreffer und feuert nie. Gemessen: 12358 zurueckgezogene Eintraege im Bestand.

Zwei Stellen mussten mitziehen, beide durch Messung gefunden und nicht durch Nachdenken: Der
Retriever sortiert vor dem Schnitt, und der Dienst sortiert danach noch einmal nach Nutzungswert —
eine Umsortierung im Retriever allein waere wirkungslos gewesen. Der Volltreffer traegt deshalb
eine Markierung, die die Sortierung im Dienst respektiert.

**Dazu zwei Haertungen aus dem Vorfall mit der springenden Zahl.** Beim Start stehen Sammlung und
Punktzahl jetzt im Protokoll, und fehlt die Sammlung, bricht der Start mit einer klaren Meldung ab
statt still gegen eine nicht existierende zu arbeiten. Der Grund fuer die Haertung: Die Anzeige
schien von 53.6xx auf 535 zu springen — in Wahrheit hatte ein Ausgabeschnitt die Zahl gekappt.

Gemessen nach dem Umbau, dieselbe Zitat-Abfrage: Grenze 1, 3, 5, 10 und 20 liefern den gesuchten
Eintrag jeweils auf **Platz 1**. Gegenprobe mit drei umformulierten Fragen: Reihenfolge unveraendert
plausibel.

14 neue Testfaelle (`tests/test_coverage_rule.py`).

## [0.22.54] - 2026-11-16

**Die Ausgabegrenze steuert die Menge, nicht die Trefferquote.**

Gemeldet von einem zweiten Agenten, nachgemessen am laufenden Dienst: Ein Eintrag, dessen Text
woertlich abgefragt wurde, war bei einer Grenze von drei, fuenf oder sechs **ueberhaupt nicht
dabei** — null Treffer. Bei Grenze acht einmal, bei zwanzig auf Platz eins. Dieselbe Abfrage,
derselbe Bestand, nur eine andere Zahl im Aufruf.

Ursache, gemessen: Der Kandidatenpool wurde aus der Grenze berechnet (`limit * 2`). Bei kleiner
Grenze waren das sechs bis zehn Kandidaten, und die waren zum grossen Teil Eintraege, die der
Aufrufer gar nicht sehen darf — in einer Probe trugen 31 von 40 Kandidaten den Geltungsbereich
einer fremden Sitzung. Der gesuchte Eintrag stand auf Rang 25, also hinter lauter unsichtbaren.
Nach dem Aussieben blieb nichts uebrig, und ein leerer Abruf sieht aus wie "gibt es nicht". Ein
Agent, der mit drei bis fuenf Treffern fragt, schliesst daraus falsch auf einen fehlenden Eintrag.

Was jetzt gilt: Der Pool hat eine Untergrenze von 100 Kandidaten, unabhaengig von der
Ausgabegrenze — an beiden Stellen (Retriever und Dienst). Die Grenze steuert damit nur noch, wie
viele Treffer ausgegeben werden. Als Test steht die **Praefix-Invariante**: Das Ergebnis fuer eine
kleine Grenze ist der Anfang des Ergebnisses fuer eine grosse.

Gemessen nach dem Umbau, dieselbe Zitat-Abfrage: Grenze 1, 2, 3, 5, 6, 8, 10 und 15 liefern den
gesuchten Eintrag — auf Platz eins. Bei Grenze 20 steht er auf Platz zwei, weil in der Zwischenzeit
ein anderer Eintrag haeufiger gelesen wurde; die Nutzung zaehlt in die Rangfolge, und jeder Abruf
ist ein Zugriff.

Was NICHT geaendert wurde: die Fusion selbst. Ein einseitiger Wortlaut-Sieger verliert in der
Rangfusion weiterhin gegen Eintraege, die in beiden Listen mittelmaessig stehen. Das ist der
naechste Schritt und braucht eine eigene Messung — der jetzige Umbau behebt den Fall, weil der
gesuchte Eintrag unter den sichtbaren ohnehin vorn steht.

Dazu 14 neue Testfaelle (`tests/test_recall_limit_invariance.py`).

## [0.22.53] - 2026-11-16

### Keine Doppel mehr am Sitzungsende, keine Fragmente, ein Weg zum Zurueckziehen

Drei Dinge, alle aus einem Befund vom selben Tag: Der Gesundheitsbericht meldete fuenf
zeichengleiche Paare. Nachgeprueft waren es jeweils derselbe Text, derselbe Agent, dieselbe
Quelle, dieselbe Stufe — nur vier Stunden auseinander (11:55 und 15:52). Im Protokoll stehen
dazu zwei Sitzungsenden derselben Unterhaltung, eines aus 326 Nachrichten, eines aus 545.

**1. Feste Kennung fuer Fakten aus dem Sitzungsende.** Der Grund fuer die Doppel war nicht der
Extraktor, sondern die Kennung: Entitaeten bekommen seit jeher eine feste Kennung aus Typ und
Name und ueberschreiben sich deshalb; Fakten bekamen eine zufaellige. Jetzt bildet der
Sitzungsende-Weg einen Schluessel aus Herkunft und normalisiertem Text (Kleinschreibung,
Mehrfach-Leerzeichen und Satzzeichen am Rand zaehlen nicht) und schreibt auf denselben Punkt.
Existiert er schon, bleibt er unangetastet — auch created_at und die Nutzungszaehler. Kein
Loeschen, keine Migration.

**2. Gespraechsfetzen werden nicht mehr gespeichert.** Vier der fuenf Eintraege waren keine
Fakten, sondern Aussagen des Nutzers an den Agenten, Rueckfragen und abgeschnittene Saetze. Der
Extraktor verwirft jetzt die zwei klaren Formen: eine Frage (Fragewort am Anfang oder
Fragezeichen am Ende) und eine Anrede an den Agenten ("Du hast ...", "dein ..."). Von den fuenf
gemeldeten Eintraegen fallen damit zwei raus — die Anrede und die Frage; drei bleiben: der
abgeschnittene Satz, die Antwort mit Befund und der Repo-Stand. Das ist im Test mit den fuenf
Originaltexten festgehalten; eine erste Fassung der Notiz behauptete "drei von vier", der Test
hat sie widerlegt.

Eine zusaetzliche Regel "kurz und ohne Satzende ist ein Fragment" waere naheliegend, ist aber
verworfen: Sie hat im Testlauf echte Fakten erwischt ("the user likes coffee", "Immer auf
Deutsch antworten") und drei bestehende Testfaelle rot gemacht. Lieber ein Fragment zu viel als
ein Fakt zu wenig.

**3. Zurueckziehen und Zusammenlegen ueber das Aenderungswerkzeug.** Bisher liessen sich
`lifecycle_status` und `supersede_reason` nur am Dienst vorbeischreiben — ohne Rollenpruefung
und ohne Protokollzeile. Das Aenderungswerkzeug hat jetzt zwei Felder: `withdraw` (Wahrheitswert)
und `reason` (Grund, dabei Pflicht), dazu optional `superseded_by` fuer das Zusammenlegen. Der
Text bleibt stehen, gesetzt werden Lebenszyklus, Zeitpunkt, Gueltigkeitsende und Grund — in
derselben Feldform, die der Dienst beim automatischen Ersetzen benutzt.

Am laufenden Dienst geprueft: Ohne Grund wird abgelehnt und der Bestand bleibt unveraendert;
mit Grund steht der Eintrag auf "deprecated", traegt den Grund und erscheint nicht mehr im
Abruf. 16 neue Testfaelle fuer Schluessel, Doppellauf und Fragmente.

## [0.22.52] - 2026-11-16

### Eine Kennung wird woertlich gesucht, nicht der Bedeutung nach

Der zweite Teil derselben Meldung: Zitate trafen nach 0.22.51 richtig, reine Kennungen nicht.
Abfrage "9122" lieferte auf Platz 1 einen Eintrag ("concept: high"), in dem die Zahl gar nicht
vorkam; der Eintrag "der serve-dienst laeuft auf port 9122" stand auf Platz 3.

Die Ursache: Eine nackte Zahl hat keine Bedeutung, die man suchen koennte. Die Bedeutungssuche
liefert trotzdem einen Ersten — und der bekam in der Zusammenfuehrung denselben Wert wie der
richtige woertliche Treffer.

Jetzt gilt: Besteht eine Abfrage praktisch nur aus einer Kennung (hoechstens drei Woerter, und
darin vier oder mehr Ziffern, eine Hex-Gruppe, eine Adresse mit Punkt oder Doppelpunkt, ein
Datum oder ein Punkt-Name wie `ai.nexus.serve`), dann sucht sie nur woertlich. Die
Bedeutungssuche bleibt draussen — ausser die woertliche Suche findet nichts, dann springt sie
als Rueckfall ein, damit kein leeres Ergebnis entsteht.

Gemessen nach dem Umbau, am laufenden Dienst:

- "9122" — alle drei Treffer enthalten die Zahl; der Eintrag ohne die Zahl ist verschwunden.
- "healthz 9122" — der gesuchte Eintrag steht auf Platz 1 (vorher nicht unter den ersten drei).
- "127.0.0.1:9122" — unveraendert richtig auf Platz 1.

Normale Fragen bleiben unberuehrt: Sie haben mehr als drei Woerter, greifen also gar nicht in
diese Regel. 13 Faelle sind als Test festgehalten, dazu der Nachweis, dass die Bedeutungssuche
bei einer Kennung nicht einmal gefragt wird und dass der Rueckfall greift.

## [0.22.51] - 2026-11-16

### Ein wortgleicher Treffer gewinnt wieder

Ein Eintrag, den ein Agent zwanzig Minuten zuvor geschrieben hatte, stand bei einer Frage, die
ihn Wort fuer Wort zitierte, auf Platz 2 — hinter einem Eintrag ohne inhaltlichen Bezug.

Die Messung zeigte, dass die Suche selbst richtig lag: In der woertlichen Liste stand der
gesuchte Eintrag auf Platz 1, mit 36,4 gegen 12,5 fuer den Zweiten. In der inhaltlichen Liste
kam er unter den ersten zwanzig nicht vor. Der Konkurrent war dort Platz 1 und woertlich weit
hinten. Bei der Zusammenfuehrung bekamen beide dadurch exakt denselben Wert (0.01639) — und im
Gleichstand entschied die Reihenfolge der Listen, nicht die Qualitaet des Treffers.

Zwei Aenderungen in der Zusammenfuehrung:

- **Klarer woertlicher Vorsprung zaehlt mehr.** Liegt der oberste woertliche Treffer mindestens
  doppelt so hoch wie der zweite, bekommt diese Liste Gewicht 1,5 statt 1,0. Ein grosser Abstand
  an der Spitze bedeutet: seltene Woerter, Kennung oder Zitat. Liegen die Treffer dicht
  beieinander, bleibt es bei gleich stark — eine umformulierte Frage wird also nicht bevorzugt
  oder benachteiligt.
- **Bei Gleichstand entscheidet der woertliche Beleg.** Haben zwei Treffer denselben
  Fusionswert, steht der mit dem hoeheren woertlichen Score vorn.

Gemessen an vier echten Eintraegen (Frage jeweils wortgleich zum gespeicherten Text): alle vier
stehen jetzt auf Platz 1; der Fall aus der Meldung vorher auf Platz 2. Semantische Gegenprobe
mit zwei umformulierten Fragen: dort greift keine der beiden Aenderungen (Gewicht bleibt 1,0),
die Reihenfolge bleibt wie vorher.

Vektor, Verfall, Quellen-Bevorzugung und Salience sind unberuehrt.

## [0.22.50] - 2026-11-16

### Ein frischer Eintrag hat wieder eine Chance

Ein gerade geschriebener Eintrag war in der Rangfolge praktisch nicht zu sehen, auch wenn die
Frage wortgleich zu ihm passte. Zwei Ursachen, beide gemessen:

- **Die Nutzung wog zu schwer.** Der Rangfaktor war `Grundtreffer × (1 + ln(1+Abrufe)) ×
  Verfall`, gedeckelt bei Faktor 4. Ein Eintrag mit 15 Abrufen bekam also das Vierfache
  dessen, was er inhaltlich verdiente — und schlug damit jeden Neuzugang, der inhaltlich
  genauso passte. Jetzt ist der Deckel 1,5. Oft gelesen heisst weiter "wichtig", aber es
  ueberstimmt nicht mehr die Passung.
- **Ein Neuzugang hatte gar nichts.** Er hat null Abrufe, also weder Verstaerkung noch
  Verfall (der ist fuer ihn neutral). Er trat damit gegen den vollen Bonus der Dauerbrenner
  an. Eintraege der letzten 24 Stunden bekommen jetzt einen Vorsprung von 1,15 — klein genug,
  um Relevanz nicht zu ueberstimmen, gross genug, um Gleichstand zu ihren Gunsten aufzuloesen.

Gemessen an einem echten Fall (ein Eintrag, den ein Agent 20 Minuten zuvor geschrieben hatte
und ueber eine wortgleiche Frage nicht wiederfand): vorher Platz 10 von 20, nachher Platz 6.
Der Rest des Abstands liegt nicht mehr an der Rangfolge, sondern am Grundtreffer selbst.

Die alten Grenzen sind unveraendert: Verfall senkt weiter nur das Ranking, geloescht wird
nichts, und `rule`/`procedure` bleiben immun (`salience >= 0.8`).

## [0.22.49] - 2026-11-16

### Harness-Plugin speichert wieder — und ein frischer Rechner laedt es

Zwei Fehler im Plugin fuer den DeepSeek-Harness, beide an derselben Stelle sichtbar: das
Gedaechtnis blieb still, ohne dass jemand einen Fehler sah.

- `captureText` kuerzte lange Turns mit `rstrip()`. Das ist Python, nicht JavaScript: jeder Turn
  ueber der Zeichengrenze warf einen `TypeError`, und das Speichern lief in den Fehlerzweig. Im
  Gedaechtnis kam nichts an. Jetzt `trimEnd()`, abgesichert durch einen Testfall (2000 Zeichen
  gegen eine Grenze von 1600).
- Auf einem frischen Linux-Rechner brach das Plugin mit `ERR_MODULE_NOT_FOUND` fuer
  `@deepseek-ai/dsh-llm` ab. Mac und Windows liefen nur, weil die Pakete dort zufaellig im Ordner
  lagen. `package.json` nennt beide jetzt (`@deepseek-ai/dsh-llm`, `@deepseek-ai/schemastery`), und
  der Installer fuehrt `npm install` im Plugin-Ordner aus und meldet Fehler deutlich statt still.
- Die Testabhaengigkeiten nennen `python-dotenv` jetzt selbst. Drei Testdateien lesen es, bisher
  verliessen sie sich auf ein zufaellig vorhandenes Paket.
- `opentelemetry-api` auf 1.45.2 (Dependabot #110, DoS beim Parsen von `traceparent`).

## [0.22.48] - 2026-11-11

### Schreibtor - eine Prüfung für alle Schreibwege

Bisher entschied jeder Weg selbst, was geschrieben werden darf: `nexus_remember` konnte eine leere
Erinnerung ablegen, ein Dokument konnte zu einer dauerhaften `rule` werden, und das OpenClaw-Plugin
umging jede Prüfung, indem es direkt per HTTP nach Qdrant schrieb. Jetzt gibt es ein Tor in der
Bibliothek, und jeder Schreibweg ruft es auf: Hermes-Plugin, MCP-Server, Claude-Code-Autocapture und
OpenClaw über den neuen Endpunkt `/write`.

- Es arbeitet fail-closed: lässt es sich nicht laden, wird das Schreiben abgewiesen statt ungeprüft
  durchzugehen. Reine Lesezugriffe laufen weiter.
- `rule` wird nur noch aus ausdrücklichem Aufruf oder Handeingabe angenommen, nie aus der
  automatischen Extraktion nach einem Turn; Zitatmerkmale im Inhalt werden zusätzlich abgewiesen.
- Die Zugriffsstufe erbt die strengste Stufe der Quelle: paperless und wiki werden `private`, eine
  Sitzung bleibt `trusted`, `public` vergibt das Tor nie selbst. Mail, Kalender, Gesundheit, Konto,
  Versicherung, Steuer und Rechnung sind immer private.
- `öffentlich` zählt nur als ganzes Wort; Pfade und `nicht-öffentlich` qualifizieren nicht mehr.
- Statt Zeichenlänge erkennt das Tor Etiketten: kurze Fakten mit Zahl gehen durch, reine Etiketten
  nicht.
- Herkunft ist Pflicht und wird notfalls aus dem Aufrufer gebaut; der Stichwortindex wird nach dem
  Schreiben nachgezogen, ein Indexfehler verhindert das Schreiben nicht.

### Regeln nur mit menschlicher Bestätigung

Eine Regel entsteht nur noch mit einem Nachweis, den ein Modell im Gespräch nicht erzeugen kann: ein
Zeitfenster-Token vom Terminal (`rule-confirm`, 10 Minuten, einmalig verwendbar). Ohne Nachweis wird
der Eintrag zu `fact` herabgestuft, nicht verworfen. `scripts/regeln-pruefen.py` prüft den Bestand
lesend.

Erster Lauf: 2.475 Regeln in der Sammlung, davon 0 von Hand bestätigt, 540 mit Zitatmerkmalen. Ein
Wiederholungslauf am 14.11. über 2.476 Regeln fand erneut keine bestätigte Regel.

### Aufsicht - täglicher Bericht über den Bestand

Ein Lesevorgang über die Sammlung, dann Zahlen statt Eindrücke: Punkte je Kategorie, Zugriffsstufe und
Schreiber, leere und sehr kurze Inhalte, Punkte ohne Herkunft, Etiketten ohne Satz, Zuwachs gegenüber
dem letzten Lauf und Abgleich gegen den Stichwortindex. Alarm nur bei Sprüngen. Berichte landen unter
`~/.hermes/state/nexus-aufsicht/`.

Erster Lauf: 51.847 Punkte, 632 ohne Inhalt, 5.146 ohne Herkunft (9,9 Prozent), 1.890 Etiketten ohne
Satz.

### Einspielweg - gewichtete Stichwortsuche, Fenster um die Fundstelle

- **Stichwortsuche mit Seltenheitsgewicht.** Ein eigener BM25-Index (SQLite FTS5) führt die
  Stichwortsuche mit Gewichtung seltener Wörter; Ähnlichkeit und Stichwortliste werden per RRF (k=60)
  verschmolzen. Der Index baut im Hintergrund, zieht bei Zählabweichung nach und fällt bei Ausfall auf
  reine Ähnlichkeit zurück.
- **Fenster statt harter Schnitt.** In den längsten Punkten steht die Antwort ganz hinten; der Schnitt
  bei 400 Zeichen warf sie weg. Jetzt wird ein Fenster um die erste Fundstelle geschnitten.
- **Volltextkandidaten nach Ähnlichkeit geordnet.** Die Volltextsuche liefert ihre Treffer in
  Kennungsreihenfolge; jetzt werden 64 Treffer geholt, gegen den Fragevektor gerechnet und die zehn
  ähnlichsten genommen. Der Zähler ging von zwölf auf sechs und steht jetzt bei zehn
  (`_KEYWORD_CANDIDATE_LIMIT`), je Eintrag ein Fenster von 400 Zeichen.
- **Messung** (20 Fragen, `scripts/prefetch-trichter.py`): 16 von 20 im fertigen Block, vorher 13. Der
  lokale Cross-Encoder macht es messbar schlechter (14) und bleibt draußen. Auf der 23er-Basis 19 von
  23 vor und nach dem Einbau; bei 4.000 Zeichen Budget meldet dieselbe Messung 21 von 23.

### Behoben

- **Block baut Inhalt mit Rückfall.** 632 Punkte tragen ihren Inhalt im Feld `text` statt
  `content`; der eingespielte Block las nur `content` und ließ sie unsichtbar. Jetzt greift
  ein Rückfall (`content` → `text`), auch beim Stichwortindex.
- **Zustandswerte am Längenfilter vorbei.** Der Mindestlängen-Filter (40 Zeichen) sperrte
  kurze Fakten aus. Ein vom Schreibtor gekennzeichneter Zustandswert (`state_value`) braucht
  nur 12 Zeichen.
- **Schreibtor erkennt Zustandswerte.** Kurzer Text bekommt das Kennzeichen, langer Text einen
  Hinweis, den Wert als eigenen Kurzfakt mit Herkunft und Datum abzulegen. Kein zusätzlicher
  Schreibvorgang, keine automatische Ableitung.
- **Der Entity-Extraktor behält den Satz.** Ein extrahierter Begriff trägt jetzt den Satz, aus dem er
  stammt, damit ein Treffer ohne den ganzen Punkt beurteilbar ist.
- **Sonde misst den laufenden Weg.** `scripts/prefetch-trichter.py` liest Inhalt und Grenze aus
  dem Plugin statt aus einer Abschrift eigener Regeln.
- **Neue Messbasis.** `scripts/prueffragen-23.txt`: 23 echte Fragen mit Muss-Stichwort, aus einer
  Zufallsstichprobe des Bestands gezogen.
- **Zwei veraltete Tests** prüfen jetzt Verhalten statt Schreibweise und sind unabhängig von der
  Notbremse `NEXUS_REWRITE`.

## [0.22.47] - 2026-11-08

### Fix - a shared room caps writes too, not only reads

The room state added in the previous release guarded the read side only. Recall served public memories in
a room a stranger had posted in, while `nexus_remember`, `sync_turn` and the OpenClaw store and
capture paths still wrote with the configured access level — so the owner's private material
went straight into such a room.

- The OpenClaw pre-check now reads with the room level instead of the configured level, so the
  gate and the write agree on what is allowed.
- Saving is capped to `public` in a room that is provably shared: OpenClaw `store` and
  Auto-Capture, Hermes `nexus_remember` and `sync_turn`.
- Reading stays fail-closed (an unknown room counts as shared); writing caps only on proof
  that a human other than the owner wrote there. Bots never count.
- Nothing is rejected — a memory that would have been private is stored as `public` instead.

## [0.22.46] - 2026-11-07

### Fix - in shared rooms only public memories are served

The plugin decided whether a room is shared from the chat type, so a Discord channel
counted as shared even when only the owner and its own agents ever wrote there, and the
owner's private memories stayed suppressed. Worse, nothing looked at who actually wrote:
in a room where a stranger had posted, recall, the tools and the graph lookups still
served whatever the configured access level allowed.

The plugin now observes the authors it sees. A room counts as shared as soon as a human
who is not the owner has written in it; bots never count. Until then the configured level
applies, and an unknown room counts as shared. Recall, `nexus_recall`, the graph tools and
`nexus_forget` all read the same room state, and `nexus_forget` also cleans the capture
retry queue. Auto-Capture stays blocked in every group turn.

The owner is identified per platform (`owner_user_ids` accepts one id per platform), and
the room state is kept in `~/.hermes/nexus/chat-sharing.json` (mode 0600) as author
hashes only. A prefetched memory block is rebuilt when the room status changed since it
was prepared.

### Fix - the reconstruction test only ran where a second checkout existed

`tests/test_query_rewrite_recon.py` put a directory outside the repository on `sys.path`
(`<parent-of-the-clone>/nexus-memory-test/src`) and imported `nexus_memory` through it. The
module was also a script rather than a test module: pytest imported it, executed the checks at
import time and reported zero tests for the file, so a failure only ever surfaced as a
collection error.

The extra path is gone. The checks are now separate `test_*` functions, so they appear in the
count and fail like any other test. The module imports `nexus_memory` the way the rest of the
suite does, through the source layout that `tests/conftest.py` already puts on `sys.path`.

### Fix - the suite could not be collected on Python 3.13

`tests/test_query_rewrite_recon.py` took `os` from `pathlib` (`from pathlib import Path, os`).
`pathlib` stopped exposing `os` as a module attribute in Python 3.13 (`hasattr(pathlib, "os")` is
`False` on 3.13, `True` on 3.11), so `pytest tests/ -q` stopped during collection with
`ImportError: cannot import name 'os' from 'pathlib'`. pytest aborts the whole run on a collection
error, so no test module ran at all. `pyproject.toml` declares `requires-python = ">=3.11"`, so 3.13
is inside the supported range.

The file now imports `os` directly. Collection succeeds and the suite runs on 3.11 and on 3.13.
## [0.22.45] - 2026-11-01

### Fix - dashboard shows a readable category instead of "uncategorized"

Around 4.3k of ~48.7k points carry no `category` field, and they are not one leftover
bucket: scanned documents (`doc_id`/`document_type` or `source=paperless`) and free-text
notes from a former collection (`type=turn/memory/index`). Both were displayed as
"uncategorized", which reads like an unfinished state. The label is now derived for
display only — `document`, `note`, `other` — and the payloads stay untouched.

### Fix - subprocess output no longer depends on the console code page

Ten `subprocess.run(..., text=True)` calls decoded child output with the parent's
preferred encoding and never passed `errors=`, so a child writing bytes that
encoding cannot represent made CPython's reader thread raise and left
`stdout`/`stderr` as `None` — without raising in the caller. On a Chinese
Windows install (`cp936`) a plain `git log` whose commit message is not ASCII
was enough; in UTF-8 mode (`PYTHONUTF8=1`, the default from Python 3.15) the
same happens to `icacls`, which writes the OEM code page.

Three error paths then raised `AttributeError: 'NoneType' object has no
attribute 'strip'` instead of reporting the failure they were meant to report
(`serve_daemon.ensure_serve_daemon`, and both the `git pull` and `pip install`
steps of the MCP update tool), one reported the wrong reason
(`setup._install_plugin`), and six silently lost the child's message.

Each call site now names its child's encoding — UTF-8 for `git` and `bash`,
`oem` for `icacls` on Windows, `PYTHONIOENCODING=utf-8` for child Python/pip
processes — and falls back to `errors="replace"`, so a wrong guess degrades to
a few mangled characters instead of a `None` that crashes the caller.

## [0.22.44] - 2026-11-01

### Security - the guardrail compared Windows paths in a form that never matched

`guardrail_check.normalize_path` normalised with `os.path.normpath`, which on Windows rewrites every
`/` to `\`, while the containment checks in `path_matches` compare against a literal `/`. A protected
path therefore never matched — neither itself, nor its children (`p + "/"`), nor its parents — and the
guardrail allowed deletes and writes it exists to block. The four failing `wave29` tests were the
first thing to say so.

The function now forces `/` as the separator on every platform, so one canonical form is compared
against. POSIX behaviour is unchanged.

Two limits are now written down instead of assumed: plugin installation on Windows degrades to
MCP-only, because every installer entry is a POSIX shell script, and the per-agent MCP config has no
`0600` promise there, because it inherits the directory ACL and holds no secrets.

### Fix - the automatic backup is protected on Windows too

`MemoryStore._do_backup` writes a copy of the store next to it and tightens the directory to `0700`
and the file to `0600` with `os.chmod`. On Windows those calls grant and deny nothing, the inherited
ACL of the profile directory stays, and the backup was readable by every local account. The windows
job reported it as `backup readable by group/other: 0o666`.

The backup path now applies the same ACL restriction that already protects the secrets file, for
the directory and the file, on Windows only. POSIX keeps the mode bits, which do apply there; the
test asserts the ACL content on Windows and the modes on POSIX, so neither platform loses its
guarantee and neither is skipped.

## [0.22.43] - 2026-11-01

### Fix - the webhook store no longer loses subscriptions on Windows

The store serializes its read-modify-write through a sidecar file lock so two processes cannot
overwrite each other's subscriptions. That lock was `fcntl.flock`, and the code was honest about
the gap: without `fcntl`, cross-process serialization degrades to none. On Windows the store
therefore kept only the asyncio lock of its own process, and two agents sharing
`~/.nexus-webhooks.json` dropped each other's entries.

The windows job showed it as a plain number: two processes wrote five subscriptions each, ten were
written, eight survived.

`msvcrt.locking` on one byte of the same sidecar file closes the gap, with the wait loop
`fuel_chain` already uses, because msvcrt has no blocking mode. POSIX keeps `flock` unchanged. If
neither module exists the store logs a warning once instead of pretending the lock is there.

## [0.22.42] - 2026-11-01

### Security - the secrets file is now protected on Windows, where `os.chmod` protects nothing

`write_env_key` and both wizard write paths set the `.env` to `0600` and its directory to `0700`
with `os.chmod`. On Windows those calls change nothing that grants or denies access: access comes
from ACLs, so the file kept the inherited rights of `BUILTIN\Users` and `Authenticated Users` and
was readable by every local account. The Windows audit job showed it as a single failing
assertion; the defect was in the code, not in the test.

The store now removes inheritance with `icacls` and grants full control to the current account and
`SYSTEM` only, for the file and its directory, and the wizard paths go through the same
restriction. The well-known principals are removed by SID rather than by name, because trustee
names are localized. A refused `icacls`, or an account name that cannot be resolved, is reported as
a runtime warning instead of being swallowed.

The tests keep the mode assertions on POSIX and check the ACL content on Windows, so neither
platform loses its guarantee and neither is skipped.

## [0.22.41] - 2026-10-31

### Tests and CI - the suite runs on Windows, and `fuel_chain` loads there

The audit job ran on `ubuntu-latest` only, so a Windows checkout looked like a broken project
without anyone seeing why. A second job now runs the same suite on `windows-latest`.

Its first run found a real defect: `import fcntl` at module scope made `nexus_memory.fuel_chain`
unimportable on Windows, so the consolidation daemon could not start and the suite stopped at
collection. Import and lock are optional now; POSIX keeps `flock`, Windows locks one byte of the
same file through `msvcrt.locking`, and with neither available the module books without a
cross-process lock and says so once.

A second run found the next one of the same kind: `tests/test_analytics.py` removed its temporary
directory while a `QdrantClient` still held `qdrant/.lock`. Windows refuses to delete an open file
(WinError 32), Linux does not care, so again only the new job saw it. The fixtures now close their
client before the directory goes away.

A third run found a test that measured the machine instead of the code: the coldstart test waited a
fixed two seconds for a helper thread to end, which a slower runner does not finish. It now waits on
the thread with a real deadline and asserts afterwards.

A fourth run found the last of the class: `tests/test_consolidation.py` imported `fcntl` inside a test
that proves the leader election through `flock`. That behaviour has no Windows equivalent, so the file
now imports `fcntl` behind a check and skips only the locking proof, and the shared marker lives in
`tests/conftest.py`.

A fifth run found a test that redirected `HOME` but not `USERPROFILE`. On Windows `expanduser("~")`
resolves through `USERPROFILE`, so the test read the real profile and picked up configuration that
earlier tests in the same file had written.

The sixth run showed the same open-handle error in a second file, which is how we learned the cause was
structural: nine test files each built their own temporary directory and their own path-backed Qdrant
client, and none closed the client before the directory went away. The `embedded_qdrant` fixture in
`tests/conftest.py` now owns both, and no path-backed client exists outside it any more.

The two symlink tests in `tests/test_guardrails.py` skip with a reason where the platform refuses
to create a link. Patch and tests by Simin Yuan (PR #12).

## [0.22.40] - 2026-10-29

### Memory - the plugin names the agent instead of leaving it unidentified

0.22.39 stamped the agent name by reading `NEXUS_AGENT_ID`. In practice that variable does not reach
the plugin process, so Hermes memories were filed as `unidentified` even though the plugin is Hermes
and knows it. A field that says nothing is worse than no field, because it looks filled in.

The plugin now passes the name: `NEXUS_AGENT_ID` when set, `hermes` otherwise. Measured after the
change with no environment set, `_agent_stamp()` returns `{'agent': 'hermes', 'instance':
'a9bd653dda02246b'}`. Two tests pin the behaviour, the default and the environment override.

## [0.22.39] - 2026-10-28

### Memory - the Hermes plugin stamps `agent` and `instance` as well

0.22.36 to 0.22.38 put the two provenance fields into the MCP server. Hermes does not store through
the MCP server, it stores through the plugin, so the fields stayed empty on the path that matters
most. Measured after the restart: a probe memory written through the plugin came back with
`agent: None` and `instance: None`.

The plugin now stamps both fields through `_agent_stamp()` and spreads them into the remember and
the entity payload. The helper is fail-open on purpose: when `nexus_memory` is not importable in the
host interpreter, which is the normal case by design, the point is stored with `None` stamps instead
of failing. A memory must never be lost because a label could not be read.

Both write paths now produce the same shape, so a point can be attributed no matter which agent
wrote it.

## [0.22.38] - 2026-10-28

### Memory - every installation keeps its own id file, even in a shared Nexus home

0.22.37 derived a new id from the installation's home but stored it in `instance.json` inside
NEXUS_HOME. Four Hermes profiles sharing one Nexus home therefore read the same file, so whichever
profile wrote first decided the id for all of them. Measured before the fix, two profiles with one
shared home both returned `2ebf77795f48a750`.

The file name now carries the seed: `instance-<8 hex of sha256(seed)>.json`. Four profiles sharing
one home write four files and read four ids. Measured after the fix with the same setup: `vater`
`841e9597ff4f2b5a`, `mutter` `2c1c09afd35e304c`, `kind1` `c3319c8826307a52`, `kind2`
`61f7294371275bd5`, and a second run returns exactly the same four values.

The old `instance.json` is deliberately neither migrated nor read: it arrived in 0.22.36, there is
no real data behind it, and carrying it over would hand the shared value to every profile, which is
the bug itself. The test for the shared-home case fails without this change and passes with it.

## [0.22.37] - 2026-10-28

### Memory - a new installation id comes from the installation's home, not from a random value

0.22.36 gave every store a random installation id on first use. It was stable, but it only told
data directories apart. Four Hermes profiles on one machine that share one Nexus data directory all
read the same `instance.json` and therefore carried the same id, which is the exact case the field
exists for. Telling the user to set `NEXUS_INSTANCE_ID` is not an answer, because nobody in that
situation knows the variable exists.

A NEW id is therefore derived from the home of the installation: `HERMES_HOME`, which Hermes sets
per profile, together with `NEXUS_HOME`, hashed to 16 hex characters. Four profiles have four homes
and get four ids with no configuration at all. An id that already exists is still only read back,
never recomputed, so moving a folder does not re-file memories that were stored before.

The seed is a location, not a guess: it says where the store lives and claims nothing about the
person or the machine. The rule that ids are never derived from hostname, user name or port stays in
force for everything else, and the docstring says why this case is different. Three tests cover the
behaviour: two homes give two ids, one home keeps its id when the environment changes, and no home
at all still yields a valid stored id.

## [0.22.36] - 2026-10-28

### Memory - every entry now says who stored it, and from which installation

A stored point carried `source` (where the information came from) and nothing about who put it
there. A preference could therefore be read later without anyone knowing whether it came from the
user, from Hermes or from another agent, and two Hermes installations on two machines looked
identical because the agent id is the same for both. This surfaced first as a real mix-up between
two agents on the same store, and it matches the transplant measurement in Yuan (2026): a memory
store carries more of an agent's identity than it looks.

Every stored point now carries two fields. `agent` comes from `resolve_agent_id` (explicit value,
then `NEXUS_AGENT_ID`, then the unidentified placeholder). `instance` comes from
`resolve_instance_id`: `NEXUS_INSTANCE_ID` if set, otherwise `instance.json` in the Nexus home,
created once on first use with a 16 hex character id. Both are returned wherever `source` is
returned today, including the hybrid and BM25 hit paths.

The instance id is deliberately never derived from hostname, user name, path or port. A guessed id
files a memory under the wrong installation, which is worse than no id. A corrupt `instance.json`
is replaced rather than used, and a read or write error yields an in-memory id instead of failing
the store call: saving a memory must not fail because of an id. Points stored before this release
return nothing for both fields; there is no migration.

## [0.22.35] - 2026-10-28

### Security - protected-path rules match on Windows too

`_path_matches` decided whether a target lies inside a protected path, and whether a protected path
sits inside a directory that is about to be deleted, by appending a literal "/" to the path it
compared against. On Windows the normalized path carries backslashes, so neither test could ever be
true: with a rule protecting `C:\data\protected`, both `rm -rf C:\data\protected\file.txt` and
`rm -rf C:\data` came back as an unprotected target. Exact matches on the protected path itself
still blocked, so only the two nested cases were lost. macOS and Linux were not affected by this one.

The comparison goes through `commonpath` now, so it uses the platform's own separator. That also
covers a protected root, and there it turned out the old code was wrong on posix too: "/" plus "/"
never matched. A module level `_osp` hook lets the test suite run the matcher under `ntpath` and
`posixpath` on any host, so this class of bug fails on a macOS run instead of waiting for a Windows
machine.

Reported and fixed by Simin Yuan (@simin-yuan). `tests/test_guardrails_paths_crossplatform.py`
covers the child path, the parent delete, a sibling with a shared prefix, a protected root and a
different drive.

## [0.22.34] - 2026-10-28

### Security - the guardrail patterns no longer backtrack

The patterns that classify destructive commands were written as `A.*B`. A plain `A.*B` is
retried from every occurrence of A and backtracks through every occurrence of B, so a crafted
command costs time quadratically, and cubically for `git.*clean.*-[fd]` where two `.*` are
scanned. Nothing caps the command length, and the check runs before every terminal command in
this setup, so a long enough line stalls the caller.

The eight patterns are now written as `(?m)^(?>.*?A).*?B`. The atomic lazy group commits to the
first A on each line, which for fixed-length tokens is always the choice that keeps the verdict,
so the scan is linear and the verdict is unchanged. Measured on this machine: `"git clean "`
repeated without a terminator takes 3.3 s at 16,000 characters on the old patterns and 0.1 ms on
the new ones. A seeded generator over 480,000 commands finds no verdict difference between the
two versions.

Reported and fixed by Simin Yuan (@simin-yuan). `tests/test_guardrails_linear.py` covers each of
the eight patterns, including the input that used to be the worst case: a repeated prefix that
never reaches the terminator, 100,000 characters.

## [0.22.33] — 2026-10-27

### Fixed — the agent registry module loads on Windows, and its lock works there

`agent_detect.py` imported `fcntl` at module scope. That is a POSIX module, so on Windows the module
could not be imported at all, and two consumers import it on module scope themselves: the setup flow
(`src/nexus_memory/setup.py`) and every test, because the autouse fixture in `tests/conftest.py` pulls
it in. Both stopped working there.

`fcntl` and `msvcrt` are optional now. `_lock_file`/`_unlock_file` take `flock` on POSIX exactly as
before, `msvcrt.locking` on Windows (non-blocking on one byte at offset 0, retried for up to 10 s, so a
stuck peer raises a clear timeout instead of hanging), and warn once and continue when neither module
exists. A shared request falls back to an exclusive lock on Windows because `msvcrt` has no shared
mode; that is stricter, never weaker.

On a real Windows host the previous revision raises `ModuleNotFoundError: No module named 'fcntl'`;
this one imports, registers an agent into the registry, and a second process waited 1.71 s for a 2.0 s
hold by the first. Nothing else changed: the suite stays green on macOS (2481 passed, 4 skipped in
the run recorded for this release; the skip count moves with the environment), and the new
`tests/test_agent_detect_windows_import.py` covers the import without `fcntl`, the Windows branch
against a recording fake, and the no-lock fallback.

## [0.22.32] — 2026-10-25

### Fixed — the wikilink scan no longer runs quadratic on a crafted memory

`WIKILINK_PATTERN` in `nexus/discovery/classifier.py` could not cross a closing bracket, so every
opening pair without a closing pair rescanned to the next bracket. One stray closing bracket at the
end of the text made the scan quadratic: 32 001 characters took 1.6 s, 64 001 took 6.4 s, and that
call runs inside discovery, which is single-threaded, so the agent waited for the whole run. The
text is the body of a memory fact and an agent writes it from web pages, tool results or injected
instructions, so an attacker could shape it. Nothing capped the length of a fact.

`_check_explicit_reference()` now uses a single left-to-right scan with two skips. The output is
identical: 24 edge cases including nested brackets and 200 000 random strings over brackets, letters
and spaces produce zero differences against the pattern. The same 64 002 bracket characters took
about 6.5 seconds before and a few microseconds now. `WIKILINK_PATTERN` stays in the module for existing imports.

Reported and fixed by Simin Yuan (@simin-yuan). The linear scan in this fix comes from the report:
the function differs from the reported version only in the docstring, in the position of one local
assignment, in one slice spacing and in two dropped comment lines. An earlier version of this entry
credited the work to "Simon Yuan", which is the wrong first name, and named the report only.

## [0.22.31] — 2026-10-25

### Changed — lock refreshed for five packages, test runner on pytest 9

Seven open advisories sat on five packages in the lockfile; all seven are addressed here:

- `certifi` 2026.7.22 → 2026.10.23 (bundles CAs with a compromised intermediate, critical) — 1 advisory.
- `filelock` 4.0.3 → 4.2.4 (read/write lock handling) — 3 advisories, one high and two low.
- `setuptools` 84.0.0 → 84.2.1 (path escape while reading metadata) — 1 advisory, low.
- `opentelemetry-api` 1.45.0 → 1.45.1 (`tracestate` parsing) — 1 advisory, medium.
- `pytest`: the bound was `>=8,<9`, but the advisory ("pytest has vulnerable tmpdir handling") is
  fixed only from 9.0.3. The bound is now `pytest>=9.0.3,<10.0.0` and the lock resolves 9.1.1 —
  1 advisory, medium. The full suite runs green on it: 2440 passed, 3 skipped, 0 failed.

The dependency refresh itself changes no runtime behaviour; the daemon fix from 0.22.30 is unchanged.

## [0.22.30] — 2026-10-24

### Fixed — the maintenance daemons stay quiet on a fresh installation

- The two in-process daemons (trust recompute, retrieval watch) stay quiet on an installation whose
  collection does not exist yet: one INFO line, no stack trace, no false quality alarm. A fresh
  install no longer looks broken.
- A scroll result that cannot be unpacked is reported once as a warning with a clear statement
  instead of raising a `ValueError` inside the daemon thread.
- The test suite switches both daemons off, so background work can no longer hide real failures
  behind unrelated warnings.

## [0.22.29] — 2026-10-24

### Changed — model and endpoint come from the installation that runs it

Reranking and consolidation no longer carry a built-in model name or a built-in endpoint. Both
resolve at call time what the installation that runs them has configured: `model.default` and the
provider block of the configuration, or the environment overrides. With nothing configured there
is no call at all — one warning per process, and the previous order is kept.

- Consolidation resolves `NEXUS_CONSOLIDATION_MODEL`, then `nexus-memory.consolidation_model`,
  then `model.default`. An empty result means the daemon stays idle instead of sending a model
  name the installation does not have; the module no longer holds a default string, and the
  legacy `OLLAMA_MODEL` name resolves through that function on every access.
- The reranker's endpoint counts as configured only when base URL, key and model are all known.
  Otherwise it is skipped before any request, and the caller keeps the vector order.
- The station tile in the dashboard reads through the same resolver, so it can only show the name
  a call would really use — and nothing when the installation has configured no model.

### Added — the dashboard can switch both features

- `GET /api/settings/rerank` returns `enabled`, `reranker`, `pool`, `llm_model` and
  `llm_configured`.
- `POST /api/settings/rerank` writes the switch atomically after an automatic timestamped backup
  of the configuration, keeps every other key and comment, and answers `400` on invalid input
  without touching the file.
- The dashboard carries a visible switch for recall ordering and one for memory AI, each with a
  live hint line (what it does, whether a model is configured) and the monthly cap.

### Fixed — access counting is universal

Counting moved out of the protocol edges and into the store path, so every read and write that
goes through the service is counted, whichever client performs it. An agent id that is not in the
registry registers itself on first sight instead of being dropped, and an access without any id is
counted under a neutral label instead of vanishing. The two plugins that talk to the vector store
directly report their access to the same registry. Self-checks opt out with
`NEXUS_AGENT_SELFTEST=1`, so a probe no longer appears as an agent.

### Changed — static assets are versioned

The dashboard loads its script and stylesheet with the release version in the URL, so an update is
visible after one reload instead of a stale file coming out of the browser cache.

## [0.22.28] — 2026-10-24

### Changed — the reranker is the model the agent already runs

Recall was not the bottleneck, ordering was. With 20 questions about facts the corpus holds, the
provider's own recall path put the answer inside the top five 14 times with the cross-encoder it
used before — the correct entry was usually in the candidate pool, below position five.

- `reranker.llm` is a third option and the new default; `"auto"` and an empty value resolve to it,
  an explicit `"cross-encoder"` stays available. One chat-completion call per deliberate search
  over the provider's rerank pool (`nexus-memory.rerank_pool`, 40 in this install) with 300
  characters per candidate.
- `reasoning_effort: "low"` is mandatory: without that brake the model spends the whole output
  budget on thinking and returns empty text with `finish_reason=length`.
- One repeat request on an empty or unusable answer; after that the reranker returns nothing and
  the caller keeps the original order, with a single warning per process.
- Credentials come from `NEXUS_RERANK_LLM_API_KEY`/`_BASE_URL`/`_MODEL`, otherwise from the
  Hermes credential pool entry `ollama-cloud`.
- The memory-dynamics tie-break in the Hermes provider now runs only when the active reranker is
  not the LLM one: it reorders within 0.02 vector-score windows by usage frequency and destroyed
  the order the model had chosen.

Measured through the provider's own recall path with the same 20 questions, the same collection,
the query rewrite switched off and the same candidate pool: 14/20 with the previous default,
**17/20 with this change**. In one of those 20 searches the reranker gave up after the repeat
request and the original order came back; that run is counted as it ran.

## [0.22.27] — 2026-10-24

### Removed — the Voyage provider is gone, not just unused

The zero-Voyage policy of 2026-10-17 was decided but never enforced in the code: `voyage`
stayed a selectable provider (embeddings, reranker, cost routing, wizard, both agent plugins,
installer examples) and the lockfile still resolved `voyageai`. Anything that merely checked
for a `VOYAGE_API_KEY` could therefore still pick Voyage.

- **Embeddings.** `VOYAGE_API_KEY`, the `_try_voyage` path, the Voyage dispatch in `embed()`
  and the `detect_available()` entry are gone; the optional dependency `voyageai` (extra
  `voyage`) is no longer declared.
- **Reranker.** The Voyage Rerank endpoint is removed. The default is the local
  cross-encoder; `"auto"` maps to it, and an explicit `"voyage"` is rejected by name instead
  of silently falling back.
- **Cost router / wizards.** `voyage` is out of the tier table, the provider priority and the
  `vo-`/`pa-` key detection, so the router can no longer bill or prefer a provider that does
  not exist.
- **Plugins.** The Claude Code hooks and the OpenClaw plugin no longer know the provider (and
  no longer default their model name to `voyage-4`); the OpenClaw installer examples point at
  a provider that is actually supported.
- **Lockfile.** `uv lock` drops `voyageai` and the packages only it pulled in, and repairs two
  versions the file had pinned outside the declared ranges (`openai` 3.x, `pytest` 9.x).

Tests that only covered Voyage-specific branches were removed with it; tests that guard
general behaviour (provider selection, fail-closed explicit choice, auto stays local) remain
and now assert that the rejected provider fails loudly.

## [0.22.26] — 2026-10-24

### Fixed — the knowledge graph no longer answers "nothing there" when the truth is "you may not look" or "I could not look"

The ClawHub audit of the OpenClaw plugin (`@neboy72/nexus-memory` 1.21.14) flagged one real
defect among two false positives: the four graph tools read and emitted points **without
checking `access_level`**, so an agent running at `public` could read `private` entities — and,
worse, reach nodes hidden *behind* a private one. The same hole sat in the Python core
(`nexus/graph/traversal.py`), which the Claude Code hook uses.

- **One rule, everywhere.** `GraphTraversal` now takes the caller's level and treats a point
  without `access_level` as `private`; an unknown level sees nothing (fail-closed). The
  OpenClaw tools build their gate from the same `visibleAccessLevels()` that `search` and
  `forget` already used.
- **Every caller hands its own level in.** `mcp_server`'s four graph tools take an
  `access_level` parameter (default `private`, the server's own convention) and reject an
  unknown value by name instead of returning an empty list. The Hermes provider derives the
  level from its existing `_visible_levels()`, so there is no second source of truth; the
  bundled integration copy got the same helper so the two cannot drift.
- **An outage is not invisibility.** Neighbour and target lookups used a call that answers
  `null` both for "point deleted" and for "request failed", so a brief Qdrant hiccup made edges
  vanish and the tool claim "No related facts found.". They now use the strict lookup: 404 keeps
  the silent skip, every other failure surfaces as an error.
- **`find_entities` counts what the caller can see.** The visibility filter ran after the scroll
  cap, so a `public` agent got fewer than the requested number of entities while more visible
  ones existed. The levels are part of the Qdrant filter now; an unknown agent level answers
  without touching Qdrant at all.
- **Proof.** `tests/test_graph_callers_access.py` (11 of 14 red before), `tests/test_graph_access_level.py`
  (22 cases), `plugins/openclaw/test-graph-access-filter.mjs` (11 checks), plus
  `test-find-entities-visible-cap.mjs` and `test-graph-lookup-failure.mjs` (3 red cases each on
  the previous commit). All green here.

## [0.22.25] — 2026-10-15

- **The local HuggingFace model no longer runs on Apple's GPU — and the host stops loading one
  at all there.** On Apple Silicon `sentence-transformers` resolves to `mps` by itself, and
  PyTorch's MPS backend aborts the WHOLE process when two encodes overlap
  (`failed assertion _status < MTLCommandBufferStatusCommitted` / `A command encoder is already
  encoding to this command buffer`). Measured in the live gateway log, read on 2026-10-15: **nine hard
  process deaths in five days** (11.10.–15.10.) — eight of them `Fatal Python error: Aborted`, one
  a `Segmentation fault` — and every one of them on or directly behind a `Batches: 0%` line, in
  threads that were
  embedding the agent's own memory. The first in-process embedding run in that log is
  **10.10. 17:17:48** (`Loading weights`) with **17:17:55** for the first `Batches:` line — and
  nothing of that kind appears anywhere earlier in the file (its first dated line is 16.08.2026).
  The engine now pins the model to the CPU on Apple Silicon (`NEXUS_HF_DEVICE` overrides it;
  elsewhere the library keeps its own choice), and the Hermes plugin no longer builds one there by
  itself — the vector comes from the local service, `NEXUS_EMBED_IN_HOST=1` is the explicit
  opt-in. Both guards are red without the fix — two failing tests in
  `tests/test_hf_device_pinned.py`, one in
  `plugins/memory/nexus/test_embed_host_never_loads_model.py`.
- **The plugin no longer puts its private venv on the host's `sys.path` at all.** The
  catalog review of the admission request was right: prepending the venv made every
  shared library (`httpx`, `pydantic`, `openai`) resolve to the plugin's copy for the
  whole host process, and two plugins doing that would break each other. Appending
  alone is not the fix either — `sys.modules` is process-global, so whatever is
  imported first stays for the rest of the process, and `site.addsitedir` would
  execute the venv's `.pth` files inside the host. The private venv is now only the
  runtime of the local embedding service; the plugin asks it for vectors
  (`POST /embed`, loopback) and never imports an embedding stack into the host. If the
  service is unavailable the plugin still loads and every read answer says so
  (`⚠ local embedder unavailable: <reason>; fix: <command>`) instead of returning an
  empty list. Five guards pin it (`plugins/memory/nexus/test_embed_service_guards.py`),
  all of them red before the fix.

## [0.22.24] — 2026-10-12

### Fixed — the dashboard told the truth about the store: full counts, a real search, honest names

Seven defects in `dashboard/`, all of the same family: **a window was presented as the whole
store, and a wrong part was named for the fault.** The memory itself was healthy the whole
time — the display was not.

- **The graph showed two colours because it read one page.** `/api/memories` scrolled
  `min(limit * 2, 5000)` points when no filter was set and then trimmed to `limit`. With ~75 %
  `fact` in a store of 38.6k, that window held 468 facts, 20 rules, 1 belief — and **zero
  sessions**. The graph page builds its nodes from exactly this endpoint, so it looked as if
  the store contained nothing but facts and rules. It now reads the full collection through
  `_scroll_all_memories` (~0.7 s) and takes a **cross-section**: round-robin over the
  categories, so a small one stays visible next to the mass of facts. `category_counts` is
  derived **before** the trim, so the legend no longer mirrors the slice (it reported 20 where
  the store holds 28,931).
- **`/api/graph` had the same defect** — it scrolled `{"limit": 500}` with no pagination and
  read the raw `payload["category"]` instead of `_extract_memory_fields()`, so two views of the
  same store disagreed. Counts now come from the full collection, the node budget is spread
  over the categories (`per_cat = max(1, BUDGET // len(groups))`), and the category is derived
  the same way everywhere.
- **The search route did not exist.** `webui-js/api.js` has always called
  `/api/memories/search?q=…`; the server never had that route, so the search box could not
  return anything. It exists now and embeds through the running service (`POST /embed`) rather
  than building its own model — the service already holds the same provider and vector space.
  A missing embedding is reported as an error, never as "nothing found".
- **"Qdrant Offline" was shown for a missing collection.** `dashboard.js` printed that text
  whenever `qdrant_healthy` was false, and `qdrant_healthy` was set from the *collection*
  probe. That is how a stale collection name looked like a dead database. `/api/status` now
  separates the two faults (`store_state`: `ok` / `collection_missing` / `qdrant_unreachable`)
  and names the one that actually applies.
- **The embedding card named a provider that was not in use.** Both `/api/status` and
  `/api/health` read the config file and then any present cloud key — they never looked at
  `NEXUS_EMBEDDING_PROVIDER`, which is the engine's first source of truth. A leftover key
  therefore won: the card said `voyage` while the local model did the work. One helper
  (`_detect_embed_provider()`) now follows the engine's own order, and the model name comes
  from the collection's vector fingerprint, so it names what is really in there.
- **A model init inside the event loop could silence the whole dashboard.** `/api/status`
  built `EmbeddingProvider()` synchronously to read its name. On a 16 GB Mac with the memory
  service already holding the embedding model, that init ran out of memory — and blocked
  **every** endpoint, not just that one (all requests timed out, `/api/health` included).
  That call is gone: the name is read without building anything. A failing model must never
  take the interface down with it.
- **Nine categories were painted in the temp colour.** The palette knew six categories; the
  rest fell back to `#6b7280` — byte for byte the temp colour. In the graph that meant 227 of
  500 nodes looked like "Temp" while the store holds exactly **one** temp entry, and the
  legend (hardcoded in `graph.html`) asserted the same wrong thing about 6,024 entries. Every
  category now has its own colour, the fallback is a distinct bright tone, and the legend is
  **built from the store** — it shows each category with its real count, so it cannot drift
  from the data again.
- **The embedding card named the model twice and wrapped mid-word.** The value comes from the
  collection's vector fingerprint (`qwen_qwen3-embedding-0_6b` = provider + organisation + model),
  and the prettifier dropped the word "embedding" *together with the hyphen next to it*, so the
  string collapsed to `Qwen_qwen30_6b`: "Qwen" twice, an unreadable number, and — because the
  column is narrower than the text and `overflow-wrap: anywhere` permits it — the last letter on a
  second line. The organisation prefix is now removed **only** when the model name repeats it
  (`qwen` + `qwen3-…` → `Qwen3-Embedding-0.6B`), and "Embedding" stays in the name: the short form
  `Qwen3-0.6B` would read as the chat model, not the embedding one. The tile never wraps any more —
  `fitOneLine()` shrinks the font (19px → 12px at a 1160px window) until the name fits, and refits
  on resize.
- **The Fuel card named an AI that some users cannot reach.** `/api/status` read
  `NEXUS_CONSOLIDATION_MODEL` with a hard-coded default and derived a "provider" from the mere
  presence of `:cloud` in that string — it never asked the chain. A user without local Ollama and
  without any API key therefore saw *"GLM-5.3-Flash · ollama-cloud"* with the toggle ON, while the
  real chain logged `fuel: all stations closed — daemon sleeps until next tick` and sorted nothing.
  The card now asks the chain itself: `open_station` (probed; only Ollama is verifiable),
  `next_station`, `model`, `provider`, `verified`, and all `stations` with
  `offen` / `bereit` / `zu`. With nothing available the model name is **empty** and the card says
  "keine Station — Erinnerungen bleiben unsortiert" instead of naming a model nobody can reach; a
  bare key is shown as *"Schlüssel vorhanden, ungeprüft"*, never as a fact. The probe runs in a
  thread (`asyncio.to_thread`) — an HTTP call in the event loop took the whole surface down on
  12.10. Five guards pin it (`tests/test_fuel_status_honesty.py`), all of them red before the fix.
- **The doors of the sorting chain were explained nowhere, and the cost line never showed a
  real amount.** `NEXUS_FUEL_BASE` / `NEXUS_FUEL_KEY` / `NEXUS_FUEL_MODEL` lived only in
  `fuel_chain.py` and in tests: a user without local Ollama and without an
  OpenRouter/OpenAI/Nous key had no way to learn what would make sorting work. The README now
  names all five doors in the order the chain tries them, and the setup wizard closes with the
  same honest line. The status itself moved into the engine (`fuel_chain.station_status()`), so
  the surface and the setup read one truth instead of rebuilding the chain; the Ollama probe is
  shared by fuel time and the status call. Separately, the tile read
  `~/.nexus-memory/fuel_spend.json` looking for a key `"YYYY-MM"` or `"total"` while the engine
  writes `{"year_month": …, "spent_usd": …}` under `NEXUS_FUEL_STATE` — the spend therefore sat
  on `$0.00` forever, real spending included. It now comes from the engine's own
  `fuel_exhausted_info()`. Guards: `tests/test_fuel_station_status.py` and
  `tests/test_fuel_spend_display.py` (station order, an **empty** key opening nothing, a ledger
  in the engine's format reaching the tile), both red before their fix.

**Wachen:** seven new guards across four files. Each is red without its fix (verified by
reinstalling the previous file and re-running) — the search route, the fail-loud error, the
two fault names, the provider precedence, the full-scroll requirement, the cross-section
itself, and the colour coverage. Built in `~/nexus-memory-build`, taken into the main tree
only after the proof.

## [0.22.23] — 2026-10-11

### Fixed — the plugin reaches its own venv for every dependency again

- **The agent's memory could be unreachable while every surface looked healthy.** Since the
  second review round (06.10.) the plugin's own venv was attached inside the qdrant repair
  branch only — and there with `sys.path.append`, which does *not* process a venv's `.pth`
  files. On a host that **has** `qdrant_client` but **not** a local embedding backend
  (Hermes' own plugin environment), that branch never ran. The embedding provider then fell
  fail-closed, every recall returned an empty list, and an empty list is indistinguishable
  from an empty memory. The health endpoint stayed green the whole time.
- **The venv is attached at module import now, for every dependency**, via
  `site.addsitedir` so the editable-installed engine package is visible too.
- **Ordering is preserved deliberately.** A dependency the package manager installed still
  wins — but only while the host can serve the embedding path at all. When it cannot, taking
  the venv as a *mixture* is worse than taking it whole: the model would come from the venv
  while its siblings still came from the host, and a sibling that is *present but
  incompatible* is the worst case of all (the host carried `huggingface_hub` 1.24.0 —
  importable, but too old for `sentence-transformers` 6.1.0, `cannot import name
  'ResolvedRevision'`). In that case the venv goes first. The review's concern — no
  shadowing of a managed install — is unchanged for every healthy host.
- **Four tests pin it** (`tests/test_plugin_venv_import_path.py`): the venv goes first when
  the host cannot embed, is only appended when it can, stays idempotent and fail-open, and
  `addsitedir` (not a bare append) is what attaches it.

Verified end to end in the real agent interpreter: no local backend in the host before,
`Qwen/Qwen3-Embedding-0.6B` (1024d) available and a live recall returning hits after, with
both agent self-reports back to `ok: true`.

## [0.22.22] — 2026-10-10

### Changed — one embedding path, one pinned engine revision

- **Every plugin embeds through the local service.** The Claude Code and OpenClaw plugins
  each carried their own provider list: a machine without Ollama had no way in, and a
  machine with a cloud key in its environment could leave the machine without anyone
  choosing that. Both now ask the engine (`POST /embed` on the local serve daemon), so one
  model and one vector space serve a collection — the same vectors the engine writes.
  (The OpenClaw manifest needed the provider name in its config schema for this; it was
  missing there while the code already had it.)
- **The engine revision is one full commit sha, named in both places.** `plugins/memory/nexus/pyproject.toml`
  (`[tool.uv.sources]`) and the repair path in `plugins/memory/nexus/__init__.py` pinned a
  commit the catalog entry did not describe, so a catalog install received an engine that
  did not carry the promise the entry made. Both now name the same full 40-character sha
  (`f5d738676f5f69c9d6a36d01ef3ea124404c3a1a`) — not the tag `v0.22.22` that points at it: a
  tag can be moved, and a moved tag would silently change the engine for every new install
  and every repair run without another review.
- **A guard test closes the chain** (`tests/test_pin_chain_is_closed.py`): the two refs must
  agree, the ref must be a full 40-character commit sha that resolves, the advertised
  versions must match the tag at that commit, and `git diff A B -- src nexus pyproject.toml`
  must be empty — the commit a re-pin carries changes no engine file over the revision the
  refs name. That last check is the one that was missing: the tree a catalog install
  actually receives was never asked what it installs, which is why every content test stayed
  green through three review rounds.

### Fixed

- **Three silent exits of the entity extraction are counted and named now.** The
  extractor prefers an LLM call and falls back to a heuristic that yields **zero
  relationships**. Between 01.10. and 11.10.2026 it ran 313 times without anyone
  noticing (counted on this machine in `~/.hermes/logs/gateway.error.log`: lines
  of 01.10.-11.10.2026 carrying "LLM extraction failed"; 267 of those are the
  empty-answer error described next, the remaining 46 other causes): on the OpenAI-compatible path (`/v1`) the field
  `think: false` is not passed through, so a reasoning model spent the whole
  token budget in its hidden `reasoning` field and returned an empty answer —
  `json.loads("")` → "Expecting value: line 1 column 1 (char 0)". The only trace
  was one warning line. Now the local endpoint (port 11434) is called through
  `/api/chat`, where `think: false` works (measured on a real 1.385-character
  memory text: 1,9 s and 0 reasoning tokens instead of 15,5 s and 6531 completion
  tokens, answer complete with 7 entities and 6 relationships), and every
  fallback — empty answer, unreachable endpoint, missing model — increments a
  counter and stores its reason verbatim under
  `~/.hermes/state/entity-extractor-health.json`, so a watch can see it instead
  of guessing from a log nobody reads. `tests/test_entity_extraction_path.py`
  pins the routing and all three exits.
- **The legacy `__version__` fallback lagged one release behind.** `nexus/__init__.py`
  reads the installed package version and keeps a literal for a bare checkout —
  that literal still said 0.22.21 while the manifests said 0.22.22, which
  `test_review_findings_20261010` caught on main. It now reads 0.22.22.
- **The OpenClaw default-provider guard followed a function that had moved.**
  `test_embed_default_is_local` still looked for `detectProvider()` in
  `lib/embedder.ts`; the environment read was moved to `lib/embedding-env.ts` on
  10.10.2026 (a static scanner read "environment beside network send" as
  credential exfiltration) and the function is now `localEmbeddingProvider()`,
  called from `lib/config.ts`. The guard was updated to the new place and keeps
  the same promise: no configuration → the local Nexus service, never null and
  never a cloud provider.
- **Three docstrings still described an Ollama fallback that no longer exists.**
  `_detect_auto()` chooses HuggingFace as the single automatic path; the engine's
  module header, the `EmbeddingProvider` docstring and the Hermes plugin's
  `_Embedder` docstring still said "HF default, then Ollama", which is what the
  code did up to v0.22.20. All three now name the one automatic path, so none of
  them can be read as a promise the code breaks — the plugin docstring sat in the
  exact lines a catalog review pointed at.
- **The first-use download was stated as about 600 MB; it is about 1.2 GB.**
  Measured against the Hugging Face file list for `Qwen/Qwen3-Embedding-0.6B`
  (`model.safetensors` 1191.6 MB, 1207.5 MB in total) and against the local
  cache. The README, the `embeddings.py` comment and a test comment now carry
  the measured figure. The old number matched the quantised Ollama model
  (639 MB), not the Hugging Face download.

## [0.22.21] — 2026-10-10

### Fixed — five access-level gaps found by the external review of v0.22.20

Every one of these reads a missing or unknown level as the **most** restrictive one, to
match what the MCP server's gate has always done. A value that is absent must never look
like permission.

- **The graph walk did not check access at all.** `_graph_boost` filtered neighbours by
  `lifecycle_status` only, so a private memory reached the prefetch as a graph-boosted
  hit — the one place in the recall path with no gate. It now filters through the same
  `_visible_levels()` boundary everything else uses.
- **`guardrails._extract_protection_rules` read a missing level as `"public"`** and
  attached it to protection rules whose text is echoed back to callers. A rule built
  from a private conversation could therefore be handed out as public. Now
  `_level_or_private()`, matching `consolidation.py` exactly (case-normalised, unknown
  values fail closed).
- **Two graph-boost labels claimed `"public"`** (`plugins/memory/nexus`, and
  `integrations/hermes-plugin`). Nothing is stored by them, but the label stated the
  opposite of what the gate does with a missing level. Both now read `"private"`.
- **`integrations/hermes-plugin/__init__.py`** — an older sibling copy that ships in the
  repository, with its own `pt_payload.get("access_level", "public")` in the graph walk.
  Fixed the same way, so the two copies cannot drift apart again.

### Tests

- `tests/test_zugriff_review_funde.py` — pins each of the five: the guardrail fallback
  (including `"PUBLIC"` and `"admin"`), the graph walk's access filter, the visibility
  boundary itself, and that no write path sets a literal `"public"`.
- Counter-test: removing the graph filter turns the file red.
- Full suite: **2276 passed, 2 skipped**.

Versions: engine 0.22.21, Claude Code plugin 1.2.7, OpenClaw plugin 1.21.9.

## [0.22.20] — 2026-10-10

### Changed — conversation history is private

- **Conversation history was stored as `public`.** `sync_turn` wrote every turn with
  `access_level: "public"`, so any agent ever attached to this memory could read it —
  including one connected tomorrow. "Public" here does not mean "on the internet"; it
  means the lowest of the three visibility levels. What actually sat in those entries
  was measured: of 1,433 public conversation entries carrying text, 641 mention family,
  507 an address or postal code, 241 tax, invoice or bank matters, 13 concrete amounts.

  Tightening later is always possible, per agent, in one line. Un-ringing a bell is not.
  New turns are written as `private`; `NexusAccess` narrowing for a future agent is a
  deliberate act rather than a default nobody chose.

- **Existing entries moved to `private`**: 2,943 of them — every `session` entry plus
  every fact matching a sensitive pattern (tax, IBAN, address, amount, family). A full
  backup was taken first (`~/.hermes/backups/nexus-qwen-vor-privat-*.jsonl`,
  38,952 points, 49.5 MB). Afterwards: 0 entries with sensitive content remain `public`.

  `agents.json` is unchanged — hermes and openclaw stay `private`, claude-code stays
  `trusted`. Claude Code no longer sees private conversation (decision of 2026-10-09).

### Fixed — a failed layout probe poisoned the whole process

- `scope_auto.vector_field` cached a **failed** probe in `_LAYOUT_CACHE`. One transient
  Qdrant hiccup was therefore permanent: every later write resolved to `None` (the
  anonymous shape), a collection with a named vector rejected it with
  `400 Not existing vector name`, and the memory was never stored — silently. Only a
  successful read is cached now; a failure is reported and retried on the next call.

  This also explained a test file that failed in one order and passed in another: the
  poisoned cache leaked from one test into the next.

### Tests

- `tests/test_zugriffsstufen.py` — the access-level contract, measured against the real
  requests the code builds: rank order `public < trusted < private`, claude-code resolves
  to `trusted`, three fallback paths land on `public`, a hand-edited `"admin"` in the
  registry is refused, and every registered agent reads back as its recorded level.
  Each check has a counter-test that turns it red.
- `tests/test_layout_cache_ueberlebt_aussetzer.py` — a failed probe is not cached, the
  next call recovers, a successful probe still is cached.
- Full suite: **2271 passed, 2 skipped**.

Versions: engine 0.22.20, Claude Code plugin 1.2.7.

## [0.22.19] — 2026-10-10

### Fixed

- **Two more silent failures in the Claude Code write path.** The POST→PUT repair in
  0.22.18 covered only the first of three layers. Both remaining ones are measured:

  - `store_memory` wrote without `?wait=true`. Qdrant answers `200 acknowledged` and
    applies the write in the background, so a **rejected** write was reported as
    success: the collection stayed at zero points while the hook said "stored".
    A write now waits for the result.
  - `_point_vector` fell back to the **flat** vector list whenever `import scope_auto`
    failed. Hooks start from arbitrary working directories, so that import fails
    routinely — and a flat list is exactly the shape a collection with a named vector
    rejects (`400 Not existing vector name`). The sibling module is now loaded by its
    own file path.

- **A rejected write is no longer reported as success.** With `wait=true` the real
  status arrives, and the existing handler now reports it instead of swallowing it.

- **Error paths that used to be silent now speak.** The same "swallow and pretend"
  pattern sat on the read side too, where nothing had noticed it yet:

  - `auto_recall._vector_body` and `session_start._vector_body` returned the wrong
    vector shape on a failed import, which the caller turned into an empty result —
    memory simply stopped appearing, with no explanation anywhere.
  - The transcript reader swallowed every exception, so captures stopped happening
    without a word.
  - The trust-level resolver degraded to `public` in silence — in all three copies.
  - Seven `logging.info(...)` calls in the hooks were **silent in practice**: without a
    handler configured (and hooks configure none) `logging.info` writes nowhere at all.
    They looked like reporting and were not. They now print to stderr.
  - `resolveLayout` in the OpenClaw client kept a failed layout probe to itself; every
    later write then sent the wrong vector shape with no warning.

  Each now writes the cause to stderr. Reporting is a design requirement, not a
  courtesy: an agent that cannot see its own failures cannot be trusted.

### Fixed — OpenClaw plugin

- Its `upsert` sent `PUT /points` **without `wait=true`** in both the first attempt and
  the retry, so a rejected write was acknowledged with 200 and lost in the background —
  the same defect the Claude Code plugin had. It now waits for the result.
  (`lib/qdrant-client.ts`)

### Tests

- `tests/test_claude_code_write_path.py` — now measures behaviour instead of text.
  Earlier versions matched source patterns and were bypassable five different ways
  (address from a function call, annotated constant, string concatenation, import
  alias, `getattr`); the file also poisoned its own environment by interrupting
  `scope_auto` mid-call and caching a wrong layout. It now intercepts the requests
  the code actually builds, restores every cache it touches, reads through the plugin's
  own search path rather than the Qdrant client, and asserts the write carries
  `wait=true` — polling in the read-back tests could not catch that one.
- `plugins/openclaw/test-schreibfehler-meldet-sich.mjs` — four checks that a rejected
  write becomes visible (wait parameter present, `upsert` throws on a bad status, a
  failed layout probe warns, nothing is printed to stdout). Both counter-tests bite:
  removing `wait=true` turns it red, silencing the warning turns it red.
- Full suite: **2263 passed, 2 skipped** (Python); OpenClaw 32 green, `tsc` clean.

Versions: engine 0.22.19, Claude Code plugin 1.2.6, OpenClaw plugin 1.21.9.

## [0.22.18] — 2026-10-10

### Fixed

- **The Claude Code plugin could read from memory but never write to it.** Its
  `store_memory` built a `urllib.request.Request` with no `method=`, which urllib sends
  as POST. Qdrant wants **PUT** for point creation and rejects POST on that endpoint
  with `400 Format error in JSON body: missing field 'ids'`. Measured against the
  running Qdrant 1.17: `POST` → 400, `PUT` → 200.

  Every capture therefore failed, the surrounding `except` swallowed the error, and the
  hook reported only to stderr — invisible in normal operation. Nothing looked wrong:
  recall kept working, because the read paths (`/points/search`, `/points/scroll`, and
  `/points` with an `ids` body) do want POST and were correct all along. Only the write
  path was broken, and only for this plugin: OpenClaw uses its own Qdrant client and
  Hermes uses the official one.

  Read paths are deliberately left as POST — switching them to PUT would break search
  and scroll.

- `tests/test_claude_code_write_path.py` — exercises the real path (write, read back with
  the official client, clean up) and pins the method on both sides: PUT on the write
  call, POST on the read calls. Red without the fix, green with it. It also asserts the
  written point carries `access_level: trusted`, since Claude Code must never see or
  create private memories (decision of 2026-09-02).

Versions: engine 0.22.18, Claude Code plugin 1.2.5, OpenClaw plugin 1.21.8.

## [0.22.17] — 2026-10-10

### Fixed

- **The memory provider could not embed at all when Hermes called it — remember
  and recall both failed.** The provider's ``_Embedder.embed`` ran a bare
  ``asyncio.run(coro)``. Hermes invokes a memory provider **from inside its own
  running event loop**, where ``asyncio.run`` refuses outright:

  ```
  RuntimeError: asyncio.run() cannot be called from a running event loop
  ```

  Every ``nexus_remember`` and ``nexus_recall`` returned an error. The database was
  healthy throughout — the defect was in the call shape, not the data. Users saw a
  silent memory, not a crash.

  The regression arrived with the Wave-38 cleanup (``5c2a50d``), which replaced a
  hand-rolled ``new_event_loop``/``run_until_complete`` pair with ``asyncio.run``.
  That earlier shape did **not** work either — running a second loop inside an
  already-running one raises ``Cannot run the event loop while another loop is
  running``. It differed only in the error message, so this is a real repair, not a
  restoration of working code. Both shapes were exercised from a running loop before
  the conclusion was drawn.

  ``embed`` now detects a running loop and, when one is present, runs the coroutine
  on its own loop in a separate thread. The plain, loop-free path keeps
  ``asyncio.run`` unchanged. A separate thread is used rather than
  ``run_coroutine_threadsafe``: the latter would run the coroutine on the **caller's**
  loop and interleave it with the caller's own tasks, which a synchronous-looking
  embed must not do.

  Why it stayed hidden: a test run has no running loop, so ``asyncio.run`` works there.
  The unit suite was green while the real path was broken.

- ``tests/test_embedder_event_loop.py`` — calls the embedder once without and once
  from inside a running loop, so this cannot regress unnoticed again. Red without the
  fix, green with it.

Versions: engine 0.22.17, Claude Code plugin 1.2.4, OpenClaw plugin 1.21.8.

## [0.22.16] — 2026-10-10

### Fixed

- **Four engine fixes that existed only in the build copy never reached the shipped tree.**
  All four share one failure mode: a named collection answers ``400 Not existing vector
  name``, the surrounding ``except`` swallowed it, and the feature went quiet instead of
  loud. They were verified against a live Qdrant (1.17) before and after.

  - ``nexus/discovery/matcher.py`` and ``nexus/confidence.py`` set the vector name as a
    top-level ``using`` field. That keyword belongs to the qdrant-client call
    (``query_points``); this endpoint expects the name **inside** the vector object.
    A REST body carrying ``using`` is rejected outright.
  - ``nexus/retrieval/__init__.py`` had the same shape in its raw search path.
  - the dimension guard in the same module read ``vectors["size"]``. A named collection
    nests the size one level deeper (``{"<fingerprint>": {"size": N}}``), so the lookup
    raised a ``KeyError`` that the handler swallowed — the guard was off exactly for the
    collections Regel B introduced.

  Client-side ``query_points`` callers (``retrieval_watch``, ``consolidation``, ``dreaming``,
  ``mcp_server``) keep ``using``: that is the correct spelling there. Only the raw REST
  bodies were wrong.

### Added

- ``scope_auto.point_vector_body()`` — the write-side twin of ``search_vector_body()``,
  and ``auto_capture`` now uses it. Writing a bare list into a named collection failed the
  same silent way, so captures vanished without a word.

Versions: engine 0.22.16, Claude Code plugin 1.2.4, OpenClaw plugin 1.21.8.

## [0.22.15] — 2026-10-10

### Fixed

- **The trust heuristic became looser, not just more generic.** Replacing the
  deployment names in the tier keywords with the generic words `agent`/`user`
  changed the matching behaviour: the names almost never matched by accident as
  substrings, the generic words do. `"user_agent"` or `"TODO agent Remember"`
  lifted arbitrary text to tier1 — boost 1.2 and a green trust label — and so
  diluted the poisoning defence. The matcher now requires whole words
  (`\b`-delimited), which is stricter than the state before the privacy sweep.
  (`nexus/retrieval/__init__.py`)

- **A functional marker check depended on one LaunchAgent filename.** The sweep
  renamed a hardcoded plist path inside `_external_backup_configured()`; a fixed
  name makes the check silently miss installations whose agent is named
  differently (and would have nudged the user towards setting up a backup chain
  that already exists). It now globs `*backup*.plist`.
  (`integrations/hermes-plugin/__init__.py`)

- **A widened separator class would have eaten legitimate answers.** The leak
  marker was briefly extended to `[\s,.;:—–-]`, which also swallowed openings like
  `So; deshalb …` / `Wait: hier …`. Only the impossible `\b` was the defect, not
  the class; the pattern is back to `[.,\s]` and the accepted floor is frozen in
  the tests. (`plugins/openclaw/hooks/thought-filter.ts`)

- **A set-but-empty `NEXUS_OBSIDIAN_WIKI` fell through silently** —
  `expanduser("")` resolves to the working directory, so the orphan check simply
  stopped looking. It now falls back to the default explicitly and logs when the
  directory is missing. (`nexus/health/__init__.py`)

- **Two version strings lagged behind.** The README footer still said `v0.22.6`
  and the `__version__` fallback `0.22.4`. A guard test now compares both against
  `pyproject.toml`, so a forgotten site fails the suite.

### Added

- `tests/test_review_findings_20261010.py` — regression guards for the above,
  each backed by a sharp proof (restore the old behaviour and the test goes red).

Versions: engine 0.22.15, Claude Code plugin 1.2.4, OpenClaw plugin 1.21.7.

## [0.22.14] — 2026-10-10

### Fixed

- **A leak marker that could never fire.** In the OpenClaw thought filter the
  pattern read `/^(so,|wait|hmm)\b[.,\s]/i`. The `\b` sat between a non-word
  character (the comma) and the character class — a word boundary cannot exist
  there by definition, so the `so,` branch was dead from the day it was written,
  even though the comment above it named exactly that form as the marker. A
  reasoning leak opening with *"So, hier ist meine Antwort"* went through
  unfiltered; `wait`/`hmm` only worked because they end in word characters.
  The marker now matches any real separator. (`plugins/openclaw/hooks/thought-filter.ts`)

- **The addressee group was hardcoded to one deployment's names.** Every other
  installation carried dead branches, and anyone reading the file could tell who
  built it. The group is now generic and extendable through `NEXUS_AGENT_NAMES`
  (comma-separated), so a deployment that wants its own names recognised sets the
  variable while an untouched install stays generic. Both markers are covered.

### Changed

- **No deployment, agent or owner names in shipped code or docs.** Names left over
  from the development history were replaced with neutral wording across `src/`,
  `plugins/`, `docs/`, `benches/` and `dashboard/`; design decisions keep their
  dates and are attributed to "the maintainer" where that carries meaning. The
  public GitHub handle and the entry documenting the earlier pseudonymisation stay
  as they are. Two guard tests that forbade hardcoded home paths previously carried
  the very path they forbade; they now match any `/Users/<name>` — stricter than
  before.

- `NEXUS_OBSIDIAN_WIKI` configures the shared Obsidian wiki path
  (default `~/ObsidianVault/NexusVault/wiki/entities`) instead of a fixed
  personal path. (`nexus/health/__init__.py`)

Versions: engine 0.22.14, Claude Code plugin 1.2.4, OpenClaw plugin 1.21.6.

## [Unreleased]

### Added

- **The catalog entry pins the engine that is actually shipped.** `plugin-catalog/nexus.yaml`
  still pointed at `ec3a21c`, which predates the local-first default, so a catalog install
  received the cloud-first auto-detection while the README and the ClawHub package described
  the fixed behaviour. The entry now pins the same commit ClawHub links as its source, and the
  repository guard test asserts the local-first default *at the pin* — a lagging pin fails the
  suite instead of passing silently.

### Fixed

- **The archive pass had silently stopped working.** `archive_forgetting`
  called `client.scroll(..., with_vector=True)`; the real qdrant client knows
  only `with_vectors`, so every pass raised *Unknown arguments:
  ['with_vector']* and the fail-open branch swallowed it — the archive
  reported "nothing stale" while stale session points piled up (175 in a
  live dry run, 314 failed passes in the serve error log). No test caught it
  because the test double accepted any keyword argument; the double now
  rejects unknown kwargs like the real client, and a strict-kwarg test locks
  it. On a **named** vector space (Regel B) qdrant returns `{name: [...]}`
  instead of `[...]`; the backup now stores the plain list, and a point with
  several vectors is skipped rather than guessed.

- **Local embeddings are the default; the cloud is now an explicit choice.**
  Auto-detection used to try cloud providers first, so a machine that merely had
  an API key exported (a key belonging to some other tool) silently sent turn
  text to a cloud service. Detection is now local-first — Ollama, then the local
  HuggingFace route — and the cloud level is only reachable through an explicit
  choice (`NEXUS_EMBEDDING_PROVIDER=voyage`, or `auto` plus
  `NEXUS_ALLOWED_CLOUD_FALLBACK=1`). Without either, a cloud client is not even
  constructed. This closes the finding a public static scanner reported twice
  (`suspicious.env_credential_access`: an environment key read *and* sent over
  the network). (`src/nexus_memory/embeddings.py`, `plugins/openclaw/lib/embedder.ts`)

- **`NEXUS_HF_BGE3=0` only drops bge-m3 from the candidate list.** It used to
  switch the whole local HuggingFace route off, which — combined with the
  cloud-first order — pushed the install toward the cloud. The local route now
  continues with the remaining candidates.

- **The collection-drift guard now holds along the full detection chain.** A
  recorded collection model that can no longer be loaded raises
  `CollectionModelUnavailable`; the local levels re-raise it instead of letting a
  broad `except Exception` swallow it, so a collection can never be written with
  a different model's vectors. If the embedding library is missing while a model
  is recorded, the same error is raised instead of falling through.

- **Coldstart catch-up can no longer wedge the prefetch gate.** If the catch-up
  thread ran past its deadline while still holding the single-flight gate, every
  later prefetch bounced off a lock only that orphan could release — memory went
  quietly dark for the rest of the process. The timeout now installs a fresh gate
  and reports it at WARNING; a late-finishing thread publishes (or clears) only
  while it still holds the current gate, so it cannot overwrite a fresher result.
  (`plugins/memory/nexus/__init__.py`)

- **A missing embedding backend is reported, not raised.** On the default path
  the provider stays unavailable and the reason is logged at ERROR; an explicit
  choice that cannot be served still fails closed with a clear message.

## [0.22.12] - 2026-10-04

### Fixed

- **The update check is now opt-in, off by default.** Hermes' plugin catalog forbids
  self-updating behaviour in a listed plugin (admission rule 3): the exact SHA pin *is* the
  trust model, so an installed copy must not reach out on its own and announce that a
  different revision is the one to want. `NEXUS_UPDATE_CHECK=1` keeps the notification for the
  standalone/MCP distribution; the catalog build does not set it. Nothing was ever downloaded
  or replaced — the only effect is one informational line in the system prompt.

- **A personal LaunchAgent filename left the shipped code.** The external-backup detector
  checked two hard-coded plist paths belonging to one deployment. It now scans the
  LaunchAgents directory for any plist that names the backups path, which is what the check
  was always trying to express.

- **The `salience` parameter of `nexus_remember` was described in German.** The description is
  user-visible in every client that renders tool schemas; it now reads in English like the rest
  of the schema.

## [0.22.11] - 2026-10-04

### Fixed

- **A catalog install would have shipped a provider without its engine.** The Hermes plugin
  directory (`plugins/memory/nexus/`) held only `__init__.py`, `plugin.yaml` and `README.md`,
  while the code it imports 32 times over — `nexus_memory.embeddings`, `nexus_memory.guardrails`,
  `nexus_graph`, and the rest — lives in this repository's `src/` and `nexus/` packages and is
  **not** part of that subdirectory. A catalog entry installs the pinned subdirectory alone, so a
  new user would have received a provider that loads and then reports "the embedding provider is
  not importable" forever. `hermes plugins validate` stayed green through all of this: it probes
  in the host process, where the engine happens to be present. Found by installing the pinned
  subdirectory in isolation and probing it.

- **The repair command pointed at a stranger's package.** With no repository above it, the plugin
  fell back to `uv pip install nexus-memory` — an index lookup for a name that belongs on PyPI to
  an unrelated project (`shivamtyagi18/smriti-memcore`, versions 0.1.x/1.0.x). A user following
  our own error message would have installed someone else's code. The fallback now installs
  `nexus-memory @ git+https://github.com/Neboy72/nexus-memory.git@<commit>` — our repository at a
  pinned commit, resolved by source rather than by name.

### Added

- **`plugins/memory/nexus/pyproject.toml`** declares the plugin's Python runtime, so the plugin
  manager installs the engine alongside the plugin. Its project name is `hermes-nexus-provider`,
  deliberately not `nexus-memory`: the dependency `nexus-memory` resolves through
  `[tool.uv.sources]` to this repository at a pinned commit, so a bare index lookup can never pull
  the unrelated PyPI project. `package = false` marks the directory as a declarative dependency
  holder — nothing is built or packaged from it.

- **The wheel no longer installs a top-level `plugins` package.** Hermes ships a package of that
  name — `plugins/memory/__init__.py`, its memory-provider discovery (26 KB of logic). This
  repository's `plugins/` directory held a two-line stub, and `[tool.setuptools.packages.find]`
  matched it, so a wheel installed next to Hermes overwrote that file: whichever distribution
  landed last won the name, and `import plugins.memory` could resolve to the stub instead of the
  discovery. Nothing in this repository imports `plugins` as an installed package — the shipped
  Hermes plugin is installed from this repository's git subdirectory, and the tests reach the
  directory by path — so the entry is gone. Verified by installing this wheel beside a package
  owning `plugins/` and confirming the owner's file and marker survive.

## [0.22.10] - 2026-10-04

### Fixed

- **The plugin manifest declared the wrong hooks, which would have failed catalog admission.**
  `on_session_end` and `on_memory_write` are `MemoryProvider` *methods*, not plugin hooks — they
  belong to the provider contract in `agent/memory_provider.py`, not to Hermes' `VALID_HOOKS`.
  Declaring them made `hermes plugins validate` report an unknown hook. The manifest now declares
  only the one real plugin hook, `transform_llm_output` (the chat-visible self-report). Kept the
  `provides_hooks` field name so the file matches every other plugin in the catalog.

- **The guardrail tool description tripped the security scanner.** Its example command read as a
  literal recursive home delete, so `hermes plugins validate` flagged `destructive_home_rm` at
  critical severity on a string that was never executable — the schema only *describes* the kind of
  command to check. Reworded to name the operation instead of showing the raw command; the tool is
  unchanged in behaviour, the false positive is gone.

Validation now passes all 15 checks (`hermes plugins validate`), which is the CI gate for catalog
admission.

## [0.22.9] - 2026-10-04

### Fixed

- **A memory outage could be invisible to every check and survive `/new` — now it heals itself.**
  If the agent process started *before* the dependencies were installed there, the provider's
  health verdict ("package missing") was cached for the whole life of the process. Every later
  repair on disk was invisible to it: the service kept running blind, a fresh session did not
  help, and only a full process restart recovered it (observed 2026-10-04: three hours
  unnoticed, while `memory status` and the watchdog both reported "available ✓" because they
  each ran in a *fresh* process that did see the package). The missing-dependency verdict now
  expires after `NEXUS_DEP_MISSING_TTL_SEC` (default 60 s) and the import is retried, so the
  running process recovers on its own.

- **The repair command no longer targets the host interpreter.** It pointed at
  `sys.executable` — Hermes' own process, which under a pipx or system install is
  foreign-managed and read-only. A user following that advice could have damaged their Hermes
  install. The command now builds the plugin's *own* venv (`<data-dir>/plugin-venv`) and pins
  the running Python minor version, because a venv of a different minor version loads happily
  and then breaks the C extensions (`sentence-transformers`/`torch`/`numpy`).

### Added

- **The plugin carries its own venv import path.** `_try_import_qdrant()` appends
  `<data-dir>/plugin-venv/.../site-packages` — matching the running version only — so Nexus
  runs independently of whatever interpreter Hermes uses, and nothing needs to be written into
  the host environment.

- **The installer provisions that venv.** `scripts/install_hermes_plugin.sh` now creates the
  plugin venv and installs the runtime dependencies into it. This closes a silent-failure
  path: Nexus is delivered as a *memory provider*, and only Hermes' plugin manager reads
  `pip_dependencies` from `plugin.yaml` — `hermes memory setup` and the provider loader have
  no code for it, so a user who only followed the symlink step got no packages and an agent
  that quietly ran without memory.

- **`scripts`-adjacent watchdog probe: the *live* process, not a fresh one.** A new
  `nexus-live-check.py` reports `LIVE_BROKEN` when the running gateway logged "provider
  reports unavailable" after its own start with no successful activation afterwards. Every
  other check starts a new process and is blind to exactly this state.

## [0.22.8] - 2026-10-03

### Fixed

- **The documented prefetch budget was three revisions out of date.** `AGENTS.md` still
  listed `NEXUS_PREFETCH_CHARS` as 1200; the code has used 2400 since v0.13.2 (2026-08-30,
  "prefetch capacity doubled"), so anyone tuning recall from the docs was working from a
  number that no longer existed. The table now carries the live default, the raise history,
  and the reason it is capped at all: the injected block is replayed with every later turn
  of the session, so on per-token billing its cost recurs on each request.

### Added

- **README: "Tuning the prefetch budget" guidance under Auto-Recall.** Documents what the
  cap actually costs, when raising it helps and when it does not (missing fact ⇒ fix the
  stored wording, not the budget), and that prefetch is a head start rather than a
  guarantee — explicit `recall`, which is never budget-capped, stays the reliable path for
  factual questions about the user's own life.

## [0.22.7] - 2026-10-02

### Fixed

- **A corrected memory no longer stays findable by its refuted wording.** The update path
  rewrote the fact in the store but never touched the keyword (BM25) index, so the entry kept
  its OLD text there: `nexus_update` returned `status: updated`, the store was right, and a
  search for the very wording that had just been refuted still returned the fact at the top.
  The remember path had been updating the index all along, which is why the gap only showed up
  on corrections. Found on 2026-10-02 while auditing a live correction (the index was rebuilt
  by hand to confirm: 31 978 entries, the corrected id carried its new text).

- **The index update is now idempotent.** `update_index()` appended ids unconditionally, so
  calling it for an already-indexed id duplicated that entry — which is also why the update
  path could not simply reuse the existing call. Existing ids are now skipped, and a new
  `replace_indexed()` swaps the stored text of an existing id in place (BM25 corpus and the
  raw-text sidecar are both rewritten, then persisted).

- **Covered by tests.** `tests/test_bm25_reindex_on_update.py` pins the behaviour: an edited
  memory stops matching its old wording, an unknown id is ignored, and adding an
  already-indexed id is refused instead of duplicated.

## [0.22.6] - 2026-09-30

### Fixed

- **A single hiccup at gateway start no longer raises a false "memory is offline"
  alarm (OpenClaw plugin).** The reachability probe behind the self-report made *one*
  attempt with a hard 5 s deadline and ran exactly once, at plugin start. A build,
  index or backup spike — or a cold IPv4/IPv6 resolution — in that one second was
  enough to publish `ok:false`, which puts "Nexus Memory self-check: NOT WORKING" into
  the agent's system prompt and into the chat. Reported on 2026-09-30 while Qdrant was
  in fact healthy (13 days uptime, five consecutive probes under 1 ms, 33813 points,
  and a live agent turn that answered from its memory). The probe now allows 15 s per
  attempt and makes exactly one retry, 750 ms apart; `ok:false` — and therefore the
  warning — only appears when **all** attempts fail, and a final deadline failure names
  the attempt count (`... is unreachable (…; 2 attempts)`). A real outage still fails
  both attempts and still warns; the healthy path still makes exactly one request.
  - New `plugins/openclaw/test-self-check-probe.mjs` (5 behaviour tests: the
    false-alarm case, the real-outage case, the budget constants, the single-request
    happy path, and an HTTP error status). Falsification-verified against the previous
    code: a one-off 6 s stall produced the exact production message before the change
    and stays silent after it, while a refused connection still reports.

## [0.22.5] - 2026-09-30

### Fixed

- **The prefetch thread was not joined on shutdown.** `shutdown()` joined three
  background threads and then closed the Qdrant client, but the prefetch thread
  spawned by `queue_prefetch()` was not among them. If it was still in flight,
  it dereferenced the already-closed (or `None`) client and failed with
  `'NoneType' object has no attribute 'query_points'` — twelve times on
  2026-09-30 alone. The affected session silently received an empty memory block
  for that turn, and the warning noise helped hide the real failures. The fix
  tracks the thread under its own lock (with a spawn guard against
  `_shutting_down`), joins it with a bounded grace period (`_PREFETCH_JOIN_TIMEOUT`,
  2 s) *before* the client is closed, logs an **ERROR** naming the grace period
  when a prefetch outlives it (the join result is no longer ignored — an ignored
  `join()` only made the race rarer, not impossible), aborts at checkpoints inside
  the prefetch thread, and resets a stale prefetch result on those aborts.
- **A genuine failure during shutdown is no longer silenced.** The error handler
  used to branch on the `_shutting_down` flag alone, so any real error landing in
  the shutdown window (which lasts seconds: 2 s prefetch grace plus the write,
  backup and update joins) was downgraded to DEBUG and skipped the health
  self-check — exactly the silent-failure class this fix exists to remove. It now
  branches on the **cause**: only an error explained by the closed or absent client
  is shutdown noise; every other error keeps its WARNING (naming the shutdown
  context) and its health re-probe.
  - **11 new regression tests** (`tests/test_prefetch_shutdown_race.py`): the
    survivor path, a three-variant stress run (fast / survivor / embed-blocked),
    checkpoint aborts, the cause predicate, the self-join guard and the positive
    path proving normal prefetch still returns memories. Verified by
    falsification: against the previous code the decisive tests fail, against the
    fix they pass.
  - Independently reviewed by a second model across two rounds; the first round
    found the ignored-join defect (reproduced, then fixed), the second found no
    P0/P1.

## [0.22.4] - 2026-09-30

### Fixed

- **The OpenClaw thought filter never worked.** The `message_sending` hook
  returned its verdict in a `{message: …}` field, but the host reads only
  `{content}` (documented contract, `PluginHookMessageSendingResult`, confirmed
  against the host bundle). Every cleaning step and every drop of pure reasoning
  text was silently discarded — the GLM chain-of-thought leak the filter exists
  for would have gone straight to the chat, while everyone assumed it was
  guarded. The handler now returns `undefined` (no opinion), `{content: cleaned}`
  or `{cancel: true}` for pure reasoning, fail-open on its own errors, and the
  detection logic moved into a pure `scanReasoningLeak()` so it is testable
  without a host.
  - **New `test-host-contract.mjs`** pins the host semantics against the real
    bundle, so a field-name drift can no longer pass unnoticed again.
- **Self-check warning could clobber a filtered message.** The new
  `hooks/self-check-warning.ts` appends the "memory is broken" block to outgoing
  messages at most once per session but never onto text that itself carries
  reasoning, and it consumes no throttle slot when it declines. Health check runs
  before the scan, so a healthy backend costs nothing.

### Added

- **A broken memory backend is now visible in the chat, not just in the prompt.**
  The v0.22.2/v0.22.3 self-report reached the model only, so a weak model could
  ignore it and the user still could not tell that memory was off.
  - **Hermes** (`plugins/memory/nexus/__init__.py`): a `transform_llm_output`
    hook appends a short two-line warning (cause masked, repair command) once per
    session, capped at 64 sessions, and returns `None` in every other case so the
    answer path can never break. Registration failures degrade to the existing
    prompt section.
  - **Claude Code** (`plugins/claude-code/scripts/self_check.py`): the
    SessionStart hook now emits a `systemMessage` alongside `additionalContext`,
    bounded to a single line for the tight chat surfaces.
- **11 new tests** (2097 total, up from 2083): `test_hermes_plugin_selfreport.py`
  covers the output hook (warn once per session, bounded cache, fail-open on
  empty text and on probe errors), `test_claude_code_selfreport.py` covers the
  bounded single-line system message.

## [0.22.3] - 2026-09-30

### Added

- **Self-report parity for the other two plugins.** The v0.22.2 self-report was
  Hermes-only, so Claude Code and OpenClaw users could still lose memory in
  silence — exactly the failure the feature exists for. Both now publish the
  same contract (`<data-dir>/agent-selfcheck-<agent-id>.json`:
  `agent_id`, `ok`, `reason`, `fix`, `interpreter`, `plugin_version`, `ts`),
  read by the same server-side watchdog.
  - **Claude Code** (`plugins/claude-code/scripts/self_check.py`): runs as a
    SessionStart hook, probes Qdrant reachability and the embedding provider's
    API key, writes the file atomically, and emits the warning into the session
    context when broken (silent while healthy).
  - **OpenClaw** (`plugins/openclaw/lib/self-check.ts`): probes Qdrant with a
    bounded timeout, publishes the verdict, and feeds the warning into the
    system prompt — repeated on every prompt build until a healthy write
    clears it, so a broken memory cannot go unnoticed mid-session. A failed
    embedder init reports before the registration fails, and Qdrant probe
    results are cached to keep recall latency unaffected.

### Fixed

- **OpenClaw installer built nothing.** `install_openclaw_plugin.sh` (repo and
  plugin copy) never ran `npm run build`, while OpenClaw executes the built
  `dist/index.js` and `dist/` is gitignored — a fresh clone registered a plugin
  whose hooks and tools could not fire. Both installers now build when the
  bundle is missing or older than the sources, with a Node.js 20+ prerequisite
  message and a hard failure instead of a silently empty plugin.
- **Dead repair command.** The OpenClaw self-check named
  `npm install -g @neboy72/nexus-memory@latest` as the fix; that npm package is
  not published (404), so following the advice failed. The hint now points at
  the two causes that actually occur: Qdrant not reachable, or a missing
  embedding key.
- **Version fallback could go stale silently.** The Claude Code self-check's
  literal fallback (used when the manifest is unreadable — i.e. exactly on a
  broken install) had no consistency check. A test now pins fallback ==
  manifest version, so a bump cannot leave the repair path reporting a wrong
  version.

### Added (tooling)

- **`scripts/install_claude_plugin.sh`** — the one-command installer the README
  has advertised since the plugin shipped, but which did not exist. Backs up an
  existing install, copies the plugin to `~/.claude/plugins/nexus-memory`,
  verifies every hook script (including the self-check), and reports whether
  `nexus_memory` is importable for the MCP server plus whether Qdrant answers.

### Tests

- 14 new tests: Claude Code self-report suite (file contract, health probe,
  atomic write, fail-open, warning text, version/manifest consistency,
  installer contract) and the OpenClaw self-check suite (payload shape,
  atomic write, prompt-warning persistence, fail-open). The OpenClaw installer
  tests stub `npm` and run against a throwaway plugin copy, so they exercise
  the build path without a Node toolchain — and prove a failed build aborts
  before anything is installed.
- Full suite: 2083 passed, 3 skipped (local); 2063 passed, 23 skipped in the
  bare Ubuntu CI replica (the difference is optional dependencies only).

## [0.22.2] - 2026-09-30

### Added

- **Hermes plugin self-report.** On every process start the plugin writes
  `<data-dir>/agent-selfcheck-<agent-id>.json` (legacy `agent-selfcheck.json`
  still supported) naming whether its memory provider is working, the reason
  when it is not, and a one-line repair command. Written once per process,
  fail-open.
- **Server-side self-report watchdog (`self_report.py`).** An in-process daemon
  reads those self-checks plus the agent registry and alerts on (a) a fresh
  `ok: false` report and (b) suspicious silence from a `plugin` agent that
  previously used memory (4–14 days quiet). Delivered through
  `NEXUS_ALERT_WEBHOOK_URL` (fallback `NEXUS_WEBHOOK_URL`) and/or
  `NEXUS_ALERT_MACOS=1`; alerts deduped (48 h broken / 168 h silent) via
  `<data-dir>/selfreport-state.json`. Silent with no channel configured;
  kill-switch `NEXUS_SELFREPORT=0`. No new dependencies.
- **Health surfaces.** The `health` MCP tool and `GET /healthz` now include a
  compact `self_report` status (`ok`/`warning`), fail-open.

## [0.22.1] - 2026-09-25

### Fixed

- **Self-kill guard (serve daemon).** `ensure_serve_daemon()` refuses to run
  `install_serve.sh` from inside a launchd-managed daemon: the install script
  bootouts the running job, which killed the daemon (and the running update)
  mid-call. The plist now sets `LAUNCHED_BY_LAUNCHD=1`; the guard returns
  `skipped_self_guard` instead of reinstalling over its own head.
- **Dreaming marker timing.** The seen-sessions marker is committed only
  AFTER a successful pass. An LLM fuel failure mid-pass no longer burns the
  sessions forever (they stay learnable), and dry runs never consume them.
- **Dreaming verdict stage wired.** The Consolidator daemon now passes its
  LLM seam into `dream_once()`, so the ja/nein quality gate actually runs in
  production (was: `llm_fn=None`, raw snippets landed unfiltered).
- **Honest archive report.** `scanned` reports the real number of points in
  the filtered scroll (was: hard-coded BATCH size). Dead `_DELETE_URL`
  constant removed.

### Install script

- Plist/systemd-unit backup before rewrite (`*.bak` rollback path).
- Linux `systemctl` calls armored with `|| true` — SSH without linger no
  longer breaks the install under `set -e`; the is-active check stays the
  verdict.

### Tests

- 6 gap-closing tests: numeric-string ID → int delete path, real backup
  completeness check (non-serializable payload blocks delete), playbook
  write + marker commit, LLM failure keeps sessions learnable, ja/nein
  verdict gate, fact protection with a real fact point present.

## [0.21.0] - 2026-09-23

### Added

- **Standalone wird Standard (serve daemon as OS service).**
  - **`scripts/install_serve.sh` (Block 1).** Installs `nexus-memory serve`
    as an OS service: launchd LaunchAgent on macOS, systemd user unit on
    Linux, Windows scheduled task via `schtasks`. Idempotent — re-running
    rewrites the service file and restarts the service (upgrade path), never
    duplicates or fails on an existing service. Stable service identity
    (`ai.nexus.serve` / `nexus-serve.service` / `NexusServe`), generated
    files match the reference unit (RunAtLoad + KeepAlive, ThrottleInterval
    30, ExitTimeOut 25, logs under `<repo>/logs/`, `NEXUS_SERVE_PORT`
    default 9122). `NEXUS_SERVE_SKIP_LAUNCHD=1` writes the service file
    without any service-control side effect (hermetic test mode — launchctl
    domains are per-uid, not per-HOME).
  - **Setup wizard wiring (Block 2).** `step_complete()` installs the serve
    daemon with **no user question**; the interactive CLI gained a serve
    step before completion; new JSON command `setup --json serve_daemon`.
    A failed install never fails the setup — it degrades to a summary note
    (stdio MCP keeps working).
  - **do_update wiring (Block 3).** After `pip install`, `_do_update()`
    re-asserts the serve daemon via `ensure_serve_daemon()`
    (`asyncio.to_thread`, fail-open: any failure is logged as a warning and
    never fails the update). Covers machines that predate the
    daemon-as-default and re-asserts the service after package changes.
  - **New module `src/nexus_memory/serve_daemon.py`** (stdlib-only imports;
    `setup.py` and `mcp_server.py` import it — no circular import by
    construction). `ensure_serve_daemon()` is idempotent: healthy `/healthz`
    → `already_installed` no-op; all failure modes (missing script, script
    rc != 0, timeout, healthz down) return dicts, never raise.

Beweis-Kranz: full suite 1999 passed / 2 skipped, live end-to-end run of
`install_serve.sh` on the reference macOS host (healthz 200, generated
plist matches the hand-built reference), idempotent re-run green,
`ensure_serve_daemon()` full-path `installed` proven after stopping the
daemon, gitleaks clean.

## [0.20.2] - 2026-09-17

### Security hardening (independent audit round)

- **SSRF guard consolidated + bypass closed (F2a).** A canonical
  `_assert_ssrf_safe()` now gates every outbound sink: `subscribe()`,
  `_post_webhook()` (defense-in-depth at delivery time), and
  `_check_sources()`. The guard resolves hostnames via
  `socket.getaddrinfo`, which covers numeric-form tricks such as
  `http://127.1/` or `http://2130706433/` that `ipaddress.ip_address`
  previously rejected as non-IPs and let through. Proven 9/9 attack
  vectors blocked, 2/2 benign hosts pass.
- **`source_url` fetch guard (F2b).** Provenance HEAD/curl checks now run
  through the same SSRF gate — internal targets report `unreachable`
  instead of leaking reachability to prompt-injectable URLs.
- **Untrusted-content marking (F3).** Auto-recall context blocks
  (Claude-Code hook and Hermes prefetch) now open with an explicit
  data-not-instructions banner, so injected memory content is framed as
  untrusted by construction.
- **Test alignment.** `test_source_blocks_private_ranges` now asserts the
  canonical guard's coverage (getaddrinfo + inet_aton) and that
  `subscribe()` calls it.

Beweis-Kranz: full suite 1945/1946 green (1 pre-existing env-dependent
failure, proven by stash), throwaway-container counter-probe 159/159
green, gitleaks clean.

## [0.20.1] - 2026-09-16

### Fixed

- **Proven prompt-injection bypass closed (security).** A stored
  `</nexus-context/>` (slash before `>`) slipped past the closing-tag
  neutralizer, survived into the recall wrapper and closed it early — the
  injection text plus the disclaimer became free prompt text. The
  neutralizer now strips every close/self-closing variant including the
  slash form. Proven end-to-end with a Node reproduction before and after
  the fix.
- **`nexus.health`: `_logger` used but never defined.** Wave 37 added log
  lines to four contradiction-detection fallback paths without defining the
  logger, so any exception there raised `NameError` and masked the real
  failure (reproduced via the drift demo). A module-level logger restores
  honest error reporting.
- **SessionStart hook fail-soft actually enforced.** The embedding response
  is now shape-validated (a top-level array / `null` data no longer raises
  `TypeError` and aborts the hook); the payload filter loop carries the
  same `isinstance` guard as `main()`.
- **session_scan robustness.** A missing session DB is now a clean error
  instead of a silently created empty database; running sessions stay
  visible (window no longer drops `ended_at IS NULL` rows); an epoch-0
  `ended_at` counts as running; unused columns dropped.
- **num.ts clamp hardening.** Fractional bounds are truncated like values;
  degenerate bounds (non-finite / inverted) fail closed by design and are
  documented.
- **text-preview fast path.** Short-enough strings return before the
  code-point array is materialized.
- **nexus-sica-analyzer honesty.** Failed counts return `None` instead of a
  fabricated `0` (no "all clear" during a Qdrant outage); a failed scroll
  marks `affected_ids` as unavailable; every suggestion branch gates on the
  count having succeeded.
- **Typebox test stub interop.** The test-only stub no longer throws on
  interop keys (`default`, `__esModule`, `toJSON`, `inspect`, …); unknown
  builder typos still throw; error message now English.

### Tests

- Version-consistency test compares calver bounds segment-wise numerically
  (lexicographic comparison falsely failed `2026.9.0` < `2026.10.0`); the
  lockfile peerRange mirror is now a hard assert.
- Evidence: macOS suite 1945 passed / 3 skipped; Ubuntu container replica
  1940 passed / 8 skipped; OpenClaw JS tests green (thought-filter 20/20
  with env provided); smoke ALL PASS + FUNCTIONAL PASS; tsc clean;
  leak-check clean.

[0.20.1]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.20.1

---

## [0.20.0] - 2026-09-16

### Fixed

- **Full OCR review campaign completed: all 513 findings closed.** A second
  reviewer pass over the entire codebase produced 513 findings (15
  critical/high dashboard XSS + injection-class, 258 medium, 240 low).
  Every finding is now closed: fixed, or a documented skip/false-alarm
  with evidence in the tracker. 26 waves, each committed separately with
  a proof-carrying test file (tests/test_waveN_fixes.py).
- **Highlights across the campaign:** dashboard XSS hardening (escapeHtml,
  delegated listeners, no inline handlers), graph store RMW races guarded
  (threading.Lock on 4 read-modify-write paths), SSRF guard on webhook
  subscribe (loopback/link-local/private/metadata IPs rejected),
  single-writer capture-retry queue (append-only enqueue, no lost entries),
  fail-closed guardrails on error paths, scope validation to one source
  (validateConfigScope), no-fsync agent-stats hot path, pathlike redirect
  pattern in guardrail_check ('a > b' no longer classified destructive).

### Changed

- pyproject.toml: readme metadata declared; asyncio test mode scoped.
- audit.yml: pip cache, permissions, concurrency, ERE-anchored checks.
- .gitignore: anchored snapshot globs, duplicates removed, lockfile now
  tracked (Nr 400).
- plugin.yaml / pyproject: dependency upper bounds (major caps).

### Tests

- 1818 passed / 2 skipped on macOS; 1758 passed / 7 skipped in the
  Ubuntu replica (known environment gaps: machine paths, Voyage key,
  Node strip-types); OpenClaw plugin suite 26/26; leak-check 0.

[0.20.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.20.0

---

## [0.19.1] - 2026-09-13

### New

- **Memory Quality Gates** — recall quality is decided at ingestion, not
  search time. Four gates between a raw conversation and the long-term
  store: (1) stated rule, only user-stated content becomes durable facts
  about the user; (2) horizon rule, soon-obsolete task state is never
  stored as a fact; (3) single-mention rule, passing remarks are capped
  (confidence 0.5) so salience follows and they decay; (4) memory-as-data
  hardening, embedded-instruction text is stored but flagged
  (memory_injection_flag) and demoted (salience cap 0.4) so it can never
  anchor recall/prefetch or outrank genuine user rules. Deterministic
  validation in the extraction path, LLM-assisted filtering in the
  consolidation distiller, zero new dependencies.

### Changed

- Extracted fact validation moved into testable `_validate_llm_facts()`
  (pure refactor, behavior unchanged).
- Session-end extraction now passes fact confidence through as salience
  (single-mention facts decay instead of sticking forever).
- Retention-priority hint in the consolidation distiller: user-stated
  rules, corrections and repeated requests rank above one-off research
  findings. No deletes introduced - the established supersede pattern is
  untouched (no deletes, ever).

### Tests

- 1106 passed / 0 failed / 0 errors (10 new: single-mention cap,
  memory-injection score, salience wiring, validation refactor) via
  isolated disposable-venv verify run (library recipe: bootstrap+pytest).

## [0.19.0] - 2026-09-13

### New

- **Query rewriting before embedding** — sloppy conversational
  queries are rewritten into concrete search terms before embedding, so the
  keyword+vector search finds what the user actually meant ("what was that
  thing for the car" -> "wallbox charging cable rfid return"). Live-store
  bench: 12 real queries, hit-rate 67% -> 75%, zero regressions, EUR 0.00
  (fuel stations resolve cheapest-open-first; interactive rewrites get a
  10 s per-station timeout). Fail-open in EVERY failure mode: any wiring,
  station, timeout or generation error returns the original query unchanged.
  The search never degrades. Emergency brake: set `NEXUS_REWRITE=0`.
  Queries with digits stay untouched (ports, IDs); rewritten results are
  memoized (64-entry FIFO) so the prefetch thread and explicit recall share
  ONE rewrite per unique query. Logs on the hot path are DEBUG-only (no
  query content in INFO).

### Changed

- `consolidation.get_default_fuel(timeout)` — one canonical station-chain
  resolver shared by the consolidation daemon and query-rewrite (was
  copy-pasted resolution logic).
- GLM think-strip deduplicated: `_extract_payload` is the canonical
  implementation; `query_rewrite._clean_output` delegates to it.
- Hot-path logging downgraded to DEBUG in query-rewrite paths.

### Tests

- 1097 passed / 0 failed (was 1096 + 25 recon/wiring tests for rewrite,
  incl. default-ON and brake cases). Runtime proof on live Qdrant: 5/5
  (rewrite hit, memo dedup, fail-open, bounded timeout, disabled-neutral).

## [0.18.7] - 2026-09-09

### Changed

- **Update notifications now re-check GitHub every 24h** (previously: a single
  check at server startup — a long-running server never saw releases published
  after it booted). The docstring's "cached 24h" promise is now real.
- **Update nudge repeats every 7 days** instead of once per server lifetime:
  a user who misses the first notice gets reminded weekly, without spam.
  Failure paths unchanged: network errors fail open silently.

## v0.18.7 (unreleased)

### Removed
- **Legacy `webui/` graph dashboard deleted** (the original DeepSeek-era UI). `nexus-memory webui` now starts the current dashboard (`dashboard/`): connected agents, memory graph with filters, memory inspector. Same command, now on port 9121 — no stale UI left behind.

### Changed
- README / AGENTS.md / install scripts point agents to the current dashboard. `pip install nexus-memory[webui]` extra removed from pyproject (fastapi/uvicorn needed only when running the dashboard from a checkout).

# v0.18.6 — Auto-Scoping Parity: All Three Plugins

**Auto-scoping now works identically on every integration path** — Hermes native plugin, OpenClaw TS plugin, and Claude Code hooks. v0.18.5 shipped self-organizing memory server-side (auto-tagging in the MCP server worked for all agents) but the client-side pieces (auto-recall gating, auto-capture tagging, store-tool tagging) lived only in the Hermes plugin. Now every plugin infers areas from scoped centroids with the same clear-match rule — no user config anywhere (project rule: full automation or useless; no release without plugin parity).

## New
- **`plugins/openclaw/lib/scope-auto.ts`** — TypeScript port of `scope_auto.py`: centroid cache with TTL + inflight dedup, conservative `inferScope()` (clear-closest only: ≥0.72 absolute AND ≥0.05 margin over runner-up), `prefetchFilterScopes()` with manual-scope-as-addition semantics. Wired into auto-recall gating, auto-capture tagging, and the `nexus_store` tool (explicit scope param wins → cfg scope → auto-infer → default).
- **`plugins/claude-code/scripts/scope_auto.py`** — shared lib for the Claude Code hooks (short-lived processes: one scroll per call, fail-open). `auto_recall.py` now infers the allowed scope set from the prompt itself; `auto_capture.py` tags captures via `_resolve_capture_scope()`.
- **Parity tests**: 13 new tests — 12 Python (clear-match, ambiguous fail-open, absolute threshold, manual-scope union, fetch fail-open, capture resolution) + 1 cross-language parity test that runs the actual TypeScript module via node and asserts identical decisions to the Python implementation.

## Tests
- 1076 passed (1063 previous + 13 parity tests)

**The memory now assigns its own areas — full automation, zero user setup (project rule: automate or it's useless).** Scope labels exist since v0.18.4, but they required a config value per agent. Now the memory infers the area itself: when a new memory is stored, it compares the content vector against the centroids of existing scoped areas and inherits the matching scope automatically. And at recall time, a question that clearly belongs to one area gets that area's memories plus the shared ones — no configuration anywhere in the loop.

## New
- **`scope_auto.py` — scope centroids + conservative inference**: centroid per scope from canonical scoped points (60s TTL cache, fail-open to "no areas"). `infer_scope()`: only tags a memory when its vector is CLEARLY closest to one area (margin ≥ 0.05 to runner-up AND ≥ 0.72 absolute similarity) — under-tagging is harmless, over-tagging is what we avoid. Zero LLM cost: pure vector math.
- **Auto-tagging at `remember()`**: caller leaves `scope` at `default` → server inherits the inferred area automatically. Explicit non-default scopes are never overridden. Fail-open: any inference error → stays `default`.
- **Query-side auto-filtering (Hermes plugin prefetch)**: a prompt that clearly belongs to one area surfaces only `default` + that area's memories; ambiguous prompts change nothing (old behavior). No `NEXUS_SCOPE` needed — the query steers itself.
- **Fully backward compatible**: with no scoped memories in the store, centroids are empty → `default` everywhere → byte-for-byte old behavior. Users never see the word "scope".

## Tests
- 1063 passed (1048 previous + 15 new auto-scoping tests: centroid math, margin logic, dimension-mismatch skip, fail-open paths, remember-integration, prefetch integration)

# v0.18.4 — Scopes: Project/Agent Areas (unreleased feature, first implementation)

**Scopes answer "which project does this belong to?"** — access levels already answer "who may see this?". Every memory can now carry a scope label so multiple agents sharing one memory store get clean, focused auto-recall instead of cross-project noise.

## New
- **Scope labels** (`scope`): optional area label on every memory (`nexus_remember(..., scope="voice")`). Valid: `[a-z0-9-]`, max 40 chars, normalized lowercase. Anything invalid degrades to `default` (fail-open) — behaves exactly like pre-scope memories.
- **Core principle — scopes steer automatic prefetch, never explicit search**: auto-prefetch (Hermes plugin `NEXUS_SCOPE` env / OpenClaw plugin `scope` config) surfaces only `default` memories plus the agent's own scope. Explicit `recall()` / `nexus_search` is NEVER scope-filtered — a scoped memory is never hidden from a direct question.
- **OpenClaw plugin parity**: `scope` config key + `NEXUS_SCOPE` env fallback in `lib/config.ts`, gating in the auto-recall hook, scope inheritance in capture + `nexus_store` tool (optional `scope` parameter with same normalization).
- **Claude Code plugin parity**: auto-recall hook gates on `NEXUS_SCOPE` (same client-side filter contract), auto-capture stores memories with inherited `_normalize_scope(NEXUS_SCOPE)`; graph-boost neighbors intentionally unfiltered (explicit relations). MCP-based Claude Code setups inherit v0.18.4 automatically.
- **Backward compatible**: no `NEXUS_SCOPE` set → the agent sees everything (old behavior); memories without a scope field behave as `default`.

## Tests
- 1048 passed on the production suite (1034 previous + 14 new scope tests; 21 additional scope/rewrite tests live on the development testbed workspace)

# v0.18.0 — Consolidation Daemon + Multi-Station Fuel Chain

**Ingestion-time consolidation is live.** Raw session dumps are distilled into atomic, self-contained facts (pronouns resolved, relative dates anchored) and contradictions are superseded at write time — the retrieval hebel from the LongMemEval findings, now in production path.

## New
- **Consolidation daemon** (`consolidation.py`): in-process background thread in the MCP server (no cron, harness-independent). Distills un-consolidated `session` points into `fact` points, resolves conflicts at write time via embed-similarity (≥0.75) + LLM classify (duplicate/supersede/unrelated). Never deletes — superseded facts keep lifecycle status. Kill-switch `NEXUS_CONSOLIDATION=0`. Interval `NEXUS_CONSOLIDATION_INTERVAL` (default 3600s).
- **Fuel chain** (`fuel_chain.py`): the daemon is a hitchhiker on the user's existing LLM config — no setup, no new account. Station order (cheapest first): local Ollama → OpenRouter → OpenAI-compatible keys (OPENAI_API_KEY / NOUS_API_KEY / explicit NEXUS_FUEL_BASE+NEXUS_FUEL_KEY). Cheapest tier model per station, never the user's flagship. All stations closed → daemon sleeps and retries next tick (fail-safe, never crashes, never blocks).
- **Monthly budget cap**: `NEXUS_FUEL_BUDGET_USD` (default 1.00). Paid stations pause when the cap is hit; free local Ollama keeps working. Spend tracker in `~/.nexus-memory/fuel_spend.json` (auto-reset each month).

## Tests
- 799 passed (8 new fuel-chain tests + 19 consolidation tests from the 05.09 GO)

# Changelog

All notable changes to **Nexus Memory** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.17.0] - 2026-09-04

### Changed

- **qwen3-embedding is now the preferred local embedding provider** (Ollama).
  Benchmark (LongMemEval-S, 100 identical questions, identical hybrid RRF
  protocol): qwen3-embedding:0.6b 66/72/75% (R@5/10/20) vs bge-m3 62/71/73%.
  Priority order is now: qwen3-embedding → bge-m3 → any 'embed' model.
  Existing users keep their current model automatically (collection drift
  guard reads `embedding_model` from config) — no silent mixed-model
  collections. bge-m3 remains fully supported as the second choice.
- **Instruct prefix for qwen3 queries**: qwen3-embedding is instruction-aware,
  so queries are embedded with the official Instruct prefix while documents
  stay plain — matches the model card guidance and the benchmark protocol.
- **Wizard**: recommends `ollama pull qwen3-embedding:0.6b` (639 MB, 1024d,
  MRL, instruction-aware, 32k context) instead of bge-m3 (1.2 GB); bge-m3
  stays available as the second option. Config now records the concrete
  local model (`embedding_model`).

## [v0.16.0] - 2026-09-03

### Added

- **Temporal Fact Validity**: every memory now carries `valid_from` (defaults to
  `created_at`, overridable via the new `effective_from` parameter for
  retro-dated imports such as mail) and `valid_to` (`None` = still valid).
  Auto-supersession stamps `valid_to` on the superseded fact at supersession
  time — the full validity history is retained instead of being overwritten.
- **Point-in-time recall**: new optional `as_of` parameter on `recall`.
  With `as_of`, deprecated facts are returned when they were valid at that
  date, and the `valid_until` TTL is compared against the cutoff instead of
  "now". Omitting `as_of` keeps the previous behavior exactly unchanged.
- **`fact_history` MCP tool**: traces the supersession chain of a memory in
  both directions (successors and predecessors), ordered by `valid_from`,
  showing how a fact evolved over time.

## [v0.15.0] - 2026-09-03

### Added

- **Memory Dynamics**: Decay (5%/Monat linear, Floor 0.3), Salience-Boost (>= 0.8 immun), ln-Versetärkung via use/access-Counts — Scores bereinigt sich jetzt mit tatsächlicher Nutzung.
- `effective_score` / `normalize_salience` / `access_update_payload` in neuem Modul `src/nexus_memory/memory_dynamics.py`.
- Recall-Flugbahn: Top-Treffer verstärken sich (use_count), ungenutzte gedächtnisse verblassen langsam.

### Fixed

- Review Runde 1+2 (Verifier GREEN): B2 (source_url-Passthrough), M2 (Dynamik-Fenster auf Basis-Score statt effektivem Score — sonst sortiert Dynamik die semantische Rerank-Ordnung um), use/access_count getrennt incrementiert mit retrieve-before-write (Reset-Bug + Lost-Update gefixt).
- ln-Cap-Docstring Klarstellung (Verifier-Kosmetik).
- README: Memory-Dynamics-Sektion.

## [v0.13.2] - 2026-08-30

### Fixed

- **Prefetch slot-replacement race** (hermes-agent `memory_manager.py`) - when a
  turn began while the previous prefetch thread was still in flight, the old
  code replaced the `_external_prefetch_threads` mapping regardless, silently
  dropping that turn's memory recall. Fixed to only skip when the previous
  prefetch is genuinely still running. 71/71 memory-provider tests green.

### Changed

- **Prefetch capacity doubled** - Auto-Prefetch now fetches 10 (was 5) vector
  hits with a 2400-char (was 1200) budget. At 14k+ stored memories the old
  limits surfaced only 2-3 hits and let session-noise dominate context.
  Both still overridable via `NEXUS_PREFETCH_CHARS`.

### Added

- **Hardware auto-entity detection** - `sync_turn` now triggers entity extraction
  immediately when a user message contains declarative hardware statements
  ("Ich habe X", "Ich nutze Y") together with known hardware keywords
  (Bose, Razer, Mikrofon, USB, Bluetooth, ...). Stores with confidence 0.9
  instead of letting the fact sink as a confidence-0.5 session log entry.
  Question sentences are excluded. Failure is non-blocking.

## [v0.13.1] - 2026-08-30

### Added

- **OpenClaw update-check** - the OpenClaw plugin now checks
  `releases/latest` on GitHub at startup (24h cache, semver compare via
  `packaging.version`) and injects a once-per-lifetime nudge into the
  system prompt when a newer release exists - update-notification parity
  across all 3 install paths (Hermes plugin, MCP server, OpenClaw plugin).

## [v0.13.0] - 2026-08-31

### Added

- **Point-in-Time Queries** (roadmap 4.5) - `nexus_recall` accepts
  `as_of: "YYYY-MM-DD"`: only memories created on/before that date are
  returned ("what did we know on date X?"). Empty string (default) keeps
  all memories visible. Lexicographic date compare, timezone-agnostic.
- **Supersede reason** (roadmap 4.7) - auto-supersession now writes
  `supersede_reason` ("replaced by fact X (similarity 0.93 >= 0.90)")
  into the deprecated payload, making silent overwrites auditable.
- **Skill health monitor** (roadmap 4.10) - SICA flags category=skill
  memories unused for 180 days as review suggestions (never auto-delete;
  skills decay by lack of use, not by error).

### Not built (documented decision)

- **Tiered Loading L2** (roadmap 3.1 remainder) - intentionally not
  built: L0 (embed cache) + L1 (prefetch budget) already cover the real
  need; a summary layer has no consumer at the current memory size.
  Building it now would be overengineering. Revisit when the store
  grows past ~50k points.

## [v0.12.0] - 2026-08-30

### Added

- **Latency benchmark** (roadmap 3.3) - `scripts/bench_latency.py`:
  p50/p95/p99 over 30 real queries (embed + qdrant + filter + rerank +
  graph). Baseline: p50=485ms, p95=610ms. Breakdown: Voyage embed ~256ms
  (53%), Qdrant ~9ms (2%), rerank+graph ~220ms (45%). The <100ms target
  needs the cloud-roundtrip removal track, not query tuning. Honest
  report, not aspirational numbers.
- **EmbedCache L0** (roadmap 3.1) - new `nexus_memory/embed_cache.py`:
  thread-safe LRU (256) for query vectors. `_recall` and `_do_prefetch`
  reuse repeated queries - second hit skips the ~256ms Voyage roundtrip
  and its API cost. Semantic no-op. 5 tests.
- **Prefetch token budget** (roadmap 3.1b) - auto-injected memory context
  was up to ~3.5k chars; now capped by `NEXUS_PREFETCH_CHARS` (default
  1200) with item-boundary-aware trimming. ~65% context tokens saved per
  message. 1 test.
- **Data flywheel** (roadmap 4.9) - recall bumps `access_count` + sets
  `last_accessed` on the top-3 hits in a fire-and-forget daemon thread;
  results order never touched. SICA can use access_count as trust signal.
  1 e2e test.
- **Autonomous purge** (roadmap 4.8) - SICA `_detect_low_confidence` now
  auto-deletes memories that are conf<0.2 **and** never accessed **and**
  older than 30 days (3-of-3 rule; everything else stays review-only).
  `_apply_auto_patch` accepts the new delete type. Unit + e2e tests.



### Added

- **Superseded-by recall skip** (roadmap 4.6) - deprecated and
  rolled_back facts stay in Qdrant for audit but never surface in the
  Hermes plugin recall or auto-prefetch (mirrors the MCP server filter).
  Legacy points without lifecycle_status stay visible. Plugin upserts
  now tag new memories lifecycle_status: canonical. 3 new tests.
- **Auto entity enrichment on nexus_remember** (roadmaps 1.1/4.1) -
  every nexus_remember with 80+ chars queues a fail-open entity
  extraction pass in a daemon thread (single-flight): entities stored as
  Qdrant points, relationships as graph edges. No tool-call latency.
  Hash-dedup skips repeated texts, single-flight lock, NEXUS_AUTO_ENRICH
  opt-out, access_level propagation (private stays private). 6 new tests.
  Session-end extraction now shares the same code path (rel_map removed).

### Changed

- **Recall pipeline order (review)** - lifecycle filter now runs BEFORE
  reranking so deprecated points don't burn rerank-pool slots; graph-boost
  neighbors also skip deprecated facts.
- **Entity edge store reuse (review)** - one EdgeStore instance per
  extraction run instead of one per relationship; identity rel_map
  removed.

## [v0.10.0] - 2026-08-30

### Added

- **Cross-Encoder Reranking** (roadmap 1.2) - new `nexus_memory/reranker.py`;
  optional rerank step in the Hermes plugin `_recall` pipeline. Config-driven
  via `nexus-memory.rerank` in `~/.hermes/config.yaml` (reranker: `auto`
  default - adapts per user: Voyage API when a key is present, free local
  CrossEncoder otherwise - or explicit `voyage` / `cross-encoder`;
  `rerank_pool` pool size; env overrides NEXUS_RERANK / NEXUS_RERANKER).
  Disabled by default; fail-open (rerank errors return the original
  vector order). 15 new tests.
- **Reflect insights** (roadmap 2.1) - SICA synthesizes one
  deterministic insight per contradiction group (confidence-based winner
  + concrete resolution suggestion) instead of review-only suggestions,
  stored in `SICAResult.reflect_insights`. LLM-free.
- **Entity dedup detection** (roadmap 4.2) - SICA groups
  category=entity memories by (entity_type, casefold-normalized name);
  duplicates surface as `merge_review` suggestions (never auto-delete;
  keeper = oldest point) and generate a keeper-focused insight.
- **Per-category retention policies** (roadmap 2.2) - SICA `_detect_retention`
  replaces the temp-only scan: temp=1 day, session=7 days by default,
  override via SICA_RETENTION_<CATEGORY> env vars; unlisted categories and
  no SICA_DEFAULT_RETENTION_DAYS keep memories forever. Memory with
  missing/unparseable timestamps is never deleted. Legacy
  SICA_STALE_TEMP_DAYS keeps working (feeds the temp policy).
  `_apply_auto_patch` now deletes `retention_expired` issues. 16 new tests.

### Changed

- SICA issue type for expired memories is now `retention_expired` (was
  `stale_temp`); `_detect_stale_temp` stays as a backwards-compatible bridge.

## [v0.9.1] — 2026-07-27

### Fixed

- **Discovery content-dict handling** — `nexus/discovery/__init__.py` now handles `content` field stored as dict (not string) in Qdrant payloads, fixing the Auto-Discovery crash that affected 23% of points
- **SICA session storage dimension mismatch** — `_store_sica_session` now loads `.env` before creating EmbeddingProvider (was picking Ollama 768d instead of Voyage 1024d), accepts caller-provided embedder, and checks collection dimension before upsert
- **Hermes plugin passes embedder to SICA** — `_sica_run()` now passes `self._embedder` to `run_sica()` to guarantee dimension match

## [v0.9.0] — 2026-07-27

### Added

- **Graph-Boosted Auto-Recall** — all 3 plugins (Hermes, OpenClaw, Claude Code) now fetch 1-hop graph neighbors from the top 3 vector search results via `GraphTraversal.get_related()`. Graph-boosted entries are tagged `[graph:<relation>]` in context output.
- **SICA Self-Improvement Cycle** — `nexus/sica/` module implementing Detect → Reflect → Act → Learn loop
  - Detect: stale temp memories (configurable age threshold), low-confidence memories, contradictions via graph edges
  - Act: auto-patches non-destructive issues (stale temp deletion), all other issues become suggestions
  - Learn: stores SICA session as memory for future iterations
  - `nexus_sica_run` tool in Hermes plugin (12 tools total)
  - Harness-independent: any plugin can call `run_sica()` directly
- **Access-level filtering in graph-boost** — OpenClaw and Claude Code plugins check target point access_level before including graph neighbors
- **SkillGraph caching** — Hermes plugin caches SkillGraph instance across calls instead of per-call init+close
- **`SkillGraph.get_point()`** — public API replacing private `_scroll_point` access
- **`_load_env()`** in SICA — loads `.env` files before creating EmbeddingProvider to ensure correct provider detection
- 20 new tests (578 total)

### Fixed

- 64 code review issues across 7 review rounds (bugs, edge cases, security, resource leaks, type safety)
- Graph-boost `break` vs `continue` — suggestion cap no longer skips auto-fixable items
- `or 0.5` truthiness — confidence=0.0 no longer silently becomes 0.5
- `_scroll_all` offset check — `if offset is not None` instead of `if offset` (offset=0 is valid)
- Shutdown race condition — `_skill_graph_lock` acquired before closing SkillGraph
- ThreadPoolExecutor leak — futures cancelled on timeout
- SICA session storage dimension mismatch — embedder from caller prevents 768d vs 1024d error
- Naive datetime handling — timezone-naive timestamps treated as UTC
- Confidence type coercion — `float()` guard for string confidence values in Qdrant payloads
- Edges type guard — `isinstance(edges, list)` prevents TypeError on malformed payloads

## [v0.8.0] — 2026-07-25

### Added

- **Cost-Aware Routing** — tier-based embedding provider selection based on memory category
- **cost_router.py** — `CostAwareRouter` class with tier mapping, provider detection, routing decisions
- **Tier system**: premium (Voyage/OpenAI for facts, rules, entities), standard (Google/Jina for preferences, beliefs, procedures), economy (Ollama/sentence-transformers for sessions, temp)
- **Cost estimation**: per-provider cost per 1M tokens, `estimate_cost()` method
- **Routing stats + explain**: MCP tools `cost_routing_stats` and `cost_routing_explain`, Hermes plugin tools `nexus_cost_routing_stats` and `nexus_cost_routing_explain`
- **Auto-enable**: routing activates when 2+ providers are available, disabled with single provider
- **Graceful fallback**: if recommended tier has no provider, falls up (economy→standard→premium)
- **Config support**: `cost_aware_routing` flag in config.json, `NEXUS_EMBEDDING_PROVIDER` env var
- 34 new tests (558 total)

## [v0.7.0] — 2026-07-25

### Added

- **Knowledge Graph Layer** — entity extraction and typed relationships alongside Qdrant vectors
- **entity_extractor.py** — two-tier entity extraction (LLM preferred, heuristic pattern-based fallback)
- **Entity types**: device, service, person, location, organization, concept, software, protocol
- **11 new EdgeRelation types** for typed entity relationships: installed_at, connected_to, manages, runs_on, part_of, owns, located_at, depends_on_service, uses, provides, controls
- **graph/traversal.py** — multi-hop BFS traversal via NetworkX (deque-based, O(1) per pop), configurable depth, relation filter, entity_type filter
- **Graph queries**: traverse(), find_entities(), get_subgraph(), get_related(), stats()
- **KG tools for all 4 plugins**: MCP Server (4 tools), Hermes Plugin (4 tools), OpenClaw (TypeScript), Claude Code (CLI script)
- **Plugin integration**: on_session_end now extracts entities alongside facts, deterministic uuid5 entity IDs, relationships stored as graph edges via EdgeStore
- **Quick health check**: 1s TCP probe before LLM call, 10s API timeout
- 48 new tests (entity_extractor: 35, traversal: 13), 524 total

### Fixed

- None.strip() crash fix for LLM null values: `(e.get('name') or '').strip()`
- Deterministic entity IDs: uuid5 instead of uuid4 (no duplicates across sessions)
- Relationship storage: extracted relationships now stored as graph edges (was silently dropped)
- BFS performance: deque.popleft() instead of list.pop(0) (O(n) → O(1))
- OpenClaw KG tools adapted to the SDK API (TypeBox schemas, registerTool pattern)

## [v0.6.0] — 2026-07-25

### Added

- **Session→Memory Pipeline** — native fact extraction in on_session_end (no more raw text dumps)
- **extractor.py** — two-tier fact extraction: LLM (preferred, uses configured model) with heuristic pattern-based fallback (always works, no external dependencies)
- **Categorization**: fact, rule, preference, belief with confidence scores (0.0-1.0)
- **Inline execution**: runs in MemoryManager's background executor (no race condition with shutdown)
- **Race condition guard**: checks _write_stop before each Qdrant write
- **Stateless recovery**: each on_session_end probes LLM fresh, auto-recovers when endpoint comes back
- 24 new tests, 476 total

### Fixed

- JSON code block parsing: case-insensitive `json|JSON` tag matching
- Empty response.choices guard: no IndexError on empty API responses
- Correction patterns: fix `ne\d+` typo → `nee?` (German colloquial "nee")
- Fact patterns: remove overly broad `ist|is`, add word boundaries
- Remove password/token pattern (security: never extract secrets)

## [v0.5.1] — 2026-07-19

### Added

- **Auto-Supersession** — automatic deprecation of similar facts at similarity > 0.90
- `superseded_by` + `supersedes` tracking in payload
- Non-blocking: if check fails, fact is still stored
- Only for fact/rule/preference/procedure (not session/belief/temp)
- 452 tests

## [v0.5.0] — 2026-07-19

### Added

- **Active Guardrails** — memory-driven prevention of destructive actions
- guardrail_check + guardrail_override MCP tools
- Pattern matching for rm/drop/kill/recreate/find-delete/git-clean/dd
- Override with audit trail (min 10 chars reasoning, stored as private session memory)
- Fail-open: Qdrant outage degrades to ALLOW (never blocks agent work by accident)
- All 4 integration paths (MCP Server, Hermes Plugin, OpenClaw Plugin, Claude Code Plugin)
- 445 tests

## [v0.4.0] — 2026-06-19

### Added

- **OpenClaw native plugin** — Auto-Recall (memories injected before every turn) and Auto-Capture (facts extracted after every turn) powered by local Qdrant
- **`install_openclaw_plugin.sh`** — one-command install script that detects OpenClaw, configures `plugins.load.paths`, sets `plugins.slots.memory`, auto-detects embedding provider, and restarts the gateway
- **3-way architecture** — Hermes Plugin · OpenClaw Plugin · MCP Server, all sharing the same Qdrant collection
- **"Which path should I use?" table** in AGENTS.md and README.md
- **MCP Server → Core Engine integration** — SkillGraph initialization, Auto-Discovery after remember(), lifecycle status filtering in recall(), supersession in update(), Events on remember/update/forget
- **Time Decay in retrieval** — Gauss-shaped score decay (offset=30d, scale=365d) so recent memories rank higher, old ones fade gracefully
- **PROCEDURE memory category** — new `MemoryCategory.PROCEDURE` for workflow/procedural memory with step ordering
- **Staging with real embeddings** — replaced placeholder zero vectors with actual embedding provider calls (auto-detect Voyage → OpenAI → Google → Jina → Ollama → sentence-transformers)

### Changed

- README.md completely rewritten — 3-way architecture diagram, all 3 install paths, release history table, GitHub Sponsors badge
- Version badge updated to v0.4.0
- All version numbers synchronized (pyproject.toml, nexus.__version__, plugin.yaml, mcp_server.py)
- Staging `ensure_collections()` auto-detects vector dimension from embedding provider (was hardcoded 512d)
- AGENTS.md categories updated to include `procedure`

### Fixed

- **Staging placeholder vectors** — `[0.0] * 512` replaced with real embeddings via `_detect_vector_size()` and `_embed_content()`
- **Personal data removed** from public files (internal IPs, names, addresses)
- **Test suite** — updated for new category count and dimension detection

### Notes

- No breaking changes — same Qdrant collection, same API, same tools
- OpenClaw plugin uses Qdrant REST via `fetch()` (no Python dependency on the OpenClaw side)
- Lifecycle filtering is backwards compatible — entries without `lifecycle_status` field pass through
- Time decay only applies when timestamps are present — entries without timestamps are not penalized

---

## [v0.3.0] — 2026-06-18

### Added

- **Hermes native MemoryProvider plugin** — direct Qdrant access with zero MCP overhead
  - Auto-prefetch: relevant memories injected into context before every turn
  - Auto-sync: user + assistant turns saved as memories automatically
  - 3 manual tools: `nexus_recall`, `nexus_remember`, `nexus_forget`
  - Dimension-mismatch protection warns if embedding provider changed
- **`install_hermes_plugin.sh`** — one-command install: symlinks plugin, sets `memory.provider`, verifies
- **Embedding Provider Selection wizard** — `nexus-memory-init` interactive CLI
  - Scans system for all 6 providers
  - Shows quality ranking (excellent / good / basic)
  - Auto-selects best available as default
  - API key URL hints for cloud providers
- **Separate landing page server** + marketing assets (poster, references)

### Changed

- `pyproject.toml` version bumped to 0.3.0
- AGENTS.md restructured with Hermes Plugin and OpenClaw Plugin sections

### Notes

- Hermes plugin shares the same Qdrant collection with the MCP server
- No breaking changes to the MCP server API

---

## [v0.2.5] — 2026-06-13

### Fixed

- **`is_success()` helper** replaces raw `status_code == 200` across 29 sites in 10 files — Qdrant 201/204 responses no longer falsely treated as errors
  - `apply.py` (9 sites), `events.py` (7 sites), `staging.py` (3 sites), `nexus/__init__.py` (2 sites), `provenance/__init__.py` (3 sites), `cli.py` (1 site), `mcp_server.py` (1 site), `retrieval/__init__.py` (1 site), examples (2 sites)
- **5 bugs from Verifier audit** — Google async, try/except handlers, missing if-condition, deps, version drift

### Changed

- **TRUST_EPSILON consolidated** — same value (0.01) in `recompute_trust` + `recompute_all` (previously 0.01 vs 1e-9)
- **EVENT_TYPES derived from Enum** — single source of truth instead of duplicate
- **Deprecated `asyncio.get_event_loop()`** replaced with `get_running_loop()`
- **Re-embedding on hybrid fallback eliminated** — one API call instead of two
- **Unused imports removed** (json, Any, datetime, timezone, sys locales)
- **Unused constants removed** (STATUS_CONTESTED, RETRACTED, HISTORICAL, VALID_STATUSES)
- **`config.py` docstring corrected** — says "nexus" instead of "hermes-memory"

### Added

- **Audit GitHub Action** — automatic check on every push:
  - Collection-name check (finds `openclaw-memory`, `hermes-memory-1024d` etc.)
  - Status-code check (finds raw `== 200`)
  - Python compile check
  - pytest
- **SECURITY.md** — contact, supported versions, reporting process
- **Webhook subscriptions** — 3 new tools: `subscribe`, `unsubscribe`, `list_subscriptions`
  - Fire-and-forget HTTP POST to registered URLs on memory events
  - Persisted in `~/.nexus-webhooks.json` (no Qdrant, no SQLite, no new dependency)
  - Event types: `memory.remember`, `memory.update`, `memory.forget`
  - 27 new tests (379 total, all passing)

### Notes

- No breaking changes — same Qdrant collection, same API

---

## [v0.2.4] — 2026-06-12

### Added

- **Web UI** with live D3.js v7 force-directed graph
  - Interactive node graph of all memories
  - Clustering and category-mapping
  - Detail view on node click
  - Drift ampel (traffic light) for belief health
  - Stats cards with tooltips
  - Filter by category, full-text search
- **`nexus-memory webui` CLI command** — launches dashboard at `http://127.0.0.1:9121`
- **Ko-fi integration** in Web UI header and footer

### Fixed

- Graph.js crash on `d.full` → `fullText` property
- Safari reader mode prevention, marked as web app
- Cache-bust all assets (`?v=20260612`)
- Graph edges visibility (4px / 75% opacity, hover 5px / 100%)
- Node sizing (7 + 15×confidence), thicker edges, larger labels

### Changed

- WebUI refactored to graph-only landing page, removed marketing clutter
- `cli()` cleaned up after patch damage, proper argparse restored

---

## [v0.2.3] — 2026-06-08

### Added

- **`check_update` tool** — checks if a newer version is available on GitHub. Returns local vs latest version, release URL, and whether an update is available
- **`do_update` tool** — pulls latest version from GitHub, reinstalls via pip, and restarts the server. Requires `confirm: true` as safety guard
- **Self-restart** — after successful `do_update`, the server exits cleanly; the MCP client automatically reconnects with the new version

### Fixed

- **macOS setup.sh** — `grep -oP` → `-oE` compatibility fix
- **uv --system** flag added for venv creation

### Notes

- Agent workflow: `check_update` → ask user → `do_update(confirm: true)` → automatic reconnect
- Language-neutral — agent communicates in whatever language the user speaks

---

## [v0.2.2] — 2026-06-08

### Added

- **Justification Check (Rung 2)** — source URL verification on recall
  - `verification` field in recall results: `verified`, `unreachable`, or `unchecked`
  - `_check_sources()` async method — parallel HTTP HEAD checks on all source URLs
  - Payload enrichment — hybrid search results now include `source_url`, `access_level`, `category`, `source`, `created_at`, `provenance`

### Fixed

- **Score key** — `rrf_score` instead of `score` in HybridRetriever
- **Score normalization** — relative instead of fixed `/10`
- **Hybrid search embedding pass-through** + shim correction
- **HybridRetriever.search() shim** — resolves recall crash (`AttributeError`)
- **Default collection** → `nexus` (was: `hermes-memory`)
- **Voyage API key detection** — support both `pa-` and `vo-` prefix
- **Health check** — `model_name` property added to EmbeddingProvider
- **pyproject.toml** — `where=['src', '.']` finds both `nexus/` (root) and `nexus_memory/` (src/)
- **CLI sync** — `cli()` wrapper for async `main()` (entrypoint bug)

### Changed

- **Privacy** — the author's full name was replaced by the short form `Nebo` in all public files
- **Headline** — "One brain for all your agents" (pain-first positioning)

### Removed

- **Hardcoded `~/.hermes/.env` path** — replaced with generic MCP `env:` block, `NEXUS_ENV_FILE`, or `cwd/.env` fallback

---

## [v0.2.0] — 2026-06-07

### Added

- **MemoryCategory Enum** — 6 scopes: `fact`, `belief`, `session`, `rule`, `preference`, `temp`
- **Provenance tracking** — `source_url`, `confidence`, `attach_source()`
- **Guardrails** — content-length warnings (>5,000 chars), PII detection hints
- **Access Control** — `public` / `trusted` / `private` levels
- **Hybrid Search** — BM25 + Vector + Reciprocal Rank Fusion
- **Health monitoring** — Qdrant + embedding provider health checks
- **Drift detection** — scored 0–10 with healthy/attention/action thresholds
- **Auto-Discovery** — zero-token relation discovery between canonical facts
- **Graph Analytics** — hub scores, isolation scores, knowledge gaps, connected components
- **Skill Export** — `export_skill()` generates `SKILL.md` from canonical facts
- **`update` tool** — in-place metadata-preserving memory updates
- **5 MCP tools** — `remember`, `recall`, `forget`, `update`, `health`

### Changed

- Full v2.8.0 feature parity ported from `hermes-nexus-memory`
- 224 tests passing
- Single collection for all agents (no per-agent silos)

### Notes

- Backward-compatible with `hermes-nexus-memory` data
- All existing memories preserved in Qdrant

---

## [v0.1.0] — 2026-06-07

### Added

- **Initial release** — Universal Memory Layer for AI Agents
- **MCP Server** with 4 tools: `remember`, `recall`, `forget`, `health`
- **Access control** — `public` / `trusted` / `private` levels
- **Qdrant-backed vector storage** (1024d, voyage-3-large)
- **Automatic `.env` loading** — `~/.hermes/.env` [deprecated since v0.2.1] and `./.env`
- **Security** — local-only server, no cloud dependencies
- **Single collection** for all agents (no per-agent silos)

### Known Limitations

- No hybrid search yet (BM25 planned)
- No encryption at rest
- No Web UI
- Qdrant must be running separately

---

[v0.4.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.4.0
[v0.3.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.3.0
[v0.2.5]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.5
[v0.2.4]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.4
[v0.2.3]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.3
[v0.2.2]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.2
[v0.2.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.0
[v0.1.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.1.0