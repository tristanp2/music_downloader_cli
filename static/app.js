
/* ===========================================================================
   TYPE DEFINITIONS (JSDoc — editor-checked, no runtime/build impact)
   =========================================================================== */
/**
 * @typedef {Object} Track
 * @property {number} pos
 * @property {string} name
 * @property {TrackStatus} status
 * @property {number} [pct]
 */
/**
 * @typedef {"pending"|"downloading"|"downloaded"|"skipped"|"missed"|"failed"} TrackStatus
 */
/**
 * @typedef {Object} Counts
 * @property {number} downloaded
 * @property {number} skipped
 * @property {number} missed
 * @property {number} failed
 */
/**
 * @typedef {Object} JobInit
 * @property {string} id
 * @property {string} url
 * @property {string} user
 * @property {string} [name]
 * @property {string} [status]
 */
/**
 * @typedef {Object} JobResult
 * @property {boolean} [ok]
 * @property {string} [error]
 * @property {number} [downloaded]
 * @property {number} [skipped]
 * @property {number} [missed]
 * @property {number} [failed]
 */
/**
 * @typedef {Object} JobSnapshot
 * @property {string} id
 * @property {string} url
 * @property {string} user
 * @property {string} [name]
 * @property {string} status
 * @property {string} [started_at]
 * @property {string} [finished_at]
 * @property {{total?:number, tracks?:Object<string, Track>}} [progress]
 * @property {JobResult} [result]
 */
/**
 * @typedef {"tracks"|"start"|"pct"|"done"} TrackEventType
 */
/**
 * @typedef {Object} TrackProgressEvent
 * @property {string} job_id
 * @property {TrackEventType} type
 * @property {number} [pos]
 * @property {string} [name]
 * @property {TrackStatus} [status]
 * @property {number} [pct]
 * @property {number} [total]
 * @property {Track[]} [items]
 */
/**
 * @typedef {Object} Playlist
 * @property {string} folder
 * @property {string} name
 * @property {number} total
 * @property {number} downloaded
 * @property {number} missed
 * @property {string} [fetched_at]
 * @property {string} [spotify_url]
 */
/**
 * @typedef {Object} LibraryTrack
 * @property {number} position
 * @property {string} [artist]
 * @property {string} title
 * @property {TrackStatus} status
 * @property {boolean} has_file
 */
/**
 * @typedef {Object} DownloadResponse
 * @property {string} job_id
 */
/**
 * @typedef {Object} SyncResponse
 * @property {number} queued
 */

/* ===========================================================================
   STATE / CONSTANTS
   =========================================================================== */
/**
 * @param {string} s
 * @returns {HTMLElement}
 */
const $ = (s) => /** @type {HTMLElement} */ (document.querySelector(s));

const token = new URLSearchParams(location.search).get("token") || null;

// Single source of truth for download statuses: the icon shown for each, plus
// the tally-chip labels used in the job-card header. Everywhere else references
// STATUS.<name> instead of duplicating bare strings, so a rename touches one spot.
/** @type {Record<string, {icon: string, chip: string|null}>} */
const STATUS = {
  pending:     { icon: "\u2022", chip: null },     // bullet        (not tallied)
  downloading: { icon: "\u25c9", chip: null },     // white diamond (not tallied)
  downloaded:  { icon: "\u2713", chip: "dl" },     // checkmark
  skipped:     { icon: "\u27a4", chip: "sk" },     // heavy arrowhead
  missed:      { icon: "\u2717", chip: "mi" },     // X
  failed:      { icon: "\u26a0", chip: "fa" },     // warning
};
// Flattened icon lookup (kept for call sites that just want the glyph).
const statusIcons = Object.fromEntries(
  Object.entries(STATUS).map(([name, v]) => [name, v.icon])
);

/* ===========================================================================
   CURRENT USER (localStorage)
   =========================================================================== */
/**
 * @returns {string}
 */
function getUser() 
{
  const storedUser = localStorage.getItem("music_downloader_user");
  if (storedUser === "tristan" || storedUser === "marc") return storedUser;
  return "tristan";
}
/**
 * @param {string} storedUser
 */
