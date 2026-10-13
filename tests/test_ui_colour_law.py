"""Unit tests for the shared colour law (R-WEB-1, cross-surface).

The law is stated in ``docs/UI_UX_GUIDE.md`` §1 and its token table lives in
``docs/MEMORY_EDITOR_UX.md`` §4, and it is a product requirement that every
surface expresses it the same way: the web panel, the Android companion and the
desktop shell.  These tests pin the load-bearing **roles and values**, not the
shape of a stylesheet, so a legitimate restyle passes and a real drift fails.

Three things are held firm here:

* **Greys carry the structure and one pink accent carries emphasis.**  The
  accent is the only hue among the neutrals, and the status colours keep their
  own hues: success green, warning amber, danger red.  Pink never stands in for
  a status colour.
* **The light-theme accent is ``#b93e83``, not ``#cf4b92``.**  That darker pink
  reaches 4.72:1 on the light background where ``#cf4b92`` reaches only 3.79:1.
  It is a decision, and the test pins the decision rather than correcting it.
* **Claire is pink and the owner is neutral grey.**  In the conversation view
  the assistant carries the accent and the owner a grey; success green is never
  a speaker colour on either surface.

Nothing here starts a server or a device: the panel stylesheet is read from the
page the panel actually renders, the Android side from its resource files and
its conversation adapter, and the desktop side from the token block and the
stylesheets and paint code its modules hand to Qt.  The gate is
``pytest tests/ -m unit`` and it SILENTLY DESELECTS a file with no unit marker,
hence ``pytestmark = pytest.mark.unit`` below.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from claire.panel.server import (
    ROUTE_INDEX,
    PanelRequest,
    TurnRecord,
    _turn_html,
    dispatch,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
ANDROID_RES = REPO_ROOT / "android/app/src/main/res"
ANDROID_COLOURS = REPO_ROOT / "android/app/src/main/res/values/colors.xml"
ANDROID_THEMES = REPO_ROOT / "android/app/src/main/res/values/themes.xml"
ANDROID_CAMERA_LAYOUT = ANDROID_RES / "layout/activity_camera.xml"
CONVERSATION_ADAPTER = (
    REPO_ROOT
    / "android/app/src/main/java/com/claire/companion/ConversationAdapter.java"
)

# --------------------------------------------------------------------------- #
# The law, as values.  Verified against the panel's served stylesheet and the
# Android resources; where the prose table and the files disagree, the files win
# and the disagreement is reported.
# --------------------------------------------------------------------------- #

DARK_LAW = {
    "--bg": "#131417",
    "--card": "#1b1d21",
    "--card-2": "#24262b",
    "--ink": "#e8eaed",
    "--ink-soft": "#a8adb5",
    "--ink-faint": "#878d96",
    # --line is the stronger component border and --line-soft the hairline; the
    # token names carry that order in both the panel and the Android outline.
    "--line": "#31343a",
    "--line-soft": "#26282d",
    "--accent": "#e562a4",
    "--accent-ink": "#150a12",
    "--ok": "#4cc38a",
    "--warn": "#d9a441",
    "--bad": "#ef6a6a",
}

LIGHT_LAW = {
    "--bg": "#f4f5f7",
    "--card": "#ffffff",
    "--card-2": "#f7f8fa",
    "--ink": "#16181c",
    "--ink-soft": "#4a4f57",
    "--ink-faint": "#6b7078",
    "--line": "#d5d8dd",
    "--line-soft": "#e4e6ea",
    "--accent": "#b93e83",
    "--accent-ink": "#ffffff",
}

# The values the two surfaces must agree on, as (panel token, Android resource).
ANDROID_LAW = {
    "bg": "--bg",
    "surface": "--card",
    "surface_alt": "--card-2",
    "outline": "--line",
    "text": "--ink",
    "text_dim": "--ink-soft",
    "accent": "--accent",
    "on_accent": "--accent-ink",
    "ok": "--ok",
    "warn": "--warn",
    "error": "--bad",
}

# The only hex literals the Android resource tree may carry outside colors.xml.
# The law's own values never appear here: the camera's two scrims reach their
# layout as @color/bg_scrim (the law's bg at the alpha a layout needs) and
# @color/scrim (neutral black, which is not a palette hue).  Neutral white is
# not a law colour on this dark-only surface either -- the text role is #E8EAED
# -- so the vector icons may keep it.  The set is closed: a new literal fails
# until it is named in colors.xml or added here with a reason.
ANDROID_NEUTRAL_HEXES = frozenset({"#ffffffff"})

STRUCTURE_ROLES = (
    "--bg",
    "--card",
    "--card-2",
    "--ink",
    "--ink-soft",
    "--ink-faint",
    "--line",
    "--line-soft",
)
STATUS_ROLES = ("--ok", "--warn", "--bad")

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CUSTOM_PROP = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;]+)")
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# Source scans use the six-digit form only: the shorter form cannot tell a
# colour apart from an issue reference such as ``#576`` in a docstring.
_HEX6 = re.compile(r"#[0-9a-fA-F]{6}\b")
# Android resources write a colour as #RRGGBB, or #AARRGGBB when it is
# translucent, so the resource scan accepts both forms.
_ANDROID_HEX = re.compile(r"#[0-9a-fA-F]{6,8}\b")
_VAR = re.compile(r"var\((--[A-Za-z0-9_-]+)\)")
_BORDER_LEFT = re.compile(r"border-left\s*:\s*([^;]+)")
_ANDROID_CASE = re.compile(
    r"case\s+([A-Z_]+)\s*:(.*?)(?=\n\s*(?:case\s+[A-Z_]+|default)\s*:|\n\s*\})",
    re.S,
)

# --------------------------------------------------------------------------- #
# Parsing helpers.  The stylesheet is read as text and split into declaration
# blocks, so reordering or reformatting the CSS does not fail a test.
# --------------------------------------------------------------------------- #


def _stylesheet() -> str:
    """The stylesheet the panel actually serves, from its rendered index page."""
    response = dispatch(object(), PanelRequest(method="GET", path=ROUTE_INDEX))
    assert response.status == 200
    match = re.search(r"<style>(.*?)</style>", response.text, re.S)
    assert match, "the panel renders no inline stylesheet"
    return match.group(1)


def _rules(css: str) -> list[tuple[str, str, str | None]]:
    """Every declaration block in *css* as ``(selector, body, media)``.

    At-rules are tracked so a ``:root`` nested in a media query is reported with
    that query rather than flattened into the top level.
    """
    css = _COMMENT.sub("", css)
    rules: list[tuple[str, str, str | None]] = []
    stack: list[tuple[str, bool]] = []
    buffer = ""
    for char in css:
        if char == "{":
            stack.append((buffer.strip(), buffer.strip().startswith("@")))
            buffer = ""
        elif char == "}":
            prelude, is_at_rule = stack.pop()
            if not is_at_rule:
                media = next((p for p, at in stack if at), None)
                rules.append((prelude, buffer, media))
            buffer = ""
        else:
            buffer += char
    return rules


def _declarations(body: str) -> dict[str, str]:
    return {name: value.strip() for name, value in _CUSTOM_PROP.findall(body)}


def _theme(css: str, scheme: str) -> dict[str, str]:
    """The effective custom properties for *scheme* after the cascade."""
    tokens: dict[str, str] = {}
    for selector, body, media in _rules(css):
        if selector != ":root":
            continue
        in_light_query = media is not None and "light" in media
        if scheme == "light" or not in_light_query:
            tokens.update(_declarations(body))
    return tokens


def _rgb(value: str) -> tuple[int, int, int]:
    digits = value.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(part * 2 for part in digits)
    if len(digits) == 8:
        digits = digits[2:]  # Android writes an opaque alpha channel first
    return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _luminance(value: str) -> float:
    """Perceived lightness of a colour, 0.0 (black) to 1.0 (white)."""
    r, g, b = _rgb(value)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _is_achromatic(value: str) -> bool:
    """True for a grey: the channels are close and no hue dominates."""
    r, g, b = _rgb(value)
    return max(r, g, b) - min(r, g, b) <= 16


def _is_pink(value: str) -> bool:
    r, g, b = _rgb(value)
    return r > b > g and g < 150


def _is_green(value: str) -> bool:
    r, g, b = _rgb(value)
    return g > r and g > b


def _is_amber(value: str) -> bool:
    r, g, b = _rgb(value)
    return r > g > b


def _is_red(value: str) -> bool:
    r, g, b = _rgb(value)
    return r > g and r > b and abs(g - b) <= 30


def _resolve(value: str, tokens: dict[str, str]) -> str:
    """Resolve a ``var(--token)`` reference, leaving anything else unchanged."""
    match = _VAR.search(value)
    return tokens.get(match.group(1), value) if match else value.strip()


def _android_colours() -> dict[str, str]:
    root = ET.parse(ANDROID_COLOURS).getroot()
    return {el.get("name"): (el.text or "").strip() for el in root.findall("color")}


def _android_res_sources() -> list[Path]:
    """Every XML resource outside the palette, so a new file is covered."""
    return sorted(
        path for path in ANDROID_RES.rglob("*.xml") if path != ANDROID_COLOURS
    )


# --------------------------------------------------------------------------- #
# 1. The panel's palette
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("token,value", sorted(DARK_LAW.items()))
def test_panel_dark_roles_hold_the_canonical_values(token, value):
    """Each dark-theme role resolves to its canonical value."""
    assert _theme(_stylesheet(), "dark")[token].lower() == value


@pytest.mark.parametrize("token,value", sorted(LIGHT_LAW.items()))
def test_panel_light_roles_hold_the_canonical_values(token, value):
    """Each light-theme role resolves to its canonical value."""
    assert _theme(_stylesheet(), "light")[token].lower() == value


def test_panel_light_accent_is_the_deliberate_darker_pink():
    """The light accent is #b93e83 on contrast grounds, and is not #cf4b92."""
    stylesheet = _stylesheet()
    light = _theme(stylesheet, "light")
    assert light["--accent"].lower() == "#b93e83"
    assert light["--accent"].lower() != "#cf4b92"
    # The same roles read differently per scheme; that is the point of the pair.
    assert light["--accent"] != _theme(stylesheet, "dark")["--accent"]


