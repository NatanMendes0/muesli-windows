// Sidebar integration tests. Run with jsdom available to Node (e.g. via NODE_PATH).
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const uiDir = process.env.MUESLI_TEST_UI_DIR || 'ui';
const script = readFileSync(join(uiDir, 'app.js'), 'utf8');
const html = readFileSync(join(uiDir, 'index.html'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup(t) {
  const { window: w } = new JSDOM(html, { url: 'https://muesli.test', runScripts: 'outside-only' });
  t.after(() => w.close());
  const $ = id => w.document.getElementById(id);
  const category = { id: 7, name: 'Projetos' };
  let meetings = [
    {id: 10, title: 'Primeira reunião', template_id: 'auto', preview: '', created_at: 100, ended_at: 200},
    {id: 20, title: 'Reunião revisada', template_id: 'daily', preview: 'Ata anterior', category_id: 7, created_at: 101, ended_at: 201},
  ];
  const calls = [];
  const api = {
    get_templates() {}, get_settings() {}, list_categories: async () => [category],
    list_meetings: async () => meetings,
    get_meeting: async id => meetings.find(m => m.id === id),
    save_notes: async () => ({ok: true}),
    start_meeting: async (title, template, categoryId) => {
      calls.push(['start', categoryId]);
      if (categoryId !== null && typeof categoryId !== 'number') return {ok: false, error: 'error binding parameter 1'};
      meetings = [...meetings, {id: 30, title: 'Nova reunião', template_id: template, category_id: categoryId, created_at: 102}];
      return {ok: true, meeting_id: 30};
    },
    enhance_meeting: async (...args) => { calls.push(['enhance', ...args]); return {ok: true, job_id: 'minutes-20', persistent: true}; },
    list_minutes_jobs: async () => [{id: 'minutes-20', meeting_id: 20, state: 'queued', title: 'Reunião revisada',
      profile: 'treinamento', completed: 0, total: 0, failed: 0, detail: 'Na fila'}],
    copy_copilot_prompt: async id => { calls.push(['copy', id]); return {ok: true}; },
    delete_meeting: async id => { calls.push(['delete', id]); meetings = meetings.filter(m => m.id !== id); return {ok: true}; },
  };
  w.pywebview = { api };
  w.confirm = () => true;
  // Wire the real page without boot polls or platform hardware requests.
  w.eval(script.slice(0, script.lastIndexOf('\nwireEvents();')) +
    '\nwireEvents(); window.ui = {state, refreshStatus, refreshMeetings, openMeeting, checkUpdatesUi, startDownloadUpdate};');
  w.ui.state.settings = {minutes: {enabled: true, profile: 'treinamento'}};
  const card = title => [...w.document.querySelectorAll('.meeting-item')].find(b => b.querySelector('.title').textContent === title);
  return {w, $, card, calls, api};
}

test('a versão do executável substitui o selo antigo e permanece igual ao verificar atualizações', async t => {
  const {w, $, api} = setup(t);
  const version = process.env.MUESLI_TEST_VERSION || '9.8.7';
  const badge = w.document.querySelector('.logo .ver-badge');
  badge.textContent = 'v1.6.5';
  api.get_state = async () => ({version, llm: {}, stt_model: {}, audio_backend_ok: true});
  await w.ui.refreshStatus();
  assert.equal(badge.textContent, `v${version}`);
  assert.equal(badge.hidden, false);
  assert.equal(w.document.title, `Muesli v${version}`);
  assert.equal($('updateCurrentVer').textContent, `Versão instalada: v${version}`);

  api.check_updates = async () => ({ok: true, current_version: '1.6.5', latest_version: 'v99.0.0',
    has_update: true, can_install: true});
  await w.ui.checkUpdatesUi(false);
  assert.equal(badge.textContent, `v${version}`);
  assert.equal($('updateCurrentVer').textContent, `Versão instalada: v${version}`);
  assert.equal($('ucVer').textContent, 'v99.0.0');
});

for (const id of ['btnNewHome', 'btnNew']) {
  test(`${id} inicia e mostra a reunião em Sem categoria`, async t => {
    const {w, $, card, calls} = setup(t);
    w.localStorage.setItem('muesli_collapsed_categories', '["uncategorized","7"]');
    $('searchInput').value = 'Busca antiga';
    await w.ui.refreshMeetings();
    $(id).click();
    await tick();
    assert.deepEqual(calls, [['start', null]]);
    assert.equal(w.ui.state.current.id, 30);
    assert.match(card('Nova reunião').closest('section').querySelector('.category-toggle').textContent, /Sem categoria/);
    assert.equal($('searchInput').value, '');
    assert.equal(w.localStorage.getItem('muesli_collapsed_categories'), '["7"]');
  });
}

test('o botão + de uma categoria mantém a categoria escolhida', async t => {
  const {w, card, calls} = setup(t);
  await w.ui.refreshMeetings();
  w.document.querySelector('button[title="Nova reunião em Projetos"]').click();
  await tick();
  assert.deepEqual(calls, [['start', 7]]);
  assert.match(card('Nova reunião').closest('section').querySelector('.category-toggle').textContent, /Projetos/);
});

test('Pausar e Retomar preservam a reunião e ignoram respostas antigas do cronômetro', async t => {
  const {w, $, card, calls, api} = setup(t);
  let activePoll;
  let pollId = 0;
  w.setInterval = callback => { activePoll = callback; return ++pollId; };
  let resolveOldPoll;
  api.poll_session = () => new Promise(resolve => { resolveOldPoll = resolve; });
  let canResume = false;
  api.set_recording_paused = async (meetingId, paused) => {
    calls.push(['setPaused', meetingId, paused]);
    if (!paused && !canResume) return {ok: false, error: 'Microfone indisponível'};
    return {ok: true, status: paused ? 'paused' : 'recording', elapsed: 65};
  };

  $('btnNewHome').click();
  await tick();
  assert.ok($('btnPause').classList.contains('visible'));
  assert.equal($('btnPause').textContent, 'Pausar');
  const oldPoll = activePoll();
  $('btnPause').click();
  assert.ok($('btnPause').disabled && $('btnStop').disabled);
  await tick();
  assert.equal($('btnPause').textContent, 'Retomar');
  assert.equal($('recPauseLabel').hidden, false);
  assert.equal($('recTime').textContent, '01:05');
  assert.equal($('meters').style.display, 'none');
  assert.equal($('trStatus').textContent, 'Gravação em pausa.');
  assert.match(card('Nova reunião').querySelector('.live').textContent, /Em pausa/);
  resolveOldPoll({status: 'recording', elapsed: 99, levels: {}, segments: [], total: 0});
  await oldPoll;
  assert.equal($('recTime').textContent, '01:05');
  assert.equal($('btnPause').textContent, 'Retomar');

  await w.ui.openMeeting(10);
  assert.equal($('btnPause').classList.contains('visible'), false);
  $('btnNew').click();
  await tick();
  assert.equal(w.ui.state.current.id, 30);
  assert.equal($('btnPause').textContent, 'Retomar');
  $('btnPause').click();
  await tick();
  assert.equal($('btnPause').textContent, 'Retomar');
  assert.match($('toast').textContent, /Microfone indisponível/);
  canResume = true;
  $('btnPause').click();
  await tick();
  assert.equal($('btnPause').textContent, 'Pausar');
  assert.equal($('recPauseLabel').hidden, true);
  assert.equal($('meters').style.display, 'flex');
  assert.equal($('recTime').textContent, '01:05');
  assert.equal(w.ui.state.current.id, 30);
  assert.deepEqual(calls, [['start', null], ['setPaused', 30, true], ['setPaused', 30, false], ['setPaused', 30, false]]);
});

test('a interface restaura uma reunião pausada e permite finalizá-la', async t => {
  const {w, $, calls, api} = setup(t);
  const jobs = new Map();
  let intervalId = 0;
  w.setInterval = callback => { jobs.set(++intervalId, callback); return intervalId; };
  w.clearInterval = id => jobs.delete(id);
  api.get_state = async () => ({llm: {}, stt_model: {}, recording: true, paused: true, active_meeting_id: 10});
  api.poll_session = async () => ({status: 'none'});
  api.stop_meeting = async () => { calls.push(['stop']); return {ok: true, job_id: 'stop'}; };
  api.job_status = async id => ({state: 'done', result: id === 'stop' ? {finalize_job_id: 'finalize'} : {local_minutes: true}});

  await w.ui.refreshStatus();
  await w.ui.openMeeting(10);
  assert.equal($('btnPause').textContent, 'Retomar');
  assert.ok($('btnStop').classList.contains('visible'));
  $('btnStop').click();
  await tick();
  assert.ok($('btnPause').disabled);
  await jobs.get(1)();
  assert.equal(w.ui.state.activeMeetingId, 10);
  await jobs.get(intervalId)();
  await tick();
  await jobs.get(intervalId)();
  await tick();
  assert.equal(w.ui.state.recording, false);
  assert.equal(w.ui.state.paused, false);
  assert.equal($('btnPause').classList.contains('visible'), false);
  assert.equal($('btnStop').classList.contains('visible'), false);
  assert.deepEqual(calls, [['stop']]);
});

test('menu usa o ID do card clicado sem trocar a reunião aberta', async t => {
  const {w, $, card, calls} = setup(t);
  await w.ui.openMeeting(10);
  await tick();
  const menu = () => $('meetingContextMenu');
  const action = text => [...menu().querySelectorAll('button')].find(b => b.textContent === text);
  const open = title => {
    const event = new w.MouseEvent('contextmenu', {bubbles: true, cancelable: true, clientX: 20, clientY: 50});
    card(title).dispatchEvent(event);
    assert.ok(event.defaultPrevented);
    assert.equal(menu().hidden, false);
  };
  open('Primeira reunião');
  assert.ok(action('Aprimorar'));
  menu().dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles:true}));
  assert.equal(menu().hidden, true);
  assert.equal(w.document.activeElement, card('Primeira reunião'));
  open('Reunião revisada');
  action('Reaprimorar').click();
  await tick();
  assert.equal($('minutesQueue').hidden, false);
  assert.match($('minutesQueueRows').textContent, /Reunião revisada/);
  open('Reunião revisada');
  action('Copiar prompt para o Copilot').click();
  await tick();
  open('Reunião revisada');
  w.confirm = () => false;
  action('Excluir reunião').click();
  await tick();
  assert.equal(calls.length, 2);
  open('Reunião revisada');
  w.confirm = () => true;
  action('Excluir reunião').click();
  await tick();
  assert.deepEqual(calls, [['enhance', 20, 'daily', 'treinamento'], ['copy', 20], ['delete', 20]]);
  assert.equal(w.ui.state.current.id, 10);
  assert.equal(card('Reunião revisada'), undefined);
});

