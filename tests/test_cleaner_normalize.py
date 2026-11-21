"""
Article normalization prototype: dateline, bidi, cross-link, and title suffix.

The three functions are additive; `clean_news_text` is unchanged. Their measured
effect on the corpus is in `_audit/horus_reuse.md` section 2.1a.
"""

import unittest

from processors.cleaner import (
    DATELINE_PATTERNS,
    normalize_article,
    normalize_title,
    strip_dateline,
)


class StripDatelineTests(unittest.TestCase):
    def test_bare_city_dateline_is_removed(self):
        self.assertEqual(
            strip_dateline("Jakarta - BRI Super League menggelar tiga laga."),
            "BRI Super League menggelar tiga laga.",
        )

    def test_two_word_city_dateline_is_removed(self):
        self.assertEqual(
            strip_dateline("New York - Laga final digelar Senin."),
            "Laga final digelar Senin.",
        )

    def test_domain_dateline_is_removed(self):
        self.assertEqual(
            strip_dateline("KOMPAS.com - Manajemen klub resmi menunjuk pelatih."),
            "Manajemen klub resmi menunjuk pelatih.",
        )

    def test_city_and_domain_dateline_is_removed(self):
        self.assertEqual(
            strip_dateline("MANGUPURA, BALIPOST.com \u2013 Cuaca panas menguntungkan atlet."),
            "Cuaca panas menguntungkan atlet.",
        )

    def test_city_with_news_agency_dateline_is_removed(self):
        self.assertEqual(
            strip_dateline("Jakarta (ANTARA) - Timnas menang 2-0."),
            "Timnas menang 2-0.",
        )

    def test_upper_case_city_dateline_is_removed(self):
        self.assertEqual(strip_dateline("JAKARTA - Timnas menang 2-0."), "Timnas menang 2-0.")

    def test_markdown_bold_dateline_is_removed(self):
        self.assertEqual(
            strip_dateline("**BANDUNG**\u2013 Persib menang 1-0."),
            "Persib menang 1-0.",
        )

    def test_stacked_datelines_are_removed(self):
        self.assertEqual(
            strip_dateline("KOMPAS.com - Jakarta - Laga berjalan sengit."),
            "Laga berjalan sengit.",
        )

    def test_sentence_without_a_dateline_is_untouched(self):
        text = "Hasil pertandingan berakhir imbang tanpa gol."
        self.assertEqual(strip_dateline(text), text)

    def test_dateline_after_the_first_sentence_is_untouched(self):
        text = "Laga dimulai pukul 19.00. Jakarta - Bek kiri cedera."
        self.assertEqual(strip_dateline(text), text)

    def test_empty_text_returns_empty(self):
        self.assertEqual(strip_dateline(""), "")

    def test_known_limit_a_non_city_word_before_a_dash_is_removed(self):
        # Two records in 225,536 hit this. The rule cannot tell a desk city from
        # a subject without a city list, and a list is not worth its upkeep for
        # two records. Pinned here so a future change is a deliberate one.
        self.assertEqual(
            strip_dateline("Jadwal - Penghargaan Ballon d'Or digelar malam ini."),
            "Penghargaan Ballon d'Or digelar malam ini.",
        )


class NormalizeArticleTests(unittest.TestCase):
    def test_dateline_is_removed(self):
        self.assertEqual(
            normalize_article("Liverpool - Manchester United unggul 1-0."),
            "Manchester United unggul 1-0.",
        )

    def test_baca_juga_line_without_a_colon_is_removed(self):
        self.assertEqual(
            normalize_article("Baca juga Hasil Liga Inggris\nJakarta - Laga berjalan sengit."),
            "Laga berjalan sengit.",
        )

    def test_simak_juga_line_is_removed(self):
        self.assertEqual(
            normalize_article("Simak juga: Jadwal pekan depan\nPersib menang 1-0."),
            "Persib menang 1-0.",
        )

    def test_bidi_marks_are_removed(self):
        self.assertEqual(normalize_article("\u200eJakarta - Laga berjalan."), "Laga berjalan.")

    def test_paragraph_breaks_are_kept(self):
        self.assertEqual(normalize_article("Jakarta - Satu.\n\nDua."), "Satu.\n\nDua.")

    def test_runs_of_spaces_and_tabs_are_collapsed(self):
        self.assertEqual(normalize_article("Satu.\t\t   Dua."), "Satu. Dua.")

    def test_clean_text_is_unchanged(self):
        text = "Persib menang 1-0 atas Persik."
        self.assertEqual(normalize_article(text), text)

    def test_normalization_is_idempotent(self):
        once = normalize_article("KOMPAS.com - Jakarta - Laga berjalan sengit.")
        self.assertEqual(normalize_article(once), once)

    def test_empty_input_returns_empty(self):
        self.assertEqual(normalize_article(""), "")
        self.assertEqual(normalize_article(None), "")


class NormalizeTitleTests(unittest.TestCase):
    def test_outlet_suffix_after_a_pipe_is_removed(self):
        self.assertEqual(
            normalize_title("Hasil Persib vs Persik 1-0 | beritajatim.com"),
            "Hasil Persib vs Persik 1-0",
        )

    def test_outlet_suffix_after_a_dash_is_removed(self):
        self.assertEqual(normalize_title("Jadwal Liga - detik.com"), "Jadwal Liga")

    def test_title_without_a_suffix_is_unchanged(self):
        self.assertEqual(normalize_title("Persib Menang 1-0"), "Persib Menang 1-0")

    def test_a_dash_without_a_domain_is_kept(self):
        self.assertEqual(normalize_title("Persib - Arema"), "Persib - Arema")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(normalize_title("  Persib   Menang  "), "Persib Menang")

    def test_empty_title_returns_empty(self):
        self.assertEqual(normalize_title(""), "")


class PatternTableTests(unittest.TestCase):
    def test_alternative_names_are_unique(self):
        names = [name for name, _ in DATELINE_PATTERNS]
        self.assertEqual(len(names), len(set(names)))

    def test_bare_city_alternative_is_present(self):
        # The corpus needs it: 66,414 records start with a bare city dateline.
        self.assertIn("bare_city", [name for name, _ in DATELINE_PATTERNS])


if __name__ == "__main__":
    unittest.main()
