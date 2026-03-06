import cv2
import numpy as np
import threading
import time
import os
from datetime import datetime
from pynput import keyboard
import queue
import mss

class ScreenCameraRecorder:
    def __init__(self):
        self.recording = False
        self.running = True
        self.start_time = None
        self.stop_event = threading.Event()
        self.screen_queue = queue.Queue(maxsize=10)
        self.camera_queue = queue.Queue(maxsize=10)
        self.screen_writer = None
        self.camera_writer = None
        
        # 동적 FPS 설정
        self.target_fps = 30.0  # 기본값
        self.actual_camera_fps = 30.0  # 실제 카메라 FPS
        self.actual_screen_fps = 30.0  # 실제 화면 캡처 FPS
        
        # 30분 단위 녹화 분할
        self.segment_duration = 30 * 60  # 30분 (초 단위)
        self.current_segment_start = None
        
        # ROI 설정
        self.screen_rois = []
        self.camera_rois = []
        self.temp_points = {"screen": [], "camera": []}
        
        # ROI 평균 픽셀 값 저장
        self.screen_roi_avg = []
        self.camera_roi_avg = []
        
        # 전체 ROI 평균값 저장
        self.screen_overall_avg = np.array([0.0, 0.0, 0.0])
        self.camera_overall_avg = np.array([0.0, 0.0, 0.0])
        
        # 이전 프레임의 평균값 (깜빡임 감지용)
        self.screen_prev_avg = np.array([0.0, 0.0, 0.0])
        self.camera_prev_avg = np.array([0.0, 0.0, 0.0])
        
        # ROI별 이전 값 (동시성 감지용)
        self.screen_roi_prev = []
        self.camera_roi_prev = []
        
        # 깜빡임 감지 임계값
        self.brightness_threshold = 30.0  # 밝기 변화량
        self.std_threshold = 15.0  # 표준편차 임계값 (블랙아웃은 낮음)
        self.simultaneity_threshold = 0.1  # 동시성 임계값 (초 단위)
        
        # Blackout 감지 이벤트 로그
        self.screen_blackout_events = []
        self.camera_blackout_events = []
        
        # Blackout 카운트
        self.screen_blackout_count = 0
        self.camera_blackout_count = 0
        
        # 영상 버퍼 (동적 크기)
        self.screen_buffer = []
        self.camera_buffer = []
        self.buffer_seconds = 30  # 30초 분량
        
        # Blackout 감지 쿨다운 (5초)
        self.screen_last_blackout_time = 0
        self.camera_last_blackout_time = 0
        self.blackout_cooldown = 5.0
        
        # Blackout 녹화 활성화 스위치
        self.blackout_recording_enabled = True
        
        # UI 컨트롤
        self.ui_lock = threading.Lock()

    @property
    def screen_buffer_max_size(self):
        """동적 버퍼 크기 계산 (FPS 기반)"""
        return int(self.actual_screen_fps * self.buffer_seconds)
    
    @property
    def camera_buffer_max_size(self):
        """동적 버퍼 크기 계산 (FPS 기반)"""
        return int(self.actual_camera_fps * self.buffer_seconds)

    def detect_camera_fps(self):
        """카메라 실제 FPS 감지"""
        cap = cv2.VideoCapture(1)
        if not cap.isOpened():
            cap = cv2.VideoCapture(0)
        
        # 카메라 FPS 읽기
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps > 0:
            self.actual_camera_fps = fps
        else:
            # FPS를 읽을 수 없으면 측정
            num_frames = 30
            start = time.time()
            for _ in range(num_frames):
                ret, _ = cap.read()
                if not ret:
                    break
            elapsed = time.time() - start
            if elapsed > 0:
                self.actual_camera_fps = num_frames / elapsed
        
        cap.release()
        print(f"Camera FPS detected: {self.actual_camera_fps:.2f}")

    def on_mouse(self, event, x, y, flags, param):
        """마우스 클릭으로 ROI 영역 설정"""
        source = param
        rois = self.screen_rois if source == "screen" else self.camera_rois
        points = self.temp_points[source]

        if event == cv2.EVENT_LBUTTONDOWN:
            points.append((x, y))
            if len(points) == 2:
                x1, y1 = points[0]
                x2, y2 = points[1]
                rect = (min(x1, x2), min(y1, y2), abs(x1 - x2), abs(y1 - y2))
                if len(rois) < 10:
                    rois.append(rect)
                points.clear()
        elif event == cv2.EVENT_RBUTTONDOWN:
            if rois: 
                rois.pop()

    def save_blackout_clip(self, source="screen", timestamp=None, blackout_frame_idx=None):
        """Blackout 발생 시 전후 10초 영상 클립 저장 (총 20초)"""
        if not self.blackout_recording_enabled:
            print(f"[Blackout] {source} - Recording disabled, skipping save")
            return
        
        desktop = os.path.expanduser("~/Desktop")
        blackout_base_dir = os.path.join(desktop, "BLACK_OUT")
        
        # 소스별 독립 폴더
        source_dir = os.path.join(blackout_base_dir, source.upper())
        os.makedirs(source_dir, exist_ok=True)
        
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        
        buffer = self.screen_buffer if source == "screen" else self.camera_buffer
        fps = self.actual_screen_fps if source == "screen" else self.actual_camera_fps
        
        if len(buffer) == 0:
            return
        
        # 전 10초, 후 10초 (FPS 기반)
        frames_before = int(fps * 10)
        frames_after = int(fps * 10)
        
        current_idx = len(buffer) - 1
        start_idx = max(0, current_idx - frames_before)
        
        # Blackout 발생 시점까지의 프레임
        clip_frames = list(buffer[start_idx:current_idx + 1])
        blackout_event_time = datetime.now()
        
        print(f"[Blackout] {source} - Capturing frames...")
        print(f"  Buffer: {len(buffer)}, Before frames: {len(clip_frames)}")
        
        # 이후 10초 동안 프레임 수집
        start_wait = time.time()
        while len(clip_frames) - frames_before < frames_after and (time.time() - start_wait) < 11:
            time.sleep(0.05)
            current_buffer = self.screen_buffer if source == "screen" else self.camera_buffer
            if len(current_buffer) > current_idx:
                new_frames = list(current_buffer[current_idx + 1:])
                clip_frames.extend(new_frames)
                current_idx = len(current_buffer) - 1
                
                if len(clip_frames) >= frames_before + frames_after:
                    break
        
        final_frames = clip_frames[:frames_before + frames_after]
        
        print(f"  Total frames: {len(final_frames)} (target: {frames_before + frames_after})")
        
        if len(final_frames) == 0:
            print(f"  ERROR: No frames!")
            return
        
        # Blackout 시점 텍스트
        blackout_time_str = blackout_event_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # 영상 저장 (왼쪽 상단에 시간 표시)
        h, w = final_frames[0].shape[:2]
        video_path = os.path.join(source_dir, f"blackout_{timestamp}.mp4")
        writer = cv2.VideoWriter(
            video_path,
            cv2.VideoWriter_fourcc(*'mp4v'),
            fps,
            (w, h)
        )
        
        for i, frame in enumerate(final_frames):
            frame_copy = frame.copy()
            # Blackout 시점 표시
            cv2.putText(frame_copy, f"BLACKOUT: {blackout_time_str}", (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            # 프레임 번호 (디버깅용)
            relative_time = (i - frames_before) / fps
            cv2.putText(frame_copy, f"T: {relative_time:+.1f}s", (10, 60), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            writer.write(frame_copy)
        
        writer.release()
        
        # Blackout 발생 시점 프레임 캡처
        capture_idx = min(frames_before, len(final_frames) - 1)
        capture_frame = final_frames[capture_idx].copy()
        
        # 캡처 이미지에도 시간 표시
        cv2.putText(capture_frame, f"BLACKOUT: {blackout_time_str}", (10, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        capture_path = os.path.join(source_dir, f"capture_{timestamp}.jpg")
        cv2.imwrite(capture_path, capture_frame)
        
        duration = len(final_frames) / fps
        print(f"  Saved: {video_path} ({duration:.1f}s)")
        print(f"  Capture: {capture_path}")

    def detect_blackout(self, current_avg_list, prev_avg_list, source="screen"):
        """
        단순 Blackout 감지 - 밝기 감소만 체크
        """
        if len(current_avg_list) == 0 or len(prev_avg_list) == 0:
            return False
        
        if len(current_avg_list) != len(prev_avg_list):
            return False
        
        # 각 ROI의 밝기 변화 계산
        brightness_changes = []
        current_brightnesses = []
        
        for curr, prev in zip(current_avg_list, prev_avg_list):
            if np.all(prev == 0):
                continue
            
            curr_brightness = 0.114 * curr[0] + 0.587 * curr[1] + 0.299 * curr[2]
            prev_brightness = 0.114 * prev[0] + 0.587 * prev[1] + 0.299 * prev[2]
            
            brightness_change = prev_brightness - curr_brightness  # 어두워지는 경우 양수
            brightness_changes.append(brightness_change)
            current_brightnesses.append(curr_brightness)
        
        if len(brightness_changes) == 0:
            return False
        
        mean_brightness_change = np.mean(brightness_changes)
        
        # 조건: 평균 밝기 감소가 임계값 이상
        if mean_brightness_change < self.brightness_threshold:
            return False
        
        current_time = time.time()
        
        # 쿨다운 체크
        last_blackout_time = self.screen_last_blackout_time if source == "screen" else self.camera_last_blackout_time
        
        if current_time - last_blackout_time < self.blackout_cooldown:
            remaining = self.blackout_cooldown - (current_time - last_blackout_time)
            print(f"[Blackout] {source} - Cooldown ({remaining:.1f}s remaining)")
            return False
        
        # Blackout 감지 성공!
        if source == "screen":
            self.screen_last_blackout_time = current_time
            self.screen_blackout_count += 1
        else:
            self.camera_last_blackout_time = current_time
            self.camera_blackout_count += 1
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        
        current_std = np.std(current_brightnesses)
        change_std = np.std(brightness_changes)
        
        event_log = {
            'time': datetime.now().strftime("%H:%M:%S.%f")[:-3],
            'brightness_change': mean_brightness_change,
            'brightness_std': current_std,
            'change_std': change_std
        }
        
        if source == "screen":
            self.screen_blackout_events.append(event_log)
            if len(self.screen_blackout_events) > 50:
                self.screen_blackout_events.pop(0)
        else:
            self.camera_blackout_events.append(event_log)
            if len(self.camera_blackout_events) > 50:
                self.camera_blackout_events.pop(0)
        
        print(f"[Blackout] {source} - DETECTED!")
        print(f"  Brightness decrease: {mean_brightness_change:.1f}")
        
        threading.Thread(target=self.save_blackout_clip, args=(source, timestamp), daemon=True).start()
        
        return True

    def calculate_roi_average(self, frame, rois):
        """ROI 영역의 평균 픽셀 값 계산 (BGR)"""
        averages = []
        for (rx, ry, rw, rh) in rois:
            if rw > 0 and rh > 0:
                roi_region = frame[ry:ry+rh, rx:rx+rw]
                if roi_region.size > 0:
                    avg_color = roi_region.mean(axis=0).mean(axis=0)
                    averages.append(avg_color)
                else:
                    averages.append(np.array([0, 0, 0]))
            else:
                averages.append(np.array([0, 0, 0]))
        return averages

    def get_color_name(self, bgr):
        """BGR 값을 기반으로 색상 이름 추정"""
        b, g, r = bgr
        
        if max(r, g, b) < 50:
            return "Black"
        if min(r, g, b) > 200:
            return "White"
        if r > g * 1.5 and r > b * 1.5:
            return "Red"
        elif g > r * 1.5 and g > b * 1.5:
            return "Green"
        elif b > r * 1.5 and b > g * 1.5:
            return "Blue"
        elif r > 150 and g > 150 and b < 100:
            return "Yellow"
        elif r > 150 and b > 150 and g < 100:
            return "Magenta"
        elif g > 150 and b > 150 and r < 100:
            return "Cyan"
        else:
            return "Gray/Mixed"

    def create_control_ui(self):
        """컨트롤 UI 이미지 생성"""
        base_height = 300
        screen_roi_height = min(len(self.screen_roi_avg), 8) * 20 + 50
        screen_event_height = min(len(self.screen_blackout_events), 10) * 16 + 40
        camera_roi_height = min(len(self.camera_roi_avg), 8) * 20 + 50
        camera_event_height = min(len(self.camera_blackout_events), 10) * 16 + 40
        
        ui_height = base_height + screen_roi_height + screen_event_height + camera_roi_height + camera_event_height
        ui_height = max(650, min(ui_height, 1200))
        
        ui_width = 700
        ui_frame = np.ones((ui_height, ui_width, 3), dtype=np.uint8) * 240
        
        y_offset = 20
        
        # 제목
        cv2.putText(ui_frame, "Recording Control Panel", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        y_offset += 35
        
        # 녹화 상태
        status_text = "Recording: ON" if self.recording else "Recording: OFF"
        status_color = (0, 200, 0) if self.recording else (0, 0, 200)
        cv2.putText(ui_frame, status_text, (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, status_color, 2)
        
        # Blackout 녹화 활성화 상태
        blackout_status = "ON" if self.blackout_recording_enabled else "OFF"
        blackout_color = (0, 150, 0) if self.blackout_recording_enabled else (150, 0, 0)
        cv2.putText(ui_frame, f"Blackout Rec: {blackout_status}", (300, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, blackout_color, 2)
        y_offset += 30
        
        # FPS 정보
        fps_text = f"Screen: {self.actual_screen_fps:.1f}fps  Camera: {self.actual_camera_fps:.1f}fps"
        cv2.putText(ui_frame, fps_text, (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1)
        y_offset += 25
        
        # 녹화 시간
        if self.recording and self.start_time:
            elapsed = time.time() - self.start_time
            minutes, seconds = divmod(int(elapsed), 60)
            hours, minutes = divmod(minutes, 60)
            timer_str = f"Duration: {hours:02d}:{minutes:02d}:{seconds:02d}"
            cv2.putText(ui_frame, timer_str, (10, y_offset), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1)
        y_offset += 30
        
        # 구분선
        cv2.line(ui_frame, (10, y_offset), (ui_width-10, y_offset), (100, 100, 100), 2)
        y_offset += 20
        
        # Screen ROI 분석
        cv2.putText(ui_frame, f"Screen ROI (Blackouts: {self.screen_blackout_count})", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
        y_offset += 23
        
        with self.ui_lock:
            if len(self.screen_roi_avg) > 0:
                b, g, r = self.screen_overall_avg
                brightness = 0.114 * b + 0.587 * g + 0.299 * r
                text = f"  Overall: R{int(r)} G{int(g)} B{int(b)} Br:{int(brightness)}"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, (50, 50, 200), 2)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-15), 
                             (ui_width-10, y_offset-2), (int(b), int(g), int(r)), -1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-15), 
                             (ui_width-10, y_offset-2), (0, 0, 0), 1)
                y_offset += 20
            
            for i, avg_color in enumerate(self.screen_roi_avg[:8]):
                b, g, r = avg_color
                color_name = self.get_color_name(avg_color)
                text = f"  ROI{i+1}: R{int(r)} G{int(g)} B{int(b)} {color_name}"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 0), 1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-13), 
                             (ui_width-10, y_offset-2), (int(b), int(g), int(r)), -1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-13), 
                             (ui_width-10, y_offset-2), (0, 0, 0), 1)
                y_offset += 17
            
            if len(self.screen_roi_avg) > 8:
                cv2.putText(ui_frame, f"  +{len(self.screen_roi_avg) - 8} more", (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.33, (100, 100, 100), 1)
                y_offset += 14
        
        y_offset += 6
        
        # Screen Blackout 이벤트
        if len(self.screen_blackout_events) > 0:
            cv2.putText(ui_frame, "  Recent Blackouts:", (10, y_offset), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 0, 150), 1)
            y_offset += 17
            for event in list(reversed(self.screen_blackout_events))[:10]:
                text = f"    {event['time']} (Δ{int(event['brightness_change'])})"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.36, (200, 0, 0), 1)
                y_offset += 14
            
            if len(self.screen_blackout_events) > 10:
                cv2.putText(ui_frame, f"    +{len(self.screen_blackout_events) - 10} more", (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.3, (100, 100, 100), 1)
                y_offset += 13
        
        y_offset += 8
        
        # 구분선
        cv2.line(ui_frame, (10, y_offset), (ui_width-10, y_offset), (100, 100, 100), 2)
        y_offset += 20
        
        # Camera ROI 분석
        cv2.putText(ui_frame, f"Camera ROI (Blackouts: {self.camera_blackout_count})", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
        y_offset += 23
        
        with self.ui_lock:
            if len(self.camera_roi_avg) > 0:
                b, g, r = self.camera_overall_avg
                brightness = 0.114 * b + 0.587 * g + 0.299 * r
                text = f"  Overall: R{int(r)} G{int(g)} B{int(b)} Br:{int(brightness)}"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, (50, 50, 200), 2)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-15), 
                             (ui_width-10, y_offset-2), (int(b), int(g), int(r)), -1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-15), 
                             (ui_width-10, y_offset-2), (0, 0, 0), 1)
                y_offset += 20
            
            for i, avg_color in enumerate(self.camera_roi_avg[:8]):
                b, g, r = avg_color
                color_name = self.get_color_name(avg_color)
                text = f"  ROI{i+1}: R{int(r)} G{int(g)} B{int(b)} {color_name}"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 0), 1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-13), 
                             (ui_width-10, y_offset-2), (int(b), int(g), int(r)), -1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-13), 
                             (ui_width-10, y_offset-2), (0, 0, 0), 1)
                y_offset += 17
            
            if len(self.camera_roi_avg) > 8:
                cv2.putText(ui_frame, f"  +{len(self.camera_roi_avg) - 8} more", (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.33, (100, 100, 100), 1)
                y_offset += 14
        
        y_offset += 6
        
        # Camera Blackout 이벤트
        if len(self.camera_blackout_events) > 0:
            cv2.putText(ui_frame, "  Recent Blackouts:", (10, y_offset), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 0, 150), 1)
            y_offset += 17
            for event in list(reversed(self.camera_blackout_events))[:10]:
                text = f"    {event['time']} (Δ{int(event['brightness_change'])})"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.36, (200, 0, 0), 1)
                y_offset += 14
            
            if len(self.camera_blackout_events) > 10:
                cv2.putText(ui_frame, f"    +{len(self.camera_blackout_events) - 10} more", (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.3, (100, 100, 100), 1)
                y_offset += 13
        
        # 하단 고정
        y_offset = ui_height - 150
        
        cv2.line(ui_frame, (10, y_offset), (ui_width-10, y_offset), (100, 100, 100), 2)
        y_offset += 18
        
        cv2.putText(ui_frame, "Hotkeys:", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)
        y_offset += 20
        cv2.putText(ui_frame, "Ctrl+Alt+W : Start Recording", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 100, 0), 1)
        y_offset += 17
        cv2.putText(ui_frame, "Ctrl+Alt+E : Stop Recording", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 150), 1)
        y_offset += 17
        cv2.putText(ui_frame, "Ctrl+Alt+R : Toggle Blackout Recording", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (150, 100, 0), 1)
        y_offset += 17
        cv2.putText(ui_frame, "Ctrl+Alt+Q : Exit Program", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (150, 0, 0), 1)
        y_offset += 20
        
        cv2.putText(ui_frame, "* Blackout clips -> ~/Desktop/BLACK_OUT/[SCREEN|CAMERA]", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 100, 100), 1)
        
        return ui_frame

    def get_screen(self):
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while not self.stop_event.is_set():
                start_t = time.time()
                img = sct.grab(monitor)
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                if not self.screen_queue.full():
                    self.screen_queue.put(frame)
                elapsed = time.time() - start_t
                time.sleep(max(0.001, (1/self.actual_screen_fps) - elapsed))

    def get_camera(self):
        cap = cv2.VideoCapture(1)
        if not cap.isOpened():
            cap = cv2.VideoCapture(0)
        
        while not self.stop_event.is_set():
            start_t = time.time()
            ret, frame = cap.read()
            if ret:
                if not self.camera_queue.full():
                    self.camera_queue.put(frame)
            elapsed = time.time() - start_t
            time.sleep(max(0.001, (1/self.actual_camera_fps) - elapsed))
        
        cap.release()

    def add_ui_elements(self, frame, rois):
        now = datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, time_str, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        if self.recording and self.start_time:
            elapsed = time.time() - self.start_time
            minutes, seconds = divmod(int(elapsed), 60)
            hours, minutes = divmod(minutes, 60)
            timer_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            cv2.putText(frame, timer_str, (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        for i, (rx, ry, rw, rh) in enumerate(rois):
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), (0, 0, 255), 2)
            cv2.putText(frame, f"ROI {i+1}", (rx, ry - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        
        return frame

    def create_new_segment(self):
        """30분 단위 새 세그먼트 생성"""
        if self.screen_writer:
            self.screen_writer.release()
        if self.camera_writer:
            self.camera_writer.release()
        
        segment_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        with mss.mss() as sct:
            mon = sct.monitors[1]
            screen_path = os.path.join(self.output_dir, f'screen_{segment_time}.mp4')
            self.screen_writer = cv2.VideoWriter(
                screen_path,
                cv2.VideoWriter_fourcc(*'mp4v'), 
                self.actual_screen_fps, 
                (mon['width'], mon['height'])
            )
            print(f"New screen segment: {screen_path}")
        
        try:
            temp_frame = self.camera_queue.get(timeout=2)
            h, w = temp_frame.shape[:2]
            camera_path = os.path.join(self.output_dir, f'camera_{segment_time}.mp4')
            self.camera_writer = cv2.VideoWriter(
                camera_path,
                cv2.VideoWriter_fourcc(*'mp4v'), 
                self.actual_camera_fps, 
                (w, h)
            )
            print(f"New camera segment: {camera_path}")
        except:
            pass
        
        self.current_segment_start = time.time()

    def start_recording(self):
        if self.recording: 
            return
        
        desktop = os.path.expanduser("~/Desktop")
        self.output_dir = os.path.join(desktop, datetime.now().strftime("Rec_%Y%m%d_%H%M%S"))
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.create_new_segment()
        self.start_time = time.time()
        self.recording = True
        print(f"Recording started: {self.output_dir}")

    def stop_recording(self):
        if not self.recording: 
            return
        
        self.recording = False
        time.sleep(0.3)
        
        if self.screen_writer: 
            self.screen_writer.release()
        if self.camera_writer: 
            self.camera_writer.release()
        
        self.screen_writer = self.camera_writer = None
        print("Recording stopped")

    def toggle_blackout_recording(self):
        """Blackout 녹화 토글"""
        self.blackout_recording_enabled = not self.blackout_recording_enabled
        status = "ENABLED" if self.blackout_recording_enabled else "DISABLED"
        print(f"Blackout recording {status}")

    def stop_and_exit(self):
        self.running = False
        self.stop_event.set()
        self.stop_recording()

    def run(self):
        # 카메라 FPS 감지
        self.detect_camera_fps()
        self.actual_screen_fps = self.target_fps  # 화면은 목표 FPS 사용
        
        threading.Thread(target=self.get_screen, daemon=True).start()
        threading.Thread(target=self.get_camera, daemon=True).start()

        cv2.namedWindow("Screen Preview", cv2.WINDOW_NORMAL)
        cv2.setMouseCallback("Screen Preview", self.on_mouse, "screen")
        cv2.namedWindow("Camera Preview", cv2.WINDOW_NORMAL)
        cv2.setMouseCallback("Camera Preview", self.on_mouse, "camera")
        cv2.namedWindow("Control Panel", cv2.WINDOW_NORMAL)
        
        hotkeys = {
            '<ctrl>+<alt>+w': self.start_recording, 
            '<ctrl>+<alt>+e': self.stop_recording,
            '<ctrl>+<alt>+r': self.toggle_blackout_recording,
            '<ctrl>+<alt>+q': self.stop_and_exit
        }

        with keyboard.GlobalHotKeys(hotkeys) as listener:
            while self.running:
                # 30분 단위 세그먼트 체크
                if self.recording and self.current_segment_start:
                    if time.time() - self.current_segment_start >= self.segment_duration:
                        print("Creating new 30-minute segment...")
                        self.create_new_segment()
                
                # Screen 처리
                if not self.screen_queue.empty():
                    s_frame = self.screen_queue.get()
                    
                    # 버퍼 관리
                    self.screen_buffer.append(s_frame.copy())
                    if len(self.screen_buffer) > self.screen_buffer_max_size:
                        self.screen_buffer.pop(0)
                    
                    # ROI 분석
                    if self.screen_rois:
                        with self.ui_lock:
                            self.screen_roi_avg = self.calculate_roi_average(s_frame, self.screen_rois)
                            
                            if len(self.screen_roi_avg) > 0:
                                self.screen_overall_avg = np.mean(self.screen_roi_avg, axis=0)
                                
                                # Blackout 감지
                                if len(self.screen_roi_prev) > 0:
                                    self.detect_blackout(
                                        self.screen_roi_avg, 
                                        self.screen_roi_prev, 
                                        "screen"
                                    )
                                
                                self.screen_roi_prev = [avg.copy() for avg in self.screen_roi_avg]
                    
                    s_frame_ui = self.add_ui_elements(s_frame.copy(), self.screen_rois)
                    if self.recording and self.screen_writer: 
                        self.screen_writer.write(s_frame_ui)
                    cv2.imshow("Screen Preview", s_frame_ui)
                
                # Camera 처리
                if not self.camera_queue.empty():
                    c_frame = self.camera_queue.get()
                    
                    # 버퍼 관리
                    self.camera_buffer.append(c_frame.copy())
                    if len(self.camera_buffer) > self.camera_buffer_max_size:
                        self.camera_buffer.pop(0)
                    
                    # ROI 분석
                    if self.camera_rois:
                        with self.ui_lock:
                            self.camera_roi_avg = self.calculate_roi_average(c_frame, self.camera_rois)
                            
                            if len(self.camera_roi_avg) > 0:
                                self.camera_overall_avg = np.mean(self.camera_roi_avg, axis=0)
                                
                                # Blackout 감지
                                if len(self.camera_roi_prev) > 0:
                                    self.detect_blackout(
                                        self.camera_roi_avg, 
                                        self.camera_roi_prev, 
                                        "camera"
                                    )
                                
                                self.camera_roi_prev = [avg.copy() for avg in self.camera_roi_avg]
                    
                    c_frame_ui = self.add_ui_elements(c_frame.copy(), self.camera_rois)
                    if self.recording and self.camera_writer: 
                        self.camera_writer.write(c_frame_ui)
                    cv2.imshow("Camera Preview", c_frame_ui)
                
                # Control Panel
                control_ui = self.create_control_ui()
                cv2.imshow("Control Panel", control_ui)
                
                cv2.waitKey(1)
            
            listener.stop()
        
        cv2.destroyAllWindows()

if __name__ == "__main__":
    ScreenCameraRecorder().run()