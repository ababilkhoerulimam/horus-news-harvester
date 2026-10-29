"""
InnerTube pacing gate: a shared minimum interval between request starts, and the
interaction with jitter.
"""

import unittest
from unittest.mock import patch

from channels import innertube


class FakeClock:
    """Monotonic clock that advances only when sleep is called."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PacingTests(unittest.TestCase):
    def tearDown(self):
        innertube.configure_pacing(0.0)

    def test_zero_interval_never_sleeps(self):
        innertube.configure_pacing(0.0)
        clock = FakeClock()
        with patch("channels.innertube.time") as fake_time:
            fake_time.monotonic.side_effect = clock.monotonic
            fake_time.sleep.side_effect = clock.sleep
            self.assertFalse(innertube._wait_turn())
            self.assertFalse(innertube._wait_turn())
        self.assertEqual(clock.sleeps, [])

    def test_interval_spaces_consecutive_calls(self):
        innertube.configure_pacing(0.25)
        clock = FakeClock()
        with patch("channels.innertube.time") as fake_time:
            fake_time.monotonic.side_effect = clock.monotonic
            fake_time.sleep.side_effect = clock.sleep
            self.assertTrue(innertube._wait_turn())
            self.assertTrue(innertube._wait_turn())
            self.assertTrue(innertube._wait_turn())
        self.assertEqual(clock.sleeps, [0.25, 0.25])

    def test_negative_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            innertube.configure_pacing(-1)

    def test_interval_is_readable(self):
        innertube.configure_pacing(0.4)
        self.assertEqual(innertube.pacing_interval(), 0.4)

    def test_pacing_disables_jitter_for_the_request(self):
        innertube.configure_pacing(0.05)
        clock = FakeClock()
        with patch("channels.innertube.time") as fake_time, patch(
            "channels.innertube.default_http_client.fetch", return_value=(200, b"{}", {})
        ) as mock_fetch:
            fake_time.monotonic.side_effect = clock.monotonic
            fake_time.sleep.side_effect = clock.sleep
            innertube.post_innertube(
                "https://example.com/youtubei/v1/next", {"context": {}}, use_jitter=True
            )
        self.assertFalse(mock_fetch.call_args.kwargs["use_jitter"])

    def test_jitter_survives_when_pacing_is_off(self):
        innertube.configure_pacing(0.0)
        with patch(
            "channels.innertube.default_http_client.fetch", return_value=(200, b"{}", {})
        ) as mock_fetch:
            innertube.post_innertube(
                "https://example.com/youtubei/v1/next", {"context": {}}, use_jitter=True
            )
        self.assertTrue(mock_fetch.call_args.kwargs["use_jitter"])


if __name__ == "__main__":
    unittest.main()
