"""
Inline HTML playground UI for testing text agents with live SSE streaming.

Serves a self-contained single-page app at GET /agent/text/playground.
No external dependencies — all CSS and JS are embedded.
"""

PLAYGROUND_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Clairvoyance — Text Agent Playground</title>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --surface-2: #1c2129;
    --border: #30363d;
    --text: #e6edf3;
    --text-dim: #8b949e;
    --accent: #58a6ff;
    --accent-dim: #1f6feb;
    --green: #3fb950;
    --green-dim: #238636;
    --red: #f85149;
    --orange: #d29922;
    --purple: #bc8cff;
    --font: 'SF Mono', 'Fira Code', 'JetBrains Mono', Consolas, monospace;
    --font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-sans);
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
  }
  .container {
    max-width: 1100px;
    margin: 0 auto;
    padding: 24px;
  }

  /* Header */
  .header {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 32px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }
  .header h1 {
    font-size: 20px;
    font-weight: 600;
    color: var(--text);
  }
  .header h1 span { color: var(--accent); }
  .header .badge {
    background: var(--accent-dim);
    color: var(--accent);
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.5px;
    text-transform: uppercase;
  }

  /* Agent selector */
  .agent-bar {
    display: flex;
    gap: 12px;
    align-items: center;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }
  .agent-bar label {
    color: var(--text-dim);
    font-size: 13px;
    font-weight: 500;
  }
  .agent-bar select {
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 13px;
    font-family: var(--font-sans);
    cursor: pointer;
    outline: none;
  }
  .agent-bar select:focus { border-color: var(--accent); }
  .agent-desc {
    color: var(--text-dim);
    font-size: 12px;
    flex-basis: 100%;
    margin-top: -4px;
  }

  /* Input area */
  .input-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 20px;
  }
  .input-section textarea {
    width: 100%;
    min-height: 120px;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    font-family: var(--font-sans);
    font-size: 14px;
    line-height: 1.5;
    resize: vertical;
    outline: none;
  }
  .input-section textarea:focus { border-color: var(--accent); }
  .input-section textarea::placeholder { color: var(--text-dim); }

  .btn-row {
    display: flex;
    gap: 10px;
    margin-top: 12px;
    align-items: center;
  }
  .btn {
    padding: 8px 20px;
    border-radius: 6px;
    border: 1px solid var(--border);
    font-size: 13px;
    font-weight: 500;
    font-family: var(--font-sans);
    cursor: pointer;
    transition: all 0.15s;
  }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-primary {
    background: var(--green-dim);
    color: #fff;
    border-color: var(--green-dim);
  }
  .btn-primary:hover:not(:disabled) { background: var(--green); }
  .btn-secondary {
    background: var(--surface-2);
    color: var(--text);
  }
  .btn-secondary:hover:not(:disabled) { background: var(--border); }
  .btn-danger {
    background: transparent;
    color: var(--red);
    border-color: var(--red);
  }
  .btn-danger:hover:not(:disabled) { background: rgba(248,81,73,0.1); }

  /* Pipeline status */
  .pipeline-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 20px;
    overflow: hidden;
    display: none;
  }
  .pipeline-section.active { display: block; }
  .pipeline-header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .pipeline-header h3 {
    font-size: 14px;
    font-weight: 600;
  }
  .pipeline-timer {
    color: var(--text-dim);
    font-family: var(--font);
    font-size: 13px;
  }

  /* Progress bar */
  .progress-bar-outer {
    height: 4px;
    background: var(--surface-2);
    position: relative;
    overflow: hidden;
  }
  .progress-bar-inner {
    height: 100%;
    background: linear-gradient(90deg, var(--accent-dim), var(--accent));
    width: 0%;
    transition: width 0.4s ease;
  }
  .progress-bar-outer.error .progress-bar-inner {
    background: var(--red);
  }
  .progress-bar-outer.done .progress-bar-inner {
    background: var(--green);
  }

  /* Stage list */
  .stage-list {
    padding: 8px 0;
    max-height: 320px;
    overflow-y: auto;
  }
  .stage-item {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 6px 16px;
    font-size: 13px;
    animation: fadeIn 0.2s ease;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .stage-icon {
    width: 20px;
    height: 20px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    margin-top: 1px;
    font-size: 11px;
  }
  .stage-icon.running {
    background: var(--accent-dim);
    color: var(--accent);
    animation: pulse 1.5s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
  .stage-icon.done { background: var(--green-dim); color: var(--green); }
  .stage-icon.error { background: rgba(248,81,73,0.2); color: var(--red); }
  .stage-msg { flex: 1; }
  .stage-msg .label { color: var(--text); }
  .stage-msg .agent-tag {
    display: inline-block;
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--purple);
    padding: 0 6px;
    border-radius: 4px;
    font-family: var(--font);
    font-size: 11px;
    margin-left: 6px;
  }
  .stage-time {
    color: var(--text-dim);
    font-family: var(--font);
    font-size: 12px;
    flex-shrink: 0;
    min-width: 50px;
    text-align: right;
  }
  .stage-pct {
    color: var(--text-dim);
    font-family: var(--font);
    font-size: 12px;
    flex-shrink: 0;
    min-width: 36px;
    text-align: right;
  }

  /* Result section */
  .result-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 20px;
    display: none;
  }
  .result-section.active { display: block; }
  .result-header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .result-header h3 { font-size: 14px; font-weight: 600; }
  .result-body {
    padding: 16px;
  }
  .result-body pre {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px;
    overflow-x: auto;
    max-height: 600px;
    overflow-y: auto;
    font-family: var(--font);
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text);
  }
  .result-status {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 500;
  }
  .result-status.success { background: var(--green-dim); color: var(--green); }
  .result-status.error { background: rgba(248,81,73,0.2); color: var(--red); }

  /* Examples */
  .examples {
    margin-top: 8px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .example-chip {
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 4px 12px;
    border-radius: 16px;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .example-chip:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  /* Footer */
  .footer {
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--text-dim);
    font-size: 12px;
    display: flex;
    justify-content: space-between;
  }
</style>
</head>
<body>
<div class="container">
  <!-- Header -->
  <div class="header">
    <h1><span>Clairvoyance</span> Text Agent Playground</h1>
    <span class="badge">Beta</span>
  </div>

  <!-- Agent selector -->
  <div class="agent-bar">
    <label for="agent-select">Agent</label>
    <select id="agent-select">
      <option value="">Loading agents...</option>
    </select>
    <div class="agent-desc" id="agent-desc"></div>
  </div>

  <!-- Input -->
  <div class="input-section">
    <textarea
      id="prompt-input"
      placeholder="Describe the voice agent you want to create...&#10;&#10;Example: Create a voice agent for confirming COD orders at ShopEasy. The agent should greet the customer, verify order details, handle address updates, and allow cancellation with a reason."
      spellcheck="false"
    ></textarea>
    <div class="examples">
      <span class="example-chip" data-prompt="Create a voice agent for confirming COD orders at ShopEasy. The agent should greet the customer, verify order items and delivery address, handle address updates, and allow cancellation with a reason.">Order Confirmation</span>
      <span class="example-chip" data-prompt="Create a voice agent for appointment reminders at HealthFirst clinic. Call patients, confirm upcoming appointments, allow rescheduling or cancellation, and collect the reason if cancelled.">Appointment Reminder</span>
      <span class="example-chip" data-prompt="Create a voice agent for collecting delivery feedback for QuickDeliver. Ask about delivery experience, rate the driver, handle complaints, and offer to connect to support if needed.">Delivery Feedback</span>
      <span class="example-chip" data-prompt="I want a template similar to the order confirmation template but for payment reminders. It should call customers about pending payments, verify the amount, and collect a promise-to-pay date.">Based on Existing Template</span>
    </div>
    <div class="btn-row">
      <button class="btn btn-primary" id="btn-generate" onclick="startGeneration()">
        Generate Template
      </button>
      <button class="btn btn-secondary" id="btn-stream" onclick="startStreaming()">
        Stream (Live Status)
      </button>
      <button class="btn btn-danger" id="btn-stop" onclick="stopGeneration()" disabled>
        Stop
      </button>
    </div>
  </div>

  <!-- Pipeline Status -->
  <div class="pipeline-section" id="pipeline-section">
    <div class="pipeline-header">
      <h3>Pipeline Status</h3>
      <span class="pipeline-timer" id="pipeline-timer">0.0s</span>
    </div>
    <div class="progress-bar-outer" id="progress-outer">
      <div class="progress-bar-inner" id="progress-inner"></div>
    </div>
    <div class="stage-list" id="stage-list"></div>
  </div>

  <!-- Result -->
  <div class="result-section" id="result-section">
    <div class="result-header">
      <h3>Result</h3>
      <div>
        <span class="result-status" id="result-status"></span>
        <button class="btn btn-secondary" style="margin-left:8px;padding:4px 12px;font-size:12px" onclick="copyResult()">Copy JSON</button>
      </div>
    </div>
    <div class="result-body">
      <pre id="result-content"></pre>
    </div>
  </div>

  <div class="footer">
    <span>Clairvoyance Blueprint Agent</span>
    <span>SSE streaming &middot; 3-agent pipeline &middot; Claude + GPT-4o</span>
  </div>
</div>

<script>
const API_BASE = window.location.pathname.replace(/\\/playground\\/?$/, '');
let currentEventSource = null;
let timerInterval = null;
let startTime = null;

// --- Init ---
document.addEventListener('DOMContentLoaded', loadAgents);

document.querySelectorAll('.example-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    document.getElementById('prompt-input').value = chip.dataset.prompt;
    document.getElementById('prompt-input').focus();
  });
});

