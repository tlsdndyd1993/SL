'''
Created on 2022. 3. 11.

@author: wooyong.shin
'''
import pandas as pd
import os
from openpyxl import load_workbook
import copy
import warnings
from pandas.core.common import SettingWithCopyWarning
from itertools import count
from numpy.distutils.lib2def import DATA_RE
warnings.simplefilter(action="ignore", category=SettingWithCopyWarning)
# pd.set_option('display.max_columns',None)

class PINMAP_CHECKER_REV0:
    def __init__(self):
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■  data 가공  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■  data 가공  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print()
        #1 파일 읽어오기
        self.excel_path = 'C:/Users/wooyong.shin/Desktop/'
        self.excel_file_name = 'pinmap.xlsx'
        self.origin_data = pd.read_excel(self.excel_path + self.excel_file_name,sheet_name="Sheet4")
        print("★★★★self.origin_data★★★★")
        print(self.origin_data)        

        #2 origin data 가공      
        self.origin_column = list(pd.DataFrame(self.origin_data))
        print("★★★★self.origin_column★★★★")        
        print(self.origin_column)
        #2-1 self.origin_data의 Unnamed: column 지우기    
        self.column_rev01 = self.column_processing(self.origin_column)
        print("★★★★self.column_rev01★★★★")
        print(self.column_rev01)
        #2-2-1 self.row_rev01 리스트 만들기
        print("★★★★self.len_row★★★★")
        self.len_row = len(self.origin_data)
        print(self.len_row)
        self.row_rev01 = self.row_processing(self.len_row)
        print("★★★★self.row_rev01★★★★")
        print(self.row_rev01)
        #2-3 프로세싱 된 self.data_rev01 출력 (self.column_rev01 에 속한 열만 출력, 행은 모두 출력)
        self.data_rev01 = self.origin_data.loc[:,self.column_rev01]
        print("★★★★self.data_rev01★★★★")
        print(self.data_rev01)
        #2-4 self.data_rev01 의 열이 모두 'NAN'인 열을 없애고 행 번호 변경
        self.row_rev02, self.data_rev03 = self.delete_nan_column(self.data_rev01, self.row_rev01, self.column_rev01, self.len_row)
        self.column_rev02 = list(self.data_rev03)
        print("★★★★self.row_rev02★★★★")
        print(self.row_rev02)        
        print("★★★★self.column_rev02★★★★")
        print(self.column_rev02)
        print("★★★★self.data_rev03★★★★")
        print(self.data_rev03)
        self.marking, self.data_rev04, self.data_rev05 = self.delete_spl(self.data_rev03, self.column_rev02, self.row_rev02)
        print("★★★★self.marking★★★★")
        print(self.marking)
        #NAN SPL
        print("★★★★self.data_rev04★★★★")
        print(self.data_rev04)
        #SPL
        print("★★★★self.data_rev05★★★★")
        print(self.data_rev05)
        print()
        #SPL LIST

        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■  data 가공  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■  data 가공  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print()
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■  결선 확인  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■  결선 확인  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")  
        print()
        #핀맵 확인
        self.check_pinmap(self.marking, self.data_rev04, self.column_rev02, self.row_rev02)
        
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■  결선 확인  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■  결선 확인  ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print()  
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■ splice 확인 ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■ splice 확인 ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        #SPL 확인
        self.spl_list=[]
        self.spl_set_list, self.spl_num_list_set, self.spl_dic = self.arrange_spl_data(self.marking, self.data_rev05, self.column_rev02, self.row_rev02, self.spl_list)
        print("★★★★self.spl_set_list★★★★")
        print(self.spl_set_list)
        print("★★★★self.spl_num_list_set★★★★")
        print(self.spl_num_list_set)
        print("★★★★self.spl_dic★★★★")
        print(self.spl_dic)
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■ splice 확인 ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■ splice 확인 ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print()
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■   전선cis   ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■   전선cis   ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        #CIS 용 전선 개수 프린트        
        self.marking_dict = self.make_marking_dict(self.marking)
        print("★★★★self.marking_dict★★★★")
        print(self.marking_dict)        
        self.d_attribute_set_dict, self.c_d_attribute_set_dict = self.guage_list_by_marking(self.marking, self.data_rev04, self.column_rev02, self.row_rev02, self.marking_dict)
        print("★★★★self.d_attribute_set_dict★★★★")
        print(self.d_attribute_set_dict)
        print("★★★★self.c_d_attribute_set_dict★★★★")
        print(self.c_d_attribute_set_dict)

        
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■   전선cis   ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        print("■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■   전선cis   ■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■")
        #와이어 결선 개수 프린트
        self.all_wire_count_list = self.wire_list(self.c_d_attribute_set_dict, self.data_rev04, self.column_rev02, self.row_rev02, self.marking_dict, self.marking)
        print("★★★★self.all_wire_count_list★★★★")
        print(self.all_wire_count_list)
        self.wiring_type = self.specify_wiring_of_type(self.c_d_attribute_set_dict)
        print("★★★★self.wiring_type★★★★")
        print(self.wiring_type)
        # self.print_wire_list(self.all_wire_count_list, self.c_d_attribute_set_dict)
        
        
        print(len(self.data_rev04['A']))
        print(len(self.data_rev04['B']))
        print(len(self.data_rev04['C']))

        

