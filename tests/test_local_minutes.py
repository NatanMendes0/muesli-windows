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


def draft_json(summary, actions=()):
    return json.dumps(dict(participantes='', citados='', resumo=summary, deliberacoes=[], acoes=list(actions)))


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


def test_long_transcript_maps_both_ends_and_consolidates():
    source = 'INÍCIO\n' + ('Fala com acentuação.\n' * 2000) + 'ÚLTIMA DECISÃO'
    calls = []

    def generate(instructions, evidence, tokens, schema=None):
        calls.append(evidence)
        text = 'Decisão: primeira e última.'
        return draft_json(text) if schema else text

    draft = local_minutes.summarize({'title': 'Projeto', 'duration_sec': 600, 'created_at': 0}, source, generate)
    assert len(calls) > 2
    assert 'INÍCIO' in calls[0]
    assert any('ÚLTIMA DECISÃO' in text for text in calls)
    assert 'Decisão: primeira e última.' in calls[-1]
    assert '# Ata Executiva: Projeto' in draft
    assert all(len(text.encode('utf-8')) <= local_minutes.INPUT_BYTES for text in calls)


def test_auto_minutes_wait_for_whisper_and_use_final_text(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    monkeypatch.setattr(db._local, 'conn', None, raising=False)
    monkeypatch.setattr(settings, 'load', lambda: {'minutes': {'enabled': True, 'ac_only': True}})
    db.init()
    mid = db.create_meeting('Projeto', 'auto')
    drained = threading.Event()
    inserted = threading.Event()
    ac = threading.Event()
    monkeypatch.setattr(local_minutes, 'power_connected', ac.is_set)
    backend = api.Api()

    class Capture:
        status = 'recording'
        meeting_id = mid

        def stop(self):
            self.status = 'stopped'
            db.end_meeting(mid, 50)
            return 50

        def wait_for_transcription(self):
            drained.wait(5)
            db.add_segment(mid, 1, 2, 'Eu', 'Última decisão')
            inserted.set()

    monkeypatch.setattr(backend, '_session', Capture())
    monkeypatch.setattr(local_minutes, 'generate', lambda instructions, evidence, tokens, schema, cancel: draft_json(
        'Última decisão' if inserted.is_set() and 'Última decisão' in evidence else 'INCOMPLETA'))
    with patch.object(api.enhance, 'enhance', side_effect=AssertionError('não deve chamar IA externa')):
        stopped = wait(backend, backend.stop_meeting()['job_id'])
        result = stopped['result']
        assert isinstance(result, dict)
        jid = result['minutes_job_id']
        assert backend.job_status(jid)['state'] == 'running'
        meeting = db.get_meeting(mid)
        assert meeting and not meeting['summary_md']
        drained.set()
        deadline = time.monotonic() + 5
        while 'tomada' not in backend.job_status(jid).get('detail', '') and time.monotonic() < deadline:
            time.sleep(.02)
        assert 'tomada' in backend.job_status(jid).get('detail', '')
        meeting = db.get_meeting(mid)
        assert meeting and not meeting['summary_md']
        ac.set()
        assert wait(backend, jid)['state'] == 'done'
    meeting = db.get_meeting(mid)
    assert meeting and 'Última decisão' in meeting['summary_md']
    assert meeting['summary_provider'] == 'local_minutes'
    assert backend.save_minutes_edit(mid, 'Ata revisada')['ok']
    db.conn().close()
    db._local.conn = None
    updated = db.get_meeting(mid)
    assert updated and updated['summary_md'] == 'Ata revisada'
    db.conn().close()


def test_draft_does_not_print_unsupported_deadline_or_owner():
    source = 'Foi definido que o painel precisa de revisão nesta semana.'
    actions = [dict(acao='Revisar painel', responsavel='Ana', prazo='sexta', trecho=source),
               dict(acao='Enviar orçamento', responsavel='Bruno', prazo='hoje', trecho='citação inventada')]
    draft = local_minutes.summarize({'title': 'Painel', 'created_at': 0}, source,
                                    lambda *args: draft_json('A equipe decidiu revisar o painel.', actions))
    assert '| Revisar painel | não informado | não informado |' in draft
    assert 'Enviar orçamento' not in draft


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


def wait(backend, jid):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = backend.job_status(jid)
        if job['state'] != 'running':
            return job
        time.sleep(.02)
    pytest.fail(f'Job não terminou: {jid}')
