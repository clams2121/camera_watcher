# Deploying camera_watcher as a systemd service

One `camera-watcher.service` for the whole fleet -- the always-on
supervisor + web UI. Every camera is added, edited, and removed entirely
through that UI once it's running, not by hand-editing config files or
enabling per-camera systemd units. Retention runs inside the same process
on a schedule (also configured from the UI). The only other unit is the
optional, separate `clip-classifier.service` -- see step 4. This
directory's unit files assume the layout below; adjust the paths in them
(and in the commands here) if yours differs.

| Path | Purpose |
| --- | --- |
| `/opt/camera-watcher` | Git checkout + `.venv` (code, read-only to the service at runtime) |
| `/opt/camera-watcher/config` | `fleet.yaml` / `fleet.secrets.yaml` / `cameras/<name>.yaml` / `cameras/<name>.secrets.yaml` / `cameras/<name>.mask.json` / `classifier.yaml` -- all created and rewritten by the web UI (writable) |
| `/var/lib/camera-watcher` | `data_root` -- every camera's clips, metadata sidecars, passthrough segment cache, and motion heatmap, all under one shared root (writable) |

## 1. Prerequisites (per host)

- Python 3 with `venv`, `ffmpeg`/`ffprobe` on `PATH`, and
  [Tailscale](https://tailscale.com/download) installed and logged in
  (`tailscale up`) -- `web.host: tailscale` (the default) fails startup
  loud if it isn't.
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
sudo -u camera-watcher mkdir -p /opt/camera-watcher/config
```

Nothing else needs creating by hand -- `config/fleet.yaml` and
`config/fleet.secrets.yaml` (with a freshly generated auth token) are
created automatically the first time the service starts.

## 3. Install and start the service

```bash
sudo cp deploy/camera-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-watcher.service
systemctl status camera-watcher.service
journalctl -u camera-watcher.service -f
```

The first-boot log line prints the generated auth token exactly once:

```
======================================================================
No auth token was configured -- generated a new one.
Log in to the web UI with this token (also saved in .../fleet.secrets.yaml):

    <the token>

Change it any time from Fleet Settings once logged in.
======================================================================
```

Copy that token, browse to `http://<tailscale-ip>:8080/login` (find the IP
with `tailscale ip -4` on this host), and log in. Everything from here --
adding cameras, tuning motion/recording settings, drawing ignore masks,
setting fleet-wide retention windows, editing the classifier's config, and
rotating the auth token -- happens in the UI. See the root README's
"Configuring a camera" and "Retention" sections for what each setting
does; there's nothing left to configure by hand-editing YAML.

## 4. Optional: deploy the clip classifier

`clip-classifier.service` runs `clip_classifier` (see the root README's
"Clip classifier (tier 1)" section) as a single instance watching the
*whole* shared `data_root` -- not one per camera, and deliberately with no
systemd `After=`/`Wants=` dependency on `camera-watcher.service`, so it
doesn't need the fleet supervisor up first and restarting/redeploying
either one never restarts or blocks the other.

Install its extra dependencies (on top of what step 2 already installed):

```bash
sudo -u camera-watcher /opt/camera-watcher/.venv/bin/pip install -r /opt/camera-watcher/requirements-classifier.txt
```

Then, from the fleet web UI's **Classifier** tab, save at least a backend
choice (`auto` by default) -- this creates `config/classifier.yaml` for
you, with `data_root` automatically pointed at the same shared root every
camera writes clips into.

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
the verdict rules, and each camera's own Recordings tab for the verdict
badges, filter, and human review buttons.

## 5. Retention

Retention runs inside `camera-watcher.service` itself, on a schedule --
there's no separate timer to install. Windows, the shared size budget, and
the sweep interval are all editable live from the UI's Fleet Settings tab,
and take effect on the *next* sweep without a restart. Verdict-aware: each
clip's `clip_classifier` verdict (and any human review decision, if it
landed in `review`) sorts it into a retention tier via `classify_tier()`
in `camera_watcher/retention.py` -- `low`, `review`, or `high` (also used
for `error`/not-yet-classified clips and `review` clips a human has kept,
so nothing gets treated as disposable just because it hasn't been looked
at). Each tier expires on its own age window; once the shared size budget
is still exceeded after that, `low` tier clips go first, oldest first,
then the oldest of whatever `high`/`review` clips remain -- logged loudly,
since that means budget pressure is deleting something otherwise
considered worth keeping. See the root README's "Retention" section for
the full tier/window breakdown.

Fleet Settings also has "Preview (dry run)" / "Run now" buttons that call
the same sweep on demand -- worth using once after changing any retention
setting, before trusting it to the schedule. The same sweep is also still
available as a standalone CLI for manual/diagnostic use against a clips
directory copied elsewhere:

```bash
sudo -u camera-watcher /opt/camera-watcher/.venv/bin/python -m camera_watcher.retention \
    --global /var/lib/camera-watcher/clips --dry-run
```

## 6. Hardening notes

Both unit files run as the unprivileged `camera-watcher` user with
`ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`, and an
explicit `ReadWritePaths=` allowlist -- everything else on the filesystem
is read-only to them. If a service fails to start and the journal points
at a hardening directive (most likely candidate: `MemoryDenyWriteExecute=true`,
present in both units, which some native library JIT paths can conflict
with -- the classifier's CPU backend (ONNX Runtime) is a plausible culprit
there, though OpenCV/NumPy/ffmpeg don't), comment out that one line rather
than loosening everything at once, so you know exactly what was needed. If
using the Hailo backend, `clip-classifier.service` will also need
`/dev/hailo0` reachable, which may require a `DeviceAllow=` addition
depending on your distro's udev/cgroup setup.

Both units restart automatically -- `camera-watcher.service` uses
`Restart=always` specifically so the web UI's "Stop Server" button doubles
as a "restart to apply a bind/token change" button; `clip-classifier.service`
uses `Restart=on-failure`, since it has no equivalent live-editable bind
setting. Both are capped at 5 restarts per 5 minutes
(`StartLimitIntervalSec`/`StartLimitBurst`) -- past that they stop
retrying and `systemctl status` shows `start-limit-hit`, rather than
spinning forever against something genuinely broken.
`systemctl reset-failed <unit>` clears that state once you've fixed the
underlying problem.

## 7. Adding/removing a camera, and updating

Adding and removing cameras is entirely a UI action now (Add Camera tab;
each camera's own Settings tab has a Remove button) -- no systemd or
config-file steps on this host at all.

Updating: `update.sh` at the repo root handles the whole install at once
-- run it as the `camera-watcher` user from `/opt/camera-watcher`:

```bash
sudo -u camera-watcher /opt/camera-watcher/update.sh
```

It fetches, fast-forwards (refusing to run at all against a dirty tree or
a diverged branch), reinstalls dependencies, then restarts
`camera-watcher.service` -- plus `clip-classifier.service`, and its own
extra dependencies from `requirements-classifier.txt`, if that's deployed
on this host too -- and reports each one's resulting status. See the root
README's "Updating" section for the full behavior and failure modes.
Since it calls `sudo systemctl restart` per unit, either run the whole
script as root instead, or grant the `camera-watcher` user passwordless
sudo scoped to just those commands (e.g. via `visudo`:
`camera-watcher ALL=(root) NOPASSWD: /usr/bin/systemctl restart camera-watcher.service,
/usr/bin/systemctl restart clip-classifier.service`).
