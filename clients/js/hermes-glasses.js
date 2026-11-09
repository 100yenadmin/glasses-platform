/** Dependency-free protocol-v1 client. Event listeners receive plain frame objects. */
export class HermesGlassesClient {
  constructor({url, token, device, app, reconnect = true, WebSocketImpl = globalThis.WebSocket,
    onCapturePhoto}) {
    Object.assign(this, {url, token, device, app, reconnect, WebSocketImpl, onCapturePhoto});
    this.listeners = new Map();
    this.queries = new Map();
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
    try {
      const url = new URL(this.url);
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
      this.lost({code: 0, reason: 'Could not open WebSocket.'});
    }
    return result;
  }
  lost(event) {
    this.ready = false;
    clearTimeout(this.handshakeTimer);
    const error = new Error('Disconnected.');
    this.connectionWait?.reject(error);
    this.connectionWait = this.connecting = null;
    for (const pending of this.queries.values()) { clearTimeout(pending.timer); pending.reject(error); }
    this.queries.clear();
    for (const pending of this.approvals.values()) pending.reject(error);
    this.approvals.clear();
    this.resetWait?.reject(error);
    this.resetWait = null;
    this.emit('disconnected', {code: event.code, reason: event.reason});
    if (this.reconnect && !this.closed && event.code !== 4401) {
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
  ask({text = '', audio, image, speak, id = `q${++this.sequence}`} = {}) {
    if (typeof id !== 'string' || !id || this.queries.has(id))
      return Promise.reject(new Error('Query id must be a unique nonempty string.'));
    return new Promise((resolve, reject) => {
      this.queries.set(id, {resolve, reject, speak});
      try { this.send({type: 'query', id, text, audio, image, speak}); }
      catch (error) { this.queries.delete(id); reject(error); }
    });
  }
  finish(id) {
    const pending = this.queries.get(id);
    if (!pending?.result) return;
    clearTimeout(pending.timer);
    this.queries.delete(id);
    pending.resolve(pending.result);
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
  interrupt(id) { this.send({type: 'interrupt', id}); }
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
        if (!pending || pending.result) break;
        pending.result = {id, text: frame.text};
        if (pending.transcript !== undefined) pending.result.transcript = pending.transcript;
        if (this.device.capabilities?.audio_out?.length && pending.speak !== false)
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
        const entries = correlated === undefined ? [...this.queries] : [[correlated, this.queries.get(correlated)]];
        for (const [key, query] of entries) if (query) {
          clearTimeout(query.timer); query.reject(error); this.queries.delete(key);
        }
        if (correlated === undefined) { this.resetWait?.reject(error); this.resetWait = null; }
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
        this.emit('capture_photo', frame);
        if (this.onCapturePhoto) Promise.resolve().then(() => this.onCapturePhoto(frame.reason))
          .then(data => this.sendPhoto(frame.request, data))
          .catch(() => { if (this.ready) this.photoError(frame.request, 'Camera unavailable.'); });
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
    return textPlain ? response.text() : response.json();
  }
  turn(body, {textPlain = false} = {}) { return this.request('/turn', body, textPlain); }
  pending(wait = 25) { return this.request(`/pending?wait=${encodeURIComponent(wait)}`); }
  approval(id, choice) { return this.request('/approval', {id, choice}); }
}
