import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createInterface} from 'node:readline';
import {HermesGlassesClient, HermesGlassesHttp} from '../hermes-glasses.js';

class Socket {
  static instances = [];
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; Socket.instances.push(this); }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  frame(frame) { this.onmessage({data: JSON.stringify(frame)}); }
  close(code = 1000) { this.readyState = 3; this.onclose({code}); }
}
function setup(options = {}) {
  const {serverFeatures = [], ...clientOptions} = options;
  const client = new HermesGlassesClient({url: 'ws://localhost/voice?extra=1', token: 'fixture & token',
    device: {name: 'test', capabilities: {}}, reconnect: false, WebSocketImpl: Socket, ...clientOptions});
  const connected = client.connect();
  const socket = Socket.instances.at(-1);
  socket.frame({type: 'welcome', protocol_max: 1});
  socket.frame({type: 'hello_ack', protocol: 1, user: 'fixture', device: 'test',
    server: {version: 'fixture', features: serverFeatures}});
  return {client, socket, connected};
}

test('scripted Python server: hello, ack, photo, approval, transcript, query and audio', {timeout: 15000}, async t => {
  const child = spawn(process.env.PYTHON || 'python3', ['-m', 'conformance', 'serve', '--port', '0',
    '--token', 'fixture', '--json'], {cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe']});
  let errors = '';
  child.stderr.on('data', data => { errors += data; });
  t.after(async () => { child.kill(); if (child.exitCode === null) await once(child, 'exit'); });
  const lines = createInterface({input: child.stdout});
  const iterator = lines[Symbol.asyncIterator]();
  const ready = await iterator.next();
  assert.match(ready.value || errors, /^READY \d+$/);
  const events = [];
  let approval;
  const client = new HermesGlassesClient({url: `ws://127.0.0.1:${ready.value.split(' ')[1]}/voice`, token: 'fixture',
    reconnect: false, device: {name: 'node', capabilities: {ack: true, camera: true, buttons: true,
      display: true, haptics: true, audio_out: ['audio/wav']}},
    onCapturePhoto: async () => '/9j/2Q=='});
  t.after(() => client.disconnect());
  for (const name of ['push', 'display', 'haptic', 'delta', 'transcript', 'audio', 'approval_closed'])
    client.on(name, frame => events.push([name, frame]));
  client.on('approval_request', frame => { approval = client.answerApproval(frame.id, 'once'); });
  const hello = await client.connect();
  assert.equal(hello.user, 'fixture');
  await client.newSession();
  const result = await client.ask({id: 'voice', audio: {data: 'AA==', mime: 'audio/wav'}});
  assert.equal(result.id, 'voice');
  assert.equal(result.text, 'Fixture reply');
  assert.equal(result.transcript, 'Fixture transcript');
  const wav = Buffer.from(result.audio.data, 'base64');
  assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
  assert.equal((await approval).ok, true);
  const report = JSON.parse((await iterator.next()).value);
  assert.equal(report.ok, true, JSON.stringify(report));
  assert.deepEqual(report.checks.map(check => check.check).sort(), ['ack', 'approval_response', 'hello', 'photo', 'query']);
  for (const name of ['push', 'display', 'haptic', 'delta', 'transcript', 'audio', 'approval_closed'])
    assert.ok(events.some(([event]) => event === name), name);
});

test('URL encoding, first-frame hello, correlation, cumulative delta and unknown fields', async () => {
  const {client, socket, connected} = setup();
  assert.equal((await connected).protocol, 1);
  assert.equal(new URL(socket.url).searchParams.get('token'), 'fixture & token');
  assert.equal(new URL(socket.url).searchParams.get('extra'), '1');
  assert.equal(socket.sent[0].type, 'hello');
  const seen = [];
  const off = client.on('delta', frame => seen.push(frame));
  const one = client.ask({text: 'one', id: 'one'});
  const two = client.ask({text: 'two', id: 'two'});
  socket.frame({type: 'response', turn: 'stray', text: 'stray'});
  socket.frame({type: 'future'});
  socket.onmessage({data: '[]'});
  socket.onmessage({data: '{'});
  socket.frame({type: 'response_delta', turn: 'two', text: 'cumulative', future: true});
  socket.frame({type: 'response', turn: 'one', text: 'first', final: true});
  socket.frame({type: 'response', turn: 'two', text: 'second', final: true});
  assert.deepEqual(await one, {id: 'one', text: 'first'});
  assert.deepEqual(await two, {id: 'two', text: 'second'});
  assert.equal(seen[0].id, 'two');
  off(); client.disconnect();
});

test('errors and disconnect reject operations, duplicate ids do not overwrite', async () => {
  const {client, socket} = setup();
  const first = assert.rejects(client.ask({id: 'one'}), /oops/);
  await assert.rejects(client.ask({id: 'one'}), /unique/);
  socket.frame({type: 'error', turn: 'one', message: 'oops'});
  await first;
  const query = assert.rejects(client.ask({id: 'two'}), /Disconnected/);
  const reset = assert.rejects(client.newSession(), /Disconnected/);
  const approval = assert.rejects(client.answerApproval('a', 'once'), /Disconnected/);
  client.disconnect();
  await Promise.all([query, reset, approval]);
  await assert.rejects(client.ask({text: 'offline'}), /Not connected/);
});

test('a query error leaves a pending reset; its own failure or an error with nothing in flight rejects it', async () => {
  const {client, socket} = setup();
  const query = assert.rejects(client.ask({id: 'one'}), /empty/);
  const resetPromise = client.newSession();
  assert.equal(resetPromise, client.newSession());
  socket.frame({type: 'error', message: 'empty'});  // the query's error, not the reset's
  await query;
  socket.frame({type: 'session_reset'});
  await resetPromise;
  assert.equal(client.resetWait, null);
  const reset = assert.rejects(client.newSession(), /reset failed/);
  socket.frame({type: 'error', message: 'reset failed'});  // nothing in flight: it is the reset's
  await reset;
  client.disconnect();
});

test('timeout null means no deadline; interrupt resolves an ask that already has its text', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {client, socket} = setup({serverFeatures: ['audio_out'],
    device: {name: 'audio', capabilities: {audio_out: ['audio/wav']}}});
  let settled = false;
  const open = client.ask({id: 'open', timeout: null}).finally(() => { settled = true; });
  t.mock.timers.tick(1);
  await Promise.resolve();
  assert.equal(settled, false);
  socket.frame({type: 'response', turn: 'open', text: 'kept'});  // now waiting only for audio
  client.interrupt({ask: 'open'});
  assert.equal((await open).text, 'kept');
  const stopId = [...client.interrupts][0];
  socket.frame({type: 'response', turn: stopId, text: 'Stopped.'});
  assert.equal(client.interrupts.size, 0);  // the /stop reply clears its id
  client.disconnect();
});

test('audio timeout returns text, speak false skips wait, push audio is emitted', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {client, socket} = setup({serverFeatures: ['audio_out'],
    device: {name: 'audio', capabilities: {audio_out: ['audio/wav']}}});
  const query = client.ask({id: 'missing'});
  socket.frame({type: 'response', turn: 'missing', text: 'text'});
  let finished = false;
  query.then(() => { finished = true; });
  t.mock.timers.tick(14999); await Promise.resolve();
  assert.equal(finished, false);
  t.mock.timers.tick(1);
  assert.deepEqual(await query, {id: 'missing', text: 'text'});
  const silent = client.ask({id: 'silent', speak: false});
  socket.frame({type: 'response', turn: 'silent', text: 'quiet'});
  assert.equal((await silent).text, 'quiet');
  client.disconnect();
});

