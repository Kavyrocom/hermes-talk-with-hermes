"""The Desktop half, checked without the app.

Static rules come from the Desktop plugin SDK: only three import specifiers
resolve, no JSX, no hardcoded colors, timers through ctx. The delegation
logic runs under node against stubbed SDK modules.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "desktop" / "plugin.js"
SOURCE = PLUGIN.read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_only_the_three_allowed_specifiers_are_imported():
    specifiers = set(re.findall(r"""from\s+['"]([^'"]+)['"]""", SOURCE))
    assert specifiers <= {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    assert "import(" not in SOURCE


def test_the_module_and_the_page_share_one_set_of_orb_rules():
    src = (Path(__file__).parent.parent / "desktop" / "plugin.js").read_text()
    assert src.count("ORB_CSS") == 3  # one definition, one use by the page, one by the module
    assert "--hue:var(--ui-accent" in src


def test_no_hardcoded_colors_and_no_bare_timers():
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", SOURCE)
    assert not re.search(r"(?<![.\w])(setInterval|setTimeout)\(", SOURCE)
    assert "window.addEventListener" not in SOURCE


def test_the_desktop_half_never_holds_a_google_url_or_key():
    assert "googleapis" not in SOURCE and "GEMINI_API_KEY" not in SOURCE


def test_every_voice_trait_of_every_service_is_translated_in_the_three_languages():
    from talk_with_hermes import services

    traits = {trait for service in services.SERVICES.values() for _, trait in service.voices if trait}
    for trait in sorted(traits):
        assert SOURCE.count(f"'trait.{trait}':") == 3, trait  # fr, en, es


def test_the_desktop_half_holds_no_voice_service_key_or_url():
    for marker in ("OPENAI_API_KEY", "XAI_API_KEY", "api.openai.com", "api.x.ai"):
        assert marker not in SOURCE, marker


def test_id_matches_the_package_and_ships_opt_in():
    assert re.search(r"export const ID = 'talk-with-hermes'", SOURCE)
    assert "defaultEnabled: false" in SOURCE


def test_no_em_dash_in_kavyro_text():
    sources = [PLUGIN, *ROOT.glob("talk_with_hermes/*.py"), *ROOT.glob("dashboard/*.py"),
               *ROOT.glob("dashboard/*.json"), ROOT / "plugin.yaml"]
    for path in sources:
        assert "\u2014" not in path.read_text(encoding="utf-8"), path


SDK_STUB = """
export const atom = v => { let x = v; return { get: () => x, set: n => { x = n } } }
export const host = {}
export const ROUTES_AREA = 'routes', SIDEBAR_NAV_AREA = 'sidebar.nav', PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const Button = 'button', Input = 'input', SegmentedControl = 'div'
export const useValue = a => a.get()
export const pluginSettingsHref = id => `/settings?plugin=${id}`
export const usePluginI18n = () => (key, ...a) => key
"""
JSX_STUB = "export const jsx = () => null, jsxs = () => null\n"
REACT_STUB = "export const useEffect = () => {}, useRef = () => ({ current: null }), useState = v => [typeof v === 'function' ? v() : v, () => {}]\n"

JS_TESTS = r"""
import test from 'node:test'
globalThis.window ??= { matchMedia: () => ({ matches: false }) }
globalThis.document ??= { documentElement: {} }
globalThis.getComputedStyle ??= () => ({ getPropertyValue: () => '' })
import assert from 'node:assert/strict'
import { createDelegations, createPoller, floatToPcm16, pcm16ToBase64, base64ToFloat, fixWords, toolLabel, trackSteps, shapeDistance, cellLevel, heightField, cellRelief, sphereProject, paint, toolKind, sideOf, formatMs, createProfileRequest, callState, wrapLines, createOrbPainter, planChunk, playLead, makeOrb, ORB_CSS, edgeOf, MODULE, mountModule, lastCallOf, readLastCall, BUNDLES, nest, keyPath, T } from './plugin.js'