test('o menu pelo teclado protege uma gravação ativa', async t => {
  const {w, $, card, calls} = setup(t);
  w.ui.state.recording = true;
  w.ui.state.activeMeetingId = 10;
  await w.ui.refreshMeetings();
  const activeCard = card('Primeira reunião');
  activeCard.focus();
  activeCard.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'F10', shiftKey: true, bubbles: true, cancelable: true}));
  assert.equal($('meetingContextMenu').hidden, false);
  assert.equal(w.document.activeElement, $('menuCopyMeeting'));
  assert.equal($('menuEnhanceMeeting').disabled, true);
  assert.equal($('menuDeleteMeeting').disabled, true);
  $('searchInput').dispatchEvent(new w.MouseEvent('pointerdown', {bubbles: true}));
  assert.equal($('meetingContextMenu').hidden, true);
  assert.deepEqual(calls, []);
});

test('Limpar fila pede confirmação, descarta a prévia e preserva a ata salva', async t => {
  const {w, $, api, calls} = setup(t);
  const getMeeting = api.get_meeting;
  api.get_meeting = async id => ({...(await getMeeting(id)), summary_md: 'Ata já salva', summary_provider: 'local_minutes'});
  api.list_minutes_jobs = async () => [{id: 'minutes-20', meeting_id: 20, state: 'partial', title: 'Reunião revisada',
    profile: 'treinamento', completed: 1, total: 2, failed: 1, detail: 'Parcial'}];
  api.get_minutes_preview = async () => ({ok: true, summary_md: 'Prévia descartável'});
  api.job_status = async () => ({state: 'unknown'});
  api.clear_minutes_queue = async () => {
    calls.push(['clear']);
    api.list_minutes_jobs = async () => [];
    return {ok: true, removed_ids: ['minutes-20']};
  };

  await w.ui.openMeeting(20);
  $('btnEnhance').click();
  await tick();
  const preview = [...$('minutesQueueRows').querySelectorAll('button')].find(b => b.textContent === 'Ver parcial');
  preview.click();
  await tick();
  assert.match($('enhancedBody').textContent, /Prévia descartável/);
  const clear = $('btnClearMinutesQueue');
  assert.ok(clear);
  w.confirm = message => { assert.match(message, /temporários/); return false; };
  clear.click();
  assert.deepEqual(calls.map(call => call[0]), ['enhance']);
  w.confirm = () => true;
  clear.click();
  await tick();
  assert.deepEqual(calls.map(call => call[0]), ['enhance', 'clear']);
  assert.equal($('minutesQueueCount').textContent, '0');
  assert.equal($('minutesQueueRows').children.length, 0);
  assert.match($('enhancedBody').textContent, /Ata já salva/);
  assert.doesNotMatch($('enhancedBody').textContent, /Prévia descartável/);
});