test('camera rejection sends photo_error and approval false is a result', async () => {
  const {client, socket} = setup({onCapturePhoto: async () => { throw new Error('private camera detail'); }});
  socket.frame({type: 'capture_photo', request: 'photo', reason: 'tool'});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(socket.sent.at(-1), {type: 'photo_error', request: 'photo', message: 'Camera unavailable.'});
  const approval = client.answerApproval('a', 'deny');
  socket.frame({type: 'approval_result', id: 'a', ok: false, message: 'expired'});
  assert.equal((await approval).ok, false);
  client.disconnect();
});

test('reconnect repeats hello and explicit disconnect stops retry; unauthorized never retries', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {client, socket} = setup({reconnect: true});
  socket.close(1006);
  t.mock.timers.tick(1000);
  const next = Socket.instances.at(-1);
  assert.notEqual(next, socket);
  next.frame({type: 'welcome', protocol_max: 1});
  assert.equal(next.sent[0].type, 'hello');
  next.frame({type: 'hello_ack', user: 'fixture'});
  client.disconnect();
  const count = Socket.instances.length;
  t.mock.timers.tick(30000);
  assert.equal(Socket.instances.length, count);
  const unauthorized = setup({reconnect: true});
  unauthorized.socket.close(4401);
  t.mock.timers.tick(30000);
  assert.equal(Socket.instances.at(-1), unauthorized.socket);
});

