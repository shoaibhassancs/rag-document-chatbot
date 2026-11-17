"""Regression test: the page must find `ui` when started the way Streamlit starts it.

`streamlit run ui/streamlit_app.py` puts the script's own folder (ui/) first on sys.path, not the
project root, and the current directory may be anywhere. pytest and AppTest start from the project
root, so they hid a `ModuleNotFoundError: No module named 'ui'` on the page's import line.

This test runs the import block of the page file in a fresh interpreter (`-I`: no PYTHONPATH, no
current directory on the path) from an unrelated directory, with only ui/ on sys.path. It needs no
network and starts no server. It stops before `st.set_page_config`, so none of the page runs.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "ui" / "streamlit_app.py"

HARNESS = """
import sys
ui_dir, page = sys.argv[1], sys.argv[2]
sys.path.insert(0, ui_dir)  # what `streamlit run` does: the script's folder first
source = open(page, encoding="utf-8").read()
imports_only = source.split("st.set_page_config(")[0]  # the import block, nothing else
scope = {"__name__": "__main__", "__file__": page}
exec(compile(imports_only, page, "exec"), scope)
print("RESOLVED", scope["view"].__file__, scope["client_from_env"].__module__)
"""


def run_like_streamlit(tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", HARNESS, str(PAGE.parent), str(PAGE)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},  # no PYTHONPATH
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_from_ui_import_view_resolves_when_streamlit_starts_the_page(tmp_path):
    result = run_like_streamlit(tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    line = next(x for x in result.stdout.splitlines() if x.startswith("RESOLVED"))
    assert str(ROOT / "ui" / "view.py") in line and "ui.api_client" in line


def test_the_page_source_still_has_the_import_block_the_harness_cuts_at():
    # If this marker moves, the harness above would test the wrong thing.
    assert PAGE.read_text(encoding="utf-8").count("st.set_page_config(") == 1
