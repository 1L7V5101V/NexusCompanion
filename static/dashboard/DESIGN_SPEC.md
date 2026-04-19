# Nexus Dashboard — Design Specification

This document is the single source of truth for all visual and UX decisions in the dashboard.
Follow it exactly. Do not invent new values, do not use inline styles, do not reach for Tailwind or Bootstrap classes.

---

## 1. Fonts

Load from Google Fonts — already in `index.html`, do not remove:

```html
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

| Role | Value |
|------|-------|
| UI text | `var(--sans)` → `"DM Sans", sans-serif` |
| Code / IDs / timestamps / numbers | `var(--mono)` → `"JetBrains Mono", monospace` |

**Rule:** all `font-family` values must reference `var(--sans)` or `var(--mono)`. Never hardcode a font name elsewhere.

---

## 2. Color Tokens

All colors live in `:root`. Never use a raw hex in component CSS.

```css
:root {
  /* ── Backgrounds ── */
  --bg:           #f3ede3;   /* page / outermost background */
  --bg-soft:      #f8f4ec;   /* slightly lighter surface, e.g. hover */
  --paper:        #fffaf2;   /* card / pane / input surface */
  --paper-strong: #fffdf8;   /* elevated card (modal) */

  /* ── Borders ── */
  --line:         #dfd3c0;   /* default border */
  --line-strong:  #cdb89d;   /* hover / focus border */
  --line-soft:    rgba(205, 184, 157, 0.2);  /* subtle dividers */

  /* ── Text ── */
  --text:         #30261b;   /* primary text */
  --text-soft:    #6f6255;   /* secondary / muted text */

  /* ── Accent (warm orange-red) ── */
  --accent:       #bc5c38;
  --accent-soft:  #f3ddd2;   /* accent tint background */

  /* ── Semantic colors ── */
  --green:        #2f7d62;
  --green-soft:   #ddf0e7;
  --yellow-soft:  #fbf0c9;
  --red-soft:     #f8d8d0;
  --blue-soft:    #ebf0f9;

  /* ── Elevation ── */
  --shadow: 0 4px 16px rgba(0,0,0,0.10), 0 1px 4px rgba(0,0,0,0.06);

  /* ── Shape ── */
  --radius:    6px;
  --radius-lg: 10px;
}
```

### Semantic mapping

| Use case | Token |
|----------|-------|
| Page background | `var(--bg)` |
| Pane / sidebar | `var(--paper)` + gradient or `var(--bg-soft)` |
| Input / select background | `var(--paper)` |
| Modal background | `var(--paper-strong)` |
| Default border | `1px solid var(--line)` |
| Focus / hover border | `var(--line-strong)` |
| Primary text | `var(--text)` |
| Secondary / placeholder text | `var(--text-soft)` |
| Active / selected accent | `var(--accent)` |
| Accent tint backgrounds | `var(--accent-soft)` |

### Role badge colors

| Role | bg | text |
|------|----|------|
| `user` | `var(--accent-soft)` | `var(--accent)` |
| `assistant` | `var(--green-soft)` | `var(--green)` |
| `system` | `var(--yellow-soft)` | `#8b6b09` |
| `tool` | `var(--blue-soft)` | `#276489` |

### Channel badge colors (derive channel from `key.split(':')[0]`)

| Channel | bg | text |
|---------|----|------|
| `telegram` | `var(--blue-soft)` | `#276489` |
| `cli` | `#ece6db` | `var(--text-soft)` |
| `qq` | `#efe0f7` | `#74488d` |
| `scheduler` | `var(--yellow-soft)` | `#8b6b09` |
| unknown | `#ece6db` | `var(--text-soft)` |

---

## 3. Typography Scale

| Class / context | size | weight | family |
|-----------------|------|--------|--------|
| Body default | 13px | 400 | sans |
| Section kicker (SESSIONS, FIELDS…) | 11px | 700 | sans, uppercase, letter-spacing 0.08em |
| Badge / pill | 10.5px | 600–700 | mono |
| Table header | 11px | 700 | sans, uppercase |
| Table cell — session key | 11px | 400 | mono |
| Table cell — seq / timestamp | 11.5px | 400 | mono |
| Table cell — content preview | 13px | 400 | sans |
| Detail panel title | 15px | 600 | sans |
| Detail label | 11px | 700 | sans, uppercase |
| Detail field value | 12.5px | 400 | sans |
| Detail field value (ID / key / ts) | 12px | 400 | mono |
| Modal title | 15px | 600 | sans |
| JSON tree | 11.5px | 400 | mono |
| Button | 12.5px | 500 | sans |

---

## 4. Spacing

