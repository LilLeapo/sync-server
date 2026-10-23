"""tool_schema：字符串参数收到数字时原样转成文字，不丢精度。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.tool_schema import S, SchemaError  # noqa: E402


class TestStringFromNumber(unittest.TestCase):
    def test_floats_become_lossless_text(self):
        text = S.string()
        self.assertEqual(text(110650.5), "110650.5")
        self.assertEqual(text(1112000.0), "1112000")
        self.assertEqual(text(8.0), "8")
        self.assertEqual(text(0.1), "0.1")
        self.assertEqual(text(-0.32), "-0.32")
        self.assertEqual(text(14448.38), "14448.38")

    def test_integral_floats_beyond_exact_range_keep_float_notation(self):
        text = S.string()
        self.assertEqual(text(float(2 ** 53 - 1)), "9007199254740991")
        self.assertEqual(text(-float(2 ** 53 - 1)), "-9007199254740991")
        self.assertEqual(text(1e23), "1e+23")
        self.assertEqual(text(float(2 ** 53)), "9007199254740992.0")

    def test_ints_and_strings_unchanged(self):
        text = S.string()
        self.assertEqual(text(1792726800000), "1792726800000")
        self.assertEqual(text(-350), "-350")
        self.assertEqual(text("  沪铜2611+550 "), "沪铜2611+550")
        with self.assertRaises(SchemaError):
            text(True)

    def test_max_length_counts_converted_text(self):
        with self.assertRaises(SchemaError):
            S.string().max(5)(110650.5)
        self.assertEqual(S.string().max(7)(1112000.0), "1112000")


if __name__ == "__main__":
    unittest.main()
