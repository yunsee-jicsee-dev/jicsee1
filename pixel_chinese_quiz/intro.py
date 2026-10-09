"""부팅 인트로: 라즈베리 파이 5 로고가 등장하는 오프닝 연출.

같은 연출을 두 화면에서 쓴다.
    k=1 → ST7735S LCD (160x128)
    k=2 → pygame 큰 창 (400x240)

연출 순서
    주사선이 열리며 전원이 켜짐 → 라즈베리가 위에서 떨어져 착지(충격파+화면 흔들림)
    → 잎 두 장이 펼쳐짐 → "Raspberry Pi" 글자가 한 자씩 올라옴
    → 금색 "5" 가 쾅 등장(섬광) → 로고 위로 광택이 스윽 지나감 → 암전 후 메뉴
"""
import math

import pygame

# 라즈베리파이 로고 (25x27 픽셀 아트)
LOGO = [
    "........ggg...ggg........",
    ".......ggGG...GGgg.......",
    "......gGGGG...GGGGg......",
    ".....ggGGGg...gGGGgg.....",
    "....ggGGGGg...gGGGGgg....",
    "....gGGGGg.....gGGGGg....",
    "....gGGGG.......GGGGg....",
    "....gGGG....r....GGGg....",
    "....gGg...rrrrr...gGg....",
    "......r..rRHHRRr..r......",
    "....rrrrrrHRRRRrrrrrr....",
    "...rRHHRRrRRRRRrRHHRRr...",
    "...rHRRRRrRRRRRrHRRRRr...",
    "..rrRRRRRrRRRRRrRRRRRrr..",
    "...rRRRRRrrrrrrrRRRRRr...",
    "...rRRRRRrrrrrrrRRRRRr...",
    "....rrrRHHRRrRHHRRrrr....",
    "......rHRRRRrHRRRRr......",
    ".....rrRRRRRrRRRRRrr.....",
    "......rRRRRRrRRRRRr......",
    "......rRRRRRrRRRRRr......",
    ".......rrrRHHRRrrr.......",
    ".........rHRRRRr.........",
    "........rrRRRRRrr........",
    ".........rRRRRRr.........",
    ".........rRRRRRr.........",
    "..........rrrrr..........",
]
LOGO_COLORS = {
    "R": (197, 26, 74),    # 라즈베리 빨강 (브랜드 색)
    "r": (122, 14, 48),    # 알알이 사이 이음선
    "H": (240, 128, 155),  # 반짝임
    "G": (117, 169, 40),   # 잎 초록 (브랜드 색)
    "g": (62, 107, 24),    # 잎 그림자
}
BERRY_KEYS = "RrH"
LEAF_KEYS = "Gg"
LOGO_W, LOGO_H = len(LOGO[0]), len(LOGO)
LEAF_ROWS = 11    # 잎이 그려진 줄 수
LEAF_SPLIT = 12   # 잎을 왼쪽/오른쪽으로 나누는 열

BG0 = (10, 7, 16)
RAY = (46, 26, 58)
WHITE = (244, 244, 244)
GRAY = (139, 155, 180)
GOLD = (254, 174, 52)

FPS = 90          # 인트로 재생 프레임레이트 (연출은 시간 기준이라 값만 바꿔도 길이는 그대로)

# 타임라인 (초)
T_DROP = 0.30     # 라즈베리 낙하 시작
T_LAND = 0.95     # 착지
T_LEAF = 1.00     # 잎 펼치기
T_TEXT = 1.45     # "Raspberry Pi" 한 자씩
T_FIVE = 2.00     # "5" 등장
T_SHINE = 2.35    # 광택 스윕
T_TAG = 2.60      # 아래 한 줄
T_FADE = 3.35     # 암전 시작
LENGTH = 3.70     # 전체 길이

# (시각, 효과음) — 메인에서 지나갈 때 한 번씩 재생
CUES = [(0.03, "boot"), (T_LAND, "land"), (T_LEAF + 0.3, "move"), (T_FIVE, "logo")]


def clamp01(v):
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def ease_out_back(p, s=1.9):
    p -= 1
    return p * p * ((s + 1) * p + s) + 1