function harness({ failSubmit = 0, timing, now } = {}) {
  const calls = [], answers = [], listeners = {}, timers = [], notes = []
  let sessions = 0
  const d = createDelegations({
    request: async (method, params) => {
      calls.push([method, params])
      if (method === 'session.create') return { session_id: `s${++sessions}` }
      if (method === 'session.resume') return { session_id: `r${++sessions}` }
      if (method === 'prompt.submit' && failSubmit-- > 0) throw new Error('session not found')
      return {}
    },
    onEvent: (type, fn) => { listeners[type] = fn },
    answer: async body => { answers.push(body) },
    log: () => {},
    setTimer: (fn, ms) => { timers.push(fn) },
    context: () => 'Utilisateur : bonjour',
    note: text => notes.push(text),
    ...(timing ? { timing } : {}), ...(now ? { now } : {}),
  })
  const complete = (sid, payload) => listeners['message.complete']({ session_id: sid, payload })
  const flush = () => new Promise(r => setTimeout(r, 0))
  return { d, calls, answers, complete, flush, timers, notes }
}

test('the transcript shows what the agent was asked and what it answered', async () => {
  const h = harness()
  h.d.enqueue({ id: 'c1', request: 'fichiers kavyro ?', prompt: 'NOTE\n\nfichiers kavyro ?' })
  await h.flush()
  assert.deepEqual(h.notes, ['je cherche : fichiers kavyro ?'])
  h.complete('s1', { status: 'complete', text: '94143 fichiers.' })
  await h.flush(); await h.flush()
  assert.equal(h.notes.length, 2)
  assert.match(h.notes[1], /^répondu en \d+,\d s : 94143 fichiers\.$/)
})

test('a failed delegation says so in the transcript', async () => {
  const h = harness()
  h.d.enqueue({ id: 'c1', request: 'météo ?', prompt: 'météo ?' })
  await h.flush()
  h.complete('s1', { status: 'error', text: 'provider down' })
  await h.flush(); await h.flush()
  assert.equal(h.notes.at(-1), 'échec : provider down')
})

test('a delegation runs on a voice-live session and its answer goes back by id', async () => {
  const h = harness()
  h.d.enqueue({ id: 'c1', request: 'ventes ?', prompt: 'NOTE\n\nventes ?' })
  await h.flush()
  const submit = h.calls.find(c => c[0] === 'prompt.submit')[1]
  assert.deepEqual(submit, { session_id: 's1', text: 'NOTE\n\nventes ?', surface: 'voice-live', voice_context: 'Utilisateur : bonjour' })
  assert.equal(h.d.busy, true)
  h.complete('other', { text: 'pas moi' })
  h.complete('s1', { text: 'Trois ventes.', status: 'complete' })
  await h.flush()
  assert.deepEqual(h.answers, [{ id: 'c1', answer: 'Trois ventes.' }])
  assert.equal(h.d.busy, false)
})

test('each answer goes back to the request that asked for it, in any order', async () => {
  const h = harness()
  h.d.enqueue({ id: 'a', prompt: 'A' })
  h.d.enqueue({ id: 'b', prompt: 'B' })
  await h.flush(); await h.flush()
  h.complete('s2', { text: 'rb' }) // the second one finishes first
  await h.flush(); await h.flush()
  assert.deepEqual(h.answers, [{ id: 'b', answer: 'rb' }])
  h.complete('s1', { text: 'ra' })
  await h.flush(); await h.flush()
  assert.deepEqual(h.answers.map(a => a.id), ['b', 'a'])
  assert.ok(!h.calls.some(c => c[0] === 'session.interrupt'))
})

test('a lost session is recreated once and the request still goes through', async () => {
  const h = harness({ failSubmit: 1 })
  h.d.enqueue({ id: 'c1', prompt: 'P' })
  await h.flush(); await h.flush()
  assert.equal(h.calls.filter(c => c[0] === 'session.create').length, 2)
  h.complete('s2', { text: 'ok' })
  await h.flush()
  assert.deepEqual(h.answers, [{ id: 'c1', answer: 'ok' }])
})

test('an agent error is reported as an error, not spoken as an answer', async () => {
  const h = harness()
  h.d.enqueue({ id: 'c1', prompt: 'P' })
  await h.flush()
  h.complete('s1', { text: 'Error: provider down', status: 'error' })
  await h.flush()
  assert.deepEqual(h.answers, [{ id: 'c1', error: 'Error: provider down' }])
})

test('a cancellation interrupts the running agent turn and sends no answer', async () => {
  const h = harness()
  h.d.enqueue({ id: 'c1', prompt: 'P' })
  await h.flush()
  h.d.cancel('c1')
  await h.flush()
  assert.deepEqual(h.calls.at(-1), ['session.interrupt', { session_id: 's1' }])
  h.complete('s1', { text: 'trop tard' })
  await h.flush()
  assert.deepEqual(h.answers, [])
  assert.equal(h.d.busy, false)
})

