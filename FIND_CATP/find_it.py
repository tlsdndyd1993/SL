'''
Created on 2021. 4. 29.

@author: wooyong.shin
'''
import glob
import pandas as pd

excel_path = 'C:/Users/wooyong.shin/Downloads/excel/'
ocr_path = 'C:/Users/wooyong.shin/Downloads/ocr/'
excel_file_name = '111111.xlsx'
export_path = 'C:/Users/wooyong.shin/Downloads/cis_excel/'    

targetPattern = r"C:\Users\wooyong.shin\Downloads\01. 커넥터 3D - 복사본\히로세\도면\*.CATPart"
catp = glob.glob(targetPattern)
len_t = len(targetPattern)-9
print(catp)
print(catp[0][len_t:])

catp_list = []
for a in catp:
    catp_list.append(a[len_t:])
    
print(catp_list)
df = pd.DataFrame(catp_list)
print(df)
print(len(targetPattern))

df.to_excel(export_path + excel_file_name, excel_file_name)