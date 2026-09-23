#! /usr/bin/env python3

# Trigger director for video decorations.
#
# Receives camera webhooks (UniFi Protect Alarm Manager) and fans them out to the
# displays that camera drives, as interrupt requests to each vlclooper's HTTP API.
# All the policy lives here: which camera drives which display, which clip to play,
# and how long to ignore repeat detections. The displays themselves just play video.

import argparse
import configparser
import json
import os
import signal
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

DEFAULT_PORT = 8090
DEFAULT_COOLDOWN = 45
DISPLAY_TIMEOUT = 3          # seconds to wait on a display before giving up on it
HEALTH_INTERVAL = 5          # seconds between display reachability checks
HISTORY = 50                 # webhooks kept for the status page


def log(message):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def load_config(path):
    """Read director.txt into {port, displays, cameras}. Raises if unusable."""
    config = configparser.ConfigParser()
    config.optionxform = str  # camera and display names keep their case
    if not config.read(path):
        raise FileNotFoundError("Config file not found: " + path)

    displays = {}
    for name, url in config.items('displays') if config.has_section('displays') else []:
        if name in config.defaults():
            continue
        displays[name] = url.strip().rstrip('/')

    default_cooldown = config.getfloat('triggers', 'cooldown', fallback=DEFAULT_COOLDOWN)

    cameras = {}
    for section in config.sections():
        if not section.startswith('camera:'):
            continue
        name = section.split(':', 1)[1].strip()
        targets = [d.strip() for d in config.get(section, 'displays', fallback='').replace(',', '\n').split() if d.strip()]
        unknown = [d for d in targets if d not in displays]
        if unknown:
            raise ValueError("Camera %r targets unknown display(s) %s. Known: %s"
                             % (name, ", ".join(unknown), ", ".join(sorted(displays)) or "none"))
        if not targets:
            raise ValueError("Camera %r lists no displays, so a trigger would do nothing" % name)
        cameras[name] = {
            'displays': targets,
            'clip': config.get(section, 'clip', fallback='').strip() or None,
            'cooldown': config.getfloat(section, 'cooldown', fallback=default_cooldown),
        }

    if not cameras:
        raise ValueError("No [camera:NAME] sections in " + path + ", so nothing could ever trigger")

    return {
        'port': config.getint('server', 'port', fallback=DEFAULT_PORT),
        'displays': displays,
        'cameras': cameras,
    }


