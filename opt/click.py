import pyautogui
import time
pyautogui.PAUSE =1
pyautogui.FAILSAFE = True

limit_time = int(input(" : "))
time_check = 0
while True:
#
    time.sleep(240)

    pyautogui.click(504,594,button='left',clicks = 1, interval = 1)
    time_check += 1
    
    print(time_check)
    
    if time_check == int(limit_time):
        break