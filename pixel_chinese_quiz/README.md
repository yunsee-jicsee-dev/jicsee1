# 像素中文 · 픽셀 중국어 퀴즈

파이썬(pygame)으로 만든 도트 그래픽 중국어 레슨 퀴즈입니다. USB 마이크로 직접 발음해서 맞히는 **말하기 연습** 모드가 있습니다.

## 설치 & 실행

```bash
cd pixel_chinese_quiz
pip install -r requirements.txt
python main.py
```

> Linux에서 `sounddevice`가 PortAudio를 못 찾으면 `sudo apt install libportaudio2`

### 라즈베리파이

라즈베리파이 OS는 그냥 `pip install` 하면 막혀서(externally-managed-environment) 음성인식 패키지가 빠질 수 있어요. 아래 한 줄로 설치하세요:

```bash
./install_pi.sh
# 또는 직접: pip install SpeechRecognition pypinyin sounddevice --break-system-packages
```

## 모드

| 모드 | 내용 |
|---|---|
| 뜻 맞히기 | 한자 + 병음을 보고 한국어 뜻 4지선다 |
| 병음 맞히기 | 한자 + 뜻을 보고 올바른 병음 4지선다 |
| 말하기 연습 | 화면의 단어를 마이크에 대고 말하면 음성인식으로 채점 (단어당 기회 3번) |

- 레슨 14개, 189단어: 인사 / 숫자 / 음식 / 가족 / 일상 / 색깔 / 동물 / 시간·요일 / 장소·교통 / 동작 / 형용사 / 날씨·계절 / 몸 / 회화 문장 + 전체 복습 (`words.json`에서 단어 추가·수정 가능)
- 라운드당 10문제, 하트 3개, 연속 정답 콤보 보너스, 끝나면 틀린 단어 복습 목록

## 조작

- 메뉴: `↑↓` 레슨 선택, `←→` 모드 선택, `Enter` 시작
- 객관식: `1~4` 또는 방향키 + `Enter`, 마우스 클릭
- 말하기: `Space`(또는 클릭)를 누르고 말하기 → 말이 끝나면(약 0.8초 무음) 자동으로 인식, `Tab` 건너뛰기
- 메뉴에서 `V`: 목소리(마이크) 기능 켜기/끄기 — 끄면 말하기 모드가 사라지고 객관식만 남아요
- `Esc` 메뉴로

## USB 마이크 / 음성 감지

- 시작할 때 이름에 **USB** 가 들어간 입력 장치를 자동으로 고릅니다 (없으면 기본 마이크). 메뉴 하단에 사용 중인 마이크가 표시됩니다.
- 음량(RMS) 기반으로 말소리 시작/끝을 감지하고, 녹음 중에는 도트 레벨 미터가 움직입니다.
- 인식은 기본적으로 Google 음성인식(`zh-CN`, **인터넷 필요**)을 씁니다.
- 채점은 인식된 한자가 정답과 같거나, 성조를 뺀 병음이 같으면 정답입니다 (`pypinyin`).

```bash
python main.py --list-devices        # 입력 장치 목록 (USB 표시)
python main.py --device 2            # 장치 번호 직접 지정
python main.py --threshold 0.04      # 주변이 시끄러우면 감지 기준 올리기
python main.py --scale 4             # 창 크게
python main.py --no-voice            # 목소리 기능 끄고 시작 (마이크 없을 때)
```

### 마이크 진단 (초록불이 안 켜질 때)

- 게임 메뉴에서 **`M`** → 마이크 진단 화면
  - `↑↓`로 장치를 바꾸면 바로 음량 막대와 **초록불**이 움직이는지 확인
  - `←→`로 감도(감지 기준선, 하늘색 줄) 조절, `Enter`로 이 마이크 사용 + 저장(`settings.json`, 다음 실행에도 유지)
- 터미널 진단: `python3 main.py --diagnose`
  - `lsusb` / `arecord -l`로 USB 마이크가 시스템에 잡혔는지, 장치마다 2초 녹음해서 소리가 들어오는지 알려줌