test('a request cancelled before it starts is never submitted', async () => {
  const h = harness()
  h.d.link('stored-1', 'live-1') // one turn at a time: b is still waiting
  h.d.enqueue({ id: 'a', prompt: 'A' })
  h.d.enqueue({ id: 'b', prompt: 'B' })
  await h.flush()
  h.d.cancel('b')
  h.complete('live-1', { text: 'ra' })
  await h.flush(); await h.flush(); await h.flush()
  assert.deepEqual(h.calls.filter(c => c[0] === 'prompt.submit').map(c => c[1].text), ['A'])
  assert.deepEqual(h.answers, [{ id: 'a', answer: 'ra' }])
})

test('cancelling a running request stops its own turn, not the others', async () => {
  const h = harness()
  h.d.enqueue({ id: 'a', prompt: 'A' })
  h.d.enqueue({ id: 'b', prompt: 'B' })
  await h.flush(); await h.flush()
  h.d.cancel('b')
  await h.flush()
  assert.deepEqual(h.calls.filter(c => c[0] === 'session.interrupt').map(c => c[1].session_id), ['s2'])
  h.complete('s1', { text: 'ra' })
  await h.flush(); await h.flush()
  assert.deepEqual(h.answers, [{ id: 'a', answer: 'ra' }])
})

test('a silent agent ends in a timeout error', async () => {
  const h = harness()
  h.d.enqueue({ id: 'c1', prompt: 'P' })
  await h.flush()
  h.timers[0]()
  await h.flush()
  assert.equal(h.answers[0].id, 'c1')
  assert.ok(h.answers[0].error)
})

test('overlapping polls over a slow link handle each event once', async () => {
  const handled = [], asked = []
  let release
  const gate = new Promise(r => { release = r })
  const feed = [{ seq: 1, kind: 'out', text: 'Salut' }, { seq: 2, kind: 'audio', data: 'AA' }]
  const poller = createPoller({
    fetch: async after => { asked.push(after); await gate; return { next: 2, events: feed.filter(e => e.seq > after) } },
    handle: e => handled.push(e.seq),
    onSummary: () => {},
  })
  const a = poller.tick(), b = poller.tick(), c = poller.tick() // the interval fires while the first poll is slow
  release()
  await Promise.all([a, b, c])
  assert.deepEqual(asked, [0])
  assert.deepEqual(handled, [1, 2])
  await poller.tick()
  assert.deepEqual(asked, [0, 2])
  assert.deepEqual(handled, [1, 2])
})

test('an event already handled is never handled again, even if the backend resends it', async () => {
  const handled = []
  const replies = [
    { next: 2, events: [{ seq: 1 }, { seq: 2 }] },
    { next: 3, events: [{ seq: 2 }, { seq: 3 }] },
  ]
  const poller = createPoller({ fetch: async () => replies.shift(), handle: e => handled.push(e.seq), onSummary: () => {} })
  await poller.tick(); await poller.tick()
  assert.deepEqual(handled, [1, 2, 3])
  poller.reset()
})

test('the plugin adds a Talk page, its sidebar row and a status bar chip, nothing in the composer', async () => {
  const plugin = (await import('./plugin.js')).default
  const added = []
  globalThis.document = { createElement: () => ({ dataset: {}, remove() {} }), head: { append() {} } }
  plugin.register({
    onDispose() {}, onEvent() {}, rest: async () => ({}), setInterval: () => () => {}, setTimeout() {},
    register: c => added.push(c), registerMany: cs => added.push(...cs),
  })
  const areas = added.map(c => c.area).sort()
  assert.deepEqual(areas, ['palette', 'routes', 'sidebar.nav', 'statusBar.right'])
  const route = added.find(c => c.area === 'routes'), nav = added.find(c => c.area === 'sidebar.nav')
  assert.equal(route.data.path, nav.data.path)
  assert.equal(nav.data.codicon, 'mic')
  assert.equal(nav.data.label, 'Parler')
  assert.equal(route.title, 'Parler')
})

test('the dictionary fixes whole words only, like the backend', () => {
  const words = [{ word: 'Kavyro', heard: ['Caviro', 'Cavaro'] }, { word: 'FluentCart', heard: ['fluent cart'] }]
  assert.equal(fixWords('mon dossier caviro, et Cavaro ?', words), 'mon dossier Kavyro, et Kavyro ?')
  assert.equal(fixWords('Caviros reste', words), 'Caviros reste')
  assert.equal(fixWords('ouvre fluent cart', words), 'ouvre FluentCart')
  assert.equal(fixWords('rien', []), 'rien')
})