def test_panel_structure_is_grey_and_the_accent_is_pink():
    """Structure is achromatic everywhere and the accent is the only pink."""
    for scheme in ("dark", "light"):
        tokens = _theme(_stylesheet(), scheme)
        for role in STRUCTURE_ROLES:
            assert _is_achromatic(tokens[role]), f"{scheme} {role}"
        assert _is_pink(tokens["--accent"]), scheme
        for role in STATUS_ROLES:
            assert not _is_pink(tokens[role]), f"{scheme} {role}"


def test_panel_status_colours_keep_their_own_hues():
    """Success stays green, warning amber and danger red in both schemes."""
    for scheme in ("dark", "light"):
        tokens = _theme(_stylesheet(), scheme)
        assert _is_green(tokens["--ok"]), scheme
        assert _is_amber(tokens["--warn"]), scheme
        assert _is_red(tokens["--bad"]), scheme


def test_panel_accent_is_distinct_from_every_status_colour():
    """Pink never doubles as success, warning or danger."""
    for scheme in ("dark", "light"):
        tokens = _theme(_stylesheet(), scheme)
        for role in STATUS_ROLES:
            assert tokens["--accent"] != tokens[role], f"{scheme} {role}"


def test_panel_light_theme_is_actually_light():
    """The light scheme lightens the background rather than restating dark."""
    dark = _theme(_stylesheet(), "dark")
    light = _theme(_stylesheet(), "light")
    assert _luminance(light["--bg"]) > 0.8
    assert _luminance(dark["--bg"]) < 0.2
    assert _luminance(light["--card"]) > _luminance(dark["--card"])
    assert _luminance(light["--ink"]) < _luminance(dark["--ink"])


