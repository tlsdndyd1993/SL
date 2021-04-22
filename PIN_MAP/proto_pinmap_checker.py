'''
Created on 2021. 4. 15.

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

##data pre-processing
rev_list = []
all_rev_list = []
for i in data_col_list:
    for j in data_row_list:
         if str(data[i][j]).find(":") == 1:
             blank1 = str(data[i][j])
             blank1 = blank1.replace(del_char,"")
             data[i][j] = blank1
         for k in first_make_zero:
             if str(data[i][j]).find(k) == 0:
                 data[i][j] = 0
print(data)

number_of_cases = []
print("row :")
print(data_row_list)



print("col :")
print(data_col_list)

aa=0

for a in range(len(data_col_list)):
    for b in range(len(data_row_list)):
        if data[data_col_list[a]][data_row_list[b]] != 0:
            point_char = data[data_col_list[a]][data_row_list[b]][0]
            point_num = data[data_col_list[a]][data_row_list[b]][1:]
            if data_col_list[a] + str(data_row_list[b]) == data[point_char][int(point_num)] :
                aa += 1
                print(str(aa) + " : " + data_col_list[a] + str(data_row_list[b])+" 일치")
            if data_col_list[a] + str(data_row_list[b]) != data[point_char][int(point_num)] :
                aa += 1
                print(str(aa) + " : " + data_col_list[a] + str(data_row_list[b])  +" 불일치@@@@@@@@@@@@@@@@")
                
                
