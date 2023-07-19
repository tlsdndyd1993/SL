import win32com.client

catia = win32com.client.Dispatch('CATIA.Application')
drawing_document = catia.Documents.Open(r"V:\GPT\Drawing1.CATDrawing")

sheet = drawing_document.Sheets.Item(1)  # 첫 번째 시트 선택
factory2D = sheet.Factory2D

line = factory2D.CreateLine(0, 0, 50, 50)