import sys
import serial
import serial.tools.list_ports
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QComboBox, QMessageBox)
from PyQt5.QtCore import Qt

class PowerController(QWidget):
    def __init__(self):
        super().__init__()
        self.ser = None
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout()

        # --- 1. 포트 선택 섹션 ---
        port_layout = QHBoxLayout()
        self.port_combo = QComboBox()
        self.refresh_ports()
        
        self.btn_connect = QPushButton("연결")
        self.btn_connect.clicked.connect(self.toggle_connection)
        
        port_layout.addWidget(QLabel("통신 포트:"))
        port_layout.addWidget(self.port_combo)
        port_layout.addWidget(self.btn_connect)
        layout.addLayout(port_layout)

        # --- 2. 전원 제어 버튼 섹션 (아두이노 ISR 규격 대응) ---
        # (표시 이름, ON 명령, OFF 명령)
        self.controls = [
            ("B+ (Battery)", "1", "Q"),
            ("TG B+ (Target)", "2", "W"),
            ("ACC (Accessory)", "3", "E"),
            ("IGN (Ignition)", "4", "R")
        ]

        for name, on_cmd, off_cmd in self.controls:
            group = QHBoxLayout()
            label = QLabel(name)
            label.setFixedWidth(120)
            group.addWidget(label)
            
            btn_on = QPushButton("ON")
            btn_on.setMinimumHeight(40)
            # lambda를 사용하여 클릭 시 명령어를 시리얼로 전송
            btn_on.clicked.connect(lambda ch, cmd=on_cmd: self.send_command(cmd))
            
            btn_off = QPushButton("OFF")
            btn_off.setMinimumHeight(40)
            btn_off.clicked.connect(lambda ch, cmd=off_cmd: self.send_command(cmd))
            
            group.addWidget(btn_on)
            group.addWidget(btn_off)
            layout.addLayout(group)

        # --- 3. 전체 꺼짐 버튼 (비상용) ---
        self.btn_all_off = QPushButton("ALL POWER OFF (Emergency)")
        self.btn_all_off.setMinimumHeight(50)
        self.btn_all_off.setStyleSheet("""
            background-color: #e74c3c; 
            color: white; 
            font-weight: bold; 
            border-radius: 5px;
        """)
        self.btn_all_off.clicked.connect(lambda: self.send_command('0'))
        layout.addWidget(self.btn_all_off)

        # 기본 설정
        self.setLayout(layout)
        self.setWindowTitle('BLTN Test Rig - Power Controller (PyQt5)')
        self.resize(400, 350)

    def refresh_ports(self):
        """현재 연결된 COM 포트 목록 갱신"""
        ports = serial.tools.list_ports.comports()
        self.port_combo.clear()
        for port in ports:
            self.port_combo.addItem(port.device)

    def toggle_connection(self):
        """아두이노 연결/해제 토글"""
        if self.ser is None or not self.ser.is_open:
            try:
                port = self.port_combo.currentText()
                if not port:
                    QMessageBox.warning(self, "오류", "연결할 포트가 없습니다.")
                    return
                # 9600bps로 연결
                self.ser = serial.Serial(port, 9600, timeout=1)
                self.btn_connect.setText("연결 해제")
                self.btn_connect.setStyleSheet("background-color: #2ecc71; color: white;")
                print(f"Connected to {port}")
            except Exception as e:
                QMessageBox.critical(self, "연결 실패", f"포트를 열 수 없습니다: {e}")
        else:
            self.ser.close()
            self.btn_connect.setText("연결")
            self.btn_connect.setStyleSheet("")
            print("Disconnected")

    def send_command(self, cmd):
        """시리얼 명령 전송"""
        if self.ser and self.ser.is_open:
            self.ser.write(cmd.encode())
            print(f"전송 명령: {cmd}")
        else:
            QMessageBox.warning(self, "연결 확인", "아두이노가 연결되지 않았습니다.")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = PowerController()
    ex.show()
    # PyQt5는 exec_()를 사용합니다.
    sys.exit(app.exec_())