# --------------------------------------------------------------------------- #
# 2. No raw hex outside the token blocks
# --------------------------------------------------------------------------- #


def test_panel_component_css_contains_no_raw_hex():
    """Every raw hex lives in a :root token block, never in a component rule.

    The rule is §4 of ``docs/MEMORY_EDITOR_UX.md``: components may only refer to
    tokens, so the palette stays single-sourced.  The light override is itself a
    token block and is allowed to carry raw values.
    """
    offenders = [
        selector
        for selector, body, _media in _rules(_stylesheet())
        if selector != ":root" and _HEX.search(body)
    ]
    assert offenders == []


# --------------------------------------------------------------------------- #
# 3. The Android mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("resource,token", sorted(ANDROID_LAW.items()))
def test_android_resources_carry_the_same_law(resource, token):
    """The Android role of a name carries the panel's value for the same role."""
    colours = _android_colours()
    assert resource in colours, f"Android defines no {resource} colour"
    assert _rgb(colours[resource]) == _rgb(DARK_LAW[token]), resource


def test_android_claire_is_the_accent_and_you_is_neutral_grey():
    """The two speaker colours hold their parts: Claire pink, owner grey."""
    colours = _android_colours()
    assert colours["claire"] == colours["accent"]
    assert _is_pink(colours["claire"])
    assert colours["you"] == colours["text_dim"]
    assert _is_achromatic(colours["you"])


