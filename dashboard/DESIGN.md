# DESIGN.md — Nexus Memory Dashboard

Agent-readable design spec. Every change to dashboard UI follows this file.
Source of truth: `dashboard/static/webui-style.css` + `dashboard.css` + the project's design laws.
Format follows the emerging DESIGN.md convention (like llms.txt, but for looks).

## Brand

Nexus Memory is the memory layer for AI agents. Look: dark, precise, calm. No clutter, no decorative noise. Data is the hero.

## Colors

### Core palette (dark theme, single theme)
* `primary` **#954EFF** — Nexus violet, brand + primary actions
* `accent-cyan` **#02C1FF** — live/active indicators, links
* `page-bg` **#1a1a2e** — main background
* `surface` **#12121f** — cards & panels
* `surface-deep` **#0d0d18** — nested surfaces, code blocks
* `text-primary` **#e8ecf4** — headings, body text
* `text-muted` **#636e72** — labels, secondary text
* `border` **#DEDDDE** — hairlines, card borders (use sparingly)

### Status colors
* `success` **#00b894** (green)
* `danger` **#e74c3c** (red)
* `warning` **#fdcb6e** (amber)
* `info` **#3b82f6** (blue)

### Rules
- Never introduce new colors. Derive shades from the core palette with rgba().
- Red = danger only. Never for decoration.
- Status colors map to system states (Qdrant ok/fail, gateway state, quota).

## Typography

* Sans: **Inter** (fallback: -apple-system, Segoe UI, sans-serif)
* Mono: **JetBrains Mono** (fallback: Fira Code, monospace) — for values, IDs, code
* Variable values (counts, sizes, latency) use **auto-fit font sizing**: shrink text, NEVER wrap or truncate
* Body: 16px/1.5 · Labels: 13px/1.4 uppercase muted · Mono data: 11.5px–13px

## Spacing

`xs` 4 · `sm` 8 · `md` 16 · `lg` 24 · `xl` 32 (px)

## Radius

`sm` 6px · `md` 10px · `lg` 16px · `xl` 24px · `full` 9999px

## Elevation

* `sm` 0 1px 2px rgba(0,0,0,.06)
* `md` 0 4px 12px rgba(0,0,0,.1)
* `lg` 0 8px 30px rgba(0,0,0,.12)
* `glow-blue` 0 0 40px rgba(0,150,255,.15) — live-state highlight only
* `glow-purple` 0 0 40px rgba(120,80,255,.15) — primary-state highlight only

## Motion

* fast 150ms ease — hovers, toggles
* base 250ms ease — panels, transitions
* No bounce, no elastic. Motion is functional, never decorative.

## Components

### Stat card (the core unit)
* Surface bg, 6px radius, 1px border (border color at low emphasis)
* Icon (silhouette style, single color — never multicolor) + label (uppercase muted) + value
* **Auto-fit rule (HART):** variable values auto-size their font. NEVER wrap, NEVER truncate, NEVER ellipsis. If content can grow → card becomes multi-line, not clipped.
* Multi-line cards when content can grow (list rows, agent names)

### Agent card
* Layout: Logo | Name | Plugin+MCP | Local/Remote | Seat-Badge
* One row, five fixed zones, no reflow
* Badge = pill, `full` radius

### Buttons
* Primary: `primary` bg, white text, `md` radius
* Secondary: transparent, 1px border, text-primary
* Danger: `danger` bg, only for destructive actions, requires confirm step
* Height: 36–40px, `md` radius

### Inputs
* `surface-deep` bg, 6px radius, 1px border
* Focus: 2px `accent-cyan` ring, no default browser outline

### Toast
* Bottom-right, `md` radius, surface bg, auto-dismiss 4s
* Status color only as 3px left edge, never full bg

## Icons

* Silhouette/single-color style (the project's icon language: voice UI + dashboard)
* 20px default, 16px in rows
* One color from palette, no gradients, no filled+outline mixing

## Layout

* Stats bar top, content grid below, max-width 1440px centered
* Cards in CSS grid, min 280px column, 16px gap
* Handbook pages: single column, 800px max, generous line-height (1.6)

## Language & tone

* User-facing strings: English (dashboard is public-facing, open source)
* Numbers: monospace, tabular
* Status words: lowercase (`running`, `degraded`) — no shouting

## Hard laws (project design laws, non-negotiable)

1. **Auto-fit, never clip:** variable values shrink font instead of wrapping/truncating.
2. **Cards grow vertically** when content can grow — never horizontal scroll.
3. **One element for both directions** (iOS slider mindset) instead of button pairs, where a continuous control fits.
4. **Verified mockup = binding spec.** When a mockup is approved, build exactly that. Never "old look with new colors" on top.
5. **Dark theme only.** No light theme, no theme toggle.

## File references

* Tokens & webui components: `dashboard/static/webui-style.css` (35KB)
* Dashboard page styles: `dashboard/static/dashboard.css` (22KB, hard-coded hex — keep in sync with this file)
* Handbook pages: `dashboard/handbook/*.html`