"""The README skeleton: its curl examples match the real routes, and it claims nothing that needs a
real run."""

import re
from pathlib import Path

from app.main import create_app

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()
CURLS = re.findall(r"```bash\n(.*?)```", README, re.DOTALL)


def curl_commands() -> list[str]:
    """Each curl command as one string (continuation lines joined)."""
    commands = []
    for block in CURLS:
        joined = re.sub(r"\\\n\s*", " ", block)
        commands += [
            line.strip() for line in joined.splitlines() if line.strip().startswith("curl")
        ]
    return commands


def routes() -> set[tuple[str, str]]:
    """(METHOD, path) pairs the app really serves, read from its OpenAPI description."""
    found = set()
    for path, operations in create_app().openapi()["paths"].items():
        for method in operations:
            found.add((method.upper(), path))
    return found


def parse(command: str) -> tuple[str, str]:
    method = re.search(r"-X (\w+)", command)
    url = re.search(r"https?://[^\s\"']+", command).group(0)
    path = re.sub(r"^https?://[^/]+", "", url)
    path = re.sub(r"<document-id>", "{document_id}", path)
    has_body = any(flag in command for flag in ("-d ", "-F "))
    return (method.group(1) if method else ("POST" if has_body else "GET")), path


def test_there_are_curl_examples_for_every_endpoint():
    paths = {parse(c)[1] for c in curl_commands()}
    assert paths == {"/chat", "/ingest", "/documents", "/documents/{document_id}", "/health"}


def test_every_curl_example_uses_a_real_method_and_path():
    real = routes()
    for command in curl_commands():
        assert parse(command) in real, command


def test_the_key_header_is_used_exactly_where_the_api_requires_it():
    for command in curl_commands():
        method, path = parse(command)
        needs_key = path == "/ingest" or method == "DELETE"
        assert ("X-API-Key" in command) == needs_key, command
        assert "$APP_API_KEY" in command or not needs_key


def test_the_chat_example_has_the_required_json_fields():
    chat = next(c for c in curl_commands() if "/chat" in c)
    assert (
        '"session_id"' in chat and '"message"' in chat and "Content-Type: application/json" in chat
    )


def test_the_upload_examples_use_the_real_form_field_names():
    commands = [c for c in curl_commands() if "/ingest" in c]
    assert any('-F "files=@' in c for c in commands) and any('-F "url=' in c for c in commands)


def test_no_key_value_or_secret_shaped_string_is_in_the_readme():
    assert not re.search(r"(gsk_|pa-|sk-)[A-Za-z0-9_-]{8,}", README)
    assert not re.search(r"(GROQ|VOYAGE)_API_KEY\s*=\s*\S+", README)


def test_every_result_that_needs_a_real_run_says_tbd():
    assert README.count("TBD: from the real evaluation") >= 6
    for metric in (
        "Retrieval hit rate",
        "Faithfulness",
        "Answer correctness",
        "Refusal accuracy",
        "Latency",
    ):
        assert re.search(rf"{metric}: TBD: from the real evaluation", README), metric


def test_the_readme_quotes_no_measured_number():
    assert not re.search(r"\d+(\.\d+)?\s?%", README)  # no percentages
    assert not re.search(r"\d+(\.\d+)?\s?(ms|seconds?|sec)\b", README)  # no latencies
    assert not re.search(r"(?i)\bp50\b|\bp95\b", README)
    assert "no evaluation has been run and no score exists" in README


def test_the_required_headings_exist_and_production_notes_is_empty():
    for heading in (
        "What this is",
        "Demo scenario",
        "Architecture",
        "Quick start",
        "API",
        "The UI",
        "Configuration",
        "How it answers",
        "Evaluation",
        "Tests",
        "Known limitations",
        "Production notes",
    ):
        assert f"\n## {heading}\n" in README, heading
    assert "## Production notes\n\nTBD: Milestone 7." in README
    assert "SKELETON" in README.splitlines()[2]


def test_the_ui_section_does_not_claim_the_ui_has_been_run():
    section = README.split("## The UI")[1].split("## Configuration")[0]
    assert "TBD" in section and "screenshots" in section
