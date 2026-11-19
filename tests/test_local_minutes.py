import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

import pytest

from muesli import api, db, local_minutes, settings


def draft_json(summary):
    return json.dumps(dict(resumo=summary, fatos=[]))


def test_download_rejects_corruption_without_installing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'models_dir', lambda: str(tmp_path))
    monkeypatch.setattr(local_minutes, 'MODEL_BYTES', 4)
    monkeypatch.setattr(local_minutes, 'MODEL_HASH', hashlib.sha256(b'GGUF').hexdigest())

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield b'BAD!'

    monkeypatch.setattr(local_minutes.requests, 'get', lambda *args, **kw: Response())
    with pytest.raises(RuntimeError, match='integridade'):
        local_minutes.download(lambda received: None)
    assert not list(tmp_path.iterdir())


def test_auto_minutes_wait_for_whisper_and_use_final_text(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    monkeypatch.setattr(db._local, 'conn', None, raising=False)
    monkeypatch.setattr(settings, 'load', lambda: {'minutes': {'enabled': True, 'ac_only': True}})
    db.init()
    mid = db.create_meeting('', 'auto')
    drained = threading.Event()
    inserted = threading.Event()
    finalizing = threading.Event()
    ac = threading.Event()
    monkeypatch.setattr(local_minutes, 'power_connected', ac.is_set)
    monkeypatch.setattr(local_minutes, 'runtime_path', lambda: tmp_path / 'fake.exe')
    monkeypatch.setattr(local_minutes, 'model_installed', lambda: True)
    backend = api.Api()

    class Capture:
        status = 'recording'
        meeting_id = mid

        def stop(self):
            self.status = 'stopped'
            db.end_meeting(mid, 50)
            return 50

        def wait_for_transcription(self):
            finalizing.set()
            drained.wait(5)
            db.add_segment(mid, 1, 2, 'Eu', 'Última decisão')
            inserted.set()

    monkeypatch.setattr(backend, '_session', Capture())
    def model(instructions, evidence, tokens, schema, cancel):
        assert inserted.is_set() and 'Última decisão' in evidence
        if 'titulo' in schema['properties']:
            return json.dumps(dict(resumo='Última decisão', titulo='Revisão do projeto'))
        return draft_json('Última decisão')

    monkeypatch.setattr(local_minutes, 'generate', model)
    monkeypatch.setattr(api.enhance, 'suggest_title', lambda _: pytest.fail('não deve inventar título provisório'))
    with patch.object(api.enhance, 'enhance', side_effect=AssertionError('não deve chamar IA externa')):
        stop_id = backend.stop_meeting()['job_id']
        assert finalizing.wait(3)
        assert db.get_minutes_job(mid) is None
        stopped = wait(backend, stop_id)
        assert stopped and stopped['state'] == 'done'
        stopping = stopped['result']
        assert isinstance(stopping, dict)
        assert backend._session is None
        assert backend.job_status(stopping['finalize_job_id'])['state'] == 'running'
        drained.set()
        finalized = wait(backend, stopping['finalize_job_id'])
        assert finalized and finalized['state'] == 'done'
        result = finalized['result']
        assert isinstance(result, dict)
        jid = result['minutes_job_id']
        assert backend.job_status(jid)['state'] == 'queued'
        meeting = db.get_meeting(mid)
        assert meeting and not meeting['summary_md'] and meeting['title'] == 'Untitled meeting'
        deadline = time.monotonic() + 5
        while 'tomada' not in backend.job_status(jid).get('detail', '') and time.monotonic() < deadline:
            time.sleep(.02)
        assert 'tomada' in backend.job_status(jid).get('detail', '')
        meeting = db.get_meeting(mid)
        assert meeting and not meeting['summary_md']
        ac.set()
        assert wait(backend, jid)['state'] == 'done'
    backend._close_minutes()
    meeting = db.get_meeting(mid)
    assert meeting and 'Última decisão' in meeting['summary_md']
    assert meeting['title'] == 'Revisão do projeto'
    assert meeting['summary_provider'] == 'local_minutes'
    assert backend.save_minutes_edit(mid, 'Ata revisada', meeting['summary_md'])['ok']
    db.conn().close()
    db._local.conn = None
    updated = db.get_meeting(mid)
    assert updated and updated['summary_md'] == 'Ata revisada'
    db.conn().close()


def test_cancel_terminates_the_local_child_process(tmp_path):
    marker = tmp_path / 'finished'
    command = [sys.executable, '-c',
               'import pathlib, sys, time; time.sleep(10); pathlib.Path(sys.argv[1]).touch()', str(marker)]
    cancel = threading.Event()
    timer = threading.Timer(.35, cancel.set)
    timer.start()
    try:
        start = time.monotonic()
        with pytest.raises(RuntimeError, match='interrompida'):
            local_minutes._complete_process(command, Path(tmp_path), 0, os.environ, cancel)
        assert time.monotonic() - start < 5
        assert not marker.exists()
    finally:
        timer.cancel()


def test_vulkan_probe_prefers_intel_integrated_and_rejects_unrelated_gpus():
    listing = '''Available devices:
  Vulkan0: NVIDIA GeForce RTX 3060 (12288 MiB, 12000 MiB free)
  Vulkan1: Intel(R) UHD Graphics (Intel Corporation) (4096 MiB, 3000 MiB free)
  Vulkan2: Intel(R) Arc(TM) A770 Graphics (8192 MiB, 8000 MiB free)
'''
    assert local_minutes.intel_device_from_listing(listing) == ('Vulkan1', 'Intel(R) UHD Graphics (Intel Corporation)')
    assert local_minutes.intel_device_from_listing(listing.split('\n')[1]) is None


def test_vulkan_failure_retries_cpu_once_and_keeps_cpu_for_rest_of_meeting(monkeypatch, tmp_path):
    cpu = tmp_path / 'cpu.exe'
    vulkan = tmp_path / 'vulkan.exe'
    monkeypatch.setattr(local_minutes, 'runtime_path', lambda use_vulkan=False: vulkan if use_vulkan else cpu)
    monkeypatch.setattr(local_minutes, 'model_installed', lambda: True)
    monkeypatch.setattr(local_minutes, 'model_path', lambda: tmp_path / 'model.gguf')
    monkeypatch.setattr(local_minutes, 'power_connected', lambda: False)
    monkeypatch.setattr(local_minutes, 'probe_intel_gpu', lambda cancel: ('Vulkan1', 'Intel UHD'))
    attempts = []
    messages = []

    def complete(executable, *args, device=None):
        attempts.append((executable, device))
        if device:
            raise RuntimeError('Falha no driver')
        return 'ok'

    monkeypatch.setattr(local_minutes, 'run_completion', complete)
    generator = local_minutes.MinutesGenerator(True, threading.Event(), messages.append)
    assert generator('instrução', 'fala', 10) == 'ok'
    assert generator('instrução', 'outra fala', 10) == 'ok'
    assert attempts == [(vulkan, 'Vulkan1'), (cpu, None), (cpu, None)]
    assert generator.backend == 'CPU'
    assert any('Vulkan' in message and 'CPU' in message for message in messages)


def test_cuda_detects_dedicated_gpu_and_falls_back_to_cpu_after_failure(monkeypatch, tmp_path):
    cpu = tmp_path / 'llama-runtime' / 'llama-completion.exe'
    cuda = tmp_path / 'llama-runtime-cuda' / 'llama-completion.exe'
    monkeypatch.setattr(local_minutes, 'runtime_path', lambda *, use_cuda=False: cuda if use_cuda else cpu)
    monkeypatch.setattr(local_minutes, 'model_installed', lambda: True)
    monkeypatch.setattr(local_minutes, 'model_path', lambda: tmp_path / 'model.gguf')
    monkeypatch.setattr(local_minutes, 'power_connected', lambda: False)
    monkeypatch.setattr(settings, 'data_dir', lambda: str(tmp_path))
    calls, messages = [], []

    def process(command, cwd, flags, env, cancel, timeout=600):
        calls.append(command)
        if '--list-devices' in command:
            assert command[0] == str(cuda)
            return (b'Available devices:\n'
                    b'  Vulkan0: Intel(R) UHD Graphics (4096 MiB, 4000 MiB free)\n'
                    b'  CUDA0: NVIDIA GeForce GT 1030 (2048 MiB, 1024 MiB free)\n'
                    b'  CUDA1: NVIDIA GeForce GTX 1650 (4096 MiB, 3584 MiB free)\n')
        if len(calls) == 3:
            raise local_minutes.GenerationTimeout()
        return b'ok'

    monkeypatch.setattr(local_minutes, '_complete_process', process)
    generator = local_minutes.MinutesGenerator(False, threading.Event(), messages.append, prefer_cuda=True)
    assert generator('Instruções', 'Fala 1', 10) == 'ok'
    assert generator.backend == 'GPU NVIDIA (CUDA)'
    gpu_args = calls[1]
    assert gpu_args[gpu_args.index('--device'):gpu_args.index('--device') + 4] == [
        '--device', 'CUDA1', '-ngl', 'auto']
    assert generator('Instruções', 'Fala 2', 10) == 'ok'
    assert generator('Instruções', 'Fala 3', 10) == 'ok'
    assert [args[0] for args in calls] == [str(cuda), str(cuda), str(cuda), str(cpu), str(cpu)]
    assert generator.backend == 'CPU'
    assert any('CUDA' in message and 'CPU' in message for message in messages)


def wait(backend, jid):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = backend.job_status(jid)
        if job['state'] not in ('running', 'queued'):
            return job
        time.sleep(.02)
    pytest.fail(f'Job não terminou: {jid}')
