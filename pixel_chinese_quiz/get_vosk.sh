#!/bin/sh
# 오프라인 중국어 음성인식(Vosk) 설치 — 인터넷 왕복이 없어서 인식이 빨라요
# 모델(약 42MB)을 게임 폴더에 풀어 두면 main.py 가 자동으로 사용합니다.
set -e
cd "$(dirname "$0")"
pip install vosk --break-system-packages
if [ ! -d vosk-model-small-cn-0.22 ]; then
  wget -O vosk-cn.zip https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
  unzip -q vosk-cn.zip && rm vosk-cn.zip
fi
echo "완료! 이제 python3 main.py (또는 ./run_lcd.sh) 실행하면 Vosk 로 인식합니다."
