"""Real-browser coverage for Chat Sources management and retrieval scope (#34).

Drives Chromium at the public chat surface to verify the Reader can manage
several Conversation Sources through source chips, switch and persist
retrieval scope, see combined Knowledge Base + chat-file citations without a
temporary Conversation Source being treated as a Compiled Page, and that source
chips reflow safely on a narrow viewport. These tests exercise the browser UI
only (no private helpers or database state) and skip gracefully when Playwright
or Chromium is unavailable, following the Reading Room browser-test conventions.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path
from subprocess import DEVNULL, Popen, TimeoutExpired

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
    process = Popen(
        [sys.executable, "-m", "lumio.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=DEVNULL,
        stderr=DEVNULL,
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
        except TimeoutExpired:
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
    """Create accounts and seed an Owner session cookie on the context."""
    context.request.post(
        f"{base}/setup",
        data=json.dumps({"username": "owner", "password": "src-owner-secret", "role": "owner"}),
        headers={"content-type": "application/json"},
    )
    context.request.post(
        f"{base}/login",
        data=json.dumps({"username": "owner", "password": "src-owner-secret"}),
        headers={"content-type": "application/json"},
    )
    context.request.post(
        f"{base}/admin/users",
        data=json.dumps({"username": "reader", "password": "reader-secret", "role": "reader"}),
        headers={"content-type": "application/json"},
    )


def _login_reader(context, base):
    """Replace the Owner session with a Reader session on the shared jar."""
    context.request.post(
        f"{base}/login",
        data=json.dumps({"username": "reader", "password": "reader-secret"}),
        headers={"content-type": "application/json"},
    )


def _upload_source(page, filename: str, body: str, *, content_type: str = "text/markdown"):
    """Attach one Conversation Source and wait for its chip to render.

    Source upload is a plain multipart POST that redirects back to /chat, so the
    chip materialises only after the full page reload.
    """
    page.locator("#chat-source-file").set_input_files(
        files=[{"name": filename, "mimeType": content_type, "buffer": body.encode()}]
    )
    page.locator("#chat-source-form button[type=submit]").click()
    expect = pytest.importorskip("playwright.sync_api").expect
    expect(page.locator(".chat-source", has_text=filename)).to_have_count(1)
    expect(page.locator(".chat-source__name", has_text=filename)).to_be_visible()


def _ask(page, question: str) -> None:
    """Submit a question through the chat composer (Datastar SSE stream)."""
    page.locator("#question").fill(question)
    page.locator("#chat-form button[type=submit]").click()


def _answer_includes(page, needle: str, *, timeout: int = 15_000) -> None:
    """Wait until the streamed #answer region contains ``needle``."""
    page.wait_for_function(
        "(t) => { const el = document.getElementById('answer');"
        " return !!el && el.textContent.toLowerCase().includes(String(t).toLowerCase()); }",
        arg=needle,
        timeout=timeout,
    )


def _select_scope(page, scope: str) -> None:
    """Submit a retrieval-scope choice and wait for it to become active."""
    page.locator(f"button.scope-btn[value='{scope}']").click()
    expect = pytest.importorskip("playwright.sync_api").expect
    expect(page.locator(f"button.scope-btn[value='{scope}']")).to_have_attribute(
        "aria-pressed", "true"
    )


