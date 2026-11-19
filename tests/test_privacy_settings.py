"""Privacy copy migrations keep saved built-in consent text accurate."""
import json
import os
import sys
import tempfile
import unittest

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from muesli import api, enhance, settings, stt, templates  # noqa: E402


@pytest.fixture
def nim_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SETTINGS_PATH', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(settings, '_cache', None)
    return api.Api()


def test_nvidia_nim_uses_only_its_own_key_and_verified_https(nim_settings, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({'choices': [{'message': {'content': 'Resposta sintética'}}]}).encode()
        return response

    monkeypatch.setattr(requests, 'post', post)
    nim_settings.save_settings({'llm': {'provider': 'nvidia', 'nvidia_key': 'fake-nvidia-fixture-1234',
                                         'nvidia_model': 'nvidia/example', 'openai_key': 'fake-openai-fixture'}})
    assert enhance.llm_available()['provider'] == 'nvidia'
    assert enhance.llm_complete('Instrução', 'Texto sintético') == ('Resposta sintética', 'nvidia')
    (url, options), = calls
    assert url == 'https://integrate.api.nvidia.com/v1/chat/completions'
    assert options['headers']['Authorization'] == 'Bearer fake-nvidia-fixture-1234'
    assert options['verify'] is True and options['allow_redirects'] is False
    assert options['json']['model'] == 'nvidia/example' and options['json']['stream'] is False
    assert stt._resolve_cloud_keys_and_model('openai')[0] == ['fake-openai-fixture']
    nim_settings.save_settings({'llm': {'provider': 'auto', 'openai_keys': []}})
    monkeypatch.setattr(enhance, 'ollama_models', lambda _: [])
    assert enhance.llm_complete('Instrução', 'Não envie automaticamente')[0] is None
    assert len(calls) == 1


@pytest.mark.parametrize(('results', 'attempts', 'provider'), [
    ([200], 1, 'nvidia'),
    ([requests.ReadTimeout(), requests.ReadTimeout(), 200], 3, 'nvidia'),
    ([503, 'truncated', 200], 3, 'nvidia'),
    ([429, 504, 503], 3, 'extractive'),
    ([401], 1, 'extractive'),
    ([requests.exceptions.SSLError()], 1, 'extractive'),
])
def test_nvidia_summary_fallback(nim_settings, monkeypatch, results, attempts, provider):
    key = 'fake-nvidia-fixture'
    nim_settings.save_settings({'llm': {'provider': 'nvidia', 'nvidia_key': key,
                                         'nvidia_model': 'nvidia/custom-chat-model'}})
    calls = []

    def post(url, **options):
        calls.append(options)
        result = results[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        response = requests.Response()
        response.status_code = 200 if result == 'truncated' else result
        response._content = json.dumps({'choices': [{
            'message': {'content': 'Resumo sintético'},
            'finish_reason': 'length' if result == 'truncated' else 'stop',
        }]}).encode()
        return response

    monkeypatch.setattr(requests, 'post', post)
    summary, actual_provider = enhance.enhance(
        {'notes_md': 'Notas sintéticas'}, [], templates.BUILTIN[0])
    assert actual_provider == provider
    if provider == 'nvidia':
        assert summary == 'Resumo sintético'
    assert [options['json']['model'] for options in calls] == [
        'nvidia/nemotron-3.5-super-vl-120b-a12b',
        'nvidia/nemotron-3-ultra-550b-a55b',
        'z-ai/glm-5.3-flash',
    ][:attempts]
    assert all(options['headers']['Authorization'] == f'Bearer {key}' for options in calls)
    assert all(options['json']['messages'] == calls[0]['json']['messages'] for options in calls)


def test_nvidia_key_is_masked_and_can_be_edited_or_cleared(nim_settings):
    key = 'fake-nvidia-fixture-1234'
    nim_settings.save_settings({'llm': {'nvidia_key': key}})
    masked = nim_settings.get_settings()['llm']['nvidia_key']
    assert masked == '********1234' and key not in json.dumps(nim_settings.get_settings())
    nim_settings.save_settings({'llm': {'nvidia_key': masked, 'nvidia_model': 'nvidia/example'}})
    assert settings.load()['llm']['nvidia_key'] == key
    nim_settings.save_settings({'llm': {'nvidia_key': ''}})
    assert nim_settings.get_settings()['llm']['nvidia_key'] == ''


def test_nvidia_connection_test_uses_synthetic_text_and_never_retries_insecurely(nim_settings, monkeypatch):
    seen = []

    def fail_tls(url, **kwargs):
        seen.append(kwargs)
        raise requests.exceptions.SSLError('TLS fixture must not appear in results')

    monkeypatch.setattr(requests, 'post', fail_tls)
    nim_settings.save_settings({'llm': {'nvidia_key': 'fake-nvidia-fixture-1234'}})
    result = nim_settings.test_nvidia_key('********1234', 'nvidia/example')
    assert not result['ok'] and 'certificado' in result['error']
    assert 'fixture' not in result['error']
    options, = seen
    assert options['verify'] is True
    assert options['json']['messages'] == [{'role': 'user', 'content': 'Responda somente OK.'}]


def test_nvidia_probe_rejects_redirects_without_exposing_response(nim_settings, monkeypatch):
    def redirect(url, **kwargs):
        response = requests.Response()
        response.status_code = 302
        response.headers['Location'] = 'https://other.example/'
        return response

    monkeypatch.setattr(requests, 'post', redirect)
    result = nim_settings.test_nvidia_key('fake-nvidia-fixture', 'nvidia/example')
    assert not result['ok'] and '302' in result['error'] and 'other.example' not in result['error']


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
