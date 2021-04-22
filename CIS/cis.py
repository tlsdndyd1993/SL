# -*- conding: utf-8 -*-
'''
Created on 2021. 3. 8.

@author: wooyong.shin
'''
import pandas as pd
import os
from openpyxl import load_workbook

class INS_CIS:
    def __init__(self, excel_file_name):
        self.excel_file_name = excel_file_name
        self.excel_path = 'C:/Users/wooyong.shin/Downloads/excel/'
        self.ocr_path = 'C:/Users/wooyong.shin/Downloads/ocr/'
        self.export_path = 'C:/Users/wooyong.shin/Downloads/cis_excel/'
        self.data = self.readExel(self.excel_path + self.excel_file_name, 'Sheet1')
        #make cis list
        self.cis_list = []
        #xlsx to list
        self.data_col = list(self.data.columns)
        #insert col into list
        self.list_data = self.data.values.tolist()
        self.list_data.insert(0,self.data_col)
        self.list_len = len(self.list_data)
        self.item_num = []
        self.align_len_and_append_to_cis_list(self.list_len, self.item_num, self.list_data, self.cis_list)
        self.product_name = []
        self.make_product_name_list(self.list_len, self.product_name, self.list_data)
        self.in2=[]
        self.make_in2_list(self.list_data, self.in2)
        self.erase_n_and_append(self.in2)
        self.pdn_in2 = []
        self.in2_list_to_product_name_list(self.list_len, self.pdn_in2, self.product_name, self.in2, self.cis_list)
        self.item_count = []
        self.item_count_to_cis_list(self.list_data, self.item_count, self.cis_list)
        self.df_f=[]
        self.df_atr=[]
        self.list_to_dataframe(self.cis_list, self.df_atr, self.df_f, self.export_path, self.excel_file_name)

        
#file path + file name
    def readExel(self, xlse_path, sheetName):
        xls_file = pd.ExcelFile(xlse_path)
        data = xls_file.parse(sheetName)
        return data

#align len of item_listr & append to cis_list
    def align_len_and_append_to_cis_list(self, list_len, item_num, list_data, cis_list):
        for i in range(list_len):
            item_num.append(list_data[i][1])
        n=0
        for j in item_num:
            if len(str(j)) != 8 and len(str(j)) <= 8:
                item_num[n] = str('0')*(8-len(str(j))) + str(item_num[n])
            n+=1
        cis_list.append(item_num)
        
#make product name list
    def make_product_name_list(self, list_len, product_name, list_data):
        for z in range(list_len):
            product_name.append(list_data[z][2])
        # print(product_name)

#make in2 list
    def make_in2_list(self, list_data, in2):
        for k in list_data:
            in2.append(k[4])
        for k in range(len(in2)):
            in2[k] = str(in2[k])   
    
# print("===erase in2's n & append===")
    def erase_n_and_append(self,in2):
        for l in range(len(in2)):
            if in2[l].find("\n") != -1:
                in2[l] = in2[l].replace("\n"," ")

#add in2_list to product_name_list
    def in2_list_to_product_name_list(self, list_len, pdn_in2, product_name, in2, cis_list):
        for y in range(list_len):
            pdn_in2.append(product_name[y] + " " + in2[y])
        # print(pdn_in2)
        cis_list.append(pdn_in2)

#append item_count to cis_list
    def item_count_to_cis_list(self, list_data, item_count, cis_list):
        for m in range(len(list_data)):
            if list_data[m][0] == "AR":
                item_count.append(1)
                continue
            item_count.append(list_data[m][0])
        cis_list.append(item_count)

#arrange cis_list(list to dataframe) & export excel file
    def list_to_dataframe(self, cis_list, df_atr, df_f, export_path, excel_file_name):
        for n in range(len(cis_list[0])):
            for o in range(3):
                df_atr.append(cis_list[o][n])
            df_f.append(df_atr)
            df_atr=[]
        df = pd.DataFrame(df_f, columns = ['item_list', 'in2', 'item_count'])
        df.to_excel(export_path + excel_file_name, excel_file_name)
        # df.to_excel(excel_writer, sheet_name, na_rep, float_format, columns, header, index, index_label, startrow, startcol, engine, merge_cells, encoding, inf_rep, verbose, freeze_panes, storage_options)
        
        
        
path = "C:/Users/wooyong.shin/Downloads/excel/"
file_list = os.listdir(path)

print ("file_list: {}".format(file_list[0]))

for g in range(len(file_list)):
    INS_CIS(file_list[g])



