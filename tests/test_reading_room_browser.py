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
                    " if (target.includes('/chat/reading-room')) window.__lumioFetches.push(target);"
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
                invalid.goto(f"{lumio_server}/chat?page=Technology%20Stack&line_start=1&line_end=9999")
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