async function loadAgents() {
  try {
    const resp = await fetch(API_BASE + '/agents');
    const data = await resp.json();
    const sel = document.getElementById('agent-select');
    sel.innerHTML = '';
    if (data.agents && data.agents.length > 0) {
      data.agents.forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.name;
        opt.textContent = a.name;
        opt.dataset.desc = a.description;
        sel.appendChild(opt);
      });
      updateDesc();
    } else {
      sel.innerHTML = '<option value="">No agents registered</option>';
    }
  } catch (e) {
    document.getElementById('agent-select').innerHTML =
      '<option value="">Failed to load agents</option>';
  }
}

document.getElementById('agent-select').addEventListener('change', updateDesc);
function updateDesc() {
  const sel = document.getElementById('agent-select');
  const opt = sel.options[sel.selectedIndex];
  document.getElementById('agent-desc').textContent = opt?.dataset?.desc || '';
}

// --- UI helpers ---
function setButtons(running) {
  document.getElementById('btn-generate').disabled = running;
  document.getElementById('btn-stream').disabled = running;
  document.getElementById('btn-stop').disabled = !running;
  document.getElementById('prompt-input').disabled = running;
}

function resetPipeline() {
  const section = document.getElementById('pipeline-section');
  section.classList.add('active');
  document.getElementById('stage-list').innerHTML = '';
  document.getElementById('progress-inner').style.width = '0%';
  document.getElementById('progress-outer').className = 'progress-bar-outer';
  document.getElementById('result-section').classList.remove('active');
  startTime = Date.now();
  timerInterval = setInterval(() => {
    const el = document.getElementById('pipeline-timer');
    el.textContent = ((Date.now() - startTime) / 1000).toFixed(1) + 's';
  }, 100);
}

