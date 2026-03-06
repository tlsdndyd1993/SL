import tkinter as tk
from tkinter import ttk

import SL.sync.DCM.cam_screen_record as cam_screen_record

class MN:
    def __init__(self):
        print("MN initialized")
        self.recorder = cam_screen_record.CamScreenRecord()
        self.is_recording = False
        self.setup_ui()
    
    def setup_ui(self):
        self.root = tk.Tk()
        self.root.title("Recording Control")
        self.root.geometry("300x150")
        
        # 녹화 시작 버튼
        self.start_btn = ttk.Button(
            self.root, 
            text="녹화 시작", 
            command=self.start_recording
        )
        self.start_btn.pack(pady=10)
        
        # 녹화 종료 버튼
        self.stop_btn = ttk.Button(
            self.root, 
            text="녹화 종료", 
            command=self.stop_recording,
            state=tk.DISABLED
        )
        self.stop_btn.pack(pady=10)
        
        # 프로그램 종료 버튼
        self.exit_btn = ttk.Button(
            self.root, 
            text="프로그램 종료", 
            command=self.exit_program
        )
        self.exit_btn.pack(pady=10)
        
        self.root.mainloop()
    
    def start_recording(self):
        self.recorder.start()
        self.is_recording = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        print("녹화 시작")
    
    def stop_recording(self):
        self.recorder.stop()
        self.is_recording = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        print("녹화 종료")
    
    def exit_program(self):
        if self.is_recording:
            self.stop_recording()
        self.root.quit()

if __name__ == "__main__":
    mn = MN()