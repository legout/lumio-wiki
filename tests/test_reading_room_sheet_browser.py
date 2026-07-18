"""Real-browser coverage for the responsive Reading Room sheet (issue #45).

These tests drive Chromium at constrained and resized viewports to verify the
chat-side Reading Room adapts from a desktop side column into a full-height,
focus-managed sheet without losing the URL/history-aware behaviour from #44.

They skip gracefully when Playwright or Chromium is unavailable.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

# A content width below the 880px container threshold forces the sheet; well
# above it forces the desktop two-column split. The rail consumes some width,
# so the viewport is set with headroom around the threshold.
_NARROW = 600
_WIDE = 1400


@pytest.fixture
def lumio_server(tmp_path: Path):
    """Start an isolated Lumio HTTP server for Chromium-level interaction."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "LUMIO_KB_PATH": str(Path(__file__).parent / "fixtures" / "valid"),
            "LUMIO_METADATA_DB_PATH": str(tmp_path / "metadata.sqlite"),
            "LUMIO_CONFIG_PATH": str(tmp_path / "config"),
            "LUMIO_INGEST_PATH": str(tmp_path / "ingest"),
            "LUMIO_PUBLISH_PATH": str(tmp_path / "publish"),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "lumio.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Lumio browser-test server did not become ready")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _launch(p):
    try:
        launch_kwargs = {}
        if chromium_path := os.environ.get("LUMIO_CHROMIUM_PATH"):
            launch_kwargs["executable_path"] = chromium_path
        return p.chromium.launch(headless=True, **launch_kwargs)
    except Exception as exc:  # pragma: no cover - depends on host install
        pytest.skip(f"Chromium is unavailable: {exc}")


def _setup(context, base):
    """Create accounts and seed a Reader session cookie on the context."""
    context.request.post(
        f"{base}/setup",
        data=json.dumps({"username": "owner", "password": "sheet-secret", "role": "owner"}),
        headers={"content-type": "application/json"},
    )
    context.request.post(
        f"{base}/login",
        data=json.dumps({"username": "owner", "password": "sheet-secret"}),
        headers={"content-type": "application/json"},
    )
    context.request.post(
        f"{base}/admin/users",
        data=json.dumps({"username": "reader", "password": "reader-secret", "role": "reader"}),
        headers={"content-type": "application/json"},
    )


def _login_reader(context, base):
    context.request.post(
        f"{base}/login",
        data=json.dumps({"username": "reader", "password": "reader-secret"}),
        headers={"content-type": "application/json"},
    )


def _open_room(page, base):
    """Ask a question and open the first citation's Reading Room."""
    page.goto(f"{base}/chat")
    page.locator("#question").fill("What technology does Lumio use for retrieval?")
    page.locator("#chat-form button[type=submit]").click()
    page.locator(".cite-card__open").first.wait_for(state="visible", timeout=15_000)
    page.locator(".cite-card__open").first.click()
    page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)


def test_narrow_viewport_opens_sheet_overlay_with_inert_chat(lumio_server):
    """On a constrained width the room is a fixed sheet and the chat is inert."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            # The slot lifts out of the grid as a fixed full-height overlay.
            assert (
                page.evaluate("getComputedStyle(document.getElementById('reading-room')).position")
                == "fixed"
            )
            # The underlying chat column is inert (removed from the a11y tree).
            assert page.locator(".chat__main").get_attribute("inert") is not None
            # Focus has moved into the named sheet.
            assert page.evaluate(
                "document.getElementById('reading-room').contains(document.activeElement)"
            )


def test_focus_restored_to_citation_when_sheet_dismissed(lumio_server):
    """Closing the sheet returns focus to the citation that opened it."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            page.locator(".rr__close").click()
            page.locator("#reading-room-inner").wait_for(state="detached", timeout=5_000)
            # The citation button regained focus.
            assert page.evaluate(
                "document.activeElement && document.activeElement.classList"
                " && document.activeElement.classList.contains('cite-card__open')"
            )
            assert page.url.endswith("/chat")


