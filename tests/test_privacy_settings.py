"""Privacy copy migrations keep saved built-in consent text accurate."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from muesli import settings  # noqa: E402


class PrivacySettingsTests(unittest.TestCase):
    def test_replaces_only_the_old_builtin_consent_copy(self):
        tmp = tempfile.TemporaryDirectory()
        old_path, old_cache = settings.SETTINGS_PATH, settings._cache
        settings.SETTINGS_PATH = os.path.join(tmp.name, "settings.json")
        settings._cache = None
        legacy = ("Aviso rápido: utilizo um aplicativo local que transcreve a nossa chamada "
                  "no meu próprio computador para eu me concentrar na conversa. Nenhum áudio "
                  "é enviado para a nuvem. Me avise se preferir que eu desligue.")
        with open(settings.SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump({"consent": {"snippet": legacy}}, f)
        try:
            loaded = settings.load()
            self.assertEqual(loaded["consent"]["snippet"], settings.DEFAULTS["consent"]["snippet"])
        finally:
            settings.SETTINGS_PATH, settings._cache = old_path, old_cache
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