function addStage(stage, message, agentName, elapsed, pct, isError) {
  const list = document.getElementById('stage-list');
  const iconClass = isError ? 'error' : (pct >= 100 ? 'done' :
    (stage.includes('complete') || stage === 'completed') ? 'done' : 'running');
  const iconChar = isError ? '!' : iconClass === 'done' ? '&#10003;' : '&#9679;';
  const agentTag = agentName && agentName !== 'null'
    ? '<span class="agent-tag">' + agentName + '</span>' : '';

  const item = document.createElement('div');
  item.className = 'stage-item';
  item.innerHTML =
    '<div class="stage-icon ' + iconClass + '">' + iconChar + '</div>' +
    '<div class="stage-msg"><span class="label">' + escapeHtml(message) + '</span>' + agentTag + '</div>' +
    '<span class="stage-pct">' + pct + '%</span>' +
    '<span class="stage-time">' + elapsed + 's</span>';
  list.appendChild(item);
  list.scrollTop = list.scrollHeight;

  // Update progress bar
  const inner = document.getElementById('progress-inner');
  if (pct >= 0) inner.style.width = pct + '%';
  if (isError) document.getElementById('progress-outer').classList.add('error');
  if (pct >= 100) document.getElementById('progress-outer').classList.add('done');
}

function showResult(status, content) {
  const section = document.getElementById('result-section');
  section.classList.add('active');
  const statusEl = document.getElementById('result-status');
  statusEl.textContent = status;
  statusEl.className = 'result-status ' + (status === 'success' ? 'success' : 'error');

  // Try to pretty-print JSON
  let display = content;
  try {
    const parsed = JSON.parse(content);
    display = JSON.stringify(parsed, null, 2);
  } catch (_) {}
  document.getElementById('result-content').textContent = display;
}

