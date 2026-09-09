"""The prompt block is the main defence against unusable ZIPs.

It is shown in the dashboard and kept in PROMPT.md; if those two drift, the
copy someone pastes stops matching what the launcher enforces.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROMPT_MD = REPO / "PROMPT.md"
INDEX = REPO / "launcher" / "templates" / "index.html"


def canonical_block() -> str:
    return PROMPT_MD.read_text().split("```text\n", 1)[1].split("\n```", 1)[0]


def dashboard_block() -> str:
    html = INDEX.read_text()
    start = html.index('<pre class="log" id="prompt-block">') + len(
        '<pre class="log" id="prompt-block">'
    )
    return html[start : html.index("</pre>", start)]


def test_dashboard_shows_the_canonical_prompt():
    assert dashboard_block() == canonical_block()


def test_prompt_states_the_rules_the_launcher_actually_enforces():
    block = canonical_block()
    # Each of these corresponds to a check that fails a deploy.
    assert "/api" in block
    assert "NEVER http://localhost:8000" in block
    assert '"build" script' in block
    assert "node_modules" in block
    assert "app = FastAPI()" in block
    assert "no .env file" in block


def test_prompt_warns_about_the_limits_of_native_mode():
    block = canonical_block()
    assert "WebSockets" in block
    assert "APP_DATA_DIR" in block
    assert "erased on the next upload" in block
