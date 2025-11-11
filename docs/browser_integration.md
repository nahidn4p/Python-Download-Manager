# Browser Integration Guide

The desktop app now exposes a lightweight HTTP bridge so browser extensions can hand downloads directly to PyIDM. This document walks you through setup for Chromium-based browsers (Chrome, Edge, Brave, Vivaldi) and Firefox.

## 1. Run the desktop bridge

1. Start the PyIDM application (`python main.py`).
2. You should see a log entry similar to:
   ```
   [Bridge] Listening on http://127.0.0.1:17894
   ```
   The extension will try this port first and fall back to nearby ports (17895–17909) if needed.

> The bridge listens only on `127.0.0.1` and accepts JSON payloads at `/enqueue` (downloads) and `/enqueue-media` (stream capture). If the bridge fails to start, check for port conflicts or firewall rules.

## 2. Load the browser extension

The source lives in `browser_extension/`. It is a Manifest V3 WebExtension that works out of the box in Chromium browsers and can be repackaged for Firefox.

### Chrome / Edge / Brave / Vivaldi

1. Open `chrome://extensions/`.
2. Enable **Developer mode** (top right).
3. Click **Load unpacked** and select the `browser_extension/` directory.
4. After loading, the extension intercepts new downloads automatically. Use the toolbar action or the link context menu (**Download with PyIDM**) to hand off specific URLs.
5. Media playlists (`.m3u8`/`.mpd`) detected in the active tab are forwarded to the desktop app automatically; a notification confirms when a stream is captured.

To package a `.crx` for distribution, you can use Chrome’s **Pack extension** button or command-line tools, pointing at the same folder.

### Firefox (`.xpi`)

Firefox still relies on Manifest V2 for some APIs, but this extension works using `web-ext` and MV3 support in recent versions:

1. Install Mozilla’s toolchain:
   ```bash
   npm install --global web-ext
   ```
2. From the project root run:
   ```bash
   web-ext run --source-dir browser_extension
   ```
   This opens Firefox with the extension loaded temporarily.
3. To build an `.xpi`:
   ```bash
   web-ext build --source-dir browser_extension --overwrite-dest
   ```
   The packaged add-on lives under `browser_extension/web-ext-artifacts/`.

If you need full Manifest V2 compatibility, duplicate the extension folder and adjust the manifest accordingly (permissions and background script registration).

## 3. Customising the bridge host/port

The extension checks `chrome.storage.sync` for an optional list of explicit bridge base URLs (without the endpoint path). You can set them in the browser console:

```js
chrome.storage.sync.set({
  pyidmBridgeConfig: {
    hosts: ["http://127.0.0.1:17894"]
  }
});
```

Otherwise it tries `http://127.0.0.1:17894` up to `http://127.0.0.1:17909`.

## 4. How interception works

- When the browser creates a download (`chrome.downloads.onCreated`), the extension POSTs `{ url, filename }` to the bridge.
- Upon a successful response, the browser download is cancelled and a notification confirms hand-off.
- The context-menu action skips the cancellation step and simply forwards the link to PyIDM.
- When the extension sees an HLS (`.m3u8`) or DASH (`.mpd`) manifest via `webRequest`, it forwards the manifest URL, tab title, and source page to `/enqueue-media`.
- Within the desktop app, bridge payloads queue new `DownloadTask` instances using the default destination folder and start them immediately. Duplicate URLs are ignored.
- Media manifests are parsed, highest-bandwidth HLS variants are selected automatically, and segments are merged into a `.ts` file.

> DRM-protected or encrypted streams (Widevine/FairPlay) remain unavailable; the downloader currently supports clear HLS playlists with TS segments.

## 5. Troubleshooting

| Symptom | Possible fix |
| --- | --- |
| Browser notification: "Could not reach the PyIDM desktop bridge." | Ensure the desktop app is running and the bridge log shows an active port; check firewalls. |
| Downloads still complete in browser | Another extension may be cancelling; confirm PyIDM bridge is enabled and devtools console shows no fetch errors. |
| Want to keep the original browser download | Disable the PyIDM extension temporarily or use the browser’s download manager. |
| Port conflicts | Change `DEFAULT_PORT` in `browser_bridge.py`; optionally update the extension storage `hosts` list. |

## 6. Troubleshooting media capture

| Symptom | Possible fix |
| --- | --- |
| Notification never appears when playing a video | Some players use DRM or WebRTC; check devtools Network tab for `.m3u8` requests. |
| Stream added but download fails immediately | Playlist may reference fMP4 segments; current release only supports TS segments. |
| Wrong audio/video language | Manifests with multiple renditions default to highest bandwidth; manual selection UI is planned. |

## 7. Packaging overview

- **Chrome `.crx`**: Use Chrome’s built-in packing tool with the extension directory and private key.
- **Firefox `.xpi`**: Use `web-ext build` as shown above.

Signing is required for publishing to Chrome Web Store or AMO; refer to each platform’s documentation for submission requirements.

---

With the bridge and extension in place, browser downloads hand off to the PyIDM scheduler automatically, keeping the desktop app as the single download queue—similar to IDM’s browser integration.

