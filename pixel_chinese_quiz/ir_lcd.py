"""IR 리모컨 신호 분석기 — ST7735S LCD 에 결과 표시.

수신기(VS1838B 등) OUT → GPIO18 (기본, --ir-pin 으로 변경)
피에조 부저 + → GPIO23 (16번 핀), - → GND  (기본, --buzzer-pin 으로 변경, -1 이면 끔)
  설정온도를 음 높이로 알려줌: 18°C=도 … 30°C=한 옥타브 위 도

※ st7735.py 기본 배선에서 LCD 백라이트(BL)도 GPIO18 이라 핀이 겹칩니다.
  IR 을 GPIO18 에 두면 LCD BL 은 3.3V 에 직접 연결하세요 (이 툴은 자동으로 BL 제어를 끔).
  또는 IR 을 다른 핀(예: GPIO17)으로 옮기고 --ir-pin 17 로 실행.

실행:  python3 ir_lcd.py            (LCD + 터미널)
       python3 ir_lcd.py --no-lcd   (터미널만)
"""
import argparse
import json
import os
import tempfile
import threading
import time

import numpy as np

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # 창 없이 글자 렌더링만
import pygame  # noqa: E402

import lgpio  # noqa: E402

W, H = 160, 128
# 이 리모컨(LG)이 보내는 코드 범위. 이 밖의 신호는 무시
CODE_MIN, CODE_MAX = 0x880834F, 0x8808F4B

# updev(장치관리자)가 읽는 상태 파일. updev 의 backends/ir.py 와 형식을 맞춰야 함.
STATE_PATH = os.environ.get("IR_AC_STATE", "/tmp/ir_ac.json")
STATE_VERSION = 1


def ac_temp(v):
    """LG 에어컨 코드 → 설정 온도(°C). 가운데 4비트(v>>8 & 0xF) + 15. 3→18도 … F→30도."""
    return ((v >> 8) & 0xF) + 15


class StateFile:
    """IR 수신 상태를 JSON 으로 기록. 원자적 교체(임시파일→rename)라 읽는 쪽이 반쪽 파일을 보지 않음.

    {"version": 1, "pid": 1234, "pin": 18, "started": 1760000000.0, "updated": ...,
     "count": 3, "last": {"code": "0x8808A4F", "data": "0x08A4", "temp_c": 25, "time": ...}}
    last 는 아직 받은 게 없으면 null.
    """

    def __init__(self, pin, path=STATE_PATH):
        self.path = path
        self.state = {"version": STATE_VERSION, "pid": os.getpid(), "pin": pin,
                      "started": time.time(), "updated": time.time(), "count": 0, "last": None}
        self._write()

    def record(self, result):
        now = time.time()
        self.state["count"] += 1
        self.state["updated"] = now
        self.state["last"] = {"code": result["code"], "data": result["data"],
                              "temp_c": ac_temp(result["v"]), "time": now}
        self._write()

    def _write(self):
        try:
            d = os.path.dirname(self.path) or "."
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".ir_ac.")
            with os.fdopen(fd, "w") as f:
                json.dump(self.state, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except OSError as e:
            print("[상태파일] 기록 실패:", e)

    def close(self):
        try:
            os.remove(self.path)  # 꺼지면 지워서 updev 가 "실행 안 됨" 으로 표시하게
        except OSError:
            pass
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "unifont.otf")


# ---------------- 디코더 ----------------
def decode_lg(f):
    """LG 28비트 (에어컨 등). f: 리더 펄스부터 시작하는 엣지 간격(µs) 리스트."""
    if len(f) < 58 or not (8000 < f[0] < 9800):
        return None
    v = 0
    for i in range(28):
        v = (v << 1) | (1 if f[3 + i * 2] > 1000 else 0)
    if (v >> 20) != 0x88:
        return None
    data = (v >> 4) & 0xFFFF
    chk = sum((data >> s) & 0xF for s in (0, 4, 8, 12)) & 0xF
    if chk != (v & 0xF):
        return None
    return {"proto": "LG28", "v": v, "code": "0x%07X" % v, "data": "0x%04X" % data}


