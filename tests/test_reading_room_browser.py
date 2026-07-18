"""Real-browser coverage for chat-side Reading Room URL/history state (#44)."""

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


def test_browser_reading_room_history_reload_and_copy(lumio_server):
    """Open, Back, Forward, Close, reload, and copy preserve one page state."""
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as p:
            try:
                launch_kwargs = {}
                if chromium_path := os.environ.get("LUMIO_CHROMIUM_PATH"):
                    launch_kwargs["executable_path"] = chromium_path
                browser = p.chromium.launch(headless=True, **launch_kwargs)
            except Exception as exc:  # pragma: no cover - depends on host install
                pytest.skip(f"Chromium is unavailable: {exc}")
            with browser:
                context = browser.new_context()
                context.request.post(
                    f"{lumio_server}/setup",
                    data=json.dumps(
                        {"username": "owner", "password": "browser-secret", "role": "owner"}
                    ),
                    headers={"content-type": "application/json"},
                )
                context.request.post(
                    f"{lumio_server}/login",
                    data=json.dumps({"username": "owner", "password": "browser-secret"}),
                    headers={"content-type": "application/json"},
                )
                context.request.post(
                    f"{lumio_server}/admin/users",
                    data=json.dumps(
                        {"username": "reader", "password": "reader-secret", "role": "reader"}
                    ),
                    headers={"content-type": "application/json"},
                )
                page = context.new_page()
                page.add_init_script(
                    "window.__lumioHistory = []; window.__lumioFetches = [];"
                    "const originalFetch = window.fetch;"
                    "window.fetch = function(...args) {"
                    " const target = String(args[0]);"
                    " if (target.includes('/chat/reading-room'))"
                    " window.__lumioFetches.push(target);"
                    " return originalFetch.apply(this, args); };"
                    "for (const method of ['pushState','replaceState','back']) {"
                    " const original = history[method];"
                    " history[method] = function(...args) { window.__lumioHistory.push(method);"
                    " return original.apply(this, args); };"
                    "}"
                )
                page.goto(f"{lumio_server}/chat")
                page.locator("#question").fill("What technology does Lumio use for retrieval?")
                page.locator("#chat-form button[type=submit]").click()
                page.locator(".cite-card__open").first.wait_for(state="visible", timeout=15_000)
                page.locator(".cite-card__row").filter(has_text="Technology Stack").locator(
                    ".cite-card__open"
                ).first.click()
                page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
                opened_url = page.url
                assert "page=" in opened_url
                assert page.evaluate("window.__lumioHistory") == ["pushState"]
                assert len(page.evaluate("window.__lumioFetches")) == 1

                page.locator(".cite-card__row").filter(has_text="Lumio Overview").locator(
                    ".cite-card__open"
                ).first.click()
                page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
                assert "page=Lumio+Overview" in page.url or "page=Lumio%20Overview" in page.url
                assert page.evaluate("window.__lumioHistory") == ["pushState", "replaceState"]
                assert len(page.evaluate("window.__lumioFetches")) == 2

                page.go_back()
                page.locator("#reading-room-inner").wait_for(state="detached", timeout=5_000)
                assert page.url.endswith("/chat")

                page.go_forward()
                page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
                assert "Lumio Overview" in page.locator("#reading-room").inner_text()

                page.reload()
                page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
                assert "Lumio Overview" in page.locator("#reading-room").inner_text()

                copied_context = browser.new_context()
                copied_context.request.post(
                    f"{lumio_server}/login",
                    data=json.dumps({"username": "reader", "password": "reader-secret"}),
                    headers={"content-type": "application/json"},
                )
                copied = copied_context.new_page()
                copied.goto(page.url)
                copied.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
                assert "Lumio Overview" in copied.locator("#reading-room").inner_text()
                copied.close()
                copied_context.close()

                page.locator(".rr__close").click()
                page.locator("#reading-room-inner").wait_for(state="detached", timeout=5_000)
                assert page.url.endswith("/chat")
                assert page.evaluate("window.__lumioHistory") == ["back"]

                invalid = context.new_page()
                invalid.goto(
                    f"{lumio_server}/chat?page=Technology%20Stack&line_start=1&line_end=9999"
                )
                assert "outside this Compiled Page" in invalid.locator("#reading-room").inner_text()
                invalid.close()
    except ModuleNotFoundError:
        pytest.skip("Playwright is unavailable")