test('NVIDIA NIM testa e salva a própria chave sem modificar a da OpenAI ou a transcrição', async t => {
  const {w, $, api, calls} = setup(t);
  const config = {audio: {}, stt: {engine: 'local'}, llm: {provider: 'openai',
    openai_keys: ['********1234'], nvidia_key: '********9876', nvidia_model: 'nvidia/example'}, consent: {}};
  api.get_settings = async () => config;
  api.list_audio_devices = async () => ({ok: true, mics: [], loopbacks: []});
  api.local_minutes_status = async () => ({});
  api.list_ollama_models = async () => [];
  api.get_state = async () => ({llm: {}, stt_model: {}, audio_backend_ok: true});
  api.test_nvidia_key = async (...args) => { calls.push(['testNvidia', ...args]); return {ok: true}; };
  api.save_settings = async patch => { calls.push(['saveSettings', patch]); return {ok: true}; };
  $('btnSettings').click();
  await tick();
  assert.ok($('setNvidiaKey'));
  assert.equal($('setNvidiaKey').type, 'password');
  assert.equal($('setNvidiaKey').value, '********9876');
  $('setLlmProvider').value = 'nvidia';
  assert.equal($('setLlmProvider').value, 'nvidia');
  $('setNvidiaKey').value = 'new-fixture-nvidia';
  $('setModelNvidia').value = 'nvidia/test-chat';
  $('btnTestNvidiaKey').click();
  await tick();
  assert.deepEqual(calls, [['testNvidia', 'new-fixture-nvidia', 'nvidia/test-chat']]);
  assert.match($('nvidiaKeyTestStatus').textContent, /Conexão/);
  $('btnSaveSettings').click();
  await tick();
  const [action, patch] = calls.at(-1);
  assert.equal(action, 'saveSettings');
  assert.equal(patch.llm.provider, 'nvidia');
  assert.equal(patch.llm.nvidia_key, 'new-fixture-nvidia');
  assert.equal(patch.llm.nvidia_model, 'nvidia/test-chat');
  assert.equal(patch.llm.openai_key, '********1234');
  assert.equal(patch.stt.engine, 'local');
  assert.equal(Object.hasOwn(patch.stt, 'nvidia_key'), false);
  assert.equal($('settingsModal').classList.contains('visible'), false);
});

