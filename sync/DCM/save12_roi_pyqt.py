# -*- coding: utf-8 -*-
"""
Screen & Camera Recorder with PyQt5 UI  v3.0
- Display 영상만 메인 창에 표시
- CAM 영상은 별도 독립 창으로 분리
- 각 미리보기 위에 스레드 ON/OFF 토글 버튼
- Recording / Blackout / Auto-Click 을 스크롤 가능한 단일 패널로 통합
"""

import sys
import os
import cv2
import numpy as np
import threading
import time
import queue
import mss
from datetime import datetime
from collections import deque

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGroupBox, QCheckBox, QSpinBox, QDoubleSpinBox,
    QScrollArea, QFrame, QGridLayout, QSlider, QTextEdit,
    QSizePolicy, QDialog, QLCDNumber
)
from PyQt5.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QPoint, QRect
)
from PyQt5.QtGui import QImage, QPixmap, QFont, QColor, QPainter, QPen

try:
    from pynput import keyboard as pynput_keyboard
    from pynput import mouse as pynput_mouse
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    print("[Warning] pynput not available.")


# ─────────────────────────────────────────────
#  Signal Bridge
# ─────────────────────────────────────────────
class Signals(QObject):
    screen_frame_ready = pyqtSignal(np.ndarray)
    camera_frame_ready = pyqtSignal(np.ndarray)
    blackout_detected  = pyqtSignal(str, dict)
    status_message     = pyqtSignal(str)
    fps_updated        = pyqtSignal(float, float)
    auto_click_count   = pyqtSignal(int)


