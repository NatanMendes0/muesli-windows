// Sidebar integration tests. Run with jsdom available to Node (e.g. via NODE_PATH).
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const script = readFileSync('ui/app.js', 'utf8');
const html = readFileSync('ui/index.html', 'utf8');
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
    '\nwireEvents(); window.ui = {state, refreshMeetings, openMeeting, checkUpdatesUi, startDownloadUpdate};');
  w.ui.state.settings = {minutes: {enabled: true, profile: 'treinamento'}};
  const card = title => [...w.document.querySelectorAll('.meeting-item')].find(b => b.querySelector('.title').textContent === title);
  return {w, $, card, calls, api};
}

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
