---
version: alpha
name: Ocean & Ink
description: >
  Dark, editorial design system for the kahzaabu fact-checking archive.
  Quiet Maldivian register (lagoon + sand accents on a deep ink ground)
  instead of generic cyberpunk dark. Semantic content colors (lie /
  deadline / credit / misleading) are preserved as meaning, not decoration.

colors:
  # surface
  bg:               "#0B1B2B"   # deep ink — page background
  surface:          "#112B40"   # cards, panels
  surface-2:        "#0E2334"   # sunk panels, code blocks
  border:           "#1F3A52"   # subtle dividers
  border-strong:    "#2E5275"   # outlined inputs, focused borders

  # text
  fg:               "#F2F1ED"   # primary text — warm off-white, not stark
  fg-dim:           "#9DB0C2"   # secondary text, labels
  fg-muted:         "#6B829A"   # tertiary text, captions
  fg-inverse:       "#0B1B2B"   # text on light accents

  # brand accents (Maldives-coded but not kitsch)
  primary:          "#C99A4D"   # sand — primary brand, active nav, key CTAs
  primary-hover:    "#D9AB60"
  secondary:        "#4A8E8E"   # lagoon — secondary CTAs, charts
  secondary-hover:  "#5DA3A3"

  # semantic content colors (DO NOT rebrand — these encode fact-check meaning)
  lie:              "#E94560"   # category: LIE / CONTRADICTION (alarm red)
  deadline:         "#F0A04B"   # category: BROKEN DEADLINE / MISLEADING (warning orange)
  credit:           "#9B6BD3"   # category: CREDIT THEFT (provenance violet)
  shifting:         "#F7C948"   # category: SHIFTING NUMBERS (numeric amber)
  good:             "#5BC787"   # neutral/positive (delivered promises, fresh data)
  info:             "#5BA8D5"   # informational state (freshness banner OK)

typography:
  sans:
    fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif"
    fontFeature: "'cv11', 'ss01', 'ss03'"   # Inter's stylistic alternates if loaded
  serif:
    fontFamily: "Charter, 'Bitstream Charter', 'Sitka Text', 'Iowan Old Style', Georgia, serif"
  mono:
    fontFamily: "'JetBrains Mono', ui-monospace, 'SF Mono', Consolas, 'Liberation Mono', monospace"

  # type scale (sans unless noted)
  display:          { fontSize: "36px", lineHeight: "1.15", fontWeight: 700, letterSpacing: "-0.02em" }
  h1:               { fontSize: "28px", lineHeight: "1.20", fontWeight: 700, letterSpacing: "-0.01em" }
  h2:               { fontSize: "22px", lineHeight: "1.30", fontWeight: 600 }
  h3:               { fontSize: "17px", lineHeight: "1.40", fontWeight: 600 }
  body:             { fontSize: "15px", lineHeight: "1.55", fontWeight: 400 }
  body-prose:       { fontSize: "16px", lineHeight: "1.65", fontWeight: 400 }   # serif, for long-form
  small:            { fontSize: "13px", lineHeight: "1.50", fontWeight: 400 }
  caption:          { fontSize: "12px", lineHeight: "1.45", fontWeight: 400 }
  label:            { fontSize: "12px", lineHeight: "1.40", fontWeight: 600, letterSpacing: "0.08em" }   # ALL CAPS
  mono-sm:          { fontSize: "12px", lineHeight: "1.45", fontWeight: 400 }
  mono-md:          { fontSize: "13px", lineHeight: "1.50", fontWeight: 400 }

rounded:
  none:             "0"
  sm:               "4px"
  md:               "8px"
  lg:               "12px"
  xl:               "16px"
  pill:             "9999px"

spacing:
  xs:               "4px"
  sm:               "8px"
  md:               "12px"
  lg:               "16px"
  xl:               "24px"
  xxl:              "32px"
  xxxl:             "48px"
  page:             "64px"   # outer page padding on desktop

