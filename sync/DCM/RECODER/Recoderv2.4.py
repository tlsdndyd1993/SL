# -*- coding: utf-8 -*-
"""
Screen & Camera Recorder  v2.4
────────────────────────────────────────────────────────────────────────────
v2.3 → v2.4 수정사항
  [문제1] ⏸ 일시정지 시 카메라 출력도 멈히는 문제
    · 원인: _on_scr_toggle 에서 engine.stop_screen() 호출 → 스크린 스레드 중단
    · 수정: 스레드는 중단하지 않고 _pump_preview 에서 렌더링만 조건부 스킵
    · 효과: 카메라 미리보기·버퍼·녹화에 전혀 영향 없음

  [문제2] 카메라 스캔 시 FPS가 계속 변동하는 이유 및 수정
    · 원인1: OS 보고값 신뢰 불가 시 단일 실측 → 시스템 부하에 따라 매번 다름
    · 원인2: 초기 버퍼 충전 프레임이 실측 속도에 포함되어 느리게 측정됨
    · 수정 : 실측 3회 → 중앙값(median)으로 노이즈 제거
             + 표준 FPS(15/24/25/30/50/60)에 ±5fps 이내면 스냅 → 안정된 정수 FPS 표시

  [문제3] T/C 검증 창이 녹화 종료 시 표시되지 않는 문제
    · 원인1: str | None 타입 힌트가 Python 3.9 이하에서 TypeError 발생
    · 원인2: self._tc_result 인스턴스 변수가 클로저에서 불안정하게 작동
    · 수정 : result_box=[None] 리스트 패턴으로 클로저 안전 처리
             Qt.ApplicationModal 설정으로 완전 모달 보장
             Ctrl+Alt+E 단축키도 RecordingPanel._on_stop 경유로 수정
             모든 단축키 콜백을 QTimer.singleShot(0, ...) 으로 UI 스레드 위임
────────────────────────────────────────────────────────────────────────────
  [수정1] 메모 오버레이 기본값 OFF → ON 활성화 즉시 영상 표출
  [수정2] 기능 순서 설정 UI → 드래그앤드랍 전용 (_FeatureDragList)
          체크박스 UI 제거, 섹션 ON/OFF는 CollapsibleSection 헤더로
  [수정3] CAM 창 스캔 UI → '▲ 스캔 숨기기' 버튼으로 접기/펼치기 가능
  [수정4] 영상 오버레이 글자 크기 → 탭별 독립 (MemoOverlayCfg.overlay_font_size)
          '공통' 슬라이더 제거, 각 오버레이 행에 크기 스핀박스 반영
  [수정5] T/C 검증 다이얼로그 → _show_tc_dialog()가 str|None 반환
          _on_stop / closeEvent 에서 반환값으로 취소 여부 판단
          다이얼로그가 메인스레드에서 exec_() 블로킹 실행되어 정상 작동
────────────────────────────────────────────────────────────────────────────
  [문제수정]
  1. 시작 시 자원 과부화 → 스크린·카메라 캡처 10초 지연 시작
     · '▶ 지금 시작' 버튼으로 즉시 수동 시작 가능
     · 미리보기 카운트다운 표시
  2. 카메라 스캔 개선 → 플랫폼별 백엔드 우선 순위(DSHOW/V4L2)
     · FPS 실측: OS 보고값 신뢰 불가 시 30프레임 타이밍 실측으로 보정
     · active_cam_idx 유효성 검사로 재스캔 후 선택 유지
  3. FPS 낮게 나오는 문제 → 실측 FPS 30프레임 기준으로 개선
  4. 기능 표시/숨기기 → 독립 스크롤 영역 + ▲▼ 순서 변경 + 더블클릭 이동
  5. Click Macro 기록 중단 시 마지막 스텝(중단버튼 클릭) 자동 제거
  6. avc1/H.264 코덱 → 사용 불가 시 mp4v 자동 폴백 + 경고 표시

  [기능추가]
  1. Recording: Display/Camera 저장 독립 ON/OFF (미리보기와 별개)
  2. 현재 프레임 캡처 버튼 (Display/Camera 각각, PNG 저장)
  3. T/C 검증 목적 체크박스
     · 활성화 시 녹화 종료·프로그램 종료 전 PASS/FAIL 선택 창 표시
     · 결과에 따라 폴더명 앞에 (PASS) / (FAIL) 태그 부여
     · feat_order DB 저장/복원

  [수정]
  1. 저장 경로 '차종_버전(ex: TK1_2541, NX5_2633)' 레이블로 변경

────────────────────────────────────────────────────────────────────────────
구조:
  - CoreEngine     : 녹화/감지/오토클릭/매크로 로직 (UI 없음, 재사용 가능)
  - SettingsDB     : SQLite 설정 저장/복원
  - Signals        : Qt 시그널 허브
  - MainWindow     : PyQt5 메인 UI
  - 각 패널 위젯   : RecordingPanel / ManualClipPanel / MemoPanel 등
────────────────────────────────────────────────────────────────────────────
요구사항:
  pip install PyQt5 opencv-python numpy mss Pillow pynput
"""

# ── 표준 라이브러리 ──────────────────────────────────────────────────────────
import sys, os, threading, time, queue, platform, subprocess, sqlite3, json, re
from datetime import datetime
from collections import deque

# ── 서드파티 ─────────────────────────────────────────────────────────────────
import cv2
import numpy as np
import mss

try:
    from PIL import Image as _PIL_Image, ImageDraw as _PIL_Draw, ImageFont as _PIL_Font
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGroupBox, QCheckBox, QDoubleSpinBox,
    QScrollArea, QFrame, QGridLayout, QTextEdit, QSizePolicy,
    QDialog, QDateTimeEdit, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPlainTextEdit, QAbstractItemView, QSpinBox, QSlider,
    QTabWidget, QComboBox, QLineEdit, QSplitter, QInputDialog,
)
from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QObject, QPoint,
                           QRect, QDateTime)
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
#  유니코드 / 한글 폰트
# =============================================================================
_FONT_CACHE: dict = {}

def _find_unicode_font(size: int = 18):
    if not PIL_AVAILABLE:
        return None
    import glob as _glob
    candidates = []
    _sys = platform.system()
    if _sys == "Windows":
        wf = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        for f in ["malgun.ttf","malgunbd.ttf","gulim.ttc","batang.ttc",
                  "NanumGothic.ttf","NanumGothicBold.ttf"]:
            candidates.append(os.path.join(wf, f))
        try:
            import winreg
            for hive, sub in [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            ]:
                try:
                    rk = winreg.OpenKey(hive, sub); i = 0
                    while True:
                        try:
                            nm, data, _ = winreg.EnumValue(rk, i); i += 1
                            if any(k in nm for k in ("맑은","Malgun","나눔","Nanum","굴림","바탕")):
                                p = data if os.path.isabs(data) else os.path.join(wf, data)
                                candidates.insert(0, p)
                        except OSError: break
                    winreg.CloseKey(rk)
                except Exception: pass
        except ImportError: pass
    elif _sys == "Darwin":
        for d in ["/System/Library/Fonts","/Library/Fonts",
                  os.path.expanduser("~/Library/Fonts")]:
            for f in ["AppleSDGothicNeo.ttc","NanumGothic.ttf","Arial Unicode.ttf"]:
                candidates.append(os.path.join(d, f))
    else:
        try:
            r = subprocess.run(["fc-list",":lang=ko","--format=%{file}\n"],
                               capture_output=True, text=True, timeout=5)
            candidates += [l.strip() for l in r.stdout.splitlines() if l.strip()]
        except Exception: pass
        candidates += list(_glob.glob("/usr/share/fonts/**/Noto*CJK*.ttc", recursive=True))
        candidates += list(_glob.glob("/usr/share/fonts/**/Nanum*.ttf", recursive=True))

    seen = set()
    for p in candidates:
        if not p or not os.path.exists(p) or p in seen: continue
        seen.add(p)
        try:
            fnt = _PIL_Font.truetype(p, size)
            # 한글 렌더링 검증
            w, h = size*4, size*2
            def rnd(txt):
                img = _PIL_Image.new("L",(w,h),0)
                _PIL_Draw.Draw(img).text((2,2),txt,font=fnt,fill=255)
                return bytes(img.tobytes())
            r1,r2,r3 = rnd("가"),rnd("나"),rnd("다")
            if sum(r1) < 20 or r1==r2==r3: continue
            return fnt
        except Exception: continue
    return None

def _get_font(size: int = 18):
    if size not in _FONT_CACHE:
        _FONT_CACHE[size] = _find_unicode_font(size)
    return _FONT_CACHE[size]



# =============================================================================
#  유틸리티
# =============================================================================
def open_folder(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    if platform.system() == "Windows": os.startfile(path)
    elif platform.system() == "Darwin": subprocess.Popen(["open", path])
    else: subprocess.Popen(["xdg-open", path])

def fmt_hms(secs: float) -> str:
    s = int(secs); return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def draw_time_bar(frame: np.ndarray, now_str: str, elapsed_str: str) -> None:
    ov = frame.copy()
    cv2.rectangle(ov, (4,4),(440,78),(0,0,0),-1)
    cv2.addWeighted(ov, 0.45, frame, 0.55, 0, frame)
    cv2.putText(frame, now_str,     (10,32), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0,255,80),  2, cv2.LINE_AA)
    cv2.putText(frame, elapsed_str, (10,68), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80,220,255),2, cv2.LINE_AA)