def test_browser_search_result_navigation_through_compiled_pages(lumio_server):
    """Search results open Compiled Pages with query context; prev/next move
    through the ranked order and Back returns to the same result set (#47)."""
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as p:
            try:
                launch_kwargs = {}
                if chromium_path := os.environ.get("LUMIO_CHROMIUM_PATH"):
                    launch_kwargs["executable_path"] = chromium_path
                browser = p.chromium.launch(headless=True, **launch_kwargs)
            except Exception as exc:  # pragma: no cover
                pytest.skip(f"Chromium is unavailable: {exc}")
            with browser:
                context = browser.new_context()
                context.request.post(
                    f"{lumio_server}/setup",
                    data=json.dumps(
                        {"username": "owner", "password": "browser-secret", "role": "owner"}
                    ),
                    headers={"content-type": "application/json"},
                )
                context.request.post(
                    f"{lumio_server}/login",
                    data=json.dumps({"username": "owner", "password": "browser-secret"}),
                    headers={"content-type": "application/json"},
                )
                page = context.new_page()

                # Browse the Reading Room and search.
                page.goto(f"{lumio_server}/kb")
                page.locator("#reading-room-search").fill("Lumio")
                page.locator("#reading-room-search").press("Enter")
                page.locator(".card--search").first.wait_for(state="visible", timeout=10_000)
                assert "q=Lumio" in page.url
                # Three results for "Lumio".
                assert page.locator(".card--search").count() == 3

                # Open the middle result (Architecture) from search.
                page.locator(".card--search").filter(has_text="Architecture").click()
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Architecture" in page.locator(".article h1").inner_text()
                assert "q=Lumio" in page.url
                # Middle result has both prev and next.
                assert page.locator(".doc__prev").is_visible()
                assert page.locator(".doc__next").is_visible()
                assert "Result 2 of 3" in page.locator(".doc__nav").inner_text()

                # Next moves to the last result (Technology Stack).
                page.locator(".doc__next").click()
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Technology Stack" in page.locator(".article h1").inner_text()
                assert "q=Lumio" in page.url
                # Last result: no next.
                assert not page.locator(".doc__next").is_visible()
                assert page.locator(".doc__prev").is_visible()

                # Previous moves back to Architecture.
                page.locator(".doc__prev").click()
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Architecture" in page.locator(".article h1").inner_text()

                # Previous again moves to the first result (Lumio Overview).
                page.locator(".doc__prev").click()
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Lumio Overview" in page.locator(".article h1").inner_text()
                # First result: no prev.
                assert not page.locator(".doc__prev").is_visible()
                assert page.locator(".doc__next").is_visible()

                # Back to Reading Room returns to the same search results.
                page.locator(".doc__back").click()
                page.locator(".card--search").first.wait_for(state="visible", timeout=10_000)
                assert "q=Lumio" in page.url
                assert page.locator(".card--search").count() == 3

                page.close()
    except ModuleNotFoundError:
        pytest.skip("Playwright is unavailable")


