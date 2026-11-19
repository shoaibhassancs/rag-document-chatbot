"""Static checks on the UI packaging and on how the page draws text. Nothing is built or run.
Comments and docstrings are ignored: the checks look at real code (via ast) and real YAML lines."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_DOCKERFILE = (ROOT / "Dockerfile.ui").read_text()
PAGE_SOURCE = (ROOT / "ui" / "streamlit_app.py").read_text()
PAGE = ast.parse(PAGE_SOURCE)


def compose_code() -> str:
    """docker-compose.yml without comment lines or trailing comments."""
    lines = []
    for line in (ROOT / "docker-compose.yml").read_text().splitlines():
        if line.strip().startswith("#"):
            continue
        lines.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(lines)


def ui_block() -> str:
    return compose_code().split("\n  ui:\n")[1].split("\nvolumes:")[0]


def calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def imports(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_the_ui_service_is_behind_a_profile_so_plain_compose_up_is_unchanged():
    code = compose_code()
    assert code.count("profiles:") == 1 and 'profiles: ["ui"]' in ui_block()
    before_ui = code.split("\n  ui:\n")[0]
    assert "profiles" not in before_ui and "  db:" in before_ui and "  api:" in before_ui


def test_the_ui_container_gets_only_the_app_key_never_the_env_file():
    block = ui_block()
    assert "env_file" not in block and "volumes" not in block
    assert "APP_API_KEY: ${APP_API_KEY:-}" in block and "API_BASE_URL: http://api:8000" in block
    for provider_key in ("GROQ", "VOYAGE", "GEMINI", "DATABASE_URL"):
        assert provider_key not in block


def test_the_ui_port_avoids_the_ports_already_in_use():
    assert "${UI_HOST_PORT:-8510}:8501" in ui_block()
    in_use = {"5432", "5433", "5440", "8000", "8010", "8080"}
    assert "8510" not in in_use and not any(f":{p}:" in ui_block() for p in in_use)


def test_compose_interpolation_cannot_break_a_plain_compose_up():
    code = compose_code()
    # Changed deliberately (INSTRUCTIONS_23): the default is the version locked in uv.lock.
    assert "${STREAMLIT_VERSION:-1.66.0}" in code and ":?" not in code  # no required variable
    assert 'name = "streamlit"\nversion = "1.66.0"' in (ROOT / "uv.lock").read_text()


def test_the_ui_dockerfile_is_marked_partly_verified_with_a_required_version_argument():
    # Changed deliberately (INSTRUCTIONS_25): the owner built it and saw 1.66.0.
    assert UI_DOCKERFILE.startswith("# The image builds and runs")
    assert "uv.lock" in UI_DOCKERFILE and "never been started" not in UI_DOCKERFILE
    assert re.search(r"^ARG STREAMLIT_VERSION$", UI_DOCKERFILE, re.MULTILINE)  # no default value
    assert 'test -n "$STREAMLIT_VERSION"' in UI_DOCKERFILE
    assert "streamlit==${STREAMLIT_VERSION}" in UI_DOCKERFILE and "COPY .env" not in UI_DOCKERFILE


def test_only_the_page_imports_streamlit():
    for name in ("__init__.py", "api_client.py", "view.py"):
        tree = ast.parse((ROOT / "ui" / name).read_text())
        assert "streamlit" not in imports(tree), name
        assert "app" not in imports(tree), name  # the UI never imports the backend
    assert "streamlit" in imports(PAGE) and "app" not in imports(PAGE)


def test_the_page_draws_model_and_document_text_only_as_plain_text():
    markdown = [c for c in calls(PAGE) if dotted(c.func) == "st.markdown"]
    assert len(markdown) == 1  # the badge, built from constants
    assert [dotted(a.func) for a in markdown[0].args if isinstance(a, ast.Call)] == [
        "view.badge_html"
    ]
    unsafe = [k for c in calls(PAGE) for k in c.keywords if k.arg == "unsafe_allow_html"]
    assert len(unsafe) == 1 and markdown[0].keywords and unsafe[0] in markdown[0].keywords
    used = {dotted(c.func) for c in calls(PAGE)}
    for forbidden in ("st.write", "st.html", "st.code", "st.latex", "st.write_stream"):
        assert forbidden not in used
    assert "st.text" in used  # model text goes here


def test_the_page_never_shows_or_stores_the_key():
    attributes = {n.attr for n in ast.walk(PAGE) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(PAGE) if isinstance(n, ast.Name)}
    assert "environ" not in attributes and "os" not in names
    assert not any("key" in a.lower() and a != "has_key" for a in attributes)
    stored = [
        n
        for n in ast.walk(PAGE)
        if isinstance(n, ast.Assign)
        and any(dotted(t).startswith("st.session_state") for t in n.targets)
    ]
    assert {dotted(t) for n in stored for t in n.targets} <= {
        "st.session_state.session_id",
        "st.session_state.history",
    }


def test_the_page_docstring_says_how_it_was_run():
    doc = ast.get_docstring(PAGE) or ""
    assert "AppTest" in doc and "UNVERIFIED" not in doc
