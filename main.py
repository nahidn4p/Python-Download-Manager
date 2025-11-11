# main.py
import sys
import os
import sqlite3
import threading
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QFileDialog,
    QTableWidget, QTableWidgetItem, QWidget as QW, QHeaderView, QMessageBox, QTextEdit,
    QDialog, QFormLayout, QDateTimeEdit, QCheckBox, QComboBox, QDialogButtonBox
)
from PyQt6.QtCore import QTimer, Qt, QDateTime
from datetime import datetime, timedelta, timezone
from downloader import DownloadTask, safe_filename_from_url
from browser_bridge import BrowserBridge

DEFAULT_THREADS_PER_TASK = 4
DB_FILE = "data/downloads.db"
REPEAT_CHOICES = [
    ("No repeat", 0),
    ("Hourly", 3600),
    ("Daily", 86400),
    ("Weekly", 604800),
]


class ScheduleDialog(QDialog):
    def __init__(self, parent, task):
        super().__init__(parent)
        self.setWindowTitle("Schedule Download")
        self.setModal(True)

        layout = QVBoxLayout()
        form = QFormLayout()

        self.start_checkbox = QCheckBox("Enable start time")
        self.start_edit = QDateTimeEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start_edit.setDateTime(QDateTime.currentDateTime().addSecs(60))

        self.end_checkbox = QCheckBox("Enable stop time")
        self.end_edit = QDateTimeEdit()
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.end_edit.setDateTime(QDateTime.currentDateTime().addSecs(3600))

        self.repeat_combo = QComboBox()
        for label, seconds in REPEAT_CHOICES:
            self.repeat_combo.addItem(label, seconds)

        self.start_checkbox.toggled.connect(self.start_edit.setEnabled)
        self.start_checkbox.toggled.connect(self.repeat_combo.setEnabled)
        self.end_checkbox.toggled.connect(self.end_edit.setEnabled)

        # Initialize from task schedule
        parent_window = parent if hasattr(parent, "_parse_iso_datetime") else None
        if task and parent_window:
            start_dt = parent_window._parse_iso_datetime(task.scheduled_start)
            if start_dt:
                self.start_checkbox.setChecked(True)
                self.start_edit.setEnabled(True)
                self.start_edit.setDateTime(parent_window._qdatetime_from_utc(start_dt))
            else:
                self.start_checkbox.setChecked(False)
                self.start_edit.setEnabled(False)
                self.repeat_combo.setEnabled(False)

            end_dt = parent_window._parse_iso_datetime(task.scheduled_end)
            if end_dt:
                self.end_checkbox.setChecked(True)
                self.end_edit.setEnabled(True)
                self.end_edit.setDateTime(parent_window._qdatetime_from_utc(end_dt))
            else:
                self.end_checkbox.setChecked(False)
                self.end_edit.setEnabled(False)

            repeat_seconds = int(task.repeat_interval or 0)
            index = next((i for i, (_, s) in enumerate(REPEAT_CHOICES) if s == repeat_seconds), 0)
            self.repeat_combo.setCurrentIndex(index)
            if not self.start_checkbox.isChecked():
                self.repeat_combo.setEnabled(False)
        else:
            self.start_checkbox.setChecked(False)
            self.start_edit.setEnabled(False)
            self.end_checkbox.setChecked(False)
            self.end_edit.setEnabled(False)
            self.repeat_combo.setEnabled(False)

        form.addRow(self.start_checkbox, self.start_edit)
        form.addRow(self.end_checkbox, self.end_edit)
        form.addRow("Repeat", self.repeat_combo)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def get_values(self):
        start = None
        end = None
        repeat = 0

        if self.start_checkbox.isChecked():
            start_epoch = self.start_edit.dateTime().toSecsSinceEpoch()
            start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
            repeat = int(self.repeat_combo.currentData() or 0)

        if self.end_checkbox.isChecked():
            end_epoch = self.end_edit.dateTime().toSecsSinceEpoch()
            end = datetime.fromtimestamp(end_epoch, tz=timezone.utc)

        return start, end, repeat

    def accept(self):
        start, end, repeat = self.get_values()
        if end and start and end <= start:
            QMessageBox.warning(self, "Invalid schedule", "Stop time must be after start time.")
            return
        if repeat > 0 and not start:
            QMessageBox.warning(self, "Invalid schedule", "Repeat requires a start time.")
            return
        super().accept()