Use multiples of 4px. Common values:

| Token (use as literal) | px |
|------------------------|----|
| `4px` | extra tight (icon gap, badge padding) |
| `6px` | tight (gap inside badge, small padding) |
| `8px` | default gap between items |
| `10px` | standard padding horizontal |
| `12px` | panel padding |
| `14px` | pane padding |
| `16px` | section margin |
| `20px` | modal padding top/bottom |
| `24px` | modal padding |

Never use odd numbers like 7px, 9px, 11px for layout. They are acceptable for fine-tuning font/line things only.

---

## 5. Layout

### Shell

```
┌──────────────────────────────────────────────────┐  height: 46px  topbar
├──────────────────────────────────────────────────┤
│  sessions-pane (256px) │ messages-pane (flex:1) │ detail-pane (380px) │
│                        │                        │                      │
│                        │                        │                      │
└──────────────────────────────────────────────────┘
```

```css
.shell    { height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
.topbar   { height: 46px; flex-shrink: 0; }
.workspace { flex: 1; display: flex; overflow: hidden; min-height: 0; }
.sessions-pane { width: 256px; flex-shrink: 0; }
.messages-pane { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.detail-pane   { width: 380px; flex-shrink: 0; }
```

**Rule:** Use `flex` for the workspace, NOT a CSS grid. The detail pane can be hidden (`display:none`) when nothing is selected; a grid makes that harder.

### Topbar

- Height: exactly `46px`
- Background: `var(--paper)`
- Border: `border-bottom: 1px solid var(--line)`
- Padding: `0 14px`
- Brand on left (fixed width ~210px), filters in the middle (`flex: 1`), optional action button on the right.

### Sessions pane (left sidebar)

- Background: `var(--bg-soft)` or `var(--paper)` — must be subtly different from main area
- `border-right: 1px solid var(--line)`
- Header: `padding: 11px 12px 8px`, `border-bottom: 1px solid var(--line)`
- List: scrollable, `overflow-y: auto`, `padding: 4px 0 12px`
- Session items: `margin: 1px 8px`, `border-radius: var(--radius)`, `border: 1px solid transparent`

### Messages pane (center table)

Table column template: `34px 80px 60px 1fr 90px 72px 60px`
— checkbox | session-key | seq | content | timestamp | role | actions

- Table header: `min-height: 36px`, `background: var(--bg-soft)`, sticky top
- Table row: `min-height: 44px`, `border-bottom: 1px solid var(--line-soft)`, cursor pointer
- Table footer: `border-top: 1px solid var(--line)`, `background: var(--bg-soft)`

### Detail pane (right)

- `border-left: 1px solid var(--line)`
- Header: `padding: 12px 14px`, `border-bottom: 1px solid var(--line)`, flex row with close button
- Body: scrollable, `padding: 14px`

---

## 6. Components

### 6.1 Buttons

Three variants only. No others.

```css
/* Primary — use for the main CTA in a modal, or creation actions */
.btn-primary {
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: var(--radius);
  padding: 6px 13px;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
}
.btn-primary:hover { opacity: 0.88; }

/* Ghost — secondary actions, cancel, pagination */
.btn-ghost {
  background: transparent;
  color: var(--text-soft);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 6px 13px;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
}
.btn-ghost:hover { background: var(--bg-soft); }

/* Ghost danger — destructive secondary (delete) */
.btn-ghost.danger {
  color: #b03a3a;
  border-color: var(--line);
}
.btn-ghost.danger:hover { border-color: #b03a3a; background: var(--red-soft); }
```

**Rule:** Never add a `background-colored danger` button in lists or tables — only use ghost-danger there. Reserve `btn-primary` for confirmations and creation.

Icon-only buttons (edit ✎, delete ✕ in table rows):

```css
.icon-btn {
  background: none;
  border: none;
  padding: 2px 4px;
  font-size: 13px;
  color: var(--text-soft);
  border-radius: var(--radius);
  cursor: pointer;
}
.icon-btn:hover { color: var(--text); background: var(--line-soft); }
.icon-btn.danger:hover { color: #b03a3a; }
```

### 6.2 Inputs

```css
input[type="text"],
input[type="number"],
textarea {
  border: 1px solid var(--line);
  background: var(--paper);
  color: var(--text);
  border-radius: var(--radius);
  padding: 6px 10px;
  font-family: var(--sans);
  font-size: 12.5px;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
input:focus, textarea:focus {
  border-color: var(--line-strong);
  box-shadow: 0 0 0 3px rgba(188, 92, 56, 0.1);
}
```

Search input with icon — wrap in `.search` div, absolute-position the icon at `left: 8px`, `padding-left: 26px` on the input.