def test_escape_dismisses_sheet_via_history(lumio_server):
    """Escape dismisses the sheet through the same URL/history path."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            page.keyboard.press("Escape")
            page.locator("#reading-room-inner").wait_for(state="detached", timeout=5_000)
            assert page.url.endswith("/chat")


def test_available_chat_width_switches_sheet_at_fixed_viewport(lumio_server):
    """The chat container's available width, not viewport identity, selects layout."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _WIDE, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            assert page.evaluate("window.innerWidth") == _WIDE
            assert (
                page.evaluate("getComputedStyle(document.getElementById('reading-room')).position")
                != "fixed"
            )

            chat = page.locator(".chat")
            chat.evaluate("el => { el.style.width = '600px'; el.style.flex = 'none'; }")
            page.wait_for_function(
                "getComputedStyle(document.getElementById('reading-room')).position === 'fixed'",
                timeout=5_000,
            )
            assert page.evaluate("window.innerWidth") == _WIDE
            assert page.locator(".chat__main").get_attribute("inert") is not None
            assert "Technology Stack" in page.locator("#reading-room").inner_text()

            chat.evaluate("el => el.style.width = '1100px'")
            page.wait_for_function(
                "getComputedStyle(document.getElementById('reading-room')).position !== 'fixed'",
                timeout=5_000,
            )
            page.wait_for_function(
                "!document.querySelector('.chat__main').hasAttribute('inert')",
                timeout=5_000,
            )
            assert page.evaluate("window.innerWidth") == _WIDE
            assert page.locator(".chat__main").get_attribute("inert") is None
            assert page.locator("#reading-room-inner").count() == 1
            assert "Technology Stack" in page.locator("#reading-room").inner_text()


def test_no_horizontal_overflow_in_sheet(lumio_server):
    """The sheet never produces a horizontal scrollbar on a narrow display."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": 360, "height": 640})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            page.wait_for_function(
                "document.documentElement.scrollWidth <= window.innerWidth + 1",
                timeout=5_000,
            )


def test_reload_restores_sheet_on_constrained_display(lumio_server):
    """A direct reload of a URL with an active page rehydrates the sheet."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            assert "page=" in page.url
            page.reload()
            page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
            assert (
                page.evaluate("getComputedStyle(document.getElementById('reading-room')).position")
                == "fixed"
            )
            assert page.locator(".chat__main").get_attribute("inert") is not None


def test_tab_and_shift_tab_wrap_within_sheet(lumio_server):
    """Focus wraps last-to-first and first-to-last inside the modal sheet."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            focusables = page.locator(
                "#reading-room-inner a[href]:visible, "
                "#reading-room-inner button:not([disabled]):visible"
            )
            assert focusables.count() >= 2
            first = focusables.first
            last = focusables.last

            last.focus()
            page.keyboard.press("Tab")
            assert first.evaluate("el => document.activeElement === el")

            first.focus()
            page.keyboard.press("Shift+Tab")
            assert last.evaluate("el => document.activeElement === el")


def test_forward_restores_sheet_on_constrained_display(lumio_server):
    """After Back dismisses the sheet, Forward reopens it as a fixed sheet."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            assert "page=" in page.url
            # Back dismisses the sheet into the preserved chat.
            page.go_back()
            page.locator("#reading-room-inner").wait_for(state="detached", timeout=5_000)
            assert page.url.endswith("/chat")
            # Forward restores the room as a fixed sheet on the narrow display.
            page.go_forward()
            page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
            assert (
                page.evaluate("getComputedStyle(document.getElementById('reading-room')).position")
                == "fixed"
            )
            assert page.locator(".chat__main").get_attribute("inert") is not None


