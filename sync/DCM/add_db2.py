# -*- coding: utf-8 -*-
"""
Screen & Camera Recorder  v8.0
─────────────────────────────────────────────────────────────────────────────
v7 → v8 변경사항:
  1. 메모 오버레이 — 녹화 전(미리보기)에도 항상 표시
  2. 한글 오버레이 — OpenCV cv2.putText → PIL/Pillow 렌더링으로 교체
     (cv2.putText 는 유니코드/한글 미지원 → ??? 표시 문제 해결)
  3. 설정 초기화 UI (Reset Settings 버튼)
─────────────────────────────────────────────────────────────────────────────
"""

# ── 표준 라이브러리 ──────────────────────────────────────────────────────────
import sys
import os
import threading
import time
import queue
import platform
import subprocess
import sqlite3
from datetime import datetime
from collections import deque

# ── 서드파티 ─────────────────────────────────────────────────────────────────
import cv2
import numpy as np
import mss

# ── PIL (유니코드/한글 텍스트 렌더링) ────────────────────────────────────────
try:
    from PIL import Image as _PIL_Image, ImageDraw as _PIL_Draw, ImageFont as _PIL_Font
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGroupBox, QCheckBox, QDoubleSpinBox,
    QScrollArea, QFrame, QGridLayout, QTextEdit, QSizePolicy,
    QDialog, QLCDNumber, QDateTimeEdit, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QPlainTextEdit,
    QAbstractItemView, QSpinBox, QSlider, QTabWidget, QComboBox,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QPoint, QRect, QDateTime
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QTextCursor

try:
    from pynput import keyboard as pynput_keyboard, mouse as pynput_mouse
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

# ── 경로 상수 ─────────────────────────────────────────────────────────────────
BASE_DIR = os.path.join(os.path.expanduser("~/Desktop"), "bltn_rec")
DB_PATH  = os.path.join(BASE_DIR, "settings.db")


