import { waitForEvenAppBridge, TextContainerProperty, CreateStartUpPageContainer, OsEventTypeList } from '@evenrealities/even_hub_sdk'
import { createG2 } from './g2.mjs'
import { mountUi, setStatus, setTranscript, settingsUi } from './ui'

mountUi()
const bridge = await waitForEvenAppBridge()
const transcript = new TextContainerProperty({
  xPosition: 0,
  yPosition: 0,
  width: 576,
  height: 288,
  borderWidth: 0,
  borderColor: 5,
  paddingLength: 4,
  containerID: 1,
  containerName: 'transcript',
  content: 'Open settings on the phone',
  isEventCapture: 1,
})

const created = await bridge.createStartUpPageContainer(
  new CreateStartUpPageContainer({ containerTotalNum: 1, textObject: [transcript] }),
)
if (created !== 0) {
  setStatus('error', `createStartUpPageContainer failed: ${created}`)
  throw new Error('Failed to create startup page')
}


const app = createG2(bridge, transcript, (state: string, text: string) => {
  setStatus(state, state); setTranscript(text, '')
})
const dev = import.meta.env.DEV ? new URLSearchParams(location.search) : new URLSearchParams()
const url = dev.get('server') ?? await bridge.getLocalStorage('hermes-url')
const token = dev.get('token') ?? await bridge.getLocalStorage('hermes-token')
async function connect(url: string, token: string) {
  try {
    const parsed = new URL(url)
    if (!['ws:', 'wss:'].includes(parsed.protocol) || parsed.search || parsed.username || parsed.password || !token) throw Error()
    if (!await app.store('hermes-url', url) || !await app.store('hermes-token', token)) throw Error()
    await app.connect(url, token)
  } catch { app.fail() }
}
settingsUi(url, token, connect)
function cleanup() { unsubscribe(); app.close() }

// Reads the event type out of one envelope.
//
// CLICK_EVENT is 0, and protobuf omits zero-value fields on the wire, so a
// single tap arrives as an envelope whose `eventType` is `undefined`. The
// default has to be resolved INSIDE the envelope check. Writing
// `event.sysEvent?.eventType ?? OsEventTypeList.CLICK_EVENT` instead would
// read CLICK on events that carry no `sysEvent` at all — and in this template
// that means every incoming audio frame would fire the tap handler.
function eventTypeOf(envelope?: { eventType?: OsEventTypeList }): OsEventTypeList | null {
  if (!envelope) return null
  return envelope.eventType ?? OsEventTypeList.CLICK_EVENT
}

// Event routing, critical details:
//   • Taps/double-taps/lifecycle come through `event.sysEvent`.
//     Audio PCM frames come through `event.audioEvent` — separate branch.
//   • Double-tap → `shutDownPageContainer(1)` is a root-level check: it
//     must fire no matter which envelope the event arrives in, so users
//     can always exit the app. System exit confirmation dialog appears;
//     SYSTEM_EXIT_EVENT fires on confirm and we clean up there.
//   • Check DOUBLE_CLICK_EVENT before CLICK_EVENT.
const unsubscribe = bridge.onEvenHubEvent(event => {
  const pcm = event.audioEvent?.audioPcm
  if (pcm) app.pcm(pcm)
  const types = [eventTypeOf(event.sysEvent), eventTypeOf(event.textEvent), eventTypeOf(event.listEvent)]
  if (types.includes(OsEventTypeList.DOUBLE_CLICK_EVENT)) { app.exit(); return }
  if (types.some(type => type === OsEventTypeList.SYSTEM_EXIT_EVENT || type === OsEventTypeList.ABNORMAL_EXIT_EVENT)) { cleanup(); return }
  if (types.includes(OsEventTypeList.FOREGROUND_EXIT_EVENT)) { app.pause(); return }
  // Index 0 is a zero-value field, so like CLICK it can be missing from the envelope.
  if (event.listEvent) app.select(event.listEvent.currentSelectItemIndex ?? 0)
  if (types.includes(OsEventTypeList.CLICK_EVENT)) void app.tap()
})
window.addEventListener('beforeunload', cleanup)
if (url && token) {
  await connect(url, token)
  const say = dev.get('say')
  if (say) void app.ask({text: say})
}
else setStatus('connecting', 'Open settings on the phone')
