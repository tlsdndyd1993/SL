import pyautogui
import time
pyautogui.PAUSE =1
pyautogui.FAILSAFE = True

while True:
    time.sleep(100)
    pyautogui.click(504,594,button='left',clicks = 1, interval = 1)
    print("1")
    
# class MOVEMOUSE:
    # def __init__(self):
        # self.position = pyautogui.position()
        # self.click = pyautogui.click(clicks = 2, interval=1)
        # self.num = 100
        # self.move = pyautogui.moveTo(self.num, self.num, 2)               
        #
    # def value_list(self):
        # print('a')
        #
    # def specific_value(self):
        # print("화면 크기 : " + str(pyautogui.size()))
        #
# a = MOVEMOUSE()
# a.specific_value()
# a.move
# a.click
#
# a.move
# a.num+=100
# print(a.num)
# a.move
# a.num = 100
# print('aaa')