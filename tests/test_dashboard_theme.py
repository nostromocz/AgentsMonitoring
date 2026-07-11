"""TDD focused tests for the dashboard's Auto/Dark/Light theme selector.

These are structural/static assertions against ``dashboard.PAGE`` (the served HTML/CSS/JS
string). There's no headless browser in this stdlib-only project, so behaviour that would
normally be checked by executing JS (e.g. "clicking Dark actually adds the .dark class") is
instead verified by asserting the exact source pattern that implements it is present — the
same technique used to keep the rest of this file dependency-free.
"""
from __future__ import annotations

import re
import unittest

from agentsmon import dashboard

PAGE = dashboard.PAGE
THEME_KEY = "agentsmon-theme"


class NoNewRuntimeDependencyTests(unittest.TestCase):
    def test_still_only_one_external_script_tag(self):
        # The only allowed runtime dependency is the existing Tailwind CDN <script src=...>.
        srcs = re.findall(r"<script[^>]+src=", PAGE)
        self.assertEqual(len(srcs), 1, srcs)
        self.assertIn("cdn.tailwindcss.com", PAGE)


class PreventFlashOfWrongThemeTests(unittest.TestCase):
    """The theme must be applied before <body> paints, so light users never see a dark
    flash and vice versa."""

    def _head(self) -> str:
        return PAGE[: PAGE.index("<body")]

    def test_head_contains_a_theme_init_script(self):
        head = self._head()
        self.assertIn("<script>", head)
        self.assertIn(THEME_KEY, head)

    def test_init_script_reads_persisted_choice(self):
        head = self._head()
        self.assertRegex(head, r'localStorage\.getItem\(\s*["\']%s["\']\s*\)' % THEME_KEY)

    def test_init_script_falls_back_to_os_preference_when_no_choice_persisted(self):
        head = self._head()
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', head)

    def test_init_script_toggles_dark_class_on_the_html_element_synchronously(self):
        head = self._head()
        self.assertRegex(head, r"document\.documentElement\.classList\.(toggle|add)\(")
        # Must not be deferred/async — it needs to run inline, before body markup paints.
        script_tag = re.search(r"<script[^>]*>(?=[^<]*%s)" % THEME_KEY, head)
        self.assertIsNotNone(script_tag)
        self.assertNotIn("defer", script_tag.group(0))
        self.assertNotIn("async", script_tag.group(0))

    def test_init_script_is_wrapped_so_a_blocked_storage_api_cannot_break_the_page(self):
        head = self._head()
        self.assertIn("try{", head.replace(" ", ""))
        storage_guard = re.search(r"try\{.*?localStorage\.getItem.*?\}catch\(e\)\{\}", head, re.S)
        if storage_guard is None:
            self.fail("expected localStorage access to have its own failure guard")
        # OS detection and class application must happen after the storage-only guard.
        self.assertGreater(head.index("matchMedia", storage_guard.end()), storage_guard.end())

    def test_invalid_or_legacy_stored_values_fall_back_to_auto(self):
        head = self._head()
        self.assertIn('t==="light"||t==="dark"', head)
        self.assertRegex(head, r"valid\s*\?\s*t===?\"dark\"\s*:\s*matchMedia")


class ThemeControlTests(unittest.TestCase):
    """A compact, accessible control offering Auto / Light / Dark."""

    def _select_tag(self) -> str:
        m = re.search(r'<select[^>]*id="theme-select"[^>]*>', PAGE)
        self.assertIsNotNone(m, "expected a <select id=\"theme-select\"> control")
        return m.group(0)

    def test_control_exists_with_three_options(self):
        block = re.search(r'<select[^>]*id="theme-select".*?</select>', PAGE, re.S)
        self.assertIsNotNone(block)
        body = block.group(0)
        for value, label in (("auto", "Auto"), ("light", "Light"), ("dark", "Dark")):
            self.assertRegex(body, r'<option value="%s"[^>]*>\s*%s' % (value, label))

    def test_control_has_meaningful_aria_label_and_title(self):
        tag = self._select_tag()
        self.assertRegex(tag, r'aria-label="[^"]*[Tt]heme[^"]*"')
        self.assertRegex(tag, r'title="[^"]*[Tt]heme[^"]*"')

    def test_control_is_a_native_keyboard_operable_element(self):
        tag = self._select_tag()
        self.assertNotIn('tabindex="-1"', tag)
        self.assertNotIn("disabled", tag)

    def test_control_has_visible_focus_style(self):
        tag = self._select_tag()
        self.assertRegex(tag, r"focus-visible:")

    def test_control_label_is_not_only_a_placeholder(self):
        # Either a visually-hidden <label for="theme-select"> or an aria-label covers it —
        # here we require the aria-label since the select itself carries it.
        tag = self._select_tag()
        self.assertIn("aria-label=", tag)


