(function () {
  "use strict";

  // ---------- Stop server ----------
  document.getElementById("stop-server").addEventListener("click", () => {
    const typed = window.prompt(
      'This stops the camera_watcher fleet supervisor (every camera + the UI). Type "quit" to confirm:'
    );
    if (typed === null) return; // cancelled
    if (typed.trim().toLowerCase() !== "quit") {
      window.alert('Not confirmed -- you must type exactly "quit". Nothing was stopped.');
      return;
    }

    fetch("/api/shutdown", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "quit" }),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (ok && data.ok) {
          document.getElementById("shutdown-banner").hidden = false;
          document.getElementById("stop-server").disabled = true;
        } else {
          window.alert((data && data.error) || "Failed to stop the server.");
        }
      })
      .catch(() => {
        // The server may have already dropped the connection while
        // stopping -- treat that as success rather than an error.
        document.getElementById("shutdown-banner").hidden = false;
        document.getElementById("stop-server").disabled = true;
      });
  });

  // ---------- Logout ----------
  document.getElementById("logout").addEventListener("click", () => {
    fetch("/api/logout", { method: "POST" }).finally(() => {
      window.location.href = "/login";
    });
  });

  const _fetch = window.fetch;
  window.fetch = function (...args) {
    return _fetch.apply(this, args).then((response) => {
      if (response.status === 401 && !String(args[0]).startsWith("/api/login")) {
        window.location.href = "/login";
      }
      return response;
    });
  };

  // ---------- Tabs ----------
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("tab-" + btn.dataset.tab).classList.add("active");

      if (btn.dataset.tab === "cameras") loadCameras();
      if (btn.dataset.tab === "fleet-settings") loadFleetSettings();
      if (btn.dataset.tab === "classifier") loadClassifierSettings();
    });
  });

  // ---------- Camera list ----------
  const cameraListEl = document.getElementById("camera-list");

  function statusBadgeFor(camera) {
    const badge = document.createElement("span");
    if (camera.error) {
      badge.className = "status-badge bad";
      badge.textContent = "error";
      badge.title = camera.error;
    } else {
      const dot = camera.connected ? "connected" : "disconnected";
      badge.className = "status-badge " + (camera.connected ? "ok" : "bad") + (camera.recording ? " rec" : "");
      badge.textContent = dot + (camera.recording ? " • recording" : "");
    }
    return badge;
  }

  function buildCameraRow(camera) {
    const li = document.createElement("li");
    li.className = "recording-item";

    const link = document.createElement("a");
    link.className = "recording-info camera-link";
    link.href = "/cameras/" + encodeURIComponent(camera.id);
    link.textContent = camera.name + (camera.host ? ` (${camera.host})` : "");

    li.appendChild(statusBadgeFor(camera));
    li.appendChild(link);
    return li;
  }

  function loadCameras() {
    cameraListEl.innerHTML = '<li class="hint">Loading...</li>';
    fetch("/api/cameras")
      .then((r) => r.json())
      .then((data) => {
        const cameras = data.cameras || [];
        cameraListEl.innerHTML = "";
        if (cameras.length === 0) {
          cameraListEl.innerHTML = '<li class="hint">No cameras yet -- add one from the "Add Camera" tab.</li>';
          return;
        }
        cameras.forEach((camera) => cameraListEl.appendChild(buildCameraRow(camera)));
      })
      .catch(() => {
        cameraListEl.innerHTML = '<li class="hint">Failed to load cameras.</li>';
      });
  }

  document.getElementById("refresh-cameras").addEventListener("click", loadCameras);
  loadCameras();
  setInterval(() => {
    if (document.getElementById("tab-cameras").classList.contains("active")) loadCameras();
  }, 5000);

  // ---------- Add camera ----------
  const addCameraForm = document.getElementById("add-camera-form");

  function collectAddCameraForm() {
    const settings = {};
    const credentials = {};
    for (const el of addCameraForm.elements) {
      if (!el.name) continue;
      const value = el.type === "number" ? (el.value === "" ? null : Number(el.value)) : el.value;
      if (el.name.startsWith("credentials.")) {
        credentials[el.name.split(".")[1]] = value;
        continue;
      }
      const [section, key] = el.name.split(".");
      settings[section] = settings[section] || {};
      settings[section][key] = value;
    }
    return { settings, credentials };
  }

  addCameraForm.addEventListener("submit", (evt) => {
    evt.preventDefault();
    const { settings, credentials } = collectAddCameraForm();
    const statusEl = document.getElementById("add-camera-status");
    statusEl.textContent = "Adding...";
    fetch("/api/cameras", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings, credentials }),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (!ok || !data.ok) {
          statusEl.textContent = "Error: " + ((data && data.error) || "failed to add the camera.");
          return;
        }
        window.location.href = "/cameras/" + encodeURIComponent(data.camera.id);
      })
      .catch(() => {
        statusEl.textContent = "Error adding the camera.";
      });
  });

  // ---------- Fleet settings ----------
  const fleetSettingsForm = document.getElementById("fleet-settings-form");
  let fleetSettingsLoaded = false;

  function applyFleetSettingsToForm(settings) {
    for (const el of fleetSettingsForm.elements) {
      if (!el.name) continue;
      const [section, key] = el.name.split(".");
      const value = settings && settings[section] ? settings[section][key] : undefined;
      if (value === undefined || value === null) continue;
      el.value = value;
    }
  }

  function loadFleetSettings() {
    fetch("/api/fleet/settings")
      .then((r) => r.json())
      .then((data) => {
        applyFleetSettingsToForm(data.settings);
        fleetSettingsLoaded = true;
      });
  }

  function collectFleetSettingsForm() {
    const settings = {};
    for (const el of fleetSettingsForm.elements) {
      if (!el.name) continue;
      const value = el.type === "number" ? (el.value === "" ? null : Number(el.value)) : el.value;
      const [section, key] = el.name.split(".");
      settings[section] = settings[section] || {};
      settings[section][key] = value;
    }
    return settings;
  }

  fleetSettingsForm.addEventListener("submit", (evt) => {
    evt.preventDefault();
    const settings = collectFleetSettingsForm();
    const statusEl = document.getElementById("fleet-settings-status");
    statusEl.textContent = "Saving...";
    fetch("/api/fleet/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings }),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        statusEl.textContent = ok && data.ok ? data.note || "Saved." : "Error: " + ((data && data.error) || "failed to save.");
        if (data.settings) applyFleetSettingsToForm(data.settings);
      })
      .catch(() => {
        statusEl.textContent = "Error saving fleet settings.";
      });
  });

  if (document.getElementById("tab-fleet-settings").classList.contains("active") && !fleetSettingsLoaded) {
    loadFleetSettings();
  }

  // ---------- Retention: run now / dry run ----------
  function runRetention(dryRun) {
    const outputEl = document.getElementById("retention-output");
    outputEl.textContent = dryRun ? "Running dry run..." : "Running...";
    fetch("/api/fleet/retention/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun }),
    })
      .then((r) => r.json())
      .then((data) => {
        const prefix = dryRun ? "[dry run] " : "";
        if (!data.removed || data.removed.length === 0) {
          outputEl.textContent = prefix + "Nothing to remove.";
          return;
        }
        outputEl.textContent = prefix + `Would remove ${data.removed.length} clip(s):\n` + data.removed.join("\n");
      })
      .catch(() => {
        outputEl.textContent = "Failed to run retention.";
      });
  }

  document.getElementById("run-retention-dry").addEventListener("click", () => runRetention(true));
  document.getElementById("run-retention-now").addEventListener("click", () => {
    if (!window.confirm("Run retention now? This can permanently delete clips per the saved settings.")) return;
    runRetention(false);
  });

  // ---------- Auth token rotation ----------
  document.getElementById("rotate-token").addEventListener("click", () => {
    if (!window.confirm("Generate a new auth token now? The old one keeps working until the service restarts.")) return;
    fetch("/api/fleet/auth-token/rotate", { method: "POST" })
      .then((r) => r.json())
      .then((data) => {
        document.getElementById("rotate-token-output").textContent = data.ok
          ? `New token: ${data.token}\n${data.note}`
          : "Failed to rotate the token.";
      })
      .catch(() => {
        document.getElementById("rotate-token-output").textContent = "Failed to rotate the token.";
      });
  });

  // ---------- Classifier settings ----------
  const classifierForm = document.getElementById("classifier-settings-form");
  let classifierSettingsLoaded = false;

  function applyClassifierSettingsToForm(settings) {
    for (const el of classifierForm.elements) {
      if (!el.name) continue;
      const parts = el.name.split(".");
      let value = settings;
      for (const part of parts) {
        value = value && typeof value === "object" ? value[part] : undefined;
      }
      if (value === undefined || value === null) continue;
      el.value = value;
    }
  }

  function loadClassifierSettings() {
    fetch("/api/classifier/settings")
      .then((r) => r.json())
      .then((data) => {
        applyClassifierSettingsToForm(data.settings);
        classifierSettingsLoaded = true;
      });
  }

  function collectClassifierSettingsForm() {
    const settings = {};
    for (const el of classifierForm.elements) {
      if (!el.name) continue;
      const value = el.type === "number" ? (el.value === "" ? null : Number(el.value)) : el.value;
      const parts = el.name.split(".");
      let node = settings;
      for (let i = 0; i < parts.length - 1; i++) {
        node[parts[i]] = node[parts[i]] || {};
        node = node[parts[i]];
      }
      node[parts[parts.length - 1]] = value;
    }
    return settings;
  }

  classifierForm.addEventListener("submit", (evt) => {
    evt.preventDefault();
    const settings = collectClassifierSettingsForm();
    const statusEl = document.getElementById("classifier-settings-status");
    statusEl.textContent = "Saving...";
    fetch("/api/classifier/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings }),
    })
      .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        statusEl.textContent = ok && data.ok ? "Saved." : "Error: " + ((data && data.error) || "failed to save.");
        if (data.settings) applyClassifierSettingsToForm(data.settings);
      })
      .catch(() => {
        statusEl.textContent = "Error saving classifier settings.";
      });
  });

  if (document.getElementById("tab-classifier").classList.contains("active") && !classifierSettingsLoaded) {
    loadClassifierSettings();
  }
})();
