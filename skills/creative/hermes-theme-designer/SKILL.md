---
name: hermes-theme-designer
description: Design and install radical Hermes CLI skins/themes.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, themes, skins, cli, customization, ascii-art, branding, colors, creative]
    category: creative
    related_skills: [claude-design, popular-web-designs, design-md, ascii-art]
---

# Hermes Theme Designer

Design fully custom Hermes CLI skins, from minimal color tweaks to radical visual overhauls with custom ASCII art banners, mythological branding, and personality-infused spinners. This skill covers the complete skin YAML schema, cohesive palette strategies, Rich-markup ASCII art, and the install/preview workflow.

Skins are **pure data** — no code changes, just a YAML file dropped in `~/.hermes/skins/` and activated with `/skin <name>`.

## When to Use

- User wants a new CLI theme or skin for Hermes
- User asks to "make it look like X" (a brand, a character, a vibe, an era)
- User wants something dramatic — new agent identity, custom ASCII banner, themed spinner faces
- User wants to tweak an existing skin (colors, branding, only what's needed)
- User asks to list, preview, or manage installed skins

## Prerequisites

- Hermes CLI running (any model)
- `~/.hermes/skins/` directory (created automatically on first save)
- No API keys or packages needed

## How to Run

```
Load this skill, then describe the theme you want:

  "Create a cyberpunk theme with neon pink and cyan"
  "Make a theme based on the Matrix — green rain on black"
  "Give me a cozy autumn harvest skin with warm browns"
  "I want something completely unhinged — a deep-sea horror theme"
```

The agent will produce a complete YAML file and install it.

## Quick Reference

```bash
/skin                      # list available skins + show active
/skin <name>               # switch to a skin
display.skin: <name>       # set default in ~/.hermes/config.yaml
```

Skin YAML lives at: `~/.hermes/skins/<name>.yaml`

## Procedure

### 1. Creative Brief

Before producing the YAML, extract:

- **Concept / Vibe** — mythological figure, film, era, aesthetic movement, brand, animal, emotion
- **Terminal background** — dark (default) or light? Dark skins use bright-on-dark; light skins need dark text.
- **Radical or subtle?** — full identity overhaul (new agent name, custom ASCII art, custom spinner faces) vs. palette-only tweak
- **Inspiration palette** — hex codes, image description, brand reference, or leave it to the designer

If the user is vague, pick an opinionated interpretation and declare it upfront. Do not ask multiple clarifying questions; make a design decision.

### 2. Color Design

Build a palette with **role-based reasoning**, not random color picking:

| Role | Keys | Purpose |
|------|------|---------|
| Primary structural | `banner_border`, `input_rule` | Frames and dividers — use the theme's dominant accent |
| Title / hero | `banner_title`, `response_border` | The most visible text — use the brightest/warmest tone |
| Accent / highlight | `banner_accent`, `ui_accent` | Section headers, interactive elements |
| Body text | `banner_text`, `prompt` | Everything you read — ensure >4.5:1 contrast on terminal bg |
| Dim / muted | `banner_dim`, `session_border` | Separators, secondary labels — 30–50% brightness of body |
| Status bar | `status_bar_bg`, `status_bar_text`, `status_bar_strong` | Background darker than terminal bg; text legible |
| Semantic | `ui_ok`, `ui_error`, `ui_warn` | Green/red/orange — adapt hue to theme but keep intent clear |
| Completion menus | `completion_menu_bg`, `completion_menu_current_bg` | Slightly lighter or darker than `status_bar_bg` |

**Color rules for radical themes:**

- Monochromatic palettes: vary lightness ±40% across roles, never use the same value for two roles.
- Complementary palettes: one dominant hue for structure, the complement for accents only.
- Neon / cyberpunk: `banner_bg` near `#000000`; saturated neon (`#FF00FF`, `#00FFFF`) for title/border only; body text must be off-white or pale tint of the neon, not the neon itself.
- Light-mode skins: set `banner_text` and `prompt` to dark values (`#1a1a1a`–`#4a4a4a`); set `completion_menu_bg` to near-white (`#f0f0f0`).
- Avoid pure `#000000` borders — use `#0a0a14` or similar; pure black borders disappear in most terminals.

### 3. Spinner Personality

Spinners carry the theme's personality. For radical themes, customize all four keys:

```yaml
spinner:
  waiting_faces:        # Shown while waiting for API response (idle cycles)
    - "(●)"
    - "(◉)"
    - "(○)"
  thinking_faces:       # Shown during active reasoning / tool calls
    - "(◈)"
    - "(◇)"
    - "(◆)"
  thinking_verbs:       # Rotate through these — match theme tone and voice
    - "calculating the odds"
    - "scanning the noosphere"
    - "tuning the frequency"
  wings:                # Optional left/right brackets around the spinner face
    - ["⟦⚡", "⚡⟧"]   # Each entry is [left_wing, right_wing]
    - ["⟦●", "●⟧"]
```

**Verb writing rules:**
- 6–12 verbs minimum; more = more varied feel
- Use gerund phrases ("hacking the grid", not "hacks")
- Match the theme's voice: war-god is terse/aggressive; ocean is slow/vast; horror is unsettling
- Avoid generic AI verbs: "processing", "analyzing", "computing" — those belong in `mono`, not a radical theme

**Face rules:**
- Use Unicode block/geometric/braille chars for abstract faces
- For character themes, use thematic symbols (⚔ war, ≋ water, ✦ space, 🕸 horror)
- Waiting faces: 3–5 entries, simple rotation
- Thinking faces: 4–6 entries, can be more varied

### 4. Branding

For full identity overhauls, change all branding keys:

```yaml
branding:
  agent_name: "Kraken Agent"
  welcome: "The deep stirs. Type your query or /help for commands."
  goodbye: "Returning to the abyss. ≋"
  response_label: " ≋ Kraken "
  prompt_symbol: "≋"
  help_header: "(≋) Available Commands"
```

Rules:
- `response_label` has leading/trailing spaces — keep them
- `prompt_symbol` is a bare token; the renderer adds a trailing space — don't add one
- `welcome` can be evocative/dramatic; it's shown once at startup
- `goodbye` is shown once on exit — give it a sendoff that fits the character

### 5. ASCII Art Banners

The two optional banner overrides use **Rich markup** (`[bold #hex]...[/]`):

| Key | Replaces | Size |
|-----|----------|------|
| `banner_logo` | The `HERMES AGENT` title text | ~6 lines of block font |
| `banner_hero` | The caduceus ASCII art | 10–15 lines of art |

**banner_logo**: Use block-font ASCII (like figlet `big` or `doom` style). Apply a horizontal color gradient by coloring each line slightly differently along the theme's hue.

```yaml
banner_logo: |
  [bold #FF0080] ██████╗ ██████╗[/]
  [bold #CC0066]██╔═══██╗██╔══██╗[/]
  [#990044]██║   ██║██████╔╝[/]
  [#770033]██║   ██║██╔══██╗[/]
  [#550022]╚██████╔╝██║  ██║[/]
  [#330011] ╚═════╝ ╚═╝  ╚═╝[/]
```

**banner_hero**: Use Braille/block chars for detailed art, or simple ASCII for symbolic art. Color each row with the theme palette.

```yaml
banner_hero: |
  [#0088FF]⠀⠀⠀⠀⠀⣀⣤⣤⣤⣀⠀⠀⠀⠀⠀[/]
  [#0066CC]⠀⠀⠀⣴⣿⡿⠛⠛⠛⢿⣷⡄⠀⠀⠀[/]
  [#004499]⠀⠀⣾⣿⠋⠀⠀≋⠀⠀⠙⣿⣷⠀⠀[/]
  [dim #003377]⠀⠀⠀the deep remembers⠀⠀⠀[/]
```

**Rich markup rules for banners:**
- Every colored segment must have a closing `[/]`
- `[bold #hex]` for bright lines; `[#hex]` for normal; `[dim #hex]` for muted
- The YAML `|` block scalar preserves newlines — do not add `\n` manually
- Test for line length: terminal widths vary. Keep art under 60 chars wide.
- `banner_logo` should not have trailing spaces on each line.

### 6. tool_prefix

Change `tool_prefix` to reinforce the theme:

| Theme | Prefix |
|-------|--------|
| Default | `┊` |
| Military / war | `╎` |
| Minimal | `│` |
| Heavy / dramatic | `║` |
| Neon / cyber | `▏` |
| Ocean / organic | `╰` |

### 7. Full YAML Template

```yaml
# ~/.hermes/skins/<name>.yaml
name: <name>
description: <short description>

colors:
  banner_border: "#..."
  banner_title: "#..."
  banner_accent: "#..."
  banner_dim: "#..."
  banner_text: "#..."
  ui_accent: "#..."
  ui_label: "#..."
  ui_ok: "#4caf50"         # keep semantic green unless theme demands override
  ui_error: "#ef5350"      # keep semantic red
  ui_warn: "#ffa726"       # keep semantic orange
  prompt: "#..."
  input_rule: "#..."
  response_border: "#..."
  session_label: "#..."
  session_border: "#..."
  status_bar_bg: "#..."
  status_bar_text: "#..."
  status_bar_strong: "#..."
  status_bar_dim: "#..."
  status_bar_good: "#..."
  status_bar_warn: "#..."
  status_bar_bad: "#..."
  status_bar_critical: "#..."
  voice_status_bg: "#..."
  selection_bg: "#..."
  completion_menu_bg: "#..."
  completion_menu_current_bg: "#..."
  completion_menu_meta_bg: "#..."
  completion_menu_meta_current_bg: "#..."

spinner:
  waiting_faces: ["(●)", "(◉)", "(○)"]
  thinking_faces: ["(◈)", "(◇)", "(◆)", "(◉)"]
  thinking_verbs:
    - "verb one"
    - "verb two"
  wings:
    - ["⟦◈", "◈⟧"]

branding:
  agent_name: "Name Agent"
  welcome: "Welcome message."
  goodbye: "Goodbye message. <symbol>"
  response_label: " <symbol> Name "
  prompt_symbol: "<symbol>"
  help_header: "(<symbol>) Available Commands"

tool_prefix: "┊"

tool_emojis: {}    # Optional: {terminal: "⚔", web_search: "🔮", ...}

# banner_logo: |
#   [bold #hex] LOGO ART [/]
# banner_hero: |
#   [#hex] HERO ART [/]
```

### 8. Install and Activate

After producing the YAML:

1. Write it to `~/.hermes/skins/<name>.yaml` using `write_file`
2. Confirm: `read_file ~/.hermes/skins/<name>.yaml` to verify the write
3. Tell the user to run `/skin <name>` in their Hermes session to activate it
4. Or set it permanently: `hermes config set display.skin <name>`

## Pitfalls

- **Broken Rich markup**: every `[bold #hex]` or `[#hex]` needs a closing `[/]`. Missing closers corrupt the entire banner render.
- **banner_logo line drift**: lines that start with spaces in the YAML `|` block will shift right. Align the art flush to the YAML indent level.
- **Unreadable body text**: saturated neon as body text (`banner_text`, `prompt`) causes eye strain and fails contrast. Use pale tints or off-white for body text, reserve neon for borders and titles only.
- **Identical bg colors for completion menu**: if `completion_menu_bg` equals `completion_menu_current_bg`, the active row is invisible. Always use ±20% lightness difference.
- **Too-short thinking_verbs list**: with only 2–3 verbs the spinner feels repetitive in long sessions. Write at least 8.
- **prompt_symbol with trailing space**: the renderer adds a space — don't add one in YAML, or input will be double-spaced.
- **Light-mode skins with dark `banner_text`**: verify `banner_text` is dark enough to read on the light terminal bg; `#333333` or darker is safe.

## Verification

```bash
# List installed skins
/skin

# Activate
/skin <name>

# Check the YAML is valid
python3 -c "import yaml; yaml.safe_load(open('/home/<user>/.hermes/skins/<name>.yaml'))"

# Inspect the active skin in Python (advanced)
python3 -c "
from hermes_cli.skin_engine import load_skin
s = load_skin('<name>')
print(s.colors)
print(s.branding)
"
```

A successful theme looks right immediately on `/skin <name>`. Check:
- Banner colors match the intended palette
- Spinner faces/wings appear during the next tool call
- Response box border uses `response_border`
- Input prompt symbol is correct
- Completion menu is readable (open with Tab)
