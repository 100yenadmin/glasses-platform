import {HermesGlassesClient} from '../../js/hermes-glasses.js';
export const LENS_CHARS = 400;
export function label63(value) {
  let out = '';
  for (const char of String(value)) { if (new TextEncoder().encode(out + char).length > 63) break; out += char; }
  return out;
}
export function createG2(bridge, textBox, mirror = (_state, _text) => {}, WebSocketImpl = globalThis.WebSocket) {
  let client, turn = false, recording = false, micBusy = false, closed = false, timer, renderTimer, pushTimer;
  let transcript = '', answer = '', content = textBox.content, state = '', approval, selected = 0, page = 'text';
  let queue = Promise.resolve();
  const pushes = [], approvals = [];
  const serial = fn => (queue = queue.then(fn).catch(() => { fail(); }));
  function show(kind, text) {
    if (closed) return;
    // The lens fits about 9 lines of 50 characters: keep the start of a long answer, not its end.
    content = String(text).length > LENS_CHARS ? String(text).slice(0, LENS_CHARS - 1) + '…' : String(text);
    if (kind !== state) { state = kind; console.log(`[hermes] ${kind}`); }
    mirror(kind, content);
    if (renderTimer) return;
    renderTimer = setTimeout(() => { renderTimer = null; serial(async () => {
      if (closed || approval) return;
      if (page !== 'text') { await bridge.rebuildPageContainer({containerTotalNum: 1, textObject: [{...textBox, content}]}); page = 'text'; }
      else await bridge.textContainerUpgrade({containerID: 1, containerName: 'transcript', content});
    }); }, 120);
  }
  function fail() { show('error', 'Connection or device error. Open settings on the phone.'); }
  function next() {
    if (closed || approval || recording) return;
    if (approvals.length) {
      approval = approvals.shift(); selected = 0;
      show('approval', approval.text || 'Hermes approval');
      const request = approval;
      serial(async () => {
        await bridge.rebuildPageContainer({containerTotalNum: 1, listObject: [{...textBox, containerName: 'choices',
          itemContainer: {itemCount: request.choices.length, itemName: request.choices.map(label63), isItemSelectBorderEn: 1}}]});
        page = 'list';
      });
    } else if (!turn && !recording && pushes.length) {
      const push = pushes.shift(); show('push', `${push.origin === 'cron' ? 'Reminder' : 'Hermes'}\n${push.text}`);
      clearTimeout(pushTimer); pushTimer = setTimeout(next, 4000);
    } else show(state || 'answer', content);
  }
  function dismiss(id) {
    const index = approvals.findIndex(item => item.id === id);
    if (index >= 0) approvals.splice(index, 1);
    if (approval?.id !== id) return;
    approval = null; show(turn ? 'thinking' : 'answer', transcript + '\n' + answer); next();
  }
  async function ask(query) {
    if (closed || turn || !client?.ready) { fail(); return; }
    turn = true; transcript = ''; answer = ''; show('thinking', 'Thinking…');
    try { const result = await client.ask({...query, speak: false}); transcript = result.transcript || transcript; answer = result.text; show('answer', `${transcript}\n${answer}`); }
    catch { fail(); }
    finally { turn = false; if (!closed) { clearTimeout(pushTimer); pushTimer = setTimeout(next, 1500); } }
  }
  async function tap() {
    if (micBusy || closed) return;
    if (approval) {
      const id = approval.id, choice = approval.choices[selected];
      if (approval.sending) return;
      approval.sending = true;
      try { await client.answerApproval(id, choice); dismiss(id); } catch { dismiss(id); fail(); }
      return;
    }
    if (!client?.ready || turn) return;
    micBusy = true;
    try {
      if (recording) {
        clearTimeout(timer); recording = false;
        if (!await serial(() => bridge.audioControl(false))) { fail(); return; }
        const data = btoa(chunks.map(chunk => Array.from(chunk, byte => String.fromCharCode(byte)).join('')).join(''));
        void ask({audio: {mime: 'audio/pcm', rate: 16000, data}}); next();
      } else {
        chunks = []; recording = true;
        if (!await serial(() => bridge.audioControl(true))) { recording = false; fail(); return; }
        show('listening', 'Listening…'); timer = setTimeout(tap, 8000);
      }
    } finally { micBusy = false; }
  }
  let chunks = [];
  return {
    fail, ask, tap,
    async store(key, value) { let saved = false; await serial(async () => { saved = await bridge.setLocalStorage(key, value); }); return saved; },
    pcm(chunk) { if (recording) chunks.push(new Uint8Array(chunk)); },
    select(index) { if (approval && Number.isInteger(index) && index >= 0 && index < approval.choices.length) selected = index; },
    async connect(url, token) {
      client?.disconnect();
      client = new HermesGlassesClient({url, token, WebSocketImpl, device: {name: 'Even G2', model: 'G2',
        capabilities: {display: true, display_chars: LENS_CHARS, buttons: true, mic: true}}, app: {name: 'Hermes Glasses', version: '0.1.0'}});
      client.on('transcript', frame => { transcript = frame.text; });
      client.on('delta', frame => { answer = frame.text; show('answer', `${transcript}\n${answer}`); });
      client.on('push', frame => { pushes.push(frame); if (!turn && !recording && !approval) next(); });
      client.on('approval_request', frame => {
        if (!Array.isArray(frame.choices) || !frame.choices.length || frame.choices.length > 20) { fail(); return; }
        approvals.push({...frame, choices: frame.choices}); next();
      });
      client.on('approval_closed', frame => dismiss(frame.id));
      client.on('error', fail); client.on('disconnected', fail);
      await client.connect(); show('answer', 'Tap to talk to Hermes');
    },
    exit() { serial(() => bridge.shutDownPageContainer(1)); },
    close() { closed = true; clearTimeout(timer); clearTimeout(renderTimer); clearTimeout(pushTimer); recording = false;
      serial(() => bridge.audioControl(false)); client?.disconnect(); }
  };
}