def test_browser_multiple_sources_individual_removal_and_default_scope(lumio_server):
    """Several Conversation Sources upload as chips, one is removed without
    disturbing the others, and the retrieval-scope selector appears with the
    Knowledge Base + chat default once a source is ready — and is absent before
    any source exists (#34)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    expect = sync_api.expect
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context()
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            page.goto(f"{lumio_server}/chat")

            # Before any source: no chips, no scope selector, no clear action.
            expect(page.locator(".chat-sources__scope")).to_have_count(0)
            expect(page.locator(".chat-sources__clear")).to_have_count(0)

            _upload_source(page, "alpha.md", "# Alpha\nThe alpha code is Aurora.")
            # The first ready source reveals the scope selector; the default is
            # Knowledge Base + chat files.
            expect(page.locator(".chat-sources__scope")).to_be_visible()
            expect(page.locator("button.scope-btn[value='kb_and_chat']")).to_have_attribute(
                "aria-pressed", "true"
            )

            _upload_source(
                page, "beta.txt", "The beta code is Borealis.", content_type="text/plain"
            )
            expect(page.locator(".chat-source")).to_have_count(2)

            # Remove only alpha; beta must remain untouched.
            page.locator(".chat-source__remove[aria-label='Remove alpha.md']").click()
            expect(page.locator(".chat-source", has_text="alpha.md")).to_have_count(0)
            expect(page.locator(".chat-source", has_text="beta.txt")).to_have_count(1)

            # The remaining source still answers a chat-grounded question.
            _ask(page, "What is the beta code?")
            _answer_includes(page, "Borealis")


def test_browser_clear_chat_files_removes_all_and_hides_scope(lumio_server):
    """The Clear chat files action removes every Conversation Source, hides the
    retrieval-scope selector, and updates retrieval immediately so a private
    question is no longer covered (#34)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    expect = sync_api.expect
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context()
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            page.goto(f"{lumio_server}/chat")

            _upload_source(page, "a.md", "The value is 42.")
            _upload_source(page, "b.md", "The name is Zephyr.")
            expect(page.locator(".chat-source")).to_have_count(2)
            expect(page.locator(".chat-sources__scope")).to_be_visible()

            page.locator(".chat-sources__clear-btn").click()
            expect(page.locator(".chat-source")).to_have_count(0)
            expect(page.locator(".chat-sources__empty")).to_be_visible()
            # No ready source remains: the selector and clear action disappear.
            expect(page.locator(".chat-sources__scope")).to_have_count(0)
            expect(page.locator(".chat-sources__clear")).to_have_count(0)

            # Retrieval immediately reflects the cleared Chat Context.
            _ask(page, "What is the value?")
            _answer_includes(page, "not covered")


def test_browser_scope_switching_restricts_retrieval(lumio_server):
    """An explicit retrieval scope is honored: chat-files-only cannot retrieve
    Knowledge Base Evidence and Knowledge-Base-only cannot retrieve Conversation
    Source Evidence (#34)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    expect = sync_api.expect
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context()
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            page.goto(f"{lumio_server}/chat")

            _upload_source(page, "secret.md", "The private codename is Zephyr.")
            # The default scope with a ready source is Knowledge Base + chat.
            expect(page.locator("button.scope-btn[value='kb_and_chat']")).to_have_attribute(
                "aria-pressed", "true"
            )

            # Chat files only: the chat source answers; the Knowledge Base cannot.
            _select_scope(page, "chat_only")
            _ask(page, "What is the private codename?")
            _answer_includes(page, "Zephyr")
            _ask(page, "What technology does Lumio use for retrieval?")
            _answer_includes(page, "not covered")

            # Knowledge Base only: the Conversation Source is hidden.
            _select_scope(page, "kb_only")
            _ask(page, "What is the private codename?")
            _answer_includes(page, "not covered")
            _ask(page, "What technology does Lumio use for retrieval?")
            _answer_includes(page, "LanceDB")


def test_browser_scope_selection_persists_across_reload(lumio_server):
    """An explicit retrieval-scope selection survives a page reload and keeps
    governing retrieval (#34)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    expect = sync_api.expect
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context()
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            page.goto(f"{lumio_server}/chat")

            _upload_source(page, "s.md", "Some private content about Aurora.")
            _select_scope(page, "chat_only")

            # Reload: the persisted chat-only scope is still the active button.
            page.reload()
            expect(page.locator("button.scope-btn[value='chat_only']")).to_have_attribute(
                "aria-pressed", "true"
            )
            expect(page.locator("button.scope-btn[value='kb_and_chat']")).to_have_attribute(
                "aria-pressed", "false"
            )

            # And it still governs retrieval: a Knowledge-Base question is not covered.
            _ask(page, "What technology does Lumio use for retrieval?")
            _answer_includes(page, "not covered")


def test_browser_combined_citations_present_chat_without_compiled_page_controls(lumio_server):
    """A combined Knowledge Base + chat answer cites both, and the chat-file
    citation keeps its temporary-source identity (filename, "This chat") without
    Compiled Page Reading Room, full-page, or Constellation controls (#34)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    expect = sync_api.expect
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context()
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            page.goto(f"{lumio_server}/chat")

            _upload_source(page, "notes.md", "Lumio uses LanceDB for retrieval and Stario for web.")
            _ask(page, "What technology does Lumio use for retrieval?")
            page.locator(".cite-card__row").first.wait_for(state="visible", timeout=15_000)

            rows = page.locator(".cite-card__row")
            # The Conversation Source citation: temporary identity, no Compiled
            # Page affordances.
            chat_row = rows.filter(has=page.locator(".cite-card__origin"))
            expect(chat_row).to_have_count(1)
            expect(chat_row).to_contain_text("This chat")
            expect(chat_row).to_contain_text("notes.md")
            expect(chat_row.locator(".cite-card__open")).to_have_count(0)
            expect(chat_row.locator(".cite-card__fullpage")).to_have_count(0)
            expect(chat_row.locator(".cite-card__constellation")).to_have_count(0)
            expect(chat_row.locator(".cite-card__source")).to_have_count(1)

            # A Knowledge Base citation is also present and carries its Compiled
            # Page Reading Room control — the combined answer cites both origins.
            kb_rows = rows.filter(has=page.locator(".cite-card__open"))
            expect(kb_rows.first).to_be_visible()
            expect(kb_rows.filter(has_text="Technology Stack").first).to_be_visible()


def test_browser_source_chips_wrap_within_narrow_viewport(lumio_server):
    """On a narrow viewport, source chips reflow onto multiple rows without
    producing any horizontal overflow — the mobile-safe wrapping from #34."""
    sync_api = pytest.importorskip("playwright.sync_api")
    expect = sync_api.expect
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        with browser:
            context = browser.new_context(viewport={"width": 360, "height": 720})
            _setup(context, lumio_server)
            _login_reader(context, lumio_server)
            page = context.new_page()
            page.goto(f"{lumio_server}/chat")

            for name in (
                "alpha-private-notes.md",
                "beta-internal-report.md",
                "gamma-secret.md",
                "delta-confidential.md",
            ):
                _upload_source(page, name, f"# {name}\nPrivate content for {name}.")

            expect(page.locator(".chat-source")).to_have_count(4)

            # The document never grows a horizontal scrollbar on a narrow display.
            page.wait_for_function(
                "() => document.documentElement.scrollWidth <= window.innerWidth + 1",
                timeout=5_000,
            )
            # The chips region itself does not overflow horizontally.
            page.wait_for_function(
                "() => { const c = document.querySelector('.chat-sources__chips');"
                " return !!c && c.scrollWidth <= c.clientWidth + 1; }",
                timeout=5_000,
            )
            # Chips actually wrapped: at least two occupy different rows.
            wrapped = page.evaluate(
                """() => {
                  const chips = Array.from(document.querySelectorAll('.chat-source'));
                  const rows = new Set(chips.map((c) => c.offsetTop));
                  return chips.length >= 2 && rows.size >= 2;
                }"""
            )
            assert wrapped