function setUser(storedUser) 
{
  localStorage.setItem("music_downloader_user", storedUser);

  for (const [fid, row] of libraryRows) 
  {
    row.remove();
    libraryRows.delete(fid);
  }

  for (var folder in fetchedTracks) delete fetchedTracks[folder];
}

/**
 * @returns {Promise<void>}
 */
async function initUserSelect() 
{
  try 
  {
    const response = await fetch("/users");
    const data = await response.json();
    const sel = $("#user-select");

    sel.innerHTML = "";

    var users = data.users || [];
    for (var i = 0; i < users.length; i++) 
    {
      var opt = document.createElement("option");
      opt.value = users[i];
      opt.textContent = users[i];
      sel.appendChild(opt);
    }

    var saved = localStorage.getItem("music_downloader_user");
    sel.value = saved && users.indexOf(saved) !== -1 ? saved : (data.default || "");
    sel.addEventListener("change", function() 
    {
      setUser(sel.value);
      refreshPlaylists();
      refreshJobs();
    });

  }
  catch 
  {
    var sel = $("#user-select");

    sel.innerHTML = '<option value="tristan">tristan</option>';
    sel.value = getUser();
    sel.addEventListener("change", function() 
    {
      setUser(sel.value);
      refreshPlaylists();
      refreshJobs();
    });
  }
}

initUserSelect();

/* ===========================================================================
   KEYED DOM RENDERING (state-backed virtual-ish DOM)
   - jobCards:   JobCard instances, keyed by job id
   - libraryRows:  LibraryRow instances, keyed by "lib-<folder>"
   - fetchedTracks: raw /library/<folder> payloads, keyed by folder (cache)
   =========================================================================== */
const jobCards = new Map();  // jobId -> JobCard instance
const libraryRows = new Map(); // folderId ("lib-<folder>") -> LibraryRow instance
/** @type {Record<string, LibraryTrack[]>} */
const fetchedTracks = {};

/**
 * @param {string|null|undefined} s
 * @returns {string}
 */
function esc(s) 
{
  if (!s) return "";
  return s.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
}

// Timestamps arrive as "YYYY-MM-DDTHH:MM:SS"; show the clock portion only.
/**
 * @param {string|null|undefined} ts
 * @returns {string}
 */
function fmtClock(ts) 
{
  if (!ts) return "—";
  return ts.length >= 19 ? ts.slice(11, 19) : ts;
}

/* ===========================================================================
   TrackRow  (one per track inside a JobCard's track list)
   Wraps a single .track-row element; create-if-missing then update.
   =========================================================================== */
class TrackRow 
{
  /** @type {HTMLElement} */
  el;
  /** @type {HTMLElement} */
  listEl;
  /**
   * @param {HTMLElement} listEl
   * @param {Track} track
   * @param {number} total
   */
  constructor(listEl, track, total) 
  {
    let row = listEl.querySelector('.track-row[data-pos="' + track.pos + '"]');
    if (!row) 
    {
      row = document.createElement("div");
      row.className = "track-row";
      row.dataset.pos = track.pos;
      row.dataset.status = track.status;

      const posEl = document.createElement("span");
      posEl.className = "pos";
      row.appendChild(posEl);

      const nameSpan = document.createElement("span");
      nameSpan.className = "track-name";
      row.appendChild(nameSpan);

      const barWrap = document.createElement("div");
      barWrap.className = "mini-bar";
      const barFill = document.createElement("div");
      barFill.className = "fill";
      barWrap.appendChild(barFill);
      row.appendChild(barWrap);

      const statusEl = document.createElement("span");
      statusEl.className = "track-status";
      row.appendChild(statusEl);

      listEl.appendChild(row);
    }
    this.el = row;
    this.listEl = listEl;
    this.render(track, total);
  }

