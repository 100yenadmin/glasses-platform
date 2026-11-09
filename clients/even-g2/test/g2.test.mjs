import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createG2, label63} from '../src/g2.mjs';
class Socket {
  static last;
  constructor() { Socket.last = this; this.readyState = 1; this.sent = []; }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  frame(frame) { this.onmessage({data: JSON.stringify(frame)}); }
  close() { this.onclose({code: 1000}); }
}
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
test('G2 PCM, cumulative stream, deferred push, choices and secret-free state logs', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const logs = [], views = [], mic = [], pages = [], writes = [];
  t.mock.method(console, 'log', line => logs.push(line));
  const app = createG2({audioControl: async enabled => { mic.push(enabled); return true; },
    textContainerUpgrade: async value => writes.push(value), rebuildPageContainer: async page => pages.push(page)},
    {containerID: 1, containerName: 'transcript', content: 'settings'}, (state, text) => views.push([state, text]), Socket);
  t.after(() => app.close());
  const connected = app.connect('ws://fixture/voice', 'PRIVATE_FIXTURE_TOKEN');
  const socket = Socket.last;
  socket.frame({type: 'welcome'}); socket.frame({type: 'hello_ack'}); await connected;
  assert.deepEqual(socket.sent[0].device.capabilities, {display: true, display_chars: 240, buttons: true, mic: true});
  await app.tap(); app.pcm(new Uint8Array([1, 2, 3, 4])); await app.tap();
  assert.deepEqual(mic, [true, false]);
  const query = socket.sent.at(-1);
  assert.deepEqual(query.audio, {mime: 'audio/pcm', rate: 16000, data: 'AQIDBA=='});
  socket.frame({type: 'transcript', turn: query.id, text: 'Hello'});
  socket.frame({type: 'response_delta', turn: query.id, text: 'Hi'});
  socket.frame({type: 'response_delta', turn: query.id, text: 'Hi there'});
  assert.equal(views.at(-1)[1], 'Hello\nHi there');
  t.mock.timers.tick(120); await flush(); assert.equal(writes.at(-1).content, 'Hello\nHi there');
  socket.frame({type: 'push', origin: 'cron', text: 'Stretch'});
  assert.equal(views.at(-1)[0], 'answer');
  socket.frame({type: 'response', turn: query.id, text: 'Final'}); await flush();
  t.mock.timers.tick(1500); await flush(); assert.deepEqual(views.at(-1), ['push', 'Reminder\nStretch']);
  const long = '界'.repeat(30);
  socket.frame({type: 'approval_request', id: 'a', choices: ['once', long]}); await flush();
  assert.equal(Buffer.byteLength(pages.at(-1).listObject[0].itemContainer.itemName[1]), 63);
  app.select(1); const choice = app.tap(); await flush();
  assert.deepEqual(socket.sent.at(-1), {type: 'approval_response', id: 'a', choice: long});
  socket.frame({type: 'approval_result', id: 'a', ok: true}); await choice;
  t.mock.timers.tick(120); await flush(); assert.ok(pages.at(-1).textObject);
  socket.frame({type: 'approval_request', id: 'b', choices: ['once', 'deny']}); await flush();
  socket.frame({type: 'approval_closed', id: 'b'}); t.mock.timers.tick(120); await flush();
  assert.ok(pages.at(-1).textObject);
  await app.tap();
  socket.frame({type: 'approval_request', id: 'during-mic', choices: ['once', 'deny']}); await flush();
  assert.notEqual(views.at(-1)[0], 'approval');
  t.mock.timers.tick(8000); await flush(); assert.equal(mic.at(-1), false);
  assert.equal(socket.sent.at(-1).audio.rate, 16000);
  assert.ok(logs.length); assert.ok(logs.every(line => /^\[hermes\] (listening|thinking|answer|push|approval|error)$/.test(line)));
  assert.ok(!logs.join('').includes('PRIVATE_FIXTURE_TOKEN'));
  assert.equal(label63('😀'.repeat(20)), '😀'.repeat(15));
});
test('a rejected mic start does not send audio', async t => {
  const app = createG2({audioControl: async () => false, textContainerUpgrade: async () => {}}, {}, () => {}, Socket);
  t.after(() => app.close());
  const connected = app.connect('ws://fixture/voice', 'fixture');
  Socket.last.frame({type: 'hello_ack'}); await connected;
  await app.tap(); assert.equal(Socket.last.sent.length, 0);
});
