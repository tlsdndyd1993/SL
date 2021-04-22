'''
Created on 2021. 4. 20.

@author: wooyong.shin
'''

import pandas as pd
import os
from openpyxl import load_workbook
import numpy as np
import math

#OCR, EXCEL path
excel_path = 'C:/Users/wooyong.shin/Downloads/excel/'
ocr_path = 'C:/Users/wooyong.shin/Downloads/ocr/'
excel_file_name = '11111.xlsx'
export_path = 'C:/Users/wooyong.shin/Downloads/cis_excel/'

del_char = ":"
first_make_zero = ["nan","SPL","-"]

#file path + file name
def readExel(xlse_path, sheetName):
    xls_file = pd.ExcelFile(xlse_path)
    data = xls_file.parse(sheetName)
    return data
 
data = readExel(excel_path + excel_file_name, 'Sheet1')

col_len = len(list(data[1]))
col = []
for c in range(col_len):
    col.append(c)
    

row = list(data)
data = data.fillna(0)
fix_data = []

for i in col:
    for j in row:
        if data[j][i] != 0:
            fix_data.append(data[j][i])

# fix_data = fix_data[~np.isnan(fix_data)]

df = pd.DataFrame(fix_data)

df.to_excel(export_path + "a" + excel_file_name)
        