  /**
   * @param {Track} track
   * @param {number} total
   */
  render(track, total) 
  {
    this.listEl.querySelector('.track-row[data-pos="' + track.pos + '"]');
    const row = this.el;

    row.querySelector(".pos").textContent = track.pos + "/" + total;

    const nameSpan = row.querySelector(".track-name");
    nameSpan.textContent = track.name;
    nameSpan.title = track.name;
    nameSpan.className = "track-name" + (track.status === "pending" ? " pending" : "");

    const barWrap = row.querySelector(".mini-bar");
    const barFill = row.querySelector(".mini-bar .fill");
    const showBar = (track.status === "downloading" || track.status === "downloaded" || track.status === "skipped");
    barWrap.style.display = showBar ? "" : "none";
    if (track.status === "downloading") 
    {
      barFill.style.width = (track.pct != null ? track.pct : 0) + "%";
      barFill.className = "fill";
    }
    else 
    {
      barFill.style.width = "100%";
      barFill.className = "fill done";
    }

    const statusEl = row.querySelector(".track-status");
    statusEl.className = "track-status " + track.status;
    statusEl.textContent = statusIcons[track.status] || "";
  }
}

/* ===========================================================================
   JobCard  (one per job; owns its DOM, receives SSE events + snapshot renders)
   The single global job_feed SSE (see jobFeed()) routes events here via
   applyEvent(type, event); renderSnapshot(job) handles the load/refresh reconcile.
   =========================================================================== */
class JobCard 
{
  /** @type {string} */
  id;
  /** @type {boolean} */
  open;
  /** @type {Counts} */
  counts;
  /** @type {Map<number, TrackRow>} */
  rows;
  /** @type {HTMLElement} */
  el;
  /** @type {HTMLElement} */
  headerEl;
  /** @type {HTMLElement} */
  chevEl;
  /** @type {HTMLElement} */
  metaEl;
  /** @type {HTMLElement} */
  resultsEl;
  /** @type {HTMLElement} */
  urlEl;
  /** @type {HTMLElement} */
  listEl;
  /**
   * @param {JobInit} j
   */
  constructor(j) 
  {
    this.id = j.id;
    this.open = false;
    this.counts = { downloaded: 0, skipped: 0, missed: 0, failed: 0 };
    this.rows = new Map();  // pos -> TrackRow

    const card = document.createElement("div");
    card.className = "job " + j.status;
    card.dataset.job = j.id;

    const header = document.createElement("div");
    header.className = "job-header";
    header.dataset.job = j.id;

    const chevEl = document.createElement("span");
    chevEl.className = "job-chevron";
    chevEl.textContent = "\u25b6";
    header.appendChild(chevEl);

    const metaEl = document.createElement("div");
    metaEl.className = "meta";
    header.appendChild(metaEl);

    const resultsEl = document.createElement("div");
    resultsEl.className = "results";
    header.appendChild(resultsEl);

    card.appendChild(header);

    const urlEl = document.createElement("div");
    urlEl.className = "job-url";
    urlEl.style.display = "none";
    card.appendChild(urlEl);

    const listEl = document.createElement("div");
    listEl.className = "track-list";
    listEl.style.display = "none";
    card.appendChild(listEl);

    header.addEventListener("click", () => 
    {
      this.setOpen(!this.open);
    });

    this.el = card;
    this.headerEl = header;
    this.chevEl = chevEl;
    this.metaEl = metaEl;
    this.resultsEl = resultsEl;
    this.urlEl = urlEl;
    this.listEl = listEl;

    $("#jobs").appendChild(card);
    const empty = $("#jobs .empty");
    if (empty) empty.remove();
  }

  /**
   * @param {boolean} open
   */
  setOpen(open) 
  {
    this.open = open;
    this.el.classList.toggle("open", open);
    this.chevEl.classList.toggle("open", open);
    this.headerEl.classList.toggle("open", open);
    this.listEl.style.display = open ? "" : "none";
    this.urlEl.style.display = open ? "" : "none";
  }

