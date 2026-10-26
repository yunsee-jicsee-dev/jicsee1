# ORICO NVMe 바이너리 에디터

ORICO NVMe 외장 케이스 펌웨어 때문에 헤매는 분들을 위한 헥스(바이너리) 에디터 겸 펌웨어 확인 도구입니다.

ORICO 케이스는 모델과 생산 시기에 따라 **Realtek RTL9210/RTL9210B**, **JMicron JMS583**,
**ASMedia ASM2362** 같은 USB↔NVMe 브릿지 칩을 씁니다. 같은 모델명이라도 칩이 다를 수 있어서
"어떤 펌웨어를 받아야 하지?", "이 .bin 파일이 내 케이스에 맞나?" 하고 헤매기 쉽습니다.
이 도구로 **내 케이스의 칩과 현재 펌웨어 버전을 확인하고, 펌웨어 파일의 내용을 비교·분석·수정**할 수 있습니다.

- 파이썬 표준 라이브러리만 사용합니다 (설치할 패키지 없음).
- **Windows / Linux**, **x64 / ARM64** 모두 같은 소스로 동작합니다. 라즈베리파이, Windows on ARM 노트북도 됩니다.
- 인자 없이 실행하면 GUI, 인자를 주면 명령줄(CLI)로 동작합니다.

> ⚠️ 이 도구는 브릿지 칩에 펌웨어를 **플래시하지 않습니다**. 실제 업데이트는 칩 제조사의 MP 툴
> (예: Realtek RTL9210 MP Tool, JMicron JMS583 업데이트 툴, ASMedia ASM236x 툴)로 하세요.
> 이 도구는 그 전에 "맞는 파일인지" 확인하고 필요한 부분을 고치는 용도입니다.

## 기능

| 기능 | 설명 |
| --- | --- |
| 헥스 에디터 | 펌웨어 `.bin`, 디스크 이미지, 실제 디스크(`/dev/nvme0n1`, `\\.\PhysicalDrive1`)를 열고 편집 |
| 펌웨어 분석 | 칩 이름 문자열, USB 장치 디스크립터(VID/PID/bcdDevice), USB 문자열, 버전 문자열, 해시 |
| VID/PID·문자열 수정 | 펌웨어 안의 USB ID / 제조사·제품 문자열을 같은 자리에서 수정 |
| 체크섬 | CRC32/sum8/sum16 형태의 "파일 전체 체크섬"을 자동으로 찾아내고, 수정 후 다시 계산 |
| 장치 검색 | 연결된 USB 브릿지의 VID:PID, REV(대부분 브릿지 펌웨어 버전), NVMe SSD 펌웨어 버전 표시 |
| 파일 비교 | ORICO 원본 펌웨어와 다른 펌웨어의 차이 구간 및 USB ID 비교 |
| 이미지 덤프 | 디스크 일부/전체를 파일로 백업 |
| 안전장치 | 저장 시 자동 백업(`.bak`), 디스크는 기본 읽기 전용, 쓰기 시 경로 재입력 확인 |

## 다운로드 / 실행

### 1) 실행 파일 (파이썬 없어도 됨)

GitHub의 **Actions → "ORICO NVMe 에디터 빌드"** 실행 결과(Artifacts)나 Releases에서 받으세요.

| 파일 | 대상 |
| --- | --- |
| `orico-nvme-editor-windows-x64.exe` | 일반 Windows PC |
| `orico-nvme-editor-windows-arm64.exe` | Windows on ARM (Snapdragon 노트북 등) |
| `orico-nvme-editor-linux-x64` | 일반 리눅스 PC |
| `orico-nvme-editor-linux-arm64` | 라즈베리파이 4/5 (64비트 OS), ARM 리눅스 |

- Windows: 더블클릭하면 GUI가 열립니다. 디스크를 직접 열려면 **관리자 권한으로 실행**하세요.
- Linux: `chmod +x orico-nvme-editor-linux-*` 후 실행. 디스크를 직접 열려면 `sudo` 가 필요합니다.

### 2) 소스로 실행 (Python 3.9 이상)

```bash
python3 nvme_hexedit.py            # GUI
python3 nvme_hexedit.py --help     # CLI 도움말
```

리눅스에서 GUI가 안 열리면 `sudo apt install python3-tk` (Fedora: `sudo dnf install python3-tkinter`).

## 펌웨어 때문에 헤맬 때 추천 순서

