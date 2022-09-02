'''
Created on 2021. 9. 17.

@author: wooyong.shin
'''
class TEST:
    
    bag = []
    
    def __init__(self):
        self.test_1 = 0
        
    def greeting(self):
        self.name = "헷"
        print(self.name)
    def test_call(self,call):
        self.bag.append(call)
        
testtt = TEST()
testtt.test_call(1)
testttt = TEST()
testttt.test_call(2)
print(testtt.bag)
    
wooyong = TEST()
wooyong.greeting()
wooyong.age = 10
print(wooyong.age)
