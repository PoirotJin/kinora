# MotionFarm Review — Design System

Apple-store-inspired light system for the MotionFarm text–motion data platform.
Single accent, restrained chrome, data and motion previews carry the visual weight.

## 0. Research Log

- Layer A: `redesign-skill.md` (audit-first upgrade of existing UI; fix priority: type → palette → states → layout → components → states → polish).
- Layer B: `apple.md` (Apple design system tokens: neutral triad, SF Pro scale, `#0071e3` action blue, radius tiers, restrained depth).
- Existing UI audit: three stacked CSS eras (dark theme → "light studio" override → feature accretion), 10+ near-identical grays, 8 radius values, no shadows, no press states, generic type hierarchy. Before-screenshots: `/home/humanoid/.playwright-mcp/mf-shots/before-*.png`.
- Lazyweb / imagen lanes: skipped — concrete brand reference (Apple) already chosen by user; internal tool, no marketing surface.

## 1. Color Tokens

| Token | Value | Use |
|---|---|---|
| `--bg` | `#ffffff` | page canvas |
| `--panel` | `#f5f5f7` | pale gray chapters, sidebars, KPI cards, prompt card |
| `--panel-raised` | `#ffffff` | cards on gray, popovers |
| `--line` | `#d2d2d7` | hairline borders |
| `--line-soft` | `#e8e8ed` | card hairlines, row separators |
| `--text` | `#1d1d1f` | primary ink |
| `--text-2` | `#6e6e73` | secondary copy, metadata |
| `--text-3` | `#86868b` | tertiary, placeholders, captions |
| `--accent` | `#0071e3` | actions, active states, focus |
| `--accent-hover` | `#0066cc` | primary hover / links |
| `--accent-tint` | `#e8f0fd` | selected backgrounds |
| `--ok` / `--ok-bg` | `#1d7a3d` / `#e5f4ea` | accepted, healthy |
| `--warn` / `--warn-bg` | `#8a6100` / `#fdf3e0` | uncertain, warning |
| `--bad` / `--bad-bg` | `#b3261e` / `#fdecea` | rejected, critical |
| `--info-bg` | `#e8f0fd` / text `#0b5394` | notice banner |

One accent only. Skeleton teal/orange and chart blue/teal live inside JS canvases — out of CSS scope, unchanged.

## 2. Typography

Stack: `-apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", "Segoe UI", "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif`.

| Role | Size/Weight | Extras |
|---|---|---|
| Gallery display | 56/650 | `letter-spacing:-.022em; line-height:1.07` |
| Page h1 | 32/600 | `letter-spacing:-.02em; line-height:1.12` |
| Panel h2 | 17/600 | `letter-spacing:-.01em` |
| Body | 14/400 | `line-height:1.55` |
| Secondary/meta | 12–13/400 | `--text-2` |
| Eyebrow | 11/600 | `letter-spacing:.14em; uppercase; --text-3` |
| Data numerals | — | `font-variant-numeric:tabular-nums` on KPIs, timecode, tables |

Headings use `text-wrap:balance`. Long English prompts `overflow-wrap:anywhere` where already present.

## 3. Geometry & Depth

- Radius tiers: **8** small controls/inputs-in-tables · **10** buttons/inputs · **12** cards/thumbnails · **18** panels/login card · **980px** pills (badges, primary CTA optional).
- Hairlines over shadows at rest: `1px solid var(--line-soft)`.
- Single soft shadow for elevation/hover: `0 1px 2px rgba(0,0,0,.04), 0 12px 32px rgba(0,0,0,.08)`.
- Header: 60px, `rgba(255,255,255,.8)` + `backdrop-filter:saturate(180%) blur(20px)`, hairline bottom, sticky.
- Spacing: 8px base unit; page padding 32–40px desktop; section gaps 32–48px.

## 4. Components

