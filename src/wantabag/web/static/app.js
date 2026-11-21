"use strict";

const JOB_KEY = "wantabag-job";
const ANNOUNCE_EVERY_MS = 5000;
const SVG_NS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);

const STATE_TEXT = {
  waiting: "Waiting",
  searching: "Searching",
  downloading: "Downloading",
  done: "Packed",
  skipped: "Packed",
  not_found: "Not found",
  failed: "Failed",
};
const STATE_ICON = {
  waiting: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16z",
  searching: "M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM15.5 15.5 20 20",
  downloading: "M12 4v12M6.5 11 12 16.5 17.5 11M5 20h14",
  done: "M5 12.5l4.5 4.5L19 7.5",
  not_found: "M6 6l12 12M18 6 6 18",
  failed: "M12 5v9M12 18.5v.5",
};
STATE_ICON.skipped = STATE_ICON.done;

let previewTimer = 0;
let previewAbort = null;
let source = null;
let ended = false;
let rows = new Map();
let totalUnits = 0;
let finishedUnits = 0;
let withVideo = false;
let announced = { at: 0, text: "", timer: 0 };

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

function icon(status) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", STATE_ICON[status] || STATE_ICON.waiting);
  for (const [k, v] of Object.entries({
    fill: "none", stroke: "currentColor", "stroke-width": "2.4",
    "stroke-linecap": "round", "stroke-linejoin": "round",
  })) path.setAttribute(k, v);
  svg.append(path);
  return svg;
}

function stateBadge(status, kind) {
  const badge = el("span", { class: kind === "video" ? "state video" : "state", "data-s": status });
  paintBadge(badge, status, kind);
  return badge;
}

function paintBadge(badge, status, kind) {
  badge.dataset.s = status;
  const what = `${kind === "video" ? "Video: " : ""}${STATE_TEXT[status] || status}`;
  badge.replaceChildren(icon(status), el("span", { class: "sr-only", text: what }));
  if (kind === "video") badge.append(el("span", { "aria-hidden": "true", text: "MP4" }));
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

function formatTotal(seconds) {
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return "";
  const h = Math.floor(minutes / 60);
  return h ? `${h} h ${String(minutes % 60).padStart(2, "0")} min` : `${minutes} min`;
}

function formatTrack(seconds) {
  if (seconds == null) return "";
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `Request failed (${res.status})`), { status: res.status });
  return data;
}

function showError(message) {
  const box = $("form-error");
  box.textContent = message || "";
  box.hidden = !message;
}


async function loadOptions() {
  let opts;
  try {
    opts = await api("/api/options");
  } catch {
    return;
  }
  const quality = $("quality");
  quality.replaceChildren(
    ...opts.audio.map((o) => {
      const input = el("input", { type: "radio", name: "quality", value: o.key, id: `q-${o.key}` });
      input.checked = o.key === opts.defaults.quality;
      return el("label", { for: `q-${o.key}` }, input, el("span", { text: o.label + (o.recommended ? " (recommended)" : "") }));
    }),
  );
  $("audio-note").textContent = opts.audio_note;
  const select = $("video-quality");
  select.replaceChildren(
    ...opts.video.map((o) => el("option", { value: o.key, text: o.label + (o.recommended ? " (recommended)" : "") })),
  );
  const wanted = opts.defaults.videos;
  $("videos").checked = Boolean(wanted);
  select.value = wanted || opts.video.find((o) => o.recommended).key;
  select.disabled = !wanted;
}

function readOptions() {
  const body = { url: $("url").value.trim() };
  const quality = document.querySelector('input[name="quality"]:checked');
  if (quality) body.quality = quality.value;
  const name = $("name").value.trim();
  if (name) body.name = name;
  body.videos = $("videos").checked ? $("video-quality").value : false;
  return body;
}


function clearPreview() {
  $("preview").hidden = true;
  $("p-warn").hidden = true;
  $("name").placeholder = "Same as the playlist";
}

