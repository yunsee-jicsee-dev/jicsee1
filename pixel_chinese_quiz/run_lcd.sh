#!/bin/sh
# ST7735S LCD 에 퀴즈 표시 (pygame 창은 키 입력 전용)
# 색/방향이 이상하면 뒤에 옵션 추가: --lcd-rgb  --lcd-invert  --lcd-flip  --lcd-offset 1,2
cd "$(dirname "$0")"
exec python3 main.py --lcd "$@"
