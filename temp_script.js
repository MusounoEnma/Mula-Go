
// ── Workflow Screen State ────────────────────────────────────────────────────
let _wfImages = [];
let _selectedPlatforms = new Set();

function init_workflow() {
  _refreshQueueFromPython();
  _initPlatformPills();
}

async function _refreshQueueFromPython() {
  try {
    const q = await window.pywebview.api.get_image_queue();
    if (q && q.length > 0) _renderQueue(q);
  } catch(e) {}
}

// ── Platform pill initialisation ─────────────────────────────────────────────
async function _initPlatformPills() {
  try {
    const connected = await window.pywebview.api.get_connected_platforms();
    connected.forEach(platform => {
      const pill = document.getElementById(`pill-${platform}`);
      if (!pill) return;
      pill.disabled = false;
      pill.classList.remove('pill-disabled');
      // Auto-select connected platforms by default
      _selectedPlatforms.add(platform);
      pill.classList.remove('pill-off');
      pill.classList.add('pill-on');
    });
    // Disable + grey out unconnected pills
    document.querySelectorAll('.platform-pill').forEach(pill => {
      if (pill.disabled) pill.classList.add('pill-disabled');
    });
    const hint = document.getElementById('broadcast-hint');
    if (hint) hint.textContent = connected.length
      ? `${connected.length} platform${connected.length > 1 ? 's' : ''} connected — toggle to include`
      : 'Connect accounts in Seller Hub to enable';
  } catch(e) {
    // Preview mode: enable all pills for demo
    document.querySelectorAll('.platform-pill').forEach(pill => {
      pill.disabled = false;
      pill.classList.remove('pill-disabled');
    });
  }
}

function _togglePill(btn) {
  const platform = btn.dataset.platform;
  if (!platform || btn.disabled) return;
  if (_selectedPlatforms.has(platform)) {
    _selectedPlatforms.delete(platform);
    btn.classList.remove('pill-on');
    btn.classList.add('pill-off');
  } else {
    _selectedPlatforms.add(platform);
    btn.classList.remove('pill-off');
    btn.classList.add('pill-on');
  }
}

