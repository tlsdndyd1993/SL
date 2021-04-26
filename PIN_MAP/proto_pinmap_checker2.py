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

print("@@@data_col_list")
print(data_col_list)


for i in range(len(data)):
    data_row_list.append(i+1)
    
for k in range(len(data)):
    df_row_list.append(k)
    
print("@@@data_row_list")
print(data_row_list)    



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

check_count = 0

for a in range(1,len(data_col_list),2):
    for b in range(len(data_row_list)):
        if data[data_col_list[a]][data_row_list[b]] != 0:
            point_char = data[data_col_list[a]][data_row_list[b]][0]
            point_num = data[data_col_list[a]][data_row_list[b]][1:]
            if data_col_list[a] + str(data_row_list[b]) == data[point_char][int(point_num)] :
                check_count += 1
                print(str(check_count) + " : " + data_col_list[a] + str(data_row_list[b])+" 일치")
            if data_col_list[a] + str(data_row_list[b]) != data[point_char][int(point_num)] :
                check_count += 1
                print(str(check_count) + " : " + data_col_list[a] + str(data_row_list[b])  +" 불일치@@@@@@@@@@@@@@@@")

check_count2 = 0
for a in range(1,len(data_col_list),2):
    for b in range(len(data_row_list)):
        if data[data_col_list[a]][data_row_list[b]] != 0:
            point_char = data[data_col_list[a]][data_row_list[b]][0]
            point_low_char = point_char.lower()
            point_num = data[data_col_list[a]][data_row_list[b]][1:]
            if data[data_col_list[a-1]][data_row_list[b]] == data[point_low_char][int(point_num)] :
                check_count2 += 1
                print(str(check_count2) + " : " + data_col_list[a] + str(data_row_list[b])+" guage 일치")
            if data[data_col_list[a-1]][data_row_list[b]] != data[point_low_char][int(point_num)] :
                check_count2 += 1
                print(str(check_count2) + " : " + data_col_list[a] + str(data_row_list[b])  +" guage 불일치@@@@@@@@@@@@@@@@")
                
                
