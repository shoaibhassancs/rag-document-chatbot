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


def _evaluation_section() -> str:
    start = README.index("## Evaluation")
    return README[start : README.index("## Tests")]


def test_the_evaluation_section_has_the_results_table_and_the_honesty_statements():
    section = _evaluation_section()
    for text in ("Run A", "runA2", "runB", "not a held-out estimate", "hand-reviewed"):
        assert text in section, text
    for metric in ("Retrieval hit rate", "Faithfulness", "Answer correctness", "Refusal accuracy"):
        assert metric in section, metric
    assert "TBD: from the real evaluation" not in section


def test_measured_numbers_appear_only_in_the_evaluation_section():
    rest = README.replace(_evaluation_section(), "")
    assert not re.search(r"\d+(\.\d+)?\s?%", rest)  # no percentages outside the results
    assert not re.search(r"\d+(\.\d+)?\s?(ms|seconds?|sec)\b", rest)
    assert not re.search(r"(?i)\bp50\b|\bp95\b", rest)


def test_the_quoted_results_match_the_committed_summary_files():
    from pathlib import Path

    results = Path(__file__).resolve().parents[1] / "eval" / "results"
    for name, numbers in (
        ("runA-20b.md", ("69.0%", "39.3%", "69.4%", "55.0%")),
        ("runA2-20b.md", ("79.3%", "69.0%", "80.6%", "72.5%")),
        ("runB-120b.md", ("82.8%", "71.4%", "83.3%", "75.0%")),
    ):
        files = list(results.glob(f"*{name}"))
        if not files:  # the summaries are tracked, but skip rather than fail in a partial checkout
            continue
        text = files[0].read_text()
        for number in numbers:
            assert number in text and number in README, (name, number)


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
    notes = README.split("\n## Production notes\n")[1].split("\n## Data")[0]
    assert "TBD" not in notes and "X-API-Key" in notes and "held-out" in notes
    assert "STATUS" in README.splitlines()[2]


def test_the_ui_section_embeds_existing_screenshots_and_keeps_the_unverified_parts():
    from pathlib import Path

    section = README.split("## The UI")[1].split("## Configuration")[0]
    images = re.findall(r"!\[[^\]]*\]\((docs/media/[^)]+)\)", section)
    assert images
    root = Path(__file__).resolve().parents[1]
    for image in images:
        assert (root / image).is_file(), image
    assert "Checked on Nov 19, 2026" in section and "Not verified" in section
    assert "uv.lock" in section and "TBD" not in section


def test_the_readme_embeds_the_architecture_picture_and_it_exists():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    section = README.split("## Architecture")[1].split("## Quick start")[0]
    assert "![" in section and "(docs/architecture.png)" in section
    assert (root / "docs" / "architecture.png").is_file()
    assert "updated in Milestone 7" not in README


def test_the_case_study_exists_is_linked_and_has_no_placeholder():
    study_path = Path(__file__).resolve().parent.parent / "docs" / "CASE_STUDY.md"
    study = study_path.read_text(encoding="utf-8")
    assert "docs/CASE_STUDY.md" in README
    assert "held-out" in study
    assert "TBD" not in study


def test_the_demo_video_is_small_linked_and_listed():
    media = Path(__file__).resolve().parent.parent / "docs" / "media"
    video = media / "demo.mp4"
    assert video.is_file() and video.stat().st_size < 10 * 1024 * 1024
    assert "docs/media/demo.mp4" in README
    assert "`demo.mp4`" in (media / "README.md").read_text(encoding="utf-8")
