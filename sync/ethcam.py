import cv2
import time

def start_crevis_stream():
    # 1. 카메라 설정
    # 크레비스 카메라는 보통 'rtsp://아이피/video1' 형식을 지원합니다.
    # 만약 IP 설정 도구로 카메라 IP를 192.168.1.100으로 맞췄다면 아래와 같이 입력합니다.
    cam_ip = "192.168.1.100" 
    mac_address = "00:14:F7:01:81:9E"
    
    # 산업용 카메라의 기본 스트림 경로 (모델에 따라 /video1 또는 /stream)
    stream_url = f"rtsp://{cam_ip}/video1"

    print(f"카메라 연결 시도 중... (MAC: {mac_address})")
    
    # 2. 비디오 캡처 객체 생성
    cap = cv2.VideoCapture(stream_url)
    
    # 연결 안정성을 위해 약간의 대기 시간을 줍니다.
    time.sleep(2)

    if not cap.isOpened():
        print("에러: 카메라를 열 수 없습니다.")
        print("1. 노트북 IP가 192.168.1.2 로 설정되었는지 확인")
        print("2. 카메라 전원 및 랜선 연결(빨간 X) 확인")
        print("3. 크레비스 전용 툴(CzPylon)에서 IP가 192.168.1.100인지 확인")
        return

    print("연결 성공! 영상을 송출합니다. (종료: 'q' 키)")

    # 3. 실시간 영상 송출 루프
    while True:
        ret, frame = cap.read()

        if not ret:
            print("영상을 수신할 수 없습니다. 재연결 시도 중...")
            break

        # 화면에 출력
        cv2.imshow('CREVIS GigE Camera Stream', frame)

        # 'q'를 누르면 종료
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    # 자원 해제
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    start_crevis_stream()