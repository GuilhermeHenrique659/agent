from autodev.config.models import DEFAULT_TEST_GLOBS, TddConfig
from autodev.gates.forbidden import forbidden_additions

PATTERNS = TddConfig().forbidden_patterns

DIFF = """\
diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,0 +2,2 @@
+    raise NotImplementedError
+    value = compute()  # noqa: E501
@@ -10,0 +13 @@
+x = 1  # TODO later
"""


def test_flags_added_production_lines_matching_patterns() -> None:
    found = forbidden_additions(DIFF, PATTERNS, DEFAULT_TEST_GLOBS)
    assert found == [
        "src/app.py:2:     raise NotImplementedError",
        "src/app.py:3:     value = compute()  # noqa: E501",
        "src/app.py:13: x = 1  # TODO later",
    ]


def test_ignores_removed_lines_context_and_test_files() -> None:
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -3 +3,0 @@
-    raise NotImplementedError
@@ -5,0 +6 @@
+clean = 1
diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -0,0 +1 @@
+    x = 1  # type: ignore
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-# TODO gone
"""
    assert forbidden_additions(diff, PATTERNS, DEFAULT_TEST_GLOBS) == []
