"""End-to-end MinIO certification of the S3 coding-agent journey (issue #166).

Certifies the COMPLETE ``lumio-wiki`` + ``lumio-lancedb`` journey from an
empty project to fresh-harness retrieval and source inspection — every CLI
phase runs as a FRESH SUBPROCESS against a real MinIO boundary, so nothing
relies on in-process state (issue #166 AC1/AC4):

1. Maintainer ``setup`` with a local worktree, S3 publication, remote
   LanceDB retrieval, private Source Artifact Store, required retention, and
   a project-scope Agent Skill install.
2. A restarted harness (new process, no prior session context) discovers the
   KB and configuration from ``.env`` alone (pathless commands).
3. Managed ingest of a mixed-format raw source (Markdown + binary), proposal
   review (list/inspect/validate), publish, and private Source Artifact
   retention.
4. One complete immutable S3 version with a built and health-checked remote
   LanceDB index is published and activated.
5. A separate read-only coding-agent project binds the S3 Location and
   retrieves citation-ready Evidence through remote LanceDB.
6. ``source inspect``/``fetch``/``link`` resolve the exact Source Artifact
   through the private Source Binding Manifest; the signed link is a
   short-lived exact-object grant.
7. A replacement Source Version publishes v2; the historical v1 Published
   Version still fetches the historical bytes.
8. Publication conflict, rollback, orphan (interrupted-build) reporting, and
   the zero-index fallback all behave (journey step 11).

Signed-link EXPIRY and full cross-role credential denial (KB Reader / source
writer / source inspector) are certified in ``test_artifact_store_minio.py``
and are not duplicated here. The install shape of the journey (only
``lumio-wiki``, ``lumio-lancedb``, their extras, and the Agent Skill — never
the full ``lumio`` app) is certified in ``test_wheel_isolation.py``.

Skips the whole module unless ``LUMIO_S3_ENDPOINT`` is configured, mirroring
the other MinIO suites. Embeddings stay deterministic: the published remote
index is the lexical BM25 build (no embedder, no provider).
"""

from __future__ import annotations

import os as _os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

obstore = pytest.importorskip("obstore", reason="obstore required for MinIO integration")
pytest.importorskip("lumio_lancedb", reason="lumio-lancedb required for the S3 journey")

if not _os.environ.get("LUMIO_S3_ENDPOINT"):  # pragma: no cover
    pytest.skip(
        "LUMIO_S3_ENDPOINT not set; skipping S3 coding-agent journey MinIO tests",
        allow_module_level=True,
    )

_RUN_PREFIXES: list[tuple[object, str]] = []


def _bucket() -> str:
    return _os.environ.get("LUMIO_S3_TEST_BUCKET", "lumio-wiki-it")


