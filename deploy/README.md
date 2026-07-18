# Deploying camera_watcher as a systemd fleet

One `camera-watcher@<name>.service` instance per camera, one shared
`camera-retention.timer` sweeping every camera's clips against a single
fleet-wide storage budget, and (optionally) one `clip-classifier.service`
instance -- watching the whole shared `data_root`, not per camera --
running the tier-1 clip classifier. This directory's unit files assume the
layout below; adjust the paths in them (and in the commands here) if yours
differs.

| Path | Purpose |
| --- | --- |
| `/opt/camera-watcher` | Git checkout + `.venv` (code, read-only to the service at runtime) |
| `/opt/camera-watcher/config` | Per-camera `<name>.yaml` / `<name>.secrets.yaml` / `<name>.mask.json` (writable -- the web UI's Settings/Ignore Mask tabs rewrite these) |
| `/var/lib/camera-watcher` | `data_root` -- clips, metadata sidecars, the passthrough segment cache, the motion heatmap (writable) |

## 1. Prerequisites (per host)

- Python 3 with `venv`, `ffmpeg`/`ffprobe` on `PATH`, and
  [Tailscale](https://tailscale.com/download) installed and logged in
  (`tailscale up`) -- `web.host: tailscale` (the default) fails every
  camera's startup loud if it isn't.
- A dedicated, unprivileged system user to run everything as:

  ```bash
  sudo useradd --system --home-dir /opt/camera-watcher --shell /usr/sbin/nologin camera-watcher
  sudo mkdir -p /var/lib/camera-watcher
  sudo chown camera-watcher:camera-watcher /var/lib/camera-watcher
  ```

## 2. Install the code

```bash
sudo git clone https://github.com/clams2121/camera_watcher.git /opt/camera-watcher
sudo chown -R camera-watcher:camera-watcher /opt/camera-watcher
sudo -u camera-watcher python3 -m venv /opt/camera-watcher/.venv
sudo -u camera-watcher /opt/camera-watcher/.venv/bin/pip install -r /opt/camera-watcher/requirements.txt
```

## 3. Configure each camera

Same as the root README's "Configuring a camera," just rooted at
`/opt/camera-watcher` and pointed at the shared data root:

```bash
sudo -u camera-watcher cp /opt/camera-watcher/config/camera.example.yaml /opt/camera-watcher/config/front-door.yaml
sudo -u camera-watcher cp /opt/camera-watcher/config/camera.secrets.example.yaml /opt/camera-watcher/config/front-door.secrets.yaml
```

Edit `front-door.yaml`: at minimum `camera.name` (must be `front-door` here
-- it has to match the systemd instance name below), `camera.host`, and
`data_root: /var/lib/camera-watcher` (overriding the relative `data`
default, so this camera's clips land in the shared root the retention timer
sweeps). Edit `front-door.secrets.yaml`: camera credentials, and a generated
`web.auth_token` (see the root README's "Authentication" section --
required, startup fails loud without one).

Repeat for every camera, each with a unique `camera.name` (matching its
config filename) and a unique `web.port`.

## 4. Install the systemd units

```bash
sudo cp deploy/camera-watcher@.service deploy/camera-retention.service deploy/camera-retention.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Start one camera (the instance name after `@` must match
`config/<name>.yaml`, without the extension):

```bash
sudo systemctl enable --now camera-watcher@front-door.service
systemctl status camera-watcher@front-door.service
journalctl -u camera-watcher@front-door.service -f
```

Repeat `enable --now camera-watcher@<name>.service` for each camera.

Start the fleet-wide retention timer once (not per camera):

```bash
sudo systemctl enable --now camera-retention.timer
systemctl list-timers camera-retention.timer   # confirm it's scheduled
```

`camera-retention.service`'s `ExecStart` bakes in defaults of
`--low-max-age-hours 48 --high-max-age-days 30 --review-max-age-days 30
--max-total-gb 500` -- edit that line directly, or override without
touching the tracked file via a drop-in:

```bash
sudo systemctl edit camera-retention.service
```

Retention is verdict-aware: each clip's `clip_classifier` verdict (and any
human review decision, if the clip landed in `review`) sorts it into a
retention tier via `classify_tier()` in `camera_watcher/retention.py` --
`low`, `review`, or `high` (also used for `error`/not-yet-classified clips
and `review` clips a human has kept, so nothing gets treated as disposable
just because it hasn't been looked at). Each tier expires on its own age
window; once the shared size budget is still exceeded after that, `low`
tier clips go first, oldest first, then the oldest of whatever `high`/
`review` clips remain -- logged loudly, since that means budget pressure
is deleting something otherwise considered worth keeping. See the root
README's "Retention" section for the full tier/window breakdown.

Before trusting any of this to the timer, dry-run it once with the exact
flags from the unit file and read what it says it would do:

```bash
sudo -u camera-watcher /opt/camera-watcher/.venv/bin/python -m camera_watcher.retention \
    --global /var/lib/camera-watcher/clips \
    --low-max-age-hours 48 --high-max-age-days 30 --review-max-age-days 30 \
    --max-total-gb 500 --dry-run
```

## 5. Optional: deploy the clip classifier

`clip-classifier.service` runs `clip_classifier` (see the root README's
"Clip classifier (tier 1)" section) as a single instance watching the
*whole* shared `data_root` -- not one per camera, and deliberately with no
systemd `After=`/`Wants=` dependency on any `camera-watcher@*.service`, so
it doesn't need the recorders up first and restarting/redeploying a camera
never restarts or blocks it.

Install its extra dependencies (on top of what step 2 already installed)
and set up its config:

```bash
sudo -u camera-watcher /opt/camera-watcher/.venv/bin/pip install -r /opt/camera-watcher/requirements-classifier.txt
sudo -u camera-watcher cp /opt/camera-watcher/config/classifier.example.yaml /opt/camera-watcher/config/classifier.yaml
```

Edit `classifier.yaml`: at minimum `data_root: /var/lib/camera-watcher`
(the same shared root every camera writes clips into) and `backend` (`auto`
by default -- uses the Hailo-8L if `/dev/hailo0` and its runtime are both
present, otherwise falls back to the CPU/ONNX Runtime backend, loudly
either way).

The CPU backend needs a real model file fetched once (see the root
README's "Detector backends" section for why `fetch_model.py` ships with
no checksum baked in -- you pin it yourself, from a network that can
actually reach the model host):

```bash
sudo -u camera-watcher /opt/camera-watcher/.venv/bin/python -m clip_classifier.fetch_model \
    --config /opt/camera-watcher/config/classifier.yaml
```

Install and start the unit:

```bash
sudo cp deploy/clip-classifier.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clip-classifier.service
systemctl status clip-classifier.service
journalctl -u clip-classifier.service -f
```

It backfills every already-finalized clip under `data_root` that doesn't
have an `.analysis.json` sidecar yet on startup, then watches for new ones
as cameras record them -- see the root README for the sidecar schema and
the verdict rules.

## 6. Hardening notes

All three unit files (camera, retention, classifier) run as the
unprivileged `camera-watcher` user with `ProtectSystem=strict`,
`ProtectHome=true`, `NoNewPrivileges=true`, and an explicit
`ReadWritePaths=` allowlist -- everything else on the filesystem is
read-only to these services. If a service fails to start and the journal
points at a hardening directive (most likely candidate:
`MemoryDenyWriteExecute=true`, present in both `camera-watcher@.service`
and `clip-classifier.service`, which some native library JIT paths can
conflict with -- the CPU backend's ONNX Runtime is a plausible culprit for
the classifier specifically, though OpenCV/NumPy/ffmpeg don't), comment out
that one line rather than loosening everything at once, so you know
exactly what was needed. If using the Hailo backend, `clip-classifier.service`
will also need `/dev/hailo0` reachable, which may require a
`DeviceAllow=` addition depending on your distro's udev/cgroup setup.

`camera-watcher@.service` and `clip-classifier.service` both restart on
failure with `RestartSec=5`, capped at 5 restarts per 5 minutes
(`StartLimitIntervalSec`/`StartLimitBurst`) -- past that they stop
retrying and `systemctl status` shows `start-limit-hit`, rather than
spinning forever against a camera (or model) that's genuinely broken.
`systemctl reset-failed <unit>` clears that state once you've fixed the
underlying problem.

## 7. Adding/removing a camera later

Adding: repeat step 3 for the new camera, then
`sudo systemctl enable --now camera-watcher@<name>.service`. No changes
needed to the retention timer or the classifier -- both already sweep/watch
every subdirectory under the shared `data_root`.

Removing: `sudo systemctl disable --now camera-watcher@<name>.service`, then
delete `/opt/camera-watcher/config/<name>.{yaml,secrets.yaml,mask.json}` and
that camera's clips under `/var/lib/camera-watcher/clips/<name>/` if you
want them gone too (neither is automatic).

## 8. Updating

`update.sh` at the repo root handles the whole fleet at once -- run it as
the `camera-watcher` user from `/opt/camera-watcher`:

```bash
sudo -u camera-watcher /opt/camera-watcher/update.sh
```

It fetches, fast-forwards (refusing to run at all against a dirty tree or a
diverged branch), reinstalls dependencies, then restarts every
`camera-watcher@*` instance it finds via `systemctl` -- plus
`clip-classifier.service`, and its own extra dependencies from
`requirements-classifier.txt`, if that's deployed on this host too -- and
reports each one's resulting status. See the root README's "Updating"
section for the full behavior and failure modes. Since it calls `sudo
systemctl restart` per instance, either run the whole script as root
instead, or grant the `camera-watcher` user passwordless sudo scoped to
just that command (e.g. via `visudo`: `camera-watcher ALL=(root) NOPASSWD:
/usr/bin/systemctl restart camera-watcher@*, /usr/bin/systemctl restart
clip-classifier.service`).