class ThemePersistenceAndLiveUpdateTests(unittest.TestCase):
    def _script_body(self) -> str:
        m = re.search(r"<script>(.*)</script>\s*</body>", PAGE, re.S)
        self.assertIsNotNone(m)
        return m.group(1)

    def test_explicit_choice_is_persisted(self):
        body = self._script_body()
        self.assertRegex(body, r'localStorage\.setItem\(\s*["\']%s["\']' % THEME_KEY)

    def test_choosing_auto_clears_the_persisted_choice(self):
        body = self._script_body()
        self.assertRegex(body, r'localStorage\.removeItem\(\s*["\']%s["\']\s*\)' % THEME_KEY)

    def test_select_change_handler_applies_the_theme(self):
        body = self._script_body()
        self.assertIn('getElementById("theme-select")', body)
        self.assertRegex(body, r'addEventListener\(\s*["\']change["\']')

    def test_os_theme_changes_are_observed(self):
        body = self._script_body()
        self.assertIn('const themeMedia=matchMedia("(prefers-color-scheme: dark)")', body)
        self.assertRegex(body, r'themeMedia\.addEventListener\(\s*["\']change["\']')

    def test_os_theme_changes_only_apply_for_the_active_auto_choice(self):
        body = self._script_body()
        m = re.search(
            r'themeMedia\.addEventListener\(\s*["\']change["\'].*?\}\)',
            body,
            re.S,
        )
        self.assertIsNotNone(m, "expected an OS-change listener block")
        listener = m.group(0)
        self.assertIn('activeTheme==="auto"', listener)
        self.assertIn('applyTheme("auto")', listener)

    def test_footer_time_uses_prague_timezone_and_24_hour_clock(self):
        body = self._script_body()
        self.assertIn('toLocaleTimeString("cs-CZ"', body)
        self.assertIn('hour12:false', body)
        self.assertIn('timeZone:"Europe/Prague"', body)
        self.assertIn('second:"2-digit"', body)

    def test_apply_theme_normalizes_invalid_input_and_tracks_session_choice(self):
        body = self._script_body()
        self.assertIn('let activeTheme="auto"', body)
        self.assertIn('choice==="light"||choice==="dark"||choice==="auto"?choice:"auto"', body)
        self.assertIn("activeTheme=choice", body)


class LightThemePreservedTests(unittest.TestCase):
    """The default (light) look must not regress — only additive dark: variants are allowed."""

    def test_body_still_has_its_original_light_classes(self):
        body_tag = re.search(r"<body[^>]*>", PAGE).group(0)
        self.assertIn("bg-slate-50", body_tag)
        self.assertIn("text-slate-800", body_tag)

    def test_cards_still_have_their_original_light_classes(self):
        self.assertIn("bg-white border-slate-200", PAGE)


class DarkVariantCoverageTests(unittest.TestCase):
    """Comprehensive dark theming: body, cards, table, borders, muted text, status headers,
    badges, action hover states, timeline no-data, toast, focus styles."""

    def test_body_is_themed(self):
        body_tag = re.search(r"<body[^>]*>", PAGE).group(0)
        self.assertIn("dark:bg-slate-900", body_tag)

    def test_cards_are_themed(self):
        # every "bg-white border-slate-200" card block should carry a dark background variant
        self.assertGreaterEqual(PAGE.count("dark:bg-slate-800"), 3)

    def test_borders_are_themed(self):
        self.assertIn("dark:border-slate-700", PAGE)

    def test_muted_text_is_themed_with_accessible_contrast(self):
        self.assertIn("dark:text-slate-400", PAGE)
        self.assertNotIn("dark:text-slate-500", PAGE)
        self.assertNotIn("dark:text-slate-600", PAGE)

        def luminance(hex_color: str) -> float:
            channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        foreground = luminance("#94a3b8")  # Tailwind slate-400
        for background in ("#1e293b", "#0f172a"):  # slate-800 and slate-900
            bg = luminance(background)
            contrast = (max(foreground, bg) + 0.05) / (min(foreground, bg) + 0.05)
            self.assertGreaterEqual(contrast, 4.5)

    def test_table_header_row_is_themed(self):
        thead_row = re.search(r'<tr class="[^"]*uppercase[^"]*">', PAGE).group(0)
        self.assertIn("dark:", thead_row)

    def test_table_row_dividers_are_themed(self):
        self.assertIn("border-b border-slate-100", PAGE)
        self.assertRegex(PAGE, r"border-b border-slate-100[^\"]*dark:border-")

    def test_status_headers_are_themed(self):
        state_block = re.search(r"const STATE = \{.*?\};", PAGE, re.S).group(0)
        self.assertIn("dark:bg-emerald-950", state_block)
        self.assertIn("dark:bg-rose-950", state_block)
        self.assertIn("dark:text-emerald-400", state_block)
        self.assertIn("dark:text-rose-400", state_block)

    def test_vendor_badges_are_themed(self):
        vendor_block = re.search(r"const VENDOR = \{.*?\};", PAGE, re.S).group(0)
        # anthropic/openai/google/gold/red/other -> 6 entries, all themed
        self.assertGreaterEqual(vendor_block.count("dark:bg-"), 6)

    def test_name_highlight_badges_are_themed(self):
        name_bg_block = re.search(r"const NAME_BG = \{.*?\};", PAGE, re.S).group(0)
        self.assertIn("dark:bg-", name_bg_block)

    def test_row_action_hover_states_are_themed(self):
        self.assertRegex(PAGE, r'data-act="restart"[\s\S]*?dark:hover:')
        self.assertRegex(PAGE, r'data-act="stop"[\s\S]*?dark:hover:')

    def test_timeline_no_data_bar_is_themed(self):
        render_timeline = re.search(r"function renderTimeline.*?\n\}", PAGE, re.S).group(0)
        self.assertRegex(render_timeline, r'bg-slate-200[^"]*dark:bg-slate-')

    def test_timeline_no_data_legend_swatch_is_themed(self):
        legend = re.search(r"No data</span>", PAGE)
        self.assertIsNotNone(legend)
        preceding = PAGE[max(0, legend.start() - 200) : legend.start()]
        self.assertIn("dark:bg-", preceding)

    def test_toast_is_themed(self):
        toast_div = re.search(r'<div id="toast"[^>]*>', PAGE).group(0)
        self.assertIn("dark:", toast_div)

    def test_focus_styles_present_beyond_the_theme_control(self):
        self.assertGreaterEqual(PAGE.count("focus-visible:"), 2)


if __name__ == "__main__":
    unittest.main()