def draw_memo_overlay(frame: np.ndarray, lines: list, position: str,
                      fw: int, fh: int, font_size: int = 18) -> None:
    if not lines: return
    fnt = _get_font(font_size)
    pad = 8
    try:
        sb = fnt.getbbox("A가") if fnt else None
        line_h = (sb[3]-sb[1]+6) if sb else font_size+6
    except: line_h = font_size+6
    max_tw = 0
    for ln in lines:
        try:
            bb = fnt.getbbox(ln) if fnt else None
            tw = (bb[2]-bb[0]) if bb else len(ln)*(font_size//2+2)
        except: tw = len(ln)*(font_size//2+2)
        max_tw = max(max_tw, tw)
    inner = 12; bg_pad = 4
    box_w = min(max_tw+inner*2, fw-bg_pad*2-pad*2)
    box_h = len(lines)*line_h+14
    if position == "top-left":    x0,y0 = pad, pad+30
    elif position == "top-right": x0,y0 = fw-box_w-pad-bg_pad, pad+30
    elif position == "bottom-left": x0,y0 = pad, fh-box_h-pad-bg_pad
    elif position == "center":    x0,y0 = (fw-box_w)//2, (fh-box_h)//2
    else:                         x0,y0 = fw-box_w-pad-bg_pad, fh-box_h-pad-bg_pad
    x0 = max(bg_pad, min(x0, fw-box_w-bg_pad))
    y0 = max(bg_pad, min(y0, fh-box_h-bg_pad))
    ov = frame.copy()
    cv2.rectangle(ov, (max(0,x0-bg_pad),max(0,y0-bg_pad)),
                  (min(fw,x0+box_w+bg_pad),min(fh,y0+box_h+bg_pad)), (0,0,0), -1)
    cv2.addWeighted(ov, 0.55, frame, 0.45, 0, frame)
    if PIL_AVAILABLE and fnt:
        pil_img = _PIL_Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw = _PIL_Draw.Draw(pil_img)
        for j, ln in enumerate(lines):
            if not ln: continue
            ty = y0 + j*line_h
            if ty > fh-bg_pad: break
            draw.text((x0+inner-6, ty), ln, font=fnt, fill=(100,240,255))
        frame[:] = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    else:
        for j, ln in enumerate(lines):
            cy = y0+j*line_h+line_h
            if cy > fh-bg_pad: break
            cv2.putText(frame, ln, (x0+inner, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100,240,255), 1, cv2.LINE_AA)


# =============================================================================
#  SettingsDB
# =============================================================================
class SettingsDB:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        return sqlite3.connect(self._path, check_same_thread=False)

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS memo_tabs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, content TEXT, sort_order INTEGER DEFAULT 0,
                    font_size INTEGER DEFAULT 11, ts_enabled INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS macro_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, steps_json TEXT, sort_order INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS schedule_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_iso TEXT, stop_iso TEXT,
                    actions_json TEXT, repeat INTEGER DEFAULT 1,
                    gap REAL DEFAULT 1.0);
                CREATE TABLE IF NOT EXISTS manual_clip_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT, clip_time TEXT,
                    pre_sec REAL, post_sec REAL, clip_path TEXT);
                CREATE TABLE IF NOT EXISTS path_settings (
                    id INTEGER PRIMARY KEY,
                    vehicle_type TEXT DEFAULT '',
                    tc_id TEXT DEFAULT '');
            """)
            # path_settings 기본 행 보장
            c.execute("INSERT OR IGNORE INTO path_settings(id) VALUES(1)")

    # ── Key-Value 설정 ────────────────────────────────────────────────────────
    def get(self, key, default=None):
        with self._lock:
            with self._conn() as c:
                row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
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
        return v.lower() in ('1','true','yes')

    # ── 메모 탭 ──────────────────────────────────────────────────────────────
    def save_memo_tabs(self, tabs: list):
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM memo_tabs")
                c.executemany(
                    "INSERT INTO memo_tabs(title,content,sort_order,font_size,ts_enabled)"
                    " VALUES(?,?,?,?,?)",
                    [(t.get('title','메모'), t.get('content',''), i,
                      int(t.get('font_size',11)), int(bool(t.get('ts_enabled',True))))
                     for i,t in enumerate(tabs)])

    def load_memo_tabs(self) -> list:
        with self._lock:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT title,content,font_size,ts_enabled"
                    " FROM memo_tabs ORDER BY sort_order").fetchall()
        return [{'title':r[0],'content':r[1],
                 'font_size':int(r[2]) if r[2] else 11,
                 'ts_enabled':bool(r[3]) if r[3] is not None else True}
                for r in rows]

    # ── 매크로 슬롯 ──────────────────────────────────────────────────────────
    def save_macro_slots(self, slots: list):
        """slots: list of {'title':str, 'steps': [MacroStep.to_dict(), ...]}"""
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM macro_slots")
                c.executemany(
                    "INSERT INTO macro_slots(title,steps_json,sort_order) VALUES(?,?,?)",
                    [(s.get('title','슬롯'),
                      json.dumps(s.get('steps',[]), ensure_ascii=False), i)
                     for i,s in enumerate(slots)])

    def load_macro_slots(self) -> list:
        """반환: list of {'title':str, 'steps': [dict,...]} — MacroStep.from_dict()로 복원 가능"""
        with self._lock:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT title,steps_json FROM macro_slots ORDER BY sort_order").fetchall()
        result = []
        for title, sj in rows:
            try:
                steps_raw = json.loads(sj) if sj else []
            except:
                steps_raw = []
            # 구버전 호환: {'x','y','delay'} 형식이면 MacroStep click 으로 변환
            steps = []
            for sd in steps_raw:
                if 'kind' not in sd:
                    sd = {'kind':'click','delay':sd.get('delay',0.5),
                          'x':sd.get('x',0),'y':sd.get('y',0),
                          'button':'left','double':False,'key_str':'',
                          'x2':0,'y2':0}
                steps.append(sd)
            result.append({'title': title, 'steps': steps})
        return result

    # ── 경로 설정 ─────────────────────────────────────────────────────────────
    def get_path_settings(self) -> dict:
        with self._lock:
            with self._conn() as c:
                row = c.execute(
                    "SELECT vehicle_type,tc_id FROM path_settings WHERE id=1").fetchone()
        return {'vehicle_type': row[0] if row else '',
                'tc_id':        row[1] if row else ''}

    def set_path_settings(self, vehicle_type: str, tc_id: str):
        with self._lock:
            with self._conn() as c:
                c.execute("INSERT OR REPLACE INTO path_settings(id,vehicle_type,tc_id)"
                          " VALUES(1,?,?)", (vehicle_type, tc_id))

    # ── 수동녹화 로그 ─────────────────────────────────────────────────────────
    def log_manual_clip(self, source, pre, post, path):
        with self._lock:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO manual_clip_log(source,clip_time,pre_sec,post_sec,clip_path)"
                    " VALUES(?,?,?,?,?)",
                    (source, datetime.now().isoformat(), pre, post, path))

    # ── DB 초기화 ─────────────────────────────────────────────────────────────
    def wipe(self):
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM settings")
                c.execute("DELETE FROM memo_tabs")
                c.execute("DELETE FROM macro_slots")
                c.execute("DELETE FROM schedule_entries")


# =============================================================================
#  데이터 모델
# =============================================================================
class MacroStep:
    """
    매크로 스텝 단일 이벤트 — 세 가지 타입 지원:
      'click'    : x,y,button,double  (마우스 클릭)
      'drag'     : x1,y1,x2,y2        (마우스 드래그)
      'key'      : key_str            (키보드 입력)
    delay  : 이전 스텝으로부터 경과 초 (float)
    """
    __slots__ = ('kind','delay','x','y','x2','y2','button','double','key_str')

    def __init__(self, kind='click', delay=0.5, **kw):
        self.kind    = kind
        self.delay   = delay
        self.x       = kw.get('x', 0)
        self.y       = kw.get('y', 0)
        self.x2      = kw.get('x2', 0)
        self.y2      = kw.get('y2', 0)
        self.button  = kw.get('button', 'left')   # 'left'/'right'/'middle'
        self.double  = kw.get('double', False)
        self.key_str = kw.get('key_str', '')

    # ── 직렬화 ──────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> 'MacroStep':
        kind  = d.get('kind','click')
        delay = d.get('delay', 0.5)
        kw    = {k: d[k] for k in ('x','y','x2','y2','button','double','key_str') if k in d}
        return cls(kind, delay, **kw)

    # ── 테이블 표시용 요약 ──────────────────────────────────────────────────
    def summary(self) -> str:
        if self.kind == 'click':
            btn = self.button; dbl = " x2" if self.double else ""
            return f"[Click{dbl}] ({self.x},{self.y}) {btn}"
        elif self.kind == 'drag':
            return f"[Drag] ({self.x},{self.y})→({self.x2},{self.y2})"
        elif self.kind == 'key':
            return f"[Key] {self.key_str}"
        return f"[{self.kind}]"

# 이전 버전 호환 alias
ClickStep = MacroStep

class ScheduleEntry:
    _cnt = 0
    def __init__(self, start_dt, stop_dt, actions=None,
                 macro_repeat=1, macro_gap=1.0):
        ScheduleEntry._cnt += 1
        self.id = ScheduleEntry._cnt
        self.start_dt = start_dt; self.stop_dt = stop_dt
        self.actions  = actions or ['rec_start','rec_stop']
        self.macro_repeat = macro_repeat
        self.macro_gap    = macro_gap
        self.started = self.stopped = self.done = False
        self.macro_run_done = False

class MemoOverlayCfg:
    """
    단일 메모 오버레이 설정.
    enabled 기본값 = False → 메모장 패널에서 ON 해야 영상에 표시.
    overlay_font_size : 탭별 독립 글자 크기 (기본 18pt)
    """
    def __init__(self, tab_idx=0, position="bottom-right",
                 target="both", enabled=False,
                 overlay_font_size=18):
        self.tab_idx           = tab_idx
        self.position          = position
        self.target            = target
        self.enabled           = enabled
        self.overlay_font_size = overlay_font_size   # ★ 탭별 독립


# =============================================================================
#  Signals
# =============================================================================
class Signals(QObject):
    blackout_detected  = pyqtSignal(str, dict)
    status_message     = pyqtSignal(str)
    ac_count_changed   = pyqtSignal(int)
    rec_started        = pyqtSignal(str)
    rec_stopped        = pyqtSignal()
    macro_step_rec     = pyqtSignal(object)   # MacroStep 객체 전달
    manual_clip_saved  = pyqtSignal(str)
    cameras_scanned    = pyqtSignal(list)
    monitors_scanned   = pyqtSignal(list)
    capture_saved      = pyqtSignal(str, str)  # (source, path)
    tc_verify_request  = pyqtSignal()           # T/C 검증 다이얼로그 요청


# =============================================================================
#  CoreEngine  (UI 없음 — 재사용 가능)
# =============================================================================
class CoreEngine:
    """
    모든 비-UI 로직 캡슐화.
    UI는 시그널만으로 소통하고 engine의 public 속성/메서드만 사용.
    """
    MANUAL_IDLE = 0; MANUAL_WAITING = 1

    def __init__(self, signals: Signals, base_dir: str = BASE_DIR):
        self.signals   = signals
        self.base_dir  = base_dir

        # ── 녹화 상태 ───────────────────────────────────────────────────────
        self.recording    = False
        self.start_time:  float = 0.0
        self.output_dir:  str   = ""

        # ── 스레드 제어 ─────────────────────────────────────────────────────
        self._scr_stop = threading.Event()
        self._cam_stop = threading.Event()
        self._scr_thread: threading.Thread = None
        self._cam_thread: threading.Thread = None

        # ── FPS 측정 ────────────────────────────────────────────────────────
        self.actual_screen_fps: float = 30.0
        self.actual_camera_fps: float = 30.0
        self._scr_fps_ts: deque = deque(maxlen=90)
        self._cam_fps_ts: deque = deque(maxlen=90)

        # ── 큐 (미리보기용) ─────────────────────────────────────────────────
        self.screen_queue: queue.Queue = queue.Queue(maxsize=3)
        self.camera_queue: queue.Queue = queue.Queue(maxsize=3)

        # ── 라이터 ─────────────────────────────────────────────────────────
        self._scr_writer = None; self._cam_writer = None
        self._writer_lock = threading.Lock()
        self._seg_start: float = 0.0
        self._scr_fidx = 0; self._cam_fidx = 0
        self.segment_duration: float = 30 * 60
        self._seg_start_time: float = 0.0

        # ── 설정 ────────────────────────────────────────────────────────────
        self.screen_rec_enabled     = True
        self.camera_rec_enabled     = True   # ★ 카메라 녹화 독립 ON/OFF
        self.blackout_rec_enabled   = True
        self.playback_speed: float  = 1.0
        self.video_codec: str       = "mp4v"
        self.video_scale: float     = 1.0
        self.overlay_font_size: int = 18

        # ── T/C 검증 ────────────────────────────────────────────────────────
        self.tc_verify_enabled: bool  = False   # 'T/C 검증 목적' 체크박스
        self.tc_verify_result: str    = ""       # "PASS" | "FAIL" | ""

        # ── 버퍼 ────────────────────────────────────────────────────────────
        self.buffer_seconds: int = 40
        self._scr_buf: deque = deque()
        self._cam_buf: deque = deque()
        self._buf_lock = threading.Lock()

        # ── ROI / 블랙아웃 ─────────────────────────────────────────────────
        self.screen_rois: list  = []; self.camera_rois: list  = []
        self.screen_roi_avg     = []; self.camera_roi_avg     = []
        self.screen_roi_prev    = []; self.camera_roi_prev    = []
        self.screen_overall_avg = np.zeros(3)
        self.camera_overall_avg = np.zeros(3)

        self.brightness_threshold: float = 30.0
        self.blackout_cooldown:    float = 5.0
        self._scr_last_bo: float = 0.0
        self._cam_last_bo: float = 0.0
        self.screen_bo_count = 0; self.camera_bo_count = 0
        self.screen_bo_events: list = []; self.camera_bo_events: list = []
        self.blackout_dir = os.path.join(base_dir, "blackout")

        # ── 메모 / 오버레이 ─────────────────────────────────────────────────
        self.memo_texts: list = [""]
        self.memo_overlays: list = [MemoOverlayCfg(
            tab_idx=0, position="bottom-right", target="both",
            enabled=False, overlay_font_size=18)]  # 기본 OFF

        # ── 수동녹화 ────────────────────────────────────────────────────────
        self.manual_pre_sec:  float = 10.0
        self.manual_post_sec: float = 10.0
        self.manual_source:   str   = "both"
        self.manual_dir = os.path.join(base_dir, "manual_clip")
        self.manual_state = self.MANUAL_IDLE
        self._manual_lock = threading.Lock()
        self._manual_trigger: float = 0.0

        # ── 오토클릭 ────────────────────────────────────────────────────────
        self.ac_enabled   = False
        self.ac_interval: float = 1.0
        self.ac_count     = 0
        self._ac_stop   = threading.Event()
        self._ac_thread: threading.Thread = None

        # ── 매크로 ──────────────────────────────────────────────────────────
        self.macro_steps:    list = []
        self.macro_running   = False
        self.macro_recording = False
        self.macro_repeat    = 1
        self.macro_gap       = 1.0
        self._mac_stop    = threading.Event()
        self._mac_thread: threading.Thread  = None
        self._mac_listener = None
        self._mac_mouse_listener = None
        self._mac_key_listener   = None

        # ── 예약 ────────────────────────────────────────────────────────────
        self.schedules: list = []

        # ── 카메라 / 모니터 ─────────────────────────────────────────────────
        self.camera_list:  list = []
        self.monitor_list: list = []
        self.active_cam_idx:     int = 0
        self.active_monitor_idx: int = 1

        # ── 저장 경로 설정 ──────────────────────────────────────────────────
        self.vehicle_type: str = ""
        self.tc_id:        str = ""

    # ── 계산 경로 ─────────────────────────────────────────────────────────────
    def _make_output_dir(self) -> str:
        ts = datetime.now().strftime("%Y%m%d")
        parts = [self.base_dir]
        if self.vehicle_type: parts.append(self.vehicle_type)
        parts.append(ts)
        if self.tc_id: parts.append(self.tc_id)
        parts.append(f"Rec_{datetime.now().strftime('%H%M%S')}")
        return os.path.join(*parts)

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────
    def measured_fps(self, dq: deque) -> float:
        if len(dq) < 2: return 0.0
        sp = dq[-1] - dq[0]
        return (len(dq)-1)/sp if sp > 0 else 0.0

    def _fourcc(self):
        """
        코덱 → fourcc 변환.
        avc1(H.264)은 OpenCV 빌드에 따라 지원 여부가 다름.
        지원하지 않으면 mp4v 로 자동 폴백 후 경고 시그널 발행.
        """
        if self.video_codec == "avc1":
            # H.264 가용성 검사: 1×1 임시 파일로 확인
            import tempfile
            try:
                tmp = tempfile.mktemp(suffix='.mp4')
                test_wr = cv2.VideoWriter(
                    tmp, cv2.VideoWriter_fourcc(*'avc1'), 30.0, (2, 2))
                ok = test_wr.isOpened()
                test_wr.release()
                try: os.remove(tmp)
                except: pass
                if ok:
                    return cv2.VideoWriter_fourcc(*'avc1')
            except Exception:
                pass
            # 폴백
            self.signals.status_message.emit(
                "⚠ avc1(H.264) 코덱을 사용할 수 없습니다. mp4v 로 자동 전환합니다.")
            self.video_codec = "mp4v"
            return cv2.VideoWriter_fourcc(*'mp4v')
        elif self.video_codec == "xvid":
            return cv2.VideoWriter_fourcc(*'XVID')
        else:
            return cv2.VideoWriter_fourcc(*'mp4v')

    def _scale(self, frame: np.ndarray) -> np.ndarray:
        if self.video_scale >= 1.0: return frame
        h,w = frame.shape[:2]
        return cv2.resize(frame, (max(2,int(w*self.video_scale)),
                                  max(2,int(h*self.video_scale))),
                          interpolation=cv2.INTER_AREA)

    @property
    def _scr_buf_max(self): return max(1, int(self.actual_screen_fps*self.buffer_seconds))
    @property
    def _cam_buf_max(self): return max(1, int(self.actual_camera_fps*self.buffer_seconds))

    # ── 모니터 스캔 ───────────────────────────────────────────────────────────
    def scan_monitors(self):
        found = []
        try:
            with mss.mss() as s:
                for i, m in enumerate(s.monitors):
                    name = ("전체 합성" if i==0 else f"Display {i}") + f"  ({m['width']}×{m['height']})"
                    found.append({"idx":i,"name":name,"w":m["width"],"h":m["height"]})
        except Exception as e:
            self.signals.status_message.emit(f"모니터 스캔 실패: {e}")
        self.monitor_list = found
        if not any(m["idx"]==self.active_monitor_idx for m in found):
            self.active_monitor_idx = found[0]["idx"] if found else 0
        self.signals.monitors_scanned.emit(found)

    # ── 카메라 스캔 ───────────────────────────────────────────────────────────
    def scan_cameras(self):
        """
        연결된 카메라를 스캔.

        FPS 안정화 전략:
          1. OS 보고값이 신뢰 가능(5~300fps)하면 그대로 사용
          2. 신뢰 불가 시 30프레임을 3회 실측 → 중앙값(median)으로 노이즈 제거
          3. 최종값을 표준 FPS(15/24/25/30/50/60)에 스냅 → 표시값 안정
        백엔드 우선순위: DSHOW(Win) > V4L2(Linux) > 기본값
        """
        _STANDARD_FPS = [15.0, 24.0, 25.0, 30.0, 50.0, 60.0]

        def snap_fps(fps: float) -> float:
            """측정 FPS를 가장 가까운 표준값으로 스냅."""
            best = min(_STANDARD_FPS, key=lambda s: abs(s - fps))
            return best if abs(best - fps) <= 5.0 else fps

        def measure_fps_once(cap) -> float:
            frames = 0; t0 = time.perf_counter()
            while frames < 30:
                ret, _ = cap.read()
                if ret: frames += 1
                if time.perf_counter() - t0 > 3.0: break
            elapsed = time.perf_counter() - t0
            return frames / elapsed if (elapsed > 0 and frames > 0) else 0.0

        found = []
        _sys = platform.system()
        if _sys == "Windows":
            backends = [cv2.CAP_DSHOW, cv2.CAP_ANY]
        elif _sys == "Linux":
            backends = [cv2.CAP_V4L2, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_ANY]

        for idx in range(8):
            cap = None
            for backend in backends:
                try:
                    c = cv2.VideoCapture(idx, backend)
                    if c.isOpened():
                        cap = c; break
                    c.release()
                except Exception:
                    pass
            if cap is None:
                continue

            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

            reported_fps = cap.get(cv2.CAP_PROP_FPS)
            if reported_fps and 5.0 < reported_fps < 300.0:
                # OS 보고값 신뢰 → 스냅만 적용
                fps = snap_fps(float(reported_fps))
            else:
                # 실측 3회 → 중앙값 → 스냅
                samples = []
                for _ in range(3):
                    m = measure_fps_once(cap)
                    if m > 0: samples.append(m)
                if samples:
                    samples.sort()
                    median = samples[len(samples)//2]
                    fps = snap_fps(median)
                else:
                    fps = 15.0

            try:
                nm = cap.getBackendName()
            except Exception:
                nm = "Camera"

            cap.release()
            found.append({
                "idx":  idx,
                "name": f"Camera {idx} [{nm}] {fps:.0f}fps",
                "fps":  fps,
            })

        self.camera_list = found
        if found:
            ids = [c["idx"] for c in found]
            if self.active_cam_idx not in ids:
                self.active_cam_idx = found[0]["idx"]
                self.actual_camera_fps = found[0]["fps"]
            else:
                cam = next(c for c in found if c["idx"] == self.active_cam_idx)
                self.actual_camera_fps = cam["fps"]
        self.signals.cameras_scanned.emit(found)

    # ── ROI 밝기 ─────────────────────────────────────────────────────────────
    @staticmethod
    def calc_roi_avg(frame, rois):
        avgs = []
        for rx,ry,rw,rh in rois:
            r = frame[ry:ry+rh, rx:rx+rw]
            avgs.append(r.mean(axis=0).mean(axis=0) if r.size>0 else np.zeros(3))
        return avgs

    # ── 블랙아웃 감지 ─────────────────────────────────────────────────────────
    def _detect_blackout(self, curr, prev, source: str) -> bool:
        if not curr or not prev or len(curr)!=len(prev): return False
        changes = []
        for c,p in zip(curr,prev):
            if np.all(p==0): continue
            cb = 0.114*c[0]+0.587*c[1]+0.299*c[2]
            pb = 0.114*p[0]+0.587*p[1]+0.299*p[2]
            changes.append(pb-cb)
        if not changes: return False
        mc = float(np.mean(changes))
        if mc < self.brightness_threshold: return False
        now = time.time()
        last = (self._scr_last_bo if source=="screen" else self._cam_last_bo)
        if now-last < self.blackout_cooldown: return False
        if source=="screen": self._scr_last_bo=now; self.screen_bo_count+=1
        else:                self._cam_last_bo=now; self.camera_bo_count+=1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        ev = {'time': datetime.now().strftime("%H:%M:%S.%f")[:-3],
              'brightness_change': mc, 'timestamp': ts}
        (self.screen_bo_events if source=="screen" else self.camera_bo_events).append(ev)
        self.signals.blackout_detected.emit(source, ev)
        if self.blackout_rec_enabled:
            threading.Thread(target=self._save_bo_clip,
                             args=(source,ts), daemon=True).start()
        return True

    def _save_bo_clip(self, source: str, timestamp: str):
        src_dir = os.path.join(self.blackout_dir, source.upper())
        os.makedirs(src_dir, exist_ok=True)
        fps = max((self.actual_screen_fps if source=="screen"
                   else self.actual_camera_fps), 1.0)
        n_pre = int(fps*20); n_post = int(fps*20)
        with self._buf_lock:
            buf = self._scr_buf if source=="screen" else self._cam_buf
            pre = list(buf)
        post=[]; deadline=time.time()+22.0
        while len(post)<n_post and time.time()<deadline:
            time.sleep(0.04)
            with self._buf_lock:
                buf = self._scr_buf if source=="screen" else self._cam_buf
                if len(buf)>len(pre): post=list(buf)[len(pre):]
        pre_clip = pre[-n_pre:] if len(pre)>=n_pre else pre
        all_f = pre_clip+post[:n_post]
        if not all_f: return
        bi = len(pre_clip); h,w = all_f[0].shape[:2]
        vp = os.path.join(src_dir, f"blackout_{timestamp}.mp4")
        wr = cv2.VideoWriter(vp, self._fourcc(), fps, (w,h))
        for i,f in enumerate(all_f):
            fc = self._scale(f.copy())
            if i==bi: cv2.rectangle(fc,(4,4),(fc.shape[1]-4,fc.shape[0]-4),(0,0,255),6)
            wr.write(fc)
        wr.release()
        self.signals.status_message.emit(f"[Blackout/{source}] → {vp}")

    # ── 오버레이 합성 ─────────────────────────────────────────────────────────
    def _apply_overlays(self, frame: np.ndarray, source: str) -> np.ndarray:
        h, w = frame.shape[:2]
        if self.recording and self.start_time:
            now = datetime.now()
            ns = now.strftime("%Y-%m-%d  %H:%M:%S.")+f"{now.microsecond//1000:03d}"
            e = time.time()-self.start_time
            draw_time_bar(frame, ns, f"REC  {fmt_hms(e)}.{int((e%1)*1000):03d}")
        for cfg in self.memo_overlays:
            if not cfg.enabled: continue
            if cfg.target!="both" and cfg.target!=source: continue
            if cfg.tab_idx >= len(self.memo_texts): continue
            text = self.memo_texts[cfg.tab_idx].strip()
            if text:
                draw_memo_overlay(frame, text.splitlines(), cfg.position,
                                  w, h, cfg.overlay_font_size)  # ★ 탭별
        return frame

    # ── PTS 동기화 쓰기 ───────────────────────────────────────────────────────
    def _write_sync(self, writer, frame, fps, fidx, elapsed):
        expected = int(elapsed*fps); diff = expected-fidx
        if diff<=0:
            writer.write(frame); return fidx+1
        for _ in range(max(1,diff)): writer.write(frame)
        return fidx+max(1,diff)

    # ── 세그먼트 생성 ─────────────────────────────────────────────────────────
    def _create_segment(self):
        seg_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._writer_lock:
            if self._scr_writer: self._scr_writer.release()
            if self._cam_writer: self._cam_writer.release()
            self._scr_writer = self._cam_writer = None
        scr_fps = max(1.0, self.actual_screen_fps*self.playback_speed)
        cam_fps = max(1.0, self.actual_camera_fps*self.playback_speed)
        fc = self._fourcc()
        if self.screen_rec_enabled:
            try:
                with mss.mss() as s:
                    midx = min(self.active_monitor_idx, len(s.monitors)-1)
                    mon = s.monitors[midx]
                sw = max(2, int(mon['width']*self.video_scale))
                sh = max(2, int(mon['height']*self.video_scale))
                sp = os.path.join(self.output_dir, f"screen_{seg_ts}.mp4")
                with self._writer_lock:
                    self._scr_writer = cv2.VideoWriter(sp, fc, scr_fps, (sw,sh))
                self.signals.status_message.emit(f"Screen seg: {sp}")
            except Exception as e:
                self.signals.status_message.emit(f"Screen writer 오류: {e}")
        with self._buf_lock:
            cf = self._cam_buf[-1] if self._cam_buf else None
        if cf is not None and self.camera_rec_enabled:
            h,w = cf.shape[:2]
            cw = max(2,int(w*self.video_scale)); ch = max(2,int(h*self.video_scale))
            cp = os.path.join(self.output_dir, f"camera_{seg_ts}.mp4")
            with self._writer_lock:
                self._cam_writer = cv2.VideoWriter(cp, fc, cam_fps, (cw,ch))
            self.signals.status_message.emit(f"Camera seg: {cp}")
        self._seg_start_time = time.time()
        self._scr_fidx = 0; self._cam_fidx = 0

    # ── 스크린 루프 ───────────────────────────────────────────────────────────
    def _screen_loop(self):
        with mss.mss() as sct:
            midx = min(self.active_monitor_idx, len(sct.monitors)-1)
            mon  = sct.monitors[midx]
            interval = 1.0/max(self.actual_screen_fps,1.0)
            next_t   = time.perf_counter()
            while not self._scr_stop.is_set():
                now = time.perf_counter()
                if next_t-now > 0: time.sleep(next_t-now)
                next_t += interval
                img   = sct.grab(mon)
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                self._scr_fps_ts.append(time.time())
                if self.screen_rois:
                    avgs = self.calc_roi_avg(frame, self.screen_rois)
                    self.screen_roi_avg = avgs
                    self.screen_overall_avg = np.mean(avgs,axis=0) if avgs else np.zeros(3)
                    if self.screen_roi_prev:
                        self._detect_blackout(avgs,self.screen_roi_prev,"screen")
                    self.screen_roi_prev = [a.copy() for a in avgs]
                stamped = self._apply_overlays(frame.copy(), "screen")
                with self._buf_lock:
                    self._scr_buf.append(stamped)
                    while len(self._scr_buf) > self._scr_buf_max:
                        self._scr_buf.popleft()
                if self.recording and self.screen_rec_enabled:
                    with self._writer_lock: w = self._scr_writer
                    if w:
                        el = time.time()-self._seg_start_time
                        self._scr_fidx = self._write_sync(
                            w, self._scale(stamped), self.actual_screen_fps,
                            self._scr_fidx, el)
                try: self.screen_queue.put_nowait(frame)
                except queue.Full: pass

    # ── 카메라 루프 ───────────────────────────────────────────────────────────
    def _camera_loop(self):
        cap = cv2.VideoCapture(self.active_cam_idx)
        if not cap.isOpened():
            self.signals.status_message.emit(f"Camera {self.active_cam_idx} 열기 실패"); return
        rep = cap.get(cv2.CAP_PROP_FPS)
        fps = float(rep) if (rep and 0<rep<300) else self.actual_camera_fps
        self.actual_camera_fps = fps
        interval = 1.0/max(fps,1.0); next_t = time.perf_counter()
        while not self._cam_stop.is_set():
            now = time.perf_counter()
            if next_t-now > 0: time.sleep(next_t-now)
            next_t += interval
            ret, frame = cap.read()
            if not ret: continue
            self._cam_fps_ts.append(time.time())
            if self.camera_rois:
                avgs = self.calc_roi_avg(frame, self.camera_rois)
                self.camera_roi_avg = avgs
                self.camera_overall_avg = np.mean(avgs,axis=0) if avgs else np.zeros(3)
                if self.camera_roi_prev:
                    self._detect_blackout(avgs,self.camera_roi_prev,"camera")
                self.camera_roi_prev = [a.copy() for a in avgs]
            stamped = self._apply_overlays(frame.copy(), "camera")
            with self._buf_lock:
                self._cam_buf.append(stamped)
                while len(self._cam_buf) > self._cam_buf_max:
                    self._cam_buf.popleft()
            if self.recording and self.camera_rec_enabled:
                with self._writer_lock: w = self._cam_writer
                if w:
                    el = time.time()-self._seg_start_time
                    self._cam_fidx = self._write_sync(
                        w, self._scale(stamped), fps, self._cam_fidx, el)
            try: self.camera_queue.put_nowait(frame)
            except queue.Full: pass
        cap.release()

    # ── 스레드 제어 ───────────────────────────────────────────────────────────
    def start_screen(self):
        if self._scr_thread and self._scr_thread.is_alive(): return
        self._scr_stop.clear()
        self._scr_thread = threading.Thread(target=self._screen_loop, daemon=True)
        self._scr_thread.start()

    def stop_screen(self): self._scr_stop.set()

    def start_camera(self):
        if self._cam_thread and self._cam_thread.is_alive(): return
        self._cam_stop.clear()
        self._cam_thread = threading.Thread(target=self._camera_loop, daemon=True)
        self._cam_thread.start()

    def stop_camera(self): self._cam_stop.set()

    def restart_screen(self):
        """스크린 스레드를 재시작 — 모니터 전환 시 사용."""
        self.stop_screen()
        def _delayed():
            time.sleep(0.15)
            self.start_screen()
        threading.Thread(target=_delayed, daemon=True).start()

    def restart_camera(self):
        self.stop_camera()
        def _wait():
            if self._cam_thread: self._cam_thread.join(timeout=2.0)
            self.start_camera()
        threading.Thread(target=_wait, daemon=True).start()

    # ── 녹화 시작/종료 ────────────────────────────────────────────────────────
    def start_recording(self):
        if self.recording: return
        self.output_dir = self._make_output_dir()
        os.makedirs(self.output_dir, exist_ok=True)
        self._scr_fidx = self._cam_fidx = 0
        self._create_segment()
        self.start_time = time.time(); self.recording = True
        self.signals.status_message.emit(f"녹화 시작 → {self.output_dir}")
        self.signals.rec_started.emit(self.output_dir)

    def stop_recording(self):
        if not self.recording: return
        self.recording = False
        time.sleep(0.35)
        with self._writer_lock:
            if self._scr_writer: self._scr_writer.release(); self._scr_writer=None
            if self._cam_writer: self._cam_writer.release(); self._cam_writer=None
        # ── T/C 검증 결과를 폴더명에 적용 ──────────────────────────────────
        if self.tc_verify_enabled and self.tc_verify_result in ("PASS","FAIL"):
            self._apply_tc_result_to_folder(self.tc_verify_result)
            self.tc_verify_result = ""
        self.start_time = 0.0
        self.signals.status_message.emit("녹화 종료")
        self.signals.rec_stopped.emit()

    def _apply_tc_result_to_folder(self, result: str):
        """녹화 출력 폴더명 앞에 (PASS) 또는 (FAIL) 을 붙임."""
        if not self.output_dir or not os.path.isdir(self.output_dir):
            return
        parent = os.path.dirname(self.output_dir)
        old_name = os.path.basename(self.output_dir)
        # 이미 태그가 붙어 있으면 교체
        import re as _re
        clean_name = _re.sub(r'^\((PASS|FAIL)\)\s*', '', old_name)
        new_name = f"({result}) {clean_name}"
        new_path = os.path.join(parent, new_name)
        try:
            os.rename(self.output_dir, new_path)
            self.output_dir = new_path
            self.signals.status_message.emit(
                f"[T/C] 폴더명 → {new_name}")
        except Exception as e:
            self.signals.status_message.emit(f"[T/C] 폴더 이름 변경 실패: {e}")

    # ── 수동녹화 ─────────────────────────────────────────────────────────────
    def save_manual_clip(self) -> bool:
        with self._manual_lock:
            if self.manual_state != self.MANUAL_IDLE: return False
            self.manual_state = self.MANUAL_WAITING
            self._manual_trigger = time.time()
        sources = []
        if self.manual_source in ("screen","both"): sources.append("screen")
        if self.manual_source in ("camera","both"): sources.append("camera")
        for src in sources:
            threading.Thread(target=self._do_manual,args=(src,),daemon=True).start()
        return True

    def _do_manual(self, source: str):
        fps = max((self.actual_screen_fps if source=="screen"
                   else self.actual_camera_fps), 1.0)
        trigger = self._manual_trigger
        n_pre = int(fps*self.manual_pre_sec)
        n_post = int(fps*self.manual_post_sec)
        with self._buf_lock:
            buf = self._scr_buf if source=="screen" else self._cam_buf
            pre = list(buf)[-n_pre:] if n_pre>0 else []
        prev_len = len(pre)
        post=[]; deadline=time.time()+self.manual_post_sec+2.0
        while len(post)<n_post and time.time()<deadline:
            time.sleep(0.04)
            with self._buf_lock:
                buf = self._scr_buf if source=="screen" else self._cam_buf
                cur = list(buf)
            if len(cur)>prev_len: post=cur[prev_len:]
        all_f = pre+post[:n_post]
        if not all_f:
            self.signals.status_message.emit(f"[수동녹화] {source}: 저장할 프레임 없음")
            with self._manual_lock: self.manual_state=self.MANUAL_IDLE
            return
        os.makedirs(self.manual_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        vp = os.path.join(self.manual_dir, f"manual_{source}_{ts}.mp4")
        h,w = all_f[0].shape[:2]; bi = len(pre)
        wr = cv2.VideoWriter(vp, self._fourcc(), fps, (w,h))
        for i,f in enumerate(all_f):
            fc = self._scale(f.copy())
            t_off = (i-bi)/fps
            ts_str = datetime.fromtimestamp(trigger+t_off).strftime("%H:%M:%S.") + \
                     f"{int(((trigger+t_off)%1)*1000):03d}"
            draw_time_bar(fc, ts_str,
                          f"{'PRE' if i<bi else 'POST'}  {t_off:+.2f}s")
            if i==bi:
                cv2.rectangle(fc,(4,4),(fc.shape[1]-4,fc.shape[0]-4),(0,200,255),5)
            wr.write(fc)
        wr.release()
        self.signals.status_message.emit(f"[수동녹화] → {vp}")
        self.signals.manual_clip_saved.emit(vp)
        time.sleep(self.manual_post_sec/2)
        with self._manual_lock: self.manual_state=self.MANUAL_IDLE

    # ── 오토클릭 ─────────────────────────────────────────────────────────────
    def _ac_loop(self):
        mc = pynput_mouse.Controller() if PYNPUT_AVAILABLE else None
        while not self._ac_stop.is_set():
            if mc: mc.click(pynput_mouse.Button.left)
            self.ac_count += 1
            self.signals.ac_count_changed.emit(self.ac_count)
            self._ac_stop.wait(self.ac_interval)

    def start_ac(self):
        if self.ac_enabled: return
        self.ac_enabled=True; self._ac_stop.clear()
        self._ac_thread = threading.Thread(target=self._ac_loop, daemon=True)
        self._ac_thread.start()

    def stop_ac(self):
        self.ac_enabled=False; self._ac_stop.set()

    def reset_ac_count(self):
        self.ac_count=0; self.signals.ac_count_changed.emit(0)

    # ── 매크로 기록 ───────────────────────────────────────────────────────────
    def macro_start_recording(self):
        """
        마우스 클릭 / 드래그 / 키보드 이벤트를 모두 기록.
        드래그 판별: mouse_press → mouse_release 사이에 이동이 5px 이상이면 drag 취급.
        키보드: press 이벤트만 기록 (modifier 단독은 제외).
        """
        if not PYNPUT_AVAILABLE or self.macro_recording: return
        self.macro_recording = True
        _q: queue.Queue = queue.Queue()

        def _delayed():
            time.sleep(0.5)
            if not self.macro_recording: return
            active_ts = time.time()

            # ── 마우스 상태 추적 (드래그 판별용) ────────────────────────────
            _press_info = {}   # button → (x, y, time)
            _DRAG_THRESH = 5   # px

            def _btn_str(btn) -> str:
                if btn == pynput_mouse.Button.left:   return 'left'
                if btn == pynput_mouse.Button.right:  return 'right'
                if btn == pynput_mouse.Button.middle: return 'middle'
                return str(btn)

            def on_click(x, y, button, pressed):
                if not self.macro_recording: return
                t = time.time()
                if t < active_ts: return
                if pressed:
                    _press_info[button] = (int(x), int(y), t)
                else:
                    info = _press_info.pop(button, None)
                    if info is None: return
                    px, py, pt = info
                    dx, dy = abs(int(x)-px), abs(int(y)-py)
                    if dx > _DRAG_THRESH or dy > _DRAG_THRESH:
                        # 드래그 이벤트
                        try: _q.put_nowait(('drag', px, py, int(x), int(y), _btn_str(button), t))
                        except: pass
                    else:
                        # 클릭 이벤트
                        try: _q.put_nowait(('click', px, py, _btn_str(button), False, t))
                        except: pass

            # ── 더블클릭 감지 ─────────────────────────────────────────────
            _last_click = {}   # (x,y,btn) → time
            _DBL_MAX    = 0.4  # seconds

            def on_click_dbl(x, y, button, pressed):
                if not pressed: return on_click(x, y, button, pressed)
                t = time.time()
                if t < active_ts: return
                key = (_btn_str(button),)
                last = _last_click.get(key, 0.0)
                is_dbl = (t - last) < _DBL_MAX
                _last_click[key] = t
                if is_dbl:
                    # 더블클릭 — 직전 클릭을 double=True 로 교체
                    try: _q.put_nowait(('double_flag', t))
                    except: pass
                on_click(x, y, button, pressed)

            # ── 키보드 이벤트 ─────────────────────────────────────────────
            _MODIFIER_KEYS = {
                pynput_keyboard.Key.shift, pynput_keyboard.Key.shift_l,
                pynput_keyboard.Key.shift_r, pynput_keyboard.Key.ctrl,
                pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r,
                pynput_keyboard.Key.alt, pynput_keyboard.Key.alt_l,
                pynput_keyboard.Key.alt_r, pynput_keyboard.Key.cmd,
                pynput_keyboard.Key.caps_lock,
            }

            def on_key_press(key):
                if not self.macro_recording: return
                t = time.time()
                if t < active_ts: return
                if key in _MODIFIER_KEYS: return
                try:
                    ks = key.char if hasattr(key,'char') and key.char else str(key)
                    _q.put_nowait(('key', ks, t))
                except: pass

            # ── emit 루프 ─────────────────────────────────────────────────
            def _emit_loop():
                last_ts = active_ts
                recent_steps: list = []   # 더블클릭 패치용

                while True:
                    try: item = _q.get(timeout=0.1)
                    except:
                        if not self.macro_recording and _q.empty(): break
                        continue
                    if item is None: break

                    ev_type = item[0]

                    if ev_type == 'double_flag':
                        # 가장 최근 클릭 스텝을 double=True 로 패치
                        for st in reversed(recent_steps):
                            if st.kind == 'click':
                                st.double = True; break
                        continue

                    if ev_type == 'click':
                        _, px, py, btn, dbl, t = item
                        delay = round(t - last_ts, 3); last_ts = t
                        step  = MacroStep('click', delay,
                                          x=px, y=py, button=btn, double=dbl)

                    elif ev_type == 'drag':
                        _, px, py, rx, ry, btn, t = item
                        delay = round(t - last_ts, 3); last_ts = t
                        step  = MacroStep('drag', delay,
                                          x=px, y=py, x2=rx, y2=ry, button=btn)

                    elif ev_type == 'key':
                        _, ks, t = item
                        delay = round(t - last_ts, 3); last_ts = t
                        step  = MacroStep('key', delay, key_str=ks)

                    else:
                        continue

                    self.macro_steps.append(step)
                    recent_steps.append(step)
                    if len(recent_steps) > 8: recent_steps.pop(0)
                    # 필터 적용 — UI에서 설정한 _rec_filter로 emit 전에 차단
                    flt = getattr(self, '_rec_filter',
                                  {'click': True, 'drag': True, 'key': True})
                    if not flt.get(step.kind, True):
                        self.macro_steps.pop()   # append 취소
                        continue
                    self.signals.macro_step_rec.emit(step)

            threading.Thread(target=_emit_loop, daemon=True,
                             name="MacroEmitLoop").start()

            # ── 리스너 시작 ───────────────────────────────────────────────
            self._mac_mouse_listener = pynput_mouse.Listener(on_click=on_click_dbl)
            self._mac_key_listener   = pynput_keyboard.Listener(on_press=on_key_press)
            self._mac_mouse_listener.start()
            self._mac_key_listener.start()
            self._mac_listener = self._mac_mouse_listener   # stop() 호환
            self.signals.status_message.emit(
                "매크로 기록 중 — 마우스 클릭/드래그 및 키보드 입력이 기록됩니다")

        threading.Thread(target=_delayed, daemon=True,
                         name="MacroRecordDelayed").start()

    def macro_stop_recording(self):
        """
        기록 중단.
        '중단 버튼 클릭' 자체가 마지막 스텝으로 기록되므로 마지막 1개를 제거.
        """
        self.macro_recording = False
        # 마지막 스텝(= 중단 버튼 클릭 이벤트) 제거
        if self.macro_steps:
            self.macro_steps.pop()
        def _stop():
            time.sleep(0.2)
            for attr in ('_mac_mouse_listener', '_mac_key_listener', '_mac_listener'):
                lst = getattr(self, attr, None)
                if lst:
                    try: lst.stop()
                    except: pass
                    setattr(self, attr, None)
        threading.Thread(target=_stop, daemon=True).start()

    def macro_start_run(self, repeat=None, gap=None):
        if not PYNPUT_AVAILABLE or self.macro_running or not self.macro_steps: return
        if repeat is not None: self.macro_repeat=repeat
        if gap    is not None: self.macro_gap=gap
        self.macro_running=True; self._mac_stop.clear()
        self._mac_thread = threading.Thread(target=self._mac_loop, daemon=True)
        self._mac_thread.start()

    def _mac_loop(self):
        mc  = pynput_mouse.Controller()
        kc  = pynput_keyboard.Controller()
        rep = 0; infinite = (self.macro_repeat == 0)

        def _wait(secs):
            waited = 0.0
            while waited < secs and not self._mac_stop.is_set():
                chunk = min(0.05, secs - waited)
                time.sleep(chunk); waited += chunk

        def _btn(btn_str):
            m = {'left':   pynput_mouse.Button.left,
                 'right':  pynput_mouse.Button.right,
                 'middle': pynput_mouse.Button.middle}
            return m.get(btn_str, pynput_mouse.Button.left)

        def _play_step(step: MacroStep):
            _wait(step.delay)
            if self._mac_stop.is_set(): return

            if step.kind == 'click':
                mc.position = (step.x, step.y)
                btn = _btn(step.button)
                if step.double:
                    mc.click(btn, 2)
                else:
                    mc.click(btn)

            elif step.kind == 'drag':
                mc.position = (step.x, step.y)
                mc.press(_btn(step.button))
                # 부드러운 드래그: 중간점 5개
                dx = (step.x2 - step.x) / 5
                dy = (step.y2 - step.y) / 5
                for i in range(1, 6):
                    if self._mac_stop.is_set(): break
                    mc.position = (int(step.x + dx*i), int(step.y + dy*i))
                    time.sleep(0.02)
                mc.position = (step.x2, step.y2)
                mc.release(_btn(step.button))

            elif step.kind == 'key':
                ks = step.key_str
                try:
                    # 단일 문자
                    if len(ks) == 1:
                        kc.press(ks); kc.release(ks)
                    else:
                        # "Key.enter" 같은 특수키
                        key_name = ks.replace('Key.','')
                        special  = getattr(pynput_keyboard.Key, key_name, None)
                        if special:
                            kc.press(special); kc.release(special)
                        else:
                            kc.type(ks)
                except Exception:
                    try: kc.type(ks)
                    except: pass

            self.signals.status_message.emit(f"[Macro] {step.summary()}")

        while not self._mac_stop.is_set():
            for step in list(self.macro_steps):
                if self._mac_stop.is_set(): break
                _play_step(step)
            rep += 1
            if not infinite and rep >= self.macro_repeat: break
            _wait(self.macro_gap)

        self.macro_running = False
        self.signals.status_message.emit("[Macro] 실행 완료")

    def macro_stop_run(self): self._mac_stop.set(); self.macro_running=False
    def macro_clear(self): self.macro_steps.clear()

    def capture_frame(self, source: str) -> str:
        """
        현재 버퍼의 최신 프레임을 PNG로 저장하고 경로를 반환.
        source: 'screen' | 'camera'
        """
        capture_dir = os.path.join(self.base_dir, "capture")
        os.makedirs(capture_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = os.path.join(capture_dir, f"capture_{source}_{ts}.png")
        with self._buf_lock:
            buf = self._scr_buf if source == "screen" else self._cam_buf
            frame = buf[-1].copy() if buf else None
        if frame is None:
            self.signals.status_message.emit(f"[캡처] {source}: 버퍼 없음")
            return ""
        try:
            cv2.imwrite(path, frame)
            self.signals.status_message.emit(f"[캡처] → {path}")
            self.signals.capture_saved.emit(source, path)
            return path
        except Exception as ex:
            self.signals.status_message.emit(f"[캡처] 저장 실패: {ex}")
            return ""



    # ── 예약 틱 ──────────────────────────────────────────────────────────────
    def schedule_tick(self) -> list:
        now=datetime.now(); actions=[]
        for s in list(self.schedules):
            if s.done: continue
            if s.start_dt and not s.started:
                if -2 <= (s.start_dt-now).total_seconds() <= 1:
                    s.started=True
                    if 'rec_start' in s.actions and not self.recording:
                        actions.append(('start',s))
                    if 'macro_run' in s.actions:
                        actions.append(('macro_run',s))
            if s.stop_dt and s.started and not s.stopped:
                if -2 <= (s.stop_dt-now).total_seconds() <= 1:
                    s.stopped=s.done=True
                    if 'rec_stop' in s.actions and self.recording:
                        actions.append(('stop',s))
            if s.started and not s.stop_dt and not s.done: s.done=True
        return actions

    # ── 전체 시작/종료 ────────────────────────────────────────────────────────
    def start(self):
        """
        엔진 초기화.
        자원 과부화 방지: 스크린·카메라 캡처는 10초 지연 후 시작.
        프로그램 시작 직후 렌더링 부하로 인한 강제 종료를 방지.
        """
        threading.Thread(target=self.scan_monitors, daemon=True).start()
        threading.Thread(target=self.scan_cameras,  daemon=True).start()
        # 10초 후 자동 시작 — UI는 수동으로도 즉시 시작 가능
        def _delayed_start():
            time.sleep(10.0)
            if not self._scr_stop.is_set():
                self.start_screen()
            if not self._cam_stop.is_set():
                self.start_camera()
        threading.Thread(target=_delayed_start, daemon=True,
                         name="EngineDelayedStart").start()

    def stop(self):
        if self.recording: self.stop_recording()
        self.stop_ac()
        self.macro_stop_run()
        self.macro_stop_recording()
        self._scr_stop.set()
        self._cam_stop.set()

    # ── 스탬프 미리보기 ───────────────────────────────────────────────────────
    @staticmethod
    def stamp_preview(frame: np.ndarray, engine: "CoreEngine",
                      source: str) -> np.ndarray:
        out = frame.copy()
        if engine.recording and engine.start_time:
            now = datetime.now()
            ns = now.strftime("%Y-%m-%d  %H:%M:%S.")+f"{now.microsecond//1000:03d}"
            e  = time.time()-engine.start_time
            draw_time_bar(out, ns, f"REC  {fmt_hms(e)}.{int((e%1)*1000):03d}")
        h,w = out.shape[:2]
        for cfg in engine.memo_overlays:
            if not cfg.enabled: continue
            if cfg.target!="both" and cfg.target!=source: continue
            if cfg.tab_idx >= len(engine.memo_texts): continue
            text = engine.memo_texts[cfg.tab_idx].strip()
            if text:
                draw_memo_overlay(out, text.splitlines(), cfg.position,
                                  w, h, cfg.overlay_font_size)  # ★ 탭별
        return out


# =============================================================================
#  공용 위젯 유틸
# =============================================================================
def _make_spinbox(min_v, max_v, val, step=1, decimals=0,
                  special=None, width=None) -> QDoubleSpinBox:
    """스타일이 적용된 QDoubleSpinBox 생성 헬퍼."""
    sb = QDoubleSpinBox()
    sb.setRange(min_v, max_v); sb.setValue(val)
    sb.setSingleStep(step); sb.setDecimals(int(decimals))
    if special: sb.setSpecialValueText(special)
    if width:   sb.setFixedWidth(width)
    sb.setStyleSheet("QDoubleSpinBox{background:#1a1a3a;color:#ddd;"
                     "border:1px solid #3a4a6a;padding:2px 4px;border-radius:3px;}")
    return sb


# =============================================================================
#  PreviewLabel
# =============================================================================
class PreviewLabel(QLabel):
    roi_changed = pyqtSignal()

    def __init__(self, source: str, engine: CoreEngine, parent=None):
        super().__init__(parent)
        self.source=source; self.engine=engine
        self._drawing=False; self._pt1=self._pt2=QPoint()
        self._raw_size=(1,1); self._active=True
        self.setMinimumSize(320,180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self._idle()

    def _idle(self):
        self.setStyleSheet("background:#0d0d1e;border:1px solid #334;")

    def set_active(self, v: bool):
        self._active = v
        if not v:
            self.clear(); self.setText("⏸ Thread Paused")
            self.setStyleSheet("background:#0d0d1e;border:1px solid #334;"
                               "color:#555;font-size:18px;font-weight:bold;")
        else: self.clear(); self._idle()

    def _rois(self):
        return self.engine.screen_rois if self.source=="screen" else self.engine.camera_rois

    def _to_raw(self, qp: QPoint):
        pw,ph=self.width(),self.height(); rw,rh=self._raw_size
        sc=min(pw/rw,ph/rh); ox=(pw-rw*sc)/2; oy=(ph-rh*sc)/2
        return int((qp.x()-ox)/sc), int((qp.y()-oy)/sc)

    def mousePressEvent(self, e):
        if e.button()==Qt.LeftButton:
            self._drawing=True; self._pt1=self._pt2=e.pos()
        elif e.button()==Qt.RightButton:
            if self._rois(): self._rois().pop(); self.roi_changed.emit()

    def mouseMoveEvent(self, e):
        if self._drawing: self._pt2=e.pos(); self.update()

    def mouseReleaseEvent(self, e):
        if self._drawing and e.button()==Qt.LeftButton:
            self._drawing=False
            x1,y1=self._to_raw(self._pt1); x2,y2=self._to_raw(self._pt2)
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
            cv2.putText(disp,f"ROI{i+1}",(rx,max(ry-4,12)),
                        cv2.FONT_HERSHEY_SIMPLEX,0.45,(255,60,60),1)
        h,w,_=disp.shape
        qi=QImage(disp.data,w,h,3*w,QImage.Format_RGB888)
        pix=QPixmap.fromImage(qi).scaled(self.size(),Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation)
        self.setPixmap(pix)


# =============================================================================
#  TimestampMemoEdit
# =============================================================================
class TimestampMemoEdit(QPlainTextEdit):
    _TS_RE = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timestamp_enabled = True

    @classmethod
    def _pat(cls):
        if cls._TS_RE is None:
            cls._TS_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ')
        return cls._TS_RE

    def contextMenuEvent(self, e): e.accept()

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if not self.timestamp_enabled: return
        cur=self.textCursor()
        cur.movePosition(QTextCursor.StartOfBlock)
        cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        line = cur.selectedText()
        ts_now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
        m = self._pat().match(line)
        if e.button()==Qt.LeftButton:
            sc = self.textCursor()
            sc.movePosition(QTextCursor.StartOfBlock)
            if m:
                sc.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, m.end())
            sc.insertText(ts_now)
            self.setTextCursor(sc)
        elif e.button()==Qt.RightButton and m:
            sc=self.textCursor()
            sc.movePosition(QTextCursor.StartOfBlock)
            sc.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, m.end())
            sc.removeSelectedText(); self.setTextCursor(sc)


# =============================================================================
#  CollapsibleSection
# =============================================================================
class CollapsibleSection(QWidget):
    def __init__(self, title: str, color: str="#3a7bd5", parent=None):
        super().__init__(parent)
        self._collapsed=False
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        self._btn=QPushButton(); self._btn.setCheckable(True); self._btn.setChecked(False)
        self._btn.setFixedHeight(34); self._btn.clicked.connect(self._toggle)
        self._title=title; self._color=color; self._style(False)
        outer.addWidget(self._btn)
        self._content=QWidget()
        self._content.setStyleSheet(
            "QWidget{background:#10102a;border:1px solid #2a2a4a;"
            "border-top:none;border-radius:0 0 6px 6px;}")
        cl=QVBoxLayout(self._content); cl.setContentsMargins(8,8,8,10); cl.setSpacing(6)
        self._cl=cl; outer.addWidget(self._content)

    def _style(self, collapsed):
        arrow="▶" if collapsed else "▼"
        self._btn.setText(f"  {arrow}  {self._title}")
        bot="1px solid #2a2a4a" if collapsed else "none"
        rad="6px" if collapsed else "6px 6px 0 0"
        self._btn.setStyleSheet(f"""
            QPushButton{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1e2240,stop:1 #12122a);
                color:#7ab4d4;font-size:12px;font-weight:bold;text-align:left;
                padding:0 12px;border-left:3px solid {self._color};
                border-top:1px solid #2a2a4a;border-right:1px solid #2a2a4a;
                border-bottom:{bot};border-radius:{rad};}}
            QPushButton:hover{{background:#22224a;color:#9ad4f4;}}""")

    def _toggle(self):
        self._collapsed=not self._collapsed
        self._content.setVisible(not self._collapsed); self._style(self._collapsed)

    def add_widget(self, w): self._cl.addWidget(w)
    def set_collapsed(self, v):
        if v!=self._collapsed: self._toggle()
    def is_collapsed(self): return self._collapsed


# =============================================================================
#  MemoOverlayRow
# =============================================================================
class MemoOverlayRow(QWidget):
    """
    오버레이 한 항목 행.
    - 활성화 체크박스 (기본 OFF)
    - 탭 번호, 위치, 대상, 글자 크기(탭별 독립)
    """
    changed = pyqtSignal()
    removed = pyqtSignal(object)
    _POSITIONS = ["top-left","top-right","bottom-left","bottom-right","center"]
    _TARGETS   = ["both","screen","camera"]
    _POS_KR    = ["좌상","우상","좌하","우하","중앙"]
    _TGT_KR    = ["Both","Display","Camera"]

    def __init__(self, cfg: MemoOverlayCfg, tab_count: int, parent=None):
        super().__init__(parent); self.cfg=cfg
        lay=QHBoxLayout(self); lay.setContentsMargins(2,2,2,2); lay.setSpacing(4)

        # ON/OFF 체크박스
        self._en=QCheckBox("ON"); self._en.setChecked(cfg.enabled)
        self._en.setStyleSheet("QCheckBox{font-size:10px;font-weight:bold;color:#2ecc71;}")
        self._en.toggled.connect(self._upd); lay.addWidget(self._en)

        lay.addWidget(QLabel("탭:"))
        self._tab=QSpinBox(); self._tab.setRange(1,max(tab_count,1))
        self._tab.setValue(cfg.tab_idx+1); self._tab.setFixedWidth(44)
        self._tab.valueChanged.connect(self._upd); lay.addWidget(self._tab)

        lay.addWidget(QLabel("위치:"))
        self._pos=QComboBox()
        for p in self._POS_KR: self._pos.addItem(p)
        if cfg.position in self._POSITIONS:
            self._pos.setCurrentIndex(self._POSITIONS.index(cfg.position))
        self._pos.currentIndexChanged.connect(self._upd)
        self._pos.setFixedWidth(58); lay.addWidget(self._pos)

        lay.addWidget(QLabel("대상:"))
        self._tgt=QComboBox()
        for t in self._TGT_KR: self._tgt.addItem(t)
        if cfg.target in self._TARGETS:
            self._tgt.setCurrentIndex(self._TARGETS.index(cfg.target))
        self._tgt.currentIndexChanged.connect(self._upd)
        self._tgt.setFixedWidth(66); lay.addWidget(self._tgt)

        # ★ 탭별 오버레이 글자 크기
        lay.addWidget(QLabel("크기:"))
        self._fsz=QSpinBox(); self._fsz.setRange(8,72)
        self._fsz.setValue(getattr(cfg,'overlay_font_size',18))
        self._fsz.setFixedWidth(50)
        self._fsz.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:3px;font-size:11px;}")
        self._fsz.valueChanged.connect(self._upd); lay.addWidget(self._fsz)

        rm=QPushButton("✕"); rm.setFixedSize(22,22)
        rm.setStyleSheet("QPushButton{background:#3a1a1a;color:#f88;border:none;"
                         "border-radius:3px;}QPushButton:hover{background:#7f2020;}")
        rm.clicked.connect(lambda: self.removed.emit(self)); lay.addWidget(rm)

    def _upd(self):
        self.cfg.enabled           = self._en.isChecked()
        self.cfg.tab_idx           = self._tab.value()-1
        self.cfg.position          = self._POSITIONS[self._pos.currentIndex()]
        self.cfg.target            = self._TARGETS[self._tgt.currentIndex()]
        self.cfg.overlay_font_size = self._fsz.value()
        self.changed.emit()

    def update_tab_max(self, n): self._tab.setMaximum(max(n,1))


# =============================================================================
#  패널 위젯들
# =============================================================================

class RecordingPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._build()
        signals.rec_started.connect(lambda _: self._on_rec_started())
        signals.rec_stopped.connect(self._on_rec_stopped)
        signals.capture_saved.connect(self._on_capture_saved)
        signals.tc_verify_request.connect(self._show_tc_dialog)

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ── 상태 ─────────────────────────────────────────────────────────────
        st=QGroupBox("상태"); g=QGridLayout(st); g.setSpacing(6)
        self.status_lbl=QLabel("● 대기")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        self.timer_lbl=QLabel("00:00:00")
        self.timer_lbl.setStyleSheet("font-size:28px;font-weight:bold;color:#2ecc71;font-family:monospace;")
        self.datetime_lbl=QLabel("—")
        self.datetime_lbl.setStyleSheet("color:#7bc8e0;font-size:11px;font-family:monospace;")
        g.addWidget(self.status_lbl,0,0,1,2)
        g.addWidget(self.timer_lbl,1,0,1,2,Qt.AlignCenter)
        g.addWidget(self.datetime_lbl,2,0,1,2,Qt.AlignCenter)
        g.addWidget(QLabel("Screen FPS:"),3,0)
        self.scr_fps_lbl=QLabel("—"); g.addWidget(self.scr_fps_lbl,3,1)
        g.addWidget(QLabel("Camera FPS:"),4,0)
        self.cam_fps_lbl=QLabel("—"); g.addWidget(self.cam_fps_lbl,4,1)
        self._folder_btn=QPushButton("📂 녹화 폴더"); self._folder_btn.setFixedHeight(26)
        self._folder_btn.clicked.connect(
            lambda: open_folder(self.engine.output_dir or self.engine.base_dir))
        g.addWidget(self._folder_btn,5,0,1,2)
        v.addWidget(st)

        # ── 녹화 소스 선택 (화면 출력과 독립) ────────────────────────────────
        src_g=QGroupBox("📹 녹화 소스  (미리보기 출력 ON/OFF 와 독립적으로 설정)")
        src_l=QHBoxLayout(src_g); src_l.setSpacing(16)
        self.scr_chk=QCheckBox("🖥 Display 저장")
        self.scr_chk.setChecked(True)
        self.scr_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;")
        self.scr_chk.toggled.connect(lambda c: setattr(self.engine,'screen_rec_enabled',c))
        self.cam_chk=QCheckBox("📷 Camera 저장")
        self.cam_chk.setChecked(True)
        self.cam_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#f0c040;")
        self.cam_chk.toggled.connect(lambda c: setattr(self.engine,'camera_rec_enabled',c))
        src_l.addWidget(self.scr_chk); src_l.addWidget(self.cam_chk); src_l.addStretch()
        v.addWidget(src_g)

        # ── 캡처 버튼 ─────────────────────────────────────────────────────────
        cap_g=QGroupBox("📸 현재 프레임 캡처 (PNG 저장)"); cap_l=QHBoxLayout(cap_g); cap_l.setSpacing(8)
        self.cap_scr_btn=QPushButton("🖥 Display 캡처"); self.cap_scr_btn.setFixedHeight(30)
        self.cap_scr_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:4px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#223344;}")
        self.cap_scr_btn.clicked.connect(lambda: self.engine.capture_frame("screen"))
        self.cap_cam_btn=QPushButton("📷 Camera 캡처"); self.cap_cam_btn.setFixedHeight(30)
        self.cap_cam_btn.setStyleSheet(
            "QPushButton{background:#1a2a1a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:4px;font-size:11px;font-weight:bold;}"
            "QPushButton:hover{background:#222a10;}")
        self.cap_cam_btn.clicked.connect(lambda: self.engine.capture_frame("camera"))
        self.cap_lbl=QLabel(""); self.cap_lbl.setStyleSheet("color:#2ecc71;font-size:10px;")
        cap_l.addWidget(self.cap_scr_btn); cap_l.addWidget(self.cap_cam_btn)
        cap_l.addWidget(self.cap_lbl,1); v.addWidget(cap_g)

        # ── T/C 검증 ─────────────────────────────────────────────────────────
        tc_g=QGroupBox("🔬 T/C 검증 목적"); tc_l=QHBoxLayout(tc_g); tc_l.setSpacing(10)
        self.tc_chk=QCheckBox("T/C 검증 목적으로 녹화")
        self.tc_chk.setStyleSheet("font-size:11px;font-weight:bold;color:#f0a040;")
        self.tc_chk.toggled.connect(lambda c: setattr(self.engine,'tc_verify_enabled',c))
        self.tc_hint=QLabel("활성화 시 녹화 종료 전 PASS/FAIL 선택창 표시")
        self.tc_hint.setStyleSheet("color:#666;font-size:10px;")
        tc_l.addWidget(self.tc_chk); tc_l.addWidget(self.tc_hint); tc_l.addStretch()
        v.addWidget(tc_g)

        # ── 컨트롤 버튼 ──────────────────────────────────────────────────────
        ctrl=QGroupBox("컨트롤"); cv=QVBoxLayout(ctrl); cv.setSpacing(8)
        self.btn_start=QPushButton("⏺  녹화 시작  [Ctrl+Alt+W]")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#2ecc71;}"
            "QPushButton:disabled{background:#1a3a28;color:#4a7a5a;}")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop=QPushButton("⏹  녹화 종료  [Ctrl+Alt+E]")
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;font-weight:bold;}"
            "QPushButton:hover{background:#e74c3c;}"
            "QPushButton:disabled{background:#3a1a1a;color:#7a4a4a;}")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        cv.addWidget(self.btn_start); cv.addWidget(self.btn_stop); v.addWidget(ctrl)

        # ── FPS / 배속 ────────────────────────────────────────────────────────
        fg=QGroupBox("FPS & 배속"); fl=QGridLayout(fg); fl.setSpacing(8)
        fl.addWidget(QLabel("화면 Target FPS:"),0,0)
        self.fps_spin=_make_spinbox(1,120,30,1,1); self.fps_spin.setFixedWidth(80)
        self.fps_spin.valueChanged.connect(lambda val: setattr(self.engine,'actual_screen_fps',val))
        fl.addWidget(self.fps_spin,0,1)
        fl.addWidget(QLabel("저장 배속:"),1,0)
        self.speed_spin=_make_spinbox(0.1,10,1,0.25,2); self.speed_spin.setFixedWidth(80)
        self.speed_spin.setStyleSheet(
            "QDoubleSpinBox{background:#1a1a2a;color:#f0c040;border:1px solid #5a5a20;"
            "border-radius:4px;padding:3px;font-size:13px;font-weight:bold;}")
        self.speed_spin.valueChanged.connect(self._on_speed)
        fl.addWidget(self.speed_spin,1,1)
        pr=QHBoxLayout(); pr.setSpacing(4)
        for lbl,val in [("0.5×",.5),("1×",1.),("2×",2.),("4×",4.)]:
            b=QPushButton(lbl); b.setFixedSize(40,24)
            b.setStyleSheet("QPushButton{background:#2a2a1a;color:#f0c040;"
                            "border:1px solid #4a4a20;border-radius:3px;font-size:10px;}")
            b.clicked.connect(lambda _,val=val: self.speed_spin.setValue(val))
            pr.addWidget(b)
        fl.addLayout(pr,2,0,1,2)
        self.speed_info=QLabel("정배속"); self.speed_info.setStyleSheet("color:#888;font-size:10px;")
        fl.addWidget(self.speed_info,3,0,1,2)
        self.speed_lock=QLabel("🔒 녹화 중 배속 변경 불가")
        self.speed_lock.setStyleSheet("color:#e74c3c;font-size:10px;font-weight:bold;")
        self.speed_lock.setVisible(False); fl.addWidget(self.speed_lock,4,0,1,2)
        v.addWidget(fg)

        # ── 용량 절감 ─────────────────────────────────────────────────────────
        cg=QGroupBox("🗜 영상 용량 절감"); cl=QGridLayout(cg); cl.setSpacing(6)
        cl.addWidget(QLabel("코덱:"),0,0)
        self.codec_cb=QComboBox()
        self.codec_cb.addItems(["mp4v (기본/안전)","avc1 / H.264 (소형, 지원 시)","xvid (호환성)"])
        self.codec_cb.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;border-radius:3px;padding:2px;}")
        self.codec_cb.currentIndexChanged.connect(self._on_codec)
        cl.addWidget(self.codec_cb,0,1)
        self.codec_warn=QLabel("※ avc1 지원 불가 시 자동으로 mp4v로 전환")
        self.codec_warn.setStyleSheet("color:#666;font-size:9px;"); self.codec_warn.setVisible(False)
        cl.addWidget(self.codec_warn,1,0,1,2)
        cl.addWidget(QLabel("해상도:"),2,0)
        self.scale_cb=QComboBox()
        self.scale_cb.addItems(["100% 원본","75%","50%"])
        self.scale_cb.setStyleSheet(
            "QComboBox{background:#1a1a3a;color:#ddd;border:1px solid #3a4a6a;border-radius:3px;padding:2px;}")
        self.scale_cb.currentIndexChanged.connect(self._on_scale)
        cl.addWidget(self.scale_cb,2,1)
        v.addWidget(cg)

        self._timer=QTimer(self); self._timer.timeout.connect(self._tick)

    # ── 슬롯 ─────────────────────────────────────────────────────────────────
    def _on_start(self):
        self.engine.start_recording()

    def _on_stop(self):
        """
        녹화 종료.
        T/C 검증 활성화 → PASS/FAIL 모달 창 표시 후 종료.
        취소 선택 시 녹화 유지.
        """
        if self.engine.tc_verify_enabled and self.engine.recording:
            result = self._show_tc_dialog()
            if result is None:          # 취소 → 녹화 계속
                return
            self.engine.tc_verify_result = result
        self.engine.stop_recording()

    def _show_tc_dialog(self):
        """
        T/C 검증 결과 선택 모달 다이얼로그.
        반환: "PASS" | "FAIL" | None(취소)

        Python 3.9 이하 호환 (str | None 타입힌트 없이 작성).
        결과를 리스트[0]에 저장하는 방식으로 클로저 안전성 확보.
        """
        result_box = [None]   # 클로저에서 안전하게 수정 가능한 컨테이너

        dlg = QDialog(self.parent() or self)
        dlg.setWindowTitle("T/C 검증 결과 입력")
        dlg.setWindowModality(Qt.ApplicationModal)   # ★ 완전 모달 보장
        dlg.setWindowFlags(
            Qt.Dialog |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.MSWindowsFixedSizeDialogHint)
        dlg.setFixedSize(420, 220)
        dlg.setStyleSheet(
            "QDialog{background:#0d0d1e;}"
            "QLabel{color:#dde;}"
            "QPushButton{border-radius:6px;border:none;}")

        lay = QVBoxLayout(dlg)
        lay.setSpacing(20); lay.setContentsMargins(28, 24, 28, 20)

        # 안내 문구
        lbl = QLabel("녹화를 종료합니다.\nT/C 검증 결과를 선택하세요.")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("font-size:14px;font-weight:bold;color:#f0c040;")
        lay.addWidget(lbl)

        # PASS / FAIL 버튼
        btn_row = QHBoxLayout(); btn_row.setSpacing(20)

        btn_pass = QPushButton("✅  PASS")
        btn_pass.setMinimumHeight(48); btn_pass.setMinimumWidth(140)
        btn_pass.setStyleSheet(
            "QPushButton{background:#1a6a3a;color:#afffcf;font-size:17px;"
            "font-weight:bold;border-radius:8px;border:2px solid #2a9a5a;}"
            "QPushButton:hover{background:#27ae60;color:#fff;}"
            "QPushButton:pressed{background:#1a5a30;}")

        btn_fail = QPushButton("❌  FAIL")
        btn_fail.setMinimumHeight(48); btn_fail.setMinimumWidth(140)
        btn_fail.setStyleSheet(
            "QPushButton{background:#6a1a1a;color:#ffaaaa;font-size:17px;"
            "font-weight:bold;border-radius:8px;border:2px solid #aa3a3a;}"
            "QPushButton:hover{background:#c0392b;color:#fff;}"
            "QPushButton:pressed{background:#5a1010;}")

        def _pick(r):
            result_box[0] = r
            dlg.accept()

        btn_pass.clicked.connect(lambda: _pick("PASS"))
        btn_fail.clicked.connect(lambda: _pick("FAIL"))
        btn_row.addWidget(btn_pass); btn_row.addWidget(btn_fail)
        lay.addLayout(btn_row)

        # 취소 버튼
        btn_cancel = QPushButton("취소  (녹화 계속)")
        btn_cancel.setFixedHeight(30)
        btn_cancel.setStyleSheet(
            "QPushButton{background:#1a1a2a;color:#888;border:1px solid #3a3a5a;"
            "font-size:11px;padding:4px 16px;border-radius:4px;}"
            "QPushButton:hover{background:#252535;color:#aab;}")
        btn_cancel.clicked.connect(dlg.reject)
        lay.addWidget(btn_cancel, alignment=Qt.AlignCenter)

        # 모달로 실행 (블로킹)
        dlg.exec_()
        return result_box[0]

    def _on_rec_started(self):
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self.status_lbl.setText("● 녹화 중")
        self.status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;font-size:14px;")
        self.speed_spin.setEnabled(False); self.speed_lock.setVisible(True)
        self._timer.start(500)

    def _on_rec_stopped(self):
        self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
        self.status_lbl.setText("● 대기")
        self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        self.timer_lbl.setText("00:00:00"); self.datetime_lbl.setText("—")
        self.speed_spin.setEnabled(True); self.speed_lock.setVisible(False)
        self._timer.stop()

    def _tick(self):
        if self.engine.recording and self.engine.start_time:
            e=time.time()-self.engine.start_time
            self.timer_lbl.setText(fmt_hms(e))
            self.datetime_lbl.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

    def _on_capture_saved(self, source, path):
        self.cap_lbl.setText(f"✅ {source}: {os.path.basename(path)}")
        QTimer.singleShot(4000, lambda: self.cap_lbl.setText(""))

    def _on_speed(self, val):
        self.engine.playback_speed=val
        if val==1.0: t="정배속"
        elif val<1.0: t=f"{val:.2f}× 슬로우모션"
        else:          t=f"{val:.2f}× 타임랩스"
        self.speed_info.setText(t)

    def _on_codec(self, i):
        codecs=["mp4v","avc1","xvid"]
        self.engine.video_codec=codecs[i] if i<len(codecs) else "mp4v"
        self.codec_warn.setVisible(i==1)  # avc1 선택 시 경고 표시

    def _on_scale(self, i):
        self.engine.video_scale=[1.0,0.75,0.5][i]

    def update_fps(self, scr: float, cam: float):
        self.scr_fps_lbl.setText(f"{scr:.1f} fps")
        self.cam_fps_lbl.setText(f"{cam:.1f} fps")


class ManualClipPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._led=False; self._led_timer=QTimer(self)
        self._led_timer.timeout.connect(self._blink)
        signals.manual_clip_saved.connect(self._on_saved)
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        info=QLabel("버튼을 누르면 해당 시점 기준 전/후 N초를 버퍼에서 추출해 저장합니다.\n최대 ±40초 저장 가능 (버퍼 크기에 따라 달라질 수 있습니다)")
        info.setWordWrap(True); info.setStyleSheet("color:#778;font-size:10px;"); v.addWidget(info)

        # 소스
        sg=QGroupBox("저장 소스"); sl=QHBoxLayout(sg); sl.setSpacing(12)
        self.scr_chk=QCheckBox("🖥 Display"); self.scr_chk.setChecked(True)
        self.cam_chk=QCheckBox("📷 Camera");  self.cam_chk.setChecked(True)
        self.scr_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#7bc8e0;")
        self.cam_chk.setStyleSheet("font-size:12px;font-weight:bold;color:#f0c040;")
        self.scr_chk.toggled.connect(self._upd_src); self.cam_chk.toggled.connect(self._upd_src)
        sl.addWidget(self.scr_chk); sl.addWidget(self.cam_chk); sl.addStretch(); v.addWidget(sg)

        # 전/후 시간
        tg=QGroupBox("전/후 시간  (최대 40초)"); tgl=QGridLayout(tg); tgl.setSpacing(6)
        self.pre_spin  = self._make_row(tgl,0,"🔵 전 (초)","#7bc8e0","manual_pre_sec",0,40,10)
        self.post_spin = self._make_row(tgl,1,"🟠 후 (초)","#f0a040","manual_post_sec",0,40,10)
        v.addWidget(tg)

        # 버튼
        br=QHBoxLayout(); br.setSpacing(8)
        self.led_lbl=QLabel("●"); self.led_lbl.setFixedWidth(22)
        self.led_lbl.setAlignment(Qt.AlignCenter)
        self.led_lbl.setStyleSheet("font-size:22px;color:#333;"); br.addWidget(self.led_lbl)
        self.clip_btn=QPushButton("🎬  지금 클립 저장  [Ctrl+Alt+M]")
        self.clip_btn.setMinimumHeight(42)
        self.clip_btn.setStyleSheet(
            "QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #7d3c98,stop:1 #e67e22);color:white;font-size:13px;"
            "font-weight:bold;border:none;border-radius:6px;padding:6px;}"
            "QPushButton:hover{background:#9b59b6;}"
            "QPushButton:disabled{background:#2a2a3a;color:#666;border:1px solid #444;}")
        self.clip_btn.clicked.connect(self._on_clip); br.addWidget(self.clip_btn,1); v.addLayout(br)
        self.status_lbl=QLabel("대기 중"); self.status_lbl.setStyleSheet("color:#888;font-size:11px;font-family:monospace;"); v.addWidget(self.status_lbl)
        open_btn=QPushButton("📂 수동클립 폴더"); open_btn.setFixedHeight(26)
        open_btn.clicked.connect(lambda: open_folder(self.engine.manual_dir)); v.addWidget(open_btn)

        self._poll=QTimer(self); self._poll.timeout.connect(self._check_state); self._poll.start(200)

    def _make_row(self, grid, row, label, color, attr, min_v, max_v, val):
        lbl=QLabel(label); lbl.setStyleSheet(f"font-weight:bold;color:{color};")
        sp=QDoubleSpinBox(); sp.setRange(min_v,max_v); sp.setValue(val)
        sp.setSingleStep(1.0); sp.setDecimals(1); sp.setMinimumHeight(28)
        sp.setStyleSheet(f"QDoubleSpinBox{{background:#1a1a3a;color:{color};"
                         "border:1px solid #3a3a5a;border-radius:4px;padding:3px;"
                         "font-size:13px;font-weight:bold;}")
        sl=QSlider(Qt.Horizontal); sl.setRange(0,int(max_v*10)); sl.setValue(int(val*10))
        sl.setStyleSheet(
            f"QSlider::groove:horizontal{{background:#1a2a3a;height:6px;border-radius:3px;}}"
            f"QSlider::handle:horizontal{{background:{color};width:16px;height:16px;"
            "margin-top:-5px;margin-bottom:-5px;border-radius:8px;}}"
            "QSlider::sub-page:horizontal{background:#3a4a6a;border-radius:3px;}")
        sp.valueChanged.connect(lambda v,sl=sl,a=attr: (sl.blockSignals(True),sl.setValue(int(v*10)),sl.blockSignals(False),setattr(self.engine,a,v)))
        sl.valueChanged.connect(lambda v,sp=sp,a=attr: (sp.blockSignals(True),sp.setValue(v/10),sp.blockSignals(False),setattr(self.engine,a,v/10)))
        grid.addWidget(lbl,row,0); grid.addWidget(sp,row,1); grid.addWidget(sl,row,2)
        grid.setColumnStretch(2,1)
        return sp

    def _upd_src(self):
        s=self.scr_chk.isChecked(); c=self.cam_chk.isChecked()
        if not s and not c:
            self.sender().blockSignals(True); self.sender().setChecked(True)
            self.sender().blockSignals(False); return
        self.engine.manual_source=("both" if s and c else "screen" if s else "camera")

    def _on_clip(self):
        ok=self.engine.save_manual_clip()
        if not ok:
            self.status_lbl.setText("⏳ 쿨다운 중 — 잠시 후 재시도")
            self.status_lbl.setStyleSheet("color:#f0c040;font-size:11px;")

    def _on_saved(self, path):
        self.status_lbl.setText(f"✅ 저장: {os.path.basename(path)}")
        self.status_lbl.setStyleSheet("color:#2ecc71;font-size:11px;")

    def _check_state(self):
        idle = (self.engine.manual_state == CoreEngine.MANUAL_IDLE)
        if idle:
            self._led_timer.stop(); self.led_lbl.setStyleSheet("font-size:22px;color:#333;")
            self.clip_btn.setEnabled(True)
        else:
            if not self._led_timer.isActive(): self._led_timer.start(400)
            self.clip_btn.setEnabled(False)

    def _blink(self):
        self._led=not self._led
        self.led_lbl.setStyleSheet(f"font-size:22px;color:{'#e74c3c' if self._led else '#7f2a2a'};")


class BlackoutPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        signals.blackout_detected.connect(self._on_bo)
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        self.rec_chk=QCheckBox("블랙아웃 클립 녹화 활성화")
        self.rec_chk.setChecked(True)
        self.rec_chk.toggled.connect(lambda c: setattr(self.engine,'blackout_rec_enabled',c))
        v.addWidget(self.rec_chk)

        tg=QGroupBox("감지 설정"); tl=QGridLayout(tg)
        tl.addWidget(QLabel("밝기 변화 임계값:"),0,0)
        self.thr_spin=_make_spinbox(5,200,30,1,0,"",80)
        self.thr_spin.setSuffix(" (0~255)")
        self.thr_spin.valueChanged.connect(lambda v: setattr(self.engine,'brightness_threshold',v))
        tl.addWidget(self.thr_spin,0,1)
        tl.addWidget(QLabel("쿨다운 (초):"),1,0)
        self.cd_spin=_make_spinbox(0.5,60,5,0.5,1,"",80)
        self.cd_spin.valueChanged.connect(lambda v: setattr(self.engine,'blackout_cooldown',v))
        tl.addWidget(self.cd_spin,1,1)
        v.addWidget(tg)

        cnt=QGroupBox("감지 횟수"); cl=QGridLayout(cnt)
        cl.addWidget(QLabel("화면:"),0,0); self.scr_cnt=QLabel("0")
        self.scr_cnt.setStyleSheet("font-weight:bold;color:#e74c3c;"); cl.addWidget(self.scr_cnt,0,1)
        cl.addWidget(QLabel("카메라:"),1,0); self.cam_cnt=QLabel("0")
        self.cam_cnt.setStyleSheet("font-weight:bold;color:#e74c3c;"); cl.addWidget(self.cam_cnt,1,1)
        open_btn=QPushButton("📂 블랙아웃 폴더"); open_btn.setFixedHeight(26)
        open_btn.clicked.connect(lambda: open_folder(self.engine.blackout_dir))
        cl.addWidget(open_btn,2,0,1,2)
        v.addWidget(cnt)

        roi_g=QGroupBox("ROI 밝기 현황"); rl=QVBoxLayout(roi_g)
        self.roi_txt=QTextEdit(); self.roi_txt.setReadOnly(True); self.roi_txt.setFixedHeight(100)
        self.roi_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;")
        rl.addWidget(self.roi_txt); v.addWidget(roi_g)

        ev_g=QGroupBox("최근 이벤트"); el=QVBoxLayout(ev_g)
        self.ev_txt=QTextEdit(); self.ev_txt.setReadOnly(True); self.ev_txt.setFixedHeight(80)
        self.ev_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;")
        el.addWidget(self.ev_txt); v.addWidget(ev_g)

    def _on_bo(self, source, event):
        self.scr_cnt.setText(str(self.engine.screen_bo_count))
        self.cam_cnt.setText(str(self.engine.camera_bo_count))

    def refresh(self):
        self.scr_cnt.setText(str(self.engine.screen_bo_count))
        self.cam_cnt.setText(str(self.engine.camera_bo_count))
        lines=[]
        for src,avgs,ov in [("Scr",self.engine.screen_roi_avg,self.engine.screen_overall_avg),
                             ("Cam",self.engine.camera_roi_avg,self.engine.camera_overall_avg)]:
            if avgs:
                b,g,r=ov; br=0.114*b+0.587*g+0.299*r
                lines.append(f"[{src}] R{int(r)} G{int(g)} B{int(b)} Br:{int(br)}")
                for i,a in enumerate(avgs[:5]):
                    b2,g2,r2=a; br2=0.114*b2+0.587*g2+0.299*r2
                    lines.append(f"  ROI{i+1}: Br:{int(br2)}")
        self.roi_txt.setPlainText("\n".join(lines))
        ev=[]
        for src,evs in [("화면",self.engine.screen_bo_events),("카메라",self.engine.camera_bo_events)]:
            for e in reversed(evs[-4:]):
                ev.append(f"[{src}] {e['time']}  변화:{int(e['brightness_change'])}")
        self.ev_txt.setPlainText("\n".join(ev))


class AutoClickPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        signals.ac_count_changed.connect(lambda n: self.lcd.display(n))
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        ig=QGroupBox("클릭 간격"); il=QGridLayout(ig)
        il.addWidget(QLabel("간격 (초):"),0,0)
        self.interval_spin=_make_spinbox(0.1,3600,1,0.1,1,"",90)
        self.interval_spin.valueChanged.connect(lambda v: setattr(self.engine,'ac_interval',v))
        il.addWidget(self.interval_spin,0,1)
        pr=QHBoxLayout()
        for lbl,val in [("0.1s",.1),("0.5s",.5),("1s",1.),("5s",5.),("10s",10.)]:
            b=QPushButton(lbl); b.setFixedSize(44,22)
            b.setStyleSheet("QPushButton{background:#1e2a3a;border:1px solid #3a4a6a;color:#ccd;font-size:10px;border-radius:3px;}")
            b.clicked.connect(lambda _,v=val: self.interval_spin.setValue(v)); pr.addWidget(b)
        il.addLayout(pr,1,0,1,2); v.addWidget(ig)

        cg=QGroupBox("클릭 카운터"); cl=QGridLayout(cg)
        from PyQt5.QtWidgets import QLCDNumber
        self.lcd=QLCDNumber(8); self.lcd.setSegmentStyle(QLCDNumber.Flat)
        self.lcd.setFixedHeight(44)
        self.lcd.setStyleSheet("background:#0d1520;border:1px solid #336;color:#2ecc71;")
        cl.addWidget(self.lcd,0,0,1,2)
        rst=QPushButton("카운터 초기화"); rst.setFixedHeight(26)
        rst.clicked.connect(self.engine.reset_ac_count); cl.addWidget(rst,1,0,1,2)
        v.addWidget(cg)

        ctrl=QGroupBox("제어"); ctl=QVBoxLayout(ctrl); ctl.setSpacing(6)
        self.btn_start=QPushButton("▶  시작  [Ctrl+Alt+A]")
        self.btn_start.setStyleSheet("QPushButton{background:#2980b9;color:white;font-size:12px;padding:7px;border-radius:5px;border:none;font-weight:bold;}QPushButton:hover{background:#3498db;}QPushButton:disabled{background:#1a2a3a;color:#4a6a8a;}")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop=QPushButton("■  정지  [Ctrl+Alt+S]")
        self.btn_stop.setStyleSheet("QPushButton{background:#5a6a7a;color:white;font-size:12px;padding:7px;border-radius:5px;border:none;font-weight:bold;}QPushButton:hover{background:#7f8c8d;}QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self.btn_stop.clicked.connect(self._on_stop); self.btn_stop.setEnabled(False)
        self.status_lbl=QLabel("● 정지"); self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;")
        ctl.addWidget(self.btn_start); ctl.addWidget(self.btn_stop); ctl.addWidget(self.status_lbl)
        v.addWidget(ctrl)

    def _on_start(self):
        self.engine.start_ac(); self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True)
        self.status_lbl.setText("● 실행 중"); self.status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;")

    def _on_stop(self):
        self.engine.stop_ac(); self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)
        self.status_lbl.setText("● 정지"); self.status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;")


class MacroPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._slots: list = []
        self._active_slot = 0
        signals.macro_step_rec.connect(self._on_step)
        self._build()
        QTimer.singleShot(0, self._init_slots)

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ── 슬롯 ──────────────────────────────────────────────────────────
        sg=QGroupBox("🗂 매크로 슬롯"); sl=QVBoxLayout(sg); sl.setSpacing(5)
        sc=QHBoxLayout(); sc.setSpacing(6)
        sc.addWidget(QLabel("슬롯:"))
        self.slot_cb=QComboBox()
        self.slot_cb.setStyleSheet(
            "QComboBox{background:#0f2a1a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:3px;font-weight:bold;font-size:11px;min-width:100px;}")
        self.slot_cb.currentIndexChanged.connect(self._on_slot_changed)
        sc.addWidget(self.slot_cb,1)
        for lbl,fn,c in [("＋","_add_slot","#1a4a2a"),
                          ("－","_del_slot","#3a1a1a"),
                          ("✏","_rename_slot","#1a2a3a")]:
            b=QPushButton(lbl); b.setFixedSize(28,26)
            b.setStyleSheet(
                f"QPushButton{{background:{c};color:#ddd;"
                "border:1px solid #4a6a4a;border-radius:3px;}}")
            b.clicked.connect(getattr(self,fn)); sc.addWidget(b)
        sl.addLayout(sc)
        self.slot_info=QLabel("슬롯 1/1  |  0 스텝")
        self.slot_info.setStyleSheet("color:#556;font-size:10px;font-family:monospace;")
        sl.addWidget(self.slot_info); v.addWidget(sg)

        # ── 기록 설정 ────────────────────────────────────────────────────
        rg=QGroupBox("📍 이벤트 기록"); rl=QVBoxLayout(rg); rl.setSpacing(6)

        # 기록할 이벤트 유형 선택
        evt_row=QHBoxLayout(); evt_row.setSpacing(10)
        evt_row.addWidget(QLabel("기록 대상:"))
        self.rec_click_chk = QCheckBox("🖱 클릭")
        self.rec_drag_chk  = QCheckBox("↔ 드래그")
        self.rec_key_chk   = QCheckBox("⌨ 키보드")
        for chk, default in [(self.rec_click_chk, True),
                              (self.rec_drag_chk,  True),
                              (self.rec_key_chk,   True)]:
            chk.setChecked(default)
            chk.setStyleSheet("QCheckBox{font-size:11px;color:#dde;spacing:4px;}")
            evt_row.addWidget(chk)
        evt_row.addStretch(); rl.addLayout(evt_row)

        rb=QHBoxLayout()
        self.rec_btn=QPushButton("⏺  기록 시작")
        self.rec_btn.setCheckable(True); self.rec_btn.setFixedHeight(32)
        self.rec_btn.setStyleSheet(
            "QPushButton{background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:5px;font-size:12px;font-weight:bold;}"
            "QPushButton:checked{background:#c0392b;color:#fff;border:2px solid #e74c3c;}")
        self.rec_btn.toggled.connect(self._on_rec_toggle)
        self.rec_st=QLabel("● 대기")
        self.rec_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb.addWidget(self.rec_btn,1); rb.addWidget(self.rec_st); rl.addLayout(rb)

        self.last_evt_lbl=QLabel("마지막 이벤트: —")
        self.last_evt_lbl.setStyleSheet("color:#f0c040;font-size:11px;font-family:monospace;")
        rl.addWidget(self.last_evt_lbl); v.addWidget(rg)

        # ── 스텝 테이블 (6컬럼) ─────────────────────────────────────────
        tg=QGroupBox("📋 이벤트 스텝"); tv=QVBoxLayout(tg); tv.setSpacing(4)
        self.tbl=QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["#", "타입", "좌표/키", "딜레이(s)", "옵션"])
        hdr=self.tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.Fixed)
        self.tbl.setColumnWidth(0, 30)
        self.tbl.setColumnWidth(1, 58)
        self.tbl.setColumnWidth(3, 70)
        self.tbl.setColumnWidth(4, 60)
        self.tbl.setFixedHeight(180)
        self.tbl.setStyleSheet(
            "QTableWidget{background:#0a0a18;color:#ccc;font-size:10px;"
            "border:1px solid #1a2a3a;gridline-color:#1a2030;}"
            "QHeaderView::section{background:#0f1a2a;color:#7ab4d4;"
            "font-size:10px;border:none;padding:3px;}")
        self.tbl.itemChanged.connect(self._on_item_changed)
        tv.addWidget(self.tbl)

        tb=QHBoxLayout(); tb.setSpacing(4)
        for lbl,fn in [("↑","_step_up"),("↓","_step_dn")]:
            b=QPushButton(lbl); b.setFixedSize(28,24)
            b.clicked.connect(getattr(self,fn)); tb.addWidget(b)
        tb.addStretch()
        for lbl,fn in [("선택 삭제","_del_step"),("전체 삭제","_clear_steps")]:
            b=QPushButton(lbl); b.setFixedHeight(24)
            b.clicked.connect(getattr(self,fn)); tb.addWidget(b)
        tv.addLayout(tb)

        # 딜레이 일괄 설정
        br=QHBoxLayout(); br.addWidget(QLabel("일괄 딜레이:"))
        self.bulk_spin=_make_spinbox(0.0,60,0.5,0.1,2,"",70)
        bb=QPushButton("적용"); bb.setFixedHeight(24)
        bb.clicked.connect(self._bulk_delay)
        br.addWidget(self.bulk_spin); br.addWidget(QLabel("초"))
        br.addWidget(bb); br.addStretch()
        tv.addLayout(br); v.addWidget(tg)

        # ── 실행 설정 ────────────────────────────────────────────────────
        run=QGroupBox("▶ 실행"); rn=QGridLayout(run); rn.setSpacing(6)
        rn.addWidget(QLabel("반복:"),0,0)
        self.rep_spin=_make_spinbox(0,9999,1,1,0,"∞",70)
        self.rep_spin.valueChanged.connect(
            lambda v: setattr(self.engine,'macro_repeat',int(v)))
        rn.addWidget(self.rep_spin,0,1)
        rn.addWidget(QLabel("루프 간격(초):"),1,0)
        self.gap_spin=_make_spinbox(0,60,1,0.5,1,"",70)
        self.gap_spin.valueChanged.connect(
            lambda v: setattr(self.engine,'macro_gap',v))
        rn.addWidget(self.gap_spin,1,1)

        rb2=QHBoxLayout(); rb2.setSpacing(6)
        self.run_btn=QPushButton("▶  실행"); self.run_btn.setFixedHeight(32)
        self.run_btn.setStyleSheet(
            "QPushButton{background:#2980b9;color:#fff;border:none;border-radius:5px;"
            "font-size:12px;font-weight:bold;}QPushButton:hover{background:#3498db;}"
            "QPushButton:disabled{background:#1a3a5a;color:#555;}")
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn=QPushButton("■  중단"); self.stop_btn.setFixedHeight(32)
        self.stop_btn.setStyleSheet(
            "QPushButton{background:#5a6a7a;color:#fff;border:none;border-radius:5px;"
            "font-size:12px;}QPushButton:hover{background:#7f8c8d;}"
            "QPushButton:disabled{background:#2a2a2a;color:#555;}")
        self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self._on_stop)
        self.run_st=QLabel("● 대기")
        self.run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")
        rb2.addWidget(self.run_btn,1); rb2.addWidget(self.stop_btn,1)
        rn.addLayout(rb2,2,0,1,2); rn.addWidget(self.run_st,3,0,1,2)
        v.addWidget(run)

    # ── 슬롯 관리 ──────────────────────────────────────────────────────────
    def _init_slots(self):
        if not self._slots:
            self._slots.append({'title':'슬롯 1','steps':[]})
            self.slot_cb.blockSignals(True)
            self.slot_cb.addItem('슬롯 1')
            self.slot_cb.blockSignals(False)
        self._active_slot=0; self.slot_cb.setCurrentIndex(0)
        self._sync(); self._info_upd()

    def _sync(self):
        if 0 <= self._active_slot < len(self._slots):
            self.engine.macro_steps.clear()
            self.engine.macro_steps.extend(self._slots[self._active_slot]['steps'])
        else:
            self.engine.macro_steps.clear()

    def _save_cur(self):
        if 0 <= self._active_slot < len(self._slots):
            self._slots[self._active_slot]['steps'] = list(self.engine.macro_steps)

    def _info_upd(self):
        n=len(self._slots); idx=self._active_slot
        nm=self._slots[idx]['title'] if 0<=idx<n else "—"
        self.slot_info.setText(
            f"{nm}  |  슬롯 {idx+1}/{n}  |  {len(self.engine.macro_steps)} 스텝")

    def _on_slot_changed(self, idx):
        self._save_cur(); self._active_slot=idx
        self._sync(); self._rebuild_tbl(); self._info_upd()

    def _add_slot(self):
        n=len(self._slots)+1; t=f"슬롯 {n}"
        self._slots.append({'title':t,'steps':[]})
        self.slot_cb.blockSignals(True)
        self.slot_cb.addItem(t); self.slot_cb.blockSignals(False)
        self.slot_cb.setCurrentIndex(len(self._slots)-1)

    def _del_slot(self):
        if len(self._slots)<=1: return
        idx=self._active_slot; self._slots.pop(idx)
        self.slot_cb.blockSignals(True)
        self.slot_cb.removeItem(idx); self.slot_cb.blockSignals(False)
        new=max(0,idx-1); self._active_slot=new
        self.slot_cb.setCurrentIndex(new)
        self._sync(); self._rebuild_tbl(); self._info_upd()

    def _rename_slot(self):
        idx=self._active_slot
        if not 0<=idx<len(self._slots): return
        old=self._slots[idx]['title']
        new,ok=QInputDialog.getText(self,"슬롯 이름 변경","새 이름:",text=old)
        if ok and new.strip():
            self._slots[idx]['title']=new.strip()
            self.slot_cb.blockSignals(True)
            self.slot_cb.setItemText(idx,new.strip())
            self.slot_cb.blockSignals(False)
            self._info_upd()

    # ── 기록 ──────────────────────────────────────────────────────────────
    def _on_rec_toggle(self, recording):
        if recording:
            self.rec_btn.setText("⏹  기록 중단")
            self.rec_st.setText("● 기록 중")
            self.rec_st.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")
            # 체크박스에 따라 engine 필터 전달 (engine이 직접 체크)
            self.engine._rec_filter = {
                'click': self.rec_click_chk.isChecked(),
                'drag':  self.rec_drag_chk.isChecked(),
                'key':   self.rec_key_chk.isChecked(),
            }
            self.engine.macro_start_recording()
        else:
            self.rec_btn.setText("⏺  기록 시작")
            self.rec_st.setText("● 완료")
            self.rec_st.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
            self.engine.macro_stop_recording()
            QTimer.singleShot(200, self._make_editable)

    # ── 시그널 수신 ──────────────────────────────────────────────────────
    def _on_step(self, step: MacroStep):
        """engine emit loop에서 필터가 이미 적용된 MacroStep만 수신."""
        self._append_row(step, editable=False)
        self.last_evt_lbl.setText(step.summary())
        self._info_upd()

    # ── 테이블 렌더링 ────────────────────────────────────────────────────
    def _type_color(self, kind: str) -> str:
        return {'click':'#7bc8e0','drag':'#f0c040','key':'#afffcf'}.get(kind,'#ccc')

    def _append_row(self, step: MacroStep, editable=True):
        ef = Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsEditable
        rf = Qt.ItemIsEnabled|Qt.ItemIsSelectable
        self.tbl.blockSignals(True)
        r = self.tbl.rowCount(); self.tbl.insertRow(r)

        # 열 0: 순번
        it0=QTableWidgetItem(str(r+1))
        it0.setFlags(Qt.ItemIsEnabled); it0.setTextAlignment(Qt.AlignCenter)
        it0.setForeground(QColor("#556")); self.tbl.setItem(r,0,it0)

        # 열 1: 타입 레이블
        it1=QTableWidgetItem(step.kind.upper())
        it1.setFlags(rf); it1.setTextAlignment(Qt.AlignCenter)
        it1.setForeground(QColor(self._type_color(step.kind)))
        self.tbl.setItem(r,1,it1)

        # 열 2: 좌표/키 요약 (편집 가능)
        coord_str = self._step_coord_str(step)
        it2=QTableWidgetItem(coord_str); it2.setTextAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        it2.setFlags(ef if editable else rf)
        if not editable: it2.setForeground(QColor("#999"))
        self.tbl.setItem(r,2,it2)

        # 열 3: 딜레이 (편집 가능)
        it3=QTableWidgetItem(f"{step.delay:.3f}")
        it3.setTextAlignment(Qt.AlignCenter)
        it3.setFlags(ef if editable else rf)
        if not editable: it3.setForeground(QColor("#999"))
        self.tbl.setItem(r,3,it3)

        # 열 4: 옵션 (버튼/더블 표시)
        opt = ""
        if step.kind == 'click':
            opt = step.button[:1].upper()
            if step.double: opt += " x2"
        elif step.kind == 'drag':
            opt = step.button[:1].upper()
        it4=QTableWidgetItem(opt); it4.setFlags(rf)
        it4.setTextAlignment(Qt.AlignCenter); it4.setForeground(QColor("#888"))
        self.tbl.setItem(r,4,it4)

        self.tbl.scrollToBottom(); self.tbl.blockSignals(False)

    @staticmethod
    def _step_coord_str(step: MacroStep) -> str:
        if step.kind == 'click':
            return f"({step.x},{step.y})"
        elif step.kind == 'drag':
            return f"({step.x},{step.y})→({step.x2},{step.y2})"
        elif step.kind == 'key':
            return step.key_str
        return ""

    def _make_editable(self):
        ef=Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsEditable
        self.tbl.blockSignals(True)
        for r in range(self.tbl.rowCount()):
            for c in (2,3):
                it=self.tbl.item(r,c)
                if it: it.setFlags(ef); it.setForeground(QColor("#ddd"))
        self.tbl.blockSignals(False)

    def _rebuild_tbl(self):
        self.tbl.blockSignals(True); self.tbl.setRowCount(0)
        self.tbl.blockSignals(False)
        for step in self.engine.macro_steps:
            self._append_row(step, editable=True)

    def _on_item_changed(self, item):
        r=item.row(); c=item.column()
        if r >= len(self.engine.macro_steps): return
        step = self.engine.macro_steps[r]
        try:
            txt = item.text().strip()
            if c == 3:
                step.delay = max(0.0, float(txt))
            elif c == 2:
                # 좌표 파싱
                if step.kind == 'click':
                    m = re.fullmatch(r'\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)', txt)
                    if m: step.x, step.y = int(m.group(1)), int(m.group(2))
                elif step.kind == 'drag':
                    m = re.fullmatch(
                        r'\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*→\s*\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)',
                        txt)
                    if m:
                        step.x,step.y = int(m.group(1)),int(m.group(2))
                        step.x2,step.y2 = int(m.group(3)),int(m.group(4))
                elif step.kind == 'key':
                    step.key_str = txt
        except: pass

    def _del_step(self):
        rows=sorted({i.row() for i in self.tbl.selectedItems()},reverse=True)
        self.tbl.blockSignals(True)
        for r in rows:
            self.tbl.removeRow(r)
            if r<len(self.engine.macro_steps): self.engine.macro_steps.pop(r)
        for r in range(self.tbl.rowCount()):
            it=self.tbl.item(r,0)
            if it: it.setText(str(r+1))
        self.tbl.blockSignals(False); self._info_upd()

    def _clear_steps(self):
        self.tbl.blockSignals(True); self.tbl.setRowCount(0)
        self.tbl.blockSignals(False)
        self.engine.macro_clear(); self._info_upd()

    def _step_up(self):
        r=self.tbl.currentRow(); s=self.engine.macro_steps
        if r<=0 or r>=len(s): return
        s[r-1],s[r]=s[r],s[r-1]; self._rebuild_tbl(); self.tbl.setCurrentCell(r-1,0)

    def _step_dn(self):
        r=self.tbl.currentRow(); s=self.engine.macro_steps
        if r<0 or r>=len(s)-1: return
        s[r],s[r+1]=s[r+1],s[r]; self._rebuild_tbl(); self.tbl.setCurrentCell(r+1,0)

    def _bulk_delay(self):
        d=self.bulk_spin.value(); self.tbl.blockSignals(True)
        for i,step in enumerate(self.engine.macro_steps):
            step.delay=d
            it=self.tbl.item(i,3)
            if it: it.setText(f"{d:.3f}")
        self.tbl.blockSignals(False)

    # ── 실행 ──────────────────────────────────────────────────────────────
    def _on_run(self):
        if not self.engine.macro_steps:
            self.signals.status_message.emit("[Macro] 스텝이 없습니다"); return
        self.engine.macro_start_run()
        self.run_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.run_st.setText("● 실행 중")
        self.run_st.setStyleSheet("color:#2ecc71;font-size:11px;font-weight:bold;")
        self._watch=QTimer(self)
        self._watch.timeout.connect(self._check_done)
        self._watch.start(300)

    def _check_done(self):
        if not self.engine.macro_running:
            self._watch.stop(); self.run_btn.setEnabled(True); self.stop_btn.setEnabled(False)
            self.run_st.setText("● 완료")
            self.run_st.setStyleSheet("color:#888;font-size:11px;font-weight:bold;")

    def _on_stop(self):
        self.engine.macro_stop_run()
        self.run_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        self.run_st.setText("● 중단")
        self.run_st.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;")

    # ── DB 직렬화 ─────────────────────────────────────────────────────────
    def get_slots_data(self) -> list:
        self._save_cur()
        return [{'title': s['title'],
                 'steps': [st.to_dict() for st in s['steps']]}
                for s in self._slots]

    def set_slots_data(self, data: list):
        self._slots.clear()
        self.slot_cb.blockSignals(True); self.slot_cb.clear()
        for s in data:
            steps=[MacroStep.from_dict(st) for st in s.get('steps',[])]
            self._slots.append({'title':s['title'],'steps':steps})
            self.slot_cb.addItem(s['title'])
        self.slot_cb.blockSignals(False)
        self._active_slot=0; self.slot_cb.setCurrentIndex(0)
        self._sync(); self._rebuild_tbl(); self._info_upd()


class SchedulePanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._build()
        self._warn_timer=QTimer(self); self._warn_timer.timeout.connect(self._check_past); self._warn_timer.start(1000)

    def _dt_edit(self, color, border) -> QDateTimeEdit:
        dte=QDateTimeEdit(); dte.setDisplayFormat("yyyy-MM-dd  HH:mm:ss")
        dte.setCalendarPopup(True); dte.setMinimumHeight(30)
        dte.setStyleSheet(
            f"QDateTimeEdit{{background:#1a1a3a;color:{color};"
            f"border:1px solid {border};border-radius:4px;padding:4px 6px;"
            "font-size:12px;font-family:monospace;}}"
            "QDateTimeEdit::drop-down{border:none;}"
            "QDateTimeEdit::down-arrow{image:none;width:0;}")
        return dte

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        inp=QGroupBox("새 예약 추가"); ig=QVBoxLayout(inp); ig.setSpacing(8)

        # 시작
        ig.addWidget(QLabel("🟢  녹화 시작 시각"))
        rs=QHBoxLayout(); rs.setSpacing(6)
        self.s_chk=QCheckBox("사용"); self.s_chk.setChecked(True)
        self.s_dt=self._dt_edit("#2ecc71","#2a6a3a")
        self.s_dt.setDateTime(QDateTime.currentDateTime().addSecs(60))
        b=QPushButton("지금"); b.setFixedSize(44,30)
        b.clicked.connect(lambda: self.s_dt.setDateTime(QDateTime.currentDateTime()))
        rs.addWidget(self.s_chk); rs.addWidget(self.s_dt,1); rs.addWidget(b); ig.addLayout(rs)

        # 종료
        ig.addWidget(QLabel("🔴  녹화 종료 시각"))
        re=QHBoxLayout(); re.setSpacing(6)
        self.e_chk=QCheckBox("사용"); self.e_chk.setChecked(True)
        self.e_dt=self._dt_edit("#e74c3c","#6a2a2a")
        self.e_dt.setDateTime(QDateTime.currentDateTime().addSecs(3660))
        b2=QPushButton("지금"); b2.setFixedSize(44,30)
        b2.clicked.connect(lambda: self.e_dt.setDateTime(QDateTime.currentDateTime()))
        re.addWidget(self.e_chk); re.addWidget(self.e_dt,1); re.addWidget(b2); ig.addLayout(re)

        # 매크로 옵션
        mac_row=QHBoxLayout()
        self.mac_chk=QCheckBox("매크로도 실행"); self.mac_chk.setChecked(False)
        mac_row.addWidget(self.mac_chk)
        mac_row.addWidget(QLabel("반복:"))
        self.mac_rep=QSpinBox(); self.mac_rep.setRange(0,9999); self.mac_rep.setValue(1)
        self.mac_rep.setSpecialValueText("∞"); self.mac_rep.setFixedWidth(60); mac_row.addWidget(self.mac_rep)
        mac_row.addWidget(QLabel("간격(초):"))
        self.mac_gap=QDoubleSpinBox(); self.mac_gap.setRange(0,60); self.mac_gap.setValue(1.0)
        self.mac_gap.setFixedWidth(60); mac_row.addWidget(self.mac_gap); mac_row.addStretch()
        ig.addLayout(mac_row)

        add_btn=QPushButton("＋  예약 추가"); add_btn.setMinimumHeight(30)
        add_btn.setStyleSheet("QPushButton{background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;border-radius:4px;font-weight:bold;}QPushButton:hover{background:#225a3a;}")
        add_btn.clicked.connect(self._add); ig.addWidget(add_btn); v.addWidget(inp)

        # 목록
        lst=QGroupBox("예약 목록"); ll=QVBoxLayout(lst)
        self.tbl=QTableWidget(0,5)
        self.tbl.setHorizontalHeaderLabels(["#","시작","종료","액션","상태"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setFixedHeight(140)
        self.tbl.setStyleSheet("QTableWidget{background:#0d0d1e;color:#ccc;font-size:11px;border:1px solid #334;}QHeaderView::section{background:#1a1a3a;color:#9ab;font-size:11px;border:none;padding:4px;}")
        ll.addWidget(self.tbl)
        br=QHBoxLayout()
        for lbl,fn in [("선택 삭제","_del"),("전체 삭제","_clear")]:
            b=QPushButton(lbl); b.setFixedHeight(26); b.clicked.connect(getattr(self,fn)); br.addWidget(b)
        ll.addLayout(br); v.addWidget(lst)

        cd=QGroupBox("다음 예약까지"); cl=QVBoxLayout(cd)
        self.cd_lbl=QLabel("예약 없음"); self.cd_lbl.setStyleSheet("color:#f0c040;font-family:monospace;font-size:12px;")
        cl.addWidget(self.cd_lbl); v.addWidget(cd)

    def _add(self):
        start_dt=stop_dt=None
        now=datetime.now()
        if self.s_chk.isChecked():
            qd=self.s_dt.dateTime(); d=qd.date(); t=qd.time()
            start_dt=datetime(d.year(),d.month(),d.day(),t.hour(),t.minute(),t.second())
        if self.e_chk.isChecked():
            qd=self.e_dt.dateTime(); d=qd.date(); t=qd.time()
            stop_dt=datetime(d.year(),d.month(),d.day(),t.hour(),t.minute(),t.second())
        if start_dt and start_dt<now: QMessageBox.warning(self,"오류","시작 시각이 과거입니다."); return
        if stop_dt and stop_dt<now:   QMessageBox.warning(self,"오류","종료 시각이 과거입니다."); return
        if start_dt and stop_dt and stop_dt<=start_dt: QMessageBox.warning(self,"오류","종료 > 시작이어야 합니다."); return
        if not start_dt and not stop_dt: QMessageBox.warning(self,"오류","시작/종료 중 하나 이상 설정하세요."); return
        actions=['rec_start','rec_stop']
        if self.mac_chk.isChecked(): actions.append('macro_run')
        e=ScheduleEntry(start_dt,stop_dt,actions,
                        macro_repeat=self.mac_rep.value(),
                        macro_gap=self.mac_gap.value())
        self.engine.schedules.append(e)
        self._add_row(e)

    def _add_row(self, e: ScheduleEntry):
        r=self.tbl.rowCount(); self.tbl.insertRow(r)
        s=e.start_dt.strftime("%m/%d %H:%M:%S") if e.start_dt else "—"
        en=e.stop_dt.strftime("%m/%d %H:%M:%S") if e.stop_dt else "—"
        acts="+".join(e.actions)
        for c,val in enumerate([str(e.id),s,en,acts,"대기"]):
            it=QTableWidgetItem(val); it.setTextAlignment(Qt.AlignCenter)
            self.tbl.setItem(r,c,it)

    def _del(self):
        rows=sorted({i.row() for i in self.tbl.selectedItems()},reverse=True)
        for r in rows:
            it=self.tbl.item(r,0)
            if it: self.engine.schedules=[s for s in self.engine.schedules if s.id!=int(it.text())]
            self.tbl.removeRow(r)

    def _clear(self): self.engine.schedules.clear(); self.tbl.setRowCount(0)

    def _check_past(self):
        now=QDateTime.currentDateTime()
        ok_s=("QDateTimeEdit{background:#1a1a3a;color:#2ecc71;border:1px solid #2a6a3a;"
               "border-radius:4px;padding:4px 6px;font-size:12px;font-family:monospace;}"
               "QDateTimeEdit::drop-down{border:none;}QDateTimeEdit::down-arrow{image:none;width:0;}")
        ok_e=("QDateTimeEdit{background:#1a1a3a;color:#e74c3c;border:1px solid #6a2a2a;"
               "border-radius:4px;padding:4px 6px;font-size:12px;font-family:monospace;}"
               "QDateTimeEdit::drop-down{border:none;}QDateTimeEdit::down-arrow{image:none;width:0;}")
        bad=("QDateTimeEdit{background:#2a0a0a;color:#ff6b6b;border:2px solid #e74c3c;"
             "border-radius:4px;padding:4px 6px;font-size:12px;font-family:monospace;}"
             "QDateTimeEdit::drop-down{border:none;}QDateTimeEdit::down-arrow{image:none;width:0;}")
        self.s_dt.setStyleSheet(bad if self.s_dt.dateTime()<now else ok_s)
        self.e_dt.setStyleSheet(bad if self.e_dt.dateTime()<now else ok_e)

    def refresh_tbl(self):
        now=datetime.now()
        for r in range(self.tbl.rowCount()):
            it=self.tbl.item(r,0)
            if not it: continue
            e=next((s for s in self.engine.schedules if s.id==int(it.text())),None)
            if not e: continue
            st=self.tbl.item(r,4)
            if st:
                if e.done:    st.setText("완료"); st.setForeground(QColor("#888"))
                elif e.started: st.setText("진행중"); st.setForeground(QColor("#2ecc71"))
                else:           st.setText("대기");  st.setForeground(QColor("#f0c040"))
        pending=[s for s in self.engine.schedules if not s.done]
        if pending:
            nxt=min(pending,key=lambda s:(s.start_dt or s.stop_dt or datetime.max))
            ref=nxt.start_dt or nxt.stop_dt
            if ref:
                secs=int((ref-now).total_seconds())
                if secs>=0: self.cd_lbl.setText(f"#{nxt.id}까지  {secs//3600:02d}h {(secs%3600)//60:02d}m {secs%60:02d}s")
                else:       self.cd_lbl.setText(f"#{nxt.id} 진행 중…")
        else: self.cd_lbl.setText("예약 없음")


class MemoPanel(QWidget):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self._editors: list = []
        self._overlay_rows: list = []
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(6)

        # ── 현재 탭 설정 (탭 전환 시 자동 반영) ─────────────────────────────
        cg=QGroupBox("📝 현재 탭 설정  (탭 전환 시 자동 반영)")
        cl=QVBoxLayout(cg); cl.setSpacing(6)

        # 에디터 글꼴 크기
        font_row=QHBoxLayout(); font_row.setSpacing(6)
        font_row.addWidget(QLabel("에디터 글꼴:"))
        self.font_spin=QSpinBox(); self.font_spin.setRange(8,36)
        self.font_spin.setValue(11); self.font_spin.setFixedWidth(52)
        self.font_spin.setStyleSheet(
            "QSpinBox{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;font-weight:bold;font-size:13px;}")
        self.font_sl=QSlider(Qt.Horizontal); self.font_sl.setRange(8,36); self.font_sl.setValue(11)
        self.font_sl.setStyleSheet(
            "QSlider::groove:horizontal{background:#1a2030;height:6px;border-radius:3px;}"
            "QSlider::handle:horizontal{background:#f0c040;width:16px;height:16px;"
            "margin-top:-5px;margin-bottom:-5px;border-radius:8px;}"
            "QSlider::sub-page:horizontal{background:#5a5a20;border-radius:3px;}")
        self.font_spin.valueChanged.connect(
            lambda v:(self.font_sl.blockSignals(True),self.font_sl.setValue(v),
                      self.font_sl.blockSignals(False),self._apply_font_cur(v)))
        self.font_sl.valueChanged.connect(
            lambda v:(self.font_spin.blockSignals(True),self.font_spin.setValue(v),
                      self.font_spin.blockSignals(False),self._apply_font_cur(v)))
        font_row.addWidget(self.font_sl,1); font_row.addWidget(self.font_spin)
        for lbl,sz in [("S",10),("M",13),("L",16),("XL",20)]:
            b=QPushButton(lbl); b.setFixedSize(28,22)
            b.setStyleSheet("QPushButton{background:#2a2a1a;color:#f0c040;"
                            "border:1px solid #4a4a20;border-radius:3px;font-size:10px;}")
            b.clicked.connect(lambda _,s=sz: self.font_spin.setValue(s))
            font_row.addWidget(b)
        cl.addLayout(font_row)

        # 타임스탬프 ON/OFF
        ts_row=QHBoxLayout()
        self.ts_chk=QCheckBox("좌클릭 타임스탬프 삽입"); self.ts_chk.setChecked(True)
        self.ts_chk.setStyleSheet("font-size:11px;font-weight:bold;color:#7bc8e0;")
        self.ts_chk.toggled.connect(self._on_ts_toggled)
        ts_row.addWidget(self.ts_chk); ts_row.addWidget(QLabel("(우클릭: 제거)"))
        ts_row.addStretch()
        cl.addLayout(ts_row)
        v.addWidget(cg)

        # 탭 조작 버튼 — _add_tab_new/_del_tab/_clear_cur 에 올바르게 연결
        tc=QHBoxLayout(); tc.setSpacing(6)
        add_tb=QPushButton("＋ 탭"); add_tb.setFixedHeight(26)
        add_tb.setStyleSheet("background:#1a3a1a;color:#8fa;border:1px solid #2a6a2a;border-radius:4px;font-size:11px;")
        add_tb.clicked.connect(self._add_tab_new)
        del_tb=QPushButton("－ 탭"); del_tb.setFixedHeight(26)
        del_tb.setStyleSheet("background:#3a1a1a;color:#f88;border:1px solid #6a2a2a;border-radius:4px;font-size:11px;")
        del_tb.clicked.connect(self._del_tab)
        clr_tb=QPushButton("현재 탭 지우기"); clr_tb.setFixedHeight(26)
        clr_tb.setStyleSheet("background:#1a1a3a;color:#aaa;border:1px solid #334;border-radius:4px;font-size:11px;")
        clr_tb.clicked.connect(self._clear_cur)
        tc.addWidget(add_tb); tc.addWidget(del_tb); tc.addWidget(clr_tb)
        tc.addStretch(); v.addLayout(tc)

        # 탭 위젯
        self.tabs=QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane{border:1px solid #2a2a4a;background:#0d0d1e;}"
            "QTabBar::tab{background:#1a1a3a;color:#888;border:1px solid #334;"
            "border-bottom:none;border-radius:4px 4px 0 0;padding:4px 10px;"
            "font-size:11px;min-width:60px;}"
            "QTabBar::tab:selected{background:#2a2a5a;color:#dde;border-color:#446;}"
            "QTabBar::tab:hover{background:#22224a;color:#bbd;}")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._add_tab("메모 1"); v.addWidget(self.tabs)

        # 오버레이 설정
        ovg=QGroupBox("🖼 오버레이 설정  (위치 / 대상 / 탭)"); ovl=QVBoxLayout(ovg); ovl.setSpacing(4)
        self._ov_cont=QWidget(); self._ov_lay=QVBoxLayout(self._ov_cont)
        self._ov_lay.setContentsMargins(0,0,0,0); self._ov_lay.setSpacing(3)
        ovl.addWidget(self._ov_cont)
        add_ov=QPushButton("＋ 오버레이 추가"); add_ov.setFixedHeight(26)
        add_ov.setStyleSheet("background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;border-radius:4px;font-size:11px;")
        add_ov.clicked.connect(self._add_overlay); ovl.addWidget(add_ov)
        v.addWidget(ovg)
        self._rebuild_ov_rows()

    def _add_tab(self, title, content="", font_size=11, ts_enabled=True):
        ed=TimestampMemoEdit()
        ed.setPlaceholderText("메모 입력… (좌클릭: 타임스탬프 | 우클릭: 제거)")
        ed.setStyleSheet("background:#0d0d1e;color:#ffe;border:none;font-family:monospace;")
        ed.timestamp_enabled=ts_enabled
        f=ed.font(); f.setPointSize(max(8,font_size)); ed.setFont(f)
        if content: ed.setPlainText(content)
        ed.textChanged.connect(self._sync_texts)
        self._editors.append(ed)
        self.tabs.addTab(ed,title)
        while len(self.engine.memo_texts)<len(self._editors):
            self.engine.memo_texts.append("")
        self._upd_ov_max()

    def _sync_texts(self):
        for i,ed in enumerate(self._editors):
            if i<len(self.engine.memo_texts): self.engine.memo_texts[i]=ed.toPlainText()
            else: self.engine.memo_texts.append(ed.toPlainText())

    def _on_tab_changed(self, idx):
        """탭 전환 시 에디터 글꼴 크기와 타임스탬프 설정을 현재 탭 값으로 반영."""
        if not 0<=idx<len(self._editors): return
        ed=self._editors[idx]
        fs=ed.font().pointSize()
        if fs<=0: fs=11
        for w in (self.font_spin,self.font_sl):
            w.blockSignals(True); w.setValue(fs); w.blockSignals(False)
        self.ts_chk.blockSignals(True)
        self.ts_chk.setChecked(ed.timestamp_enabled)
        self.ts_chk.blockSignals(False)

    def _apply_font_cur(self, size):
        idx=self.tabs.currentIndex()
        if 0<=idx<len(self._editors):
            f=self._editors[idx].font(); f.setPointSize(max(8,size)); self._editors[idx].setFont(f)

    def _on_ts_toggled(self, v):
        idx=self.tabs.currentIndex()
        if 0<=idx<len(self._editors): self._editors[idx].timestamp_enabled=v

    def _add_tab_new(self):
        """＋ 탭 버튼에서 호출 — 새 탭을 자동 이름으로 추가하고 포커스 이동."""
        n = self.tabs.count() + 1
        self._add_tab(f"메모 {n}")
        self.tabs.setCurrentIndex(self.tabs.count() - 1)

    def _del_tab(self):
        if self.tabs.count()<=1: return
        idx=self.tabs.currentIndex(); self.tabs.removeTab(idx)
        if idx<len(self._editors): self._editors.pop(idx)
        if idx<len(self.engine.memo_texts): self.engine.memo_texts.pop(idx)
        self._upd_ov_max(); self._on_tab_changed(self.tabs.currentIndex())

    def _clear_cur(self):
        idx=self.tabs.currentIndex()
        if idx<len(self._editors): self._editors[idx].clear()

    def _rebuild_ov_rows(self):
        while self._ov_lay.count():
            it=self._ov_lay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._overlay_rows.clear()
        for cfg in self.engine.memo_overlays: self._add_ov_row(cfg)

    def _add_ov_row(self, cfg):
        n=max(self.tabs.count(),1); row=MemoOverlayRow(cfg,n)
        row.removed.connect(self._rm_ov_row); self._overlay_rows.append(row); self._ov_lay.addWidget(row)

    def _rm_ov_row(self, row):
        if len(self.engine.memo_overlays)<=1: return
        if row.cfg in self.engine.memo_overlays: self.engine.memo_overlays.remove(row.cfg)
        self._ov_lay.removeWidget(row); row.deleteLater()
        if row in self._overlay_rows: self._overlay_rows.remove(row)

    def _add_overlay(self):
        cfg=MemoOverlayCfg(0,"bottom-right","both",True)
        self.engine.memo_overlays.append(cfg); self._add_ov_row(cfg)

    def _upd_ov_max(self):
        n=max(self.tabs.count(),1)
        for row in self._overlay_rows: row.update_tab_max(n)

    def get_tab_data(self) -> list:
        tabs=[]
        for i in range(self.tabs.count()):
            if i>=len(self._editors): break
            ed=self._editors[i]
            fs=ed.font().pointSize()
            if fs<=0: fs=11
            tabs.append({'title':self.tabs.tabText(i),'content':ed.toPlainText(),
                         'font_size':fs,'ts_enabled':ed.timestamp_enabled})
        return tabs

    def set_tab_data(self, tabs: list):
        while self.tabs.count()>0: self.tabs.removeTab(0)
        self._editors.clear(); self.engine.memo_texts.clear()
        for t in tabs:
            self._add_tab(t['title'],t['content'],
                          t.get('font_size',11),t.get('ts_enabled',True))
        self._on_tab_changed(self.tabs.currentIndex())


class PathSettingsPanel(QWidget):
    """저장 경로 설정 (차종_버전 / TC-ID)"""
    def __init__(self, engine: CoreEngine, db: SettingsDB, parent=None):
        super().__init__(parent)
        self.engine=engine; self.db=db; self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        info=QLabel(
            "저장 경로 구조:\n"
            "~/Desktop/bltn_rec/[차종_버전]/YYYYMMDD/[TC-ID]/Rec_HHMMSS/\n\n"
            "항목을 비워두면 해당 폴더 단계가 생략됩니다.\n"
            "T/C 검증 사용 시: [(PASS)|(FAIL)] Rec_HHMMSS/ 로 저장")
        info.setWordWrap(True)
        info.setStyleSheet(
            "color:#778;font-size:10px;border:1px solid #1a2a3a;"
            "border-radius:4px;padding:6px;background:#0d0d1e;")
        v.addWidget(info)

        g=QGroupBox("경로 항목"); gl=QGridLayout(g); gl.setSpacing(8)

        # 차종_버전
        gl.addWidget(QLabel("차종_버전 (ex: TK1_2541, NX5_2633):"),0,0)
        self.vehicle_ed=QLineEdit()
        self.vehicle_ed.setPlaceholderText("예: TK1_2541")
        self.vehicle_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#f0c040;border:1px solid #4a4a20;"
            "border-radius:3px;padding:4px 8px;font-size:12px;font-weight:bold;}")
        self.vehicle_ed.textChanged.connect(lambda t: setattr(self.engine,'vehicle_type',t.strip()))
        self.vehicle_ed.textChanged.connect(self._upd_preview)
        gl.addWidget(self.vehicle_ed,0,1)

        # TC-ID
        gl.addWidget(QLabel("TC-ID (ex: BLTN_CAM_TC_3-0001):"),1,0)
        self.tc_ed=QLineEdit()
        self.tc_ed.setPlaceholderText("예: BLTN_CAM_TC_3-0001")
        self.tc_ed.setStyleSheet(
            "QLineEdit{background:#1a1a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:3px;padding:4px 8px;font-size:12px;}")
        self.tc_ed.textChanged.connect(lambda t: setattr(self.engine,'tc_id',t.strip()))
        self.tc_ed.textChanged.connect(self._upd_preview)
        gl.addWidget(self.tc_ed,1,1)
        v.addWidget(g)

        save_btn=QPushButton("💾  경로 설정 저장"); save_btn.setMinimumHeight(34)
        save_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:4px;font-weight:bold;}QPushButton:hover{background:#225a3a;}")
        save_btn.clicked.connect(self._save)
        v.addWidget(save_btn)

        self.result_lbl=QLabel("")
        self.result_lbl.setStyleSheet("font-size:11px;")
        v.addWidget(self.result_lbl)

        self.preview_lbl=QLabel("")
        self.preview_lbl.setStyleSheet(
            "color:#556;font-size:10px;font-family:monospace;")
        self.preview_lbl.setWordWrap(True)
        v.addWidget(self.preview_lbl)

        open_btn=QPushButton("📂 기본 폴더 열기"); open_btn.setFixedHeight(26)
        open_btn.clicked.connect(lambda: open_folder(self.engine.base_dir))
        v.addWidget(open_btn)

    def _upd_preview(self):
        vv=self.vehicle_ed.text().strip(); t=self.tc_ed.text().strip()
        parts=["~/Desktop/bltn_rec"]
        if vv: parts.append(vv)
        parts.append("YYYYMMDD")
        if t: parts.append(t)
        parts.append("Rec_HHMMSS")
        self.preview_lbl.setText("경로 미리보기:\n"+os.path.join(*parts))

    def _save(self):
        self.db.set_path_settings(
            self.vehicle_ed.text().strip(), self.tc_ed.text().strip())
        self.result_lbl.setText("✅ 저장 완료")
        self.result_lbl.setStyleSheet("color:#2ecc71;font-size:11px;")
        QTimer.singleShot(2000, lambda: self.result_lbl.setText(""))

    def load_from_db(self):
        ps=self.db.get_path_settings()
        self.vehicle_ed.setText(ps.get('vehicle_type',''))
        self.tc_ed.setText(ps.get('tc_id',''))
        self._upd_preview()


class ResetPanel(QWidget):
    def __init__(self, engine: CoreEngine, db: SettingsDB,
                 on_reset_cb, parent=None):
        super().__init__(parent)
        self.engine=engine; self.db=db; self.on_reset_cb=on_reset_cb
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(10)
        info=QLabel(
            "설정 초기화 시 아래 항목이 기본값으로 되돌아갑니다.\n"
            "• FPS / 배속 / 임계값 / 쿨다운\n• 오토클릭 간격 / 매크로 반복\n"
            "• 전/후 시간 / 메모 탭 내용\n• 섹션 접힘 상태\n\n녹화 중에는 초기화 불가")
        info.setWordWrap(True); info.setStyleSheet("color:#999;font-size:11px;padding:4px;border:1px solid #2a2a3a;border-radius:4px;background:#0d0d1e;"); v.addWidget(info)
        chk_g=QGroupBox("초기화 항목"); cg=QVBoxLayout(chk_g); cg.setSpacing(5)
        self.chk_settings=QCheckBox("설정값 (FPS, 배속, 임계값 등)"); self.chk_settings.setChecked(True)
        self.chk_memo=QCheckBox("메모 탭 전체 내용"); self.chk_memo.setChecked(False)
        self.chk_sections=QCheckBox("섹션 접힘 상태"); self.chk_sections.setChecked(True)
        self.chk_db=QCheckBox("DB 전체 초기화"); self.chk_db.setChecked(False)
        self.chk_db.setStyleSheet("color:#e09040;font-weight:bold;")
        for chk in [self.chk_settings,self.chk_memo,self.chk_sections,self.chk_db]:
            cg.addWidget(chk); v.addWidget(chk_g)
        cf_g=QGroupBox("확인 입력"); cfl=QVBoxLayout(cf_g); cfl.setSpacing(5)
        cfl.addWidget(QLabel("RESET 을 입력 후 버튼을 클릭하세요"))
        self.confirm_ed=QLineEdit(); self.confirm_ed.setPlaceholderText("RESET")
        self.confirm_ed.setStyleSheet("QLineEdit{background:#0d0d1e;color:#f0c040;border:1px solid #4a4a20;border-radius:4px;padding:4px 8px;font-size:13px;font-family:monospace;font-weight:bold;}")
        cfl.addWidget(self.confirm_ed); v.addWidget(cf_g)
        btn=QPushButton("🔄  설정 초기화 실행"); btn.setMinimumHeight(40)
        btn.setStyleSheet("QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #4a1a1a,stop:1 #6a2a2a);color:#ffaaaa;font-size:13px;font-weight:bold;border:1px solid #8a3a3a;border-radius:6px;padding:6px;}QPushButton:hover{background:#7a2a2a;color:white;}")
        btn.clicked.connect(self._on_reset); v.addWidget(btn)
        self.result_lbl=QLabel(""); self.result_lbl.setStyleSheet("font-size:11px;"); v.addWidget(self.result_lbl)

    def _on_reset(self):
        if self.engine.recording:
            self.result_lbl.setText("⚠ 녹화 중에는 초기화할 수 없습니다.")
            self.result_lbl.setStyleSheet("color:#e74c3c;font-size:11px;font-weight:bold;"); return
        if self.confirm_ed.text().strip() != "RESET":
            self.result_lbl.setText("❌ 'RESET'을 정확히 입력하세요.")
            self.result_lbl.setStyleSheet("color:#e74c3c;font-size:11px;"); return
        self.on_reset_cb(self.chk_settings.isChecked(), self.chk_memo.isChecked(),
                         self.chk_sections.isChecked(), self.chk_db.isChecked())
        self.confirm_ed.clear()
        self.result_lbl.setText("✅ 초기화 완료!"); self.result_lbl.setStyleSheet("color:#2ecc71;font-size:12px;font-weight:bold;")
        QTimer.singleShot(3000, lambda: self.result_lbl.setText(""))


class LogPanel(QWidget):
    def __init__(self, signals: Signals, parent=None):
        super().__init__(parent)
        signals.status_message.connect(self.append)
        self._build()

    def _build(self):
        v=QVBoxLayout(self); v.setContentsMargins(0,0,0,0); v.setSpacing(4)
        self.txt=QTextEdit(); self.txt.setReadOnly(True); self.txt.setFixedHeight(200)
        self.txt.setStyleSheet("font-family:monospace;font-size:10px;background:#080810;color:#aaa;")
        cl=QPushButton("Clear"); cl.setFixedHeight(24); cl.clicked.connect(self.txt.clear)
        v.addWidget(self.txt); v.addWidget(cl)

    def append(self, msg: str):
        ts=datetime.now().strftime("%H:%M:%S")
        self.txt.append(f"[{ts}] {msg}")


# =============================================================================
#  CameraWindow
# =============================================================================
class CameraWindow(QDialog):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self.setWindowTitle("📷  Camera Feed")
        self.setWindowFlags(Qt.Window|Qt.WindowMinimizeButtonHint|Qt.WindowMaximizeButtonHint|Qt.WindowCloseButtonHint)
        self.resize(680,520); self.setMinimumSize(420,280)
        self.setStyleSheet("background:#0d0d1e;color:#ddd;")
        self._build()
        signals.cameras_scanned.connect(self._on_scanned)

    def _build(self):
        root=QVBoxLayout(self); root.setSpacing(0); root.setContentsMargins(0,0,0,0)

        # ── 헤더 ─────────────────────────────────────────────────────────────
        hdr=QFrame(); hdr.setStyleSheet("QFrame{background:#0a0a18;border-bottom:1px solid #1e2a3a;}")
        hdr.setFixedHeight(40)
        hl=QHBoxLayout(hdr); hl.setContentsMargins(10,0,8,0); hl.setSpacing(8)
        hl.addWidget(QLabel("📷"))
        t=QLabel("Camera Preview"); t.setStyleSheet("color:#9ab;font-weight:bold;font-size:13px;")
        hl.addWidget(t); hl.addStretch()
        self._fps_lbl=QLabel("FPS: —"); self._fps_lbl.setStyleSheet("color:#888;font-size:11px;")
        hl.addWidget(self._fps_lbl)
        self._toggle=QPushButton("⏸ 일시정지"); self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:13px;font-size:10px;padding:2px 10px;}"
            "QPushButton:checked{background:#3a1a1a;color:#f88;}")
        self._toggle.toggled.connect(self._on_toggle); hl.addWidget(self._toggle)
        # ★ 스캔 UI 접기/펼치기 버튼
        self._fold_btn=QPushButton("▲ 스캔 숨기기"); self._fold_btn.setCheckable(True)
        self._fold_btn.setChecked(False)
        self._fold_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7ab4d4;border:1px solid #2a4a6a;"
            "border-radius:13px;font-size:10px;padding:2px 8px;}"
            "QPushButton:checked{background:#0d1a2a;color:#446;}")
        self._fold_btn.toggled.connect(self._on_fold); hl.addWidget(self._fold_btn)
        root.addWidget(hdr)

        # ── 카메라 스캔 패널 (접기 가능) ────────────────────────────────────
        self._cam_scan_panel=QFrame()
        self._cam_scan_panel.setStyleSheet(
            "QFrame{background:#0c0c1e;border-bottom:1px solid #1a2a3a;}")
        self._cam_scan_panel.setMaximumHeight(140)
        cf=QVBoxLayout(self._cam_scan_panel)
        cf.setContentsMargins(10,8,10,8); cf.setSpacing(5)
        sr=QHBoxLayout()
        self._scan_btn=QPushButton("🔍 카메라 스캔"); self._scan_btn.setFixedHeight(28)
        self._scan_btn.setStyleSheet(
            "QPushButton{background:#1a2a4a;color:#7bc8e0;border:1px solid #2a4a7a;"
            "border-radius:4px;font-size:11px;padding:2px 12px;}"
            "QPushButton:hover{background:#223366;}"
            "QPushButton:disabled{background:#0d1525;color:#446;}")
        self._scan_btn.clicked.connect(self._on_scan)
        self._sel_lbl=QLabel("선택: —")
        self._sel_lbl.setStyleSheet("color:#f0c040;font-size:11px;font-weight:bold;")
        sr.addWidget(self._scan_btn); sr.addStretch(); sr.addWidget(self._sel_lbl)
        cf.addLayout(sr)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFixedHeight(68)
        scroll.setStyleSheet("QScrollArea{border:1px solid #1a2a3a;background:#080818;}")
        self._cb_cont=QWidget(); self._cb_cont.setStyleSheet("background:#080818;")
        self._cb_lay=QVBoxLayout(self._cb_cont)
        self._cb_lay.setContentsMargins(4,4,4,4); self._cb_lay.setSpacing(2)
        scroll.setWidget(self._cb_cont); cf.addWidget(scroll)
        root.addWidget(self._cam_scan_panel)

        # ── 미리보기 ─────────────────────────────────────────────────────────
        pc=QWidget(); pc.setStyleSheet("background:#0d0d1e;")
        pv=QVBoxLayout(pc); pv.setContentsMargins(6,4,6,4); pv.setSpacing(3)
        self.lbl=PreviewLabel("camera",self.engine); pv.addWidget(self.lbl,1)
        hint=QLabel("Left-drag: ROI 추가  |  Right-click: ROI 제거")
        hint.setStyleSheet("color:#444;font-size:10px;"); hint.setAlignment(Qt.AlignCenter)
        pv.addWidget(hint)
        root.addWidget(pc,1)

        QTimer(self, timeout=self._upd_fps, interval=2000).start()
        threading.Thread(target=self.engine.scan_cameras, daemon=True).start()

    def _on_fold(self, folded: bool):
        """스캔 패널 접기/펼치기."""
        self._cam_scan_panel.setVisible(not folded)
        self._fold_btn.setText("▼ 스캔 표시" if folded else "▲ 스캔 숨기기")

    def _on_scanned(self, cams):
        while self._cb_lay.count():
            it=self._cb_lay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._cbs={}
        if not cams:
            lbl=QLabel("  카메라 없음"); lbl.setStyleSheet("color:#666;font-size:11px;"); self._cb_lay.addWidget(lbl); return
        for cam in cams:
            cb=QCheckBox(f"  {cam['name']}")
            cb.setStyleSheet("QCheckBox{color:#ccd;font-size:11px;spacing:6px;padding:4px 8px;}")
            cb.setChecked(cam["idx"]==self.engine.active_cam_idx)
            cb.toggled.connect(lambda chk,idx=cam["idx"],c=cb:self._on_cb(idx,chk,c))
            self._cbs[cam["idx"]]=cb; self._cb_lay.addWidget(cb)
        self._cb_lay.addStretch()
        active=next((c for c in cams if c["idx"]==self.engine.active_cam_idx),None)
        if active: self._sel_lbl.setText(f"선택: {active['name']}")

    def _on_cb(self, idx, checked, cb):
        if not checked: cb.blockSignals(True); cb.setChecked(True); cb.blockSignals(False); return
        if idx==self.engine.active_cam_idx: return
        for oi,ocb in self._cbs.items():
            if oi!=idx: ocb.blockSignals(True); ocb.setChecked(False); ocb.blockSignals(False)
        cam=next((c for c in self.engine.camera_list if c["idx"]==idx),None)
        if cam:
            self.engine.active_cam_idx=idx; self.engine.actual_camera_fps=cam["fps"]
            self._sel_lbl.setText(f"선택: {cam['name']}")
            self.engine.restart_camera()

    def _on_scan(self):
        self._scan_btn.setEnabled(False); threading.Thread(target=self.engine.scan_cameras,daemon=True).start()
        QTimer.singleShot(600,lambda:self._scan_btn.setEnabled(True))

    def _on_toggle(self, paused):
        self.lbl.set_active(not paused)
        self._toggle.setText("▶ 재개" if paused else "⏸ 일시정지")
        if paused: self.engine.stop_camera()
        else: self.engine.start_camera()

    def _upd_fps(self):
        fps=self.engine.measured_fps(self.engine._cam_fps_ts)
        self._fps_lbl.setText(f"FPS: {fps:.1f}")

    def closeEvent(self,e): e.ignore(); self.hide()