def test_chat_state_preserved_while_sheet_open(lumio_server):
    """The composer draft, answer, Citations, and Trace stay intact under the sheet."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            page.goto(f"{lumio_server}/chat")
            page.locator("#question").fill("What technology does Lumio use for retrieval?")
            page.locator("#chat-form button[type=submit]").click()
            page.locator(".cite-card__open").first.wait_for(state="visible", timeout=15_000)
            # Capture the rendered answer, Citations, and Trace before opening.
            answer_text = page.locator("#answer").inner_text()
            assert page.locator("#citations .cite-card__row").count() >= 1
            trace_html = page.locator("#trace").inner_html()
            # Type a follow-up draft (not submitted) to prove it survives the sheet.
            page.locator("#question").fill("Tell me more about the stack")
            draft = page.locator("#question").input_value()
            # Open the sheet over the preserved chat.
            page.locator(".cite-card__open").first.click()
            page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
            # The chat thread, draft, answer, Citations, and Trace are untouched.
            assert page.locator("#question").input_value() == draft
            assert page.locator("#answer").inner_text() == answer_text
            assert page.locator("#citations .cite-card__row").count() >= 1
            assert page.locator("#trace").inner_html() == trace_html


def test_resize_preserves_cited_range_scroll_and_actions(lumio_server):
    """Resizing between sheet and column preserves the cited range, scroll position,
    full-page action, page title, and follow-up affordances."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            # Use a shorter viewport so the Compiled Page body is actually scrollable.
            context = browser.new_context(viewport={"width": _NARROW, "height": 500})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            page.locator("#rr-cited-passage").wait_for(state="visible", timeout=5_000)
            passage_text = page.locator("#rr-cited-passage").inner_text()
            full_page_href = page.locator(".rr__open").get_attribute("href")
            title_text = page.locator("#reading-room-title").inner_text()
            chips = page.locator(".rr__chip").all_inner_texts()
            assert full_page_href and full_page_href.startswith("/kb/page/")
            assert chips

            scroll = page.locator("#reading-room-inner .rr__scroll")
            scroll.evaluate("el => el.scrollTop = 60")
            scroll_before = scroll.evaluate("el => el.scrollTop")
            assert scroll_before > 0, (
                "Compiled Page body is not scrollable; scroll preservation cannot be exercised"
            )

            # Widen to column: the same range, title, actions, and scroll survive.
            page.set_viewport_size({"width": _WIDE, "height": 500})
            page.wait_for_function(
                "getComputedStyle(document.getElementById('reading-room')).position !== 'fixed'",
                timeout=5_000,
            )
            assert page.locator("#rr-cited-passage").inner_text() == passage_text
            assert page.locator(".rr__open").get_attribute("href") == full_page_href
            assert page.locator("#reading-room-title").inner_text() == title_text
            assert page.locator(".rr__chip").all_inner_texts() == chips
            scroll_after_wide = scroll.evaluate("el => el.scrollTop")
            assert scroll_after_wide == scroll_before

            # Narrow back to sheet: still one Reading Room instance with the same state.
            page.set_viewport_size({"width": _NARROW, "height": 500})
            page.wait_for_function(
                "getComputedStyle(document.getElementById('reading-room')).position === 'fixed'",
                timeout=5_000,
            )
            assert page.locator("#reading-room-inner").count() == 1
            assert page.locator("#rr-cited-passage").inner_text() == passage_text
            assert page.locator(".rr__open").get_attribute("href") == full_page_href
            assert page.locator("#reading-room-title").inner_text() == title_text
            assert page.locator(".rr__chip").all_inner_texts() == chips
            scroll_after_narrow = scroll.evaluate("el => el.scrollTop")
            assert scroll_after_narrow == scroll_before


