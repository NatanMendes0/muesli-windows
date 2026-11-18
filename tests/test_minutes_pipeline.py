import json
import sqlite3
import subprocess
import sys
import threading
import time


import pytest

from muesli import api, db, local_minutes, minutes_pipeline as mp, minutes_queue, settings


def turn(i, time, text):
    return dict(id=i, t0=time, t1=time+3, speaker='Them', text=text)


def fact(i, quote, title, owner='', due='', kind='acao'):
    return dict(tipo=kind, texto=title, fonte=i, trecho=quote, responsavel=owner, prazo=due)


def test_structured_generation_retries_only_bad_output_and_validates_shape():
    calls = []
    values = iter(['{"resumo":"cortado', '{"resumo":"ok","fatos":[]}'])

    def model(prompt, evidence, cap, schema):
        calls.append((prompt, cap))
        return next(values)

    assert mp.complete_json(model, 'extraia', 'evidência', mp.BLOCK_SCHEMA, 500, 900)['resumo'] == 'ok'
    assert len(calls) == 2 and calls[0][1] < calls[1][1] and calls[0][0] != calls[1][0]
    with pytest.raises(mp.InvalidOutput):
        mp.complete_json(lambda *args: '{"resumo": 3, "fatos": []}', 'extraia', '', mp.BLOCK_SCHEMA, 500, 900)
    with pytest.raises(local_minutes.GenerationCancelled):
        mp.complete_json(lambda *args: (_ for _ in ()).throw(local_minutes.GenerationCancelled()),
                         'extraia', '', mp.BLOCK_SCHEMA, 500, 900)


