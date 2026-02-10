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
        self.target_fps = 30.0
        
        # ROI 설정을 위한 변수 (각각 최대 10개)
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
        
        # 깜빡임 감지 임계값 (밝기 변화량)
        self.flicker_threshold = 30.0
        
        # 깜빡임 감지 이벤트 로그 (최근 50개)
        self.screen_flicker_events = []
        self.camera_flicker_events = []
        
        # 영상 버퍼 (전후 10초 저장용, 30fps 기준)
        self.screen_buffer = []
        self.camera_buffer = []
        self.buffer_max_size = 900  # 30초 분량 (여유 있게)
        
        # Blackout 감지 쿨다운 (5초)
        self.screen_last_blackout_time = 0
        self.camera_last_blackout_time = 0
        self.blackout_cooldown = 5.0  # 5초
        
        # UI 컨트롤
        self.ui_lock = threading.Lock()

    def on_mouse(self, event, x, y, flags, param):
        """마우스 클릭으로 ROI 영역 설정 (param: "screen" 또는 "camera")"""
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
            if rois: rois.pop() # 우클릭 시 마지막 ROI 삭제

    def save_blackout_clip(self, source="screen", timestamp=None):
        """깜빡임 발생 시 전후 10초 영상 클립 저장 (총 20초)"""
        desktop = os.path.expanduser("~/Desktop")
        blackout_dir = os.path.join(desktop, "BLACK_OUT")
        os.makedirs(blackout_dir, exist_ok=True)
        
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        
        # 버퍼에서 영상 클립 생성
        buffer = self.screen_buffer if source == "screen" else self.camera_buffer
        
        if len(buffer) == 0:
            return
        
        # 현재 시점(blackout 발생 시점)을 기준으로 전 10초, 후 10초 (30fps 기준 각 300프레임)
        frames_before = 300  # 10초 전
        frames_after = 300   # 10초 후
        
        current_idx = len(buffer) - 1
        start_idx = max(0, current_idx - frames_before)
        
        # 먼저 blackout 발생 시점까지의 프레임 가져오기
        clip_frames = list(buffer[start_idx:current_idx + 1])
        
        print(f"[Blackout] {source} - Capturing frames from buffer...")
        print(f"  Buffer size: {len(buffer)}, Current idx: {current_idx}")
        print(f"  Frames before blackout: {len(clip_frames)}")
        
        # 이후 10초 동안 추가 프레임 수집 (비동기로 대기)
        start_wait = time.time()
        while len(clip_frames) < frames_before + frames_after and (time.time() - start_wait) < 11:
            time.sleep(0.1)
            current_buffer = self.screen_buffer if source == "screen" else self.camera_buffer
            if len(current_buffer) > current_idx:
                new_frames = list(current_buffer[current_idx + 1:])
                clip_frames.extend(new_frames)
                current_idx = len(current_buffer) - 1
                
                if len(clip_frames) >= frames_before + frames_after:
                    break
        
        # 최종적으로 수집된 프레임만 저장
        final_frames = clip_frames[:frames_before + frames_after]
        
        print(f"  Total frames collected: {len(final_frames)} (target: {frames_before + frames_after})")
        
        if len(final_frames) == 0:
            print(f"  ERROR: No frames to save!")
            return
        
        # 영상 저장
        h, w = final_frames[0].shape[:2]
        video_path = os.path.join(blackout_dir, f"{source}_blackout_{timestamp}.mp4")
        writer = cv2.VideoWriter(
            video_path,
            cv2.VideoWriter_fourcc(*'mp4v'),
            self.target_fps,
            (w, h)
        )
        
        for frame in final_frames:
            writer.write(frame)
        
        writer.release()
        
        # 깜빡임 발생 시점 프레임 캡처 저장 (전 10초의 마지막 프레임)
        capture_idx = min(frames_before, len(final_frames) - 1)
        capture_frame = final_frames[capture_idx]
        capture_path = os.path.join(blackout_dir, f"{source}_capture_{timestamp}.jpg")
        cv2.imwrite(capture_path, capture_frame)
        
        duration = len(final_frames) / self.target_fps
        print(f"  BLACK_OUT saved: {video_path} ({duration:.1f}s, {len(final_frames)} frames)")
        print(f"  Capture saved: {capture_path}")

    def detect_flicker(self, current_avg, prev_avg, source="screen"):
        """깜빡임 감지 - 어두워지는 경우만 감지 (화면 꺼짐)"""
        if prev_avg is None or np.all(prev_avg == 0):
            return False
        
        # 밝기 계산 (0.299*R + 0.587*G + 0.114*B)
        current_brightness = 0.114 * current_avg[0] + 0.587 * current_avg[1] + 0.299 * current_avg[2]
        prev_brightness = 0.114 * prev_avg[0] + 0.587 * prev_avg[1] + 0.299 * prev_avg[2]
        
        brightness_change = prev_brightness - current_brightness  # 어두워지는 경우 양수
        
        # 어두워지는 경우만 감지 (임계값 이상 감소)
        if brightness_change > self.flicker_threshold:
            current_time = time.time()
            
            # 쿨다운 체크 (마지막 blackout 이후 5초 경과 확인)
            last_blackout_time = self.screen_last_blackout_time if source == "screen" else self.camera_last_blackout_time
            
            if current_time - last_blackout_time < self.blackout_cooldown:
                # 쿨다운 중이므로 로그만 남기고 저장하지 않음
                print(f"[Blackout] {source} - Cooldown active (skipping save, {self.blackout_cooldown - (current_time - last_blackout_time):.1f}s remaining)")
                return False
            
            # 쿨다운 타이머 업데이트
            if source == "screen":
                self.screen_last_blackout_time = current_time
            else:
                self.camera_last_blackout_time = current_time
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            event_log = {
                'time': datetime.now().strftime("%H:%M:%S.%f")[:-3],
                'brightness_change': brightness_change,
                'current': current_brightness,
                'prev': prev_brightness
            }
            
            if source == "screen":
                self.screen_flicker_events.append(event_log)
                if len(self.screen_flicker_events) > 50:  # 최대 50개 저장
                    self.screen_flicker_events.pop(0)
            else:
                self.camera_flicker_events.append(event_log)
                if len(self.camera_flicker_events) > 50:  # 최대 50개 저장
                    self.camera_flicker_events.pop(0)
            
            # BLACK_OUT 클립 저장 (별도 스레드에서 실행)
            print(f"[Blackout] {source} - Detected! Starting clip save...")
            threading.Thread(target=self.save_blackout_clip, args=(source, timestamp), daemon=True).start()
            
            return True
        
        return False

    def calculate_roi_average(self, frame, rois):
        """ROI 영역의 평균 픽셀 값 계산 (BGR)"""
        averages = []
        for (rx, ry, rw, rh) in rois:
            # ROI 영역이 프레임 범위 내에 있는지 확인
            if rw > 0 and rh > 0:
                roi_region = frame[ry:ry+rh, rx:rx+rw]
                if roi_region.size > 0:
                    avg_color = roi_region.mean(axis=0).mean(axis=0)  # (B, G, R)
                    averages.append(avg_color)
                else:
                    averages.append(np.array([0, 0, 0]))
            else:
                averages.append(np.array([0, 0, 0]))
        return averages

    def get_color_name(self, bgr):
        """BGR 값을 기반으로 색상 이름 추정"""
        b, g, r = bgr
        
        # 어두운 색상 (거의 검정)
        if max(r, g, b) < 50:
            return "Black"
        
        # 밝은 색상 (거의 흰색)
        if min(r, g, b) > 200:
            return "White"
        
        # 주요 색상 판단
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
        # 동적 높이 계산
        base_height = 250  # 헤더 + 단축키 영역
        screen_roi_height = min(len(self.screen_roi_avg), 8) * 20 + 50  # ROI당 20px
        screen_flicker_height = min(len(self.screen_flicker_events), 10) * 16 + 40  # 이벤트당 16px
        camera_roi_height = min(len(self.camera_roi_avg), 8) * 20 + 50
        camera_flicker_height = min(len(self.camera_flicker_events), 10) * 16 + 40
        
        ui_height = base_height + screen_roi_height + screen_flicker_height + camera_roi_height + camera_flicker_height
        ui_height = max(600, min(ui_height, 1200))  # 최소 600, 최대 1200
        
        ui_width = 650
        ui_frame = np.ones((ui_height, ui_width, 3), dtype=np.uint8) * 240
        
        y_offset = 20
        
        # 제목
        cv2.putText(ui_frame, "Recording Control Panel", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        y_offset += 40
        
        # 녹화 상태
        status_text = "Recording: ON" if self.recording else "Recording: OFF"
        status_color = (0, 200, 0) if self.recording else (0, 0, 200)
        cv2.putText(ui_frame, status_text, (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)
        y_offset += 35
        
        # 녹화 시간
        if self.recording and self.start_time:
            elapsed = time.time() - self.start_time
            minutes, seconds = divmod(int(elapsed), 60)
            hours, minutes = divmod(minutes, 60)
            timer_str = f"Duration: {hours:02d}:{minutes:02d}:{seconds:02d}"
            cv2.putText(ui_frame, timer_str, (10, y_offset), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)
        y_offset += 35
        
        # 구분선
        cv2.line(ui_frame, (10, y_offset), (ui_width-10, y_offset), (100, 100, 100), 2)
        y_offset += 20
        
        # Screen ROI 정보
        cv2.putText(ui_frame, "Screen ROI Analysis:", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 2)
        y_offset += 25
        
        with self.ui_lock:
            # 전체 평균 표시
            if len(self.screen_roi_avg) > 0:
                b, g, r = self.screen_overall_avg
                brightness = 0.114 * b + 0.587 * g + 0.299 * r
                text = f"  Overall: R{int(r)} G{int(g)} B{int(b)} Br:{int(brightness)}"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (50, 50, 200), 2)
                # 색상 샘플
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-15), 
                             (ui_width-10, y_offset-2), (int(b), int(g), int(r)), -1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-15), 
                             (ui_width-10, y_offset-2), (0, 0, 0), 1)
                y_offset += 22
            
            # 개별 ROI 표시 (최대 8개)
            display_count = 0
            for i, avg_color in enumerate(self.screen_roi_avg):
                if display_count >= 8:
                    break
                b, g, r = avg_color
                color_name = self.get_color_name(avg_color)
                text = f"  ROI{i+1}: R{int(r)} G{int(g)} B{int(b)} {color_name}"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-13), 
                             (ui_width-10, y_offset-2), (int(b), int(g), int(r)), -1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-13), 
                             (ui_width-10, y_offset-2), (0, 0, 0), 1)
                y_offset += 18
                display_count += 1
            
            if len(self.screen_roi_avg) > 8:
                more_text = f"  +{len(self.screen_roi_avg) - 8} more"
                cv2.putText(ui_frame, more_text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 100, 100), 1)
                y_offset += 15
        
        y_offset += 8
        
        # Screen Blackout 이벤트
        if len(self.screen_flicker_events) > 0:
            cv2.putText(ui_frame, "  Screen Blackouts:", (10, y_offset), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 150), 1)
            y_offset += 18
            display_count = 0
            for event in reversed(self.screen_flicker_events):  # 최신순
                if display_count >= 10:
                    break
                text = f"    {event['time']} (-{int(event['brightness_change'])})"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, (200, 0, 0), 1)
                y_offset += 15
                display_count += 1
            
            if len(self.screen_flicker_events) > 10:
                more_text = f"    +{len(self.screen_flicker_events) - 10} more"
                cv2.putText(ui_frame, more_text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.32, (100, 100, 100), 1)
                y_offset += 14
        
        y_offset += 10
        
        # 구분선
        cv2.line(ui_frame, (10, y_offset), (ui_width-10, y_offset), (100, 100, 100), 2)
        y_offset += 20
        
        # Camera ROI 정보
        cv2.putText(ui_frame, "Camera ROI Analysis:", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 2)
        y_offset += 25
        
        with self.ui_lock:
            # 전체 평균 표시
            if len(self.camera_roi_avg) > 0:
                b, g, r = self.camera_overall_avg
                brightness = 0.114 * b + 0.587 * g + 0.299 * r
                text = f"  Overall: R{int(r)} G{int(g)} B{int(b)} Br:{int(brightness)}"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (50, 50, 200), 2)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-15), 
                             (ui_width-10, y_offset-2), (int(b), int(g), int(r)), -1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-15), 
                             (ui_width-10, y_offset-2), (0, 0, 0), 1)
                y_offset += 22
            
            # 개별 ROI 표시 (최대 8개)
            display_count = 0
            for i, avg_color in enumerate(self.camera_roi_avg):
                if display_count >= 8:
                    break
                b, g, r = avg_color
                color_name = self.get_color_name(avg_color)
                text = f"  ROI{i+1}: R{int(r)} G{int(g)} B{int(b)} {color_name}"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-13), 
                             (ui_width-10, y_offset-2), (int(b), int(g), int(r)), -1)
                cv2.rectangle(ui_frame, (ui_width-70, y_offset-13), 
                             (ui_width-10, y_offset-2), (0, 0, 0), 1)
                y_offset += 18
                display_count += 1
            
            if len(self.camera_roi_avg) > 8:
                more_text = f"  +{len(self.camera_roi_avg) - 8} more"
                cv2.putText(ui_frame, more_text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 100, 100), 1)
                y_offset += 15
        
        y_offset += 8
        
        # Camera Blackout 이벤트
        if len(self.camera_flicker_events) > 0:
            cv2.putText(ui_frame, "  Camera Blackouts:", (10, y_offset), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 150), 1)
            y_offset += 18
            display_count = 0
            for event in reversed(self.camera_flicker_events):  # 최신순
                if display_count >= 10:
                    break
                text = f"    {event['time']} (-{int(event['brightness_change'])})"
                cv2.putText(ui_frame, text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, (200, 0, 0), 1)
                y_offset += 15
                display_count += 1
            
            if len(self.camera_flicker_events) > 10:
                more_text = f"    +{len(self.camera_flicker_events) - 10} more"
                cv2.putText(ui_frame, more_text, (10, y_offset), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.32, (100, 100, 100), 1)
                y_offset += 14
        
        # 하단 고정 영역 (단축키)
        y_offset = ui_height - 130
        
        # 구분선
        cv2.line(ui_frame, (10, y_offset), (ui_width-10, y_offset), (100, 100, 100), 2)
        y_offset += 20
        
        # 단축키 안내
        cv2.putText(ui_frame, "Hotkeys:", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
        y_offset += 23
        cv2.putText(ui_frame, "Ctrl+Alt+W : Start Recording", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 100, 0), 1)
        y_offset += 18
        cv2.putText(ui_frame, "Ctrl+Alt+E : Stop Recording", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 150), 1)
        y_offset += 18
        cv2.putText(ui_frame, "Ctrl+Alt+Q : Exit Program", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (150, 0, 0), 1)
        y_offset += 22
        
        # BLACK_OUT 저장 안내
        cv2.putText(ui_frame, "* Blackout clips -> ~/Desktop/BLACK_OUT", (10, y_offset), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (100, 100, 100), 1)
        
        return ui_frame

    def get_screen(self):
        with mss.mss() as sct:
            monitor = sct.monitors[2] 
            while not self.stop_event.is_set():
                start_t = time.time()
                img = sct.grab(monitor)
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                if not self.screen_queue.full():
                    self.screen_queue.put(frame)
                elapsed = time.time() - start_t
                time.sleep(max(0.001, (1/self.target_fps) - elapsed))

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
            time.sleep(max(0.001, (1/self.target_fps) - elapsed))
        cap.release()

    def add_ui_elements(self, frame, rois):
        now = datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, time_str, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        if self.recording and self.start_time:
            elapsed = time.time() - self.start_time
            minutes, seconds = divmod(int(elapsed), 60)
            hours, minutes = divmod(minutes, 60)
            timer_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            cv2.putText(frame, timer_str, (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        for i, (rx, ry, rw, rh) in enumerate(rois):
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), (0, 0, 255), 2)
            cv2.putText(frame, f"ROI {i+1}", (rx, ry - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
        
        return frame

    def start_recording(self):
        if self.recording: return
        desktop = os.path.expanduser("~/Desktop")
        self.output_dir = os.path.join(desktop, datetime.now().strftime("Rec_%Y%m%d_%H%M%S"))
        os.makedirs(self.output_dir, exist_ok=True)
        with mss.mss() as sct:
            mon = sct.monitors[2]
            self.screen_writer = cv2.VideoWriter(
                os.path.join(self.output_dir, 'screen.mp4'),
                cv2.VideoWriter_fourcc(*'mp4v'), self.target_fps, (mon['width'], mon['height'])
            )
        try:
            temp_frame = self.camera_queue.get(timeout=2)
            h, w = temp_frame.shape[:2]
            self.camera_writer = cv2.VideoWriter(
                os.path.join(self.output_dir, 'camera.mp4'),
                cv2.VideoWriter_fourcc(*'mp4v'), self.target_fps, (w, h)
            )
        except: pass
        self.start_time = time.time()
        self.recording = True

    def stop_recording(self):
        if not self.recording: return
        self.recording = False
        time.sleep(0.3)
        if self.screen_writer: self.screen_writer.release()
        if self.camera_writer: self.camera_writer.release()
        self.screen_writer = self.camera_writer = None

    def stop_and_exit(self):
        self.running = False
        self.stop_event.set()
        self.stop_recording()

    def run(self):
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
            '<ctrl>+<alt>+q': self.stop_and_exit
        }

        with keyboard.GlobalHotKeys(hotkeys) as listener:
            while self.running:
                # Screen 처리
                if not self.screen_queue.empty():
                    s_frame = self.screen_queue.get()
                    
                    # 프레임 버퍼에 추가 (±10초 저장용)
                    self.screen_buffer.append(s_frame.copy())
                    if len(self.screen_buffer) > self.buffer_max_size:
                        self.screen_buffer.pop(0)
                    
                    # ROI 평균값 계산
                    if self.screen_rois:
                        with self.ui_lock:
                            self.screen_roi_avg = self.calculate_roi_average(s_frame, self.screen_rois)
                            # 전체 ROI 평균 계산
                            if len(self.screen_roi_avg) > 0:
                                self.screen_overall_avg = np.mean(self.screen_roi_avg, axis=0)
                                # 깜빡임 감지
                                self.detect_flicker(self.screen_overall_avg, self.screen_prev_avg, "screen")
                                # 이전 값 업데이트
                                self.screen_prev_avg = self.screen_overall_avg.copy()
                    
                    s_frame_ui = self.add_ui_elements(s_frame.copy(), self.screen_rois)
                    if self.recording and self.screen_writer: 
                        self.screen_writer.write(s_frame_ui)
                    cv2.imshow("Screen Preview", s_frame_ui)
                
                # Camera 처리
                if not self.camera_queue.empty():
                    c_frame = self.camera_queue.get()
                    
                    # 프레임 버퍼에 추가 (±10초 저장용)
                    self.camera_buffer.append(c_frame.copy())
                    if len(self.camera_buffer) > self.buffer_max_size:
                        self.camera_buffer.pop(0)
                    
                    # ROI 평균값 계산
                    if self.camera_rois:
                        with self.ui_lock:
                            self.camera_roi_avg = self.calculate_roi_average(c_frame, self.camera_rois)
                            # 전체 ROI 평균 계산
                            if len(self.camera_roi_avg) > 0:
                                self.camera_overall_avg = np.mean(self.camera_roi_avg, axis=0)
                                # 깜빡임 감지
                                self.detect_flicker(self.camera_overall_avg, self.camera_prev_avg, "camera")
                                # 이전 값 업데이트
                                self.camera_prev_avg = self.camera_overall_avg.copy()
                    
                    c_frame_ui = self.add_ui_elements(c_frame.copy(), self.camera_rois)
                    if self.recording and self.camera_writer: 
                        self.camera_writer.write(c_frame_ui)
                    cv2.imshow("Camera Preview", c_frame_ui)
                
                # Control Panel UI 업데이트
                control_ui = self.create_control_ui()
                cv2.imshow("Control Panel", control_ui)
                
                cv2.waitKey(1)
            
            listener.stop()
        
        cv2.destroyAllWindows()

if __name__ == "__main__":
    ScreenCameraRecorder().run()