test('HTTP: Bearer, plain text, JSON, pending 202, approval and errors', async () => {
  const calls = [];
  let status = 202;
  const http = new HermesGlassesHttp({baseUrl: 'http://localhost/', token: 'fixture',
    fetchImpl: async (url, options) => {
      calls.push({url, ...options});
      return {ok: status < 400, status, json: async () => ({status: 'pending'}), text: async () => 'plain'};
    }});
  assert.deepEqual(await http.turn({text: 'hello'}), {status: 'pending'});
  assert.equal(calls[0].headers.Authorization, 'Bearer fixture');
  assert.equal(calls[0].body, '{"text":"hello"}');
  assert.deepEqual(await http.turn({text: 'hello'}, {textPlain: true}), {status: 'pending', text: 'plain'});
  assert.equal(calls.at(-1).headers.Accept, 'text/plain');
  status = 200;
  assert.deepEqual(await http.turn({text: 'hello'}, {textPlain: true}), {status: 'done', text: 'plain'});
  await http.pending(0);
  assert.equal(calls.at(-1).url, 'http://localhost/pending?wait=0');
  assert.equal(calls.at(-1).method, 'GET');
  await http.approval('a', 'deny');
  assert.equal(calls.at(-1).body, '{"id":"a","choice":"deny"}');
  status = 401;
  await assert.rejects(http.pending(), /HTTP 401/);
});

test('approval yes replies before the running ask without settling it', async t => {
  const {client, socket} = setup();
  t.after(() => client.disconnect());
  const first = client.ask({id: 'first', text: 'run command'}).catch(error => error);
  socket.frame({type: 'push', id: 'approval', origin: 'approval', text: 'Allow?'});
  const yes = client.ask({id: 'yes', text: 'yes'});
  socket.frame({type: 'response', turn: 'yes', text: 'Okay, going ahead.'});
  assert.equal((await yes).text, 'Okay, going ahead.');
  assert.ok(client.queries.has('first'));
  socket.frame({type: 'response', turn: 'first', text: 'Command completed.'});
  assert.deepEqual(await first, {id: 'first', text: 'Command completed.'});
});

test('audio ask and later text ask resolve their own out-of-order replies', async t => {
  const {client, socket} = setup();
  t.after(() => client.disconnect());
  const audio = client.ask({id: 'audio', audio: {data: 'AA==', mime: 'audio/wav'}}).catch(error => error);
  const text = client.ask({id: 'text', text: 'later'});
  socket.frame({type: 'response', turn: 'text', text: 'Text reply'});
  assert.deepEqual(await text, {id: 'text', text: 'Text reply'});
  socket.frame({type: 'transcript', turn: 'audio', text: 'spoken'});
  socket.frame({type: 'response', turn: 'audio', text: 'Audio reply'});
  assert.deepEqual(await audio, {id: 'audio', text: 'Audio reply', transcript: 'spoken'});
});

