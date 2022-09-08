# class test:
    # def __init__(self):
        # self.test01(self.a)
        
a = {'A': ['B:0.5', 'D:1.25', 'F:0.5', 'G:0.5'], 'B': ['C:0.5'], 'C': [], 'D': [], 'F': [], 'G': []}


cc_d_attribute_set_dict = {'A': ['C:0.3', 'D:0.3', 'E:0.3', 'H:0.3', 'I:0.3', 'K:0.3'], 'B': ['D:0.3', 'F:0.3', 'H:0.3', 'I:0.3'], 'C': ['E:0.3', 'H:0.3', 'I:0.3'], 'D': ['D:0.3', 'E:0.3', 'H:0.3'], 'E': ['E:0.3', 'H:0.3'], 'H': ['I:0.3']}

all_length_dict = {'A': ['10', '20', '30', '40', '50', '60'], 'B': ['70', '80', '90', '10'], 'C': ['20', '30', '40'], 'D': ['50', '60', '70'], 'E': ['80', '90'], 'H': ['100']}

spl_dic = {'SPL:1': ['C:16', 'E:3', 'E:13'], 'SPL:2': ['C:17', 'E:2', 'E:8'], 'SPL:3': ['C:18', 'D:14', 'E:7'], 'SPL:4': ['C:19', 'D:4', 'D:13'], 'SPL:5': ['C:20', 'D:3', 'D:8'], 'SPL:6': ['A:14', 'E:18', 'M:1']}
print(len(spl_dic))


spl_main_dic = {}
spl_main = []
for i in range(len(spl_dic)):
    print(list(spl_dic.keys())[i] + "의 주선은 무엇인가요?")
    #주선 선택
    a, b = map(int,input().split())
    spl_main.append(a)
    spl_main.append(b)
    spl_main = sorted(spl_main)
    #주선 핀맵
    spl_main_1 = list(spl_dic.values())[i][spl_main[0]-1]
    spl_main_2 = list(spl_dic.values())[i][spl_main[1]-1]
    #주선 핀맵 마킹
    spl_main_1_m = list(spl_dic.values())[i][spl_main[0]-1][0:spl_main_1.find(":")]
    spl_main_2_m = list(spl_dic.values())[i][spl_main[1]-1][0:spl_main_2.find(":")]    
    
    print(list(spl_dic.keys())[i] + "의 주선은 " + spl_main_1 + " 과 " + spl_main_2 + " 입니다.")
    
    if 
    
    

    
    
    


#
# print(a.keys())
# print(a.values())
#
# print(list(a.keys()))
# print(list(a.values())[0])
# print(type(a.keys()))
# print(type(a.values()))
#
#
# for i in list(a.values()):
    # print(i)
    #



# row_rev02_dict = dict(zip(row_rev01,map((lambda x : x+1), row_rev01)))   #행 1씩 더한 딕셔너리 만들기