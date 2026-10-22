"""
College Bus IN / OUT Management System  -  v2

Camera + OCR number-plate reader that logs buses entering / leaving campus.

What's new compared with v1
---------------------------
* Modern look: header with live clock, dashboard cards, colour status banner
* Search / filter / sort / export (CSV) for the IN/OUT records
* Duration column (how long a bus was inside)
* Manage Buses window: search, add, EDIT, delete
* "Currently Inside" window with one-click Mark OUT
* Snapshot of the camera frame saved on every automatic IN / OUT
  (double-click a record to view it)
* Unregistered plates are logged (with snapshot) and counted on the dashboard
* Sound alerts (Windows), camera selector, keyboard shortcuts
* Registered-bus list is cached in memory (v1 hit the database on every
  OCR pass), sample buses no longer re-appear after you delete them
* Input validation (plate length / characters), safe DB handling
* Tesseract path auto-detected, friendly warning if it's missing
"""

import csv
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import tkinter as tk
from contextlib import contextmanager
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageTk

try:                                    # Windows only (sound alerts)
    import winsound
except ImportError:                     # pragma: no cover
    winsound = None


# =========================================================
# SETTINGS  (edit these if needed)
# =========================================================
APP_TITLE = "College Bus IN/OUT Management System"
DB_NAME = "college_bus.db"
CAPTURE_DIR = "captures"
TIME_FMT = "%Y-%m-%d %H:%M:%S"

COOLDOWN_SECONDS = 10       # ignore the same bus again for this long
OCR_INTERVAL = 0.3          # seconds between OCR attempts
MAX_RECORDS_SHOWN = 1000

TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]

SAMPLE_BUSES = [
    ("TN38N3244", "GOPAL K", "THENI"),
    ("TN57AB1234", "KUMAR", "MADURAI"),
    ("TN60CD5678", "RAVI", "CUMBUM"),
]

# Colours
C_PRIMARY = "#12355b"
C_BG = "#f1f4f9"
C_CARD = "#ffffff"
C_TEXT = "#1f2937"
C_MUTED = "#6b7280"
C_IN = "#1e8e3e"
C_OUT = "#d97706"
C_UNKNOWN = "#c5221f"
C_IDLE = "#6b7280"

STATE_STYLE = {
    "IDLE": ("WAITING FOR BUS", C_IDLE),
    "IN": ("BUS IN", C_IN),
    "OUT": ("BUS OUT", C_OUT),
    "UNKNOWN": ("NOT REGISTERED", C_UNKNOWN),
}
STATE_BGR = {"IN": (58, 142, 30), "OUT": (11, 119, 217), "UNKNOWN": (31, 34, 197)}


def configure_tesseract():
    """Point pytesseract at the Tesseract executable. Returns True if found."""
    for path in TESSERACT_PATHS:
        if os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return True

    found = shutil.which("tesseract")
    if found:
        pytesseract.pytesseract.tesseract_cmd = found
        return True

    return False


