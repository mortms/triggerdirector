# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Overview

`triggerdirector` receives camera webhooks (UniFi Protect Alarm Manager) and fans them out
as interrupt requests to [vlclooper](https://github.com/mortms/vlclooper) displays.

The split is deliberate: **displays play video and decide nothing.** Which camera drives
which display, which clip to play, and how long to ignore repeat detections all live here.
That keeps the N-to-M mapping in one version-controlled file instead of spread across
Protect alarm actions, and gives cross-display coordination somewhere to live later.

## Running

Python 3.11+, standard library only — no venv, no dependencies.

```bash
python3 director.py --config director.txt
python3 director.py --check          # validate config + display reachability, exit non-zero if any are down
```

## Architecture

**`director.py`** is the whole service.

- `load_config()` parses `director.txt` and *validates the graph*: a camera naming an
  unknown display, or no displays at all, raises rather than silently never firing. This
  is the opposite of vlclooper's "warn and carry on" stance, and deliberately so — the
  director isn't what's on screen, so failing loudly at start beats a decoration that
  quietly never triggers.
- `Director.handle()` holds the policy: unknown camera → ignored, inside the per-camera
  cooldown → ignored, otherwise fan out.
- `Director._fan_out()` posts to every mapped display **in parallel**, each with
  `DISPLAY_TIMEOUT`, so one unplugged Pi can't delay the others. A display being
  unreachable is normal (switched off out of season) and is recorded, not raised.
- `Director.health_loop()` polls each display's `/status.json` every `HEALTH_INTERVAL`
  so the page can show what's reachable and what each is playing.
- `history` (a deque) keeps recent detections, fired or not, with the reason — this is
  the data you tune dwell thresholds against.

**`status.html`** is a static page polling `/status.json` once a second. Inline CSS/JS
only, no external assets, so it works on an isolated network. It shares vlclooper's
palette and type so the two pages look like one system.

## Endpoints

- `GET`/`POST` `/webhook?camera=<name>` — a detection. Both verbs, because Protect's
  default webhook is a GET. **Always 200** unless `camera` is missing (400): an ignored
  detection is a normal outcome, not something Protect should retry.
- `GET /` and `/status.json` — status page and its data.
- `POST /reload` — re-read the config; a broken one is refused and the running config kept.

Requests to `/` and `/status.json` aren't logged, so the journal stays readable — webhooks
and errors are what matter there.

## Configuration (`director.txt`)

`[displays]` maps a name to a vlclooper base URL. `[camera:<name>]` sections map a camera
to the displays it drives, optionally pinning a `clip` and overriding `cooldown`
(defaulting to `[triggers] cooldown`). The camera name must match what the webhook sends
as `?camera=`.

## Protect setup

Alarm Manager → alarm with a person-detection trigger and a loitering (dwell) condition →
Webhook action → `http://<director-host>:8090/webhook?camera=<name>`.

Use the host's **DNS name, not an IP and not `.local`**: a UniFi gateway resolves its DHCP
clients by name (and by FQDN under the site's search domain), so `videoplayer4` and
`videoplayer4.sf.jharding.org` both work from anything using the gateway as its resolver,
while `.local` (mDNS) generally does not resolve on the Protect console. Give the host a
DHCP reservation so the name keeps pointing at the same machine.

## Future work

**Synchronised playback across displays.** The fan-out is the natural place: rather than
"play now", send each display a clip and a wall-clock start time, and have vlclooper honour
a scheduled trigger. Needs the Pis clock-synced (they run NTP by default).
