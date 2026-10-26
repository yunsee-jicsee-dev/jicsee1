#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ORICO NVMe 바이너리 에디터
==========================

ORICO 등 USB-NVMe 외장 케이스의 브릿지 칩(Realtek RTL9210, JMicron JMS583,
ASMedia ASM2362 …) 펌웨어 때문에 헤매는 사람들을 위한 도구.

* 펌웨어(.bin) 파일 / 디스크 이미지 / 실제 디스크(NVMe·USB 디스크)를 여는 헥스 에디터
* 펌웨어 분석: 칩 추정, USB 디스크립터(VID/PID/bcdDevice), 문자열, 체크섬 방식 후보
* VID/PID·USB 문자열 수정, 단순 체크섬(sum8/sum16/CRC32) 재계산
* 연결된 USB 브릿지·NVMe 장치 검색 (펌웨어 버전 확인)
* 두 펌웨어 파일 비교(diff), 디스크 영역 이미지 덤프

표준 라이브러리만 사용하므로 Windows / Linux, x64 / ARM64 어디서나 그대로 동작한다.
인자 없이 실행하면 GUI, 인자를 주면 CLI.

이 프로그램은 브릿지 칩에 펌웨어를 '플래시'하지 않는다. 플래시는 각 칩 제조사의
MP 툴로 해야 하며, 이 도구는 플래시 전에 파일을 확인·비교·수정하는 용도다.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import asdict, dataclass

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, simpledialog, ttk
    from tkinter.scrolledtext import ScrolledText
except ImportError:  # 헤드리스 리눅스 등: CLI만 사용 가능
    tk = None

__version__ = "1.0.0"
APP_NAME = "ORICO NVMe 바이너리 에디터"

IS_WINDOWS = os.name == "nt"
DEVICE_ALIGN = 4096          # 디스크 읽기/쓰기 정렬 단위 (512B·4Kn 섹터 모두 호환)
CHUNK = 1 << 20              # 검색·복사 시 한 번에 읽는 크기
ANALYZE_LIMIT = 64 << 20     # 펌웨어 분석에 쓰는 최대 크기

# ---------------------------------------------------------------------------
# 브릿지 칩 정보
# ---------------------------------------------------------------------------

BRIDGE_VENDORS = {0x0BDA: "Realtek", 0x152D: "JMicron", 0x174C: "ASMedia"}

KNOWN_BRIDGES = {
    (0x0BDA, 0x9210): "Realtek RTL9210 / RTL9210B (USB ↔ NVMe/SATA)",
    (0x152D, 0x0583): "JMicron JMS583 (USB ↔ NVMe)",
    (0x174C, 0x2362): "ASMedia ASM2362 (USB ↔ NVMe)",
}

CHIP_SIGNATURES = [
    ("Realtek RTL9210 계열", [b"RTL9210"]),
    ("Realtek RTL9220 계열", [b"RTL9220"]),
    ("JMicron JMS583 계열", [b"JMS583"]),
    ("JMicron JMS581 계열", [b"JMS581"]),
    ("ASMedia ASM2362 계열", [b"ASM2362"]),
    ("ASMedia ASM2364 계열", [b"ASM2364"]),
    ("ASMedia ASM2464 계열", [b"ASM2464"]),
]
VENDOR_KEYWORDS = [b"Realtek", b"JMicron", b"ASMedia", b"ORICO"]


def bridge_label(vid: int, pid: int) -> str:
    if (vid, pid) in KNOWN_BRIDGES:
        return KNOWN_BRIDGES[(vid, pid)]
    if vid in BRIDGE_VENDORS:
        return f"{BRIDGE_VENDORS[vid]} 칩 (모델 미확인)"
    return ""


# ---------------------------------------------------------------------------
# 공용 유틸
# ---------------------------------------------------------------------------

def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{n} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{n} B"


def parse_int(text: str) -> int:
    text = text.strip().replace("_", "")
    if re.fullmatch(r"[0-9A-Fa-f]+[hH]", text):
        return int(text[:-1], 16)
    return int(text, 0)


def parse_hex_bytes(text: str) -> bytes:
    cleaned = re.sub(r"[\s:,\-]|0x", "", text.strip(), flags=re.IGNORECASE)
    if not cleaned or len(cleaned) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", cleaned):
        raise ValueError(f"올바른 16진수 바이트열이 아닙니다: {text!r}")
    return bytes.fromhex(cleaned)


def hexdump_lines(data: bytes, base: int = 0):
    width = 8 if base + len(data) <= 0xFFFFFFFF else 12
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexpart = " ".join(f"{b:02X}" for b in chunk).ljust(47)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        yield f"{base + i:0{width}X}  {hexpart}  |{asc}|"


def fmt_bcd(v: int) -> str:
    return f"{v >> 8:X}.{v & 0xFF:02X} (0x{v:04X})"


def normalize_device_path(path: str) -> str:
    if IS_WINDOWS and re.fullmatch(r"(?i)physicaldrive\d+", path):
        return "\\\\.\\" + path
    return path


# ---------------------------------------------------------------------------
# 펌웨어 분석
# ---------------------------------------------------------------------------

@dataclass
class UsbDeviceDescriptor:
    offset: int
    bcd_usb: int
    vid: int
    pid: int
    bcd_device: int
    i_manufacturer: int
    i_product: int
    i_serial: int
    num_configs: int

    def describe(self) -> str:
        chip = bridge_label(self.vid, self.pid)
        return (f"VID:PID {self.vid:04X}:{self.pid:04X}  bcdDevice {fmt_bcd(self.bcd_device)}  "
                f"USB {self.bcd_usb >> 8:X}.{(self.bcd_usb >> 4) & 0xF:X}"
                + (f"  → {chip}" if chip else ""))


@dataclass
class UsbStringDescriptor:
    offset: int
    length: int
    text: str

    @property
    def max_chars(self) -> int:
        return (self.length - 2) // 2


_VALID_BCD_USB = {0x0100, 0x0110, 0x0200, 0x0201, 0x0210, 0x0300, 0x0310, 0x0320}
_VALID_MPS0 = {8, 9, 16, 32, 64}


def find_device_descriptors(data: bytes) -> list[UsbDeviceDescriptor]:
    """USB 장치 디스크립터(12 01 …)처럼 보이는 18바이트 구조를 찾는다."""
    found = []
    start = 0
    while True:
        i = data.find(b"\x12\x01", start)
        if i < 0 or i + 18 > len(data):
            break
        start = i + 1
        bcd_usb, = struct.unpack_from("<H", data, i + 2)
        if bcd_usb not in _VALID_BCD_USB or data[i + 7] not in _VALID_MPS0:
            continue
        if not 1 <= data[i + 17] <= 4 or max(data[i + 14:i + 17]) > 15:
            continue
        vid, pid, bcd_dev = struct.unpack_from("<HHH", data, i + 8)
        if vid in (0, 0xFFFF):
            continue
        found.append(UsbDeviceDescriptor(i, bcd_usb, vid, pid, bcd_dev,
                                         data[i + 14], data[i + 15], data[i + 16], data[i + 17]))
    return found


_STRDESC_RE = re.compile(rb"([\x06-\xfe])\x03((?:[\x20-\x7e]\x00){2,})")


def find_string_descriptors(data: bytes, min_chars: int = 3) -> list[UsbStringDescriptor]:
    """USB 문자열 디스크립터(bLength 03 + UTF-16LE)를 찾는다."""
    found = []
    for m in _STRDESC_RE.finditer(data):
        blen = m.group(1)[0]
        if blen % 2:
            continue
        n = (blen - 2) // 2
        if n < min_chars or n > len(m.group(2)) // 2:
            continue
        found.append(UsbStringDescriptor(m.start(), blen, m.group(2)[:n * 2].decode("utf-16-le")))
    return found


_INTERESTING_RE = re.compile(
    r"(?i)(v?\d+\.\d+|fw|firmware|ver|rtl|jms|asm|realtek|jmicron|asmedia|orico|nvme|usb|uas|bridge)")


def find_interesting_strings(data: bytes, min_len: int = 5, limit: int = 60) -> list[tuple[int, str]]:
    out = []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data):
        s = m.group().decode("ascii")
        if _INTERESTING_RE.search(s):
            out.append((m.start(), s[:120]))
            if len(out) >= limit:
                break
    return out


def guess_chip(data: bytes, descriptors: list[UsbDeviceDescriptor]) -> list[str]:
    hints = []
    for label, sigs in CHIP_SIGNATURES:
        count = 0
        for sig in sigs:
            for enc in (sig, sig.decode().encode("utf-16-le")):
                count += len(re.findall(re.escape(enc), data, flags=re.IGNORECASE))
        if count:
            hints.append(f"{label} — 칩 이름 문자열 {count}회 발견")
    for d in descriptors:
        label = bridge_label(d.vid, d.pid)
        if label:
            hints.append(f"{label} — USB 디스크립터 VID:PID {d.vid:04X}:{d.pid:04X} (오프셋 0x{d.offset:X})")
    for kw in VENDOR_KEYWORDS:
        count = sum(len(re.findall(re.escape(enc), data, flags=re.IGNORECASE))
                    for enc in (kw, kw.decode().encode("utf-16-le")))
        if count:
            hints.append(f"'{kw.decode()}' 문자열 {count}회")
    return hints


# --- 체크섬 -----------------------------------------------------------------

CHECKSUM_SCHEMES = {
    "crc32le-tail": "마지막 4바이트(LE) = 앞부분 전체의 CRC32",
    "crc32be-tail": "마지막 4바이트(BE) = 앞부분 전체의 CRC32",
    "sum16-tail": "마지막 2바이트(LE) = 앞부분 바이트 합의 하위 16비트",
    "sum8-tail": "마지막 1바이트 = 앞부분 바이트 합의 하위 8비트",
    "sum8-zero": "파일 전체 바이트 합(8비트)이 0 (마지막 바이트로 보정)",
}


