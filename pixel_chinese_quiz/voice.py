"""USB 마이크 음성 감지 + 중국어 음성 인식.

- sounddevice 로 마이크 입력을 받아 RMS 음량으로 말소리 시작/끝을 감지(VAD)
- 녹음된 음성을 Google 음성인식(zh-CN, 인터넷 필요) 또는 Vosk(오프라인)로 텍스트 변환
"""
import json
import math
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


def resample(x, src_rate, dst_rate):
    """간단한 선형 보간 리샘플링 (음성 인식용으로 충분)."""
    if src_rate == dst_rate or len(x) == 0:
        return x
    n = int(round(len(x) * dst_rate / src_rate))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def level_from_rms(rms):
    """RMS → 0~1 레벨 (dB 눈금: -60dB=0, 0dB=1). 작은 소리도 막대가 보이게."""
    if rms <= 1e-6:
        return 0.0
    return max(0.0, min(1.0, (20 * math.log10(rms) + 60) / 60))


def probe_input(device):
    """마이크가 지원하는 (샘플레이트, 채널 수) 찾기.

    USB 마이크는 16000Hz 를 못 받는 경우가 많아서(44100/48000 만 지원)
    되는 설정으로 녹음한 뒤 16000Hz 로 변환한다.
    """
    try:
        info = sd.query_devices(device, "input")
        default_rate = int(info.get("default_samplerate") or 48000)
        max_ch = int(info.get("max_input_channels") or 1)
    except Exception:
        default_rate, max_ch = 48000, 1
    rates = []
    for r in (SAMPLE_RATE, default_rate, 48000, 44100, 32000, 22050, 8000):
        if r not in rates:
            rates.append(r)
    last_err = None
    for ch in sorted({1, max(1, min(max_ch, 2))}):
        for r in rates:
            try:
                sd.check_input_settings(device=device, channels=ch, samplerate=r, dtype="float32")
            except Exception as e:
                last_err = e
                continue
            return r, ch
    raise RuntimeError(f"마이크 설정을 못 찾음: {last_err}")


class MicMonitor:
    """진단 화면용: 마이크를 계속 열어 두고 음량을 실시간으로 측정."""

    def __init__(self, device):
        self.device = device
        self.rms = 0.0
        self.peak = 0.0       # 최근 최대값 (천천히 떨어짐)
        self.max_rms = 0.0    # 열린 뒤 최대값
        self.blocks = 0       # 받은 오디오 블록 수 (0 이면 데이터가 안 들어옴)
        self.config = None
        self.error = ""
        self._stream = None
        try:
            if sd is None:
                raise RuntimeError("sounddevice 없음")
            rate, ch = self.config = probe_input(device)

            def cb(indata, frames, t, status):
                r = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
                self.rms = r
                self.peak = max(r, self.peak * 0.9)
                self.max_rms = max(self.max_rms, r)
                self.blocks += 1

            self._stream = sd.InputStream(device=device, channels=ch, samplerate=rate,
                                          blocksize=int(rate * 0.05), dtype="float32", callback=cb)
            self._stream.start()
        except Exception as e:
            self.error = str(e)

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


def _run(cmd):
    try:
        import subprocess
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None


