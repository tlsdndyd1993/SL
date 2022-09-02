# class test:
    # def __init__(self):
        # self.test01(self.a)
        
a = {'A': ['B:0.5', 'D:1.25', 'F:0.5', 'G:0.5'], 'B': ['C:0.5'], 'C': [], 'D': [], 'F': [], 'G': []}
print(a.keys())
print(a.values())

print(list(a.keys()))
print(list(a.values())[0])
print(type(a.keys()))
print(type(a.values()))


for i in list(a.values()):
    print(i)
 



# row_rev02_dict = dict(zip(row_rev01,map((lambda x : x+1), row_rev01)))   #행 1씩 더한 딕셔너리 만들기