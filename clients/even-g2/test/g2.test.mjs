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
  assert.deepEqual(socket.sent[0].device.capabilities, {display: true, display_chars: 400, buttons: true, mic: true});
  await app.tap(); app.pcm(new Uint8Array([1, 2, 3, 4])); await app.tap();
  assert.deepEqual(mic, [true, false]);
  const query = socket.sent.at(-1);
  assert.deepEqual(query.audio, {mime: 'audio/pcm', rate: 16000, data: 'AQIDBA=='});
  socket.frame({type: 'transcript', turn: query.id, text: 'Hello'});
  socket.frame({type: 'response_delta', turn: query.id, text: 'Hi'});
  socket.frame({type: 'response_delta', turn: query.id, text: 'Hi there'});
  assert.equal(views.at(-1)[1], 'Hello\nHi there');
  t.mock.timers.tick(120); await flush(); assert.equal(writes.at(-1).content, 'Hello\nHi there');
  const longAnswer = 'Start ' + 'x'.repeat(500);
  socket.frame({type: 'response_delta', turn: query.id, text: longAnswer});
  assert.equal(views.at(-1)[1].length, 400);
  assert.ok(views.at(-1)[1].startsWith('Hello\nStart ') && views.at(-1)[1].endsWith('…'));
  socket.frame({type: 'push', origin: 'cron', text: 'Stretch'});
  assert.equal(views.at(-1)[0], 'answer');
  socket.frame({type: 'response', turn: query.id, text: 'Final'}); await flush();
  t.mock.timers.tick(1500); await flush(); assert.deepEqual(views.at(-1), ['push', 'Reminder\nStretch']);
  const long = '界'.repeat(30);
  socket.frame({type: 'approval_request', id: 'a', text: 'Hermes wants to run: rm -rf build.', choices: ['once', long]});
  await flush();
  assert.equal(pages.at(-1).textObject[0].content, 'Hermes wants to run: rm -rf build.');  // what is approved stays visible
  assert.equal(pages.at(-1).textObject[0].isEventCapture, 0); assert.equal(pages.at(-1).listObject[0].isEventCapture, 1);
  assert.equal(Buffer.byteLength(pages.at(-1).listObject[0].itemContainer.itemName[1]), 63);
  assert.ok(!('content' in pages.at(-1).listObject[0]));  // the real bridge rejects unknown list fields
  app.select(1); const choice = app.tap(); await flush();
  assert.deepEqual(socket.sent.at(-1), {type: 'approval_response', id: 'a', choice: long});
  socket.frame({type: 'approval_result', id: 'a', ok: true}); await choice;
  t.mock.timers.tick(120); await flush(); assert.ok(pages.at(-1).textObject && !pages.at(-1).listObject);
  socket.frame({type: 'approval_request', id: 'b', choices: ['once', 'deny']}); await flush();
  socket.frame({type: 'approval_closed', id: 'b'}); t.mock.timers.tick(120); await flush();
  assert.ok(pages.at(-1).textObject && !pages.at(-1).listObject);
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
test('a disconnect while recording still turns the mic off and sends nothing', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const mic = [];
  const app = createG2({audioControl: async enabled => { mic.push(enabled); return true; }, textContainerUpgrade: async () => {}},
    {}, () => {}, Socket);
  t.after(() => app.close());
  const connected = app.connect('ws://fixture/voice', 'fixture');
  const socket = Socket.last;
  socket.frame({type: 'hello_ack'}); await connected;
  await app.tap(); app.pcm(new Uint8Array([1, 2]));
  socket.close();
  t.mock.timers.tick(8000); await flush();
  assert.deepEqual(mic, [true, false]);
  assert.ok(!socket.sent.some(frame => frame.type === 'query'));
});
test('leaving the foreground only stops the mic; the app keeps working', async t => {
  const mic = [];
  const app = createG2({audioControl: async enabled => { mic.push(enabled); return true; }, textContainerUpgrade: async () => {}},
    {}, () => {}, Socket);
  t.after(() => app.close());
  const connected = app.connect('ws://fixture/voice', 'fixture');
  const socket = Socket.last;
  socket.frame({type: 'hello_ack'}); await connected;
  await app.tap(); app.pause(); await flush();
  assert.deepEqual(mic, [true, false]);
  await app.tap(); assert.deepEqual(mic, [true, false, true]);  // still connected and listening again
});
test('pushes in a burst each get their turn on the lens; display frames show like pushes', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const views = [];
  const app = createG2({audioControl: async () => true, textContainerUpgrade: async () => {}, rebuildPageContainer: async () => {}},
    {height: 288}, (state, text) => views.push([state, text]), Socket);
  t.after(() => app.close());
  const connected = app.connect('ws://fixture/voice', 'fixture');
  const socket = Socket.last;
  socket.frame({type: 'hello_ack'}); await connected;
  socket.frame({type: 'push', origin: 'cron', text: 'One'});
  socket.frame({type: 'push', origin: 'message', text: 'Two'});
  socket.frame({type: 'display', text: 'Three'});
  const shown = () => views.filter(([state]) => state === 'push').map(([, text]) => text);
  assert.deepEqual(shown(), ['Reminder\nOne']);
  t.mock.timers.tick(4000); assert.deepEqual(shown(), ['Reminder\nOne', 'Hermes\nTwo']);
  t.mock.timers.tick(4000); assert.deepEqual(shown(), ['Reminder\nOne', 'Hermes\nTwo', 'Hermes\nThree']);
});
async function connectedApp(t) {
  t.mock.timers.enable({apis: ['setTimeout']});
  t.mock.method(console, 'log', () => {});
  const views = [], mic = [], pages = [];
  const app = createG2({audioControl: async enabled => { mic.push(enabled); return true; },
    textContainerUpgrade: async () => {}, rebuildPageContainer: async page => pages.push(page)}, {height: 288},
    (state, text) => views.push([state, text]), Socket);
  t.after(() => app.close());
  const connected = app.connect('ws://fixture/voice', 'fixture');
  const socket = Socket.last;
  socket.frame({type: 'hello_ack'}); await connected;
  return {app, socket, views, mic, pages};
}
async function talk(app, bytes) { await app.tap(); app.pcm(new Uint8Array(bytes)); await app.tap(); await flush(); }
test('#44: a clarify question mid-turn shows at once and a tap answers it; the folded reply ends the turn', async t => {
  const {app, socket, views, mic} = await connectedApp(t);
  await talk(app, [1, 2]);
  const opener = socket.sent.at(-1);
  socket.frame({type: 'push', origin: 'cron', text: 'Stretch'});
  socket.frame({type: 'push', origin: 'clarify', text: 'Which door?'});
  assert.deepEqual(views.at(-1), ['push', 'Hermes asks\nWhich door?\nTap to answer']);  // not held behind the turn
  await talk(app, [3, 4]);
  assert.deepEqual(mic, [true, false, true, false]);
  const answer = socket.sent.at(-1);
  assert.notEqual(answer.id, opener.id); assert.equal(answer.audio.data, 'AwQ=');
  // The adapter folds the answer into the running turn: the turn's final reply comes back on the answer's id.
  socket.frame({type: 'response', turn: answer.id, text: 'Opened the front door.'}); await flush();
  assert.ok(views.some(([kind, text]) => kind === 'answer' && text.endsWith('Opened the front door.')));
  assert.ok(!views.some(([kind]) => kind === 'error'));  // the first ask's release shows no error (the tap below proves the turn ended)
  t.mock.timers.tick(1500); await flush();
  assert.deepEqual(views.at(-1), ['push', 'Reminder\nStretch']);  // ordinary pushes still wait for the turn
  t.mock.timers.tick(4000); await flush();
  await talk(app, [5]);  // the turn is over: a tap starts a new turn
  assert.equal(socket.sent.at(-1).audio.data, 'BQ==');
});
test('#44: when the reply comes back on the first ask, the answer ask is released too', async t => {
  const {app, socket, views} = await connectedApp(t);
  await talk(app, [1]);
  const opener = socket.sent.at(-1);
  socket.frame({type: 'push', origin: 'clarify', text: 'Which door?'});
  await talk(app, [2]);
  const answer = socket.sent.at(-1);
  assert.notEqual(answer.id, opener.id); assert.equal(answer.audio.data, 'Ag==');
  socket.frame({type: 'response', turn: opener.id, text: 'Done.'}); await flush();
  socket.frame({type: 'response', turn: answer.id, text: 'late'}); await flush();
  assert.ok(!views.some(([, text]) => text.endsWith('late')));  // released: a late reply to it is ignored
  assert.ok(views.some(([kind, text]) => kind === 'answer' && text.endsWith('Done.')));
  assert.ok(!views.some(([kind]) => kind === 'error'));
  t.mock.timers.tick(1500); await flush();
  await talk(app, [3]);
  assert.equal(socket.sent.at(-1).audio.data, 'Aw==');
});
test('#44: without a question, a tap during a turn still does nothing', async t => {
  const {app, socket, mic} = await connectedApp(t);
  await talk(app, [1]);
  const sent = socket.sent.length;
  await app.tap(); await flush();
  assert.equal(socket.sent.length, sent); assert.deepEqual(mic, [true, false]);
});
test('#44 review: with no local turn, a clarify push is an ordinary push and cannot stick on the lens', async t => {
  const {socket, views} = await connectedApp(t);
  socket.frame({type: 'push', origin: 'clarify', text: 'Which door?'});  // asked by another device's turn, or replayed later
  socket.frame({type: 'push', origin: 'cron', text: 'Stretch'});
  assert.deepEqual(views.at(-1), ['push', 'Hermes\nWhich door?']);
  t.mock.timers.tick(4000); await flush();
  assert.deepEqual(views.at(-1), ['push', 'Reminder\nStretch']);
});
test('#44 review: an approval card takes the lens over a mid-turn question, which comes back after', async t => {
  const {app, socket, views, pages} = await connectedApp(t);
  await talk(app, [1]);
  socket.frame({type: 'push', origin: 'clarify', text: 'Which door?'});
  socket.frame({type: 'approval_request', id: 'a', text: 'Run it?', choices: ['once', 'deny']}); await flush();
  assert.ok(pages.at(-1).listObject);
  socket.frame({type: 'approval_closed', id: 'a'}); await flush();
  assert.deepEqual(views.at(-1), ['push', 'Hermes asks\nWhich door?\nTap to answer']);
});
test('#44 review: the first ask timing out while the answer is on its way does not drop the reply', async t => {
  const {app, socket, views} = await connectedApp(t);
  await talk(app, [1]);
  t.mock.timers.tick(100000);
  socket.frame({type: 'push', origin: 'clarify', text: 'Which door?'});
  await talk(app, [2]);
  const answer = socket.sent.at(-1);
  t.mock.timers.tick(80001); await flush();  // the first ask's 180 s timeout passes
  assert.ok(!views.some(([kind]) => kind === 'error'));
  socket.frame({type: 'response', turn: answer.id, text: 'Locked the back door.'}); await flush();
  assert.ok(views.some(([kind, text]) => kind === 'answer' && text.endsWith('Locked the back door.')));
  assert.ok(!views.some(([kind]) => kind === 'error'));
  t.mock.timers.tick(1500); await flush();
  await talk(app, [3]);
  assert.equal(socket.sent.at(-1).audio.data, 'Aw==');  // the turn ended
});
test('#44 review: two questions in one turn; the deadline follows the latest answer', async t => {
  const {app, socket, views} = await connectedApp(t);
  await talk(app, [1]);
  t.mock.timers.tick(100000);
  socket.frame({type: 'push', origin: 'clarify', text: 'Which door?'});
  await talk(app, [2]);
  t.mock.timers.tick(100000);
  socket.frame({type: 'push', origin: 'clarify', text: 'Lock it now?'});
  await talk(app, [3]);
  const second = socket.sent.at(-1);
  t.mock.timers.tick(180001); await flush();  // the first ask and the first answer have both timed out
  assert.ok(!views.some(([kind]) => kind === 'error'));
  socket.frame({type: 'response', turn: second.id, text: 'Locked the back door now.'}); await flush();
  assert.ok(views.some(([kind, text]) => kind === 'answer' && text.endsWith('Locked the back door now.')));
  assert.ok(!views.some(([kind]) => kind === 'error'));
  t.mock.timers.tick(1500); await flush();
  await talk(app, [4]);
  assert.equal(socket.sent.at(-1).audio.data, 'BA==');
});