################################################################################################################################################################################################
################################################################################################################################################################################################
################################################################################################################################################################################################
################################################################################################################################################################################################


    #2-1 Unnamed: column 지우기 (열 인덱스 글자수 6이하인 orgin_data만 출력하기)
    def column_processing(self,origin_column):
        number_column = 0
        for i in range(len(origin_column)):
            if len(origin_column[i]) <= 6:
                number_column += 1
        return self.origin_column[:number_column]    
    #2-2-1 self.row_rev01 리스트 만들기
    def row_processing(self,len_row):
        row_rev01 = []
        for i in range(len_row):
            row_rev01.append(i)
        return row_rev01
    #2-4 self.data_rev01 의 열이 모두 'NAN'인 열을 없애기       
    def delete_nan_column(self,data_rev01, row_rev01, column_rev01, len_row):
        zero_count = 0
        zero_list = []
        zero_dic_values_list = []
        zero_dic_keys_list = []
        data_rev02 = data_rev01.fillna('0')
        print("★★★★data_rev02★★★★")
        print(data_rev02)
        #각각의 열이 0이 몇 번 나오는지에 대한 dictionary 만들기(요소가 전부 0인 열을 찾기위함)
        for i in column_rev01:
            for j in row_rev01:
                if data_rev02[i][j] == '-':
                    data_rev02[i][j] = '0'                
                if data_rev02[i][j] == '0':
                    zero_count += 1
            zero_list.append(zero_count)
            zero_count = 0                
        zero_dic = dict(zip(column_rev01,zero_list))
        print("★★★★zero_dic★★★★")
        print(zero_dic)
        print("★★★★zero_dic.values()★★★★")
        print(zero_dic.values())
        #열이 전부 0인 열의 keys, values 리스트 만들기
        for k in range(len(list(zero_dic.values()))):
            if list(zero_dic.values())[k] == len_row:
                zero_dic_values_list.append(k) 
        print("★★★★zero_dic_values_list★★★★")
        print(zero_dic_values_list)        
        for l in list(zero_dic_values_list):
            zero_dic_keys_list.append(list(zero_dic.keys())[l])
        print("★★★★zero_dic_keys_list★★★★")
        print(zero_dic_keys_list)
        #data_rev02 중 열이 모두 0인 열 제거
        for m in range(len(zero_dic_keys_list)):
            data_rev02 = data_rev02.drop(zero_dic_keys_list[m], axis = 1)
        #행 +1씩 재설정할 dictionary 생성
        row_rev02_dict = dict(zip(row_rev01,map((lambda x : x+1), row_rev01)))
        print("★★★★row_rev02_dict★★★★")
        print(row_rev02_dict)
        #data_rev02 의 key(=index)를 +1하여 변경
        data_rev02.rename(index = row_rev02_dict, inplace = True)                         
        data_rev03 = data_rev02                        
        return list(row_rev02_dict.values()), data_rev03        
    #3 커넥터 스펠링 표시 리스트 만들기, SPL관련 DATA 재가공하기
    def delete_spl(self,data_rev03,column_rev02,row_rev02):
        data_rev03_1 = data_rev03.copy(deep=True)
        data_rev03_2 = data_rev03.copy(deep=True)
        marking = []
        for i in data_rev03:
            if len(i) == 1:
                marking.append(i)
        #SPL인 것과 아닌 것을 data_rev03, data_re03_1에 각각 할당하기
        #SPL 0으로 만들기
        for ii in marking:
            for jj in row_rev02:
                if data_rev03_1[ii][jj].find('SPL') != -1:
                    data_rev03_1[ii][jj] = '0'
        #SPL 를 제외한 MARINKG 0 으로 만들기
        for iii in marking:
            for jjj in row_rev02:
                if data_rev03_2[iii][jjj].find('SPL') == -1:
                    data_rev03_2[iii][jjj] = '0' 
                    
        return marking, data_rev03_1, data_rev03_2
    #NAN SPL(ONLY MARKING) 결선 확인
    def check_pinmap(self,marking,data_rev04,column_rev02,row_rev02):
        resulf_pinmap = 0
        for j in marking:
            for k in row_rev02:
                # SPL 제거한 DATA 핀맵 확인
                colon_index = data_rev04[j][k].find(':')                
                # 요소의 marking과 pin_num 가공 추출
                if colon_index != -1:
                    #포인터 값
                    matching_marking = data_rev04[j][k][0:colon_index]
                    matching_pin_num = data_rev04[j][k][colon_index+1:]
                    #핀맵이 서로 맞는지 확인(포인터 값 == 마킹 값)
                    if data_rev04[matching_marking][int(matching_pin_num)] == j + ":" + str(k):
                        print("▼▼▼▼▼▼▼▼▼▼▼▼  " + j + ":" + str(k) + '<-->' + matching_marking + ":" + matching_pin_num + '(O)'+ "  ▼▼▼▼▼▼▼▼▼▼▼▼")
                        #결선이 맞다는 전제에서 펑션,직경,색상이 맞는지 확인
                        if data_rev04[j+"_F"][int(k)]  == data_rev04[matching_marking+"_F"][int(matching_pin_num)]:
                            print("-" + data_rev04[j+"_F"][int(k)] + " (O)" )
                        else :
                            print("펑션 불일치◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈◈")
                        if data_rev04[j+"_D"][int(k)]  == data_rev04[matching_marking+"_D"][int(matching_pin_num)]:
                            print("-" + str(data_rev04[j+"_D"][int(k)]) + " (O)") 
                        else : 
                            print("직경 불일치▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦▦")
                        if data_rev04[j+"_C"][int(k)]  == data_rev04[matching_marking+"_C"][int(matching_pin_num)]:
                            print("-" + data_rev04[j+"_C"][int(k)] + " (O)")
                        else:
                            print("색상 불일치♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬♬")
                    else:
                        print(j + ":" + str(k) + '<-->' + matching_marking + ":" + matching_pin_num + '(xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)') 
                        print("펑션 확인 필요~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
                        print("직경 확인 필요~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
                        print("색상 확인 필요~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        print()
    #SPL 딕셔너리 만들기
    def arrange_spl_data(self,marking,data_rev05,column_rev02,row_rev02,spl_list):
        #SPL에 대한 리스트 만들기
        spl_set_list = []
        for i in marking:
            for j in row_rev02:
                if data_rev05[i][j].find('SPL') != -1:
                    spl_list.append(data_rev05[i][j])
        spl_set_list = sorted(list(set(spl_list)))
        #SPL 번호별 해당 핀맵 딕셔너리 만들기
        spl_num_list = []
        spl_num_list_set = []
        for ii in spl_set_list:
            for jj in marking:
                for kk in row_rev02:
                     if ii == data_rev05[jj][kk]:
                         spl_num_list.append(jj +":"+ str(kk))
            spl_num_list_set.append(spl_num_list)
            spl_num_list = []
        spl_dic = dict(zip(spl_set_list,spl_num_list_set))
        return spl_set_list, spl_num_list_set, spl_dic
    
    #마킹 별 내림차순으로 순서 정하기
    def make_marking_dict(self,marking):
        #마킹별 순서 할당된 딕셔너리 만들기
        marking_list = list(map((lambda x : x+1), list(range(len(marking)))))
        marking_dict = dict(zip(marking,marking_list))
        return marking_dict
    
    #마킹별 결선 리스트 만들기
    def guage_list_by_marking(self,marking,data_rev04,column_rev02,row_rev02,marking_dict):
        #maring 끼리의 결선관련, gauge별로 몇 개씩 연결되어있는지 확인
        attribute_list = []
        attribute_set_list = []
        d_attribute_set_list = []       
        d_attribute_set_dict = {}                
        #marking(gauge) 의 sorted set 생성 -> marking별 sorted gauge set 딕셔너리 생성. (요소들과 sorted set을 비교하기 위함 -> gauge 별 결선 개수 측정.)
        for i in marking:
            for j in row_rev02:                
                if data_rev04[i][j] != '0':
                    attribute_list.append(data_rev04[i][j][0] + ":" + str(data_rev04[i+"_D"][j]))            
            attribute_set_list = sorted(list(set(attribute_list)))
            d_attribute_set_list.append(attribute_set_list)
            attribute_list = []
            attribute_set_list = []
        #self.d_attribute_set_dict 에서 중복 결선을 없애기 위한 작업.
        c_d_attribute_set_list = copy.deepcopy(d_attribute_set_list)  #결선에 대한 중복 값을 없애기 위한 리스트
        m_count = 1 #marking 카운트
        m_l_count = 0 #marking별 결선 리스트에 대한 카운트
        for ii in d_attribute_set_list:
            for jj in ii:
                if m_count > int(marking_dict[jj[0]]):
                    c_d_attribute_set_list[m_count-1].pop(m_l_count)
                m_l_count += 1
            m_l_count = 0
            m_count += 1
        m_count = 0            
        #결선 dictionary
        d_attribute_set_dict = dict(zip(marking,d_attribute_set_list))
        c_d_attribute_set_dict = dict(zip(marking,c_d_attribute_set_list))
        print(list(data_rev04['A']))
        return d_attribute_set_dict, c_d_attribute_set_dict
    #와이어 결선 개수 프린트
    def wire_list(self,c_d_attribute_set_dict,data_rev04,column_rev02,row_rev02,marking_dict,marking):
        wire_count = 0
        wire_count_list = []
        all_wire_count_list = []        
        for i in marking:
            for j in c_d_attribute_set_dict[i]:
                for k in range(len(data_rev04[i])):
                    #c_d_attribute_set_dict 요소와 같으면 +1 을 해라
                    if j == data_rev04[i][k+1][0] + ":" + str(data_rev04[i+"_D"][k+1]):
                        wire_count += 1
                wire_count_list.append(wire_count)
                wire_count = 0
            all_wire_count_list.append(wire_count_list)
            wire_count_list = []
        return all_wire_count_list
    #와이어링 재질 리스트 입력
    def specify_wiring_of_type(self,c_d_attribute_set_dict): 
        wiring_type = list(map(str,input("WIRING TYPE : ").upper().split()))
        print(wiring_type)
        if len(wiring_type) != len(c_d_attribute_set_dict):
            print("WIRING TYPE 을 다시 입력하세요.")
            self.specify_wiring_of_type()
        return wiring_type

    # def print_wire_list(self,all_wire_count_list,c_d_attribute_set_dict):
        # wiring_type = list(map(str,input("WIRING TYPE : ").split()))
        # for i in range(len(c_d_attribute_set_dict.values())):
            # for j in range(len(i)):
                # for k in range(j):
                    # print()
                
PINMAP_CHECKER_REV0()



# data_rev03_1 = data_rev03.copy(deep=True)                                #pandas deep copy
# data_rev02 = data_rev01.fillna(0)                                        #0으로 채우기
# data_rev02 = data_rev02.drop(zero_dic_keys_list[m], axis = 1)            #특정 열 지우기
# data_rev02.rename(index = row_rev02_dict, inplace = True)                #key 이름 변경
# row_rev02_dict = dict(zip(row_rev01,map((lambda x : x+1), row_rev01)))   #행 1씩 더한 딕셔너리 만들기