def test_sheet_applies_emulated_safe_area_insets(lumio_server):
    """Computed sheet padding follows browser-emulated display safe areas."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            page.locator("#reading-room").evaluate(
                """el => {
                    el.style.setProperty('--lumio-safe-area-top', '17px');
                    el.style.setProperty('--lumio-safe-area-right', '19px');
                    el.style.setProperty('--lumio-safe-area-bottom', '23px');
                    el.style.setProperty('--lumio-safe-area-left', '29px');
                }"""
            )
            padding = page.locator("#reading-room").evaluate(
                "el => { const s = getComputedStyle(el); return "
                "[s.paddingTop, s.paddingRight, s.paddingBottom, s.paddingLeft]; }"
            )
            assert padding == ["17px", "19px", "23px", "29px"]


def test_sheet_opens_without_motion_when_reduction_requested(lumio_server):
    """Reduced-motion emulation yields a readable sheet with zero computed motion."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(
                viewport={"width": _NARROW, "height": 800},
                reduced_motion="reduce",
            )
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)
            assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
            sheet = page.locator("#reading-room")
            assert sheet.is_visible()
            assert page.locator("#rr-cited-passage").is_visible()
            motion_seconds = sheet.evaluate(
                "el => { const s = getComputedStyle(el); return "
                "[s.transitionDuration, s.animationDuration]"
                ".map(value => parseFloat(value)); }"
            )
            assert all(duration <= 0.0001 for duration in motion_seconds)


def test_chat_scroll_position_preserved_while_sheet_open(lumio_server):
    """Opening the Reading Room sheet does not reset the underlying chat scroll."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": _NARROW, "height": 800})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            page.goto(f"{lumio_server}/chat")
            page.locator("#question").fill("What technology does Lumio use for retrieval?")
            page.locator("#chat-form button[type=submit]").click()
            page.locator(".cite-card__open").first.wait_for(state="visible", timeout=15_000)
            # Constrain the chat scroll area so the button has a non-zero scroll offset,
            # then scroll the button to the top of the visible area.
            chat_scroll = page.locator(".chat__scroll")
            chat_scroll.evaluate("el => el.style.maxHeight = '120px'")
            button = page.locator(".cite-card__open").first
            button.evaluate("el => el.scrollIntoView({ block: 'start', behavior: 'instant' })")
            chat_scroll_before = chat_scroll.evaluate("el => el.scrollTop")
            assert chat_scroll_before > 0
            # Open the sheet from the citation, which is already in view.
            button.click()
            page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
            # The chat scroll position is unchanged.
            assert chat_scroll.evaluate("el => el.scrollTop") == chat_scroll_before


def test_cited_range_highlight_survives_forced_colors(lumio_server):
    """The cited-range highlight stays understandable when color is removed (#48).

    Source highlighting must not rely on color alone. Under forced-colors
    (Windows High Contrast / monochrome), the cited passage keeps a visible
    text label and a structural left bar on each cited line, so the Reader can
    still tell which passage a Citation points at without the gold tint.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(
                viewport={"width": _NARROW, "height": 800},
                forced_colors="active",
            )
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            _open_room(page, lumio_server)

            assert page.evaluate("matchMedia('(forced-colors: active)').matches")
            passage = page.locator("#rr-cited-passage")
            assert passage.is_visible()
            # The textual label is the primary non-color cue and always survives.
            assert "Cited lines" in passage.inner_text()

            # Each cited line keeps a structural left bar (not only a tint) so
            # the highlighted range stays visible when the gold background is
            # mapped to the system canvas color.
            line_bar = page.locator(".rr__ln").first.evaluate(
                "el => { const s = getComputedStyle(el); "
                "return { style: s.borderLeftStyle, width: parseFloat(s.borderLeftWidth) }; }"
            )
            assert line_bar["style"] != "none"
            assert line_bar["width"] > 0

            # The passage container itself stays visibly bounded.
            container_border = passage.evaluate(
                "el => { const s = getComputedStyle(el); "
                "return { style: s.borderTopStyle, width: parseFloat(s.borderTopWidth) }; }"
            )
            assert container_border["style"] != "none"
            assert container_border["width"] > 0
