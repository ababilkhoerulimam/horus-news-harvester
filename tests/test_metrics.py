"""Tests for shared social counter parsing."""

import unittest

from processors.metrics import parse_count


class TestParseCount(unittest.TestCase):
    """Verifies counter strings from YouTube and Twitter map to integers."""

    def test_plain_integer_string(self):
        self.assertEqual(parse_count('17'), 17)

    def test_blank_values_become_zero(self):
        for value in (' ', '', None):
            self.assertEqual(parse_count(value), 0)

    def test_integer_passthrough(self):
        self.assertEqual(parse_count(45), 45)

    def test_indonesian_thousands_separator(self):
        self.assertEqual(parse_count('1.234'), 1234)
        self.assertEqual(parse_count('1.234.567'), 1234567)

    def test_indonesian_abbreviation(self):
        self.assertEqual(parse_count('1,2 rb'), 1200)
        self.assertEqual(parse_count('2,5 jt'), 2500000)

    def test_english_abbreviation(self):
        self.assertEqual(parse_count('1.2K'), 1200)


if __name__ == "__main__":
    unittest.main()