  // Full reconcile from a job snapshot (used on load + user-triggered refresh).
  /**
   * @param {JobSnapshot} job
   */
  renderSnapshot(job) 
  {
    const badge = job.status === "queued" ? "queued"
                 : job.status === "done" ? "done"
                 : job.status === "error" ? "error" : "running";
    this.el.className = "job " + badge;

    const badgeStatus = badge;
    const nameHtml = job.playlist_name ? '<span class="pname">' + esc(job.playlist_name) + "</span> &nbsp; " : "";
    this.metaEl.innerHTML = '<span class="badge ' + badgeStatus + '">' + esc(job.status) + "</span> &nbsp; " + nameHtml
      + '<span class="clock" title="started">st: ' + fmtClock(job.started_at) + "</span>"
      + '<span class="clock" title="finished">et: ' + fmtClock(job.finished_at) + "</span>";

    this.urlEl.textContent = job.url;

    const progress = job.progress || {};
    const total = progress.total || 0;
    const tracksMap = progress.tracks || {};
    const trackArr = Object.values(tracksMap).sort(function (a, b) 
    {
      return a.pos - b.pos; 
    });
    for (let i = 0; i < trackArr.length; i++) 
    {
      this.upsertTrackRow(trackArr[i], total);
    }

    // Keep the list visible if rows are already rendered (live SSE) so finished
    // jobs keep showing their tracks -- they only vanish when the card is removed.
    const hasRows = this.listEl.querySelectorAll(".track-row").length > 0;
    const shouldShow = (hasRows || trackArr.length || job.status === "running");
    if (shouldShow && this.open) this.setOpen(true);

    if (job.result && job.result.ok) 
    {
      this.counts = {
        downloaded: job.result.downloaded || 0,
        skipped: job.result.skipped || 0,
        missed: job.result.missed || 0,
        failed: job.result.failed || 0,
      };
      this.paintChips();
    }
    else if (job.result && job.result.error) 
    {
      this.resultsEl.innerHTML = '<span class="chip err">error</span>';
    }
    else 
    {
      this.paintChips();
    }
  }

  // Per-track SSE event router.
  /**
   * @param {TrackEventType} type
   * @param {TrackProgressEvent} event
   */
  applyEvent(type, event) 
  {
    if (type === "tracks") 
    {
      this.counts = { downloaded: 0, skipped: 0, missed: 0, failed: 0 };
      for (let i = 0; i < event.items.length; i++) 
      {
        this.upsertTrackRow({ pos: event.items[i].pos, name: event.items[i].name, status: "pending", pct: 0 }, event.total);
      }
      this.paintChips();

    }
    else if (type === "start") 
    {
      const row = this.rows.get(event.pos);
      if (row) row.render({ pos: event.pos, name: event.name, status: "downloading", pct: 0 }, this.listEl.querySelectorAll(".track-row").length);

    }
    else if (type === "pct") 
    {
      // A mid-range PCT (1-99%) means the progress bar is actively filling --
      // expand this card so the user sees live progress. Do it once.
      if (!this.open && event.pct > 0 && event.pct < 100) 
      {
        JobCard.collapseOthers(this.id);
        this.setOpen(true);
      }

      const row = this.rows.get(event.pos);
      if (!row) return;
      const barFill = row.el.querySelector(".mini-bar .fill");
      if (barFill) 
      {
        barFill.style.width = (event.pct || 0) + "%"; barFill.className = "fill"; 
      }
      const barWrap = row.el.querySelector(".mini-bar");
      if (barWrap) barWrap.style.display = "";
      const statusEl = row.el.querySelector(".track-status");
      if (statusEl) 
      {
        statusEl.className = "track-status downloading"; statusEl.textContent = statusIcons.downloading; 
      }

    }
    else if (type === "done") 
    {
      const row = this.rows.get(event.pos);
      if (row) 
      {
        const name = row.el.querySelector(".track-name").textContent;
        row.render({ pos: event.pos, name: name, status: event.status, pct: event.pct }, this.listEl.querySelectorAll(".track-row").length);
      }

      if (event.status in this.counts) this.counts[event.status]++;
      this.paintChips();
    }
  }

  /**
   * @param {Track} track
   * @param {number} total
   */
  upsertTrackRow(track, total) 
  {
    const existing = this.rows.get(track.pos);
    if (existing) 
    {
      existing.render(track, total); return existing; 
    }

    const row = new TrackRow(this.listEl, track, total);
    this.rows.set(track.pos, row);

    return row;
  }

  // Paint this card's live tally (this.counts) into the results element.
  paintChips() 
  {
    const order = /** @type {(keyof Counts)[]} */ (["downloaded", "skipped", "missed", "failed"]);
    const cls = /** @type {Record<string, string>} */ ({ downloaded: "ok", skipped: "warn", missed: "miss", failed: "err" });
    const chip = (n, label, c) => '<span class="chip ' + c + '">' + label + ' ' + n + '</span>';

    this.resultsEl.innerHTML = order.map((name) =>
      chip(this.counts[name] || 0, STATUS[name].chip, cls[name])
    ).join("");
  }