# =============================================================================
#  유니코드/한글 폰트 탐색 & 텍스트 렌더링  (PIL 사용)
# =============================================================================
def _find_unicode_font(size: int = 18):
    """
    시스템에서 유니코드(한글 포함) 폰트를 찾아 PIL ImageFont를 반환합니다.
    못 찾으면 PIL 기본 폰트를 반환합니다.
    """
    if not PIL_AVAILABLE:
        return None
    candidates = [
        # Windows
        "C:/Windows/Fonts/malgun.ttf",
        "C:/Windows/Fonts/malgunbd.ttf",
        "C:/Windows/Fonts/gulim.ttc",
        "C:/Windows/Fonts/batang.ttc",
        "C:/Windows/Fonts/NanumGothic.ttf",
        # macOS
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/Library/Fonts/NanumGothic.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        # Linux — Noto CJK (한중일 지원)
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        # Linux — 나눔 고딕
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        # Linux — 은폰트
        "/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf",
        # 일본어 폰트 (CJK 한글 포함 가능)
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        # 범용 fallback
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    import glob
    # 동적 glob 탐색
    for pattern in [
        "/usr/share/fonts/**/*Noto*CJK*.ttc",
        "/usr/share/fonts/**/*Nanum*.ttf",
        "/usr/share/fonts/**/*nanum*.ttf",
    ]:
        for found in glob.glob(pattern, recursive=True):
            candidates.insert(0, found)

    for path in candidates:
        if os.path.exists(path):
            try:
                fnt = _PIL_Font.truetype(path, size)
                return fnt
            except Exception:
                continue
    # fallback: PIL 기본 비트맵 폰트 (한글 미지원이지만 ASCII는 가능)
    try:
        return _PIL_Font.load_default()
    except Exception:
        return None


# 폰트 캐시 (크기별)
_FONT_CACHE: dict[int, object] = {}


def _get_font(size: int = 18):
    if size not in _FONT_CACHE:
        _FONT_CACHE[size] = _find_unicode_font(size)
    return _FONT_CACHE[size]


def _put_unicode_text(frame: np.ndarray, text: str,
                      x: int, y: int, font_size: int,
                      color_bgr: tuple, alpha: float = 1.0) -> None:
    """
    PIL을 이용해 유니코드(한글 포함) 텍스트를 frame에 in-place 합성합니다.
    color_bgr: OpenCV BGR 순서 (예: (100, 220, 80))
    """
    if not PIL_AVAILABLE:
        # fallback: OpenCV (한글 깨짐 있지만 없는 것보단 나음)
        cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    font_size / 30, color_bgr, 2, cv2.LINE_AA)
        return

    fnt = _get_font(font_size)
    if fnt is None:
        cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    font_size / 30, color_bgr, 2, cv2.LINE_AA)
        return

    # BGR → RGB 변환
    color_rgb = (color_bgr[2], color_bgr[1], color_bgr[0])

    # frame 슬라이싱해 PIL Image 생성
    h_f, w_f = frame.shape[:2]

    # 텍스트 크기 계산
    try:
        bbox = fnt.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        # 구버전 PIL
        tw, th = fnt.getsize(text)

    # 그릴 영역이 프레임 안에 있는지 확인
    x1 = max(0, x); y1 = max(0, y - th - 4)
    x2 = min(w_f, x1 + tw + 4); y2 = min(h_f, y1 + th + 8)
    if x2 <= x1 or y2 <= y1:
        return

    # 해당 ROI만 PIL로 변환
    roi = frame[y1:y2, x1:x2]
    pil_img = _PIL_Image.fromarray(cv2.cvtColor(roi, cv2.COLOR_BGR2RGB))
    draw = _PIL_Draw.Draw(pil_img)
    draw.text((x - x1, th // 2), text, font=fnt, fill=color_rgb)
    frame[y1:y2, x1:x2] = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def _put_unicode_text_block(frame: np.ndarray, lines: list[str],
                             x0: int, y0: int,
                             font_size: int, color_bgr: tuple,
                             line_gap: int = 4) -> None:
    """여러 줄 텍스트를 y0부터 아래로 렌더링합니다."""
    fnt = _get_font(font_size)
    if fnt is None:
        for i, line in enumerate(lines):
            cy = y0 + i * (font_size + line_gap) + font_size
            _put_unicode_text(frame, line, x0, cy, font_size, color_bgr)
        return

    try:
        sample_bbox = fnt.getbbox("A")
        line_h = sample_bbox[3] - sample_bbox[1] + line_gap
    except AttributeError:
        _, lh = fnt.getsize("A")
        line_h = lh + line_gap

    for i, line in enumerate(lines):
        cy = y0 + i * line_h + line_h
        _put_unicode_text(frame, line, x0, cy, font_size, color_bgr)


# =============================================================================
#  유틸리티
# =============================================================================
def open_folder(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    if platform.system() == "Windows":
        os.startfile(path)
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# =============================================================================
#  SQLite 설정 DB
# =============================================================================
class SettingsDB:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self._path, check_same_thread=False)

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS memo_tabs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, content TEXT, sort_order INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS manual_clip_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT, clip_time TEXT,
                    pre_sec REAL, post_sec REAL, clip_path TEXT);
                CREATE TABLE IF NOT EXISTS blackout_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT, event_time TEXT,
                    brightness REAL, clip_path TEXT);
            """)

    def get(self, key, default=None):
        with self._lock:
            with self._conn() as c:
                row = c.execute(
                    "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self._lock:
            with self._conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                          (key, str(value)))

    def get_float(self, key, default=0.0):
        v = self.get(key)
        try: return float(v) if v is not None else default
        except: return default

    def get_int(self, key, default=0):
        v = self.get(key)
        try: return int(float(v)) if v is not None else default
        except: return default

    def get_bool(self, key, default=True):
        v = self.get(key)
        if v is None: return default
        return v.lower() in ('1', 'true', 'yes')

    def save_memo_tabs(self, tabs: list):
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM memo_tabs")
                c.executemany(
                    "INSERT INTO memo_tabs(title,content,sort_order) VALUES(?,?,?)",
                    [(t.get('title', '메모'), t.get('content', ''), i)
                     for i, t in enumerate(tabs)])

    def load_memo_tabs(self) -> list:
        with self._lock:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT title,content FROM memo_tabs ORDER BY sort_order"
                ).fetchall()
        return [{'title': r[0], 'content': r[1]} for r in rows]

    def log_manual_clip(self, source, pre, post, path):
        with self._lock:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO manual_clip_log"
                    "(source,clip_time,pre_sec,post_sec,clip_path) VALUES(?,?,?,?,?)",
                    (source, datetime.now().isoformat(), pre, post, path))


# =============================================================================
#  시그널
# =============================================================================
class Signals(QObject):
    blackout_detected   = pyqtSignal(str, dict)
    status_message      = pyqtSignal(str)
    auto_click_count    = pyqtSignal(int)
    rec_started         = pyqtSignal(str)
    rec_stopped         = pyqtSignal()
    macro_step_recorded = pyqtSignal(int, int, float)
    manual_clip_saved   = pyqtSignal(str)


# =============================================================================
#  데이터 모델
# =============================================================================
class ScheduleEntry:
    _cnt = 0
    def __init__(self, start_dt, stop_dt):
        ScheduleEntry._cnt += 1
        self.id = ScheduleEntry._cnt
        self.start_dt = start_dt; self.stop_dt = stop_dt
        self.started = False; self.stopped = False; self.done = False

    def label(self):
        s = self.start_dt.strftime("%m/%d %H:%M:%S") if self.start_dt else "—"
        e = self.stop_dt.strftime("%m/%d %H:%M:%S")  if self.stop_dt  else "—"
        return f"#{self.id}  {s} → {e}"


class ClickStep:
    def __init__(self, x, y, delay=0.5):
        self.x = x; self.y = y; self.delay = delay


class MemoOverlayConfig:
    def __init__(self, tab_idx=0, position="bottom-right", target="both", enabled=True):
        self.tab_idx  = tab_idx
        self.position = position
        self.target   = target
        self.enabled  = enabled


# =============================================================================
#  RecorderEngine
# =============================================================================
class RecorderEngine:
    MANUAL_IDLE     = 0
    MANUAL_WAITING  = 1
    MANUAL_COOLDOWN = 2

    def __init__(self, signals: Signals):
        self.signals = signals

        self.running   = False
        self.recording = False
        self.start_time: float | None = None
        self.output_dir = ""

        self._screen_thread: threading.Thread | None = None
        self._camera_thread: threading.Thread | None = None
        self._screen_stop = threading.Event()
        self._camera_stop = threading.Event()

        self.actual_screen_fps = 30.0
        self.actual_camera_fps = 30.0
        self._screen_fps_ts: deque = deque(maxlen=90)
        self._camera_fps_ts: deque = deque(maxlen=90)

        self.screen_recording_enabled   = True
        self.blackout_recording_enabled = True

        self.screen_queue: queue.Queue = queue.Queue(maxsize=3)
        self.camera_queue: queue.Queue = queue.Queue(maxsize=3)

        self.screen_writer: cv2.VideoWriter | None = None
        self.camera_writer: cv2.VideoWriter | None = None
        self._writer_lock = threading.Lock()

        self.segment_duration = 30 * 60
        self.current_segment_start: float | None = None
        self._scr_frame_idx = 0
        self._cam_frame_idx = 0
        self._seg_start_time = 0.0

        self.screen_rois: list = []; self.camera_rois: list = []
        self.screen_roi_avg:  list = []; self.camera_roi_avg:  list = []
        self.screen_roi_prev: list = []; self.camera_roi_prev: list = []
        self.screen_overall_avg = np.zeros(3)
        self.camera_overall_avg = np.zeros(3)

        self.brightness_threshold      = 30.0
        self.blackout_cooldown         = 5.0
        self.screen_last_blackout_time = 0.0
        self.camera_last_blackout_time = 0.0
        self.screen_blackout_count = 0
        self.camera_blackout_count = 0
        self.screen_blackout_events: list = []
        self.camera_blackout_events: list = []
        self.blackout_dir = os.path.join(BASE_DIR, "blackout")

        self.buffer_seconds = 30
        self._screen_buffer: deque = deque()
        self._camera_buffer: deque = deque()
        self._buf_lock = threading.Lock()

        self.memo_texts: list[str] = [""]
        self.memo_overlays: list[MemoOverlayConfig] = [
            MemoOverlayConfig(0, "bottom-right", "both", True)
        ]

        self.auto_click_enabled  = False
        self.auto_click_interval = 1.0
        self.auto_click_count    = 0
        self._ac_thread: threading.Thread | None = None
        self._ac_stop   = threading.Event()

        self.schedules: list[ScheduleEntry] = []
        self.playback_speed = 1.0

        self.camera_list: list[dict] = []
        self.active_camera_idx = 0

        self.macro_steps:    list[ClickStep] = []
        self.macro_running   = False
        self.macro_recording = False
        self.macro_repeat    = 1
        self.macro_loop_gap  = 1.0
        self._macro_thread: threading.Thread | None = None
        self._macro_stop    = threading.Event()
        self._macro_listener = None
        self._macro_last_ts  = 0.0
        self._macro_listen_active_ts = 0.0

        self.manual_pre_sec  = 10.0
        self.manual_post_sec = 10.0
        self.manual_source   = "both"
        self.manual_dir = os.path.join(BASE_DIR, "manual_clip")
        self.manual_state = self.MANUAL_IDLE
        self._manual_lock = threading.Lock()
        self._manual_trigger_time: float = 0.0

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────
    def measured_fps(self, ts_dq: deque) -> float:
        if len(ts_dq) < 2: return 0.0
        span = ts_dq[-1] - ts_dq[0]
        return (len(ts_dq) - 1) / span if span > 0 else 0.0

    @property
    def screen_buf_max(self):
        return max(1, int(self.actual_screen_fps * self.buffer_seconds))

    @property
    def camera_buf_max(self):
        return max(1, int(self.actual_camera_fps * self.buffer_seconds))

    # ── 카메라 스캔 ───────────────────────────────────────────────────────────
    def scan_cameras(self) -> list:
        found = []
        for idx in range(8):
            cap = cv2.VideoCapture(idx)
            if not cap.isOpened():
                cap.release(); continue
            fps = cap.get(cv2.CAP_PROP_FPS)
            if not (fps and 0 < fps < 300):
                frames, t0 = 0, time.time()
                while frames < 15:
                    ret, _ = cap.read()
                    if ret: frames += 1
                elapsed = time.time() - t0
                fps = frames / elapsed if elapsed > 0 else 30.0
            try: name = cap.getBackendName()
            except: name = "Camera"
            cap.release()
            found.append({"idx": idx,
                          "name": f"Camera {idx} [{name}] {fps:.1f}fps",
                          "fps": float(fps)})
        self.camera_list = found
        if found:
            self.active_camera_idx = found[0]["idx"]
            self.actual_camera_fps = found[0]["fps"]
            self.signals.status_message.emit(f"카메라 {len(found)}개 감지됨.")
        else:
            self.signals.status_message.emit("카메라를 찾을 수 없습니다.")
        return found

    # ── ROI / 블랙아웃 ────────────────────────────────────────────────────────
    @staticmethod
    def calc_roi_avg(frame, rois):
        avgs = []
        for rx, ry, rw, rh in rois:
            r = frame[ry:ry+rh, rx:rx+rw]
            avgs.append(r.mean(axis=0).mean(axis=0) if r.size > 0 else np.zeros(3))
        return avgs

    def detect_blackout(self, curr, prev, source: str) -> bool:
        if not curr or not prev or len(curr) != len(prev): return False
        changes = []
        for c, p in zip(curr, prev):
            if np.all(p == 0): continue
            cb = 0.114*c[0]+0.587*c[1]+0.299*c[2]
            pb = 0.114*p[0]+0.587*p[1]+0.299*p[2]
            changes.append(pb - cb)
        if not changes: return False
        mc = float(np.mean(changes))
        if mc < self.brightness_threshold: return False
        now  = time.time()
        last = (self.screen_last_blackout_time if source == "screen"
                else self.camera_last_blackout_time)
        if now - last < self.blackout_cooldown: return False
        if source == "screen":
            self.screen_last_blackout_time = now; self.screen_blackout_count += 1
        else:
            self.camera_last_blackout_time = now; self.camera_blackout_count += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        ev = {'time': datetime.now().strftime("%H:%M:%S.%f")[:-3],
              'brightness_change': mc, 'timestamp': ts}
        lst = (self.screen_blackout_events if source == "screen"
               else self.camera_blackout_events)
        lst.append(ev)
        if len(lst) > 50: lst.pop(0)
        self.signals.blackout_detected.emit(source, ev)
        if self.blackout_recording_enabled:
            threading.Thread(target=self.save_blackout_clip,
                             args=(source, ts), daemon=True).start()
        return True

    def save_blackout_clip(self, source: str, timestamp: str):
        src_dir = os.path.join(self.blackout_dir, source.upper())
        os.makedirs(src_dir, exist_ok=True)
        fps = max((self.actual_screen_fps if source == "screen"
                   else self.actual_camera_fps), 1.0)
        n_pre = int(fps * 10); n_post = int(fps * 10)
        with self._buf_lock:
            buf = self._screen_buffer if source == "screen" else self._camera_buffer
            pre = list(buf)
        post: list = []; deadline = time.time() + 11.0
        while len(post) < n_post and time.time() < deadline:
            time.sleep(0.04)
            with self._buf_lock:
                buf = self._screen_buffer if source == "screen" else self._camera_buffer
                if len(buf) > len(pre): post = list(buf)[len(pre):]
        pre_clip   = pre[-n_pre:] if len(pre) >= n_pre else pre
        all_frames = pre_clip + post[:n_post]
        if not all_frames: return
        bi  = len(pre_clip); h, w = all_frames[0].shape[:2]
        vpath = os.path.join(src_dir, f"blackout_{timestamp}.mp4")
        wr = cv2.VideoWriter(vpath, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
        for i, f in enumerate(all_frames):
            fc = f.copy()
            if i == bi: cv2.rectangle(fc, (4,4),(w-4,h-4),(0,0,255),6)
            wr.write(fc)
        wr.release()
        self.signals.status_message.emit(f"[Blackout/{source}] → {vpath}")

    # ── 수동 녹화 ─────────────────────────────────────────────────────────────
    def save_manual_clip(self) -> bool:
        with self._manual_lock:
            if self.manual_state != self.MANUAL_IDLE: return False
            self.manual_state = self.MANUAL_WAITING
            self._manual_trigger_time = time.time()
        sources = []
        if self.manual_source in ("screen", "both"): sources.append("screen")
        if self.manual_source in ("camera", "both"): sources.append("camera")
        for src in sources:
            threading.Thread(target=self._do_manual_clip,
                             args=(src,), daemon=True).start()
        return True

    def _do_manual_clip(self, source: str):
        fps       = max((self.actual_screen_fps if source == "screen"
                         else self.actual_camera_fps), 1.0)
        trigger   = self._manual_trigger_time
        pre_s     = self.manual_pre_sec; post_s = self.manual_post_sec
        n_pre     = int(fps * pre_s); n_post = int(fps * post_s)
        cooldown_unlock = trigger + post_s / 2.0
        with self._buf_lock:
            buf = self._screen_buffer if source == "screen" else self._camera_buffer
            pre_frames = list(buf)[-n_pre:] if n_pre > 0 else []
        prev_snap_len = len(pre_frames)
        post_frames: list = []; deadline = time.time() + post_s + 2.0
        while len(post_frames) < n_post and time.time() < deadline:
            time.sleep(0.04)
            with self._buf_lock:
                buf = self._screen_buffer if source == "screen" else self._camera_buffer
                cur = list(buf)
            if len(cur) > prev_snap_len: post_frames = cur[prev_snap_len:]
        post_frames = post_frames[:n_post]
        all_frames  = pre_frames + post_frames
        if not all_frames:
            self.signals.status_message.emit(f"[수동녹화] {source}: 저장할 프레임 없음")
            with self._manual_lock: self.manual_state = self.MANUAL_IDLE
            return
        bi   = len(pre_frames); h, w = all_frames[0].shape[:2]
        os.makedirs(self.manual_dir, exist_ok=True)
        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        vpath = os.path.join(self.manual_dir, f"manual_{source}_{ts}.mp4")
        wr    = cv2.VideoWriter(vpath, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
        trigger_str = datetime.fromtimestamp(trigger).strftime("%H:%M:%S")
        for i, f in enumerate(all_frames):
            fc = f.copy()
            ov_ts = datetime.fromtimestamp(
                trigger + (i - bi) / fps).strftime("%H:%M:%S.")
            ov_ts += f"{int(((trigger + (i-bi)/fps) % 1)*1000):03d}"
            _draw_time_overlay(fc, ov_ts,
                               f"{'PRE' if i < bi else 'POST'}  {abs(i-bi)/fps:+.2f}s")
            if i == bi:
                cv2.rectangle(fc,(4,4),(w-4,h-4),(0,200,255),5)
                cv2.putText(fc, f"▼ MANUAL CLIP  {trigger_str}",
                            (10,110), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8,(0,200,255),2,cv2.LINE_AA)
            wr.write(fc)
        wr.release()
        self.signals.status_message.emit(f"[수동녹화] {source} → {vpath}")
        self.signals.manual_clip_saved.emit(vpath)
        remaining = cooldown_unlock - time.time()
        if remaining > 0: time.sleep(remaining)
        with self._manual_lock: self.manual_state = self.MANUAL_IDLE

    # ── 메모 오버레이 ─────────────────────────────────────────────────────────
    def apply_memo_overlays(self, frame: np.ndarray, target_source: str) -> np.ndarray:
        """★ v8: 녹화 여부 관계없이 항상 호출됨. 활성화된 오버레이만 그림."""
        h, w = frame.shape[:2]
        for cfg in self.memo_overlays:
            if not cfg.enabled: continue
            if cfg.target != "both" and cfg.target != target_source: continue
            if cfg.tab_idx >= len(self.memo_texts): continue
            text = self.memo_texts[cfg.tab_idx].strip()
            if not text: continue
            lines = text.splitlines()
            if lines: _draw_memo_block(frame, lines, cfg.position, w, h)
        return frame

    def _add_overlay(self, frame: np.ndarray, rois: list, source: str) -> np.ndarray:
        """★ v8: 녹화 시 타임스탬프 + 항상 메모 오버레이."""
        if self.recording and self.start_time:
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond//1000:03d}"
            e  = time.time() - self.start_time
            hh = int(e//3600); mm = int((e%3600)//60); ss = int(e%60); ms = int((e%1)*1000)
            _draw_time_overlay(frame, now_str, f"REC  {hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}")
        # ★ 녹화 전후 항상 메모 오버레이 적용
        self.apply_memo_overlays(frame, source)
        for i, (rx, ry, rw, rh) in enumerate(rois):
            cv2.rectangle(frame, (rx,ry),(rx+rw,ry+rh),(0,0,255),2)
            cv2.putText(frame, f"ROI{i+1}", (rx, max(ry-5,15)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45,(0,0,255),1,cv2.LINE_AA)
        return frame

    @staticmethod
    def stamp_preview(frame: np.ndarray, engine: "RecorderEngine", source: str) -> np.ndarray:
        """★ v8: 미리보기용. 녹화 중이면 타임스탬프, 항상 메모 오버레이."""
        out = frame.copy()
        if engine.recording and engine.start_time is not None:
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond//1000:03d}"
            e  = time.time() - engine.start_time
            hh = int(e//3600); mm = int((e%3600)//60); ss = int(e%60); ms = int((e%1)*1000)
            _draw_time_overlay(out, now_str, f"REC  {hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}")
        # ★ 항상 메모 오버레이 표시 (녹화 전도 포함)
        engine.apply_memo_overlays(out, source)
        return out

    # ── PTS-sync write ────────────────────────────────────────────────────────
    def _write_frame_sync(self, writer, frame, fps, frame_idx, elapsed):
        expected = int(elapsed * fps); diff = expected - frame_idx
        if diff <= 0:
            writer.write(frame); return frame_idx + 1
        for _ in range(max(1, diff)): writer.write(frame)
        return frame_idx + max(1, diff)

    # ── 세그먼트 생성 ─────────────────────────────────────────────────────────
    def _create_segment(self):
        seg_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._writer_lock:
            if self.screen_writer: self.screen_writer.release()
            if self.camera_writer: self.camera_writer.release()
            self.screen_writer = None; self.camera_writer = None
        scr_fps = max(1.0, self.actual_screen_fps * self.playback_speed)
        cam_fps = max(1.0, self.actual_camera_fps * self.playback_speed)
        if self.screen_recording_enabled:
            with mss.mss() as sct:
                mon = sct.monitors[2 if len(sct.monitors) > 2 else 1]
                spath = os.path.join(self.output_dir, f"screen_{seg_ts}.mp4")
            with self._writer_lock:
                self.screen_writer = cv2.VideoWriter(
                    spath, cv2.VideoWriter_fourcc(*'mp4v'),
                    scr_fps, (mon['width'], mon['height']))
            self.signals.status_message.emit(f"Screen seg: {spath}")
        with self._buf_lock:
            cframe = self._camera_buffer[-1] if self._camera_buffer else None
        if cframe is not None:
            h, w = cframe.shape[:2]
            cpath = os.path.join(self.output_dir, f"camera_{seg_ts}.mp4")
            with self._writer_lock:
                self.camera_writer = cv2.VideoWriter(
                    cpath, cv2.VideoWriter_fourcc(*'mp4v'), cam_fps, (w,h))
            self.signals.status_message.emit(f"Camera seg: {cpath}")
        self.current_segment_start = time.time()
        self._scr_frame_idx = 0; self._cam_frame_idx = 0
        self._seg_start_time = time.time()

    # ── 스크린 루프 ───────────────────────────────────────────────────────────
    def _screen_loop(self):
        with mss.mss() as sct:
            mon = sct.monitors[2 if len(sct.monitors) > 2 else 1]
            interval = 1.0 / max(self.actual_screen_fps, 1.0)
            next_t   = time.perf_counter()
            while not self._screen_stop.is_set():
                now = time.perf_counter()
                if next_t - now > 0: time.sleep(next_t - now)
                next_t += interval
                img   = sct.grab(mon)
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                self._screen_fps_ts.append(time.time())
                if self.screen_rois:
                    avgs = self.calc_roi_avg(frame, self.screen_rois)
                    self.screen_roi_avg     = avgs
                    self.screen_overall_avg = np.mean(avgs,axis=0) if avgs else np.zeros(3)
                    if self.screen_roi_prev: self.detect_blackout(avgs,self.screen_roi_prev,"screen")
                    self.screen_roi_prev = [a.copy() for a in avgs]
                stamped = self._add_overlay(frame.copy(), self.screen_rois, "screen")
                with self._buf_lock:
                    self._screen_buffer.append(stamped)
                    while len(self._screen_buffer) > self.screen_buf_max:
                        self._screen_buffer.popleft()
                if self.recording and self.screen_recording_enabled:
                    with self._writer_lock: w = self.screen_writer
                    if w:
                        elapsed = time.time() - self._seg_start_time
                        self._scr_frame_idx = self._write_frame_sync(
                            w, stamped, self.actual_screen_fps,
                            self._scr_frame_idx, elapsed)
                try: self.screen_queue.put_nowait(frame)
                except queue.Full: pass

    # ── 카메라 루프 ───────────────────────────────────────────────────────────
    def _camera_loop(self):
        idx = self.active_camera_idx
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            self.signals.status_message.emit(f"ERROR: Camera {idx} 열기 실패"); return
        reported = cap.get(cv2.CAP_PROP_FPS)
        cam_fps  = float(reported) if (reported and 0 < reported < 300) else self.actual_camera_fps
        self.actual_camera_fps = cam_fps
        interval = 1.0 / max(cam_fps, 1.0); next_t = time.perf_counter()
        while not self._camera_stop.is_set():
            now = time.perf_counter()
            if next_t - now > 0: time.sleep(next_t - now)
            next_t += interval
            ret, frame = cap.read()
            if not ret: continue
            self._camera_fps_ts.append(time.time())
            if self.camera_rois:
                avgs = self.calc_roi_avg(frame, self.camera_rois)
                self.camera_roi_avg     = avgs
                self.camera_overall_avg = np.mean(avgs,axis=0) if avgs else np.zeros(3)
                if self.camera_roi_prev: self.detect_blackout(avgs,self.camera_roi_prev,"camera")
                self.camera_roi_prev = [a.copy() for a in avgs]
            stamped = self._add_overlay(frame.copy(), self.camera_rois, "camera")
            with self._buf_lock:
                self._camera_buffer.append(stamped)
                while len(self._camera_buffer) > self.camera_buf_max:
                    self._camera_buffer.popleft()
            if self.recording:
                with self._writer_lock: w = self.camera_writer
                if w:
                    elapsed = time.time() - self._seg_start_time
                    self._cam_frame_idx = self._write_frame_sync(
                        w, stamped, cam_fps, self._cam_frame_idx, elapsed)
            try: self.camera_queue.put_nowait(frame)
            except queue.Full: pass
        cap.release()

    # ── 스레드 제어 ───────────────────────────────────────────────────────────
    def start_screen_thread(self):
        if self._screen_thread and self._screen_thread.is_alive(): return
        self._screen_stop.clear()
        self._screen_thread = threading.Thread(target=self._screen_loop, daemon=True)
        self._screen_thread.start()

    def stop_screen_thread(self): self._screen_stop.set()

    def start_camera_thread(self):
        if self._camera_thread and self._camera_thread.is_alive(): return
        self._camera_stop.clear()
        self._camera_thread = threading.Thread(target=self._camera_loop, daemon=True)
        self._camera_thread.start()

    def stop_camera_thread(self): self._camera_stop.set()

    # ── 녹화 시작/정지 ────────────────────────────────────────────────────────
    def start_recording(self):
        if self.recording: return
        ts = datetime.now().strftime("Rec_%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(BASE_DIR, ts)
        os.makedirs(self.output_dir, exist_ok=True)
        self._scr_frame_idx = 0; self._cam_frame_idx = 0
        self._seg_start_time = time.time()
        self._create_segment()
        self.start_time = time.time(); self.recording = True
        self.signals.status_message.emit(f"Recording → {self.output_dir}")
        self.signals.rec_started.emit(self.output_dir)

    def stop_recording(self):
        """
        ★ 수정: recording 플래그를 False로 먼저 설정하고,
        루프 스레드가 현재 프레임을 write 완료할 시간(0.35s)을 대기한 뒤
        VideoWriter를 release 합니다.
        release 전에 writer를 None으로 바꾸지 않아 마지막 프레임까지 안전하게 기록됩니다.
        """
        if not self.recording: return
        self.recording = False
        time.sleep(0.35)          # 루프 스레드가 마지막 프레임 write 완료 대기
        with self._writer_lock:
            if self.screen_writer:
                self.screen_writer.release(); self.screen_writer = None
            if self.camera_writer:
                self.camera_writer.release(); self.camera_writer = None
        self.start_time = None
        self.signals.status_message.emit("Recording stopped")
        self.signals.rec_stopped.emit()

    # ── 오토클릭 ──────────────────────────────────────────────────────────────
    def _ac_loop(self):
        mc = pynput_mouse.Controller() if PYNPUT_AVAILABLE else None
        while not self._ac_stop.is_set():
            if mc: mc.click(pynput_mouse.Button.left)
            self.auto_click_count += 1
            self.signals.auto_click_count.emit(self.auto_click_count)
            self._ac_stop.wait(self.auto_click_interval)

    def start_auto_click(self):
        if self.auto_click_enabled: return
        self.auto_click_enabled = True; self._ac_stop.clear()
        self._ac_thread = threading.Thread(target=self._ac_loop, daemon=True)
        self._ac_thread.start()

    def stop_auto_click(self):
        self.auto_click_enabled = False; self._ac_stop.set()

    def reset_click_count(self):
        self.auto_click_count = 0; self.signals.auto_click_count.emit(0)

    # ── 클릭 매크로 ───────────────────────────────────────────────────────────
    def macro_start_recording(self):
        if not PYNPUT_AVAILABLE or self.macro_recording: return
        self.macro_recording = True
        self.signals.status_message.emit("매크로 기록 준비 중…")
        def _delayed():
            time.sleep(0.3)
            if not self.macro_recording: return
            self._macro_listen_active_ts = time.time()
            self._macro_last_ts = self._macro_listen_active_ts
            def on_click(x, y, button, pressed):
                if not pressed or button != pynput_mouse.Button.left: return
                if not self.macro_recording: return
                now = time.time()
                if now < self._macro_listen_active_ts: return
                delay = round(now - self._macro_last_ts, 3)
                self._macro_last_ts = now
                step = ClickStep(int(x), int(y), delay)
                self.macro_steps.append(step)
                self.signals.macro_step_recorded.emit(int(x), int(y), delay)
            self._macro_listener = pynput_mouse.Listener(on_click=on_click)
            self._macro_listener.start()
            self.signals.status_message.emit("매크로 기록 중")
        threading.Thread(target=_delayed, daemon=True).start()

    def macro_stop_recording(self):
        self.macro_recording = False
        def _stop():
            time.sleep(0.1)
            if self._macro_listener:
                self._macro_listener.stop(); self._macro_listener = None
        threading.Thread(target=_stop, daemon=True).start()

    def macro_start_run(self):
        if not PYNPUT_AVAILABLE or self.macro_running or not self.macro_steps: return
        self.macro_running = True; self._macro_stop.clear()
        self._macro_thread = threading.Thread(target=self._macro_loop, daemon=True)
        self._macro_thread.start()

    def _macro_loop(self):
        mc = pynput_mouse.Controller(); rep = 0; infinite = (self.macro_repeat == 0)
        while not self._macro_stop.is_set():
            for step in list(self.macro_steps):
                if self._macro_stop.is_set(): break
                waited = 0.0
                while waited < step.delay and not self._macro_stop.is_set():
                    chunk = min(0.05, step.delay - waited); time.sleep(chunk); waited += chunk
                if self._macro_stop.is_set(): break
                mc.position = (step.x, step.y); mc.click(pynput_mouse.Button.left)
                self.signals.status_message.emit(f"[Macro] ({step.x},{step.y})")
            rep += 1
            if not infinite and rep >= self.macro_repeat: break
            waited = 0.0
            while waited < self.macro_loop_gap and not self._macro_stop.is_set():
                chunk = min(0.05, self.macro_loop_gap - waited); time.sleep(chunk); waited += chunk
        self.macro_running = False
        self.signals.status_message.emit("[Macro] 실행 완료")

    def macro_stop_run(self): self._macro_stop.set(); self.macro_running = False
    def macro_clear(self): self.macro_steps.clear()

    # ── 예약 ─────────────────────────────────────────────────────────────────
    def schedule_tick(self) -> list:
        now = datetime.now(); actions = []
        for s in list(self.schedules):
            if s.done: continue
            if s.start_dt and not s.started:
                delta = (s.start_dt - now).total_seconds()
                if -2 <= delta <= 1:
                    s.started = True
                    if not self.recording: actions.append(('start', s))
            if s.stop_dt and s.started and not s.stopped:
                delta = (s.stop_dt - now).total_seconds()
                if -2 <= delta <= 1:
                    s.stopped = True; s.done = True
                    if self.recording: actions.append(('stop', s))
            if s.started and not s.stop_dt and not s.done: s.done = True
        return actions

    # ── 엔진 시작/정지 ────────────────────────────────────────────────────────
    def start(self):
        self.running = True
        threading.Thread(target=self.scan_cameras, daemon=True).start()
        self.start_screen_thread()
        self.start_camera_thread()

    def stop(self):
        """
        ★ 수정: stop_recording()을 동기적으로 완전히 수행한 뒤 스레드를 종료.
        이 순서를 지켜야 VideoWriter가 정상 완성됩니다.
        """
        if self.recording:
            self.stop_recording()   # 내부에서 0.35s sleep + release
        self.stop_auto_click()
        self.macro_stop_run()
        self.macro_stop_recording()
        self._screen_stop.set()
        self._camera_stop.set()
        self.running = False


# =============================================================================
#  공용 그리기 함수
# =============================================================================
def _draw_time_overlay(frame: np.ndarray, now_str: str, elapsed_str: str) -> None:
    """좌상단 시각 / 경과 오버레이. ASCII 전용이므로 cv2.putText 유지."""
    ov = frame.copy()
    cv2.rectangle(ov, (4,4),(440,78),(0,0,0),-1)
    cv2.addWeighted(ov, 0.45, frame, 0.55, 0, frame)
    cv2.putText(frame, now_str,     (10,32), cv2.FONT_HERSHEY_SIMPLEX,0.72,(0,255,80),  2,cv2.LINE_AA)
    cv2.putText(frame, elapsed_str, (10,68), cv2.FONT_HERSHEY_SIMPLEX,0.65,(80,220,255),2,cv2.LINE_AA)


def _draw_memo_block(frame: np.ndarray, lines: list[str],
                     position: str, fw: int, fh: int,
                     font_size: int = 18) -> None:
    """
    ★ v9: 우측 위치일 때 잘림 방지 — box_w를 실제 텍스트 폭에 맞게 계산하고
    배경 박스와 텍스트 모두 프레임 경계 안에 완전히 들어오도록 클램핑.
    """
    if not lines: return
    fnt = _get_font(font_size)
    pad = 8

    # ── 줄 높이 ──────────────────────────────────────────────────────────────
    try:
        sample_bbox = fnt.getbbox("A가") if fnt else None
        line_h = (sample_bbox[3] - sample_bbox[1] + 6) if sample_bbox else font_size + 6
    except Exception:
        line_h = font_size + 6

    # ── 각 줄의 실제 렌더 폭 측정 ────────────────────────────────────────────
    max_tw = 0
    for line in lines:
        try:
            bb = fnt.getbbox(line) if fnt else None
            tw = (bb[2] - bb[0]) if bb else len(line) * (font_size // 2 + 2)
        except Exception:
            tw = len(line) * (font_size // 2 + 2)
        max_tw = max(max_tw, tw)

    # 텍스트 내부 여백(좌우 각 6px) + 배경 박스 여백(좌우 각 4px)
    inner_pad = 12   # 텍스트↔박스 내부 여백
    bg_pad    = 4    # 배경 박스↔프레임 여백

    box_w = max_tw + inner_pad * 2
    box_h = len(lines) * line_h + 14

    # 프레임을 벗어나지 않도록 box_w 제한
    box_w = min(box_w, fw - bg_pad * 2 - pad * 2)

    # ── 위치 계산 ─────────────────────────────────────────────────────────────
    if position == "top-left":
        x0, y0 = pad, pad + 30
    elif position == "top-right":
        # ★ 오른쪽 가장자리에서 (box_w + pad + bg_pad)만큼 안쪽에서 시작
        x0 = fw - box_w - pad - bg_pad
        y0 = pad + 30
    elif position == "bottom-left":
        x0 = pad
        y0 = fh - box_h - pad - bg_pad
    elif position == "center":
        x0 = (fw - box_w) // 2
        y0 = (fh - box_h) // 2
    else:  # bottom-right (기본값)
        x0 = fw - box_w - pad - bg_pad
        y0 = fh - box_h - pad - bg_pad

    # 프레임 경계 완전 클램핑
    x0 = max(bg_pad, x0)
    y0 = max(bg_pad, y0)
    # 오른쪽/아래쪽도 초과 방지
    x0 = min(x0, fw - box_w - bg_pad)
    y0 = min(y0, fh - box_h - bg_pad)

    # ── 반투명 배경 박스 ──────────────────────────────────────────────────────
    bx1 = max(0, x0 - bg_pad)
    by1 = max(0, y0 - bg_pad)
    bx2 = min(fw, x0 + box_w + bg_pad)
    by2 = min(fh, y0 + box_h + bg_pad)
    ov = frame.copy()
    cv2.rectangle(ov, (bx1, by1), (bx2, by2), (0, 0, 0), -1)
    cv2.addWeighted(ov, 0.55, frame, 0.45, 0, frame)

    # ── 텍스트 렌더링 (PIL) ───────────────────────────────────────────────────
    for j, line in enumerate(lines):
        cy = y0 + j * line_h + line_h
        if cy > fh - bg_pad: break
        if not line: continue
        # 텍스트 시작 x는 박스 왼쪽 + 내부 여백
        tx = x0 + inner_pad - 6   # _put_unicode_text의 내부 오프셋 보정
        _put_unicode_text(frame, line, tx, cy, font_size, (100, 240, 255))


# =============================================================================
#  PreviewLabel
# =============================================================================
class PreviewLabel(QLabel):
    roi_changed = pyqtSignal()
    def __init__(self, source: str, engine: RecorderEngine, parent=None):
        super().__init__(parent)
        self.source=source; self.engine=engine
        self._drawing=False; self._pt1=self._pt2=QPoint()
        self._raw_size=(1,1); self._active=True
        self.setMinimumSize(320,180)
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter); self._idle_style()
    def _idle_style(self): self.setStyleSheet("background:#0d0d1e;border:1px solid #334;")
    def set_active(self, v: bool):
        self._active=v
        if not v:
            self.clear(); self.setText("⏸ Thread Paused")
            self.setStyleSheet("background:#0d0d1e;border:1px solid #334;color:#555;font-size:18px;font-weight:bold;")
        else: self.clear(); self._idle_style()
    def _rois(self): return self.engine.screen_rois if self.source=="screen" else self.engine.camera_rois
    def _label_to_raw(self, qp: QPoint):
        pw,ph=self.width(),self.height(); rw,rh=self._raw_size; sc=min(pw/rw,ph/rh)
        ox=(pw-rw*sc)/2; oy=(ph-rh*sc)/2
        return int((qp.x()-ox)/sc), int((qp.y()-oy)/sc)
    def mousePressEvent(self, e):
        if e.button()==Qt.LeftButton: self._drawing=True; self._pt1=self._pt2=e.pos()
        elif e.button()==Qt.RightButton:
            if self._rois(): self._rois().pop(); self.roi_changed.emit()
    def mouseMoveEvent(self, e):
        if self._drawing: self._pt2=e.pos(); self.update()
    def mouseReleaseEvent(self, e):
        if self._drawing and e.button()==Qt.LeftButton:
            self._drawing=False
            x1,y1=self._label_to_raw(self._pt1); x2,y2=self._label_to_raw(self._pt2)
            rx,ry=min(x1,x2),min(y1,y2); rw,rh=abs(x1-x2),abs(y1-y2)
            if rw>5 and rh>5 and len(self._rois())<10:
                self._rois().append((rx,ry,rw,rh)); self.roi_changed.emit()
            self.update()
    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drawing:
            p=QPainter(self); p.setPen(QPen(QColor(255,80,80),2,Qt.DashLine))
            p.drawRect(QRect(self._pt1,self._pt2).normalized())
    def update_frame(self, frame: np.ndarray):
        if not self._active: return
        self._raw_size=(frame.shape[1],frame.shape[0])
        rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB); disp=rgb.copy()
        for i,(rx,ry,rw,rh) in enumerate(self._rois()):
            cv2.rectangle(disp,(rx,ry),(rx+rw,ry+rh),(255,60,60),2)
            cv2.putText(disp,f"ROI{i+1}",(rx,max(ry-4,12)),cv2.FONT_HERSHEY_SIMPLEX,0.45,(255,60,60),1)
        h,w,_=disp.shape; qi=QImage(disp.data,w,h,3*w,QImage.Format_RGB888)
        pix=QPixmap.fromImage(qi).scaled(self.size(),Qt.KeepAspectRatio,Qt.SmoothTransformation)
        self.setPixmap(pix)


# =============================================================================
#  ThreadToggleBtn
# =============================================================================
class ThreadToggleBtn(QPushButton):
    def __init__(self, lon="▶ ON", loff="⏸ OFF", parent=None):
        super().__init__(parent)
        self._lon=lon; self._loff=loff
        self.setFixedHeight(26); self.setCheckable(True); self.setChecked(True)
        self.toggled.connect(self._upd); self._upd(True)
    def _upd(self, checked):
        self.setText(self._lon if checked else self._loff)
        on  = ("QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
               "stop:0 #1a6b3a,stop:1 #27ae60);color:#eaffea;border:none;"
               "border-radius:13px;font-size:10px;font-weight:bold;padding:0 10px;}"
               "QPushButton:hover{background:#2ecc71;}")
        off = ("QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
               "stop:0 #3a1a1a,stop:1 #7f3030);color:#ffcccc;border:none;"
               "border-radius:13px;font-size:10px;font-weight:bold;padding:0 10px;}"
               "QPushButton:hover{background:#c0392b;}")
        self.setStyleSheet(on if checked else off)


# =============================================================================
#  CollapsibleSection
# =============================================================================
class CollapsibleSection(QWidget):
    def __init__(self, title: str, color: str = "#3a7bd5", parent=None):
        super().__init__(parent)
        self._collapsed = False
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        self._btn = QPushButton(); self._btn.setCheckable(True); self._btn.setChecked(False)
        self._btn.setFixedHeight(34); self._btn.clicked.connect(self._toggle)
        self._title=title; self._color=color; self._style_btn(False)
        outer.addWidget(self._btn)
        self._content = QWidget()
        self._content.setStyleSheet(
            "QWidget{background:#10102a;border:1px solid #2a2a4a;"
            "border-top:none;border-radius:0 0 6px 6px;}")
        cl = QVBoxLayout(self._content); cl.setContentsMargins(8,8,8,10); cl.setSpacing(6)
        self._cl = cl; outer.addWidget(self._content)
    def _style_btn(self, collapsed: bool):
        arrow = "▶" if collapsed else "▼"
        self._btn.setText(f"  {arrow}  {self._title}")
        bot = "1px solid #2a2a4a" if collapsed else "none"
        rad = "6px" if collapsed else "6px 6px 0 0"
        self._btn.setStyleSheet(f"""
            QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1e2240,stop:0.5 #1a1a38,stop:1 #12122a);
                color:#7ab4d4;font-size:12px;font-weight:bold;
                text-align:left;padding:0 12px;
                border-left:3px solid {self._color};
                border-top:1px solid #2a2a4a;border-right:1px solid #2a2a4a;
                border-bottom:{bot};border-radius:{rad};}}
            QPushButton:hover{{background:#22224a;color:#9ad4f4;}}
        """)
    def _toggle(self):
        self._collapsed = not self._collapsed
        self._content.setVisible(not self._collapsed); self._style_btn(self._collapsed)
    def add_widget(self, w): self._cl.addWidget(w)
    def set_collapsed(self, v: bool):
        if v != self._collapsed: self._toggle()
    def is_collapsed(self) -> bool: return self._collapsed


# =============================================================================
#  CameraWindow  ★ 카메라 목록 체크박스 + 접기/펼치기 복원
# =============================================================================
class CameraWindow(QDialog):
    def __init__(self, engine: RecorderEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self.setWindowTitle("📷  Camera Feed")
        self.setWindowFlags(Qt.Window|Qt.WindowMinimizeButtonHint|
                            Qt.WindowMaximizeButtonHint|Qt.WindowCloseButtonHint)
        self.resize(680,560); self.setMinimumSize(420,300)
        self.setStyleSheet("background:#0d0d1e;color:#ddd;")

        root = QVBoxLayout(self); root.setSpacing(0); root.setContentsMargins(0,0,0,0)

        # ── 헤더 바 ─────────────────────────────────────────────────────────
        hdr = QFrame()
        hdr.setStyleSheet("QFrame{background:#0a0a18;border-bottom:1px solid #1e2a3a;}")
        hdr.setFixedHeight(40)
        hl = QHBoxLayout(hdr); hl.setContentsMargins(10,0,8,0); hl.setSpacing(8)
        hl.addWidget(QLabel("📷"))
        t = QLabel("Camera Preview"); t.setStyleSheet("color:#9ab;font-weight:bold;font-size:13px;")
        hl.addWidget(t); hl.addStretch()
        self._fps_lbl = QLabel("실측 FPS: —"); self._fps_lbl.setStyleSheet("color:#888;font-size:11px;")
        hl.addWidget(self._fps_lbl)
        self._toggle = ThreadToggleBtn("▶ ON","⏸ OFF"); self._toggle.toggled.connect(self._on_thread_toggle)
        hl.addWidget(self._toggle)
        # 접기/펼치기 버튼
        self._fold_btn = QPushButton("▲ 카메라 선택 숨기기")
        self._fold_btn.setFixedHeight(26); self._fold_btn.setCheckable(True); self._fold_btn.setChecked(False)
        self._fold_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:4px;font-size:10px;padding:2px 8px;}"
            "QPushButton:checked{background:#0d1a2a;color:#4a8aaa;}"
            "QPushButton:hover{background:#223344;}")
        self._fold_btn.toggled.connect(self._on_fold_toggle)
        hl.addWidget(self._fold_btn)
        root.addWidget(hdr)

        # ── 카메라 선택 패널 ─────────────────────────────────────────────────
        self._cam_panel = QFrame()
        self._cam_panel.setStyleSheet("QFrame{background:#0c0c1e;border-bottom:1px solid #1a2a3a;}")
        cp = QVBoxLayout(self._cam_panel); cp.setContentsMargins(10,8,10,8); cp.setSpacing(6)

        scan_row = QHBoxLayout()
        self._scan_btn = QPushButton("🔍  카메라 스캔"); self._scan_btn.setFixedHeight(28)
        self._scan_btn.setStyleSheet(
            "QPushButton{background:#1a2a4a;color:#7bc8e0;border:1px solid #2a4a7a;"
            "border-radius:4px;font-size:11px;padding:2px 12px;}"
            "QPushButton:hover{background:#223366;}"
            "QPushButton:disabled{background:#0d1525;color:#446;}")
        self._scan_btn.clicked.connect(self._on_scan)
        self._sel_lbl = QLabel("선택: —"); self._sel_lbl.setStyleSheet("color:#f0c040;font-size:11px;font-weight:bold;")
        scan_row.addWidget(self._scan_btn); scan_row.addStretch(); scan_row.addWidget(self._sel_lbl)
        cp.addLayout(scan_row)

        # 체크박스 목록 (스크롤 가능)
        list_scroll = QScrollArea(); list_scroll.setWidgetResizable(True)
        list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        list_scroll.setFixedHeight(100)
        list_scroll.setStyleSheet(
            "QScrollArea{border:1px solid #1a2a3a;border-radius:4px;background:#080818;}"
            "QScrollBar:vertical{background:#0d0d1e;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#2a3a5a;border-radius:3px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._cb_container = QWidget(); self._cb_container.setStyleSheet("background:#080818;")
        self._cb_layout = QVBoxLayout(self._cb_container)
        self._cb_layout.setContentsMargins(4,4,4,4); self._cb_layout.setSpacing(2)
        list_scroll.setWidget(self._cb_container)
        cp.addWidget(list_scroll)

        self._cam_info_lbl = QLabel("카메라를 스캔하세요")
        self._cam_info_lbl.setStyleSheet("color:#556;font-size:10px;font-family:monospace;padding:1px 2px;")
        cp.addWidget(self._cam_info_lbl)
        root.addWidget(self._cam_panel)

        # ── 미리보기 영역 ─────────────────────────────────────────────────────
        pc = QWidget(); pc.setStyleSheet("background:#0d0d1e;")
        pl = QVBoxLayout(pc); pl.setContentsMargins(6,4,6,4); pl.setSpacing(3)
        self._lbl = PreviewLabel("camera", self.engine)
        pl.addWidget(self._lbl, 1)
        hint = QLabel("Left-drag: add ROI  |  Right-click: remove")
        hint.setStyleSheet("color:#444;font-size:10px;"); hint.setAlignment(Qt.AlignCenter)
        pl.addWidget(hint)
        root.addWidget(pc, 1)

        QTimer(self, timeout=self._upd_fps, interval=2000).start()
        # 초기 스캔
        threading.Thread(target=self._bg_scan, daemon=True).start()

    # ── 접기/펼치기 ──────────────────────────────────────────────────────────
    def _on_fold_toggle(self, folded: bool):
        self._cam_panel.setVisible(not folded)
        self._fold_btn.setText("▼ 카메라 선택 표시" if folded else "▲ 카메라 선택 숨기기")
        QTimer.singleShot(10, self._fit_window)

    def _fit_window(self):
        self.adjustSize()
        min_h = 300 if self._fold_btn.isChecked() else 480
        if self.height() < min_h: self.resize(self.width(), min_h)

    # ── 카메라 스캔 ──────────────────────────────────────────────────────────
    def _bg_scan(self):
        self.engine.scan_cameras()
        QTimer.singleShot(0, self._populate_checkboxes)

    def _on_scan(self):
        while self._cb_layout.count():
            it = self._cb_layout.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._cam_info_lbl.setText("스캔 중…"); self._scan_btn.setEnabled(False)
        threading.Thread(target=self._bg_scan, daemon=True).start()
        QTimer.singleShot(600, lambda: self._scan_btn.setEnabled(True))

    def _populate_checkboxes(self):
        while self._cb_layout.count():
            it = self._cb_layout.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        cams = self.engine.camera_list
        if not cams:
            lbl = QLabel("  연결된 카메라를 찾을 수 없습니다.")
            lbl.setStyleSheet("color:#666;font-size:11px;padding:6px;")
            self._cb_layout.addWidget(lbl)
            self._cam_info_lbl.setText("카메라 없음"); return
        self._cam_cbs: dict[int, QCheckBox] = {}
        for cam in cams:
            cb = QCheckBox(f"  {cam['name']}")
            cb.setStyleSheet(
                "QCheckBox{color:#ccd;font-size:11px;spacing:6px;padding:5px 8px;border-radius:3px;}"
                "QCheckBox:hover{background:#121224;}"
                "QCheckBox::indicator{width:15px;height:15px;}"
                "QCheckBox::indicator:checked{background:#2a6a9a;border:2px solid #4a9aca;border-radius:3px;}"
                "QCheckBox::indicator:unchecked{background:#0d0d1e;border:1px solid #2a3a4a;border-radius:3px;}")
            cb.setChecked(cam["idx"] == self.engine.active_camera_idx)
            cb.toggled.connect(lambda checked, idx=cam["idx"], c=cb:
                               self._on_cb_toggled(idx, checked, c))
            self._cam_cbs[cam["idx"]] = cb
            self._cb_layout.addWidget(cb)
        self._cb_layout.addStretch()
        self._cam_info_lbl.setText(f"총 {len(cams)}개 감지  |  하나만 선택 가능  |  FPS 자동 감지")
        self._update_sel_label(self.engine.active_camera_idx)

    def _on_cb_toggled(self, idx: int, checked: bool, cb: QCheckBox):
        if not checked:
            # 이미 선택된 항목 해제 방지
            cb.blockSignals(True); cb.setChecked(True); cb.blockSignals(False); return
        if idx == self.engine.active_camera_idx: return
        # 나머지 체크박스 해제
        for other_idx, other_cb in self._cam_cbs.items():
            if other_idx != idx:
                other_cb.blockSignals(True); other_cb.setChecked(False); other_cb.blockSignals(False)
        # 스레드 OFF → 메타데이터 변경 → 스레드 ON
        self._toggle.blockSignals(True); self._toggle.setChecked(False); self._toggle.blockSignals(False)
        self._lbl.set_active(False); self.engine.stop_camera_thread()
        cam = next((c for c in self.engine.camera_list if c["idx"] == idx), None)
        if cam:
            self.engine.active_camera_idx = idx; self.engine.actual_camera_fps = cam["fps"]
            self.signals.status_message.emit(f"카메라 변경: {cam['name']}")
            self._update_sel_label(idx)
            self._cam_info_lbl.setText(f"활성: {cam['name']}  |  {cam['fps']:.2f} fps")
        def _restart():
            if self.engine._camera_thread and self.engine._camera_thread.is_alive():
                self.engine._camera_thread.join(timeout=2.0)
            QTimer.singleShot(0, self._do_thread_on)
        threading.Thread(target=_restart, daemon=True).start()

    def _do_thread_on(self):
        self._toggle.blockSignals(True); self._toggle.setChecked(True); self._toggle.blockSignals(False)
        self._lbl.set_active(True); self.engine._camera_stop.clear()
        self.engine._camera_thread = threading.Thread(target=self.engine._camera_loop, daemon=True)
        self.engine._camera_thread.start()
        self.signals.status_message.emit("Camera thread restarted")

    def _update_sel_label(self, idx: int):
        cam = next((c for c in self.engine.camera_list if c["idx"] == idx), None)
        self._sel_lbl.setText(f"선택: {cam['name'] if cam else f'Camera {idx}'}")

    def _upd_fps(self):
        fps = self.engine.measured_fps(self.engine._camera_fps_ts)
        self._fps_lbl.setText(f"실측 FPS: {fps:.1f}")

    def _on_thread_toggle(self, checked: bool):
        self._lbl.set_active(checked)
        if checked: self.engine.start_camera_thread()
        else:       self.engine.stop_camera_thread()

    def get_label(self): return self._lbl
    def closeEvent(self, e): e.ignore(); self.hide()


# =============================================================================
#  FeatureListWidget / FeatureBar
# =============================================================================
class FeatureListWidget(QWidget):
    toggled       = pyqtSignal(str, bool)
    order_changed = pyqtSignal(list)
    scroll_to_key = pyqtSignal(str)   # ★ v9: 더블클릭 → 해당 섹션으로 스크롤
    FEATURES = [
        ("recording",   "⏺  Recording"),
        ("manual_clip", "🎬  수동 녹화"),
        ("schedule",    "⏰  Schedule"),
        ("blackout",    "⚡  Blackout"),
        ("autoclick",   "🖱  Auto-Click"),
        ("macro",       "🎯  Click Macro"),
        ("memo",        "📝  Memo"),
        ("log",         "📋  Log"),
        ("reset",       "🔄  설정 초기화"),
    ]
    _IH=34; _DC="#2a3a5a"; _IC="#12122e"; _HC="#1a2a3a"
    def __init__(self, parent=None):
        super().__init__(parent)
        self._checks={}; self._rows=[]; self._drag_idx=-1
        self._drag_start=QPoint(); self._dragging=False
        self._dbl_pending = False   # 더블클릭 감지용
        lay=QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(2)
        self._lay=lay
        for key,text in self.FEATURES:
            row=self._make_row(key,text); self._rows.append((key,row)); lay.addWidget(row)
        lay.addStretch()
    def _make_row(self,key,text):
        row=QWidget(); row.setFixedHeight(self._IH)
        row.setStyleSheet(f"QWidget{{background:{self._IC};border-radius:4px;}}QWidget:hover{{background:{self._HC};}}")
        row.setCursor(Qt.OpenHandCursor)
        h=QHBoxLayout(row); h.setContentsMargins(6,2,8,2); h.setSpacing(6)
        grip=QLabel("⠿"); grip.setStyleSheet("color:#447;font-size:16px;"); h.addWidget(grip)
        cb=QCheckBox(text); cb.setChecked(True)
        cb.setStyleSheet("QCheckBox{font-size:12px;color:#dde;spacing:6px;background:transparent;}QCheckBox::indicator{width:15px;height:15px;}")
        cb.toggled.connect(lambda v,k=key: self.toggled.emit(k,v)); self._checks[key]=cb; h.addWidget(cb,1)
        return row
    def is_enabled(self,key): return self._checks[key].isChecked()
    def current_order(self): return [k for k,_ in self._rows]
    def _row_at_y(self,y):
        for i,(_,w) in enumerate(self._rows):
            if w.y()<=y<w.y()+w.height(): return i
        return -1
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton:
            idx=self._row_at_y(e.pos().y())
            if idx>=0: self._drag_idx=idx; self._drag_start=e.pos()
        super().mousePressEvent(e)
    def mouseDoubleClickEvent(self, e):
        """★ v9: 더블클릭 → scroll_to_key 시그널 발생."""
        if e.button() == Qt.LeftButton:
            idx = self._row_at_y(e.pos().y())
            if idx >= 0:
                key, _ = self._rows[idx]
                self._dbl_pending = True
                self.scroll_to_key.emit(key)
        super().mouseDoubleClickEvent(e)
    def mouseMoveEvent(self,e):
        if self._drag_idx>=0 and (e.pos()-self._drag_start).manhattanLength()>8:
            self._dragging=True; self._highlight(self._row_at_y(e.pos().y()))
        super().mouseMoveEvent(e)
    def mouseReleaseEvent(self,e):
        if self._dragging and self._drag_idx>=0:
            tgt=self._row_at_y(e.pos().y())
            if tgt>=0 and tgt!=self._drag_idx:
                item=self._rows.pop(self._drag_idx); self._rows.insert(tgt,item)
                while self._lay.count():
                    it=self._lay.takeAt(0)
                    if it.widget(): it.widget().setParent(None)
                for _,w in self._rows: w.setParent(self); self._lay.addWidget(w)
                self._lay.addStretch(); self.order_changed.emit(self.current_order())
        self._dragging=False; self._drag_idx=-1; self._highlight(-1)
        super().mouseReleaseEvent(e)
    def _highlight(self,idx):
        for i,(_,w) in enumerate(self._rows):
            if i==idx: w.setStyleSheet(f"QWidget{{background:{self._DC};border:1px solid #5a7aaa;border-radius:4px;}}")
            else: w.setStyleSheet(f"QWidget{{background:{self._IC};border-radius:4px;}}QWidget:hover{{background:{self._HC};}}")


class FeatureBar(QFrame):
    toggled       = pyqtSignal(str, bool)
    order_changed = pyqtSignal(list)
    scroll_to_key = pyqtSignal(str)   # ★ v9: 더블클릭 전파
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setStyleSheet("QFrame{background:#08081a;border-bottom:2px solid #2a3a5a;}")
        outer=QVBoxLayout(self); outer.setContentsMargins(8,6,8,4); outer.setSpacing(4)
        hdr=QHBoxLayout()
        t=QLabel("⚙  표시 · 순서 설정"); t.setStyleSheet("color:#7ab4d4;font-size:12px;font-weight:bold;")
        h=QLabel("드래그: 순서변경  |  더블클릭: 해당 섹션으로 이동")
        h.setStyleSheet("color:#446;font-size:10px;")
        hdr.addWidget(t); hdr.addStretch(); hdr.addWidget(h); outer.addLayout(hdr)
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); scroll.setFixedHeight(175)
        scroll.setStyleSheet(
            "QScrollArea{border:1px solid #223;background:transparent;border-radius:4px;}"
            "QScrollBar:vertical{background:#0d0d1e;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:3px;min-height:16px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._list=FeatureListWidget()
        self._list.toggled.connect(self.toggled)
        self._list.order_changed.connect(self.order_changed)
        self._list.scroll_to_key.connect(self.scroll_to_key)   # ★ 전파
        scroll.setWidget(self._list)
        outer.addWidget(scroll)
    def is_enabled(self,key): return self._list.is_enabled(key)
    def current_order(self): return self._list.current_order()


# =============================================================================
#  TimestampMemoEdit
# =============================================================================
class TimestampMemoEdit(QPlainTextEdit):
    """
    ★ v9:
    - 좌클릭: 해당 줄 맨 앞에 타임스탬프 삽입 (한 줄에 하나만 유지)
    - 우클릭: 해당 줄의 타임스탬프 제거(롤백)
    """
    # 타임스탬프 패턴: [YYYY-MM-DD HH:MM:SS] 
    _TS_RE = None  # 지연 초기화

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timestamp_enabled = True

    @classmethod
    def _ts_pattern(cls):
        if cls._TS_RE is None:
            import re
            cls._TS_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ')
        return cls._TS_RE

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if not self.timestamp_enabled:
            return

        cur = self.textCursor()
        cur.movePosition(QTextCursor.StartOfBlock)
        cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        line_text = cur.selectedText()

        if e.button() == Qt.LeftButton:
            # ── 타임스탬프 삽입 (줄 앞에 하나만) ────────────────────────────
            # 이미 타임스탬프가 있으면 기존 것을 현재 시각으로 교체
            cur.movePosition(QTextCursor.StartOfBlock)
            m = self._ts_pattern().match(line_text)
            if m:
                # 기존 타임스탬프 범위 선택 후 교체
                sel_cur = self.textCursor()
                sel_cur.movePosition(QTextCursor.StartOfBlock)
                sel_cur.movePosition(QTextCursor.Right,
                                     QTextCursor.KeepAnchor, m.end())
                sel_cur.insertText(datetime.now().strftime("[%Y-%m-%d %H:%M:%S] "))
                self.setTextCursor(sel_cur)
            else:
                # 없으면 맨 앞에 새로 삽입
                start_cur = self.textCursor()
                start_cur.movePosition(QTextCursor.StartOfBlock)
                start_cur.insertText(datetime.now().strftime("[%Y-%m-%d %H:%M:%S] "))
                self.setTextCursor(start_cur)

        elif e.button() == Qt.RightButton:
            # ── 타임스탬프 롤백(제거) ─────────────────────────────────────────
            m = self._ts_pattern().match(line_text)
            if m:
                sel_cur = self.textCursor()
                sel_cur.movePosition(QTextCursor.StartOfBlock)
                sel_cur.movePosition(QTextCursor.Right,
                                     QTextCursor.KeepAnchor, m.end())
                sel_cur.removeSelectedText()
                self.setTextCursor(sel_cur)
            # 우클릭 기본 컨텍스트 메뉴는 억제 (타임스탬프 롤백만 수행)


# =============================================================================
#  MemoOverlayRow
# =============================================================================
class MemoOverlayRow(QWidget):
    changed = pyqtSignal()
    removed = pyqtSignal(object)
    POS_LABELS=["top-left","top-right","bottom-left","bottom-right","center"]
    TGT_LABELS=["both","screen","camera"]
    def __init__(self, cfg: MemoOverlayConfig, tab_count: int, parent=None):
        super().__init__(parent); self.cfg=cfg
        lay=QHBoxLayout(self); lay.setContentsMargins(2,2,2,2); lay.setSpacing(6)
        self._en=QCheckBox(); self._en.setChecked(cfg.enabled); self._en.toggled.connect(self._on_change); lay.addWidget(self._en)
        lay.addWidget(QLabel("탭:"))
        self._tab=QSpinBox(); self._tab.setRange(1,max(tab_count,1)); self._tab.setValue(cfg.tab_idx+1); self._tab.setFixedWidth(48); self._tab.valueChanged.connect(self._on_change); lay.addWidget(self._tab)
        lay.addWidget(QLabel("위치:"))
        self._pos=QComboBox()
        for p in ["좌상","우상","좌하","우하","중앙"]: self._pos.addItem(p)
        if cfg.position in self.POS_LABELS: self._pos.setCurrentIndex(self.POS_LABELS.index(cfg.position))
        self._pos.currentIndexChanged.connect(self._on_change); self._pos.setFixedWidth(62); lay.addWidget(self._pos)
        lay.addWidget(QLabel("대상:"))
        self._tgt=QComboBox()
        for t in ["Both","Display","Camera"]: self._tgt.addItem(t)
        if cfg.target in self.TGT_LABELS: self._tgt.setCurrentIndex(self.TGT_LABELS.index(cfg.target))
        self._tgt.currentIndexChanged.connect(self._on_change); self._tgt.setFixedWidth(72); lay.addWidget(self._tgt)
        rm=QPushButton("✕"); rm.setFixedSize(22,22)
        rm.setStyleSheet("QPushButton{background:#3a1a1a;color:#f88;border:none;border-radius:3px;font-size:10px;}QPushButton:hover{background:#7f2020;}")
        rm.clicked.connect(lambda: self.removed.emit(self)); lay.addWidget(rm)
    def _on_change(self):
        self.cfg.enabled=self._en.isChecked(); self.cfg.tab_idx=self._tab.value()-1
        self.cfg.position=self.POS_LABELS[self._pos.currentIndex()]
        self.cfg.target=self.TGT_LABELS[self._tgt.currentIndex()]; self.changed.emit()
    def update_tab_max(self,n): self._tab.setMaximum(max(n,1))


# =============================================================================
#  유틸
# =============================================================================
def _folder_btn(label: str, path_fn) -> QPushButton:
    btn=QPushButton(label)
    btn.setStyleSheet("QPushButton{background:#1a2a1a;color:#8fa;border:1px solid #2a5a2a;border-radius:4px;padding:3px 8px;font-size:10px;}QPushButton:hover{background:#223a22;}")
    btn.clicked.connect(lambda: open_folder(path_fn())); return btn


# =============================================================================
#  MainWindow
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen & Camera Recorder  v8.0")
        self.resize(1440, 920); self.setStyleSheet(self._dark_style())

        self.signals  = Signals()
        self.engine   = RecorderEngine(self.signals)
        self.db       = SettingsDB()
        self._cam_win = CameraWindow(self.engine, self.signals, self)

        self._sections: dict[str, CollapsibleSection] = {}
        self._overlay_rows: list[MemoOverlayRow] = []
        self._memo_editors: list[TimestampMemoEdit] = []

        self._led_state = False
        self._led_timer = QTimer(self); self._led_timer.timeout.connect(self._blink_led)

        self._build_ui()
        self._connect_signals()
        self._load_settings()   # ★ DB에서 세팅 복원

        QTimer(self, timeout=self._refresh_ui,        interval=500  ).start()
        QTimer(self, timeout=self._update_fps,         interval=2000 ).start()
        QTimer(self, timeout=self._check_segment,      interval=5000 ).start()
        QTimer(self, timeout=self._tick_schedule,      interval=1000 ).start()
        QTimer(self, timeout=self._pump_preview,       interval=33   ).start()
        QTimer(self, timeout=self._auto_save_settings, interval=10000).start()
        QTimer(self, timeout=self._poll_manual_state,  interval=200  ).start()

        self._setup_hotkeys()
        self.engine.start()

    # =========================================================================
    #  UI 빌드
    # =========================================================================
    def _build_ui(self):
        central=QWidget(); self.setCentralWidget(central)
        root=QHBoxLayout(central); root.setSpacing(0); root.setContentsMargins(8,8,8,8)

        from PyQt5.QtWidgets import QSplitter

        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet(
            "QSplitter::handle{background:#2a2a4a;width:5px;}"
            "QSplitter::handle:hover{background:#4a6aaa;}")

        # ── 왼쪽: 스크린 미리보기 ──────────────────────────────────────────
        left_w = QWidget()
        left = QVBoxLayout(left_w); left.setSpacing(6); left.setContentsMargins(0,0,4,0)
        scr_hdr=QHBoxLayout()
        t=QLabel("🖥  Screen Preview"); t.setStyleSheet("color:#9ab;font-weight:bold;font-size:13px;")
        scr_hdr.addWidget(t); scr_hdr.addStretch()
        self._scr_fps_badge=QLabel("FPS: —"); self._scr_fps_badge.setStyleSheet("color:#888;font-size:11px;"); scr_hdr.addWidget(self._scr_fps_badge)
        self._scr_toggle=ThreadToggleBtn("▶ Thread ON","⏸ Thread OFF"); self._scr_toggle.toggled.connect(self._on_scr_toggle); scr_hdr.addWidget(self._scr_toggle)
        self._cam_win_btn=QPushButton("📷 Camera Window"); self._cam_win_btn.setCheckable(True)
        self._cam_win_btn.setStyleSheet("QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;border-radius:13px;font-size:10px;padding:2px 10px;}QPushButton:checked{background:#1a4060;}")
        self._cam_win_btn.toggled.connect(self._on_cam_win_toggle); scr_hdr.addWidget(self._cam_win_btn)
        scr_frame=QFrame(); scr_frame.setStyleSheet("QFrame{border:1px solid #334;border-radius:6px;background:#0d0d1e;}")
        sf=QVBoxLayout(scr_frame); sf.setContentsMargins(4,4,4,4); sf.setSpacing(3)
        sf.addLayout(scr_hdr)
        self._scr_lbl=PreviewLabel("screen",self.engine); sf.addWidget(self._scr_lbl,1)
        hint=QLabel("Left-drag: add ROI  |  Right-click: remove ROI"); hint.setStyleSheet("color:#555;font-size:10px;"); hint.setAlignment(Qt.AlignCenter); sf.addWidget(hint)
        left.addWidget(scr_frame,1)
        self._status_lbl=QLabel("Ready"); self._status_lbl.setStyleSheet("color:#888;font-size:11px;padding:2px 4px;border-top:1px solid #334;"); left.addWidget(self._status_lbl)

        # ── 오른쪽: 컨트롤 패널 (유동 너비) ───────────────────────────────
        rw=QWidget(); rw.setMinimumWidth(320)
        rv=QVBoxLayout(rw); rv.setContentsMargins(4,0,0,0); rv.setSpacing(0)
        pt=QLabel("⚙  Control Panel"); pt.setStyleSheet("color:#ccc;font-size:13px;font-weight:bold;padding:8px 10px;background:#1a1a3a;border-bottom:1px solid #334;"); rv.addWidget(pt)
        self._feat_bar=FeatureBar()
        self._feat_bar.toggled.connect(self._on_feature_toggle)
        self._feat_bar.order_changed.connect(self._on_feature_order)
        self._feat_bar.scroll_to_key.connect(self._on_scroll_to_section)  # ★ v9
        rv.addWidget(self._feat_bar)

        self._panel_scroll=QScrollArea()  # ★ v9: 인스턴스 변수로 저장
        self._panel_scroll.setWidgetResizable(True)
        self._panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._panel_scroll.setStyleSheet(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{background:#0d0d1e;width:8px;border-radius:4px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:4px;min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._panel_w=QWidget(); self._panel_w.setStyleSheet("background:#12122a;")
        self._panel_l=QVBoxLayout(self._panel_w); self._panel_l.setContentsMargins(8,6,8,14); self._panel_l.setSpacing(10)

        def add_sec(key,title,color,build_fn):
            sec=CollapsibleSection(title,color); sec.add_widget(build_fn())
            self._sections[key]=sec; self._panel_l.addWidget(sec)

        add_sec("recording",  "⏺  Recording",         "#27ae60",self._build_recording_grp)
        add_sec("manual_clip","🎬  수동 녹화",          "#e67e22",self._build_manual_grp)
        add_sec("schedule",   "⏰  Schedule",           "#8e44ad",self._build_schedule_grp)
        add_sec("blackout",   "⚡  Blackout Detection", "#e74c3c",self._build_blackout_grp)
        add_sec("autoclick",  "🖱  Auto-Click",         "#2980b9",self._build_autoclick_grp)
        add_sec("macro",      "🎯  Click Macro",        "#16a085",self._build_macro_grp)
        add_sec("memo",       "📝  메모장",             "#f39c12",self._build_memo_grp)
        add_sec("log",        "📋  Log",                "#7f8c8d",self._build_log_grp)
        add_sec("reset",      "🔄  설정 초기화",        "#636e72",self._build_reset_grp)
        self._panel_l.addStretch()
        self._panel_scroll.setWidget(self._panel_w)
        rv.addWidget(self._panel_scroll,1)

        splitter.addWidget(left_w)
        splitter.addWidget(rw)
        splitter.setStretchFactor(0, 1)   # 왼쪽(미리보기): 늘어남
        splitter.setStretchFactor(1, 0)   # 오른쪽(패널): 고정비율
        splitter.setSizes([960, 440])     # 초기 분할 비율

        root.addWidget(splitter)

    # =========================================================================
    #  섹션 빌더
    # =========================================================================
    def _build_recording_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(6)
        st=QGroupBox("Status"); g=QGridLayout(st); g.setSpacing(6)
        self._rec_status_lbl=QLabel("● STOPPED"); self._rec_status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;"); g.addWidget(self._rec_status_lbl,0,0,1,2)
        self._rec_timer_lbl=QLabel("00:00:00"); self._rec_timer_lbl.setStyleSheet("font-size:26px;font-weight:bold;color:#2ecc71;font-family:monospace;"); g.addWidget(self._rec_timer_lbl,1,0,1,2,Qt.AlignCenter)
        g.addWidget(QLabel("Screen FPS:"),2,0); self._scr_fps_lbl=QLabel("—"); g.addWidget(self._scr_fps_lbl,2,1)
        g.addWidget(QLabel("Camera FPS:"),3,0); self._cam_fps_lbl=QLabel("—"); g.addWidget(self._cam_fps_lbl,3,1)
        g.addWidget(_folder_btn("📂 녹화 폴더",lambda: self.engine.output_dir or BASE_DIR),4,0,1,2)
        v.addWidget(st)
        ctrl=QGroupBox("Controls"); cv=QVBoxLayout(ctrl); cv.setSpacing(8)
        self._btn_start=QPushButton("⏺  Start Recording  [Ctrl+Alt+W]")
        self._btn_start.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#2ecc71;}"
            "QPushButton:pressed{background:#1a8a46;border:2px solid #0a6a30;"
            "padding:9px 7px 7px 9px;}"
            "QPushButton:disabled{background:#1a3a28;color:#4a7a5a;border:none;}")
        self._btn_start.clicked.connect(self._on_start_rec)
        self._btn_stop=QPushButton("⏹  Stop Recording  [Ctrl+Alt+E]")
        self._btn_stop.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#e74c3c;}"
            "QPushButton:pressed{background:#8a1a1a;border:2px solid #6a0a0a;"
            "padding:9px 7px 7px 9px;}"
            "QPushButton:disabled{background:#3a1a1a;color:#7a4a4a;border:none;}")
        self._btn_stop.clicked.connect(self._on_stop_rec); self._btn_stop.setEnabled(False)
        cv.addWidget(self._btn_start); cv.addWidget(self._btn_stop); v.addWidget(ctrl)
        sr=QGroupBox("Screen Recording"); sv=QVBoxLayout(sr); sv.setSpacing(6)
        self._scr_rec_chk=QCheckBox("Enable screen recording  [Ctrl+Alt+D]"); self._scr_rec_chk.setChecked(True); self._scr_rec_chk.toggled.connect(self._on_scr_rec_toggle); sv.addWidget(self._scr_rec_chk); v.addWidget(sr)
        fps_g=QGroupBox("FPS & 배속"); fg=QGridLayout(fps_g); fg.setSpacing(8)
        fg.addWidget(QLabel("Target Screen FPS:"),0,0)
        self._scr_fps_spin=QDoubleSpinBox(); self._scr_fps_spin.setRange(1,120); self._scr_fps_spin.setValue(30.0); self._scr_fps_spin.setSingleStep(1.0); self._scr_fps_spin.valueChanged.connect(lambda v: setattr(self.engine,'actual_screen_fps',v)); fg.addWidget(self._scr_fps_spin,0,1)
        fg.addWidget(QLabel("Camera FPS (감지):"),1,0); self._cam_fps_det_lbl=QLabel("—"); fg.addWidget(self._cam_fps_det_lbl,1,1)
        sep=QFrame(); sep.setFrameShape(QFrame.HLine); fg.addWidget(sep,2,0,1,2)
        fg.addWidget(QLabel("저장 배속:"),3,0)
        self._speed_spin=QDoubleSpinBox(); self._speed_spin.setRange(0.1,10.0); self._speed_spin.setValue(1.0); self._speed_spin.setSingleStep(0.25); self._speed_spin.setDecimals(2); self._speed_spin.setStyleSheet("QDoubleSpinBox{background:#1a1a2a;color:#f0c040;border:1px solid #5a5a20;border-radius:4px;padding:3px;font-size:13px;font-weight:bold;}"); self._speed_spin.valueChanged.connect(self._on_speed_changed); fg.addWidget(self._speed_spin,3,1)
        pr=QHBoxLayout(); pr.setSpacing(4)
        for lbl,val in [("0.5×",.5),("1×",1.),("1.5×",1.5),("2×",2.),("4×",4.)]:
            b=QPushButton(lbl); b.setFixedHeight(24); b.setStyleSheet("QPushButton{background:#2a2a1a;color:#f0c040;border:1px solid #4a4a20;border-radius:3px;font-size:10px;padding:0 4px;}QPushButton:hover{background:#3a3a28;}"); b.clicked.connect(lambda _,v=val: self._speed_spin.setValue(v)); pr.addWidget(b)
        fg.addLayout(pr,4,0,1,2)
        self._speed_info=QLabel("  정배속"); self._speed_info.setStyleSheet("color:#888;font-size:10px;"); fg.addWidget(self._speed_info,5,0,1,2)
        self._speed_lock=QLabel("🔒 녹화 중에는 배속 변경 불가"); self._speed_lock.setStyleSheet("color:#e74c3c;font-size:10px;font-weight:bold;"); self._speed_lock.setVisible(False); fg.addWidget(self._speed_lock,6,0,1,2)
        v.addWidget(fps_g); return w

    def _build_manual_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        info=QLabel("버튼을 누르면 현재 시점 기준 전/후 N초를 버퍼에서 추출해 클립을 저장합니다.\n버퍼에 데이터가 없으면 있는 만큼만 저장됩니다.")
        info.setWordWrap(True); info.setStyleSheet("color:#778;font-size:10px;padding:2px;"); v.addWidget(info)
        src_g=QGroupBox("📹  저장 소스"); sg=QHBoxLayout(src_g); sg.setSpacing(12)
        self._m_scr_chk=QCheckBox("🖥 Display"); self._m_scr_chk.setChecked(True)
        self._m_cam_chk=QCheckBox("📷 Camera");  self._m_cam_chk.setChecked(True)
        self._m_scr_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;"); self._m_cam_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#f0c040;")
        self._m_scr_chk.toggled.connect(self._on_manual_src_changed); self._m_cam_chk.toggled.connect(self._on_manual_src_changed)
        sg.addWidget(self._m_scr_chk); sg.addWidget(self._m_cam_chk); sg.addStretch(); v.addWidget(src_g)
        tg=QGroupBox("⏱  전/후 시간 (최대 30초)"); tgl=QGridLayout(tg); tgl.setSpacing(8)
        def make_row(label,color,set_attr):
            lbl=QLabel(label); lbl.setStyleSheet(f"font-weight:bold;color:{color};")
            spin=QDoubleSpinBox(); spin.setRange(0,30); spin.setValue(10.0); spin.setSingleStep(1.0); spin.setDecimals(1); spin.setMinimumHeight(28)
            spin.setStyleSheet(f"QDoubleSpinBox{{background:#1a1a3a;color:{color};border:1px solid #3a3a5a;border-radius:4px;padding:3px;font-size:13px;font-weight:bold;}}")
            slider=QSlider(Qt.Horizontal); slider.setRange(0,300); slider.setValue(100)
            slider.setStyleSheet(
                f"QSlider::groove:horizontal{{background:#1a2a3a;height:6px;border-radius:3px;}}"
                f"QSlider::handle:horizontal{{background:{color};width:16px;height:16px;"
                "margin-top:-5px;margin-bottom:-5px;border-radius:8px;}}"
                "QSlider::sub-page:horizontal{background:#3a4a6a;border-radius:3px;}")
            spin.valueChanged.connect(lambda val,sl=slider,a=set_attr: (sl.blockSignals(True),sl.setValue(int(val*10)),sl.blockSignals(False),setattr(self.engine,a,val)))
            slider.valueChanged.connect(lambda val,sp=spin,a=set_attr: (sp.blockSignals(True),sp.setValue(val/10),sp.blockSignals(False),setattr(self.engine,a,val/10)))
            return lbl,spin,slider
        pre_lbl,self._m_pre_spin,self._m_pre_sl=make_row("🔵 전 (초):","#7bc8e0","manual_pre_sec")
        post_lbl,self._m_post_spin,self._m_post_sl=make_row("🟠 후 (초):","#f0a040","manual_post_sec")
        for row,(lbl,sp,sl) in enumerate([(pre_lbl,self._m_pre_spin,self._m_pre_sl),(post_lbl,self._m_post_spin,self._m_post_sl)]):
            tgl.addWidget(lbl,row,0); tgl.addWidget(sp,row,1); tgl.addWidget(sl,row,2)
        tgl.setColumnStretch(2,1); v.addWidget(tg)
        btn_row=QHBoxLayout(); btn_row.setSpacing(8)
        self._led_lbl=QLabel("●"); self._led_lbl.setFixedWidth(22); self._led_lbl.setAlignment(Qt.AlignCenter); self._led_lbl.setStyleSheet("font-size:22px;color:#333;"); btn_row.addWidget(self._led_lbl)
        self._manual_btn=QPushButton("🎬  지금 클립 저장  [Ctrl+Alt+M]"); self._manual_btn.setMinimumHeight(42); self._manual_btn.setStyleSheet(self._manual_btn_style(True)); self._manual_btn.clicked.connect(self._on_manual_clip); btn_row.addWidget(self._manual_btn,1); v.addLayout(btn_row)
        self._manual_status=QLabel("대기 중"); self._manual_status.setStyleSheet("color:#888;font-size:11px;font-family:monospace;"); v.addWidget(self._manual_status)
        v.addWidget(_folder_btn("📂 수동클립 폴더",lambda: self.engine.manual_dir)); return w

    @staticmethod
    def _manual_btn_style(active: bool) -> str:
        if active:
            return ("QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #7d3c98,stop:1 #e67e22);color:white;font-size:13px;font-weight:bold;border:none;border-radius:6px;padding:6px;}QPushButton:hover{background:#9b59b6;}QPushButton:pressed{background:#6c3483;}")
        else:
            return ("QPushButton{background:#2a2a3a;color:#666;font-size:13px;font-weight:bold;border:1px solid #444;border-radius:6px;padding:6px;}")

    def _build_schedule_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        inp=QGroupBox("새 예약 추가"); ig=QVBoxLayout(inp); ig.setSpacing(8)
        def dt_edit(color,border):
            dte=QDateTimeEdit(); dte.setDisplayFormat("yyyy-MM-dd  HH:mm:ss"); dte.setCalendarPopup(True); dte.setMinimumHeight(30)
            dte.setStyleSheet(f"QDateTimeEdit{{background:#1a1a3a;color:{color};border:1px solid {border};border-radius:4px;padding:4px 6px;font-size:12px;font-family:monospace;}}QDateTimeEdit::drop-down{{border:none;}}QDateTimeEdit::down-arrow{{image:none;width:0;}}"); return dte
        ig.addWidget(QLabel("🟢  녹화 시작 시각"))
        rs=QHBoxLayout(); rs.setSpacing(6)
        self._s_start_chk=QCheckBox("사용"); self._s_start_chk.setChecked(True)
        self._s_start_dt=dt_edit("#2ecc71","#2a6a3a"); self._s_start_dt.setDateTime(QDateTime.currentDateTime().addSecs(60))
        b=QPushButton("지금"); b.setFixedSize(46,30); b.clicked.connect(lambda: self._s_start_dt.setDateTime(QDateTime.currentDateTime()))
        rs.addWidget(self._s_start_chk); rs.addWidget(self._s_start_dt,1); rs.addWidget(b); ig.addLayout(rs)
        ig.addWidget(QLabel("🔴  녹화 종료 시각"))
        re=QHBoxLayout(); re.setSpacing(6)
        self._s_stop_chk=QCheckBox("사용"); self._s_stop_chk.setChecked(True)
        self._s_stop_dt=dt_edit("#e74c3c","#6a2a2a"); self._s_stop_dt.setDateTime(QDateTime.currentDateTime().addSecs(3660))
        b2=QPushButton("지금"); b2.setFixedSize(46,30); b2.clicked.connect(lambda: self._s_stop_dt.setDateTime(QDateTime.currentDateTime()))
        re.addWidget(self._s_stop_chk); re.addWidget(self._s_stop_dt,1); re.addWidget(b2); ig.addLayout(re)
        ba=QPushButton("＋  예약 추가"); ba.setMinimumHeight(32); ba.setStyleSheet("background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;border-radius:4px;font-weight:bold;font-size:12px;"); ba.clicked.connect(self._on_schedule_add); ig.addWidget(ba); v.addWidget(inp)
        lst=QGroupBox("예약 목록"); ll=QVBoxLayout(lst); ll.setSpacing(4)
        self._sched_tbl=QTableWidget(0,4); self._sched_tbl.setHorizontalHeaderLabels(["#","시작","종료","상태"])
        self._sched_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self._sched_tbl.setSelectionBehavior(QAbstractItemView.SelectRows); self._sched_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers); self._sched_tbl.setFixedHeight(150)
        self._sched_tbl.setStyleSheet("QTableWidget{background:#0d0d1e;color:#ccc;font-size:11px;border:1px solid #334;gridline-color:#223;}QHeaderView::section{background:#1a1a3a;color:#9ab;font-size:11px;border:none;padding:4px;}")
        ll.addWidget(self._sched_tbl)
        br=QHBoxLayout(); bd=QPushButton("선택 삭제"); bd.setFixedHeight(26); bd.clicked.connect(self._on_sched_del)
        bc=QPushButton("전체 삭제"); bc.setFixedHeight(26); bc.clicked.connect(self._on_sched_clear)
        br.addWidget(bd); br.addWidget(bc); ll.addLayout(br); v.addWidget(lst)
        cd=QGroupBox("카운트다운"); cl=QVBoxLayout(cd)
        self._sched_cd=QLabel("예약 없음"); self._sched_cd.setStyleSheet("color:#f0c040;font-family:monospace;font-size:12px;"); cl.addWidget(self._sched_cd); v.addWidget(cd); return w

    def _build_blackout_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        self._bo_rec_chk=QCheckBox("Enable Blackout Clip Recording"); self._bo_rec_chk.setChecked(True); self._bo_rec_chk.toggled.connect(lambda c: setattr(self.engine,'blackout_recording_enabled',c)); v.addWidget(self._bo_rec_chk)
        thr=QGroupBox("Detection Threshold"); tl=QGridLayout(thr)
        tl.addWidget(QLabel("Brightness drop:"),0,0)
        self._thr_spin=QDoubleSpinBox(); self._thr_spin.setRange(5,200); self._thr_spin.setValue(30.0); self._thr_spin.setSuffix("  (0–255)"); self._thr_spin.valueChanged.connect(lambda v: setattr(self.engine,'brightness_threshold',v)); tl.addWidget(self._thr_spin,0,1)
        tl.addWidget(QLabel("Cooldown (s):"),1,0)
        self._cd_spin=QDoubleSpinBox(); self._cd_spin.setRange(0.5,60); self._cd_spin.setValue(5.0); self._cd_spin.valueChanged.connect(lambda v: setattr(self.engine,'blackout_cooldown',v)); tl.addWidget(self._cd_spin,1,1); v.addWidget(thr)
        cnt=QGroupBox("Counts"); cl=QGridLayout(cnt)
        cl.addWidget(QLabel("Screen:"),0,0); self._scr_bo=QLabel("0"); self._scr_bo.setStyleSheet("font-weight:bold;color:#e74c3c;"); cl.addWidget(self._scr_bo,0,1)
        cl.addWidget(QLabel("Camera:"),1,0); self._cam_bo=QLabel("0"); self._cam_bo.setStyleSheet("font-weight:bold;color:#e74c3c;"); cl.addWidget(self._cam_bo,1,1)
        cl.addWidget(_folder_btn("📂 Blackout 폴더",lambda: self.engine.blackout_dir),2,0,1,2); v.addWidget(cnt)
        roi_g=QGroupBox("ROI Brightness"); rl=QVBoxLayout(roi_g)
        self._roi_txt=QTextEdit(); self._roi_txt.setReadOnly(True); self._roi_txt.setFixedHeight(110); self._roi_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;"); rl.addWidget(self._roi_txt); v.addWidget(roi_g)
        ev_g=QGroupBox("Recent Events"); el=QVBoxLayout(ev_g)
        self._ev_txt=QTextEdit(); self._ev_txt.setReadOnly(True); self._ev_txt.setFixedHeight(90); self._ev_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;"); el.addWidget(self._ev_txt); v.addWidget(ev_g); return w

    def _build_autoclick_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        ig=QGroupBox("Click Interval"); il=QGridLayout(ig)
        il.addWidget(QLabel("Interval (s):"),0,0)
        self._ci_spin=QDoubleSpinBox(); self._ci_spin.setRange(0.1,3600); self._ci_spin.setValue(1.0); self._ci_spin.setSingleStep(0.1); self._ci_spin.valueChanged.connect(lambda v: setattr(self.engine,'auto_click_interval',v)); il.addWidget(self._ci_spin,0,1)
        pr=QHBoxLayout()
        for lbl,val in [("0.1s",.1),("0.5s",.5),("1s",1.),("5s",5.),("10s",10.)]:
            b=QPushButton(lbl); b.setFixedWidth(42); b.setFixedHeight(22); b.clicked.connect(lambda _,v=val: self._ci_spin.setValue(v)); pr.addWidget(b)
        il.addLayout(pr,1,0,1,2); v.addWidget(ig)
        cg=QGroupBox("Click Counter"); cl=QGridLayout(cg)
        self._click_lcd=QLCDNumber(8); self._click_lcd.setSegmentStyle(QLCDNumber.Flat); self._click_lcd.setFixedHeight(44); cl.addWidget(self._click_lcd,0,0,1,2)
        br=QPushButton("Reset"); br.clicked.connect(self.engine.reset_click_count); cl.addWidget(br,1,0,1,2); v.addWidget(cg)
        ctrl=QGroupBox("Control"); ctl=QVBoxLayout(ctrl); ctl.setSpacing(6)
        self._btn_ac_start=QPushButton("▶  Start Auto-Click  [Ctrl+Alt+A]")
        self._btn_ac_start.setStyleSheet(
            "QPushButton{background:#2980b9;color:white;font-size:12px;padding:7px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#3498db;}"
            "QPushButton:pressed{background:#1a5a8a;border:2px solid #0a3a6a;"
            "padding:8px 6px 6px 8px;}"
            "QPushButton:disabled{background:#1a2a3a;color:#4a6a8a;border:none;}")
        self._btn_ac_start.clicked.connect(self._on_ac_start)
        self._btn_ac_stop=QPushButton("■  Stop Auto-Click  [Ctrl+Alt+S]")
        self._btn_ac_stop.setStyleSheet(
            "QPushButton{background:#5a6a7a;color:white;font-size:12px;padding:7px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:pressed{background:#3a4a5a;border:2px solid #2a3a4a;"
            "padding:8px 6px 6px 8px;}"
            "QPushButton:disabled{background:#2a2a2a;color:#555;border:none;}")
        self._btn_ac_stop.clicked.connect(self._on_ac_stop); self._btn_ac_stop.setEnabled(False)
        self._ac_status=QLabel("● STOPPED"); self._ac_status.setStyleSheet("color:#e74c3c;font-weight:bold;")
        ctl.addWidget(self._btn_ac_start); ctl.addWidget(self._btn_ac_stop); ctl.addWidget(self._ac_status); v.addWidget(ctrl); return w

    def _build_macro_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        info=QLabel("기록 시작 후 화면을 클릭하면 좌표+딜레이가 자동 기록됩니다."); info.setWordWrap(True); info.setStyleSheet("color:#778;font-size:10px;"); v.addWidget(info)
        rg=QGroupBox("📍  좌표 기록"); rl=QVBoxLayout(rg); rl.setSpacing(6)
        rb=QHBoxLayout()
        self._mac_rec_btn=QPushButton("⏺  기록 시작"); self._mac_rec_btn.setCheckable(True); self._mac_rec_btn.setFixedHeight(30)
        self._mac_rec_btn.setStyleSheet(
            "QPushButton{background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:5px;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#226a3a;border-color:#3aaa6a;}"
            "QPushButton:pressed{background:#0a2a18;border:2px solid #1a6a40;"
            "padding:4px 7px 2px 9px;}"
            "QPushButton:checked{background:#c0392b;color:#fff;border:2px solid #e74c3c;}"
            "QPushButton:checked:hover{background:#e74c3c;}"
            "QPushButton:checked:pressed{background:#8a1a1a;border-color:#6a0a0a;}")
        self._mac_rec_btn.toggled.connect(self._on_mac_rec_toggle)
        self._mac_rec_st=QLabel("● 대기"); self._mac_rec_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb.addWidget(self._mac_rec_btn,1); rb.addWidget(self._mac_rec_st); rl.addLayout(rb)
        self._mac_pos_lbl=QLabel("마지막 클릭: —"); self._mac_pos_lbl.setStyleSheet("color:#f0c040;font-size:11px;font-family:monospace;"); rl.addWidget(self._mac_pos_lbl); v.addWidget(rg)
        tg=QGroupBox("📋  클릭 스텝"); tv=QVBoxLayout(tg); tv.setSpacing(4)
        self._mac_tbl=QTableWidget(0,4); self._mac_tbl.setHorizontalHeaderLabels(["#","X","Y","딜레이(s)"])
        for c,m in [(0,QHeaderView.Fixed),(1,QHeaderView.Stretch),(2,QHeaderView.Stretch),(3,QHeaderView.Stretch)]: self._mac_tbl.horizontalHeader().setSectionResizeMode(c,m)
        self._mac_tbl.setColumnWidth(0,32); self._mac_tbl.setFixedHeight(160)
        self._mac_tbl.setStyleSheet("QTableWidget{background:#0a0a18;color:#ccc;font-size:11px;border:1px solid #1a2a3a;gridline-color:#1a2030;}QHeaderView::section{background:#0f1a2a;color:#7ab4d4;font-size:11px;border:none;padding:4px;}QTableWidget::item:selected{background:#1a3a5a;}")
        self._mac_tbl.itemChanged.connect(self._on_mac_item_changed); tv.addWidget(self._mac_tbl)
        tb=QHBoxLayout(); tb.setSpacing(4)
        for lbl,fn in [("↑",self._on_mac_up),("↓",self._on_mac_dn)]: b=QPushButton(lbl); b.setFixedSize(28,24); b.clicked.connect(fn); tb.addWidget(b)
        tb.addStretch()
        for lbl,fn in [("선택 삭제",self._on_mac_del),("전체 삭제",self._on_mac_clear)]: b=QPushButton(lbl); b.setFixedHeight(24); b.clicked.connect(fn); tb.addWidget(b)
        tv.addLayout(tb)
        bkr=QHBoxLayout(); bkr.setSpacing(6); bkr.addWidget(QLabel("전체 딜레이:"))
        self._mac_bulk=QDoubleSpinBox(); self._mac_bulk.setRange(0.05,60); self._mac_bulk.setValue(0.5); self._mac_bulk.setSingleStep(0.1); self._mac_bulk.setDecimals(2); self._mac_bulk.setFixedWidth(80)
        bkb=QPushButton("일괄 적용"); bkb.setFixedHeight(24); bkb.clicked.connect(self._on_mac_bulk)
        bkr.addWidget(self._mac_bulk); bkr.addWidget(QLabel("초")); bkr.addWidget(bkb); bkr.addStretch(); tv.addLayout(bkr); v.addWidget(tg)
        run=QGroupBox("▶  실행"); rn=QGridLayout(run); rn.setSpacing(6)
        rn.addWidget(QLabel("반복:"),0,0)
        self._mac_rep=QDoubleSpinBox(); self._mac_rep.setRange(0,9999); self._mac_rep.setDecimals(0); self._mac_rep.setValue(1); self._mac_rep.setSpecialValueText("∞"); self._mac_rep.valueChanged.connect(lambda v: setattr(self.engine,'macro_repeat',int(v))); rn.addWidget(self._mac_rep,0,1)
        rn.addWidget(QLabel("루프 간격(s):"),1,0)
        self._mac_gap=QDoubleSpinBox(); self._mac_gap.setRange(0,60); self._mac_gap.setValue(1.0); self._mac_gap.valueChanged.connect(lambda v: setattr(self.engine,'macro_loop_gap',v)); rn.addWidget(self._mac_gap,1,1)
        rb2=QHBoxLayout(); rb2.setSpacing(6)
        self._mac_run_btn=QPushButton("▶  실행"); self._mac_run_btn.setFixedHeight(32)
        self._mac_run_btn.setStyleSheet(
            "QPushButton{background:#2980b9;color:#fff;border:none;border-radius:5px;"
            "font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#3498db;}"
            "QPushButton:pressed{background:#1a5a8a;border:2px solid #0a3a6a;}"
            "QPushButton:disabled{background:#1a3a5a;color:#555;}")
        self._mac_run_btn.clicked.connect(self._on_mac_run)
        self._mac_stop_btn=QPushButton("■  중단"); self._mac_stop_btn.setFixedHeight(32)
        self._mac_stop_btn.setStyleSheet(
            "QPushButton{background:#5a6a7a;color:#fff;border:none;border-radius:5px;font-size:12px;}"
            "QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:pressed{background:#3a4a5a;border:2px solid #2a3a4a;}"
            "QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self._mac_stop_btn.setEnabled(False); self._mac_stop_btn.clicked.connect(self._on_mac_stop)
        self._mac_run_st=QLabel("● 대기"); self._mac_run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb2.addWidget(self._mac_run_btn,1); rb2.addWidget(self._mac_stop_btn,1); rn.addLayout(rb2,2,0,1,2); rn.addWidget(self._mac_run_st,3,0,1,2); v.addWidget(run); return w

    def _build_memo_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(6)
        ts_g=QGroupBox("줄 클릭 시 타임스탬프 삽입"); tsg=QHBoxLayout(ts_g); tsg.setSpacing(8)
        self._memo_ts_chk=QCheckBox("활성화"); self._memo_ts_chk.setChecked(True); self._memo_ts_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;"); self._memo_ts_chk.toggled.connect(self._on_ts_toggled)
        tsg.addWidget(self._memo_ts_chk); tsg.addWidget(QLabel("← 클릭한 줄 앞에 [날짜 시:분:초] 삽입")); tsg.addStretch(); v.addWidget(ts_g)
        tab_ctrl=QHBoxLayout(); tab_ctrl.setSpacing(6)
        for lbl,fn,style in [("＋ 탭",self._on_memo_add_tab,"background:#1a3a1a;color:#8fa;border:1px solid #2a6a2a;border-radius:4px;font-size:11px;"),("－ 탭",self._on_memo_del_tab,"background:#3a1a1a;color:#f88;border:1px solid #6a2a2a;border-radius:4px;font-size:11px;"),("현재 탭 지우기",self._on_memo_clear_cur,"background:#1a1a3a;color:#aaa;border:1px solid #334;border-radius:4px;font-size:11px;")]:
            b=QPushButton(lbl); b.setFixedHeight(26); b.setStyleSheet(style); b.clicked.connect(fn); tab_ctrl.addWidget(b)
        tab_ctrl.addStretch(); v.addLayout(tab_ctrl)
        self._memo_tabs=QTabWidget()
        self._memo_tabs.setStyleSheet("QTabWidget::pane{border:1px solid #2a2a4a;background:#0d0d1e;}QTabBar::tab{background:#1a1a3a;color:#888;border:1px solid #334;border-bottom:none;border-radius:4px 4px 0 0;padding:4px 10px;font-size:11px;min-width:60px;}QTabBar::tab:selected{background:#2a2a5a;color:#dde;border-color:#446;}QTabBar::tab:hover{background:#22224a;color:#bbd;}")
        self._memo_tabs.currentChanged.connect(self._on_memo_tab_changed)
        self._add_memo_tab("메모 1"); v.addWidget(self._memo_tabs)
        ov_g=QGroupBox("🖼  오버레이 설정  (위치 / 대상 / 탭)"); ovl=QVBoxLayout(ov_g); ovl.setSpacing(4)
        self._overlay_container=QWidget(); self._overlay_layout=QVBoxLayout(self._overlay_container)
        self._overlay_layout.setContentsMargins(0,0,0,0); self._overlay_layout.setSpacing(3)
        ovl.addWidget(self._overlay_container)
        add_ov_btn=QPushButton("＋ 오버레이 추가"); add_ov_btn.setFixedHeight(26)
        add_ov_btn.setStyleSheet("background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;border-radius:4px;font-size:11px;"); add_ov_btn.clicked.connect(self._on_add_overlay); ovl.addWidget(add_ov_btn); v.addWidget(ov_g)
        self._rebuild_overlay_rows(); return w

    def _build_log_grp(self) -> QWidget:
        g=QGroupBox("System Log"); v=QVBoxLayout(g); v.setSpacing(4)
        self._log_txt=QTextEdit(); self._log_txt.setReadOnly(True); self._log_txt.setFixedHeight(180); self._log_txt.setStyleSheet("font-family:monospace;font-size:10px;background:#080810;color:#aaa;")
        bc=QPushButton("Clear"); bc.setFixedHeight(24); bc.clicked.connect(self._log_txt.clear)
        v.addWidget(self._log_txt); v.addWidget(bc); return g

    # ── ★ 설정 초기화 섹션 ────────────────────────────────────────────────────
    def _build_reset_grp(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)

        # 안내 텍스트
        info = QLabel(
            "설정을 초기화하면 아래 항목들이 기본값으로 되돌아갑니다.\n"
            "• FPS / 배속 / 블랙아웃 임계값 / 쿨다운\n"
            "• 오토클릭 인터벌 / 매크로 반복 횟수\n"
            "• 수동녹화 전/후 시간\n"
            "• 메모 탭 전체 내용\n"
            "• 섹션 접힘 상태\n\n"
            "녹화 중에는 초기화가 적용되지 않습니다."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "color:#999; font-size:11px; padding:4px 2px; "
            "border:1px solid #2a2a3a; border-radius:4px; background:#0d0d1e;")
        v.addWidget(info)

        # 항목별 체크박스
        chk_grp = QGroupBox("초기화 항목 선택")
        cg = QVBoxLayout(chk_grp); cg.setSpacing(5)

        self._rst_chk_settings = QCheckBox("모든 설정값 (FPS, 배속, 임계값 등)")
        self._rst_chk_settings.setChecked(True)
        self._rst_chk_memo     = QCheckBox("메모 탭 전체 내용")
        self._rst_chk_memo.setChecked(False)
        self._rst_chk_sections = QCheckBox("섹션 접힘/펼침 상태")
        self._rst_chk_sections.setChecked(True)
        self._rst_chk_db       = QCheckBox("DB 파일 완전 삭제 후 재생성")
        self._rst_chk_db.setChecked(False)
        self._rst_chk_db.setStyleSheet("color:#e74c3c; font-weight:bold;")

        for chk in [self._rst_chk_settings, self._rst_chk_memo,
                    self._rst_chk_sections, self._rst_chk_db]:
            chk.setStyleSheet(chk.styleSheet() +
                              " QCheckBox{font-size:11px;}")
            cg.addWidget(chk)
        v.addWidget(chk_grp)

        # 확인 입력 필드
        confirm_grp = QGroupBox("확인 입력")
        cfl = QVBoxLayout(confirm_grp); cfl.setSpacing(6)
        confirm_hint = QLabel("초기화하려면 아래에  RESET  을 입력하세요")
        confirm_hint.setStyleSheet("color:#f0a040; font-size:11px;")
        cfl.addWidget(confirm_hint)
        from PyQt5.QtWidgets import QLineEdit
        self._rst_confirm_edit = QLineEdit()
        self._rst_confirm_edit.setPlaceholderText("RESET 입력 후 버튼 클릭")
        self._rst_confirm_edit.setStyleSheet(
            "QLineEdit{background:#0d0d1e;color:#f0c040;"
            "border:1px solid #4a4a20;border-radius:4px;"
            "padding:4px 8px;font-size:13px;font-family:monospace;"
            "font-weight:bold;}")
        cfl.addWidget(self._rst_confirm_edit)
        v.addWidget(confirm_grp)

        # 초기화 버튼
        self._rst_btn = QPushButton("🔄  설정 초기화 실행")
        self._rst_btn.setMinimumHeight(40)
        self._rst_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #4a1a1a, stop:1 #6a2a2a);
                color: #ffaaaa; font-size:13px; font-weight:bold;
                border: 1px solid #8a3a3a; border-radius:6px; padding:6px;
            }
            QPushButton:hover { background: #7a2a2a; color: white; }
            QPushButton:pressed { background: #3a1010; }
        """)
        self._rst_btn.clicked.connect(self._on_reset_settings)
        v.addWidget(self._rst_btn)

        # 결과 레이블
        self._rst_result_lbl = QLabel("")
        self._rst_result_lbl.setStyleSheet("font-size:11px; padding:2px;")
        v.addWidget(self._rst_result_lbl)

        return w

    def _on_reset_settings(self):
        """설정 초기화 실행."""
        if self.engine.recording:
            self._rst_result_lbl.setText("⚠ 녹화 중에는 초기화할 수 없습니다.")
            self._rst_result_lbl.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")
            return

        if self._rst_confirm_edit.text().strip() != "RESET":
            self._rst_result_lbl.setText("❌ 확인 입력이 올바르지 않습니다. 'RESET'을 정확히 입력하세요.")
            self._rst_result_lbl.setStyleSheet("color:#e74c3c;font-size:11px;")
            return

        do_settings = self._rst_chk_settings.isChecked()
        do_memo     = self._rst_chk_memo.isChecked()
        do_sections = self._rst_chk_sections.isChecked()
        do_db       = self._rst_chk_db.isChecked()

        # DB 완전 삭제
        if do_db:
            try:
                if os.path.exists(DB_PATH):
                    os.remove(DB_PATH)
                self.db = SettingsDB()   # 재생성
                self._log("[초기화] DB 파일 삭제 후 재생성 완료")
            except Exception as ex:
                self._rst_result_lbl.setText(f"❌ DB 삭제 실패: {ex}")
                self._rst_result_lbl.setStyleSheet("color:#e74c3c;font-size:11px;")
                return

        # 설정값 초기화
        if do_settings:
            self._scr_fps_spin.setValue(30.0)
            self._speed_spin.setValue(1.0)
            self._scr_rec_chk.setChecked(True)
            self._thr_spin.setValue(30.0)
            self._cd_spin.setValue(5.0)
            self._bo_rec_chk.setChecked(True)
            self._ci_spin.setValue(1.0)
            self._mac_rep.setValue(1)
            self._mac_gap.setValue(1.0)
            self._m_pre_spin.setValue(10.0)
            self._m_pre_sl.setValue(100)
            self._m_post_spin.setValue(10.0)
            self._m_post_sl.setValue(100)
            self._m_scr_chk.setChecked(True)
            self._m_cam_chk.setChecked(True)
            self._memo_ts_chk.setChecked(True)
            self._log("[초기화] 설정값 기본값으로 복원")

        # 메모 탭 초기화
        if do_memo:
            while self._memo_tabs.count() > 0:
                self._memo_tabs.removeTab(0)
            self._memo_editors.clear()
            self.engine.memo_texts.clear()
            self._add_memo_tab("메모 1")
            self._log("[초기화] 메모 탭 초기화")

        # 섹션 접힘 상태 초기화 (모두 펼치기)
        if do_sections:
            for sec in self._sections.values():
                sec.set_collapsed(False)
            self._log("[초기화] 섹션 상태 초기화 (모두 펼침)")

        # DB에 반영
        self._save_settings()

        # 결과 표시
        self._rst_confirm_edit.clear()
        self._rst_result_lbl.setText("✅ 초기화 완료!")
        self._rst_result_lbl.setStyleSheet("color:#2ecc71;font-size:12px;font-weight:bold;")
        self._log("[초기화] 설정 초기화 완료")
        # 3초 후 결과 메시지 지우기
        QTimer.singleShot(3000, lambda: self._rst_result_lbl.setText(""))


    def _connect_signals(self):
        self.signals.blackout_detected.connect(self._on_blackout)
        self.signals.status_message.connect(self._log)
        self.signals.auto_click_count.connect(self._click_lcd.display)
        self.signals.macro_step_recorded.connect(self._on_mac_step)
        self.signals.manual_clip_saved.connect(self._on_manual_saved)

    # =========================================================================
    #  ★ 설정 저장 / 복원  (완전 동기식)
    # =========================================================================
    def _save_settings(self):
        """현재 UI 상태를 DB에 저장. 종료 시에도 동기적으로 호출됩니다."""
        db = self.db
        db.set("screen_fps",     self._scr_fps_spin.value())
        db.set("playback_speed", self._speed_spin.value())
        db.set("screen_rec",     self._scr_rec_chk.isChecked())
        db.set("bo_threshold",   self._thr_spin.value())
        db.set("bo_cooldown",    self._cd_spin.value())
        db.set("bo_rec",         self._bo_rec_chk.isChecked())
        db.set("ac_interval",    self._ci_spin.value())
        db.set("mac_repeat",     int(self._mac_rep.value()))
        db.set("mac_gap",        self._mac_gap.value())
        db.set("manual_pre",     self._m_pre_spin.value())
        db.set("manual_post",    self._m_post_spin.value())
        db.set("manual_source",  self.engine.manual_source)
        db.set("manual_src_scr", self._m_scr_chk.isChecked())
        db.set("manual_src_cam", self._m_cam_chk.isChecked())
        db.set("memo_ts",        self._memo_ts_chk.isChecked())
        # 섹션 접힘 상태
        for key, sec in self._sections.items():
            db.set(f"sec_col_{key}", sec.is_collapsed())
        # 메모 탭
        tabs = [{'title': self._memo_tabs.tabText(i),
                 'content': self._memo_editors[i].toPlainText()}
                for i in range(self._memo_tabs.count())
                if i < len(self._memo_editors)]
        db.save_memo_tabs(tabs)

    def _load_settings(self):
        """DB에서 세팅을 읽어 UI에 반영합니다."""
        db = self.db
        self._scr_fps_spin.setValue(db.get_float("screen_fps",      30.0))
        self._speed_spin.setValue(  db.get_float("playback_speed",  1.0))
        self._scr_rec_chk.setChecked(db.get_bool("screen_rec",      True))
        self._thr_spin.setValue(    db.get_float("bo_threshold",    30.0))
        self._cd_spin.setValue(     db.get_float("bo_cooldown",     5.0))
        self._bo_rec_chk.setChecked(db.get_bool("bo_rec",           True))
        self._ci_spin.setValue(     db.get_float("ac_interval",     1.0))
        self._mac_rep.setValue(     db.get_int("mac_repeat",        1))
        self._mac_gap.setValue(     db.get_float("mac_gap",         1.0))
        pre  = db.get_float("manual_pre",  10.0)
        post = db.get_float("manual_post", 10.0)
        self._m_pre_spin.setValue(pre)
        self._m_pre_sl.setValue(int(pre * 10))
        self._m_post_spin.setValue(post)
        self._m_post_sl.setValue(int(post * 10))
        self._m_scr_chk.setChecked(db.get_bool("manual_src_scr", True))
        self._m_cam_chk.setChecked(db.get_bool("manual_src_cam", True))
        src = db.get("manual_source", "both")
        self.engine.manual_source = src
        self._memo_ts_chk.setChecked(db.get_bool("memo_ts", True))
        # 섹션 접힘 복원
        for key, sec in self._sections.items():
            sec.set_collapsed(db.get_bool(f"sec_col_{key}", False))
        # 메모 탭 복원
        tabs = db.load_memo_tabs()
        if tabs:
            while self._memo_tabs.count() > 0:
                self._memo_tabs.removeTab(0)
            self._memo_editors.clear()
            self.engine.memo_texts.clear()
            for t in tabs:
                self._add_memo_tab(t['title'], t['content'])

    def _auto_save_settings(self):
        """10초마다 백그라운드에서 저장."""
        threading.Thread(target=self._save_settings, daemon=True).start()

    # =========================================================================
    #  미리보기 펌프
    # =========================================================================
    def _pump_preview(self):
        try:
            sf=self.engine.screen_queue.get_nowait()
            if self._scr_toggle.isChecked():
                self._scr_lbl.update_frame(RecorderEngine.stamp_preview(sf,self.engine,"screen"))
        except queue.Empty: pass
        try:
            cf=self.engine.camera_queue.get_nowait()
            if self._cam_win.isVisible():
                self._cam_win.get_label().update_frame(RecorderEngine.stamp_preview(cf,self.engine,"camera"))
        except queue.Empty: pass

    # =========================================================================
    #  주기적 갱신
    # =========================================================================
    def _refresh_ui(self):
        if self.engine.recording and self.engine.start_time:
            e=time.time()-self.engine.start_time
            self._rec_timer_lbl.setText(f"{int(e//3600):02d}:{int((e%3600)//60):02d}:{int(e%60):02d}")
        self._scr_bo.setText(str(self.engine.screen_blackout_count))
        self._cam_bo.setText(str(self.engine.camera_blackout_count))
        lines=[]
        for src,avgs,ov in [("Scr",self.engine.screen_roi_avg,self.engine.screen_overall_avg),("Cam",self.engine.camera_roi_avg,self.engine.camera_overall_avg)]:
            if avgs:
                b,g,r=ov; br=0.114*b+0.587*g+0.299*r
                lines.append(f"[{src}] R{int(r)} G{int(g)} B{int(b)} Br:{int(br)}")
                for i,a in enumerate(avgs[:5]):
                    b2,g2,r2=a; br2=0.114*b2+0.587*g2+0.299*r2
                    lines.append(f"  ROI{i+1}: R{int(r2)} G{int(g2)} Br:{int(br2)}")
        self._roi_txt.setPlainText("\n".join(lines))
        ev=[]
        for src,evs in [("Screen",self.engine.screen_blackout_events),("Camera",self.engine.camera_blackout_events)]:
            if evs:
                ev.append(f"── {src} ──")
                for e2 in reversed(evs[-5:]): ev.append(f"  {e2['time']}  변화:{int(e2['brightness_change'])}")
        self._ev_txt.setPlainText("\n".join(ev))
        self._refresh_sched_tbl()

    def _update_fps(self):
        sf=self.engine.measured_fps(self.engine._screen_fps_ts); cf=self.engine.measured_fps(self.engine._camera_fps_ts)
        self._scr_fps_lbl.setText(f"{sf:.1f} fps"); self._cam_fps_lbl.setText(f"{cf:.1f} fps")
        self._scr_fps_badge.setText(f"FPS: {sf:.1f}"); self._cam_fps_det_lbl.setText(f"{self.engine.actual_camera_fps:.2f} fps")

    def _check_segment(self):
        if (self.engine.recording and self.engine.current_segment_start and
                time.time()-self.engine.current_segment_start>=self.engine.segment_duration):
            self._log("Creating new segment…")
            threading.Thread(target=self.engine._create_segment,daemon=True).start()

    def _poll_manual_state(self):
        state=self.engine.manual_state
        if state==RecorderEngine.MANUAL_IDLE:
            self._led_timer.stop(); self._led_lbl.setStyleSheet("font-size:22px;color:#333;")
            self._manual_btn.setEnabled(True); self._manual_btn.setStyleSheet(self._manual_btn_style(True))
        else:
            if not self._led_timer.isActive(): self._led_timer.start(400)
            self._manual_btn.setEnabled(False); self._manual_btn.setStyleSheet(self._manual_btn_style(False))

    def _blink_led(self):
        self._led_state=not self._led_state
        if self._led_state:
            self._led_lbl.setStyleSheet("font-size:22px;color:#e74c3c;")
            self._manual_status.setText("🔴 클립 수집 중…"); self._manual_status.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")
        else: self._led_lbl.setStyleSheet("font-size:22px;color:#7f2a2a;")

    # =========================================================================
    #  Schedule
    # =========================================================================
    def _on_schedule_add(self):
        start_dt=stop_dt=None
        if self._s_start_chk.isChecked():
            qdt=self._s_start_dt.dateTime(); d=qdt.date(); t=qdt.time()
            start_dt=datetime(d.year(),d.month(),d.day(),t.hour(),t.minute(),t.second())
        if self._s_stop_chk.isChecked():
            qdt=self._s_stop_dt.dateTime(); d=qdt.date(); t=qdt.time()
            stop_dt=datetime(d.year(),d.month(),d.day(),t.hour(),t.minute(),t.second())
        now=datetime.now()
        if start_dt and start_dt<now: QMessageBox.warning(self,"오류","시작 시각이 과거입니다."); return
        if stop_dt and stop_dt<now: QMessageBox.warning(self,"오류","종료 시각이 과거입니다."); return
        if start_dt and stop_dt and stop_dt<=start_dt: QMessageBox.warning(self,"오류","종료 > 시작 이어야 합니다."); return
        if not start_dt and not stop_dt: QMessageBox.warning(self,"오류","시작/종료 중 하나 이상 설정하세요."); return
        new_s=start_dt or now; new_e=stop_dt
        for ex in self.engine.schedules:
            if ex.done: continue
            ex_s=ex.start_dt or now; ex_e=ex.stop_dt
            no_ov=((new_e is not None and new_e<=ex_s) or (ex_e is not None and ex_e<=new_s))
            if not no_ov: QMessageBox.warning(self,"겹침",f"예약 #{ex.id} 와 겹칩니다."); return
        entry=ScheduleEntry(start_dt,stop_dt); self.engine.schedules.append(entry)
        self._add_sched_row(entry); self._log(f"[Schedule] #{entry.id} 추가: {entry.label()}")

    def _add_sched_row(self,e):
        r=self._sched_tbl.rowCount(); self._sched_tbl.insertRow(r)
        s=e.start_dt.strftime("%m/%d %H:%M:%S") if e.start_dt else "—"
        ed=e.stop_dt.strftime("%m/%d %H:%M:%S")  if e.stop_dt  else "—"
        for c,v in enumerate([str(e.id),s,ed,"대기"]):
            it=QTableWidgetItem(v); it.setTextAlignment(Qt.AlignCenter); self._sched_tbl.setItem(r,c,it)

    def _refresh_sched_tbl(self):
        for r in range(self._sched_tbl.rowCount()):
            it=self._sched_tbl.item(r,0)
            if not it: continue
            entry=next((s for s in self.engine.schedules if s.id==int(it.text())),None)
            if not entry: continue
            st=self._sched_tbl.item(r,3)
            if st:
                if entry.done: st.setText("완료"); st.setForeground(QColor("#888"))
                elif entry.started: st.setText("진행중"); st.setForeground(QColor("#2ecc71"))
                else: st.setText("대기"); st.setForeground(QColor("#f0c040"))
        now=datetime.now(); pending=[s for s in self.engine.schedules if not s.done]
        if pending:
            nxt=min(pending,key=lambda s: s.start_dt or s.stop_dt or datetime.max)
            ref=nxt.start_dt or nxt.stop_dt
            if ref:
                secs=int((ref-now).total_seconds())
                if secs>=0: self._sched_cd.setText(f"#{nxt.id} 까지  {secs//3600:02d}h {(secs%3600)//60:02d}m {secs%60:02d}s")
                else: self._sched_cd.setText(f"#{nxt.id} 진행 중…")
        else: self._sched_cd.setText("예약 없음")

    def _on_sched_del(self):
        rows=sorted({i.row() for i in self._sched_tbl.selectedItems()},reverse=True)
        for r in rows:
            it=self._sched_tbl.item(r,0)
            if it: self.engine.schedules=[s for s in self.engine.schedules if s.id!=int(it.text())]
            self._sched_tbl.removeRow(r)

    def _on_sched_clear(self): self.engine.schedules.clear(); self._sched_tbl.setRowCount(0)

    def _tick_schedule(self):
        for action,entry in self.engine.schedule_tick():
            if action=='start': self._log(f"[Schedule] ⏺ #{entry.id} 녹화 시작"); self._on_start_rec()
            elif action=='stop': self._log(f"[Schedule] ⏹ #{entry.id} 녹화 종료"); self._on_stop_rec()

    # =========================================================================
    #  Feature bar
    # =========================================================================
    def _on_feature_toggle(self,key,enabled):
        sec=self._sections.get(key)
        if sec: sec.setVisible(enabled)

    def _on_feature_order(self,order):
        while self._panel_l.count():
            it=self._panel_l.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for key in order:
            sec=self._sections.get(key)
            if sec: sec.setParent(self._panel_w); self._panel_l.addWidget(sec); sec.setVisible(self._feat_bar.is_enabled(key))
        self._panel_l.addStretch()

    def _on_scroll_to_section(self, key: str):
        """★ v9: 더블클릭 → 해당 섹션으로 스크롤 + 접혀 있으면 펼치기."""
        sec = self._sections.get(key)
        if not sec: return
        # 섹션이 숨겨져 있으면 먼저 표시
        if not sec.isVisible():
            sec.setVisible(True)
        # 접혀 있으면 펼치기
        if sec.is_collapsed():
            sec.set_collapsed(False)
        # 레이아웃이 갱신될 시간을 주고 스크롤
        QTimer.singleShot(50, lambda: self._scroll_to_widget(sec))

    def _scroll_to_widget(self, widget: QWidget):
        """QScrollArea 내에서 특정 위젯이 보이도록 스크롤."""
        # 위젯의 패널 내 Y 좌표 계산
        pos = widget.mapTo(self._panel_w, QPoint(0, 0))
        vsb = self._panel_scroll.verticalScrollBar()
        vsb.setValue(max(0, pos.y() - 10))

    # =========================================================================
    #  슬롯
    # =========================================================================
    def _on_scr_toggle(self,checked):
        self._scr_lbl.set_active(checked)
        if checked: self.engine.start_screen_thread()
        else: self.engine.stop_screen_thread()

    def _on_cam_win_toggle(self,checked):
        if checked: self._cam_win.show(); self._cam_win.raise_()
        else: self._cam_win.hide()

    def _on_speed_changed(self,val):
        self.engine.playback_speed=val
        if val==1.0: desc="정배속"
        elif val<1.0: desc=f"{val:.2f}× 슬로우"
        else: desc=f"{val:.2f}× 타임랩스"
        self._speed_info.setText(f"  {desc}")

    def _on_start_rec(self):
        self.engine.start_recording()
        self._btn_start.setEnabled(False); self._btn_stop.setEnabled(True)
        self._rec_status_lbl.setText("● RECORDING"); self._rec_status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;font-size:14px;")
        self._speed_spin.setEnabled(False); self._speed_lock.setVisible(True)

    def _on_stop_rec(self):
        self.engine.stop_recording()
        self._btn_start.setEnabled(True); self._btn_stop.setEnabled(False)
        self._rec_status_lbl.setText("● STOPPED"); self._rec_status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        self._rec_timer_lbl.setText("00:00:00")
        self._speed_spin.setEnabled(True); self._speed_lock.setVisible(False)

    def _on_scr_rec_toggle(self,checked):
        self.engine.screen_recording_enabled=checked
        if self.engine.recording: threading.Thread(target=self.engine._create_segment,daemon=True).start()

    def _on_ac_start(self):
        self.engine.start_auto_click(); self._btn_ac_start.setEnabled(False); self._btn_ac_stop.setEnabled(True)
        self._ac_status.setText("● RUNNING"); self._ac_status.setStyleSheet("color:#2ecc71;font-weight:bold;")

    def _on_ac_stop(self):
        self.engine.stop_auto_click(); self._btn_ac_start.setEnabled(True); self._btn_ac_stop.setEnabled(False)
        self._ac_status.setText("● STOPPED"); self._ac_status.setStyleSheet("color:#e74c3c;font-weight:bold;")

    def _on_manual_src_changed(self):
        s=self._m_scr_chk.isChecked(); c=self._m_cam_chk.isChecked()
        if not s and not c:
            self.sender().blockSignals(True); self.sender().setChecked(True); self.sender().blockSignals(False); return
        self.engine.manual_source=("both" if s and c else "screen" if s else "camera")

    def _on_manual_clip(self):
        ok=self.engine.save_manual_clip()
        if not ok:
            self._manual_status.setText("⏳ 쿨다운 중 — 잠시 후 다시 시도하세요")
            self._manual_status.setStyleSheet("color:#f0c040;font-size:11px;")

    def _on_manual_saved(self,path):
        self._log(f"[수동녹화] 저장됨 → {path}")
        self.db.log_manual_clip(self.engine.manual_source,self.engine.manual_pre_sec,self.engine.manual_post_sec,path)
        self._manual_status.setText(f"✅ 저장: {os.path.basename(path)}"); self._manual_status.setStyleSheet("color:#2ecc71;font-size:11px;")

    # Macro
    def _on_mac_rec_toggle(self,recording):
        if recording:
            self._mac_rec_btn.setText("⏹  기록 중단"); self._mac_rec_st.setText("● 기록 중"); self._mac_rec_st.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;"); self.engine.macro_start_recording()
        else:
            self._mac_rec_btn.setText("⏺  기록 시작"); self._mac_rec_st.setText("● 완료"); self._mac_rec_st.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;"); self.engine.macro_stop_recording(); QTimer.singleShot(150,self._make_mac_editable)

    def _on_mac_step(self,x,y,delay):
        step=ClickStep(x,y,delay); self._mac_append_row(step,False)

    def _mac_append_row(self,step,editable=True):
        self._mac_tbl.blockSignals(True); r=self._mac_tbl.rowCount(); self._mac_tbl.insertRow(r)
        ef=Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsEditable; rf=Qt.ItemIsEnabled|Qt.ItemIsSelectable
        idx_it=QTableWidgetItem(str(r+1)); idx_it.setFlags(Qt.ItemIsEnabled); idx_it.setTextAlignment(Qt.AlignCenter); idx_it.setForeground(QColor("#556")); self._mac_tbl.setItem(r,0,idx_it)
        for col,val in [(1,str(step.x)),(2,str(step.y)),(3,f"{step.delay:.3f}")]:
            it=QTableWidgetItem(val); it.setTextAlignment(Qt.AlignCenter); it.setFlags(ef if editable else rf)
            if not editable: it.setForeground(QColor("#888"))
            self._mac_tbl.setItem(r,col,it)
        self._mac_tbl.scrollToBottom(); self._mac_tbl.blockSignals(False)
        self._mac_pos_lbl.setText(f"({step.x},{step.y}) delay={step.delay:.3f}s")

    def _make_mac_editable(self):
        ef=Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsEditable; self._mac_tbl.blockSignals(True)
        for r in range(self._mac_tbl.rowCount()):
            for c in (1,2,3):
                it=self._mac_tbl.item(r,c)
                if it: it.setFlags(ef); it.setForeground(QColor("#ddd"))
        self._mac_tbl.blockSignals(False)

    def _on_mac_item_changed(self,item):
        r=item.row(); c=item.column()
        if r>=len(self.engine.macro_steps): return
        step=self.engine.macro_steps[r]
        try:
            v=float(item.text())
            if c==1: step.x=int(v)
            elif c==2: step.y=int(v)
            elif c==3: step.delay=max(0.0,v)
        except ValueError: pass

    def _on_mac_del(self):
        rows=sorted({i.row() for i in self._mac_tbl.selectedItems()},reverse=True)
        self._mac_tbl.blockSignals(True)
        for r in rows:
            self._mac_tbl.removeRow(r)
            if r<len(self.engine.macro_steps): self.engine.macro_steps.pop(r)
        for r in range(self._mac_tbl.rowCount()):
            it=self._mac_tbl.item(r,0)
            if it: it.setText(str(r+1))
        self._mac_tbl.blockSignals(False)

    def _on_mac_clear(self):
        self._mac_tbl.blockSignals(True); self._mac_tbl.setRowCount(0); self._mac_tbl.blockSignals(False); self.engine.macro_clear(); self._mac_pos_lbl.setText("마지막 클릭: —")

    def _on_mac_up(self):
        r=self._mac_tbl.currentRow()
        if r<=0 or r>=len(self.engine.macro_steps): return
        s=self.engine.macro_steps; s[r-1],s[r]=s[r],s[r-1]; self._rebuild_mac_tbl(); self._mac_tbl.setCurrentCell(r-1,0)

    def _on_mac_dn(self):
        r=self._mac_tbl.currentRow(); s=self.engine.macro_steps
        if r<0 or r>=len(s)-1: return
        s[r],s[r+1]=s[r+1],s[r]; self._rebuild_mac_tbl(); self._mac_tbl.setCurrentCell(r+1,0)

    def _rebuild_mac_tbl(self):
        self._mac_tbl.blockSignals(True); self._mac_tbl.setRowCount(0); self._mac_tbl.blockSignals(False)
        for step in self.engine.macro_steps: self._mac_append_row(step,True)

    def _on_mac_bulk(self):
        d=self._mac_bulk.value(); self._mac_tbl.blockSignals(True)
        for i,step in enumerate(self.engine.macro_steps):
            step.delay=d
            it=self._mac_tbl.item(i,3)
            if it: it.setText(f"{d:.3f}")
        self._mac_tbl.blockSignals(False)

    def _on_mac_run(self):
        if not self.engine.macro_steps: self._log("[Macro] 스텝 없음"); return
        self.engine.macro_start_run(); self._mac_run_btn.setEnabled(False); self._mac_stop_btn.setEnabled(True)
        self._mac_run_st.setText("● 실행 중"); self._mac_run_st.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
        self._mac_watch=QTimer(self); self._mac_watch.timeout.connect(self._check_mac_done); self._mac_watch.start(300)

    def _check_mac_done(self):
        if not self.engine.macro_running:
            self._mac_watch.stop(); self._mac_run_btn.setEnabled(True); self._mac_stop_btn.setEnabled(False)
            self._mac_run_st.setText("● 완료"); self._mac_run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")

    def _on_mac_stop(self):
        self.engine.macro_stop_run(); self._mac_run_btn.setEnabled(True); self._mac_stop_btn.setEnabled(False)
        self._mac_run_st.setText("● 중단"); self._mac_run_st.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")

    # Memo
    def _add_memo_tab(self,title,content=""):
        ed=TimestampMemoEdit(); ed.setPlaceholderText("메모 입력… (줄 클릭 시 타임스탬프 삽입)")
        ed.setStyleSheet("background:#0d0d1e;color:#ffe;border:none;font-size:11px;font-family:monospace;")
        ed.timestamp_enabled=self._memo_ts_chk.isChecked()
        if content: ed.setPlainText(content)
        ed.textChanged.connect(self._on_any_memo_changed)
        self._memo_editors.append(ed); self._memo_tabs.addTab(ed,title)
        while len(self.engine.memo_texts)<len(self._memo_editors): self.engine.memo_texts.append("")
        self._sync_overlay_tab_max()

    def _on_memo_add_tab(self):
        n=self._memo_tabs.count()+1; self._add_memo_tab(f"메모 {n}"); self._memo_tabs.setCurrentIndex(self._memo_tabs.count()-1)

    def _on_memo_del_tab(self):
        if self._memo_tabs.count()<=1: return
        idx=self._memo_tabs.currentIndex(); self._memo_tabs.removeTab(idx)
        if idx<len(self._memo_editors): self._memo_editors.pop(idx)
        if idx<len(self.engine.memo_texts): self.engine.memo_texts.pop(idx)
        self._sync_overlay_tab_max()

    def _on_memo_clear_cur(self):
        idx=self._memo_tabs.currentIndex()
        if idx<len(self._memo_editors): self._memo_editors[idx].clear()

    def _on_memo_tab_changed(self,idx): pass

    def _on_any_memo_changed(self):
        for i,ed in enumerate(self._memo_editors):
            if i<len(self.engine.memo_texts): self.engine.memo_texts[i]=ed.toPlainText()
            else: self.engine.memo_texts.append(ed.toPlainText())

    def _on_ts_toggled(self,enabled):
        for ed in self._memo_editors: ed.timestamp_enabled=enabled

    def _rebuild_overlay_rows(self):
        while self._overlay_layout.count():
            it=self._overlay_layout.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._overlay_rows.clear()
        for cfg in self.engine.memo_overlays: self._add_overlay_row(cfg)

    def _add_overlay_row(self,cfg):
        n=max(self._memo_tabs.count(),1); row_w=MemoOverlayRow(cfg,n)
        row_w.removed.connect(self._remove_overlay_row); self._overlay_rows.append(row_w); self._overlay_layout.addWidget(row_w)

    def _remove_overlay_row(self,row_w):
        if len(self.engine.memo_overlays)<=1: return
        if row_w.cfg in self.engine.memo_overlays: self.engine.memo_overlays.remove(row_w.cfg)
        self._overlay_layout.removeWidget(row_w); row_w.deleteLater()
        if row_w in self._overlay_rows: self._overlay_rows.remove(row_w)

    def _on_add_overlay(self):
        cfg=MemoOverlayConfig(0,"bottom-right","both",True); self.engine.memo_overlays.append(cfg); self._add_overlay_row(cfg)

    def _sync_overlay_tab_max(self):
        n=max(self._memo_tabs.count(),1)
        for row in self._overlay_rows: row.update_tab_max(n)

    def _on_blackout(self,source,event):
        self._log(f"[BLACKOUT/{source.upper()}] {event['time']}  변화:{int(event['brightness_change'])}")

    def _log(self,msg):
        ts=datetime.now().strftime("%H:%M:%S"); self._log_txt.append(f"[{ts}] {msg}"); self._status_lbl.setText(msg[:130])

    # =========================================================================
    #  단축키
    # =========================================================================
    def _setup_hotkeys(self):
        if not PYNPUT_AVAILABLE: return
        hk={
            '<ctrl>+<alt>+w': self._on_start_rec,
            '<ctrl>+<alt>+e': self._on_stop_rec,
            '<ctrl>+<alt>+d': lambda: self._scr_rec_chk.setChecked(not self._scr_rec_chk.isChecked()),
            '<ctrl>+<alt>+a': self._on_ac_start,
            '<ctrl>+<alt>+s': self._on_ac_stop,
            '<ctrl>+<alt>+m': self._on_manual_clip,
            '<ctrl>+<alt>+q': self.close,
        }
        self._hkl=pynput_keyboard.GlobalHotKeys(hk); self._hkl.start()

    # =========================================================================
    #  스타일
    # =========================================================================
    @staticmethod
    def _dark_style() -> str:
        return """
        QMainWindow,QWidget{background:#12122a;color:#ddd;}
        QDialog{background:#0d0d1e;color:#ddd;}
        QGroupBox{border:1px solid #2a2a4a;border-radius:6px;margin-top:18px;
            font-weight:bold;color:#9bc;font-size:12px;padding-top:10px;}
        QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;
            left:10px;top:-2px;padding:2px 6px;background:#12122a;border-radius:3px;}
        QPushButton{background:#1e2a3a;border:1px solid #4a5a7a;border-radius:4px;padding:4px 10px;color:#ccd;font-size:11px;}
        QPushButton:hover{background:#2a3e56;border-color:#6a8aaa;color:#eef;}
        QPushButton:pressed{background:#0d1a2a;border:2px solid #3a6a9a;color:#7bc8ff;padding:5px 9px 3px 11px;}
        QPushButton:checked{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #1a4a6a,stop:1 #0d2a3a);border:2px solid #3a8aca;color:#7bc8ff;font-weight:bold;}
        QPushButton:checked:hover{background:#1a5a7a;border-color:#5aaada;}
        QPushButton:checked:pressed{background:#0a1e2e;border-color:#1a6a9a;}
        QPushButton:disabled{background:#141424;color:#3a3a5a;border-color:#2a2a3a;}
        QTextEdit,QPlainTextEdit{background:#0d0d1e;border:1px solid #2a2a4a;color:#ccc;}
        QTextEdit:focus,QPlainTextEdit:focus{border-color:#4a6a9a;}
        /* ── 스핀박스 / 콤보박스 ── */
        QDoubleSpinBox,QSpinBox{background:#1a1a3a;border:1px solid #3a4a6a;color:#ddd;padding:2px 4px;border-radius:3px;}
        QDoubleSpinBox:focus,QSpinBox:focus{border-color:#5a8aca;color:#eef;}
        QDoubleSpinBox::up-button,QSpinBox::up-button,
        QDoubleSpinBox::down-button,QSpinBox::down-button{background:#1e2a3a;border:none;width:14px;}
        QDoubleSpinBox::up-button:hover,QSpinBox::up-button:hover,
        QDoubleSpinBox::down-button:hover,QSpinBox::down-button:hover{background:#2a3a4e;}
        QDoubleSpinBox::up-button:pressed,QSpinBox::up-button:pressed,
        QDoubleSpinBox::down-button:pressed,QSpinBox::down-button:pressed{background:#0d1a2a;}
        QComboBox{background:#1a1a3a;border:1px solid #3a4a6a;color:#ddd;padding:2px 4px;border-radius:3px;}
        QComboBox:hover{border-color:#5a8aca;background:#1e2040;}
        QComboBox:focus{border-color:#5a8aca;}
        /* ── 체크박스 ── */
        QCheckBox{color:#ccd;spacing:6px;}
        QCheckBox:hover{color:#eef;}
        QCheckBox::indicator{width:16px;height:16px;border:1px solid #4a5a7a;border-radius:3px;background:#0d0d1e;}
        QCheckBox::indicator:hover{border-color:#6a9aca;background:#1a2030;}
        QCheckBox::indicator:pressed{background:#0a1828;border:2px solid #2a7aca;}
        QCheckBox::indicator:checked{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #2a7aca,stop:1 #1a5a9a);border:2px solid #5aaae0;}
        QCheckBox::indicator:checked:hover{background:#3a8ada;border-color:#7abaf0;}
        QCheckBox::indicator:checked:pressed{background:#1a5a8a;border-color:#3a7aba;}
        QLabel{color:#ccd;}
        QLCDNumber{background:#0d1520;border:1px solid #336;color:#2ecc71;}
        QTableWidget{selection-background-color:#1a3a5a;}
        QTableWidget::item:selected{background:#1a3a5a;color:#eef;}
        /* ── 스크롤바 ── */
        QScrollBar:vertical{background:#0d0d1e;width:8px;border-radius:4px;}
        QScrollBar::handle:vertical{background:#336;border-radius:4px;min-height:20px;}
        QScrollBar::handle:vertical:hover{background:#4a5a8a;}
        QScrollBar::handle:vertical:pressed{background:#5a6a9a;}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
        /* ── 슬라이더 ── */
        QSlider::groove:horizontal{background:#1a2030;height:6px;border-radius:3px;}
        QSlider::handle:horizontal{background:#3a7abd;width:16px;height:16px;
            margin-top:-5px;margin-bottom:-5px;border-radius:8px;border:1px solid #1a4a8a;}
        QSlider::handle:horizontal:hover{background:#4a8ace;border-color:#2a6aaa;}
        QSlider::handle:horizontal:pressed{background:#1a5a9a;border-color:#0a3a7a;}
        QSlider::sub-page:horizontal{background:#2a5a8a;border-radius:3px;}
        /* ── 탭 ── */
        QTabWidget::pane{border:1px solid #2a2a4a;}
        QTabBar::tab{background:#1a1a3a;color:#888;border:1px solid #334;
            border-bottom:none;border-radius:4px 4px 0 0;padding:4px 10px;}
        QTabBar::tab:selected{background:#2a2a5a;color:#dde;border-color:#446;font-weight:bold;}
        QTabBar::tab:hover{background:#22224a;color:#bbd;}
        QTabBar::tab:pressed{background:#1a1a40;}
        QComboBox::drop-down{border:none;}
        QComboBox QAbstractItemView{background:#1a1a3a;color:#ddd;selection-background-color:#2a4a7a;selection-color:#fff;}
        QDateTimeEdit{background:#1a1a3a;border:1px solid #336;color:#ddd;padding:2px 4px;border-radius:3px;}
        QDateTimeEdit:focus{border-color:#5a8aca;}
        QDateTimeEdit::drop-down{border:none;}
        /* ── 스플리터 ── */
        QSplitter::handle{background:#2a2a4a;width:5px;}
        QSplitter::handle:hover{background:#4a6aaa;}
        QSplitter::handle:pressed{background:#6a8aca;}
        """

    # =========================================================================
    #  ★ 종료 이벤트 — 녹화 중이면 정상 종료 후 파일 완성
    # =========================================================================
    def closeEvent(self, e):
        """
        종료 순서:
        1. 세팅 DB 동기 저장 (메인 스레드)
        2. 녹화 중이면 stop_recording() → VideoWriter.release() 완전 수행
        3. 엔진 정리 (스레드 종료)
        4. 핫키 리스너 종료
        """
        # ① 세팅 저장 (동기)
        self._save_settings()

        # ② 녹화 중이면 Ctrl+E 와 동일한 stop_recording 로직 수행
        if self.engine.recording:
            self._log("종료: 녹화 파일 저장 중…")
            # UI 상태 업데이트 (선택적)
            self._btn_stop.setEnabled(False)
            self._rec_status_lbl.setText("● 저장 중…")
            # stop_recording은 내부에서 0.35s sleep + release 수행
            self.engine.stop_recording()   # 동기 호출 → 완전히 끝난 뒤 반환

        # ③ 엔진 정리
        self.engine.stop()
        self._cam_win.hide()

        # ④ 핫키 종료
        if PYNPUT_AVAILABLE and hasattr(self, '_hkl'):
            self._hkl.stop()

        e.accept()


# =============================================================================
#  엔트리포인트
# =============================================================================
def main():
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("ScreenCameraRecorder")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()