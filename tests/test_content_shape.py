"""Tests for the content-shape rules and their quality mapping."""

import unittest

from processors.content_policy import LOW, OK, QUALITY_FOR_LABEL, REJECT, shape_decision
from processors.content_shape import (
    AD_INTERLEAVED,
    ARTICLE,
    CHROME_ONLY,
    ERROR_PAGE,
    MISSING_TITLE,
    PHOTO_CAPTION,
    READER_ENVELOPE,
    classify,
    title_warning,
)

ENVELOPE = (
    "Title: Hasil PSS vs Borneo\n"
    "URL Source: https://bola.example.com/x\n\n"
    "Markdown Content:\n\n"
    "Pesut Etam menang dua gol tanpa balas pada laga pekan kelima.\n"
)
CHROME = "HOME\nBOLA\nDetail Berita\nLink Copied!\n"
AD_BODY = (
    "Paragraf pertama tentang pertandingan.\n\nAdvertisement\n\n"
    "Paragraf kedua tentang gol pembuka.\n\nAdvertisement\n\n"
    "Paragraf ketiga tentang kartu merah.\n\nAdvertisement\n\n"
    "Paragraf keempat tentang klasemen."
)
CAPTION = "Pemain Persib berlatih di stadion jelang laga. (BP/Antara)"
PROSE = (
    "Jakarta - Pelatih Persib Bandung menyebut anak asuhnya bermain disiplin. "
    "Ia menilai laga berjalan sesuai rencana dan menegaskan rotasi tetap dilakukan."
)


class ClassifyTests(unittest.TestCase):
    """One test per label, then one per rule condition."""

    def test_reader_envelope_is_detected(self):
        self.assertEqual(classify(ENVELOPE)[0], READER_ENVELOPE)

    def test_chrome_only_is_detected(self):
        self.assertEqual(classify(CHROME)[0], CHROME_ONLY)

    def test_error_page_is_detected_for_each_phrase(self):
        for phrase in ("The request could not be satisfied.", "403 ERROR", "Request Blocked"):
            with self.subTest(phrase=phrase):
                self.assertEqual(classify(phrase)[0], ERROR_PAGE)

    def test_ad_interleaved_is_detected(self):
        self.assertEqual(classify(AD_BODY)[0], AD_INTERLEAVED)

    def test_photo_caption_is_detected(self):
        self.assertEqual(classify(CAPTION)[0], PHOTO_CAPTION)

    def test_clean_short_article_is_article(self):
        self.assertEqual(classify(PROSE)[0], ARTICLE)

    def test_empty_body_is_not_an_article(self):
        self.assertEqual(classify("")[0], CHROME_ONLY)
        self.assertEqual(classify("   \n\n ")[0], CHROME_ONLY)

    def test_reader_envelope_requires_both_literals(self):
        self.assertEqual(classify("URL Source: https://a\n" + PROSE)[0], ARTICLE)
        self.assertEqual(classify("Markdown Content:\n" + PROSE)[0], ARTICLE)

    def test_chrome_only_requires_both_literals(self):
        self.assertEqual(classify("Detail Berita\n" + PROSE)[0], ARTICLE)
        self.assertEqual(classify("Link Copied!\n" + PROSE)[0], ARTICLE)

    def test_ad_interleaved_needs_three_lines(self):
        two = "Paragraf satu.\n\nAdvertisement\n\nParagraf dua.\n\nAdvertisement\n\nParagraf tiga."
        self.assertEqual(classify(two)[0], ARTICLE)
        self.assertEqual(classify(two + "\n\nAdvertisement\n")[0], AD_INTERLEAVED)

    def test_ad_interleaved_ignores_a_prose_mention(self):
        prose = PROSE + " Iklan Advertisement muncul di tengah halaman, kata redaksi."
        self.assertEqual(classify(prose)[0], ARTICLE)

    def test_photo_caption_needs_one_paragraph(self):
        two = "Pemain Persib berlatih di stadion.\n\n(BP/Antara)"
        self.assertEqual(classify(two)[0], ARTICLE)

    def test_photo_caption_needs_a_credit(self):
        self.assertEqual(classify("Pemain Persib berlatih di stadion jelang laga.")[0], ARTICLE)

    def test_photo_caption_needs_under_400_chars(self):
        long_caption = "Pemain Persib berlatih di stadion jelang laga. " + "x" * 400 + " (BP/Antara)"
        self.assertEqual(classify(long_caption)[0], ARTICLE)

    def test_photo_caption_survives_a_trailing_nbsp(self):
        self.assertEqual(classify(CAPTION + "&nbsp;")[0], PHOTO_CAPTION)

    def test_precedence_error_page_beats_reader_envelope(self):
        self.assertEqual(classify(ENVELOPE + "403 error")[0], ERROR_PAGE)

    def test_precedence_reader_envelope_beats_chrome_only(self):
        self.assertEqual(classify(ENVELOPE + CHROME)[0], READER_ENVELOPE)

    def test_reason_is_machine_readable(self):
        samples = (ENVELOPE, CHROME, AD_BODY, CAPTION, PROSE, "403 error")
        for sample in samples:
            with self.subTest(sample=sample[:20]):
                reason = classify(sample)[1]
                self.assertTrue(reason)
                self.assertEqual(reason, reason.lower())
                self.assertNotIn(" ", reason)

    def test_classify_is_deterministic(self):
        self.assertEqual(classify(AD_BODY), classify(AD_BODY))
        self.assertEqual(classify(CAPTION, "https://a"), classify(CAPTION, "https://b"))


class TitleWarningTests(unittest.TestCase):
    def test_missing_title_warning_fires_on_empty_title(self):
        self.assertEqual(title_warning(""), MISSING_TITLE)
        self.assertEqual(title_warning("   "), MISSING_TITLE)
        self.assertEqual(title_warning(None), MISSING_TITLE)

    def test_missing_title_warning_is_silent_on_title(self):
        self.assertEqual(title_warning("Hasil PSS vs Borneo"), "")


class ShapeDecisionTests(unittest.TestCase):
    def test_every_label_maps_to_a_quality(self):
        for label in (READER_ENVELOPE, CHROME_ONLY, ERROR_PAGE, AD_INTERLEAVED, PHOTO_CAPTION, ARTICLE):
            with self.subTest(label=label):
                self.assertIn(QUALITY_FOR_LABEL[label], (REJECT, LOW, OK))

    def test_rejected_shapes_are_reject(self):
        self.assertEqual(shape_decision(ENVELOPE), (READER_ENVELOPE, REJECT, "envelope_marker"))
        self.assertEqual(shape_decision(CHROME), (CHROME_ONLY, REJECT, "chrome_marker"))
        self.assertEqual(shape_decision("403 error")[1], REJECT)

    def test_ad_and_caption_are_low_not_reject(self):
        self.assertEqual(shape_decision(AD_BODY)[1], LOW)
        self.assertEqual(shape_decision(CAPTION)[1], LOW)

    def test_article_is_ok_however_short(self):
        self.assertEqual(shape_decision(PROSE), (ARTICLE, OK, "prose"))


if __name__ == "__main__":
    unittest.main()