  remove() 
  {
    this.el.remove();
  }

  // Collapse every other card so the newest stays expanded by default.
  /**
   * @param {string} exceptId
   */
  static collapseOthers(exceptId) 
  {
    for (const [id, card] of jobCards) 
    {
      if (id !== exceptId) card.setOpen(false);
    }
  }
}

// --- registry factory: lookup-or-create a JobCard for a job id ---
/**
 * @param {JobInit} j
 * @returns {JobCard}
 */
function ensureJobCard(j) 
{
  let card = jobCards.get(j.id);
  if (!card) 
  {
    card = new JobCard(j);
    jobCards.set(j.id, card);
  }
  return card;
}

/**
 * @param {JobSnapshot[]} jobs
 */
function syncJobs(jobs) 
{
  const jobIds = new Set();
  for (let i = 0; i < jobs.length; i++) 
  {
    jobIds.add(jobs[i].id);
  }

  for (let i = 0; i < jobs.length; i++) 
  {
    ensureJobCard(jobs[i]).renderSnapshot(jobs[i]);
  }

  for (const [id, card] of jobCards) 
  {
    if (!jobIds.has(id)) 
    {
      card.remove();
      jobCards.delete(id);
    }
  }

  // Cap the visible queue so it can't grow unbounded; newest jobs are appended last.
  const MAX_CARDS = 30;
  const box = $("#jobs");
  while (box.querySelectorAll(".job").length > MAX_CARDS) 
  {
    const first = box.querySelector(".job");
    if (!first) break;
    const rid = first.dataset.job;
    first.remove();
    if (jobCards.has(rid)) jobCards.delete(rid);
  }

  if (!jobs.length) 
  {
    const box = $("#jobs");
    if (!box.querySelector(".empty")) 
    {
      box.innerHTML = '<div class="empty">No jobs yet.</div>';
    }
  }
}

/* ===========================================================================
   FETCHERS (REST polling: health / jobs / playlists / users)
   =========================================================================== */
/**
 * @param {string} url
 * @returns {Promise<DownloadResponse>}
 */
async function postDownload(url) 
{
  const headers = /** @type {Record<string, string>} */ ({ "Content-Type": "application/json" });

  if (token) headers["X-Auth-Token"] = token;

  const response = await fetch("/download?user=" + encodeURIComponent(getUser()), {
    method: "POST", headers, body: JSON.stringify({ url })
  });

  if (!response.ok) 
  {
    const e = await response.json().catch(()=>({detail:response.statusText})); throw new Error(e.detail || response.statusText); 
  }

  return response.json();
}

/**
 * @returns {Promise<void>}
 */
async function refreshHealth() 
{
  try 
  {
    const response = await fetch("/health");
    const healthEl = $("#health");

    if (response.ok) 
    {
      healthEl.textContent = "up"; healthEl.className = "ok"; 
    }
    else 
    {
      healthEl.textContent = "down"; healthEl.className = "bad"; 
    }

  }
  catch 
  {
    $("#health").textContent = "server down"; $("#health").className = "bad"; 
  }
}

/**
 * @returns {Promise<void>}
 */
async function refreshJobs() 
{
  try 
  {
    const response = await fetch("/jobs?user=" + encodeURIComponent(getUser()));
    const data = await response.json();

    syncJobs(data.jobs || []);

  }
  catch 
  {}
}

/* ===========================================================================
   SHARED TRACK-PROGRESS DISPATCHER (driven by the ONE job-feed SSE)
   Thin router: look up the card and hand the event to its applyEvent method.
   All per-card rendering lives in JobCard.applyEvent.
   =========================================================================== */
/**
 * @param {string} jobId
 * @param {TrackEventType} type
 * @param {TrackProgressEvent} event
 */
function applyTrackEvent(jobId, type, event) 
{
  const card = jobCards.get(jobId);

  if (!card) return;

  card.applyEvent(type, event);
}

/* ===========================================================================
   LibraryRow  (one per playlist in the library table)
   Owns its row + detail elements; renderSummary() paints the summary cells,
   toggle() expands/collapses the track list (fetching it on first open).
   =========================================================================== */