test('agent steps: one per tool, done on complete, a few kept', () => {
  let s = []
  s = trackSteps(s, { phase: 'start', id: 't1', name: 'terminal', context: 'find /opt' })
  s = trackSteps(s, { phase: 'start', id: 't2', name: 'web_search', context: 'météo' })
  s = trackSteps(s, { phase: 'complete', id: 't1' })
  assert.deepEqual(s.map(x => [x.label, x.done]), [['Terminal', true], ['Web', false]])
  for (let i = 0; i < 9; i++) s = trackSteps(s, { phase: 'start', id: `x${i}`, name: 'read_file' })
  assert.equal(s.length, 6) // three a side
  assert.equal(toolLabel('mcp__todoist__find_tasks'), 'Todoist')
  assert.equal(toolLabel('mcp__airtable__list_bases'), 'list bases')
})

test('tool events reach the page only for the voice session', async () => {
  const listeners = {}, seen = []
  const d = createDelegations({
    request: async m => m === 'session.create' ? { session_id: 's1' } : {},
    onEvent: (type, fn) => { listeners[type] = fn },
    answer: async () => {}, log: () => {}, setTimer: () => {}, context: () => '',
    tool: ev => seen.push(ev),
  })
  listeners['tool.start']({ session_id: 's1', payload: { tool_id: 'a', name: 'terminal' } }) // before the session exists
  d.enqueue({ id: 'c1', request: 'x', prompt: 'x' })
  await new Promise(r => setTimeout(r, 0))
  listeners['tool.start']({ session_id: 'other', payload: { tool_id: 'b', name: 'terminal' } })
  listeners['tool.start']({ session_id: 's1', payload: { tool_id: 'c', name: 'terminal', context: 'ls' } })
  listeners['tool.complete']({ session_id: 's1', payload: { tool_id: 'c', name: 'terminal' } })
  assert.deepEqual(seen.map(e => [e.phase, e.id]), [['start', 'c'], ['complete', 'c']])
  d.reset()
})

test('every shape is centred and closed', () => {
  for (const shape of ['circle', 'square', 'oval', 'triangle']) {
    assert.ok(shapeDistance(shape, 0, 0.1) < 0.6, shape)
    assert.ok(shapeDistance(shape, 0.99, 0.99) > 1, shape)
  }
  assert.ok(shapeDistance('square', 0.8, 0.8) <= 1 && shapeDistance('circle', 0.8, 0.8) > 1)
  assert.ok(shapeDistance('oval', 0, 0.8) > 1 && shapeDistance('oval', 0.9, 0) <= 1)
  assert.ok(shapeDistance('triangle', 0, -0.8) <= 1 && shapeDistance('triangle', 0.8, -0.8) > 1)
})

test('cells stay in range and come alive with the voice', () => {
  for (const state of ['idle', 'connecting', 'listening', 'speaking', 'thinking', 'error']) {
    for (let k = 0; k < 50; k++) {
      const a = cellLevel(state, k / 50, k, k, 2 * k, k / 7, (k % 10) / 10, k % 3 ? -1 : 0.3)
      assert.ok(a >= 0 && a <= 1, state)
    }
  }
  const quiet = cellLevel('speaking', 0.3, 0, 3, 4, 1, 0, -1)
  const loud = cellLevel('speaking', 0.3, 0, 3, 4, 1, 0.8, -1)
  assert.ok(loud > quiet + 0.3)
  // an agent step sends a ring outward
  assert.ok(cellLevel('thinking', 0.33, 3, 1, 1, 0, 0, 0.3) > cellLevel('thinking', 0.33, 3, 1, 1, 0, 0, -1))
})

test('two requests run side by side, each in its own session', async () => {
  const h = harness()
  for (const id of ['c1', 'c2', 'c3']) h.d.enqueue({ id, request: id, prompt: `P-${id}` })
  await h.flush(); await h.flush()
  const creates = h.calls.filter(c => c[0] === 'session.create').length
  const submits = h.calls.filter(c => c[0] === 'prompt.submit').map(c => [c[1].text, c[1].session_id])
  assert.equal(creates, 3)
  assert.deepEqual(submits, [['P-c1', 's1'], ['P-c2', 's2'], ['P-c3', 's3']]) // all three went out, no queue
  h.complete('s2', { status: 'complete', text: 'deux' })
  await h.flush(); await h.flush()
  assert.deepEqual(h.answers, [{ id: 'c2', answer: 'deux' }]) // answered on its own, first
})

