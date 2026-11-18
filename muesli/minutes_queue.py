"""Single, resumable local inference worker; the SQLite queue is authoritative."""
import json
import logging
import sqlite3
import time

from . import db, local_minutes, minutes_pipeline as pipeline, settings

LOG = logging.getLogger(__name__)


def blocker(api, cfg):
    if not cfg.get('enabled'):
        return 'Ative a geração de atas locais nas configurações.'
    if api._session or api._draining or api._capture_requested:
        return 'Aguardando a gravação e a transcrição terminarem…'
    if cfg.get('ac_only') and not local_minutes.power_connected():
        return 'Ata local aguardando conexão à tomada…'
    if local_minutes.runtime_path() is None:
        return 'Motor de atas indisponível. Use o pacote Windows x64.'
    if not local_minutes.model_installed():
        return 'Baixe o Qwen3 4B em Configurações → IA → Atas locais.'
    return ''


def run(api, recover=False):
    retry_job = None
    delay = .25
    try:
        while not api._closing.is_set():
            try:
                if recover:
                    db.recover_minutes_jobs()
                    recover = False
                if retry_job:
                    db.set_minutes_state(retry_job, 'queued', expected='running')
                    retry_job = None
                api._minutes_wake.clear()
                cfg = settings.load().get('minutes', {})
                with api._start_lock:
                    job = db.next_minutes_job()
                    if (job and not api._closing.is_set() and not blocker(api, cfg)
                            and db.set_minutes_state(job['id'], 'running', 'Preparando transcrição…', expected='queued')):
                        retry_job = api._minutes_running_id = job['id']
                        api._minutes_active = True
                        api._minutes_cancel.clear()
                        api._minutes_idle.clear()
                    else:
                        job = None
                if job:
                    try:
                        _run_job(job, cfg, api._minutes_cancel)
                        retry_job = None
                    finally:
                        with api._start_lock:
                            api._minutes_running_id = None
                            api._minutes_active = False
                            api._minutes_idle.set()
                else:
                    api._minutes_wake.wait(1)
                delay = .25
            except sqlite3.OperationalError as exc:
                LOG.warning('minutes database error code=%s', getattr(exc, 'sqlite_errorcode', None))
                api._closing.wait(delay)
                delay = min(delay * 2, 5)
    finally:
        connection = getattr(db._local, 'conn', None)
        if connection is not None:
            connection.close()


def _run_job(job, cfg, cancel):
    jid = job['id']
    try:
        process(job, cfg, cancel)
    except local_minutes.GenerationCancelled:
        db.set_minutes_state(jid, 'queued', expected='running')
    except local_minutes.GenerationError as exc:
        db.set_minutes_state(jid, 'error', str(exc), expected='running')
    except sqlite3.OperationalError:
        raise
    except Exception as exc:
        LOG.warning('minutes job failed: %s', type(exc).__name__)
        db.set_minutes_state(jid, 'error', 'Não foi possível processar a ata. Tente novamente.', expected='running')


def process(job, cfg, cancel):
    jid, mid, profile = job['id'], job['meeting_id'], job['profile']
    started = time.monotonic()
    watermark = db.minutes_watermark(mid)
    meeting = db.get_meeting(mid)
    if not watermark or not meeting:
        return
    blocks = list(pipeline.blocks_for(db.iter_minutes_segments(mid, watermark[1]), meeting.get('notes_md') or ''))
    if not blocks:
        raise local_minutes.GenerationError('Não há transcrição ou anotações para gerar uma ata local.')
    signature = pipeline.source_signature(blocks, local_minutes.MODEL_HASH, profile)
    if not db.prepare_minutes(jid, signature, len(blocks), watermark):
        return

    def status(detail):
        db.set_minutes_state(jid, 'running', detail, expected='running')

    generator = local_minutes.MinutesGenerator(cfg.get('prefer_vulkan', False), cancel, status)

    def check():
        current = settings.load().get('minutes', {})
        if (cancel.is_set() or not current.get('enabled') or
                (current.get('ac_only') and not local_minutes.power_connected())):
            raise local_minutes.GenerationCancelled()

    def complete(*args):
        check()
        return generator(*args)

    cached = db.load_minutes_blocks(jid)
    records, failed = [], []
    for ordinal, block in enumerate(blocks):
        check()
        saved = cached.get(ordinal)
        if saved and saved['state'] == 'done':
            records.append(saved['result'])
            continue
        start, end = block[0]['t0'], max(row['t1'] for row in block)
        status(f'Analisando bloco {ordinal + 1} de {len(blocks)}…')
        try:
            record = pipeline.extract_with_split(block, profile, complete, status)
        except (pipeline.InvalidOutput, local_minutes.GenerationTimeout):
            failed.append((start, end))
            state, record = 'error', None
        else:
            records.append(record)
            state = 'done'
        if not db.save_minutes_block(jid, ordinal, state, record, start, end):
            return
    check()
    if failed:
        db.set_minutes_state(jid, 'partial', 'Alguns blocos falharam. Veja o rascunho parcial ou refaça só os blocos com falha.',
                             expected='running')
        return
    status('Consolidando o resumo e montando a ata…')
    try:
        overview = pipeline.synopsis(records, profile, complete, status)
    except local_minutes.GenerationTimeout:
        overview = ''
    check()
    latest = db.get_meeting(mid)
    if latest:
        markdown = pipeline.render_minutes(latest, profile, records, [], overview)
        detail = '' if overview else 'Os capítulos estão completos, mas a síntese geral não ficou disponível.'
        db.finish_minutes(jid, markdown, generator.backend, round(time.monotonic() - started), detail, watermark)


def preview(meeting_id):
    job = db.get_minutes_job(meeting_id)
    meeting = db.get_meeting(meeting_id)
    if not job or not meeting or not job['snapshot'] or job['state'] in ('done', 'conflict'):
        return {'ok': False, 'error': 'Não há rascunho parcial disponível.'}
    watermark = db.minutes_watermark(meeting_id)
    if not watermark or list(watermark) != json.loads(job['snapshot']):
        return {'ok': False, 'error': 'A fonte mudou. Os blocos serão refeitos ao retomar.'}
    rows = db.load_minutes_blocks(job['id']).values()
    records = [row['result'] for row in rows if row['state'] == 'done']
    failed = [(row['start_sec'], row['end_sec']) for row in rows if row['state'] == 'error']
    if not records:
        return {'ok': False, 'error': 'Nenhum bloco concluído ainda.'}
    pending = max(job['total'] - len(records) - len(failed), 0)
    return {'ok': True, 'summary_md': pipeline.render_minutes(
        meeting, job['profile'], records, failed, '', pending, preview=True)}
