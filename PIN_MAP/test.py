'''
Created on 2021. 4. 22.

@author: wooyong.shin
'''
import pandas as pd
import os
from openpyxl import load_workbook
#OCR, EXCEL path
excel_path = 'C:/Users/wooyong.shin/Downloads/excel/'
ocr_path = 'C:/Users/wooyong.shin/Downloads/ocr/'
excel_file_name = '1111.xlsx'
export_path = 'C:/Users/wooyong.shin/Downloads/cis_excel/'

del_char = ":"
first_make_zero = ["nan","SPL","-"]

#file path + file name
def readExel(xlse_path, sheetName):
    xls_file = pd.ExcelFile(xlse_path)
    data = xls_file.parse(sheetName)
    return data
 
data = readExel(excel_path + excel_file_name, 'Sheet1')

data_col_list = list(pd.DataFrame(data))
data_row_list = []
df_row_list = []

for i in range(len(data)):
    data_row_list.append(i+1)
    
for k in range(len(data)):
    df_row_list.append(k)
    
row_dic = dict(zip(df_row_list,data_row_list))


data = data.rename(index = row_dic)
print(data)