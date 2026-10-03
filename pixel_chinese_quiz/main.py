"""픽셀풍 중국어 레슨 퀴즈 (pygame) + USB 마이크 말하기 연습.

실행:  python main.py
옵션:  --list-devices        입력 장치 목록 출력
       --device N            사용할 마이크 장치 번호 (기본: 이름에 USB 가 들어간 장치 자동 선택)
       --threshold 0.02      말소리 감지 음량 기준 (주변이 시끄러우면 올리기)
       --vosk-model PATH     오프라인 인식용 Vosk 중국어 모델 폴더
       --no-voice            목소리(마이크) 기능 끄기 — 메뉴에서 V 키로도 켜고 끌 수 있음
       --scale 3             창 확대 배율
"""
import argparse
import glob
import json
import math
import os
import random
import sys

import numpy as np
import pygame

import voice

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 400, 240            # 내부 저해상도 캔버스 (정수 배율로 확대 → 픽셀 느낌)
QUESTIONS_PER_ROUND = 10
FPS = 30

# 레트로 팔레트
BG = (24, 20, 37)
BG2 = (38, 32, 58)
PANEL = (58, 68, 102)
PANEL_DARK = (38, 43, 68)
WHITE = (244, 244, 244)
GRAY = (139, 155, 180)
RED = (228, 59, 68)
GOLD = (254, 174, 52)
GREEN = (99, 199, 77)
CYAN = (44, 232, 245)
PINK = (246, 117, 122)

MODES = [
    ("meaning", "뜻 맞히기"),
    ("pinyin", "병음 맞히기"),
    ("speak", "말하기 연습"),
]

PANDA = [
    "..KK....KK..",
    ".KKKWWWWKKK.",
    "..WWWWWWWW..",
    ".WWKKWWKKWW.",
    ".WKKWWWWKKW.",
    ".WWWWKKWWWW.",
    "..WWWRRWWW..",
    "...WWWWWW...",
    "..KKWWWWKK..",
    ".KKKWWWWKKK.",
    ".KK.WWWW.KK.",
    "...KK..KK...",
]
PANDA_COLORS = {"K": (20, 16, 24), "W": WHITE, "R": PINK}


# ---------------------------------------------------------------- 폰트 ----
FONT_CANDIDATES = [
    # 픽셀 폰트 우선 (assets 폴더에 넣으면 최우선 사용)
    *sorted(glob.glob(os.path.join(HERE, "assets", "*.ttf"))),
    *sorted(glob.glob(os.path.join(HERE, "assets", "*.otf"))),
    "/usr/share/fonts/opentype/unifont/unifont.otf",
    "/usr/share/fonts/truetype/unifont/unifont.ttf",
    "/usr/share/fonts/opentype/unifont/unifont_jp.otf",
]
FONT_NAMES = [
    "unifont", "fusionpixel", "galmuri11",
    "notosanscjksc", "notosanscjkkr", "notosanssc", "notosanskr",
    "wenquanyizenhei", "wenquanyimicrohei",
    "microsoftyahei", "simhei", "simsun", "malgungothic", "gulim",
    "pingfangsc", "applesdgothicneo", "hiraginosansgb",
]


_NOTDEF = {}


def _notdef(font):
    """폰트에 없는 글자를 그릴 때 나오는 □ 모양 (비교용)."""
    if font not in _NOTDEF:
        img = font.render("\uffff", False, (255, 255, 255), (0, 0, 0))
        _NOTDEF[font] = (img.get_size(), pygame.image.tostring(img, "RGB"))
    return _NOTDEF[font]


def _covers(font, text):
    """폰트가 text 의 모든 글자를 가지고 있는지.

    pygame/SDL_ttf 버전에 따라 metrics() 가 없는 글자도 OK 로 돌려주므로,
    실제로 그려서 □(notdef) 모양과 같은지도 비교한다.
    """
    try:
        if any(m is None for m in font.metrics(text)):
            return False
        nd = _notdef(font)
        for ch in text:
            if ch.isspace():
                continue
            img = font.render(ch, False, (255, 255, 255), (0, 0, 0))
            if (img.get_size(), pygame.image.tostring(img, "RGB")) == nd:
                return False
        return True
    except Exception:
        return False