function showPreview(p) {
  $("p-name").textContent = p.name;
  const parts = [plural(p.count, "song", "songs"), formatTotal(p.total_duration_s)].filter(Boolean);
  $("p-meta").textContent = parts.join(" · ");
  const warn = $("p-warn");
  warn.hidden = !(p.declared_count && p.declared_count > p.count);
  if (!warn.hidden) warn.textContent = `The source lists ${p.declared_count} songs but only ${p.count} could be read.`;
  const cover = $("cover");
  cover.hidden = !p.artwork;
  if (p.artwork) cover.src = p.artwork;
  $("name").placeholder = p.name;
  $("preview").hidden = false;
}

async function updatePreview() {
  const url = $("url").value.trim();
  if (previewAbort) previewAbort.abort();
  showError("");
  if (!url) return clearPreview();
  previewAbort = new AbortController();
  const signal = previewAbort.signal;
  try {
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
      signal,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    showPreview(data);
  } catch (err) {
    if (err.name === "AbortError") return;
    clearPreview();
    showError(err.message);
  }
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 300);
}


function announce(text, force = false) {
  announced.text = text;
  const wait = announced.at + ANNOUNCE_EVERY_MS - Date.now();
  clearTimeout(announced.timer);
  if (force || wait <= 0) {
    announced.at = Date.now();
    $("summary").textContent = text;
  } else {
    announced.timer = setTimeout(() => announce(announced.text, true), wait);
  }
}

function setBag(fraction) {
  $("bag-fill").style.transform = `translateY(${(1 - fraction) * 80}px)`;
}

function renderProgress() {
  const fraction = totalUnits ? finishedUnits / totalUnits : 0;
  $("bar").value = fraction;
  $("counter").textContent = `${finishedUnits} / ${totalUnits}`;
  setBag(fraction);
  document.title = ended ? "wantabag: I want a bag" : `(${Math.round(fraction * 100)}%) wantabag`;
}

function resetProgress() {
  rows = new Map();
  totalUnits = finishedUnits = 0;
  ended = false;
  $("tracks").replaceChildren();
  $("missed").replaceChildren();
  $("missed-box").hidden = true;
  $("done").hidden = true;
  $("saved").hidden = true;
  $("again").hidden = true;
  $("zip").hidden = false;
  renderProgress();
}

function onQueued({ position }) {
  $("progress-title").textContent = "Waiting in line";
  announce(position > 1 ? `${plural(position - 1, "download", "downloads")} ahead of yours.` : "You are next.", true);
}

function onPlaylist(p) {
  resetProgress();
  withVideo = Boolean(p.videos);
  totalUnits = p.tracks.length * (withVideo ? 2 : 1);
  $("progress-title").textContent = p.name;
  for (const t of p.tracks) {
    const audio = stateBadge("waiting", "audio");
    const li = el(
      "li",
      {},
      audio,
      el("span", { class: "label" }, el("span", { text: t.title }), el("span", { class: "artist", text: ` · ${t.artist}` })),
      el("span", { class: "leader", "aria-hidden": "true" }),
      el("span", { class: "dur num", text: formatTrack(t.duration_s) }),
    );
    const row = { li, label: t, audio, video: null };
    if (withVideo) {
      row.video = stateBadge("waiting", "video");
      li.insertBefore(row.video, li.querySelector(".leader"));
    }
    rows.set(t.index, row);
    $("tracks").append(li);
  }
  renderProgress();
  announce(`Packing ${plural(p.tracks.length, "song", "songs")}.`, true);
}

function addMissed(row, kind, status) {
  const why = status === "not_found" ? "Not found" : "Download failed";
  const entry = row.li;
  if (kind === "audio") {
    entry.querySelector(".dur").replaceWith(el("span", { class: "why", text: why }));
    $("missed").append(entry);
  } else {
    $("missed").append(
      el("li", {}, stateBadge(status, "video"), el("span", { class: "label", text: `${row.label.label} (video)` }),
        el("span", { class: "leader", "aria-hidden": "true" }), el("span", { class: "why", text: why })),
    );
  }
  $("missed-box").hidden = false;
}