# =========================================================
# HELPERS
# =========================================================
def normalize_plate(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def now_str():
    return datetime.now().strftime(TIME_FMT)


def format_duration(start, end):
    try:
        delta = datetime.strptime(end, TIME_FMT) - datetime.strptime(start, TIME_FMT)
    except (ValueError, TypeError):
        return ""

    minutes = int(delta.total_seconds() // 60)

    if minutes < 0:
        return ""

    hours, mins = divmod(minutes, 60)

    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


# =========================================================
# DATABASE LAYER
# =========================================================
class Database:

    def __init__(self, path=DB_NAME):
        self.path = path
        self._lock = threading.Lock()
        self._cache = None
        self.init_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -----------------------------------------------------
    def init_schema(self):
        with self.connect() as c:

            c.execute("""
                CREATE TABLE IF NOT EXISTS buses (
                    bus_number TEXT PRIMARY KEY,
                    driver_name TEXT NOT NULL,
                    route TEXT NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS bus_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bus_number TEXT NOT NULL,
                    driver_name TEXT NOT NULL,
                    route TEXT NOT NULL,
                    in_time TEXT,
                    out_time TEXT,
                    status TEXT NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS unregistered_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plate TEXT NOT NULL,
                    seen_time TEXT NOT NULL,
                    snapshot TEXT
                )
            """)

            # Upgrade an existing v1 database in place
            columns = {row[1] for row in c.execute("PRAGMA table_info(bus_records)")}

            for col in ("in_snapshot", "out_snapshot"):
                if col not in columns:
                    c.execute(f"ALTER TABLE bus_records ADD COLUMN {col} TEXT")

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_records_bus
                ON bus_records (bus_number, id)
            """)

            # Seed demo buses only once, on a brand-new database
            version = c.execute("PRAGMA user_version").fetchone()[0]

            if version == 0:
                if c.execute("SELECT COUNT(*) FROM buses").fetchone()[0] == 0:
                    c.executemany(
                        "INSERT OR IGNORE INTO buses VALUES (?, ?, ?)",
                        SAMPLE_BUSES
                    )

                c.execute("PRAGMA user_version = 1")

    # -----------------------------------------------------
    # Registered buses (cached, thread-safe)
    # -----------------------------------------------------
    def registry(self):
        with self._lock:
            if self._cache is None:
                with self.connect() as c:
                    self._cache = c.execute("""
                        SELECT bus_number, driver_name, route
                        FROM buses ORDER BY bus_number
                    """).fetchall()

            return list(self._cache)

    def _invalidate(self):
        with self._lock:
            self._cache = None

    def registered_numbers(self):
        return [row[0] for row in self.registry()]

    def get_bus(self, number):
        """Returns (bus_number, driver, route) or None."""
        key = normalize_plate(number)

        for bus_number, driver, route in self.registry():
            if normalize_plate(bus_number) == key:
                return bus_number, driver, route

        return None

    def add_bus(self, bus_number, driver, route):
        if self.get_bus(bus_number):
            return False

        try:
            with self.connect() as c:
                c.execute(
                    "INSERT INTO buses VALUES (?, ?, ?)",
                    (bus_number, driver, route)
                )
        except sqlite3.IntegrityError:
            return False

        self._invalidate()
        return True

    def update_bus(self, bus_number, driver, route):
        with self.connect() as c:
            c.execute(
                "UPDATE buses SET driver_name = ?, route = ? WHERE bus_number = ?",
                (driver, route, bus_number)
            )

        self._invalidate()

    def delete_bus(self, bus_number):
        with self.connect() as c:
            c.execute("DELETE FROM buses WHERE bus_number = ?", (bus_number,))

        self._invalidate()

    # -----------------------------------------------------
    # IN / OUT records
    # -----------------------------------------------------
    def current_status(self, bus_number):
        with self.connect() as c:
            row = c.execute("""
                SELECT status FROM bus_records
                WHERE bus_number = ? ORDER BY id DESC LIMIT 1
            """, (bus_number,)).fetchone()

        # No record = the bus is outside
        return row[0] if row else "OUT"

    def record_in(self, bus_number, driver, route, snapshot="", when=None):
        with self.connect() as c:
            c.execute("""
                INSERT INTO bus_records
                (bus_number, driver_name, route, in_time, out_time,
                 status, in_snapshot)
                VALUES (?, ?, ?, ?, '', 'IN', ?)
            """, (bus_number, driver, route, when or now_str(), snapshot))

    def record_out(self, bus_number, snapshot="", when=None):
        with self.connect() as c:
            row = c.execute("""
                SELECT id FROM bus_records
                WHERE bus_number = ? AND status = 'IN'
                ORDER BY id DESC LIMIT 1
            """, (bus_number,)).fetchone()

            if row:
                c.execute("""
                    UPDATE bus_records
                    SET out_time = ?, status = 'OUT', out_snapshot = ?
                    WHERE id = ?
                """, (when or now_str(), snapshot, row[0]))

    def fetch_records(self, search="", status="ALL", period="ALL",
                      limit=MAX_RECORDS_SHOWN):
        sql = """
            SELECT id, bus_number, driver_name, route, in_time, out_time,
                   status, in_snapshot, out_snapshot
            FROM bus_records WHERE 1 = 1
        """
        params = []

        if search:
            sql += " AND (bus_number LIKE ? OR driver_name LIKE ? OR route LIKE ?)"
            params += [f"%{search}%"] * 3

        if status in ("IN", "OUT"):
            sql += " AND status = ?"
            params.append(status)

        if period == "TODAY":
            today = datetime.now().strftime("%Y-%m-%d") + "%"
            sql += " AND (in_time LIKE ? OR out_time LIKE ?)"
            params += [today, today]

        elif period == "WEEK":
            start = (datetime.now() - timedelta(days=7)).strftime(TIME_FMT)
            sql += " AND (in_time >= ? OR out_time >= ?)"
            params += [start, start]

        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self.connect() as c:
            return c.execute(sql, params).fetchall()

    def inside_buses(self):
        with self.connect() as c:
            return c.execute("""
                SELECT r.bus_number, r.driver_name, r.route, r.in_time
                FROM bus_records r
                WHERE r.status = 'IN'
                  AND r.id = (SELECT MAX(id) FROM bus_records
                              WHERE bus_number = r.bus_number)
                  AND r.bus_number IN (SELECT bus_number FROM buses)
                ORDER BY r.in_time
            """).fetchall()

    def log_unregistered(self, plate, snapshot=""):
        with self.connect() as c:
            c.execute(
                "INSERT INTO unregistered_log (plate, seen_time, snapshot) "
                "VALUES (?, ?, ?)",
                (plate, now_str(), snapshot)
            )

    def stats(self):
        today = datetime.now().strftime("%Y-%m-%d") + "%"
        registered = len(self.registry())
        inside = len(self.inside_buses())

        with self.connect() as c:
            moves = c.execute("""
                SELECT
                  (SELECT COUNT(*) FROM bus_records WHERE in_time LIKE ?) +
                  (SELECT COUNT(*) FROM bus_records WHERE out_time LIKE ?)
            """, (today, today)).fetchone()[0]

            unknown = c.execute(
                "SELECT COUNT(*) FROM unregistered_log WHERE seen_time LIKE ?",
                (today,)
            ).fetchone()[0]

        return {
            "registered": registered,
            "inside": inside,
            "outside": max(0, registered - inside),
            "moves": moves,
            "unknown": unknown,
        }


# =========================================================
# PLATE READER  (OCR pipeline - same tuned logic as v1)
# =========================================================
class PlateReader:

    # Letters / digits that OCR commonly mixes up are treated as equal
    OCR_LOOKALIKES = str.maketrans({
        "O": "0", "Q": "0", "D": "0",
        "I": "1", "L": "1",
        "Z": "2", "S": "5",
        "B": "8", "G": "6"
    })

    @staticmethod
    def approx_substring_distance(pattern, text):
        """Smallest edit distance between pattern and ANY part of text."""
        m = len(pattern)
        prev = list(range(m + 1))
        best = prev[m]

        for ch in text:
            cur = [0]

            for i in range(1, m + 1):
                cost = 0 if pattern[i - 1] == ch else 1
                cur.append(min(prev[i] + 1, cur[i - 1] + 1, prev[i - 1] + cost))

            prev = cur
            best = min(best, prev[m])

        return best

    def match_registered_bus(self, text, registered):
        """Returns (registered_bus, distance). distance 0 = exact."""
        text = normalize_plate(text)

        if len(text) < 6:
            return "", None

        canon_text = text.translate(self.OCR_LOOKALIKES)
        candidates = []

        for bus in registered:
            clean_bus = normalize_plate(bus)

            if not clean_bus:
                continue

            canon_bus = clean_bus.translate(self.OCR_LOOKALIKES)
            distance = self.approx_substring_distance(canon_bus, canon_text)
            allowed = 2 if len(clean_bus) >= 8 else 1

            if distance <= allowed:
                candidates.append((distance, bus))

        if not candidates:
            return "", None

        candidates.sort()

        # Two buses equally close -> too risky to guess
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            return "", None

        return candidates[0][1], candidates[0][0]

    # -----------------------------------------------------
    def find_plate_regions(self, roi):
        """Finds yellow / white plate-shaped areas inside the green box."""
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        yellow = cv2.inRange(hsv, (12, 70, 90), (40, 255, 255))
        white = cv2.inRange(hsv, (0, 0, 170), (180, 50, 255))

        roi_area = roi.shape[0] * roi.shape[1]
        kernel = np.ones((9, 9), np.uint8)
        found = []

        for mask in (yellow, white):
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
            contours = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )[-2]

            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)

                if w * h < 0.08 * roi_area:
                    continue
                if not (1.15 < w / float(h) < 8):
                    continue
                if cv2.contourArea(contour) < 0.5 * w * h:
                    continue

                found.append((w * h, x, y, w, h))

        found.sort(reverse=True)
        return [f[1:] for f in found[:2]]

    @staticmethod
    def pad_image(img, border=30):
        return cv2.copyMakeBorder(
            img, border, border, border, border, cv2.BORDER_CONSTANT,
            value=int(np.median(img))
        )

    @staticmethod
    def find_split_row(binary):
        """Row between the two text lines of a 2-line plate (the emptiest
        horizontal band near the middle)."""
        h = binary.shape[0]
        ink = (binary < 128).sum(axis=1).astype(np.float32)
        k = max(3, h // 25)
        ink = np.convolve(ink, np.ones(k, np.float32) / k, mode="same")

        lo, hi = int(h * 0.35), int(h * 0.65)

        if hi <= lo:
            return h // 2

        return lo + int(np.argmin(ink[lo:hi]))

    def split_two_lines(self, images, binary, border=30):
        """Cuts every image into top / bottom line. Returns [top, bottom]
        for the first image set; callers OCR each line and join the text."""
        row = self.find_split_row(binary)
        lines = []

        for img in images:
            lines.append(img[:row, :])
            lines.append(img[row:, :])

        return [self.pad_image(l, border) for l in lines if l.shape[0] > 8]

    def build_plate_passes(self, roi):
        """Each pass is ([images], psm); the text of all images in a pass is
        joined, so a 2-line plate is read line by line and glued together."""
        passes = []

        for x, y, w, h in self.find_plate_regions(roi):

            m = int(0.06 * h)                       # cut the plate frame off
            crop = roi[y + m:y + h - m, x + m:x + w - m]

            if crop.size == 0:
                continue

            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            scale = max(1.0, 400.0 / gray.shape[0])
            gray = cv2.resize(gray, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_CUBIC)
            gray = cv2.GaussianBlur(gray, (3, 3), 0)

            _, otsu = cv2.threshold(
                gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )

            one_line = [
                ([self.pad_image(gray)], 7),
                ([self.pad_image(gray)], 6),
                ([self.pad_image(otsu)], 7),
            ]

            # Squarish plate (2 rows of characters, e.g. RJ23G / X123Y)
            two_line = [
                (self.split_two_lines([gray], otsu), 7),
                (self.split_two_lines([otsu], otsu), 7),
                ([self.pad_image(gray)], 6),
                ([self.pad_image(otsu)], 6),
            ]

            if w / float(h) < 3.2:
                passes += two_line + one_line
            else:
                passes += one_line + two_line[:2]

        return passes

    @staticmethod
    def extract_plate_format(text):
        """Finds an Indian-plate-shaped string (e.g. TN21BZ5893) in OCR text,
        fixing letter/digit mix-ups by position. Returns "" if none."""
        text = normalize_plate(text)

        to_digit = {"O": "0", "Q": "0", "D": "0", "I": "1",
                    "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}
        to_letter = {"0": "O", "1": "I", "2": "Z",
                     "5": "S", "8": "B", "6": "G"}

        best = None

        for start in range(len(text)):
            for series, digits in ((2, 4), (1, 4), (3, 4),
                                   (2, 3), (1, 3), (3, 3)):
                shape = "LL" + "DD" + "L" * series + "D" * digits
                window = text[start:start + len(shape)]

                if len(window) != len(shape):
                    continue

                fixed = ""
                corrections = 0

                for ch, kind in zip(window, shape):
                    if kind == "L":
                        if ch.isalpha():
                            fixed += ch
                        elif ch in to_letter:
                            fixed += to_letter[ch]
                            corrections += 1
                        else:
                            fixed = None
                            break
                    else:
                        if ch.isdigit():
                            fixed += ch
                        elif ch in to_digit:
                            fixed += to_digit[ch]
                            corrections += 1
                        else:
                            fixed = None
                            break

                if fixed is None or corrections > 2:
                    continue

                rank = (-len(shape), corrections, start)

                if best is None or rank < best[0]:
                    best = (rank, fixed)

        return best[1] if best else ""

    def build_ocr_passes(self, roi):
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        scale = min(3.0, max(1.0, 1200.0 / gray.shape[1]))
        gray = cv2.resize(gray, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_CUBIC)
        gray = cv2.bilateralFilter(gray, 9, 75, 75)

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)

        _, otsu = cv2.threshold(
            enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        adaptive = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 51, 10
        )
        inverted = cv2.bitwise_not(otsu)

        def pad(img):
            return cv2.copyMakeBorder(img, 20, 20, 20, 20, cv2.BORDER_REPLICATE)

        # Whole green box (single-line plate, or 2-line read as a block)
        fallback = [
            ([pad(enhanced)], 7), ([pad(otsu)], 7), ([pad(enhanced)], 6),
            ([pad(adaptive)], 7), ([pad(inverted)], 7), ([pad(otsu)], 6),
        ]

        # Whole green box holding a 2-line plate: read line by line
        fallback += [
            (self.split_two_lines([enhanced], otsu, 20), 7),
            (self.split_two_lines([otsu], otsu, 20), 7),
        ]

        # Plate-only crops first (more accurate), whole box as fallback
        return self.build_plate_passes(roi) + fallback

    def read(self, roi, registered):
        """Runs all OCR passes. Returns (bus, distance, raw_text, plate_guess)."""
        best_bus = ""
        best_distance = None
        raw_text = ""
        plate_guess = ""

        for images, psm in self.build_ocr_passes(roi):
            # A pass may hold several images (the lines of a 2-line plate);
            # their text is joined into one plate number
            text = "".join(
                pytesseract.image_to_string(
                    image,
                    config=f"--oem 3 --psm {psm} "
                           "-c tessedit_char_whitelist="
                           "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
                           "-c load_system_dawg=0 -c load_freq_dawg=0"
                ).strip() + "\n"
                for image in images
            )

            cleaned = normalize_plate(text)

            if len(cleaned) > len(raw_text):
                raw_text = cleaned

            bus, distance = self.match_registered_bus(text, registered)

            if bus and (best_distance is None or distance < best_distance):
                best_bus = bus
                best_distance = distance

            if best_distance == 0:
                break

            if not plate_guess:
                plate_guess = self.extract_plate_format(text)

        return best_bus, best_distance, raw_text, plate_guess


# =========================================================
# MAIN APPLICATION
# =========================================================
class BusManagementSystem:

    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1500x900")
        self.root.minsize(1200, 750)

        self.db = Database()
        self.reader = PlateReader()
        self.tesseract_ok = configure_tesseract()

        self.camera = None
        self.camera_running = False
        self.read_failures = 0
        self.last_frame = None

        # Prevent repeated IN/OUT changes
        self.last_detected_bus = ""
        self.last_detection_time = None

        # Background OCR state
        self.ocr_busy = False
        self.ocr_queue = queue.Queue()
        self.last_ocr_start = 0.0
        self.fuzzy_candidate = ""
        self.unreg_candidate = ""

        # On-video message (text, BGR colour, show-until)
        self.overlay = ("", (0, 255, 0), 0.0)

        self.current_rows = []
        self.sort_state = {}

        self.setup_style()
        self.build_menu()
        self.build_ui()
        self.bind_shortcuts()

        self.refresh_all()
        self.tick_clock()

        self.root.protocol("WM_DELETE_WINDOW", self.close_application)

        if not self.tesseract_ok:
            self.root.after(300, lambda: messagebox.showwarning(
                "Tesseract not found",
                "Tesseract OCR could not be found.\n\n"
                "Install it from https://github.com/UB-Mannheim/tesseract/wiki "
                "or set TESSERACT_PATHS at the top of main.py.\n\n"
                "You can still use manual IN / OUT."
            ))

    # =====================================================
    # STYLE / MENU / SHORTCUTS
    # =====================================================
    def setup_style(self):
        self.root.configure(bg=C_BG)

        style = ttk.Style()

        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10),
                        background=C_CARD, fieldbackground=C_CARD)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"),
                        background="#dfe6f0", foreground=C_TEXT)
        style.map("Treeview.Heading", background=[("active", "#cdd8e6")])

        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"),
                        padding=7, background=C_PRIMARY, foreground="white")
        style.map("Accent.TButton",
                  background=[("active", "#1d4f85"), ("disabled", "#9aa7b8")])
        style.configure("Card.TFrame", background=C_CARD)
        style.configure("TLabelframe", background=C_BG)
        style.configure("TLabelframe.Label", background=C_BG,
                        font=("Segoe UI", 11, "bold"), foreground=C_PRIMARY)

    def build_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Export records to CSV...",
                              accelerator="Ctrl+E", command=self.export_csv)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.close_application)
        menubar.add_cascade(label="File", menu=file_menu)

        bus_menu = tk.Menu(menubar, tearoff=0)
        bus_menu.add_command(label="Add bus...", accelerator="Ctrl+N",
                             command=self.add_bus_window)
        bus_menu.add_command(label="Manage buses...", command=self.manage_buses)
        bus_menu.add_command(label="Manual IN / OUT...", accelerator="Ctrl+M",
                             command=self.manual_entry_window)
        bus_menu.add_command(label="Buses currently inside...",
                             accelerator="Ctrl+I", command=self.inside_window)
        menubar.add_cascade(label="Buses", menu=bus_menu)

        cam_menu = tk.Menu(menubar, tearoff=0)
        cam_menu.add_command(label="Start camera", command=self.start_camera)
        cam_menu.add_command(label="Stop camera", command=self.stop_camera)
        menubar.add_cascade(label="Camera", menu=cam_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def bind_shortcuts(self):
        self.root.bind("<Control-e>", lambda e: self.export_csv())
        self.root.bind("<Control-n>", lambda e: self.add_bus_window())
        self.root.bind("<Control-m>", lambda e: self.manual_entry_window())
        self.root.bind("<Control-i>", lambda e: self.inside_window())
        self.root.bind("<F5>", lambda e: self.refresh_all())

    def show_about(self):
        messagebox.showinfo(
            "About",
            f"{APP_TITLE}\n\n"
            "Reads bus number plates from the camera and records\n"
            "IN / OUT times automatically.\n\n"
            "Shortcuts:\n"
            "  Ctrl+N  Add bus      Ctrl+M  Manual IN/OUT\n"
            "  Ctrl+I  Buses inside Ctrl+E  Export CSV\n"
            "  F5      Refresh"
        )

    # =====================================================
    # GUI
    # =====================================================
    def build_ui(self):

        # ---------- header ----------
        header = tk.Frame(self.root, bg=C_PRIMARY)
        header.pack(fill="x")

        tk.Label(
            header, text="COLLEGE BUS  IN / OUT  MANAGEMENT SYSTEM",
            font=("Segoe UI", 20, "bold"), bg=C_PRIMARY, fg="white"
        ).pack(side="left", padx=20, pady=12)

        self.clock_label = tk.Label(
            header, text="", font=("Segoe UI", 12),
            bg=C_PRIMARY, fg="#cfe0f5"
        )
        self.clock_label.pack(side="right", padx=20)

        # ---------- dashboard cards ----------
        cards = tk.Frame(self.root, bg=C_BG)
        cards.pack(fill="x", padx=10, pady=(10, 0))

        self.card_values = {}

        for key, title, color in (
            ("registered", "REGISTERED BUSES", C_PRIMARY),
            ("inside", "INSIDE CAMPUS", C_IN),
            ("outside", "OUTSIDE", C_OUT),
            ("moves", "MOVEMENTS TODAY", "#2563eb"),
            ("unknown", "UNKNOWN PLATES TODAY", C_UNKNOWN),
        ):
            cards.columnconfigure(len(self.card_values), weight=1)
            self.card_values[key] = self.make_card(
                cards, title, color, column=len(self.card_values)
            )

        # ---------- main area ----------
        main = tk.Frame(self.root, bg=C_BG)
        main.pack(fill="both", expand=True, padx=10, pady=10)

        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(0, weight=1)

        self.build_camera_panel(main)
        self.build_right_panel(main)

    def make_card(self, parent, title, color, column):
        card = tk.Frame(parent, bg=C_CARD, highlightthickness=1,
                        highlightbackground="#dbe2ec")
        card.grid(row=0, column=column, sticky="ew", padx=5)

        tk.Frame(card, bg=color, height=4).pack(fill="x")

        value = tk.Label(card, text="0", font=("Segoe UI", 26, "bold"),
                         bg=C_CARD, fg=color)
        value.pack(pady=(6, 0))

        tk.Label(card, text=title, font=("Segoe UI", 9, "bold"),
                 bg=C_CARD, fg=C_MUTED).pack(pady=(0, 8))

        return value

    # -----------------------------------------------------
    def build_camera_panel(self, main):
        left = ttk.LabelFrame(main, text="  CAMERA / BUS NUMBER DETECTION  ")
        left.grid(row=0, column=0, sticky="nsew", padx=5)

        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        # Fixed-size container so the picture never resizes the window
        self.cam_frame = tk.Frame(left, bg="black")
        self.cam_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.cam_frame.pack_propagate(False)

        self.camera_label = tk.Label(
            self.cam_frame, text="Camera is OFF",
            font=("Segoe UI", 20, "bold"), bg="black", fg="white"
        )
        self.camera_label.pack(fill="both", expand=True)

        controls = tk.Frame(left, bg=C_BG)
        controls.grid(row=1, column=0, pady=6)

        self.start_button = ttk.Button(
            controls, text="START CAMERA", style="Accent.TButton",
            width=16, command=self.start_camera
        )
        self.start_button.grid(row=0, column=0, padx=6)

        self.stop_button = ttk.Button(
            controls, text="STOP CAMERA", width=16,
            command=self.stop_camera, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, padx=6)

        tk.Label(controls, text="Camera:", bg=C_BG).grid(row=0, column=2,
                                                        padx=(18, 2))
        self.camera_index = tk.StringVar(value="0")
        ttk.Combobox(
            controls, textvariable=self.camera_index, values=["0", "1", "2", "3"],
            width=3, state="readonly"
        ).grid(row=0, column=3)

        self.sound_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls, text="Sound", variable=self.sound_var
        ).grid(row=0, column=4, padx=(18, 0))

        self.ocr_status = tk.Label(
            left, text="OCR Status: Waiting...",
            font=("Segoe UI", 11, "bold"), bg=C_BG, fg=C_TEXT
        )
        self.ocr_status.grid(row=2, column=0, pady=(0, 8))

    # -----------------------------------------------------
    def build_right_panel(self, main):
        right = tk.Frame(main, bg=C_BG)
        right.grid(row=0, column=1, sticky="nsew", padx=5)

        # ---------- detection card ----------
        info = tk.Frame(right, bg=C_CARD, highlightthickness=1,
                        highlightbackground="#dbe2ec")
        info.pack(fill="x", pady=(0, 8))

        self.banner = tk.Label(
            info, text=STATE_STYLE["IDLE"][0], bg=C_IDLE, fg="white",
            font=("Segoe UI", 22, "bold"), pady=10
        )
        self.banner.pack(fill="x")

        self.bus_number_label = tk.Label(
            info, text="---", font=("Segoe UI", 24, "bold"),
            bg=C_CARD, fg=C_TEXT
        )
        self.bus_number_label.pack(pady=(10, 2))

        self.driver_label = tk.Label(
            info, text="Driver: ---", font=("Segoe UI", 13),
            bg=C_CARD, fg=C_TEXT
        )
        self.driver_label.pack()

        self.route_label = tk.Label(
            info, text="Route: ---", font=("Segoe UI", 13),
            bg=C_CARD, fg=C_TEXT
        )
        self.route_label.pack()

        self.time_label = tk.Label(
            info, text="", font=("Segoe UI", 10), bg=C_CARD, fg=C_MUTED
        )
        self.time_label.pack(pady=(2, 10))

        # ---------- action buttons ----------
        buttons = tk.Frame(right, bg=C_BG)
        buttons.pack(fill="x", pady=(0, 8))

        for col, (text, cmd) in enumerate((
            ("ADD BUS", self.add_bus_window),
            ("MANAGE BUSES", self.manage_buses),
            ("MANUAL IN/OUT", self.manual_entry_window),
            ("INSIDE NOW", self.inside_window),
        )):
            buttons.columnconfigure(col, weight=1)
            ttk.Button(buttons, text=text, command=cmd).grid(
                row=0, column=col, sticky="ew", padx=3
            )

        # ---------- records ----------
        records = ttk.LabelFrame(right, text="  Bus IN / OUT Records  ")
        records.pack(fill="both", expand=True)

        filters = tk.Frame(records, bg=C_BG)
        filters.pack(fill="x", padx=8, pady=(8, 4))

        tk.Label(filters, text="Search:", bg=C_BG).pack(side="left")

        self.search_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self.search_var, width=16).pack(
            side="left", padx=(4, 10)
        )

        self.status_filter = tk.StringVar(value="ALL")
        ttk.Combobox(
            filters, textvariable=self.status_filter, width=6,
            values=["ALL", "IN", "OUT"], state="readonly"
        ).pack(side="left", padx=(0, 6))

        self.period_filter = tk.StringVar(value="TODAY")
        ttk.Combobox(
            filters, textvariable=self.period_filter, width=8,
            values=["TODAY", "WEEK", "ALL"], state="readonly"
        ).pack(side="left", padx=(0, 6))

        ttk.Button(filters, text="Export CSV",
                   command=self.export_csv).pack(side="right")

        for var in (self.search_var, self.status_filter, self.period_filter):
            var.trace_add("write", lambda *a: self.load_records())

        columns = ("ID", "Bus Number", "Driver", "Route",
                   "IN Time", "OUT Time", "Duration", "Status")

        body = tk.Frame(records)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        self.records_tree = ttk.Treeview(body, columns=columns, show="headings")

        widths = (40, 100, 90, 80, 125, 125, 70, 55)

        for column, width in zip(columns, widths):
            self.records_tree.heading(
                column, text=column,
                command=lambda c=column: self.sort_tree(
                    self.records_tree, c, numeric=(c == "ID")
                )
            )
            self.records_tree.column(column, width=width, minwidth=40)

        self.records_tree.tag_configure("IN", background="#e6f4ea")
        self.records_tree.bind("<Double-1>", self.show_record_details)

        yscroll = ttk.Scrollbar(body, orient="vertical",
                                command=self.records_tree.yview)
        xscroll = ttk.Scrollbar(body, orient="horizontal",
                                command=self.records_tree.xview)
        self.records_tree.configure(yscrollcommand=yscroll.set,
                                    xscrollcommand=xscroll.set)

        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.records_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        self.records_hint = tk.Label(
            records, text="", font=("Segoe UI", 9), bg=C_BG, fg=C_MUTED
        )
        self.records_hint.pack(anchor="w", padx=10, pady=(0, 6))

    # =====================================================
    # SMALL UI HELPERS
    # =====================================================
    def tick_clock(self):
        self.clock_label.config(
            text=datetime.now().strftime("%A, %d %B %Y    %I:%M:%S %p")
        )
        self.root.after(1000, self.tick_clock)

    def sort_tree(self, tree, column, numeric=False):
        key = (str(tree), column)
        reverse = self.sort_state.get(key, False)

        def sort_key(item):
            value = item[0]

            if numeric:
                try:
                    return float(value)
                except ValueError:
                    return 0.0

            return value.lower()

        items = [(tree.set(i, column), i) for i in tree.get_children("")]
        items.sort(key=sort_key, reverse=reverse)

        for index, (_, item) in enumerate(items):
            tree.move(item, "", index)

        self.sort_state[key] = not reverse

    def show_detection(self, bus_number, driver, route, state):
        text, color = STATE_STYLE[state]

        self.banner.config(text=text, bg=color)
        self.bus_number_label.config(text=bus_number or "---")
        self.driver_label.config(text=f"Driver: {driver}")
        self.route_label.config(text=f"Route: {route}")
        self.time_label.config(
            text=datetime.now().strftime("Last event: %d %b %Y, %I:%M:%S %p")
        )

    def flash_overlay(self, text, state, seconds=3.0):
        self.overlay = (text, STATE_BGR.get(state, (0, 255, 0)),
                        time.time() + seconds)

    def beep(self, kind):
        if winsound is None or not self.sound_var.get():
            return

        pattern = {
            "IN": [(1000, 150)],
            "OUT": [(700, 150), (700, 150)],
            "UNKNOWN": [(400, 500)],
        }.get(kind, [])

        def play():
            for freq, duration in pattern:
                try:
                    winsound.Beep(freq, duration)
                except RuntimeError:
                    pass

        threading.Thread(target=play, daemon=True).start()

    def save_snapshot(self, label, tag):
        """Saves the latest camera frame. Returns the file path ('' if none)."""
        if self.last_frame is None:
            return ""

        try:
            os.makedirs(CAPTURE_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(CAPTURE_DIR, f"{stamp}_{label}_{tag}.jpg")

            if cv2.imwrite(path, self.last_frame):
                return path
        except OSError:
            pass

        return ""

    # =====================================================
    # REFRESH
    # =====================================================
    def refresh_all(self):
        self.load_records()
        self.refresh_stats()

    def refresh_stats(self):
        stats = self.db.stats()

        for key, label in self.card_values.items():
            label.config(text=str(stats[key]))

    def load_records(self):
        rows = self.db.fetch_records(
            search=self.search_var.get().strip(),
            status=self.status_filter.get(),
            period=self.period_filter.get(),
        )

        tree = self.records_tree
        tree.delete(*tree.get_children())

        self.current_rows = []
        self.record_snapshots = {}
        now = now_str()

        for (rec_id, bus, driver, route, in_time, out_time,
             status, in_snap, out_snap) in rows:

            if status == "OUT":
                duration = format_duration(in_time, out_time)
            else:
                elapsed = format_duration(in_time, now)
                duration = f"{elapsed} (inside)" if elapsed else ""

            values = (rec_id, bus, driver, route, in_time, out_time,
                      duration, status)

            tree.insert("", "end", iid=str(rec_id), values=values,
                        tags=(status,))

            self.current_rows.append(values)
            self.record_snapshots[str(rec_id)] = (in_snap, out_snap)

        self.records_hint.config(
            text=f"{len(rows)} record(s) shown   |   "
                 "double-click a row to see snapshots"
        )

    # =====================================================
    # EXPORT
    # =====================================================
    def export_csv(self):
        if not self.current_rows:
            messagebox.showinfo("Export", "There are no records to export.")
            return

        path = filedialog.asksaveasfilename(
            title="Export records",
            defaultextension=".csv",
            initialfile=f"bus_records_{datetime.now():%Y%m%d_%H%M}.csv",
            filetypes=[("CSV file", "*.csv")]
        )

        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["ID", "Bus Number", "Driver", "Route",
                                 "IN Time", "OUT Time", "Duration", "Status"])
                writer.writerows(self.current_rows)

        except OSError as e:
            messagebox.showerror("Export failed", str(e))
            return

        messagebox.showinfo(
            "Export complete",
            f"{len(self.current_rows)} record(s) saved to:\n{path}"
        )

    # =====================================================
    # RECORD DETAILS (snapshots)
    # =====================================================
    def show_record_details(self, event=None):
        selected = self.records_tree.selection()

        if not selected:
            return

        rec_id = selected[0]
        values = self.records_tree.item(rec_id, "values")
        in_snap, out_snap = self.record_snapshots.get(rec_id, ("", ""))

        window = tk.Toplevel(self.root)
        window.title(f"Record #{values[0]}  -  {values[1]}")
        window.transient(self.root)
        window.configure(bg=C_BG)

        tk.Label(
            window,
            text=f"{values[1]}   |   {values[2]}   |   {values[3]}",
            font=("Segoe UI", 14, "bold"), bg=C_BG, fg=C_PRIMARY
        ).pack(padx=20, pady=(14, 2))

        tk.Label(
            window,
            text=f"IN: {values[4] or '-'}     OUT: {values[5] or '-'}     "
                 f"Duration: {values[6] or '-'}",
            font=("Segoe UI", 11), bg=C_BG
        ).pack(pady=(0, 10))

        row = tk.Frame(window, bg=C_BG)
        row.pack(padx=14, pady=(0, 14))

        window.photos = []

        for col, (title, path) in enumerate((("IN snapshot", in_snap),
                                             ("OUT snapshot", out_snap))):
            box = tk.LabelFrame(row, text=title, bg=C_BG)
            box.grid(row=0, column=col, padx=6)

            if path and os.path.exists(path):
                try:
                    image = Image.open(path)
                    image.thumbnail((440, 330))
                    photo = ImageTk.PhotoImage(image)
                    window.photos.append(photo)
                    tk.Label(box, image=photo).pack(padx=6, pady=6)
                    continue
                except OSError:
                    pass

            tk.Label(box, text="No snapshot", width=48, height=12,
                     bg="#e5e7eb", fg=C_MUTED).pack(padx=6, pady=6)

    # =====================================================
    # ADD / EDIT BUS
    # =====================================================
    def add_bus_window(self):
        self.bus_dialog()

    def bus_dialog(self, existing=None, on_done=None):
        window = tk.Toplevel(self.root)
        window.title("Edit Bus" if existing else "Add New Bus")
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()

        form = ttk.Frame(window, padding=22)
        form.pack()

        ttk.Label(
            form, text="EDIT BUS" if existing else "ADD BUS",
            font=("Segoe UI", 18, "bold")
        ).grid(row=0, column=0, columnspan=2, pady=(0, 14))

        bus_var = tk.StringVar(value=existing[0] if existing else "")
        driver_var = tk.StringVar(value=existing[1] if existing else "")
        route_var = tk.StringVar(value=existing[2] if existing else "")

        fields = (("Bus Number", bus_var), ("Driver Name", driver_var),
                  ("Route", route_var))
        entries = []

        for i, (label, var) in enumerate(fields, start=1):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w",
                                             pady=6, padx=(0, 12))
            entry = ttk.Entry(form, textvariable=var, width=30,
                              font=("Segoe UI", 11))
            entry.grid(row=i, column=1, pady=6)
            entries.append(entry)

        if existing:
            entries[0].config(state="disabled")     # plate is the key
            entries[1].focus_set()
        else:
            entries[0].focus_set()

        ttk.Label(
            form, foreground=C_MUTED,
            text="Plate: letters and digits only, e.g. TN38N3244"
        ).grid(row=4, column=0, columnspan=2, pady=(2, 8))

        def save(event=None):
            bus = normalize_plate(bus_var.get())
            driver = driver_var.get().strip().upper()
            route = route_var.get().strip().upper()

            if not bus or not driver or not route:
                messagebox.showwarning("Warning", "Please fill all fields.",
                                       parent=window)
                return

            if len(bus) < 6:
                messagebox.showwarning(
                    "Warning",
                    "Bus number must have at least 6 letters / digits\n"
                    "(shorter numbers cannot be matched by the camera).",
                    parent=window
                )
                return

            if existing:
                self.db.update_bus(existing[0], driver, route)
            elif not self.db.add_bus(bus, driver, route):
                messagebox.showerror("Error", "This bus number already exists.",
                                     parent=window)
                return

            window.destroy()
            self.refresh_all()

            if on_done:
                on_done()

        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, pady=(6, 0))

        ttk.Button(buttons, text="SAVE", style="Accent.TButton", width=14,
                   command=save).grid(row=0, column=0, padx=6)
        ttk.Button(buttons, text="Cancel", width=10,
                   command=window.destroy).grid(row=0, column=1, padx=6)

        window.bind("<Return>", save)
        window.bind("<Escape>", lambda e: window.destroy())

    # =====================================================
    # MANAGE BUSES
    # =====================================================
    def manage_buses(self):
        window = tk.Toplevel(self.root)
        window.title("Registered Buses")
        window.geometry("720x520")
        window.transient(self.root)

        top = ttk.Frame(window, padding=(10, 10, 10, 0))
        top.pack(fill="x")

        ttk.Label(top, text="Search:").pack(side="left")
        search_var = tk.StringVar()
        ttk.Entry(top, textvariable=search_var, width=26).pack(side="left",
                                                               padx=6)
        count_label = ttk.Label(top, text="")
        count_label.pack(side="right")

        columns = ("Bus Number", "Driver", "Route", "Status")
        tree = ttk.Treeview(window, columns=columns, show="headings")

        for column, width in zip(columns, (190, 190, 170, 90)):
            tree.heading(column, text=column,
                         command=lambda c=column: self.sort_tree(tree, c))
            tree.column(column, width=width)

        tree.tag_configure("IN", background="#e6f4ea")
        tree.pack(fill="both", expand=True, padx=10, pady=10)

        def reload(*args):
            term = search_var.get().strip().upper()
            tree.delete(*tree.get_children())
            shown = 0

            for bus, driver, route in self.db.registry():
                if term and term not in f"{bus} {driver} {route}":
                    continue

                inside = self.db.current_status(bus) == "IN"
                tree.insert("", "end", values=(bus, driver, route,
                                               "INSIDE" if inside else "OUTSIDE"),
                            tags=("IN" if inside else "",))
                shown += 1

            count_label.config(text=f"{shown} bus(es)")

        search_var.trace_add("write", reload)
        reload()

        def selected_bus():
            selected = tree.selection()

            if not selected:
                messagebox.showwarning("Warning", "Please select a bus.",
                                       parent=window)
                return None

            return tree.item(selected[0], "values")[0]

        def edit_selected(event=None):
            bus = selected_bus()

            if bus:
                self.bus_dialog(self.db.get_bus(bus), on_done=reload)

        def delete_selected():
            bus = selected_bus()

            if not bus:
                return

            if messagebox.askyesno(
                "Confirm Delete",
                f"Delete bus {bus}?\n\n"
                "Its past IN/OUT records are kept in the history.",
                parent=window
            ):
                self.db.delete_bus(bus)
                reload()
                self.refresh_all()

        tree.bind("<Double-1>", edit_selected)

        buttons = ttk.Frame(window)
        buttons.pack(pady=(0, 12))

        ttk.Button(buttons, text="Add", width=12,
                   command=lambda: self.bus_dialog(on_done=reload)
                   ).grid(row=0, column=0, padx=5)
        ttk.Button(buttons, text="Edit", width=12,
                   command=edit_selected).grid(row=0, column=1, padx=5)
        ttk.Button(buttons, text="Delete", width=12,
                   command=delete_selected).grid(row=0, column=2, padx=5)
        ttk.Button(buttons, text="Close", width=12,
                   command=window.destroy).grid(row=0, column=3, padx=5)

    # =====================================================
    # BUSES CURRENTLY INSIDE
    # =====================================================
    def inside_window(self):
        window = tk.Toplevel(self.root)
        window.title("Buses Currently Inside")
        window.geometry("680x420")
        window.transient(self.root)

        columns = ("Bus Number", "Driver", "Route", "Inside Since", "Duration")
        tree = ttk.Treeview(window, columns=columns, show="headings")

        for column, width in zip(columns, (130, 130, 110, 160, 90)):
            tree.heading(column, text=column,
                         command=lambda c=column: self.sort_tree(tree, c))
            tree.column(column, width=width)

        tree.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        summary = ttk.Label(window, text="")
        summary.pack()

        def reload():
            tree.delete(*tree.get_children())
            now = now_str()
            rows = self.db.inside_buses()

            for bus, driver, route, since in rows:
                tree.insert("", "end", values=(
                    bus, driver, route, since, format_duration(since, now)
                ))

            summary.config(text=f"{len(rows)} bus(es) inside campus")

        def mark_out():
            selected = tree.selection()

            if not selected:
                messagebox.showwarning("Warning", "Please select a bus.",
                                       parent=window)
                return

            bus = tree.item(selected[0], "values")[0]
            info = self.db.get_bus(bus)

            if info:
                self.apply_movement(info[0], info[1], info[2], "OUT")

            reload()

        reload()

        buttons = ttk.Frame(window)
        buttons.pack(pady=10)

        ttk.Button(buttons, text="Mark selected OUT", style="Accent.TButton",
                   command=mark_out).grid(row=0, column=0, padx=5)
        ttk.Button(buttons, text="Refresh", command=reload).grid(
            row=0, column=1, padx=5)
        ttk.Button(buttons, text="Close", command=window.destroy).grid(
            row=0, column=2, padx=5)

    # =====================================================
    # MANUAL IN / OUT
    # =====================================================
    def manual_entry_window(self):
        window = tk.Toplevel(self.root)
        window.title("Manual Bus IN / OUT")
        window.geometry("480x430")
        window.resizable(False, False)
        window.transient(self.root)

        ttk.Label(window, text="MANUAL IN / OUT",
                  font=("Segoe UI", 18, "bold")).pack(pady=14)

        ttk.Label(window, text="Bus Number (select or type)").pack()

        bus_var = tk.StringVar()
        bus_box = ttk.Combobox(
            window, textvariable=bus_var, values=self.db.registered_numbers(),
            font=("Segoe UI", 12), width=28
        )
        bus_box.pack(pady=6)
        bus_box.focus_set()

        details = ttk.Label(
            window, text="Driver: ---\nRoute: ---\nCurrent Status: ---",
            font=("Segoe UI", 12), justify="left"
        )
        details.pack(pady=14)

        def refresh_details(event=None):
            info = self.db.get_bus(bus_var.get())

            if info is None:
                details.config(text="Driver: ---\nRoute: ---\n"
                                    "Current Status: Not Registered")
                return

            status = self.db.current_status(info[0])
            status_text = "IN (inside)" if status == "IN" else "OUT (outside)"

            details.config(text=f"Driver: {info[1]}\nRoute: {info[2]}\n"
                                f"Current Status: {status_text}")

        bus_box.bind("<<ComboboxSelected>>", refresh_details)
        bus_box.bind("<KeyRelease>", refresh_details)

        def mark(direction):
            number = normalize_plate(bus_var.get())

            if not number:
                messagebox.showwarning(
                    "Warning", "Please enter or select a bus number.",
                    parent=window
                )
                return

            info = self.db.get_bus(number)

            if info is None:
                messagebox.showerror(
                    "Not Registered",
                    f"Bus {number} is not registered.\n"
                    "Use ADD BUS to register it first.",
                    parent=window
                )
                return

            bus, driver, route = info
            status = self.db.current_status(bus)

            if direction == "IN" and status == "IN":
                messagebox.showinfo("Already IN",
                                    f"Bus {bus} is already marked IN.",
                                    parent=window)
                return

            if direction == "OUT" and status != "IN":
                messagebox.showinfo(
                    "Already OUT",
                    f"Bus {bus} is already OUT (it has no open IN record).",
                    parent=window
                )
                return

            self.apply_movement(bus, driver, route, direction)
            refresh_details()

        row = ttk.Frame(window)
        row.pack(pady=14)

        tk.Button(row, text="MARK IN", font=("Segoe UI", 13, "bold"), width=12,
                  bg="#c8f7c5", command=lambda: mark("IN")
                  ).grid(row=0, column=0, padx=10)
        tk.Button(row, text="MARK OUT", font=("Segoe UI", 13, "bold"), width=12,
                  bg="#f7d9b0", command=lambda: mark("OUT")
                  ).grid(row=0, column=1, padx=10)

        ttk.Button(window, text="Close", command=window.destroy).pack(pady=4)

    # =====================================================
    # CAMERA
    # =====================================================
    def start_camera(self):
        if self.camera_running:
            return

        index = int(self.camera_index.get())

        # DirectShow opens faster / more reliably on Windows
        if os.name == "nt":
            self.camera = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        else:
            self.camera = cv2.VideoCapture(index)

        if not self.camera.isOpened():
            self.camera.release()
            self.camera = cv2.VideoCapture(index)

        if not self.camera.isOpened():
            self.camera.release()
            self.camera = None

            messagebox.showerror(
                "Camera Error",
                f"Could not open camera {index}.\n"
                "Try another camera number or close other apps using it."
            )
            return

        # Higher resolution = sharper plate text
        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.camera.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        self.camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self.fuzzy_candidate = ""
        self.unreg_candidate = ""
        self.read_failures = 0
        self.drain_ocr_queue()

        self.camera_running = True
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")

        self.ocr_status.config(text="OCR Status: Camera running...")

        self.update_camera()

    def stop_camera(self):
        self.camera_running = False

        if self.camera is not None:
            self.camera.release()
            self.camera = None

        self.last_frame = None
        self.camera_label.config(image="", text="Camera is OFF")
        self.camera_label.image = None

        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")

        self.ocr_status.config(text="OCR Status: Camera stopped")

    def drain_ocr_queue(self):
        while True:
            try:
                self.ocr_queue.get_nowait()
            except queue.Empty:
                return

    def update_camera(self):
        if not self.camera_running or self.camera is None:
            return

        ret, frame = self.camera.read()

        if not ret:
            self.read_failures += 1

            self.camera_label.config(image="", text="Unable to read camera")
            self.camera_label.image = None

            if self.read_failures == 50:
                self.ocr_status.config(
                    text="OCR Status: Camera not responding - check the cable"
                )

            self.root.after(100, self.update_camera)
            return

        self.read_failures = 0

        # Keep an UNMIRRORED copy for OCR (mirrored text can't be read)
        raw_frame = frame.copy()
        self.last_frame = raw_frame

        # Flip camera (display only)
        frame = cv2.flip(frame, 1)
        height, width = frame.shape[:2]

        # ---------- OCR region (green box) ----------
        roi_x1 = int(width * 0.12)
        roi_x2 = int(width * 0.88)
        roi_y1 = int(height * 0.22)
        roi_y2 = int(height * 0.78)

        # Crop BEFORE drawing, so the box lines are not read as characters
        roi = raw_frame[roi_y1:roi_y2, roi_x1:roi_x2].copy()

        overlay_text, overlay_color, overlay_until = self.overlay
        showing_overlay = time.time() < overlay_until

        cv2.rectangle(
            frame, (roi_x1, roi_y1), (roi_x2, roi_y2),
            overlay_color if showing_overlay else (0, 255, 0), 3
        )

        cv2.putText(frame, "Place BUS NUMBER (1 or 2 lines) inside GREEN BOX", (30, 45),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        if showing_overlay:
            cv2.putText(frame, overlay_text, (30, height - 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.4, overlay_color, 4)

        # ---------- OCR in a background thread (no camera lag) ----------
        now = time.time()

        if (self.tesseract_ok and not self.ocr_busy
                and now - self.last_ocr_start >= OCR_INTERVAL):

            self.ocr_busy = True
            self.last_ocr_start = now

            threading.Thread(
                target=self.ocr_worker,
                args=(roi, self.db.registered_numbers()),
                daemon=True
            ).start()

        try:
            result = self.ocr_queue.get_nowait()
        except queue.Empty:
            result = None

        if result is not None:
            self.handle_ocr_result(result)

        # ---------- display ----------
        avail_w = max(320, self.cam_frame.winfo_width() - 4)
        avail_h = max(240, self.cam_frame.winfo_height() - 4)
        scale = min(avail_w / width, avail_h / height)

        shown = cv2.resize(
            frame, (int(width * scale), int(height * scale)),
            interpolation=cv2.INTER_AREA
        )

        photo = ImageTk.PhotoImage(
            image=Image.fromarray(cv2.cvtColor(shown, cv2.COLOR_BGR2RGB))
        )

        self.camera_label.config(image=photo, text="")
        self.camera_label.image = photo

        self.root.after(30, self.update_camera)

    # =====================================================
    # OCR RESULT HANDLING
    # =====================================================
    def ocr_worker(self, roi, registered):
        try:
            self.ocr_queue.put(("ok",) + self.reader.read(roi, registered))
        except Exception as e:
            self.ocr_queue.put(("error", str(e)))
        finally:
            self.ocr_busy = False

    def handle_ocr_result(self, result):
        if result[0] == "error":
            self.ocr_status.config(text=f"OCR Error: {result[1][:80]}")
            return

        _, bus, distance, raw_text, plate_guess = result

        if bus:
            # Not an exact read -> must be seen twice in a row
            if distance > 0 and self.fuzzy_candidate != bus:
                self.fuzzy_candidate = bus
                self.ocr_status.config(text=f"OCR Checking: {bus}")
                return

            self.fuzzy_candidate = ""
            self.unreg_candidate = ""

            self.ocr_status.config(text=f"OCR Detected: {bus}")
            self.process_detected_bus(bus)

        elif plate_guess:
            # Plate read but not in the database: confirm with a second
            # similar read, then show it as "Not Registered"
            if (not self.unreg_candidate
                    or self.reader.approx_substring_distance(
                        self.unreg_candidate, plate_guess) > 1):

                self.unreg_candidate = plate_guess
                self.ocr_status.config(text=f"OCR Checking: {plate_guess}")
                return

            self.unreg_candidate = ""
            self.ocr_status.config(
                text=f"OCR Detected: {plate_guess} (not registered)"
            )
            self.process_detected_bus(plate_guess)

        else:
            message = "OCR Status: Searching bus number..."

            if raw_text:
                message += f"  (read: {raw_text[:20]})"

            self.ocr_status.config(text=message)

    # =====================================================
    # IN / OUT LOGIC
    # =====================================================
    def process_detected_bus(self, bus_number):
        now = datetime.now()

        # Cooldown
        if (self.last_detected_bus == bus_number
                and self.last_detection_time is not None
                and (now - self.last_detection_time).total_seconds()
                < COOLDOWN_SECONDS):
            return

        self.last_detected_bus = bus_number
        self.last_detection_time = now

        info = self.db.get_bus(bus_number)

        if info is None:
            snapshot = self.save_snapshot(bus_number, "UNKNOWN")
            self.db.log_unregistered(bus_number, snapshot)

            self.show_detection(bus_number, "Not Registered",
                                "Not Registered", "UNKNOWN")
            self.flash_overlay(f"{bus_number} - NOT REGISTERED", "UNKNOWN")
            self.beep("UNKNOWN")
            self.refresh_stats()
            return

        bus, driver, route = info

        direction = "IN" if self.db.current_status(bus) == "OUT" else "OUT"
        snapshot = self.save_snapshot(bus, direction)

        self.apply_movement(bus, driver, route, direction, snapshot)

    def apply_movement(self, bus, driver, route, direction, snapshot=""):
        """Writes an IN / OUT record and updates the screen."""
        if direction == "IN":
            self.db.record_in(bus, driver, route, snapshot)
        else:
            self.db.record_out(bus, snapshot)

        # Stop the camera from toggling this bus again right away
        self.last_detected_bus = bus
        self.last_detection_time = datetime.now()

        self.show_detection(bus, driver, route, direction)
        self.flash_overlay(f"{bus} - BUS {direction}", direction)
        self.beep(direction)
        self.refresh_all()

    # =====================================================
    # CLOSE
    # =====================================================
    def close_application(self):
        self.camera_running = False

        if self.camera is not None:
            self.camera.release()
            self.camera = None

        self.root.destroy()


# =========================================================
# RUN APPLICATION
# =========================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = BusManagementSystem(root)
    root.mainloop()