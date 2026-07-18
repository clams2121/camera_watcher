# Deploying camera_watcher as a systemd fleet

One `camera-watcher@<name>.service` instance per camera, plus one shared
`camera-retention.timer` sweeping every camera's clips against a single
fleet-wide storage budget. This directory's unit files assume the layout
below; adjust the paths in them (and in the commands here) if yours differs.

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

`camera-retention.service`'s `ExecStart` bakes in a default
`--max-age-days 14 --max-total-gb 500` -- edit that line directly, or
override without touching the tracked file via a drop-in:

```bash
sudo systemctl edit camera-retention.service
```

Retention deletes the globally oldest clips first once the shared budget is
exceeded (not per camera, so one busy camera can't starve a quiet one), with
one seam already wired in for later: `classify_tier()` in
`camera_watcher/retention.py` currently always returns `"unclassified"`, but
once a clip classifier exists (see the project roadmap) its verdict will
feed into this same sweep to prefer deleting low-value clips first.

## 5. Hardening notes

Both unit files run as the unprivileged `camera-watcher` user with
`ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`, and an
explicit `ReadWritePaths=` allowlist -- everything else on the filesystem is
read-only to these services. If a service fails to start and the journal
points at a hardening directive (most likely candidate:
`MemoryDenyWriteExecute=true` in `camera-watcher@.service`, which some
native library JIT paths can conflict with, though OpenCV/NumPy/ffmpeg
don't), comment out that one line rather than loosening everything at once,
so you know exactly what was needed.

`camera-watcher@.service` restarts on failure with `RestartSec=5`, capped at
5 restarts per 5 minutes (`StartLimitIntervalSec`/`StartLimitBurst`) -- past
that it stops retrying and `systemctl status` shows `start-limit-hit`,
rather than spinning forever against a camera that's genuinely down.
`systemctl reset-failed camera-watcher@<name>.service` clears that state
once you've fixed the underlying problem.

## 6. Adding/removing a camera later

Adding: repeat step 3 for the new camera, then
`sudo systemctl enable --now camera-watcher@<name>.service`. No changes
needed to the retention timer -- it already sweeps every subdirectory under
the shared `data_root`.

Removing: `sudo systemctl disable --now camera-watcher@<name>.service`, then
delete `/opt/camera-watcher/config/<name>.{yaml,secrets.yaml,mask.json}` and
that camera's clips under `/var/lib/camera-watcher/clips/<name>/` if you
want them gone too (neither is automatic).
