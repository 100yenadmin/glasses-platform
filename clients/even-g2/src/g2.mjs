import {HermesGlassesClient} from '../../js/hermes-glasses.js';
export const LENS_CHARS = 400;
const PROMPT_HEIGHT = 112;  // approval page: the request text above the choices
const PROMPTS = new Set(['clarify', 'approval']);  // Hermes waits on these: they never wait for the turn
export function label63(value) {
  let out = '';
  for (const char of String(value)) { if (new TextEncoder().encode(out + char).length > 63) break; out += char; }
  return out;
}
export function createG2(bridge, textBox, mirror = (_state, _text) => {}, WebSocketImpl = globalThis.WebSocket) {
  let client, turn = false, recording = false, micBusy = false, closed = false, timer, renderTimer, pushTimer, pushing = false;
  let transcript = '', answer = '', content = textBox.content, state = '', approval, selected = 0, page = 'text';
  let prompt = null, chain = null, forPrompt = false, answering = null;  // a question Hermes asked mid-turn; one abort for the turn's asks
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
        // What is being approved stays on the lens above the choices; only the list takes touchpad events.
        const {content: _text, ...frame} = textBox;  // the bridge rejects text-only fields on a list container
        const prompt = String(request.text || 'Hermes approval');
        await bridge.rebuildPageContainer({containerTotalNum: 2,
          textObject: [{...textBox, containerID: 2, containerName: 'prompt', yPosition: 0, height: PROMPT_HEIGHT,
            isEventCapture: 0, content: prompt.length > 160 ? prompt.slice(0, 159) + '…' : prompt}],
          listObject: [{...frame, containerID: 1, containerName: 'choices', yPosition: PROMPT_HEIGHT,
            height: textBox.height - PROMPT_HEIGHT, isEventCapture: 1,
            itemContainer: {itemCount: request.choices.length, itemName: request.choices.map(label63), isItemSelectBorderEn: 1}}]});
        page = 'list';
      });
    } else if (prompt) {
      show('push', `${prompt.note ? prompt.note + '\n' : ''}Hermes asks\n${prompt.text}\nTap to answer`);
    } else if (pushing) {
      // A push is on the lens: its own timer shows the next one.
    } else if (!turn && !recording && pushes.length) {
      const push = pushes.shift(); show('push', `${push.origin === 'cron' ? 'Reminder' : 'Hermes'}\n${push.text}`);
      pushing = true; clearTimeout(pushTimer); pushTimer = setTimeout(() => { pushing = false; next(); }, 4000);
    } else show(state || 'answer', content);
  }
  function dismiss(id) {
    const index = approvals.findIndex(item => item.id === id);
    if (index >= 0) approvals.splice(index, 1);
    if (approval?.id !== id) return;
    approval = null; show(turn ? 'thinking' : 'answer', transcript + '\n' + answer); next();
  }
  function settle(result) { transcript = result.transcript || transcript; answer = result.text; show('answer', `${transcript}\n${answer}`); }
  async function ask(query) {
    if (closed || turn || !client?.ready) { fail(); return; }
    turn = true; prompt = null; transcript = ''; answer = ''; show('thinking', 'Thinking…');
    const own = chain = new AbortController();
    try { settle(await client.ask({...query, speak: false, signal: own.signal})); }
    catch (error) {
      // The wearer's thinking time counts against this ask's timeout: once an answer is on its way, that answer
      // (which Hermes folds into this turn) decides how the turn ends.
      if (error?.code === 'timeout' && answering) { while (answering) await answering; }  // follows the latest answer
      else if (error?.code !== 'aborted') fail();
    }
    finally {
      own.abort();  // the answer to a mid-turn question is still waiting: this reply ended the turn
      if (chain === own) chain = null;
      turn = false; prompt = null; pushing = false; if (!closed) { clearTimeout(pushTimer); pushTimer = setTimeout(next, 1500); }
    }
  }
  async function reply(query) {
    // The wearer answers a question Hermes asked during the turn. Hermes folds a clarify answer into the running
    // turn, so the turn's final reply may come back on this ask or on the first one: whichever arrives ends both.
    // A spoken answer to a text approval gets its own short reply instead; the turn carries on with the first ask.
    const own = chain, question = prompt, ends = question?.origin !== 'approval';
    prompt = null; show('thinking', 'Thinking…');
    const mine = answering = (async () => {
      try {
        const result = await client.ask({...query, speak: false, signal: own?.signal});
        if (ends || !turn) { settle(result); own?.abort(); } else show('thinking', result.text || 'Thinking…');
      } catch (error) {
        // A later question superseded this answer: Hermes replies on the latest one, so this timeout is expected.
        if (error?.code === 'aborted' || (error?.code === 'timeout' && answering !== mine)) return;
        // Not heard (or another error for this answer alone): the turn still waits, so ask the question again.
        if (turn && question && chain === own && error?.code !== 'timeout') {
          prompt = {...question, note: error?.message || 'Try again.'};
          if (!recording) next();
        } else fail();
      }
    })();
    try { await mine; } finally { if (answering === mine) answering = null; }
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
    if ((turn && !prompt && !recording) || (!recording && !client?.ready)) return;  // a recording always stops, even after a disconnect
    micBusy = true;
    try {
      if (recording) {
        clearTimeout(timer); recording = false;
        if (!await serial(() => bridge.audioControl(false))) { fail(); return; }
        if (!client?.ready) { chunks = []; fail(); return; }
        const data = btoa(chunks.map(chunk => Array.from(chunk, byte => String.fromCharCode(byte)).join('')).join(''));
        const query = {audio: {mime: 'audio/pcm', rate: 16000, data}};
        void (forPrompt && turn ? reply(query) : ask(query)); next();
      } else {
        chunks = []; recording = true; forPrompt = turn;
        if (!await serial(() => bridge.audioControl(true))) { recording = false; fail(); return; }
        if (!recording) { serial(() => bridge.audioControl(false)); return; }  // paused while the mic was opening
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
      client.on('delta', frame => { answer = frame.text; if (!prompt) show('answer', `${transcript}\n${answer}`); });
      client.on('push', frame => {
        // A question this device's turn waits on is shown at once and stays until answered or the turn ends.
        // Without a local turn (asked elsewhere, or replayed later) it is an ordinary push and cannot stick.
        if (PROMPTS.has(frame.origin) && turn) { prompt = frame; if (!recording) next(); return; }
        pushes.push(frame); if (!turn && !recording && !approval) next();
      });
      // glasses_show_text: shown like a push, after the turn and between other pushes.
      client.on('display', frame => { pushes.push({origin: 'display', text: frame.text}); if (!turn && !recording && !approval) next(); });
      client.on('approval_request', frame => {
        if (!Array.isArray(frame.choices) || !frame.choices.length || frame.choices.length > 20) { fail(); return; }
        approvals.push({...frame, choices: frame.choices}); next();
      });
      client.on('approval_closed', frame => dismiss(frame.id));
      // An error that names its query is handled by that query's ask; only operation-wide errors show here.
      client.on('error', frame => { if ((frame?.turn ?? frame?.id) === undefined) fail(); }); client.on('disconnected', fail);
      client.on('connected', () => { if (!turn && !recording && !approval) { show('answer', 'Tap to talk to Hermes'); next(); } });
      await client.connect(); show('answer', 'Tap to talk to Hermes');
    },
    pause() {  // leaving the foreground: stop the mic, keep the app and its connection
      if (!recording) return;
      clearTimeout(timer); recording = false; chunks = [];
      serial(() => bridge.audioControl(false));
      if (turn) next(); else show('answer', 'Tap to talk to Hermes');
    },
    exit() { serial(() => bridge.shutDownPageContainer(1)); },
    close() { closed = true; clearTimeout(timer); clearTimeout(renderTimer); clearTimeout(pushTimer); recording = false;
      serial(() => bridge.audioControl(false)); client?.disconnect(); }
  };
}