test('hanging up stops every turn in flight and nothing else runs', async () => {
  const h = harness()
  for (const id of ['c1', 'c2']) h.d.enqueue({ id, request: id, prompt: `P-${id}` })
  await h.flush(); await h.flush()
  h.d.reset() // Raccrocher
  await h.flush(); await h.flush()
  assert.deepEqual(h.calls.filter(c => c[0] === 'session.interrupt').map(c => c[1].session_id).sort(), ['s1', 's2'])
  assert.deepEqual(h.answers, []) // neither answer was kept
})

test('a linked session keeps one turn at a time: the next request waits its turn', async () => {
  const h = harness()
  h.d.link('stored-42', 'live-7')
  for (const id of ['c1', 'c2']) h.d.enqueue({ id, request: id, prompt: `P-${id}` })
  await h.flush(); await h.flush()
  assert.deepEqual(h.calls.filter(c => c[0] === 'prompt.submit').map(c => c[1].text), ['P-c1'])
  assert.ok(!h.calls.some(c => c[0] === 'session.create')) // the operator's own conversation
  h.complete('live-7', { status: 'complete', text: 'un' })
  await h.flush(); await h.flush(); await h.flush()
  assert.deepEqual(h.calls.filter(c => c[0] === 'prompt.submit').map(c => c[1].text), ['P-c1', 'P-c2'])
})

test('a timed out turn is interrupted and the next request gets a fresh session', async () => {
  const h = harness()
  h.d.enqueue({ id: 'c1', request: 'a', prompt: 'a' })
  await h.flush()
  h.timers[0]() // five minutes later
  await h.flush(); await h.flush()
  assert.ok(h.calls.some(c => c[0] === 'session.interrupt' && c[1].session_id === 's1'))
  h.d.enqueue({ id: 'c2', request: 'b', prompt: 'b' })
  await h.flush()
  h.complete('s1', { status: 'interrupted', text: 'late' }) // the old turn finishing late
  await h.flush()
  const second = h.calls.filter(c => c[0] === 'prompt.submit')[1][1]
  assert.equal(second.session_id, 's2')
  assert.equal(h.answers.length, 1) // only c1's timeout, the late event did not answer c2
  h.complete('s2', { status: 'complete', text: 'bon' })
  await h.flush(); await h.flush()
  assert.deepEqual(h.answers.at(-1), { id: 'c2', answer: 'bon' })
})

test('more than three queued delegations are refused at once', async () => {
  const h = harness()
  for (const id of ['c1', 'c2', 'c3', 'c4']) h.d.enqueue({ id, request: id, prompt: id })
  await h.flush()
  assert.equal(h.answers.length, 1)
  assert.equal(h.answers[0].id, 'c4')
  assert.match(h.answers[0].error, /trop de demandes/)
  h.d.reset()
})

test('a dictionary word with $ signs stays literal', () => {
  assert.equal(fixWords('dis caviro', [{ word: 'US$&x$$y$1', heard: ['caviro'] }]), 'dis US$&x$$y$1')
})

test('the relief: lit from the top left, a sphere that packs tiles at the edge', () => {
  const tl = heightField('circle', -0.5, -0.5), br = heightField('circle', 0.5, 0.5), mid = heightField('circle', 0, 0)
  assert.ok(mid.h > 0.99 && tl.h < mid.h)
  assert.ok(cellRelief(tl.gx, tl.gy, 1).shade > cellRelief(br.gx, br.gy, 1).shade)
  assert.ok(cellRelief(mid.gx, mid.gy, 1).spec < 1)
  const centre = sphereProject(0.05, 0, 0.05), edge = sphereProject(0.9, 0, 0.94)
  assert.ok(centre.fx > edge.fx)           // a tile facing away is narrower
  assert.ok(edge.px <= 0.97 && edge.px > 0.9) // and the edge stays inside the shape
  assert.ok(Math.abs(sphereProject(0.3, 0, 0.31).px) > 0.3) // the centre spreads out
  assert.match(paint(255, 0, 128, 0.5), /^color\(srgb 1\.000 0\.000 0\.502 \/ 0\.500\)$/)
})

