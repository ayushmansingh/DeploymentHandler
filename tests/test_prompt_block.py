"""The prompt block is the main defence against unusable ZIPs.

It is shown in the dashboard and kept in PROMPT.md; if those two drift, the
copy someone pastes stops matching what the launcher enforces.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROMPT_MD = REPO / "PROMPT.md"
# The prompt and the upload form live on the Deploy tab.
DEPLOY = REPO / "launcher" / "templates" / "deploy.html"


def canonical_block() -> str:
    return PROMPT_MD.read_text().split("```text\n", 1)[1].split("\n```", 1)[0]


def dashboard_block() -> str:
    html = DEPLOY.read_text()
    start = html.index('<pre class="log" id="prompt-block">') + len(
        '<pre class="log" id="prompt-block">'
    )
    return html[start : html.index("</pre>", start)]


def test_the_deploy_page_shows_the_canonical_prompt():
    assert dashboard_block() == canonical_block()


def test_prompt_states_the_rules_the_launcher_actually_enforces():
    block = canonical_block()
    # Each of these corresponds to a check that fails a deploy.
    assert "/api" in block
    assert "NEVER http://localhost:8000" in block
    assert '"build" script' in block
    assert "node_modules" in block
    assert "app = FastAPI()" in block
    assert "os.environ" in block


def test_prompt_warns_about_the_limits_of_native_mode():
    block = canonical_block()
    assert "WebSockets" in block
    assert "APP_DATA_DIR" in block
    assert "erased on the next upload" in block


PIPELINE_MD = REPO / "PIPELINE.md"


def test_pipeline_doc_matches_the_code_it_describes():
    """It is pasted into an AI to debug failures, so wrong facts mislead."""
    from launcher import config, deployer, detect

    doc = PIPELINE_MD.read_text()

    # Values quoted in the document that are easy to change and forget.
    assert f"{config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB" in doc
    assert f"{config.MAX_ARCHIVE_ENTRIES:,} files" in doc
    assert f"{deployer.VERIFY_TIMEOUT_SECONDS} seconds" in doc
    assert f"every {config.SUPERVISOR_INTERVAL_SECONDS} seconds" in doc
    assert f"{config.MEMORY_STRIKES_BEFORE_RESTART} consecutive checks" in doc
    assert f"{config.KEEP_VERSIONS} per app" in doc

    # Every junk directory the extractor drops is listed.
    for junk in config.JUNK_DIRS:
        assert junk in doc, f"{junk} is stripped but not documented"

    # Every file the detector reads to find the start command.
    for entry in ("main.py", "app.py", "server.py", "api.py", "run.py"):
        assert entry in doc

    # Both halves of the directory-name preference lists.
    for hint in detect.BACKEND_HINTS + detect.FRONTEND_HINTS:
        assert f"`{hint}/`" in doc, f"{hint}/ is searched but not documented"


def test_prompt_step_comes_before_the_upload_form():
    """People must meet the rules before the box that ignores them."""
    html = DEPLOY.read_text()
    assert html.index("copy-prompt") < html.index('action="/upload"')


def test_copy_button_reads_text_not_rendered_content():
    """A collapsed <details> is not rendered, so innerText would copy nothing."""
    html = DEPLOY.read_text()
    copy_section = html[html.index('id="copy-prompt"'):]
    # Comments explain why innerText is wrong, so judge the code alone.
    code = "\n".join(
        line for line in copy_section.splitlines()
        if not line.strip().startswith("//")
    )
    assert "textContent" in code
    assert "innerText" not in code
