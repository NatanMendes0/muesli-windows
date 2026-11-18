import hashlib
import io
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest
import requests

from muesli import api
from muesli import updater

TAG = 'v9.8.7'
NAME = f'Muesli-windows-x64-{TAG}'
URL = f'https://github.com/NatanMendes0/muesli-windows/releases/download/{TAG}/{NAME}.zip'


def package(extra=None, drop=None):
    out = io.BytesIO()
    members = {
        NAME + '.exe': b'MZ mock', NAME + '.exe.config': b'<configuration/>',
        '_internal/ui/index.html': b'<title>Muesli</title>',
        'llama-runtime/llama-completion.exe': b'MZ CPU',
        'llama-runtime-vulkan/llama-completion.exe': b'MZ Vulkan',
        'llama-runtime-vulkan/ggml-vulkan.dll': b'MZ DLL',
    }
    if drop:
        del members[drop]
    with zipfile.ZipFile(out, 'w') as z:
        for path, body in (members | (extra or {})).items():
            z.writestr(path, body)
    return out.getvalue()


def asset(blob):
    return {'name': NAME + '.zip', 'browser_download_url': URL,
            'digest': 'sha256:' + hashlib.sha256(blob).hexdigest(), 'size': len(blob)}


def metadata(blob):
    return {'tag_name': TAG, 'draft': False, 'prerelease': False, 'assets': [asset(blob)]}


class Response:
    def __init__(self, status=200, blob=b'', headers=None, document=None):
        self.status_code, self.blob, self.headers, self.document = status, blob, headers or {}, document

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def close(self): pass
    def json(self): return self.document
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))
    def iter_content(self, chunk_size):
        return (self.blob[i:i + chunk_size] for i in range(0, len(self.blob), chunk_size))


def test_download_and_stage_preserves_active_version_and_data(tmp_path, monkeypatch):
    blob = package()
    root = tmp_path / 'Programs' / 'Muesli'
    root.mkdir(parents=True)
    (root / 'active').write_text('prior')
    data = tmp_path / 'Muesli.db'
    data.write_bytes(b'untouched')
    cdn = 'https://release-assets.githubusercontent.com/a.zip?signature=example'
    seen = []

    def get(url, **kwargs):
        seen.append(url)
        assert kwargs.get('verify') is not False
        assert kwargs.get('allow_redirects') is False
        return (Response(document=metadata(blob)) if '/releases/tags/' in url else
                Response(302, headers={'Location': cdn}) if url == URL else Response(blob=blob))

    monkeypatch.setattr(updater.requests, 'get', get)
    progress = []
    candidate = updater.stage_update(root, TAG, '1.6.2', progress.append)
    assert seen == [updater.API_BASE + '/releases/tags/' + TAG, URL, cdn]
    assert candidate.name == NAME + '.exe' and candidate.read_bytes() == b'MZ mock'
    assert (root / 'active').read_text() == 'prior' and data.read_bytes() == b'untouched'
    assert progress[-1] == 100


@pytest.mark.parametrize('mode', ['hash', 'tls', 'redirect', 'missing_digest', 'not_newer'])
def test_untrusted_update_does_not_stage_or_retry_tls(tmp_path, monkeypatch, mode):
    blob = package()
    meta = metadata(blob)
    if mode == 'missing_digest':
        del meta['assets'][0]['digest']
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if '/releases/tags/' in url:
            return Response(document=meta)
        if mode == 'tls':
            raise requests.exceptions.SSLError('untrusted')
        if mode == 'redirect':
            return Response(302, headers={'Location': 'http://release-assets.githubusercontent.com/a.zip'})
        return Response(blob=b'?' * len(blob))

    monkeypatch.setattr(updater.requests, 'get', get)
    with pytest.raises(updater.UpdateError):
        updater.stage_update(tmp_path, TAG, '10.0.0' if mode == 'not_newer' else '1.6.2')
    assert len(calls) == {'not_newer': 0, 'missing_digest': 1}.get(mode, 2)
    assert not list((tmp_path / 'versions').glob('v*'))