test('interrupt rejects the stopped ask and removes its entry', async t => {
  const {client, socket} = setup();
  t.after(() => client.disconnect());
  for (const explicit of [true, false]) {
    const slash = client.ask({id: 'slash', text: '/status'});
    const answer = client.ask({id: 'q1', text: 'hello'}).catch(error => error);
    const other = client.ask({id: 'other', text: 'later'});
    client.interrupt(explicit ? {ask: 'q1'} : undefined);
    assert.equal(client.queries.size, 2);
    assert.equal((await answer).code, 'interrupted');
    const stop = socket.sent.at(-1);
    assert.equal(stop.type, 'interrupt');
    assert.equal(typeof stop.id, 'string');
    assert.ok(!['q1', 'other', 'slash'].includes(stop.id));
    await assert.rejects(client.ask({id: stop.id}), /unique/);
    socket.frame({type: 'response', turn: stop.id, text: 'Stopped.'});
    assert.equal(client.queries.size, 2);
    socket.frame({type: 'response', turn: 'other', text: 'Own reply'});
    socket.frame({type: 'response', turn: 'slash', text: 'Status'});
    assert.equal((await other).text, 'Own reply');
    await slash;
  }
});

test('default timeout expires folded asks at 180000 ms; zero disables it', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {client, socket} = setup();
  t.after(() => client.disconnect());
  const folded = client.ask({id: 'folded'}).catch(error => error);
  const unlimited = client.ask({id: 'unlimited', timeout: 0});
  t.mock.timers.tick(179999);
  assert.ok(client.queries.has('folded'));
  t.mock.timers.tick(1);
  assert.equal((await folded).code, 'timeout');
  assert.equal(client.queries.has('folded'), false);
  assert.ok(client.queries.has('unlimited'));
  socket.frame({type: 'response', turn: 'unlimited', text: 'Late reply'});
  assert.equal((await unlimited).text, 'Late reply');
});

test('ask timeout and AbortSignal settle and clean up without disconnecting', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {client, socket} = setup();
  t.after(() => client.disconnect());
  const timed = client.ask({id: 'timed', timeout: 20}).catch(error => error);
  t.mock.timers.tick(20);
  assert.equal(client.queries.size, 0);
  assert.equal((await timed).code, 'timeout');
  const controller = new AbortController();
  const aborted = client.ask({id: 'aborted', signal: controller.signal}).catch(error => error);
  controller.abort();
  assert.equal(client.queries.size, 0);
  assert.equal((await aborted).code, 'aborted');
  const sent = socket.sent.length;
  const already = client.ask({signal: controller.signal}).catch(error => error);
  assert.equal(client.queries.size, 0);
  assert.equal((await already).code, 'aborted');
  assert.equal(socket.sent.length, sent);
  const completed = client.ask({id: 'ok', timeout: 20, signal: new AbortController().signal});
  socket.frame({type: 'response', turn: 'ok', text: 'ok'});
  await completed;
  t.mock.timers.tick(20);
  assert.equal(client.ready, true);
});

test('uncorrelated errors resolve text already received and preserve an in-flight reset', async t => {
  const {client, socket} = setup({serverFeatures: ['audio_out'],
    device: {name: 'audio', capabilities: {audio_out: ['audio/wav']}}});
  t.after(() => client.disconnect());
  const completed = client.ask({id: 'text'}).catch(error => error);
  socket.frame({type: 'response', turn: 'text', text: 'kept'});
  const waiting = client.ask({id: 'waiting'}).catch(error => error);
  const reset = client.newSession().catch(error => error);
  socket.frame({type: 'error', message: 'query failed'});
  assert.deepEqual(await completed, {id: 'text', text: 'kept'});
  assert.match((await waiting).message, /query failed/);
  assert.ok(client.resetWait);
  socket.frame({type: 'session_reset'});
  assert.equal((await reset).type, 'session_reset');
});

