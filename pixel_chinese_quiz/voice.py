"""USB 마이크 음성 감지 + 중국어 음성 인식.

- sounddevice 로 마이크 입력을 받아 RMS 음량으로 말소리 시작/끝을 감지(VAD)
- 녹음된 음성을 Google 음성인식(zh-CN, 인터넷 필요) 또는 Vosk(오프라인)로 텍스트 변환
"""
import json
import queue
import re
import threading
import time

import numpy as np

try:
    import sounddevice as sd
except Exception as e:  # PortAudio 가 없는 환경 등
    sd = None
    _SD_ERROR = str(e)
else:
    _SD_ERROR = ""

try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    from pypinyin import lazy_pinyin
except ImportError:
    lazy_pinyin = None

SAMPLE_RATE = 16000
BLOCK = 800  # 50ms


def list_input_devices():
    if sd is None:
        return []
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append((i, d["name"]))
    return out


def find_usb_mic():
    """이름에 USB 가 들어간 입력 장치를 우선 선택, 없으면 기본 장치(None)."""
    for idx, name in list_input_devices():
        if "usb" in name.lower():
            return idx
    return None


def device_name(index):
    if sd is None:
        return "마이크 없음"
    try:
        if index is None:
            index = sd.default.device[0]
        return sd.query_devices(index)["name"]
    except Exception:
        return "기본 마이크"


_PUNCT = re.compile(r"[\s,.!?;:，。！？；：、\"'“”‘’…·]+")


def _normalize(text):
    return _PUNCT.sub("", text or "")


def _tone_less(text):
    if lazy_pinyin is None:
        return None
    return "".join(lazy_pinyin(_normalize(text)))


def is_match(heard, target_hanzi):
    """인식 결과가 정답 한자와 같은지 판정 (한자 일치 또는 성조 없는 병음 일치)."""
    h, t = _normalize(heard), _normalize(target_hanzi)
    if not h:
        return False
    if t in h:
        return True
    hp, tp = _tone_less(h), _tone_less(t)
    return bool(hp and tp and tp in hp)


class VoiceListener:
    """백그라운드 스레드에서 한 번 듣고 결과를 돌려주는 리스너."""

    def __init__(self, device=None, threshold=0.02, vosk_model=None):
        self.device = device
        self.threshold = threshold
        self.level = 0.0          # 현재 음량 (0~1), UI 레벨 미터용
        self.state = "idle"       # idle / waiting / recording / recognizing / done / error
        self.result = None        # 인식된 텍스트
        self.error = ""
        self._thread = None
        self._vosk = None
        if vosk_model:
            try:
                from vosk import Model
                self._vosk = Model(vosk_model)
            except Exception as e:
                self.error = f"Vosk 모델 로드 실패: {e}"

    @property
    def available(self):
        return sd is not None and (sr is not None or self._vosk is not None)

    def unavailable_reason(self):
        """화면에 띄울 짧은 이유."""
        if sd is None:
            return "마이크 모듈(sounddevice) 없음"
        if sr is None and self._vosk is None:
            return "음성인식 패키지 없음 (터미널 참고)"
        return ""

    def install_hint(self):
        """터미널에 출력할 자세한 설치 안내."""
        if sd is None:
            return ("[음성] sounddevice 를 쓸 수 없습니다: " + _SD_ERROR + "\n"
                    "  sudo apt install libportaudio2\n"
                    "  pip install sounddevice --break-system-packages")
        if sr is None and self._vosk is None:
            return ("[음성] SpeechRecognition 패키지가 없습니다. 설치:\n"
                    "  pip install SpeechRecognition pypinyin --break-system-packages\n"
                    "  (라즈베리파이 OS 는 --break-system-packages 가 필요하거나 venv 사용)")
        return ""

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, max_seconds=6.0, silence_seconds=0.8, wait_seconds=5.0):
        if self.busy or not self.available:
            return
        self.result, self.error, self.level = None, "", 0.0
        self.state = "waiting"
        self._thread = threading.Thread(
            target=self._run, args=(max_seconds, silence_seconds, wait_seconds), daemon=True)
        self._thread.start()

    def _run(self, max_seconds, silence_seconds, wait_seconds):
        try:
            audio = self._record(max_seconds, silence_seconds, wait_seconds)
            if audio is None:
                self.state = "error"
                self.error = "소리가 감지되지 않았어요"
                return
            self.state = "recognizing"
            self.result = self._recognize(audio)
            self.state = "done"
        except Exception as e:
            self.state = "error"
            self.error = str(e)[:60]

    def _record(self, max_seconds, silence_seconds, wait_seconds):
        q = queue.Queue()

        def cb(indata, frames, t, status):
            q.put(indata[:, 0].copy())

        chunks, pre = [], []
        started = False
        silent_for = 0.0
        t0 = time.time()
        block_sec = BLOCK / SAMPLE_RATE
        with sd.InputStream(device=self.device, channels=1, samplerate=SAMPLE_RATE,
                            blocksize=BLOCK, dtype="float32", callback=cb):
            while True:
                block = q.get(timeout=2)
                rms = float(np.sqrt(np.mean(block ** 2)))
                self.level = min(1.0, rms * 8)
                loud = rms > self.threshold
                if not started:
                    pre = (pre + [block])[-6:]  # 말 시작 직전 300ms 보존
                    if loud:
                        started = True
                        self.state = "recording"
                        chunks = list(pre)
                        t0 = time.time()
                    elif time.time() - t0 > wait_seconds:
                        return None
                    continue
                chunks.append(block)
                silent_for = 0.0 if loud else silent_for + block_sec
                if silent_for >= silence_seconds or time.time() - t0 > max_seconds:
                    break
        self.level = 0.0
        pcm = np.concatenate(chunks)
        return (np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes()

    def _recognize(self, pcm16):
        if self._vosk is not None:
            from vosk import KaldiRecognizer
            rec = KaldiRecognizer(self._vosk, SAMPLE_RATE)
            rec.AcceptWaveform(pcm16)
            return json.loads(rec.FinalResult()).get("text", "").replace(" ", "")
        data = sr.AudioData(pcm16, SAMPLE_RATE, 2)
        try:
            return sr.Recognizer().recognize_google(data, language="zh-CN")
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            raise RuntimeError(f"인식 서버 오류(인터넷 확인): {e}")