def diagnose_cli(seconds=2.0):
    """터미널용 USB 마이크 진단 (python main.py --diagnose)."""
    print("=== USB 마이크 진단 ===")
    usb = _run(["lsusb"])
    if usb is not None:
        lines = [l for l in usb.splitlines() if any(k in l.lower() for k in ("audio", "mic", "sound", "usb pnp"))]
        print("[1] lsusb (오디오 관련):")
        print("    " + ("\n    ".join(lines) if lines else "없음 → USB 마이크가 인식 안 됨 (다른 포트에 꽂아 보세요)"))
    arec = _run(["arecord", "-l"])
    if arec is not None:
        cards = [l for l in arec.splitlines() if l.startswith(("card", "카드"))]
        print("[2] arecord -l (ALSA 녹음 장치):")
        print("    " + ("\n    ".join(cards) if cards else "없음 → 시스템이 녹음 장치를 못 찾음"))
    if sd is None:
        print("[3] sounddevice 사용 불가:", _SD_ERROR)
        print("    sudo apt install libportaudio2 && pip install sounddevice --break-system-packages")
        return
    print("[3] sounddevice 입력 장치:")
    devs = list_input_devices()
    if not devs:
        print("    없음")
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    for i, name in devs:
        mark = " (기본)" if i == default_in else ""
        print(f"    [{i}] {name}{mark}{'   <- USB' if 'usb' in name.lower() else ''}")
    print(f"[4] 장치별 {seconds:.0f}초 녹음 테스트 — 지금 마이크에 대고 말해 보세요!")
    for i, name in devs:
        try:
            rate, ch = probe_input(i)
            data = sd.rec(int(rate * seconds), samplerate=rate, channels=ch, dtype="float32", device=i)
            sd.wait()
            rms = float(np.sqrt(np.mean(data ** 2)))
            peak = float(np.max(np.abs(data)))
            if peak < 1e-4:
                verdict = "무음 (0) → 음소거됐거나 다른 장치"
            elif peak < 0.02:
                verdict = "너무 작음 → alsamixer 에서 F6 로 USB 선택, F4(Capture) 볼륨 올리기"
            else:
                verdict = "OK! 소리 들어옴"
            print(f"    [{i}] {rate}Hz {ch}ch  RMS={rms:.4f}  PEAK={peak:.3f}  → {verdict}")
        except Exception as e:
            print(f"    [{i}] 열기 실패: {e}")
    print("팁: 잘 되는 장치 번호로  python3 main.py --device 번호  또는 게임 메뉴에서 M 키(마이크 진단)로 선택·저장")


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
        self._stream = None
        self._listening = False
        self._noise = None        # 주변 소음 RMS (미리 열어 둔 마이크로 측정)
        self._vosk_result = ""
        self._recognizer = sr.Recognizer() if sr else None
        self._vosk = None
        if vosk_model:
            try:
                from vosk import Model
                self._vosk = Model(vosk_model)
                print(f"[음성] 오프라인 인식(Vosk) 사용: {vosk_model}")
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

    @property
    def engine(self):
        return "vosk" if self._vosk is not None else "google"

    # ---------------- 마이크 미리 열어두기 ----------------
    def warm(self):
        """마이크 스트림을 미리 열어 둔다 (SPACE 누르는 순간 바로 녹음 + 주변 소음 측정)."""
        if self._stream is not None or sd is None:
            return
        rate, channels = self._input_config()
        self._rate = rate
        self._q = queue.Queue(maxsize=400)

        def cb(indata, frames, t, status):
            data = indata.mean(axis=1).astype(np.float32)  # 여러 채널이면 평균해서 모노로
            if self._listening:
                try:
                    self._q.put_nowait(data)
                except queue.Full:
                    pass
            else:  # 듣지 않는 동안 주변 소음 크기를 계속 측정
                rms = float(np.sqrt(np.mean(data ** 2)))
                self._noise = rms if self._noise is None else self._noise * 0.95 + rms * 0.05

        self._stream = sd.InputStream(device=self.device, channels=channels, samplerate=rate,
                                      blocksize=int(rate * 0.05), dtype="float32", callback=cb)
        self._stream.start()

    def cool(self):
        """미리 열어 둔 마이크 닫기 (진단 화면 등 다른 곳에서 마이크를 쓸 때)."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream = None

    def start(self, max_seconds=4.0, silence_seconds=0.5, wait_seconds=6.0):
        if self.busy or not self.available:
            return
        self.result, self.error, self.level = None, "", 0.0
        self.state = "waiting"
        self._thread = threading.Thread(
            target=self._run, args=(max_seconds, silence_seconds, wait_seconds), daemon=True)
        self._thread.start()

    def _run(self, max_seconds, silence_seconds, wait_seconds):
        try:
            t0 = time.time()
            audio = self._record(max_seconds, silence_seconds, wait_seconds)
            if audio is None:
                self.state = "error"
                self.error = "소리가 감지되지 않았어요"
                return
            t1 = time.time()
            self.state = "recognizing"
            self.result = self._recognize(audio)
            t2 = time.time()
            print(f"[음성] 녹음 {len(audio) / 2 / SAMPLE_RATE:.1f}초 (대기 포함 {t1 - t0:.1f}초), "
                  f"인식({self.engine}) {t2 - t1:.1f}초 → {self.result!r}")
            self.state = "done"
        except Exception as e:
            self.state = "error"
            self.error = str(e)[:60]
        finally:
            self._listening = False
            self.level = 0.0

    def set_device(self, device):
        self.cool()
        self.device = device
        self._config = None
        self._noise = None

    def _input_config(self):
        if not getattr(self, "_config", None):
            self._config = probe_input(self.device)
            print(f"[음성] 마이크 설정: {self._config[0]}Hz, {self._config[1]}채널")
        return self._config

    def _record(self, max_seconds, silence_seconds, wait_seconds):
        self.warm()
        rate = self._rate
        block_sec = 0.05
        while not self._q.empty():  # 이전에 쌓인 소리 버리기
            self._q.get_nowait()
        self._listening = True

        noise = self._noise or 0.0
        start_thr = max(self.threshold, noise * 2.0)
        vosk_rec = None
        if self._vosk is not None:  # Vosk 는 말하는 동안 바로바로 인식 → 끝나자마자 결과
            from vosk import KaldiRecognizer
            vosk_rec = KaldiRecognizer(self._vosk, SAMPLE_RATE)

        def feed(block):
            if vosk_rec is not None:
                pcm = resample(block, rate, SAMPLE_RATE)
                vosk_rec.AcceptWaveform((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())

        chunks, pre = [], []
        started = False
        silent_for = 0.0
        peak = 0.0
        t0 = time.time()
        while True:
            data = self._q.get(timeout=2)
            rms = float(np.sqrt(np.mean(data ** 2)))
            self.level = level_from_rms(rms)
            if not started:
                pre = (pre + [data])[-6:]  # 말 시작 직전 300ms 보존
                if rms > start_thr:
                    started = True
                    self.state = "recording"
                    chunks = list(pre)
                    for b in chunks:
                        feed(b)
                    t0 = time.time()
                elif time.time() - t0 > wait_seconds:
                    return None
                continue
            chunks.append(data)
            feed(data)
            peak = max(peak, rms)
            # 말 끝 판단: 고정 기준 + 주변 소음 + 말소리 크기 기준 중 가장 큰 값 아래로 떨어지면 "조용"
            end_thr = max(self.threshold * 0.8, noise * 1.8, peak * 0.1)
            silent_for = 0.0 if rms > end_thr else silent_for + block_sec
            if silent_for >= silence_seconds or time.time() - t0 > max_seconds:
                break
        self._listening = False
        self.level = 0.0
        # 끝의 조용한 부분은 0.15초만 남기고 잘라서 보내는 양 줄이기
        trim = max(0, int((silent_for - 0.15) / block_sec))
        if trim and len(chunks) > trim:
            chunks = chunks[:-trim]
        if vosk_rec is not None:
            self._vosk_result = json.loads(vosk_rec.FinalResult()).get("text", "").replace(" ", "")
        pcm = resample(np.concatenate(chunks), rate, SAMPLE_RATE)
        return (np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes()

    def _recognize(self, pcm16):
        if self._vosk is not None:
            return self._vosk_result
        data = sr.AudioData(pcm16, SAMPLE_RATE, 2)
        try:
            return self._recognizer.recognize_google(data, language="zh-CN")
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            raise RuntimeError(f"인식 서버 오류(인터넷 확인): {e}")
