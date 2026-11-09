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
  const client = new HermesGlassesClient({url: 'ws://localhost/voice?extra=1', token: 'fixture & token',
    device: {name: 'test', capabilities: {}}, reconnect: false, WebSocketImpl: Socket, ...options});
  const connected = client.connect();
  const socket = Socket.instances.at(-1);
  socket.frame({type: 'welcome', protocol_max: 1});
  socket.frame({type: 'hello_ack', protocol: 1, user: 'fixture', device: 'test',
    server: {version: 'fixture', features: []}});
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
  socket.frame({type: 'response', turn: 'two', text: 'second', final: true});
  socket.frame({type: 'response', turn: 'one', text: 'first', final: true});
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

test('uncorrelated errors reject queries and reset; repeated reset shares one request', async () => {
  const {client, socket} = setup();
  const query = assert.rejects(client.ask({id: 'one'}), /empty/);
  const resetPromise = client.newSession();
  assert.equal(resetPromise, client.newSession());
  const reset = assert.rejects(resetPromise, /empty/);
  socket.frame({type: 'error', message: 'empty'});
  await Promise.all([query, reset]);
  client.interrupt('stop');
  assert.deepEqual(socket.sent.at(-1), {type: 'interrupt', id: 'stop'});
  client.disconnect();
});

test('audio timeout returns text, speak false skips wait, push audio is emitted', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {client, socket} = setup({device: {name: 'audio', capabilities: {audio_out: ['audio/wav']}}});
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
  assert.equal(await http.turn({text: 'hello'}, {textPlain: true}), 'plain');
  assert.equal(calls.at(-1).headers.Accept, 'text/plain');
  await http.pending(0);
  assert.equal(calls.at(-1).url, 'http://localhost/pending?wait=0');
  assert.equal(calls.at(-1).method, 'GET');
  await http.approval('a', 'deny');
  assert.equal(calls.at(-1).body, '{"id":"a","choice":"deny"}');
  status = 401;
  await assert.rejects(http.pending(), /HTTP 401/);
});