function stopTimer() {
  if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

function copyResult() {
  const text = document.getElementById('result-content').textContent;
  navigator.clipboard.writeText(text).catch(() => {});
}

// --- Generate (one-shot, no streaming) ---
async function startGeneration() {
  const agent = document.getElementById('agent-select').value;
  const input = document.getElementById('prompt-input').value.trim();
  if (!agent || !input) return;

  setButtons(true);
  resetPipeline();
  addStage('initializing', 'Sending request to ' + agent + ' agent...', null, '0.0', 5, false);

  try {
    const resp = await fetch(API_BASE + '/playground/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent, input }),
    });
    const data = await resp.json();
    stopTimer();

    if (resp.ok) {
      addStage('completed', data.message || 'Done', null,
        data.elapsed_secs || ((Date.now() - startTime)/1000).toFixed(1), 100, false);
      showResult(data.status, data.result || data.message || 'No result');
    } else {
      addStage('error', data.detail || 'Request failed', null,
        ((Date.now() - startTime)/1000).toFixed(1), -1, true);
      showResult('error', data.detail || JSON.stringify(data));
    }
  } catch (e) {
    stopTimer();
    addStage('error', 'Network error: ' + e.message, null,
      ((Date.now() - startTime)/1000).toFixed(1), -1, true);
  }
  setButtons(false);
}

// --- Stream (SSE) ---
function startStreaming() {
  const agent = document.getElementById('agent-select').value;
  const input = document.getElementById('prompt-input').value.trim();
  if (!agent || !input) return;

  setButtons(true);
  resetPipeline();

  // SSE via fetch (POST not supported by EventSource, so we use fetch + ReadableStream)
  const abortController = new AbortController();
  currentEventSource = abortController;

  fetch(API_BASE + '/playground/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent, input }),
    signal: abortController.signal,
  }).then(async response => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\\n');
      buffer = lines.pop();

      let eventType = 'status';
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          eventType = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const dataStr = line.slice(6);
          try {
            const data = JSON.parse(dataStr);
            handleSSEEvent(eventType, data);
          } catch (_) {}
          eventType = 'status';
        }
      }
    }
    // Stream ended
    stopTimer();
    setButtons(false);
  }).catch(e => {
    if (e.name !== 'AbortError') {
      stopTimer();
      addStage('error', 'Stream error: ' + e.message, null,
        ((Date.now() - startTime)/1000).toFixed(1), -1, true);
    }
    setButtons(false);
  });
}

function handleSSEEvent(eventType, data) {
  if (eventType === 'done') {
    stopTimer();
    return;
  }
  if (eventType === 'error' || data.stage === 'error') {
    addStage(data.stage || 'error', data.message || 'Error', data.agent_name,
      data.elapsed_secs || 0, data.progress_pct || -1, true);
    stopTimer();
    return;
  }
  // Normal status
  addStage(
    data.stage || 'unknown',
    data.message || '',
    data.agent_name,
    data.elapsed_secs || 0,
    data.progress_pct || 0,
    false
  );
  // If completed, show any detail as result
  if (data.stage === 'completed' && data.detail) {
    showResult('success', data.detail);
  }
}

function stopGeneration() {
  if (currentEventSource) {
    currentEventSource.abort();
    currentEventSource = null;
  }
  stopTimer();
  addStage('cancelled', 'Generation stopped by user', null,
    ((Date.now() - startTime)/1000).toFixed(1), -1, true);
  setButtons(false);
}
</script>
</body>
</html>
"""
