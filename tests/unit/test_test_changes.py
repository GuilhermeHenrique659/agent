from autodev.config.models import DEFAULT_TEST_GLOBS
from autodev.pipeline.test_changes import diff_of_test_files, truncate_diff

DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-a
+b
diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1 +1 @@
-old
+new
"""


def test_keeps_only_test_file_sections() -> None:
    text = diff_of_test_files(DIFF, DEFAULT_TEST_GLOBS)
    assert "tests/test_app.py" in text and "+new" in text
    assert "src/app.py" not in text


def test_truncate_keeps_file_headers() -> None:
    big = DIFF + "".join(f"+line {i}\n" for i in range(500))
    text = truncate_diff(big, limit=300)
    assert "+++ b/src/app.py" in text and "+++ b/tests/test_app.py" in text
    assert "... (truncated)" in text
    assert len(text) < 500
    assert truncate_diff(DIFF, limit=10000) == DIFF
