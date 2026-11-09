/** Dependency-free protocol-v1 client. Event listeners receive plain frame objects. */
export class HermesGlassesClient {
  constructor({url, token, device, app, reconnect = true, WebSocketImpl = globalThis.WebSocket,
    onCapturePhoto}) {
    Object.assign(this, {url, token, device, app, reconnect, WebSocketImpl, onCapturePhoto});
    this.listeners = new Map();
    this.queries = new Map();
    this.interrupts = new Set();
    this.approvals = new Map();
    this.sequence = 0;
    this.backoff = 1000;
    this.closed = false;
  }
  on(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
    return () => this.off(type, fn);
  }
  off(type, fn) { this.listeners.get(type)?.delete(fn); }
  emit(type, frame) { for (const fn of this.listeners.get(type) || []) fn(frame); }
  connect() {
    if (this.ready) return Promise.resolve(this.handshake);
    if (this.connecting) return this.connecting;
    this.closed = false;
    clearTimeout(this.retryTimer);
    this.connecting = new Promise((resolve, reject) => {
      this.connectionWait = {resolve, reject};
    });
    const result = this.connecting;
    let parsed = false;
    try {
      const url = new URL(this.url);
      parsed = true;
      url.searchParams.set('token', this.token);
      const ws = this.socket = new this.WebSocketImpl(url.toString());
      ws.onmessage = event => {
        if (ws !== this.socket) return;
        let frame;
        try { frame = JSON.parse(event.data); } catch { return; }
        if (frame && typeof frame === 'object' && !Array.isArray(frame)) this.receive(frame);
      };
      ws.onerror = () => this.emit('error', {type: 'error', message: 'WebSocket transport error.'});
      ws.onclose = event => {
        if (ws !== this.socket) return;
        this.lost(event);
      };
      this.handshakeTimer = setTimeout(() => {
        if (ws === this.socket && !this.ready) ws.close();
      }, 10000);
    } catch {
      this.closed = !parsed;
      this.connectionWait?.reject(new Error('Invalid WebSocket URL or implementation.'));
      this.connectionWait = this.connecting = null;
      this.emit('error', {type: 'error', message: 'Invalid WebSocket URL or implementation.'});
      if (parsed) this.scheduleRetry();
    }
    return result;
  }
  lost(event) {
    this.ready = false;
    clearTimeout(this.handshakeTimer);
    const error = new Error('Disconnected.');
    this.connectionWait?.reject(error);
    this.connectionWait = this.connecting = null;
    for (const id of this.queries.keys()) this.finish(id, error);
    this.interrupts.clear();
    for (const pending of this.approvals.values()) pending.reject(error);
    this.approvals.clear();
    this.resetWait?.reject(error);
    this.resetWait = null;
    this.emit('disconnected', {code: event.code, reason: event.reason});
    if (event.code !== 4401) this.scheduleRetry();
  }
  scheduleRetry() {
    if (this.reconnect && !this.closed) {
      const delay = this.backoff * (0.75 + Math.random() * 0.25);
      this.backoff = Math.min(30000, this.backoff * 2);
      this.retryTimer = setTimeout(() => this.connect().catch(() => {}), delay);
    }
  }
  disconnect() {
    this.closed = true;
    clearTimeout(this.retryTimer);
    this.socket?.close();
  }
  send(frame, handshake = false) {
    if (!this.socket || this.socket.readyState !== 1 || (!handshake && !this.ready))
      throw new Error('Not connected.');
    this.socket.send(JSON.stringify(frame));
  }
  nextId() {
    let id;
    do { id = `q${++this.sequence}`; } while (this.queries.has(id) || this.interrupts.has(id));
    return id;
  }
  ask({text = '', audio, image, speak, id = this.nextId(), timeout = 180000, signal} = {}) {
    if (typeof id !== 'string' || !id || this.queries.has(id) || this.interrupts.has(id))
      return Promise.reject(new Error('Query id must be a unique nonempty string.'));
    return new Promise((resolve, reject) => {
      const abort = () => this.finish(id, Object.assign(new Error('Query aborted.'), {code: 'aborted'}));
      const pending = {resolve, reject, speak, slash: !audio && text.trim().startsWith('/'), signal, abort};
      this.queries.set(id, pending);
      if (signal?.aborted) { abort(); return; }
      signal?.addEventListener('abort', abort, {once: true});
      if (timeout) pending.deadline = setTimeout(() =>
        this.finish(id, Object.assign(new Error('Query timed out.'), {code: 'timeout'})), timeout);
      try { this.send({type: 'query', id, text, audio, image, speak}); }
      catch (error) { this.finish(id, error); }
    });
  }
  finish(id, error) {
    const pending = this.queries.get(id);
    if (!pending || (!pending.result && !error)) return;
    clearTimeout(pending.timer);
    clearTimeout(pending.deadline);
    pending.signal?.removeEventListener('abort', pending.abort);
    this.queries.delete(id);
    if (error) pending.reject(error);
    else pending.resolve(pending.result);
  }
  newSession() {
    if (this.resetWait) return this.resetWait.promise;
    const promise = new Promise((resolve, reject) => {
      this.resetWait = {resolve, reject};
      try { this.send({type: 'new_session'}); }
      catch (error) { this.resetWait = null; reject(error); }
    });
    if (this.resetWait) this.resetWait.promise = promise;
    return promise;
  }
  interrupt({ask} = {}) {
    const id = this.nextId();
    this.interrupts.add(id);
    this.send({type: 'interrupt', id});
    const stopped = ask ?? [...this.queries].find(([, query]) => !query.result && !query.slash)?.[0];
    // An ask that already has its text (waiting only for audio) is resolved with it, not rejected.
    this.finish(stopped, this.queries.get(stopped)?.result
      ? undefined : Object.assign(new Error('Query interrupted.'), {code: 'interrupted'}));
  }
  answerApproval(id, choice) {
    if (this.approvals.has(id)) return Promise.reject(new Error('Approval answer already pending.'));
    return new Promise((resolve, reject) => {
      this.approvals.set(id, {resolve, reject});
      try { this.send({type: 'approval_response', id, choice}); }
      catch (error) { this.approvals.delete(id); reject(error); }
    });
  }
  sendPhoto(request, data) { this.send({type: 'photo', request, data, mime: 'image/jpeg'}); }
  photoError(request, message) { this.send({type: 'photo_error', request, message}); }
  receive(frame) {
    const id = frame.turn;
    const pending = this.queries.get(id);
    switch (frame.type) {
      case 'welcome':
        this.send({type: 'hello', protocol: 1, device: this.device, app: this.app}, true);
        break;
      case 'hello_ack':
        clearTimeout(this.handshakeTimer);
        this.ready = true;
        this.handshake = frame;
        this.backoff = 1000;
        this.connectionWait?.resolve(frame);
        this.connectionWait = this.connecting = null;
        this.emit('connected', frame);
        break;
      case 'response':
        if (this.interrupts.delete(id) || !pending || pending.result) break;  // a /stop reply settles nothing
        pending.result = {id, text: frame.text};
        if (pending.transcript !== undefined) pending.result.transcript = pending.transcript;
        if (this.device.capabilities?.audio_out?.length && pending.speak !== false &&
            this.handshake?.server?.features?.includes('audio_out'))
          pending.timer = setTimeout(() => this.finish(id), 15000);
        else this.finish(id);
        break;
      case 'audio': {
        const query = this.queries.get(frame.ref);
        if (query?.result) { query.result.audio = {data: frame.data, mime: frame.mime}; this.finish(frame.ref); }
        this.emit('audio', frame);
        break;
      }
      case 'transcript':
        if (pending) pending.transcript = frame.text;
        this.emit('transcript', {...frame, id});
        break;
      case 'response_delta': this.emit('delta', {...frame, id}); break;
      case 'status':
        // Status has no normative turn id. Only infer it when exactly one query is pending.
        this.emit('status', {...frame, id: id ?? (this.queries.size === 1 ? this.queries.keys().next().value : undefined)});
        break;
      case 'error': {
        // Uncorrelated errors are operation-wide; never guess which concurrent query failed.
        const error = new Error(frame.message || 'Server error.');
        const correlated = frame.turn ?? frame.id;
        const inFlight = this.queries.size;
        const entries = correlated === undefined ? [...this.queries] : [[correlated, this.queries.get(correlated)]];
        for (const [key, query] of entries) if (query) {
          this.finish(key, query.result ? undefined : error);
        }
        // A query's error is not the reset's: reject the reset only for its own failure or with no query in flight.
        if (frame.message === 'New session failed.' || inFlight === 0) {
          this.resetWait?.reject(error); this.resetWait = null;
        }
        this.emit('error', frame);
        break;
      }
      case 'session_reset': this.resetWait?.resolve(frame); this.resetWait = null; break;
      case 'approval_result':
        this.approvals.get(frame.id)?.resolve(frame); this.approvals.delete(frame.id); break;
      case 'push':
        if (this.device.capabilities?.ack) this.send({type: 'ack', id: frame.id});
        this.emit('push', frame); break;
      case 'capture_photo':
        if (this.onCapturePhoto) {
          const failure = () => { if (this.ready) this.photoError(frame.request, 'Camera unavailable.'); };
          try {
            Promise.resolve(this.onCapturePhoto(frame.reason)).then(data => {
              if (typeof data !== 'string') throw new Error('Invalid photo.');
              this.sendPhoto(frame.request, data);
            }).catch(failure);
          } catch { failure(); }
        }
        try { this.emit('capture_photo', frame); } catch { /* Listener failures must not stop camera replies. */ }
        break;
      case 'approval_request': case 'approval_closed': case 'display': case 'haptic':
        this.emit(frame.type, frame); break;
    }
  }
}

export class HermesGlassesHttp {
  constructor({baseUrl, token, fetchImpl = globalThis.fetch}) {
    Object.assign(this, {baseUrl: baseUrl.replace(/\/$/, ''), token, fetchImpl});
  }
  async request(path, body, textPlain = false) {
    const response = await this.fetchImpl(this.baseUrl + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {Authorization: `Bearer ${this.token}`, Accept: textPlain ? 'text/plain' : 'application/json',
        ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return textPlain ? {status: response.status === 202 ? 'pending' : 'done', text: await response.text()} : response.json();
  }
  turn(body, {textPlain = false} = {}) { return this.request('/turn', body, textPlain); }
  pending(wait = 25) { return this.request(`/pending?wait=${encodeURIComponent(wait)}`); }
  approval(id, choice) { return this.request('/approval', {id, choice}); }
}
