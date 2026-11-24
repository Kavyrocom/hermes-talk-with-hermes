# talk-with-hermes

Talk to a Hermes agent by voice, full duplex: in Hermes Desktop and in Discord voice channels.

## Status

| Surface or service | Status |
|---|---|
| Hermes Desktop with Gemini Live | In real calls since 2026-11-20. |
| Discord voice channels | Written and tested against fakes. Live, only the voice connection is proven: the bot joins the channel. A full call (listening, answer, delegation to the agent) is still to be done. |
| OpenAI Realtime, Grok Voice | Written from the providers' documentation, tested against a fake server, never in a real call. |

A model is listed as supported only after a real call: a sentence said, an answer heard.

## Installation

1. Hermes Desktop, Settings, Plugins: install `talk-with-hermes` in the agent's profile.
2. Plugin settings page: paste the voice service key (Gemini by default). This is the only place where a key is entered. Desktop and the Discord voice channels of every agent read it there, and it is copied nowhere else.
3. The key to each agent's local API server (`API_SERVER_KEY`, 127.0.0.1) is not entered by hand: on the first voice call, the plugin creates it in the `.env` of the agent's profile if it is missing. It never replaces an existing one.

Python dependency declared in the manifest: `numpy` 2.x. The Discord part relies on Hermes' Discord platform and its voice dependencies (`discord.py[voice]`).

## Discord

A voice channel routed to an agent is that agent's voice channel. One entry under `gateway.profile_routes` in the gateway's `config.yaml`:

```yaml
gateway:
  profile_routes:
    - name: <agent>-voice
      platform: discord
      guild_id: '<server id>'
      chat_id: '<voice channel id>'
      profile: <agent>
```

Voice applies the gateway's routes with its own rule: the routes loaded at startup, as for text channels. A new entry therefore counts after a gateway restart. An entry with `enabled: false`, restricted to a `user_id`, meant for another profile's bot (`bot_profile`) or for another server does not open a call: one call serves the whole channel, with the gateway's bot. If two entries target the same channel, the more specific one wins.

You join the channel: the agent arrives and the call starts. You leave: it leaves a few seconds later. `/talk` from an agent's text channel brings it into your voice channel, `/talk leave` hangs up.

Only accounts on the gateway's Discord allowlist (`DISCORD_ALLOWED_USERS` or roles) are heard, and only they can use `/talk` and `/talk leave`. `DISCORD_ALLOWED_CHANNELS` alone opens a text channel, not voice.

## Network prerequisite: outbound UDP

Discord voice runs over UDP. If the server gets no UDP replies from the internet, the bot joins the channel but never speaks. In the logs: `Voice handshake complete`, then `Timed out connecting to voice` 30 s later.

On a Hetzner Cloud VPS, the Cloud firewall is the one to open (the machine's own firewall can allow everything, that is not enough):

- Hetzner Cloud Console, Firewalls, the server's firewall.
- Outbound rules: protocol UDP, ports any (or `50000-65535`), destination Any IPv4 and Any IPv6.

Check from the server:

```bash
tailscale netcheck | grep UDP   # UDP: true
```

Without Tailscale, any STUN client (for example against `stun.cloudflare.com:3478`) must get a reply.

## What the plugin does on your machine

- **Audio**: during a call, what you say (Desktop microphone, or Discord voice channel) streams continuously to the chosen voice service: Google, OpenAI or xAI.
- **Identity**: the agent's name, the first paragraph of its `SOUL.md` and its `## Style` section also go to that service, capped at 40, 400 and 700 characters. Nothing else from the profile: neither the rest of `SOUL.md` nor memory.
- **Delegation**: when the voice hands a request to the agent, the request goes through the gateway's local API server (127.0.0.1), under Hermes' usual approvals. The request and the answer stay in the agent's sessions, like any Hermes conversation.
- **Discord**: a background task in the gateway watches the voice channels routed to an agent and brings the agent in when an allowed member arrives.
- **Files**: in the profile where the plugin is installed, `plugin-state/talk-with-hermes/prefs.json` (whether the voice says *tu* or *vous*, and the dictionary) and `resume.json` (the only session resumption token Gemini gives). The plugin writes neither audio nor transcripts to the server's disk.
- **Desktop**: the last 16 lines of the last call, 400 characters at most each, stay in the plugin's storage on your computer, to resume the conversation. No expiry for now.
- **Gemini free tier**: on the free tier of the Gemini API, Google uses the content sent and the responses to improve its products ([Gemini API terms](https://ai.google.dev/gemini-api/terms), Unpaid Services section). Worth knowing before passing sensitive data through it.

## Known limits

- The voice instructions are written in French. They tell the voice to answer in the language you just used. Only French has been used in real calls. The `languages` setting (default `fr-FR`) is a hint for transcription: set it to the codes of the languages you speak.
- Do not enable this plugin next to `hermes-talk` on the same Hermes: both declare `/talk`, and the last one loaded wins without warning.
- Hermes has no public surface yet for two things the plugin needs, imported privately: `gateway.run._async_profile_runtime_scope` and `gateway.run._gateway_runner_ref`. The Discord part also reads internals of Hermes' voice receiver. Nothing checks them during a call: the contract test (Tests section) checks them against the installed Hermes. Run it again after each Hermes update.
- If the gateway's API server listens on a specific address other than loopback, the key to the local API server travels there in clear (http). Keep it on 127.0.0.1.
- The text the agent sends back goes to the voice model. Hostile content read by the agent (a web page, for example) can push the voice to hand it a new request: you hear the announcement and the answer, not the request itself. Hermes' approvals remain the barrier.
- Desktop: if sending a request to the agent fails, it is retried in a new session, and can then run twice.
- OpenAI Realtime and Grok Voice: a request you cancel keeps running, and its answer is sent to the model.

## Security

A static security scan (hermes-security) covered an earlier, private version (`ac8baef`): status `partial` (all 28 files in scope reviewed, but two finding records left incomplete), 25 findings, 16 low and 9 informational, none medium or higher. This version fixes 11 of them, the limits above declare 5, and the other 9, low or informational, are hardening in progress. A new scan of this published version is planned before the submission to the Hermes catalog.

To report a vulnerability: privately, from this repository's Security tab, "Report a vulnerability". No public issues.

## Tests

```bash
pytest
```

The contract test against the host's internals is skipped when Hermes cannot be imported. To run it against the installed Hermes, with the interpreter that runs the gateway (`<python>`: Hermes' Python, `<hermes>`: its source checkout, `<pytest>`: a folder where pytest is installed for that Python):

```bash
HERMES_HOME="$(mktemp -d)" TWH_HOST_CONTRACT=1 <python> -I -c "
import os, sys
sys.path.insert(0, '<hermes>'); import hermes_bootstrap
sys.path[:0] = ['<pytest>', os.getcwd()]; import pytest
sys.exit(pytest.main(['-q', 'tests']))"
```

`hermes_bootstrap` loads Hermes' dependencies the way the gateway does at startup. With `TWH_HOST_CONTRACT=1`, a Hermes that cannot be found fails the test instead of skipping it. The throwaway `HERMES_HOME` keeps the suite away from the profile's keys.

## License

MIT, see [`LICENSE`](LICENSE). Code adapted and ideas taken from other projects: [`NOTICE.md`](NOTICE.md).
