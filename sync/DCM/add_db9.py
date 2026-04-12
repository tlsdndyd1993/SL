# -*- coding: utf-8 -*-
"""
Screen & Camera Recorder  v13.0
─────────────────────────────────────────────────────────────────────────────
v12 → v13 변경사항:
  [수정] Click Macro 기록 중 프로그램 뻗는 버그 완전 해결

  ■ 근본 원인 3단 분석:
    1. pynput Listener의 on_click 콜백은 OS의 저수준 마우스 훅
       (Windows: WH_MOUSE_LL, macOS: CGEventTap) 스레드에서 실행된다.
       이 훅 스레드는 OS가 다음 이벤트를 전달하기 전까지 콜백이
       반환되기를 기다린다.

    2. on_click 안에서 PyQt5 시그널(macro_step_recorded.emit)을 호출하면
       크로스스레드 queued connection이 발동한다.
       Windows에서 이 과정은 PostMessage → 메인 스레드 이벤트 루프를
       통해 처리되는데, 훅 스레드 자체가 메시지 펌프를 가지고 있어서
       Qt 이벤트 루프와 서로 메시지를 기다리는 교착(deadlock) 상태가 된다.

    3. auto_click의 mc.click()이 SendInput()으로 synthetic 이벤트를
       주입하면, 이미 on_click 실행 중인 훅 스레드가 재귀적으로 호출된다.
       Python GIL + pynput 내부 락이 동시에 경합하면서 프로그램이 완전히
       멈춘다.

  ■ 해결책: "훅 스레드 완전 분리" 패턴
    - on_click 콜백은 queue.Queue.put_nowait() 한 줄만 실행하고 즉시 반환
    - 별도 데몬 스레드(_macro_emit_loop)가 큐에서 꺼내 시그널 emit
    - 훅 스레드가 절대 블로킹되지 않으므로 deadlock 원천 차단
    - auto_click 동시 동작과 완전히 독립 (강제 중단 불필요)
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
    QLineEdit,
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

def _verify_korean_glyph(fnt, size: int) -> bool:
    """
    ★ v12: Missing Glyph 완전 검증
    
    핵심 원리 (Gemini 진단 기반):
    - 'UTF-8 인코딩 불일치'가 아닌 '폰트 Missing Glyph' 문제임을 확인
    - DejaVu 같은 폰트는 한글 코드포인트를 받으면 □(대체 글리프)를 렌더링
    - 대체 글리프도 픽셀을 가지므로 단순 픽셀 수 체크로는 구분 불가
    
    해결: 한글 렌더링 픽셀 분포 vs ASCII 렌더링 픽셀 분포를 비교
    - 진짜 한글 폰트: 'ㄱ', '나', '다'가 각기 다른 픽셀 패턴을 가짐
    - Missing Glyph 폰트: 한글 여러 글자가 동일한 □ 패턴 → 픽셀이 거의 같음
    """
    try:
        w, h = size * 4, size * 2
        
        def render(text):
            img = _PIL_Image.new("L", (w, h), 0)
            _PIL_Draw.Draw(img).text((2, 2), text, font=fnt, fill=255)
            return bytes(img.tobytes())
        
        # 서로 다른 한글 3글자 렌더링
        r1 = render("가")
        r2 = render("나")
        r3 = render("다")
        
        # 픽셀 총량이 너무 적으면 탈락 (공백 렌더링)
        total = sum(r1)
        if total < 20:
            return False
        
        # Missing Glyph: 세 글자가 모두 같은 □로 렌더링되면 r1==r2==r3
        # 진짜 한글: 각 글자가 달라서 최소 두 쌍은 달라야 함
        if r1 == r2 == r3:
            return False  # 모두 같으면 대체 글리프(□) 렌더링
        
        return True
    except Exception:
        return True  # 검증 실패 시 일단 허용 (구버전 PIL)


def _find_unicode_font(size: int = 18):
    """
    ★ v12: 플랫폼별 한글 폰트 탐색 + Missing Glyph 완전 검증
    v8 방식(명확한 경로 목록)과 레지스트리 탐색을 결합
    """
    if not PIL_AVAILABLE:
        return None

    import glob as _glob

    candidates: list = []
    _sys = platform.system()

    # ── Windows ──────────────────────────────────────────────────────────────
    if _sys == "Windows":
        win_fonts_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        user_fonts    = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                     "Microsoft", "Windows", "Fonts")

        # 1순위: 레지스트리에서 실제 설치 경로 조회 (가장 신뢰성 높음)
        try:
            import winreg
            _ko_kw = ("맑은", "Malgun", "malgun", "나눔", "Nanum", "nanum",
                      "굴림", "Gulim", "바탕", "Batang", "돋움", "Dotum",
                      "궁서", "Gungsuh")
            for hive, subkey in [
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                (winreg.HKEY_CURRENT_USER,
                 r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            ]:
                try:
                    rk = winreg.OpenKey(hive, subkey)
                    i = 0
                    while True:
                        try:
                            name, data, _ = winreg.EnumValue(rk, i); i += 1
                            if not any(k in name for k in _ko_kw): continue
                            path = data if os.path.isabs(data) \
                                        else os.path.join(win_fonts_dir, data)
                            candidates.append(path)
                        except OSError:
                            break
                    winreg.CloseKey(rk)
                except Exception:
                    pass
        except ImportError:
            pass

        # 2순위: 잘 알려진 고정 경로 (v8 방식)
        for fname in ["malgun.ttf", "malgunbd.ttf",
                      "gulim.ttc", "batang.ttc", "dotum.ttc",
                      "NanumGothic.ttf", "NanumGothicBold.ttf"]:
            candidates.append(os.path.join(win_fonts_dir, fname))
            candidates.append(os.path.join(user_fonts, fname))

    # ── macOS ─────────────────────────────────────────────────────────────────
    elif _sys == "Darwin":
        for d in ["/System/Library/Fonts", "/Library/Fonts",
                  os.path.expanduser("~/Library/Fonts")]:
            for f in ["AppleSDGothicNeo.ttc", "AppleSDGothicNeo-Regular.otf",
                      "NanumGothic.ttf", "Arial Unicode.ttf"]:
                candidates.append(os.path.join(d, f))
            for p in _glob.glob(os.path.join(d, "*Nanum*")):
                candidates.append(p)

    # ── Linux ─────────────────────────────────────────────────────────────────
    else:
        # fc-list로 실제 설치된 한국어 폰트를 1순위로
        try:
            import subprocess as _sp
            result = _sp.run(["fc-list", ":lang=ko", "--format=%{file}\n"],
                             capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                line = line.strip()
                if line and os.path.exists(line):
                    candidates.append(line)
        except Exception:
            pass

        for pat in ["/usr/share/fonts/**/Noto*CJK*.ttc",
                    "/usr/share/fonts/**/Noto*CJK*.otf",
                    "/usr/share/fonts/**/Nanum*.ttf",
                    "/usr/share/fonts/**/Un*.ttf"]:
            candidates += _glob.glob(pat, recursive=True)

        candidates += [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf",
        ]

    # ── 로드 & Missing Glyph 검증 ────────────────────────────────────────────
    seen = set()
    for path in candidates:
        if not path or not os.path.exists(path) or path in seen:
            continue
        seen.add(path)
        try:
            fnt = _PIL_Font.truetype(path, size)
            if _verify_korean_glyph(fnt, size):
                return fnt
        except Exception:
            continue

    return None  # 한글 폰트 없음 — None 반환 (DejaVu fallback 완전 제거)


_FONT_CACHE: dict = {}
_FOUND_FONT_PATH: str = ""


def _get_font(size: int = 18):
    global _FOUND_FONT_PATH
    if size not in _FONT_CACHE:
        _FONT_CACHE[size] = _find_unicode_font(size)
        if _FONT_CACHE[size] is not None and not _FOUND_FONT_PATH:
            try:
                _FOUND_FONT_PATH = getattr(_FONT_CACHE[size], 'path', '(unknown)')
            except Exception:
                _FOUND_FONT_PATH = "(loaded)"
    return _FONT_CACHE[size]


def check_korean_font() -> tuple:
    if not PIL_AVAILABLE:
        return False, "Pillow(PIL) 라이브러리가 없습니다.\npip install Pillow 를 실행하세요."

    fnt = _get_font(18)
    if fnt is None:
        return False, "한글 폰트를 찾을 수 없습니다.\n메모 오버레이가 ???로 표시됩니다."

    try:
        img = _PIL_Image.new("RGB", (200, 40), (0, 0, 0))
        draw = _PIL_Draw.Draw(img)
        draw.text((5, 5), "가나다라", font=fnt, fill=(255, 255, 100))
        arr = np.array(img)
        nonzero = (arr > 0).sum()
        if nonzero < 20:
            return False, (
                "한글 폰트를 로드했으나 한글 렌더링이 안 됩니다.\n"
                f"폰트 경로: {_FOUND_FONT_PATH}\n"
                "Windows: '맑은 고딕(malgun.ttf)' 설치를 확인하세요.")
        path_info = _FOUND_FONT_PATH or "(경로 미상)"
        return True, f"한글 폰트 OK: {path_info}"
    except Exception as ex:
        return False, f"한글 렌더링 테스트 실패: {ex}"


# =============================================================================
#  ★ v9 수정: PIL 전체 프레임 렌더링으로 한글 깨짐(ㅁㅁㅁ) 완전 해결
# =============================================================================
def _put_unicode_text(frame: np.ndarray, text: str,
                      x: int, y: int, font_size: int,
                      color_bgr: tuple, alpha: float = 1.0) -> None:
    """
    PIL을 이용해 유니코드(한글 포함) 텍스트를 frame에 in-place 합성합니다.

    ★ v9 핵심 수정:
    - 기존 ROI 슬라이싱 방식은 작은 ROI에서 좌표가 잘려 ㅁㅁㅁ가 됨
    - 전체 프레임을 PIL로 변환 → 텍스트 그리기 → 다시 numpy로 변환
    - 성능을 위해 텍스트 영역 주변만 처리하는 방식 유지하되
      경계 검사를 훨씬 넉넉하게 잡음
    """
    if not PIL_AVAILABLE:
        cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    font_size / 30, color_bgr, 2, cv2.LINE_AA)
        return

    fnt = _get_font(font_size)
    if fnt is None:
        cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    font_size / 30, color_bgr, 2, cv2.LINE_AA)
        return

    h_f, w_f = frame.shape[:2]
    color_rgb = (color_bgr[2], color_bgr[1], color_bgr[0])

    # 텍스트 크기 계산
    try:
        bbox = fnt.getbbox(text)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
    except AttributeError:
        try:
            tw, th = fnt.getsize(text)
        except Exception:
            tw, th = font_size * len(text), font_size

    # ★ 충분히 넉넉한 패딩으로 ROI 설정 (글자가 잘리지 않도록)
    pad = font_size + 8
    x1 = max(0, x - pad)
    y1 = max(0, y - th - pad)
    x2 = min(w_f, x + tw + pad * 2)
    y2 = min(h_f, y + pad)

    if x2 <= x1 or y2 <= y1:
        return

    # ROI만 PIL로 변환하여 처리
    roi = frame[y1:y2, x1:x2].copy()
    pil_img = _PIL_Image.fromarray(cv2.cvtColor(roi, cv2.COLOR_BGR2RGB))
    draw = _PIL_Draw.Draw(pil_img)

    # ROI 내 상대 좌표로 텍스트 그리기
    rel_x = x - x1
    rel_y = y - th - y1  # y는 baseline이므로 th만큼 올림

    draw.text((rel_x, rel_y), text, font=fnt, fill=color_rgb)
    frame[y1:y2, x1:x2] = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def _put_unicode_text_block(frame: np.ndarray, lines: list,
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
        try:
            _, lh = fnt.getsize("A")
            line_h = lh + line_gap
        except Exception:
            line_h = font_size + line_gap

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
                CREATE TABLE IF NOT EXISTS macro_slots (
                    id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, steps_json TEXT,
                    sort_order INTEGER DEFAULT 0);
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

    # ★ v10: 매크로 슬롯 저장/복원
    def save_macro_slots(self, slots: list):
        import json as _j
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM macro_slots")
                c.executemany(
                    "INSERT INTO macro_slots(title,steps_json,sort_order) VALUES(?,?,?)",
                    [(s.get('title','슬롯'), _j.dumps(s.get('steps',[]), ensure_ascii=False), i)
                     for i, s in enumerate(slots)])

    def load_macro_slots(self) -> list:
        import json as _j
        with self._lock:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT title,steps_json FROM macro_slots ORDER BY sort_order"
                ).fetchall()
        result = []
        for title, sj in rows:
            try:    steps = _j.loads(sj) if sj else []
            except: steps = []
            result.append({'title': title, 'steps': steps})
        return result


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

    # ★ v9: actions 리스트로 확장 — 녹화/매크로 조합 가능
    # actions 예시: [('rec_start',), ('rec_stop',), ('macro_run', repeat, gap)]
    def __init__(self, start_dt, stop_dt, actions=None):
        ScheduleEntry._cnt += 1
        self.id = ScheduleEntry._cnt
        self.start_dt = start_dt
        self.stop_dt  = stop_dt
        self.actions  = actions or ['rec_start', 'rec_stop']  # 기본: 녹화 시작/종료
        self.started  = False
        self.stopped  = False
        self.done     = False
        # 매크로 예약용 추가 필드
        self.macro_repeat   = 1
        self.macro_gap      = 1.0
        self.macro_run_done = False

    def label(self):
        s = self.start_dt.strftime("%m/%d %H:%M:%S") if self.start_dt else "—"
        e = self.stop_dt.strftime("%m/%d %H:%M:%S")  if self.stop_dt  else "—"
        acts = "+".join(self.actions) if isinstance(self.actions, list) else str(self.actions)
        return f"#{self.id}  {s} → {e}  [{acts}]"


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
        self.start_time = None
        self.output_dir = ""

        self._screen_thread = None
        self._camera_thread = None
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

        self.screen_writer = None
        self.camera_writer = None
        self._writer_lock = threading.Lock()

        self.segment_duration = 30 * 60
        self.current_segment_start = None
        self._scr_frame_idx = 0
        self._cam_frame_idx = 0
        self._seg_start_time = 0.0

        self.screen_rois  = []; self.camera_rois  = []
        self.screen_roi_avg  = []; self.camera_roi_avg  = []
        self.screen_roi_prev = []; self.camera_roi_prev = []
        self.screen_overall_avg = np.zeros(3)
        self.camera_overall_avg = np.zeros(3)

        self.brightness_threshold      = 30.0
        self.blackout_cooldown         = 5.0
        self.screen_last_blackout_time = 0.0
        self.camera_last_blackout_time = 0.0
        self.screen_blackout_count = 0
        self.camera_blackout_count = 0
        self.screen_blackout_events = []
        self.camera_blackout_events = []
        self.blackout_dir = os.path.join(BASE_DIR, "blackout")

        self.buffer_seconds = 30
        self._screen_buffer: deque = deque()
        self._camera_buffer: deque = deque()
        self._buf_lock = threading.Lock()

        self.memo_texts: list = [""]
        self.memo_overlays: list = [
            MemoOverlayConfig(0, "bottom-right", "both", True)
        ]
        self.overlay_font_size: int = 18   # ★ v11: 영상 오버레이 글자 크기

        self.auto_click_enabled  = False
        self.auto_click_interval = 1.0
        self.auto_click_count    = 0
        self._ac_thread = None
        self._ac_stop   = threading.Event()

        self.schedules: list = []
        self.playback_speed = 1.0

        # ★ v9: 영상 압축 설정
        self.video_codec     = "mp4v"   # mp4v / avc1 / xvid
        self.video_scale     = 1.0      # 1.0=원본 / 0.75 / 0.5
        self.video_quality   = 95       # JPEG 계열 품질 (미사용 시 참고용)

        self.camera_list: list = []
        self.active_camera_idx = 0

        self.macro_steps   = []
        self.macro_running   = False
        self.macro_recording = False
        self.macro_repeat    = 1
        self.macro_loop_gap  = 1.0
        self._macro_thread  = None
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
        self._manual_trigger_time = 0.0

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

    def _get_fourcc(self):
        """★ v9: 코덱 문자열 → fourcc"""
        codec = self.video_codec
        if codec == "avc1":
            return cv2.VideoWriter_fourcc(*'avc1')
        elif codec == "xvid":
            return cv2.VideoWriter_fourcc(*'XVID')
        else:
            return cv2.VideoWriter_fourcc(*'mp4v')

    def _scale_frame(self, frame: np.ndarray) -> np.ndarray:
        """★ v9: 해상도 스케일 적용"""
        if self.video_scale >= 1.0:
            return frame
        h, w = frame.shape[:2]
        nw = max(2, int(w * self.video_scale))
        nh = max(2, int(h * self.video_scale))
        return cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)

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
        post = []; deadline = time.time() + 11.0
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
        wr = cv2.VideoWriter(vpath, self._get_fourcc(), fps, (w, h))
        for i, f in enumerate(all_frames):
            fc = self._scale_frame(f.copy())
            if i == bi: cv2.rectangle(fc, (4,4),(fc.shape[1]-4,fc.shape[0]-4),(0,0,255),6)
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
        post_frames = []; deadline = time.time() + post_s + 2.0
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
        wr    = cv2.VideoWriter(vpath, self._get_fourcc(), fps, (w, h))
        trigger_str = datetime.fromtimestamp(trigger).strftime("%H:%M:%S")
        for i, f in enumerate(all_frames):
            fc = self._scale_frame(f.copy())
            ov_ts = datetime.fromtimestamp(
                trigger + (i - bi) / fps).strftime("%H:%M:%S.")
            ov_ts += f"{int(((trigger + (i-bi)/fps) % 1)*1000):03d}"
            _draw_time_overlay(fc, ov_ts,
                               f"{'PRE' if i < bi else 'POST'}  {abs(i-bi)/fps:+.2f}s")
            if i == bi:
                cv2.rectangle(fc,(4,4),(fc.shape[1]-4,fc.shape[0]-4),(0,200,255),5)
                cv2.putText(fc, f"MANUAL CLIP  {trigger_str}",
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
        h, w = frame.shape[:2]
        for cfg in self.memo_overlays:
            if not cfg.enabled: continue
            if cfg.target != "both" and cfg.target != target_source: continue
            if cfg.tab_idx >= len(self.memo_texts): continue
            text = self.memo_texts[cfg.tab_idx].strip()
            if not text: continue
            lines = text.splitlines()
            if lines:
                _draw_memo_block(frame, lines, cfg.position, w, h,
                                 font_size=self.overlay_font_size)  # ★ v11
        return frame

    def _add_overlay(self, frame: np.ndarray, rois: list, source: str) -> np.ndarray:
        if self.recording and self.start_time:
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond//1000:03d}"
            e  = time.time() - self.start_time
            hh = int(e//3600); mm = int((e%3600)//60); ss = int(e%60); ms = int((e%1)*1000)
            _draw_time_overlay(frame, now_str, f"REC  {hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}")
        self.apply_memo_overlays(frame, source)
        for i, (rx, ry, rw, rh) in enumerate(rois):
            cv2.rectangle(frame, (rx,ry),(rx+rw,ry+rh),(0,0,255),2)
            cv2.putText(frame, f"ROI{i+1}", (rx, max(ry-5,15)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45,(0,0,255),1,cv2.LINE_AA)
        return frame

    @staticmethod
    def stamp_preview(frame: np.ndarray, engine: "RecorderEngine", source: str) -> np.ndarray:
        out = frame.copy()
        if engine.recording and engine.start_time is not None:
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond//1000:03d}"
            e  = time.time() - engine.start_time
            hh = int(e//3600); mm = int((e%3600)//60); ss = int(e%60); ms = int((e%1)*1000)
            _draw_time_overlay(out, now_str, f"REC  {hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}")
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
        fourcc  = self._get_fourcc()
        if self.screen_recording_enabled:
            with mss.mss() as sct:
                mon = sct.monitors[2 if len(sct.monitors) > 2 else 1]
                spath = os.path.join(self.output_dir, f"screen_{seg_ts}.mp4")
            sw = max(2, int(mon['width']  * self.video_scale))
            sh = max(2, int(mon['height'] * self.video_scale))
            with self._writer_lock:
                self.screen_writer = cv2.VideoWriter(
                    spath, fourcc, scr_fps, (sw, sh))
            self.signals.status_message.emit(f"Screen seg: {spath}")
        with self._buf_lock:
            cframe = self._camera_buffer[-1] if self._camera_buffer else None
        if cframe is not None:
            h, w = cframe.shape[:2]
            cw = max(2, int(w * self.video_scale))
            ch = max(2, int(h * self.video_scale))
            cpath = os.path.join(self.output_dir, f"camera_{seg_ts}.mp4")
            with self._writer_lock:
                self.camera_writer = cv2.VideoWriter(
                    cpath, fourcc, cam_fps, (cw, ch))
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
                        scaled  = self._scale_frame(stamped)
                        self._scr_frame_idx = self._write_frame_sync(
                            w, scaled, self.actual_screen_fps,
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
                    scaled  = self._scale_frame(stamped)
                    self._cam_frame_idx = self._write_frame_sync(
                        w, scaled, cam_fps, self._cam_frame_idx, elapsed)
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
        if not self.recording: return
        self.recording = False
        time.sleep(0.35)
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

        # ★ v13: 훅 스레드 완전 분리용 큐
        # on_click 콜백은 이 큐에 데이터를 넣기만 하고 즉시 반환
        # → 훅 스레드가 절대 블로킹되지 않아 deadlock 원천 차단
        self._macro_click_q: queue.Queue = queue.Queue()

        def _delayed():
            time.sleep(0.5)
            if not self.macro_recording: return
            self._macro_listen_active_ts = time.time()
            self._macro_last_ts = self._macro_listen_active_ts

            # ★ v13: 훅 스레드에서 실행되는 콜백 — 최대한 가볍게
            # Qt 시그널 emit, append 등 일체 금지
            # queue.Queue.put_nowait()만 호출하고 즉시 반환
            def on_click(x, y, button, pressed):
                if not pressed or button != pynput_mouse.Button.left: return
                if not self.macro_recording: return
                now = time.time()
                if now < self._macro_listen_active_ts: return
                # nowait으로 블로킹 없이 큐에 삽입 후 즉시 반환
                try:
                    self._macro_click_q.put_nowait((int(x), int(y), now))
                except Exception:
                    pass  # 큐 full이면 그냥 버림 (훅 스레드는 절대 블로킹 안 함)

            # ★ v13: 큐에서 꺼내 시그널 emit하는 별도 데몬 스레드
            # 이 스레드는 일반 Python 스레드 → PyQt 시그널 emit 안전
            def _emit_loop():
                while self.macro_recording or not self._macro_click_q.empty():
                    try:
                        x, y, now = self._macro_click_q.get(timeout=0.05)
                    except Exception:
                        continue
                    delay = round(now - self._macro_last_ts, 3)
                    self._macro_last_ts = now
                    step = ClickStep(x, y, delay)
                    self.macro_steps.append(step)
                    # 일반 Python 스레드에서의 시그널 emit → 안전
                    self.signals.macro_step_recorded.emit(x, y, delay)

            threading.Thread(target=_emit_loop, daemon=True).start()

            self._macro_listener = pynput_mouse.Listener(on_click=on_click)
            self._macro_listener.start()
            self.signals.status_message.emit("매크로 기록 중")

        threading.Thread(target=_delayed, daemon=True).start()

    def macro_stop_recording(self):
        self.macro_recording = False
        def _stop():
            time.sleep(0.2)   # emit_loop가 큐를 비울 시간
            if self._macro_listener:
                self._macro_listener.stop()
                self._macro_listener = None
        threading.Thread(target=_stop, daemon=True).start()

    def macro_start_run(self, repeat=None, gap=None):
        if not PYNPUT_AVAILABLE or self.macro_running or not self.macro_steps: return
        if repeat is not None: self.macro_repeat = repeat
        if gap    is not None: self.macro_loop_gap = gap
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

    # ── 예약 (★ v9 확장: 매크로 액션 지원) ────────────────────────────────────
    def schedule_tick(self) -> list:
        now = datetime.now(); actions = []
        for s in list(self.schedules):
            if s.done: continue
            # 시작 시각 처리
            if s.start_dt and not s.started:
                delta = (s.start_dt - now).total_seconds()
                if -2 <= delta <= 1:
                    s.started = True
                    # 녹화 시작 액션
                    if 'rec_start' in s.actions and not self.recording:
                        actions.append(('start', s))
                    # 매크로 실행 액션 (시작 시각에)
                    if 'macro_run' in s.actions:
                        actions.append(('macro_run', s))
            # 종료 시각 처리
            if s.stop_dt and s.started and not s.stopped:
                delta = (s.stop_dt - now).total_seconds()
                if -2 <= delta <= 1:
                    s.stopped = True; s.done = True
                    if 'rec_stop' in s.actions and self.recording:
                        actions.append(('stop', s))
                    # 매크로 실행 액션 (종료 시각에, 아직 안 실행됐을 때)
                    if 'macro_run_at_stop' in s.actions and not s.macro_run_done:
                        actions.append(('macro_run', s))
            # 시작만 있고 종료 없는 경우
            if s.started and not s.stop_dt and not s.done: s.done = True
        return actions

    # ── 엔진 시작/정지 ────────────────────────────────────────────────────────
    def start(self):
        self.running = True
        threading.Thread(target=self.scan_cameras, daemon=True).start()
        self.start_screen_thread()
        self.start_camera_thread()

    def stop(self):
        if self.recording:
            self.stop_recording()
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
    ov = frame.copy()
    cv2.rectangle(ov, (4,4),(440,78),(0,0,0),-1)
    cv2.addWeighted(ov, 0.45, frame, 0.55, 0, frame)
    cv2.putText(frame, now_str,     (10,32), cv2.FONT_HERSHEY_SIMPLEX,0.72,(0,255,80),  2,cv2.LINE_AA)
    cv2.putText(frame, elapsed_str, (10,68), cv2.FONT_HERSHEY_SIMPLEX,0.65,(80,220,255),2,cv2.LINE_AA)


def _draw_memo_block(frame: np.ndarray, lines: list,
                     position: str, fw: int, fh: int,
                     font_size: int = 18) -> None:
    """
    ★ v9: PIL 전체 프레임 방식으로 한글 ㅁㅁㅁ 완전 해결
    배경 박스는 OpenCV로, 텍스트만 PIL로 렌더링
    """
    if not lines: return
    fnt = _get_font(font_size)
    pad = 8

    # 줄 높이
    try:
        sample_bbox = fnt.getbbox("A가") if fnt else None
        line_h = (sample_bbox[3] - sample_bbox[1] + 6) if sample_bbox else font_size + 6
    except Exception:
        line_h = font_size + 6

    # 각 줄 실제 폭 측정
    max_tw = 0
    for line in lines:
        try:
            bb = fnt.getbbox(line) if fnt else None
            tw = (bb[2] - bb[0]) if bb else len(line) * (font_size // 2 + 2)
        except Exception:
            tw = len(line) * (font_size // 2 + 2)
        max_tw = max(max_tw, tw)

    inner_pad = 12
    bg_pad    = 4
    box_w = max_tw + inner_pad * 2
    box_h = len(lines) * line_h + 14
    box_w = min(box_w, fw - bg_pad * 2 - pad * 2)

    # 위치 계산
    if position == "top-left":
        x0, y0 = pad, pad + 30
    elif position == "top-right":
        x0 = fw - box_w - pad - bg_pad
        y0 = pad + 30
    elif position == "bottom-left":
        x0 = pad
        y0 = fh - box_h - pad - bg_pad
    elif position == "center":
        x0 = (fw - box_w) // 2
        y0 = (fh - box_h) // 2
    else:  # bottom-right
        x0 = fw - box_w - pad - bg_pad
        y0 = fh - box_h - pad - bg_pad

    x0 = max(bg_pad, min(x0, fw - box_w - bg_pad))
    y0 = max(bg_pad, min(y0, fh - box_h - bg_pad))

    # 반투명 배경 박스 (OpenCV)
    bx1 = max(0, x0 - bg_pad)
    by1 = max(0, y0 - bg_pad)
    bx2 = min(fw, x0 + box_w + bg_pad)
    by2 = min(fh, y0 + box_h + bg_pad)
    ov = frame.copy()
    cv2.rectangle(ov, (bx1, by1), (bx2, by2), (0, 0, 0), -1)
    cv2.addWeighted(ov, 0.55, frame, 0.45, 0, frame)

    # ★ v9: 텍스트 전체를 한 번에 PIL로 렌더링 (ROI 슬라이싱 없이 전체 프레임 처리)
    if PIL_AVAILABLE and fnt:
        # 전체 프레임을 PIL로 변환
        pil_img = _PIL_Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw    = _PIL_Draw.Draw(pil_img)
        for j, line in enumerate(lines):
            if not line: continue
            ty = y0 + j * line_h
            tx = x0 + inner_pad - 6
            # 프레임 경계 체크
            if ty > fh - bg_pad: break
            draw.text((tx, ty), line, font=fnt, fill=(100, 240, 255))
        # 다시 numpy로 변환
        frame[:] = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    else:
        # PIL 없을 때 fallback
        for j, line in enumerate(lines):
            cy = y0 + j * line_h + line_h
            if cy > fh - bg_pad: break
            cv2.putText(frame, line, (x0 + inner_pad, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100, 240, 255), 1, cv2.LINE_AA)


# =============================================================================
#  PreviewLabel
# =============================================================================
class PreviewLabel(QLabel):
    roi_changed = pyqtSignal()
    def __init__(self, source: str, engine, parent=None):
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
#  CameraWindow
# =============================================================================
class CameraWindow(QDialog):
    def __init__(self, engine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self.setWindowTitle("📷  Camera Feed")
        self.setWindowFlags(Qt.Window|Qt.WindowMinimizeButtonHint|
                            Qt.WindowMaximizeButtonHint|Qt.WindowCloseButtonHint)
        self.resize(680,560); self.setMinimumSize(420,300)
        self.setStyleSheet("background:#0d0d1e;color:#ddd;")

        root = QVBoxLayout(self); root.setSpacing(0); root.setContentsMargins(0,0,0,0)

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

        pc = QWidget(); pc.setStyleSheet("background:#0d0d1e;")
        pl = QVBoxLayout(pc); pl.setContentsMargins(6,4,6,4); pl.setSpacing(3)
        self._lbl = PreviewLabel("camera", self.engine)
        pl.addWidget(self._lbl, 1)
        hint = QLabel("Left-drag: add ROI  |  Right-click: remove")
        hint.setStyleSheet("color:#444;font-size:10px;"); hint.setAlignment(Qt.AlignCenter)
        pl.addWidget(hint)
        root.addWidget(pc, 1)

        QTimer(self, timeout=self._upd_fps, interval=2000).start()
        threading.Thread(target=self._bg_scan, daemon=True).start()

    def _on_fold_toggle(self, folded: bool):
        self._cam_panel.setVisible(not folded)
        self._fold_btn.setText("▼ 카메라 선택 표시" if folded else "▲ 카메라 선택 숨기기")
        QTimer.singleShot(10, self._fit_window)

    def _fit_window(self):
        self.adjustSize()
        min_h = 300 if self._fold_btn.isChecked() else 480
        if self.height() < min_h: self.resize(self.width(), min_h)

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
        self._cam_cbs = {}
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
            cb.blockSignals(True); cb.setChecked(True); cb.blockSignals(False); return
        if idx == self.engine.active_camera_idx: return
        for other_idx, other_cb in self._cam_cbs.items():
            if other_idx != idx:
                other_cb.blockSignals(True); other_cb.setChecked(False); other_cb.blockSignals(False)
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
    scroll_to_key = pyqtSignal(str)
    FEATURES = [
        ("recording",      "⏺  Recording"),
        ("manual_clip",    "🎬  수동 녹화"),
        ("memo",           "📝  메모"),
        ("autoclick",      "🖱  Auto-Click"),
        ("schedule",       "⏰  Schedule"),
        ("macro",          "🎯  Click Macro"),
        ("macro_schedule", "🤖  매크로 예약"),
        ("blackout",       "⚡  Blackout"),
        ("log",            "📋  Log"),
        ("reset",          "🔄  설정 초기화"),
    ]
    _IH=34; _DC="#2a3a5a"; _IC="#12122e"; _HC="#1a2a3a"
    def __init__(self, parent=None):
        super().__init__(parent)
        self._checks={}; self._rows=[]; self._drag_idx=-1
        self._drag_start=QPoint(); self._dragging=False
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
        if e.button() == Qt.LeftButton:
            idx = self._row_at_y(e.pos().y())
            if idx >= 0:
                key, _ = self._rows[idx]
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


# =============================================================================
#  ★ v10: TutorialDialog — 시작 튜토리얼
# =============================================================================
class TutorialDialog(QDialog):
    """
    프로그램 시작 시 한 번씩 표시되는 기능 소개 다이얼로그.
    Skip / 일주일 안보기 / 다시는 안보기 세 가지 옵션.
    """
    SLIDES = [
        ("⏺  Recording",
         "녹화 시작/종료 단축키: Ctrl+Alt+W / E\n\n"
         "• 화면(Screen)과 카메라(Camera)를 동시에 녹화합니다.\n"
         "• 세그먼트 단위(기본 30분)로 자동 분할 저장됩니다.\n"
         "• 저장 배속 조절로 타임랩스·슬로우모션 녹화도 가능합니다.\n"
         "• 🗜 영상 용량 절감: H.264 코덱 또는 해상도 다운스케일을 선택하세요.",
         "#27ae60"),
        ("🎬  수동 녹화",
         "단축키: Ctrl+Alt+M\n\n"
         "• 버튼을 누른 시점 기준 전/후 N초를 버퍼에서 추출해 별도 클립으로 저장합니다.\n"
         "• 전 시간은 메모리 버퍼(최대 30초)에서 가져옵니다.\n"
         "• 화면·카메라 중 원하는 소스만 선택할 수 있습니다.",
         "#e67e22"),
        ("📝  메모장",
         "• 탭을 여러 개 만들어 독립적으로 메모할 수 있습니다.\n"
         "• 좌클릭: 해당 줄 앞에 현재 시각 타임스탬프 자동 삽입\n"
         "• 우클릭: 타임스탬프 제거\n"
         "• 글꼴 크기 슬라이더로 8–36pt 조절 가능\n"
         "• 🖼 오버레이 설정: 메모 내용을 녹화 영상 위에 실시간으로 표시합니다.",
         "#f39c12"),
        ("🖱  Auto-Click",
         "단축키: Ctrl+Alt+A(시작) / S(정지)\n\n"
         "• 설정한 간격(초)마다 마우스 왼쪽 버튼을 자동으로 클릭합니다.\n"
         "• 총 클릭 횟수가 LCD 숫자로 실시간 표시됩니다.",
         "#2980b9"),
        ("🎯  Click Macro",
         "• 슬롯 1·2·3… 처럼 독립적인 매크로를 여러 개 저장할 수 있습니다.\n"
         "• ⏺ 기록 시작 후 화면을 클릭하면 좌표와 딜레이가 자동 기록됩니다.\n"
         "• 반복 횟수 0 = 무한 반복, 루프 간격 설정 가능\n"
         "• 스텝별 딜레이·좌표를 테이블에서 직접 수정할 수 있습니다.",
         "#16a085"),
        ("⏰  Schedule & 🤖 매크로 예약",
         "Schedule\n"
         "• 지정 시각에 녹화 자동 시작/종료합니다.\n"
         "• 예약 시각은 현재 시각보다 과거이면 빨간색으로 경고됩니다.\n\n"
         "매크로 예약\n"
         "• 지정 시각에 녹화 시작 + 매크로 실행 + 녹화 종료를 조합할 수 있습니다.\n"
         "• 여러 예약을 추가해 복수의 자동화 시나리오를 구성하세요.",
         "#8e44ad"),
        ("⚡  Blackout Detection",
         "• ROI(관심 영역)를 드래그해 지정하면, 해당 영역의 밝기가 급격히 떨어질 때 이벤트를 감지합니다.\n"
         "• 블랙아웃 발생 전후 각 10초 클립을 자동 저장합니다.\n"
         "• Threshold(밝기 변화량)와 Cooldown(재감지 최소 간격)을 조절할 수 있습니다.",
         "#e74c3c"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("시작 가이드  —  Screen & Camera Recorder")
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.resize(600, 480)
        self.setStyleSheet(
            "QDialog{background:#0d0d1e;color:#ddd;}"
            "QLabel{color:#ddd;}"
            "QPushButton{background:#1e2a3a;border:1px solid #4a5a7a;border-radius:4px;"
            "padding:5px 14px;color:#ccd;font-size:11px;}"
            "QPushButton:hover{background:#2a3e56;color:#eef;}"
            "QCheckBox{color:#aab;font-size:11px;spacing:6px;}"
            "QCheckBox::indicator{width:15px;height:15px;border:1px solid #4a5a7a;"
            "border-radius:3px;background:#0d0d1e;}"
            "QCheckBox::indicator:checked{background:#2a7aca;border:2px solid #5aaae0;}")
        self.choice = "skip_once"   # 결과값
        self._idx   = 0
        self._build()
        self._show_slide(0)

    def _build(self):
        v = QVBoxLayout(self); v.setSpacing(0); v.setContentsMargins(0,0,0,0)

        # 진행 표시줄 (슬라이드 번호)
        self._prog_lbl = QLabel()
        self._prog_lbl.setAlignment(Qt.AlignCenter)
        self._prog_lbl.setStyleSheet(
            "background:#08081a;color:#556;font-size:10px;padding:6px;border-bottom:1px solid #1a2a3a;")
        v.addWidget(self._prog_lbl)

        # 슬라이드 영역
        slide_w = QWidget(); slide_w.setStyleSheet("background:#0d0d1e;")
        sv = QVBoxLayout(slide_w); sv.setContentsMargins(32, 24, 32, 16); sv.setSpacing(14)

        self._title_lbl = QLabel()
        self._title_lbl.setStyleSheet("font-size:18px;font-weight:bold;")
        sv.addWidget(self._title_lbl)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("border:1px solid #2a2a4a;"); sv.addWidget(sep)

        self._body_lbl = QLabel()
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setStyleSheet("font-size:12px;color:#ccc;line-height:1.7;padding:4px 0;")
        self._body_lbl.setTextFormat(Qt.PlainText)
        sv.addWidget(self._body_lbl, 1)
        v.addWidget(slide_w, 1)

        # 점 네비게이터
        dot_row = QHBoxLayout(); dot_row.setContentsMargins(0,8,0,8)
        dot_row.addStretch()
        self._dots = []
        for i in range(len(self.SLIDES)):
            d = QLabel("●"); d.setFixedWidth(16); d.setAlignment(Qt.AlignCenter)
            d.setStyleSheet("font-size:10px;color:#334;")
            self._dots.append(d); dot_row.addWidget(d)
        dot_row.addStretch()
        v.addLayout(dot_row)

        # 옵션 체크박스
        chk_w = QWidget(); chk_w.setStyleSheet("background:#08081a;border-top:1px solid #1a2a3a;")
        ch = QHBoxLayout(chk_w); ch.setContentsMargins(20,10,20,10); ch.setSpacing(24)
        self._chk_week    = QCheckBox("일주일 동안 안보기")
        self._chk_forever = QCheckBox("다시는 안보기")
        self._chk_week.toggled.connect(lambda c: self._chk_forever.setChecked(False) if c else None)
        self._chk_forever.toggled.connect(lambda c: self._chk_week.setChecked(False) if c else None)
        ch.addStretch()
        ch.addWidget(self._chk_week); ch.addWidget(self._chk_forever)
        ch.addStretch()
        v.addWidget(chk_w)

        # 하단 버튼
        btn_w = QWidget(); btn_w.setStyleSheet("background:#08081a;")
        bh = QHBoxLayout(btn_w); bh.setContentsMargins(16,8,16,14); bh.setSpacing(8)
        self._prev_btn = QPushButton("◀ 이전")
        self._prev_btn.clicked.connect(lambda: self._show_slide(self._idx - 1))
        self._next_btn = QPushButton("다음 ▶")
        self._next_btn.clicked.connect(lambda: self._show_slide(self._idx + 1))
        self._skip_btn = QPushButton("Skip  ✕")
        self._skip_btn.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;border:1px solid #334;"
            "border-radius:4px;padding:5px 14px;font-size:11px;}"
            "QPushButton:hover{background:#2a1a1a;color:#f88;}")
        self._skip_btn.clicked.connect(self._on_skip)
        bh.addWidget(self._prev_btn); bh.addWidget(self._next_btn)
        bh.addStretch(); bh.addWidget(self._skip_btn)
        v.addWidget(btn_w)

    def _show_slide(self, idx: int):
        idx = max(0, min(idx, len(self.SLIDES) - 1))
        self._idx = idx
        title, body, color = self.SLIDES[idx]
        total = len(self.SLIDES)
        self._prog_lbl.setText(f"  {idx+1} / {total}  —  기능 소개")
        self._title_lbl.setText(title)
        self._title_lbl.setStyleSheet(f"font-size:18px;font-weight:bold;color:{color};")
        self._body_lbl.setText(body)
        for i, d in enumerate(self._dots):
            d.setStyleSheet(f"font-size:10px;color:{'#eee' if i==idx else '#334'};")
        self._prev_btn.setEnabled(idx > 0)
        last = (idx == total - 1)
        self._next_btn.setText("완료  ✓" if last else "다음 ▶")
        if last:
            self._next_btn.setStyleSheet(
                "QPushButton{background:#27ae60;color:#fff;border:none;border-radius:4px;"
                "padding:5px 14px;font-size:11px;font-weight:bold;}"
                "QPushButton:hover{background:#2ecc71;}")
            self._next_btn.clicked.disconnect()
            self._next_btn.clicked.connect(self._on_skip)
        else:
            self._next_btn.setStyleSheet("")
            try: self._next_btn.clicked.disconnect()
            except: pass
            self._next_btn.clicked.connect(lambda: self._show_slide(self._idx + 1))

    def _on_skip(self):
        if self._chk_forever.isChecked():
            self.choice = "skip_forever"
        elif self._chk_week.isChecked():
            self.choice = "skip_week"
        else:
            self.choice = "skip_once"
        self.accept()


class FeatureBar(QFrame):
    toggled       = pyqtSignal(str, bool)
    order_changed = pyqtSignal(list)
    scroll_to_key = pyqtSignal(str)
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
        self._list.scroll_to_key.connect(self.scroll_to_key)
        scroll.setWidget(self._list)
        outer.addWidget(scroll)
    def is_enabled(self,key): return self._list.is_enabled(key)
    def current_order(self): return self._list.current_order()


# =============================================================================
#  ★ v9 수정: TimestampMemoEdit — 우클릭 컨텍스트 메뉴 비활성화
# =============================================================================
class TimestampMemoEdit(QPlainTextEdit):
    """
    - 좌클릭: 해당 줄 맨 앞에 타임스탬프 삽입
    - 우클릭: 해당 줄의 타임스탬프 제거 (컨텍스트 메뉴 없음)
    """
    _TS_RE = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timestamp_enabled = True

    @classmethod
    def _ts_pattern(cls):
        if cls._TS_RE is None:
            import re
            cls._TS_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ')
        return cls._TS_RE

    # ★ v9: 우클릭 컨텍스트 메뉴 완전 비활성화
    def contextMenuEvent(self, e):
        e.accept()  # 이벤트 소비 → 메뉴 표시 안 함

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if not self.timestamp_enabled:
            return

        cur = self.textCursor()
        cur.movePosition(QTextCursor.StartOfBlock)
        cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        line_text = cur.selectedText()

        if e.button() == Qt.LeftButton:
            cur.movePosition(QTextCursor.StartOfBlock)
            m = self._ts_pattern().match(line_text)
            if m:
                sel_cur = self.textCursor()
                sel_cur.movePosition(QTextCursor.StartOfBlock)
                sel_cur.movePosition(QTextCursor.Right,
                                     QTextCursor.KeepAnchor, m.end())
                sel_cur.insertText(datetime.now().strftime("[%Y-%m-%d %H:%M:%S] "))
                self.setTextCursor(sel_cur)
            else:
                start_cur = self.textCursor()
                start_cur.movePosition(QTextCursor.StartOfBlock)
                start_cur.insertText(datetime.now().strftime("[%Y-%m-%d %H:%M:%S] "))
                self.setTextCursor(start_cur)

        elif e.button() == Qt.RightButton:
            m = self._ts_pattern().match(line_text)
            if m:
                sel_cur = self.textCursor()
                sel_cur.movePosition(QTextCursor.StartOfBlock)
                sel_cur.movePosition(QTextCursor.Right,
                                     QTextCursor.KeepAnchor, m.end())
                sel_cur.removeSelectedText()
                self.setTextCursor(sel_cur)


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
        self.setWindowTitle("Screen & Camera Recorder  v13.0")
        self.resize(1440, 920); self.setStyleSheet(self._dark_style())

        self.signals  = Signals()
        self.engine   = RecorderEngine(self.signals)
        self.db       = SettingsDB()
        self._cam_win = CameraWindow(self.engine, self.signals, self)

        self._sections: dict = {}
        self._overlay_rows: list = []
        self._memo_editors: list = []

        # ★ v9: 매크로 예약 목록
        self._macro_schedules: list = []

        # ★ v10: 매크로 슬롯 (탭처럼 복수 저장)
        self._macro_slots: list = []          # [{'title':str, 'steps':[ClickStep,...]}]
        self._active_mac_slot: int = 0        # 현재 활성 슬롯 인덱스

        self._led_state = False
        self._led_timer = QTimer(self); self._led_timer.timeout.connect(self._blink_led)

        # ★ v10: DateTimeEdit 실시간 카운트다운 타이머 (해당 섹션 ON시만)
        self._dt_live_timer = QTimer(self)
        self._dt_live_timer.setInterval(1000)
        self._dt_live_timer.timeout.connect(self._on_dt_live_tick)

        self._build_ui()
        self._connect_signals()
        self._load_settings()

        QTimer(self, timeout=self._refresh_ui,        interval=500  ).start()
        QTimer(self, timeout=self._update_fps,         interval=2000 ).start()
        QTimer(self, timeout=self._check_segment,      interval=5000 ).start()
        QTimer(self, timeout=self._tick_schedule,      interval=1000 ).start()
        QTimer(self, timeout=self._pump_preview,       interval=33   ).start()
        QTimer(self, timeout=self._auto_save_settings, interval=10000).start()
        QTimer(self, timeout=self._poll_manual_state,  interval=200  ).start()

        self._setup_hotkeys()
        self.engine.start()

        QTimer.singleShot(800,  self._check_font_on_startup)
        QTimer.singleShot(1200, self._show_tutorial_if_needed)   # ★ v10

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

        # ── 오른쪽: 컨트롤 패널 ───────────────────────────────────────────
        rw=QWidget(); rw.setMinimumWidth(320)
        rv=QVBoxLayout(rw); rv.setContentsMargins(4,0,0,0); rv.setSpacing(0)
        pt=QLabel("⚙  Control Panel"); pt.setStyleSheet("color:#ccc;font-size:13px;font-weight:bold;padding:8px 10px;background:#1a1a3a;border-bottom:1px solid #334;"); rv.addWidget(pt)
        self._feat_bar=FeatureBar()
        self._feat_bar.toggled.connect(self._on_feature_toggle)
        self._feat_bar.order_changed.connect(self._on_feature_order)
        self._feat_bar.scroll_to_key.connect(self._on_scroll_to_section)
        rv.addWidget(self._feat_bar)

        self._panel_scroll=QScrollArea()
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

        add_sec("recording",      "⏺  Recording",          "#27ae60", self._build_recording_grp)
        add_sec("manual_clip",    "🎬  수동 녹화",          "#e67e22", self._build_manual_grp)
        add_sec("memo",           "📝  메모장",             "#f39c12", self._build_memo_grp)
        add_sec("autoclick",      "🖱  Auto-Click",         "#2980b9", self._build_autoclick_grp)
        add_sec("schedule",       "⏰  Schedule",           "#8e44ad", self._build_schedule_grp)
        add_sec("macro",          "🎯  Click Macro",        "#16a085", self._build_macro_grp)
        add_sec("macro_schedule", "🤖  매크로 예약",        "#00b894", self._build_macro_schedule_grp)
        add_sec("blackout",       "⚡  Blackout Detection", "#e74c3c", self._build_blackout_grp)
        add_sec("log",            "📋  Log",                "#7f8c8d", self._build_log_grp)
        add_sec("reset",          "🔄  설정 초기화",        "#636e72", self._build_reset_grp)
        self._panel_l.addStretch()
        self._panel_scroll.setWidget(self._panel_w)
        rv.addWidget(self._panel_scroll,1)

        splitter.addWidget(left_w)
        splitter.addWidget(rw)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([960, 440])

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
            "QPushButton:pressed{background:#1a8a46;}"
            "QPushButton:disabled{background:#1a3a28;color:#4a7a5a;}")
        self._btn_start.clicked.connect(self._on_start_rec)
        self._btn_stop=QPushButton("⏹  Stop Recording  [Ctrl+Alt+E]")
        self._btn_stop.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#e74c3c;}"
            "QPushButton:disabled{background:#3a1a1a;color:#7a4a4a;}")
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
        v.addWidget(fps_g)

        # ★ v9: 영상 용량 절감 설정
        comp_g = QGroupBox("🗜  영상 용량 절감"); cg = QGridLayout(comp_g); cg.setSpacing(8)
        cg.addWidget(QLabel("코덱:"), 0, 0)
        self._codec_combo = QComboBox()
        self._codec_combo.addItems(["mp4v (기본)", "avc1 / H.264 (소형)", "xvid (호환성)"])
        self._codec_combo.setStyleSheet("QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;padding:2px;border-radius:3px;}")
        self._codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        cg.addWidget(self._codec_combo, 0, 1)

        cg.addWidget(QLabel("해상도 스케일:"), 1, 0)
        self._scale_combo = QComboBox()
        self._scale_combo.addItems(["100% (원본)", "75%", "50%"])
        self._scale_combo.setStyleSheet("QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;padding:2px;border-radius:3px;}")
        self._scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        cg.addWidget(self._scale_combo, 1, 1)

        comp_hint = QLabel(
            "• H.264는 mp4v 대비 ~3–5배 작은 파일 크기\n"
            "• 해상도 50%는 파일 크기 ~75% 감소\n"
            "• H.264 지원 여부는 OpenCV 빌드에 따라 다름")
        comp_hint.setStyleSheet("color:#556;font-size:10px;padding:2px;")
        comp_hint.setWordWrap(True)
        cg.addWidget(comp_hint, 2, 0, 1, 2)
        v.addWidget(comp_g)

        return w

    def _build_manual_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        info=QLabel("버튼을 누르면 현재 시점 기준 전/후 N초를 버퍼에서 추출해 클립을 저장합니다.")
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

    # ★ v9: 매크로 예약 섹션 빌더
    def _build_macro_schedule_grp(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        info = QLabel(
            "녹화 예약과 독립적으로 매크로 실행을 시간 예약할 수 있습니다.\n"
            "예: '녹화 시작 → 매크로 실행 → 녹화 종료'를 하나의 예약으로 구성할 수 있습니다.\n"
            "매크로는 먼저 [클릭 매크로] 섹션에서 기록해야 합니다."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#778;font-size:10px;padding:2px;background:#0d0d1e;border:1px solid #1a2a3a;border-radius:4px;")
        v.addWidget(info)

        inp = QGroupBox("새 매크로 예약 추가"); ig = QVBoxLayout(inp); ig.setSpacing(8)

        def dt_edit(color, border):
            dte = QDateTimeEdit(); dte.setDisplayFormat("yyyy-MM-dd  HH:mm:ss")
            dte.setCalendarPopup(True); dte.setMinimumHeight(30)
            dte.setStyleSheet(
                f"QDateTimeEdit{{background:#1a1a3a;color:{color};"
                f"border:1px solid {border};border-radius:4px;padding:4px 6px;"
                "font-size:12px;font-family:monospace;}}"
                "QDateTimeEdit::drop-down{border:none;}"
                "QDateTimeEdit::down-arrow{image:none;width:0;}")
            return dte

        # 실행 시각
        ig.addWidget(QLabel("⏰  매크로 실행 시각"))
        ms_row = QHBoxLayout(); ms_row.setSpacing(6)
        self._ms_dt = dt_edit("#00b894", "#1a6a5a")
        self._ms_dt.setDateTime(QDateTime.currentDateTime().addSecs(300))
        ms_now_btn = QPushButton("지금"); ms_now_btn.setFixedSize(46,30)
        ms_now_btn.clicked.connect(lambda: self._ms_dt.setDateTime(QDateTime.currentDateTime()))
        ms_row.addWidget(self._ms_dt, 1); ms_row.addWidget(ms_now_btn)
        ig.addLayout(ms_row)

        # 액션 선택
        ig.addWidget(QLabel("🎬  실행 액션 선택"))
        act_row = QHBoxLayout(); act_row.setSpacing(8)
        self._ms_act_rec_start = QCheckBox("녹화 시작")
        self._ms_act_rec_start.setChecked(True)
        self._ms_act_macro     = QCheckBox("매크로 실행")
        self._ms_act_macro.setChecked(True)
        self._ms_act_rec_stop  = QCheckBox("녹화 종료")
        self._ms_act_rec_stop.setChecked(False)
        for chk in [self._ms_act_rec_start, self._ms_act_macro, self._ms_act_rec_stop]:
            chk.setStyleSheet("font-size:11px;color:#dde;")
            act_row.addWidget(chk)
        act_row.addStretch()
        ig.addLayout(act_row)

        # 종료 시각 (녹화 종료 예약)
        stop_row = QHBoxLayout(); stop_row.setSpacing(6)
        self._ms_stop_chk = QCheckBox("녹화 종료 시각 별도 지정")
        self._ms_stop_chk.setChecked(False)
        self._ms_stop_dt = dt_edit("#e74c3c", "#6a2a2a")
        self._ms_stop_dt.setDateTime(QDateTime.currentDateTime().addSecs(3900))
        self._ms_stop_dt.setEnabled(False)
        self._ms_stop_chk.toggled.connect(self._ms_stop_dt.setEnabled)
        ms_stop_now = QPushButton("지금"); ms_stop_now.setFixedSize(46,30)
        ms_stop_now.clicked.connect(lambda: self._ms_stop_dt.setDateTime(QDateTime.currentDateTime()))
        stop_row.addWidget(self._ms_stop_chk); stop_row.addWidget(self._ms_stop_dt, 1); stop_row.addWidget(ms_stop_now)
        ig.addLayout(stop_row)

        # 매크로 반복/간격
        rep_row = QHBoxLayout(); rep_row.setSpacing(8)
        rep_row.addWidget(QLabel("반복 횟수:"))
        self._ms_rep = QSpinBox(); self._ms_rep.setRange(0, 9999); self._ms_rep.setValue(1)
        self._ms_rep.setSpecialValueText("∞"); self._ms_rep.setFixedWidth(70)
        rep_row.addWidget(self._ms_rep)
        rep_row.addWidget(QLabel("루프 간격(s):"))
        self._ms_gap = QDoubleSpinBox(); self._ms_gap.setRange(0, 60); self._ms_gap.setValue(1.0)
        self._ms_gap.setFixedWidth(70)
        rep_row.addWidget(self._ms_gap)
        rep_row.addStretch()
        ig.addLayout(rep_row)

        add_btn = QPushButton("＋  매크로 예약 추가")
        add_btn.setMinimumHeight(32)
        add_btn.setStyleSheet(
            "QPushButton{background:#1a4a3a;color:#afffef;border:1px solid #2a8a7a;"
            "border-radius:4px;font-weight:bold;font-size:12px;}"
            "QPushButton:hover{background:#225a4a;}")
        add_btn.clicked.connect(self._on_macro_sched_add)
        ig.addWidget(add_btn)
        v.addWidget(inp)

        # 예약 목록 테이블
        lst_g = QGroupBox("매크로 예약 목록"); ll = QVBoxLayout(lst_g); ll.setSpacing(4)
        self._ms_tbl = QTableWidget(0, 5)
        self._ms_tbl.setHorizontalHeaderLabels(["#", "실행 시각", "액션", "반복", "상태"])
        self._ms_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._ms_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._ms_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._ms_tbl.setFixedHeight(160)
        self._ms_tbl.setStyleSheet(
            "QTableWidget{background:#0d0d1e;color:#ccc;font-size:11px;"
            "border:1px solid #334;gridline-color:#223;}"
            "QHeaderView::section{background:#1a1a3a;color:#9ab;font-size:11px;"
            "border:none;padding:4px;}")
        ll.addWidget(self._ms_tbl)
        del_row = QHBoxLayout()
        del_btn = QPushButton("선택 삭제"); del_btn.setFixedHeight(26)
        del_btn.clicked.connect(self._on_ms_del)
        clr_btn = QPushButton("전체 삭제"); clr_btn.setFixedHeight(26)
        clr_btn.clicked.connect(self._on_ms_clear)
        del_row.addWidget(del_btn); del_row.addWidget(clr_btn)
        ll.addLayout(del_row)
        v.addWidget(lst_g)

        # 카운트다운
        cd_g = QGroupBox("다음 예약까지"); cl = QVBoxLayout(cd_g)
        self._ms_cd_lbl = QLabel("예약 없음")
        self._ms_cd_lbl.setStyleSheet("color:#00b894;font-family:monospace;font-size:12px;")
        cl.addWidget(self._ms_cd_lbl)
        v.addWidget(cd_g)

        return w

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
        self._btn_ac_start.setStyleSheet("QPushButton{background:#2980b9;color:white;font-size:12px;padding:7px;border-radius:5px;border:none;font-weight:bold;}QPushButton:hover{background:#3498db;}QPushButton:disabled{background:#1a2a3a;color:#4a6a8a;}")
        self._btn_ac_start.clicked.connect(self._on_ac_start)
        self._btn_ac_stop=QPushButton("■  Stop Auto-Click  [Ctrl+Alt+S]")
        self._btn_ac_stop.setStyleSheet("QPushButton{background:#5a6a7a;color:white;font-size:12px;padding:7px;border-radius:5px;border:none;font-weight:bold;}QPushButton:hover{background:#7f8c8d;}QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self._btn_ac_stop.clicked.connect(self._on_ac_stop); self._btn_ac_stop.setEnabled(False)
        self._ac_status=QLabel("● STOPPED"); self._ac_status.setStyleSheet("color:#e74c3c;font-weight:bold;")
        ctl.addWidget(self._btn_ac_start); ctl.addWidget(self._btn_ac_stop); ctl.addWidget(self._ac_status); v.addWidget(ctrl); return w

    def _build_macro_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ★ v10: 슬롯 선택 영역
        sg=QGroupBox("🗂  매크로 슬롯  (메모탭처럼 여러 세트 저장)"); sl=QVBoxLayout(sg); sl.setSpacing(6)
        sc=QHBoxLayout(); sc.setSpacing(6)
        sc.addWidget(QLabel("슬롯:"))
        self._mac_slot_combo=QComboBox()
        self._mac_slot_combo.setStyleSheet("QComboBox{background:#0f2a1a;color:#afffcf;border:1px solid #2a8a5a;border-radius:3px;font-weight:bold;font-size:11px;min-width:100px;}")
        self._mac_slot_combo.currentIndexChanged.connect(self._on_mac_slot_changed)
        sc.addWidget(self._mac_slot_combo,1)
        for lbl,fn,st in [
            ("＋","_on_mac_slot_add","background:#1a4a2a;color:#8fa;border:1px solid #2a6a2a;"),
            ("－","_on_mac_slot_del","background:#3a1a1a;color:#f88;border:1px solid #6a2a2a;"),
            ("✏","_on_mac_slot_rename","background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"),
        ]:
            b=QPushButton(lbl); b.setFixedSize(28,26)
            b.setStyleSheet(f"QPushButton{{{st}border-radius:3px;font-size:12px;}}QPushButton:hover{{filter:brightness(1.3);}}")
            b.clicked.connect(getattr(self,fn)); sc.addWidget(b)
        sl.addLayout(sc)
        self._mac_slot_info=QLabel("슬롯 1/1  |  0 스텝")
        self._mac_slot_info.setStyleSheet("color:#556;font-size:10px;font-family:monospace;")
        sl.addWidget(self._mac_slot_info)
        v.addWidget(sg)

        info=QLabel("기록 시작 후 화면을 클릭하면 좌표+딜레이가 자동 기록됩니다. (현재 활성 슬롯에 저장)"); info.setWordWrap(True); info.setStyleSheet("color:#778;font-size:10px;"); v.addWidget(info)
        rg=QGroupBox("📍  좌표 기록"); rl=QVBoxLayout(rg); rl.setSpacing(6)
        rb=QHBoxLayout()
        self._mac_rec_btn=QPushButton("⏺  기록 시작"); self._mac_rec_btn.setCheckable(True); self._mac_rec_btn.setFixedHeight(30)
        self._mac_rec_btn.setStyleSheet(
            "QPushButton{background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;border-radius:5px;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:#226a3a;}"
            "QPushButton:checked{background:#c0392b;color:#fff;border:2px solid #e74c3c;}"
            "QPushButton:checked:hover{background:#e74c3c;}")
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
        bkr=QHBoxLayout(); bkr.addWidget(QLabel("전체 딜레이:"))
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
        self._mac_run_btn.setStyleSheet("QPushButton{background:#2980b9;color:#fff;border:none;border-radius:5px;font-size:12px;font-weight:bold;}QPushButton:hover{background:#3498db;}QPushButton:disabled{background:#1a3a5a;color:#555;}")
        self._mac_run_btn.clicked.connect(self._on_mac_run)
        self._mac_stop_btn=QPushButton("■  중단"); self._mac_stop_btn.setFixedHeight(32)
        self._mac_stop_btn.setStyleSheet("QPushButton{background:#5a6a7a;color:#fff;border:none;border-radius:5px;font-size:12px;}QPushButton:hover{background:#7f8c8d;}QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self._mac_stop_btn.setEnabled(False); self._mac_stop_btn.clicked.connect(self._on_mac_stop)
        self._mac_run_st=QLabel("● 대기"); self._mac_run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb2.addWidget(self._mac_run_btn,1); rb2.addWidget(self._mac_stop_btn,1); rn.addLayout(rb2,2,0,1,2); rn.addWidget(self._mac_run_st,3,0,1,2); v.addWidget(run)

        # 슬롯 초기화 (UI 완성 후)
        QTimer.singleShot(0, self._mac_slots_init)
        return w

    def _build_memo_grp(self) -> QWidget:
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(6)

        def _make_font_row(spin_attr, sl_attr, default, color, on_change):
            row = QHBoxLayout(); row.setSpacing(8)
            spin = QSpinBox(); spin.setRange(8, 72); spin.setValue(default); spin.setFixedWidth(52)
            spin.setStyleSheet(f"QSpinBox{{background:#1a1a3a;color:{color};border:1px solid #3a3a3a;border-radius:3px;font-weight:bold;font-size:13px;}}")
            sl = QSlider(Qt.Horizontal); sl.setRange(8, 72); sl.setValue(default)
            sl.setStyleSheet(
                f"QSlider::groove:horizontal{{background:#1a2030;height:6px;border-radius:3px;}}"
                f"QSlider::handle:horizontal{{background:{color};width:16px;height:16px;margin-top:-5px;margin-bottom:-5px;border-radius:8px;}}"
                f"QSlider::sub-page:horizontal{{background:#3a3a20;border-radius:3px;}}")
            spin.valueChanged.connect(lambda val, s=sl: (s.blockSignals(True), s.setValue(val), s.blockSignals(False), on_change(val)))
            sl.valueChanged.connect(lambda val, sp=spin: (sp.blockSignals(True), sp.setValue(val), sp.blockSignals(False), on_change(val)))
            setattr(self, spin_attr, spin); setattr(self, sl_attr, sl)
            row.addWidget(sl, 1); row.addWidget(spin)
            return row

        # ── 메모장 에디터 글꼴 크기 ──────────────────────────────────────────
        fg=QGroupBox("📝  메모장 에디터 글꼴 크기  (컨트롤 패널 내 텍스트)")
        fv=QVBoxLayout(fg); fv.setSpacing(4)
        fv.addLayout(_make_font_row("_memo_font_spin","_memo_font_sl", 11, "#f0c040", self._apply_memo_font))
        pr1=QHBoxLayout(); pr1.setSpacing(4)
        for lbl,sz in [("S",10),("M",13),("L",16),("XL",20)]:
            b=QPushButton(lbl); b.setFixedSize(28,22)
            b.setStyleSheet("QPushButton{background:#2a2a1a;color:#f0c040;border:1px solid #4a4a20;border-radius:3px;font-size:10px;}QPushButton:hover{background:#3a3a28;}")
            b.clicked.connect(lambda _,s=sz: self._memo_font_spin.setValue(s)); pr1.addWidget(b)
        pr1.addStretch(); fv.addLayout(pr1)
        v.addWidget(fg)

        # ── 영상 오버레이 글자 크기 ──────────────────────────────────────────
        og=QGroupBox("🎬  영상 오버레이 글자 크기  (녹화 영상에 표시되는 메모 크기)")
        ov_=QVBoxLayout(og); ov_.setSpacing(4)
        ov_.addLayout(_make_font_row("_overlay_font_spin","_overlay_font_sl", 18, "#7bc8e0", self._apply_overlay_font))
        pr2=QHBoxLayout(); pr2.setSpacing(4)
        for lbl,sz in [("S",14),("M",18),("L",24),("XL",32)]:
            b=QPushButton(lbl); b.setFixedSize(28,22)
            b.setStyleSheet("QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;border-radius:3px;font-size:10px;}QPushButton:hover{background:#223344;}")
            b.clicked.connect(lambda _,s=sz: self._overlay_font_spin.setValue(s)); pr2.addWidget(b)
        pr2.addStretch(); ov_.addLayout(pr2)
        v.addWidget(og)

        ts_g=QGroupBox("줄 클릭 시 타임스탬프 삽입"); tsg=QHBoxLayout(ts_g); tsg.setSpacing(8)
        self._memo_ts_chk=QCheckBox("활성화"); self._memo_ts_chk.setChecked(True); self._memo_ts_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;"); self._memo_ts_chk.toggled.connect(self._on_ts_toggled)
        tsg.addWidget(self._memo_ts_chk)
        ts_hint = QLabel("← 좌클릭: 타임스탬프 삽입  |  우클릭: 타임스탬프 제거")
        ts_hint.setStyleSheet("color:#556;font-size:10px;")
        tsg.addWidget(ts_hint); tsg.addStretch(); v.addWidget(ts_g)
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

    def _build_reset_grp(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)
        info = QLabel(
            "설정을 초기화하면 아래 항목들이 기본값으로 되돌아갑니다.\n"
            "• FPS / 배속 / 블랙아웃 임계값 / 쿨다운\n"
            "• 오토클릭 인터벌 / 매크로 반복 횟수\n"
            "• 수동녹화 전/후 시간\n"
            "• 메모 탭 전체 내용\n"
            "• 섹션 접힘 상태\n\n"
            "녹화 중에는 초기화가 적용되지 않습니다.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#999;font-size:11px;padding:4px 2px;border:1px solid #2a2a3a;border-radius:4px;background:#0d0d1e;")
        v.addWidget(info)
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
        self._rst_chk_db.setStyleSheet("color:#e74c3c;font-weight:bold;")
        for chk in [self._rst_chk_settings, self._rst_chk_memo,
                    self._rst_chk_sections, self._rst_chk_db]:
            cg.addWidget(chk)
        v.addWidget(chk_grp)
        confirm_grp = QGroupBox("확인 입력")
        cfl = QVBoxLayout(confirm_grp); cfl.setSpacing(6)
        confirm_hint = QLabel("초기화하려면 아래에  RESET  을 입력하세요")
        confirm_hint.setStyleSheet("color:#f0a040;font-size:11px;")
        cfl.addWidget(confirm_hint)
        self._rst_confirm_edit = QLineEdit()
        self._rst_confirm_edit.setPlaceholderText("RESET 입력 후 버튼 클릭")
        self._rst_confirm_edit.setStyleSheet(
            "QLineEdit{background:#0d0d1e;color:#f0c040;border:1px solid #4a4a20;border-radius:4px;padding:4px 8px;font-size:13px;font-family:monospace;font-weight:bold;}")
        cfl.addWidget(self._rst_confirm_edit)
        v.addWidget(confirm_grp)
        self._rst_btn = QPushButton("🔄  설정 초기화 실행")
        self._rst_btn.setMinimumHeight(40)
        self._rst_btn.setStyleSheet("""
            QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #4a1a1a,stop:1 #6a2a2a);
                color:#ffaaaa;font-size:13px;font-weight:bold;border:1px solid #8a3a3a;border-radius:6px;padding:6px;}
            QPushButton:hover{background:#7a2a2a;color:white;}
            QPushButton:pressed{background:#3a1010;}""")
        self._rst_btn.clicked.connect(self._on_reset_settings)
        v.addWidget(self._rst_btn)
        self._rst_result_lbl = QLabel("")
        self._rst_result_lbl.setStyleSheet("font-size:11px;padding:2px;")
        v.addWidget(self._rst_result_lbl)
        return w

    def _on_reset_settings(self):
        if self.engine.recording:
            self._rst_result_lbl.setText("⚠ 녹화 중에는 초기화할 수 없습니다.")
            self._rst_result_lbl.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")
            return
        if self._rst_confirm_edit.text().strip() != "RESET":
            self._rst_result_lbl.setText("❌ 'RESET'을 정확히 입력하세요.")
            self._rst_result_lbl.setStyleSheet("color:#e74c3c;font-size:11px;")
            return
        do_settings = self._rst_chk_settings.isChecked()
        do_memo     = self._rst_chk_memo.isChecked()
        do_sections = self._rst_chk_sections.isChecked()
        do_db       = self._rst_chk_db.isChecked()
        if do_db:
            try:
                if os.path.exists(DB_PATH): os.remove(DB_PATH)
                self.db = SettingsDB()
                self._log("[초기화] DB 파일 삭제 후 재생성 완료")
            except Exception as ex:
                self._rst_result_lbl.setText(f"❌ DB 삭제 실패: {ex}")
                self._rst_result_lbl.setStyleSheet("color:#e74c3c;font-size:11px;")
                return
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
            self._m_post_spin.setValue(10.0)
            self._memo_ts_chk.setChecked(True)
            self._codec_combo.setCurrentIndex(0)
            self._scale_combo.setCurrentIndex(0)
            self._log("[초기화] 설정값 기본값으로 복원")
        if do_memo:
            while self._memo_tabs.count() > 0: self._memo_tabs.removeTab(0)
            self._memo_editors.clear(); self.engine.memo_texts.clear()
            self._add_memo_tab("메모 1"); self._log("[초기화] 메모 탭 초기화")
        if do_sections:
            for sec in self._sections.values(): sec.set_collapsed(False)
            self._log("[초기화] 섹션 상태 초기화")
        self._save_settings()
        self._rst_confirm_edit.clear()
        self._rst_result_lbl.setText("✅ 초기화 완료!")
        self._rst_result_lbl.setStyleSheet("color:#2ecc71;font-size:12px;font-weight:bold;")
        self._log("[초기화] 설정 초기화 완료")
        QTimer.singleShot(3000, lambda: self._rst_result_lbl.setText(""))

    def _connect_signals(self):
        self.signals.blackout_detected.connect(self._on_blackout)
        self.signals.status_message.connect(self._log)
        self.signals.auto_click_count.connect(self._click_lcd.display)
        self.signals.macro_step_recorded.connect(self._on_mac_step)
        self.signals.manual_clip_saved.connect(self._on_manual_saved)

    # =========================================================================
    #  설정 저장 / 복원
    # =========================================================================
    def _save_settings(self):
        import json as _j
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
        db.set("video_codec",    self._codec_combo.currentIndex())
        db.set("video_scale",    self._scale_combo.currentIndex())
        db.set("memo_font_size",    self._memo_font_spin.value())
        db.set("overlay_font_size", self._overlay_font_spin.value())   # ★ v11
        db.set("feature_order",  _j.dumps(self._feat_bar.current_order()))  # ★ v10
        for key, sec in self._sections.items():
            db.set(f"sec_col_{key}", sec.is_collapsed())
        tabs = [{'title': self._memo_tabs.tabText(i),
                 'content': self._memo_editors[i].toPlainText()}
                for i in range(self._memo_tabs.count())
                if i < len(self._memo_editors)]
        db.save_memo_tabs(tabs)
        # ★ v10: 매크로 슬롯 저장
        self._mac_slot_flush()

    def _load_settings(self):
        import json as _j
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
        self._m_pre_spin.setValue(pre); self._m_pre_sl.setValue(int(pre * 10))
        self._m_post_spin.setValue(post); self._m_post_sl.setValue(int(post * 10))
        self._m_scr_chk.setChecked(db.get_bool("manual_src_scr", True))
        self._m_cam_chk.setChecked(db.get_bool("manual_src_cam", True))
        self.engine.manual_source = db.get("manual_source", "both")
        self._memo_ts_chk.setChecked(db.get_bool("memo_ts", True))
        self._codec_combo.setCurrentIndex(db.get_int("video_codec", 0))
        self._scale_combo.setCurrentIndex(db.get_int("video_scale", 0))
        # ★ v10: 글꼴 크기 복원
        fs = db.get_int("memo_font_size", 11)
        self._memo_font_spin.setValue(fs)
        self._apply_memo_font(fs)
        # ★ v11: 오버레이 글자 크기 복원
        ofs = db.get_int("overlay_font_size", 18)
        self._overlay_font_spin.setValue(ofs)
        self._apply_overlay_font(ofs)
        # ★ v10: feature 순서 복원
        feat_raw = db.get("feature_order", "")
        if feat_raw:
            try:
                saved = _j.loads(feat_raw)
                self._apply_feature_order(saved)
            except Exception:
                pass
        for key, sec in self._sections.items():
            sec.set_collapsed(db.get_bool(f"sec_col_{key}", False))
        tabs = db.load_memo_tabs()
        if tabs:
            while self._memo_tabs.count() > 0: self._memo_tabs.removeTab(0)
            self._memo_editors.clear(); self.engine.memo_texts.clear()
            for t in tabs: self._add_memo_tab(t['title'], t['content'])
        # ★ v10: 매크로 슬롯 복원
        mac_data = db.load_macro_slots()
        if mac_data:
            self._macro_slots.clear()
            for s in mac_data:
                steps = [ClickStep(st['x'], st['y'], st['delay']) for st in s['steps']]
                self._macro_slots.append({'title': s['title'], 'steps': steps})
            self._mac_slot_combo.blockSignals(True)
            self._mac_slot_combo.clear()
            for s in self._macro_slots:
                self._mac_slot_combo.addItem(s['title'])
            self._mac_slot_combo.blockSignals(False)
            self._active_mac_slot = 0
            self._mac_slot_combo.setCurrentIndex(0)
            self._mac_slot_sync()
            self._rebuild_mac_tbl()
            self._mac_slot_info_upd()
        # ★ v10: 로드 후 DateTimeEdit 타이머 상태 결정
        self._dt_live_update()

    def _auto_save_settings(self):
        threading.Thread(target=self._save_settings, daemon=True).start()

    # =========================================================================
    #  코덱 / 스케일 변경
    # =========================================================================
    def _on_codec_changed(self, idx: int):
        codecs = ["mp4v", "avc1", "xvid"]
        self.engine.video_codec = codecs[idx] if idx < len(codecs) else "mp4v"
        self._log(f"[영상] 코덱 변경 → {self.engine.video_codec}")

    def _on_scale_changed(self, idx: int):
        scales = [1.0, 0.75, 0.5]
        self.engine.video_scale = scales[idx] if idx < len(scales) else 1.0
        self._log(f"[영상] 해상도 스케일 변경 → {int(self.engine.video_scale*100)}%")

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
        self._refresh_ms_cd()  # ★ v9

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
    #  Schedule (기존)
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
        entry=ScheduleEntry(start_dt, stop_dt, ['rec_start','rec_stop'])
        self.engine.schedules.append(entry)
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
        for action, entry in self.engine.schedule_tick():
            if action == 'start':
                self._log(f"[Schedule] ⏺ #{entry.id} 녹화 시작")
                self._on_start_rec()
            elif action == 'stop':
                self._log(f"[Schedule] ⏹ #{entry.id} 녹화 종료")
                self._on_stop_rec()
            elif action == 'macro_run':
                self._log(f"[Schedule] 🎯 #{entry.id} 매크로 실행 (반복:{entry.macro_repeat})")
                if not self.engine.macro_steps:
                    self._log("[Schedule] ⚠ 매크로 스텝이 없습니다. 먼저 기록하세요.")
                else:
                    self.engine.macro_start_run(
                        repeat=entry.macro_repeat,
                        gap=entry.macro_gap)
                    entry.macro_run_done = True
        # ★ v9: 매크로 예약 틱
        self._tick_macro_schedule()

    # =========================================================================
    #  ★ v9: 매크로 예약 (독립 시스템)
    # =========================================================================
    def _on_macro_sched_add(self):
        qdt  = self._ms_dt.dateTime(); d = qdt.date(); t = qdt.time()
        run_dt = datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second())
        now = datetime.now()
        if run_dt < now:
            QMessageBox.warning(self, "오류", "실행 시각이 과거입니다."); return

        stop_dt = None
        if self._ms_stop_chk.isChecked():
            qdt2 = self._ms_stop_dt.dateTime(); d2 = qdt2.date(); t2 = qdt2.time()
            stop_dt = datetime(d2.year(), d2.month(), d2.day(), t2.hour(), t2.minute(), t2.second())
            if stop_dt <= run_dt:
                QMessageBox.warning(self, "오류", "종료 시각은 실행 시각보다 늦어야 합니다."); return

        actions = []
        if self._ms_act_rec_start.isChecked(): actions.append('rec_start')
        if self._ms_act_macro.isChecked():     actions.append('macro_run')
        if self._ms_act_rec_stop.isChecked():  actions.append('rec_stop')
        if not actions:
            QMessageBox.warning(self, "오류", "최소 하나의 액션을 선택하세요."); return

        entry = {
            'id':         len(self._macro_schedules) + 1,
            'run_dt':     run_dt,
            'stop_dt':    stop_dt,
            'actions':    actions,
            'repeat':     self._ms_rep.value(),
            'gap':        self._ms_gap.value(),
            'done':       False,
            'ran':        False,
            'stopped':    False,
        }
        self._macro_schedules.append(entry)
        self._ms_add_row(entry)
        self._log(f"[매크로예약] #{entry['id']} 추가  실행:{run_dt.strftime('%m/%d %H:%M:%S')}  액션:{actions}")

    def _ms_add_row(self, e: dict):
        r = self._ms_tbl.rowCount(); self._ms_tbl.insertRow(r)
        run_str  = e['run_dt'].strftime("%m/%d %H:%M:%S")
        act_str  = "+".join(e['actions'])
        rep_str  = "∞" if e['repeat'] == 0 else str(int(e['repeat']))
        for c, v in enumerate([str(e['id']), run_str, act_str, rep_str, "대기"]):
            it = QTableWidgetItem(v); it.setTextAlignment(Qt.AlignCenter)
            self._ms_tbl.setItem(r, c, it)

    def _on_ms_del(self):
        rows = sorted({i.row() for i in self._ms_tbl.selectedItems()}, reverse=True)
        for r in rows:
            it = self._ms_tbl.item(r, 0)
            if it:
                eid = int(it.text())
                self._macro_schedules = [e for e in self._macro_schedules if e['id'] != eid]
            self._ms_tbl.removeRow(r)

    def _on_ms_clear(self):
        self._macro_schedules.clear(); self._ms_tbl.setRowCount(0)

    def _tick_macro_schedule(self):
        now = datetime.now()
        for e in self._macro_schedules:
            if e['done']: continue
            # 실행 시각 도달
            if not e['ran']:
                delta = (e['run_dt'] - now).total_seconds()
                if -2 <= delta <= 1:
                    e['ran'] = True
                    if 'rec_start' in e['actions'] and not self.engine.recording:
                        self._log(f"[매크로예약] #{e['id']} 녹화 시작")
                        self._on_start_rec()
                    if 'macro_run' in e['actions']:
                        if not self.engine.macro_steps:
                            self._log(f"[매크로예약] #{e['id']} ⚠ 매크로 스텝 없음")
                        else:
                            self._log(f"[매크로예약] #{e['id']} 매크로 실행 (반복:{int(e['repeat'])})")
                            self.engine.macro_start_run(
                                repeat=int(e['repeat']),
                                gap=float(e['gap']))
                    if 'rec_stop' in e['actions'] and not e.get('stop_dt'):
                        # 종료 시각 별도 지정 없으면 즉시 종료
                        self._log(f"[매크로예약] #{e['id']} 녹화 종료")
                        self._on_stop_rec()
                        e['done'] = True
            # 종료 시각 별도 지정 있을 때
            if e['ran'] and e.get('stop_dt') and not e['stopped']:
                delta2 = (e['stop_dt'] - now).total_seconds()
                if -2 <= delta2 <= 1:
                    e['stopped'] = True; e['done'] = True
                    if 'rec_stop' in e['actions'] and self.engine.recording:
                        self._log(f"[매크로예약] #{e['id']} 녹화 종료 (예약)")
                        self._on_stop_rec()
            # done 처리
            if e['ran'] and not e.get('stop_dt'):
                e['done'] = True
        # 테이블 상태 업데이트
        for r in range(self._ms_tbl.rowCount()):
            it = self._ms_tbl.item(r, 0)
            if not it: continue
            eid = int(it.text())
            entry = next((e for e in self._macro_schedules if e['id'] == eid), None)
            if not entry: continue
            st = self._ms_tbl.item(r, 4)
            if st:
                if entry['done']: st.setText("완료"); st.setForeground(QColor("#888"))
                elif entry['ran']: st.setText("진행중"); st.setForeground(QColor("#2ecc71"))
                else: st.setText("대기"); st.setForeground(QColor("#f0c040"))

    def _refresh_ms_cd(self):
        now = datetime.now()
        pending = [e for e in self._macro_schedules if not e['done']]
        if pending:
            nxt = min(pending, key=lambda e: e['run_dt'])
            secs = int((nxt['run_dt'] - now).total_seconds())
            if secs >= 0:
                self._ms_cd_lbl.setText(
                    f"#{nxt['id']} 까지  {secs//3600:02d}h {(secs%3600)//60:02d}m {secs%60:02d}s")
            else:
                self._ms_cd_lbl.setText(f"#{nxt['id']} 진행 중…")
        else:
            self._ms_cd_lbl.setText("예약 없음")

    # =========================================================================
    #  Feature bar  ★ v10: 타이머 연동 + DB저장
    # =========================================================================
    def _on_feature_toggle(self, key, enabled):
        sec = self._sections.get(key)
        if sec: sec.setVisible(enabled)
        self._dt_live_update()   # ★ v10

    def _on_feature_order(self, order):
        while self._panel_l.count():
            it = self._panel_l.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for key in order:
            sec = self._sections.get(key)
            if sec:
                sec.setParent(self._panel_w); self._panel_l.addWidget(sec)
                sec.setVisible(self._feat_bar.is_enabled(key))
        self._panel_l.addStretch()
        # ★ v10: 순서 변경 즉시 DB 저장
        threading.Thread(target=self._save_settings, daemon=True).start()

    def _apply_feature_order(self, order: list):
        """DB에서 읽은 순서로 패널 재배치 (내부용)."""
        while self._panel_l.count():
            it = self._panel_l.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        placed = set()
        for key in order:
            sec = self._sections.get(key)
            if sec:
                sec.setParent(self._panel_w); self._panel_l.addWidget(sec)
                sec.setVisible(self._feat_bar.is_enabled(key))
                placed.add(key)
        for key, sec in self._sections.items():
            if key not in placed:
                sec.setParent(self._panel_w); self._panel_l.addWidget(sec)
        self._panel_l.addStretch()

    def _on_scroll_to_section(self, key: str):
        sec = self._sections.get(key)
        if not sec: return
        if not sec.isVisible(): sec.setVisible(True)
        if sec.is_collapsed(): sec.set_collapsed(False)
        QTimer.singleShot(50, lambda: self._scroll_to_widget(sec))

    def _scroll_to_widget(self, widget):
        pos = widget.mapTo(self._panel_w, QPoint(0, 0))
        vsb = self._panel_scroll.verticalScrollBar()
        vsb.setValue(max(0, pos.y() - 10))

    # =========================================================================
    #  ★ v10: DateTimeEdit 실시간 카운트다운 (섹션 ON시만)
    # =========================================================================
    def _dt_live_update(self):
        """schedule/macro_schedule 섹션 중 하나라도 ON이면 타이머 동작."""
        on = (self._feat_bar.is_enabled("schedule") or
              self._feat_bar.is_enabled("macro_schedule"))
        if on:
            if not self._dt_live_timer.isActive():
                self._dt_live_timer.start()
        else:
            self._dt_live_timer.stop()

    def _on_dt_live_tick(self):
        """매 1초: DateTimeEdit가 현재보다 과거이면 빨간 테두리 경고."""
        now = QDateTime.currentDateTime()
        _ok  = ("QDateTimeEdit{background:#1a1a3a;color:%s;border:1px solid %s;"
                "border-radius:4px;padding:4px 6px;font-size:12px;font-family:monospace;}"
                "QDateTimeEdit::drop-down{border:none;}QDateTimeEdit::down-arrow{image:none;width:0;}")
        _bad = ("QDateTimeEdit{background:#2a0a0a;color:#ff6b6b;border:2px solid #e74c3c;"
                "border-radius:4px;padding:4px 6px;font-size:12px;font-family:monospace;}"
                "QDateTimeEdit::drop-down{border:none;}QDateTimeEdit::down-arrow{image:none;width:0;}")
        pairs = [
            (self._s_start_dt, "#2ecc71", "#2a6a3a"),
            (self._s_stop_dt,  "#e74c3c", "#6a2a2a"),
            (self._ms_dt,      "#00b894", "#1a6a5a"),
            (self._ms_stop_dt, "#e74c3c", "#6a2a2a"),
        ]
        for dte, col, brd in pairs:
            if dte.dateTime() < now:
                dte.setStyleSheet(_bad)
            else:
                dte.setStyleSheet(_ok % (col, brd))

    # =========================================================================
    #  ★ v10: 메모 글꼴 크기
    # =========================================================================
    def _apply_memo_font(self, size: int):
        from PyQt5.QtGui import QFont
        for ed in self._memo_editors:
            f = ed.font(); f.setPointSize(size); ed.setFont(f)

    # =========================================================================
    #  ★ v10: 매크로 슬롯 시스템
    # =========================================================================
    def _mac_slots_init(self):
        """슬롯이 비어있을 때 기본 슬롯 1개 생성."""
        if not self._macro_slots:
            self._macro_slots.append({'title': '슬롯 1', 'steps': []})
            self._mac_slot_combo.blockSignals(True)
            self._mac_slot_combo.clear()
            self._mac_slot_combo.addItem('슬롯 1')
            self._mac_slot_combo.blockSignals(False)
        self._active_mac_slot = 0
        self._mac_slot_combo.setCurrentIndex(0)
        self._mac_slot_sync()
        self._mac_slot_info_upd()

    def _mac_slot_sync(self):
        """활성 슬롯 steps → engine.macro_steps."""
        if 0 <= self._active_mac_slot < len(self._macro_slots):
            self.engine.macro_steps = self._macro_slots[self._active_mac_slot]['steps']
        else:
            self.engine.macro_steps = []

    def _mac_slot_save_cur(self):
        """현재 engine.macro_steps → 활성 슬롯에 백업."""
        if 0 <= self._active_mac_slot < len(self._macro_slots):
            self._macro_slots[self._active_mac_slot]['steps'] = list(self.engine.macro_steps)

    def _mac_slot_info_upd(self):
        n   = len(self._macro_slots)
        idx = self._active_mac_slot
        steps = len(self.engine.macro_steps)
        name  = self._macro_slots[idx]['title'] if 0 <= idx < n else "—"
        self._mac_slot_info.setText(f"{name}  |  슬롯 {idx+1}/{n}  |  {steps} 스텝")

    def _mac_slot_flush(self):
        """DB에 모든 슬롯 저장."""
        self._mac_slot_save_cur()
        data = []
        for s in self._macro_slots:
            data.append({'title': s['title'],
                         'steps': [{'x': st.x, 'y': st.y, 'delay': st.delay}
                                    for st in s['steps']]})
        self.db.save_macro_slots(data)

    def _on_mac_slot_changed(self, idx: int):
        self._mac_slot_save_cur()
        self._active_mac_slot = idx
        self._mac_slot_sync()
        self._rebuild_mac_tbl()
        self._mac_slot_info_upd()

    def _on_mac_slot_add(self):
        n = len(self._macro_slots) + 1
        title = f"슬롯 {n}"
        self._macro_slots.append({'title': title, 'steps': []})
        self._mac_slot_combo.blockSignals(True)
        self._mac_slot_combo.addItem(title)
        self._mac_slot_combo.blockSignals(False)
        self._mac_slot_combo.setCurrentIndex(len(self._macro_slots) - 1)

    def _on_mac_slot_del(self):
        if len(self._macro_slots) <= 1:
            self._log("[Macro] 슬롯은 최소 1개 필요합니다."); return
        idx = self._active_mac_slot
        self._macro_slots.pop(idx)
        self._mac_slot_combo.blockSignals(True)
        self._mac_slot_combo.removeItem(idx)
        self._mac_slot_combo.blockSignals(False)
        new_idx = max(0, idx - 1)
        self._active_mac_slot = new_idx
        self._mac_slot_combo.setCurrentIndex(new_idx)
        self._mac_slot_sync()
        self._rebuild_mac_tbl()
        self._mac_slot_info_upd()

    def _on_mac_slot_rename(self):
        from PyQt5.QtWidgets import QInputDialog
        idx = self._active_mac_slot
        if not (0 <= idx < len(self._macro_slots)): return
        old = self._macro_slots[idx]['title']
        new, ok = QInputDialog.getText(self, "슬롯 이름 변경", "새 이름:", text=old)
        if ok and new.strip():
            self._macro_slots[idx]['title'] = new.strip()
            self._mac_slot_combo.blockSignals(True)
            self._mac_slot_combo.setItemText(idx, new.strip())
            self._mac_slot_combo.blockSignals(False)
            self._mac_slot_info_upd()

    # =========================================================================
    #  ★ v10: 튜토리얼
    # =========================================================================
    def _show_tutorial_if_needed(self):
        import time as _t
        mode = self.db.get("tutorial_skip", "")
        if mode == "forever": return
        if mode == "week":
            ts = self.db.get_float("tutorial_week_ts", 0.0)
            if _t.time() - ts < 7 * 86400: return
        dlg = TutorialDialog(self)
        dlg.exec_()
        if dlg.choice == "skip_week":
            self.db.set("tutorial_skip", "week")
            self.db.set("tutorial_week_ts", str(_t.time()))
        elif dlg.choice == "skip_forever":
            self.db.set("tutorial_skip", "forever")

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

    def _on_mac_step(self, x, y, delay):
        step = ClickStep(x, y, delay)
        self._mac_append_row(step, False)
        self._mac_slot_info_upd()   # ★ v10

    def _on_mac_clear(self):
        self._mac_tbl.blockSignals(True); self._mac_tbl.setRowCount(0); self._mac_tbl.blockSignals(False)
        self.engine.macro_clear(); self._mac_pos_lbl.setText("마지막 클릭: —")
        self._mac_slot_info_upd()   # ★ v10
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
    def _add_memo_tab(self, title, content=""):
        ed = TimestampMemoEdit()
        ed.setPlaceholderText("메모 입력… (좌클릭: 타임스탬프 삽입 | 우클릭: 제거)")
        ed.setStyleSheet("background:#0d0d1e;color:#ffe;border:none;font-family:monospace;")
        ed.timestamp_enabled = self._memo_ts_chk.isChecked()
        # ★ v10: 현재 글꼴 크기 적용
        try:
            from PyQt5.QtGui import QFont
            f = ed.font(); f.setPointSize(self._memo_font_spin.value()); ed.setFont(f)
        except Exception:
            pass
        if content: ed.setPlainText(content)
        ed.textChanged.connect(self._on_any_memo_changed)
        self._memo_editors.append(ed); self._memo_tabs.addTab(ed, title)
        while len(self.engine.memo_texts) < len(self._memo_editors):
            self.engine.memo_texts.append("")
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

    # ★ v10: 메모 에디터 글꼴 크기 적용
    def _apply_memo_font(self, size: int):
        from PyQt5.QtGui import QFont
        for ed in self._memo_editors:
            f = ed.font(); f.setPointSize(size); ed.setFont(f)

    # ★ v11: 영상 오버레이 글자 크기 적용
    def _apply_overlay_font(self, size: int):
        self.engine.overlay_font_size = size

    def _check_font_on_startup(self):
        ok, msg = check_korean_font()
        if ok:
            self._log(f"[폰트] ✅ {msg}")
        else:
            self._log(f"[폰트] ⚠ {msg}")
            box = QMessageBox(self)
            box.setWindowTitle("한글 폰트 경고")
            box.setIcon(QMessageBox.Warning)
            box.setText(
                "<b>메모 오버레이에 한글이 정상 표시되지 않을 수 있습니다.</b><br><br>"
                + msg.replace("\n", "<br>") +
                "<br><br><b>Windows 해결:</b> '맑은 고딕' 또는 나눔고딕 설치")
            box.setTextFormat(Qt.RichText)
            box.exec_()

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
        QPushButton:pressed{background:#0d1a2a;color:#7bc8ff;padding:5px 9px 3px 11px;}
        QPushButton:checked{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #1a4a6a,stop:1 #0d2a3a);border:2px solid #3a8aca;color:#7bc8ff;font-weight:bold;}
        QPushButton:disabled{background:#141424;color:#3a3a5a;border-color:#2a2a3a;}
        QTextEdit,QPlainTextEdit{background:#0d0d1e;border:1px solid #2a2a4a;color:#ccc;}
        QTextEdit:focus,QPlainTextEdit:focus{border-color:#4a6a9a;}
        QDoubleSpinBox,QSpinBox{background:#1a1a3a;border:1px solid #3a4a6a;color:#ddd;padding:2px 4px;border-radius:3px;}
        QDoubleSpinBox:focus,QSpinBox:focus{border-color:#5a8aca;color:#eef;}
        QDoubleSpinBox::up-button,QSpinBox::up-button,
        QDoubleSpinBox::down-button,QSpinBox::down-button{background:#1e2a3a;border:none;width:14px;}
        QComboBox{background:#1a1a3a;border:1px solid #3a4a6a;color:#ddd;padding:2px 4px;border-radius:3px;}
        QComboBox:hover{border-color:#5a8aca;}
        QComboBox::drop-down{border:none;}
        QComboBox QAbstractItemView{background:#1a1a3a;color:#ddd;selection-background-color:#2a4a7a;}
        QCheckBox{color:#ccd;spacing:6px;}
        QCheckBox::indicator{width:16px;height:16px;border:1px solid #4a5a7a;border-radius:3px;background:#0d0d1e;}
        QCheckBox::indicator:checked{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #2a7aca,stop:1 #1a5a9a);border:2px solid #5aaae0;}
        QLabel{color:#ccd;}
        QLCDNumber{background:#0d1520;border:1px solid #336;color:#2ecc71;}
        QLineEdit{background:#1a1a3a;border:1px solid #3a4a6a;color:#ddd;padding:2px 6px;border-radius:3px;}
        QLineEdit:focus{border-color:#5a8aca;}
        QTableWidget{selection-background-color:#1a3a5a;}
        QTableWidget::item:selected{background:#1a3a5a;color:#eef;}
        QScrollBar:vertical{background:#0d0d1e;width:8px;border-radius:4px;}
        QScrollBar::handle:vertical{background:#336;border-radius:4px;min-height:20px;}
        QScrollBar::handle:vertical:hover{background:#4a5a8a;}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
        QSlider::groove:horizontal{background:#1a2030;height:6px;border-radius:3px;}
        QSlider::handle:horizontal{background:#3a7abd;width:16px;height:16px;
            margin-top:-5px;margin-bottom:-5px;border-radius:8px;}
        QSlider::sub-page:horizontal{background:#2a5a8a;border-radius:3px;}
        QTabWidget::pane{border:1px solid #2a2a4a;}
        QTabBar::tab{background:#1a1a3a;color:#888;border:1px solid #334;
            border-bottom:none;border-radius:4px 4px 0 0;padding:4px 10px;}
        QTabBar::tab:selected{background:#2a2a5a;color:#dde;border-color:#446;font-weight:bold;}
        QTabBar::tab:hover{background:#22224a;color:#bbd;}
        QDateTimeEdit{background:#1a1a3a;border:1px solid #336;color:#ddd;padding:2px 4px;border-radius:3px;}
        QSplitter::handle{background:#2a2a4a;width:5px;}
        QSplitter::handle:hover{background:#4a6aaa;}
        """

    # =========================================================================
    #  종료 이벤트
    # =========================================================================
    def closeEvent(self, e):
        self._dt_live_timer.stop()        # ★ v10
        self._mac_slot_flush()            # ★ v10: 슬롯 저장
        self._save_settings()
        if self.engine.recording:
            self._log("종료: 녹화 파일 저장 중…")
            self.engine.stop_recording()
        self.engine.stop()
        self._cam_win.hide()
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