class Director:
    """Applies trigger policy and fans out to displays."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.started_at = time.time()
        self.history = deque(maxlen=HISTORY)
        self.health = {}                 # display name -> {ok, detail, checked}
        self._last_fired = {}            # camera name -> monotonic time
        self._lock = threading.Lock()

    # --- policy ---------------------------------------------------------------

    def handle(self, camera):
        """Decide whether a camera detection should fire, and if so tell the displays."""
        cameras = self.config['cameras']
        if camera not in cameras:
            return self._record(camera, False, "unknown camera; known: %s" % ", ".join(sorted(cameras)), [])

        settings = cameras[camera]
        with self._lock:
            remaining = settings['cooldown'] - (time.monotonic() - self._last_fired.get(camera, -1e9))
            if remaining > 0:
                return self._record(camera, False, "cooldown, %.0fs remaining" % remaining, [])
            self._last_fired[camera] = time.monotonic()

        results = self._fan_out(camera, settings)
        played = sum(1 for r in results if r.get('triggered'))
        return self._record(camera, True, "sent to %d display(s), %d played" % (len(results), played), results)

    def _fan_out(self, camera, settings):
        """Ask every display this camera drives to play an interrupt, in parallel."""
        results = [None] * len(settings['displays'])
        threads = []
        for i, display in enumerate(settings['displays']):
            t = threading.Thread(target=self._tell_display, args=(results, i, display, camera, settings['clip']))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(DISPLAY_TIMEOUT + 2)
        return [r for r in results if r]

    def _tell_display(self, results, index, display, camera, clip):
        url = "%s/trigger?source=%s" % (self.config['displays'][display], camera)
        if clip:
            url += "&clip=" + clip
        entry = {'display': display, 'ok': False, 'triggered': False}
        try:
            with urlopen(Request(url, method='POST'), timeout=DISPLAY_TIMEOUT) as response:
                body = json.loads(response.read().decode())
            entry.update(ok=True, triggered=bool(body.get('triggered')), reason=body.get('reason'))
        except (URLError, OSError, ValueError) as e:
            # A display being off is normal (unplugged for the season, rebooting).
            entry['reason'] = "%s: %s" % (type(e).__name__, e)
        results[index] = entry
        return entry

    def _record(self, camera, fired, reason, results):
        event = {'at': time.time(), 'camera': camera, 'fired': fired, 'reason': reason, 'displays': results}
        self.history.appendleft(event)
        detail = "; ".join("%s %s" % (r['display'], "played" if r['triggered'] else (r.get('reason') or "ignored"))
                           for r in results)
        log("webhook %s: %s (%s)%s" % (camera, "fired" if fired else "ignored", reason, " -- " + detail if detail else ""))
        return event

    # --- housekeeping ---------------------------------------------------------

    def check_health(self):
        """Poll every display's status in parallel so the page can show what's reachable.

        In parallel because a display that is off costs DISPLAY_TIMEOUT to find out, and
        sequential checks would make every other display's reading that much staler.
        """
        threads = [threading.Thread(target=self._check_display, args=(name, base))
                   for name, base in self.config['displays'].items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(DISPLAY_TIMEOUT + 2)

    def _check_display(self, name, base):
        entry = {'checked': time.time(), 'ok': False}
        try:
            with urlopen(base + "/status.json", timeout=DISPLAY_TIMEOUT) as response:
                body = json.loads(response.read().decode())
            current = body.get('current') or {}
            entry.update(ok=True, state=body.get('state'), playing=current.get('name'))
        except (URLError, OSError, ValueError) as e:
            entry['detail'] = "%s: %s" % (type(e).__name__, e)
        self.health[name] = entry

    def health_loop(self, stop):
        while not stop.wait(HEALTH_INTERVAL):
            self.check_health()

    def reload(self):
        """Re-read the config, keeping the running one if the new one is unusable."""
        config = load_config(self.config_path)
        if config['port'] != self.config['port']:
            log("note: [server] port changed to %d; that needs a restart" % config['port'])
        self.config = config
        self.check_health()
        log("reloaded %s: %d camera(s), %d display(s)"
            % (self.config_path, len(config['cameras']), len(config['displays'])))
        return config

    def status(self):
        return {
            'displays': {name: dict(self.health.get(name, {}), url=url)
                         for name, url in self.config['displays'].items()},
            'cameras': {name: {'displays': c['displays'], 'clip': c['clip'], 'cooldown': c['cooldown'],
                               'ready_in': max(0, round(c['cooldown'] - (time.monotonic() - self._last_fired.get(name, -1e9))))}
                        for name, c in self.config['cameras'].items()},
            'history': list(self.history),
            'uptime': time.time() - self.started_at,
        }


class DirectorHandler(BaseHTTPRequestHandler):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.html"), "rb") as f:
        _page = f.read()

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, "application/json", json.dumps(payload).encode())

    def webhook(self, request):
        camera = parse_qs(request.query).get("camera", [None])[0]
        if not camera:
            self._json(400, {'error': "missing ?camera=<name>"})
            return
        event = self.server.director.handle(camera)
        # Always 200: an ignored detection is a normal outcome, not a webhook failure.
        self._json(200, {'fired': event['fired'], 'reason': event['reason'],
                         'displays': [{k: r.get(k) for k in ('display', 'ok', 'triggered')} for r in event['displays']]})

    def do_GET(self):
        request = urlparse(self.path)
        match request.path:
            case '/' | '/status': self._send(200, "text/html; charset=utf-8", self._page)
            case '/status.json': self._json(200, self.server.director.status())
            case '/webhook': self.webhook(request)
            case _: self._send(404, "text/plain; charset=utf-8",
                               ("No page at %s. Try / or /webhook?camera=NAME." % self.path).encode())

    def do_POST(self):
        request = urlparse(self.path)
        match request.path:
            case '/webhook': self.webhook(request)
            case '/reload':
                try:
                    config = self.server.director.reload()
                except Exception as e:
                    self._json(400, {'error': "%s: %s" % (type(e).__name__, e)})
                else:
                    self._json(200, {'cameras': len(config['cameras']), 'displays': len(config['displays'])})
            case _: self._send(404, "text/plain; charset=utf-8", b"Not found")

    def log_message(self, format, *args):
        # The status page polls once a second; logging that would bury the webhooks.
        if urlparse(getattr(self, "path", "")).path not in ('/status.json', '/'):
            log("%s %s" % (self.address_string(), format % args))


def main():
    parser = argparse.ArgumentParser(description="Fan out camera detections to video displays.")
    parser.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'director.txt'),
                        help="config file to read (default: director.txt next to this script)")
    parser.add_argument('--check', action='store_true',
                        help="validate the config and report display reachability, then exit")
    args = parser.parse_args()

    director = Director(args.config)
    for name, settings in sorted(director.config['cameras'].items()):
        log("camera %s -> %s (clip %s, cooldown %gs)"
            % (name, ", ".join(settings['displays']), settings['clip'] or "random", settings['cooldown']))
    director.check_health()
    for name, entry in sorted(director.health.items()):
        log("display %s at %s: %s" % (name, director.config['displays'][name],
                                      "playing %s" % entry.get('playing') if entry['ok'] else entry.get('detail')))
    if args.check:
        return 0 if all(e['ok'] for e in director.health.values()) else 1

    server = ThreadingHTTPServer(("", director.config['port']), DirectorHandler)
    server.daemon_threads = True
    server.director = director

    stop = threading.Event()
    threading.Thread(target=director.health_loop, args=(stop,), daemon=True).start()

    def handle_signal(signum, frame):
        log("shutting down")
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log("listening on port %d" % director.config['port'])
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
