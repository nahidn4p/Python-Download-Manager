const DEFAULT_PORT = 17894;
const MAX_PORT_OFFSET = 16;
const STORAGE_KEY = "pyidmBridgeConfig";
const CONTEXT_MENU_ID = "pyidm-download";
const MEDIA_CACHE_TTL = 30000;
const MEDIA_PATTERNS = [/\.m3u8(\?|$)/i, /\.mpd(\?|$)/i];

const recentMedia = new Map();

async function getConfiguredEndpoints() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(
      {
        [STORAGE_KEY]: {
          hosts: [],
        },
      },
      (items) => {
        const config = items[STORAGE_KEY] || {};
        const hosts = Array.isArray(config.hosts) ? config.hosts : [];
        resolve(hosts);
      },
    );
  });
}

async function resolveBridgeTargets() {
  const configured = await getConfiguredEndpoints();
  const defaults = Array.from({ length: MAX_PORT_OFFSET }, (_, idx) => `http://127.0.0.1:${DEFAULT_PORT + idx}`);
  const combined = [...configured, ...defaults];
  const unique = [...new Set(combined.filter(Boolean))];
  return unique;
}

async function sendBridgeRequest(path, payload) {
  const targets = await resolveBridgeTargets();
  const body = JSON.stringify(payload);
  for (const base of targets) {
    const endpoint = `${base}${path}`;
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (response.ok) {
        return true;
      }
    } catch (err) {
      // Try next target
    }
  }
  return false;
}

async function postDownload(url, filename) {
  return sendBridgeRequest("/enqueue", { url, filename });
}

async function postMedia(manifestUrl, meta) {
  return sendBridgeRequest("/enqueue-media", {
    manifest_url: manifestUrl,
    media_type: meta.mediaType,
    source_url: meta.sourceUrl,
    title: meta.title,
    headers: meta.headers || {},
  });
}

async function handleDownload(item, source = "auto") {
  if (!item || !item.url) {
    return;
  }

  const finalUrl = item.finalUrl || item.url;
  const filename = item.filename || "";

  const success = await postDownload(finalUrl, filename);
  if (!success) {
    if (source === "context") {
      chrome.notifications.create({
        type: "basic",
        iconUrl: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAALElEQVR4AWMYmIj/BwSxgAnG////PwMRv0jC4TBiTbiKBIYVwzAEJgYAgBk1A8iMJHN6AAAAAElFTkSuQmCC",
        title: "PyIDM Bridge",
        message: "Could not reach the PyIDM desktop bridge.",
        priority: 1,
      });
    }
    return;
  }

  if (source === "context") {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAALElEQVR4AWMYmIj/BwSxgAnG////PwMRv0jC4TBiTbiKBIYVwzAEJgYAgBk1A8iMJHN6AAAAAElFTkSuQmCC",
      title: "PyIDM Bridge",
      message: "Link sent to PyIDM.",
      priority: 0,
    });
    return;
  }

  if (item.byExtensionId === chrome.runtime.id) {
    return;
  }

  chrome.downloads.cancel(item.id, () => {
    chrome.downloads.erase({ id: item.id });
  });

  chrome.notifications.create({
    type: "basic",
    iconUrl: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAALElEQVR4AWMYmIj/BwSxgAnG////PwMRv0jC4TBiTbiKBIYVwzAEJgYAgBk1A8iMJHN6AAAAAElFTkSuQmCC",
    title: "PyIDM Bridge",
    message: "Download handed over to PyIDM.",
    priority: 0,
  });
}

function registerContextMenu() {
  chrome.contextMenus.create({
    id: CONTEXT_MENU_ID,
    title: "Download with PyIDM",
    contexts: ["link"],
  });
}

chrome.runtime.onInstalled.addListener(() => {
  registerContextMenu();
});

chrome.runtime.onStartup?.addListener(() => {
  registerContextMenu();
});

async function getTabMetadata(tabId) {
  if (typeof tabId !== "number" || tabId < 0) {
    return { title: "", url: "" };
  }
  return new Promise((resolve) => {
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) {
        resolve({ title: "", url: "" });
        return;
      }
      resolve({
        title: tab?.title || "",
        url: tab?.url || "",
      });
    });
  });
}

function shouldHandleMedia(url) {
  return MEDIA_PATTERNS.some((pattern) => pattern.test(url));
}

function markMediaSeen(url) {
  const now = Date.now();
  const lastSeen = recentMedia.get(url);
  if (lastSeen && now - lastSeen < MEDIA_CACHE_TTL) {
    return false;
  }
  recentMedia.set(url, now);
  return true;
}

async function handleMediaRequest(details) {
  if (!shouldHandleMedia(details.url)) {
    return;
  }
  if (!markMediaSeen(details.url)) {
    return;
  }

  const tabMeta = await getTabMetadata(details.tabId);
  const mediaType = /\.mpd(\?|$)/i.test(details.url) ? "dash" : "hls";
  const success = await postMedia(details.url, {
    mediaType,
    sourceUrl: tabMeta.url || details.initiator || details.documentUrl || "",
    title: tabMeta.title || details.url,
  });

  if (success) {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAALElEQVR4AWMYmIj/BwSxgAnG////PwMRv0jC4TBiTbiKBIYVwzAEJgYAgBk1A8iMJHN6AAAAAElFTkSuQmCC",
      title: "PyIDM Bridge",
      message: "Captured media stream and forwarded to PyIDM.",
      priority: 0,
    });
  }
}

chrome.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== CONTEXT_MENU_ID || !info.linkUrl) {
    return;
  }
  await handleDownload({ url: info.linkUrl, filename: info.linkUrl.split("/").pop() }, "context");
});

chrome.downloads.onCreated.addListener(async (item) => {
  await handleDownload(item, "auto");
});

chrome.webRequest.onCompleted.addListener(
  async (details) => {
    await handleMediaRequest(details);
  },
  {
    urls: ["<all_urls>"],
    types: ["xmlhttprequest", "media", "other"],
  },
);

