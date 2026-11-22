import ast
import unittest

from selector import changes_to_lines, evaluation_spans, is_cosmetic, parse_patch, schedule


class SelectorTests(unittest.TestCase):
    def test_replacement_and_insertion(self):
        original = "def example(a, b):\n    if a and b:\n        return 1\n    return 0\n"
        diff = (
            "--- a/example.py\n+++ b/example.py\n@@ -1,4 +1,5 @@\n"
            " def example(a, b):\n-    if a and b:\n+    if a or b:\n"
            "         return 1\n+    print(a)\n     return 0\n"
        )
        entry, = parse_patch(diff)
        revised, probes, _ = changes_to_lines(original, entry.hunks)
        self.assertEqual(revised, original.replace("and", "or").replace("    return 0", "    print(a)\n    return 0"))
        self.assertEqual(probes, [([2], 2, True), ([], 4, False)])

    def test_truncated_hunk_rejected(self):
        with self.assertRaises(ValueError):
            parse_patch("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old\n+new\n")

    def test_python_comments_inside_strings_affect_ast(self):
        before = 'data = """\n# old\n"""\n'
        revised = ast.parse(before.replace("old", "new"), type_comments=True)
        self.assertFalse(is_cosmetic(before, revised, ["# old", "# new"]))
        self.assertTrue(is_cosmetic("x = 1\n", ast.parse("# comment\nx = 1\n"), ["# comment"]))

    def test_multiline_guard_spans_early_operands(self):
        self.assertIn((2, 3), evaluation_spans("if (\n    a\n    and b\n):\n    pass\n"))

    def test_scheduler_uses_only_profitable_workers(self):
        self.assertEqual(schedule([0, 1], {0: 4000, 1: 3000}), [[0], [1]])
        self.assertEqual(schedule([0, 1], {0: 2000, 1: 2000}), [[0, 1]])
        self.assertEqual(schedule([], {}), [])


if __name__ == "__main__":
    unittest.main()
