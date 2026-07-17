(function () {
  "use strict";

  // ---------- Stop server ----------
  document.getElementById("stop-server").addEventListener("click", () => {
    const typed = window.prompt('This stops the camera_watcher server and ends the recording process.\nType "quit" to confirm:');
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
          window.alert(data.error || "Failed to stop the server.");
        }
      })
      .catch(() => {
        // The server may have already dropped the connection while
        // stopping -- treat that as success rather than an error.
        document.getElementById("shutdown-banner").hidden = false;
        document.getElementById("stop-server").disabled = true;
      });
  });

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

      if (btn.dataset.tab === "recordings") {
        loadRecordings();
      } else {
        recordingVideo.pause();
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
  const shapeListEl = document.getElementById("shape-list");
  const deleteShapeBtn = document.getElementById("delete-selected-shape");

  // The canvas is drawn larger than the actual camera image by this fraction
  // on every side, so a shape can fully enclose something touching the
  // frame's edge. Points placed in that margin are stored as normalized
  // coordinates outside [0, 1] -- valid in the mask format, and they clip
  // naturally against the real frame when rasterized server-side.
  const MARGIN_FRAC = 0.15;
  let imgWidth = 0;
  let imgHeight = 0;
  let marginX = 0;
  let marginY = 0;

  let snapshotImg = null;
  let shapes = [];
  let currentShape = [];
  let pendingPolygons = [];
  let selectedShapeIndex = null;
  let showHeatmap = false;
  let heatmapImg = null;

  function renderShapeList() {
    shapeListEl.innerHTML = "";
    shapes.forEach((shape, idx) => {
      const li = document.createElement("li");
      li.textContent = `Shape ${idx + 1} (${shape.length} points)`;
      li.className = idx === selectedShapeIndex ? "selected" : "";
      li.addEventListener("click", () => {
        selectedShapeIndex = selectedShapeIndex === idx ? null : idx;
        renderShapeList();
        redraw();
      });
      shapeListEl.appendChild(li);
    });
    deleteShapeBtn.disabled = selectedShapeIndex === null;
  }

  deleteShapeBtn.addEventListener("click", () => {
    if (selectedShapeIndex === null) return;
    shapes.splice(selectedShapeIndex, 1);
    selectedShapeIndex = null;
    renderShapeList();
    redraw();
  });

  function loadMask() {
    fetch("/api/mask")
      .then((r) => r.json())
      .then((data) => {
        pendingPolygons = data.polygons || [];
        applyPendingPolygons();
      });
  }

  function applyPendingPolygons() {
    if (!imgWidth || !imgHeight || pendingPolygons.length === 0) return;
    shapes = pendingPolygons.map((poly) => poly.map(([nx, ny]) => [nx * imgWidth + marginX, ny * imgHeight + marginY]));
    pendingPolygons = [];
    selectedShapeIndex = null;
    renderShapeList();
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
          imgWidth = img.naturalWidth;
          imgHeight = img.naturalHeight;
          marginX = imgWidth * MARGIN_FRAC;
          marginY = imgHeight * MARGIN_FRAC;
          canvas.width = imgWidth + marginX * 2;
          canvas.height = imgHeight + marginY * 2;
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

    ctx.fillStyle = "#88888833";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (snapshotImg) ctx.drawImage(snapshotImg, marginX, marginY, imgWidth, imgHeight);
    if (showHeatmap && heatmapImg) ctx.drawImage(heatmapImg, marginX, marginY, imgWidth, imgHeight);

    // Dashed outline marking where the actual camera frame ends and the
    // drawable margin begins.
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = "#ffffffcc";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(marginX, marginY, imgWidth, imgHeight);
    ctx.restore();

    ctx.lineWidth = 2;
    shapes.forEach((shape, idx) => {
      if (idx === selectedShapeIndex) {
        ctx.fillStyle = "rgba(59,110,165,0.45)";
        ctx.strokeStyle = "#3b6ea5";
      } else {
        ctx.fillStyle = "rgba(255,0,0,0.35)";
        ctx.strokeStyle = "red";
      }
      drawPolygon(shape, true);
    });
    ctx.fillStyle = "rgba(255,0,0,0.35)";
    ctx.strokeStyle = "red";
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
        renderShapeList();
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
      renderShapeList();
      redraw();
    }
  });

  document.getElementById("undo-point").addEventListener("click", () => {
    if (currentShape.length) currentShape.pop();
    else {
      shapes.pop();
      selectedShapeIndex = null;
      renderShapeList();
    }
    redraw();
  });

  document.getElementById("clear-shapes").addEventListener("click", () => {
    shapes = [];
    currentShape = [];
    selectedShapeIndex = null;
    renderShapeList();
    redraw();
  });

  document.getElementById("refresh-snapshot").addEventListener("click", loadSnapshot);

  document.getElementById("save-mask").addEventListener("click", () => {
    const polygons = shapes.map((shape) => shape.map(([x, y]) => [(x - marginX) / imgWidth, (y - marginY) / imgHeight]));
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

  function loadHeatmap() {
    const statusEl = document.getElementById("heatmap-status");
    fetch("/api/heatmap.png?t=" + Date.now())
      .then((r) => {
        if (!r.ok) throw new Error("no heatmap yet");
        return r.blob();
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          heatmapImg = img;
          URL.revokeObjectURL(url);
          statusEl.textContent = "";
          redraw();
        };
        img.src = url;
      })
      .catch(() => {
        heatmapImg = null;
        statusEl.textContent = "No motion analyzed yet.";
        redraw();
      });
  }

  document.getElementById("toggle-heatmap").addEventListener("change", (evt) => {
    showHeatmap = evt.target.checked;
    if (showHeatmap) loadHeatmap();
    else redraw();
  });

  document.getElementById("reset-heatmap").addEventListener("click", () => {
    fetch("/api/heatmap/reset", { method: "POST" }).then(() => {
      heatmapImg = null;
      document.getElementById("heatmap-status").textContent = "Heatmap cleared.";
      if (showHeatmap) redraw();
    });
  });

  // ---------- Recordings ----------
  const recordingsUl = document.getElementById("recordings-ul");
  const recordingsPlayer = document.getElementById("recordings-player");
  const recordingVideo = document.getElementById("recording-video");
  const playingNameEl = document.getElementById("playing-name");
  const playPauseBtn = document.getElementById("play-pause");

  function formatSize(bytes) {
    if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(2) + " GB";
    if (bytes >= 1024 ** 2) return (bytes / 1024 ** 2).toFixed(1) + " MB";
    return Math.round(bytes / 1024) + " KB";
  }

  function playRecording(name) {
    playingNameEl.textContent = name;
    recordingVideo.src = "/api/recordings/" + encodeURIComponent(name);
    recordingsPlayer.hidden = false;
    recordingVideo.load();
    recordingVideo.play().catch(() => {}); // autoplay can be blocked by the browser; ignore
  }

  function loadRecordings() {
    recordingsUl.innerHTML = '<li class="hint">Loading...</li>';
    fetch("/api/recordings")
      .then((r) => r.json())
      .then((data) => {
        const recordings = data.recordings || [];
        recordingsUl.innerHTML = "";
        if (recordings.length === 0) {
          recordingsUl.innerHTML = '<li class="hint">No recordings yet.</li>';
          return;
        }
        recordings.forEach((rec) => {
          const li = document.createElement("li");
          const when = new Date(rec.modified * 1000).toLocaleString();
          li.textContent = `${rec.name} — ${when} (${formatSize(rec.size_bytes)})`;
          li.addEventListener("click", () => playRecording(rec.name));
          recordingsUl.appendChild(li);
        });
      })
      .catch(() => {
        recordingsUl.innerHTML = '<li class="hint">Failed to load recordings.</li>';
      });
  }

  playPauseBtn.addEventListener("click", () => {
    if (recordingVideo.paused) recordingVideo.play();
    else recordingVideo.pause();
  });
  recordingVideo.addEventListener("play", () => (playPauseBtn.textContent = "Pause"));
  recordingVideo.addEventListener("pause", () => (playPauseBtn.textContent = "Play"));

  document.getElementById("seek-back").addEventListener("click", () => {
    recordingVideo.currentTime = Math.max(0, recordingVideo.currentTime - 10);
  });
  document.getElementById("seek-fwd").addEventListener("click", () => {
    const duration = isFinite(recordingVideo.duration) ? recordingVideo.duration : Infinity;
    recordingVideo.currentTime = Math.min(duration, recordingVideo.currentTime + 10);
  });

  document.getElementById("refresh-recordings").addEventListener("click", loadRecordings);
})();