// -- Folder Picker (click trigger) -------------------------------------------
// select_product_folder() uses self._window (injected window ref) to avoid
// PyWebView threading deadlocks. Returns {status, folder, images} or
// {status: 'cancelled'} or {status: 'error', message}.
async function triggerFolderPicker() {
  // Guard: pywebview bridge must be ready. boot() guarantees this at runtime.
  if (typeof window.pywebview === 'undefined' || !window.pywebview.api) {
    console.warn('[Workflow] pywebview not ready -- using mock data for preview.');
    _onScanSuccess(_mockScanResult());
    return;
  }

  const zone = document.getElementById('drop-zone');
  if (zone.classList.contains('dz-loading')) return;  // prevent double-click
  zone.classList.add('dz-loading');
  setDropZoneState('scanning');

  try {
    console.log('[Workflow] Calling select_product_folder...');
    const result = await window.pywebview.api.select_product_folder();
    console.log('[Workflow] select_product_folder result:', result);

    if (!result) { setDropZoneState('idle'); return; }

    if (result.status === 'success') {
      _onScanSuccess(result);           // result has .folder + .images
    } else if (result.status === 'cancelled') {
      setDropZoneState('idle');         // user dismissed dialog -- no alert
    } else {
      // result.status === 'error'
      setDropZoneState('idle');
      console.error('[Workflow] select_product_folder error:', result.message);
      alert('PYTHON ERROR:
' + (result.message || 'Unknown error'));
    }
  } catch(e) {
    // Unexpected JS exception
    console.error('[Workflow] triggerFolderPicker threw:', e);
    setDropZoneState('idle');
    alert('JS BRIDGE ERROR:
' + e.name + '
' + e.message + '

Stack:
' + e.stack);
  } finally {
    zone.classList.remove('dz-loading');
  }
}

// ── Drag & Drop handlers (REMOVED — D&D blocked by PyWebView security) ───────
// Stub functions kept to avoid JS errors if any residual event fires.
function handleDragOver(e) { e.preventDefault(); }
function handleDragLeave(e) { e.preventDefault(); }
function handleDrop(e) { e.preventDefault(); }

// ── Scan result handler ────────────────────────────────────────────
function _onScanSuccess(result) {
  _wfImages = result.images || [];
  setDropZoneState('idle');

  // Show folder pill
  const pill = document.getElementById('wf-folder-pill');
  const folderName = result.folder ? result.folder.split(/[/\\\\]/).pop() : 'Selected Folder';
  document.getElementById('wf-folder-name').textContent = `${folderName} · ${_wfImages.length} images`;
  pill.classList.remove('hidden');

  // Reset status feed — only shown when Process & Post is clicked
  document.getElementById('feed-log').innerHTML = '';
  document.getElementById('feed-done-banner').classList.add('hidden');
  document.getElementById('status-feed-section').classList.add('hidden');

  _renderQueue(_wfImages);
}

// ── Queue rendering ──────────────────────────────────────────────────────────
function _renderQueue(images) {
  const list     = document.getElementById('queue-list');
  const empty    = document.getElementById('queue-empty');
  const subtitle = document.getElementById('queue-subtitle');
  const clearBtn = document.getElementById('queue-clear-btn');
  const countBadge = document.getElementById('wf-count-badge');
  const processBtn = document.getElementById('wf-process-btn');

  if (!images || images.length === 0) {
    list.classList.add('hidden');
    empty.classList.remove('hidden');
    subtitle.textContent = 'No images loaded yet';
    clearBtn.classList.add('hidden');
    countBadge.classList.add('hidden');
    processBtn.classList.add('hidden');
    return;
  }

  // Update counts
  subtitle.textContent = `${images.length} image${images.length !== 1 ? 's' : ''} ready`;
  document.getElementById('wf-count-num').textContent = images.length;
  countBadge.classList.remove('hidden');
  countBadge.classList.add('flex');
  clearBtn.classList.remove('hidden');
  processBtn.classList.remove('hidden');

  // Render cards
  list.innerHTML = images.map((img, i) => `
    <div class="queue-card" id="qcard-${i}">
      <div class="flex items-center gap-2">
        <div class="card-status-dot" title="pending"></div>
        <span class="ext-badge">${img.ext || 'IMG'}</span>
        <div class="flex-1 min-w-0">
          <div class="font-heading font-semibold text-slate-700 text-xs truncate">${img.filename}</div>
          <div class="font-body text-slate-400 text-xs">${_formatBytes(img.size)}</div>
        </div>
        <span class="status-label font-heading text-slate-300" style="font-size:0.6rem;font-weight:700;">Queued</span>
      </div>
      <div class="vision-box" id="vision-${i}">
        <span class="vision-label">AI Vision (Moondream):</span>
        <span class="vision-text" id="vision-text-${i}">Awaiting vision scan…</span>
      </div>
      <div class="debug-vision-box" id="debug-box-${i}">
        <span class="debug-vision-label">🐛 DEBUG: Moondream Vision Output</span>
        <pre class="debug-vision-text" id="debug-text-${i}"></pre>
      </div>
    </div>
  `).join('');

  empty.classList.add('hidden');
  list.classList.remove('hidden');
}

// ── Clear queue ──────────────────────────────────────────────────────────────
async function clearQueue() {
  try { await window.pywebview.api.clear_image_queue(); } catch(e) {}
  _wfImages = [];
  document.getElementById('wf-folder-pill').classList.add('hidden');
  document.getElementById('status-feed-section').classList.add('hidden');
  _renderQueue([]);
}

// ── Process & Post (future) ───────────────────────────────────────────────────
// ── Process & Post — LIVE PIPELINE ─────────────────────────────────────────
let _pipelinePollInterval = null;
let _lastLogLength = 0;

async function startProcessing() {
  if (!_wfImages || _wfImages.length === 0) return;
  const btn = document.getElementById('wf-process-btn');
  btn.disabled = true;
  btn.innerHTML = '<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg> Processing...';

  document.getElementById('status-feed-section').classList.remove('hidden');
  document.getElementById('feed-log').innerHTML = '';
  document.getElementById('feed-done-banner').classList.add('hidden');
  document.getElementById('feed-pulse').style.animation = 'pulseRing 1.4s ease-in-out infinite';
  _lastLogLength = 0;

  const paths = _wfImages.map(img => img.path).filter(Boolean);
  const platforms = [..._selectedPlatforms];
  try {
    const res = await window.pywebview.api.run_pipeline(paths, platforms);
    if (!res || !res.success) {
      _appendFeedEntry('00:00', (res && res.message) || 'Pipeline could not start.');
      btn.disabled = false;
      return;
    }
    document.getElementById('feed-image-counter').textContent = `0 / ${res.total} images`;
  } catch(e) { _simulateFeedPreview(); return; }

  _pipelinePollInterval = setInterval(_pollPipeline, 1000);
}

async function _pollPipeline() {
  try {
    const s = await window.pywebview.api.get_pipeline_status();
    s.log.slice(_lastLogLength).forEach(e => _appendFeedEntry(e.ts, e.msg));
    _lastLogLength = s.log.length;
    if (s.total > 0)
      document.getElementById('feed-image-counter').textContent =
        `${Math.min(s.current_index + 1, s.total)} / ${s.total} images`;
    if (s.queue_status)
      _wfImages.forEach((img, i) => {
        const st = img.path && s.queue_status[img.path];
        if (st) _updateQueueCard(i, st);
      });
    // Inject Moondream vision text into cards as soon as it arrives
    if (s.vision_data)
      _wfImages.forEach((img, i) => {
        const raw = img.path && s.vision_data[img.path];
        if (raw) _updateVisionText(i, raw);
      });
    if (s.done && !s.running) {
      clearInterval(_pipelinePollInterval);
      document.getElementById('feed-pulse').style.animation = 'none';
      document.getElementById('feed-pulse').style.background = '#4ade80';
      document.getElementById('feed-done-banner').classList.remove('hidden');
      const btn = document.getElementById('wf-process-btn');
      btn.disabled = false;
      btn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg> Process &amp; Post';
    }
  } catch(e) {}
}

function _updateVisionText(index, rawText) {
  const el = document.getElementById(`vision-text-${index}`);
  if (!el || el.dataset.filled) return;
  el.dataset.filled = '1';
  el.textContent = rawText;
  el.classList.add('vision-filled');
}

function _appendFeedEntry(ts, msg) {
  // ── Intercept debug sentinel from pipeline.py ────────────────────────────
  // pipeline.py emits "__VISION_DEBUG__:<raw moondream text>" immediately
  // after the vision scan. We route it to the active card's debug box
  // instead of the live feed so it doesn't pollute the feed log.
  const DEBUG_PREFIX = '__VISION_DEBUG__:';
  if (msg.startsWith(DEBUG_PREFIX)) {
    const rawText = msg.slice(DEBUG_PREFIX.length);
    // Inject into the currently-processing card (current_index)
    const activeIndex = _wfImages.findIndex(
      (img, i) => document.getElementById(`qcard-${i}`) &&
                  document.getElementById(`qcard-${i}`).classList.contains('status-scanning')
    );
    const idx = activeIndex >= 0 ? activeIndex : 0;
    _injectDebugVision(idx, rawText);
    return;  // don't add to the visible feed log
  }

  const log = document.getElementById('feed-log');
  log.querySelectorAll('.active-entry').forEach(el => el.classList.remove('active-entry'));
  const div = document.createElement('div');
  div.className = 'feed-entry active-entry flex items-start gap-3';
  div.innerHTML = `<span class="font-mono text-xs text-slate-300 mt-0.5 flex-shrink-0 select-none">[${_escHtml(ts)}]</span><span class="feed-msg font-body text-sm leading-snug text-slate-600">${_escHtml(msg)}</span>`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function _injectDebugVision(index, rawText) {
  // Also update the "AI Vision" soft box
  _updateVisionText(index, rawText);
  // Show the dark debug box
  const box  = document.getElementById(`debug-box-${index}`);
  const pre  = document.getElementById(`debug-text-${index}`);
  if (!box || !pre || pre.dataset.filled) return;
  pre.dataset.filled = '1';
  pre.textContent = rawText;
  pre.classList.add('debug-filled');
  box.classList.add('debug-visible');
}

function _updateQueueCard(index, status) {
  const card = document.getElementById(`qcard-${index}`);
  if (!card) return;
  card.classList.remove('status-scanning','status-seo','status-posting','status-done','status-failed');
  if (status && status !== 'pending') card.classList.add(`status-${status}`);
  const lbl = card.querySelector('.status-label');
  if (lbl) lbl.textContent = ({pending:'Queued',scanning:'Scanning',seo:'SEO',posting:'Posting',done:'Done',failed:'Failed'})[status] || status;
}

function _escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _simulateFeedPreview() {
  document.getElementById('status-feed-section').classList.remove('hidden');
  [['00:01',"Taking a closer look at your product's unique features..."],
   ['00:04','Crafting the perfect sales copy to engage your audience...'],
   ['00:07','Preparing the stage for your global social media drop...'],
   ['00:09','Broadcasting your content across connected networks...'],
   ['00:15','Published successfully. Ready for the next masterpiece.'],
  ].forEach(([ts,msg],i) => setTimeout(() => {
    _appendFeedEntry(ts,msg);
    if (i===4) {
      document.getElementById('feed-done-banner').classList.remove('hidden');
      document.getElementById('feed-pulse').style.background='#4ade80';
    }
  }, i*1400));
}

// ── Drop zone visual state machine ──────────────────────────────────────────
function setDropZoneState(state) {
  // Helper: hide an element by ID safely (no crash if it was removed from DOM)
  const _hide = id => { const el = document.getElementById(id); if (el) el.classList.add('hidden'); };
  const _show = id => { const el = document.getElementById(id); if (el) el.classList.remove('hidden'); };

  _hide('dz-idle');
  _hide('dz-hover');      // removed from DOM but guard prevents crash
  _hide('dz-scanning');

  if (state === 'idle') {
    _show('dz-idle');
  } else if (state === 'scanning') {
    const s = document.getElementById('dz-scanning');
    if (s) { s.classList.remove('hidden'); s.classList.add('flex'); }
  }
}

// ── Utilities ────────────────────────────────────────────────────────────────
function _formatBytes(bytes) {
  if (!bytes) return '';
  if (bytes < 1024)        return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// Preview/dev mode mock data
function _mockScanResult() {
  const mockImages = [
    { filename: 'product_front.jpg',  ext: 'JPG',  size: 245760,  rel_folder: '' },
    { filename: 'product_back.jpg',   ext: 'JPG',  size: 189440,  rel_folder: '' },
    { filename: 'lifestyle_01.png',   ext: 'PNG',  size: 1048576, rel_folder: '' },
    { filename: 'closeup_detail.jpg', ext: 'JPG',  size: 307200,  rel_folder: 'detail' },
    { filename: 'banner_wide.webp',   ext: 'WEBP', size: 512000,  rel_folder: '' },
  ];
  return { success: true, folder: 'C:\\Users\\Demo\\Products\\My Product', count: mockImages.length, images: mockImages };
}
