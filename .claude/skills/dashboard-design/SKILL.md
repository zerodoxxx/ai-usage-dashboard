---
name: dashboard-design
description: Design system for the AI usage dashboard ("the meter"). Load before changing anything visual in src/templates/index.html, src/static/css/*.css or chart/table rendering in src/static/js/*.js — new panels, charts, columns, colors, copy, or layout.
---

# AI usage dashboard: design system

The dashboard is a personal meter for AI coding-agent consumption, not a SaaS admin panel. Tokens are the units consumed, blended $/1M is the unit rate, burn rate is the projected monthly bill, and API value is the meter reading. One physical object (the drum register) leads the page; everything else stays flat, quiet and legible.

## Where things live

- Tokens: `:root` of `src/static/css/dashboard.css` (light defaults; dark under `prefers-color-scheme` guarded by `:root:not([data-theme="light"])` and again under `:root[data-theme="dark"]`). Never add a raw hex outside the token blocks.
- Page shell, controls, register, readout, ledger, tables: `dashboard.css`, `index.html`, `dashboard.js`, `tables.js`, `odometer.js`.
- Everything Chart.js draws, plus the heatmap, donut legend/center, daily readout and tooltip: `charts.js` + `charts.css`. `charts.js` reads colors from the CSS tokens at runtime and re-reads them on the `themechange` event and OS scheme change, so new chart colors must be tokens too.

## Encoding (the core rule)

| Color | Means | Used for |
|---|---|---|
| Amber `--money`, `--money-1…6` | dollars | cost bars, cost heatmap, burn sparkline, $/1M rate bars |
| Steel `--tokens`, `--tokens-1…6` | tokens | token bars, token heatmap, cache-hit line, token sparkline, ledger token segments |
| Ink `--count` | counts | API-call lines, calls sparkline |
| `--tool-codex` / `--tool-claude` / `--tool-agy` / `--tool-other` | tool identity | small dots and donut segments only, always next to the tool's name |

- A new metric picks its color by what it measures, never by what looks nice next to its neighbor.
- Text never wears a data color. Values and labels use `--ink` / `--ink-2` / `--ink-3`; a dot, swatch or line key next to the text carries identity.
- Sequential ramps are one hue, low→high (`-1` lowest). Quantile-bucket nonzero values into the 6 steps; zero is `--surface-2`.
- Tool keys are mapped once in `utils.js` (`toolKey`, `toolLabel`, `toolDotHtml`); reuse them.

## Charts

- Spend vs usage over time is ONE overlaid plot, by the owner's explicit preference: they read divergence directly ("spend fell while calls rose"). Token volume is drawn as faint steel bars on a hidden scale behind, cost as an amber line on the left $ axis, and calls as an ink line on the right axis. Both visible axes start at 0 with the same number of intervals, so the right ticks sit on the left hairlines, and each axis carries a small caption with its line key ("Spent", "Calls"). The daily and hourly charts follow this pattern. Don't split them into lanes, and don't add a third visible axis.
- Bars: `maxBarThickness` 18, 3px radius on the data end only, grow from the baseline. Lines: 2px. Grid: horizontal hairlines in `--line`, solid, no vertical grid. Ticks 11px `--ink-3`, at most 4 per lane.
- Label directly only the one value that matters (the peak bar). Hover gives the rest: the daily chart's header readout, or the shared floating tooltip for the others.
- Refreshes update data in place (`chart.update('none')`); only the first render may animate, and nothing animates under reduced motion.
- An all-zero series shows "No usage in this period" instead of an empty plot.

## Type, space, shape

- One family: Archivo (variable, width axis). Width is set with `font-stretch`: ~108–110% for large values and the wordmark, 92% plus `tabular-nums` for table and axis numerals. No monospace anywhere.
- Scale: 11 / 12 / 13 (default) / 15 (tray titles, 600) / 18 / 24 / 30 (readout values, 600) / register `--register-size`.
- Spacing: 4 8 12 16 24 32 48 64. The board gap is 24.
- Radii follow hierarchy: trays 16, register housing 14, heatmap cells 6, drums 5, controls 8, seg track 10 / item 7, chips 999, bars 3. Don't unify them.
- Trays are flat: `--surface` fill, `--tray-border`, no shadow. Only floating things (popover, tooltip, toast) and the selected seg item get a shadow, via `--shadow-*`.

## Layout

A single `.board` grid of 12 columns with `grid-auto-flow: row dense`, in DOM order:

| Breakpoint | Arrangement |
|---|---|
| ≥1200 | daily (8) + heatmap (4); ledger (8) + `.stack` (4) holding Cost by tool and the flexible cache tray (`.tray--fill`); hourly (12) |
| 768–1199 | `.stack` is `display: contents`; daily 12; heatmap 6 + tools 6; ledger 12; cache 6 + hourly 6 |
| <768 | everything spans 12 |

- A new panel joins the board. Check 1440, 768 and 390 in both themes for dead tray space and horizontal scroll.

## The register

- `#odo-total-cost` is a physical object. In light theme it is a drum register (`--drum-*`, same drum colors as before). In dark theme it becomes a row of Nixie tubes whose amber glow is the money hue (`--nixie-*`).
- It shows at least 4 integer digits, with a wider gap at thousands groups instead of commas. Leading zeros are dimmed drums, or blank tubes in Nixie mode. The 2 cents digits are inverted drums, or slightly dimmer tubes.

## Motion: nothing moves unless something happened

Motion is allowed only when it reports a real event or answers the user:

1. The boot sequence, once per page load: the register spins like a slot machine, or Nixies cycle their cathodes, then settles right to left. The daily chart draws in like a pen recorder at the same time. The whole sequence takes ≤1.6s.
2. The register rolls (drums) or switches cathodes (Nixie) when its value changes.
3. The receipt tape works like an itemized bill: the top 6 models by cost, an "other models" subtotal and a total. It reprints when the user changes the query or the ranking changes. Amounts that changed swap in place on a refresh.
4. A milestone celebration plays when a live refresh pushes the total across a round threshold.
5. User-triggered motion: the skyline rises and rotates, and linked-hover transitions run (≤160ms).

No ambient loops, hover lifts on trays, entrance animations on panels or decorative pulsing. Everything above snaps or is skipped under reduced motion.

## Feature modules

Each feature owns its own JS and CSS. `dashboard.js` calls `update(data, ctx)` on each of them after every render, with `ctx = { boot, queryKey, tool, timeRange }`.

- **Register** (`odometer.js`, `register.css`): drums in light theme, Nixie tubes in dark. `boot(value)` runs the load sequence.
- **Milestones** (`milestones.js`): fires only when a live refresh of the same query crosses a 1-2.5-5 threshold.
- **Receipt** (`receipt.js`, `receipt.css`): a physical paper strip in the meter showing the top 6 models by cost, an "other models" row and a total. Its amounts must match the ledger and the register to the cent.
- **Skyline** (`skyline.js`, `skyline.css`): a Canvas 2D isometric city of `heatmap_daily`. Height is tokens; colour is the cost ramp, shaded in OKLCH and never toward grey.
- **Linked hover** (`linked.js`, `linked.css`): focus on a model or a tool shows that series over ghosted totals.
  - It uses `timeline_by_model`, `heatmap_by_model` and `model_index`.
  - Map keys to tools through `model_index`. Never parse the key prefix.
  - Axes never rescale on focus.

## Copy and chrome rules

- Sentence case everywhere, including table headers ("Uncached input", not "UNCACHED INPUT").
- No tracked all-caps eyebrows, no " · " meta strings, no "→" in buttons, no emoji, no gradient washes as decoration.
- Name things by what the user sees: "Spend and calls per day", "Last 30 days", "Blended rate".
- Empty and error states say what happened and what to do: "Couldn't load usage data. Check that the server is running, then refresh."
- Estimates are marked with the `est.`/`mixed` chip (`--warn-bg`) plus text, never by color alone.

## Verify a visual change

1. Screenshot full pages at 1440, 768 and 390 in dark and light, plus hover states for any chart you touched. Look at them.
2. Check the console for errors, and check `tool=claude-code` (short ledger) and `time_range=all` (long timeline). Trigger a milestone by hand with `DashboardMilestones.celebrate('cost', 500)`. For the boot sequence, capture frames during the first ~1.6s.
3. Run `python -m pytest -q test_server.py`; the index test asserts key ids and copy.
