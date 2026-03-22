# -*- coding: utf-8 -*-
"""
Screen & Camera Recorder  v4.0
─────────────────────────────────────────────────────────────────────────────
변경사항:
  1. 녹화 예약 여러 개 (스케줄 목록 테이블)
  2. 컨트롤러 맨 상단 기능 체크박스 → 섹션 ON/OFF
  3. UI 섹션 간 여백 개선
  4. 메모장 기능 → 녹화 시 우측 하단 오버레이 + 파일 저장
  5. Blackout·녹화 폴더를 바로 여는 버튼  /  저장 경로: ~/Desktop/bltn_rec/
  6. 영상 길이 짧아지는 버그 수정 (PTS 기반 프레임 드롭 보상)
─────────────────────────────────────────────────────────────────────────────
"""

import sys, os, cv2, numpy as np, threading, time, queue, mss, subprocess, platform
from datetime import datetime
from collections import deque

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGroupBox, QCheckBox, QDoubleSpinBox,
    QScrollArea, QFrame, QGridLayout, QTextEdit, QSizePolicy,
    QDialog, QLCDNumber, QDateTimeEdit, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QPlainTextEdit, QSplitter,
    QAbstractItemView, QComboBox, QListWidget, QListWidgetItem
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QPoint, QRect, QDateTime
from PyQt5.QtGui  import QImage, QPixmap, QColor, QPainter, QPen, QFont

try:
    from pynput import keyboard as pynput_keyboard, mouse as pynput_mouse
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

# 저장 루트 경로
BASE_DIR = os.path.join(os.path.expanduser("~/Desktop"), "bltn_rec")


def open_folder(path: str):
    """OS별 파일 탐색기로 폴더 열기."""
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    if platform.system() == "Windows":
        os.startfile(path)
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ─────────────────────────────────────────────
#  Signals
# ─────────────────────────────────────────────
class Signals(QObject):
    screen_frame_ready = pyqtSignal(np.ndarray)
    camera_frame_ready = pyqtSignal(np.ndarray)
    blackout_detected  = pyqtSignal(str, dict)
    status_message     = pyqtSignal(str)
    auto_click_count   = pyqtSignal(int)
    rec_started        = pyqtSignal(str)   # output_dir
    rec_stopped        = pyqtSignal()


# ─────────────────────────────────────────────
#  Schedule Entry
# ─────────────────────────────────────────────
class ScheduleEntry:
    _id_counter = 0

    def __init__(self, start_dt: datetime | None, stop_dt: datetime | None):
        ScheduleEntry._id_counter += 1
        self.id         = ScheduleEntry._id_counter
        self.start_dt   = start_dt
        self.stop_dt    = stop_dt
        self.started    = False
        self.stopped    = False
        self.done       = False   # completed

    def label(self) -> str:
        s = self.start_dt.strftime("%m/%d %H:%M:%S") if self.start_dt else "—"
        e = self.stop_dt.strftime("%m/%d %H:%M:%S")  if self.stop_dt  else "—"
        return f"#{self.id}  {s} → {e}"