def decode_nec(f):
    """NEC 32비트 (TV/셋톱 리모컨 대부분). LSB 먼저."""
    if len(f) < 66 or not (8000 < f[0] < 10000) or not (3500 < f[1] < 5000):
        return None
    v = 0
    for i in range(32):
        if f[3 + i * 2] > 1000:
            v |= 1 << i
    addr, naddr, cmd, ncmd = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF
    if cmd ^ ncmd != 0xFF:
        return None
    a = "0x%02X" % addr if addr ^ naddr == 0xFF else "0x%02X%02X" % (naddr, addr)  # 확장 NEC
    return {"proto": "NEC", "code": "0x%08X" % v, "data": "A=%s C=0x%02X" % (a, cmd)}


def decode_nec_repeat(f):
    if 2 <= len(f) <= 4 and 8000 < f[0] < 10000 and 1800 < f[1] < 2800:
        return {"proto": "NEC", "code": "REPEAT", "data": "(누르고 있음)"}
    return None


def decode_generic(f):
    """모르는 프로토콜: 펄스 길이로 비트를 추정해 해시처럼 보여줌."""
    if len(f) < 8:
        return None
    spaces = f[3::2]
    thr = (min(spaces) + max(spaces)) / 2
    bits = "".join("1" if s > thr else "0" for s in spaces)
    v = int(bits, 2) if bits else 0
    return {"proto": "RAW?", "code": "%d비트" % len(bits), "data": "0x%X" % v}


DECODERS = (decode_lg, decode_nec, decode_nec_repeat)


def analyze(frame):
    for d in DECODERS:
        r = d(frame)
        if r:
            return r, True
    return decode_generic(frame), False


# ---------------- IR 수신 ----------------
class Buzzer:
    """피에조 부저로 설정온도를 소리로 알림. 18°C=도(C5) … 30°C=한 옥타브 위 도(C6), 1도에 반음씩.

    수동(passive) 부저: PWM 주파수로 음 높이가 바뀜 (기본).
    능동(active) 부저: 전원만 주면 고정음 → 음 높이 대신 짧게 한 번 삑.
    IR 수신을 막지 않도록 소리는 별도 스레드에서 냄.
    """

    BASE_HZ = 523.25  # C5 = 18°C

    def __init__(self, h, pin, active=False):
        self.h, self.pin, self.active = h, pin, active
        lgpio.gpio_claim_output(h, pin, 0)
        self._lock = threading.Lock()

    def freq(self, temp_c):
        return self.BASE_HZ * 2 ** ((temp_c - 18) / 12)

    def _tone(self, hz, dur):
        if self.active:
            lgpio.gpio_write(self.h, self.pin, 1)
        else:
            lgpio.tx_pwm(self.h, self.pin, hz, 50)
        time.sleep(dur)
        if self.active:
            lgpio.gpio_write(self.h, self.pin, 0)
        else:
            lgpio.tx_pwm(self.h, self.pin, 0, 0)

    def _play(self, notes):
        with self._lock:  # 연타해도 소리가 겹치지 않게
            for hz, dur, gap in notes:
                self._tone(hz, dur)
                time.sleep(gap)

    def play(self, notes):
        threading.Thread(target=self._play, args=(notes,), daemon=True).start()

    def temp(self, temp_c):
        """설정온도 음 한 번. 끝 온도(18/30)는 두 번 울려서 '더 못 내려/올려' 느낌."""
        hz = self.freq(temp_c)
        n = 2 if temp_c in (18, 30) else 1
        self.play([(hz, 0.12, 0.06)] * n)

    def hello(self):
        """시작음: 도-미-솔."""
        self.play([(self.freq(t), 0.08, 0.02) for t in (18, 22, 25)])

    def close(self):
        try:
            if not self.active:
                lgpio.tx_pwm(self.h, self.pin, 0, 0)
            lgpio.gpio_write(self.h, self.pin, 0)
            lgpio.gpio_free(self.h, self.pin)
        except lgpio.error:
            pass