def test_browser_search_result_navigation_history_back_and_forward(lumio_server):
    """Browser Back and Forward restore the standalone document and the
    Reading Room search result set with the query intact (#47)."""
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as p:
            try:
                launch_kwargs = {}
                if chromium_path := os.environ.get("LUMIO_CHROMIUM_PATH"):
                    launch_kwargs["executable_path"] = chromium_path
                browser = p.chromium.launch(headless=True, **launch_kwargs)
            except Exception as exc:  # pragma: no cover
                pytest.skip(f"Chromium is unavailable: {exc}")
            with browser:
                context = browser.new_context()
                context.request.post(
                    f"{lumio_server}/setup",
                    data=json.dumps(
                        {"username": "owner", "password": "browser-secret", "role": "owner"}
                    ),
                    headers={"content-type": "application/json"},
                )
                context.request.post(
                    f"{lumio_server}/login",
                    data=json.dumps({"username": "owner", "password": "browser-secret"}),
                    headers={"content-type": "application/json"},
                )
                page = context.new_page()

                # Search and open the middle result after scrolling to it so
                # that scroll restoration after Back is observable.
                page.goto(f"{lumio_server}/kb")
                page.locator("#reading-room-search").fill("Lumio")
                page.locator("#reading-room-search").press("Enter")
                page.locator(".card--search").first.wait_for(state="visible", timeout=10_000)
                selected_card = page.locator("#search-result-2")
                selected_card.scroll_into_view_if_needed()
                before_top = selected_card.evaluate("el => el.getBoundingClientRect().top")
                selected_card.click()
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Architecture" in page.locator(".article h1").inner_text()
                assert "q=Lumio" in page.url

                # Browser Back returns to the search result set and restores
                # reasonable scroll context (the selected card remains visible).
                page.go_back()
                page.locator(".card--search").first.wait_for(state="visible", timeout=10_000)
                assert "q=Lumio" in page.url
                assert page.locator(".card--search").count() == 3
                restored = page.locator("#search-result-2")
                assert restored.is_visible()
                assert "Architecture" in restored.inner_text()
                after_top = restored.evaluate("el => el.getBoundingClientRect().top")
                assert abs(after_top - before_top) < 50

                # Browser Forward returns to the standalone document.
                page.go_forward()
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Architecture" in page.locator(".article h1").inner_text()
                assert "q=Lumio" in page.url

                page.close()
    except ModuleNotFoundError:
        pytest.skip("Playwright is unavailable")


def test_browser_search_result_navigation_direct_deep_link(lumio_server):
    """A direct /kb/page/{title}?q=... URL opens the standalone document with
    server-recomputed prev/next navigation (#47)."""
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as p:
            try:
                launch_kwargs = {}
                if chromium_path := os.environ.get("LUMIO_CHROMIUM_PATH"):
                    launch_kwargs["executable_path"] = chromium_path
                browser = p.chromium.launch(headless=True, **launch_kwargs)
            except Exception as exc:  # pragma: no cover
                pytest.skip(f"Chromium is unavailable: {exc}")
            with browser:
                context = browser.new_context()
                context.request.post(
                    f"{lumio_server}/setup",
                    data=json.dumps(
                        {"username": "owner", "password": "browser-secret", "role": "owner"}
                    ),
                    headers={"content-type": "application/json"},
                )
                context.request.post(
                    f"{lumio_server}/login",
                    data=json.dumps({"username": "owner", "password": "browser-secret"}),
                    headers={"content-type": "application/json"},
                )
                page = context.new_page()

                page.goto(f"{lumio_server}/kb/page/Architecture?q=Lumio")
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Architecture" in page.locator(".article h1").inner_text()
                assert "q=Lumio" in page.url
                assert page.locator(".doc__prev").is_visible()
                assert page.locator(".doc__next").is_visible()
                assert "Result 2 of 3" in page.locator(".doc__nav").inner_text()

                page.close()
    except ModuleNotFoundError:
        pytest.skip("Playwright is unavailable")


def _wide_owner_context(browser, lumio_server):
    """Authenticated Owner browser context for wide-viewport journeys."""
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    context.request.post(
        f"{lumio_server}/setup",
        data=json.dumps({"username": "owner", "password": "browser-secret", "role": "owner"}),
        headers={"content-type": "application/json"},
    )
    context.request.post(
        f"{lumio_server}/login",
        data=json.dumps({"username": "owner", "password": "browser-secret"}),
        headers={"content-type": "application/json"},
    )
    return context


