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


    def on_release(self, key):
        try:
            if key.char == 'w':
                if not self.recording: self.start_recording()
            elif key.char == 'e':
                if self.recording: self.stop_recording()
            elif key.char == 'q':
                self.stop_and_exit()
        except AttributeError:
            pass

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
                time.sleep(max(0.001, (1/self.target_fps) - elapsed))

    def get_camera(self):
        cap = cv2.VideoCapture(1)
        while not self.stop_event.is_set():
            start_t = time.time()
            ret, frame = cap.read()
            if ret:
                if not self.camera_queue.full():
                    self.camera_queue.put(frame)
            
            elapsed = time.time() - start_t
            time.sleep(max(0.001, (1/self.target_fps) - elapsed))
        cap.release()

    def add_ui_elements(self, frame, label=""):

        now = datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, time_str, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        if self.recording and self.start_time:
            elapsed = time.time() - self.start_time
            minutes, seconds = divmod(int(elapsed), 60)
            hours, minutes = divmod(minutes, 60)
            timer_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            cv2.putText(frame, timer_str, (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

            start_time_str = datetime.fromtimestamp(self.start_time).strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, f"Started: {start_time_str}", (10, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)

        
        
        return frame

    def start_recording(self):
        desktop = os.path.expanduser("~/Desktop")
        self.output_dir = os.path.join(desktop, datetime.now().strftime("Rec_%Y%m%d_%H%M%S"))
        os.makedirs(self.output_dir, exist_ok=True)
        
        # 1. 스크린 Writer 설정
        with mss.mss() as sct:
            mon = sct.monitors[1]
            self.screen_writer = cv2.VideoWriter(
                os.path.join(self.output_dir, 'screen.mp4'),
                cv2.VideoWriter_fourcc(*'mp4v'), self.target_fps, (mon['width'], mon['height'])
            )

        # 2. 카메라 Writer 설정 (가장 중요: 현재 큐에 있는 프레임 크기를 직접 확인)
        try:
            # 카메라 큐에서 프레임 하나를 확인하여 크기 추출
            temp_frame = self.camera_queue.get(timeout=2)
            h, w = temp_frame.shape[:2]
            self.camera_writer = cv2.VideoWriter(
                os.path.join(self.output_dir, 'camera.mp4'),
                cv2.VideoWriter_fourcc(*'mp4v'), self.target_fps, (w, h)
            )
            # 확인용 프레임 다시 넣어주기 (선택사항)
            # self.camera_queue.put(temp_frame) 
            print(f"카메라 녹화 시작: 해상도 {w}x{h}")
        except:
            print("에러: 카메라 프레임을 받지 못해 녹화를 시작할 수 없습니다.")

        self.start_time = time.time()
        self.recording = True

    def stop_recording(self):
        self.recording = False
        time.sleep(0.3)
        if self.screen_writer: self.screen_writer.release()
        if self.camera_writer: self.camera_writer.release()
        self.screen_writer = None
        self.camera_writer = None
        print("녹화 종료.")

    def stop_and_exit(self):
        self.running = False
        self.stop_event.set()
        self.stop_recording()

    def run(self):
        threading.Thread(target=self.get_screen, daemon=True).start()
        threading.Thread(target=self.get_camera, daemon=True).start()

        cv2.namedWindow("Screen Preview", cv2.WINDOW_NORMAL)
        cv2.namedWindow("Camera Preview", cv2.WINDOW_NORMAL)
        
        listener = keyboard.Listener(on_release=self.on_release)
        listener.start()

        while self.running:
            # 스크린 처리
            if not self.screen_queue.empty():
                s_frame = self.screen_queue.get()
                s_frame = self.add_ui_elements(s_frame, "SCREEN")
                if self.recording and self.screen_writer:
                    self.screen_writer.write(s_frame)
                cv2.imshow("Screen Preview", s_frame)

            # 카메라 처리
            if not self.camera_queue.empty():
                c_frame = self.camera_queue.get()
                # UI 추가 전 원본 크기 유지 (저장용)
                if self.recording and self.camera_writer:
                    # UI 요소를 넣은 프레임을 저장
                    c_frame_ui = self.add_ui_elements(c_frame.copy(), "CAMERA")
                    self.camera_writer.write(c_frame_ui)
                    cv2.imshow("Camera Preview", c_frame_ui)
                else:
                    cv2.imshow("Camera Preview", self.add_ui_elements(c_frame, "CAMERA"))

            if cv2.waitKey(1) & 0xFF == ord('q'):
                self.stop_and_exit()
                break
        cv2.destroyAllWindows()

if __name__ == "__main__":
    ScreenCameraRecorder().run()