# ─────────────────────────────────────────────
#  Core Recorder Engine
# ─────────────────────────────────────────────
class RecorderEngine:
    def __init__(self, signals: Signals):
        self.signals = signals
        self.running   = False
        self.recording = False
        self.start_time = None
        self.stop_event = threading.Event()

        # Thread on/off flags
        self.screen_thread_enabled = True
        self.camera_thread_enabled = True
        self._screen_thread: threading.Thread = None
        self._camera_thread: threading.Thread = None
        self._screen_stop = threading.Event()
        self._camera_stop = threading.Event()

        # FPS
        self.target_screen_fps = 30.0
        self.actual_camera_fps = 30.0
        self.actual_screen_fps = 30.0

        # Display recording toggle
        self.screen_recording_enabled = True

        # Queues
        self.screen_queue = queue.Queue(maxsize=5)
        self.camera_queue = queue.Queue(maxsize=5)

        # Writers
        self.screen_writer = None
        self.camera_writer = None
        self.output_dir    = ""

        # 30-min segment
        self.segment_duration      = 30 * 60
        self.current_segment_start = None

        # ROIs
        self.screen_rois = []
        self.camera_rois = []

        # ROI averages
        self.screen_roi_avg  = []
        self.camera_roi_avg  = []
        self.screen_roi_prev = []
        self.camera_roi_prev = []
        self.screen_overall_avg = np.zeros(3)
        self.camera_overall_avg = np.zeros(3)

        # Blackout settings
        self.brightness_threshold      = 30.0
        self.blackout_cooldown         = 5.0
        self.screen_last_blackout_time = 0
        self.camera_last_blackout_time = 0
        self.blackout_recording_enabled = True
        self.screen_blackout_count = 0
        self.camera_blackout_count = 0
        self.screen_blackout_events: list = []
        self.camera_blackout_events: list = []

        # Rolling buffer (30 s)
        self.buffer_seconds   = 30
        self._screen_buffer: deque = deque()
        self._camera_buffer: deque = deque()
        self._buf_lock = threading.Lock()

        # Auto-click
        self.auto_click_enabled  = False
        self.auto_click_interval = 1.0
        self.auto_click_count    = 0
        self._auto_click_thread  = None
        self._auto_click_stop    = threading.Event()

        self._writer_lock = threading.Lock()

        # FPS measurement
        self._screen_fps_ts: deque = deque(maxlen=60)
        self._camera_fps_ts: deque = deque(maxlen=60)

    def _measured_fps(self, ts_deque: deque) -> float:
        if len(ts_deque) < 2:
            return 0.0
        span = ts_deque[-1] - ts_deque[0]
        return (len(ts_deque) - 1) / span if span > 0 else 0.0

    def detect_camera_fps(self):
        for idx in [1, 0]:
            cap = cv2.VideoCapture(idx)
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS)
                if fps and fps > 0:
                    self.actual_camera_fps = fps
                else:
                    frames, t0 = 0, time.time()
                    while frames < 30:
                        ret, _ = cap.read()
                        if ret:
                            frames += 1
                    elapsed = time.time() - t0
                    self.actual_camera_fps = frames / elapsed if elapsed > 0 else 30.0
                cap.release()
                self.signals.status_message.emit(
                    f"Camera FPS detected: {self.actual_camera_fps:.2f}")
                return
        self.signals.status_message.emit("Camera not found, using default 30 FPS")

    @property
    def screen_buffer_max(self):
        return max(1, int(self.actual_screen_fps * self.buffer_seconds))

    @property
    def camera_buffer_max(self):
        return max(1, int(self.actual_camera_fps * self.buffer_seconds))

    def calculate_roi_average(self, frame, rois):
        avgs = []
        for (rx, ry, rw, rh) in rois:
            region = frame[ry:ry+rh, rx:rx+rw]
            if region.size > 0:
                avgs.append(region.mean(axis=0).mean(axis=0))
            else:
                avgs.append(np.zeros(3))
        return avgs

    def detect_blackout(self, curr_list, prev_list, source: str) -> bool:
        if not curr_list or not prev_list or len(curr_list) != len(prev_list):
            return False
        changes, cur_br = [], []
        for c, p in zip(curr_list, prev_list):
            if np.all(p == 0):
                continue
            cb = 0.114*c[0] + 0.587*c[1] + 0.299*c[2]
            pb = 0.114*p[0] + 0.587*p[1] + 0.299*p[2]
            changes.append(pb - cb)
            cur_br.append(cb)
        if not changes:
            return False
        mean_change = float(np.mean(changes))
        if mean_change < self.brightness_threshold:
            return False
        now  = time.time()
        last = self.screen_last_blackout_time if source == "screen" \
               else self.camera_last_blackout_time
        if now - last < self.blackout_cooldown:
            return False
        if source == "screen":
            self.screen_last_blackout_time = now
            self.screen_blackout_count += 1
        else:
            self.camera_last_blackout_time = now
            self.camera_blackout_count += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        event = {
            'time': datetime.now().strftime("%H:%M:%S.%f")[:-3],
            'brightness_change': mean_change,
            'timestamp': ts
        }
        evlist = self.screen_blackout_events if source == "screen" \
                 else self.camera_blackout_events
        evlist.append(event)
        if len(evlist) > 50:
            evlist.pop(0)
        self.signals.blackout_detected.emit(source, event)
        if self.blackout_recording_enabled:
            threading.Thread(
                target=self.save_blackout_clip,
                args=(source, ts), daemon=True).start()
        return True

    def save_blackout_clip(self, source: str, timestamp: str):
        desktop = os.path.expanduser("~/Desktop")
        src_dir = os.path.join(desktop, "BLACK_OUT", source.upper())
        os.makedirs(src_dir, exist_ok=True)
        fps = self.actual_screen_fps if source == "screen" else self.actual_camera_fps
        frames_before = int(fps * 10)
        frames_after  = int(fps * 10)
        with self._buf_lock:
            buf = self._screen_buffer if source == "screen" else self._camera_buffer
            pre_frames = list(buf)
        post_frames: list = []
        deadline = time.time() + 11.0
        while len(post_frames) < frames_after and time.time() < deadline:
            time.sleep(0.04)
            with self._buf_lock:
                buf = self._screen_buffer if source == "screen" else self._camera_buffer
                if len(buf) > len(pre_frames):
                    post_frames = list(buf)[len(pre_frames):]
        pre_clip  = pre_frames[-frames_before:] if len(pre_frames) >= frames_before else pre_frames
        post_clip = post_frames[:frames_after]
        all_frames = pre_clip + post_clip
        if not all_frames:
            self.signals.status_message.emit(f"[Blackout] {source} – no frames to save")
            return
        blackout_idx  = len(pre_clip)
        blackout_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        h, w = all_frames[0].shape[:2]
        video_path   = os.path.join(src_dir, f"blackout_{timestamp}.mp4")
        capture_path = os.path.join(src_dir, f"capture_{timestamp}.jpg")
        writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
        for i, frame in enumerate(all_frames):
            f = frame.copy()
            # blackout 마커 (타임스탬프는 버퍼 프레임에 이미 포함)
            if i == blackout_idx:
                cv2.rectangle(f, (4, 4), (w-4, h-4), (0, 0, 255), 6)
                cv2.putText(f, f"▼ BLACKOUT  {blackout_time}", (10, 100),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 255), 2,
                            cv2.LINE_AA)
            elif i > blackout_idx:
                rel = (i - blackout_idx) / fps
                cv2.putText(f, f"+{rel:.1f}s", (10, 100),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 0), 2,
                            cv2.LINE_AA)
            else:
                rel = (i - blackout_idx) / fps
                cv2.putText(f, f"{rel:.1f}s", (10, 100),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1,
                            cv2.LINE_AA)
            writer.write(f)
        writer.release()
        cap_f = all_frames[min(blackout_idx, len(all_frames)-1)].copy()
        cv2.putText(cap_f, f"BLACKOUT: {blackout_time}", (10, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        cv2.imwrite(capture_path, cap_f)
        duration = len(all_frames) / fps
        self.signals.status_message.emit(
            f"[Blackout/{source}] Saved {duration:.1f}s clip → {video_path}")

    # ── Screen capture thread ─────────────────
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

                # ROI 분석은 원본 프레임으로
                if self.screen_rois:
                    avgs = self.calculate_roi_average(frame, self.screen_rois)
                    self.screen_roi_avg     = avgs
                    self.screen_overall_avg = np.mean(avgs, axis=0) if avgs else np.zeros(3)
                    if self.screen_roi_prev:
                        self.detect_blackout(avgs, self.screen_roi_prev, "screen")
                    self.screen_roi_prev = [a.copy() for a in avgs]

                # 저장/버퍼용: 녹화 중이면 타임스탬프 오버레이 포함
                if self.recording and self.start_time:
                    stamped = self._add_overlay(frame.copy(), self.screen_rois)
                else:
                    stamped = frame

                # 버퍼 (blackout 클립용) — 오버레이 포함 프레임 보관
                with self._buf_lock:
                    self._screen_buffer.append(stamped.copy())
                    while len(self._screen_buffer) > self.screen_buffer_max:
                        self._screen_buffer.popleft()

                # 파일 기록
                if self.recording and self.screen_writer and self.screen_recording_enabled:
                    with self._writer_lock:
                        self.screen_writer.write(stamped)

                try:
                    self.screen_queue.put_nowait(frame)   # 미리보기엔 클린 프레임
                except queue.Full:
                    pass

    # ── Camera capture thread ─────────────────
    def _camera_loop(self):
        cap = None
        for idx in [1, 0]:
            cap = cv2.VideoCapture(idx)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FPS, self.actual_camera_fps)
                break
        if cap is None or not cap.isOpened():
            self.signals.status_message.emit("ERROR: Cannot open camera")
            return
        interval = 1.0 / self.actual_camera_fps
        next_t   = time.perf_counter()
        while not self._camera_stop.is_set():
            now = time.perf_counter()
            if now < next_t:
                time.sleep(next_t - now)
            next_t += interval
            ret, frame = cap.read()
            if not ret:
                continue
            self._camera_fps_ts.append(time.time())

            # ROI 분석은 원본 프레임으로
            if self.camera_rois:
                avgs = self.calculate_roi_average(frame, self.camera_rois)
                self.camera_roi_avg     = avgs
                self.camera_overall_avg = np.mean(avgs, axis=0) if avgs else np.zeros(3)
                if self.camera_roi_prev:
                    self.detect_blackout(avgs, self.camera_roi_prev, "camera")
                self.camera_roi_prev = [a.copy() for a in avgs]

            # 저장/버퍼용: 녹화 중이면 타임스탬프 오버레이 포함
            if self.recording and self.start_time:
                stamped = self._add_overlay(frame.copy(), self.camera_rois)
            else:
                stamped = frame

            # 버퍼 (blackout 클립용) — 오버레이 포함 프레임 보관
            with self._buf_lock:
                self._camera_buffer.append(stamped.copy())
                while len(self._camera_buffer) > self.camera_buffer_max:
                    self._camera_buffer.popleft()

            # 파일 기록
            if self.recording and self.camera_writer:
                with self._writer_lock:
                    self.camera_writer.write(stamped)

            try:
                self.camera_queue.put_nowait(frame)   # 미리보기엔 클린 프레임
            except queue.Full:
                pass
        cap.release()

    def _add_overlay(self, frame, rois):
        """녹화 파일에 저장될 오버레이.
        녹화 중일 때만 날짜/시간(밀리초 포함)과 경과 시간을 왼쪽 상단에 표시.
        """
        if self.recording and self.start_time:
            now    = datetime.now()
            now_str = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
            e  = time.time() - self.start_time
            hh = int(e // 3600); mm = int((e % 3600) // 60)
            ss = int(e % 60);    ms = int((e % 1) * 1000)
            elapsed_str = f"REC  {hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"

            # 반투명 배경 박스
            overlay = frame.copy()
            cv2.rectangle(overlay, (4, 4), (430, 78), (0, 0, 0), -1)
            cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)

            cv2.putText(frame, now_str,    (10, 32),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 255, 80), 2,
                        cv2.LINE_AA)
            cv2.putText(frame, elapsed_str, (10, 68),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80, 220, 255), 2,
                        cv2.LINE_AA)

        for i, (rx, ry, rw, rh) in enumerate(rois):
            cv2.rectangle(frame, (rx, ry), (rx+rw, ry+rh), (0, 0, 255), 2)
            cv2.putText(frame, f"ROI{i+1}", (rx, max(ry-5, 15)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1,
                        cv2.LINE_AA)
        return frame

    @staticmethod
    def _stamp_preview(frame: np.ndarray, recording: bool,
                        start_time: float) -> np.ndarray:
        """UI 미리보기 전용 타임스탬프 합성 (원본 프레임 복사본에 적용)."""
        if not recording or start_time is None:
            return frame
        out    = frame.copy()
        now    = datetime.now()
        now_str = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        e  = time.time() - start_time
        hh = int(e // 3600); mm = int((e % 3600) // 60)
        ss = int(e % 60);    ms = int((e % 1) * 1000)
        elapsed_str = f"REC  {hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"

        overlay = out.copy()
        cv2.rectangle(overlay, (4, 4), (430, 78), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.45, out, 0.55, 0, out)

        cv2.putText(out, now_str,    (10, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 255, 80), 2, cv2.LINE_AA)
        cv2.putText(out, elapsed_str, (10, 68),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80, 220, 255), 2, cv2.LINE_AA)
        return out

    def _create_segment(self):
        with self._writer_lock:
            if self.screen_writer:
                self.screen_writer.release()
            if self.camera_writer:
                self.camera_writer.release()
        seg_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.screen_recording_enabled:
            with mss.mss() as sct:
                mon_idx = 2 if len(sct.monitors) > 2 else 1
                mon     = sct.monitors[mon_idx]
                spath   = os.path.join(self.output_dir, f"screen_{seg_ts}.mp4")
                with self._writer_lock:
                    self.screen_writer = cv2.VideoWriter(
                        spath, cv2.VideoWriter_fourcc(*'mp4v'),
                        self.actual_screen_fps, (mon['width'], mon['height']))
            self.signals.status_message.emit(f"New screen segment: {spath}")
        cframe = None
        with self._buf_lock:
            if self._camera_buffer:
                cframe = self._camera_buffer[-1]
        if cframe is not None:
            h, w = cframe.shape[:2]
            cpath = os.path.join(self.output_dir, f"camera_{seg_ts}.mp4")
            with self._writer_lock:
                self.camera_writer = cv2.VideoWriter(
                    cpath, cv2.VideoWriter_fourcc(*'mp4v'),
                    self.actual_camera_fps, (w, h))
            self.signals.status_message.emit(f"New camera segment: {cpath}")
        self.current_segment_start = time.time()

    # ── Thread ON/OFF ─────────────────────────
    def start_screen_thread(self):
        if self._screen_thread and self._screen_thread.is_alive():
            return
        self._screen_stop.clear()
        self._screen_thread = threading.Thread(
            target=self._screen_loop, daemon=True)
        self._screen_thread.start()
        self.signals.status_message.emit("Screen capture thread started")

    def stop_screen_thread(self):
        self._screen_stop.set()
        self.signals.status_message.emit("Screen capture thread stopped")

    def start_camera_thread(self):
        if self._camera_thread and self._camera_thread.is_alive():
            return
        self._camera_stop.clear()
        self._camera_thread = threading.Thread(
            target=self._camera_loop, daemon=True)
        self._camera_thread.start()
        self.signals.status_message.emit("Camera capture thread started")

    def stop_camera_thread(self):
        self._camera_stop.set()
        self.signals.status_message.emit("Camera capture thread stopped")

    # ── Recording control ─────────────────────
    def start_recording(self):
        if self.recording:
            return
        desktop = os.path.expanduser("~/Desktop")
        self.output_dir = os.path.join(
            desktop, datetime.now().strftime("Rec_%Y%m%d_%H%M%S"))
        os.makedirs(self.output_dir, exist_ok=True)
        self._create_segment()
        self.start_time = time.time()
        self.recording  = True
        self.signals.status_message.emit(f"Recording started → {self.output_dir}")

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        time.sleep(0.3)
        with self._writer_lock:
            if self.screen_writer:
                self.screen_writer.release()
                self.screen_writer = None
            if self.camera_writer:
                self.camera_writer.release()
                self.camera_writer = None
        self.signals.status_message.emit("Recording stopped")

    # ── Auto-click ────────────────────────────
    def _auto_click_loop(self):
        if not PYNPUT_AVAILABLE:
            return
        mc = pynput_mouse.Controller()
        while not self._auto_click_stop.is_set():
            mc.click(pynput_mouse.Button.left)
            self.auto_click_count += 1
            self.signals.auto_click_count.emit(self.auto_click_count)
            self._auto_click_stop.wait(self.auto_click_interval)

    def start_auto_click(self):
        if self.auto_click_enabled:
            return
        self.auto_click_enabled = True
        self._auto_click_stop.clear()
        self._auto_click_thread = threading.Thread(
            target=self._auto_click_loop, daemon=True)
        self._auto_click_thread.start()

    def stop_auto_click(self):
        self.auto_click_enabled = False
        self._auto_click_stop.set()

    def reset_auto_click_count(self):
        self.auto_click_count = 0
        self.signals.auto_click_count.emit(0)

    # ── Engine start / stop ───────────────────
    def start(self):
        self.running = True
        self._screen_stop.clear()
        self._camera_stop.clear()
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
#  ROI Preview Label
# ─────────────────────────────────────────────
class PreviewLabel(QLabel):
    roi_changed = pyqtSignal()

    def __init__(self, source: str, engine: RecorderEngine, parent=None):
        super().__init__(parent)
        self.source    = source
        self.engine    = engine
        self._drawing  = False
        self._pt1      = QPoint()
        self._pt2      = QPoint()
        self._raw_size = (1, 1)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background: #0d0d1e; border: 1px solid #334;")
        self.setCursor(Qt.CrossCursor)

        # Placeholder text
        self._thread_active = True
        self._show_placeholder()

    def _show_placeholder(self):
        self.setText("⏸ Thread Paused")
        self.setStyleSheet(
            "background:#0d0d1e; border:1px solid #334; "
            "color:#555; font-size:18px; font-weight:bold;")

    def set_thread_active(self, active: bool):
        self._thread_active = active
        if not active:
            self._show_placeholder()
        else:
            self.setStyleSheet(
                "background:#0d0d1e; border:1px solid #334;")
            self.setText("")

    def _rois(self):
        return self.engine.screen_rois if self.source == "screen" \
               else self.engine.camera_rois

    def _scale(self):
        pw, ph = self.width(), self.height()
        rw, rh = self._raw_size
        scale = min(pw / rw, ph / rh)
        return scale, scale, (pw - rw*scale)/2, (ph - rh*scale)/2

    def _label_to_raw(self, qp: QPoint):
        sx, sy, ox, oy = self._scale()
        return int((qp.x() - ox) / sx), int((qp.y() - oy) / sy)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drawing = True
            self._pt1 = e.pos()
            self._pt2 = e.pos()
        elif e.button() == Qt.RightButton:
            rois = self._rois()
            if rois:
                rois.pop()
                self.roi_changed.emit()

    def mouseMoveEvent(self, e):
        if self._drawing:
            self._pt2 = e.pos()
            self.update()

    def mouseReleaseEvent(self, e):
        if self._drawing and e.button() == Qt.LeftButton:
            self._drawing = False
            x1, y1 = self._label_to_raw(self._pt1)
            x2, y2 = self._label_to_raw(self._pt2)
            rx, ry = min(x1,x2), min(y1,y2)
            rw, rh = abs(x1-x2), abs(y1-y2)
            if rw > 5 and rh > 5:
                rois = self._rois()
                if len(rois) < 10:
                    rois.append((rx, ry, rw, rh))
                self.roi_changed.emit()
            self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drawing:
            p = QPainter(self)
            p.setPen(QPen(QColor(255, 80, 80), 2, Qt.DashLine))
            p.drawRect(QRect(self._pt1, self._pt2).normalized())

    def update_frame(self, frame: np.ndarray):
        if not self._thread_active:
            return
        self._raw_size = (frame.shape[1], frame.shape[0])
        rgb  = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        disp = rgb.copy()
        for i, (rx, ry, rw, rh) in enumerate(self._rois()):
            cv2.rectangle(disp, (rx, ry), (rx+rw, ry+rh), (255, 60, 60), 2)
            cv2.putText(disp, f"ROI{i+1}", (rx, max(ry-4, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 60, 60), 1)
        h, w, _ = disp.shape
        qimg = QImage(disp.data, w, h, 3*w, QImage.Format_RGB888)
        pix  = QPixmap.fromImage(qimg).scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setPixmap(pix)


# ─────────────────────────────────────────────
#  Thread Toggle Button (pill style)
# ─────────────────────────────────────────────
class ThreadToggleBtn(QPushButton):
    def __init__(self, label_on="▶ Thread ON", label_off="⏸ Thread OFF", parent=None):
        super().__init__(parent)
        self._label_on  = label_on
        self._label_off = label_off
        self._active    = True
        self._update_style()
        self.setFixedHeight(28)
        self.setCheckable(True)
        self.setChecked(True)
        self.toggled.connect(self._on_toggled)

    def _on_toggled(self, checked):
        self._active = checked
        self._update_style()

    def _update_style(self):
        if self._active:
            self.setText(self._label_on)
            self.setStyleSheet("""
                QPushButton {
                    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                        stop:0 #1a6b3a, stop:1 #27ae60);
                    color: #eaffea; border: none; border-radius: 14px;
                    font-size: 11px; font-weight: bold; padding: 0 12px;
                }
                QPushButton:hover { background: #2ecc71; }
            """)
        else:
            self.setText(self._label_off)
            self.setStyleSheet("""
                QPushButton {
                    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                        stop:0 #3a1a1a, stop:1 #7f3030);
                    color: #ffcccc; border: none; border-radius: 14px;
                    font-size: 11px; font-weight: bold; padding: 0 12px;
                }
                QPushButton:hover { background: #c0392b; }
            """)


# ─────────────────────────────────────────────
#  Cam 전용 독립 창
# ─────────────────────────────────────────────
class CameraWindow(QDialog):
    def __init__(self, engine: RecorderEngine, signals: Signals, parent=None):
        super().__init__(parent)
        self.engine  = engine
        self.signals = signals
        self.setWindowTitle("📷  Camera Feed")
        self.setWindowFlags(Qt.Window | Qt.WindowMinimizeButtonHint |
                            Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
        self.resize(640, 420)
        self.setStyleSheet("background:#0d0d1e;")

        v = QVBoxLayout(self)
        v.setSpacing(4)
        v.setContentsMargins(6, 6, 6, 6)

        # Header bar
        hdr = QHBoxLayout()
        title = QLabel("📷  Camera Preview")
        title.setStyleSheet("color:#9ab; font-weight:bold; font-size:13px;")
        hdr.addWidget(title)
        hdr.addStretch()

        self._cam_fps_lbl = QLabel("FPS: —")
        self._cam_fps_lbl.setStyleSheet("color:#888; font-size:11px;")
        hdr.addWidget(self._cam_fps_lbl)

        self._toggle_btn = ThreadToggleBtn("▶ Thread ON", "⏸ Thread OFF")
        self._toggle_btn.toggled.connect(self._on_thread_toggle)
        hdr.addWidget(self._toggle_btn)

        v.addLayout(hdr)

        # Preview
        self._cam_lbl = PreviewLabel("camera", self.engine)
        hint = QLabel("Left-drag: add ROI  |  Right-click: remove ROI")
        hint.setStyleSheet("color:#555; font-size:10px;")
        hint.setAlignment(Qt.AlignCenter)

        v.addWidget(self._cam_lbl, 1)
        v.addWidget(hint)

        self.signals.camera_frame_ready.connect(self._cam_lbl.update_frame)

        # FPS refresh
        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._update_fps)
        self._fps_timer.start(2000)

    def _on_thread_toggle(self, checked: bool):
        self._cam_lbl.set_thread_active(checked)
        if checked:
            self.engine.start_camera_thread()
        else:
            self.engine.stop_camera_thread()

    def _update_fps(self):
        cfps = self.engine._measured_fps(self.engine._camera_fps_ts)
        self._cam_fps_lbl.setText(f"FPS: {cfps:.1f}")

    def closeEvent(self, e):
        # Don't actually close — just hide so it can be re-shown
        e.ignore()
        self.hide()

    def get_preview_label(self):
        return self._cam_lbl


# ─────────────────────────────────────────────
#  Section Header (collapsible-style divider)
# ─────────────────────────────────────────────
def _make_section_header(title: str) -> QLabel:
    lbl = QLabel(title)
    lbl.setStyleSheet("""
        QLabel {
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #1e2240, stop:1 #12122a);
            color: #7ab4d4;
            font-size: 12px;
            font-weight: bold;
            padding: 6px 10px;
            border-left: 3px solid #3a7bd5;
            border-radius: 2px;
            margin-top: 8px;
        }
    """)
    return lbl


# ─────────────────────────────────────────────
#  Main Window
# ─────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen & Camera Recorder  v3.0")
        self.resize(1300, 820)
        self.setStyleSheet(self._dark_style())

        self.signals = Signals()
        self.engine  = RecorderEngine(self.signals)

        # Camera window (separate)
        self._cam_win = CameraWindow(self.engine, self.signals, self)

        self._build_ui()
        self._connect_signals()

        # Timers
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start(500)

        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._update_fps_display)
        self._fps_timer.start(2000)

        self._seg_timer = QTimer(self)
        self._seg_timer.timeout.connect(self._check_segment)
        self._seg_timer.start(5000)

        self._setup_hotkeys()
        self.engine.start()

        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._pump_preview)
        self._preview_timer.start(33)

    # ─────────────────────────────────────────
    #  UI Build
    # ─────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(8, 8, 8, 8)

        # ── Left: Screen preview only ──────────────────
        left = QVBoxLayout()
        left.setSpacing(6)

        # Screen header bar
        scr_hdr = QHBoxLayout()
        scr_title = QLabel("🖥  Screen Preview")
        scr_title.setStyleSheet("color:#9ab; font-weight:bold; font-size:13px;")
        scr_hdr.addWidget(scr_title)
        scr_hdr.addStretch()

        self._screen_fps_badge = QLabel("FPS: —")
        self._screen_fps_badge.setStyleSheet("color:#888; font-size:11px;")
        scr_hdr.addWidget(self._screen_fps_badge)

        # Screen thread toggle
        self._scr_toggle = ThreadToggleBtn("▶ Thread ON", "⏸ Thread OFF")
        self._scr_toggle.toggled.connect(self._on_screen_thread_toggle)
        scr_hdr.addWidget(self._scr_toggle)

        # Camera window toggle button
        self._cam_win_btn = QPushButton("📷 Camera Window")
        self._cam_win_btn.setStyleSheet("""
            QPushButton {
                background: #1a2a3a; color: #7bc8e0; border: 1px solid #2a4a6a;
                border-radius: 14px; font-size: 11px; padding: 3px 12px;
            }
            QPushButton:hover { background: #223344; }
            QPushButton:checked { background: #1a4060; }
        """)
        self._cam_win_btn.setCheckable(True)
        self._cam_win_btn.toggled.connect(self._on_cam_win_toggle)
        scr_hdr.addWidget(self._cam_win_btn)

        # Screen preview
        scr_frame = QFrame()
        scr_frame.setStyleSheet(
            "QFrame { border: 1px solid #334; border-radius: 6px; background: #0d0d1e; }")
        scr_lay = QVBoxLayout(scr_frame)
        scr_lay.setContentsMargins(4, 4, 4, 4)
        scr_lay.setSpacing(3)
        scr_lay.addLayout(scr_hdr)

        self._screen_lbl = PreviewLabel("screen", self.engine)
        scr_lay.addWidget(self._screen_lbl, 1)

        scr_hint = QLabel("Left-drag: add ROI  |  Right-click: remove ROI")
        scr_hint.setStyleSheet("color:#555; font-size:10px;")
        scr_hint.setAlignment(Qt.AlignCenter)
        scr_lay.addWidget(scr_hint)

        left.addWidget(scr_frame, 1)

        # Status bar
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(
            "color:#888; font-size:11px; padding:2px 4px;"
            "border-top:1px solid #334;")
        left.addWidget(self._status_lbl)

        # ── Right: Scrollable control panel ─────────────
        right_container = QWidget()
        right_container.setFixedWidth(370)
        right_v = QVBoxLayout(right_container)
        right_v.setContentsMargins(0, 0, 0, 0)
        right_v.setSpacing(0)

        panel_title = QLabel("⚙  Control Panel")
        panel_title.setStyleSheet(
            "color:#ccc; font-size:13px; font-weight:bold; "
            "padding:8px 10px; background:#1a1a3a; "
            "border-bottom:1px solid #334;")
        right_v.addWidget(panel_title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical {
                background: #0d0d1e; width: 8px; border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #336; border-radius: 4px; min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        panel_widget = QWidget()
        panel_widget.setStyleSheet("background: #12122a;")
        panel_layout = QVBoxLayout(panel_widget)
        panel_layout.setContentsMargins(8, 6, 8, 12)
        panel_layout.setSpacing(6)

        # ── ① Recording Section ───────────────
        panel_layout.addWidget(_make_section_header("⏺  Recording"))
        panel_layout.addWidget(self._build_status_group())
        panel_layout.addWidget(self._build_rec_buttons_group())
        panel_layout.addWidget(self._build_screen_rec_group())
        panel_layout.addWidget(self._build_fps_group())

        # ── ② Blackout Section ────────────────
        panel_layout.addWidget(_make_section_header("⚡  Blackout Detection"))
        panel_layout.addWidget(self._build_blackout_group())

        # ── ③ Auto-Click Section ──────────────
        panel_layout.addWidget(_make_section_header("🖱  Auto-Click"))
        panel_layout.addWidget(self._build_autoclick_group())

        # ── ④ Log Section ─────────────────────
        panel_layout.addWidget(_make_section_header("📋  Log"))
        panel_layout.addWidget(self._build_log_group())

        panel_layout.addStretch()

        scroll.setWidget(panel_widget)
        right_v.addWidget(scroll, 1)

        root.addLayout(left, 1)
        root.addWidget(right_container, 0)

    # ─────────────────────────────────────────
    #  Control Panel Sub-Widgets
    # ─────────────────────────────────────────
    def _build_status_group(self):
        grp = QGroupBox("Status")
        g   = QGridLayout(grp)
        g.setSpacing(6)

        self._rec_status_lbl = QLabel("● STOPPED")
        self._rec_status_lbl.setStyleSheet(
            "color:#e74c3c; font-weight:bold; font-size:14px;")
        g.addWidget(self._rec_status_lbl, 0, 0, 1, 2)

        self._rec_timer_lbl = QLabel("00:00:00")
        self._rec_timer_lbl.setStyleSheet(
            "font-size:26px; font-weight:bold; color:#2ecc71; font-family:monospace;")
        g.addWidget(self._rec_timer_lbl, 1, 0, 1, 2, Qt.AlignCenter)

        g.addWidget(QLabel("Screen FPS:"), 2, 0)
        self._screen_fps_lbl = QLabel("—")
        g.addWidget(self._screen_fps_lbl, 2, 1)

        g.addWidget(QLabel("Camera FPS:"), 3, 0)
        self._camera_fps_lbl = QLabel("—")
        g.addWidget(self._camera_fps_lbl, 3, 1)
        return grp

    def _build_rec_buttons_group(self):
        grp = QGroupBox("Controls")
        bg  = QVBoxLayout(grp)
        bg.setSpacing(6)

        self._btn_start = QPushButton("⏺  Start Recording  [Ctrl+Alt+W]")
        self._btn_start.setStyleSheet(
            "background:#27ae60; color:white; font-size:12px; "
            "padding:7px; border-radius:5px; border:none;")
        self._btn_start.clicked.connect(self._on_start_rec)

        self._btn_stop = QPushButton("⏹  Stop Recording  [Ctrl+Alt+E]")
        self._btn_stop.setStyleSheet(
            "background:#c0392b; color:white; font-size:12px; "
            "padding:7px; border-radius:5px; border:none;")
        self._btn_stop.clicked.connect(self._on_stop_rec)
        self._btn_stop.setEnabled(False)

        bg.addWidget(self._btn_start)
        bg.addWidget(self._btn_stop)
        return grp

    def _build_screen_rec_group(self):
        grp = QGroupBox("Screen Recording")
        sl  = QVBoxLayout(grp)

        self._screen_rec_chk = QCheckBox(
            "Enable screen recording  [Ctrl+Alt+D]")
        self._screen_rec_chk.setChecked(True)
        self._screen_rec_chk.toggled.connect(self._on_screen_rec_toggle)
        sl.addWidget(self._screen_rec_chk)

        tip = QLabel("Disabling reduces CPU / disk load. Camera always active.")
        tip.setStyleSheet("color:#666; font-size:10px;")
        tip.setWordWrap(True)
        sl.addWidget(tip)
        return grp

    def _build_fps_group(self):
        grp = QGroupBox("FPS Settings")
        fl  = QGridLayout(grp)

        fl.addWidget(QLabel("Target Screen FPS:"), 0, 0)
        self._screen_fps_spin = QDoubleSpinBox()
        self._screen_fps_spin.setRange(1, 120)
        self._screen_fps_spin.setValue(30.0)
        self._screen_fps_spin.setSingleStep(1.0)
        self._screen_fps_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'actual_screen_fps', v))
        fl.addWidget(self._screen_fps_spin, 0, 1)

        fl.addWidget(QLabel("Detected Camera FPS:"), 1, 0)
        self._cam_fps_detected_lbl = QLabel("—")
        fl.addWidget(self._cam_fps_detected_lbl, 1, 1)
        return grp

    def _build_blackout_group(self):
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        # Toggle
        self._blackout_rec_chk = QCheckBox("Enable Blackout Clip Recording")
        self._blackout_rec_chk.setChecked(True)
        self._blackout_rec_chk.toggled.connect(
            lambda c: setattr(self.engine, 'blackout_recording_enabled', c))
        v.addWidget(self._blackout_rec_chk)

        # Threshold group
        th_grp = QGroupBox("Detection Threshold")
        tl = QGridLayout(th_grp)
        tl.addWidget(QLabel("Brightness drop:"), 0, 0)
        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(5, 200)
        self._threshold_spin.setValue(30.0)
        self._threshold_spin.setSuffix("  (0–255)")
        self._threshold_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'brightness_threshold', v))
        tl.addWidget(self._threshold_spin, 0, 1)

        tl.addWidget(QLabel("Cooldown (s):"), 1, 0)
        self._cooldown_spin = QDoubleSpinBox()
        self._cooldown_spin.setRange(0.5, 60)
        self._cooldown_spin.setValue(5.0)
        self._cooldown_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'blackout_cooldown', v))
        tl.addWidget(self._cooldown_spin, 1, 1)
        v.addWidget(th_grp)

        # Counts
        cnt_grp = QGroupBox("Detection Counts")
        cl = QGridLayout(cnt_grp)
        cl.addWidget(QLabel("Screen blackouts:"), 0, 0)
        self._scr_bo_lbl = QLabel("0")
        self._scr_bo_lbl.setStyleSheet("font-weight:bold; color:#e74c3c;")
        cl.addWidget(self._scr_bo_lbl, 0, 1)
        cl.addWidget(QLabel("Camera blackouts:"), 1, 0)
        self._cam_bo_lbl = QLabel("0")
        self._cam_bo_lbl.setStyleSheet("font-weight:bold; color:#e74c3c;")
        cl.addWidget(self._cam_bo_lbl, 1, 1)
        v.addWidget(cnt_grp)

        # ROI live brightness
        roi_grp = QGroupBox("ROI Brightness (live)")
        rl = QVBoxLayout(roi_grp)
        self._roi_text = QTextEdit()
        self._roi_text.setReadOnly(True)
        self._roi_text.setFixedHeight(120)
        self._roi_text.setStyleSheet(
            "font-size:10px; font-family:monospace; background:#0d0d1e;")
        rl.addWidget(self._roi_text)
        v.addWidget(roi_grp)

        # Recent events
        ev_grp = QGroupBox("Recent Blackout Events")
        el = QVBoxLayout(ev_grp)
        self._bo_events_text = QTextEdit()
        self._bo_events_text.setReadOnly(True)
        self._bo_events_text.setFixedHeight(100)
        self._bo_events_text.setStyleSheet(
            "font-size:10px; font-family:monospace; background:#0d0d1e;")
        el.addWidget(self._bo_events_text)
        v.addWidget(ev_grp)

        return container

    def _build_autoclick_group(self):
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        info = QLabel(
            "Auto-clicker sends left mouse clicks at the current cursor position.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#888; font-size:10px;")
        v.addWidget(info)

        # Interval
        int_grp = QGroupBox("Click Interval")
        il = QGridLayout(int_grp)
        il.addWidget(QLabel("Interval (s):"), 0, 0)
        self._click_interval_spin = QDoubleSpinBox()
        self._click_interval_spin.setRange(0.1, 3600)
        self._click_interval_spin.setValue(1.0)
        self._click_interval_spin.setSingleStep(0.1)
        self._click_interval_spin.valueChanged.connect(
            lambda v: setattr(self.engine, 'auto_click_interval', v))
        il.addWidget(self._click_interval_spin, 0, 1)

        preset_row = QHBoxLayout()
        for label, val in [("0.1s", 0.1), ("0.5s", 0.5),
                            ("1s", 1.0), ("5s", 5.0), ("10s", 10.0)]:
            btn = QPushButton(label)
            btn.setFixedWidth(44)
            btn.setFixedHeight(22)
            btn.clicked.connect(
                lambda _, v=val: self._click_interval_spin.setValue(v))
            preset_row.addWidget(btn)
        il.addLayout(preset_row, 1, 0, 1, 2)
        v.addWidget(int_grp)

        # Counter
        cnt_grp = QGroupBox("Click Counter")
        cl = QGridLayout(cnt_grp)
        self._click_count_lbl = QLCDNumber(8)
        self._click_count_lbl.setSegmentStyle(QLCDNumber.Flat)
        self._click_count_lbl.setStyleSheet("color:#2ecc71;")
        self._click_count_lbl.setFixedHeight(44)
        cl.addWidget(self._click_count_lbl, 0, 0, 1, 2)
        btn_reset = QPushButton("Reset Counter")
        btn_reset.clicked.connect(self.engine.reset_auto_click_count)
        cl.addWidget(btn_reset, 1, 0, 1, 2)
        v.addWidget(cnt_grp)

        # Start / Stop
        ctrl_grp = QGroupBox("Control")
        ctl = QVBoxLayout(ctrl_grp)

        self._btn_ac_start = QPushButton("▶  Start Auto-Click  [Ctrl+Alt+A]")
        self._btn_ac_start.setStyleSheet(
            "background:#2980b9; color:white; font-size:12px; "
            "padding:7px; border-radius:5px; border:none;")
        self._btn_ac_start.clicked.connect(self._on_ac_start)

        self._btn_ac_stop = QPushButton("■  Stop Auto-Click  [Ctrl+Alt+S]")
        self._btn_ac_stop.setStyleSheet(
            "background:#7f8c8d; color:white; font-size:12px; "
            "padding:7px; border-radius:5px; border:none;")
        self._btn_ac_stop.clicked.connect(self._on_ac_stop)
        self._btn_ac_stop.setEnabled(False)

        self._ac_status_lbl = QLabel("● STOPPED")
        self._ac_status_lbl.setStyleSheet("color:#e74c3c; font-weight:bold;")

        ctl.addWidget(self._btn_ac_start)
        ctl.addWidget(self._btn_ac_stop)
        ctl.addWidget(self._ac_status_lbl)
        v.addWidget(ctrl_grp)

        return container

    def _build_log_group(self):
        grp = QGroupBox("System Log")
        v = QVBoxLayout(grp)
        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setFixedHeight(180)
        self._log_text.setStyleSheet(
            "font-family:monospace; font-size:10px; "
            "background:#080810; color:#aaa;")
        btn_clear = QPushButton("Clear Log")
        btn_clear.setFixedHeight(24)
        btn_clear.clicked.connect(self._log_text.clear)
        v.addWidget(self._log_text)
        v.addWidget(btn_clear)
        return grp

    # ─────────────────────────────────────────
    #  Signal connections
    # ─────────────────────────────────────────
    def _connect_signals(self):
        self.signals.screen_frame_ready.connect(self._screen_lbl.update_frame)
        # Camera frames go to camera window's label
        self.signals.blackout_detected.connect(self._on_blackout_detected)
        self.signals.status_message.connect(self._append_log)
        self.signals.fps_updated.connect(self._on_fps_updated)
        self.signals.auto_click_count.connect(
            lambda n: self._click_count_lbl.display(n))

    # ─────────────────────────────────────────
    #  Preview pump
    # ─────────────────────────────────────────
    def _pump_preview(self):
        rec   = self.engine.recording
        st    = self.engine.start_time

        # Screen frames → main window
        try:
            sf = self.engine.screen_queue.get_nowait()
            if self._scr_toggle.isChecked():
                disp_sf = RecorderEngine._stamp_preview(sf, rec, st)
                self._screen_lbl.update_frame(disp_sf)
        except queue.Empty:
            pass

        # Camera frames → camera window
        try:
            cf = self.engine.camera_queue.get_nowait()
            if self._cam_win.isVisible():
                disp_cf = RecorderEngine._stamp_preview(cf, rec, st)
                self._cam_win.get_preview_label().update_frame(disp_cf)
        except queue.Empty:
            pass

    # ─────────────────────────────────────────
    #  Periodic UI refresh
    # ─────────────────────────────────────────
    def _refresh_ui(self):
        if self.engine.recording and self.engine.start_time:
            e  = time.time() - self.engine.start_time
            hh = int(e // 3600); mm = int((e % 3600) // 60); ss = int(e % 60)
            self._rec_timer_lbl.setText(f"{hh:02d}:{mm:02d}:{ss:02d}")

        self._scr_bo_lbl.setText(str(self.engine.screen_blackout_count))
        self._cam_bo_lbl.setText(str(self.engine.camera_blackout_count))

        # ROI averages
        lines = []
        for src, avgs, overall in [
            ("Screen", self.engine.screen_roi_avg, self.engine.screen_overall_avg),
            ("Camera", self.engine.camera_roi_avg, self.engine.camera_overall_avg),
        ]:
            if avgs:
                b, g, r = overall
                br = 0.114*b + 0.587*g + 0.299*r
                lines.append(
                    f"[{src}] R{int(r)} G{int(g)} B{int(b)} Br:{int(br)}")
                for i, a in enumerate(avgs[:6]):
                    b2, g2, r2 = a
                    br2 = 0.114*b2 + 0.587*g2 + 0.299*r2
                    lines.append(
                        f"  ROI{i+1}: R{int(r2)} G{int(g2)} B{int(b2)} Br:{int(br2)}")
        self._roi_text.setPlainText("\n".join(lines))

        # Blackout events
        ev_lines = []
        for src, evs in [("Screen", self.engine.screen_blackout_events),
                          ("Camera", self.engine.camera_blackout_events)]:
            if evs:
                ev_lines.append(f"── {src} ──")
                for ev in reversed(evs[-8:]):
                    ev_lines.append(
                        f"  {ev['time']}  변화량: {int(ev['brightness_change'])}")
        self._bo_events_text.setPlainText("\n".join(ev_lines))

    def _update_fps_display(self):
        sfps = self.engine._measured_fps(self.engine._screen_fps_ts)
        cfps = self.engine._measured_fps(self.engine._camera_fps_ts)
        self._screen_fps_lbl.setText(f"{sfps:.1f} fps")
        self._camera_fps_lbl.setText(f"{cfps:.1f} fps")
        self._screen_fps_badge.setText(f"FPS: {sfps:.1f}")
        self._cam_fps_detected_lbl.setText(
            f"{self.engine.actual_camera_fps:.2f} fps")

    def _check_segment(self):
        if self.engine.recording and self.engine.current_segment_start:
            if (time.time() - self.engine.current_segment_start
                    >= self.engine.segment_duration):
                self._append_log("Creating new 30-min segment…")
                threading.Thread(
                    target=self.engine._create_segment, daemon=True).start()

    # ─────────────────────────────────────────
    #  Slot handlers
    # ─────────────────────────────────────────
    def _on_screen_thread_toggle(self, checked: bool):
        self._screen_lbl.set_thread_active(checked)
        if checked:
            self.engine.start_screen_thread()
        else:
            self.engine.stop_screen_thread()

    def _on_cam_win_toggle(self, checked: bool):
        if checked:
            self._cam_win.show()
            self._cam_win.raise_()
        else:
            self._cam_win.hide()

    def _on_start_rec(self):
        self.engine.start_recording()
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._rec_status_lbl.setText("● RECORDING")
        self._rec_status_lbl.setStyleSheet(
            "color:#2ecc71; font-weight:bold; font-size:14px;")

    def _on_stop_rec(self):
        self.engine.stop_recording()
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._rec_status_lbl.setText("● STOPPED")
        self._rec_status_lbl.setStyleSheet(
            "color:#e74c3c; font-weight:bold; font-size:14px;")
        self._rec_timer_lbl.setText("00:00:00")

    def _on_screen_rec_toggle(self, checked: bool):
        self.engine.screen_recording_enabled = checked
        if self.engine.recording:
            threading.Thread(
                target=self.engine._create_segment, daemon=True).start()
        self._append_log(
            f"Screen recording {'ENABLED' if checked else 'DISABLED'}")

    def _on_ac_start(self):
        self.engine.start_auto_click()
        self._btn_ac_start.setEnabled(False)
        self._btn_ac_stop.setEnabled(True)
        self._ac_status_lbl.setText("● RUNNING")
        self._ac_status_lbl.setStyleSheet("color:#2ecc71; font-weight:bold;")

    def _on_ac_stop(self):
        self.engine.stop_auto_click()
        self._btn_ac_start.setEnabled(True)
        self._btn_ac_stop.setEnabled(False)
        self._ac_status_lbl.setText("● STOPPED")
        self._ac_status_lbl.setStyleSheet("color:#e74c3c; font-weight:bold;")

    def _on_fps_updated(self, sfps, cfps):
        self._screen_fps_lbl.setText(f"{sfps:.1f} fps")
        self._camera_fps_lbl.setText(f"{cfps:.1f} fps")

    def _on_blackout_detected(self, source: str, event: dict):
        self._append_log(
            f"[BLACKOUT/{source.upper()}] {event['time']}  "
            f"변화량: {int(event['brightness_change'])}")

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.append(f"[{ts}] {msg}")
        self._status_lbl.setText(msg[:100])

    # ─────────────────────────────────────────
    #  Hotkeys
    # ─────────────────────────────────────────
    def _setup_hotkeys(self):
        if not PYNPUT_AVAILABLE:
            return
        hotkeys = {
            '<ctrl>+<alt>+w': self._on_start_rec,
            '<ctrl>+<alt>+e': self._on_stop_rec,
            '<ctrl>+<alt>+r': lambda: self._blackout_rec_chk.setChecked(
                not self._blackout_rec_chk.isChecked()),
            '<ctrl>+<alt>+d': lambda: self._screen_rec_chk.setChecked(
                not self._screen_rec_chk.isChecked()),
            '<ctrl>+<alt>+a': self._on_ac_start,
            '<ctrl>+<alt>+s': self._on_ac_stop,
            '<ctrl>+<alt>+q': self.close,
        }
        self._hotkey_listener = pynput_keyboard.GlobalHotKeys(hotkeys)
        self._hotkey_listener.start()

    # ─────────────────────────────────────────
    #  Dark style
    # ─────────────────────────────────────────
    def _dark_style(self):
        return """
        QMainWindow, QWidget  { background: #12122a; color: #ddd; }
        QDialog               { background: #0d0d1e; color: #ddd; }
        QGroupBox {
            border: 1px solid #2a2a4a;
            border-radius: 6px;
            margin-top: 8px;
            font-weight: bold;
            color: #7ab;
        }
        QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
        QPushButton {
            background: #1e2a3a; border: 1px solid #336;
            border-radius: 4px; padding: 4px 10px; color: #ccd;
        }
        QPushButton:hover   { background: #2a3a4e; }
        QPushButton:pressed { background: #1a2030; }
        QPushButton:disabled { background: #1a1a2e; color: #444; }
        QTextEdit  { background: #0d0d1e; border: 1px solid #2a2a4a; color: #ccc; }
        QDoubleSpinBox, QSpinBox, QComboBox {
            background: #1a1a3a; border: 1px solid #336;
            color: #ddd; padding: 2px 4px; border-radius: 3px;
        }
        QCheckBox { color: #ccd; spacing: 6px; }
        QCheckBox::indicator { width:15px; height:15px; }
        QLabel  { color: #ccd; }
        QLCDNumber { background: #0d1520; border: 1px solid #336; color:#2ecc71; }
        """

    # ─────────────────────────────────────────
    #  Close
    # ─────────────────────────────────────────
    def closeEvent(self, e):
        self.engine.stop()
        self._cam_win.hide()
        if PYNPUT_AVAILABLE and hasattr(self, '_hotkey_listener'):
            self._hotkey_listener.stop()
        e.accept()


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ScreenCameraRecorder")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()