def checksum_fix(data: bytes, scheme: str) -> tuple[int, bytes]:
    """주어진 방식에 맞도록 끝부분에 써야 할 (오프셋, 바이트)를 계산한다."""
    n = len(data)
    if scheme == "crc32le-tail":
        return n - 4, struct.pack("<I", zlib.crc32(data[:-4]) & 0xFFFFFFFF)
    if scheme == "crc32be-tail":
        return n - 4, struct.pack(">I", zlib.crc32(data[:-4]) & 0xFFFFFFFF)
    if scheme == "sum16-tail":
        return n - 2, struct.pack("<H", sum(data[:-2]) & 0xFFFF)
    if scheme == "sum8-tail":
        return n - 1, bytes([sum(data[:-1]) & 0xFF])
    if scheme == "sum8-zero":
        return n - 1, bytes([(-sum(data[:-1])) & 0xFF])
    raise ValueError(f"알 수 없는 체크섬 방식: {scheme}")


def checksum_matches(data: bytes, scheme: str) -> bool:
    if len(data) < 16:
        return False
    off, expected = checksum_fix(data, scheme)
    return data[off:off + len(expected)] == expected


def detect_checksum_schemes(data: bytes) -> list[str]:
    """파일 전체를 덮는 단순 체크섬 방식 중 현재 데이터와 일치하는 것 (신뢰도 높은 순)."""
    if len(data) < 16 or data.count(data[:1]) == len(data):
        return []  # 전부 같은 바이트면 판단 불가
    return [s for s in CHECKSUM_SCHEMES if checksum_matches(data, s)]


# --- 수정 헬퍼 --------------------------------------------------------------

def usb_id_patch(desc: UsbDeviceDescriptor, vid: int, pid: int, bcd: int | None = None) -> tuple[int, bytes]:
    if bcd is None:
        bcd = desc.bcd_device
    for name, v in (("VID", vid), ("PID", pid), ("bcdDevice", bcd)):
        if not 0 <= v <= 0xFFFF:
            raise ValueError(f"{name} 값은 0x0000~0xFFFF 이어야 합니다")
    return desc.offset + 8, struct.pack("<HHH", vid, pid, bcd)


def usb_string_patch(desc: UsbStringDescriptor, text: str) -> tuple[int, bytes]:
    """문자열 디스크립터를 같은 자리에서 바꾼다. 원래 길이보다 길어질 수는 없다."""
    if not all(32 <= ord(c) < 127 for c in text):
        raise ValueError("ASCII 출력 가능 문자만 사용할 수 있습니다")
    if not 1 <= len(text) <= desc.max_chars:
        raise ValueError(f"길이는 1~{desc.max_chars}자여야 합니다 (원래 문자열 길이 이하)")
    new = bytes([2 + 2 * len(text), 3]) + text.encode("utf-16-le")
    return desc.offset, new.ljust(desc.length, b"\x00")


# --- 분석 리포트 ------------------------------------------------------------

def analyze_firmware(data: bytes, original: bytes | None = None) -> dict:
    """data: 현재(수정 반영) 내용, original: 수정 전 내용(체크섬 방식 판별용)."""
    descs = find_device_descriptors(data)
    original = data if original is None else original
    schemes = detect_checksum_schemes(original)
    return {
        "size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08X}",
        "sum8": f"{sum(data) & 0xFF:02X}",
        "sum16": f"{sum(data) & 0xFFFF:04X}",
        "chip_hints": guess_chip(data, descs),
        "device_descriptors": [asdict(d) for d in descs],
        "string_descriptors": [asdict(s) for s in find_string_descriptors(data)][:80],
        "checksum_schemes": schemes,
        "checksum_valid_now": {s: checksum_matches(data, s) for s in schemes},
        "strings": find_interesting_strings(data),
    }


def format_report(rep: dict, name: str = "") -> str:
    L = []
    L.append(f"=== 펌웨어 분석 {name} ===")
    L.append(f"크기     : {human_size(rep['size'])} ({rep['size']} 바이트)")
    L.append(f"MD5      : {rep['md5']}")
    L.append(f"SHA-256  : {rep['sha256']}")
    L.append(f"CRC32    : {rep['crc32']}   바이트합 8비트 {rep['sum8']} / 16비트 {rep['sum16']}")
    L.append("")
    L.append("[칩 추정]")
    if rep["chip_hints"]:
        L += [f"  - {h}" for h in rep["chip_hints"]]
    else:
        L.append("  - 단서 없음 (암호화/압축된 펌웨어이거나 지원하지 않는 칩일 수 있음)")
    L.append("")
    L.append("[USB 장치 디스크립터]  ← OS에 보이는 VID/PID/버전")
    if rep["device_descriptors"]:
        for i, d in enumerate(rep["device_descriptors"]):
            L.append(f"  #{i} @0x{d['offset']:X}: " + UsbDeviceDescriptor(**d).describe())
    else:
        L.append("  - 찾지 못함")
    L.append("")
    L.append("[USB 문자열 디스크립터]")
    if rep["string_descriptors"]:
        for s in rep["string_descriptors"]:
            L.append(f"  @0x{s['offset']:X} (최대 {(s['length'] - 2) // 2}자): {s['text']!r}")
    else:
        L.append("  - 찾지 못함")
    L.append("")
    L.append("[체크섬 방식 후보] (파일 전체를 덮는 단순 방식만 검사)")
    if rep["checksum_schemes"]:
        for s in rep["checksum_schemes"]:
            ok = "일치" if rep["checksum_valid_now"].get(s) else "불일치 → 재계산 필요"
            L.append(f"  - {s}: {CHECKSUM_SCHEMES[s]}  [현재 {ok}]")
        if rep["checksum_schemes"][0].startswith("sum8"):
            L.append("    (8비트 합은 우연히 맞을 확률이 1/256 — 확신할 수 없음)")
    else:
        L.append("  - 해당 없음. 헤더 내부 체크섬/서명을 쓰는 펌웨어일 수 있으니")
        L.append("    수정한 파일은 반드시 제조사 MP 툴이 인식하는지 확인할 것.")
    L.append("")
    L.append("[관심 문자열]")
    if rep["strings"]:
        L += [f"  @0x{off:X}: {s}" for off, s in rep["strings"]]
    else:
        L.append("  - 없음")
    L.append("")
    L.append("※ 펌웨어를 수정하기 전에 원본을 반드시 보관하세요. 칩과 맞지 않는 펌웨어를")
    L.append("  플래시하면 케이스가 인식되지 않을 수 있습니다(대부분 MP 툴로 복구 가능).")
    return "\n".join(L)


# --- 비교 -------------------------------------------------------------------

def diff_ranges(a: bytes, b: bytes, merge_gap: int = 8) -> list[tuple[int, int]]:
    """공통 길이 안에서 서로 다른 구간 [(start, end), …]."""
    n = min(len(a), len(b))
    ranges: list[list[int]] = []
    block = 4096
    for base in range(0, n, block):
        end = min(base + block, n)
        if a[base:end] == b[base:end]:
            continue
        for i in range(base, end):
            if a[i] != b[i]:
                if ranges and i - ranges[-1][1] <= merge_gap:
                    ranges[-1][1] = i + 1
                else:
                    ranges.append([i, i + 1])
    return [(s, e) for s, e in ranges]


def format_diff(a: bytes, b: bytes, name_a: str, name_b: str, max_ranges: int = 50) -> str:
    L = [f"=== 비교: A={name_a}  B={name_b} ===",
         f"크기: A {len(a)} / B {len(b)} 바이트" + ("" if len(a) == len(b) else "  (크기 다름!)")]
    ranges = diff_ranges(a, b)
    total = sum(e - s for s, e in ranges)
    L.append(f"다른 구간 {len(ranges)}개, 다른 바이트 약 {total}개")
    for s, e in ranges[:max_ranges]:
        L.append("")
        L.append(f"-- 0x{s:X} ~ 0x{e - 1:X} ({e - s} 바이트)")
        show_end = min(e, s + 64)
        for tag, src in (("A", a), ("B", b)):
            for line in hexdump_lines(src[s:show_end], s):
                L.append(f"  {tag} {line}")
    if len(ranges) > max_ranges:
        L.append(f"\n… 이하 {len(ranges) - max_ranges}개 구간 생략")
    da, db = find_device_descriptors(a), find_device_descriptors(b)
    if da or db:
        L.append("")
        L.append("[USB ID 비교]")
        L.append("  A: " + ("; ".join(d.describe() for d in da) or "없음"))
        L.append("  B: " + ("; ".join(d.describe() for d in db) or "없음"))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 파일/디스크 접근 (수정 사항은 저장 전까지 메모리의 patches 에만 보관)
# ---------------------------------------------------------------------------