def _launch_chromium(p):
    """Launch headless Chromium, skipping the test when it is unavailable."""
    launch_kwargs = {}
    if chromium_path := os.environ.get("LUMIO_CHROMIUM_PATH"):
        launch_kwargs["executable_path"] = chromium_path
    return p.chromium.launch(headless=True, **launch_kwargs)


def test_browser_wide_journey_persistent_replacement_close_and_standalone(lumio_server):
    """One unified wide-viewport Reader journey (#48).

    Ask → Citation open with cited range → another question leaves the page
    open (persistence) → selecting another Citation replaces the active page
    → Close restores chat width → the chat-side 'Open in Reading Room'
    affordance opens the standalone full-width document. The persistent-column
    (#43) and replacement contracts are exercised end to end at the
    real-browser seam, complementing the HTTP-level persistence tests.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as p:
            try:
                browser = _launch_chromium(p)
            except Exception as exc:  # pragma: no cover - depends on host install
                pytest.skip(f"Chromium is unavailable: {exc}")
            with browser:
                context = _wide_owner_context(browser, lumio_server)
                page = context.new_page()
                page.goto(f"{lumio_server}/chat")

                # Ask → cited answer.
                page.locator("#question").fill("What technology does Lumio use for retrieval?")
                page.locator("#chat-form button[type=submit]").click()
                page.locator(".cite-card__open").first.wait_for(state="visible", timeout=15_000)

                # Citation open with a cited range → the passage is inspectable.
                page.locator(".cite-card__row").filter(has_text="Technology Stack").locator(
                    ".cite-card__open"
                ).first.click()
                page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
                assert "Technology Stack" in page.locator("#reading-room").inner_text()
                assert page.locator("#rr-cited-passage").is_visible()
                assert "Cited lines" in page.locator("#rr-cited-passage").inner_text()

                # Another question leaves the explicitly opened page in place.
                # Wait for the second answer to actually arrive (the #answer
                # region's text changes) so the persistence assertion runs only
                # after the new answer — not against the first answer's already-
                # visible Citations. The chat refreshes Citations but never
                # patches #reading-room, so the Reader's page survives (#43).
                answer_before = page.locator("#answer").inner_text()
                page.locator("#question").fill("What is Lumio?")
                page.locator("#chat-form button[type=submit]").click()
                page.wait_for_function(
                    "(prev) => (document.getElementById('answer')||{}).innerText !== prev",
                    arg=answer_before,
                    timeout=15_000,
                )
                page.locator(".cite-card__open").first.wait_for(state="visible", timeout=15_000)
                assert "Technology Stack" in page.locator("#reading-room").inner_text()
                # Still exactly one Reading Room surface — no duplicated columns.
                assert page.locator("#reading-room-inner").count() == 1

                # Selecting another Citation replaces the single active page.
                page.locator(".cite-card__row").filter(has_text="Lumio Overview").locator(
                    ".cite-card__open"
                ).first.click()
                page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
                assert "Lumio Overview" in page.locator("#reading-room").inner_text()
                # Still exactly one Reading Room surface after replacement.
                assert page.locator("#reading-room-inner").count() == 1

                # Close restores chat width (the slot empties and the URL resets).
                page.locator(".rr__close").click()
                page.locator("#reading-room-inner").wait_for(state="detached", timeout=5_000)
                assert page.url.endswith("/chat")

                # Reopen and move into the standalone full-width document via the
                # chat-side 'Open in Reading Room' affordance — the same shared
                # article interface, presented without chat.
                page.locator(".cite-card__row").filter(has_text="Technology Stack").locator(
                    ".cite-card__open"
                ).first.click()
                page.locator("#reading-room-inner").wait_for(state="visible", timeout=15_000)
                page.locator(".rr__open").first.click()
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Technology Stack" in page.locator(".article h1").inner_text()
                # The standalone document is chat-free.
                assert page.locator("#chat-form").count() == 0
                assert page.locator("#composer").count() == 0

                page.close()
    except ModuleNotFoundError:
        pytest.skip("Playwright is unavailable")


def test_browser_standalone_journey_browse_search_select_prevnext_back_and_deep_link(lumio_server):
    """One unified standalone Reading Room journey (#48).

    Browse the index → lexical search returns ranked results → a no-match query
    produces a deliberate no-results state with a way back → clearing the query
    restores browse → selecting a result opens the standalone document with
    prev/next ranked navigation → Back restores the same result set → a direct
    deep link opens the standalone document.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as p:
            try:
                browser = _launch_chromium(p)
            except Exception as exc:  # pragma: no cover - depends on host install
                pytest.skip(f"Chromium is unavailable: {exc}")
            with browser:
                context = _wide_owner_context(browser, lumio_server)
                page = context.new_page()

                # Browse landing: published Compiled Pages, no chat composer.
                page.goto(f"{lumio_server}/kb")
                page.locator(".card").first.wait_for(state="visible", timeout=10_000)
                assert page.locator("#chat-form").count() == 0
                browse_count = page.locator(".card").count()
                assert browse_count >= 1

                # Lexical search returns ranked results; query is in the URL.
                page.locator("#reading-room-search").fill("Lumio")
                page.locator("#reading-room-search").press("Enter")
                page.locator(".card--search").first.wait_for(state="visible", timeout=10_000)
                assert "q=Lumio" in page.url
                result_count = page.locator(".card--search").count()
                assert result_count >= 1

                # A no-match query produces a deliberate, recoverable empty state.
                page.locator("#reading-room-search").fill("zzznomatchxyz")
                page.locator("#reading-room-search").press("Enter")
                page.locator(".search-state").wait_for(state="visible", timeout=10_000)
                assert "No matching Compiled Pages" in page.locator(".search-state").inner_text()
                # The empty state offers a way back to browsing (not a hard 404).
                assert page.locator(".search-state a").get_attribute("href") == "/kb"

                # The empty-state link restores browse mode.
                page.locator(".search-state a").click()
                page.locator(".card").first.wait_for(state="visible", timeout=10_000)
                assert "q=" not in page.url
                # Clearing search restored the same browse index cardinality.
                assert page.locator(".card").count() == browse_count

                # Re-run the ranked search and select a result → standalone
                # document with prev/next ranked navigation for this query.
                page.locator("#reading-room-search").fill("Lumio")
                page.locator("#reading-room-search").press("Enter")
                page.locator(".card--search").first.wait_for(state="visible", timeout=10_000)
                page.locator(".card--search").filter(has_text="Architecture").click()
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Architecture" in page.locator(".article h1").inner_text()
                assert "q=Lumio" in page.url
                # With multiple ranked results the document carries prev/next
                # and reports its position in the ranked set.
                if result_count > 1:
                    assert page.locator(".doc__prev").is_visible()
                    assert page.locator(".doc__next").is_visible()

                    # Next moves to another result in the same ranked set.
                    page.locator(".doc__next").click()
                    page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                    assert "q=Lumio" in page.url
                    # Previous returns to the first selected result.
                    page.locator(".doc__prev").click()
                    page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                    assert "Architecture" in page.locator(".article h1").inner_text()

                # Back to Reading Room restores the same ranked result set.
                page.locator(".doc__back").click()
                page.locator(".card--search").first.wait_for(state="visible", timeout=10_000)
                assert "q=Lumio" in page.url
                assert page.locator(".card--search").count() == result_count

                # A direct standalone deep link opens the full-width document.
                page.goto(f"{lumio_server}/kb/page/Architecture")
                page.locator(".article h1").wait_for(state="visible", timeout=10_000)
                assert "Architecture" in page.locator(".article h1").inner_text()
                # Standalone document carries Back to Reading Room.
                assert page.locator(".doc__back").is_visible()
                # No search context → no prev/next navigation.
                assert page.locator(".doc__prev").count() == 0
                assert page.locator(".doc__next").count() == 0

                page.close()
    except ModuleNotFoundError:
        pytest.skip("Playwright is unavailable")