def test_android_accent_has_the_pressed_step():
    """``accent_dim`` is the darker pressed accent, #cf4b92."""
    colours = _android_colours()
    assert _rgb(colours["accent_dim"]) == (0xCF, 0x4B, 0x92)


def test_android_pink_is_not_the_error_colour():
    """The theme's error role resolves to the red status hue, not the accent."""
    theme = ET.parse(ANDROID_THEMES).getroot()
    items = {
        el.get("name"): (el.text or "").strip()
        for el in theme.iter("item")
        if el.get("name")
    }
    assert items["colorPrimary"] == "@color/accent"
    assert items["colorError"] == "@color/error"
    colours = _android_colours()
    assert _is_red(colours["error"])
    assert colours["error"] != colours["accent"]


def test_android_resource_tree_keeps_law_hexes_in_colors_xml():
    """A palette value is defined in colors.xml and reached by reference.

    The Android counterpart of the desktop confinement in section 5: no layout
    or drawable may restate a law colour.  The camera's two scrims are named in
    colors.xml rather than written into the layout, and the only literal the
    tree may hold is the neutral white on the icons.
    """
    law_rgb = {_rgb(value) for value in _android_colours().values() if value}
    offenders: list[str] = []
    neutrals: set[str] = set()
    for path in _android_res_sources():
        for literal in _ANDROID_HEX.findall(path.read_text(encoding="utf-8")):
            if _rgb(literal) in law_rgb:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {literal}")
            else:
                neutrals.add(literal.lower())
    assert offenders == []
    assert neutrals <= ANDROID_NEUTRAL_HEXES, sorted(
        neutrals - ANDROID_NEUTRAL_HEXES
    )


def test_android_camera_scrims_are_named_colours():
    """The camera's overlays are names, not values repeated in the layout.

    ``bg_scrim`` carries the law's bg and ``scrim`` stays neutral black: a scrim
    is a shadow, not a hue, so neither is derived from the accent.
    """
    colours = _android_colours()
    assert _rgb(colours["bg_scrim"]) == _rgb(DARK_LAW["--bg"])
    assert _rgb(colours["scrim"]) == (0, 0, 0)
    layout = ANDROID_CAMERA_LAYOUT.read_text(encoding="utf-8")
    assert "@color/bg_scrim" in layout
    assert "@color/scrim" in layout


