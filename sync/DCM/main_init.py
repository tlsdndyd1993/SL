
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
    """
    이 클래스는 화면 녹화와 카메라 녹화를 동시에 수행하며 실시간 프리뷰를 제공합니다.
    
    [설계 구조 및 이유]
    1. 멀티스레딩 (Multithreading): 
       - 화면 캡처, 카메라 캡처, 그리고 메인 루프(UI/저장)를 분리하여 병렬 처리합니다.
       - 단일 스레드 사용 시 I/O 병목으로 인해 FPS(초당 프레임 수)가 급격히 저하되는 것을 방지합니다.
    
    2. 큐 (Queue) 시스템:
       - 스레드 간 안전한 데이터 전달을 위해 queue.Queue를 사용합니다.
       - 생산자(캡처 스레드)와 소비자(메인 루프)의 속도 차이를 완충하는 버퍼 역할을 합니다.
       - maxsize를 제한하여 메모리 과부하를 방지합니다.
    
    3. mss 라이브러리:
       - OpenCV의 기본 캡처보다 속도가 빠른 mss를 사용하여 고해상도 화면을 효율적으로 획득합니다.
    """
    def __init__(self):
        self.recording = False
        self.running = True
        self.start_time = None
        # 스레드 종료 신호를 안전하게 전달하기 위한 Event 객체
        self.stop_event = threading.Event()
        
        # 프레임 버퍼링을 위한 큐 설정
        self.screen_queue = queue.Queue(maxsize=10)
        self.camera_queue = queue.Queue(maxsize=10)
        
        self.screen_writer = None
        self.camera_writer = None
        
        # 일정한 녹화 속도 유지를 위한 목표 FPS
        self.target_fps = 30.0

    def get_screen(self):
        """
        화면 캡처 전용 스레드:
        - 메인 루프와 별개로 작동하여 화면 획득 지연이 전체 프로그램에 영향을 주지 않도록 함.
        """
        with mss.mss() as sct:
            monitor = sct.monitors[2] 
            while not self.stop_event.is_set():
                start_t = time.time()
                img = sct.grab(monitor)
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)
                
                if not self.screen_queue.full():
                    self.screen_queue.put(frame)
                
                # 정해진 FPS를 맞추기 위한 정밀한 시간 조절
                elapsed = time.time() - start_t
                time.sleep(max(0.001, (1/self.target_fps) - elapsed))

    def get_camera(self):
        """
        카메라 캡처 전용 스레드:
        - 하드웨어 장치(카메라)의 응답 대기 시간이 길어질 수 있으므로 독립 스레드로 운영.
        """
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
        """
        프레임 오버레이:
        - 현재 시간 및 녹화 경과 시간을 프레임에 직접 그려 넣어 데이터의 동시성을 시각적으로 확인.
        """
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
        """
        녹화 시작 로직:
        - 고유한 폴더명을 생성하여 파일 덮어쓰기를 방지.
        - VideoWriter를 초기화하여 디스크 쓰기 준비.
        """
        if self.recording:
            return
            
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
            # 카메라 해상도를 동적으로 파악하기 위해 첫 프레임을 가져옴
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
        """
        녹화 중지 로직:
        - Writer를 안전하게 release하여 파일 손상을 방지.
        """
        if not self.recording:
            return
            
        self.recording = False
        time.sleep(0.3) # 남은 프레임 처리를 위한 짧은 대기
        if self.screen_writer: self.screen_writer.release()
        if self.camera_writer: self.camera_writer.release()
        self.screen_writer = None
        self.camera_writer = None
        print("녹화 종료 및 파일 저장 완료.")

    def stop_and_exit(self):
        """프로그램 전체 종료 및 자원 해제"""
        print("프로그램 종료 중...")
        self.running = False
        self.stop_event.set()
        self.stop_recording()

    def run(self):
        """
        메인 실행 루프:
        1. 백그라운드 스레드 시작.
        2. 전역 핫키(GlobalHotKeys) 등록: 프로그램 창이 활성화되어 있지 않아도 제어 가능하게 함.
        3. 큐에서 프레임을 꺼내어 UI를 추가하고 화면에 표시 및 파일 저장 수행.
        """
        threading.Thread(target=self.get_screen, daemon=True).start()
        threading.Thread(target=self.get_camera, daemon=True).start()

        cv2.namedWindow("Screen Preview", cv2.WINDOW_NORMAL)
        cv2.namedWindow("Camera Preview", cv2.WINDOW_NORMAL)
        
        # pynput을 이용한 전역 단축키 설정 (백그라운드 제어용)
        hotkeys = {
            '<ctrl>+<alt>+w': self.start_recording,
            '<ctrl>+<alt>+e': self.stop_recording,
            '<ctrl>+<alt>+q': self.stop_and_exit
        }

        with keyboard.GlobalHotKeys(hotkeys) as listener:
            while self.running:
                # 스크린 큐 처리
                if not self.screen_queue.empty():
                    s_frame = self.screen_queue.get()
                    s_frame = self.add_ui_elements(s_frame, "SCREEN")
                    if self.recording and self.screen_writer:
                        self.screen_writer.write(s_frame)
                    cv2.imshow("Screen Preview", s_frame)

                # 카메라 큐 처리
                if not self.camera_queue.empty():
                    c_frame = self.camera_queue.get()
                    if self.recording and self.camera_writer:
                        c_frame_ui = self.add_ui_elements(c_frame.copy(), "CAMERA")
                        self.camera_writer.write(c_frame_ui)
                        cv2.imshow("Camera Preview", c_frame_ui)
                    else:
                        cv2.imshow("Camera Preview", self.add_ui_elements(c_frame, "CAMERA"))
                
                # OpenCV 창 이벤트를 처리하기 위한 필수 함수
                cv2.waitKey(1)

            listener.stop()
        
        cv2.destroyAllWindows()

if __name__ == "__main__":
    ScreenCameraRecorder().run()
