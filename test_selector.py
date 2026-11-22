import ast
import unittest

from bytecode_safety import same_executable_code
from comparison_types import changed_sites, equivalent
from import_effects import executable_declarations_changed
from selector import bounded_schedule, changes_to_lines, cost, evaluation_spans, is_cosmetic, parse_patch, schedule
from value_profile import UNKNOWN, freeze, same_result


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

    def test_changes_to_import_initialization(self):
        before = ast.parse('class Base:\n    x = ","\n    __slots__ = ("valid",)\n')
        after = ast.parse('class Base:\n    x = int("bad")\n    __slots__ = ("valid",)\n')
        slots = ast.parse('class Base:\n    x = ","\n    __slots__ = ("bad-name",)\n')
        self.assertTrue(executable_declarations_changed(before, after))
        self.assertTrue(executable_declarations_changed(before, slots))
        early = ast.parse('class Base:\n    _names = ("valid",)\n    __slots__ = _names\n')
        broken = ast.parse('class Base:\n    _names = ("bad-name",)\n    __slots__ = _names\n')
        self.assertTrue(executable_declarations_changed(early, broken))

    def test_statement_body_change_leaves_declarations_unchanged(self):
        before = ast.parse("def f():\n    return 1\n")
        after = ast.parse("def f():\n    return 2\n")
        self.assertFalse(executable_declarations_changed(before, after))

    def test_bytecode_constant_folding_preserves_values_and_types(self):
        original = compile("x = 1 + 2", "test.py", "exec")
        self.assertTrue(same_executable_code(original, compile("# comment\nx = 3", "test.py", "exec")))
        self.assertFalse(same_executable_code(original, compile("x = 3.0", "test.py", "exec")))

    def test_identity_and_equality_keep_custom_dispatch(self):
        obj = ("example.Value", "builtins.object", "builtins.object", False)
        none = ("builtins.NoneType", "builtins.object", "builtins.object", False)
        custom = ("example.Value", "example.Value", "builtins.object", False)
        self.assertTrue(equivalent((obj, none, False), False))
        self.assertFalse(equivalent((custom, none, False), False))
        before = ast.parse("return x is None")
        after = ast.parse("return x == None")
        self.assertEqual(len(changed_sites(before, after)), 1)
        self.assertIsInstance(after.body[0].value.ops[0], ast.Eq)
        self.assertEqual(changed_sites(before, ast.parse("return y == None")), ())

    def test_schedule_guard_fits_inside_the_original_cost(self):
        times = {0: 4000, 1: 3000, 2: 1000, 3: 9000}
        original = {0, 1}
        selected = bounded_schedule(original, {0}, {2, 3}, times)
        self.assertEqual({number for worker in selected for number in worker}, {0})
        selected_with_check = bounded_schedule(original, {0}, {2}, times)
        self.assertEqual({number for worker in selected_with_check for number in worker}, {0, 2})
        self.assertLessEqual(cost(selected_with_check, times), cost(schedule(original, times), times))

    def test_operand_value_proof_preserves_boundary_cases(self):
        model = ast.Lt, ast.LtE, (UNKNOWN, UNKNOWN)
        boundary = freeze((23, 12))
        self.assertFalse(same_result(model, (boundary, boundary, False)))
        self.assertTrue(same_result(model, (freeze((2**63 - 1,)), boundary, False)))


if __name__ == "__main__":
    unittest.main()