test('New session failed rejects reset even while query text awaits audio', async t => {
  const {client, socket} = setup({serverFeatures: ['audio_out'],
    device: {name: 'audio', capabilities: {audio_out: ['audio/wav']}}});
  t.after(() => client.disconnect());
  const answer = client.ask({id: 'text'});
  socket.frame({type: 'response', turn: 'text', text: 'kept'});
  const reset = assert.rejects(client.newSession(), /New session failed/);
  socket.frame({type: 'error', message: 'New session failed.'});
  await reset;
  assert.equal(client.resetWait, null);
  assert.equal((await answer).text, 'kept');
});

test('automatic reconnect retries constructor failures with continuing backoff', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {client, socket} = setup({reconnect: true});
  let attempts = 0;
  client.WebSocketImpl = class extends Socket {
    constructor(url) {
      if (++attempts === 1) throw new Error('temporary transport failure');
      super(url);
    }
  };
  t.after(() => client.disconnect());
  socket.close(1006);
  t.mock.timers.tick(1000);
  assert.equal(attempts, 1);
  assert.equal(client.closed, false);
  assert.equal(client.backoff, 4000);
  t.mock.timers.tick(2000);
  assert.equal(attempts, 2);
  const next = Socket.instances.at(-1);
  next.frame({type: 'welcome'});
  next.frame({type: 'hello_ack', server: {features: []}});
  assert.equal(client.ready, true);
  assert.equal(client.backoff, 1000);
});

test('audio wait requires server audio_out feature', async t => {
  const {client, socket} = setup({device: {name: 'audio', capabilities: {audio_out: ['audio/wav']}}});
  t.after(() => client.disconnect());
  const answer = client.ask({id: 'no-audio'});
  socket.frame({type: 'response', turn: 'no-audio', text: 'ready'});
  assert.equal(client.queries.size, 0);
  assert.equal((await answer).text, 'ready');
});

test('camera handler runs before throwing event listeners; invalid camera results send photo_error', async t => {
  for (const result of ['base64', undefined, 123, new Error('camera detail')]) {
    const order = [];
    const {client, socket} = setup({onCapturePhoto: () => {
      order.push('handler');
      if (result instanceof Error) throw result;
      return result;
    }});
    t.after(() => client.disconnect());
    client.on('capture_photo', () => { order.push('event'); throw new Error('listener'); });
    assert.doesNotThrow(() => socket.frame({type: 'capture_photo', request: 'photo', reason: 'tool'}));
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(order, ['handler', 'event']);
    assert.equal(socket.sent.at(-1).type, typeof result === 'string' ? 'photo' : 'photo_error');
  }
});

test('invalid URL rejects and emits once with no reconnect timer', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const client = new HermesGlassesClient({url: 'not a URL', token: 'fixture', reconnect: true,
    device: {name: 'fixture'}, WebSocketImpl: Socket});
  t.after(() => client.disconnect());
  let errors = 0;
  client.on('error', () => { errors++; });
  await assert.rejects(client.connect(), /URL/i);
  assert.equal(errors, 1);
  assert.equal(client.retryTimer, undefined);
  t.mock.timers.tick(60000);
  assert.equal(errors, 1);
});

test('generated ids are unique per client instance; disconnect then connect opens a fresh socket', async () => {
  const first = setup().client, second = setup().client;
  const a = first.nextId(), b = second.nextId();
  assert.match(a, /^q-[a-z0-9]+-\d+$/);
  assert.notEqual(a, b);
  const {client, socket} = setup();
  await client.connect();
  client.disconnect();
  assert.equal(client.ready, false);
  const again = client.connect();
  const fresh = Socket.instances.at(-1);
  assert.notEqual(fresh, socket);
  fresh.frame({type: 'welcome', protocol_max: 1});
  fresh.frame({type: 'hello_ack', protocol: 1, user: 'fixture', device: 'test', server: {version: 'x', features: []}});
  await again;
  assert.equal(client.ready, true);
  for (const c of [first, second, client]) c.disconnect();
});