@pytest.fixture(scope="module")
def _root_store():
    endpoint = _os.environ["LUMIO_S3_ENDPOINT"]
    config = {
        "aws_region": _os.environ.get("LUMIO_S3_REGION", "us-east-1"),
        "aws_endpoint": endpoint,
        "aws_access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "aws_secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    client_options: dict[str, object] = {}
    if endpoint.startswith("http://"):
        client_options["allow_http"] = True
    yield obstore.store.from_url(f"s3://{_bucket()}", config=config, client_options=client_options)
    # Best-effort cleanup of every prefix this module created.
    try:  # pragma: no cover - cleanup is best-effort
        for store, prefix in _RUN_PREFIXES:
            for batch in obstore.list(store, prefix=prefix):
                for obj in batch:
                    obstore.delete(store, obj["path"])
    except Exception:  # pragma: no cover
        pass


def _hermetic_child_env() -> dict[str, str]:
    """Hermetic child env: keep the MinIO ``LUMIO_S3_*`` deployment settings
    the journey needs, but strip every other inherited ``LUMIO_*`` config
    var — another suite's conftest (e.g. tests/conftest.py setting
    ``LUMIO_KB_PATH`` to a fixture KB) must not redirect this journey.
    Project configuration comes from the ``.env`` setup wrote (docstring).
    """
    return {
        key: value
        for key, value in _os.environ.items()
        if not (key.startswith("LUMIO_") and not key.startswith("LUMIO_S3_"))
    }


def _run(
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    expect_rc: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run the ``lumio-wiki`` CLI as a FRESH subprocess (issue #166 AC4).

    The ambient environment carries the MinIO ``LUMIO_S3_*`` deployment
    settings; project configuration (KB path, publication destination,
    retrieval backend, source store) must come from the project ``.env``
    that ``setup`` wrote — exactly what a restarted harness session sees.
    """
    if env is None:
        env = _hermetic_child_env()
    result = subprocess.run(
        [sys.executable, "-m", "lumio_wiki.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == expect_rc, (
        f"lumio-wiki {' '.join(args)} (cwd={cwd}) exited {result.returncode}, "
        f"expected {expect_rc}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _authored_page(title: str, source_id: str, body: str) -> str:
    return (
        "---\n"
        f'id: "entity:{source_id}"\n'
        f'title: "{title}"\n'
        "entity_types:\n  - document\n"
        "aliases: []\n"
        'tags:\n  - "journey"\n'
        f'summary: "Authored from {source_id} for the S3 journey."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        'category: "references"\n'
        'type: "source-digest"\n'
        'durability_rationale: "Distilled from a stable original Knowledge Source."\n'
        "sources:\n"
        f'  - id: "{source_id}"\n'
        f'    title: "{source_id} source"\n'
        "synthetic: false\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _staged_proposal_id(out: str) -> str:
    match = re.search(r"^Staged proposal (\S+)$", out, re.MULTILINE)
    assert match, f"could not read staged proposal id from output:\n{out}"
    return match.group(1)


def test_s3_coding_agent_journey(tmp_path, _root_store):
    run = uuid.uuid4().hex
    kb_prefix = f"journey166/{run}/kb"
    artifact_prefix = f"journey166/{run}/src"
    _RUN_PREFIXES.append((_root_store, f"journey166/{run}"))
    destination = f"s3://{_bucket()}/{kb_prefix}"
    source_store_uri = f"s3://{_bucket()}/{artifact_prefix}"

    maintainer = tmp_path / "maintainer-project"
    reader = tmp_path / "reader-project"
    maintainer.mkdir()
    reader.mkdir()
    staging = maintainer / "staging"
    staging.mkdir()

    # --- Journey step 2: Maintainer setup (local worktree, S3 publication,
    # remote LanceDB, private Source Artifact Store, project skill choice).
    out = _run(
        "setup", "./knowledge-base",
        "--publish-to", destination,
        "--retrieval", "lancedb",
        "--source-store", source_store_uri,
        "--artifact-retention", "required",
        "--skill-scope", "project",
        cwd=maintainer,
    ).stdout
    assert f"LUMIO_PUBLISH_TO={destination}" in out
    assert "LUMIO_SOURCE_STORE" in out
    env_text = (maintainer / ".env").read_text()
    for key in (
        "LUMIO_KB_PATH=", "LUMIO_PUBLISH_TO=", "LUMIO_RETRIEVAL_BACKEND=lancedb",
        "LUMIO_SOURCE_STORE=", "LUMIO_ARTIFACT_RETENTION=required",
    ):
        assert key in env_text, f".env missing {key}"
    # Setup never writes credentials (standard AWS resolution stays authoritative).
    assert _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"] not in env_text
    # The generated AGENTS.md records the S3 configuration but never the
    # private Source Artifact Store URI (ADR-0020 privacy guard).
    agents_md = (maintainer / "AGENTS.md").read_text()
    assert "LUMIO_PUBLISH_TO" in agents_md and "LUMIO_SOURCE_STORE" in agents_md
    assert source_store_uri not in agents_md
    assert "source inspect" in agents_md
    # The Agent Skill is part of the journey install (project scope).
    assert (maintainer / ".agents" / "skills" / "lumio-wiki" / "SKILL.md").is_file()

    # --- Journey step 2b: the Maintainer deliberately declares the KB-local
    # ontology (the setup seed carries an empty ontology; Entity Types are
    # reviewed content, ADR-0021). Authored pages carry `entity_types`.
    (maintainer / "knowledge-base" / "lumio.yaml").write_text(
        "version: 2\n"
        'mode: "categorized"\n'
        "categories:\n"
        "  - name: concepts\n"
        "  - name: entities\n"
        "  - name: references\n"
        "  - name: procedures\n"
        "  - name: tables\n"
        "  - name: datasets\n"
        "  - name: synthesis\n"
        "ontology:\n"
        "  entity_types:\n"
        "    document: {}\n",
        encoding="utf-8",
    )

    # --- Journey step 3: a restarted harness (fresh subprocess) discovers the
    # KB and configuration without prior session context.
    assert "Knowledge base is valid" in _run("validate", cwd=maintainer).stdout

    # --- Journey step 4: managed ingest of a mixed-format raw source — one
    # text file and one binary — with proposal review and private retention.
    handbook_md = staging / "handbook.md"
    handbook_md.write_text(
        "# Field Handbook\n\nRotate the sensors quarterly.\n", encoding="utf-8"
    )
    report_pdf = staging / "field-report.pdf"
    report_v1_bytes = b"%PDF-1.4 journey source artifact bytes v1"
    report_pdf.write_bytes(report_v1_bytes)
    handbook_page = staging / "handbook-page.md"
    handbook_page.write_text(
        _authored_page(
            "Field Handbook Notes", "handbook-src",
            "The original handbook instructs: rotate the sensors quarterly.",
        ),
        encoding="utf-8",
    )
    report_page = staging / "report-page.md"
    report_page.write_text(
        _authored_page("Field Report Digest", "report-src", "Digest of the field report."),
        encoding="utf-8",
    )
    proposal_ids = []
    for source_file, page_file, source_id in (
        (handbook_md, handbook_page, "handbook-src"),
        (report_pdf, report_page, "report-src"),
    ):
        out = _run(
            "ingest", "knowledge-base", str(source_file),
            "--compiled-page", str(page_file),
            "--source-id", source_id,
            cwd=maintainer,
        ).stdout
        assert f"source_id:      {source_id}" in out
        proposal_ids.append(_staged_proposal_id(out))
    listed = _run("proposal", "list", "knowledge-base", cwd=maintainer).stdout
    for proposal_id in proposal_ids:
        assert proposal_id in listed
        out = _run(
            "proposal", "inspect", "knowledge-base", proposal_id, cwd=maintainer
        ).stdout
        assert proposal_id in out and "source_id" in out
        assert "Knowledge base is valid" in _run(
            "proposal", "validate", "knowledge-base", proposal_id, cwd=maintainer
        ).stdout
        _run("publish", "knowledge-base", proposal_id, cwd=maintainer)

    # --- Journey step 5: publish one complete immutable S3 version (with the
    # remote LanceDB build) and activate it.
    out = _run(
        "publish-s3", "--version", "v1", "--retrieval", "lancedb", cwd=maintainer
    ).stdout
    assert "Published v1" in out
    assert "lance:       built and health-checked under derived/lance/" in out

    # --- Journey step 6: a separate read-only coding-agent project against
    # the S3 Location.
    out = _run(
        "setup", "--from", destination,
        "--retrieval", "lancedb",
        "--source-store", source_store_uri,
        "--skill-scope", "project",
        cwd=reader,
    ).stdout
    assert f"LUMIO_KB_PATH={destination}" in out
    reader_env = (reader / ".env").read_text()
    assert "LUMIO_RETRIEVAL_BACKEND=lancedb" in reader_env
    assert "LUMIO_SOURCE_STORE=" in reader_env
    assert "LUMIO_PUBLISH_TO" not in reader_env  # read-only project
    assert (reader / ".agents" / "skills" / "lumio-wiki" / "SKILL.md").is_file()
    reader_agents_md = (reader / "AGENTS.md").read_text()
    assert destination in reader_agents_md

    # --- Journey step 3 (reader side) + step 7: a fresh harness session
    # retrieves citation-ready Evidence through remote LanceDB (pathless).
    out = _run("search", "handbook sensors", cwd=reader).stdout
    assert "## Field Handbook Notes" in out
    assert ".md" in out
    assert "note:" not in out  # healthy remote index: no fallback disclosure
    # Doctor guidance is part of the journey contract: the reader install
    # reports both S3 capabilities present.
    out = _run("doctor", cwd=reader).stdout
    assert "extra[s3]: installed" in out
    assert "extra[lancedb]: installed" in out

    # --- Journey step 8: fetch the exact underlying Source Artifact; the
    # text original supports a bounded quote, the binary is byte-exact.
    out = _run("source", "inspect", "--source-id", "handbook-src", cwd=reader).stdout
    assert "source_id:       handbook-src" in out
    assert "bound_to:        published version v1 (Source Binding Manifest)" in out
    assert "authorization:   granted" in out
    fetched_text = reader / "handbook-original.md"
    _run("source", "fetch", "--source-id", "handbook-src",
         "--output", str(fetched_text), cwd=reader)
    assert "Rotate the sensors quarterly" in fetched_text.read_text(encoding="utf-8")
    fetched_pdf = reader / "field-report.pdf"
    _run("source", "fetch", "--source-id", "report-src",
         "--output", str(fetched_pdf), cwd=reader)
    assert fetched_pdf.read_bytes() == report_v1_bytes

    # --- Journey step 9: a five-minute signed download link for the same
    # exact artifact (expiry + cross-role denial are certified in
    # test_artifact_store_minio.py; here: the link serves the exact bytes and
    # authorizes exactly one object).
    out = _run("source", "link", "--source-id", "report-src",
               "--expires", "5m", cwd=reader).stdout
    url = out.splitlines()[0]
    assert url.startswith("http")
    assert "bearer secret" in out
    assert _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"] not in url
    assert urllib.request.urlopen(url, timeout=10).read() == report_v1_bytes
    match = re.search(r"/([0-9a-f]{64})(?:\?|$)", url)
    # ADR-0020: the URL is a bearer secret — the failure message must not
    # embed it (redacted diagnostics), so assert on shape, not the URL.
    assert match, "signed URL has no digest path segment (URL redacted)"
    digest = match.group(1)
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url.replace(digest, "0" * 64), timeout=10)

    # --- Journey step 10: a replacement Source Version publishes v2; the
    # historical v1 Published Version still fetches the historical bytes.
    # The documented replacement workflow (#149 lifecycle): retire, reactivate
    # with the new bytes, then managed-ingest the revised page (idempotent
    # version reuse retains the replacement artifact for required retention).
    report_v2_bytes = b"%PDF-1.4 journey source artifact bytes v2 (replacement)"
    report_pdf.write_bytes(report_v2_bytes)
    revised_page = staging / "report-page-v2.md"
    revised_page.write_text(
        _authored_page(
            "Field Report Digest", "report-src",
            "Digest of the REPLACEMENT field report.",
        ),
        encoding="utf-8",
    )
    out = _run(
        "source", "retire", "knowledge-base", "--source-id", "report-src", cwd=maintainer
    ).stdout
    _run("publish", "knowledge-base", _staged_proposal_id(out), cwd=maintainer)
    out = _run(
        "source", "reactivate", "knowledge-base",
        "--source-id", "report-src", "--file", str(report_pdf),
        cwd=maintainer,
    ).stdout
    _run("publish", "knowledge-base", _staged_proposal_id(out), cwd=maintainer)
    out = _run(
        "ingest", "knowledge-base", str(report_pdf),
        "--compiled-page", str(revised_page),
        "--source-id", "report-src",
        cwd=maintainer,
    ).stdout
    replacement_id = _staged_proposal_id(out)
    _run("publish", "knowledge-base", replacement_id, cwd=maintainer)
    _run("publish-s3", "--version", "v2", "--retrieval", "lancedb", cwd=maintainer)
    # The active Published Version (v2) serves the replacement bytes...
    _run("source", "fetch", "--source-id", "report-src",
         "--output", str(fetched_pdf), cwd=reader)
    assert fetched_pdf.read_bytes() == report_v2_bytes
    # ...and the historical v1 binding still fetches the historical bytes.
    historical = reader / "field-report-v1.pdf"
    _run("source", "fetch", "--source-id", "report-src",
         "--published-version", "v1", "--output", str(historical), cwd=reader)
    assert historical.read_bytes() == report_v1_bytes
    out = _run("source", "inspect", "--source-id", "report-src",
               "--published-version", "v1", cwd=reader).stdout
    assert "bound_to:        published version v1 (Source Binding Manifest)" in out

    # --- Journey step 11 legs: conflict, rollback, orphan reporting, and the
    # zero-index fallback — from the same real destination.
    # Re-publishing an existing immutable version is refused outright.
    result = _run("publish-s3", "--version", "v1", cwd=maintainer, expect_rc=2)
    assert "already exists" in (result.stdout + result.stderr)
    assert "cannot be overwritten" in (result.stdout + result.stderr)
    out = _run("cleanup-s3", destination, cwd=maintainer).stdout
    assert "No cleanup candidates" in out
    # An interrupted publication leaves an incomplete version prefix: the
    # report-only cleanup surface names it and deletes nothing. The stray
    # object's nested path is copied from the OBSERVED listing of a real
    # published version, so the test never assumes private object-key layout
    # beyond what the public listing surface returns.
    published_keys = [
        obj["path"] for batch in obstore.list(_root_store, prefix=kb_prefix) for obj in batch
    ]
    assert published_keys
    nested = min(published_keys, key=len)[len(kb_prefix) + 1 :].split("/", 1)[1]
    orphan_key = f"{kb_prefix}/v9-interrupted/{nested}"
    obstore.put(_root_store, orphan_key, b"orphaned bytes from an interrupted build")
    try:
        out = _run("cleanup-s3", destination, cwd=maintainer).stdout
        assert "v9-interrupted" in out
    finally:
        obstore.delete(_root_store, orphan_key)
    # Rollback re-activates the prior complete version atomically...
    _run("rollback-s3", destination, "--version", "v1", cwd=maintainer)
    out = _run("source", "inspect", "--source-id", "report-src", cwd=reader).stdout
    assert "published version v1 (Source Binding Manifest)" in out
    # ...and the zero-index backend still serves Evidence from the same S3
    # Location without any index (offline/local behavior stays green).
    zero_env = _hermetic_child_env()
    zero_env["LUMIO_RETRIEVAL_BACKEND"] = "zero-index"
    out = _run("search", "handbook sensors", cwd=reader, env=zero_env).stdout
    assert "## Field Handbook Notes" in out
