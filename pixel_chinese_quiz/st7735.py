"""ST7735S SPI LCD (160x128, 가로 모드) 드라이버.

라즈베리파이 기본 배선 (BCM 번호):
    LCD      →  Pi
    VCC      →  3.3V   (1번 핀)
    GND      →  GND    (6번 핀)
    SCL/SCK  →  GPIO11 (23번 핀, SPI0 SCLK)
    SDA/MOSI →  GPIO10 (19번 핀, SPI0 MOSI)
    CS       →  GPIO8  (24번 핀, SPI0 CE0)
    DC/A0/RS →  GPIO24 (18번 핀)
    RES/RST  →  GPIO25 (22번 핀)
    BL/LED   →  GPIO18 (12번 핀)  또는 3.3V 에 바로 연결

SPI 를 켜야 합니다:  sudo raspi-config → Interface Options → SPI → Enable
"""
import time

import numpy as np

# MADCTL 비트
_MY, _MX, _MV, _BGR = 0x80, 0x40, 0x20, 0x08


class ST7735:
    def __init__(self, port=0, cs=0, dc=24, rst=25, bl=18, speed_hz=24_000_000,
                 width=160, height=128, flip=False, bgr=True, invert=False,
                 offset_x=0, offset_y=0):
        import spidev  # 라즈베리파이 OS 기본 포함 (python3-spidev)

        self.width, self.height = width, height
        self.ox, self.oy = offset_x, offset_y
        self.spi = spidev.SpiDev()
        try:
            self.spi.open(port, cs)
        except FileNotFoundError:
            raise RuntimeError(f"/dev/spidev{port}.{cs} 없음 → raspi-config 에서 SPI 를 켜세요")
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0

        self._dc = _Pin(dc)
        self._rst = _Pin(rst) if rst is not None and rst >= 0 else None
        self._bl = _Pin(bl) if bl is not None and bl >= 0 else None

        # 가로 모드: MV(행/열 교환) + 미러 1개. flip 이면 180도 회전
        madctl = (_MX | _MV) if flip else (_MY | _MV)
        if bgr:
            madctl |= _BGR
        self._init(madctl, invert)
        self._last = None
        if self._bl:
            self._bl.on()

    # ---------------- 저수준 ----------------
    def _cmd(self, c, data=None):
        self._dc.off()
        self.spi.writebytes([c])
        if data:
            self._dc.on()
            self.spi.writebytes2(bytes(data))

    def _init(self, madctl, invert):
        if self._rst:
            self._rst.on(); time.sleep(0.01)
            self._rst.off(); time.sleep(0.01)
            self._rst.on(); time.sleep(0.12)
        self._cmd(0x01); time.sleep(0.15)                       # SWRESET
        self._cmd(0x11); time.sleep(0.12)                       # SLPOUT
        self._cmd(0xB1, [0x01, 0x2C, 0x2D])                     # FRMCTR1
        self._cmd(0xB2, [0x01, 0x2C, 0x2D])                     # FRMCTR2
        self._cmd(0xB3, [0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D])   # FRMCTR3
        self._cmd(0xB4, [0x07])                                 # INVCTR
        self._cmd(0xC0, [0xA2, 0x02, 0x84])                     # PWCTR1
        self._cmd(0xC1, [0xC5])                                 # PWCTR2
        self._cmd(0xC2, [0x0A, 0x00])                           # PWCTR3
        self._cmd(0xC3, [0x8A, 0x2A])                           # PWCTR4
        self._cmd(0xC4, [0x8A, 0xEE])                           # PWCTR5
        self._cmd(0xC5, [0x0E])                                 # VMCTR1
        self._cmd(0x21 if invert else 0x20)                     # INVON / INVOFF
        self._cmd(0x36, [madctl])                               # MADCTL (방향/색 순서)
        self._cmd(0x3A, [0x05])                                 # COLMOD: 16bit RGB565
        self._cmd(0xE0, [0x02, 0x1C, 0x07, 0x12, 0x37, 0x32, 0x29, 0x2D,
                         0x29, 0x25, 0x2B, 0x39, 0x00, 0x01, 0x03, 0x10])
        self._cmd(0xE1, [0x03, 0x1D, 0x07, 0x06, 0x2E, 0x2C, 0x29, 0x2D,
                         0x2E, 0x2E, 0x37, 0x3F, 0x00, 0x00, 0x02, 0x10])
        self._cmd(0x13); time.sleep(0.01)                       # NORON
        self._cmd(0x29); time.sleep(0.1)                        # DISPON

    def _window(self, x0, y0, x1, y1):
        x0 += self.ox; x1 += self.ox; y0 += self.oy; y1 += self.oy
        self._cmd(0x2A, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])  # CASET
        self._cmd(0x2B, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])  # RASET
        self._cmd(0x2C)                                            # RAMWR

    # ---------------- 공개 API ----------------
    def show_rgb(self, rgb):
        """rgb: (height, width, 3) uint8 배열. 바뀐 경우에만 전송."""
        buf = rgb_to_565(rgb)
        if buf == self._last:
            return
        self._last = buf
        self._window(0, 0, self.width - 1, self.height - 1)
        self._dc.on()
        self.spi.writebytes2(buf)

    def close(self):
        try:
            self.show_rgb(np.zeros((self.height, self.width, 3), np.uint8))
            if self._bl:
                self._bl.off()
            self.spi.close()
        except Exception:
            pass


def rgb_to_565(rgb):
    rgb = rgb.astype(np.uint16)
    v = ((rgb[..., 0] & 0xF8) << 8) | ((rgb[..., 1] & 0xFC) << 3) | (rgb[..., 2] >> 3)
    return v.astype(">u2").tobytes()


class _Pin:
    """gpiozero(라즈베리파이 5 포함) 우선, 없으면 RPi.GPIO."""

    def __init__(self, bcm):
        try:
            from gpiozero import DigitalOutputDevice
            self._dev = DigitalOutputDevice(bcm, initial_value=True)
            self.on, self.off = self._dev.on, self._dev.off
        except ImportError:
            import RPi.GPIO as GPIO
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(bcm, GPIO.OUT, initial=GPIO.HIGH)
            self.on = lambda: GPIO.output(bcm, GPIO.HIGH)
            self.off = lambda: GPIO.output(bcm, GPIO.LOW)