@pytest.mark.parametrize('extra,drop', [
    ({'../outside.exe': b'no'}, None), ({'C:\\escape': b'no'}, None),
    ({'nested/AUX.txt': b'no'}, None), ({'_INTERNAL/UI/index.html': b'collision'}, None),
    ({}, '_internal/ui/index.html'),
])
def test_malformed_package_is_not_extracted(tmp_path, extra, drop):
    archive = tmp_path / 'release.zip'
    archive.write_bytes(package(extra, drop))
    dest = tmp_path / 'stage'
    with pytest.raises(updater.UpdateError):
        updater.extract_package(archive, dest, TAG)
    assert not dest.exists()
    assert not (tmp_path / 'outside.exe').exists()


def test_activate_requires_startup_ack_and_rolls_back_on_failure(tmp_path, monkeypatch):
    candidate = tmp_path / 'versions' / (TAG + '-' + 'a' * 16) / (NAME + '.exe')
    candidate.parent.mkdir(parents=True)
    candidate.touch()
    old = tmp_path / 'original.exe'
    old.touch()
    calls = []

    def spawn(exe, *args):
        calls.append((exe, args))
        if exe == candidate and len(calls) == 3:
            updater.mark_ready(tmp_path, exe, TAG[1:], args[1])
        elif exe == candidate:
            return SimpleNamespace(poll=lambda: 1)
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(updater, 'spawn', spawn)
    assert not updater.restart_into(tmp_path, candidate, old, timeout=.2)
    assert not (tmp_path / 'active').exists()
    assert calls[1][0] == old
    assert updater.restart_into(tmp_path, candidate, old, timeout=.2)
    assert updater.active_executable(tmp_path, '1.6.2') == candidate
    assert updater.active_executable(tmp_path, TAG[1:]) is None


def test_apply_refuses_while_transcription_is_draining():
    backend = api.Api()
    backend._draining.add(42)
    result = backend.install_update('job')
    assert not result['ok'] and 'transcrição' in str(result['error'])


def test_activation_waits_for_a_real_child_process_to_confirm(tmp_path, monkeypatch):
    candidate = tmp_path / 'versions' / (TAG + '-' + 'a' * 16) / (NAME + '.exe')
    candidate.parent.mkdir(parents=True)
    candidate.touch()
    old = tmp_path / 'old.exe'
    children = []

    def spawn(executable, flag, token):
        assert executable == candidate and flag == updater.TRIAL_ARG
        assert not (tmp_path / 'active').exists()
        source = '''
import sys, time
from pathlib import Path
from muesli.updater import mark_ready, trial_committed
root, exe, tag, token = sys.argv[1:]
assert mark_ready(Path(root), Path(exe), tag, token)
for _ in range(100):
    if trial_committed(Path(root), Path(exe)):
        sys.exit(0)
    time.sleep(.05)
sys.exit(1)
'''
        child = subprocess.Popen([sys.executable, '-c', source, str(tmp_path), str(executable), TAG[1:], token])
        children.append(child)
        return child

    monkeypatch.setattr(updater, 'spawn', spawn)
    try:
        assert updater.restart_into(tmp_path, candidate, old, timeout=6)
        assert children[0].wait(timeout=6) == 0
        assert updater.active_executable(tmp_path, '1.6.2') == candidate
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_install_requires_the_prepared_job_and_reserves_shutdown(tmp_path, monkeypatch):
    backend = api.Api()
    candidate = tmp_path / (NAME + '.exe')
    backend._staged_update = (TAG, 'verified', candidate)
    triggered = []
    backend._quit_for_update = lambda: triggered.append('quit')

    class Timer:
        def __init__(self, _, action): self.action = action
        def start(self): self.action()

    monkeypatch.setattr(api.threading, 'Timer', Timer)
    assert not backend.install_update('different')['ok']
    assert backend.install_update('verified')['ok']
    assert backend._pending_update == candidate and triggered == ['quit']
    assert not backend.start_meeting('', 'auto')['ok']