def sprite_surface(rows, colors, px, keys=None):
    """픽셀 아트 문자열 → 투명 배경 Surface."""
    surf = pygame.Surface((len(rows[0]) * px, len(rows) * px), pygame.SRCALPHA)
    for j, row in enumerate(rows):
        for i, ch in enumerate(row):
            if ch in colors and (keys is None or ch in keys):
                surf.fill(colors[ch], (i * px, j * px, px, px))
    return surf


def white_mask(img):
    """같은 모양의 흰색 실루엣 (광택 스윕을 로고 모양으로 자를 때 사용)."""
    m = img.copy()
    m.fill((255, 255, 255), special_flags=pygame.BLEND_RGB_MAX)
    return m


def blit_rotated(dst, img, angle, pivot, pos):
    """img 의 pivot 점이 dst 의 pos 에 오도록 회전해서 그린다."""
    rot = pygame.transform.rotate(img, angle)
    vx, vy = pivot[0] - img.get_width() / 2, pivot[1] - img.get_height() / 2
    a = math.radians(angle)
    rx = vx * math.cos(a) + vy * math.sin(a)
    ry = -vx * math.sin(a) + vy * math.cos(a)
    dst.blit(rot, rot.get_rect(center=(pos[0] - rx, pos[1] - ry)))


