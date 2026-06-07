# Cron Job Architecture — Safety & Stability

**Why 27 jobs don't break the system.**  
*Zero loops, zero chain reactions, zero surprise bills.*

---

## Core Principles

### 1. No Job Chains
Every cron job is **self-contained**. No job triggers another job, reads another job's output as input, or depends on another job completing first. This is the #1 rule that prevents loop storms.

```
❌ BAD:  Drift Detection → triggers SICA → triggers Session Export
✅ GOOD: Drift Detection | SICA | Session Export   (independent)
```

### 2. Fail-Once, Stop-Once
Jobs NEVER retry indefinitely:
- **LLM-based (agent) jobs:** `max_turns=60` global limit. After 3 LLM failures (`api_max_retries: 3`) the agent gives up.
- **Script-based (no_agent) jobs:** run once, exit. If the Python script throws, the cron system logs the error and stops. No retry loop.
- **No exponential backoff, no repeat-until-success, no self-rescheduling.**

### 3. Temporal De-Risking
Jobs are spread across the day with at least 30 minutes between them:
```
02:30  Backup (no_agent)
03:00  Paperless (agent)
03:05  Nexus→Obsidian Export (no_agent)
03:15  Graph Update (agent)
03:25  Wiki Lint (no_agent)
04:00  Nexus Wiki Nightly (no_agent)
04:15  Nexus Paperless Incremental (no_agent)
04:30  Drift Detection (no_agent)
05:00  SICA (no_agent)
05:01  Trust Recompute (no_agent)
06:00  Brew Updates (no_agent)
08:00  Morning Report (agent)
09:00  Medium Scout (agent)
10:00  HN Scout (agent) + Voxyz Watch (agent)
10:15  Wiki Growth Weekly (agent)
10:30  Memory Promotion (agent)
10:45  Skill Curator (no_agent)
11:00  Multi-Agent Eval (agent)
18:00  Voxyz Watch (agent)
23:00  Session-to-Memory Export (agent)
```

No two jobs run in the same minute. Peak concurrency: 2 jobs (10:00).

---

## Job Types

### No-Agent Jobs (Watchdog Pattern)
Pure Python/Bash scripts, zero LLM calls, zero API costs.

**Characteristics:**
- Run as subprocess, not agent session
- No `hermes cron` overhead beside script execution
- Output is delivered verbatim OR suppressed entirely (SILENT pattern)
- Cost per run: ~0€ (CPU time only)

**When healthy → SILENT.** When problematic → one message.
The user never sees "everything is fine" messages.

### Agent Jobs (LLM-Based)
Full agent sessions with tool access.

**Characteristics:**
- Bounded by `max_turns: 60` and `gateway_timeout: 0` (no forced timeout)
- Uses DeepSeek Flash (cheap) for standard tasks
- Falls back to Gemini 2.5 Flash only if DeepSeek is unreachable
- Results delivered to `local` (no Telegram spam) — not shown to the user unless significant

**Cost per run:** ~$0.01–0.05 (DeepSeek Flash). Maximum ~$0.30 if all 60 turns are used.

---

## Safety Numbers

| Metric | Value | Why |
|---|---|---|
| Max retries per LLM call | 3 | `api_max_retries` in config |
| Max turns per agent job | 60 | `max_turns` in config |
| Max concurrent jobs | 2 | By schedule design |
| No-agent job cost | ~€0 | CPU + disk only |
| Agent job cost (typical) | €0.01–0.05 | DeepSeek Flash |
| Agent job cost (worst case) | €0.30 | 60 turns × 5 retries |
| **Worst case all jobs fail** | **~€5** | All 18 jobs × worst case |
| **Loop storm risk** | **0** | No job chains, fail-once |

---

## What Happens When a Job Fails

### No-Agent Job
1. Script exits with non-zero code
2. Cron system logs: `last_status: "error"`
3. No retry, no alert (unless configured to deliver)
4. Next scheduled run picks up as normal

### Agent Job
1. Each tool call gets 3 retries via `api_max_retries`
2. After 3 failures, the agent continues to next step
3. If agent gets stuck (no relevant tool calls), `max_turns` is reached
4. Session ends, `last_status: "ok"` but the prompt wasn't fulfilled
5. Next scheduled run starts fresh — no accumulated state

**What never happens:**
- Job triggering itself again
- Job triggering another cron job
- Exponential backoff billing loops
- Session persisting across cron runs

---

## Integration

This architecture is baked into:
- `setup.sh` — installs scripts but never chains them
- `AGENTS.md` — all setup steps are independent
- `scripts/hermes-cron-setup.sh` — idempotent, skips existing jobs
- `~/.hermes/config.yaml` — `api_max_retries: 3`, `max_turns: 60`

---

*Last updated: 2026-06-07*
**Principle:** *"Fail once, stop, and let the next scheduled run try again."*
