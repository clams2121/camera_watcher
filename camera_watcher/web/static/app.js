(function () {
  "use strict";

  // ---------- Tabs ----------
  const previewImg = document.getElementById("preview-img");
  let maskLoaded = false;

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("tab-" + btn.dataset.tab).classList.add("active");

      if (btn.dataset.tab === "preview") {
        previewImg.src = "/api/stream?t=" + Date.now();
      } else {
        previewImg.src = ""; // stop pulling the MJPEG stream when not visible
      }

      if (btn.dataset.tab === "mask" && !maskLoaded) {
        maskLoaded = true;
        loadMask();
        loadSnapshot();
      }
    });
  });

  // ---------- Status badge ----------
  function pollStatus() {
    fetch("/api/status")
      .then((r) => r.json())
      .then((data) => {
        const badge = document.getElementById("status-badge");
        const dropped = data.dropped_frames > 0 ? " • dropped " + data.dropped_frames : "";
        badge.textContent =
          (data.connected ? "connected" : "disconnected") + (data.recording ? " • recording" : "") + dropped;
        badge.className = "status-badge " + (data.connected ? "ok" : "bad") + (data.recording ? " rec" : "");
      })
      .catch(() => {});
  }
  setInterval(pollStatus, 3000);
  pollStatus();

  // ---------- Settings form ----------
  const form = document.getElementById("settings-form");

  function applySettingsToForm(settings) {
    for (const el of form.elements) {
      if (!el.name || el.name.startsWith("credentials.")) continue;
      const [section, key] = el.name.split(".");
      const value = settings && settings[section] ? settings[section][key] : undefined;
      if (value === undefined) continue;
      if (el.type === "checkbox") el.checked = !!value;
      else el.value = value;
    }
  }

  function updateCredStatus(hasCredentials) {
    const el = document.getElementById("redacted-url");
    if (!hasCredentials) {
      el.title = "No credentials saved yet";
    }
  }

  function loadSettings() {
    fetch("/api/settings")
      .then((r) => r.json())
      .then((data) => {
        applySettingsToForm(data.settings);
        document.getElementById("redacted-url").textContent = data.redacted_rtsp_url;
        updateCredStatus(data.has_credentials);
      });
  }

  function collectForm() {
    const settings = {};
    const credentials = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      let value;
      if (el.type === "checkbox") value = el.checked;
      else if (el.type === "number") value = el.value === "" ? null : Number(el.value);
      else value = el.value;

      if (el.name.startsWith("credentials.")) {
        if (el.type === "password" && value === "") continue; // blank = leave unchanged
        credentials[el.name.split(".")[1]] = value;
        continue;
      }
      const [section, key] = el.name.split(".");
      settings[section] = settings[section] || {};
      settings[section][key] = value;
    }
    return { settings, credentials };
  }

  form.addEventListener("submit", (evt) => {
    evt.preventDefault();
    const { settings, credentials } = collectForm();
    const statusEl = document.getElementById("settings-status");
    statusEl.textContent = "Saving...";
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings, credentials }),
    })
      .then((r) => r.json())
      .then((data) => {
        statusEl.textContent = data.ok ? "Saved." : "Error saving settings.";
        applySettingsToForm(data.settings);
        updateCredStatus(data.has_credentials);
        form.querySelector('input[name="credentials.password"]').value = "";
      })
      .catch(() => {
        statusEl.textContent = "Error saving settings.";
      });
  });

  loadSettings();

  // ---------- Mask editor ----------
  const canvas = document.getElementById("mask-canvas");
  const ctx = canvas.getContext("2d");
  let snapshotImg = null;
  let shapes = [];
  let currentShape = [];
  let pendingPolygons = [];

  function loadMask() {
    fetch("/api/mask")
      .then((r) => r.json())
      .then((data) => {
        pendingPolygons = data.polygons || [];
        applyPendingPolygons();
      });
  }

  function applyPendingPolygons() {
    if (!canvas.width || !canvas.height || pendingPolygons.length === 0) return;
    shapes = pendingPolygons.map((poly) => poly.map(([nx, ny]) => [nx * canvas.width, ny * canvas.height]));
    pendingPolygons = [];
    redraw();
  }

  function loadSnapshot() {
    document.getElementById("mask-status").textContent = "Loading snapshot...";
    fetch("/api/snapshot?t=" + Date.now())
      .then((r) => {
        if (!r.ok) throw new Error("snapshot unavailable");
        return r.blob();
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          canvas.width = img.naturalWidth;
          canvas.height = img.naturalHeight;
          snapshotImg = img;
          applyPendingPolygons();
          redraw();
          URL.revokeObjectURL(url);
          document.getElementById("mask-status").textContent = "";
        };
        img.src = url;
      })
      .catch(() => {
        document.getElementById("mask-status").textContent = "No frames from the camera yet.";
      });
  }

  function drawPolygon(points, closed) {
    if (points.length === 0) return;
    ctx.beginPath();
    points.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
    if (closed) {
      ctx.closePath();
      ctx.fill();
    }
    ctx.stroke();
    points.forEach(([x, y]) => {
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, 2 * Math.PI);
      const prevFill = ctx.fillStyle;
      ctx.fillStyle = "yellow";
      ctx.fill();
      ctx.fillStyle = prevFill;
    });
  }

  function redraw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (snapshotImg) ctx.drawImage(snapshotImg, 0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "rgba(255,0,0,0.35)";
    ctx.strokeStyle = "red";
    ctx.lineWidth = 2;
    shapes.forEach((shape) => drawPolygon(shape, true));
    if (currentShape.length) drawPolygon(currentShape, false);
  }

  function canvasPoint(evt) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return [(evt.clientX - rect.left) * scaleX, (evt.clientY - rect.top) * scaleY];
  }

  canvas.addEventListener("click", (evt) => {
    const [x, y] = canvasPoint(evt);
    if (currentShape.length >= 3) {
      const [fx, fy] = currentShape[0];
      if (Math.hypot(x - fx, y - fy) < 10) {
        shapes.push(currentShape);
        currentShape = [];
        redraw();
        return;
      }
    }
    currentShape.push([x, y]);
    redraw();
  });

  canvas.addEventListener("dblclick", (evt) => {
    evt.preventDefault();
    if (currentShape.length >= 3) {
      shapes.push(currentShape);
      currentShape = [];
      redraw();
    }
  });

  document.getElementById("undo-point").addEventListener("click", () => {
    if (currentShape.length) currentShape.pop();
    else shapes.pop();
    redraw();
  });

  document.getElementById("clear-shapes").addEventListener("click", () => {
    shapes = [];
    currentShape = [];
    redraw();
  });

  document.getElementById("refresh-snapshot").addEventListener("click", loadSnapshot);

  document.getElementById("save-mask").addEventListener("click", () => {
    const polygons = shapes.map((shape) => shape.map(([x, y]) => [x / canvas.width, y / canvas.height]));
    fetch("/api/mask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polygons }),
    })
      .then((r) => r.json())
      .then((data) => {
        document.getElementById("mask-status").textContent = "Saved " + (data.polygons || []).length + " shape(s).";
      });
  });
})();
