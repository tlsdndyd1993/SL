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
        self.start_time = None
        self.screen_frames = queue.Queue()
        self.camera_frames = queue.Queue()
        self.stop_event = threading.Event()
        self.screen_writer = None
        self.camera_writer = None

    def on_press(self, key):
        try:
            if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                pass
        except:
            pass

    def on_release(self, key):
        try:
            if key.char == 'w':
                if not self.recording:
                    self.start_recording()
            elif key.char == 'q':
                self.stop_and_exit()
            elif key.char == 'e':
                self.stop_recording()
        except AttributeError:
            pass

    def get_screen(self):
        sct = mss.mss()
        monitor = sct.monitors[1]
        while not self.stop_event.is_set():
            screenshot = sct.grab(monitor)
            frame = np.array(screenshot)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            self.screen_frames.put(frame)
            time.sleep(0.033)

    def get_camera(self):
        cap = cv2.VideoCapture(1)
        while not self.stop_event.is_set():
            ret, frame = cap.read()
            if ret:
                self.camera_frames.put(frame)
            time.sleep(0.033)
        cap.release()

    def add_ui_elements(self, frame):
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

    def create_output_directory(self):
        desktop = os.path.expanduser("~/Desktop")
        folder_name = datetime.fromtimestamp(self.start_time).strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(desktop, folder_name)
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def start_recording(self):
        self.recording = True
        self.start_time = time.time()

    def stop_recording(self):
        self.recording = False

    def stop_and_exit(self):
        self.stop_event.set()

    def save_videos(self, output_dir, screen_frames_list, camera_frames_list):
        if screen_frames_list:
            h, w = screen_frames_list[0].shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(os.path.join(output_dir, 'screen.mp4'), fourcc, 30.0, (w, h))
            for frame in screen_frames_list:
                out.write(frame)
            out.release()

        if camera_frames_list:
            h, w = camera_frames_list[0].shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(os.path.join(output_dir, 'camera.mp4'), fourcc, 30.0, (w, h))
            for frame in camera_frames_list:
                out.write(frame)
            out.release()

    def run(self):
        threading.Thread(target=self.get_screen, daemon=True).start()
        threading.Thread(target=self.get_camera, daemon=True).start()

        listener = keyboard.Listener(on_release=self.on_release)
        listener.start()

        screen_frames_list = []
        camera_frames_list = []

        while not self.stop_event.is_set():
            try:
                screen_frame = self.screen_frames.get_nowait() if not self.screen_frames.empty() else None
                camera_frame = self.camera_frames.get_nowait() if not self.camera_frames.empty() else None

                if screen_frame is not None:
                    screen_frame = self.add_ui_elements(screen_frame)
                    cv2.imshow("Screen Recording", screen_frame)
                    if self.recording:
                        screen_frames_list.append(screen_frame.copy())

                if camera_frame is not None:
                    camera_frame = self.add_ui_elements(camera_frame)
                    cv2.imshow("Camera Recording", camera_frame)
                    if self.recording:
                        camera_frames_list.append(camera_frame.copy())

                if cv2.waitKey(1) & 0xFF == ord('ctrl+q'):
                    self.stop_and_exit()
            except Exception:
                pass

        cv2.destroyAllWindows()

        if self.start_time and (screen_frames_list or camera_frames_list):
            output_dir = self.create_output_directory()
            self.save_videos(output_dir, screen_frames_list, camera_frames_list)
            print(f"Recording saved to {output_dir}")
        else:
            print("No frames recorded")

if __name__ == "__main__":
    recorder = ScreenCameraRecorder()
    recorder.run()