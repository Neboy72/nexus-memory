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

**Auto-scoping now works identically on every integration path** — Hermes native plugin, OpenClaw TS plugin, and Claude Code hooks. v0.18.5 shipped self-organizing memory server-side (auto-tagging in the MCP server worked for all agents) but the client-side pieces (auto-recall gating, auto-capture tagging, store-tool tagging) lived only in the Hermes plugin. Now every plugin infers areas from scoped centroids with the same clear-match rule — no user config anywhere (Nebo law: full automation or useless; no release without plugin parity).

## New
- **`plugins/openclaw/lib/scope-auto.ts`** — TypeScript port of `scope_auto.py`: centroid cache with TTL + inflight dedup, conservative `inferScope()` (clear-closest only: ≥0.72 absolute AND ≥0.05 margin over runner-up), `prefetchFilterScopes()` with manual-scope-as-addition semantics. Wired into auto-recall gating, auto-capture tagging, and the `nexus_store` tool (explicit scope param wins → cfg scope → auto-infer → default).
- **`plugins/claude-code/scripts/scope_auto.py`** — shared lib for the Claude Code hooks (short-lived processes: one scroll per call, fail-open). `auto_recall.py` now infers the allowed scope set from the prompt itself; `auto_capture.py` tags captures via `_resolve_capture_scope()`.
- **Parity tests**: 13 new tests — 12 Python (clear-match, ambiguous fail-open, absolute threshold, manual-scope union, fetch fail-open, capture resolution) + 1 cross-language parity test that runs the actual TypeScript module via node and asserts identical decisions to the Python implementation.

## Tests
- 1076 passed (1063 previous + 13 parity tests)

**The memory now assigns its own areas — full automation, zero user setup (Nebo law: automate or it's useless).** Scope labels exist since v0.18.4, but they required a config value per agent. Now the memory infers the area itself: when a new memory is stored, it compares the content vector against the centroids of existing scoped areas and inherits the matching scope automatically. And at recall time, a question that clearly belongs to one area gets that area's memories plus the shared ones — no configuration anywhere in the loop.

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
- OpenClaw KG tools adapted to SDK API by Miosha (TypeBox schemas, registerTool pattern)

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