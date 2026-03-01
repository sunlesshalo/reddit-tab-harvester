const SERVER = "http://localhost:7777";
let redditTabs = [];

// --- Init ---

async function init() {
  const tabs = await chrome.tabs.query({ url: "*://*.reddit.com/*" });
  redditTabs = tabs;
  document.getElementById("tab-count").textContent = tabs.length;

  const harvestBtn = document.getElementById("harvest-btn");
  if (tabs.length > 0) {
    harvestBtn.disabled = false;
  }

  try {
    const resp = await fetch(`${SERVER}/health`);
    if (!resp.ok) throw new Error();
  } catch {
    harvestBtn.disabled = true;
    document.getElementById("server-warning").style.display = "block";
  }
}

// --- Harvest with SSE progress ---

document.getElementById("harvest-btn").addEventListener("click", async () => {
  const btn = document.getElementById("harvest-btn");
  const status = document.getElementById("status");
  const progress = document.getElementById("progress-bar");

  btn.disabled = true;
  progress.style.display = "block";
  status.innerHTML = "Starting harvest...";

  const urls = redditTabs.map((t) => t.url);
  const total = urls.length;

  try {
    const resp = await fetch(`${SERVER}/harvest-stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });

    if (!resp.ok) {
      throw new Error("Server error");
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop(); // Keep incomplete line

      let eventName = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventName = line.slice(7);
        } else if (line.startsWith("data: ")) {
          const data = JSON.parse(line.slice(6));
          handleSSE(eventName, data, total, status, progress);
        }
      }
    }
  } catch (err) {
    // Fallback to regular JSON endpoint
    status.innerHTML = '<span class="spinner"></span>Fetching...';
    try {
      const resp = await fetch(`${SERVER}/harvest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls }),
      });
      const data = await resp.json();
      handleDone(data, status);
    } catch (e) {
      status.innerHTML = `<span class="error">Error: ${e.message}</span>`;
      btn.disabled = false;
    }
  }

  progress.style.display = "none";
});

function handleSSE(event, data, total, status, progressEl) {
  if (event === "progress") {
    const pct = Math.round((data.done / data.total) * 100);
    const fill = progressEl.querySelector(".fill");

    if (data.stage === "fetch") {
      status.textContent = `Fetching ${data.done}/${data.total} posts...`;
      fill.style.width = `${pct * 0.7}%`; // 0-70% for fetching
    } else if (data.stage === "analyze") {
      status.innerHTML = '<span class="spinner"></span>Claude is analyzing...';
      fill.style.width = "75%";
    }
  } else if (event === "done") {
    const fill = progressEl.querySelector(".fill");
    fill.style.width = "100%";
    handleDone(data, status);
  }
}

function handleDone(data, status) {
  status.innerHTML = `Done! ${data.fetched} posts analyzed.`;
  if (data.errors > 0) {
    status.innerHTML += ` <span class="error">(${data.errors} skipped)</span>`;
  }
  if (data.digest_url) {
    chrome.tabs.create({ url: data.digest_url });
  }
  document.getElementById("close-btn").style.display = "block";
}

// --- Close Tabs ---

document.getElementById("close-btn").addEventListener("click", async () => {
  const tabIds = redditTabs.map((t) => t.id);
  await chrome.tabs.remove(tabIds);
  document.getElementById("status").textContent = `Closed ${tabIds.length} tabs.`;
  document.getElementById("close-btn").style.display = "none";
  document.getElementById("tab-count").textContent = "0";
});

// --- Knowledge Base ---

document.getElementById("kb-btn").addEventListener("click", () => {
  chrome.tabs.create({ url: `${SERVER}/knowledge` });
});

init();
