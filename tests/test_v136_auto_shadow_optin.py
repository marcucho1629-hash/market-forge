"""Auto Shadow replay is expensive and must be explicitly enabled."""
import os
import unittest
from unittest.mock import patch

from scanner.v136_live.web import auto_shadow_enabled


class TestWebhookShadowOptIn(unittest.TestCase):
    def test_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(auto_shadow_enabled())

    def test_only_literal_true_enables(self):
        for value in ("true", "TRUE", " True "):
            with self.subTest(value=value), patch.dict(os.environ, {"MF_V136_AUTO_SHADOW": value}):
                self.assertTrue(auto_shadow_enabled())

    def test_other_values_remain_off(self):
        for value in ("", "1", "false", "yes", "on", "disabled"):
            with self.subTest(value=value), patch.dict(os.environ, {"MF_V136_AUTO_SHADOW": value}):
                self.assertFalse(auto_shadow_enabled())


if __name__ == "__main__":
    unittest.main()