components:
  card:
    backgroundColor: "{colors.surface}"
    borderColor:     "{colors.border}"
    rounded:         "{rounded.lg}"
    padding:         "{spacing.xl}"
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor:       "{colors.fg-inverse}"
    rounded:         "{rounded.md}"
    padding:         "10px 18px"
    typography:      "{typography.label}"
  button-secondary:
    backgroundColor: "transparent"
    textColor:       "{colors.fg}"
    borderColor:     "{colors.border-strong}"
    rounded:         "{rounded.md}"
    padding:         "10px 18px"
  pill:
    backgroundColor: "{colors.surface-2}"
    textColor:       "{colors.fg-dim}"
    rounded:         "{rounded.pill}"
    padding:         "2px 10px"
    typography:      "{typography.caption}"
  input:
    backgroundColor: "{colors.surface-2}"
    textColor:       "{colors.fg}"
    borderColor:     "{colors.border}"
    rounded:         "{rounded.md}"
    padding:         "10px 14px"
  nav-link-active:
    backgroundColor: "rgba(201,154,77,0.12)"
    textColor:       "{colors.primary}"
---

# DESIGN.md — Kahzaabu UI system

This document is the single source of truth for the kahzaabu web UI's visual language. Agents and humans editing `kahzaabu/web/static/**` should consult this before introducing new colors, font sizes, or spacing values.

