'''
Created on 2021. 4. 15.

@author: wooyong.shin
'''
import pandas as pd
import os
from openpyxl import load_workbook

class PIN_CHECKER:
    def __init__(self):
        #OCR, EXCEL path
        self.excel_path = 'C:/Users/wooyong.shin/Downloads/excel/'
        self.ocr_path = 'C:/Users/wooyong.shin/Downloads/ocr/'
        self.excel_file_name = '1111.xlsx'
        self.export_path = 'C:/Users/wooyong.shin/Downloads/cis_excel/'        
        self.del_char = ":"
        self.first_make_zero = ["nan","SPL","-"]
        self.data = self.readExel(self.excel_path + self.excel_file_name, 'Sheet1')        
        self.data_col_list = list(pd.DataFrame(self.data))
        self.data_row_list = []
        self.df_row_list = []
        self.convert_row_list(self.data)
        self.row_dic = dict(zip(self.df_row_list,self.data_row_list))
        self.data = self.data.rename(index = self.row_dic)
        self.rev_list = []
        self.all_rev_list = []
        self.data_pre_processing(self.data_col_list,self.data_row_list, self.data,self.del_char,self.first_make_zero)
        self.number_of_cases = []
        self.check_count = 0
        print(self.data)
        print("==================================PIN_MAP====================================")
        self.check_pin_map(self.data_col_list, self.data_row_list, self.data, self.check_count)
        print("==================================GUAGE=====================================")
        self.check_count2 = 0
        self.check_guage(self.data_col_list, self.data_row_list, self.data, self.check_count2)
        print("==================================COUNTINT_PIN=====================================")
        self.pre_con = []
        self.make_list_connecting_wiring(self.data_col_list, self.data_row_list, self.data, self.check_count2, self.pre_con)
        self.pre_con = sorted(self.pre_con)
        self.set_pre_con = sorted(list(set(self.pre_con)))
        print(self.pre_con)
        print(self.set_pre_con)
        print(len(self.set_pre_con))
        self.pin_count = 0
        self.pin_count_list =[]        
        self.dic_pre_con = {}
        self.count_connecting_each_pin(self.set_pre_con, self.pre_con, self.pin_count)
        print(self.pin_count_list)        
        print(self.dic_pre_con)
        print(list(self.dic_pre_con.keys())[0][0])
        print(list(self.dic_pre_con.keys())[0][3])
        
        self.d_set_pre_con = []
        # self.coincident_con_list(self.set_pre_con, self.d_set_pre_con)
        
        
#file path + file name
    def readExel(self, xlse_path, sheetName):
        xls_file = pd.ExcelFile(xlse_path)
        data = xls_file.parse(sheetName)
        return data     
       
    def convert_row_list(self, data):
        for i in range(len(data)):
            self.data_row_list.append(i+1)            
        for k in range(len(data)):
            self.df_row_list.append(k)
    
    ##data pre-processing
    def data_pre_processing(self, data_col_list, data_row_list, data, del_char, first_make_zero):
        for i in data_col_list:
            for j in data_row_list:
                 if str(data[i][j]).find(":") == 1:
                     blank1 = str(data[i][j])
                     blank1 = blank1.replace(del_char,"")
                     data[i][j] = blank1
                 for k in first_make_zero:
                     if str(data[i][j]).find(k) == 0:
                         data[i][j] = 0      
   
    def check_pin_map(self,data_col_list, data_row_list, data, check_count):
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
            
    def check_guage(self, data_col_list, data_row_list, data, check_count2):
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
                
    def make_list_connecting_wiring(self,data_col_list, data_row_list, data, check_count, pre_con):
        for a in range(1,len(data_col_list),2):
            for b in range(len(data_row_list)):
                if data[data_col_list[a]][data_row_list[b]] != 0:
                    point_char = data[data_col_list[a]][data_row_list[b]][0]
                    point_num = data[data_col_list[a]][data_row_list[b]][1:]
                    if data_col_list[a] + str(data_row_list[b]) == data[point_char][int(point_num)] :
                        check_count += 1
                        print(str(check_count) + " : " +data_col_list[a] + "->" + data[data_col_list[a]][data_row_list[b]][0])                        
                        pre_con.append(data_col_list[a] + "->" + data[data_col_list[a]][data_row_list[b]][0])
                                                
                         
                    if data_col_list[a] + str(data_row_list[b]) != data[point_char][int(point_num)] :
                        check_count += 1
                        print(str(check_count) + " : " + data_col_list[a] + str(data_row_list[b])  +" 불일치@@@@@@@@@@@@@@@@")

    def count_connecting_each_pin(self, set_pre_con, pre_con, pin_count):
        for set_con in set_pre_con:
            for con in pre_con:
                if set_con == con:
                    pin_count += 1
            self.pin_count_list.append(pin_count)
            pin_count = 0
        self.dic_pre_con = dict(zip(self.set_pre_con,self.pin_count_list))
        
    def coincident_con_list(self, set_pre_con, d_set_pre_con):
        d_set_pre_con = set_pre_con
        print(set_pre_con)
        for set_pc in set_pre_con:
            if set_pc[3]+"->"+set_pc[0] != set_pc:
                if set_pre_con.find(set_pc[3]+"->"+set_pc[0]) != -1:
                    del d_set_pre_con[d_set_pre_con.index(set_pc[3]+"->"+set_pc[0])]
        print(d_set_pre_con)
            # if set_pc[3]+"->"+set_pc[0] == set_pc:
                # del self.set_pre_con[self.set_pre_con.index(set_pc[3]+"->"+set_pc[0])] 
                
            
            
PIN_CHECKER()
