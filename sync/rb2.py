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

    # 기존 on_release 메서드는 삭제하거나 주석 처리해도 됩니다.
    # GlobalHotkeys가 이를 대신합니다.

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
        # 카메라 인덱스는 환경에 따라 0 또는 1로 설정
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
        if self.recording:
            return
            
        desktop = os.path.expanduser("~/Desktop")
        self.output_dir = os.path.join(desktop, datetime.now().strftime("Rec_%Y%m%d_%H%M%S"))
        os.makedirs(self.output_dir, exist_ok=True)
        
        # 1. 스크린 Writer 설정
        with mss.mss() as sct:
            mon = sct.monitors[2]
            self.screen_writer = cv2.VideoWriter(
                os.path.join(self.output_dir, 'screen.mp4'),
                cv2.VideoWriter_fourcc(*'mp4v'), self.target_fps, (mon['width'], mon['height'])
            )

        # 2. 카메라 Writer 설정
        try:
            temp_frame = self.camera_queue.get(timeout=2)
            h, w = temp_frame.shape[:2]
            self.camera_writer = cv2.VideoWriter(
                os.path.join(self.output_dir, 'camera.mp4'),
                cv2.VideoWriter_fourcc(*'mp4v'), self.target_fps, (w, h)
            )
            print(f"녹화 시작: {self.output_dir}")
        except:
            print("에러: 카메라 프레임을 받지 못했습니다.")

        self.start_time = time.time()
        self.recording = True

    def stop_recording(self):
        if not self.recording:
            return
            
        self.recording = False
        time.sleep(0.3)
        if self.screen_writer: self.screen_writer.release()
        if self.camera_writer: self.camera_writer.release()
        self.screen_writer = None
        self.camera_writer = None
        print("녹화 종료 및 파일 저장 완료.")

    def stop_and_exit(self):
        print("프로그램 종료 중...")
        self.running = False
        self.stop_event.set()
        self.stop_recording()

    def run(self):
        # 영상 획득 스레드 시작
        threading.Thread(target=self.get_screen, daemon=True).start()
        threading.Thread(target=self.get_camera, daemon=True).start()

        cv2.namedWindow("Screen Preview", cv2.WINDOW_NORMAL)
        cv2.namedWindow("Camera Preview", cv2.WINDOW_NORMAL)
        
        # 단축키 설정: Ctrl + Alt + W(시작), E(중지), Q(종료)
        # 일반 타이핑과 겹치지 않게 하기 위해 <ctrl>+<alt> 조합을 권장합니다.
        hotkeys = {
            '<ctrl>+<alt>+w': self.start_recording,
            '<ctrl>+<alt>+e': self.stop_recording,
            '<ctrl>+<alt>+q': self.stop_and_exit
        }

        with keyboard.GlobalHotKeys(hotkeys) as listener:
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
                    if self.recording and self.camera_writer:
                        c_frame_ui = self.add_ui_elements(c_frame.copy(), "CAMERA")
                        self.camera_writer.write(c_frame_ui)
                        cv2.imshow("Camera Preview", c_frame_ui)
                    else:
                        cv2.imshow("Camera Preview", self.add_ui_elements(c_frame, "CAMERA"))
                cv2.waitKey(1)

            
            listener.stop()
        
        cv2.destroyAllWindows()

if __name__ == "__main__":
    ScreenCameraRecorder().run()