# ─────────────────────────────────────────────
#  Core Engine
# ─────────────────────────────────────────────
class RecorderEngine:
    def __init__(self, signals: Signals):
        self.signals = signals

        # State
        self.running   = False
        self.recording = False
        self.start_time: float | None = None
        self.output_dir = ""

        # Threads
        self._screen_thread: threading.Thread | None = None
        self._camera_thread: threading.Thread | None = None
        self._screen_stop = threading.Event()
        self._camera_stop = threading.Event()

        # FPS
        self.target_screen_fps = 30.0
        self.actual_screen_fps = 30.0
        self.actual_camera_fps = 30.0

        # Feature flags
        self.screen_recording_enabled   = True
        self.blackout_recording_enabled = True
        self.memo_overlay_enabled       = True

        # Queues (preview)
        self.screen_queue = queue.Queue(maxsize=5)
        self.camera_queue = queue.Queue(maxsize=5)

        # Writers
        self.screen_writer: cv2.VideoWriter | None = None
        self.camera_writer: cv2.VideoWriter | None = None
        self._writer_lock = threading.Lock()

        # 30-min segment
        self.segment_duration      = 30 * 60
        self.current_segment_start: float | None = None

        # ROIs
        self.screen_rois: list = []
        self.camera_rois: list = []

        # ROI state
        self.screen_roi_avg   = []
        self.camera_roi_avg   = []
        self.screen_roi_prev  = []
        self.camera_roi_prev  = []
        self.screen_overall_avg = np.zeros(3)
        self.camera_overall_avg = np.zeros(3)

        # Blackout
        self.brightness_threshold      = 30.0
        self.blackout_cooldown         = 5.0
        self.screen_last_blackout_time = 0.0
        self.camera_last_blackout_time = 0.0
        self.screen_blackout_count = 0
        self.camera_blackout_count = 0
        self.screen_blackout_events: list = []
        self.camera_blackout_events: list = []
        self.blackout_dir = os.path.join(BASE_DIR, "blackout")

        # Rolling buffer (30 s)
        self.buffer_seconds = 30
        self._screen_buffer: deque = deque()
        self._camera_buffer: deque = deque()
        self._buf_lock = threading.Lock()

        # Memo text (set from UI)
        self.memo_text: str = ""

        # Auto-click
        self.auto_click_enabled  = False
        self.auto_click_interval = 1.0
        self.auto_click_count    = 0
        self._ac_thread: threading.Thread | None = None
        self._ac_stop   = threading.Event()

        # FPS measurement
        self._screen_fps_ts: deque = deque(maxlen=60)
        self._camera_fps_ts: deque = deque(maxlen=60)

        # Schedule list
        self.schedules: list[ScheduleEntry] = []

        # Playback speed
        self.playback_speed: float = 1.0

        # ── Multi-camera support ──────────────────
        # 스캔된 카메라 목록: [{idx, name, fps}]
        self.camera_list: list[dict] = []
        # 현재 활성 카메라 인덱스 (UI에서 선택)
        self.active_camera_idx: int = 0

    # ── helpers ──────────────────────────────
    def measured_fps(self, ts_dq: deque) -> float:
        if len(ts_dq) < 2:
            return 0.0
        span = ts_dq[-1] - ts_dq[0]
        return (len(ts_dq) - 1) / span if span > 0 else 0.0

    @property
    def screen_buf_max(self):
        return max(1, int(self.actual_screen_fps * self.buffer_seconds))

    @property
    def camera_buf_max(self):
        return max(1, int(self.actual_camera_fps * self.buffer_seconds))

    # ── Camera scan & FPS detection ──────────
    def scan_cameras(self) -> list[dict]:
        """
        연결된 카메라를 인덱스 0~9 범위에서 스캔하고
        각 카메라의 FPS를 자동 감지합니다.
        반환: [{idx, name, fps}, ...]
        """
        found = []
        for idx in range(10):
            cap = cv2.VideoCapture(idx)
            if not cap.isOpened():
                cap.release()
                continue

            # FPS 자동 감지
            reported_fps = cap.get(cv2.CAP_PROP_FPS)
            if reported_fps and reported_fps > 0 and reported_fps < 300:
                fps = float(reported_fps)
            else:
                # 실측: 20프레임 캡처해 평균 FPS 계산
                frames, t0 = 0, time.time()
                while frames < 20:
                    ret, _ = cap.read()
                    if ret:
                        frames += 1
                elapsed = time.time() - t0
                fps = frames / elapsed if elapsed > 0 else 30.0

            # 카메라 이름 (지원 안 되는 경우 fallback)
            try:
                name = cap.getBackendName()
            except Exception:
                name = "Camera"
            label = f"Camera {idx}  [{name}]  {fps:.1f} fps"

            cap.release()
            found.append({"idx": idx, "name": label, "fps": fps})

        self.camera_list = found
        if found:
            # 활성 카메라 FPS를 첫 번째 카메라로 초기화
            self.active_camera_idx = found[0]["idx"]
            self.actual_camera_fps = found[0]["fps"]
            self.signals.status_message.emit(
                f"카메라 {len(found)}개 감지됨. 선택: {found[0]['name']}")
        else:
            self.signals.status_message.emit("카메라를 찾을 수 없습니다.")
        return found

    def select_camera(self, idx: int):
        """카메라 인덱스를 선택하고 해당 FPS로 업데이트."""
        cam = next((c for c in self.camera_list if c["idx"] == idx), None)
        if cam:
            self.active_camera_idx = idx
            self.actual_camera_fps = cam["fps"]
            self.signals.status_message.emit(
                f"카메라 변경: {cam['name']}")
            # 카메라 스레드 재시작
            self.stop_camera_thread()
            time.sleep(0.1)
            self.start_camera_thread()

    def detect_camera_fps(self):
        """하위호환용 래퍼 — scan_cameras를 호출합니다."""
        self.scan_cameras()

    # ── ROI ──────────────────────────────────
    def calc_roi_avg(self, frame, rois):
        avgs = []
        for rx, ry, rw, rh in rois:
            r = frame[ry:ry+rh, rx:rx+rw]
            avgs.append(r.mean(axis=0).mean(axis=0) if r.size > 0 else np.zeros(3))
        return avgs

    # ── Blackout detection ───────────────────
    def detect_blackout(self, curr, prev, source: str) -> bool:
        if not curr or not prev or len(curr) != len(prev):
            return False
        changes = []
        for c, p in zip(curr, prev):
            if np.all(p == 0): continue
            cb = 0.114*c[0] + 0.587*c[1] + 0.299*c[2]
            pb = 0.114*p[0] + 0.587*p[1] + 0.299*p[2]
            changes.append(pb - cb)
        if not changes: return False
        mc = float(np.mean(changes))
        if mc < self.brightness_threshold: return False
        now  = time.time()
        last = self.screen_last_blackout_time if source == "screen" else self.camera_last_blackout_time
        if now - last < self.blackout_cooldown: return False
        if source == "screen":
            self.screen_last_blackout_time = now
            self.screen_blackout_count += 1
        else:
            self.camera_last_blackout_time = now
            self.camera_blackout_count += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        ev = {'time': datetime.now().strftime("%H:%M:%S.%f")[:-3],
              'brightness_change': mc, 'timestamp': ts}
        lst = self.screen_blackout_events if source == "screen" else self.camera_blackout_events
        lst.append(ev)
        if len(lst) > 50: lst.pop(0)
        self.signals.blackout_detected.emit(source, ev)
        if self.blackout_recording_enabled:
            threading.Thread(target=self.save_blackout_clip,
                             args=(source, ts), daemon=True).start()
        return True

    # ── Blackout clip ─────────────────────────
    def save_blackout_clip(self, source: str, timestamp: str):
        src_dir = os.path.join(self.blackout_dir, source.upper())
        os.makedirs(src_dir, exist_ok=True)
        fps = self.actual_screen_fps if source == "screen" else self.actual_camera_fps
        n_pre = int(fps * 10); n_post = int(fps * 10)
        with self._buf_lock:
            buf = self._screen_buffer if source == "screen" else self._camera_buffer
            pre = list(buf)
        post: list = []
        deadline = time.time() + 11.0
        while len(post) < n_post and time.time() < deadline:
            time.sleep(0.04)
            with self._buf_lock:
                buf = self._screen_buffer if source == "screen" else self._camera_buffer
                if len(buf) > len(pre): post = list(buf)[len(pre):]
        pre_clip  = pre[-n_pre:] if len(pre) >= n_pre else pre
        all_frames = pre_clip + post[:n_post]
        if not all_frames:
            self.signals.status_message.emit(f"[Blackout] {source} – no frames"); return
        bi   = len(pre_clip)
        bt   = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        h, w = all_frames[0].shape[:2]
        vpath = os.path.join(src_dir, f"blackout_{timestamp}.mp4")
        cpath = os.path.join(src_dir, f"capture_{timestamp}.jpg")
        wr    = cv2.VideoWriter(vpath, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
        for i, f in enumerate(all_frames):
            f = f.copy()
            if i == bi:
                cv2.rectangle(f, (4,4), (w-4,h-4), (0,0,255), 6)
                cv2.putText(f, f"▼ BLACKOUT  {bt}", (10,100),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0,0,255), 2, cv2.LINE_AA)
            elif i > bi:
                cv2.putText(f, f"+{(i-bi)/fps:.1f}s", (10,100),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200,200,0), 2, cv2.LINE_AA)
            else:
                cv2.putText(f, f"{(i-bi)/fps:.1f}s", (10,100),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180,180,180), 1, cv2.LINE_AA)
            wr.write(f)
        wr.release()
        cv2.imwrite(cpath, all_frames[min(bi, len(all_frames)-1)].copy())
        self.signals.status_message.emit(
            f"[Blackout/{source}] {len(all_frames)/fps:.1f}s → {vpath}")

    # ── Overlay helpers ───────────────────────
    def _add_overlay(self, frame, rois):
        """녹화 파일 저장용 오버레이 (좌상단 시각 + 우하단 메모)."""
        if self.recording and self.start_time:
            now = datetime.now()
            now_str = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond//1000:03d}"
            e   = time.time() - self.start_time
            hh  = int(e//3600); mm = int((e%3600)//60); ss = int(e%60); ms = int((e%1)*1000)
            elapsed_str = f"REC  {hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"

            # 좌상단 반투명 박스
            ov = frame.copy()
            cv2.rectangle(ov, (4,4), (430,78), (0,0,0), -1)
            cv2.addWeighted(ov, 0.45, frame, 0.55, 0, frame)
            cv2.putText(frame, now_str,     (10,32), cv2.FONT_HERSHEY_SIMPLEX,
                        0.72, (0,255,80), 2, cv2.LINE_AA)
            cv2.putText(frame, elapsed_str, (10,68), cv2.FONT_HERSHEY_SIMPLEX,
                        0.65, (80,220,255), 2, cv2.LINE_AA)

            # 우하단 메모 — 줄 수 제한 없이 전체 표시
            if self.memo_overlay_enabled and self.memo_text.strip():
                h, w = frame.shape[:2]
                lines = self.memo_text.strip().splitlines()
                if lines:
                    line_h  = 22
                    font_sc = 0.52
                    box_h   = len(lines) * line_h + 14
                    max_len = max(len(l) for l in lines)
                    box_w   = min(max_len * 11 + 24, w - 20)  # 화면폭 초과 방지
                    x0      = max(4, w - box_w - 8)
                    y0      = max(4, h - box_h - 8)
                    ov2 = frame.copy()
                    cv2.rectangle(ov2, (x0-4, y0-4), (w-4, h-4), (0,0,0), -1)
                    cv2.addWeighted(ov2, 0.55, frame, 0.45, 0, frame)
                    for j, line in enumerate(lines):
                        cy = y0 + j * line_h + line_h
                        if cy > h - 6: break   # 화면 아래 벗어나면 중단
                        cv2.putText(frame, line, (x0, cy),
                                    cv2.FONT_HERSHEY_SIMPLEX, font_sc,
                                    (255, 240, 100), 1, cv2.LINE_AA)

        for i, (rx,ry,rw,rh) in enumerate(rois):
            cv2.rectangle(frame, (rx,ry), (rx+rw,ry+rh), (0,0,255), 2)
            cv2.putText(frame, f"ROI{i+1}", (rx, max(ry-5,15)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0,0,255), 1, cv2.LINE_AA)
        return frame

    @staticmethod
    def _stamp_preview(frame: np.ndarray, recording: bool,
                       start_time: float | None,
                       memo: str, memo_enabled: bool) -> np.ndarray:
        """UI 미리보기 전용 타임스탬프 합성."""
        if not recording or start_time is None:
            return frame
        out = frame.copy()
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond//1000:03d}"
        e  = time.time() - start_time
        hh = int(e//3600); mm = int((e%3600)//60); ss = int(e%60); ms = int((e%1)*1000)
        elapsed_str = f"REC  {hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"
        ov = out.copy()
        cv2.rectangle(ov, (4,4), (430,78), (0,0,0), -1)
        cv2.addWeighted(ov, 0.45, out, 0.55, 0, out)
        cv2.putText(out, now_str,     (10,32), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0,255,80),  2, cv2.LINE_AA)
        cv2.putText(out, elapsed_str, (10,68), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80,220,255), 2, cv2.LINE_AA)
        if memo_enabled and memo.strip():
            h, w = out.shape[:2]
            lines = memo.strip().splitlines()
            if lines:
                line_h  = 22
                font_sc = 0.52
                box_h   = len(lines) * line_h + 14
                max_len = max(len(l) for l in lines)
                box_w   = min(max_len * 11 + 24, w - 20)
                x0      = max(4, w - box_w - 8)
                y0      = max(4, h - box_h - 8)
                ov2 = out.copy()
                cv2.rectangle(ov2, (x0-4, y0-4), (w-4, h-4), (0,0,0), -1)
                cv2.addWeighted(ov2, 0.55, out, 0.45, 0, out)
                for j, line in enumerate(lines):
                    cy = y0 + j * line_h + line_h
                    if cy > h - 6: break
                    cv2.putText(out, line, (x0, cy),
                                cv2.FONT_HERSHEY_SIMPLEX, font_sc,
                                (255, 240, 100), 1, cv2.LINE_AA)
        return out

    # ─────────────────────────────────────────
    # ★ FIX: 영상 길이 단축 방지 — PTS 기반 프레임 드롭/중복 보상
    # ─────────────────────────────────────────
    def _write_frame_sync(self, writer: cv2.VideoWriter, frame: np.ndarray,
                          fps: float, frame_idx: int, elapsed: float):
        """
        elapsed 기준으로 이 순간에 있어야 할 프레임 인덱스를 계산하여
        실제 기록된 frame_idx와 비교.
        - 뒤처진 경우(드롭이 있었음) → 같은 프레임을 여러 번 기록해 채움
        - 앞서는 경우 → 기록 건너뜀(다음 호출까지 대기)
        """
        expected = int(elapsed * fps)
        diff = expected - frame_idx
        if diff <= 0:
            # 이미 충분히 기록됐거나 앞선 경우: 1프레임만 정상 기록
            writer.write(frame)
            return frame_idx + 1
        else:
            # 뒤처진 경우: diff 만큼 복제 기록
            for _ in range(max(1, diff)):
                writer.write(frame)
            return frame_idx + max(1, diff)

    # ── Segment ───────────────────────────────
    def _create_segment(self):
        seg_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._writer_lock:
            if self.screen_writer: self.screen_writer.release()
            if self.camera_writer: self.camera_writer.release()

        # ★ 배속: 저장 FPS = 실제캡처FPS * playback_speed
        #   2배속 → FPS 2배 → 플레이어가 같은 시간에 2배 많은 프레임을 재생 → 빠르게 보임
        scr_write_fps = max(1.0, self.actual_screen_fps * self.playback_speed)
        cam_write_fps = max(1.0, self.actual_camera_fps * self.playback_speed)

        if self.screen_recording_enabled:
            with mss.mss() as sct:
                mon_idx = 2 if len(sct.monitors) > 2 else 1
                mon     = sct.monitors[mon_idx]
                spath   = os.path.join(self.output_dir, f"screen_{seg_ts}.mp4")
                with self._writer_lock:
                    self.screen_writer = cv2.VideoWriter(
                        spath, cv2.VideoWriter_fourcc(*'mp4v'),
                        scr_write_fps, (mon['width'], mon['height']))
            self.signals.status_message.emit(f"Screen segment: {spath}")

        with self._buf_lock:
            cframe = self._camera_buffer[-1] if self._camera_buffer else None
        if cframe is not None:
            h, w = cframe.shape[:2]
            cpath = os.path.join(self.output_dir, f"camera_{seg_ts}.mp4")
            with self._writer_lock:
                self.camera_writer = cv2.VideoWriter(
                    cpath, cv2.VideoWriter_fourcc(*'mp4v'),
                    cam_write_fps, (w, h))
            self.signals.status_message.emit(f"Camera segment: {cpath}")

        self.current_segment_start = time.time()
        # Reset frame counters
        self._scr_frame_idx = 0
        self._cam_frame_idx = 0
        self._seg_start_time = time.time()

    # ── Screen loop ───────────────────────────
    def _screen_loop(self):
        with mss.mss() as sct:
            mon_idx = 2 if len(sct.monitors) > 2 else 1
            monitor = sct.monitors[mon_idx]
            interval = 1.0 / self.actual_screen_fps
            next_t   = time.perf_counter()
            while not self._screen_stop.is_set():
                now = time.perf_counter()
                if now < next_t:
                    time.sleep(next_t - now)
                next_t += interval

                img   = sct.grab(monitor)
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                self._screen_fps_ts.append(time.time())

                # ROI
                if self.screen_rois:
                    avgs = self.calc_roi_avg(frame, self.screen_rois)
                    self.screen_roi_avg     = avgs
                    self.screen_overall_avg = np.mean(avgs, axis=0) if avgs else np.zeros(3)
                    if self.screen_roi_prev:
                        self.detect_blackout(avgs, self.screen_roi_prev, "screen")
                    self.screen_roi_prev = [a.copy() for a in avgs]

                # Overlay + buffer
                if self.recording and self.start_time:
                    stamped = self._add_overlay(frame.copy(), self.screen_rois)
                else:
                    stamped = frame

                with self._buf_lock:
                    self._screen_buffer.append(stamped.copy())
                    while len(self._screen_buffer) > self.screen_buf_max:
                        self._screen_buffer.popleft()

                # ★ PTS-sync write
                if self.recording and self.screen_writer and self.screen_recording_enabled:
                    elapsed = time.time() - self._seg_start_time
                    with self._writer_lock:
                        self._scr_frame_idx = self._write_frame_sync(
                            self.screen_writer, stamped,
                            self.actual_screen_fps,
                            self._scr_frame_idx, elapsed)

                try:
                    self.screen_queue.put_nowait(frame)
                except queue.Full:
                    pass

    # ── Camera loop ───────────────────────────
    def _camera_loop(self):
        idx = self.active_camera_idx
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            self.signals.status_message.emit(f"ERROR: Camera {idx} 열기 실패"); return

        # 카메라 실제 FPS를 다시 확인하여 interval 계산에 사용
        reported = cap.get(cv2.CAP_PROP_FPS)
        if reported and 0 < reported < 300:
            cam_fps = float(reported)
        else:
            cam_fps = self.actual_camera_fps
        # 엔진 FPS 동기화
        self.actual_camera_fps = cam_fps

        interval = 1.0 / cam_fps
        next_t   = time.perf_counter()

        while not self._camera_stop.is_set():
            now = time.perf_counter()
            if now < next_t:
                time.sleep(next_t - now)
            next_t += interval

            ret, frame = cap.read()
            if not ret: continue
            self._camera_fps_ts.append(time.time())

            if self.camera_rois:
                avgs = self.calc_roi_avg(frame, self.camera_rois)
                self.camera_roi_avg     = avgs
                self.camera_overall_avg = np.mean(avgs, axis=0) if avgs else np.zeros(3)
                if self.camera_roi_prev:
                    self.detect_blackout(avgs, self.camera_roi_prev, "camera")
                self.camera_roi_prev = [a.copy() for a in avgs]

            if self.recording and self.start_time:
                stamped = self._add_overlay(frame.copy(), self.camera_rois)
            else:
                stamped = frame

            with self._buf_lock:
                self._camera_buffer.append(stamped.copy())
                while len(self._camera_buffer) > self.camera_buf_max:
                    self._camera_buffer.popleft()

            # ★ PTS-sync write
            if self.recording and self.camera_writer:
                elapsed = time.time() - self._seg_start_time
                with self._writer_lock:
                    self._cam_frame_idx = self._write_frame_sync(
                        self.camera_writer, stamped,
                        cam_fps, self._cam_frame_idx, elapsed)

            try:
                self.camera_queue.put_nowait(frame)
            except queue.Full:
                pass
        cap.release()

    # ── Thread controls ───────────────────────
    def start_screen_thread(self):
        if self._screen_thread and self._screen_thread.is_alive(): return
        self._screen_stop.clear()
        self._screen_thread = threading.Thread(target=self._screen_loop, daemon=True)
        self._screen_thread.start()
        self.signals.status_message.emit("Screen thread started")

    def stop_screen_thread(self):
        self._screen_stop.set()
        self.signals.status_message.emit("Screen thread stopped")

    def start_camera_thread(self):
        if self._camera_thread and self._camera_thread.is_alive(): return
        self._camera_stop.clear()
        self._camera_thread = threading.Thread(target=self._camera_loop, daemon=True)
        self._camera_thread.start()
        self.signals.status_message.emit("Camera thread started")

    def stop_camera_thread(self):
        self._camera_stop.set()
        self.signals.status_message.emit("Camera thread stopped")

    # ── Recording ─────────────────────────────
    def start_recording(self):
        if self.recording: return
        ts  = datetime.now().strftime("Rec_%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(BASE_DIR, ts)
        os.makedirs(self.output_dir, exist_ok=True)
        self._scr_frame_idx = 0
        self._cam_frame_idx = 0
        self._seg_start_time = time.time()
        self._create_segment()
        self.start_time = time.time()
        self.recording  = True
        # Save memo snapshot
        if self.memo_text.strip():
            memo_path = os.path.join(self.output_dir, "memo.txt")
            with open(memo_path, "w", encoding="utf-8") as f:
                f.write(self.memo_text)
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
        self.signals.status_message.emit("Recording stopped")
        self.signals.rec_stopped.emit()

    # ── Auto-click ────────────────────────────
    def _ac_loop(self):
        mc = pynput_mouse.Controller() if PYNPUT_AVAILABLE else None
        while not self._ac_stop.is_set():
            if mc: mc.click(pynput_mouse.Button.left)
            self.auto_click_count += 1
            self.signals.auto_click_count.emit(self.auto_click_count)
            self._ac_stop.wait(self.auto_click_interval)

    def start_auto_click(self):
        if self.auto_click_enabled: return
        self.auto_click_enabled = True
        self._ac_stop.clear()
        self._ac_thread = threading.Thread(target=self._ac_loop, daemon=True)
        self._ac_thread.start()

    def stop_auto_click(self):
        self.auto_click_enabled = False
        self._ac_stop.set()

    def reset_click_count(self):
        self.auto_click_count = 0
        self.signals.auto_click_count.emit(0)

    # ── Schedule tick ─────────────────────────
    def schedule_tick(self):
        """Returns list of actions ['start'|'stop'] triggered this tick."""
        now = datetime.now()
        actions = []
        for s in list(self.schedules):
            if s.done: continue
            if s.start_dt and not s.started:
                delta = (s.start_dt - now).total_seconds()
                if -2 <= delta <= 1:
                    s.started = True
                    if not self.recording:
                        actions.append(('start', s))
            if s.stop_dt and s.started and not s.stopped:
                delta = (s.stop_dt - now).total_seconds()
                if -2 <= delta <= 1:
                    s.stopped = True
                    s.done    = True
                    if self.recording:
                        actions.append(('stop', s))
            # No stop_dt: mark done once started
            if s.started and not s.stop_dt and not s.done:
                s.done = True
        return actions

    # ── Engine start / stop ───────────────────
    def start(self):
        self.running = True
        self.detect_camera_fps()
        self.actual_screen_fps = self.target_screen_fps
        self.start_screen_thread()
        self.start_camera_thread()

    def stop(self):
        self.stop_recording()
        self.stop_auto_click()
        self._screen_stop.set()
        self._camera_stop.set()
        self.running = False


# ─────────────────────────────────────────────
#  PreviewLabel
# ─────────────────────────────────────────────
class PreviewLabel(QLabel):
    roi_changed = pyqtSignal()

    def __init__(self, source: str, engine: RecorderEngine, parent=None):
        super().__init__(parent)
        self.source    = source
        self.engine    = engine
        self._drawing  = False
        self._pt1 = self._pt2 = QPoint()
        self._raw_size = (1, 1)
        self._active   = True
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self._set_idle_style()

    def _set_idle_style(self):
        self.setStyleSheet("background:#0d0d1e; border:1px solid #334;")

    def set_active(self, v: bool):
        self._active = v
        if not v:
            self.clear()
            self.setText("⏸ Thread Paused")
            self.setStyleSheet("background:#0d0d1e; border:1px solid #334; "
                               "color:#555; font-size:18px; font-weight:bold;")
        else:
            self.clear(); self._set_idle_style()

    def _rois(self):
        return self.engine.screen_rois if self.source == "screen" else self.engine.camera_rois

    def _label_to_raw(self, qp: QPoint):
        pw, ph = self.width(), self.height()
        rw, rh = self._raw_size
        sc = min(pw/rw, ph/rh)
        ox = (pw - rw*sc)/2; oy = (ph - rh*sc)/2
        return int((qp.x()-ox)/sc), int((qp.y()-oy)/sc)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drawing = True; self._pt1 = self._pt2 = e.pos()
        elif e.button() == Qt.RightButton:
            if self._rois(): self._rois().pop(); self.roi_changed.emit()

    def mouseMoveEvent(self, e):
        if self._drawing: self._pt2 = e.pos(); self.update()

    def mouseReleaseEvent(self, e):
        if self._drawing and e.button() == Qt.LeftButton:
            self._drawing = False
            x1,y1 = self._label_to_raw(self._pt1)
            x2,y2 = self._label_to_raw(self._pt2)
            rx,ry = min(x1,x2), min(y1,y2)
            rw,rh = abs(x1-x2), abs(y1-y2)
            if rw>5 and rh>5 and len(self._rois())<10:
                self._rois().append((rx,ry,rw,rh)); self.roi_changed.emit()
            self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drawing:
            p = QPainter(self)
            p.setPen(QPen(QColor(255,80,80), 2, Qt.DashLine))
            p.drawRect(QRect(self._pt1, self._pt2).normalized())

    def update_frame(self, frame: np.ndarray):
        if not self._active: return
        self._raw_size = (frame.shape[1], frame.shape[0])
        rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        disp = rgb.copy()
        for i,(rx,ry,rw,rh) in enumerate(self._rois()):
            cv2.rectangle(disp,(rx,ry),(rx+rw,ry+rh),(255,60,60),2)
            cv2.putText(disp,f"ROI{i+1}",(rx,max(ry-4,12)),
                        cv2.FONT_HERSHEY_SIMPLEX,0.45,(255,60,60),1)
        h,w,_ = disp.shape
        qi  = QImage(disp.data, w, h, 3*w, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qi).scaled(self.size(), Qt.KeepAspectRatio,
                                            Qt.SmoothTransformation)
        self.setPixmap(pix)


# ─────────────────────────────────────────────
#  ThreadToggleBtn
# ─────────────────────────────────────────────
class ThreadToggleBtn(QPushButton):
    def __init__(self, label_on="▶ ON", label_off="⏸ OFF", parent=None):
        super().__init__(parent)
        self._lon = label_on; self._loff = label_off
        self.setFixedHeight(26); self.setCheckable(True); self.setChecked(True)
        self.toggled.connect(self._upd)
        self._upd(True)

    def _upd(self, checked):
        self.setText(self._lon if checked else self._loff)
        self.setStyleSheet(
            "QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #1a6b3a,stop:1 #27ae60);color:#eaffea;border:none;"
            "border-radius:13px;font-size:10px;font-weight:bold;padding:0 10px;}"
            "QPushButton:hover{background:#2ecc71;}" if checked else
            "QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #3a1a1a,stop:1 #7f3030);color:#ffcccc;border:none;"
            "border-radius:13px;font-size:10px;font-weight:bold;padding:0 10px;}"
            "QPushButton:hover{background:#c0392b;}"
        )


# ─────────────────────────────────────────────
#  Camera Window  (카메라 선택 + 미리보기)
# ─────────────────────────────────────────────
class CameraWindow(QDialog):
    def __init__(self, engine: RecorderEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine  = engine
        self.signals = signals
        self.setWindowTitle("📷  Camera Feed")
        self.setWindowFlags(Qt.Window | Qt.WindowMinimizeButtonHint |
                            Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
        self.resize(680, 540)
        self.setStyleSheet("background:#0d0d1e; color:#ddd;")

        v = QVBoxLayout(self); v.setSpacing(6); v.setContentsMargins(8,8,8,8)

        # ── 헤더 ─────────────────────────────
        hdr = QHBoxLayout()
        lbl = QLabel("📷  Camera Preview")
        lbl.setStyleSheet("color:#9ab; font-weight:bold; font-size:13px;")
        hdr.addWidget(lbl); hdr.addStretch()
        self._fps_lbl = QLabel("FPS: —")
        self._fps_lbl.setStyleSheet("color:#888; font-size:11px;")
        hdr.addWidget(self._fps_lbl)
        self._toggle = ThreadToggleBtn("▶ Thread ON", "⏸ Thread OFF")
        self._toggle.toggled.connect(self._on_toggle)
        hdr.addWidget(self._toggle)
        v.addLayout(hdr)

        # ── 카메라 선택 패널 ──────────────────
        cam_grp = QGroupBox("🎥  연결된 카메라 선택")
        cam_grp.setStyleSheet(
            "QGroupBox{border:1px solid #2a3a5a;border-radius:5px;"
            "margin-top:18px;padding-top:10px;color:#9bc;font-size:11px;font-weight:bold;}"
            "QGroupBox::title{subcontrol-origin:margin;left:10px;top:-2px;"
            "padding:2px 6px;background:#0d0d1e;border-radius:3px;}")
        cg = QVBoxLayout(cam_grp); cg.setSpacing(6)

        # 스캔 버튼 + 현재 선택 표시
        scan_row = QHBoxLayout()
        self._scan_btn = QPushButton("🔍  카메라 스캔")
        self._scan_btn.setFixedHeight(28)
        self._scan_btn.setStyleSheet(
            "QPushButton{background:#1a2a4a;color:#7bc8e0;border:1px solid #2a4a7a;"
            "border-radius:4px;font-size:11px;padding:2px 10px;}"
            "QPushButton:hover{background:#223366;}")
        self._scan_btn.clicked.connect(self._on_scan)
        self._sel_lbl = QLabel("선택된 카메라: —")
        self._sel_lbl.setStyleSheet("color:#f0c040; font-size:11px;")
        scan_row.addWidget(self._scan_btn)
        scan_row.addStretch()
        scan_row.addWidget(self._sel_lbl)
        cg.addLayout(scan_row)

        # 카메라 목록 (라디오버튼 방식의 QListWidget)
        self._cam_list = QListWidget()
        self._cam_list.setFixedHeight(110)
        self._cam_list.setStyleSheet(
            "QListWidget{background:#080818;border:1px solid #223;color:#ccc;"
            "font-size:11px;border-radius:3px;outline:none;}"
            "QListWidget::item{padding:5px 8px;border-bottom:1px solid #1a1a2e;}"
            "QListWidget::item:selected{background:#1a2a4a;color:#7bc8e0;}"
            "QListWidget::item:hover{background:#141424;}")
        self._cam_list.itemClicked.connect(self._on_cam_selected)
        cg.addWidget(self._cam_list)

        # 선택 카메라 상세 정보
        self._cam_info_lbl = QLabel("카메라를 스캔하세요")
        self._cam_info_lbl.setStyleSheet(
            "color:#666; font-size:10px; font-family:monospace;")
        cg.addWidget(self._cam_info_lbl)

        v.addWidget(cam_grp)

        # ── 미리보기 ──────────────────────────
        self._lbl = PreviewLabel("camera", self.engine)
        v.addWidget(self._lbl, 1)
        hint = QLabel("Left-drag: add ROI  |  Right-click: remove")
        hint.setStyleSheet("color:#555; font-size:10px;")
        hint.setAlignment(Qt.AlignCenter)
        v.addWidget(hint)

        # FPS 업데이트 타이머
        t = QTimer(self)
        t.timeout.connect(self._update_fps_lbl)
        t.start(2000)

        # 초기 스캔 (백그라운드)
        threading.Thread(target=self._bg_scan, daemon=True).start()

    # ── 카메라 스캔 ───────────────────────────
    def _bg_scan(self):
        """백그라운드 스캔 후 UI 업데이트."""
        self.engine.scan_cameras()
        # Qt 메인 스레드에서 업데이트
        QTimer.singleShot(0, self._populate_list)

    def _on_scan(self):
        self._cam_list.clear()
        self._cam_info_lbl.setText("스캔 중…")
        self._scan_btn.setEnabled(False)
        threading.Thread(target=self._bg_scan, daemon=True).start()
        QTimer.singleShot(200, lambda: self._scan_btn.setEnabled(True))

    def _populate_list(self):
        self._cam_list.clear()
        cams = self.engine.camera_list
        if not cams:
            item = QListWidgetItem("  카메라 없음")
            item.setForeground(QColor("#666"))
            self._cam_list.addItem(item)
            self._cam_info_lbl.setText("연결된 카메라를 찾을 수 없습니다.")
            return

        for cam in cams:
            item = QListWidgetItem(f"  ✓  {cam['name']}")
            item.setData(Qt.UserRole, cam["idx"])
            self._cam_list.addItem(item)

        # 현재 활성 카메라를 선택 상태로
        for i in range(self._cam_list.count()):
            it = self._cam_list.item(i)
            if it.data(Qt.UserRole) == self.engine.active_camera_idx:
                self._cam_list.setCurrentRow(i)
                self._update_sel_label(self.engine.active_camera_idx)
                break

        self._cam_info_lbl.setText(
            f"총 {len(cams)}개 카메라 감지됨  |  FPS는 각 카메라별 자동 감지")

    def _on_cam_selected(self, item: QListWidgetItem):
        idx = item.data(Qt.UserRole)
        if idx is None: return
        cam = next((c for c in self.engine.camera_list if c["idx"] == idx), None)
        if cam:
            self.engine.select_camera(idx)
            self._update_sel_label(idx)
            self._cam_info_lbl.setText(
                f"활성: {cam['name']}  |  캡처 FPS: {cam['fps']:.2f}")

    def _update_sel_label(self, idx: int):
        cam = next((c for c in self.engine.camera_list if c["idx"] == idx), None)
        name = cam["name"] if cam else str(idx)
        self._sel_lbl.setText(f"선택: {name}")

    def _update_fps_lbl(self):
        fps = self.engine.measured_fps(self.engine._camera_fps_ts)
        self._fps_lbl.setText(f"실측 FPS: {fps:.1f}")

    # ── Thread toggle ─────────────────────────
    def _on_toggle(self, checked: bool):
        self._lbl.set_active(checked)
        if checked: self.engine.start_camera_thread()
        else:       self.engine.stop_camera_thread()

    def get_label(self): return self._lbl

    def closeEvent(self, e): e.ignore(); self.hide()


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def _sec_hdr(title: str) -> QLabel:
    l = QLabel(title)
    l.setStyleSheet("""QLabel{
        background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #1e2240,stop:1 #12122a);
        color:#7ab4d4; font-size:12px; font-weight:bold;
        padding:7px 10px; border-left:3px solid #3a7bd5;
        border-radius:2px; margin-top:12px; margin-bottom:4px;}""")
    return l

def _folder_btn(label: str, path_fn) -> QPushButton:
    btn = QPushButton(label)
    btn.setStyleSheet(
        "QPushButton{background:#1a2a1a;color:#8fa;border:1px solid #2a5a2a;"
        "border-radius:4px;padding:3px 8px;font-size:10px;}"
        "QPushButton:hover{background:#223a22;}")
    btn.clicked.connect(lambda: open_folder(path_fn()))
    return btn


# ─────────────────────────────────────────────
#  Feature Panel  (드래그&드롭 순서 변경 + 세로 스크롤)
# ─────────────────────────────────────────────
class FeatureListWidget(QWidget):
    """
    체크박스 + 드래그&드롭으로 섹션 표시 ON/OFF 및 순서를 제어하는 위젯.
    순서가 바뀌면 order_changed(key_list) 시그널이 발생합니다.
    """
    toggled       = pyqtSignal(str, bool)    # key, enabled
    order_changed = pyqtSignal(list)         # [key, ...]  새 순서

    FEATURES = [
        ("recording", "⏺  Recording"),
        ("schedule",  "⏰  Schedule"),
        ("blackout",  "⚡  Blackout"),
        ("autoclick", "🖱  Auto-Click"),
        ("memo",      "📝  Memo"),
        ("log",       "📋  Log"),
    ]

    _ITEM_H     = 34    # 항목 높이
    _DRAG_COLOR = "#2a3a5a"
    _IDLE_COLOR = "#12122e"
    _HOVER_COLOR= "#1a2a3a"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checks: dict[str, QCheckBox] = {}
        self._rows:   list[tuple[str, QWidget]] = []   # (key, row_widget)
        self._drag_key:   str | None = None
        self._drag_start: QPoint = QPoint()
        self._drag_idx:   int    = -1
        self._hover_idx:  int    = -1
        self._dragging    = False

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)

        for key, text in self.FEATURES:
            row_w = self._make_row(key, text)
            self._rows.append((key, row_w))
            self._layout.addWidget(row_w)

        self._layout.addStretch()
        self.setAcceptDrops(True)

    # ── row factory ──────────────────────────
    def _make_row(self, key: str, text: str) -> QWidget:
        row = QWidget()
        row.setFixedHeight(self._ITEM_H)
        row.setStyleSheet(
            f"QWidget{{background:{self._IDLE_COLOR};border-radius:4px;}}"
            f"QWidget:hover{{background:{self._HOVER_COLOR};}}")
        row.setProperty("key", key)
        row.setCursor(Qt.OpenHandCursor)

        h = QHBoxLayout(row)
        h.setContentsMargins(6, 2, 8, 2)
        h.setSpacing(6)

        # drag handle
        grip = QLabel("⠿")
        grip.setStyleSheet("color:#447; font-size:16px; padding:0;")
        grip.setCursor(Qt.OpenHandCursor)
        h.addWidget(grip)

        cb = QCheckBox(text)
        cb.setChecked(True)
        cb.setStyleSheet(
            "QCheckBox{font-size:12px;color:#dde;spacing:6px;background:transparent;}"
            "QCheckBox::indicator{width:15px;height:15px;}")
        cb.toggled.connect(lambda v, k=key: self.toggled.emit(k, v))
        self._checks[key] = cb
        h.addWidget(cb, 1)

        return row

    def is_enabled(self, key: str) -> bool:
        return self._checks[key].isChecked()

    def current_order(self) -> list[str]:
        return [k for k, _ in self._rows]

    # ── drag & drop (custom, works inside QScrollArea) ──
    def _row_at_y(self, y: int) -> int:
        for i, (_, w) in enumerate(self._rows):
            wy = w.y(); wh = w.height()
            if wy <= y < wy + wh:
                return i
        return -1

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            idx = self._row_at_y(e.pos().y())
            if idx >= 0:
                self._drag_idx   = idx
                self._drag_key   = self._rows[idx][0]
                self._drag_start = e.pos()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._drag_idx >= 0
                and (e.pos() - self._drag_start).manhattanLength() > 8):
            self._dragging   = True
            self._hover_idx  = self._row_at_y(e.pos().y())
            self._highlight(self._hover_idx)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragging and self._drag_idx >= 0:
            target = self._row_at_y(e.pos().y())
            if target >= 0 and target != self._drag_idx:
                self._move_row(self._drag_idx, target)
        # reset
        self._dragging  = False
        self._drag_idx  = -1
        self._drag_key  = None
        self._hover_idx = -1
        self._clear_highlight()
        super().mouseReleaseEvent(e)

    def _highlight(self, idx: int):
        for i, (_, w) in enumerate(self._rows):
            if i == idx:
                w.setStyleSheet(
                    f"QWidget{{background:{self._DRAG_COLOR};"
                    f"border:1px solid #5a7aaa;border-radius:4px;}}")
            else:
                w.setStyleSheet(
                    f"QWidget{{background:{self._IDLE_COLOR};border-radius:4px;}}"
                    f"QWidget:hover{{background:{self._HOVER_COLOR};}}")

    def _clear_highlight(self):
        for _, w in self._rows:
            w.setStyleSheet(
                f"QWidget{{background:{self._IDLE_COLOR};border-radius:4px;}}"
                f"QWidget:hover{{background:{self._HOVER_COLOR};}}")

    def _move_row(self, src: int, dst: int):
        # 레이아웃에서 모든 row 제거 후 새 순서로 재삽입
        item = self._rows.pop(src)
        self._rows.insert(dst, item)

        # QLayout 재구성
        # stretch 아이템까지 모두 제거
        while self._layout.count():
            w = self._layout.takeAt(0)
            if w.widget():
                w.widget().setParent(None)

        for _, w in self._rows:
            w.setParent(self)
            self._layout.addWidget(w)
        self._layout.addStretch()

        self.order_changed.emit(self.current_order())


class FeatureBar(QFrame):
    """
    FeatureListWidget을 세로 스크롤 영역으로 감싼 컨테이너.
    외부에서 toggled / order_changed 시그널을 연결합니다.
    """
    toggled       = pyqtSignal(str, bool)
    order_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame{background:#08081a;border-bottom:2px solid #2a3a5a;}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 4)
        outer.setSpacing(4)

        # 헤더
        hdr = QHBoxLayout()
        title = QLabel("⚙  표시 · 순서 설정")
        title.setStyleSheet(
            "color:#7ab4d4;font-size:12px;font-weight:bold;")
        hint = QLabel("드래그로 순서 변경")
        hint.setStyleSheet("color:#446;font-size:10px;")
        hdr.addWidget(title); hdr.addStretch(); hdr.addWidget(hint)
        outer.addLayout(hdr)

        # 세로 스크롤 영역
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setFixedHeight(170)     # ← 높이 확장 (약 4~5개 행 표시)
        scroll.setStyleSheet(
            "QScrollArea{border:1px solid #223;background:transparent;border-radius:4px;}"
            "QScrollBar:vertical{background:#0d0d1e;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:3px;min-height:16px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")

        self._list = FeatureListWidget()
        self._list.toggled.connect(self.toggled)
        self._list.order_changed.connect(self.order_changed)

        scroll.setWidget(self._list)
        outer.addWidget(scroll)

    def is_enabled(self, key: str) -> bool:
        return self._list.is_enabled(key)

    def current_order(self) -> list[str]:
        return self._list.current_order()


# ─────────────────────────────────────────────
#  Main Window
# ─────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen & Camera Recorder  v4.0")
        self.resize(1380, 860)
        self.setStyleSheet(self._dark_style())

        self.signals = Signals()
        self.engine  = RecorderEngine(self.signals)
        self._cam_win = CameraWindow(self.engine, self.signals, self)

        self._build_ui()
        self._connect_signals()

        # Timers
        QTimer(self, timeout=self._refresh_ui,       interval=500 ).start()
        QTimer(self, timeout=self._update_fps,        interval=2000).start()
        QTimer(self, timeout=self._check_segment,     interval=5000).start()
        QTimer(self, timeout=self._tick_schedule,     interval=1000).start()
        QTimer(self, timeout=self._pump_preview,      interval=33  ).start()

        self._setup_hotkeys()
        self.engine.start()

    # ─────────────────────────────────────────
    #  UI Build
    # ─────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setSpacing(8); root.setContentsMargins(8,8,8,8)

        # ── Left: Screen preview ──────────────
        left = QVBoxLayout(); left.setSpacing(6)

        scr_hdr = QHBoxLayout()
        t = QLabel("🖥  Screen Preview")
        t.setStyleSheet("color:#9ab;font-weight:bold;font-size:13px;")
        scr_hdr.addWidget(t); scr_hdr.addStretch()
        self._scr_fps_badge = QLabel("FPS: —")
        self._scr_fps_badge.setStyleSheet("color:#888;font-size:11px;")
        scr_hdr.addWidget(self._scr_fps_badge)
        self._scr_toggle = ThreadToggleBtn("▶ Thread ON","⏸ Thread OFF")
        self._scr_toggle.toggled.connect(self._on_scr_toggle)
        scr_hdr.addWidget(self._scr_toggle)
        self._cam_win_btn = QPushButton("📷 Camera Window")
        self._cam_win_btn.setCheckable(True)
        self._cam_win_btn.setStyleSheet(
            "QPushButton{background:#1a2a3a;color:#7bc8e0;border:1px solid #2a4a6a;"
            "border-radius:13px;font-size:10px;padding:2px 10px;}"
            "QPushButton:checked{background:#1a4060;}")
        self._cam_win_btn.toggled.connect(self._on_cam_win_toggle)
        scr_hdr.addWidget(self._cam_win_btn)

        scr_frame = QFrame()
        scr_frame.setStyleSheet(
            "QFrame{border:1px solid #334;border-radius:6px;background:#0d0d1e;}")
        sf_lay = QVBoxLayout(scr_frame)
        sf_lay.setContentsMargins(4,4,4,4); sf_lay.setSpacing(3)
        sf_lay.addLayout(scr_hdr)
        self._scr_lbl = PreviewLabel("screen", self.engine)
        sf_lay.addWidget(self._scr_lbl, 1)
        hint = QLabel("Left-drag: add ROI  |  Right-click: remove ROI")
        hint.setStyleSheet("color:#555;font-size:10px;"); hint.setAlignment(Qt.AlignCenter)
        sf_lay.addWidget(hint)
        left.addWidget(scr_frame, 1)

        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(
            "color:#888;font-size:11px;padding:2px 4px;border-top:1px solid #334;")
        left.addWidget(self._status_lbl)

        # ── Right: Control panel ──────────────
        right_w = QWidget(); right_w.setFixedWidth(400)
        right_v = QVBoxLayout(right_w)
        right_v.setContentsMargins(0,0,0,0); right_v.setSpacing(0)

        # Panel title
        pt = QLabel("⚙  Control Panel")
        pt.setStyleSheet("color:#ccc;font-size:13px;font-weight:bold;"
                         "padding:8px 10px;background:#1a1a3a;border-bottom:1px solid #334;")
        right_v.addWidget(pt)

        # Feature toggle bar
        self._feat_bar = FeatureBar()
        self._feat_bar.toggled.connect(self._on_feature_toggle)
        self._feat_bar.order_changed.connect(self._on_feature_order_changed)
        right_v.addWidget(self._feat_bar)

        # Scrollable panel
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{background:#0d0d1e;width:8px;border-radius:4px;}"
            "QScrollBar::handle:vertical{background:#336;border-radius:4px;min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")

        self._panel_widget = QWidget(); self._panel_widget.setStyleSheet("background:#12122a;")
        self._panel_layout = QVBoxLayout(self._panel_widget)
        pl = self._panel_layout
        pl.setContentsMargins(8,4,8,14); pl.setSpacing(8)

        # ① Recording
        self._sec_recording = QWidget()
        rl = QVBoxLayout(self._sec_recording); rl.setContentsMargins(0,0,0,0); rl.setSpacing(6)
        rl.addWidget(_sec_hdr("⏺  Recording"))
        rl.addWidget(self._build_status_grp())
        rl.addWidget(self._build_rec_btn_grp())
        rl.addWidget(self._build_screen_rec_grp())
        rl.addWidget(self._build_fps_grp())
        pl.addWidget(self._sec_recording)

        # ② Schedule
        self._sec_schedule = QWidget()
        sl = QVBoxLayout(self._sec_schedule); sl.setContentsMargins(0,0,0,0); sl.setSpacing(6)
        sl.addWidget(_sec_hdr("⏰  Schedule (예약 녹화)"))
        sl.addWidget(self._build_schedule_grp())
        pl.addWidget(self._sec_schedule)

        # ③ Blackout
        self._sec_blackout = QWidget()
        bl = QVBoxLayout(self._sec_blackout); bl.setContentsMargins(0,0,0,0); bl.setSpacing(6)
        bl.addWidget(_sec_hdr("⚡  Blackout Detection"))
        bl.addWidget(self._build_blackout_grp())
        pl.addWidget(self._sec_blackout)

        # ④ Auto-Click
        self._sec_autoclick = QWidget()
        al = QVBoxLayout(self._sec_autoclick); al.setContentsMargins(0,0,0,0); al.setSpacing(6)
        al.addWidget(_sec_hdr("🖱  Auto-Click"))
        al.addWidget(self._build_autoclick_grp())
        pl.addWidget(self._sec_autoclick)

        # ⑤ Memo
        self._sec_memo = QWidget()
        ml = QVBoxLayout(self._sec_memo); ml.setContentsMargins(0,0,0,0); ml.setSpacing(6)
        ml.addWidget(_sec_hdr("📝  메모장"))
        ml.addWidget(self._build_memo_grp())
        pl.addWidget(self._sec_memo)

        # ⑥ Log
        self._sec_log = QWidget()
        ll = QVBoxLayout(self._sec_log); ll.setContentsMargins(0,0,0,0); ll.setSpacing(6)
        ll.addWidget(_sec_hdr("📋  Log"))
        ll.addWidget(self._build_log_grp())
        pl.addWidget(self._sec_log)

        pl.addStretch()

        # 섹션 딕셔너리 (순서 변경에 사용)
        self._sec_map = {
            "recording": self._sec_recording,
            "schedule":  self._sec_schedule,
            "blackout":  self._sec_blackout,
            "autoclick": self._sec_autoclick,
            "memo":      self._sec_memo,
            "log":       self._sec_log,
        }
        scroll.setWidget(self._panel_widget)
        right_v.addWidget(scroll, 1)

        root.addLayout(left, 1)
        root.addWidget(right_w, 0)

    # ─────────────────────────────────────────
    #  Section widgets
    # ─────────────────────────────────────────
    def _build_status_grp(self):
        grp = QGroupBox("Status"); g = QGridLayout(grp); g.setSpacing(6)
        self._rec_status_lbl = QLabel("● STOPPED")
        self._rec_status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        g.addWidget(self._rec_status_lbl, 0, 0, 1, 2)
        self._rec_timer_lbl = QLabel("00:00:00")
        self._rec_timer_lbl.setStyleSheet(
            "font-size:26px;font-weight:bold;color:#2ecc71;font-family:monospace;")
        g.addWidget(self._rec_timer_lbl, 1, 0, 1, 2, Qt.AlignCenter)
        g.addWidget(QLabel("Screen FPS:"), 2, 0)
        self._scr_fps_lbl = QLabel("—"); g.addWidget(self._scr_fps_lbl, 2, 1)
        g.addWidget(QLabel("Camera FPS:"), 3, 0)
        self._cam_fps_lbl = QLabel("—"); g.addWidget(self._cam_fps_lbl, 3, 1)
        # Folder button
        self._rec_dir_btn = _folder_btn("📂 녹화 폴더 열기",
                                         lambda: self.engine.output_dir or BASE_DIR)
        g.addWidget(self._rec_dir_btn, 4, 0, 1, 2)
        return grp

    def _build_rec_btn_grp(self):
        grp = QGroupBox("Controls"); bg = QVBoxLayout(grp); bg.setSpacing(8)
        self._btn_start = QPushButton("⏺  Start Recording  [Ctrl+Alt+W]")
        self._btn_start.setStyleSheet(
            "background:#27ae60;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;")
        self._btn_start.clicked.connect(self._on_start_rec)
        self._btn_stop = QPushButton("⏹  Stop Recording  [Ctrl+Alt+E]")
        self._btn_stop.setStyleSheet(
            "background:#c0392b;color:white;font-size:12px;padding:8px;"
            "border-radius:5px;border:none;")
        self._btn_stop.clicked.connect(self._on_stop_rec)
        self._btn_stop.setEnabled(False)
        bg.addWidget(self._btn_start); bg.addWidget(self._btn_stop)
        return grp

    def _build_screen_rec_grp(self):
        grp = QGroupBox("Screen Recording"); sl = QVBoxLayout(grp); sl.setSpacing(6)
        self._scr_rec_chk = QCheckBox("Enable screen recording  [Ctrl+Alt+D]")
        self._scr_rec_chk.setChecked(True)
        self._scr_rec_chk.toggled.connect(self._on_scr_rec_toggle)
        sl.addWidget(self._scr_rec_chk)
        tip = QLabel("Disabling reduces CPU / disk load.")
        tip.setStyleSheet("color:#666;font-size:10px;"); sl.addWidget(tip)
        return grp

    def _build_fps_grp(self):
        grp = QGroupBox("FPS & 배속 설정"); fl = QGridLayout(grp); fl.setSpacing(8)

        fl.addWidget(QLabel("Target Screen FPS:"), 0, 0)
        self._scr_fps_spin = QDoubleSpinBox()
        self._scr_fps_spin.setRange(1, 120); self._scr_fps_spin.setValue(30.0)
        self._scr_fps_spin.setSingleStep(1.0)
        self._scr_fps_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'actual_screen_fps', v))
        fl.addWidget(self._scr_fps_spin, 0, 1)

        fl.addWidget(QLabel("Detected Camera FPS:"), 1, 0)
        self._cam_fps_det_lbl = QLabel("—")
        fl.addWidget(self._cam_fps_det_lbl, 1, 1)

        # ── 배속 설정 ─────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#334;")
        fl.addWidget(sep, 2, 0, 1, 2)

        speed_lbl = QLabel("저장 배속  (x배속):")
        speed_lbl.setStyleSheet("font-size:12px; font-weight:bold; color:#f0c040;")
        fl.addWidget(speed_lbl, 3, 0)

        self._speed_spin = QDoubleSpinBox()
        self._speed_spin.setRange(0.1, 10.0)
        self._speed_spin.setValue(1.0)
        self._speed_spin.setSingleStep(0.25)
        self._speed_spin.setDecimals(2)
        self._speed_spin.setMinimumHeight(28)
        self._speed_spin.setStyleSheet(
            "QDoubleSpinBox{background:#1a1a2a;color:#f0c040;"
            "border:1px solid #5a5a20;border-radius:4px;"
            "padding:3px;font-size:13px;font-weight:bold;}")
        self._speed_spin.valueChanged.connect(self._on_speed_changed)
        fl.addWidget(self._speed_spin, 3, 1)

        # 프리셋 버튼
        preset_row = QHBoxLayout(); preset_row.setSpacing(4)
        for label, val in [("0.25×", 0.25), ("0.5×", 0.5), ("1×", 1.0),
                            ("1.5×", 1.5), ("2×", 2.0), ("4×", 4.0)]:
            b = QPushButton(label)
            b.setFixedHeight(24)
            b.setStyleSheet(
                "QPushButton{background:#2a2a1a;color:#f0c040;border:1px solid #4a4a20;"
                "border-radius:3px;font-size:10px;padding:0 4px;}"
                "QPushButton:hover{background:#3a3a28;}")
            b.clicked.connect(lambda _, v=val: self._speed_spin.setValue(v))
            preset_row.addWidget(b)
        fl.addLayout(preset_row, 4, 0, 1, 2)

        # 설명 레이블
        self._speed_info_lbl = QLabel("  정배속 (1:1 실시간 재생)")
        self._speed_info_lbl.setStyleSheet("color:#888; font-size:10px;")
        fl.addWidget(self._speed_info_lbl, 5, 0, 1, 2)

        # 녹화 중 잠금 경고
        self._speed_lock_lbl = QLabel("🔒 녹화 중에는 배속을 변경할 수 없습니다")
        self._speed_lock_lbl.setStyleSheet(
            "color:#e74c3c; font-size:10px; font-weight:bold;")
        self._speed_lock_lbl.setVisible(False)
        fl.addWidget(self._speed_lock_lbl, 6, 0, 1, 2)

        return grp

    # ── Schedule ──────────────────────────────
    def _build_schedule_grp(self):
        container = QWidget(); v = QVBoxLayout(container)
        v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ── Input area ────────────────────────
        inp_grp = QGroupBox("새 예약 추가"); ig = QVBoxLayout(inp_grp); ig.setSpacing(8)

        # ─ 시작 시각 ─
        ig.addWidget(QLabel("🟢  녹화 시작 시각"))
        row_start = QHBoxLayout(); row_start.setSpacing(6)
        self._sched_start_chk = QCheckBox("사용")
        self._sched_start_chk.setChecked(True)
        self._sched_start_chk.setStyleSheet("font-size:12px;")

        self._sched_start_dt = QDateTimeEdit()
        self._sched_start_dt.setDisplayFormat("yyyy-MM-dd  HH:mm:ss")
        self._sched_start_dt.setCalendarPopup(True)           # ← 달력 팝업
        self._sched_start_dt.setDateTime(QDateTime.currentDateTime().addSecs(60))
        self._sched_start_dt.setMinimumHeight(30)
        self._sched_start_dt.setStyleSheet(
            "QDateTimeEdit{background:#1a1a3a;color:#2ecc71;"
            "border:1px solid #2a6a3a;border-radius:4px;"
            "padding:4px 6px;font-size:12px;font-family:monospace;}"
            "QDateTimeEdit::drop-down{subcontrol-origin:padding;"
            "subcontrol-position:top right;width:22px;"
            "border-left:1px solid #2a6a3a;border-radius:0 4px 4px 0;}"
            "QDateTimeEdit::down-arrow{image:none;width:0;}"
            "QCalendarWidget QToolButton{color:#ddd;background:#1a2a3a;"
            "border-radius:3px;font-size:12px;}"
            "QCalendarWidget QMenu{color:#ddd;background:#1a1a3a;}"
            "QCalendarWidget QWidget#qt_calendar_navigationbar{background:#1a2240;}"
            "QCalendarWidget QAbstractItemView{background:#0d0d1e;color:#ccc;"
            "selection-background-color:#2a4a8a;selection-color:#fff;}")
        btn_now_s = QPushButton("지금")
        btn_now_s.setFixedSize(46, 30)
        btn_now_s.setStyleSheet(
            "QPushButton{background:#1a3a2a;color:#8fa;border:1px solid #2a6a3a;"
            "border-radius:4px;font-size:11px;}")
        btn_now_s.clicked.connect(
            lambda: self._sched_start_dt.setDateTime(QDateTime.currentDateTime()))
        row_start.addWidget(self._sched_start_chk)
        row_start.addWidget(self._sched_start_dt, 1)
        row_start.addWidget(btn_now_s)
        ig.addLayout(row_start)

        # ─ 종료 시각 ─
        ig.addWidget(QLabel("🔴  녹화 종료 시각"))
        row_stop = QHBoxLayout(); row_stop.setSpacing(6)
        self._sched_stop_chk = QCheckBox("사용")
        self._sched_stop_chk.setChecked(True)
        self._sched_stop_chk.setStyleSheet("font-size:12px;")

        self._sched_stop_dt = QDateTimeEdit()
        self._sched_stop_dt.setDisplayFormat("yyyy-MM-dd  HH:mm:ss")
        self._sched_stop_dt.setCalendarPopup(True)            # ← 달력 팝업
        self._sched_stop_dt.setDateTime(QDateTime.currentDateTime().addSecs(3660))
        self._sched_stop_dt.setMinimumHeight(30)
        self._sched_stop_dt.setStyleSheet(
            "QDateTimeEdit{background:#1a1a3a;color:#e74c3c;"
            "border:1px solid #6a2a2a;border-radius:4px;"
            "padding:4px 6px;font-size:12px;font-family:monospace;}"
            "QDateTimeEdit::drop-down{subcontrol-origin:padding;"
            "subcontrol-position:top right;width:22px;"
            "border-left:1px solid #6a2a2a;border-radius:0 4px 4px 0;}"
            "QDateTimeEdit::down-arrow{image:none;width:0;}"
            "QCalendarWidget QToolButton{color:#ddd;background:#1a2a3a;"
            "border-radius:3px;font-size:12px;}"
            "QCalendarWidget QMenu{color:#ddd;background:#1a1a3a;}"
            "QCalendarWidget QWidget#qt_calendar_navigationbar{background:#2a1a1a;}"
            "QCalendarWidget QAbstractItemView{background:#0d0d1e;color:#ccc;"
            "selection-background-color:#8a2a2a;selection-color:#fff;}")
        btn_now_e = QPushButton("지금")
        btn_now_e.setFixedSize(46, 30)
        btn_now_e.setStyleSheet(
            "QPushButton{background:#3a1a1a;color:#f88;border:1px solid #6a2a2a;"
            "border-radius:4px;font-size:11px;}")
        btn_now_e.clicked.connect(
            lambda: self._sched_stop_dt.setDateTime(QDateTime.currentDateTime()))
        row_stop.addWidget(self._sched_stop_chk)
        row_stop.addWidget(self._sched_stop_dt, 1)
        row_stop.addWidget(btn_now_e)
        ig.addLayout(row_stop)

        btn_add = QPushButton("＋  예약 추가")
        btn_add.setMinimumHeight(32)
        btn_add.setStyleSheet(
            "background:#1a4a2a;color:#afffcf;border:1px solid #2a8a5a;"
            "border-radius:4px;padding:5px;font-weight:bold;font-size:12px;")
        btn_add.clicked.connect(self._on_schedule_add)
        ig.addWidget(btn_add)
        v.addWidget(inp_grp)

        # ── Schedule list table ───────────────
        list_grp = QGroupBox("예약 목록"); lg = QVBoxLayout(list_grp); lg.setSpacing(4)
        self._sched_table = QTableWidget(0, 4)
        self._sched_table.setHorizontalHeaderLabels(["#","시작","종료","상태"])
        self._sched_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._sched_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._sched_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._sched_table.setFixedHeight(150)
        self._sched_table.setStyleSheet(
            "QTableWidget{background:#0d0d1e;color:#ccc;font-size:11px;"
            "border:1px solid #334;gridline-color:#223;}"
            "QHeaderView::section{background:#1a1a3a;color:#9ab;font-size:11px;"
            "border:none;padding:4px;}")
        lg.addWidget(self._sched_table)

        btn_row = QHBoxLayout()
        btn_del = QPushButton("선택 삭제"); btn_del.setFixedHeight(26)
        btn_del.clicked.connect(self._on_schedule_delete)
        btn_clr = QPushButton("전체 삭제"); btn_clr.setFixedHeight(26)
        btn_clr.clicked.connect(self._on_schedule_clear)
        btn_row.addWidget(btn_del); btn_row.addWidget(btn_clr)
        lg.addLayout(btn_row)
        v.addWidget(list_grp)

        # ── Countdown ────────────────────────
        cd_grp = QGroupBox("카운트다운"); cl = QVBoxLayout(cd_grp); cl.setSpacing(4)
        self._sched_cd_lbl = QLabel("다음 예약 없음")
        self._sched_cd_lbl.setStyleSheet(
            "color:#f0c040;font-family:monospace;font-size:12px;")
        cl.addWidget(self._sched_cd_lbl)
        v.addWidget(cd_grp)

        return container

    # ── Blackout ──────────────────────────────
    def _build_blackout_grp(self):
        container = QWidget(); v = QVBoxLayout(container)
        v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        self._bo_rec_chk = QCheckBox("Enable Blackout Clip Recording")
        self._bo_rec_chk.setChecked(True)
        self._bo_rec_chk.toggled.connect(
            lambda c: setattr(self.engine,'blackout_recording_enabled',c))
        v.addWidget(self._bo_rec_chk)

        th_grp = QGroupBox("Detection Threshold"); tl = QGridLayout(th_grp)
        tl.addWidget(QLabel("Brightness drop:"), 0, 0)
        self._thr_spin = QDoubleSpinBox(); self._thr_spin.setRange(5,200)
        self._thr_spin.setValue(30.0); self._thr_spin.setSuffix("  (0–255)")
        self._thr_spin.valueChanged.connect(
            lambda v: setattr(self.engine,'brightness_threshold',v))
        tl.addWidget(self._thr_spin, 0, 1)
        tl.addWidget(QLabel("Cooldown (s):"), 1, 0)
        self._cd_spin = QDoubleSpinBox(); self._cd_spin.setRange(0.5,60)
        self._cd_spin.setValue(5.0)
        self._cd_spin.valueChanged.connect(
            lambda v: setattr(self.engine,'blackout_cooldown',v))
        tl.addWidget(self._cd_spin, 1, 1)
        v.addWidget(th_grp)

        cnt_grp = QGroupBox("Counts"); cl = QGridLayout(cnt_grp)
        cl.addWidget(QLabel("Screen:"), 0, 0)
        self._scr_bo_lbl = QLabel("0")
        self._scr_bo_lbl.setStyleSheet("font-weight:bold;color:#e74c3c;")
        cl.addWidget(self._scr_bo_lbl, 0, 1)
        cl.addWidget(QLabel("Camera:"), 1, 0)
        self._cam_bo_lbl = QLabel("0")
        self._cam_bo_lbl.setStyleSheet("font-weight:bold;color:#e74c3c;")
        cl.addWidget(self._cam_bo_lbl, 1, 1)
        # Blackout folder button
        bo_dir_btn = _folder_btn("📂 Blackout 폴더 열기",
                                  lambda: self.engine.blackout_dir)
        cl.addWidget(bo_dir_btn, 2, 0, 1, 2)
        v.addWidget(cnt_grp)

        roi_grp = QGroupBox("ROI Brightness (live)"); rl = QVBoxLayout(roi_grp)
        self._roi_txt = QTextEdit(); self._roi_txt.setReadOnly(True)
        self._roi_txt.setFixedHeight(110)
        self._roi_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;")
        rl.addWidget(self._roi_txt); v.addWidget(roi_grp)

        ev_grp = QGroupBox("Recent Blackout Events"); el = QVBoxLayout(ev_grp)
        self._ev_txt = QTextEdit(); self._ev_txt.setReadOnly(True)
        self._ev_txt.setFixedHeight(90)
        self._ev_txt.setStyleSheet("font-size:10px;font-family:monospace;background:#0d0d1e;")
        el.addWidget(self._ev_txt); v.addWidget(ev_grp)
        return container

    # ── Auto-click ────────────────────────────
    def _build_autoclick_grp(self):
        container = QWidget(); v = QVBoxLayout(container)
        v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        int_grp = QGroupBox("Click Interval"); il = QGridLayout(int_grp)
        il.addWidget(QLabel("Interval (s):"), 0, 0)
        self._ci_spin = QDoubleSpinBox(); self._ci_spin.setRange(0.1,3600)
        self._ci_spin.setValue(1.0); self._ci_spin.setSingleStep(0.1)
        self._ci_spin.valueChanged.connect(
            lambda v: setattr(self.engine,'auto_click_interval',v))
        il.addWidget(self._ci_spin, 0, 1)
        pr = QHBoxLayout()
        for lbl,val in [("0.1s",.1),("0.5s",.5),("1s",1.),("5s",5.),("10s",10.)]:
            b = QPushButton(lbl); b.setFixedWidth(42); b.setFixedHeight(22)
            b.clicked.connect(lambda _,v=val: self._ci_spin.setValue(v))
            pr.addWidget(b)
        il.addLayout(pr, 1, 0, 1, 2)
        v.addWidget(int_grp)

        cnt_grp = QGroupBox("Click Counter"); cl = QGridLayout(cnt_grp)
        self._click_lcd = QLCDNumber(8)
        self._click_lcd.setSegmentStyle(QLCDNumber.Flat)
        self._click_lcd.setFixedHeight(44)
        cl.addWidget(self._click_lcd, 0, 0, 1, 2)
        br = QPushButton("Reset Counter"); br.clicked.connect(self.engine.reset_click_count)
        cl.addWidget(br, 1, 0, 1, 2)
        v.addWidget(cnt_grp)

        ctrl_grp = QGroupBox("Control"); ctl = QVBoxLayout(ctrl_grp); ctl.setSpacing(6)
        self._btn_ac_start = QPushButton("▶  Start Auto-Click  [Ctrl+Alt+A]")
        self._btn_ac_start.setStyleSheet(
            "background:#2980b9;color:white;font-size:12px;padding:7px;"
            "border-radius:5px;border:none;")
        self._btn_ac_start.clicked.connect(self._on_ac_start)
        self._btn_ac_stop = QPushButton("■  Stop Auto-Click  [Ctrl+Alt+S]")
        self._btn_ac_stop.setStyleSheet(
            "background:#7f8c8d;color:white;font-size:12px;padding:7px;"
            "border-radius:5px;border:none;")
        self._btn_ac_stop.clicked.connect(self._on_ac_stop)
        self._btn_ac_stop.setEnabled(False)
        self._ac_status_lbl = QLabel("● STOPPED")
        self._ac_status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;")
        ctl.addWidget(self._btn_ac_start); ctl.addWidget(self._btn_ac_stop)
        ctl.addWidget(self._ac_status_lbl)
        v.addWidget(ctrl_grp)
        return container

    # ── Memo ──────────────────────────────────
    def _build_memo_grp(self):
        grp = QGroupBox("메모 (녹화 영상 우측 하단에 표시됩니다)")
        v = QVBoxLayout(grp); v.setSpacing(6)

        self._memo_overlay_chk = QCheckBox("영상 오버레이 활성화")
        self._memo_overlay_chk.setChecked(True)
        self._memo_overlay_chk.toggled.connect(
            lambda c: setattr(self.engine,'memo_overlay_enabled',c))
        v.addWidget(self._memo_overlay_chk)

        self._memo_edit = QPlainTextEdit()
        self._memo_edit.setPlaceholderText("메모를 입력하세요…\n(최대 5줄이 영상에 표시됩니다)")
        self._memo_edit.setFixedHeight(110)
        self._memo_edit.setStyleSheet(
            "background:#0d0d1e;color:#ffe;border:1px solid #554;"
            "font-size:11px;font-family:monospace;border-radius:3px;")
        self._memo_edit.textChanged.connect(self._on_memo_changed)
        v.addWidget(self._memo_edit)

        btn_row = QHBoxLayout()
        btn_clr = QPushButton("지우기")
        btn_clr.setFixedHeight(24)
        btn_clr.clicked.connect(self._memo_edit.clear)
        btn_row.addStretch(); btn_row.addWidget(btn_clr)
        v.addLayout(btn_row)
        return grp

    # ── Log ───────────────────────────────────
    def _build_log_grp(self):
        grp = QGroupBox("System Log"); v = QVBoxLayout(grp); v.setSpacing(4)
        self._log_txt = QTextEdit(); self._log_txt.setReadOnly(True)
        self._log_txt.setFixedHeight(180)
        self._log_txt.setStyleSheet(
            "font-family:monospace;font-size:10px;background:#080810;color:#aaa;")
        bc = QPushButton("Clear"); bc.setFixedHeight(24)
        bc.clicked.connect(self._log_txt.clear)
        v.addWidget(self._log_txt); v.addWidget(bc)
        return grp

    # ─────────────────────────────────────────
    #  Signal connections
    # ─────────────────────────────────────────
    def _connect_signals(self):
        self.signals.blackout_detected.connect(self._on_blackout)
        self.signals.status_message.connect(self._log)
        self.signals.auto_click_count.connect(self._click_lcd.display)
        self.signals.rec_started.connect(lambda d: None)
        self.signals.rec_stopped.connect(lambda: None)

    # ─────────────────────────────────────────
    #  Preview pump
    # ─────────────────────────────────────────
    def _pump_preview(self):
        rec = self.engine.recording; st = self.engine.start_time
        memo = self.engine.memo_text; me = self.engine.memo_overlay_enabled
        try:
            sf = self.engine.screen_queue.get_nowait()
            if self._scr_toggle.isChecked():
                self._scr_lbl.update_frame(
                    RecorderEngine._stamp_preview(sf, rec, st, memo, me))
        except queue.Empty: pass
        try:
            cf = self.engine.camera_queue.get_nowait()
            if self._cam_win.isVisible():
                self._cam_win.get_label().update_frame(
                    RecorderEngine._stamp_preview(cf, rec, st, memo, me))
        except queue.Empty: pass

    # ─────────────────────────────────────────
    #  Periodic refresh
    # ─────────────────────────────────────────
    def _refresh_ui(self):
        if self.engine.recording and self.engine.start_time:
            e  = time.time() - self.engine.start_time
            self._rec_timer_lbl.setText(
                f"{int(e//3600):02d}:{int((e%3600)//60):02d}:{int(e%60):02d}")
        self._scr_bo_lbl.setText(str(self.engine.screen_blackout_count))
        self._cam_bo_lbl.setText(str(self.engine.camera_blackout_count))
        # ROI
        lines = []
        for src,avgs,overall in [
            ("Scr",self.engine.screen_roi_avg,self.engine.screen_overall_avg),
            ("Cam",self.engine.camera_roi_avg,self.engine.camera_overall_avg)]:
            if avgs:
                b,g,r = overall; br = 0.114*b+0.587*g+0.299*r
                lines.append(f"[{src}] R{int(r)} G{int(g)} B{int(b)} Br:{int(br)}")
                for i,a in enumerate(avgs[:5]):
                    b2,g2,r2=a; br2=0.114*b2+0.587*g2+0.299*r2
                    lines.append(f"  ROI{i+1}: R{int(r2)} G{int(g2)} Br:{int(br2)}")
        self._roi_txt.setPlainText("\n".join(lines))
        # Blackout events
        ev = []
        for src,evs in [("Screen",self.engine.screen_blackout_events),
                         ("Camera",self.engine.camera_blackout_events)]:
            if evs:
                ev.append(f"── {src} ──")
                for e2 in reversed(evs[-6:]):
                    ev.append(f"  {e2['time']}  변화량:{int(e2['brightness_change'])}")
        self._ev_txt.setPlainText("\n".join(ev))
        # Schedule table colors
        self._refresh_sched_table()

    def _update_fps(self):
        sfps = self.engine.measured_fps(self.engine._screen_fps_ts)
        cfps = self.engine.measured_fps(self.engine._camera_fps_ts)
        self._scr_fps_lbl.setText(f"{sfps:.1f} fps")
        self._cam_fps_lbl.setText(f"{cfps:.1f} fps")
        self._scr_fps_badge.setText(f"FPS: {sfps:.1f}")
        self._cam_fps_det_lbl.setText(f"{self.engine.actual_camera_fps:.2f} fps")

    def _check_segment(self):
        if self.engine.recording and self.engine.current_segment_start:
            if time.time() - self.engine.current_segment_start >= self.engine.segment_duration:
                self._log("Creating new segment…")
                threading.Thread(target=self.engine._create_segment, daemon=True).start()

    # ─────────────────────────────────────────
    #  Schedule
    # ─────────────────────────────────────────
    def _on_schedule_add(self):
        start_dt = stop_dt = None
        if self._sched_start_chk.isChecked():
            qdt = self._sched_start_dt.dateTime()
            start_dt = datetime(qdt.date().year(), qdt.date().month(), qdt.date().day(),
                                qdt.time().hour(), qdt.time().minute(), qdt.time().second())
        if self._sched_stop_chk.isChecked():
            qdt = self._sched_stop_dt.dateTime()
            stop_dt  = datetime(qdt.date().year(), qdt.date().month(), qdt.date().day(),
                                qdt.time().hour(), qdt.time().minute(), qdt.time().second())
        now = datetime.now()
        if start_dt and start_dt < now:
            QMessageBox.warning(self,"오류","시작 시각이 현재보다 이전입니다."); return
        if stop_dt and stop_dt < now:
            QMessageBox.warning(self,"오류","종료 시각이 현재보다 이전입니다."); return
        if start_dt and stop_dt and stop_dt <= start_dt:
            QMessageBox.warning(self,"오류","종료 시각이 시작 시각보다 늦어야 합니다."); return
        if not start_dt and not stop_dt:
            QMessageBox.warning(self,"오류","시작 또는 종료 시각을 설정하세요."); return

        # ── 겹침 검사 ────────────────────────────────────
        # 새 예약의 실효 구간: [new_s, new_e)
        # start_dt만 있으면 stop_dt=무한대, stop_dt만 있으면 start_dt=지금으로 간주
        new_s = start_dt or now
        new_e = stop_dt   # None = 열린 끝
        for ex in self.engine.schedules:
            if ex.done:
                continue
            ex_s = ex.start_dt or now
            ex_e = ex.stop_dt          # None = 열린 끝
            # 두 구간이 겹치는지 확인
            # 겹치지 않는 조건: new_e <= ex_s  OR  ex_e <= new_s
            # 둘 중 하나라도 열린 끝(None)이면 겹칩니다
            no_overlap = (
                (new_e is not None and ex_s is not None and new_e <= ex_s) or
                (ex_e  is not None and new_s is not None and ex_e  <= new_s)
            )
            if not no_overlap:
                QMessageBox.warning(
                    self, "예약 겹침",
                    f"예약 #{ex.id} ({ex.label()}) 와 시간이 겹칩니다.\n"
                    "겹치는 예약은 추가할 수 없습니다.")
                return

        entry = ScheduleEntry(start_dt, stop_dt)
        self.engine.schedules.append(entry)
        self._add_sched_row(entry)
        self._log(f"[Schedule] 예약 추가 #{entry.id}: {entry.label()}")

    def _add_sched_row(self, entry: ScheduleEntry):
        row = self._sched_table.rowCount()
        self._sched_table.insertRow(row)
        self._sched_table.setItem(row, 0, QTableWidgetItem(str(entry.id)))
        s = entry.start_dt.strftime("%m/%d %H:%M:%S") if entry.start_dt else "—"
        e = entry.stop_dt.strftime("%m/%d %H:%M:%S")  if entry.stop_dt  else "—"
        self._sched_table.setItem(row, 1, QTableWidgetItem(s))
        self._sched_table.setItem(row, 2, QTableWidgetItem(e))
        self._sched_table.setItem(row, 3, QTableWidgetItem("대기"))
        for col in range(4):
            it = self._sched_table.item(row, col)
            if it: it.setTextAlignment(Qt.AlignCenter)

    def _refresh_sched_table(self):
        for row in range(self._sched_table.rowCount()):
            it_id = self._sched_table.item(row, 0)
            if not it_id: continue
            sid = int(it_id.text())
            entry = next((s for s in self.engine.schedules if s.id == sid), None)
            if not entry: continue
            st_it = self._sched_table.item(row, 3)
            if st_it:
                if entry.done:
                    st_it.setText("완료"); st_it.setForeground(QColor("#888"))
                elif entry.started:
                    st_it.setText("진행 중"); st_it.setForeground(QColor("#2ecc71"))
                else:
                    st_it.setText("대기"); st_it.setForeground(QColor("#f0c040"))
        # Countdown to next pending
        now = datetime.now()
        pending = [s for s in self.engine.schedules if not s.done]
        if pending:
            nxt = min(pending, key=lambda s: s.start_dt or s.stop_dt or datetime.max)
            ref = nxt.start_dt or nxt.stop_dt
            if ref:
                secs = int((ref - now).total_seconds())
                if secs >= 0:
                    h=secs//3600; m=(secs%3600)//60; s2=secs%60
                    self._sched_cd_lbl.setText(
                        f"#{nxt.id} 까지  {h:02d}h {m:02d}m {s2:02d}s")
                else:
                    self._sched_cd_lbl.setText(f"#{nxt.id} 진행 중…")
        else:
            self._sched_cd_lbl.setText("예약 없음")

    def _on_schedule_delete(self):
        rows = sorted(set(i.row() for i in self._sched_table.selectedItems()), reverse=True)
        for row in rows:
            it_id = self._sched_table.item(row, 0)
            if it_id:
                sid = int(it_id.text())
                self.engine.schedules = [s for s in self.engine.schedules if s.id != sid]
            self._sched_table.removeRow(row)

    def _on_schedule_clear(self):
        self.engine.schedules.clear()
        self._sched_table.setRowCount(0)

    def _tick_schedule(self):
        for action, entry in self.engine.schedule_tick():
            if action == 'start':
                self._log(f"[Schedule] ⏺ 예약 녹화 시작! #{entry.id}")
                self._on_start_rec()
            elif action == 'stop':
                self._log(f"[Schedule] ⏹ 예약 녹화 종료! #{entry.id}")
                self._on_stop_rec()

    # ─────────────────────────────────────────
    #  Feature toggle bar
    # ─────────────────────────────────────────
    def _on_feature_toggle(self, key: str, enabled: bool):
        w = self._sec_map.get(key)
        if w: w.setVisible(enabled)

    def _on_feature_order_changed(self, key_order: list):
        """드래그&드롭 순서 변경 → 패널 내 섹션 위젯을 새 순서로 재배치."""
        pl = self._panel_layout

        # 1. stretch 포함 모든 아이템을 레이아웃에서 제거 (위젯은 숨기지 않음)
        while pl.count():
            item = pl.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        # 2. 새 순서로 위젯 재삽입 (visibility 유지)
        for key in key_order:
            w = self._sec_map.get(key)
            if w:
                w.setParent(self._panel_widget)
                pl.addWidget(w)
                w.show() if self._feat_bar.is_enabled(key) else w.hide()

        pl.addStretch()

    # ─────────────────────────────────────────
    #  Slot handlers
    # ─────────────────────────────────────────
    def _on_scr_toggle(self, checked: bool):
        self._scr_lbl.set_active(checked)
        if checked: self.engine.start_screen_thread()
        else:       self.engine.stop_screen_thread()

    def _on_cam_win_toggle(self, checked: bool):
        if checked: self._cam_win.show(); self._cam_win.raise_()
        else:       self._cam_win.hide()

    def _on_speed_changed(self, val: float):
        self.engine.playback_speed = val
        if val == 1.0:
            desc = "정배속 (1:1 실시간 재생)"
        elif val < 1.0:
            desc = f"{val:.2f}× — 슬로우 모션 (느리게 재생)"
        else:
            desc = f"{val:.2f}× — 빠른 재생 (타임랩스)"
        self._speed_info_lbl.setText(f"  {desc}")

    def _on_start_rec(self):
        self.engine.start_recording()
        self._btn_start.setEnabled(False); self._btn_stop.setEnabled(True)
        self._rec_status_lbl.setText("● RECORDING")
        self._rec_status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;font-size:14px;")
        # 배속 UI 잠금
        self._speed_spin.setEnabled(False)
        self._speed_lock_lbl.setVisible(True)

    def _on_stop_rec(self):
        self.engine.stop_recording()
        self._btn_start.setEnabled(True); self._btn_stop.setEnabled(False)
        self._rec_status_lbl.setText("● STOPPED")
        self._rec_status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;font-size:14px;")
        self._rec_timer_lbl.setText("00:00:00")
        # 배속 UI 잠금 해제
        self._speed_spin.setEnabled(True)
        self._speed_lock_lbl.setVisible(False)

    def _on_scr_rec_toggle(self, checked: bool):
        self.engine.screen_recording_enabled = checked
        if self.engine.recording:
            threading.Thread(target=self.engine._create_segment, daemon=True).start()

    def _on_ac_start(self):
        self.engine.start_auto_click()
        self._btn_ac_start.setEnabled(False); self._btn_ac_stop.setEnabled(True)
        self._ac_status_lbl.setText("● RUNNING")
        self._ac_status_lbl.setStyleSheet("color:#2ecc71;font-weight:bold;")

    def _on_ac_stop(self):
        self.engine.stop_auto_click()
        self._btn_ac_start.setEnabled(True); self._btn_ac_stop.setEnabled(False)
        self._ac_status_lbl.setText("● STOPPED")
        self._ac_status_lbl.setStyleSheet("color:#e74c3c;font-weight:bold;")

    def _on_memo_changed(self):
        self.engine.memo_text = self._memo_edit.toPlainText()

    def _on_blackout(self, source: str, event: dict):
        self._log(f"[BLACKOUT/{source.upper()}] {event['time']}  "
                  f"변화량:{int(event['brightness_change'])}")

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_txt.append(f"[{ts}] {msg}")
        self._status_lbl.setText(msg[:120])

    # ─────────────────────────────────────────
    #  Hotkeys
    # ─────────────────────────────────────────
    def _setup_hotkeys(self):
        if not PYNPUT_AVAILABLE: return
        hk = {
            '<ctrl>+<alt>+w': self._on_start_rec,
            '<ctrl>+<alt>+e': self._on_stop_rec,
            '<ctrl>+<alt>+d': lambda: self._scr_rec_chk.setChecked(
                not self._scr_rec_chk.isChecked()),
            '<ctrl>+<alt>+a': self._on_ac_start,
            '<ctrl>+<alt>+s': self._on_ac_stop,
            '<ctrl>+<alt>+q': self.close,
        }
        self._hkl = pynput_keyboard.GlobalHotKeys(hk)
        self._hkl.start()

    # ─────────────────────────────────────────
    #  Style
    # ─────────────────────────────────────────
    def _dark_style(self):
        return """
        QMainWindow,QWidget{background:#12122a;color:#ddd;}
        QDialog{background:#0d0d1e;color:#ddd;}
        QGroupBox{border:1px solid #2a2a4a;border-radius:6px;
                  margin-top:18px;font-weight:bold;color:#9bc;
                  font-size:12px;padding-top:10px;}
        QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;
                         left:10px;top:-2px;padding:2px 6px;
                         background:#12122a;border-radius:3px;}
        QPushButton{background:#1e2a3a;border:1px solid #336;
                    border-radius:4px;padding:4px 10px;color:#ccd;}
        QPushButton:hover{background:#2a3a4e;}
        QPushButton:pressed{background:#1a2030;}
        QPushButton:disabled{background:#1a1a2e;color:#444;}
        QTextEdit,QPlainTextEdit{background:#0d0d1e;border:1px solid #2a2a4a;color:#ccc;}
        QDoubleSpinBox,QSpinBox,QComboBox{background:#1a1a3a;border:1px solid #336;
            color:#ddd;padding:2px 4px;border-radius:3px;}
        QCheckBox{color:#ccd;spacing:5px;}
        QCheckBox::indicator{width:14px;height:14px;}
        QLabel{color:#ccd;}
        QLCDNumber{background:#0d1520;border:1px solid #336;color:#2ecc71;}
        QTableWidget{selection-background-color:#1a2a4a;}
        QScrollBar:vertical{background:#0d0d1e;width:8px;border-radius:4px;}
        QScrollBar::handle:vertical{background:#336;border-radius:4px;min-height:20px;}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
        QDateTimeEdit{background:#1a1a3a;border:1px solid #336;color:#ddd;
                      padding:2px 4px;border-radius:3px;}
        QDateTimeEdit::drop-down{border:none;}
        """

    def closeEvent(self, e):
        self.engine.stop()
        self._cam_win.hide()
        if PYNPUT_AVAILABLE and hasattr(self,'_hkl'):
            self._hkl.stop()
        e.accept()


# ─────────────────────────────────────────────
#  Entry
# ─────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ScreenCameraRecorder")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()