### 6.3 Select (custom component required)

**Never use a raw `<select>` for user-visible filters.** The browser dropdown is styled by the OS — it will never match the design.

Always wrap with `CustomSelect` (see `app.js`). The native `<select>` is hidden; the custom component fires `change` events on it so existing event listeners work unchanged.

Trigger styling:

```css
.cs-trigger {
  border: 1px solid var(--line);
  background: var(--paper);
  border-radius: var(--radius);
  padding: 6px 28px 6px 10px;
  font-size: 12.5px;
  /* SVG chevron via background-image, right 9px center */
}
.custom-select.open .cs-trigger {
  border-color: var(--line-strong);
  box-shadow: 0 0 0 3px rgba(188, 92, 56, 0.1);
}
```

Dropdown options:

```css
.cs-dropdown {
  position: absolute; top: calc(100% + 5px); left: 0;
  background: var(--paper-strong);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  z-index: 200;
  animation: cs-drop-in 0.12s ease;
}
.cs-option { padding: 7px 12px; font-size: 12.5px; cursor: pointer; }
.cs-option:hover { background: var(--bg-soft); }
.cs-option.active { color: var(--accent); background: var(--accent-soft); font-weight: 500; }
```

### 6.4 Badges / Pills

```css
/* Role or channel pill */
.badge {
  display: inline-flex;
  align-items: center;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 600;
  font-family: var(--mono);
  white-space: nowrap;
}
```

Use inline `style` or a modifier class to set the bg/color pair from the tables in §2.

### 6.5 Active-session chip (topbar filter indicator)

```css
.active-session-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: var(--accent-soft);
  border: 1px solid #e6c9bc;
  border-radius: var(--radius);
  font-size: 12px;
  max-width: 280px;
}
.active-session-chip code { font-family: var(--mono); color: var(--accent); font-weight: 500; }
.active-session-chip button { background: none; border: none; color: var(--text-soft); cursor: pointer; }
```

### 6.6 Session list item

Structure (two rows):
```
row1: [channel-badge] [:uid-part]  [count pill]
row2: [relative-time]              [✎ ✕ — show on hover only]
```

Active state: `background: var(--accent-soft); border-color: #e6c9bc;`
Hover state: `background: var(--bg-soft);`
Default: `background: transparent; border: 1px solid transparent;`

Action buttons MUST be `opacity: 0` by default, `opacity: 1` on `.session-item:hover`. Do NOT show them permanently — it clutters the list.

```css
.session-actions { opacity: 0; transition: opacity 0.15s; }
.session-item:hover .session-actions { opacity: 1; }
```

### 6.7 Table row states

| State | CSS |
|-------|-----|
| Default | transparent bg |
| Hover | `background: var(--bg-soft)` |
| Active (detail open) | `background: var(--accent-soft); box-shadow: inset 3px 0 0 var(--accent)` |
| Selected (checkbox) | `background: var(--yellow-soft)` |
| Active+hover | keep active bg — do NOT revert on hover |

### 6.8 Batch action bar

Appears above the table only when `selectedCount > 0`. Styled in accent tint:

```css
.batch-bar {
  padding: 6px 14px;
  background: var(--accent-soft);
  border-bottom: 1px solid #e6c9bc;
  display: flex; align-items: center; gap: 10px;
}
.batch-label { font-size: 12.5px; color: var(--accent); font-weight: 500; }
```

### 6.9 Detail pane blocks

Every section in the detail pane uses this pattern:

```html
<div class="detail-block">
  <div class="detail-label">SECTION TITLE</div>
  <!-- content -->
</div>
```

```css
.detail-block { margin-bottom: 14px; }
.detail-label {
  font-size: 11px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--text-soft); margin-bottom: 6px;
}
```

Content box (for the message content field — supports markdown):

```css
.detail-content {
  padding: 10px 12px;
  background: var(--bg-soft);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  font-size: 13px; line-height: 1.8;
}
```

Field key-value grid (for metadata fields):

```css
.detail-row {
  display: flex; gap: 10px;
  padding: 7px 0; border-bottom: 1px solid var(--line);
}
.detail-row-label { width: 120px; flex-shrink: 0; font-size: 11.5px; color: var(--text-soft); }
.detail-row-val   { flex: 1; font-size: 12.5px; word-break: break-all; }
```

### 6.10 JSON tree viewer

**Never use `<pre>` for JSON data.** Use the `makeJsonViewer(data)` DOM function from `app.js` which returns a collapsible tree node. It:
- Auto-parses nested JSON strings (double-encoded `result` fields etc.)
- Collapses objects/arrays at depth ≥ 3 by default (depth 0–2 are expanded)
- Colors: strings green, numbers accent, booleans blue, null muted