def is_device_path(path: str) -> bool:
    if IS_WINDOWS:
        return path.startswith("\\\\.\\") or path.startswith("//./")
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def _win_disk_length(path: str) -> int:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(path, 0x80000000, 0x3, None, 3, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = ctypes.c_longlong(0)
        returned = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(handle, 0x0007405C,  # IOCTL_DISK_GET_LENGTH_INFO
                                 None, 0, ctypes.byref(length), 8, ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return length.value
    finally:
        k32.CloseHandle(handle)


def device_size(path: str) -> int:
    if IS_WINDOWS:
        return _win_disk_length(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def make_backup(path: str) -> str:
    bak = path + ".bak"
    i = 1
    while os.path.exists(bak):
        bak = f"{path}.bak{i}"
        i += 1
    shutil.copy2(path, bak)
    return bak


def access_hint(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return (" — 관리자 권한으로 실행하세요" if IS_WINDOWS else " — sudo 로 실행하세요")
    return ""


class ByteSource:
    def __init__(self, path: str, allow_device_write: bool = False):
        self.path = normalize_device_path(path)
        self.is_device = is_device_path(self.path)
        self.align = DEVICE_ALIGN if self.is_device else 1
        self.size = device_size(self.path) if self.is_device else os.path.getsize(self.path)
        self.writable = (not self.is_device) or allow_device_write
        self.patches: dict[int, int] = {}
        self._sorted: list[int] | None = None
        self._fh = open(self.path, "rb", buffering=0)

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    # --- 읽기 ---
    def _read_fully(self, n: int) -> bytes:
        parts, got = [], 0
        while got < n:
            b = self._fh.read(n - got)
            if not b:
                break
            parts.append(b)
            got += len(b)
        return b"".join(parts)

    def read_raw(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        if self.align == 1:
            self._fh.seek(offset)
            return self._read_fully(length)
        start = offset - offset % self.align
        end = offset + length
        end = min(end + (-end) % self.align, self.size)
        self._fh.seek(start)
        buf = self._read_fully(end - start)
        return buf[offset - start:offset - start + length]

    def read(self, offset: int, length: int) -> bytearray:
        buf = bytearray(self.read_raw(offset, length))
        for k in self.patched_in_range(offset, offset + len(buf)):
            buf[k - offset] = self.patches[k]
        return buf

    # --- 수정 ---
    def _keys(self) -> list[int]:
        if self._sorted is None:
            self._sorted = sorted(self.patches)
        return self._sorted

    def patched_in_range(self, start: int, end: int) -> list[int]:
        keys = self._keys()
        return keys[bisect.bisect_left(keys, start):bisect.bisect_left(keys, end)]

    def get_patch(self, offset: int) -> int | None:
        return self.patches.get(offset)

    def restore_patch(self, offset: int, value: int | None):
        if value is None:
            self.patches.pop(offset, None)
        else:
            self.patches[offset] = value
        self._sorted = None

    def write_bytes(self, offset: int, data: bytes):
        if offset < 0 or offset + len(data) > self.size:
            raise ValueError(f"범위를 벗어났습니다: 0x{offset:X}+{len(data)} (크기 0x{self.size:X})")
        raw = self.read_raw(offset, len(data))
        for i, v in enumerate(data):
            if v == raw[i]:
                self.patches.pop(offset + i, None)
            else:
                self.patches[offset + i] = v
        self._sorted = None

    @property
    def modified(self) -> bool:
        return bool(self.patches)

    # --- 저장 ---
    def _runs(self, keys: list[int]) -> list[tuple[int, int]]:
        runs: list[list[int]] = []
        for k in keys:
            if runs and k == runs[-1][1]:
                runs[-1][1] = k + 1
            else:
                runs.append([k, k + 1])
        return [(s, e) for s, e in runs]

    def save(self, backup: bool = True) -> str | None:
        """수정 사항을 원본에 기록. 백업 경로(파일 또는 폴더)를 반환."""
        if not self.patches:
            return None
        if not self.writable:
            raise PermissionError("읽기 전용으로 열려 있습니다")
        backup_path = None
        if self.is_device:
            blocks = sorted({k - k % self.align for k in self.patches})
            runs = [(s, min(e, self.size)) for s, e in self._merge_block_runs(blocks)]
            if backup:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                backup_path = os.path.join(os.path.expanduser("~"), "orico-nvme-backups", stamp)
                os.makedirs(backup_path, exist_ok=True)
            with open(self.path, "r+b", buffering=0) as w:
                for s, e in runs:
                    orig = self.read_raw(s, e - s)
                    if backup_path:
                        with open(os.path.join(backup_path, f"0x{s:012X}.bin"), "wb") as f:
                            f.write(orig)
                    new = self.read(s, e - s)
                    w.seek(s)
                    w.write(bytes(new))
                try:
                    os.fsync(w.fileno())
                except OSError:
                    pass
            if backup_path:
                with open(os.path.join(backup_path, "README.txt"), "w", encoding="utf-8") as f:
                    f.write(f"장치: {self.path}\n각 파일 이름의 16진수가 원래 데이터의 오프셋입니다.\n"
                            "복구 예: nvme_hexedit.py patch <장치> --in-place --allow-device-write "
                            "--write-file 0x오프셋=파일.bin\n")
        else:
            if backup:
                backup_path = make_backup(self.path)
            self._fh.close()
            try:
                with open(self.path, "r+b") as w:
                    for s, e in self._runs(self._keys()):
                        w.seek(s)
                        w.write(bytes(self.patches[k] for k in range(s, e)))
                    w.flush()
                    os.fsync(w.fileno())
            finally:
                self._fh = open(self.path, "rb", buffering=0)
        self.patches.clear()
        self._sorted = None
        return backup_path

    def _merge_block_runs(self, blocks: list[int]) -> list[tuple[int, int]]:
        """정렬된 블록 시작 주소들 → 연속 구간 [(시작, 끝), …]."""
        runs: list[list[int]] = []
        for b in blocks:
            if runs and b == runs[-1][1]:
                runs[-1][1] = b + self.align
            else:
                runs.append([b, b + self.align])
        return [(s, e) for s, e in runs]

    def save_as(self, out_path: str, offset: int = 0, length: int | None = None, progress=None) -> int:
        """수정 사항을 반영한 내용을 새 파일로 저장(디스크면 이미지 덤프)."""
        if length is None:
            length = self.size - offset
        length = max(0, min(length, self.size - offset))
        done = 0
        with open(out_path, "wb") as out:
            while done < length:
                chunk = self.read(offset + done, min(CHUNK, length - done))
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, length)
        return done

    def find(self, pattern: bytes, start: int = 0, progress=None) -> int:
        if not pattern:
            return -1
        overlap = len(pattern) - 1
        pos = max(0, start)
        while pos < self.size:
            chunk = self.read(pos, CHUNK + overlap)
            i = chunk.find(pattern)
            if i >= 0:
                return pos + i
            pos += CHUNK
            if progress and progress(pos, self.size) is False:
                return -1
        return -1


# ---------------------------------------------------------------------------
# 장치 검색
# ---------------------------------------------------------------------------

def _sysread(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def scan_linux() -> list[dict]:
    items = []
    blocks = []
    if os.path.isdir("/sys/block"):
        for name in sorted(os.listdir("/sys/block")):
            blocks.append((name, os.path.realpath(os.path.join("/sys/block", name))))
    base = "/sys/bus/usb/devices"
    if os.path.isdir(base):
        for dev in sorted(os.listdir(base)):
            p = os.path.join(base, dev)
            if ":" in dev or not os.path.exists(os.path.join(p, "idVendor")):
                continue
            try:
                vid = int(_sysread(os.path.join(p, "idVendor")), 16)
                pid = int(_sysread(os.path.join(p, "idProduct")), 16)
                bcd = int(_sysread(os.path.join(p, "bcdDevice")) or "0", 16)
            except ValueError:
                continue
            classes, drivers = set(), set()
            for sub in os.listdir(p):
                if sub.startswith(dev + ":"):
                    classes.add(_sysread(os.path.join(p, sub, "bInterfaceClass")))
                    drv = os.path.join(p, sub, "driver")
                    if os.path.exists(drv):
                        drivers.add(os.path.basename(os.path.realpath(drv)))
            if "08" not in classes and (vid, pid) not in KNOWN_BRIDGES:
                continue
            real = os.path.realpath(p)
            disks = ["/dev/" + n for n, r in blocks if r.startswith(real + "/")]
            items.append({
                "kind": "usb",
                "path": ", ".join(disks),
                "name": " ".join(x for x in (_sysread(os.path.join(p, "manufacturer")),
                                             _sysread(os.path.join(p, "product"))) if x),
                "vid": f"{vid:04X}", "pid": f"{pid:04X}",
                "firmware": fmt_bcd(bcd),
                "bridge": bridge_label(vid, pid),
                "driver": ",".join(sorted(drivers)),
                "speed": _sysread(os.path.join(p, "speed")) + " Mbps",
            })
    for ctrl in sorted(os.listdir("/sys/class/nvme")) if os.path.isdir("/sys/class/nvme") else []:
        c = os.path.join("/sys/class/nvme", ctrl)
        namespaces = [n for n, _ in blocks if re.fullmatch(rf"{ctrl}n\d+", n)]
        for ns in namespaces or [""]:
            items.append({
                "kind": "nvme",
                "path": "/dev/" + ns if ns else "/dev/" + ctrl,
                "name": _sysread(os.path.join(c, "model")),
                "firmware": _sysread(os.path.join(c, "firmware_rev")),
                "size": human_size(int(_sysread(f"/sys/block/{ns}/size") or 0) * 512) if ns else "",
                "transport": _sysread(os.path.join(c, "transport")),
            })
    for name, real in blocks:
        if not re.fullmatch(r"sd[a-z]+", name):
            continue
        d = f"/sys/block/{name}/device"
        items.append({
            "kind": "disk",
            "path": "/dev/" + name,
            "name": f"{_sysread(d + '/vendor')} {_sysread(d + '/model')}".strip(),
            "firmware": _sysread(d + "/rev"),
            "size": human_size(int(_sysread(f"/sys/block/{name}/size") or 0) * 512),
            "transport": "usb" if "/usb" in real else "",
        })
    return items


_WIN_BUS = {7: "USB", 8: "RAID", 10: "SAS", 11: "SATA", 17: "NVMe"}


def _powershell_json(cmd: str):
    flags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                         capture_output=True, text=True, timeout=60, creationflags=flags,
                         encoding="utf-8", errors="replace")
    out = res.stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def scan_windows() -> list[dict]:
    items = []
    prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    usb = _powershell_json(prefix + "Get-CimInstance Win32_PnPEntity | "
                           "Where-Object { $_.DeviceID -like 'USB\\VID_*' } | "
                           "Select-Object DeviceID,Name,Service,HardwareID | ConvertTo-Json -Compress")
    for d in usb:
        m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", d.get("DeviceID") or "", re.I)
        if not m:
            continue
        vid, pid = int(m.group(1), 16), int(m.group(2), 16)
        service = d.get("Service") or ""
        if service.lower() not in ("usbstor", "uaspstor") and (vid, pid) not in KNOWN_BRIDGES:
            continue
        rev = ""
        for hid in d.get("HardwareID") or []:
            r = re.search(r"REV_([0-9A-F]{4})", hid, re.I)
            if r:
                rev = fmt_bcd(int(r.group(1), 16))
                break
        items.append({"kind": "usb", "path": d.get("DeviceID", ""), "name": d.get("Name") or "",
                      "vid": f"{vid:04X}", "pid": f"{pid:04X}", "firmware": rev,
                      "bridge": bridge_label(vid, pid), "driver": service})
    disks = _powershell_json(prefix + "Get-PhysicalDisk | Select-Object DeviceId,FriendlyName,BusType,"
                             "FirmwareVersion,Size | ConvertTo-Json -Compress")
    for d in disks:
        bus = d.get("BusType")
        if isinstance(bus, int):
            bus = _WIN_BUS.get(bus, str(bus))
        items.append({"kind": "nvme" if str(bus).lower() == "nvme" else "disk",
                      "path": f"\\\\.\\PhysicalDrive{d.get('DeviceId')}",
                      "name": d.get("FriendlyName") or "", "firmware": d.get("FirmwareVersion") or "",
                      "size": human_size(int(d.get("Size") or 0)), "transport": str(bus or "")})
    return items


def scan_devices() -> list[dict]:
    if IS_WINDOWS:
        return scan_windows()
    if sys.platform.startswith("linux"):
        return scan_linux()
    raise OSError("장치 검색은 Windows / Linux 만 지원합니다")


def format_scan(items: list[dict]) -> str:
    if not items:
        return "USB 저장장치 / NVMe 장치를 찾지 못했습니다."
    kinds = {"usb": "USB 브릿지", "nvme": "NVMe", "disk": "디스크"}
    L = []
    for it in items:
        head = f"[{kinds.get(it['kind'], it['kind'])}] "
        if it["kind"] == "usb":
            L.append(head + f"{it['vid']}:{it['pid']}  {it['name']}")
            if it.get("bridge"):
                L.append(f"    칩 추정   : {it['bridge']}")
            L.append(f"    REV(bcdDevice): {it['firmware']}  ← 대부분 브릿지 펌웨어 버전")
            L.append(f"    드라이버  : {it.get('driver', '')}"
                     + (f"   속도: {it['speed']}" if it.get("speed") else ""))
            if it.get("path"):
                L.append(f"    디스크    : {it['path']}")
        else:
            L.append(head + f"{it['path']}  {it['name']}")
            L.append(f"    펌웨어: {it.get('firmware', '')}   크기: {it.get('size', '')}"
                     + (f"   연결: {it['transport']}" if it.get("transport") else ""))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_limited(src: ByteSource, original: bool = False) -> bytes:
    n = min(src.size, ANALYZE_LIMIT)
    return src.read_raw(0, n) if original else bytes(src.read(0, n))


def _confirm_device_write(path: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    print(f"\n!!! 경고: 장치 {path} 에 직접 기록합니다. 잘못되면 데이터가 사라집니다.")
    answer = input("계속하려면 장치 경로를 그대로 입력하세요: ").strip()
    return answer == path


def cmd_info(args) -> int:
    src = ByteSource(args.path)
    try:
        rep = analyze_firmware(_read_limited(src))
    finally:
        src.close()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        if src.size > ANALYZE_LIMIT:
            print(f"(앞부분 {human_size(ANALYZE_LIMIT)}만 분석했습니다)")
        print(format_report(rep, os.path.basename(args.path)))
    return 0


def cmd_dump(args) -> int:
    src = ByteSource(args.path)
    try:
        offset = parse_int(args.offset)
        data = src.read(offset, parse_int(args.length))
    finally:
        src.close()
    for line in hexdump_lines(bytes(data), offset):
        print(line)
    return 0


def cmd_scan(args) -> int:
    items = scan_devices()
    print(json.dumps(items, ensure_ascii=False, indent=2) if args.json else format_scan(items))
    return 0


def cmd_diff(args) -> int:
    with open(args.a, "rb") as fa, open(args.b, "rb") as fb:
        a, b = fa.read(), fb.read()
    print(format_diff(a, b, args.a, args.b, args.max))
    return 0 if a == b else 1


def cmd_image(args) -> int:
    src = ByteSource(args.src)
    try:
        offset = parse_int(args.offset)
        length = parse_int(args.length) if args.length else None

        def progress(done, total):
            print(f"\r{human_size(done)} / {human_size(total)}", end="", file=sys.stderr)
        n = src.save_as(args.out, offset, length, progress)
    finally:
        src.close()
    print(f"\n{args.out} 에 {n} 바이트 저장", file=sys.stderr)
    return 0


def cmd_checksum(args) -> int:
    with open(args.path, "rb") as f:
        data = f.read()
    print(f"CRC32 {zlib.crc32(data) & 0xFFFFFFFF:08X}  sum8 {sum(data) & 0xFF:02X}  "
          f"sum16 {sum(data) & 0xFFFF:04X}")
    print(f"MD5    {hashlib.md5(data).hexdigest()}")
    print(f"SHA256 {hashlib.sha256(data).hexdigest()}")
    schemes = detect_checksum_schemes(data)
    print("체크섬 방식 후보: " + (", ".join(schemes) if schemes else "없음"))
    return 0


def _split_assign(text: str, what: str) -> tuple[str, str]:
    if "=" not in text:
        raise ValueError(f"{what} 형식은 오프셋=값 입니다: {text!r}")
    left, right = text.split("=", 1)
    return left, right


def cmd_patch(args) -> int:
    src = ByteSource(args.target, allow_device_write=args.allow_device_write)
    try:
        if src.is_device and args.in_place and not args.allow_device_write:
            print("장치에 쓰려면 --allow-device-write 가 필요합니다", file=sys.stderr)
            return 2
        for item in args.set or []:
            off, val = _split_assign(item, "--set")
            src.write_bytes(parse_int(off), parse_hex_bytes(val))
        for item in args.write_file or []:
            off, fname = _split_assign(item, "--write-file")
            with open(fname, "rb") as f:
                src.write_bytes(parse_int(off), f.read())
        if args.usb_id:
            parts = [parse_int("0x" + p if not p.lower().startswith("0x") else p)
                     for p in args.usb_id.split(":")]
            if len(parts) not in (2, 3):
                raise ValueError("--usb-id 형식: VID:PID 또는 VID:PID:BCD (16진수)")
            descs = find_device_descriptors(_read_limited(src))
            if not descs:
                raise ValueError("USB 장치 디스크립터를 찾지 못했습니다")
            if args.desc_index is None and len(descs) > 1:
                raise ValueError("디스크립터가 여러 개입니다. --desc-index 로 지정하세요:\n" +
                                 "\n".join(f"  #{i} @0x{d.offset:X} {d.describe()}" for i, d in enumerate(descs)))
            d = descs[args.desc_index or 0]
            off, data = usb_id_patch(d, parts[0], parts[1], parts[2] if len(parts) == 3 else None)
            src.write_bytes(off, data)
            print(f"USB ID 변경 @0x{d.offset:X}: {d.vid:04X}:{d.pid:04X} → {parts[0]:04X}:{parts[1]:04X}")
        for item in args.usb_string or []:
            off_s, text = _split_assign(item, "--usb-string")
            off = parse_int(off_s)
            match = [s for s in find_string_descriptors(_read_limited(src), 1) if s.offset == off]
            if not match:
                raise ValueError(f"0x{off:X} 에서 USB 문자열 디스크립터를 찾지 못했습니다 (info 명령으로 확인)")
            o, data = usb_string_patch(match[0], text)
            src.write_bytes(o, data)
            print(f"문자열 변경 @0x{off:X}: {match[0].text!r} → {text!r}")

        schemes = detect_checksum_schemes(src.read_raw(0, src.size)) if src.size <= ANALYZE_LIMIT else []
        if args.fix_checksum != "none":
            scheme = schemes[0] if args.fix_checksum == "auto" and schemes else args.fix_checksum
            if scheme == "auto":
                print("원본에서 체크섬 방식을 찾지 못해 재계산하지 않았습니다", file=sys.stderr)
            else:
                off, data = checksum_fix(bytes(src.read(0, src.size)), scheme)
                src.write_bytes(off, data)
                print(f"체크섬 재계산({scheme}) @0x{off:X}: {data.hex(' ').upper()}")
        elif schemes and src.modified:
            print(f"참고: 원본이 체크섬 방식 {', '.join(schemes)} 와 일치합니다. "
                  "필요하면 --fix-checksum auto 를 사용하세요.", file=sys.stderr)

        if not src.modified:
            print("변경된 바이트가 없습니다.")
            return 0
        print(f"변경 바이트 수: {len(src.patches)}")
        if args.in_place:
            if src.is_device and not _confirm_device_write(src.path, args.yes):
                print("취소했습니다.")
                return 1
            bak = src.save(backup=not args.no_backup)
            print("원본에 저장했습니다." + (f" 백업: {bak}" if bak else ""))
        else:
            if args.output:
                out = args.output
            elif src.is_device:
                print("장치는 -o 로 출력 파일을 지정하거나 --in-place 를 사용하세요", file=sys.stderr)
                return 2
            else:
                stem, ext = os.path.splitext(src.path)
                out = f"{stem}.patched{ext or '.bin'}"
            src.save_as(out)
            print(f"저장: {out}")
    finally:
        src.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nvme_hexedit",
        description=f"{APP_NAME} {__version__} — 인자 없이 실행하면 GUI가 열립니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예)\n"
               "  nvme_hexedit info RTL9210B_fw.bin\n"
               "  nvme_hexedit scan\n"
               "  nvme_hexedit diff orico_stock.bin generic.bin\n"
               "  nvme_hexedit patch fw.bin --usb-id 0BDA:9210 --fix-checksum auto -o fw_new.bin\n"
               "  nvme_hexedit dump /dev/nvme0n1 --length 0x200          (Linux, sudo)\n"
               "  nvme_hexedit image PhysicalDrive2 head.bin --length 0x100000   (Windows, 관리자)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("gui", help="GUI 실행")
    g.add_argument("path", nargs="?")

    s = sub.add_parser("info", help="펌웨어 분석 (칩·VID/PID·문자열·체크섬)")
    s.add_argument("path")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("dump", help="헥스 덤프 출력 (파일/디스크)")
    s.add_argument("path")
    s.add_argument("--offset", default="0")
    s.add_argument("--length", default="0x100")
    s.set_defaults(func=cmd_dump)

    s = sub.add_parser("scan", help="연결된 USB 브릿지 / NVMe 장치와 펌웨어 버전 표시")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("diff", help="두 파일 비교")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--max", type=int, default=50, help="표시할 최대 구간 수")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("image", help="파일/디스크 일부를 이미지 파일로 저장 (백업용)")
    s.add_argument("src")
    s.add_argument("out")
    s.add_argument("--offset", default="0")
    s.add_argument("--length", help="생략하면 끝까지")
    s.set_defaults(func=cmd_image)

    s = sub.add_parser("checksum", help="체크섬/해시 계산")
    s.add_argument("path")
    s.set_defaults(func=cmd_checksum)

    s = sub.add_parser("patch", help="바이트/USB ID/문자열 수정")
    s.add_argument("target")
    s.add_argument("--set", action="append", metavar="OFF=HEX", help="예: 0x1A0=DEADBEEF")
    s.add_argument("--write-file", action="append", metavar="OFF=FILE", help="파일 내용을 오프셋에 덮어쓰기")
    s.add_argument("--usb-id", metavar="VID:PID[:BCD]", help="USB 장치 디스크립터 수정 (16진수)")
    s.add_argument("--desc-index", type=int, help="디스크립터가 여러 개일 때 번호 (info 참고)")
    s.add_argument("--usb-string", action="append", metavar="OFF=TEXT", help="USB 문자열 디스크립터 수정")
    s.add_argument("--fix-checksum", default="none", choices=["none", "auto", *CHECKSUM_SCHEMES])
    out = s.add_mutually_exclusive_group()
    out.add_argument("-o", "--output", help="결과 파일 (기본: 이름.patched.bin)")
    out.add_argument("--in-place", action="store_true", help="원본에 직접 저장 (백업 생성)")
    s.add_argument("--no-backup", action="store_true")
    s.add_argument("--allow-device-write", action="store_true", help="디스크 장치에 쓰기 허용 (위험)")
    s.add_argument("--yes", action="store_true", help="장치 쓰기 확인 질문 생략")
    s.set_defaults(func=cmd_patch)
    return p


def _fix_stdio():
    """한글을 못 쓰는 출력 인코딩(파이프로 넘긴 Windows 콘솔 등)이면 UTF-8로 바꾼다."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            "가".encode(stream.encoding or "ascii")
        except (LookupError, UnicodeEncodeError):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _fix_stdio()
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd in (None, "gui"):
        return run_gui(getattr(args, "path", None))
    try:
        return args.func(args)
    except (OSError, ValueError) as e:
        print(f"오류: {e}{access_hint(e)}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def _hide_own_console():
    """Windows에서 더블클릭으로 실행했을 때 뜨는 빈 콘솔 창을 숨긴다."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        hwnd = k32.GetConsoleWindow()
        if hwnd:
            buf = (ctypes.c_uint * 4)()
            limit = 2 if getattr(sys, "frozen", False) else 1  # PyInstaller는 부트로더 프로세스 포함
            if k32.GetConsoleProcessList(buf, 4) <= limit:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _pick_mono_font():
    families = set(tkfont.families())
    for fam in ("D2Coding", "Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Noto Sans Mono",
                "Liberation Mono", "Courier New"):
        if fam in families:
            return tkfont.Font(family=fam, size=11)
    f = tkfont.nametofont("TkFixedFont").copy()
    f.configure(size=11)
    return f


HELP_TEXT = f"""{APP_NAME} {__version__}

■ 기본 조작
  • 바이트 클릭 → 선택, 16진수 키 입력 → 값 변경 (Tab: HEX/ASCII 영역 전환)
  • 방향키 / PageUp / PageDown / Ctrl+Home / Ctrl+End 이동, 마우스 휠 스크롤
  • Ctrl+Z 되돌리기, Ctrl+Y 다시 실행, Ctrl+G 오프셋 이동, Ctrl+F 찾기, F3 다음 찾기
  • 빨간 글자 = 아직 저장하지 않은 변경

■ 펌웨어 작업 순서 (추천)
  1. 도구 → 장치 검색: 케이스 칩(VID:PID)과 현재 펌웨어 버전(REV) 확인
  2. 펌웨어 파일 열기 → 도구 → 펌웨어 분석: 칩 이름·VID/PID가 내 케이스와 맞는지 확인
  3. 필요하면 ORICO 원본과 다른 펌웨어를 도구 → 파일 비교
  4. 수정 후 '다른 이름으로 저장' (원본 보존), 체크섬 후보가 있으면 재계산
  5. 실제 플래시는 칩 제조사 MP 툴로 진행

■ 디스크 열기
  Windows: \\\\.\\PhysicalDrive1 (관리자 권한 필요)
  Linux  : /dev/nvme0n1, /dev/sda (sudo 필요)
  기본은 읽기 전용입니다. 쓰기는 경고를 확인한 경우에만 허용됩니다.
"""


class FindDialog:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("찾기")
        self.win.transient(app.root)
        self.win.resizable(False, False)
        frm = ttk.Frame(self.win, padding=10)
        frm.pack(fill="both")
        ttk.Label(frm, text="찾을 내용:").grid(row=0, column=0, sticky="w")
        self.entry = ttk.Entry(frm, width=40)
        self.entry.grid(row=0, column=1, columnspan=3, sticky="we", padx=5)
        self.mode = tk.StringVar(value=app.find_mode)
        for i, (val, label) in enumerate((("hex", "16진수"), ("ascii", "텍스트(ASCII)"),
                                          ("utf16", "텍스트(UTF-16LE)"))):
            ttk.Radiobutton(frm, text=label, value=val, variable=self.mode).grid(row=1, column=1 + i, sticky="w")
        ttk.Button(frm, text="찾기", command=self.go).grid(row=2, column=3, sticky="e", pady=(8, 0))
        if app.find_text:
            self.entry.insert(0, app.find_text)
        self.entry.focus_set()
        self.win.bind("<Return>", lambda e: self.go())
        self.win.bind("<Escape>", lambda e: self.win.destroy())

    def go(self):
        text, mode = self.entry.get(), self.mode.get()
        try:
            if mode == "hex":
                pat = parse_hex_bytes(text)
            elif mode == "ascii":
                pat = text.encode("utf-8")
            else:
                pat = text.encode("utf-16-le")
        except ValueError as e:
            messagebox.showerror("찾기", str(e), parent=self.win)
            return
        if not pat:
            return
        self.app.find_text, self.app.find_mode, self.app.find_pattern = text, mode, pat
        self.win.destroy()
        self.app.find_next(from_start=False)


class HexEditorApp:
    def __init__(self, root, initial_path: str | None = None):
        self.root = root
        self.src: ByteSource | None = None
        self.top_row = 0
        self.rows = 32
        self.cursor = 0
        self.area = "hex"
        self.nibble_pending = False
        self.undo_stack: list[list[tuple[int, int | None]]] = []
        self.redo_stack: list[list[tuple[int, int | None]]] = []
        self.find_text, self.find_mode, self.find_pattern = "", "hex", b""
        self.found: tuple[int, int] | None = None

        root.title(APP_NAME)
        root.geometry("1180x760")
        root.minsize(900, 420)
        self.font = _pick_mono_font()
        self.status = tk.StringVar()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        ttk.Label(root, textvariable=self.status, anchor="w", relief="sunken",
                  padding=(6, 2)).pack(side="bottom", fill="x")
        self._bind_keys()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.render()
        if initial_path:
            root.after(50, lambda: self.open_path(initial_path))

    # --- UI 구성 ---
    def _build_menu(self):
        m = tk.Menu(self.root)
        f = tk.Menu(m, tearoff=False)
        f.add_command(label="파일 열기…", accelerator="Ctrl+O", command=self.cmd_open_file)
        f.add_command(label="디스크/장치 열기…", command=self.cmd_open_device)
        f.add_separator()
        f.add_command(label="저장", accelerator="Ctrl+S", command=self.cmd_save)
        f.add_command(label="다른 이름으로 저장…", accelerator="Ctrl+Shift+S", command=self.cmd_save_as)
        f.add_command(label="범위 내보내기(이미지 덤프)…", command=self.cmd_export_range)
        f.add_separator()
        f.add_command(label="닫기", command=self.close_source)
        f.add_command(label="종료", command=self.quit)
        m.add_cascade(label="파일", menu=f)
        e = tk.Menu(m, tearoff=False)
        e.add_command(label="되돌리기", accelerator="Ctrl+Z", command=self.undo)
        e.add_command(label="다시 실행", accelerator="Ctrl+Y", command=self.redo)
        e.add_separator()
        e.add_command(label="오프셋으로 이동…", accelerator="Ctrl+G", command=self.cmd_goto)
        e.add_command(label="찾기…", accelerator="Ctrl+F", command=lambda: FindDialog(self))
        e.add_command(label="다음 찾기", accelerator="F3", command=lambda: self.find_next(False))
        e.add_separator()
        e.add_command(label="바이트열 붙여 쓰기(16진수)…", command=self.cmd_write_hex)
        e.add_command(label="범위 채우기…", command=self.cmd_fill)
        m.add_cascade(label="편집", menu=e)
        t = tk.Menu(m, tearoff=False)
        t.add_command(label="펌웨어 분석 / VID·PID·문자열 수정", command=self.cmd_analyze)
        t.add_command(label="체크섬 재계산", command=self.cmd_fix_checksum)
        t.add_command(label="장치 검색 (브릿지 칩·펌웨어 버전)", command=lambda: DeviceScanWindow(self))
        t.add_command(label="두 파일 비교…", command=self.cmd_diff)
        m.add_cascade(label="도구", menu=t)
        h = tk.Menu(m, tearoff=False)
        h.add_command(label="사용법", command=lambda: self.show_text("사용법", HELP_TEXT))
        h.add_command(label="정보", command=lambda: messagebox.showinfo(
            "정보", f"{APP_NAME} {__version__}\nPython {sys.version.split()[0]} / {sys.platform}\n\n"
                    "ORICO 케이스 펌웨어로 헤매는 분들을 위해 만들었습니다."))
        m.add_cascade(label="도움말", menu=h)
        self.root.config(menu=m)

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(6, 4))
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="파일 열기", command=self.cmd_open_file).pack(side="left")
        ttk.Button(bar, text="저장", command=self.cmd_save).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="펌웨어 분석", command=self.cmd_analyze).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="장치 검색", command=lambda: DeviceScanWindow(self)).pack(side="left", padx=4)
        ttk.Label(bar, text="오프셋:").pack(side="left", padx=(16, 2))
        self.goto_entry = ttk.Entry(bar, width=14)
        self.goto_entry.pack(side="left")
        self.goto_entry.bind("<Return>", lambda e: self.goto_text(self.goto_entry.get()))
        ttk.Button(bar, text="이동", command=lambda: self.goto_text(self.goto_entry.get())).pack(side="left", padx=2)
        ttk.Button(bar, text="찾기", command=lambda: FindDialog(self)).pack(side="left", padx=(12, 0))

    def _build_body(self):
        body = ttk.Frame(self.root)
        body.pack(side="top", fill="both", expand=True)
        side = ttk.LabelFrame(body, text="값 해석", padding=8)
        side.pack(side="right", fill="y", padx=(0, 6), pady=4)
        self.inspect_vars = {}
        for i, key in enumerate(("오프셋", "u8", "s8", "u16 LE", "u16 BE", "u32 LE", "u32 BE",
                                 "2진수", "문자")):
            ttk.Label(side, text=key).grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar()
            ttk.Label(side, textvariable=var, font=self.font, width=22).grid(row=i, column=1, sticky="w")
            self.inspect_vars[key] = var
        self.text = tk.Text(body, font=self.font, wrap="none", width=80, cursor="arrow",
                            insertwidth=0, padx=6, pady=4, exportselection=False)
        self.vsb = ttk.Scrollbar(body, orient="vertical", command=self._on_scrollbar)
        self.vsb.pack(side="right", fill="y", pady=4)
        self.text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        self.text.tag_configure("offs", foreground="#7a7a7a")
        self.text.tag_configure("mod", foreground="#d32f2f", font=tkfont.Font(font=self.font, weight="bold"))
        self.text.tag_configure("found", background="#ffe082")
        self.text.tag_configure("cur", background="#1e88e5", foreground="white")
        self.text.tag_configure("cur2", background="#bbdefb")
        self.text.configure(state="disabled")
        self.text.bind("<Configure>", self._on_resize)

    def _bind_keys(self):
        r, t = self.root, self.text
        r.bind("<Control-o>", lambda e: self.cmd_open_file())
        r.bind("<Control-s>", lambda e: self.cmd_save())
        r.bind("<Control-S>", lambda e: self.cmd_save_as())
        r.bind("<Control-z>", lambda e: self.undo())
        r.bind("<Control-y>", lambda e: self.redo())
        r.bind("<Control-g>", lambda e: self.cmd_goto())
        r.bind("<Control-f>", lambda e: FindDialog(self))
        r.bind("<F3>", lambda e: self.find_next(False))
        t.bind("<Button-1>", self._on_click)
        t.bind("<B1-Motion>", lambda e: "break")
        t.bind("<Double-Button-1>", lambda e: "break")
        t.bind("<MouseWheel>", lambda e: self._scroll_rows(-3 if e.delta > 0 else 3))
        t.bind("<Button-4>", lambda e: self._scroll_rows(-3))
        t.bind("<Button-5>", lambda e: self._scroll_rows(3))
        t.bind("<Key>", self._on_key)

    # --- 열기/닫기 ---
    def confirm_discard(self) -> bool:
        if self.src and self.src.modified:
            return messagebox.askyesno("저장하지 않은 변경", "저장하지 않은 변경이 있습니다. 버리시겠습니까?")
        return True

    def open_path(self, path: str, allow_device_write: bool = False):
        if not self.confirm_discard():
            return
        try:
            src = ByteSource(path, allow_device_write)
        except OSError as e:
            messagebox.showerror("열기 실패", f"{path}\n{e}{access_hint(e)}")
            return
        self.close_source(force=True)
        self.src = src
        self.top_row = self.cursor = 0
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.found = None
        self.root.title(f"{os.path.basename(src.path) or src.path} — {APP_NAME}")
        self.text.focus_set()
        self.render()

    def close_source(self, force: bool = False):
        if not force and not self.confirm_discard():
            return
        if self.src:
            self.src.close()
        self.src = None
        self.root.title(APP_NAME)
        self.render()

    def quit(self):
        if self.confirm_discard():
            if self.src:
                self.src.close()
            self.root.destroy()

    def cmd_open_file(self):
        path = filedialog.askopenfilename(title="펌웨어/이미지 파일 열기",
                                          filetypes=[("펌웨어/이미지", "*.bin *.img *.rom *.fw *.hex"),
                                                     ("모든 파일", "*.*")])
        if path:
            self.open_path(path)

    def cmd_open_device(self, preset: str | None = None):
        hint = "\\\\.\\PhysicalDrive1" if IS_WINDOWS else "/dev/nvme0n1 또는 /dev/sda"
        path = preset or simpledialog.askstring(
            "디스크 열기", f"장치 경로를 입력하세요 (예: {hint})\n장치 목록은 도구 → 장치 검색에서 확인",
            parent=self.root)
        if not path:
            return
        path = normalize_device_path(path.strip())
        write = False
        if messagebox.askyesno("쓰기 허용?", "디스크에 쓰기를 허용할까요?\n\n"
                               "'아니요'를 누르면 읽기 전용으로 엽니다 (권장).", default="no"):
            again = simpledialog.askstring(
                "위험 확인", f"디스크 데이터가 손상될 수 있습니다.\n확인을 위해 경로를 다시 입력하세요:\n{path}",
                parent=self.root)
            write = (again or "").strip() == path
            if not write:
                messagebox.showinfo("읽기 전용", "경로가 일치하지 않아 읽기 전용으로 엽니다.")
        self.open_path(path, allow_device_write=write)

    # --- 저장 ---
    def cmd_save(self):
        if not self.src or not self.src.modified:
            return
        if not self.src.writable:
            messagebox.showwarning("저장", "읽기 전용입니다. '다른 이름으로 저장'을 사용하세요.")
            return
        if self.src.is_device and not messagebox.askyesno(
                "장치에 쓰기", f"{self.src.path} 에 {len(self.src.patches)} 바이트를 기록합니다.\n"
                               "변경되는 블록의 원본은 ~/orico-nvme-backups 에 백업됩니다.\n계속할까요?",
                default="no", icon="warning"):
            return
        try:
            bak = self.src.save(backup=True)
        except OSError as e:
            messagebox.showerror("저장 실패", f"{e}{access_hint(e)}")
            return
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.render()
        messagebox.showinfo("저장", "저장했습니다." + (f"\n백업: {bak}" if bak else ""))

    def _run_with_progress(self, title: str, func):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        bar = ttk.Progressbar(win, length=360, mode="determinate", maximum=1000)
        bar.pack(padx=16, pady=16)

        def progress(done, total):
            bar["value"] = 1000 * done / max(total, 1)
            win.update()
        try:
            return func(progress)
        finally:
            win.destroy()

    def cmd_save_as(self):
        if not self.src:
            return
        if self.src.is_device and self.src.size > (1 << 30) and not messagebox.askyesno(
                "디스크 전체 저장", f"디스크 전체({human_size(self.src.size)})를 이미지로 저장합니다. "
                                   "오래 걸립니다. 계속할까요?\n(일부만 필요하면 '범위 내보내기' 사용)"):
            return
        out = filedialog.asksaveasfilename(title="다른 이름으로 저장", defaultextension=".bin")
        if not out:
            return
        if os.path.abspath(out) == os.path.abspath(self.src.path):
            self.cmd_save()
            return
        try:
            n = self._run_with_progress("저장 중", lambda p: self.src.save_as(out, progress=p))
        except OSError as e:
            messagebox.showerror("저장 실패", str(e))
            return
        if not self.src.is_device and messagebox.askyesno(
                "저장 완료", f"{out} ({n} 바이트) 저장 완료.\n새 파일을 열어 계속 편집할까요?"):
            self.src.patches.clear()
            self.open_path(out)

    def cmd_export_range(self):
        if not self.src:
            return
        rng = simpledialog.askstring("범위 내보내기", "시작 오프셋, 길이 (예: 0, 0x100000)",
                                     initialvalue=f"0x{self.cursor:X}, 0x1000", parent=self.root)
        if not rng:
            return
        try:
            off_s, len_s = rng.split(",", 1)
            off, length = parse_int(off_s), parse_int(len_s)
        except ValueError:
            messagebox.showerror("범위", "형식: 오프셋, 길이")
            return
        out = filedialog.asksaveasfilename(title="저장할 파일", defaultextension=".bin")
        if out:
            n = self._run_with_progress("내보내는 중", lambda p: self.src.save_as(out, off, length, p))
            messagebox.showinfo("내보내기", f"{out} 에 {n} 바이트 저장")

    # --- 이동/검색 ---
    def goto_text(self, text: str):
        if not self.src or not text.strip():
            return
        try:
            t = text.strip()
            off = self.cursor + parse_int(t) if t[0] in "+-" else parse_int(t)
        except ValueError:
            messagebox.showerror("이동", f"올바른 오프셋이 아닙니다: {text}")
            return
        self.move_to(off)

    def cmd_goto(self):
        if not self.src:
            return
        t = simpledialog.askstring("오프셋 이동", "오프셋 (0x1F0, 496, +0x10 …)", parent=self.root)
        if t:
            self.goto_text(t)

    def find_next(self, from_start: bool = False):
        if not self.src:
            return
        if not self.find_pattern:
            FindDialog(self)
            return
        start = 0 if from_start else self.cursor + 1
        self.status.set("검색 중…")
        self.root.update_idletasks()
        off = self.src.find(self.find_pattern, start)
        if off < 0 and start > 0:
            if messagebox.askyesno("찾기", "끝까지 찾지 못했습니다. 처음부터 다시 찾을까요?"):
                off = self.src.find(self.find_pattern, 0)
        if off < 0:
            messagebox.showinfo("찾기", "찾지 못했습니다.")
            self.render()
            return
        self.found = (off, off + len(self.find_pattern))
        self.move_to(off)

    # --- 편집 ---
    def _require_writable(self) -> bool:
        if not self.src:
            return False
        if not self.src.writable:
            self.status.set("읽기 전용입니다 — 디스크를 쓰기 허용으로 다시 열거나 이미지로 저장 후 편집하세요")
            self.root.bell()
            return False
        return True

    def apply_writes(self, writes: list[tuple[int, bytes]]):
        """여러 쓰기를 하나의 되돌리기 단위로 적용."""
        group = []
        for off, data in writes:
            group += [(off + i, self.src.get_patch(off + i)) for i in range(len(data))]
            self.src.write_bytes(off, data)
        if group:
            self.undo_stack.append(group)
            self.redo_stack.clear()
        self.render()

    def _swap(self, stack_from, stack_to):
        if not self.src or not stack_from:
            return
        group = stack_from.pop()
        stack_to.append([(off, self.src.get_patch(off)) for off, _ in group])
        for off, old in reversed(group):
            self.src.restore_patch(off, old)
        self.cursor = group[0][0]
        self.ensure_visible()
        self.render()

    def undo(self):
        self._swap(self.undo_stack, self.redo_stack)

    def redo(self):
        self._swap(self.redo_stack, self.undo_stack)

    def cmd_write_hex(self):
        if not self._require_writable():
            return
        t = simpledialog.askstring("바이트열 쓰기", f"0x{self.cursor:X} 부터 덮어쓸 16진수 (예: DE AD BE EF)",
                                   parent=self.root)
        if t:
            try:
                self.apply_writes([(self.cursor, parse_hex_bytes(t))])
            except ValueError as e:
                messagebox.showerror("쓰기", str(e))

    def cmd_fill(self):
        if not self._require_writable():
            return
        t = simpledialog.askstring("범위 채우기", "시작, 길이, 값 (예: 0x100, 0x20, FF)",
                                   initialvalue=f"0x{self.cursor:X}, 0x10, FF", parent=self.root)
        if not t:
            return
        try:
            off_s, len_s, val_s = [x.strip() for x in t.split(",")]
            length = parse_int(len_s)
            if length > (16 << 20):
                raise ValueError("한 번에 16MiB 까지 채울 수 있습니다")
            self.apply_writes([(parse_int(off_s), parse_hex_bytes(val_s)[:1] * length)])
        except ValueError as e:
            messagebox.showerror("채우기", str(e))

    # --- 도구 ---
    def cmd_analyze(self):
        if not self.src:
            messagebox.showinfo("펌웨어 분석", "먼저 펌웨어 파일을 여세요.")
            return
        AnalysisWindow(self)

    def cmd_fix_checksum(self):
        if not self._require_writable():
            return
        if self.src.size > ANALYZE_LIMIT:
            messagebox.showinfo("체크섬", "파일이 너무 커서 지원하지 않습니다.")
            return
        schemes = detect_checksum_schemes(self.src.read_raw(0, self.src.size))
        if not schemes:
            messagebox.showinfo("체크섬", "원본에서 단순 체크섬 방식을 찾지 못했습니다.\n"
                                         "(헤더 내부 체크섬을 쓰는 펌웨어는 제조사 툴을 사용하세요)")
            return
        scheme = schemes[0]
        if not messagebox.askyesno("체크섬 재계산",
                                   f"원본과 일치하는 방식: {', '.join(schemes)}\n\n"
                                   f"'{scheme}' ({CHECKSUM_SCHEMES[scheme]}) 방식으로 재계산할까요?"):
            return
        off, data = checksum_fix(bytes(self.src.read(0, self.src.size)), scheme)
        self.apply_writes([(off, data)])
        self.move_to(off)

    def cmd_diff(self):
        a = filedialog.askopenfilename(title="비교할 첫 번째 파일 (A)",
                                       initialfile=self.src.path if self.src and not self.src.is_device else "")
        if not a:
            return
        b = filedialog.askopenfilename(title="비교할 두 번째 파일 (B)")
        if not b:
            return
        try:
            with open(a, "rb") as fa, open(b, "rb") as fb:
                text = format_diff(fa.read(), fb.read(), os.path.basename(a), os.path.basename(b))
        except OSError as e:
            messagebox.showerror("비교", str(e))
            return
        self.show_text("파일 비교", text)

    def show_text(self, title: str, text: str):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("900x600")
        st = ScrolledText(win, font=self.font, wrap="none")
        st.pack(fill="both", expand=True)
        st.insert("1.0", text)
        st.configure(state="disabled")

    # --- 화면 ---
    @property
    def off_width(self) -> int:
        return 8 if not self.src or self.src.size <= 0xFFFFFFFF else 12

    def _cols(self) -> tuple[int, int]:
        hx = self.off_width + 2
        return hx, hx + 49

    def total_rows(self) -> int:
        return (self.src.size + 15) // 16 if self.src else 0

    def _clamp_top(self):
        self.top_row = max(0, min(self.top_row, max(0, self.total_rows() - self.rows)))

    def ensure_visible(self):
        row = self.cursor // 16
        if row < self.top_row:
            self.top_row = row
        elif row >= self.top_row + self.rows:
            self.top_row = row - self.rows + 1
        self._clamp_top()

    def move_to(self, off: int):
        if not self.src or self.src.size == 0:
            return
        self.cursor = max(0, min(off, self.src.size - 1))
        self.nibble_pending = False
        self.ensure_visible()
        self.render()

    def _on_resize(self, event):
        ls = self.font.metrics("linespace")
        rows = max(4, (event.height - 12) // max(ls, 1))
        if rows != self.rows:
            self.rows = rows
            self._clamp_top()
            self.render()

    def _on_scrollbar(self, *args):
        if not self.src:
            return
        if args[0] == "moveto":
            self.top_row = int(float(args[1]) * self.total_rows())
        elif args[0] == "scroll":
            self.top_row += int(args[1]) * (self.rows if args[2] == "pages" else 1)
        self._clamp_top()
        self.render()

    def _scroll_rows(self, n: int):
        if self.src:
            self.top_row += n
            self._clamp_top()
            self.render()
        return "break"

    def _on_click(self, event):
        self.text.focus_set()
        if not self.src:
            return "break"
        line, col = map(int, self.text.index(f"@{event.x},{event.y}").split("."))
        hx, ax = self._cols()
        if hx <= col < hx + 47:
            i, area = (col - hx) // 3, "hex"
        elif ax <= col < ax + 16:
            i, area = col - ax, "ascii"
        else:
            return "break"
        off = (self.top_row + line - 1) * 16 + i
        if off < self.src.size:
            self.area = area
            self.move_to(off)
        return "break"

    def _on_key(self, event):
        if not self.src:
            return None
        if event.state & 0x4:  # Ctrl 조합은 전역 단축키로
            return None
        k = event.keysym
        moves = {"Left": -1, "Right": 1, "Up": -16, "Down": 16,
                 "Prior": -16 * self.rows, "Next": 16 * self.rows}
        if k in moves:
            self.move_to(self.cursor + moves[k])
            return "break"
        if k == "Home":
            self.move_to(self.cursor - self.cursor % 16)
            return "break"
        if k == "End":
            self.move_to(self.cursor - self.cursor % 16 + 15)
            return "break"
        if k in ("Tab", "ISO_Left_Tab"):
            self.area = "ascii" if self.area == "hex" else "hex"
            self.nibble_pending = False
            self.render()
            return "break"
        ch = event.char
        if not ch or len(ch) != 1 or not 32 <= ord(ch) < 127:
            return "break"
        if not self._require_writable():
            return "break"
        cur = self.src.read(self.cursor, 1)
        if not cur:
            return "break"
        cur = cur[0]
        if self.area == "hex":
            if ch.lower() not in "0123456789abcdef":
                return "break"
            d = int(ch, 16)
            if not self.nibble_pending:
                self.apply_writes([(self.cursor, bytes([(d << 4) | (cur & 0x0F)]))])
                self.nibble_pending = True
            else:
                # 첫 니블 입력 때 되돌리기 항목이 이미 만들어졌으므로 여기선 값만 바꾼다
                self.src.write_bytes(self.cursor, bytes([(cur & 0xF0) | d]))
                self.nibble_pending = False
                self.move_to(self.cursor + 1)
        else:
            self.apply_writes([(self.cursor, ch.encode("ascii"))])
            self.move_to(self.cursor + 1)
        return "break"

    def render(self):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not self.src:
            t.insert("1.0", "\n  파일 → 파일 열기 (Ctrl+O) 로 펌웨어 .bin 을 열거나\n"
                            "  도구 → 장치 검색 으로 케이스의 브릿지 칩과 펌웨어 버전을 확인하세요.\n\n"
                            "  도움말 → 사용법 에 작업 순서가 있습니다.")
            t.configure(state="disabled")
            self.vsb.set(0, 1)
            self.status.set(f"{APP_NAME} {__version__}")
            for v in self.inspect_vars.values():
                v.set("")
            return
        w = self.off_width
        hx, ax = self._cols()
        start = self.top_row * 16
        data = self.src.read(start, self.rows * 16)
        lines = []
        for r in range(0, len(data), 16):
            chunk = data[r:r + 16]
            lines.append(f"{start + r:0{w}X}  " + " ".join(f"{b:02X}" for b in chunk).ljust(47) + "  "
                         + "".join(chr(b) if 32 <= b < 127 else "." for b in chunk))
        t.insert("1.0", "\n".join(lines))
        for r in range(len(lines)):
            t.tag_add("offs", f"{r + 1}.0", f"{r + 1}.{w}")

        def mark(off, tag, hex_tag=None):
            rel = off - start
            if 0 <= rel < len(data):
                line, i = rel // 16 + 1, rel % 16
                t.tag_add(hex_tag or tag, f"{line}.{hx + 3 * i}", f"{line}.{hx + 3 * i + 2}")
                t.tag_add(tag, f"{line}.{ax + i}", f"{line}.{ax + i + 1}")

        for off in self.src.patched_in_range(start, start + len(data)):
            mark(off, "mod")
        if self.found:
            for off in range(max(self.found[0], start), min(self.found[1], start + len(data))):
                mark(off, "found")
        rel = self.cursor - start
        if 0 <= rel < len(data):
            line, i = rel // 16 + 1, rel % 16
            main_tag, other = ("cur", "cur2") if self.area == "hex" else ("cur2", "cur")
            t.tag_add(main_tag, f"{line}.{hx + 3 * i}", f"{line}.{hx + 3 * i + 2}")
            t.tag_add(other, f"{line}.{ax + i}", f"{line}.{ax + i + 1}")
        t.tag_raise("cur")
        t.tag_raise("cur2")
        t.configure(state="disabled")

        total = max(self.total_rows(), 1)
        self.vsb.set(self.top_row / total, min(1.0, (self.top_row + self.rows) / total))
        self._update_inspector()
        mode = "편집 가능" if self.src.writable else "읽기 전용"
        kind = "디스크" if self.src.is_device else "파일"
        self.status.set(f"{self.src.path}  |  {kind} {human_size(self.src.size)}  |  {mode}  |  "
                        f"커서 0x{self.cursor:X} ({self.cursor})  |  영역 {self.area.upper()}  |  "
                        f"변경 {len(self.src.patches)} 바이트")

    def _update_inspector(self):
        b = bytes(self.src.read(self.cursor, 4))
        v = self.inspect_vars
        v["오프셋"].set(f"0x{self.cursor:X} / {self.cursor}")
        if not b:
            return
        v["u8"].set(f"{b[0]} (0x{b[0]:02X})")
        v["s8"].set(str(struct.unpack("b", b[:1])[0]))
        v["u16 LE"].set(f"{int.from_bytes(b[:2], 'little')} (0x{int.from_bytes(b[:2], 'little'):04X})"
                        if len(b) >= 2 else "-")
        v["u16 BE"].set(f"{int.from_bytes(b[:2], 'big')} (0x{int.from_bytes(b[:2], 'big'):04X})"
                        if len(b) >= 2 else "-")
        v["u32 LE"].set(f"0x{int.from_bytes(b, 'little'):08X}" if len(b) == 4 else "-")
        v["u32 BE"].set(f"0x{int.from_bytes(b, 'big'):08X}" if len(b) == 4 else "-")
        v["2진수"].set(f"{b[0]:08b}")
        v["문자"].set(repr(chr(b[0])) if 32 <= b[0] < 127 else "-")


class AnalysisWindow:
    def __init__(self, app: HexEditorApp):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("펌웨어 분석")
        self.win.geometry("980x680")
        btns = ttk.Frame(self.win, padding=6)
        btns.pack(side="top", fill="x")
        ttk.Button(btns, text="선택 위치로 이동", command=self.jump).pack(side="left")
        ttk.Button(btns, text="VID/PID/버전 수정…", command=self.edit_usb_id).pack(side="left", padx=4)
        ttk.Button(btns, text="문자열 수정…", command=self.edit_string).pack(side="left")
        ttk.Button(btns, text="체크섬 재계산", command=self.fix_checksum).pack(side="left", padx=4)
        ttk.Button(btns, text="새로 고침", command=self.refresh).pack(side="right")
        pane = ttk.PanedWindow(self.win, orient="vertical")
        pane.pack(fill="both", expand=True)
        frame = ttk.Frame(pane)
        self.tree = ttk.Treeview(frame, columns=("kind", "offset", "value"), show="headings", height=9)
        for col, label, width in (("kind", "종류", 140), ("offset", "오프셋", 110), ("value", "내용", 640)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=(col == "value"))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.jump())
        self.report = ScrolledText(pane, font=app.font, wrap="none")
        pane.add(frame, weight=1)
        pane.add(self.report, weight=2)
        self.items: dict[str, tuple[str, object]] = {}
        self.refresh()

    def refresh(self):
        src = self.app.src
        if not src:
            self.win.destroy()
            return
        data = _read_limited(src)
        original = _read_limited(src, original=True)
        rep = analyze_firmware(data, original)
        self.descs = find_device_descriptors(data)
        self.strings = find_string_descriptors(data)
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        for i, d in enumerate(self.descs):
            iid = self.tree.insert("", "end", values=(f"USB 장치 #{i}", f"0x{d.offset:X}", d.describe()))
            self.items[iid] = ("dev", d)
        for s in self.strings:
            iid = self.tree.insert("", "end", values=("USB 문자열", f"0x{s.offset:X}",
                                                      f"{s.text!r} (최대 {s.max_chars}자)"))
            self.items[iid] = ("str", s)
        for off, s in rep["strings"]:
            iid = self.tree.insert("", "end", values=("문자열", f"0x{off:X}", s))
            self.items[iid] = ("txt", off)
        self.report.configure(state="normal")
        self.report.delete("1.0", "end")
        note = f"(앞부분 {human_size(ANALYZE_LIMIT)}만 분석)\n" if src.size > ANALYZE_LIMIT else ""
        self.report.insert("1.0", note + format_report(rep, os.path.basename(src.path)))
        self.report.configure(state="disabled")

    def _selected(self):
        sel = self.tree.selection()
        return self.items.get(sel[0]) if sel else None

    def jump(self):
        item = self._selected()
        if item:
            kind, obj = item
            self.app.move_to(obj if kind == "txt" else obj.offset)

    def _after_edit(self, off):
        self.app.move_to(off)
        self.refresh()
        if detect_checksum_schemes(_read_limited(self.app.src, original=True)):
            messagebox.showinfo("체크섬", "이 파일은 체크섬 후보가 있습니다. '체크섬 재계산'을 고려하세요.",
                                parent=self.win)

    def edit_usb_id(self):
        item = self._selected()
        if not item or item[0] != "dev":
            messagebox.showinfo("VID/PID 수정", "목록에서 'USB 장치' 항목을 선택하세요.", parent=self.win)
            return
        if not self.app._require_writable():
            return
        d: UsbDeviceDescriptor = item[1]
        t = simpledialog.askstring("VID/PID/bcdDevice 수정", "VID:PID:bcdDevice (16진수)",
                                   initialvalue=f"{d.vid:04X}:{d.pid:04X}:{d.bcd_device:04X}", parent=self.win)
        if not t:
            return
        try:
            parts = [int(p, 16) for p in t.split(":")]
            if len(parts) not in (2, 3):
                raise ValueError("형식: VID:PID 또는 VID:PID:bcdDevice")
            self.app.apply_writes([usb_id_patch(d, parts[0], parts[1], parts[2] if len(parts) == 3 else None)])
        except ValueError as e:
            messagebox.showerror("VID/PID 수정", str(e), parent=self.win)
            return
        self._after_edit(d.offset)

    def edit_string(self):
        item = self._selected()
        if not item or item[0] != "str":
            messagebox.showinfo("문자열 수정", "목록에서 'USB 문자열' 항목을 선택하세요.", parent=self.win)
            return
        if not self.app._require_writable():
            return
        s: UsbStringDescriptor = item[1]
        t = simpledialog.askstring("문자열 수정", f"새 문자열 (최대 {s.max_chars}자, ASCII)",
                                   initialvalue=s.text, parent=self.win)
        if t is None:
            return
        try:
            self.app.apply_writes([usb_string_patch(s, t)])
        except ValueError as e:
            messagebox.showerror("문자열 수정", str(e), parent=self.win)
            return
        self._after_edit(s.offset)

    def fix_checksum(self):
        self.app.cmd_fix_checksum()
        self.refresh()


class DeviceScanWindow:
    def __init__(self, app: HexEditorApp):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("장치 검색")
        self.win.geometry("1000x480")
        btns = ttk.Frame(self.win, padding=6)
        btns.pack(side="top", fill="x")
        ttk.Button(btns, text="새로 고침", command=self.refresh).pack(side="left")
        ttk.Button(btns, text="선택한 디스크 열기", command=self.open_selected).pack(side="left", padx=4)
        ttk.Button(btns, text="텍스트로 보기", command=self.as_text).pack(side="left")
        self.info = tk.StringVar()
        ttk.Label(btns, textvariable=self.info).pack(side="left", padx=12)
        cols = (("kind", "종류", 90), ("path", "경로", 220), ("name", "이름", 230),
                ("id", "VID:PID", 90), ("fw", "펌웨어/REV", 130), ("chip", "브릿지 칩 추정", 260))
        self.tree = ttk.Treeview(self.win, columns=[c[0] for c in cols], show="headings")
        for key, label, width in cols:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.items: list[dict] = []
        self.refresh()

    def refresh(self):
        self.info.set("검색 중…")
        self.tree.delete(*self.tree.get_children())
        result: dict = {}

        def worker():
            try:
                result["items"] = scan_devices()
            except Exception as e:  # PowerShell 없음 등
                result["error"] = str(e)
        th = threading.Thread(target=worker, daemon=True)
        th.start()

        def poll():
            if th.is_alive():
                self.win.after(150, poll)
                return
            if "error" in result:
                self.info.set(f"검색 실패: {result['error']}")
                return
            self.items = result["items"]
            kinds = {"usb": "USB 브릿지", "nvme": "NVMe", "disk": "디스크"}
            for i, it in enumerate(self.items):
                vidpid = f"{it['vid']}:{it['pid']}" if it.get("vid") else ""
                self.tree.insert("", "end", iid=str(i), values=(
                    kinds.get(it["kind"], it["kind"]), it.get("path", ""), it.get("name", ""), vidpid,
                    it.get("firmware", ""), it.get("bridge", "")))
            self.info.set(f"{len(self.items)}개 발견" + ("" if self.items else
                          " — USB 케이스를 연결했는지 확인하세요"))
        poll()

    def open_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        it = self.items[int(sel[0])]
        path = it.get("path", "").split(",")[0].strip()
        if it["kind"] == "usb" and not path.startswith(("/dev/", "\\\\.\\")):
            messagebox.showinfo("열기", "USB 브릿지 항목은 디스크 경로가 아닙니다.\n"
                                        "같은 크기의 '디스크' 항목을 선택하세요.", parent=self.win)
            return
        if path:
            self.app.cmd_open_device(path)

    def as_text(self):
        self.app.show_text("장치 검색 결과", format_scan(self.items))


def run_gui(initial_path: str | None = None) -> int:
    if tk is None:
        print("tkinter 가 없어 GUI를 열 수 없습니다.\n"
              "  Debian/Ubuntu/Raspberry Pi OS: sudo apt install python3-tk\n"
              "  Fedora: sudo dnf install python3-tkinter\n"
              "CLI 사용법은 --help 를 참고하세요.", file=sys.stderr)
        return 2
    _hide_own_console()
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"GUI를 열 수 없습니다 (디스플레이 없음?): {e}\nCLI 사용법은 --help 참고", file=sys.stderr)
        return 2
    try:
        ttk.Style(root).theme_use("vista" if IS_WINDOWS else "clam")
    except tk.TclError:
        pass
    HexEditorApp(root, initial_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
