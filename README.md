# triggerdirector

Turns camera detections into interrupt videos on halloween/digital-signage displays.

UniFi Protect (Alarm Manager) calls this service's webhook when a camera sees a person
lingering; the director decides whether that should fire — which displays that camera
drives, which clip they should play, and how long to ignore repeat detections — and then
tells each display's [vlclooper](https://github.com/mortms/vlclooper) to play an interrupt.

Displays play video and nothing else. All the policy is here.

```
Protect camera ──webhook──▶ director ──POST /trigger──▶ vlclooper (porch)
                                    └──POST /trigger──▶ vlclooper (yard)
```

## Running

Python 3.11+, standard library only — no venv or dependencies needed.

```bash
python3 director.py                  # uses director.txt next to the script
python3 director.py --config /path/to/director.txt
python3 director.py --check          # validate config, report display reachability, exit
```

Then point a Protect webhook at `http://<host>:8090/webhook?camera=front_porch` and open
`http://<host>:8090/` for status.

## Endpoints

| Route | Purpose |
| --- | --- |
| `GET`/`POST` `/webhook?camera=<name>` | A detection. Always 200 with `{"fired": bool, "reason": ..., "displays": [...]}` — an ignored detection is a normal outcome, not something Protect should retry. |
| `GET /` | Status page: displays and whether they're reachable, cameras and their mappings, recent detections. |
| `GET /status.json` | The same as JSON. |
| `POST /reload` | Re-read the config. A broken config is refused and the running one kept. |

## Configuration

See [director.txt](director.txt). Cameras map to displays, so one camera can drive several
displays and one display can serve several cameras.

## Deploying to a Pi

```bash
git clone <this repo> /home/pi/triggerdirector
cd /home/pi/triggerdirector && cp director.txt director.txt.local  # then edit
sudo cp triggerdirector.service /etc/systemd/system/
sudo systemctl enable --now triggerdirector
journalctl -u triggerdirector -f
```