class LibraryRow 
{
  /** @type {string} */
  folder;
  /** @type {string} */
  fid;
  /** @type {HTMLElement} */
  row;
  /** @type {HTMLElement} */
  detail;
  /** @type {HTMLElement} */
  chevron;
  /** @type {HTMLElement} */
  inner;
  /**
   * @param {HTMLElement} tb
   * @param {Playlist} playlist
   */
  constructor(tb, playlist) 
  {
    this.folder = playlist.folder;
    this.fid = "lib-" + playlist.folder;

    const row = document.createElement("tr");
    row.className = "lib-row";
    row.dataset.folder = playlist.folder;
    row.dataset.fid = this.fid;
    row.onclick = () => this.toggle();

    const chevTd = document.createElement("td");
    chevTd.className = "lt-chevron";
    chevTd.innerHTML = "▶";
    row.appendChild(chevTd);

    const folderTd = document.createElement("td");
    row.appendChild(folderTd);

    const nameTd = document.createElement("td");
    row.appendChild(nameTd);

    const totalTd = document.createElement("td");
    row.appendChild(totalTd);

    const dlTd = document.createElement("td");
    row.appendChild(dlTd);

    const missTd = document.createElement("td");
    row.appendChild(missTd);

    const fetchTd = document.createElement("td");
    row.appendChild(fetchTd);

    const updateTd = document.createElement("td");
    const updateLink = document.createElement("a");
    updateLink.className = "update-link";
    updateLink.href = "#";
    updateLink.textContent = "update";
    updateLink.dataset.url = playlist.spotify_url || "";
    updateLink.onclick = (e) => 
    {
      e.preventDefault();
      e.stopPropagation();
      startUpdate(updateLink.dataset.url);
    };
    updateTd.appendChild(updateLink);
    row.appendChild(updateTd);

    const zipTd = document.createElement("td");
    const zipLink = document.createElement("a");
    zipLink.className = "zip-link";
    zipLink.href = "/zip/" + encodeURIComponent(playlist.folder) + "?user=" + encodeURIComponent(getUser());
    zipLink.textContent = "zip";
    zipLink.onclick = (e) => e.stopPropagation();
    zipTd.appendChild(zipLink);
    row.appendChild(zipTd);

    const detail = document.createElement("tr");
    detail.className = "lib-detail";
    detail.dataset.fid = this.fid;

    const detailTd = document.createElement("td");
    detailTd.colSpan = 9;

    const inner = document.createElement("div");
    inner.className = "lib-detail-inner";
    inner.id = "inner-" + this.fid;

    detailTd.appendChild(inner);
    detail.appendChild(detailTd);

    tb.appendChild(row);
    tb.appendChild(detail);

    this.row = row;
    this.detail = detail;
    this.chevron = chevTd;
    this.inner = inner;
  }

  // Paint the summary cells (folder / name / total / dl / miss / fetched).
  /**
   * @param {Playlist} playlist
   */
  renderSummary(playlist) 
  {
    const cells = this.row.children;
    cells[1].textContent = playlist.folder;

    cells[2].textContent = playlist.name;

    cells[3].textContent = String(playlist.total);

    cells[4].textContent = String(playlist.downloaded);

    cells[5].textContent = String(playlist.missed);

    cells[6].textContent = playlist.fetched_at || "";
  }

  // Expand/collapse the track list; fetch + cache it on first open.
  /**
   * @returns {Promise<void>}
   */
  async toggle() 
  {
    const isOpen = this.detail.classList.contains("open");

    if (isOpen) 
    {
      this.detail.classList.remove("open");
      this.chevron.classList.remove("open");
      return;
    }

    if (!fetchedTracks[this.folder]) 
    {
      this.inner.innerHTML = '<div class="empty">loading...</div>';
      try 
      {
        const response = await fetch("/library/" + encodeURIComponent(this.folder) + "?user=" + encodeURIComponent(getUser()));
        const payload = await response.json();
        fetchedTracks[this.folder] = payload.tracks || [];
      }
      catch 
      {
        this.inner.innerHTML = '<div class="empty">failed to load</div>';
        return;
      }
    }

    const tracks = fetchedTracks[this.folder];
    this.inner.innerHTML = tracks.map((t) => 
    {
      const name = t.artist ? t.artist + " - " + t.title : t.title;
      const dl = t.has_file
        ? '<a class="lt-dl" href="/library/' + encodeURIComponent(this.folder) +
          '/track/' + encodeURIComponent(t.position) + '?user=' + encodeURIComponent(getUser()) +
          '">dl</a>'
        : '';

      return '<div class="lib-track">' +
        '<span class="lt-pos">' + t.position + '</span>' +
        '<span class="lt-name">' + esc(name) + '</span>' +
        '<span class="lt-status ' + t.status + '">' + (statusIcons[t.status] || "") + '</span>' +
        '<span class="lt-dl-cell">' + dl + '</span>' +
      '</div>';
    }).join("");

    this.detail.classList.add("open");
    this.chevron.classList.add("open");
  }