def _fc_list(lang):
    """Linux(라즈베리파이 등): fontconfig 로 해당 언어 폰트 파일 찾기."""
    try:
        import subprocess
        out = subprocess.run(["fc-list", f":lang={lang}", "file"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return []
    return sorted(line.split(":")[0].strip() for line in out.splitlines() if line.strip())

# 어떤 폰트에도 없는 기호는 ASCII 로 바꿔서 □(두부) 대신 보이게
SYMBOL_FALLBACK = {"▶": ">", "◀": "<", "★": "*", "☆": "-", "●": "*", "·": "-",
                   "↑": "^", "↓": "v", "←": "<", "→": ">", "↔": "<>", "…": "..."}


def load_fonts(size=16):
    """쓸 수 있는 폰트 목록 (assets 의 픽셀 폰트 → 시스템 CJK 폰트 순)."""
    paths = [p for p in FONT_CANDIDATES if os.path.exists(p)]
    for name in FONT_NAMES:
        try:
            p = pygame.font.match_font(name)
        except Exception:
            p = None
        if p:
            paths.append(p)
    paths += _fc_list("ko") + _fc_list("zh-cn")
    fonts, names, seen = [], [], set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        try:
            f = pygame.font.Font(p, size)
        except Exception as e:
            print(f"[폰트] 로드 실패 {p}: {e}")
            continue
        # 한글이나 한자 중 하나라도 실제로 있는 폰트만 사용
        if _covers(f, "가") or _covers(f, "你"):
            fonts.append(f)
            names.append(p)
        if len(fonts) >= 6:
            break
    fonts.append(pygame.font.Font(None, size))
    names.append("pygame 기본 폰트")
    ko = next((n for f, n in zip(fonts, names) if _covers(f, "가나다")), None)
    zh = next((n for f, n in zip(fonts, names) if _covers(f, "你好谢")), None)
    print(f"[폰트] 한글: {ko or '없음!'}")
    print(f"[폰트] 한자: {zh or '없음!'}")
    if not (ko and zh):
        print("[폰트] 글자가 □로 보이면: git pull 로 assets/unifont.otf 를 받거나 "
              "sudo apt install fonts-unifont fonts-noto-cjk")
    return fonts


class Text:
    """글자마다 그 글자를 가진 폰트를 골라 이어 붙여 그린다 (빠진 글자 □ 방지)."""

    def __init__(self):
        self.fonts = load_fonts(16)
        self.zh_first = next((f for f in self.fonts if _covers(f, "你好谢饺")), self.fonts[0])
        self.cache = {}
        self.char_font = {}

    def _font_for(self, ch, zh):
        key = (ch, zh)
        if key not in self.char_font:
            order = ([self.zh_first] if zh else []) + self.fonts
            self.char_font[key] = next((f for f in order if _covers(f, ch)), None)
        return self.char_font[key]

    def _runs(self, s, zh):
        runs = []  # [(font, text)]
        for ch in s:
            f = self._font_for(ch, zh)
            if f is None:
                ch = SYMBOL_FALLBACK.get(ch, "?")
                f = self.fonts[-1]
            if runs and runs[-1][0] is f:
                runs[-1][1] += ch
            else:
                runs.append([f, ch])
        return runs

    def _plain(self, s, color, zh):
        parts = [f.render(t, False, color) for f, t in self._runs(s, zh)]  # 안티앨리어싱 OFF → 도트 느낌
        if not parts:
            return pygame.Surface((1, 1), pygame.SRCALPHA)
        h = max(p.get_height() for p in parts)
        out = pygame.Surface((sum(p.get_width() for p in parts), h), pygame.SRCALPHA)
        x = 0
        for p in parts:
            out.blit(p, (x, h - p.get_height()))
            x += p.get_width()
        return out

    def render(self, s, color=WHITE, scale=1, zh=False, shadow=True):
        key = (s, color, scale, zh, shadow)
        if key in self.cache:
            return self.cache[key]
        img = self._plain(s, color, zh)
        if scale != 1:
            img = pygame.transform.scale(img, (img.get_width() * scale, img.get_height() * scale))
        if shadow:
            sh = self._plain(s, (10, 8, 16), zh)
            if scale != 1:
                sh = pygame.transform.scale(sh, img.get_size())
            out = pygame.Surface((img.get_width() + scale, img.get_height() + scale), pygame.SRCALPHA)
            out.blit(sh, (scale, scale))
            out.blit(img, (0, 0))
            img = out
        self.cache[key] = img
        return img

    def draw(self, surf, s, pos, color=WHITE, scale=1, zh=False, center=False, shadow=True):
        img = self.render(s, color, scale, zh, shadow)
        x, y = pos
        if center:
            x -= img.get_width() // 2
        surf.blit(img, (x, y))
        return img.get_rect(topleft=(x, y))


# --------------------------------------------------------------- 사운드 ---
def make_beep(freqs, dur=0.08, vol=0.25):
    try:
        rate = pygame.mixer.get_init()[0]
    except Exception:
        return None
    parts = []
    for f in freqs:
        t = np.arange(int(rate * dur)) / rate
        wave = np.sign(np.sin(2 * math.pi * f * t)) * vol  # 사각파 = 8비트 소리
        fade = np.linspace(1, 0, len(t)) ** 0.5
        parts.append(wave * fade)
    data = (np.concatenate(parts) * 32767).astype(np.int16)
    channels = pygame.mixer.get_init()[2]
    if channels == 2:
        data = np.column_stack([data, data])
    return pygame.sndarray.make_sound(np.ascontiguousarray(data))


class Sfx:
    def __init__(self):
        self.sounds = {}
        try:
            pygame.mixer.init(22050, -16, 2, 512)
            self.sounds = {
                "move": make_beep([660], 0.03, 0.15),
                "ok": make_beep([523, 659, 784, 1046], 0.07),
                "ng": make_beep([220, 165], 0.15),
                "start": make_beep([392, 523], 0.06),
                "mic": make_beep([880], 0.05, 0.15),
                "clear": make_beep([523, 659, 784, 659, 784, 1046], 0.1),
            }
        except Exception:
            pass

    def play(self, name):
        s = self.sounds.get(name)
        if s:
            s.play()


# --------------------------------------------------------------- 그리기 ---
def panel(surf, rect, color=PANEL, border=WHITE):
    """두꺼운 도트 테두리 패널 (모서리 깎인 레트로 박스)."""
    x, y, w, h = rect
    pygame.draw.rect(surf, (10, 8, 16), (x + 2, y + 2, w, h))
    pygame.draw.rect(surf, border, (x + 1, y, w - 2, h))
    pygame.draw.rect(surf, border, (x, y + 1, w, h - 2))
    pygame.draw.rect(surf, color, (x + 2, y + 2, w - 4, h - 4))
    pygame.draw.rect(surf, PANEL_DARK if color == PANEL else color, (x + 2, y + h - 4, w - 4, 2))


def draw_sprite(surf, rows, colors, pos, px=2):
    for j, row in enumerate(rows):
        for i, c in enumerate(row):
            if c in colors:
                pygame.draw.rect(surf, colors[c], (pos[0] + i * px, pos[1] + j * px, px, px))


def draw_heart(surf, pos, filled=True):
    rows = [".XX.XX.", "XXXXXXX", "XXXXXXX", ".XXXXX.", "..XXX..", "...X..."]
    draw_sprite(surf, rows, {"X": RED if filled else PANEL_DARK}, pos, 1)


class Stars:
    def __init__(self):
        self.stars = [(random.randrange(W), random.randrange(H), random.random() * 6) for _ in range(40)]

    def draw(self, surf, t):
        surf.fill(BG)
        for y in range(0, H, 8):
            if (y // 8) % 2:
                pygame.draw.rect(surf, BG2, (0, y, W, 8))
        for x, y, p in self.stars:
            if math.sin(t * 2 + p) > 0.2:
                surf.set_at((x, y), GRAY)


# --------------------------------------------------------------- 게임 ----
class Game:
    def __init__(self, args):
        pygame.init()
        self.scale = args.scale
        self.window = pygame.display.set_mode((W * self.scale, H * self.scale))
        pygame.display.set_caption("像素中文 · 픽셀 중국어 퀴즈")
        self.canvas = pygame.Surface((W, H))
        self.clock = pygame.time.Clock()
        self.text = Text()
        self.sfx = Sfx()
        self.stars = Stars()

        with open(os.path.join(HERE, "words.json"), encoding="utf-8") as f:
            self.lessons = json.load(f)["lessons"]
        all_words = [w for l in self.lessons for w in l["words"]]
        self.lessons.append({"title": "★ 전체 복습", "words": all_words})
        self.all_words = all_words

        device = args.device if args.device is not None else voice.find_usb_mic()
        self.mic = voice.VoiceListener(device, args.threshold, args.vosk_model)
        self.mic_name = voice.device_name(device) if voice.sd else "없음"
        self.voice_on = not args.no_voice

        self.scene = "menu"
        self.lesson_i = 0
        self.mode_i = 0
        self.t = 0.0
        self.running = True

    # ---------------- 라운드 진행 ----------------
    def start_round(self):
        lesson = self.lessons[self.lesson_i]
        words = lesson["words"][:]
        random.shuffle(words)
        self.queue = (words * 3)[:min(QUESTIONS_PER_ROUND, max(len(words), 1) * 3)]
        self.q_index = 0
        self.score = 0
        self.streak = 0
        self.best_streak = 0
        self.lives = 3
        self.wrong = []
        self.scene = "quiz"
        self.sfx.play("start")
        self.next_question()

    @property
    def mode(self):
        return self.modes[self.mode_i][0]

    @property
    def modes(self):
        """목소리 끔 상태면 말하기 모드를 뺀다."""
        return MODES if self.voice_on else [m for m in MODES if m[0] != "speak"]

    def next_question(self):
        if self.q_index >= len(self.queue) or self.lives <= 0:
            self.scene = "result"
            self.sfx.play("clear" if self.lives > 0 else "ng")
            return
        self.word = self.queue[self.q_index]
        self.cursor = 0
        self.feedback = None      # (맞음여부, 타이머)
        self.heard = ""
        self.tries = 0
        if self.mode in ("meaning", "pinyin"):
            key = "meaning" if self.mode == "meaning" else "pinyin"
            pool = list({w[key] for w in self.all_words if w[key] != self.word[key]})
            choices = random.sample(pool, 3) + [self.word[key]]
            random.shuffle(choices)
            self.choices = choices
            self.answer_key = key
        else:
            self.choices = []

    def answer(self, correct):
        self.feedback = [correct, 1.4 if correct else 2.2]
        if correct:
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            self.score += 100 + 20 * (self.streak - 1)
            self.sfx.play("ok")
        else:
            self.streak = 0
            self.lives -= 1
            if self.word not in self.wrong:
                self.wrong.append(self.word)
            self.sfx.play("ng")

    def pick(self, i):
        if self.feedback or i >= len(self.choices):
            return
        self.cursor = i
        self.answer(self.choices[i] == self.word[self.answer_key])

    def listen(self):
        if self.feedback or self.mic.busy:
            return
        if not self.mic.available:
            self.heard = self.mic.unavailable_reason()
            return
        self.heard = ""
        self.sfx.play("mic")
        self.mic.start()

    def update(self, dt):
        self.t += dt
        if self.scene != "quiz":
            return
        if self.mode == "speak" and self.mic.state in ("done", "error") and not self.mic.busy \
                and not self.feedback:
            if self.mic.state == "error":
                self.heard = self.mic.error
                self.mic.state = "idle"
            else:
                self.heard = self.mic.result or "(알아듣지 못했어요)"
                self.mic.state = "idle"
                self.tries += 1
                if voice.is_match(self.mic.result, self.word["hanzi"]):
                    self.answer(True)
                elif self.tries >= 3:
                    self.answer(False)
                else:
                    self.sfx.play("ng")
        if self.feedback:
            self.feedback[1] -= dt
            if self.feedback[1] <= 0:
                self.q_index += 1
                self.next_question()

    # ---------------- 입력 ----------------
    def handle_key(self, key):
        if self.scene == "menu":
            if key in (pygame.K_UP, pygame.K_w):
                self.lesson_i = (self.lesson_i - 1) % len(self.lessons); self.sfx.play("move")
            elif key in (pygame.K_DOWN, pygame.K_s):
                self.lesson_i = (self.lesson_i + 1) % len(self.lessons); self.sfx.play("move")
            elif key in (pygame.K_LEFT, pygame.K_a):
                self.mode_i = (self.mode_i - 1) % len(self.modes); self.sfx.play("move")
            elif key in (pygame.K_RIGHT, pygame.K_d):
                self.mode_i = (self.mode_i + 1) % len(self.modes); self.sfx.play("move")
            elif key in (pygame.K_RETURN, pygame.K_SPACE):
                self.start_round()
            elif key == pygame.K_v:
                self.voice_on = not self.voice_on
                self.mode_i %= len(self.modes)
                self.sfx.play("mic" if self.voice_on else "move")
            elif key == pygame.K_ESCAPE:
                self.running = False
        elif self.scene == "quiz":
            if key == pygame.K_ESCAPE:
                self.scene = "menu"
            elif self.feedback and key in (pygame.K_RETURN, pygame.K_SPACE):
                self.feedback[1] = 0
            elif self.mode == "speak":
                if key == pygame.K_SPACE:
                    self.listen()
                elif key == pygame.K_TAB and not self.mic.busy:  # 건너뛰기
                    self.answer(False)
            else:
                if key in (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4):
                    self.pick(key - pygame.K_1)
                elif key in (pygame.K_UP, pygame.K_LEFT):
                    self.cursor = (self.cursor - 1) % 4; self.sfx.play("move")
                elif key in (pygame.K_DOWN, pygame.K_RIGHT):
                    self.cursor = (self.cursor + 1) % 4; self.sfx.play("move")
                elif key in (pygame.K_RETURN, pygame.K_SPACE):
                    self.pick(self.cursor)
        elif self.scene == "result":
            if key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_ESCAPE):
                self.scene = "menu"
            elif key == pygame.K_r:
                self.start_round()

    def handle_click(self, pos):
        x, y = pos[0] // self.scale, pos[1] // self.scale
        if self.scene == "quiz" and self.choices:
            for i, r in enumerate(self.choice_rects()):
                if r.collidepoint(x, y):
                    self.pick(i)
        elif self.scene == "quiz" and self.mode == "speak":
            self.listen()

    def choice_rects(self):
        return [pygame.Rect(16 + (i % 2) * 188, 150 + (i // 2) * 36, 180, 30) for i in range(4)]

    # ---------------- 화면 ----------------
    def draw(self):
        c = self.canvas
        self.stars.draw(c, self.t)
        getattr(self, "draw_" + self.scene)(c)
        pygame.transform.scale(c, self.window.get_size(), self.window)
        pygame.display.flip()

    def draw_menu(self, c):
        T = self.text
        bob = int(math.sin(self.t * 3) * 2)
        T.draw(c, "像素中文", (W // 2, 8 + bob), GOLD, 2, zh=True, center=True)
        T.draw(c, "픽셀 중국어 퀴즈", (W // 2, 44), CYAN, center=True)
        draw_sprite(c, PANDA, PANDA_COLORS, (24, 14 + bob), 3)
        draw_sprite(c, PANDA, PANDA_COLORS, (W - 60, 14 - bob), 3)

        panel(c, (60, 62, 280, 18 * len(self.lessons) + 8))
        for i, l in enumerate(self.lessons):
            y = 66 + i * 18
            sel = i == self.lesson_i
            if sel:
                pygame.draw.rect(c, PANEL_DARK, (64, y - 1, 272, 18))
                if int(self.t * 4) % 2:
                    T.draw(c, "▶", (70, y), GOLD)
            T.draw(c, l["title"], (88, y), GOLD if sel else WHITE)
            T.draw(c, f"{len(l['words'])}단어", (286, y), GRAY)

        my = 62 + 18 * len(self.lessons) + 14
        T.draw(c, f"◀  {self.modes[self.mode_i][1]}  ▶", (W // 2, my), PINK, center=True)
        T.draw(c, "↑↓ 레슨  ←→ 모드  ENTER 시작", (W // 2, H - 34), GRAY, center=True)
        mic = self.mic_name if len(self.mic_name) < 22 else self.mic_name[:20] + ".."
        if self.voice_on:
            T.draw(c, f"MIC: {mic}  (V 끄기)", (W // 2, H - 18), GREEN if voice.sd else RED, center=True)
        else:
            T.draw(c, "목소리 OFF  (V 켜기)", (W // 2, H - 18), GRAY, center=True)

    def draw_hud(self, c):
        T = self.text
        T.draw(c, self.lessons[self.lesson_i]["title"], (8, 4), CYAN)
        T.draw(c, f"{self.q_index + 1}/{len(self.queue)}", (W // 2, 4), WHITE, center=True)
        T.draw(c, f"{self.score:05d}", (W - 52, 4), GOLD)
        for i in range(3):
            draw_heart(c, (W - 90 + i * 10, 9), i < self.lives)
        # 진행 바
        pygame.draw.rect(c, PANEL_DARK, (8, 22, W - 16, 4))
        pw = int((W - 16) * self.q_index / max(1, len(self.queue)))
        pygame.draw.rect(c, GREEN, (8, 22, pw, 4))
        if self.streak >= 2:
            T.draw(c, f"{self.streak} COMBO!", (W - 8 - 72, 28), PINK)

    def draw_quiz(self, c):
        T = self.text
        self.draw_hud(c)
        w = self.word
        panel(c, (16, 34, W - 32, 106))
        if self.mode == "meaning":
            T.draw(c, "이 단어의 뜻은?", (24, 40), GRAY)
            T.draw(c, w["hanzi"], (W // 2, 58), WHITE, 3, zh=True, center=True)
            T.draw(c, w["pinyin"], (W // 2, 114), GOLD, center=True)
        elif self.mode == "pinyin":
            T.draw(c, "알맞은 병음은?", (24, 40), GRAY)
            T.draw(c, w["hanzi"], (W // 2, 58), WHITE, 3, zh=True, center=True)
            T.draw(c, w["meaning"], (W // 2, 114), CYAN, center=True)
        else:
            T.draw(c, "소리 내어 말해 보세요!", (24, 40), GRAY)
            T.draw(c, w["hanzi"], (W // 2, 56), WHITE, 3, zh=True, center=True)
            T.draw(c, f"{w['pinyin']}  ·  {w['meaning']}", (W // 2, 114), GOLD, center=True)

        if self.choices:
            for i, (r, ch) in enumerate(zip(self.choice_rects(), self.choices)):
                color = PANEL
                if self.feedback:
                    if ch == w[self.answer_key]:
                        color = (46, 120, 60)
                    elif i == self.cursor:
                        color = (140, 40, 50)
                border = GOLD if i == self.cursor and not self.feedback else WHITE
                panel(c, r, color, border)
                T.draw(c, f"{i + 1}", (r.x + 8, r.y + 7), GOLD)
                T.draw(c, ch, (r.x + 24, r.y + 7), WHITE)
        else:
            self.draw_mic(c)

        if self.feedback:
            ok = self.feedback[0]
            mark = "正确!" if ok else "错了…"
            jump = int(abs(math.sin(self.t * 8)) * 4) if ok else 0
            panel(c, (16, 34, W - 32, 106), (30, 70, 45) if ok else (80, 28, 40), GREEN if ok else RED)
            T.draw(c, mark, (W // 2, 46 - jump), GREEN if ok else RED, 3, zh=True, center=True)
            T.draw(c, f"{w['hanzi']}  {w['pinyin']}  {w['meaning']}", (W // 2, 108), WHITE, center=True)

    def draw_mic(self, c):
        T = self.text
        panel(c, (16, 146, W - 32, 74), PANEL_DARK)
        st = self.mic.state
        # 레벨 미터 (도트 막대)
        bars = 24
        lvl = self.mic.level if st in ("waiting", "recording") else 0
        for i in range(bars):
            on = i < int(lvl * bars * 1.5)
            col = (GREEN if i < 14 else GOLD if i < 20 else RED) if on else PANEL
            h = 4 + i // 3
            pygame.draw.rect(c, col, (W - 30 - (bars - i) * 6, 196 - h, 4, h))
        # 마이크 아이콘
        mic_rows = [".XX.", "XXXX", "XXXX", "XXXX", ".XX.", "X..X", ".XX.", "..X."]
        col = RED if st == "recording" and int(self.t * 6) % 2 else WHITE
        draw_sprite(c, mic_rows, {"X": col}, (26, 160), 3)

        msg = {
            "idle": "SPACE / 클릭 → 말하기",
            "waiting": "듣는 중... 말해 보세요!",
            "recording": "● 녹음 중",
            "recognizing": "인식 중" + "." * (int(self.t * 3) % 4),
        }.get(st, "")
        T.draw(c, msg, (46, 154), WHITE)
        if self.heard:
            T.draw(c, f"들린 말: {self.heard}"[:40], (46, 200), PINK, zh=bool(self.mic.result))
        T.draw(c, f"기회 {3 - self.tries}   TAB 건너뛰기", (46, 174), GRAY)

    def draw_result(self, c):
        T = self.text
        total = len(self.queue)
        correct = total - len(self.wrong) if self.lives > 0 else self.q_index - len(self.wrong)
        title = "LESSON CLEAR!" if self.lives > 0 else "GAME OVER"
        T.draw(c, title, (W // 2, 10), GOLD if self.lives > 0 else RED, 2, center=True)
        panel(c, (40, 48, W - 80, 150))
        stars = 3 if not self.wrong and self.lives > 0 else 2 if self.lives >= 2 else 1 if self.lives else 0
        T.draw(c, "★" * stars + "☆" * (3 - stars), (W // 2, 56), GOLD, 2, center=True)
        T.draw(c, f"점수 {self.score}   맞힌 개수 {max(correct, 0)}/{total}   최고 콤보 {self.best_streak}",
               (W // 2, 92), WHITE, center=True)
        if self.wrong:
            T.draw(c, "복습할 단어", (52, 114), PINK)
            for i, wd in enumerate(self.wrong[:6]):
                x, y = 52 + (i % 2) * 150, 132 + (i // 2) * 20
                r = T.draw(c, wd["hanzi"], (x, y), WHITE, zh=True)
                T.draw(c, wd["meaning"], (r.right + 6, y), GRAY)
        else:
            T.draw(c, "완벽해요! 太棒了!", (W // 2, 140), GREEN, center=True)
        draw_sprite(c, PANDA, PANDA_COLORS, (16, 8 + int(math.sin(self.t * 4) * 2)), 3)
        draw_sprite(c, PANDA, PANDA_COLORS, (W - 52, 8 - int(math.sin(self.t * 4) * 2)), 3)
        T.draw(c, "ENTER 메뉴   R 다시하기", (W // 2, H - 30), GRAY, center=True)

    # ---------------- 루프 ----------------
    def run(self):
        while self.running:
            dt = self.clock.tick(FPS) / 1000
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    self.running = False
                elif e.type == pygame.KEYDOWN:
                    self.handle_key(e.key)
                elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                    self.handle_click(e.pos)
            self.update(dt)
            self.draw()
        pygame.quit()


def main():
    p = argparse.ArgumentParser(description="픽셀 중국어 퀴즈")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--device", type=int, default=None)
    p.add_argument("--threshold", type=float, default=0.02)
    p.add_argument("--vosk-model", default=None)
    p.add_argument("--no-voice", action="store_true", help="목소리(마이크) 기능 끄기")
    p.add_argument("--scale", type=int, default=3)
    args = p.parse_args()
    if args.list_devices:
        devs = voice.list_input_devices()
        if not devs:
            print("입력 장치를 찾지 못했습니다.", voice._SD_ERROR)
        for i, name in devs:
            print(f"[{i}] {name}{'   <- USB' if 'usb' in name.lower() else ''}")
        return
    Game(args).run()


if __name__ == "__main__":
    sys.exit(main())