The format follows the [DESIGN.md](https://github.com/google-labs-code/design.md) alpha spec: YAML front matter for machine-readable tokens, markdown prose below for rationale and application guidance.

## Overview

Kahzaabu is a fact-checking archive of Maldives Presidency press releases. The UI's job is to make uncomfortable factual patterns legible without performing them — so the design register is **calm, editorial, dark**, not flashy.

Three intents shape every choice:

1. **Newsroom, not dashboard.** Type hierarchy and line-length matter more than chart density. Long-form prose (article quotes, agentic-ask answers, narrative-tricks sections) gets a quiet serif. Chrome stays sans.
2. **Maldives-coded without kitsch.** Lagoon teal and warm sand are the accent pair — a quiet nod to place. No cliché blue ocean or coconut motifs.
3. **Semantic colors are meaning, not decoration.** The `lie` / `deadline` / `credit` / `shifting` colors encode fact-check categories. They must NEVER be repurposed for unrelated UI states (e.g. don't use `--lie` for a "delete" button).

## Colors

| Token | Where it appears | Notes |
|---|---|---|
| `bg` | page background | the deep ink ground |
| `surface` | cards, panels, modals | one step lighter than bg |
| `surface-2` | nested panels, code blocks, inputs | one step darker than surface — yes, lower than bg — for visual recession |
| `border` | subtle dividers, card outlines | low-contrast, restrained |
| `border-strong` | input borders, focused states | for elements that need to look interactive |
| `fg` | primary text | warm off-white (#F2F1ED) — avoids the sterile feel of pure white on dark |
| `fg-dim` | section labels, metadata | for everything *adjacent* to primary content |
| `fg-muted` | timestamps, footnotes | for content that should fade |
| `primary` (sand) | active nav, primary CTAs, the brand tint in headers | sand-amber, low chroma |
| `secondary` (lagoon) | chart series, secondary buttons | desaturated teal |

**Do** use `primary` for the *one* most-important action on a page. **Do not** sprinkle it.

**Semantic colors** keep their existing values — they're already established in the fact-check categorization, charts, and the lies tracker. Treat them as content, not styling:

| Token | Category | Pixel value |
|---|---|---|
| `lie` | LIE, CONTRADICTION | red `#E94560` |
| `deadline` | BROKEN DEADLINE, MISLEADING | orange `#F0A04B` |
| `credit` | CREDIT THEFT | violet `#9B6BD3` |
| `shifting` | SHIFTING NUMBERS | amber `#F7C948` |

## Typography

Two families:

- **Sans (Inter / system-ui)** for chrome, navigation, dashboards, forms, tables — everything that says "interface."
- **Serif (Charter / Georgia)** for long-form prose — agentic-ask answers, article bodies, the narrative-tricks layer, manifesto promise text. The serif slows the eye down. Use class `.prose` to opt in.

No web font is loaded by default — system stacks render acceptably and keep the page lean. If you later want a uniform Inter look across platforms, drop a Google Fonts `<link>` into the page head; the token already names Inter first.

Scale (always reference the named token, never raw px):

| Token | Use |
|---|---|
| `display` | hero page titles only — not used on every page |
| `h1` | page heading |
| `h2` | section heading |
| `h3` | card / panel heading |
| `body` | default UI body text |
| `body-prose` | serif, long-form (articles, answers, prose blocks) |
| `small` | dense secondary info, table cells |
| `caption` | timestamps, hint text under inputs |
| `label` | ALL-CAPS micro-labels above stat numbers, badges |
| `mono-sm` / `mono-md` | code, article IDs `[NNNNN]`, session-id pills |

## Layout

8-point spacing grid. **Reference scale tokens (`xs`..`xxxl`) — do not write raw pixel values.**

Common patterns:

- Card padding: `xl` (24px)
- Gap between sibling cards: `lg` (16px) — wide grid; `md` (12px) — tight grid
- Section bottom margin: `xxl` (32px)
- Page outer padding: `xl` on mobile → `xxl` on desktop
- Form row vertical gap: `md` (12px)

Max content width: `1280px` (already in `main`). Reading width for prose blocks: `680px` — long lines kill long-form readability.

## Elevation & Depth

The Ocean & Ink palette is intentionally flat. No drop shadows on cards — depth comes from the `surface` ↔ `surface-2` ↔ `bg` triangulation. The only exceptions:

- Modals / dropdowns: 1px `border-strong` + a 1-stop drop into `surface-2`
- Sticky header: 1px bottom border (no shadow)

Reserve drop shadows for floating affordances (active tooltip, focused button ring) — never for resting state.

## Shapes

Corners are softened, not pillowed.

- Inputs, buttons, cards: `rounded.md` (8px)
- Charts, large containers: `rounded.lg` (12px)
- Pills, badges, avatars: `rounded.pill`
- Section dividers: `0` (sharp 1px borders read as editorial; rounded dividers read as marketing)

## Components

**Card** — the workhorse. `surface` background, `border` outline, `lg` rounded, `xl` padding. Headings use `h3`; numbers use the `.num` utility (32px, weight 700). Stat cards prefix the number with a 12px `label`-typography metric name.

**Button (primary)** — sand. Used for the *one* primary action per page (Update, Ask, Publish). Size: `padding 10px 18px`, `rounded.md`, `label` typography.

**Button (secondary)** — transparent with `border-strong`. For non-primary actions.

**Pill / badge** — `pill` rounded, `surface-2` background, `caption` typography. Use for fact-check categories (with category color override), session ids, timestamps.

**Input / textarea** — `surface-2` background, `border` outline. On focus: `border-strong` + a 2px sand-tinted ring `box-shadow: 0 0 0 2px rgba(201,154,77,0.20)`.

**Table** — borderless cells, 1px `border` between rows. Header row uses `label` typography. Right-align numbers.

**Code / IDs** — `mono-sm` or `mono-md`. Inline code: `surface-2` background, `rounded.sm`, 1-2px horizontal padding.

**Header (site)** — flat `surface-2`, 1px `border` bottom. No gradient. Brand at left, nav at right. Active nav link uses `primary` text on a translucent sand background (see `nav-link-active` token).

**Freshness banner** — sits below header on the dashboard. Uses `info` (cool blue) when fresh, `deadline` orange when stale, `lie` red only on hard errors.

## Do's and Don'ts

**DO**

- Reference tokens by name. `var(--primary)` not `#C99A4D`.
- Use the type scale. `font-size: var(--type-h2-size)` not `font-size: 22px`.
- Use semantic colors only for semantic content. `--lie` on a fact-check pill, never on a delete button.
- Keep the page outer padding consistent. Override only for full-bleed elements (charts, hero images).
- Use `.prose` class when rendering Markdown answer/article bodies — it switches to serif and constrains line length.
- Give every interactive element a visible focus ring. The 2px sand glow on inputs is the canonical one.

**DO NOT**

- Don't introduce new colors. If you think you need one, you don't — check whether `fg-dim` / `fg-muted` / `secondary` fit.
- Don't reuse semantic colors as decoration. The categories mean something.
- Don't use drop shadows on resting card states.
- Don't put serif on UI chrome (buttons, nav, labels). Reserve it for prose.
- Don't write raw pixel values for spacing or font sizes. Use the scale.
- Don't override `--bg` per-page. The page background is a system-wide constant.
- Don't introduce a third accent. Sand + lagoon is the brand pair; semantic colors are content. That's the full palette.