test('delegations run on the installed agent profile, not the window one', async () => {
  const calls = [], logs = []
  const host = {
    profileRoutes: async () => [{ profile: 'default', targetProfile: 'default', connectionId: 'vps' },
      { profile: 'iris', targetProfile: 'iris', connectionId: 'other' }, { profile: 'iris', targetProfile: 'iris', connectionId: 'vps' }],
    activeConnectionId: () => 'vps',
    retainProfile: async route => { calls.push(['retain', route.connectionId]); return () => calls.push(['release']) },
    requestProfile: async (route, method, params, timeout, opts) => { calls.push([route.connectionId, route.profile, method, opts.spawnPriority]); return { ok: 1 } },
    request: async method => { calls.push(['active', method]) }
  }
  const rpc = createProfileRequest({ host, profile: () => 'iris', log: m => logs.push(m) })
  await rpc.request('session.create', {})
  await rpc.request('prompt.submit', {})
  rpc.reset()
  assert.deepEqual(calls, [['retain', 'vps'], ['vps', 'iris', 'session.create', 'foreground'], ['vps', 'iris', 'prompt.submit', 'foreground'], ['release']])
  assert.deepEqual(logs, ['agent : profil iris'])
  const missing = createProfileRequest({ host: { ...host, profileRoutes: async () => [] }, profile: () => 'ghost', log: m => logs.push(m) })
  calls.length = 0
  await missing.request('session.create', {})
  assert.deepEqual(calls, [['active', 'session.create']])
  assert.match(logs.at(-1), /introuvable/)
})

test('each delegation is timed and the slow step is named', async () => {
  let clock = 1000
  const timings = []
  const h = harness({ timing: t => timings.push(t), now: () => clock })
  h.d.enqueue({ id: 'c1', request: 'mes tâches lundi', prompt: 'p' })
  await h.flush()
  clock += 4200
  h.complete('s1', { status: 'complete', text: 'trois tâches' })
  await h.flush(); await h.flush()
  assert.deepEqual(timings, [{ id: 'c1', request: 'mes tâches lundi', ms: 4200, ok: true }])
  assert.match(h.notes.at(-1), /^répondu en 4,2 s : trois tâches/)
  let s = trackSteps([], { phase: 'start', id: 't', name: 'terminal' }, 0)
  s = trackSteps(s, { phase: 'complete', id: 't' }, 186000)
  assert.equal(s[0].ms, 186000); assert.equal(s[0].slow, true)
  assert.equal(formatMs(186000), '3 min 06 s'); assert.equal(formatMs(1800), '1,8 s')
})

test('every kind of work gets its own icon and steps alternate sides', () => {
  assert.equal(toolKind('web_search'), 'web'); assert.equal(toolKind('terminal'), 'terminal')
  assert.equal(toolKind('mcp__todoist__find_tasks_by_date'), 'tasks'); assert.equal(toolKind('mcp__airtable__x'), 'plug')
  assert.equal(toolKind('whatever'), 'gear')
  assert.deepEqual([0, 1, 2, 3].map(sideOf), ['right', 'left', 'right', 'left'])
})

test('the floating window knows the state and wraps the reply', () => {
  assert.equal(callState('live', true, false), 'speaking'); assert.equal(callState('live', true, true), 'thinking')
  assert.equal(callState('live', false, false), 'listening'); assert.equal(callState('idle', false, false), 'idle')
  assert.equal(callState('error', false, false), 'error'); assert.equal(callState('connecting', false, false), 'connecting')
  const m = s => s.length * 10
  assert.deepEqual(wrapLines(m, 'un deux trois quatre', 100, 3), ['un deux', 'trois', 'quatre'])
  const cut = wrapLines(m, 'aaaa bbbb cccc dddd eeee ffff', 90, 2)
  assert.equal(cut.length, 2); assert.ok(cut[1].endsWith('…')); assert.ok(cut.every(l => m(l) <= 90))
  assert.deepEqual(wrapLines(m, '', 100, 3), [])
  assert.ok(createOrbPainter('circle').cells > createOrbPainter('triangle').cells)
})

test('audio chunks play back to back and a dry queue is reported', () => {
  assert.deepEqual(planChunk(0, 10), { at: 10.12, gapMs: null }) // first chunk: a lead, not a gap
  // a remote backend is slower than the old fixed 120 ms lead
  assert.ok(playLead(0.17) > 0.12)
  assert.deepEqual(planChunk(0, 10, playLead(0.17)), { at: 10 + playLead(0.17), gapMs: null })
  assert.ok(Math.abs(playLead(0.01) - 0.14) < 1e-9) // a fast local backend keeps a small lead
  assert.ok(playLead(0.5) > playLead(0.17)) // it grows with the measured latency
  assert.equal(playLead(5), 1.2) // bounded: never a two-second wait
  assert.equal(playLead(Number.NaN), playLead(0.17)) // a broken sample keeps a sane lead
  assert.deepEqual(planChunk(10.5, 10.2), { at: 10.5, gapMs: null }) // queued: back to back
  const late = planChunk(10.5, 10.8)
  assert.equal(late.gapMs, 300); assert.ok(Math.abs(late.at - 10.92) < 1e-9)
})