# --------------------------------------------------------------------------- #
# 4. Claire is pink, the owner is grey, success marks neither
# --------------------------------------------------------------------------- #


def test_panel_turns_emit_the_claire_and_you_roles():
    """The conversation markup labels the two speakers distinctly."""
    claire = _turn_html(TurnRecord(role="claire", text="hello"))
    you = _turn_html(TurnRecord(role="you", text="hi"))
    assert 'class="turn claire"' in claire
    assert 'class="turn you"' in you


def _speaker_border(css: str, role: str) -> str:
    tokens = _theme(css, "dark")
    matches = [
        body
        for selector, body, _media in _rules(css)
        if re.search(rf"\.turn\.{role}\b", selector)
    ]
    assert matches, f"no rule colours the {role} turn"
    declaration = _BORDER_LEFT.search(matches[0])
    assert declaration, f"the {role} turn carries no border-left colour"
    return _resolve(declaration.group(1), tokens)


def test_panel_claire_is_pink_and_the_owner_is_grey():
    """In the conversation the assistant takes the accent, the owner a grey."""
    css = _stylesheet()
    claire = _speaker_border(css, "claire")
    you = _speaker_border(css, "you")
    assert _is_pink(claire)
    assert _is_achromatic(you)
    assert claire != you


def test_panel_success_colour_never_marks_a_speaker():
    """No rule that colours a turn reaches for the success green."""
    css = _stylesheet()
    tokens = _theme(css, "dark")
    success = tokens["--ok"]
    for selector, body, _media in _rules(css):
        if not re.search(r"\.turn\b", selector):
            continue
        assert success not in body.lower(), selector
        for token in _VAR.findall(body):
            assert tokens.get(token, "").lower() != success, selector


def test_android_conversation_binds_the_speaker_colours_by_role():
    """The adapter gives Claire the accent and the owner the grey, never green."""
    source = CONVERSATION_ADAPTER.read_text(encoding="utf-8")
    blocks = {name: body for name, body in _ANDROID_CASE.findall(source)}
    assert "R.color.claire" in blocks["CLAIRE"]
    assert "R.color.you" in blocks["YOU"]
    for role in ("CLAIRE", "YOU"):
        assert "R.color.ok" not in blocks[role], role
    colours = _android_colours()
    assert colours["you"] != colours["ok"]
    assert colours["claire"] != colours["ok"]


# --------------------------------------------------------------------------- #
# 5. The desktop shell
#
# The desktop package holds the law once, in ``desktop_app.themes``, under its
# own Qt role names.  These tests read that token block and the stylesheets and
# paint code the package actually hands to Qt, plus the source files
# themselves: a hex that appears anywhere but the token block is drift.
# --------------------------------------------------------------------------- #

DESKTOP_PACKAGE = REPO_ROOT / "src/desktop_app"

# The desktop's role names bound to the same values the panel and Android use.
DESKTOP_LAW = {
    "bg_primary": DARK_LAW["--bg"],
    "bg_secondary": DARK_LAW["--card"],
    "bg_tertiary": DARK_LAW["--card-2"],
    "bg_card": DARK_LAW["--card"],
    # --surface-3 is not in the panel's block but is Android's surface_high and
    # the third grey in docs/MEMORY_EDITOR_UX.md §4.
    "bg_hover": "#2e3138",
    "accent_primary": DARK_LAW["--accent"],
    "accent_secondary": DARK_LAW["--accent"],
    # The pressed accent is Android's accent_dim; the panel does not define one.
    "accent_muted": "#cf4b92",
    "on_accent": DARK_LAW["--accent-ink"],
    "text_primary": DARK_LAW["--ink"],
    "text_secondary": DARK_LAW["--ink-soft"],
    "text_muted": DARK_LAW["--ink-faint"],
    "border": DARK_LAW["--line"],
    "border_soft": DARK_LAW["--line-soft"],
    "success": DARK_LAW["--ok"],
    "success_light": DARK_LAW["--ok"],
    "warning": DARK_LAW["--warn"],
    "warning_light": DARK_LAW["--warn"],
    "error": DARK_LAW["--bad"],
    "error_light": DARK_LAW["--bad"],
}

