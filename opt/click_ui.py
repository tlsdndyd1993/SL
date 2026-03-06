import tkinter as tk
from tkinter import messagebox
import pyautogui
import threading
import keyboard
import time
from datetime import datetime

class AutoClicker:
    def __init__(self, root):
        self.root = root
        self.root.title("1234")
        self.root.geometry("350x300")
        
        self.running = False
        self.interval = 1.0
        self.click_count = 0
        self.start_time = None
        self.app_launch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # UI 구성
        tk.Label(root, text=f"프로그램 시작 시각: {self.app_launch_time}", fg="blue").pack(pady=5)

        tk.Label(root, text="주기 (초):").pack()
        self.entry = tk.Entry(root, justify='center')
        self.entry.insert(0, "1.0")
        self.entry.pack(pady=5)

        # 상태 및 카운트 표시
        self.status_label = tk.Label(root, text="상태: 정지 (OFF)", font=("Arial", 10, "bold"), fg="red")
        self.status_label.pack(pady=5)

        self.count_label = tk.Label(root, text="횟수: 0회", font=("Arial", 10))
        self.count_label.pack()

        self.timer_label = tk.Label(root, text="실행 타이머: 00:00:00", font=("Arial", 10))
        self.timer_label.pack(pady=5)

        tk.Label(root, text="단축키: Ctrl+Shift+F2", fg="gray").pack(pady=10)

        # 단축키 및 타이머 업데이트 시작
        keyboard.add_hotkey('ctrl+shift+f2', self.toggle_clicking)
        self.update_ui_elements()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def toggle_clicking(self):
        if not self.running:
            try:
                self.interval = float(self.entry.get())
                if self.interval < 0.01: raise ValueError
                
                self.running = True
                self.start_time = time.time() # 클릭 시작 시점 타이머 리셋
                self.status_label.config(text="상태: 실행 중 (ON)", fg="green")
                threading.Thread(target=self.click_loop, daemon=True).start()
            except ValueError:
                messagebox.showerror("오류", "올바른 숫자(0.01 이상)를 입력하세요.")
        else:
            self.running = False
            self.status_label.config(text="상태: 정지 (OFF)", fg="red")

    def click_loop(self):
        while self.running:
            pyautogui.click()
            self.click_count += 1
            time.sleep(self.interval)

    def update_ui_elements(self):
        """UI의 카운트와 타이머를 실시간으로 업데이트 (0.1초마다)"""
        self.count_label.config(text=f"횟수: {self.click_count}회")
        
        if self.running and self.start_time:
            elapsed = int(time.time() - self.start_time)
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            self.timer_label.config(text=f"실행 타이머: {hours:02}:{minutes:02}:{seconds:02}")
        
        # 100ms 후에 다시 이 함수를 호출 (재귀적 업데이트)
        self.root.after(100, self.update_ui_elements)

    def on_closing(self):
        self.running = False
        self.root.destroy()

if __name__ == "__main__":
    app = AutoClicker(tk.Tk())
    app.root.mainloop()