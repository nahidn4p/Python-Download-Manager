# downloader.py
import os
import math
import threading
import requests
import time
import urllib3
from urllib.parse import urlparse, unquote, urljoin

# Disable SSL warnings (for self-signed certificates)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CHUNK_SIZE = 64 * 1024  # 64KB


def safe_filename_from_url(url):
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or "download"
    name = unquote(name)
    return name


class DownloadTask:
    """
    Represents a single download job.
    - Run with .start()
    - Call .pause() to stop (keeps partial parts)
    - Call .resume() to continue (skips completed parts)
    """

    def __init__(self, url, dest_folder=".", threads=4, temp_root="data/temp",
                 scheduled_start=None, scheduled_end=None, repeat_interval=0,
                 media_info=None):
        self.url = url
        self.threads = max(1, int(threads))
        self.dest_folder = dest_folder
        os.makedirs(dest_folder, exist_ok=True)
        self.filename = safe_filename_from_url(url)
        self.dest_path = os.path.join(dest_folder, self.filename)
        self.session = requests.Session()
        
        # Configure session headers to avoid 403 errors
        parsed_url = urlparse(url)
        referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Referer': referer,
        })
        
        # Disable SSL verification to handle SSL errors (use with caution)
        self.session.verify = False

        # temp dir for this task
        self.temp_root = temp_root
        self.task_temp = os.path.join(self.temp_root, f"{self.filename}.parts")
        os.makedirs(self.task_temp, exist_ok=True)

        # state
        self.total_size = 0
        self.downloaded = 0  # aggregated across parts
        self.status = "queued"  # queued, downloading, paused, completed, error
        self.error = None

        # threading control
        self._worker_thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # internal: remember last update time/bytes for speed calc
        self._last_bytes = 0
        self._last_time = time.time()
        self.speed_bps = 0.0

        # scheduling
        self.scheduled_start = self._coerce_datetime_input(scheduled_start)
        self.scheduled_end = self._coerce_datetime_input(scheduled_end)
        self.repeat_interval = int(repeat_interval or 0)
        self.media_info = media_info or None
        self.media_state = {
            "segments_total": 0,
            "segments_done": 0,
        }

    # -------------------------
    # HTTP helpers
    # -------------------------
    def _get_file_info(self, timeout=10):
        """
        Get file info using GET with Range header instead of HEAD.
        Some servers block HEAD requests but allow GET with Range.
        """
        # Try HEAD first, if it fails, use GET with Range: bytes=0-0
        try:
            r = self.session.head(self.url, allow_redirects=True, timeout=timeout, verify=False)
            if r.status_code in (200, 206):
                return r
        except Exception:
            pass
        
        # Fallback: Use GET with Range header to get just the first byte
        # Use simpler headers for this request
        headers = {
            'Range': 'bytes=0-0',
            'Accept': '*/*',
            'Accept-Encoding': 'identity',
        }
        r = self.session.get(self.url, headers=headers, allow_redirects=True, timeout=timeout, verify=False, stream=True)
        return r

    def supports_range_and_size(self):
        try:
            r = self._get_file_info()
            r.raise_for_status()
            
            # Check Accept-Ranges header
            accept = r.headers.get("Accept-Ranges", "")
            supports_range = "bytes" in accept.lower()
            
            # Get Content-Length
            length = r.headers.get("Content-Length")
            if length and length.isdigit():
                total_size = int(length)
            else:
                # For Range requests, Content-Range header might have the total size
                content_range = r.headers.get("Content-Range", "")
                if "/" in content_range:
                    total_size = int(content_range.split("/")[-1])
                else:
                    total_size = None
            
            return supports_range, total_size
        except Exception as e:
            # If we get 403 or other errors, return False
            return False, None

    # -------------------------
    # Worker functions
    # -------------------------
    def _download_range(self, start, end, part_path):
        # Create headers with Range request - use simpler headers for download requests
        headers = {
            "Range": f"bytes={start}-{end}",
            "Accept": "*/*",
            "Accept-Encoding": "identity",  # Don't compress range requests
        }
        # If a complete part file exists, skip
        if os.path.exists(part_path):
            existing = os.path.getsize(part_path)
            expected = (end - start + 1)
            if existing == expected:
                with self._lock:
                    self.downloaded += existing
                return
            # if partial, we'll overwrite in this simple implementation
        try:
            with self.session.get(self.url, headers=headers, stream=True, timeout=30, verify=False, allow_redirects=True) as r:
                # Accept both 200 (full file) and 206 (partial content) status codes
                if r.status_code not in (200, 206):
                    r.raise_for_status()
                with open(part_path, "wb") as f:
                    for chunk in r.iter_content(CHUNK_SIZE):
                        if self._stop_event.is_set():
                            return
                        if chunk:
                            f.write(chunk)
                            with self._lock:
                                self.downloaded += len(chunk)
        except Exception as e:
            # propagate by setting error flag; upper runner will handle
            raise

    def _merge_parts(self, parts):
        # stream-merge parts to final file
        with open(self.dest_path, "wb") as out:
            for p in parts:
                with open(p, "rb") as src:
                    while True:
                        chunk = src.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
        # remove part files
        for p in parts:
            try:
                os.remove(p)
            except Exception:
                pass

    def _single_stream_download(self, dest_path):
        try:
            with self.session.get(self.url, stream=True, timeout=30, verify=False, allow_redirects=True) as r:
                r.raise_for_status()
                with open(dest_path, "ab") as f:
                    for chunk in r.iter_content(CHUNK_SIZE):
                        if self._stop_event.is_set():
                            return
                        if chunk:
                            f.write(chunk)
                            with self._lock:
                                self.downloaded += len(chunk)
        except Exception:
            raise

    def _run(self):
        """
        The main worker that runs in a background thread.
        """
        print(f"[DownloadTask] Starting download: {self.url}")

        self.status = "starting"
        self.error = None
        self._stop_event.clear()

        try:
            # Calculate downloaded count from existing part files FIRST (before getting file info)
            # This preserves progress when resuming
            self.downloaded = 0
            if os.path.exists(self.task_temp):
                for f in os.listdir(self.task_temp):
                    fp = os.path.join(self.task_temp, f)
                    if os.path.isfile(fp):
                        self.downloaded += os.path.getsize(fp)
            
            # Media downloads are handled via special pipeline
            if self.media_info:
                self._run_media_download()
                return

            # Get file info from server
            supports_range, total = self.supports_range_and_size()
            if total is None:
                total = 0
            
            # Preserve existing total_size if we have one (from restore), otherwise use server value
            if self.total_size > 0 and total > 0:
                # Use the larger value (in case file was updated)
                self.total_size = max(self.total_size, total)
            elif total > 0:
                self.total_size = total
            # If total is 0, keep existing total_size if we have one

            if not supports_range or self.total_size == 0:
                # fallback single-stream (no range)
                self.status = "downloading"
                self._single_stream_download(self.dest_path)
                if self._stop_event.is_set():
                    self.status = "paused"
                    return
                self.status = "completed"
                return

            # segmented download
            part_size = math.ceil(self.total_size / self.threads)
            parts = []
            threads = []

            # start per-part threads
            for i in range(self.threads):
                start = i * part_size
                end = min(start + part_size - 1, self.total_size - 1)
                part_path = os.path.join(self.task_temp, f"part_{i}.tmp")
                parts.append(part_path)
                # If part already exists and size matches expected, skip thread
                if os.path.exists(part_path):
                    existing = os.path.getsize(part_path)
                    expected = end - start + 1
                    if existing == expected:
                        continue
                t = threading.Thread(target=self._download_range, args=(start, end, part_path), daemon=True)
                threads.append(t)

            # if there are no threads (all parts already present), just merge
            if not threads:
                self._merge_parts(parts)
                self.status = "completed"
                return

            self.status = "downloading"
            for t in threads:
                t.start()

            # monitor threads
            while any(t.is_alive() for t in threads):
                if self._stop_event.is_set():
                    # leave partial files intact and stop
                    self.status = "paused"
                    return
                time.sleep(0.25)
                # update speed
                now = time.time()
                with self._lock:
                    d = self.downloaded
                dt = now - self._last_time
                if dt >= 0.5:
                    self.speed_bps = (d - self._last_bytes) / dt if dt > 0 else 0.0
                    self._last_time = now
                    self._last_bytes = d

            # all parts finished -> merge
            self._merge_parts(parts)
            self.status = "completed"
            # set speed to zero
            self.speed_bps = 0.0
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)

    # -------------------------
    # Public control API
    # -------------------------
    def start(self):
        """Start downloading (from queued or paused)."""
        if self.status == "downloading":
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._run, daemon=True)
        self._worker_thread.start()

    def pause(self):
        """Pause/stop the current download (partial parts preserved)."""
        if self.status in ("downloading", "starting"):
            self._stop_event.set()
            # wait a short time for threads to respect stop
            if self._worker_thread:
                self._worker_thread.join(timeout=1.0)
            self.status = "paused"

    def resume(self):
        """Alias to start (keeps partial parts and resumes)."""
        if self.status in ("paused", "queued", "error"):
            # reset stop flag and start new worker thread
            self.start()

    def is_alive(self):
        return self._worker_thread is not None and self._worker_thread.is_alive()
    
    # -------------------------
    # Serialization for persistence
    # -------------------------
    def to_dict(self):
        """Convert task to dictionary for saving."""
        return {
            'url': self.url,
            'dest_folder': self.dest_folder,
            'threads': self.threads,
            'filename': self.filename,
            'total_size': self.total_size,
            'downloaded': self.downloaded,
            'status': self.status,
            'error': self.error,
            'temp_root': self.temp_root,
            'scheduled_start': self.scheduled_start,
            'scheduled_end': self.scheduled_end,
            'repeat_interval': self.repeat_interval,
            'media_info': self.media_info,
            'media_state': self.media_state,
        }
    
    @classmethod
    def from_dict(cls, data):
        """Create task from dictionary."""
        task = cls(
            url=data['url'],
            dest_folder=data['dest_folder'],
            threads=data.get('threads', 4),
            temp_root=data.get('temp_root', 'data/temp'),
            scheduled_start=data.get('scheduled_start'),
            scheduled_end=data.get('scheduled_end'),
            repeat_interval=data.get('repeat_interval', 0),
            media_info=data.get('media_info'),
        )
        task.total_size = data.get('total_size', 0)
        task.downloaded = data.get('downloaded', 0)
        task.status = data.get('status', 'paused')
        task.error = data.get('error')
        task.media_state = data.get('media_state', {'segments_total': 0, 'segments_done': 0})
        
        # Restore downloaded count from existing part files
        if os.path.exists(task.task_temp):
            task.downloaded = 0
            for f in os.listdir(task.task_temp):
                fp = os.path.join(task.task_temp, f)
                if os.path.isfile(fp):
                    task.downloaded += os.path.getsize(fp)
        
        return task

    # -------------------------
    # Scheduling helpers
    # -------------------------
    def _coerce_datetime_input(self, value):
        if not value:
            return None
        if isinstance(value, str):
            return value
        try:
            from datetime import datetime, timezone
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
        raise TypeError("scheduled_start/end must be ISO string or datetime")

    def update_schedule(self, start=None, end=None, repeat_interval=0):
        self.scheduled_start = self._coerce_datetime_input(start)
        self.scheduled_end = self._coerce_datetime_input(end)
        self.repeat_interval = int(repeat_interval or 0)

    # -------------------------
    # Media helpers
    # -------------------------
    def _run_media_download(self):
        self.status = "starting"
        self.error = None
        self._stop_event.clear()
        self.downloaded = 0
        self._last_bytes = 0
        self._last_time = time.time()
        self.total_size = 0
        try:
            media_type = (self.media_info or {}).get("media_type", "hls").lower()
            if media_type == "hls":
                self._download_hls_media()
            else:
                raise ValueError(f"Unsupported media type: {media_type}")
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)

    def _fetch_text(self, url, headers=None, timeout=15):
        hdrs = self.session.headers.copy()
        if headers:
            hdrs.update(headers)
        resp = self.session.get(url, headers=hdrs, timeout=timeout, verify=False, allow_redirects=True)
        resp.raise_for_status()
        return resp.text

    def _download_binary(self, url, file_obj, headers=None):
        hdrs = self.session.headers.copy()
        if headers:
            hdrs.update(headers)
        with self.session.get(url, headers=hdrs, stream=True, timeout=30, verify=False, allow_redirects=True) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(CHUNK_SIZE):
                if self._stop_event.is_set():
                    return False
                if chunk:
                    file_obj.write(chunk)
                    with self._lock:
                        self.downloaded += len(chunk)
            return True

    def _parse_hls_playlist(self, text, manifest_url):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("#EXTM3U"):
            raise ValueError("Invalid HLS playlist")

        variants = []
        segments = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#EXT-X-STREAM-INF"):
                attrs = self._parse_attribute_list(line)
                bandwidth = int(attrs.get("BANDWIDTH", 0))
                resolution = attrs.get("RESOLUTION")
                uri = None
                j = i + 1
                while j < len(lines):
                    if not lines[j].startswith("#"):
                        uri = lines[j]
                        break
                    j += 1
                if uri:
                    variants.append({
                        "uri": urljoin(manifest_url, uri),
                        "bandwidth": bandwidth,
                        "resolution": resolution,
                    })
                i = j
            elif line.startswith("#EXTINF"):
                # Segment duration line, next line should be the segment URL
                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    segment_url = lines[i + 1]
                    segments.append(urljoin(manifest_url, segment_url))
                    i += 2
                    continue
            elif not line.startswith("#"):
                # Direct segment URL (some playlists don't use #EXTINF)
                segments.append(urljoin(manifest_url, line))
            i += 1

        if variants:
            # choose highest bandwidth variant
            variants.sort(key=lambda v: v.get("bandwidth", 0), reverse=True)
            return {"type": "master", "variants": variants}

        if not segments:
            raise ValueError("Playlist has no segments")
        return {"type": "media", "segments": segments}

    def _parse_attribute_list(self, line):
        # line like #EXT-X-STREAM-INF:BANDWIDTH=1234,RESOLUTION=1920x1080
        attrs = {}
        if ":" not in line:
            return attrs
        parts = line.split(":", 1)[1]
        for chunk in parts.split(","):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            value = value.strip().strip('"')
            attrs[key.strip()] = value
        return attrs

    def _download_hls_media(self):
        headers = (self.media_info or {}).get("headers") or {}
        manifest_url = (self.media_info or {}).get("manifest_url") or self.url

        manifest_text = self._fetch_text(manifest_url, headers=headers)
        parsed = self._parse_hls_playlist(manifest_text, manifest_url)

        if parsed["type"] == "master":
            target_variant = parsed["variants"][0]
            manifest_url = target_variant["uri"]
            manifest_text = self._fetch_text(manifest_url, headers=headers)
            parsed = self._parse_hls_playlist(manifest_text, manifest_url)

        segments = parsed.get("segments", [])
        self.media_state["segments_total"] = len(segments)
        self.media_state["segments_done"] = 0

        if not segments:
            raise ValueError("No media segments found")

        temp_path = f"{self.dest_path}.downloading"
        # ensure folder
        os.makedirs(os.path.dirname(self.dest_path), exist_ok=True)

        self.status = "downloading"
        with open(temp_path, "wb") as out:
            for idx, segment_url in enumerate(segments, start=1):
                if self._stop_event.is_set():
                    self.status = "paused"
                    return

                success = self._download_binary(segment_url, out, headers=headers)
                if not success:
                    self.status = "paused"
                    return

                self.media_state["segments_done"] = idx

                # update speed
                now = time.time()
                with self._lock:
                    d = self.downloaded
                dt = now - self._last_time
                if dt >= 0.5:
                    self.speed_bps = (d - self._last_bytes) / dt if dt > 0 else 0.0
                    self._last_time = now
                    self._last_bytes = d

        os.replace(temp_path, self.dest_path)
        self.status = "completed"
        self.speed_bps = 0.0
