# main.py
import sys
import os
import sqlite3
import threading
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QFileDialog,
    QTableWidget, QTableWidgetItem, QWidget as QW, QHeaderView, QMessageBox, QTextEdit
)
from PyQt6.QtCore import QTimer, Qt
from downloader import DownloadTask

DEFAULT_THREADS_PER_TASK = 4
DB_FILE = "data/downloads.db"


class IDMWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyIDM - Multi Download Manager")
        self.resize(950, 500)
        self.tasks = []  # list of DownloadTask objects

        # --- top controls ---
        top_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste download URL here...")
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self.add_task_dialog)
        self.folder_btn = QPushButton("Choose Folder")
        self.folder_btn.clicked.connect(self.choose_folder)
        self.default_folder = os.getcwd()
        self.folder_label = QLabel(self.default_folder)

        top_layout.addWidget(self.url_input)
        top_layout.addWidget(self.add_btn)
        top_layout.addWidget(self.folder_btn)
        top_layout.addWidget(self.folder_label)

        # --- table ---
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "File", "Progress", "Status", "Speed", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(2, 200)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 240)
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

    # ------------------ GUI callbacks ------------------
    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", self.default_folder)
        if folder:
            self.default_folder = folder
            self.folder_label.setText(folder)

    def add_task_dialog(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "No URL", "Please paste a download URL first.")
            return
        self.add_task(url, self.default_folder)
        self.url_input.clear()

    def add_task(self, url, folder):
        # create task with progress callback
        task = DownloadTask(
            url,
            dest_folder=folder,
            threads=DEFAULT_THREADS_PER_TASK
        )
        self.tasks.append(task)
        self._add_table_row(task)
        self.log(f"[Added] {task.filename}")
        self.save_task(task)  # Save after adding

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

        start_btn.clicked.connect(lambda _, r=row: self.start_task(r))
        pause_btn.clicked.connect(lambda _, r=row: self.pause_task(r))
        resume_btn.clicked.connect(lambda _, r=row: self.resume_task(r))
        remove_btn.clicked.connect(lambda _, r=row: self.remove_task(r))

        act_layout.addWidget(start_btn)
        act_layout.addWidget(pause_btn)
        act_layout.addWidget(resume_btn)
        act_layout.addWidget(remove_btn)
        act_layout.setContentsMargins(0, 0, 0, 0)
        action_widget.setLayout(act_layout)
        self.table.setCellWidget(row, 5, action_widget)

    def refresh_table(self):
        needs_save = False
        tasks_to_save = []
        for idx, task in enumerate(list(self.tasks)):
            if idx >= self.table.rowCount():
                continue
            progress_widget = self.table.cellWidget(idx, 2)
            status_item = self.table.item(idx, 3)
            speed_item = self.table.item(idx, 4)

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
            
            # Save if status changed or task is incomplete
            if old_status != task.status or task.status != "completed":
                needs_save = True
                if task.status != "completed":
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
            conn.close()
            
            # Migrate from JSON if it exists
            self.migrate_from_json()
        except Exception as e:
            print(f"Error initializing database: {e}")
    
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
            if existing:
                # Update existing task
                cursor.execute('''
                    UPDATE downloads 
                    SET filename = ?, threads = ?, total_size = ?, downloaded = ?, 
                        status = ?, error = ?, temp_root = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE url = ? AND dest_folder = ?
                ''', (
                    task_dict['filename'],
                    task_dict['threads'],
                    task_dict['total_size'],
                    task_dict['downloaded'],
                    task_dict['status'],
                    task_dict['error'],
                    task_dict['temp_root'],
                    task.url,
                    task.dest_folder
                ))
            else:
                # Insert new task
                cursor.execute('''
                    INSERT INTO downloads 
                    (url, dest_folder, filename, threads, total_size, downloaded, status, error, temp_root)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    task_dict['url'],
                    task_dict['dest_folder'],
                    task_dict['filename'],
                    task_dict['threads'],
                    task_dict['total_size'],
                    task_dict['downloaded'],
                    task_dict['status'],
                    task_dict['error'],
                    task_dict['temp_root']
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
                       status, error, temp_root
                FROM downloads
                WHERE status != 'completed'
                ORDER BY created_at DESC
            ''')
            rows = cursor.fetchall()
            conn.close()
            
            loaded_count = 0
            for row in rows:
                try:
                    url, dest_folder, filename, threads, total_size, downloaded, status, error, temp_root = row
                    
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
                            'temp_root': temp_root or 'data/temp'
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
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = IDMWindow()
    w.show()
    sys.exit(app.exec())
