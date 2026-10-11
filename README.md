# /call-me for Hermes Agent

**Text your Hermes agent from your iPhone. It texts back, and when a text is not
enough it rings your phone, asks out loud, and turns your spoken answer into text.**

[/call-me](https://callmemcp.com) is an iPhone app that gives you a private
10-digit number for your AI agents. This plugin makes it a Hermes gateway
platform, next to Telegram, Signal or Slack:

- **Text the agent** from the /call-me app, like any other chat thread.
- **Get replies** as normal iPhone notifications, lock screen included.
- **Get called.** The `callme_call` tool rings the phone with a real incoming-call
  screen, speaks the agent's text, waits for your answer, and returns the
  transcript to the agent.
- **Leave a voicemail.** Call the agent's number from the app and speak; the
  transcript arrives as a message starting with `[Voicemail]`.
- **Share one agent.** List several numbers (you, your partner) and each person
  gets their own thread with the same agent.
- **Cron delivery.** Scheduled jobs can deliver to `callme`, which texts the home
  number.

```
you, on the phone:  "remind me to call the plumber tomorrow at 9"
Hermes:             "Done, I'll text you at 9."
...next morning, the phone rings...
Hermes (spoken):    "The plumber opens at 9. Want me to draft the message?"
you:                "Yes, ask about Thursday"
```

## Install

```bash
hermes plugins install callmemcp
```

Or straight from GitHub:

```bash
hermes plugins install https://github.com/radres/hermes-plugin-call-me
```

Then:

1. Install /call-me on each iPhone: [App Store](https://callmemcp.com/aiphone/go/hermes).
   Open it and note the 10-digit number it shows.
2. Add the numbers to the Hermes profile's `.env`:

   ```bash
   CALLME_ALLOWED_USERS=5551234567,5559876543
   CALLME_NAMES=5551234567=Sam,5559876543=Alex
   ```

3. Restart the gateway. The platform turns itself on once `CALLME_ALLOWED_USERS`
   is set:

   ```bash
   hermes gateway install    # first time: run the gateway as a background service
   hermes gateway restart
   ```

On first start the agent texts each listed number a short hello. That thread is
the agent: reply to it to talk to Hermes.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `CALLME_ALLOWED_USERS` | yes | Comma-separated /call-me numbers that may talk to the agent. Nobody else can. |
| `CALLME_NAMES` | no | `number=name` pairs. The agent sees the name, and `callme_call` accepts it. |
| `CALLME_LABEL` | no | Thread name on the phone. Default `Hermes`. |
| `CALLME_HOME_CHANNEL` | no | Number that receives cron deliveries. Default: the first allowed number. |
| `CALLME_HOME_CHANNEL_NAME` | no | Label for the home channel. |
| `CALLME_ALLOW_UNVERIFIED` | no | Also accept messages from numbers with no verified device (very old app builds). Off by default. |

## Security model

- **Deny by default.** Inbound messages are accepted only from numbers in
  `CALLME_ALLOWED_USERS`. Pairing codes are not used.
- **Verified senders only.** /call-me marks every inbound message with whether it
  came from the number's own registered device. Messages without that mark are
  dropped, so knowing someone's number is not enough to talk to their agent as
  them.
- **Outbound is fenced too.** The adapter, the cron sender and `callme_call` refuse
  any number that is not on the allowlist, so a prompt-injected agent cannot text
  or ring a stranger.
- A /call-me number is a capability: anyone who knows it can text or ring that
  phone through the service. Treat the numbers in `.env` like any other secret.

## What this plugin does that you should know about

- **Network calls.** It talks to the /call-me service at `https://callmemcp.com/aiphone`
  (operated by the plugin author) and nowhere else. It creates one agent session
  (`POST /sessions`), sends texts (`POST /messages`), places calls (`POST /calls`),
  renames the thread (`POST /sessions/label`) and long-polls for replies
  (`GET /sessions/events`).
- **Data sent.** Message text you or the agent write, the text the agent asks to
  have spoken, and the destination numbers. Call audio is transcribed by the
  service and only the transcript comes back. See the
  [privacy policy](https://callmemcp.com/aiphone/privacy).
- **Background task.** While the gateway runs, the adapter keeps one long-poll open
  to `/sessions/events`, at most one request every 3 seconds.
- **Stored credential.** The agent session token and the event cursor are kept in
  `<HERMES_HOME>/plugin-data/call-me/session.json` (mode 600). Delete the file to
  get a fresh agent number. The plugin reads no other files and no other
  credentials.
- **Waiting for a person.** `callme_call` blocks until the call ends, at most
  300 seconds plus network slack, then returns `missed` if nobody answered. It
  never hangs an unattended run.
- No shell commands, no telemetry, no self-update.

## Cost

/call-me is a subscription app on the receiving iPhone. The plugin itself is free
and MIT-licensed.

## Development

```bash
PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m unittest discover -s tests
hermes plugins validate .
```

The tests run against an in-process fake of the /call-me API and never touch the
network or a real phone.