# =============================================================================
#  DisplayWindow (모니터 선택)
# =============================================================================
class DisplayWindow(QDialog):
    def __init__(self, engine: CoreEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine=engine; self.signals=signals
        self.setWindowTitle("🖥  Display 선택")
        self.setWindowFlags(Qt.Window|Qt.WindowMinimizeButtonHint|Qt.WindowCloseButtonHint)
        self.resize(460,240); self.setStyleSheet("background:#0d0d1e;color:#ddd;")
        self._build(); signals.monitors_scanned.connect(self._on_scanned)

    def _build(self):
        root=QVBoxLayout(self); root.setSpacing(0); root.setContentsMargins(0,0,0,0)
        hdr=QFrame(); hdr.setStyleSheet("QFrame{background:#0a0a18;border-bottom:1px solid #1e2a3a;}"); hdr.setFixedHeight(40)
        hl=QHBoxLayout(hdr); hl.setContentsMargins(10,0,8,0)
        t=QLabel("🖥  디스플레이 선택"); t.setStyleSheet("color:#9ab;font-weight:bold;font-size:13px;"); hl.addWidget(t); hl.addStretch()
        self._sel_lbl=QLabel("선택: —"); self._sel_lbl.setStyleSheet("color:#f0c040;font-size:11px;font-weight:bold;"); hl.addWidget(self._sel_lbl)
        root.addWidget(hdr)
        body=QWidget(); bv=QVBoxLayout(body); bv.setContentsMargins(12,10,12,10); bv.setSpacing(8)
        sr=QHBoxLayout()
        self._scan_btn=QPushButton("🔍 스캔"); self._scan_btn.setFixedHeight(28)
        self._scan_btn.setStyleSheet("QPushButton{background:#1a2a4a;color:#7bc8e0;border:1px solid #2a4a7a;border-radius:4px;font-size:11px;padding:2px 12px;}")
        self._scan_btn.clicked.connect(self._on_scan)
        self._info=QLabel("스캔하여 목록 로드"); self._info.setStyleSheet("color:#556;font-size:10px;")
        sr.addWidget(self._scan_btn); sr.addStretch(); sr.addWidget(self._info); bv.addLayout(sr)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setStyleSheet("QScrollArea{border:1px solid #1a2a3a;background:#080818;}")
        self._cb_cont=QWidget(); self._cb_cont.setStyleSheet("background:#080818;")
        self._cb_lay=QVBoxLayout(self._cb_cont); self._cb_lay.setContentsMargins(4,4,4,4); self._cb_lay.setSpacing(2)
        scroll.setWidget(self._cb_cont); bv.addWidget(scroll); root.addWidget(body,1)
        self._cbs={}

    def _on_scanned(self, mons):
        while self._cb_lay.count():
            it=self._cb_lay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._cbs.clear()
        if not mons: self._cb_lay.addWidget(QLabel("  모니터 없음")); return
        for m in mons:
            cb=QCheckBox(f"  {m['name']}")
            cb.setStyleSheet("QCheckBox{color:#ccd;font-size:11px;spacing:6px;padding:5px 8px;}")
            cb.setChecked(m["idx"]==self.engine.active_monitor_idx)
            cb.toggled.connect(lambda chk,idx=m["idx"],c=cb:self._on_cb(idx,chk,c))
            self._cbs[m["idx"]]=cb; self._cb_lay.addWidget(cb)
        self._cb_lay.addStretch()
        self._info.setText(f"{len(mons)}개 감지")
        active=next((m for m in mons if m["idx"]==self.engine.active_monitor_idx),None)
        if active: self._sel_lbl.setText(f"선택: {active['name']}")

    def _on_cb(self,idx,checked,cb):
        if not checked: cb.blockSignals(True); cb.setChecked(True); cb.blockSignals(False); return
        if idx==self.engine.active_monitor_idx: return
        for oi,ocb in self._cbs.items():
            if oi!=idx: ocb.blockSignals(True); ocb.setChecked(False); ocb.blockSignals(False)
        self.engine.active_monitor_idx=idx; self.engine.restart_screen()
        m=next((m for m in self.engine.monitor_list if m["idx"]==idx),None)
        if m: self._sel_lbl.setText(f"선택: {m['name']}")

    def _on_scan(self):
        self._scan_btn.setEnabled(False); threading.Thread(target=self.engine.scan_monitors,daemon=True).start()
        QTimer.singleShot(800,lambda:self._scan_btn.setEnabled(True))

    def closeEvent(self,e): e.ignore(); self.hide()


# =============================================================================
#  MainWindow
# =============================================================================

# =============================================================================
#  _FeatureDragList — 드래그앤드랍 기능 순서 리스트
# =============================================================================
class _FeatureDragList(QWidget):
    """
    기능 섹션 순서를 드래그앤드랍으로 변경하는 위젯.
    - 각 행: ⠿ 핸들 + 기능 이름
    - 마우스 드래그로 순서 변경
    - 더블클릭: 해당 섹션으로 스크롤
    - 체크박스 없음 (섹션 ON/OFF는 CollapsibleSection 헤더로)
    """
    order_changed = pyqtSignal(list)   # 새 순서 [key, ...]
    scroll_to     = pyqtSignal(str)    # 더블클릭된 key

    _ROW_H = 30
    _BG    = "#10102a"
    _BG_HV = "#1a1a3a"
    _BG_DG = "#2a3a5a"   # 드래그 중 강조

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{self._BG};")
        self._order:  list = []   # key 순서
        self._labels: dict = {}   # key → 표시 레이블
        self._rows:   list = []   # QFrame 위젯 순서 (order 와 동기)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(4,4,4,4); self._lay.setSpacing(2)
        # 드래그 상태
        self._drag_idx:    int   = -1
        self._drag_start:  QPoint = QPoint()
        self._dragging:    bool  = False
        self._hover_idx:   int   = -1

    def populate(self, order: list, labels: dict):
        self._order  = list(order)
        self._labels = labels
        self._rebuild()

    def _rebuild(self):
        """_order 에 따라 행 위젯 전체 재생성."""
        while self._lay.count():
            it=self._lay.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._rows.clear()
        for key in self._order:
            row = self._make_row(key)
            self._rows.append(row)
            self._lay.addWidget(row)
        self._lay.addStretch()

    def _make_row(self, key: str) -> QFrame:
        row=QFrame(); row.setFixedHeight(self._ROW_H)
        row.setObjectName(key)
        row.setStyleSheet(
            f"QFrame{{background:{self._BG};border-radius:3px;}}"
            f"QFrame:hover{{background:{self._BG_HV};}}")
        row.setCursor(Qt.OpenHandCursor)
        hl=QHBoxLayout(row); hl.setContentsMargins(6,2,8,2); hl.setSpacing(6)
        grip=QLabel("⠿"); grip.setStyleSheet("color:#4a5a8a;font-size:18px;")
        grip.setFixedWidth(18)
        lbl=QLabel(self._labels.get(key,key))
        lbl.setStyleSheet("color:#ccd;font-size:11px;background:transparent;")
        hl.addWidget(grip); hl.addWidget(lbl,1)
        return row

    def _row_at_y(self, y: int) -> int:
        for i, row in enumerate(self._rows):
            ry=row.y(); rh=row.height()
            if ry <= y < ry+rh: return i
        return -1

    def mousePressEvent(self, e):
        if e.button()==Qt.LeftButton:
            idx=self._row_at_y(e.pos().y())
            if idx>=0:
                self._drag_idx   = idx
                self._drag_start = e.pos()
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button()==Qt.LeftButton:
            idx=self._row_at_y(e.pos().y())
            if 0<=idx<len(self._order):
                self.scroll_to.emit(self._order[idx])
        super().mouseDoubleClickEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_idx>=0 and not self._dragging:
            if (e.pos()-self._drag_start).manhattanLength()>6:
                self._dragging=True
                self._rows[self._drag_idx].setStyleSheet(
                    f"QFrame{{background:{self._BG_DG};border:1px solid #5a7aaa;"
                    "border-radius:3px;}")
                self._rows[self._drag_idx].setCursor(Qt.ClosedHandCursor)
        if self._dragging:
            hi=self._row_at_y(e.pos().y())
            if hi!=self._hover_idx:
                # 이전 hover 해제
                if 0<=self._hover_idx<len(self._rows) and self._hover_idx!=self._drag_idx:
                    self._rows[self._hover_idx].setStyleSheet(
                        f"QFrame{{background:{self._BG};border-radius:3px;}}"
                        f"QFrame:hover{{background:{self._BG_HV};}}")
                # 새 hover 강조
                if 0<=hi<len(self._rows) and hi!=self._drag_idx:
                    self._rows[hi].setStyleSheet(
                        f"QFrame{{background:#1a2a1a;border:1px dashed #4a8a4a;"
                        "border-radius:3px;}")
                self._hover_idx=hi
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragging and self._drag_idx>=0:
            tgt=self._row_at_y(e.pos().y())
            if 0<=tgt<len(self._order) and tgt!=self._drag_idx:
                # 순서 교환
                self._order.insert(tgt, self._order.pop(self._drag_idx))
                self._rebuild()
                self.order_changed.emit(list(self._order))
            else:
                # 취소: 원래 스타일 복원
                if 0<=self._drag_idx<len(self._rows):
                    self._rows[self._drag_idx].setStyleSheet(
                        f"QFrame{{background:{self._BG};border-radius:3px;}}"
                        f"QFrame:hover{{background:{self._BG_HV};}}")
                    self._rows[self._drag_idx].setCursor(Qt.OpenHandCursor)
        self._drag_idx=-1; self._dragging=False; self._hover_idx=-1
        super().mouseReleaseEvent(e)

    def current_order(self) -> list:
        return list(self._order)

    def set_order(self, order: list):
        """외부(DB 복원)에서 순서를 설정."""
        valid=[k for k in order if k in self._labels]
        for k in self._order:
            if k not in valid: valid.append(k)
        self._order=valid
        self._rebuild()


class MainWindow(QMainWindow):
    # 섹션 정의: (key, title, color, panel_class_or_factory)
    _SECTION_DEFS = [
        ("recording",   "⏺  Recording",          "#27ae60", None),
        ("manual",      "🎬  수동 녹화",          "#e67e22", None),
        ("memo",        "📝  메모장",             "#f39c12", None),
        ("autoclick",   "🖱  Auto-Click",         "#2980b9", None),
        ("schedule",    "⏰  Schedule",           "#8e44ad", None),
        ("macro",       "🎯  Click Macro",        "#16a085", None),
        ("blackout",    "⚡  Blackout Detection", "#e74c3c", None),
        ("path",        "📂  저장 경로",          "#00b894", None),
        ("log",         "📋  Log",                "#7f8c8d", None),
        ("reset",       "🔄  설정 초기화",        "#636e72", None),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen & Camera Recorder  v2.4")
        self.resize(1440, 920)
        self.setStyleSheet(self._dark())

        self.signals = Signals()
        self.engine  = CoreEngine(self.signals)
        self.db      = SettingsDB()

        self._sections: dict = {}
        self._panels:   dict = {}
        self._feat_order: list = []   # feature bar 순서

        self._cam_win  = CameraWindow(self.engine, self.signals, self)
        self._disp_win = DisplayWindow(self.engine, self.signals, self)

        self._build_ui()
        self._setup_hotkeys()
        self._load_settings()

        # ── 타이머 ───────────────────────────────────────────────────────────
        QTimer(self, timeout=self._refresh,       interval=500  ).start()
        QTimer(self, timeout=self._update_fps,    interval=2000 ).start()
        QTimer(self, timeout=self._check_segment, interval=5000 ).start()
        QTimer(self, timeout=self._tick_schedule, interval=1000 ).start()
        QTimer(self, timeout=self._pump_preview,  interval=33   ).start()
        QTimer(self, timeout=self._auto_save,     interval=10000).start()

        # 엔진 시작: 스크린·카메라는 10초 후 자동 ON (과부화 방지)
        # UI 버튼은 즉시 수동 시작 가능
        self.engine.start()

        # 10초 후 미리보기 자동 활성화
        self._preview_countdown_lbl.setText("미리보기: 10초 후 자동 활성화")
        self._preview_timer=QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._on_preview_ready)
        self._preview_timer.start(10_000)

    # ── UI 빌드 ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        central=QWidget(); self.setCentralWidget(central)
        root=QHBoxLayout(central); root.setSpacing(0); root.setContentsMargins(8,8,8,8)
        splitter=QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("QSplitter::handle{background:#2a2a4a;width:5px;}QSplitter::handle:hover{background:#4a6aaa;}")

        # 왼쪽 미리보기
        lw=QWidget(); lv=QVBoxLayout(lw); lv.setSpacing(6); lv.setContentsMargins(0,0,4,0)
        hdr=QHBoxLayout()
        t=QLabel("🖥  Screen Preview"); t.setStyleSheet("color:#9ab;font-weight:bold;font-size:13px;"); hdr.addWidget(t); hdr.addStretch()
        self._scr_fps_badge=QLabel("FPS: —"); self._scr_fps_badge.setStyleSheet("color:#888;font-size:11px;"); hdr.addWidget(self._scr_fps_badge)
        # 스레드 토글
        self._scr_toggle=QPushButton("⏸ 일시정지"); self._scr_toggle.setCheckable(True); self._scr_toggle.setChecked(False)
        self._scr_toggle.setStyleSheet("QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;border-radius:13px;font-size:10px;padding:2px 10px;}QPushButton:checked{background:#3a1a1a;color:#f88;}")
        self._scr_toggle.toggled.connect(self._on_scr_toggle); hdr.addWidget(self._scr_toggle)
        # Display 버튼
        self._disp_btn=QPushButton("🖥 Display"); self._disp_btn.setCheckable(True)
        self._disp_btn.setStyleSheet("QPushButton{background:#1a3a2a;color:#7be0bc;border:1px solid #2a6a4a;border-radius:13px;font-size:10px;padding:2px 10px;}QPushButton:checked{background:#1a4035;}")
        self._disp_btn.toggled.connect(self._on_disp_toggle); hdr.addWidget(self._disp_btn)
        # Camera 버튼
        self._cam_btn=QPushButton("📷 Camera"); self._cam_btn.setCheckable(True)
        self._cam_btn.setStyleSheet("QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;border-radius:13px;font-size:10px;padding:2px 10px;}QPushButton:checked{background:#1a4060;}")
        self._cam_btn.toggled.connect(self._on_cam_toggle); hdr.addWidget(self._cam_btn)

        scr_frame=QFrame(); scr_frame.setStyleSheet("QFrame{border:1px solid #334;border-radius:6px;background:#0d0d1e;}")
        sf=QVBoxLayout(scr_frame); sf.setContentsMargins(4,4,4,4); sf.setSpacing(3)
        sf.addLayout(hdr)
        self._scr_lbl=PreviewLabel("screen",self.engine); sf.addWidget(self._scr_lbl,1)
        # 미리보기 카운트다운 / 수동 시작
        preview_bar=QHBoxLayout()
        self._preview_countdown_lbl=QLabel("미리보기: 준비 중...")
        self._preview_countdown_lbl.setStyleSheet("color:#f0c040;font-size:10px;font-weight:bold;")
        self._preview_now_btn=QPushButton("▶ 지금 시작")
        self._preview_now_btn.setFixedHeight(22)
        self._preview_now_btn.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#8fa;border:1px solid #2a6a3a;"
            "border-radius:3px;font-size:10px;padding:0 8px;}"
            "QPushButton:hover{background:#225a3a;}")
        self._preview_now_btn.clicked.connect(self._on_preview_start_now)
        hint=QLabel("Left-drag: ROI 추가  |  Right-click: ROI 제거")
        hint.setStyleSheet("color:#555;font-size:10px;")
        preview_bar.addWidget(self._preview_countdown_lbl)
        preview_bar.addWidget(self._preview_now_btn)
        preview_bar.addStretch()
        preview_bar.addWidget(hint)
        sf.addLayout(preview_bar)
        lv.addWidget(scr_frame,1)
        self._status_lbl=QLabel("준비"); self._status_lbl.setStyleSheet("color:#888;font-size:11px;padding:2px 4px;border-top:1px solid #334;"); lv.addWidget(self._status_lbl)
        self.signals.status_message.connect(lambda m: self._status_lbl.setText(m[:130]))

        # 오른쪽 컨트롤 패널
        rw=QWidget(); rw.setMinimumWidth(320)
        rv=QVBoxLayout(rw); rv.setContentsMargins(4,0,0,0); rv.setSpacing(0)
        pt=QLabel("⚙  Control Panel"); pt.setStyleSheet("color:#ccc;font-size:13px;font-weight:bold;padding:8px 10px;background:#1a1a3a;border-bottom:1px solid #334;"); rv.addWidget(pt)

        # 기능 ON/OFF 체크박스
        fbar=self._build_feature_bar(); rv.addWidget(fbar)

        self._panel_scroll=QScrollArea(); self._panel_scroll.setWidgetResizable(True)
        self._panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._panel_scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}QScrollBar:vertical{background:#0d0d1e;width:8px;border-radius:4px;}QScrollBar::handle:vertical{background:#336;border-radius:4px;min-height:20px;}QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        self._panel_w=QWidget(); self._panel_w.setStyleSheet("background:#12122a;")
        self._panel_l=QVBoxLayout(self._panel_w); self._panel_l.setContentsMargins(8,6,8,14); self._panel_l.setSpacing(10)
        self._build_sections()
        self._panel_l.addStretch()
        self._panel_scroll.setWidget(self._panel_w)
        rv.addWidget(self._panel_scroll,1)

        splitter.addWidget(lw); splitter.addWidget(rw)
        splitter.setStretchFactor(0,1); splitter.setStretchFactor(1,0)
        splitter.setSizes([960,440]); root.addWidget(splitter)

    # ── 기능 레이블 (클래스 상수) ─────────────────────────────────────────────
    _FEAT_LABELS = {
        "recording":"⏺ Recording","manual":"🎬 수동녹화","memo":"📝 메모",
        "autoclick":"🖱 Auto-Click","schedule":"⏰ Schedule",
        "macro":"🎯 Macro","blackout":"⚡ Blackout",
        "path":"📂 경로","log":"📋 Log","reset":"🔄 초기화",
    }

    def _build_feature_bar(self) -> QWidget:
        """
        기능 순서 설정 패널 (드래그앤드랍 전용).
        - 체크박스 없음: 섹션 접기/펼치기는 CollapsibleSection 자체 헤더로 수행
        - 드래그로 순서 변경
        - 더블클릭: 해당 섹션으로 스크롤
        - ⠿ 핸들: 드래그 가능 표시
        """
        bar=QFrame()
        bar.setStyleSheet("QFrame{background:#08081a;border-bottom:2px solid #2a3a5a;}")
        bv=QVBoxLayout(bar); bv.setContentsMargins(8,6,8,4); bv.setSpacing(4)

        hdr_row=QHBoxLayout()
        t=QLabel("⚙  기능 순서 설정")
        t.setStyleSheet("color:#7ab4d4;font-size:12px;font-weight:bold;")
        hint=QLabel("드래그: 순서변경  |  더블클릭: 섹션 이동")
        hint.setStyleSheet("color:#446;font-size:9px;")
        hdr_row.addWidget(t); hdr_row.addStretch(); hdr_row.addWidget(hint)
        bv.addLayout(hdr_row)

        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFixedHeight(148)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border:1px solid #1a2a3a;border-radius:4px;background:#0c0c1e;}"
            "QScrollBar:vertical{background:#0d0d1e;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:3px;min-height:16px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")

        self._feat_list_w = _FeatureDragList(self)
        self._feat_list_w.order_changed.connect(self._on_feat_order_changed)
        self._feat_list_w.scroll_to.connect(self._on_scroll_to_section)

        self._feat_order = [d[0] for d in self._SECTION_DEFS]
        self._feat_list_w.populate(self._feat_order, self._FEAT_LABELS)

        scroll.setWidget(self._feat_list_w)
        bv.addWidget(scroll)
        return bar

    def _on_feat_order_changed(self, new_order: list):
        """드래그앤드랍으로 순서가 바뀌었을 때 호출."""
        self._feat_order = new_order
        self._reorder_sections()

    def _make_feat_row(self, key: str, label: str) -> QWidget:
        """하위 호환용 — _FeatureDragList 에서 직접 생성."""
        pass

    def _feat_move(self, key: str, direction: int): pass
    def _feat_up(self, key: str): pass
    def _feat_dn(self, key: str): pass

    def _reorder_sections(self):
        """_feat_order 에 맞게 panel_l 내 섹션 순서 재배치."""
        while self._panel_l.count():
            it=self._panel_l.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        for k in self._feat_order:
            sec=self._sections.get(k)
            if sec:
                sec.setParent(self._panel_w)
                self._panel_l.addWidget(sec)
        self._panel_l.addStretch()

    def _on_scroll_to_section(self, key: str):
        sec=self._sections.get(key)
        if not sec: return
        if not sec.isVisible(): sec.setVisible(True)
        if sec.is_collapsed(): sec.set_collapsed(False)
        QTimer.singleShot(50, lambda: self._scroll_to_widget(sec))

    def _scroll_to_widget(self, w: QWidget):
        pos=w.mapTo(self._panel_w, QPoint(0,0))
        vsb=self._panel_scroll.verticalScrollBar()
        vsb.setValue(max(0, pos.y()-10))

    def _build_sections(self):
        # 패널 인스턴스 생성
        self._panels["recording"] = RecordingPanel(self.engine, self.signals)
        self._panels["manual"]    = ManualClipPanel(self.engine, self.signals)
        self._panels["memo"]      = MemoPanel(self.engine, self.signals)
        self._panels["autoclick"] = AutoClickPanel(self.engine, self.signals)
        self._panels["schedule"]  = SchedulePanel(self.engine, self.signals)
        self._panels["macro"]     = MacroPanel(self.engine, self.signals)
        self._panels["blackout"]  = BlackoutPanel(self.engine, self.signals)
        self._panels["path"]      = PathSettingsPanel(self.engine, self.db)
        self._panels["log"]       = LogPanel(self.signals)
        self._panels["reset"]     = ResetPanel(self.engine, self.db, self._on_reset)

        for key,title,color,_ in self._SECTION_DEFS:
            sec=CollapsibleSection(title,color)
            sec.add_widget(self._panels[key])
            self._sections[key]=sec; self._panel_l.addWidget(sec)

    # ── 타이머 콜백 ───────────────────────────────────────────────────────────
    def _refresh(self):
        self._panels["blackout"].refresh()
        self._panels["schedule"].refresh_tbl()

    def _update_fps(self):
        sf=self.engine.measured_fps(self.engine._scr_fps_ts)
        cf=self.engine.measured_fps(self.engine._cam_fps_ts)
        self._scr_fps_badge.setText(f"FPS: {sf:.1f}")
        self._panels["recording"].update_fps(sf,cf)

    def _check_segment(self):
        if (self.engine.recording and self.engine._seg_start_time and
                time.time()-self.engine._seg_start_time >= self.engine.segment_duration):
            threading.Thread(target=self.engine._create_segment,daemon=True).start()

    def _tick_schedule(self):
        for action, entry in self.engine.schedule_tick():
            if action=='start':
                self.signals.status_message.emit(f"[Schedule] #{entry.id} 녹화 시작")
                self.engine.start_recording()
            elif action=='stop':
                self.signals.status_message.emit(f"[Schedule] #{entry.id} 녹화 종료")
                self.engine.stop_recording()
            elif action=='macro_run':
                if self.engine.macro_steps:
                    self.engine.macro_start_run(entry.macro_repeat, entry.macro_gap)
                else:
                    self.signals.status_message.emit(f"[Schedule] #{entry.id} ⚠ 매크로 스텝 없음")

    def _on_preview_ready(self):
        """10초 경과 후 미리보기 자동 활성화."""
        self._preview_countdown_lbl.setText("미리보기: 활성화됨")
        self._preview_countdown_lbl.setStyleSheet("color:#2ecc71;font-size:10px;font-weight:bold;")
        self._preview_now_btn.setVisible(False)
        self._scr_lbl.set_active(True)

    def _on_preview_start_now(self):
        """수동 즉시 미리보기 시작."""
        if self._preview_timer.isActive():
            self._preview_timer.stop()
        # 스크린/카메라 스레드 즉시 시작
        self.engine.start_screen()
        self.engine.start_camera()
        self._on_preview_ready()

    def _pump_preview(self):
        """
        미리보기 프레임 펌프 — 33ms 마다 호출.
        스크린·카메라 스레드는 독립적으로 동작.
        ⏸ 일시정지: 스레드 중단 없이 화면 렌더만 중단 (버퍼는 계속 유지).
        """
        try:
            sf = self.engine.screen_queue.get_nowait()
            # ⏸ 상태가 아닐 때만 스크린 렌더 (스레드는 계속 돌아 버퍼 유지)
            if not self._scr_toggle.isChecked():
                self._scr_lbl.update_frame(
                    CoreEngine.stamp_preview(sf, self.engine, "screen"))
        except queue.Empty:
            pass

        try:
            cf = self.engine.camera_queue.get_nowait()
            # 카메라 창이 보일 때만 렌더 — 스크린 상태와 완전 독립
            if self._cam_win.isVisible():
                self._cam_win.lbl.update_frame(
                    CoreEngine.stamp_preview(cf, self.engine, "camera"))
        except queue.Empty:
            pass

    def _on_feat_toggle(self, key, visible):
        sec=self._sections.get(key)
        if sec: sec.setVisible(visible)

    def _auto_save(self):
        threading.Thread(target=self._save_settings, daemon=True).start()

    # ── 버튼 콜백 ─────────────────────────────────────────────────────────────
    def _on_scr_toggle(self, paused: bool):
        """
        스크린 미리보기 일시정지/재개.
        ⚠ 스레드는 중단하지 않음 — 버퍼(블랙아웃·수동클립용)를 유지하기 위해
          스레드는 계속 실행하고, 렌더링만 _pump_preview 에서 조건부 처리.
        """
        self._scr_lbl.set_active(not paused)
        self._scr_toggle.setText("▶ 재개" if paused else "⏸ 일시정지")
        # 스레드는 중단하지 않음 — 화면에만 표시하지 않을 뿐

    def _on_cam_toggle(self, checked: bool):
        """카메라 미리보기 창 표시/숨기기 — 카메라 스레드와 독립."""
        if checked: self._cam_win.show(); self._cam_win.raise_()
        else: self._cam_win.hide()

    def _on_disp_toggle(self, checked: bool):
        """디스플레이 선택 창 표시/숨기기 — 스크린 스레드와 독립."""
        if checked: self._disp_win.show(); self._disp_win.raise_()
        else: self._disp_win.hide()

    # ── 설정 저장 / 복원 ─────────────────────────────────────────────────────
    def _save_settings(self):
        db=self.db; p=self._panels
        rp=p["recording"]
        db.set("screen_fps",      rp.fps_spin.value())
        db.set("playback_speed",  rp.speed_spin.value())
        db.set("screen_rec",      rp.scr_chk.isChecked())
        db.set("camera_rec",      rp.cam_chk.isChecked())   # ★ 카메라 녹화 독립
        db.set("codec_idx",       rp.codec_cb.currentIndex())
        db.set("scale_idx",       rp.scale_cb.currentIndex())
        db.set("tc_verify",       rp.tc_chk.isChecked())    # ★ T/C 검증
        mp2=p["manual"]
        db.set("manual_pre",      mp2.pre_spin.value())
        db.set("manual_post",     mp2.post_spin.value())
        db.set("manual_src_scr",  mp2.scr_chk.isChecked())
        db.set("manual_src_cam",  mp2.cam_chk.isChecked())
        bp=p["blackout"]
        db.set("bo_threshold",    bp.thr_spin.value())
        db.set("bo_cooldown",     bp.cd_spin.value())
        db.set("bo_rec",          bp.rec_chk.isChecked())
        ac=p["autoclick"]
        db.set("ac_interval",     ac.interval_spin.value())
        mp=p["macro"]
        db.set("mac_repeat",      int(mp.rep_spin.value()))
        db.set("mac_gap",         mp.gap_spin.value())
        # overlay_font 는 각 MemoOverlayCfg 에서 탭별 관리 (별도 DB 저장 불필요)
        # 기능 순서
        import json as _j
        db.set("feat_order", _j.dumps(self._feat_list_w.current_order()))
        # 섹션 접힘 상태
        for key,sec in self._sections.items():
            db.set(f"sec_col_{key}", sec.is_collapsed())
        # 메모 탭
        db.save_memo_tabs(p["memo"].get_tab_data())
        # 매크로 슬롯
        db.save_macro_slots(p["macro"].get_slots_data())

    def _load_settings(self):
        import json as _j
        db=self.db; p=self._panels
        rp=p["recording"]
        rp.fps_spin.setValue(db.get_float("screen_fps",30.0))
        rp.speed_spin.setValue(db.get_float("playback_speed",1.0))
        rp.scr_chk.setChecked(db.get_bool("screen_rec",True))
        rp.cam_chk.setChecked(db.get_bool("camera_rec",True))   # ★
        rp.codec_cb.setCurrentIndex(db.get_int("codec_idx",0))
        rp.scale_cb.setCurrentIndex(db.get_int("scale_idx",0))
        rp.tc_chk.setChecked(db.get_bool("tc_verify",False))    # ★
        mp2=p["manual"]
        pre=db.get_float("manual_pre",10.0); post=db.get_float("manual_post",10.0)
        mp2.pre_spin.setValue(pre); mp2.post_spin.setValue(post)
        mp2.scr_chk.setChecked(db.get_bool("manual_src_scr",True))
        mp2.cam_chk.setChecked(db.get_bool("manual_src_cam",True))
        bp=p["blackout"]
        bp.thr_spin.setValue(db.get_float("bo_threshold",30.0))
        bp.cd_spin.setValue(db.get_float("bo_cooldown",5.0))
        bp.rec_chk.setChecked(db.get_bool("bo_rec",True))
        ac=p["autoclick"]
        ac.interval_spin.setValue(db.get_float("ac_interval",1.0))
        mp=p["macro"]
        mp.rep_spin.setValue(db.get_int("mac_repeat",1))
        mp.gap_spin.setValue(db.get_float("mac_gap",1.0))
        memo=p["memo"]
        # feat_order 복원 → 섹션 재배치
        order_raw=db.get("feat_order","")
        if order_raw:
            try:
                saved_order=[k for k in _j.loads(order_raw)
                             if k in self._sections]
                # DB에 없는 새 key 보완
                for d in self._SECTION_DEFS:
                    if d[0] not in saved_order: saved_order.append(d[0])
                self._feat_order = saved_order
                self._feat_list_w.set_order(saved_order)
                self._reorder_sections()
            except Exception: pass
        # 섹션 접힘
        for key,sec in self._sections.items():
            sec.set_collapsed(db.get_bool(f"sec_col_{key}",False))
        # 메모 탭
        tabs=db.load_memo_tabs()
        if tabs: memo.set_tab_data(tabs)
        # 매크로 슬롯
        slots=db.load_macro_slots()
        if slots: mp.set_slots_data(slots)
        # 경로
        p["path"].load_from_db()

    def _on_reset(self, do_settings, do_memo, do_sections, do_db):
        if self.engine.recording:
            self.signals.status_message.emit("⚠ 녹화 중에는 초기화할 수 없습니다."); return
        if do_db: self.db.wipe()
        if do_settings:
            rp=self._panels["recording"]
            rp.fps_spin.setValue(30.0); rp.speed_spin.setValue(1.0)
            rp.scr_chk.setChecked(True); rp.cam_chk.setChecked(True)
            rp.codec_cb.setCurrentIndex(0); rp.scale_cb.setCurrentIndex(0)
            rp.tc_chk.setChecked(False)
            bp=self._panels["blackout"]
            bp.thr_spin.setValue(30.0); bp.cd_spin.setValue(5.0); bp.rec_chk.setChecked(True)
            ac=self._panels["autoclick"]; ac.interval_spin.setValue(1.0)
            mp=self._panels["macro"]; mp.rep_spin.setValue(1); mp.gap_spin.setValue(1.0)
        if do_memo:
            m=self._panels["memo"]
            while m.tabs.count()>0: m.tabs.removeTab(0)
            m._editors.clear(); self.engine.memo_texts.clear()
            m._add_tab("메모 1")
        if do_sections:
            for sec in self._sections.values(): sec.set_collapsed(False)
        self._save_settings()
        self.signals.status_message.emit("설정 초기화 완료")

    # ── 단축키 ───────────────────────────────────────────────────────────────
    def _setup_hotkeys(self):
        if not PYNPUT_AVAILABLE: return
        # ★ Ctrl+Alt+E 는 engine.stop_recording 직접 호출이 아닌
        #   RecordingPanel._on_stop 을 통해야 T/C 검증 다이얼로그가 표시됨.
        #   단, 단축키는 백그라운드 스레드에서 호출되므로
        #   QTimer.singleShot(0, ...) 으로 UI 메인스레드에 위임.
        def _stop_via_panel():
            QTimer.singleShot(0, self._panels["recording"]._on_stop)

        hk={
            '<ctrl>+<alt>+w': self.engine.start_recording,
            '<ctrl>+<alt>+e': _stop_via_panel,
            '<ctrl>+<alt>+d': lambda: QTimer.singleShot(
                0, lambda: self._panels["recording"].scr_chk.setChecked(
                    not self._panels["recording"].scr_chk.isChecked())),
            '<ctrl>+<alt>+a': lambda: QTimer.singleShot(
                0, self._panels["autoclick"]._on_start),
            '<ctrl>+<alt>+s': lambda: QTimer.singleShot(
                0, self._panels["autoclick"]._on_stop),
            '<ctrl>+<alt>+m': self.engine.save_manual_clip,
            '<ctrl>+<alt>+q': lambda: QTimer.singleShot(0, self.close),
        }
        self._hkl=pynput_keyboard.GlobalHotKeys(hk); self._hkl.start()

    # ── 종료 이벤트 ───────────────────────────────────────────────────────────
    def closeEvent(self, e):
        # T/C 검증이 활성화된 상태로 녹화 중이면 종료 전 PASS/FAIL 확인
        if self.engine.recording and self.engine.tc_verify_enabled:
            result = self._panels["recording"]._show_tc_dialog()
            if result is None:
                e.ignore(); return   # 취소 → 창 닫기 취소
            self.engine.tc_verify_result = result
        # 미리보기 타이머 정리
        if hasattr(self, '_preview_timer') and self._preview_timer.isActive():
            self._preview_timer.stop()
        self._save_settings()
        if self.engine.recording: self.engine.stop_recording()
        self.engine.stop()
        self._cam_win.hide(); self._disp_win.hide()
        if PYNPUT_AVAILABLE and hasattr(self,'_hkl'): self._hkl.stop()
        e.accept()

    # ── 다크 스타일 ──────────────────────────────────────────────────────────
    @staticmethod
    def _dark() -> str:
        return """
        QMainWindow,QWidget{background:#12122a;color:#ddd;}
        QDialog{background:#0d0d1e;color:#ddd;}
        QGroupBox{border:1px solid #2a2a4a;border-radius:6px;margin-top:18px;
            font-weight:bold;color:#9bc;font-size:12px;padding-top:10px;}
        QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;
            left:10px;top:-2px;padding:2px 6px;background:#12122a;border-radius:3px;}
        QPushButton{background:#1e2a3a;border:1px solid #4a5a7a;border-radius:4px;
            padding:4px 10px;color:#ccd;font-size:11px;}
        QPushButton:hover{background:#2a3e56;border-color:#6a8aaa;color:#eef;}
        QPushButton:pressed{background:#0d1a2a;color:#7bc8ff;}
        QPushButton:disabled{background:#141424;color:#3a3a5a;border-color:#2a2a3a;}
        QTextEdit,QPlainTextEdit{background:#0d0d1e;border:1px solid #2a2a4a;color:#ccc;}
        QTextEdit:focus,QPlainTextEdit:focus{border-color:#4a6a9a;}
        QDoubleSpinBox,QSpinBox{background:#1a1a3a;border:1px solid #3a4a6a;
            color:#ddd;padding:2px 4px;border-radius:3px;}
        QDoubleSpinBox:focus,QSpinBox:focus{border-color:#5a8aca;}
        QDoubleSpinBox::up-button,QSpinBox::up-button,
        QDoubleSpinBox::down-button,QSpinBox::down-button{background:#1e2a3a;border:none;width:14px;}
        QComboBox{background:#1a1a3a;border:1px solid #3a4a6a;color:#ddd;padding:2px 4px;border-radius:3px;}
        QComboBox:hover{border-color:#5a8aca;}
        QComboBox::drop-down{border:none;}
        QComboBox QAbstractItemView{background:#1a1a3a;color:#ddd;selection-background-color:#2a4a7a;}
        QCheckBox{color:#ccd;spacing:6px;}
        QCheckBox::indicator{width:16px;height:16px;border:1px solid #4a5a7a;
            border-radius:3px;background:#0d0d1e;}
        QCheckBox::indicator:checked{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 #2a7aca,stop:1 #1a5a9a);border:2px solid #5aaae0;}
        QLabel{color:#ccd;}
        QLineEdit{background:#1a1a3a;border:1px solid #3a4a6a;color:#ddd;
            padding:2px 6px;border-radius:3px;}
        QLineEdit:focus{border-color:#5a8aca;}
        QTableWidget{selection-background-color:#1a3a5a;background:#0d0d1e;color:#ccc;}
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
        QDateTimeEdit{background:#1a1a3a;border:1px solid #336;color:#ddd;
            padding:2px 4px;border-radius:3px;}
        QSplitter::handle{background:#2a2a4a;width:5px;}
        QSplitter::handle:hover{background:#4a6aaa;}
        QHeaderView::section{background:#1a1a3a;color:#9ab;font-size:11px;
            border:none;padding:4px;}
        """


# =============================================================================
#  엔트리포인트
# =============================================================================
def main():
    if hasattr(Qt,'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt,'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app=QApplication(sys.argv)
    app.setApplicationName("ScreenCameraRecorder")
    win=MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__=="__main__":
    main()