# Every value the desktop token block may hold: the law's hexes, or a tint of
# one of them.
DESKTOP_LAW_HEXES = (
    set(DARK_LAW.values())
    | set(LIGHT_LAW.values())
    | {"#2e3138", "#cf4b92"}
)


def _desktop_tokens() -> dict[str, str]:
    """The desktop surface's token block, as the Qt surfaces read it."""
    from desktop_app.themes import COLORS as tokens
    return tokens


def _desktop_sources() -> list[Path]:
    return sorted(
        path
        for path in DESKTOP_PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _rgba_rgb(value: str) -> str:
    """The ``r, g, b`` of an ``rgba(...)`` string, as lowercase hex."""
    parts = re.findall(r"[\d.]+", value)
    return "#" + "".join(f"{int(float(part)):02x}" for part in parts[:3])


@pytest.mark.parametrize("role,value", sorted(DESKTOP_LAW.items()))
def test_desktop_roles_hold_the_canonical_values(role, value):
    """Each Qt role resolves to the law's value."""
    assert _desktop_tokens()[role].lower() == value


def test_desktop_token_block_holds_only_law_colours():
    """Every desktop token is a law hex or a tint of one; nothing else."""
    law_rgb = {_rgb(value) for value in DESKTOP_LAW_HEXES}
    for role, value in _desktop_tokens().items():
        if value.startswith("#"):
            assert value in DESKTOP_LAW_HEXES, f"{role} = {value}"
        else:
            assert value.startswith("rgba("), f"{role} = {value}"
            assert _rgb(_rgba_rgb(value)) in law_rgb, f"{role} = {value}"


def test_desktop_stylesheets_hand_qt_only_law_colours():
    """The stylesheets Qt renders carry no hex outside the law."""
    from desktop_app.themes import CHAT_THEME_STYLESHEET, CLAIRE_THEME_STYLESHEET

    allowed = {value.lower() for value in DESKTOP_LAW_HEXES}
    for sheet in (CHAT_THEME_STYLESHEET, CLAIRE_THEME_STYLESHEET):
        for value in _HEX6.findall(sheet):
            assert value.lower() in allowed, value


def test_desktop_keeps_its_hex_in_the_themes_module():
    """No desktop module but the token block writes a hex of its own."""
    offenders: dict[str, set[str]] = {}
    for path in _desktop_sources():
        found = set(_HEX6.findall(path.read_text(encoding="utf-8")))
        if path.name == "themes.py":
            continue
        if found:
            offenders[path.name] = found
    assert offenders == {}


def test_desktop_themes_module_defines_only_law_hexes():
    """The one module allowed a hex holds the law's values and no others."""
    source = (DESKTOP_PACKAGE / "themes.py").read_text(encoding="utf-8")
    allowed = {value.lower() for value in DESKTOP_LAW_HEXES}
    for value in _HEX6.findall(source):
        assert value.lower() in allowed, value


def test_desktop_face_is_grey_with_a_pink_accent():
    """The desktop face is the panel's face: grey structure, pink glow."""
    from desktop_app.face_widget import LowPolyFaceWidget as Face

    assert Face.PRIMARY_COLOR.name().lower() == DARK_LAW["--ink"]
    assert Face.SECONDARY_COLOR.name().lower() == DARK_LAW["--ink-soft"]
    assert Face.GLOW_COLOR.name().lower() == DARK_LAW["--accent"]
    assert Face.BG_COLOR.name().lower() == DARK_LAW["--bg"]
    assert Face.GRID_COLOR.name().lower() == DARK_LAW["--line-soft"]
    for grey in (Face.PRIMARY_COLOR, Face.SECONDARY_COLOR, Face.BG_COLOR, Face.GRID_COLOR):
        assert _is_achromatic(grey.name()), grey.name()
    assert _is_pink(Face.GLOW_COLOR.name())


def test_desktop_conversation_marks_claire_pink_and_the_owner_grey():
    """Claire's bubble carries the accent, the owner's a neutral grey."""
    from desktop_app.chat_window import _BUBBLE_STYLES

    claire = {value.lower() for value in _HEX.findall(_BUBBLE_STYLES["assistant"])}
    owner = {value.lower() for value in _HEX.findall(_BUBBLE_STYLES["user"])}
    assert DARK_LAW["--accent"] in claire
    assert DARK_LAW["--accent-ink"] in claire
    assert DARK_LAW["--accent"] not in owner
    assert owner & {DARK_LAW["--card"], DARK_LAW["--card-2"], "#2e3138"}
    assert DARK_LAW["--ok"] not in claire | owner


def test_desktop_status_colours_keep_their_own_hues():
    """Success stays green, warning amber and danger red, and never pink."""
    tokens = _desktop_tokens()
    assert _is_green(tokens["success"])
    assert _is_amber(tokens["warning"])
    assert _is_red(tokens["error"])
    assert _is_pink(tokens["accent_primary"])
    for role in ("success", "warning", "error"):
        assert not _is_pink(tokens[role]), role
        assert tokens["accent_primary"] != tokens[role], role


def _role_marker() -> re.Pattern:
    """A ``{role}`` or ``{COLORS['role']}`` marker left in a rendered string."""
    roles = "|".join(sorted(_desktop_tokens()))
    return re.compile(r"\{COLORS\[[^\]]+\]|\{(?:" + roles + r")\}")


def _law_style_literals() -> list[tuple[str, int, str]]:
    """Every literal template the desktop modules pass to ``law_style``."""
    import ast

    found: list[tuple[str, int, str]] = []
    for path in _desktop_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            argument = node.args[0]
            if (
                name in ("law_style", "_style")
                and isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
            ):
                found.append((path.name, argument.lineno, argument.value))
    return found


def test_desktop_law_style_templates_resolve_completely():
    """Every marker in a ``law_style`` template resolves to a value.

    A template that keeps ``{COLORS['border']}`` or ``{border}`` after
    ``law_style`` hands Qt an invalid stylesheet, which costs a control its
    real size hint; this catches that class of bug.
    """
    from desktop_app.themes import law_style

    templates = _law_style_literals()
    assert templates, "no law_style templates found in the desktop package"
    for name, lineno, template in templates:
        leftover = _role_marker().findall(law_style(template))
        assert leftover == [], f"{name}:{lineno} leaves {leftover[:3]}"


def test_desktop_stylesheets_leave_no_marker_unresolved():
    """The stylesheets Qt is handed carry no unresolved role marker."""
    from desktop_app.themes import (
        CHAT_THEME_STYLESHEET,
        CLAIRE_THEME_STYLESHEET,
        WIZARD_STYLESHEET,
    )

    marker = _role_marker()
    for sheet in (CHAT_THEME_STYLESHEET, CLAIRE_THEME_STYLESHEET, WIZARD_STYLESHEET):
        assert marker.findall(sheet) == []


def test_desktop_memory_viewer_renders_the_law():
    """The memory viewer page renders law colours and leaves no marker."""
    from desktop_app.memory_viewer import index

    page = index()
    assert _role_marker().findall(page) == []
    allowed = {value.lower() for value in DESKTOP_LAW_HEXES}
    for value in _HEX6.findall(page):
        assert value.lower() in allowed, value



