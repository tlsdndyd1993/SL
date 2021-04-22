# -*- conding: utf-8 -*-
'''
Created on 2021. 3. 8.

@author: wooyong.shin
'''
import pandas as pd
import os
from openpyxl import load_workbook

#OCR, EXCEL path
excel_path = 'C:/Users/wooyong.shin/Downloads/excel/'
ocr_path = 'C:/Users/wooyong.shin/Downloads/ocr/'
excel_file_name = 'RR_STD.xlsx'
export_path = 'C:/Users/wooyong.shin/Downloads/cis_excel/'

#file path + file name
def readExel(xlse_path, sheetName):
    xls_file = pd.ExcelFile(xlse_path)
    data = xls_file.parse(sheetName)
    return data
 
data = readExel(excel_path + excel_file_name, 'Sheet1')

#make cis list
cis_list = []

#xlsx to list
data_col = list(data.columns)
list_data = data.values.tolist()

#insert col into list
list_data.insert(0,data_col)

print("===data===")
print(list_data)

#list len
list_len = len(list_data)

#align len of item number & append num data to cis
item_num = []
for i in range(list_len):
    item_num.append(list_data[i][1])
n=0
for j in item_num:
    if len(str(j)) != 8 and len(str(j)) <= 8:
        item_num[n] = str('0')*(8-len(str(j))) + str(item_num[n])
    n+=1
cis_list.append(item_num)

print("===product_name===")
product_name = []
for z in range(list_len):
    product_name.append(list_data[z][2])
print(product_name)


print("====in2====")
in2=[]
for k in list_data:
    in2.append(k[4])
print(in2)


for k in range(len(in2)):
    in2[k] = str(in2[k])   
    
print("===erase in2's n & append===")
for l in range(len(in2)):
    if in2[l].find("\n") != -1:
        in2[l] = in2[l].replace("\n"," ")

print("===product_name + in2===")
pdn_in2 = []
for y in range(list_len):
    pdn_in2.append(product_name[y] + " " + in2[y])
print(pdn_in2)
cis_list.append(pdn_in2)

print("===item_count append===")
item_count = []
for m in range(len(list_data)):
    if list_data[m][0] == "AR":
        item_count.append(1)
        continue
    item_count.append(list_data[m][0])
cis_list.append(item_count)
print(item_count)

print("===cis_list===")
print(cis_list)

df_f=[]
df_atr=[]

#list to dataframe
for n in range(len(cis_list[0])):
    for o in range(3):
        df_atr.append(cis_list[o][n])
    df_f.append(df_atr)
    df_atr=[]
print(df_f)
df = pd.DataFrame(df_f, columns = ['item_list', 'in2', 'item_count'])
print(df)

#export excel file
df.to_excel(export_path + excel_file_name, excel_file_name)