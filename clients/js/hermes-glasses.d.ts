export interface Capabilities {
  camera?: boolean; display?: boolean; display_chars?: number; speaker?: boolean; mic?: boolean;
  audio_out?: string[]; buttons?: boolean; haptics?: boolean; ack?: boolean;
}
export interface Media { data: string; mime: string; rate?: number }
export interface Query { text?: string; audio?: Media; image?: Media; speak?: boolean; id?: string }
export interface Frame { type?: string; [field: string]: unknown }
export interface HelloAck extends Frame {
  protocol: number; user: string; device: string;
  server: {plugin?: string; version: string; features: string[]};
}
export interface TurnResult { id: string; text: string; transcript?: string; audio?: Media }
export interface WebSocketLike {
  readyState: number;
  onmessage: ((event: {data: string}) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onclose: ((event: {code: number; reason?: string}) => void) | null;
  send(data: string): void;
  close(): void;
}
export interface ClientOptions {
  url: string; token: string;
  device: {name: string; model?: string; capabilities: Capabilities};
  app?: {name: string; version: string}; reconnect?: boolean;
  WebSocketImpl?: new (url: string) => WebSocketLike;
  onCapturePhoto?: (reason: string) => Promise<string>;
}
export type ClientEvent = 'connected' | 'disconnected' | 'push' | 'approval_request' |
  'approval_closed' | 'capture_photo' | 'display' | 'haptic' | 'audio' | 'status' | 'delta' |
  'transcript' | 'error';
export class HermesGlassesClient {
  constructor(options: ClientOptions);
  onCapturePhoto?: (reason: string) => Promise<string>;
  on(type: ClientEvent, listener: (frame: Frame) => void): () => void;
  off(type: ClientEvent, listener: (frame: Frame) => void): void;
  connect(): Promise<HelloAck>;
  disconnect(): void;
  ask(query?: Query): Promise<TurnResult>;
  newSession(): Promise<Frame>;
  interrupt(id?: string): void;
  answerApproval(id: string, choice: string): Promise<Frame>;
  sendPhoto(request: string, base64Jpeg: string): void;
  photoError(request: string, message: string): void;
}
export interface HttpTurn extends Query { capabilities?: Capabilities }
export interface HttpResult {
  id: string; text?: string; transcript?: string; audio?: Media;
  status: 'done' | 'pending' | 'needs_answer'; prompts?: Frame[];
}
export interface FetchResponse {
  ok: boolean; status: number; text(): Promise<string>; json(): Promise<unknown>;
}
export type FetchImpl = (url: string, init: {
  method: string; headers: Record<string, string>; body?: string;
}) => Promise<FetchResponse>;
export class HermesGlassesHttp {
  constructor(options: {baseUrl: string; token: string; fetchImpl?: FetchImpl});
  turn(body: HttpTurn, options: {textPlain: true}): Promise<string>;
  turn(body: HttpTurn, options?: {textPlain?: false}): Promise<HttpResult>;
  pending(wait?: number): Promise<{items: Frame[]}>;
  approval(id: string, choice: string): Promise<Frame>;
}