class Intro:
    """로고 인트로 한 장면. draw(surf, t) 를 매 프레임 호출."""

    def __init__(self, text, w, h, k=1):
        self.T = text
        self.w, self.h, self.k = w, h, k
        px = self.px = 2 * k

        self.berry = sprite_surface(LOGO, LOGO_COLORS, px, BERRY_KEYS)
        leaves = sprite_surface(LOGO[:LEAF_ROWS], LOGO_COLORS, px, LEAF_KEYS)
        self.leaf_l = leaves.subsurface((0, 0, LEAF_SPLIT * px, LEAF_ROWS * px)).copy()
        self.leaf_r = leaves.subsurface(
            (LEAF_SPLIT * px, 0, (LOGO_W - LEAF_SPLIT) * px, LEAF_ROWS * px)).copy()
        self.logo = sprite_surface(LOGO, LOGO_COLORS, px)
        self.logo_mask = white_mask(self.logo)

        # 배치
        self.logo_x = (w - LOGO_W * px) // 2
        self.logo_y = int(h * 0.04)
        self.berry_y = self.logo_y          # 베리의 최종 y (스프라이트 전체 기준)
        self.text_y = self.logo_y + LOGO_H * px + 4 * k

        self.title = "Raspberry Pi"
        self.five = "5"
        # 화면 폭에 맞는 가장 긴 아래 문구를 고른다 (LCD 는 160px 뿐)
        self.tag = "像素中文"
        for cand in ("像素中文 · 픽셀 중국어 퀴즈", "像素中文 · 픽셀 중국어", "像素中文 퀴즈"):
            if text.render(cand, GRAY, k, zh=True, shadow=False).get_width() <= w - 6 * k:
                self.tag = cand
                break
        tag_h = text.render(self.tag, GRAY, k, zh=True, shadow=False).get_height()
        self.tag_y = h - tag_h - 3 * k
        t1 = text.render(self.title, WHITE, k, shadow=False)
        t5 = text.render(self.five, GOLD, 2 * k, shadow=False)
        self.title_h, self.five_h = t1.get_height(), t5.get_height()
        gap = 3 * k
        total = t1.get_width() + gap + t5.get_width()
        self.title_x = (w - total) // 2
        self.five_x = self.title_x + t1.get_width() + gap
        self.five_mid = (self.five_x + t5.get_width() // 2, self.text_y + self.five_h // 2)

        self._fx = pygame.Surface((w, h), pygame.SRCALPHA)   # 효과용 재사용 버퍼
        self._scan = self._make_scanlines()

    # ---------------- 보조 ----------------
    def _make_scanlines(self):
        s = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        for y in range(0, self.h, 2):
            s.fill((0, 0, 0, 46), (0, y, self.w, 1))
        return s

    def _rays(self, surf, t, alpha):
        """로고 뒤에서 천천히 도는 빛살."""
        fx = self._fx
        fx.fill((0, 0, 0, 0))
        cx = self.logo_x + LOGO_W * self.px // 2
        cy = self.logo_y + LOGO_H * self.px // 2
        rad = self.w + self.h
        col = (*RAY, int(alpha))
        for i in range(10):
            a0 = math.radians(t * 16 + i * 36)
            a1 = a0 + math.radians(17)
            pygame.draw.polygon(fx, col, [
                (cx, cy),
                (cx + rad * math.cos(a0), cy + rad * math.sin(a0)),
                (cx + rad * math.cos(a1), cy + rad * math.sin(a1)),
            ])
        surf.blit(fx, (0, 0))

    def _ring(self, surf, center, r, alpha, color=WHITE, flat=0.38):
        if r < 1 or alpha <= 2:
            return
        fx = self._fx
        fx.fill((0, 0, 0, 0))
        ry = max(1, int(r * flat))
        rect = pygame.Rect(center[0] - r, center[1] - ry, r * 2, ry * 2)
        pygame.draw.ellipse(fx, (*color, int(alpha)), rect, max(1, self.k))
        surf.blit(fx, (0, 0))

    def _sparks(self, surf, center, t, q, n=9, color=WHITE):
        """착지/등장 때 사방으로 튀는 점."""
        if not 0 < q < 1:
            return
        for i in range(n):
            a = math.radians(i * (360 / n) + t * 40)
            d = (10 + 44 * q) * self.k
            x = int(center[0] + math.cos(a) * d)
            y = int(center[1] + math.sin(a) * d * 0.55)
            s = max(1, int(self.k * (1 - q) * 2))
            if 0 <= x < self.w and 0 <= y < self.h:
                surf.fill(color, (x, y, s, s))

    def _text_shadow(self, surf, img, x, y):
        sh = img.copy()
        sh.fill((0, 0, 0), special_flags=pygame.BLEND_RGB_MULT)
        sh.set_alpha(img.get_alpha() if img.get_alpha() is not None else 255)
        surf.blit(sh, (x + self.k, y + self.k))

    # ---------------- 본 그림 ----------------
    def draw(self, surf, t):
        k, px, w, h = self.k, self.px, self.w, self.h
        surf.fill(BG0)

        # 착지 흔들림 (로고/글자에만 적용)
        shake = 0
        if 0 <= t - T_LAND < 0.3:
            q = (t - T_LAND) / 0.3
            shake = int(math.sin(q * 34) * 3 * k * (1 - q))

        if t > T_LEAF:
            self._rays(surf, t, min(62, (t - T_LEAF) * 80))

        # 라즈베리 낙하 → 착지 때 찌그러짐
        if t >= T_DROP:
            bh = self.berry.get_height()
            img, bx, by = self.berry, self.logo_x, self.berry_y + shake
            if t < T_LAND:
                p = clamp01((t - T_DROP) / (T_LAND - T_DROP))
                by = int(-bh + (self.berry_y + bh) * (p * p))
                img = pygame.transform.rotate(self.berry, (1 - p) * 20)
                bx = self.logo_x - (img.get_width() - self.berry.get_width()) // 2
                by -= (img.get_height() - bh) // 2
            elif t - T_LAND < 0.22:
                q = (t - T_LAND) / 0.22
                sq = math.sin(q * math.pi) * 0.22
                nw, nh = int(self.berry.get_width() * (1 + sq)), int(bh * (1 - sq))
                img = pygame.transform.scale(self.berry, (nw, nh))
                bx = self.logo_x - (nw - self.berry.get_width()) // 2
                by = self.berry_y + (bh - nh) + shake
            surf.blit(img, (bx, by))

        # 착지 충격파 + 불꽃
        if 0 <= t - T_LAND < 0.5:
            q = (t - T_LAND) / 0.5
            foot = (self.logo_x + LOGO_W * px // 2, self.berry_y + LOGO_H * px - 2 * k)
            self._ring(surf, foot, int((6 + 60 * q) * k), 210 * (1 - q))
            self._sparks(surf, foot, t, q, color=LOGO_COLORS["H"])

        # 잎 펼치기 (안쪽 밑동을 축으로)
        if t >= T_LEAF:
            p = clamp01((t - T_LEAF) / 0.45)
            e = ease_out_back(p)
            ang = (1 - e) * 72
            for img, pivot_x, sign in ((self.leaf_l, LEAF_SPLIT * px, 1),
                                       (self.leaf_r, 0, -1)):
                img = img.copy()
                img.set_alpha(int(255 * clamp01(p * 2)))
                blit_rotated(surf, img, sign * ang,
                             (pivot_x, LEAF_ROWS * px),
                             (self.logo_x + LEAF_SPLIT * px,
                              self.logo_y + LEAF_ROWS * px + shake))

        # "Raspberry Pi" — 한 글자씩 올라오며 나타남
        ty = self.text_y + self.five_h - self.title_h + shake
        x = self.title_x
        for i, ch in enumerate(self.title):
            img = self.T.render(ch, WHITE, k, shadow=False)
            start = T_TEXT + i * 0.045
            p = clamp01((t - start) / 0.25)
            if p > 0:
                img.set_alpha(int(255 * p))
                dy = int((1 - p) * 7 * k)
                self._text_shadow(surf, img, x, ty + dy)
                surf.blit(img, (x, ty + dy))
            x += img.get_width()

        # 금색 "5" — 크게 쾅
        if t >= T_FIVE:
            p = clamp01((t - T_FIVE) / 0.33)
            img = self.T.render(self.five, GOLD, 2 * k, shadow=False)
            sc = 1 + (1 - ease_out_back(p, 2.4)) * 1.4
            if sc > 1.02:
                big = pygame.transform.scale(
                    img, (max(1, int(img.get_width() * sc)), max(1, int(img.get_height() * sc))))
                img = big
            img.set_alpha(int(255 * clamp01(p * 3)))
            r = img.get_rect(center=(self.five_mid[0], self.five_mid[1] + shake))
            self._text_shadow(surf, img, r.x, r.y)
            surf.blit(img, r)
            if p < 1:
                self._ring(surf, (self.five_mid[0], self.five_mid[1] + shake),
                           int((4 + 40 * p) * k), 200 * (1 - p), GOLD, flat=0.9)
                self._sparks(surf, self.five_mid, t, p, 7, GOLD)

        # 로고 위로 광택이 스윽
        if T_SHINE <= t < T_SHINE + 0.55:
            self._shine(surf, (t - T_SHINE) / 0.55, shake)

        # 아래 한 줄
        if t >= T_TAG:
            p = clamp01((t - T_TAG) / 0.4)
            img = self.T.render(self.tag, GRAY, k, zh=True, shadow=False)
            img.set_alpha(int(200 * p))
            surf.blit(img, ((w - img.get_width()) // 2, self.tag_y))

        # 등장 섬광
        if 0 <= t - T_FIVE < 0.14:
            fl = self._fx
            fl.fill((255, 255, 255, int(150 * (1 - (t - T_FIVE) / 0.14))))
            surf.blit(fl, (0, 0))

        surf.blit(self._scan, (0, 0))   # CRT 느낌 주사선

        # 전원 켜질 때 위아래로 열리는 가림막
        if t < 0.45:
            e = clamp01(t / 0.45)
            bar = int((h / 2) * (1 - e * e))
            if bar > 0:
                surf.fill((0, 0, 0), (0, 0, w, bar))
                surf.fill((0, 0, 0), (0, h - bar, w, bar))
                surf.fill(WHITE, (0, bar, w, max(1, k)))
                surf.fill(WHITE, (0, h - bar - max(1, k), w, max(1, k)))

        # 암전 → 메뉴로
        if t >= T_FADE:
            fade = self._fx
            fade.fill((0, 0, 0, int(255 * clamp01((t - T_FADE) / (LENGTH - T_FADE)))))
            surf.blit(fade, (0, 0))

    def _shine(self, surf, p, shake=0):
        """로고 모양으로 잘라낸 밝은 띠를 대각선으로 지나가게."""
        lw, lh = self.logo.get_size()
        band = pygame.Surface((lw, lh), pygame.SRCALPHA)
        x = int(-lw + p * lw * 2.6)
        bw = max(3, lw // 5)
        pygame.draw.polygon(band, (255, 255, 255, 150), [
            (x, lh), (x + bw, lh), (x + bw + lh // 2, 0), (x + lh // 2, 0)])
        band.blit(self.logo_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        surf.blit(band, (self.logo_x, self.logo_y + shake))