- 소리가 너무 작으면: `alsamixer` → `F6`로 USB 장치 선택 → `F4`(Capture) → 볼륨 올리기

### 인식 속도

- 말하기 모드에 들어가면 마이크를 미리 열어 둬서 `Space`를 누르자마자 녹음이 시작되고, 그동안 주변 소음 크기를 재요.
- 말이 끝나고 **0.5초** 조용하면 바로 녹음을 끝내요. 주변 소음과 말소리 크기에 맞춰 기준이 자동으로 바뀌어서, 시끄러운 곳에서도 녹음이 늘어지지 않아요.
- 터미널에 `[음성] 녹음 0.9초 (대기 포함 1.4초), 인식(google) 1.2초` 처럼 어디서 시간이 걸렸는지 나와요.

### 오프라인 인식 (더 빠름)

Google 인식은 인터넷 왕복 때문에 1~2초 걸려요. Vosk를 쓰면 **말하는 동안 바로 인식**해서 말이 끝나자마자 결과가 나와요.

```bash
./get_vosk.sh      # vosk 설치 + 중국어 모델(42MB)을 게임 폴더에 받기
python3 main.py    # 게임 폴더에 vosk-model*cn* 폴더가 있으면 자동으로 Vosk 사용
```

다른 위치의 모델은 `--vosk-model 경로`로 지정할 수 있어요. 정확도는 Google이 조금 더 좋아요 (모델 폴더를 지우거나 옮기면 다시 Google 사용).

## ST7735S LCD (160x128 가로) 에 띄우기

퀴즈 화면은 LCD에 나오고, PC의 pygame 창은 **키 입력 전용**(현재 화면의 키 안내 + 마지막 누른 키 + LCD 상태)이 됩니다.
LCD 해상도에 맞춘 전용 레이아웃이라 글자가 뭉개지지 않고, 긴 뜻/문장은 좌우로 흐르면서 보여요.

### 배선 (BCM 기준, SPI0)

| LCD 핀 | 라즈베리파이 |
|---|---|
| VCC | 3.3V (1번) |
| GND | GND (6번) |
| SCL / SCK | GPIO11 (23번) |
| SDA / MOSI | GPIO10 (19번) |
| CS | GPIO8 / CE0 (24번) |
| DC / A0 / RS | GPIO24 (18번) |
| RES / RST | GPIO25 (22번) |
| BL / LED | GPIO18 (12번) 또는 3.3V |

### 준비 & 실행

```bash
sudo raspi-config        # Interface Options → SPI → Enable (재부팅)
sudo apt install python3-spidev python3-gpiozero
python3 main.py --lcd-test     # 색 막대 테스트 화면 (10초)
./run_lcd.sh                   # = python3 main.py --lcd
```

`--lcd-test`에서 이상하면 옵션으로 보정하세요 (`run_lcd.sh` 뒤에 붙이면 됨):

| 증상 | 옵션 |
|---|---|
| 빨강↔파랑이 바뀜 | `--lcd-rgb` |
| 색이 반전(네거티브) | `--lcd-invert` |
| 화면이 거꾸로 | `--lcd-flip` |
| 가장자리에 쓰레기 줄 / 잘림 | `--lcd-offset 1,2` (x,y) |
| 핀을 다르게 연결 | `--lcd-dc 24 --lcd-rst 25 --lcd-bl 18` (백라이트 3.3V 직결이면 `--lcd-bl -1`) |
| 화면이 깨짐 | `--spi-speed 16000000` |

LCD 없이 레이아웃만 보고 싶으면 `python3 main.py --lcd-preview` (키 입력 창 옆에 2배 미리보기).

## 폰트

`assets/unifont.otf`(GNU Unifont, 도트 폰트)가 같이 들어 있어서 한글·한자·병음·기호가 어떤 OS에서도 □ 없이 나옵니다.
글자마다 그 글자가 있는 폰트를 골라 그리기 때문에, 다른 픽셀 폰트(예: [Fusion Pixel Font](https://github.com/TakWolf/fusion-pixel-font))를 `assets/`에 넣으면 그 폰트를 우선 쓰고 빠진 글자만 Unifont로 채웁니다.