  remove() 
  {
    this.row.remove();
    this.detail.remove();
  }
}

/**
 * @returns {Promise<void>}
 */
async function refreshPlaylists() 
{
  try 
  {
    const response = await fetch("/playlists?user=" + encodeURIComponent(getUser()));
    const data = await response.json();
    const tableBody = $("#playlists tbody");

    const rows = data.playlists || [];
    if (!rows.length) 
    {
      for (const [fid, row] of libraryRows) 
      {
        row.remove();
        libraryRows.delete(fid);
      }
      tableBody.innerHTML = '<tr><td colspan="9" class="empty">No playlists yet.</td></tr>';
      return;
    }

    const emptyEl = tableBody.querySelector(".empty");
    if (emptyEl) emptyEl.remove();

    const folders = new Set();
    for (let i = 0; i < rows.length; i++) 
    {
      const playlist = rows[i];
      const fid = "lib-" + playlist.folder;
      folders.add(fid);

      let row = libraryRows.get(fid);
      if (!row) 
      {
        row = new LibraryRow(tableBody, playlist);
        libraryRows.set(fid, row);
      }
      row.renderSummary(playlist);
    }

    for (const [fid, row] of libraryRows) 
    {
      if (!folders.has(fid)) 
      {
        row.remove();
        libraryRows.delete(fid);
      }
    }
  }
  catch 
  {}
}

// Disable the queue button while a request is in flight so rapid clicks /
// Enter presses can't spam the job queue (the backend also rejects duplicate
// active jobs, but this gives immediate visual feedback and stops the race).
/**
 * @param {boolean} on
 */
function setGoBusy(on) 
{
  const button = document.getElementById("go");
  if (button) button.disabled = on;
}
/**
 * @param {boolean} on
 */
function setSyncBusy(on) 
{
  const button = document.getElementById("sync");
  if (button) button.disabled = on;
}

$("#go").addEventListener("click", async () => 
{
  const url = $("#url").value.trim();
  if (!url) return;

  setGoBusy(true);
  $("#msg").textContent = "starting...";

  try 
  {
    const response = await postDownload(url);
    $("#msg").textContent = "job " + response.job_id + " started";
    $("#url").value = "";

    // Ensure the job card exists, then open SSE for live progress
    const job = { id: response.job_id, url: url, user: getUser(), status: "running" };
    const card = ensureJobCard(job);
    card.renderSnapshot(job);

    JobCard.collapseOthers(response.job_id);
    card.setOpen(true);

    // One-time fetch of the list view (lightweight)
    refreshJobs();
  }
  catch (e) 
  {
    $("#msg").textContent = "error: " + e.message;
  }
  finally 
  {
    setGoBusy(false);
  }
});

/**
 * @returns {Promise<SyncResponse>}
 */
async function postSync() 
{
  const headers = /** @type {Record<string, string>} */ ({ "Content-Type": "application/json" });
  if (token) headers["X-Auth-Token"] = token;

  const response = await fetch("/sync", { method: "POST", headers });
  if (response.status === 409) throw new Error("sync already running");
  if (!response.ok) 
  {
    const e = await response.json().catch(()=>({detail:response.statusText})); throw new Error(e.detail || response.statusText); 
  }

  return response.json();
}