- **Buttons**: default = `#f5f5f7` bg, ink text, 10px radius, 1px transparent border; hover `#e8e8ed`; active `transform:scale(.97)`; transition 160ms `background,border-color,transform,box-shadow`. `.primary` = accent fill, white, hover `--accent-hover`. `.quiet` = transparent, `--text-2`, hover ink. Disabled: opacity .4, no transform.
- **Focus**: `:focus-visible` → `outline:2px solid rgba(0,113,227,.55); outline-offset:2px` (or 3px box-shadow ring on inputs).
- **Inputs/selects/textarea**: white bg, `1px var(--line)`, 10px radius, padding 9px 12px; focus border accent + `0 0 0 3px rgba(0,113,227,.18)`. Readonly = `#f5f5f7`.
- **Page nav**: text tabs, 15px, `--text-2`; hover ink; active = ink + 2px accent underline (inset, not layout-shifting); 52px bar, hairline bottom.
- **Badges**: pill, 11px/600, tinted bg + deep text per semantic (default gray `#f5f5f7`/`#6e6e73`).
- **Notice**: slim banner, `--info-bg`/`#0b5394`, error variant red; 13px, centered-left 32px padding.
- **Gallery**: sidebar quiet list — 14px rows, 10px radius, hover `#ececf0`, active = `--accent-tint` bg + accent text (no heavy blue fill). Cards: white, `--line-soft` hairline, 16px radius, thumb canvas `#f5f5f7` 12px radius; hover `translateY(-3px)` + soft shadow + hairline darkens; title 15/600, desc 13 `--text-2` 3-line clamp, meta 12 `--text-3`.
- **Review workspace**: 3-col grid; side panels `--panel`; motion cards white hairline 12px, active = `--accent-tint` + inset 3px accent bar; viewport `--panel` bg, 18px radius, hairline; transport controls quiet pills, play = accent text 600; prompt card `--panel`, 18px radius, borderless.
- **Monitor**: KPI cards `--panel` borderless 16px radius, value 26/600 tabular; tables = white, header 12px `--text-2` weight 500 bg `#fafafc`, rows hairline `--line-soft`, 13–14px tabular-nums; alerts = tinted banners per semantic, 12px radius.
- **Login**: centered card 400px, white, 18px radius, soft shadow, 40px padding; eyebrow + 28px h1.
- **Production/storage panels**: white, `--line-soft` hairline, 18px radius, 28px padding; step chips = `--panel` pills 12px; progress = accent.
- **Scrollbars** (webkit): 8px, thumb `#c7c7cc` rounded, transparent track.
- **Range input**: accent thumb, `#d2d2d7` track.

## 5. Motion

- Standard ease `cubic-bezier(.32,.72,.35,1)`; 160–220ms.
- Animate only `transform`, `opacity`, colors, `box-shadow`.
- Hover lifts: gallery card `translateY(-3px)`; press `scale(.97)` on buttons.
- `@media (prefers-reduced-motion:reduce)`: all transitions/animations off.

## 6. Responsive

- ≤1150px: gallery 2-col, sidebar 180px; ≤850px: production 1-col; ≤700px: header wraps, nav scrolls horizontally, gallery single column + horizontal topic chips, review grid stacks, monitor KPIs 2-col.
- Use existing breakpoints; do not regress current mobile behavior.

## 7. Accessibility Constraints

- Contrast: body text ≥ 4.5:1 on its surface; `--text-3` only for ≥12px non-essential meta.
- Every interactive element keeps a visible `:focus-visible` ring.
- Hit targets ≥ 32px on nav/buttons where layout allows.
- No information carried by color alone (badges keep text labels).

## 8. Accepted Debt

- Canvas-drawn colors (skeleton, charts, studio light) remain JS-side; palette harmonization of the 3D viewport is out of scope.
- Dark mode not pursued; `color-scheme: light` only.
- System font stack instead of licensed SF Pro; Inter not bundled to keep the app dependency-free.
- HTML structure unchanged except `<head>` additions (favicon, meta description, theme-color) — all JS selectors/IDs preserved.
