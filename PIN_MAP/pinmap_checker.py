'''
Created on 2021. 4. 15.

@author: wooyong.shin
'''
import pandas as pd
import os
from openpyxl import load_workbook
import copy
import warnings
from pandas.core.common import SettingWithCopyWarning
warnings.simplefilter(action="ignore", category=SettingWithCopyWarning)

class PIN_CHECKER:
    def __init__(self):
        self.excel_path = 'C:/Users/wooyong.shin/Desktop/'
        self.ocr_path = 'C:/Users/wooyong.shin/Downloads/ocr/'
        self.excel_file_name = 'pinmap.xlsx'
        self.export_path = 'C:/Users/wooyong.shin/Downloads/cis_excel/'        
        self.del_char = ":"
        self.first_make_zero = ["nan","SPL","-"]
        self.first_make_non_zero = ["SPL"]
        self.data = self.readExel(self.excel_path + self.excel_file_name, 'Sheet2')
        # print(self.data)    
        self.data_col_list = list(pd.DataFrame(self.data))
        # print("data_col_list")
        # print(self.data_col_list)
        self.data_row_list = []
        self.df_row_list = []
        self.convert_row_list(self.data)
        # print("data_row_list")
        # print(self.data_row_list)
        # print("df_row_list")
        # print(self.df_row_list)
        self.row_dic = dict(zip(self.df_row_list,self.data_row_list))
        # print(self.row_dic)
        self.data = self.data.rename(index = self.row_dic)
        
        self.data_org = copy.deepcopy(self.data)
        # print(self.data)
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
        
        print("==================================COLOR=====================================")
        self.check_color(self.data_col_list, self.data_row_list, self.data, self.check_count2)
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
        print("self.dic_pre_con")  
        print(self.dic_pre_con)
        print("len(self.dic_pre_con)")
        print(len(self.dic_pre_con))
        self.dict_list = []
        self.dict_set = 0
        self.matching_num_list = []
        self.key_match = {}        
        self.match_col(self.dic_pre_con)        
        self.dup_dic_pre_con = copy.deepcopy(self.dic_pre_con)
        self.wire_num = 0
        self.delete_overlap_pin()
        self.wire_lenth = 0
        self.wiring_type = []
        self.wiring_lenth = []
        self.specify_wiring_of_type()
        self.specify_wiring_of_lenth()
        self.print_cnt = 0
        self.print_wiring_bom()
        self.data_org_pre_processing(self.data_col_list, self.data_row_list, self.data_org, self.del_char, self.first_make_non_zero)
        self.spl_l = []
        self.spl_s = ()
        self.make_spl_list_s(self.data_col_list, self.data_row_list, self.data_org, self.check_count, self.pre_con)
        self.a_spl_l = []
        self.p_spl_l = []
        self.make_spl_list_p(self.data_col_list, self.data_row_list, self.data_org, self.check_count, self.pre_con, self.spl_l)
        self.spl_dic = dict(zip(self.spl_l,self.a_spl_l))
        # print(self.spl_dic)
        # self.choice_m_s1, self.choice_m_s2 = input("주선을 몇 번째? : ").split()
        # self.choice_m_s1 = int(self.choice_m_s1)
        # self.choice_m_s2 = int(self.choice_m_s2)
        # print(self.choice_m_s1)
        # print(self.choice_m_s2)
        # print(self.data_col_list)
        # print(self.data_row_list)
        print("a;sdkfjal;dksfh;aeskj;akjfsd;flkj")
        print(self.data_org)
        
        
        self.s_g_l = []
        self.s_l_l = []
        self.s_m_l = []
        self.main_line = []
        self.spl_w_bom = []
        self.a_spl_w_bom = []
        self.spl_count = 0
        print(self.spl_dic)
        
        self.input_main_spl_data(self.spl_dic)
        
        
        
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
        for a in range(2,len(data_col_list),3):
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
        for a in range(2,len(data_col_list),3):
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
        self.check_count2 = 0
        
        
    def check_color(self, data_col_list, data_row_list, data, check_count2):
        for a in range(2,len(data_col_list),3):
            for b in range(len(data_row_list)):
                if data[data_col_list[a]][data_row_list[b]] != 0:
                    point_char = data[data_col_list[a]][data_row_list[b]][0]
                    # print("asdkfjaskehflkajsdfljahselfjhalksjleajh")
                    # print(point_char)
                    point_low_char = point_char.lower()
                    point_num = data[data_col_list[a]][data_row_list[b]][1:]
                    if data[data_col_list[a-2]][data_row_list[b]] == data[point_low_char+"c"][int(point_num)] :
                        check_count2 += 1
                        print(str(check_count2) + " : " + data_col_list[a] + str(data_row_list[b])+" color 일치")
                        # print(" color 일치")
                    if data[data_col_list[a-2]][data_row_list[b]] != data[point_low_char+"c"][int(point_num)] :
                        check_count2 += 1
                        print(str(check_count2) + " : " + data_col_list[a] + str(data_row_list[b])  +" color 불일치@@@@@@@@@@@@@@@@")
                        # print(" guage 불일치@@@@@@@@@@@@@@@@")
                        
    def make_list_connecting_wiring(self,data_col_list, data_row_list, data, check_count, pre_con):
        for a in range(2,len(data_col_list),3):
            for b in range(len(data_row_list)):
                if data[data_col_list[a]][data_row_list[b]] != 0:
                    point_char = data[data_col_list[a]][data_row_list[b]][0]
                    point_num = data[data_col_list[a]][data_row_list[b]][1:]
                    if data_col_list[a] + str(data_row_list[b]) == data[point_char][int(point_num)] :
                        check_count += 1
                        print(str(check_count) + " : " + data_col_list[a] + "->" + data[data_col_list[a]][data_row_list[b]][0] + "[" + str(data[data_col_list[a-1]][data_row_list[b]])+"]")                        
                        self.pre_con.append(data_col_list[a] + "->" + data[data_col_list[a]][data_row_list[b]][0] + "[" + str(data[data_col_list[a-1]][data_row_list[b]])+"]")                         
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
        
    def match_col(self, dic_pre_con):
        for i in range(len(dic_pre_con)):
            self.dict_list.append(list(self.dic_pre_con.keys())[i][0])
            self.matching_num_list.append(i)        
        self.dict_set = sorted(set(self.dict_list))
        self.key_match = dict(zip(self.dict_set, self.matching_num_list))
        print("self.key_match")
        print(self.key_match)
    
    def delete_overlap_pin(self):
        for i in self.dup_dic_pre_con:
            if self.key_match[i[0]] > self.key_match[i[3]]:
                del self.dic_pre_con[i]
        print("==>pin map match<==")
        print(self.dic_pre_con)
        
        for j in self.dic_pre_con.values():
            self.wire_num += j
        print("=>" + str(self.wire_num) + "개 wire 필요")
        print(len(self.dic_pre_con))
            
    def specify_wiring_of_type(self): 
        self.wiring_type = list(map(str,input("WIRING TYPE : ").split()))
        print(self.wiring_type)
        if len(self.wiring_type) != len(self.dic_pre_con):
            print("WIRING TYPE 을 다시 입력하세요.")
            self.specify_wiring_of_type()
    
    def specify_wiring_of_lenth(self):       
        self.wiring_lenth = list(map(str,input("WIRING LENTH : ").split()))
        if len(self.wiring_lenth) != len(self.dic_pre_con):
            print("아직 적지 않은 wiring 길이가 있습니다. 다시 입력하세요.")
            self.specify_wiring_of_lenth()
        print(self.wiring_lenth)
        
    def print_wiring_bom(self):
        for i in range(len(self.dic_pre_con)):
            for j in range(list(self.dic_pre_con.values())[i]):
                # print(self.wiring_type[self.print_cnt] + " " + str(list(self.dic_pre_con.keys())[i][5:-1]) + "mm2" + str(self.wire_lenth[self.print_cnt]) + "mm")
                print(self.wiring_type[self.print_cnt] + " " + str(list(self.dic_pre_con.keys())[i][5:-1]) + "mm2" + " " + self.wiring_lenth[self.print_cnt] + "mm")
            self.print_cnt += 1 
            
    #SPL 을 제외한 data 0으로 만들기
    def data_org_pre_processing(self, data_col_list, data_row_list, data_org, del_char, first_make_non_zero):
        for a in range(2,len(data_col_list),3):
            for b in range(len(data_row_list)):                
                if str(data_org[data_col_list[a]][data_row_list[b]]).find('SPL') != 0:                    
                    data_org[data_col_list[a]][data_row_list[b]] = 0
                    data_org[data_col_list[a].lower()][data_row_list[b]] = 0
                    data_org[data_col_list[a].lower()+'c'][data_row_list[b]] = 0
        print(data_org)

    #0이 아닌 spl list 담기
    def make_spl_list_s(self,data_col_list, data_row_list, data_org, check_count, pre_con):
        for a in range(2,len(data_col_list),3):
            for b in range(len(data_row_list)):
                if data_org[data_col_list[a]][data_row_list[b]] != 0:
                    self.spl_l.append(data_org[data_col_list[a]][data_row_list[b]])
                    self.spl_s = set(self.spl_l)
                    self.spl_l = sorted(list(self.spl_s))
        print(self.spl_l)

    def make_spl_list_p(self,data_col_list, data_row_list, data_org, check_count, pre_con, spl_l):
        for i in spl_l:
            for a in range(2,len(data_col_list),3):
                for b in range(len(data_row_list)):
                    if data_org[data_col_list[a]][data_row_list[b]] != 0:    
                        if data_org[data_col_list[a]][data_row_list[b]] == i:
                            self.p_spl_l.append(data_col_list[a] + str(data_row_list[b]))
            self.a_spl_l.append(self.p_spl_l)
            self.p_spl_l = [] 
    
    def input_main_spl_data(self,spl_dic):
        for key, value in self.spl_dic.items():
            print(key + " : ", end='')
            print(value)
            print(key + "의 주선은 몇 번째 입니까? : ", end='')
            choice_m_s1, choice_m_s2 = input().split()
            choice_m_s1 = int(choice_m_s1)
            choice_m_s2 = int(choice_m_s2)
            
            f_s = value[choice_m_s1-1]
            s_s = value[choice_m_s2-1]
            
            f_s_c_u = f_s[0]
            f_s_c_l = f_s_c_u.lower()
            f_s_n = int(f_s[1:])
            # print(f_s_c_l)
            # print(f_s_n)
            # print(type(f_s_n))
            
            s_s_c_u = s_s[0]
            s_s_c_l = s_s_c_u.lower()
            s_s_n = int(s_s[1:])
            # print(s_s_c_l)
            # print(s_s_n)            
            
            if self.data_org[f_s_c_l][f_s_n] == self.data_org[s_s_c_l][s_s_n]:
                print("<<<@^ ㅅ^)/~주선의 gauge가 같습니다~>>>")               
                self.s_g_l.append(str(self.data_org[f_s_c_l][f_s_n]) + "mm2")
                print(key+" 주선 gauge : ", end = '')
                print(self.s_g_l)
                
                print(key, end="")
                self.s_m_l.append(input(" 주선의 재질을 입력하세요 : ").upper())
                print(key + " 주선의 재질 : ", end='')
                print(self.s_m_l)
                
                print(key, end="")
                self.s_l_l.append(input(" 주선의 길이를 입력하세요 : ") + "mm")
                print(key + "주선의 길이 : ", end='')
                print(self.s_l_l)
                print("")
                
                self.spl_w_bom.append(self.s_m_l[self.spl_count] + " " + self.s_g_l[self.spl_count] + " " + self.s_l_l[self.spl_count])
                print(self.spl_w_bom)
                
                print("선택된 주선 : ",end = "")
                self.main_line.append(f_s)
                self.main_line.append(s_s)
                self.main_line = set(self.main_line)
                self.main_line = list(self.main_line)
                
                print(self.main_line)
                for i in self.main_line:
                    value.remove(i)
                print("남은 지선 : ", end ="")
                print(value)                
                
                self.spl_count += 1
                
                for j in value:
                    print("j", end= '')
                    print(j[0].lower())
                    print(self.data_org[j[0].lower()][int(j[1:])])
                    self.s_g_l.append(str(self.data_org[j[0].lower()][int(j[1:])]) + "mm2")
                    # print("지선의 gauge 는 :", end ="")
                    # print(self.s_g_l)
                    print(key + " 지선 " + j + "의 재질을 입력하세요 : ", end = '')
                    self.s_m_l.append(input().upper())
                    print(key + " 주선 + 지선 재질", end ='')
                    print(self.s_m_l)
                    print()
                    
                    print(key + " 지선 " + j + "의 길이를 입력하세요 : ", end = '')
                    self.s_l_l.append(input() + "mm")
                    print(key + " 주선 + 지선 길이", end ='')
                    print(self.s_l_l)
                    print("카운트 : " + str(self.spl_count))
                    print(self.s_m_l)
                    print(self.s_l_l)
                    print(self.s_g_l)
                    self.spl_w_bom.append(self.s_m_l[self.spl_count] + " " + self.s_g_l[self.spl_count] + " " + self.s_l_l[self.spl_count])
                    print(self.spl_w_bom)
                    self.spl_count += 1
                
                for k in self.spl_w_bom:
                    print(k)
                # print(self.s_m_l)                     
                self.main_line = []
                print("")
                
            else:
                print("@@@주선의 gauge가 다릅니다@@@")
                print("====>" + f_s + " : " + str(self.data_org[f_s_c_l][f_s_n]) + " // " + s_s + " : " + str(self.data_org[s_s_c_l][s_s_n]))
                # self.input_main_spl_data(self.spl_dic)
                w_s_g = input("주선의 gauge를 입력하세요 : ")
                w_s_g = str(w_s_g)
                self.s_g_l.append(w_s_g + "mm2")
                print(key+" 주선 gauge : ", end = '')
                print(self.s_g_l)
                
                print(key, end="")
                self.s_m_l.append(input(" 주선의 재질을 입력하세요 : ").upper())
                print(key + " 주선의 재질 : ", end='')
                print(self.s_m_l)
                
                print(key, end="")
                self.s_l_l.append(input(" 주선의 길이를 입력하세요 : ") + "mm")
                print(key + "주선의 길이 : ", end='')
                print(self.s_l_l)
                print("")
                
                self.spl_w_bom.append(self.s_m_l[self.spl_count] + " " + self.s_g_l[self.spl_count] + " " + self.s_l_l[self.spl_count])
                print(self.spl_w_bom)
                
                print("선택된 주선 : ",end = "")
                self.main_line.append(f_s)
                self.main_line.append(s_s)
                self.main_line = set(self.main_line)
                self.main_line = list(self.main_line)
                
                print(self.main_line)
                for i in self.main_line:
                    value.remove(i)
                print("남은 지선 : ", end ="")
                print(value)                
                
                self.spl_count += 1
                
                for j in value:
                    print("j", end= '')
                    print(j[0].lower())
                    print(self.data_org[j[0].lower()][int(j[1:])])
                    self.s_g_l.append(str(self.data_org[j[0].lower()][int(j[1:])]) + "mm2")
                    # print("지선의 gauge 는 :", end ="")
                    # print(self.s_g_l)
                    print(key + " 지선 " + j + "의 재질을 입력하세요 : ", end = '')
                    self.s_m_l.append(input().upper())
                    print(key + " 주선 + 지선 재질", end ='')
                    print(self.s_m_l)
                    print()
                    
                    print(key + " 지선 " + j + "의 길이를 입력하세요 : ", end = '')
                    self.s_l_l.append(input() + "mm")
                    print(key + " 주선 + 지선 길이", end ='')
                    print(self.s_l_l)
                    print("카운트 : " + str(self.spl_count))
                    print(self.s_m_l)
                    print(self.s_l_l)
                    print(self.s_g_l)
                    self.spl_w_bom.append(self.s_m_l[self.spl_count] + " " + self.s_g_l[self.spl_count] + " " + self.s_l_l[self.spl_count])
                    print(self.spl_w_bom)
                    self.spl_count += 1
                    
                for k in self.spl_w_bom:
                    print(k)
                # print(self.s_m_l)                     
                self.main_line = []
                print("")               
            

PIN_CHECKER()