class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)
        
        # Load current settings
        settings = parent.settings
        
        layout = QVBoxLayout()
        form = QFormLayout()
        
        # Default download folder
        folder_layout = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setText(settings.get("default_folder", parent.default_folder))
        folder_btn = QPushButton("Browse...")
        folder_btn.clicked.connect(self.choose_folder)
        folder_layout.addWidget(self.folder_edit)
        folder_layout.addWidget(folder_btn)
        form.addRow("Default Download Folder:", folder_layout)
        
        # Threads per task
        self.threads_spin = QComboBox()
        self.threads_spin.addItems(["1", "2", "4", "8", "16"])
        self.threads_spin.setCurrentText(str(settings.get("threads", DEFAULT_THREADS_PER_TASK)))
        form.addRow("Threads per Download:", self.threads_spin)
        
        # Auto-start downloads
        self.auto_start_check = QCheckBox()
        self.auto_start_check.setChecked(settings.get("auto_start", True))
        form.addRow("Auto-start New Downloads:", self.auto_start_check)
        
        # Bridge port
        self.port_spin = QLineEdit()
        self.port_spin.setText(str(settings.get("bridge_port", 17894)))
        self.port_spin.setPlaceholderText("17894")
        form.addRow("Bridge Port:", self.port_spin)
        
        # Max download speed (optional)
        speed_layout = QHBoxLayout()
        self.speed_limit_check = QCheckBox()
        self.speed_limit_check.setChecked(settings.get("speed_limit_enabled", False))
        self.speed_limit_edit = QLineEdit()
        self.speed_limit_edit.setText(settings.get("speed_limit", ""))
        self.speed_limit_edit.setPlaceholderText("MB/s")
        self.speed_limit_edit.setEnabled(self.speed_limit_check.isChecked())
        self.speed_limit_check.toggled.connect(self.speed_limit_edit.setEnabled)
        speed_layout.addWidget(self.speed_limit_check)
        speed_layout.addWidget(self.speed_limit_edit)
        speed_layout.addWidget(QLabel("(Not yet implemented)"))
        form.addRow("Speed Limit:", speed_layout)
        
        # Media settings
        self.media_auto_check = QCheckBox()
        self.media_auto_check.setChecked(settings.get("media_auto", True))
        form.addRow("Auto-capture Media Streams:", self.media_auto_check)
        
        layout.addLayout(form)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
        self.setLayout(layout)
    
    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Default Download Folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)
    
    def get_values(self):
        return {
            "default_folder": self.folder_edit.text(),
            "threads": int(self.threads_spin.currentText()),
            "auto_start": self.auto_start_check.isChecked(),
            "bridge_port": int(self.port_spin.text() or "17894"),
            "speed_limit_enabled": self.speed_limit_check.isChecked(),
            "speed_limit": self.speed_limit_edit.text(),
            "media_auto": self.media_auto_check.isChecked(),
        }


class IDMWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyIDM - Multi Download Manager")
        self.resize(950, 500)
        self.tasks = []  # list of DownloadTask objects
        self.bridge = None

        # --- top controls ---
        top_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste download URL here...")
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self.add_task_dialog)
        self.folder_btn = QPushButton("Choose Folder")
        self.folder_btn.clicked.connect(self.choose_folder)
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        # Settings
        self.settings = self.load_settings()
        self.default_folder = self.settings.get("default_folder", os.getcwd())
        self.folder_label = QLabel(self.default_folder)

        top_layout.addWidget(self.url_input)
        top_layout.addWidget(self.add_btn)
        top_layout.addWidget(self.folder_btn)
        top_layout.addWidget(self.settings_btn)
        top_layout.addWidget(self.folder_label)

        # --- table ---
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "File", "Progress", "Status", "Speed", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(2, 200)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 320)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)

        # --- log text ---
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(120)

        # --- bottom controls ---
        bottom_layout = QHBoxLayout()
        self.start_all_btn = QPushButton("Start All")
        self.start_all_btn.clicked.connect(self.start_all)
        self.pause_all_btn = QPushButton("Pause All")
        self.pause_all_btn.clicked.connect(self.pause_all)
        self.clear_completed_btn = QPushButton("Clear Completed")
        self.clear_completed_btn.clicked.connect(self.clear_completed)

        bottom_layout.addWidget(self.start_all_btn)
        bottom_layout.addWidget(self.pause_all_btn)
        bottom_layout.addWidget(self.clear_completed_btn)
        bottom_layout.addStretch()

        # main layout
        layout = QVBoxLayout()
        layout.addLayout(top_layout)
        layout.addWidget(self.table)
        layout.addWidget(self.log_box)
        layout.addLayout(bottom_layout)
        self.setLayout(layout)

        # timer to refresh UI
        self.timer = QTimer()
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.refresh_table)
        self.timer.start()
        
        # Initialize database
        self.init_database()
        
        # Load previous incomplete downloads
        self.load_tasks()

        # Start browser bridge
        self._init_bridge()

    # ------------------ GUI callbacks ------------------
    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", self.default_folder)
        if folder:
            self.default_folder = folder
            self.folder_label.setText(folder)
            self.settings["default_folder"] = folder
            self.save_settings()

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_settings = dialog.get_values()
            self.settings.update(new_settings)
            self.save_settings()
            
            # Apply settings
            self.default_folder = self.settings.get("default_folder", os.getcwd())
            self.folder_label.setText(self.default_folder)
            
            # Restart bridge if port changed
            if self.bridge:
                old_port = getattr(self.bridge, 'port', 17894)
                new_port = self.settings.get("bridge_port", 17894)
                if old_port != new_port:
                    self.bridge.stop()
                    self.bridge = None
                    self._init_bridge()
            
            self.log("[Settings] Configuration updated")

    def load_settings(self):
        """Load settings from file or return defaults."""
        settings_file = "data/settings.json"
        defaults = {
            "default_folder": os.getcwd(),
            "threads": DEFAULT_THREADS_PER_TASK,
            "auto_start": True,
            "bridge_port": 17894,
            "speed_limit_enabled": False,
            "speed_limit": "",
            "media_auto": True,
        }
        
        if os.path.exists(settings_file):
            try:
                import json
                with open(settings_file, 'r') as f:
                    loaded = json.load(f)
                    defaults.update(loaded)
            except Exception as e:
                print(f"Error loading settings: {e}")
        
        return defaults

    def save_settings(self):
        """Save settings to file."""
        settings_file = "data/settings.json"
        try:
            os.makedirs(os.path.dirname(settings_file), exist_ok=True)
            import json
            with open(settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            print(f"Error saving settings: {e}")

    def add_task_dialog(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "No URL", "Please paste a download URL first.")
            return
        self.add_task(url, self.default_folder)
        self.url_input.clear()

    def add_task(self, url, folder):
        # create task with progress callback
        threads = self.settings.get("threads", DEFAULT_THREADS_PER_TASK)
        task = DownloadTask(
            url,
            dest_folder=folder,
            threads=threads
        )
        self.tasks.append(task)
        self._add_table_row(task)
        self.log(f"[Added] {task.filename}")
        self.save_task(task)  # Save after adding
        
        # Auto-start if enabled
        if self.settings.get("auto_start", True):
            task.start()

    def _add_table_row(self, task):
        row = self.table.rowCount()
        self.table.insertRow(row)

        # id
        id_item = QTableWidgetItem(str(row + 1))
        id_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 0, id_item)

        # file name
        self.table.setItem(row, 1, QTableWidgetItem(task.filename))

        # progress bar
        progress_bar = QProgressBar()
        progress_bar.setMaximum(100)
        # Set initial progress if task has progress
        if task.total_size and task.total_size > 0:
            percent = int((task.downloaded / task.total_size) * 100)
            progress_bar.setValue(percent)
        else:
            progress_bar.setValue(0)
        self.table.setCellWidget(row, 2, progress_bar)

        # status
        status_item = QTableWidgetItem(task.status)
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 3, status_item)

        # speed
        speed_item = QTableWidgetItem("0 B/s")
        speed_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 4, speed_item)

        # actions
        action_widget = QW()
        act_layout = QHBoxLayout()
        start_btn = QPushButton("Start")
        pause_btn = QPushButton("Pause")
        resume_btn = QPushButton("Resume")
        remove_btn = QPushButton("Remove")
        schedule_btn = QPushButton("Schedule")

        start_btn.clicked.connect(lambda _, r=row: self.start_task(r))
        pause_btn.clicked.connect(lambda _, r=row: self.pause_task(r))
        resume_btn.clicked.connect(lambda _, r=row: self.resume_task(r))
        remove_btn.clicked.connect(lambda _, r=row: self.remove_task(r))
        schedule_btn.clicked.connect(lambda _, r=row: self.schedule_task(r))

        act_layout.addWidget(start_btn)
        act_layout.addWidget(pause_btn)
        act_layout.addWidget(resume_btn)
        act_layout.addWidget(remove_btn)
        act_layout.addWidget(schedule_btn)
        act_layout.setContentsMargins(0, 0, 0, 0)
        action_widget.setLayout(act_layout)
        self.table.setCellWidget(row, 5, action_widget)

    def refresh_table(self):
        try:
            self._consume_bridge_requests()
        except Exception as exc:
            self.log(f"[Bridge Error] {exc}")

        needs_save = False
        tasks_to_save = []
        for idx, task in enumerate(list(self.tasks)):
            if idx >= self.table.rowCount():
                continue
            progress_widget = self.table.cellWidget(idx, 2)
            status_item = self.table.item(idx, 3)
            speed_item = self.table.item(idx, 4)

            schedule_changed, schedule_needs_save = self._enforce_schedule(task)

            total = task.total_size
            downloaded = task.downloaded

            if total and total > 0:
                percent = int((downloaded / total) * 100)
                progress_widget.setMaximum(100)
                progress_widget.setValue(percent)
            else:
                progress_widget.setMaximum(100)
                # crude approximation if unknown size
                progress_widget.setValue(min(downloaded * 100 // 1024 // 1024, 100))

            old_status = status_item.text()
            status_item.setText(task.status)
            speed_item.setText(self._format_speed(task.speed_bps))
            tooltip_parts = []
            schedule_description = self._schedule_description(task)
            if schedule_description:
                tooltip_parts.append(schedule_description)
            media_description = self._media_description(task)
            if media_description:
                tooltip_parts.append(media_description)
            status_item.setToolTip("\n".join(tooltip_parts))
            
            # Save if status changed or task is incomplete
            if old_status != task.status or task.status != "completed" or schedule_needs_save or schedule_changed:
                needs_save = True
                if task.status != "completed":
                    if task not in tasks_to_save:
                        tasks_to_save.append(task)
                elif schedule_needs_save:
                    if task not in tasks_to_save:
                        tasks_to_save.append(task)
            elif schedule_needs_save:
                needs_save = True
                if task not in tasks_to_save:
                    tasks_to_save.append(task)

            # log errors automatically
            if task.status == "error" and task.error:
                self.log(f"[ERROR] {task.filename}: {task.error}")
        
        # Save tasks that need updating (more efficient than saving all)
        if needs_save:
            for task in tasks_to_save:
                self.save_task(task)

    def _format_speed(self, bps):
        if bps is None or bps <= 0:
            return "0 B/s"
        if bps > 1024**2:
            return f"{bps / (1024**2):.2f} MB/s"
        if bps > 1024:
            return f"{bps / 1024:.2f} KB/s"
        return f"{bps:.0f} B/s"

    def _now_utc(self):
        return datetime.now(timezone.utc)

    def _parse_iso_datetime(self, value):
        if not value:
            return None
        try:
            text = value
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _format_local_datetime(self, dt):
        if not dt:
            return ""
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")

    def _qdatetime_from_utc(self, dt):
        if not dt:
            return QDateTime.currentDateTime()
        return QDateTime.fromSecsSinceEpoch(int(dt.timestamp()))

    def _schedule_description(self, task):
        start_dt = self._parse_iso_datetime(task.scheduled_start)
        end_dt = self._parse_iso_datetime(task.scheduled_end)
        parts = []
        if start_dt:
            parts.append(f"Starts {self._format_local_datetime(start_dt)}")
        if end_dt:
            parts.append(f"Stops {self._format_local_datetime(end_dt)}")
        repeat = int(task.repeat_interval or 0)
        if repeat > 0:
            label = next((label for label, seconds in REPEAT_CHOICES if seconds == repeat), None)
            if label:
                parts.append(f"Repeats {label.lower()}")
            else:
                parts.append(f"Repeats every {timedelta(seconds=repeat)}")
        return ", ".join(parts)

    def _media_description(self, task):
        if not getattr(task, "media_info", None):
            return ""
        info = task.media_state or {}
        total = info.get("segments_total") or 0
        done = info.get("segments_done") or 0
        if total:
            return f"Media segments: {done}/{total}"
        if done:
            return f"Media segments downloaded: {done}"
        return "Media capture in progress"

    def _advance_schedule(self, task, start_dt, end_dt, repeat, now):
        if repeat <= 0:
            return start_dt, end_dt, False

        updated = False
        while start_dt and end_dt and end_dt <= now:
            start_dt += timedelta(seconds=repeat)
            end_dt += timedelta(seconds=repeat)
            updated = True
        while start_dt and not end_dt and start_dt + timedelta(seconds=repeat) <= now:
            start_dt += timedelta(seconds=repeat)
            updated = True
        while not start_dt and end_dt and end_dt <= now:
            end_dt += timedelta(seconds=repeat)
            updated = True

        if updated:
            task.update_schedule(start_dt, end_dt, repeat)
        return start_dt, end_dt, updated

    def _enforce_schedule(self, task):
        start_dt = self._parse_iso_datetime(task.scheduled_start)
        end_dt = self._parse_iso_datetime(task.scheduled_end)
        repeat = int(task.repeat_interval or 0)
        now = self._now_utc()

        schedule_changed = False
        schedule_updated = False

        if repeat > 0:
            start_dt, end_dt, advanced = self._advance_schedule(task, start_dt, end_dt, repeat, now)
            if advanced:
                schedule_updated = True
                start_dt = self._parse_iso_datetime(task.scheduled_start)
                end_dt = self._parse_iso_datetime(task.scheduled_end)

        if not start_dt and not end_dt:
            if task.status == "scheduled":
                task.status = "queued"
                schedule_changed = True
            return schedule_changed, schedule_updated

        if start_dt and now < start_dt:
            if task.status == "downloading":
                task.pause()
                self.log(f"[Scheduled pause] {task.filename}")
                schedule_changed = True
            if task.status != "scheduled":
                task.status = "scheduled"
                schedule_changed = True
            return schedule_changed, schedule_updated

        if start_dt and now >= start_dt and (not end_dt or now < end_dt):
            if task.status in ("queued", "paused", "scheduled", "error"):
                task.error = None
                task.start()
                schedule_changed = True
            if repeat > 0 and not end_dt:
                next_start = start_dt + timedelta(seconds=repeat)
                task.update_schedule(next_start, None, repeat)
                schedule_updated = True
            return schedule_changed, schedule_updated

        if end_dt and now >= end_dt:
            if task.status == "downloading":
                task.pause()
                self.log(f"[Scheduled stop] {task.filename}")
                schedule_changed = True
            if repeat > 0:
                next_start = None
                current_start = self._parse_iso_datetime(task.scheduled_start)
                if current_start:
                    next_start = current_start + timedelta(seconds=repeat)
                elif start_dt:
                    next_start = start_dt + timedelta(seconds=repeat)
                next_end = end_dt + timedelta(seconds=repeat)
                task.update_schedule(next_start, next_end, repeat)
                schedule_updated = True
                if task.status != "scheduled":
                    task.status = "scheduled"
                    schedule_changed = True
            else:
                task.update_schedule(None, None, 0)
                schedule_updated = True
                if task.status not in ("paused", "queued", "completed"):
                    task.status = "paused"
                    schedule_changed = True
            return schedule_changed, schedule_updated

        return schedule_changed, schedule_updated

    # ------------------ Task actions ------------------
    def start_task(self, row):
        try:
            task = self.tasks[row]
        except IndexError:
            return

        if task.status in ("downloading",):
            return

        self.log(f"[Start] {task.filename}")
        task.start()

    def pause_task(self, row):
        try:
            task = self.tasks[row]
        except IndexError:
            return
        task.pause()
        self.log(f"[Paused] {task.filename}")

    def resume_task(self, row):
        try:
            task = self.tasks[row]
        except IndexError:
            return
        task.resume()
        self.log(f"[Resumed] {task.filename}")

    def _init_bridge(self):
        if self.bridge:
            return
        try:
            port = self.settings.get("bridge_port", 17894)
            self.bridge = BrowserBridge(port=port)
            self.bridge.start()
            bridge_addr = self.bridge.resolve_server_address()
            if bridge_addr:
                self.log(f"[Bridge] Listening on http://{bridge_addr[0]}:{bridge_addr[1]}")
            else:
                self.log("[Bridge] Failed to resolve bridge address")
        except Exception as exc:
            self.bridge = None
            self.log(f"[Bridge Error] Failed to start: {exc}")

    def _consume_bridge_requests(self):
        if not self.bridge:
            return
        payloads = self.bridge.poll_requests()
        for payload in payloads:
            kind = payload.get("kind", "download")
            if kind == "media":
                self._handle_media_request(payload)
            else:
                self._handle_download_request(payload)

    def _handle_download_request(self, payload):
        url = payload.get("url")
        if not url:
            return
        filename_hint = payload.get("filename")
        headers = payload.get("headers") or {}
        # skip if duplicate URL already queued
        if any(t.url == url and t.status != "completed" for t in self.tasks):
            self.log(f"[Bridge] Skipped duplicate URL: {url}")
            return
        threads = self.settings.get("threads", DEFAULT_THREADS_PER_TASK)
        task = DownloadTask(
            url,
            dest_folder=self.default_folder,
            threads=threads,
        )
        if filename_hint:
            task.filename = filename_hint
            task.dest_path = os.path.join(task.dest_folder, task.filename)
        if headers:
            task.session.headers.update(headers)
        self.tasks.append(task)
        self._add_table_row(task)
        self.log(f"[Bridge] Added {task.filename}")
        self.save_task(task)
        if self.settings.get("auto_start", True):
            task.start()

    def _handle_media_request(self, payload):
        # Check if media auto-capture is enabled
        if not self.settings.get("media_auto", True):
            return
        
        manifest_url = payload.get("manifest_url")
        if not manifest_url:
            return

        # avoid duplicate manifests
        if any((t.media_info or {}).get("manifest_url") == manifest_url and t.status != "completed" for t in self.tasks):
            self.log(f"[Media] Skipped duplicate manifest: {manifest_url}")
            return

        media_type = payload.get("media_type", "hls")
        title = payload.get("title") or payload.get("source_url") or manifest_url
        filename = safe_filename_from_url(title)
        if not filename.lower().endswith(".ts"):
            filename = f"{filename}.ts"
        headers = payload.get("headers") or {}

        task = DownloadTask(
            manifest_url,
            dest_folder=self.default_folder,
            threads=1,
            media_info={
                "media_type": media_type,
                "manifest_url": manifest_url,
                "headers": headers,
                "source_url": payload.get("source_url"),
            }
        )
        task.filename = filename
        task.dest_path = os.path.join(task.dest_folder, task.filename)
        if headers:
            task.session.headers.update(headers)
        self.tasks.append(task)
        self._add_table_row(task)
        self.log(f"[Media] Captured stream {task.filename}")
        self.save_task(task)
        if self.settings.get("auto_start", True):
            task.start()

    def schedule_task(self, row):
        try:
            task = self.tasks[row]
        except IndexError:
            return

        dialog = ScheduleDialog(self, task)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            start, end, repeat = dialog.get_values()
            task.update_schedule(start, end, repeat)

            now = self._now_utc()
            if start and now < start:
                if task.status == "downloading":
                    task.pause()
                    self.log(f"[Scheduled pause] {task.filename}")
                task.status = "scheduled"
            elif not start and not end and repeat == 0 and task.status == "scheduled":
                task.status = "queued"

            self.save_task(task)

            description = self._schedule_description(task)
            if description:
                self.log(f"[Scheduled] {task.filename} - {description}")
            else:
                self.log(f"[Schedule cleared] {task.filename}")

            self.refresh_table()

    def remove_task(self, row):
        try:
            task = self.tasks[row]
        except IndexError:
            return
        if task.is_alive():
            task.pause()
        try:
            import shutil
            shutil.rmtree(task.task_temp, ignore_errors=True)
        except Exception:
            pass
        self.delete_task(task.url, task.dest_folder)  # Remove from database
        self.table.removeRow(row)
        self.tasks.pop(row)
        # update IDs
        for i in range(self.table.rowCount()):
            self.table.item(i, 0).setText(str(i + 1))
        self.log(f"[Removed] {task.filename}")

    # ------------------ Batch actions ------------------
    def start_all(self):
        for t in self.tasks:
            if t.status not in ("downloading", "completed"):
                self.log(f"[Start All] {t.filename}")
                t.start()

    def pause_all(self):
        for t in self.tasks:
            if t.status == "downloading":
                t.pause()
                self.log(f"[Paused All] {t.filename}")

    def clear_completed(self):
        i = 0
        while i < len(self.tasks):
            if self.tasks[i].status == "completed":
                self.table.removeRow(i)
                self.tasks.pop(i)
            else:
                i += 1
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setText(str(r + 1))
        self.save_tasks()  # Save after clearing

    # ------------------ Logging ------------------
    def log(self, msg):
        self.log_box.append(msg)
        print(msg)
    
    # ------------------ Persistence ------------------
    def init_database(self):
        """Initialize SQLite database and create table if it doesn't exist."""
        try:
            os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    dest_folder TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    threads INTEGER DEFAULT 4,
                    total_size INTEGER DEFAULT 0,
                    downloaded INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'queued',
                    error TEXT,
                    temp_root TEXT DEFAULT 'data/temp',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(url, dest_folder)
                )
            ''')
            conn.commit()
            self._ensure_schedule_columns(cursor)
            conn.commit()
            conn.close()
            
            # Migrate from JSON if it exists
            self.migrate_from_json()
        except Exception as e:
            print(f"Error initializing database: {e}")
    
    def _ensure_schedule_columns(self, cursor):
        try:
            cursor.execute("PRAGMA table_info(downloads)")
            columns = {row[1] for row in cursor.fetchall()}
            if 'scheduled_start' not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN scheduled_start TEXT")
            if 'scheduled_end' not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN scheduled_end TEXT")
            if 'repeat_interval' not in columns:
                cursor.execute("ALTER TABLE downloads ADD COLUMN repeat_interval INTEGER DEFAULT 0")
        except Exception as e:
            print(f"Error ensuring schedule columns: {e}")

    def migrate_from_json(self):
        """Migrate data from JSON file to SQLite if JSON exists and DB is empty."""
        try:
            json_file = "data/downloads.json"
            if not os.path.exists(json_file):
                return
            
            # Check if database already has data
            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM downloads')
            count = cursor.fetchone()[0]
            conn.close()
            
            if count > 0:
                # Database already has data, skip migration
                return
            
            # Read JSON and migrate
            import json
            with open(json_file, 'r') as f:
                tasks_data = json.load(f)
            
            migrated = 0
            for task_data in tasks_data:
                try:
                    # Only migrate incomplete tasks
                    if task_data.get('status') != 'completed':
                        task = DownloadTask.from_dict(task_data)
                        self.save_task(task)
                        migrated += 1
                except Exception as e:
                    print(f"Error migrating task: {e}")
            
            if migrated > 0:
                print(f"Migrated {migrated} tasks from JSON to SQLite")
                # Optionally backup or remove JSON file
                # os.rename(json_file, json_file + ".backup")
        except Exception as e:
            print(f"Error migrating from JSON: {e}")
    
    def get_db_connection(self):
        """Get database connection."""
        return sqlite3.connect(DB_FILE)
    
    def save_task(self, task):
        """Save or update a single task in database."""
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()
            
            # Check if task exists
            cursor.execute('SELECT id FROM downloads WHERE url = ? AND dest_folder = ?', 
                         (task.url, task.dest_folder))
            existing = cursor.fetchone()
            
            task_dict = task.to_dict()
            scheduled_start = task_dict.get('scheduled_start')
            scheduled_end = task_dict.get('scheduled_end')
            repeat_interval = task_dict.get('repeat_interval', 0)

            if existing:
                # Update existing task
                cursor.execute('''
                    UPDATE downloads 
                    SET filename = ?, threads = ?, total_size = ?, downloaded = ?, 
                        status = ?, error = ?, temp_root = ?, scheduled_start = ?, 
                        scheduled_end = ?, repeat_interval = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE url = ? AND dest_folder = ?
                ''', (
                    task_dict['filename'],
                    task_dict['threads'],
                    task_dict['total_size'],
                    task_dict['downloaded'],
                    task_dict['status'],
                    task_dict['error'],
                    task_dict['temp_root'],
                    scheduled_start,
                    scheduled_end,
                    repeat_interval,
                    task.url,
                    task.dest_folder
                ))
            else:
                # Insert new task
                cursor.execute('''
                    INSERT INTO downloads 
                    (url, dest_folder, filename, threads, total_size, downloaded, status, error, temp_root,
                     scheduled_start, scheduled_end, repeat_interval)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    task_dict['url'],
                    task_dict['dest_folder'],
                    task_dict['filename'],
                    task_dict['threads'],
                    task_dict['total_size'],
                    task_dict['downloaded'],
                    task_dict['status'],
                    task_dict['error'],
                    task_dict['temp_root'],
                    scheduled_start,
                    scheduled_end,
                    repeat_interval
                ))
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error saving task: {e}")
    
    def delete_task(self, url, dest_folder):
        """Delete a task from database."""
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM downloads WHERE url = ? AND dest_folder = ?', 
                         (url, dest_folder))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error deleting task: {e}")
    
    def save_tasks(self):
        """Save all incomplete tasks to database."""
        try:
            for task in self.tasks:
                # Only save incomplete tasks
                if task.status not in ("completed",):
                    self.save_task(task)
                else:
                    # Remove completed tasks from database
                    self.delete_task(task.url, task.dest_folder)
        except Exception as e:
            print(f"Error saving tasks: {e}")
    
    def load_tasks(self):
        """Load incomplete tasks from database."""
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT url, dest_folder, filename, threads, total_size, downloaded, 
                       status, error, temp_root, scheduled_start, scheduled_end, repeat_interval
                FROM downloads
                WHERE status != 'completed'
                ORDER BY created_at DESC
            ''')
            rows = cursor.fetchall()
            conn.close()
            
            loaded_count = 0
            for row in rows:
                try:
                    (url, dest_folder, filename, threads, total_size, downloaded,
                     status, error, temp_root, scheduled_start, scheduled_end, repeat_interval) = row
                    
                    # Check if temp directory still exists (partial files)
                    temp_dir = os.path.join(temp_root or 'data/temp', f"{filename}.parts")
                    
                    # Only restore if temp directory exists (has partial files) or file doesn't exist
                    dest_path = os.path.join(dest_folder, filename)
                    # Check if file is already complete
                    file_complete = False
                    if os.path.exists(dest_path):
                        file_size = os.path.getsize(dest_path)
                        if total_size > 0 and file_size >= total_size:
                            file_complete = True
                    
                    if not file_complete and (os.path.exists(temp_dir) or not os.path.exists(dest_path)):
                        task_data = {
                            'url': url,
                            'dest_folder': dest_folder,
                            'filename': filename,
                            'threads': threads,
                            'total_size': total_size,
                            'downloaded': downloaded,
                            'status': status,
                            'error': error,
                            'temp_root': temp_root or 'data/temp',
                            'scheduled_start': scheduled_start,
                            'scheduled_end': scheduled_end,
                            'repeat_interval': repeat_interval or 0
                        }
                        task = DownloadTask.from_dict(task_data)
                        self.tasks.append(task)
                        self._add_table_row(task)
                        loaded_count += 1
                        # Log with progress percentage
                        if task.total_size > 0:
                            percent = (task.downloaded / task.total_size) * 100
                            self.log(f"[Restored] {task.filename} ({task.status}) - {percent:.1f}%")
                        else:
                            self.log(f"[Restored] {task.filename} ({task.status})")
                except Exception as e:
                    print(f"Error loading task {row[2] if len(row) > 2 else 'unknown'}: {e}")
            
            if loaded_count > 0:
                self.log(f"[Loaded] {loaded_count} incomplete download(s)")
        except Exception as e:
            print(f"Error loading tasks: {e}")
    
    def closeEvent(self, event):
        """Called when window is closed."""
        self.save_tasks()
        if self.bridge:
            try:
                self.bridge.stop()
            except Exception:
                pass
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = IDMWindow()
    w.show()
    sys.exit(app.exec())