@pytest.mark.parametrize('minutes', [4, 60, 360])
def test_transcript_preserves_actions_and_revisions_from_first_middle_and_last_blocks(minutes):
    segments = [turn(i+1, i*30, f'À equipe foram apresentados exemplos e exercícios da seção {i}. ' * 3)
                for i in range(minutes*2)]
    distinct = {0: 'início', len(segments)//2: 'meio', len(segments)-1: 'fim'}
    source = {}
    for idx, marker in distinct.items():
        quote = f'Ana vai revisar o item de {marker} até sexta-feira.'
        segments[idx]['text'] += quote
        source[segments[idx]['id']] = [fact(segments[idx]['id'], quote, f'Revisar item de {marker}', 'Ana', 'sexta-feira')]
    segments[1]['text'] = 'Sugestão: trocar a ferramenta.'
    source[2] = [fact(2, segments[1]['text'], 'Trocar a ferramenta', kind='proposta')]
    segments[2]['text'] = 'Ana vai revisar o item de início até sexta-feira.'
    source[3] = [fact(3, segments[2]['text'], 'Revisar item de início', 'Ana', 'sexta-feira')]
    corrected = segments[-2]
    corrected['text'] = 'Não vamos trocar a ferramenta. Ana vai revisar o item de início na segunda-feira, não na sexta.'
    source[corrected['id']] = [fact(corrected['id'], 'Não vamos trocar a ferramenta.', 'Manter a ferramenta', kind='decisao'),
                              fact(corrected['id'], 'Ana vai revisar o item de início na segunda-feira',
                                   'Revisar item de início', 'Ana', 'segunda-feira')]
    blocks = list(mp.blocks_for(segments, ''))
    if minutes > 4:
        assert len(blocks) > 4
    assert {row['id'] for block in blocks for row in block} == set(range(1, len(segments)+1))
    calls = []

    def generator(prompt, evidence, cap, schema):
        calls.append(len(evidence.encode('utf-8')))
        ids = {row['id'] for row in segments if f"[id={row['id']}]" in evidence}
        facts = [item for i in ids for item in source.get(i, ()) if item['trecho'] in evidence]
        return json.dumps(dict(resumo='Exercícios revisados.', fatos=facts))

    records = [mp.extract_block(block, 'executiva', generator) for block in blocks]
    md = mp.render_minutes(dict(title='Piloto', created_at=0, duration_sec=minutes*60), 'executiva', records, [], 'Síntese.')
    assert max(calls) <= mp.INPUT_BYTES
    for marker in distinct.values():
        assert md.count(f'| Revisar item de {marker} | Ana | sexta-feira |') == 1
    assert f'[[seg:{len(segments)}@' in md
    assert '| Revisar item de início | Ana | segunda-feira |' in md
    assert 'versões divergentes' in md
    assert '**Proposta:** Trocar a ferramenta' in md and '**Decisão:** Manter a ferramenta' in md
    assert 'Rascunho parcial' not in md


def test_unverifiable_fact_retries_before_marking_a_block_complete():
    row = turn(12, 10, 'Ana vai enviar o relatório quinta-feira.')
    outputs = iter([fact(11, row['text'], 'Enviar o relatório', 'Ana', 'quinta-feira'),
                    fact(12, row['text'], 'Enviar o relatório', 'Ana', 'quinta-feira')])
    record = mp.extract_block([row], 'executiva', lambda *args: json.dumps(dict(resumo='Relatório.', fatos=[next(outputs)])))
    facts = record['fatos']
    assert isinstance(facts, list) and [item['fonte'] for item in facts] == [12]


def test_full_fact_array_splits_a_single_turn_instead_of_losing_actions():
    items = [f'Ana vai revisar o item {i:02d} sexta-feira.' for i in range(12)]
    row = turn(12, 10, ' '.join(items))

    def generate(prompt, evidence, cap, schema):
        visible = [(i, quote) for i, quote in enumerate(items) if quote in evidence]
        return json.dumps(dict(resumo='Revisão', fatos=[
            fact(12, quote, f'Revisar item {i:02d}', 'Ana', 'sexta-feira') for i, quote in visible[:10]]))

    # No sentence boundary is invented; both halves keep the original ID.
    record = mp.extract_with_split([row], 'executiva', generate, lambda _: None)
    facts = record['fatos']
    assert isinstance(facts, list) and len(facts) == 12


def test_failed_block_does_not_disguise_coverage_and_training_does_not_force_actions():
    first = turn(1, 0, 'Ana vai validar o piloto sexta-feira.')
    fake = fact(1, first['text'], 'Validar piloto', 'Bia', 'sábado')
    records = [mp.extract_block([first], 'executiva', lambda *args: json.dumps(dict(resumo='Piloto.', fatos=[fake, fake])))]
    m = dict(title='Piloto', created_at=0, duration_sec=240)
    partial = mp.render_minutes(m, 'executiva', records, [(60, 120)], '', pending=2)
    assert 'Rascunho parcial' in partial and '00:01:00' in partial and '2 bloco(s)' in partial
    assert partial.count('| Validar piloto | não informado | não informado |') == 1
    training = [mp.extract_block([first], 'treinamento', lambda *args: '{"resumo":"Exercício.","fatos":[]}')]
    assert 'Plano de ação' not in mp.render_minutes(m, 'treinamento', training, [], '')


def test_default_model_replaces_old_gguf():
    assert 'Qwen3-4B-Instruct-2507-Q4_K_M' in local_minutes.MODEL_NAME
    assert local_minutes.MODEL_BYTES == 2497280736


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'minutes.sqlite'))
    monkeypatch.setattr(db._local, 'conn', None, raising=False)
    db.init()
    yield
    if getattr(db._local, 'conn', None):
        db._local.conn.close()
        db._local.conn = None


@pytest.mark.parametrize('mode, expected', [
    ('automatic', 'Homologação do portal'),
    ('manual', 'Título definido por mim'),
    ('renamed', 'Título definido por mim'),
    ('bad_output', 'Título provisório'),
    ('timeout', 'Título provisório'),
])
def test_local_minutes_publish_title_with_the_summary(database, monkeypatch, mode, expected):
    mid = db.create_meeting(expected if mode == 'manual' else '', 'auto')
    if mode != 'manual':
        db.update_meeting(mid, title='Título provisório')
    db.end_meeting(mid, 180)
    db.add_segment(mid, 0, 4, 'Them', 'Validamos a homologação do portal.')
    cfg = {'enabled': True}
    monkeypatch.setattr(settings, 'load', lambda: {'minutes': cfg})
    generated = []

    def model(prompt, evidence, cap, schema, cancel):
        if schema == mp.BLOCK_SCHEMA:
            return '{"resumo":"O grupo validou a homologação do portal.","fatos":[]}'
        generated.append(evidence)
        if mode == 'renamed':
            api.Api().rename_meeting(mid, expected)
        if mode == 'timeout':
            raise local_minutes.GenerationTimeout()
        title = 'Título: "# Homologação do portal"' if mode != 'bad_output' else '<think>sem título</think>'
        return json.dumps(dict(titulo=title, resumo='A homologação foi validada.'))

    monkeypatch.setattr(local_minutes, 'generate', model)
    job = db.enqueue_minutes(mid, 'executiva', 'estratégia')
    assert job and db.set_minutes_state(job['id'], 'running')
    minutes_queue.process(job, cfg, threading.Event())
    meeting = db.get_meeting(mid)
    assert meeting and meeting['title'] == expected
    assert meeting['summary_md'].startswith(f'# Ata Executiva: {expected}\n')
    assert int(meeting['title_manual']) == int(mode in ('manual', 'renamed'))
    result = api.Api().job_status(job['id'])['result']
    assert isinstance(result, dict) and result['title'] == expected
    if mode == 'manual':
        assert not generated
    else:
        assert generated == ['O grupo validou a homologação do portal.']