function onTrack({ index, kind, status }) {
  const row = rows.get(index);
  if (!row) return;
  paintBadge(row[kind], status, kind);
  if (["done", "skipped", "not_found", "failed"].includes(status)) {
    finishedUnits += 1;
    if (status === "not_found" || status === "failed") addMissed(row, kind, status);
    renderProgress();
    const packed = [...rows.values()].filter((r) => ["done", "skipped"].includes(r.audio.dataset.s)).length;
    announce(`${packed} of ${rows.size} songs packed.`);
  }
}

function onEnd(end) {
  ended = true;
  source.close();
  if (end.status === "error") {
    sessionStorage.removeItem(JOB_KEY);
    $("progress-title").textContent = "Something went wrong";
    announce(end.error || "The download failed.", true);
    $("again").hidden = false;
    $("done").hidden = false;
    $("zip").hidden = true;
    renderProgress();
    return;
  }
  const missing = end.missing.length;
  const text = `${plural(end.downloaded, "song", "songs")} in the bag.` + (missing ? ` ${missing} didn't make it.` : "");
  $("progress-title").textContent = "Your bag is ready";
  announce(text, true);
  $("zip").href = end.zip_url;
  $("done").hidden = false;
  renderProgress();
}

function openStream(id) {
  if (source) source.close();
  source = new EventSource(`/api/jobs/${encodeURIComponent(id)}/events`);
  const on = (type, handler) => source.addEventListener(type, (e) => handler(JSON.parse(e.data)));
  on("queued", onQueued);
  on("playlist", onPlaylist);
  on("track", onTrack);
  on("end", onEnd);
  source.addEventListener("error", async () => {
    if (ended) return;
    if (source.readyState === EventSource.CLOSED) {
      sessionStorage.removeItem(JOB_KEY);
      announce("This download expired. Paste the link again to start a new one.", true);
      $("again").hidden = false;
      $("done").hidden = false;
      $("zip").hidden = true;
    } else {
      announce("Connection lost, trying again…", true);
    }
  });
}

function startJob(id) {
  sessionStorage.setItem(JOB_KEY, id);
  $("form").hidden = true;
  $("progress").hidden = false;
  $("progress-title").textContent = "Packing";
  resetProgress();
  announce("Starting…", true);
  openStream(id);
  $("progress").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

function packAnother() {
  if (source) source.close();
  sessionStorage.removeItem(JOB_KEY);
  $("progress").hidden = true;
  $("form").hidden = false;
  $("url").value = "";
  $("go").disabled = false;
  clearPreview();
  document.title = "wantabag: I want a bag";
  $("url").focus();
}


$("url").addEventListener("input", schedulePreview);
$("url").addEventListener("paste", () => setTimeout(schedulePreview, 0));
$("videos").addEventListener("change", () => { $("video-quality").disabled = !$("videos").checked; });

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!$("url").value.trim()) {
    showError("Paste a playlist link first.");
    $("url").focus();
    return;
  }
  showError("");
  $("go").disabled = true;
  try {
    const job = await api("/api/jobs", readOptions());
    startJob(job.id);
  } catch (err) {
    showError(err.message);
    $("go").disabled = false;
  }
});

$("zip").addEventListener("click", () => {
  sessionStorage.removeItem(JOB_KEY);
  setTimeout(() => {
    $("saved").textContent = "Download started. The server deletes its copy as soon as the file is sent.";
    $("saved").hidden = false;
    $("again").hidden = false;
  }, 1200);
});
$("again").addEventListener("click", packAnother);

async function resume() {
  const id = sessionStorage.getItem(JOB_KEY);
  if (!id) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}`);
    startJob(id);
  } catch (err) {
    sessionStorage.removeItem(JOB_KEY);
    if (err.status === 404) showError("This download expired. Paste the link again to start a new one.");
  }
}

loadOptions();
resume();
