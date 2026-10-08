#!/bin/sh
# 라즈베리파이 OS 에서 필요한 패키지 한 번에 설치
set -e
sudo apt install -y libportaudio2 python3-pygame python3-numpy flac python3-spidev python3-gpiozero
pip install sounddevice SpeechRecognition pypinyin --break-system-packages
echo "설치 완료! python3 main.py 로 실행하세요."