test('the detached module draws the very same orb as the page', () => {
  const drawn = []
  const g = new Proxy({}, { get: (_, k) => k === 'createRadialGradient' || k === 'createLinearGradient'
    ? () => ({ addColorStop() {} }) : (...a) => { drawn.push(k) }, set: () => true })
  const canvas = { width: 0, height: 0, clientWidth: 72, getContext: () => g, ownerDocument: { defaultView: { devicePixelRatio: 2 } } }
  const copied = []
  const probe = { dataset: {}, style: { setProperty: (k, v) => copied.push(k) },
    ownerDocument: { defaultView: { getComputedStyle: () => ({ color: 'rgb(10, 20, 30)' }) } } }
  const at = v => { let x = v; return { get: () => x, set: n => { x = n } } }
  const call = { status: at('live'), speaking: at(true), thinking: at(false), muted: at(false), shape: at('triangle'), level: () => 0.6, pulse: { at: 0 } }
  const orb = makeOrb(canvas, probe, call)
  assert.equal(orb(1000), 'speaking')
  assert.equal(probe.dataset.state, 'speaking') // the state drives the hue, as on the page
  assert.equal(canvas.width, 144) // retina, like the page's orb
  assert.ok(drawn.includes('fillRect'))
  assert.ok(copied.includes('--ui-accent') && copied.includes('--ui-success')) // the theme follows it into the other window
  const tri = drawn.length
  drawn.length = 0
  call.shape.set('circle'); call.speaking.set(false); call.thinking.set(true)
  assert.equal(orb(1100), 'thinking')
  assert.notEqual(drawn.length, tri) // another shape is another set of cells
  call.status.set('idle')
  assert.equal(orb(1200), 'idle')
})

test('a linked session is where the agent works', async () => {
  const h = harness()
  h.d.link('stored-42', 'live-7')
  h.d.enqueue({ id: 'c1', request: 'aide-moi', prompt: 'p' })
  await h.flush()
  assert.deepEqual(h.calls.map(c => [c[0], c[1].session_id]), [['prompt.submit', 'live-7']])
  h.complete('live-7', { status: 'complete', text: 'ok' })
  await h.flush(); await h.flush()
  const g = harness({ failSubmit: 1 }) // the runtime id went stale: resume the same stored session, never a new one
  g.d.link('stored-42', 'gone')
  g.d.enqueue({ id: 'c2', request: 'x', prompt: 'p' })
  await g.flush(); await g.flush()
  assert.deepEqual(g.calls.map(c => c[0]), ['prompt.submit', 'session.resume', 'prompt.submit'])
  assert.equal(g.calls[1][1].session_id, 'stored-42')
  assert.ok(!g.calls.some(c => c[0] === 'session.create'))
})

test('resuming keeps the spoken lines of the last call only', () => {
  const rows = [{ who: 'agent', text: 'je cherche' }, { who: 'toi', text: 'salut' }, { who: 'hermes', text: 'bonjour', hidden: true },
    { who: 'toi', text: '   ' }, ...Array.from({ length: 30 }, (_, i) => ({ who: 'toi', text: `l${i}` }))]
  const v = lastCallOf(rows, 'Iris', 5)
  assert.equal(v.lines.length, 16); assert.equal(v.lines.at(-1).text, 'l29'); assert.equal(v.agent, 'Iris')
  assert.ok(v.lines.every(r => r.who !== 'agent'))
  assert.equal(lastCallOf([{ who: 'agent', text: 'x' }], 'D', 1), null)
  assert.deepEqual(readLastCall(JSON.stringify(v)), v)
  assert.equal(readLastCall('{oops'), null); assert.equal(readLastCall(''), null); assert.equal(readLastCall('{"lines":[]}'), null)
  // a cleared screen still resumes: hidden lines are kept
  assert.equal(lastCallOf([{ who: 'hermes', text: 'caché', hidden: true }], 'D', 1).lines[0].text, 'caché')
})