test('Atas locais carregam a preferência Vulkan antiga e salvam CUDA pelo seletor', async t => {
  const {$, api, calls} = setup(t);
  let minutes = {enabled: true, prefer_vulkan: true};
  api.get_settings = async () => ({audio: {}, stt: {engine: 'local'}, llm: {}, minutes, consent: {}});
  api.list_audio_devices = async () => ({ok: true, mics: [], loopbacks: []});
  api.local_minutes_status = async () => ({runtime: true, installed: true});
  api.list_ollama_models = async () => [];
  api.get_state = async () => ({llm: {}, stt_model: {}, audio_backend_ok: true});
  api.save_settings = async patch => {
    calls.push(['saveSettings', patch.minutes]);
    minutes = patch.minutes;
    return {ok: true};
  };
  $('btnSettings').click();
  await tick();
  const backend = $('setLocalMinutesBackend');
  assert.equal(backend.value, 'vulkan');
  backend.value = 'cuda';
  $('btnSaveSettings').click();
  await tick();
  assert.deepEqual(calls.map(([action, saved]) => [action, saved.prefer_cuda, saved.prefer_vulkan]),
    [['saveSettings', true, false]]);
  $('btnSettings').click();
  await tick();
  assert.equal(backend.value, 'cuda');
});

test('Atualizar no aviso abre a aba Atualizações sem aguardar o carregamento das configurações', async t => {
  const {w, $, api, calls} = setup(t);
  let finishSettings;
  api.get_settings = () => new Promise(resolve => { finishSettings = resolve; });
  api.list_audio_devices = async () => ({ok: true, mics: [], loopbacks: []});
  api.local_minutes_status = async () => ({});
  api.list_ollama_models = async () => [];
  api.check_updates = async () => ({ok: true, current_version: '1.6.3', latest_version: 'v1.6.4',
    can_install: true, has_update: true});
  const activeTab = () => w.document.querySelector('#settingsModal .modal-tabs .tab.active').dataset.set;
  const visibleSections = () => [...w.document.querySelectorAll('#settingsModal .set-section.visible')]
    .map(section => section.dataset.set);

  $('btnSettings').click();
  assert.equal(activeTab(), 'audio');
  assert.deepEqual(visibleSections(), ['audio']);
  finishSettings({audio: {}, stt: {}, llm: {}, consent: {}});
  await tick();
  $('btnCloseSettings').click();

  await w.ui.checkUpdatesUi(false);
  assert.equal($('updateCornerBanner').style.display, 'flex');
  $('btnUcOpen').click();
  assert.equal($('settingsModal').classList.contains('visible'), true);
  assert.equal(activeTab(), 'updates');
  assert.deepEqual(visibleSections(), ['updates']);
  assert.equal($('updateCornerBanner').style.display, 'none');
  assert.match($('updateTitle').textContent, /v1\.6\.4/);
  assert.equal($('btnDownloadUpdate').disabled, false);
  assert.deepEqual(calls, []);

  w.document.querySelector('#settingsModal .modal-tabs [data-set="audio"]').click();
  finishSettings({audio: {}, stt: {}, llm: {}, consent: {}});
  await tick();
  assert.equal(activeTab(), 'audio');
  assert.deepEqual(visibleSections(), ['audio']);
});