To use inside `innerHTML` templates, use the `jvPlaceholder(data)` helper which emits a `<div data-jv="...">` marker, then call `attachJsonViewers(container)` after setting `innerHTML`.

```css
.json-tree {
  padding: 9px 11px;
  background: var(--bg-soft);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  font-family: var(--mono); font-size: 11.5px; line-height: 1.75;
  overflow-x: auto; word-break: break-all;
}
.jt-children { padding-left: 16px; border-left: 1px solid var(--line-soft); margin-left: 3px; }
.jt-str  { color: #2f7d62; }
.jt-num  { color: var(--accent); }
.jt-bool { color: #276489; }
.jt-null { color: var(--text-soft); font-style: italic; }
```

### 6.11 Modal

```css
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(43, 31, 21, 0.28);
  backdrop-filter: blur(6px);
  z-index: 400;
}
.modal {
  position: fixed; left: 50%; top: 50%;
  transform: translate(-50%, -50%);
  width: min(500px, calc(100vw - 24px));
  max-height: calc(100vh - 40px);
  overflow-y: auto;
  background: var(--paper-strong);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow);
  padding: 24px;
  z-index: 500;
}
```

Modal content order: title → subtitle (muted, 13px) → form grid → actions (right-aligned).

### 6.12 Empty state

```css
.empty-state {
  padding: 48px 16px;
  text-align: center;
  color: var(--text-soft);
  font-size: 13px;
}
```

---

## 7. Interaction rules

| Trigger | Effect |
|---------|--------|
| `input:focus` / `select:focus` | `border-color: var(--line-strong)` + `box-shadow: 0 0 0 3px rgba(188,92,56,0.1)` |
| Button hover | See per-variant rules above. Prefer `opacity` change over color swap for primary. |
| Row hover | `background: var(--bg-soft)` — never `cursor: default` on clickable rows |
| Active row | Accent left border `box-shadow: inset 3px 0 0 var(--accent)` |
| Custom select open | Arrow turns accent-colored, trigger gets focus ring |
| Session item hover | Reveal edit/delete icons via `opacity` transition |

Transition duration: `0.12s–0.16s ease` for bg/color. `0.15s` for borders. Never exceed `0.2s` for micro-interactions.

---

## 8. Scrollbar styling (webkit)

```css
::-webkit-scrollbar        { width: 5px; height: 5px; }
::-webkit-scrollbar-track  { background: transparent; }
::-webkit-scrollbar-thumb  { background: var(--line-strong); border-radius: 99px; }
```

Firefox: `scrollbar-width: thin; scrollbar-color: var(--line-strong) transparent;`

---

## 9. Anti-patterns — do NOT do these

| Wrong | Correct |
|-------|---------|
| Raw `<select>` for user-facing filters | `CustomSelect` component |
| `<pre>` block for JSON data | `makeJsonViewer()` |
| Raw markdown syntax in table previews | `stripMarkdown()` before `escapeHtml()` |
| `display: grid` for the workspace | `display: flex` |
| Hardcoded hex colors in component CSS | `var(--token)` |
| Three-always-visible columns in table | Session-key column can hide when session is active |
| `font-family: monospace` | `font-family: var(--mono)` |
| `alert()` for error messages | Inline error in modal / toast pattern |
| `width: 100%` on topbar selects | `width: auto` (let content size it) |
| Showing edit/delete buttons always in session list | Show on `:hover` via `opacity` only |
| `background: blue` or OS-native select options | Custom dropdown with warm palette |
| `border-radius: 4px` for pills/badges | `border-radius: 999px` |
| `font-weight: bold` | `font-weight: 600` or `700` — never keyword `bold` |
| `z-index: 9999` | Modals at `500`, dropdowns at `200`, topbar at `3` |

---

## 10. File structure

```
static/dashboard/
  index.html        — shell HTML, no logic, no inline styles
  styles.css        — all styles, variables at :root
  app.js            — all JS: state, API calls, render functions, utilities
  DESIGN_SPEC.md    — this file
```

**Rule:** never add a fourth JS file. Keep everything in `app.js`. Split only if it exceeds ~1200 lines.

---

## 11. Body background

```css
body {
  background:
    radial-gradient(circle at top left, rgba(188, 92, 56, 0.1), transparent 24%),
    radial-gradient(circle at bottom right, rgba(47, 125, 98, 0.09), transparent 22%),
    var(--bg);
}
```

This warm gradient is intentional — it ties the accent and green semantic colors into the background. Do not replace with a flat color.
