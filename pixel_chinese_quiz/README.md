# 像素中文 · 픽셀 중국어 퀴즈

파이썬(pygame)으로 만든 도트 그래픽 중국어 레슨 퀴즈입니다. USB 마이크로 직접 발음해서 맞히는 **말하기 연습** 모드가 있습니다.

## 설치 & 실행

```bash
cd pixel_chinese_quiz
pip install -r requirements.txt
python main.py
```

> Linux에서 `sounddevice`가 PortAudio를 못 찾으면 `sudo apt install libportaudio2`

## 모드

| 모드 | 내용 |
|---|---|
| 뜻 맞히기 | 한자 + 병음을 보고 한국어 뜻 4지선다 |
| 병음 맞히기 | 한자 + 뜻을 보고 올바른 병음 4지선다 |
| 말하기 연습 | 화면의 단어를 마이크에 대고 말하면 음성인식으로 채점 (단어당 기회 3번) |

- 레슨: 인사 / 숫자 / 음식 / 가족 / 일상 / 전체 복습 (`words.json`에서 단어 추가·수정 가능)
- 라운드당 10문제, 하트 3개, 연속 정답 콤보 보너스, 끝나면 틀린 단어 복습 목록

## 조작

- 메뉴: `↑↓` 레슨 선택, `←→` 모드 선택, `Enter` 시작
- 객관식: `1~4` 또는 방향키 + `Enter`, 마우스 클릭
- 말하기: `Space`(또는 클릭)를 누르고 말하기 → 말이 끝나면(약 0.8초 무음) 자동으로 인식, `Tab` 건너뛰기
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
```

### 오프라인 인식 (선택)

`pip install vosk` 후 [Vosk 중국어 모델](https://alphacephei.com/vosk/models)(`vosk-model-small-cn-0.22`)을 받아서:

```bash
python main.py --vosk-model ./vosk-model-small-cn-0.22
```

## 폰트

한글과 한자가 모두 나오는 폰트를 자동으로 찾습니다 (Unifont, Noto Sans CJK, 맑은 고딕 + Microsoft YaHei 등).
더 예쁜 도트 느낌을 원하면 [Fusion Pixel Font](https://github.com/TakWolf/fusion-pixel-font)처럼 한글·중국어를 지원하는 픽셀 폰트의 `.ttf/.otf`를 `assets/` 폴더에 넣으면 우선 사용합니다.