class IRReceiver:
    def __init__(self, pin):
        self.pin = pin
        self.h = None
        for chip in (0, 4):  # 파이5 는 4, 이전 모델은 0
            try:
                h = lgpio.gpiochip_open(chip)
                lgpio.gpio_claim_alert(h, pin, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP)
                self.h = h
                break
            except lgpio.error:
                pass
        if self.h is None:
            raise RuntimeError("GPIO%d 를 열 수 없음 (다른 프로그램이 사용 중?)" % pin)
        self.edges, self.last_tick, self.last_time = [], 0, time.monotonic()
        self.lock = threading.Lock()
        self.cb = lgpio.callback(self.h, pin, lgpio.BOTH_EDGES, self._on_edge)

    def _on_edge(self, chip, gpio, level, tick):
        with self.lock:
            self.edges.append((tick - self.last_tick) // 1000)
            self.last_tick, self.last_time = tick, time.monotonic()

    def get_frame(self, gap=0.1):
        """마지막 엣지 후 gap 초 조용하면 한 프레임으로 반환."""
        with self.lock:
            if self.edges and time.monotonic() - self.last_time > gap:
                frame = self.edges[1:]
                self.edges.clear()
                return frame
        return None

    def close(self):
        self.cb.cancel()
        lgpio.gpiochip_close(self.h)


# ---------------- 화면 ----------------
class Screen:
    """해커 터미널 느낌: 초록 글자, 타이핑 애니메이션, 깜빡이는 커서, 스캔라인."""
    BG = (0, 8, 2)
    GREEN, DIM, AMBER, CYAN = (60, 255, 120), (20, 110, 50), (255, 200, 60), (90, 220, 255)
    TYPE_CPS = 40  # 초당 타이핑 글자 수

    def __init__(self, lcd):
        pygame.font.init()
        self.lcd = lcd
        self.font = pygame.font.Font(FONT_PATH if os.path.exists(FONT_PATH) else None, 16)
        self.surf = pygame.Surface((W, H))
        self.scan = pygame.Surface((W, H), pygame.SRCALPHA)
        for y in range(0, H, 2):
            pygame.draw.line(self.scan, (0, 0, 0, 70), (0, y), (W, y))
        self.cur, self.since, self.history = None, 0.0, []

    def push(self, result):
        if self.cur:
            self.history.append(self.cur["code"])
        self.cur, self.since = result, time.monotonic()

    def _text(self, s, pos, color):
        self.surf.blit(self.font.render(s, False, color), pos)

    def draw(self):
        s, now = self.surf, time.monotonic()
        s.fill(self.BG)
        self._text("pi@ir:~$ listen", (2, 0), self.DIM)
        for i, code in enumerate(self.history[-3:]):
            self._text("> " + code, (2, 18 + i * 16), self.DIM)
        blink = int(now * 2) % 2 == 0
        if self.cur is None:
            self._text("waiting" + "." * (int(now * 3) % 4), (2, 80), self.GREEN)
        else:
            lines = [("코드: " + self.cur["code"], self.GREEN),
                     ("데이터: " + self.cur["data"], self.AMBER),
                     ("온도: %d°C" % ac_temp(self.cur["v"]), self.CYAN)]
            n = int((now - self.since) * self.TYPE_CPS)
            for i, (txt, col) in enumerate(lines):
                self._text(txt[:max(0, n)], (2, 74 + i * 17), col)
                n -= len(txt)
            if now - self.since < 0.25:  # 수신 순간 테두리 번쩍
                pygame.draw.rect(s, self.CYAN, (0, 0, W, H), 2)
        if blink:
            pygame.draw.rect(s, self.GREEN, (W - 10, H - 14, 7, 12))
        s.blit(self.scan, (0, 0))
        if self.lcd:
            rgb = pygame.surfarray.array3d(s).transpose(1, 0, 2)
            self.lcd.show_rgb(np.ascontiguousarray(rgb))


def main():
    ap = argparse.ArgumentParser(description="IR 리모컨 신호 분석기 (ST7735S LCD 표시)")
    ap.add_argument("--ir-pin", type=int, default=18)
    ap.add_argument("--no-lcd", action="store_true")
    ap.add_argument("--all", action="store_true", help="범위 밖/다른 프로토콜 신호도 표시")
    ap.add_argument("--buzzer-pin", type=int, default=23,
                    help="피에조 부저 GPIO (기본 23 = 16번 핀, -1 이면 끔)")
    ap.add_argument("--buzzer-active", action="store_true",
                    help="능동 부저(전원만 주면 삑)일 때. 기본은 수동 부저(음 높이 변화)")
    ap.add_argument("--raw", action="store_true", help="터미널에 엣지 간격(µs) 전체 출력")
    ap.add_argument("--lcd-dc", type=int, default=24)
    ap.add_argument("--lcd-rst", type=int, default=25)
    ap.add_argument("--lcd-bl", type=int, default=18)
    ap.add_argument("--lcd-flip", action="store_true")
    ap.add_argument("--lcd-rgb", action="store_true")
    ap.add_argument("--lcd-invert", action="store_true")
    args = ap.parse_args()

    lcd = None
    if not args.no_lcd:
        bl = -1 if args.lcd_bl == args.ir_pin else args.lcd_bl
        if bl < 0 and args.lcd_bl >= 0:
            print("[LCD] 백라이트 핀이 IR 핀(GPIO%d)과 겹쳐서 BL 제어 끔 → BL 을 3.3V 에 연결하세요"
                  % args.ir_pin)
        try:
            from st7735 import ST7735
            lcd = ST7735(dc=args.lcd_dc, rst=args.lcd_rst, bl=bl, flip=args.lcd_flip,
                         bgr=not args.lcd_rgb, invert=args.lcd_invert)
        except Exception as e:
            print("[LCD] 사용 안 함:", e)

    ir = IRReceiver(args.ir_pin)
    state = StateFile(args.ir_pin)
    buzzer = None
    if args.buzzer_pin >= 0:
        try:
            buzzer = Buzzer(ir.h, args.buzzer_pin, args.buzzer_active)
            buzzer.hello()
        except lgpio.error as e:
            print("[부저] GPIO%d 사용 안 함:" % args.buzzer_pin, e)
    scr = Screen(lcd)
    next_draw = 0.0
    print("버튼을 눌러보세요 (Ctrl+C 종료)")
    try:
        while True:
            frame = ir.get_frame()
            if frame:
                result, ok = analyze(frame)
                wanted = result and "v" in result and CODE_MIN <= result["v"] <= CODE_MAX
                if wanted:
                    print("\033[92m코드: %s\033[0m  \033[93m데이터: %s\033[0m  \033[96m%d°C\033[0m"
                          % (result["code"], result["data"], ac_temp(result["v"])))
                    scr.push(result)
                    state.record(result)
                    if buzzer:
                        buzzer.temp(ac_temp(result["v"]))
                elif args.all and result:
                    print("  (무시) [%s] %s  %s  (엣지 %d)" % (result["proto"], result["code"],
                                                            result["data"], len(frame)))
                if args.raw:
                    print("   ", frame)
            if time.monotonic() >= next_draw:  # 애니메이션 ~20fps (안 바뀐 프레임은 전송 안 함)
                scr.draw()
                next_draw = time.monotonic() + 0.05
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        if buzzer:
            buzzer.close()  # 같은 gpiochip 핸들을 쓰므로 ir.close() 보다 먼저
        ir.close()
        state.close()
        if lcd:
            lcd.close()


if __name__ == "__main__":
    main()