test('French, English and Spanish carry the same keys, and every one resolves', () => {
  const flat = (tree, p = '') => Object.entries(tree).flatMap(([k, v]) =>
    v && typeof v === 'object' ? flat(v, `${p}${k}.`) : [[`${p}${k}`, v]])
  const keys = Object.fromEntries(Object.entries(BUNDLES).map(([l, b]) => [l, flat(b)]))
  const fr = keys.fr.map(([k]) => k).sort()
  for (const lang of ['en', 'es']) assert.deepEqual(keys[lang].map(([k]) => k).sort(), fr, lang)
  for (const lang of ['fr', 'en', 'es']) {
    for (const [k, v] of keys[lang]) {
      const out = typeof v === 'function' ? v(2, 'x', 'y') : v
      assert.equal(typeof out, 'string', `${lang} ${k}`)
      assert.ok(out.length > 0 && !out.includes('undefined') && !out.includes('\u2014'), `${lang} ${k}: ${out}`)
      if (typeof v === 'function') assert.equal(typeof v.length, 'number')
    }
  }
  // a leaf that also has children is reached through '._'
  assert.equal(keyPath('btn.detach'), 'btn.detach._'); assert.equal(keyPath('btn.clear.title'), 'btn.clear.title')
  assert.equal(BUNDLES.en.btn.detach._, 'Pop out'); assert.equal(BUNDLES.es.btn.detach.title.length > 0, true)
  assert.deepEqual(nest({ 'a.b': 1, 'a.b.c': 2, d: 3 }), { a: { b: { _: 1, c: 2 } }, d: 3 })
  // before registration the module translator answers in French
  assert.equal(T('btn.hangup'), 'Raccrocher'); assert.equal(T('log.closed', ''), 'fin : coupure')
  assert.equal(formatMs(1500), '1,5 s')
})

test('the page has no French left outside the catalog', async () => {
  const { readFileSync } = await import('node:fs')
  const src = readFileSync(new URL('./plugin.js', import.meta.url), 'utf8')
  const code = src.slice(src.indexOf('export function nest'))
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '').replace(/\s\/\/ .*$/gm, '')
  const strings = code.match(/(['"`])(?:(?!\1)[^\\\n]|\\.)*\1/g) || []
  // accents, or a common French word the accent check would miss ('Couper le micro')
  const french = /[àâçéèêîôûœ]|\b(le|la|les|du|des|une|pour|avec|sans|dans|est|pas|tu|ton|micro|appel|agent :|session \w|cherche|couper|rouvrir|ouvrir|fermer|aucune?)\b/i
  const left = strings.filter(s => french.test(s) && !/^['"`](?:[\w.-]+|twh-[\w -]+)['"`]$/.test(s))
  // allowed: the plugin description (manifest, read before any locale), the
  // keywords, and the errors sent back to the voice model, which rephrases them
  const allowed = [/Parle à ton agent en direct/, /n'a pas répondu à temps/, /trop de demandes en attente/]
  assert.deepEqual(left.filter(s => !allowed.some(re => re.test(s))), [])
})

test('pcm round trip keeps the samples', () => {
  const { pcm, peak } = floatToPcm16(new Float32Array([0, 0.5, -0.5, 1, -1, 2]))
  assert.equal(peak, 1)
  const back = base64ToFloat(pcm16ToBase64(pcm))
  assert.equal(back.length, 6)
  for (const [i, v] of [0, 0.5, -0.5, 1, -1, 1].entries()) assert.ok(Math.abs(back[i] - v) < 1e-3, `${i}`)
})
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_desktop_logic_under_node(tmp_path):
    assert NODE is not None
    shutil.copy(PLUGIN, tmp_path / "plugin.js")
    sdk = tmp_path / "node_modules" / "@hermes" / "plugin-sdk"
    jsx = tmp_path / "node_modules" / "react"
    sdk.mkdir(parents=True)
    jsx.mkdir(parents=True)
    (sdk / "package.json").write_text(json.dumps({"name": "@hermes/plugin-sdk", "type": "module", "main": "index.js"}))
    (sdk / "index.js").write_text(SDK_STUB)
    (jsx / "package.json").write_text(json.dumps({"name": "react", "type": "module", "exports": {".": "./index.js", "./jsx-runtime": "./jsx-runtime.js"}}))
    (jsx / "index.js").write_text(REACT_STUB)
    (jsx / "jsx-runtime.js").write_text(JSX_STUB)
    (tmp_path / "package.json").write_text(json.dumps({"type": "module"}))
    (tmp_path / "plugin.test.js").write_text(JS_TESTS)
    run = subprocess.run([NODE, "--test", "plugin.test.js"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