1. **내 케이스 칩 확인** — GUI의 `도구 → 장치 검색` 또는 `nvme_hexedit scan` (출력 예시)
   ```
   [USB 브릿지] 0BDA:9210  Realtek RTL9210B-CG
       칩 추정   : Realtek RTL9210 / RTL9210B (USB ↔ NVMe/SATA)
       REV(bcdDevice): 20.01 (0x2001)  ← 대부분 브릿지 펌웨어 버전
   ```
2. **받은 펌웨어가 맞는 칩용인지 확인** — 파일을 열고 `도구 → 펌웨어 분석` 또는 `nvme_hexedit info 파일.bin`.
   칩 이름 문자열과 VID:PID가 1번 결과와 같은 계열인지 봅니다.
3. **원본과 비교** — `도구 → 두 파일 비교` 또는 `nvme_hexedit diff 원본.bin 새것.bin`
4. **필요하면 수정** — VID/PID, 제품 문자열 등. 수정 후 체크섬 후보가 있으면 재계산.
   항상 **다른 이름으로 저장**하고 원본은 보관하세요.
5. **플래시는 제조사 MP 툴로.** MP 툴이 파일을 거부하면 수정 전 원본으로 돌아가세요.

## GUI 사용법

- 바이트 클릭 → 선택, 16진수 입력 → 값 변경 (`Tab`: HEX ↔ ASCII 영역 전환)
- `Ctrl+Z`/`Ctrl+Y` 되돌리기/다시 실행, `Ctrl+G` 오프셋 이동, `Ctrl+F` 찾기(16진수·ASCII·UTF-16), `F3` 다음 찾기
- 빨간 글자 = 아직 저장하지 않은 변경, 오른쪽 패널 = 선택 위치의 u8/u16/u32(LE/BE) 값
- `편집 → 바이트열 붙여 쓰기`, `범위 채우기`
- `파일 → 범위 내보내기` 로 디스크 일부를 이미지로 저장

## CLI 예시

```bash
# 펌웨어 분석 (JSON 출력도 가능: --json)
nvme_hexedit info RTL9210B_fw.bin

# 연결된 장치와 펌웨어 버전
nvme_hexedit scan

# 두 펌웨어 비교
nvme_hexedit diff orico_stock.bin generic.bin

# USB ID 변경 + 체크섬 자동 재계산 → 새 파일로 저장 (원본은 그대로)
nvme_hexedit patch fw.bin --usb-id 0BDA:9210 --fix-checksum auto -o fw_new.bin

# 특정 위치 바이트 수정, USB 문자열 수정 (오프셋은 info 결과 참고)
nvme_hexedit patch fw.bin --set 0x1A0=DEADBEEF --usb-string 0x300=ORICO -o fw_new.bin

# 디스크 앞부분 보기 / 백업 (Linux: sudo, Windows: 관리자)
sudo nvme_hexedit dump /dev/nvme0n1 --length 0x200
nvme_hexedit image PhysicalDrive2 head.bin --length 0x100000
```

## 한계와 주의 사항

- **칩 추정은 휴리스틱입니다.** 펌웨어 안의 문자열과 USB 디스크립터 패턴으로 추정하므로,
  암호화·압축된 펌웨어에서는 아무것도 못 찾을 수 있습니다.
- **체크섬은 "파일 전체를 덮는 단순 방식"(CRC32/바이트 합)만 자동 처리합니다.** 헤더 안에 체크섬이나
  서명이 있는 펌웨어는 수정하면 MP 툴이 거부할 수 있습니다. sum8 방식은 우연히 맞을 확률(1/256)이 있습니다.
- **디스크 쓰기는 위험합니다.** 기본은 읽기 전용이며, 쓰기 전에 바뀌는 블록의 원본을
  `~/orico-nvme-backups/<시각>/` 에 저장합니다. Windows는 마운트된 볼륨 영역에 대한 직접 쓰기를 막습니다.
- 브릿지 칩과 맞지 않는 펌웨어를 플래시하면 케이스가 인식되지 않을 수 있습니다.
  대부분 MP 툴의 복구 모드로 살릴 수 있지만, 책임은 사용자에게 있습니다.

## 직접 빌드하기

PyInstaller는 교차 빌드를 지원하지 않으므로 대상 OS/CPU에서 직접 빌드합니다.

```bash
python -m pip install -r requirements-build.txt
python build.py        # → dist/orico-nvme-editor-<os>-<arch>
```

저장소의 `.github/workflows/orico-nvme-editor.yml` 이 Windows x64/ARM64, Linux x64/ARM64를 모두 빌드합니다.
`orico-nvme-v1.0.0` 같은 태그를 푸시하면 Release에 4개 실행 파일이 올라갑니다.

## 테스트

```bash
python -m unittest discover -s tests -v
```