test('baixar atualização não reinicia; só instala depois de confirmar e salvar notas', async t => {
  const {w, $, api, calls} = setup(t);
  await w.ui.openMeeting(10);
  await tick();
  api.check_updates = async () => ({ok: true, current_version: '1.6.2', latest_version: 'v1.6.3',
    can_install: true, has_update: true});
  api.prepare_update = async tag => { calls.push(['prepare', tag]); return {ok: true, job_id: 'update'}; };
  api.job_status = async () => ({state: 'done', result: {tag: 'v1.6.3'}});
  api.save_notes = async id => { calls.push(['save', id]); return {ok: true}; };
  let draining = true;
  api.install_update = async id => {
    calls.push(['install', id]);
    return draining ? {ok: false, error: 'Aguarde a transcrição'} : {ok: true};
  };
  await w.ui.checkUpdatesUi();
  await w.ui.startDownloadUpdate();
  assert.deepEqual(calls, [['prepare', 'v1.6.3']]);
  assert.equal($('btnDownloadUpdate').textContent, 'Instalar e reiniciar');
  await w.ui.startDownloadUpdate();
  assert.match($('updateStatusText').textContent, /Aguarde a transcrição/);
  assert.equal($('btnDownloadUpdate').disabled, false);
  draining = false;
  await w.ui.startDownloadUpdate();
  assert.deepEqual(calls.slice(1), [['save', 10], ['install', 'update'], ['save', 10], ['install', 'update']]);
  assert.equal($('btnDownloadUpdate').disabled, true);
});