// Manually trigger a sync: enqueue a job for every known playlist. The jobs
// surface in the normal queue, so no separate UI is needed.
$("#sync").addEventListener("click", async () => 
{
  $("#msg").textContent = "syncing all playlists...";
  setSyncBusy(true);
  try 
  {
    const response = await postSync();

    $("#msg").textContent = "queued " + response.queued + " playlist job(s)";
    refreshJobs();

  }
  catch (e) 
  {
    $("#msg").textContent = "sync error: " + e.message;
  }
  finally 
  {
    setSyncBusy(false);
  }
});

// Pressing Enter in the URL input triggers the same queue action as the button.
$("#url").addEventListener("keydown", (e) => 
{
  if (e.key === "Enter") 
  {
    e.preventDefault();
    $("#go").click();
  }
});

// Re-download a playlist already in the library (Update button). Reuses the
// existing /download endpoint with the playlist's stored Spotify URL; the
// backend skip-check means only missing/partial tracks are actually fetched.
/**
 * @param {string} url
 * @returns {Promise<void>}
 */
async function startUpdate(url) 
{
  if (!url) 
  {
    $("#msg").textContent = "error: playlist has no stored Spotify URL"; return; 
  }
  $("#msg").textContent = "updating playlist...";

  try 
  {
    const response = await postDownload(url);
    $("#msg").textContent = "job " + response.job_id + " started (update)";

    const job = { id: response.job_id, url: url, user: getUser(), status: "running" };
    ensureJobCard(job).renderSnapshot(job);

    refreshJobs();
  }
  catch (e) 
  {
    $("#msg").textContent = "error: " + e.message;
  }
}

/* ===========================================================================
   SHARED JOB-FEED SSE (ONE connection per tab, opened at page load)
   Carries job_created / job_done / per-track progress. Also our liveness signal:
   a live connection means the server is up; onopen => healthy; onerror => down.
   EventSource auto-reconnects, so we only drive the badge + overlay here and
   reload once on recovery to re-sync the full job snapshot.
   =========================================================================== */
(function jobFeed() 
{
  var wasDown = false;

  function setBadge(up) 
  {
    var healthEl = $("#health");
    if (!healthEl) return;
    healthEl.textContent = up ? "up" : "server down";
    healthEl.className = up ? "ok" : "bad";
  }
  function showOverlay(on) 
  {
    var overlay = document.getElementById("serverOverlay");
    if (overlay) overlay.classList.toggle("show", on);
    // disable the user dropdown while disconnected so no requests are sent
    var sel = document.getElementById("user-select");
    if (sel) sel.disabled = on;
  }

  const eventSource = new EventSource("/jobs/stream");

  eventSource.addEventListener("job_created", function(e) 
  {
    const event = JSON.parse(e.data);
    const job = event.job;
    ensureJobCard({ id: job.id, url: job.url, user: job.user, playlist_name: job.playlist_name, status: job.status });
    ensureJobCard(job).renderSnapshot(job);
  });

  // Per-track progress is relayed by the backend for EVERY job; apply to its card.
  ["tracks", "start", "pct", "done"].forEach(function(type) 
  {
    eventSource.addEventListener(type, function(e) 
    {
      const event = JSON.parse(e.data);
      ensureJobCard({ id: event.job_id, url: "", user: "", status: "running" });
      applyTrackEvent(event.job_id, type, event);
    });
  });

  eventSource.addEventListener("job_done", function(e) 
  {
    const event = JSON.parse(e.data);
    ensureJobCard(event.job).renderSnapshot(event.job);
    refreshPlaylists();
  });

  // Download actually began (after the DZ_LOCK wait): re-render so the start
  // time shows the real download-start, not the enqueue/sync time.
  eventSource.addEventListener("job_started", function(e) 
  {
    const event = JSON.parse(e.data);
    if (event.job) ensureJobCard(event.job).renderSnapshot(event.job);
  });

  eventSource.onopen = function() 
  {
    setBadge(true);
    showOverlay(false);
    // Reconnect after an outage: reload once to re-fetch the job list / state.
    if (wasDown) 
    {
      wasDown = false; location.reload(); 
    }
  };

  eventSource.onerror = function() 
  {
    wasDown = true;
    setBadge(false);
    showOverlay(true);
  };
})();

refreshHealth();
refreshJobs();
refreshPlaylists();
