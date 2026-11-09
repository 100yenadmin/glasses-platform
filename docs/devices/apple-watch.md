# Apple Watch, iPhone and Mac: "Ask Hermes" with Siri Shortcuts

Talk to your own Hermes agent from your wrist with a Siri Shortcut: no app to install. The shortcut sends what you
say to the plugin's HTTP route and speaks the answer. It shares the same conversation, memory and reminders as your
glasses.

Apple Watch apps can only make plain HTTP requests (watchOS blocks always-open connections except during audio or
calls), so the watch uses `POST /turn` instead of the WebSocket.

## 1. Pair the watch

```bash
hermes glasses pair <your-name> watch --no-qr
```

Copy the token from the URL it prints (the part after `token=`). Restart the gateway so it loads the new device:

```bash
hermes gateway restart
```

Your watch or phone must reach the plugin's port (8765 by default): on the same Wi-Fi, through Tailscale, or behind
an HTTPS proxy. Away from home, use Tailscale or HTTPS. The token is sent with every request.

## 2. Build the shortcut (iPhone or Mac, 2 minutes)

In the Shortcuts app, create a shortcut named **Ask Hermes** with these actions:

1. **Ask for Input**: type *Text*, prompt *What do you want to ask?* (on the watch this opens dictation).
2. **Get Contents of URL**:
   - URL: `http://<your-host>:8765/turn`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <your token>`, and `Accept` = `text/plain`
   - Request Body: **JSON**, one field: `text` (Text) = *Provided Input*
3. **Speak Text**: *Contents of URL*. Add **Show Result** with the same value if you also want it on screen.

Open the shortcut's details and turn on **Show on Apple Watch**. Then say "Hey Siri, Ask Hermes", or add it to
a watch face as a complication.

## 3. Answers that take longer

- **Approvals.** If the agent needs permission to run a command, the answer is the question ("Hermes wants to run
  ... Say yes to allow it once, or no to block it."). Run the shortcut again and say "yes" or "no".
- **Long tasks.** If the turn is still running after 50 seconds, the reply is empty and the answer waits in the
  queue for the "Hermes news" shortcut below.
- **Reminders** you set from any device wait in the same queue. A plain-text answer never includes them, so they
  stay queued until "Hermes news" fetches them.

### "Hermes news": reminders and late answers

1. **Get Contents of URL**: URL `http://<your-host>:8765/pending?wait=0`, Method **GET**, Headers
   `Authorization` = `Bearer <your token>`.
2. **Get Dictionary Value**: Key `items` in *Contents of URL*.
3. **Repeat with Each** item in *Dictionary Value*: **Get Dictionary Value** `text` in *Repeat Item*, then
   **Speak Text** *Dictionary Value*.

Turn on **Show on Apple Watch** for it too ("Hey Siri, Hermes news"). Each item is removed from the queue once it
has been returned.

## Check it from a terminal

```bash
curl -s -X POST http://127.0.0.1:8765/turn \
  -H "Authorization: Bearer $TOKEN" -H 'Accept: text/plain' \
  -H 'Content-Type: application/json' -d '{"text":"What time is it?"}'
```

## What is proven

The HTTP route is tested in the plugin's test suite and against a real Hermes gateway. The shortcut has not yet
been run on a real Apple Watch: Shortcuts on watchOS can differ from iPhone (for example, request time limits).
Reports welcome in issue #22.