def test_clear_queue_discards_progress_and_prompt_files_without_deleting_meetings(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', lambda: str(tmp_path))
    audio = tmp_path / 'recording.wav'
    audio.write_bytes(b'audio')
    model = local_minutes.model_path()
    model.write_bytes(b'model')
    orphan = tmp_path / 'minutes-abcd1234'
    orphan.mkdir()
    (orphan / 'prompt.txt').write_text('rascunho temporário', encoding='utf-8')
    unrelated = tmp_path / 'minutes-keep1234'
    unrelated.mkdir()
    (unrelated / 'notes.txt').write_text('preservar', encoding='utf-8')

    jobs = {}
    for state in ('queued', 'paused', 'partial', 'error', 'conflict', 'done'):
        mid = db.create_meeting(state, 'auto')
        db.end_meeting(mid, 60, str(audio))
        db.update_meeting(mid, notes_md='anotações', summary_md='ata salva')
        db.add_segment(mid, 0, 3, 'Them', 'transcrição original')
        job = db.enqueue_minutes(mid, 'executiva', 'estratégia')
        assert job
        jid = job['id']
        db.set_minutes_state(jid, 'running')
        assert db.save_minutes_block(jid, 0, 'done', {'resumo': 'progresso'}, 0, 3)
        db.set_minutes_state(jid, state)
        jobs[state] = mid, jid

    response = api.Api().clear_minutes_queue()
    assert response['ok'] and not response.get('warning')
    assert response['removed_files'] == 1
    assert set(response['removed_ids']) == {jid for state, (_, jid) in jobs.items() if state != 'done'}
    assert not orphan.exists()
    assert unrelated.is_dir() and audio.is_file() and model.is_file()
    db.conn().close()
    db._local.conn = None
    db.recover_minutes_jobs()
    assert db.list_minutes_jobs() == []
    for state, (mid, jid) in jobs.items():
        meeting = db.get_meeting(mid)
        assert meeting and meeting['summary_md'] == 'ata salva' and meeting['notes_md'] == 'anotações'
        assert db.get_segments(mid)[0]['text'] == 'transcrição original'
        assert bool(db.load_minutes_blocks(jid)) == (state == 'done')
    mid, jid = jobs['partial']
    retry = db.enqueue_minutes(mid, 'executiva', 'estratégia')
    assert retry and retry['id'] != jid


def test_clear_queue_does_not_follow_linked_prompt_paths(database, tmp_path, monkeypatch):
    data = tmp_path / 'Muesli'
    data.mkdir()
    monkeypatch.setattr(settings, 'data_dir', lambda: str(data))
    protected = tmp_path / 'original'
    protected.mkdir()
    original = protected / 'prompt.txt'
    original.write_text('arquivo fora da fila', encoding='utf-8')
    linked_dir = data / 'minutes-linked01'
    real_dir = data / 'minutes-linked02'
    real_dir.mkdir()
    linked_file = real_dir / 'prompt.txt'
    try:
        linked_dir.symlink_to(protected, target_is_directory=True)
        linked_file.symlink_to(original)
    except OSError as exc:
        if sys.platform == 'win32' and getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Creating symlinks requires Windows Developer Mode or elevated privileges')
        raise

    response = api.Api().clear_minutes_queue()
    assert response['ok'] and response['removed_files'] == 0
    assert linked_dir.is_symlink() and linked_file.is_symlink()
    assert original.read_text(encoding='utf-8') == 'arquivo fora da fila'


def test_clear_queue_reports_a_locked_prompt_and_allows_retry(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', lambda: str(tmp_path))
    folder = tmp_path / 'minutes-testlock'
    folder.mkdir()
    prompt = folder / 'prompt.txt'
    prompt.write_text('conteúdo temporário', encoding='utf-8')
    unlink = type(prompt).unlink

    def deny_prompt(path, *args, **kwargs):
        if path == prompt:
            raise PermissionError('simulated file lock')
        return unlink(path, *args, **kwargs)

    backend = api.Api()
    with monkeypatch.context() as patcher:
        patcher.setattr(type(prompt), 'unlink', deny_prompt)
        response = backend.clear_minutes_queue()
        assert response['ok'] and response['warning'] and prompt.exists()
    response = backend.clear_minutes_queue()
    assert response['ok'] and response['removed_files'] == 1 and not prompt.exists()


def test_clear_queue_cancels_active_worker_before_cleaning_prompt_files(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', lambda: str(tmp_path))
    monkeypatch.setattr(settings, 'load', lambda: {'minutes': {'enabled': True}})
    monkeypatch.setattr(local_minutes, 'runtime_path', lambda: tmp_path / 'llama-completion')
    monkeypatch.setattr(local_minutes, 'model_installed', lambda: True)
    started = threading.Event()
    stopped = threading.Event()

    def complete(command, cwd, flags, env, cancel):
        started.set()
        assert cancel.wait(5)
        stopped.set()
        raise local_minutes.GenerationCancelled()

    monkeypatch.setattr(local_minutes, '_complete_process', complete)
    mid = db.create_meeting('Reunião', 'auto')
    db.end_meeting(mid, 30)
    db.add_segment(mid, 0, 4, 'Them', 'Dados para a ata local.')
    backend = api.Api()
    try:
        job = backend.enhance_meeting(mid, 'auto')
        assert started.wait(5)
        assert list(tmp_path.glob('minutes-*/prompt.txt'))
        response = backend.clear_minutes_queue()
        assert response['ok'] and not response.get('warning') and response['removed_ids'] == [job['job_id']]
        assert stopped.is_set()
        assert not list(tmp_path.glob('minutes-*/prompt.txt'))
        assert backend.job_status(job['job_id'])['state'] == 'unknown'
        meeting = db.get_meeting(mid)
        assert meeting and meeting['summary_md'] == ''
    finally:
        backend._close_minutes()


def test_checkpoint_survives_reopen_and_protects_summary(database):
    mid = db.create_meeting('Reunião', 'auto')
    db.end_meeting(mid, 240)
    db.update_meeting(mid, summary_md='ata anterior', summary_provider='local_minutes')
    job = db.enqueue_minutes(mid, 'executiva', 'modelo-v1')
    assert job
    db.set_minutes_state(job['id'], 'running')
    signature, watermark = 'hash dos blocos', db.minutes_watermark(mid)
    db.prepare_minutes(job['id'], signature, 2)
    assert db.save_minutes_block(job['id'], 0, 'done', {'resumo': 'primeiro'}, 0, 9)
    result = subprocess.run([sys.executable, '-c', '''
import sys
from muesli import db
db.DB_PATH = sys.argv[1]
db.init()
db.recover_minutes_jobs()
assert db.get_minutes_job(int(sys.argv[2]))['state'] == 'queued'
''', db.DB_PATH, str(mid)], text=True, capture_output=True)
    assert not result.returncode, result.stderr
    restored = db.get_minutes_job(mid)
    assert restored and restored['state'] == 'queued'
    assert db.load_minutes_blocks(job['id'])[0]['result']['resumo'] == 'primeiro'
    retry = db.enqueue_minutes(mid, 'executiva', 'modelo-v1')
    assert retry and retry['id'] == job['id']
    db.update_meeting(mid, summary_md='ata revisada')
    db.set_minutes_state(job['id'], 'running')
    assert not db.finish_minutes(job['id'], lambda _: 'nova ata', 'CPU', 10, '', watermark, 'novo título')
    meeting = db.get_meeting(mid)
    assert meeting and meeting['summary_md'] == 'ata revisada'
    db.delete_meeting(mid)
    assert db.get_minutes_job(mid) is None and db.load_minutes_blocks(job['id']) == {}


def test_worker_retries_one_failed_block_and_reuses_checkpoints_after_restart(database, monkeypatch):
    mid = db.create_meeting('Alinhamento', 'auto')
    db.end_meeting(mid, 240)
    for i, tag in enumerate(('primeiro', 'meio', 'último')):
        db.add_segment(mid, i * 100, i * 100 + 5, 'Them', f'Ana vai revisar o item {tag} até segunda-feira.')
    db.update_meeting(mid, summary_md='Versão anterior', summary_provider='local_minutes')
    cfg = {'enabled': True, 'profile': 'executiva'}
    monkeypatch.setattr(settings, 'load', lambda: {'minutes': cfg})
    attempts = []
    broken = True

    def model(prompt, evidence, cap, schema, cancel):
        if schema == mp.SYNOPSIS_SCHEMA:
            return '{"resumo":"Síntese final."}'
        row = next(tag for tag in ('primeiro', 'meio', 'último') if tag in evidence)
        attempts.append(row)
        if row == 'meio' and broken:
            return '{"resumo":"incompleto'
        sid = int(evidence.split(']')[0].split('=')[1])
        quote = evidence.split('Outros: ', 1)[1]
        return json.dumps(dict(resumo=row, fatos=[fact(sid, quote, f'Revisar item {row}', 'Ana', 'segunda-feira')]))

    monkeypatch.setattr(local_minutes, 'generate', model)
    queued = db.enqueue_minutes(mid, 'executiva', 'estratégia')
    assert queued
    jid = queued['id']
    db.set_minutes_state(jid, 'running')
    minutes_queue.process(queued, cfg, threading.Event())
    backend = api.Api()
    assert backend.job_status(jid)['state'] == 'partial'
    assert backend.job_status(jid)['completed'] == 2
    assert backend.job_status(jid)['failed'] == 1
    preview = backend.get_minutes_preview(mid)
    assert preview['ok'] and '00:01:40' in preview['summary_md']
    previous = db.get_meeting(mid)
    assert previous and previous['summary_md'] == 'Versão anterior'
    db.conn().close()
    db._local.conn = None
    db.init()
    broken = False
    attempts.clear()
    db.set_minutes_state(jid, 'running')
    minutes_queue.process(queued, cfg, threading.Event())
    assert attempts == ['meio']
    published = api.Api().job_status(jid)
    assert published['state'] == 'done'
    result = published['result']
    assert isinstance(result, dict)
    assert all(f'Revisar item {tag}' in result['summary_md'] for tag in ('primeiro', 'meio', 'último'))


def test_transcript_snapshot_orders_equal_timestamps_and_does_not_read_new_rows(database):
    mid = db.create_meeting('Teste', 'auto')
    a = db.add_segment(mid, 2, 3, 'Them', 'B')
    b = db.add_segment(mid, 1, 2, 'Me', 'A')
    c = db.add_segment(mid, 2, 4, 'Me', 'C')
    snapshot = db.minutes_watermark(mid)
    assert snapshot
    db.add_segment(mid, 0, 1, 'Them', 'nova chegada')
    assert [row['id'] for row in db.iter_minutes_segments(mid, snapshot[1], page_size=1)] == [b, a, c]


def test_editor_rejects_replacing_a_new_generation_from_a_stale_base(database):
    mid = db.create_meeting('Ata', 'auto')
    db.update_meeting(mid, summary_md='Ata nova', summary_provider='local_minutes')
    backend = api.Api()
    rejected = backend.save_minutes_edit(mid, 'Revisão manual', 'Ata anterior')
    assert not rejected['ok'] and rejected['latest'] == 'Ata nova'
    assert backend.save_minutes_edit(mid, 'Revisão manual', 'Ata nova')['ok']


def test_minutes_worker_retries_a_transient_claim_error(database, monkeypatch):
    mid = db.create_meeting('Fila', 'auto')
    db.end_meeting(mid, 240)
    db.add_segment(mid, 1, 2, 'Me', 'Revisão original')
    monkeypatch.setattr(settings, 'load', lambda: {'minutes': {'enabled': True}})
    monkeypatch.setattr(local_minutes, 'runtime_path', lambda: 'runtime')
    monkeypatch.setattr(local_minutes, 'model_installed', lambda: True)
    monkeypatch.setattr(local_minutes, 'generate', lambda *args: '{"resumo":"Revisão.","fatos":[]}')
    read = db.next_minutes_job
    failed = threading.Event()

    def locked_once():
        if not failed.is_set():
            failed.set()
            raise sqlite3.OperationalError('database is locked')
        return read()

    monkeypatch.setattr(db, 'next_minutes_job', locked_once)
    backend = api.Api()
    try:
        result = backend.enhance_meeting(mid, 'auto')
        assert result['ok'] and failed.wait(3)
        assert backend._minutes_worker is not None
        deadline = time.monotonic() + 4
        while backend.job_status(result['job_id'])['state'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.05)
        assert backend.job_status(result['job_id'])['state'] == 'done'
    finally:
        backend._close_minutes()
