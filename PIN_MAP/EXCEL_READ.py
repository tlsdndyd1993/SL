'''
Created on 2022. 11. 29.

@author: wooyong.shin
'''
import pandas as pd
import os
from openpyxl import load_workbook
import copy
import warnings
from pandas.core.common import SettingWithCopyWarning
warnings.simplefilter(action="ignore", category=SettingWithCopyWarning)

class EXCEL_READ:
    def __init__(self):
        self.a = 5
        self.column_processing(self.a)
        
    
    def column_processing(self,a):
        print(a)
        if a != 0:
            a = a-1
            self.column_processing(a)

            

EXCEL_READ()  