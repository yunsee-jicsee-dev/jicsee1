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
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
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
SECTOR = 512                 # 파일/이미지의 불량 섹터 추적 단위
CHUNK = 1 << 20              # 검색·복사 시 한 번에 읽는 크기
ANALYZE_LIMIT = 64 << 20     # 펌웨어 분석에 쓰는 최대 크기

# ---------------------------------------------------------------------------
# 브릿지 칩 정보
# ---------------------------------------------------------------------------

BRIDGE_VENDORS = {0x0BDA: "Realtek", 0x152D: "JMicron", 0x174C: "ASMedia", 0x13FD: "Initio"}
SATA_ONLY_VENDORS = {0x13FD}  # USB↔SATA/PATA 브릿지만 만드는 회사 (NVMe 케이스 칩이 아님)

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
    if vid in SATA_ONLY_VENDORS:
        return f"{BRIDGE_VENDORS[vid]} 칩 — USB↔SATA 브릿지 (NVMe 케이스 칩 아님)"
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


def parse_size(text: str) -> int:
    """'512G', '1T', '0x100000', '4096' 같은 크기 표기 (K/M/G/T 는 1024 단위)."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KMGT])i?B?\s*", text, re.IGNORECASE)
    if m:
        return int(float(m.group(1)) * 1024 ** ("KMGT".index(m.group(2).upper()) + 1))
    return parse_int(text)


def parse_hex_bytes(text: str) -> bytes:
    cleaned = re.sub(r"[\s:,\-]|0x", "", text.strip(), flags=re.IGNORECASE)
    if not cleaned or len(cleaned) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", cleaned):
        raise ValueError(f"올바른 16진수 바이트열이 아닙니다: {text!r}")
    return bytes.fromhex(cleaned)


def hexdump_lines(data: bytes, base: int = 0, bad: set[int] | None = None):
    """bad: 읽기 실패한 바이트의 절대 오프셋 집합 → '??' 로 표시."""
    width = 8 if base + len(data) <= 0xFFFFFFFF else 12
    bad = bad or set()
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        offs = range(base + i, base + i + len(chunk))
        hexpart = " ".join("??" if o in bad else f"{b:02X}" for o, b in zip(offs, chunk)).ljust(47)
        asc = "".join("?" if o in bad else (chr(b) if 32 <= b < 127 else ".") for o, b in zip(offs, chunk))
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


def _win_ioctl(path: str, code: int, out_size: int) -> bytes:
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
        buf = ctypes.create_string_buffer(out_size)
        returned = wintypes.DWORD(0)
        if not k32.DeviceIoControl(handle, code, None, 0, buf, out_size, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.raw[:returned.value]
    finally:
        k32.CloseHandle(handle)


def device_size(path: str) -> int:
    if IS_WINDOWS:
        return struct.unpack_from("<q", _win_ioctl(path, 0x0007405C, 8))[0]  # IOCTL_DISK_GET_LENGTH_INFO
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def device_sector_size(path: str) -> int:
    """논리 섹터 크기 (512 또는 4096). 알 수 없으면 512."""
    try:
        if IS_WINDOWS:
            # IOCTL_DISK_GET_DRIVE_GEOMETRY_EX → DISK_GEOMETRY.BytesPerSector (오프셋 20)
            value = struct.unpack_from("<I", _win_ioctl(path, 0x000700A0, 256), 20)[0]
        else:
            import fcntl
            fd = os.open(path, os.O_RDONLY)
            try:
                value = struct.unpack("i", fcntl.ioctl(fd, 0x1268, b"\0" * 4))[0]  # BLKSSZGET
            finally:
                os.close(fd)
        return value if value in (512, 1024, 2048, 4096) else SECTOR
    except Exception:
        return SECTOR


class RangeSet:
    """겹치거나 붙은 구간을 합쳐서 보관하는 [시작, 끝) 구간 집합 (불량 섹터 기록용).

    백그라운드 읽기 스레드와 화면 스레드가 함께 쓰므로 자체 잠금을 둔다."""

    def __init__(self, ranges=()):
        self._starts: list[int] = []
        self._ends: list[int] = []
        self._lock = threading.Lock()
        for s, e in ranges:
            self.add(s, e)

    def add(self, s: int, e: int):
        if e <= s:
            return
        with self._lock:
            i = bisect.bisect_left(self._ends, s)
            j = i
            while j < len(self._starts) and self._starts[j] <= e:
                s, e = min(s, self._starts[j]), max(e, self._ends[j])
                j += 1
            self._starts[i:j] = [s]
            self._ends[i:j] = [e]

    def remove(self, s: int, e: int):
        with self._lock:
            kept = []
            for a, b in zip(self._starts, self._ends):
                if b <= s or a >= e:
                    kept.append((a, b))
                else:
                    if a < s:
                        kept.append((a, s))
                    if b > e:
                        kept.append((e, b))
            self._starts = [a for a, _ in kept]
            self._ends = [b for _, b in kept]

    def overlaps(self, s: int, e: int) -> list[tuple[int, int]]:
        """[s, e) 와 겹치는 부분들 (잘라서 반환)."""
        with self._lock:
            out = []
            i = bisect.bisect_right(self._ends, s)
            while i < len(self._starts) and self._starts[i] < e:
                out.append((max(s, self._starts[i]), min(e, self._ends[i])))
                i += 1
            return out

    def next_after(self, off: int) -> int | None:
        with self._lock:
            i = bisect.bisect_right(self._starts, off)
            return self._starts[i] if i < len(self._starts) else None

    def ranges(self) -> list[tuple[int, int]]:
        with self._lock:
            return list(zip(self._starts, self._ends))

    def total(self) -> int:
        with self._lock:
            return sum(e - s for s, e in zip(self._starts, self._ends))

    def __len__(self) -> int:
        return len(self._starts)


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


def rescue_map_path(image_path: str) -> str:
    return image_path + ".rescue.json"


class ByteSource:
    """파일/디스크를 연다. size 를 주면 용량 인식이 안 되는 고장 디스크도 그 크기로 연다.

    읽다가 실패한 섹터는 0으로 채우고 self.bad 에 기록한 뒤 계속 진행한다(고장 디스크 탐색용).
    한 번 실패한 섹터는 다시 읽지 않으므로 같은 곳을 볼 때마다 오래 멈추지 않는다."""

    def __init__(self, path: str, allow_device_write: bool = False, size: int | None = None):
        self.path = normalize_device_path(path)
        self.is_device = is_device_path(self.path)
        self.sector = device_sector_size(self.path) if self.is_device else SECTOR
        self.align = max(DEVICE_ALIGN, self.sector) if self.is_device else 1
        if size is not None:
            self.size = size
        else:
            self.size = device_size(self.path) if self.is_device else os.path.getsize(self.path)
        self.writable = (not self.is_device) or allow_device_write
        self.patches: dict[int, int] = {}
        self._sorted: list[int] | None = None
        self.bad = RangeSet()
        self.retries = 0
        self.rescue_info: dict | None = None
        self._lock = threading.RLock()
        self._fh = open(self.path, "rb", buffering=0)
        if not self.is_device:
            self._load_rescue_map()

    def _load_rescue_map(self):
        """복구 이미지 옆의 .rescue.json 이 있으면 못 읽은 구간을 불량으로 표시."""
        try:
            with open(rescue_map_path(self.path), encoding="utf-8") as f:
                info = json.load(f)
            base = int(info["offset"])
            for s, e in list(info.get("bad", [])) + list(info.get("pending", [])):
                self.bad.add(max(0, s - base), min(self.size, e - base))
            if int(info.get("pos", base + self.size)) < base + self.size:  # 아직 복사 안 한 뒷부분
                self.bad.add(int(info["pos"]) - base, self.size)
            self.sector = int(info.get("sector", SECTOR))
            self.rescue_info = info
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def close(self):
        fh, self._fh = self._fh, None
        if fh:
            fh.close()

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

    def _pread(self, start: int, n: int) -> bytes:
        """정확히 n 바이트를 읽거나 OSError (짧게 읽혀도 오류로 본다)."""
        if self._fh is None:
            raise OSError(errno.EBADF, "닫힌 장치")
        self._fh.seek(start)
        data = self._read_fully(n)
        if len(data) != n:
            raise OSError(errno.EIO, f"0x{start:X} 에서 {n}바이트 중 {len(data)}바이트만 읽힘")
        return data

    def read_strict(self, offset: int, length: int) -> bytes:
        """오류를 그대로 올려보내는 읽기 (복구 이미지용). offset/length 는 섹터 정렬."""
        with self._lock:
            return self._pread(offset, length)

    def _read_tolerant(self, start: int, end: int) -> bytearray:
        """섹터 단위로 읽고, 실패한 섹터는 0으로 채워 self.bad 에 기록."""
        out = bytearray()
        pos = start
        while pos < end:
            n = min(self.sector, end - pos)
            data = None
            if not self.bad.overlaps(pos, pos + n):
                for _ in range(self.retries + 1):
                    try:
                        data = self._pread(pos, n)
                        break
                    except OSError:
                        data = None
                if data is None:
                    self.bad.add(pos, pos + n)
            out += data if data is not None else bytes(n)
            pos += n
        return out

    def read_raw(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        start = offset - offset % self.align
        end = offset + length
        end = min(end + (-end) % self.align, self.size)
        with self._lock:
            buf = None
            if not self.bad.overlaps(start, end):
                try:
                    buf = self._pread(start, end - start)
                except OSError:
                    buf = None
            if buf is None:  # 오류 → 섹터 단위로 다시 읽으며 불량 섹터만 건너뜀
                s0 = start - start % self.sector
                e0 = min(end + (-end) % self.sector, self.size)
                buf = self._read_tolerant(s0, e0)[start - s0:]
        return bytes(buf[offset - start:offset - start + length])

    def bad_offsets(self, start: int, end: int) -> set[int]:
        out: set[int] = set()
        for s, e in self.bad.overlaps(start, end):
            out.update(range(s, e))
        return out

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
            for s, e in runs:
                self.read_raw(s, e - s)  # 원본 확인 (실패 시 bad 에 기록됨)
                if self.bad.overlaps(s, e):
                    raise OSError(errno.EIO, f"0x{s:X} 블록에 읽기 실패 섹터가 있어 쓰지 않습니다 "
                                             "(원본을 백업할 수 없음)")
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
# 고장 디스크 탐색: 파티션/파일시스템 구조, 시그니처 스캔, 복구 이미지
# ---------------------------------------------------------------------------

GPT_TYPES = {
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI 시스템",
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "기본 데이터 (Windows)",
    "E3C9E316-0B5C-4DB8-817D-F92DF00215AE": "Microsoft 예약(MSR)",
    "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": "Windows 복구",
    "0FC63DAF-8483-4772-8E79-3D69D8477DE4": "Linux 파일시스템",
    "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F": "Linux 스왑",
    "E6D6D379-F507-44C2-A23C-238F2A3DF928": "Linux LVM",
    "7C3457EF-0000-11AA-AA11-00306543ECAC": "Apple APFS",
    "48465300-0000-11AA-AA11-00306543ECAC": "Apple HFS+",
}
MBR_TYPES = {0x01: "FAT12", 0x04: "FAT16", 0x05: "확장", 0x06: "FAT16", 0x07: "NTFS/exFAT", 0x0B: "FAT32",
             0x0C: "FAT32 (LBA)", 0x0E: "FAT16 (LBA)", 0x0F: "확장 (LBA)", 0x82: "Linux 스왑", 0x83: "Linux",
             0x8E: "Linux LVM", 0xAF: "Apple HFS+", 0xEE: "GPT 보호", 0xEF: "EFI"}


def detect_fs(src: ByteSource, off: int) -> str:
    """off 위치에서 시작하는 파일시스템 종류 (모르면 빈 문자열)."""
    head = bytes(src.read(off, 4096))
    if len(head) < 512:
        return ""
    if head[3:11] == b"NTFS    ":
        return "NTFS"
    if head[3:11] == b"EXFAT   ":
        return "exFAT"
    if head[3:11] == b"-FVE-FS-":
        return "BitLocker 암호화"
    if head[82:90] == b"FAT32   ":
        return "FAT32"
    if head[54:59] in (b"FAT12", b"FAT16"):
        return head[54:59].decode()
    if head[:6] == b"LUKS\xba\xbe":
        return "LUKS 암호화"
    if head[:4] == b"XFSB":
        return "XFS"
    if head[32:36] == b"NXSB":
        return "APFS"
    if len(head) >= 1084 and head[1080:1082] == b"\x53\xef":
        return "ext2/3/4"
    if head[1024:1026] in (b"H+", b"HX"):
        return "HFS+"
    if bytes(src.read(off + 0x10040, 8)) == b"_BHRfS_M":
        return "Btrfs"
    return ""


def _gpt_header_crc_ok(hdr: bytes) -> bool:
    hs = struct.unpack_from("<I", hdr, 12)[0]
    if not 92 <= hs <= len(hdr):
        return False
    h = bytearray(hdr[:hs])
    stored = struct.unpack_from("<I", h, 16)[0]
    h[16:20] = bytes(4)
    return zlib.crc32(h) & 0xFFFFFFFF == stored


def parse_layout(src: ByteSource) -> list[dict]:
    """MBR/GPT 와 각 파티션의 파일시스템을 읽어 탐색용 목록을 만든다. 손상 여부도 표시."""
    items: list[dict] = []

    def add(kind, off, length, label, detail=""):
        items.append({"kind": kind, "offset": off, "length": length, "label": label, "detail": detail})

    mbr = bytes(src.read(0, 512))
    fs0 = detect_fs(src, 0)
    has_mbr = len(mbr) == 512 and mbr[510:512] == b"\x55\xaa" and not fs0
    gpt_found = False
    for ss in sorted({src.sector, 512, 4096}):
        hdr = bytes(src.read(ss, 512))
        if hdr[:8] != b"EFI PART":
            continue
        gpt_found = True
        _my, alt_lba = struct.unpack_from("<QQ", hdr, 24)
        ent_lba, n, esz, ent_crc = struct.unpack_from("<QIII", hdr, 72)
        crc_ok = _gpt_header_crc_ok(hdr)
        add("gpt", ss, 92, "GPT 헤더 (주)",
            f"섹터 {ss}B, CRC {'정상' if crc_ok else '손상!'}, 파티션 항목 {n}개, 백업 헤더 LBA {alt_lba}")
        n = min(n, 1024)
        esz = esz if 128 <= esz <= 1024 else 128
        table = bytes(src.read(ent_lba * ss, n * esz))
        ent_ok = zlib.crc32(table) & 0xFFFFFFFF == ent_crc
        add("gpt", ent_lba * ss, n * esz, "GPT 파티션 항목", f"CRC {'정상' if ent_ok else '손상!'}")
        bk_off = alt_lba * ss
        bk = bytes(src.read(bk_off, 512)) if 0 < bk_off < src.size else b""
        if bk[:8] == b"EFI PART":
            add("gpt", bk_off, 92, "GPT 헤더 (백업)", "CRC 정상" if _gpt_header_crc_ok(bk) else "CRC 손상!")
        else:
            add("warn", bk_off if 0 < bk_off < src.size else 0, 0, "GPT 백업 헤더 없음",
                "디스크 끝부분 손상이거나, 케이스가 용량을 잘못 인식하고 있을 수 있음")
        for i in range(n):
            e = table[i * esz:i * esz + 128]
            if len(e) < 128 or e[:16] == bytes(16):
                continue
            tguid = str(uuid.UUID(bytes_le=e[:16])).upper()
            first, last = struct.unpack_from("<QQ", e, 32)
            name = e[56:128].decode("utf-16-le", "replace").split("\x00")[0]
            start = first * ss
            fs = detect_fs(src, start) if start < src.size else ""
            detail = " / ".join(x for x in (GPT_TYPES.get(tguid, tguid), fs and f"파일시스템 {fs}",
                                            "" if start < src.size else "디스크 범위 밖!") if x)
            add("part", start, (last - first + 1) * ss, f"파티션 {i + 1}: {name or GPT_TYPES.get(tguid, '?')}",
                detail)
        break
    if fs0:
        add("fs", 0, src.size, f"오프셋 0 의 파일시스템: {fs0}", "파티션 테이블 없이 통째로 포맷된 디스크")
    elif has_mbr:
        add("mbr", 0, 512, "MBR", "부트 시그니처 55AA 정상")
        for i in range(4):
            e = mbr[446 + 16 * i:462 + 16 * i]
            ptype = e[4]
            lba, count = struct.unpack_from("<II", e, 8)
            if ptype == 0 or count == 0:
                continue
            if ptype == 0xEE:
                if not gpt_found:
                    add("warn", 512, 0, "GPT 보호 MBR 인데 GPT 헤더가 없음",
                        "GPT 손상 가능 — 백업 GPT(디스크 끝) 나 시그니처 스캔으로 파티션을 찾아보세요")
                continue
            if gpt_found:
                continue
            start = lba * src.sector
            fs = detect_fs(src, start) if start < src.size else ""
            add("part", start, count * src.sector, f"MBR 파티션 {i + 1}: {MBR_TYPES.get(ptype, f'0x{ptype:02X}')}",
                f"파일시스템 {fs}" if fs else ("디스크 범위 밖!" if start >= src.size else ""))
    elif not gpt_found:
        add("warn", 0, 512, "파티션 테이블 없음",
            "LBA0 이 비었거나 손상됨 — 시그니처 스캔으로 파티션 시작 위치를 찾아보세요")
    for s, e in src.bad.ranges()[:200]:
        add("bad", s, e - s, "읽기 실패 구간")
    return items


def format_layout(items: list[dict]) -> str:
    if not items:
        return "구조 정보 없음"
    return "\n".join(f"0x{it['offset']:012X}  {human_size(it['length']):>12}  {it['label']}"
                     + (f"  — {it['detail']}" if it["detail"] else "") for it in items)


def _bootsig(d: bytes, i: int) -> bool:
    return d[i + 510:i + 512] == b"\x55\xaa"


def _ext_superblock(d: bytes, i: int) -> bool:
    log_bs, = struct.unpack_from("<I", d, i + 24)
    state, = struct.unpack_from("<H", d, i + 58)
    rev, = struct.unpack_from("<I", d, i + 76)
    return log_bs <= 6 and rev <= 1 and 1 <= state <= 7


# (이름, 섹터 시작 기준 위치, 매직 바이트, 추가 검사)
SIGNATURES = [
    ("GPT 헤더", 0, b"EFI PART", None),
    ("NTFS 부트 섹터 (파티션 시작)", 3, b"NTFS    ", _bootsig),
    ("exFAT 부트 섹터 (파티션 시작)", 3, b"EXFAT   ", _bootsig),
    ("FAT32 부트 섹터 (파티션 시작)", 82, b"FAT32   ", _bootsig),
    ("BitLocker 볼륨 (파티션 시작)", 3, b"-FVE-FS-", None),
    ("ext 슈퍼블록 (파티션 시작 = 여기 - 0x400)", 56, b"\x53\xef", _ext_superblock),
    ("LUKS 암호화 헤더", 0, b"LUKS\xba\xbe", None),
    ("APFS 컨테이너", 32, b"NXSB", None),
    ("JPEG 사진", 0, b"\xff\xd8\xff", lambda d, i: d[i + 3] in (0xE0, 0xE1, 0xDB, 0xEE)),
    ("PNG 이미지", 0, b"\x89PNG\r\n\x1a\n", None),
    ("PDF 문서", 0, b"%PDF-", None),
    ("ZIP / Office 문서", 0, b"PK\x03\x04", None),
    ("MP4 / MOV 동영상", 4, b"ftyp", None),
    ("SQLite DB", 0, b"SQLite format 3\x00", None),
    ("7z 압축", 0, b"7z\xbc\xaf\x27\x1c", None),
    ("RAR 압축", 0, b"Rar!\x1a\x07", None),
]
_SIG_BY_MAGIC: dict[bytes, list] = {}
for _sig in SIGNATURES:
    _SIG_BY_MAGIC.setdefault(_sig[2], []).append(_sig)
_SIG_RE = re.compile(b"|".join(re.escape(m) for m in sorted(_SIG_BY_MAGIC, key=len, reverse=True)))


def scan_signatures(src: ByteSource, start: int = 0, length: int | None = None, step: int = 512,
                    progress=None, should_stop=None, limit: int = 2000) -> list[tuple[int, str]]:
    """섹터 경계에서 시작하는 파티션 부트 섹터·파일 시그니처를 찾는다 (파일 시작은 대부분 섹터 경계)."""
    end = src.size if length is None else min(src.size, start + length)
    start -= start % step
    hits: list[tuple[int, str]] = []
    pos = start
    while pos < end:
        n = min(CHUNK, end - pos)
        data = bytes(src.read(pos, n + 512))
        for m in _SIG_RE.finditer(data):
            for name, rel, _magic, check in _SIG_BY_MAGIC[m.group()]:
                s = m.start() - rel
                if s < 0 or s >= n or (pos + s) % step:
                    continue
                try:
                    if check and not check(data, s):
                        continue
                except (IndexError, struct.error):
                    continue
                hits.append((pos + s, name))
                if len(hits) >= limit:
                    return hits
        pos += n
        if progress:
            progress(pos - start, end - start)
        if should_stop and should_stop():
            break
    return hits


def rescue_image(src: ByteSource, out_path: str, offset: int = 0, length: int | None = None,
                 block: int = 1 << 16, retries: int = 2, progress=None, should_stop=None) -> dict:
    """ddrescue 처럼 읽을 수 있는 곳부터 이미지로 복사한다.

    1단계: block 단위로 빠르게 복사, 오류 난 블록은 건너뛰고 '보류'에 기록
    2단계: 보류 구간을 섹터 단위로 재시도, 끝내 실패한 섹터는 0으로 두고 '불량'에 기록
    진행 상황은 <출력>.rescue.json 에 저장되어, 같은 명령을 다시 실행하면 이어서 한다."""
    sec = src.sector
    offset -= offset % sec
    end = src.size if length is None else min(src.size, offset + length)
    block = max(sec, block - block % sec)
    map_path = rescue_map_path(out_path)
    state = None
    if os.path.exists(map_path) and os.path.exists(out_path):
        try:
            with open(map_path, encoding="utf-8") as f:
                old = json.load(f)
            if (old.get("source"), old.get("offset"), old.get("length")) == (src.path, offset, end - offset):
                state = old
        except (OSError, ValueError):
            pass
    if state is None:
        state = {"version": 1, "source": src.path, "offset": offset, "length": end - offset, "sector": sec,
                 "pos": offset, "pending": [], "bad": [], "finished": False}
        with open(out_path, "wb") as f:
            f.truncate(end - offset)
    pending, bad = RangeSet(state["pending"]), RangeSet(state["bad"])
    last_save = [0.0]

    def save(force=False):
        if force or time.time() - last_save[0] > 2:
            state["pending"], state["bad"] = pending.ranges(), bad.ranges()
            with open(map_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(map_path + ".tmp", map_path)
            last_save[0] = time.time()

    def report(phase):
        if progress:
            progress({"phase": phase, "done": state["pos"] - offset, "total": end - offset,
                      "pending": pending.total(), "bad": bad.total()})

    stopped = False
    with open(out_path, "r+b") as out:
        pos = state["pos"]
        while pos < end and not stopped:
            n = min(block, end - pos)
            try:
                data = src.read_strict(pos, n)
                out.seek(pos - offset)
                out.write(data)
            except OSError:
                pending.add(pos, pos + n)
            pos += n
            state["pos"] = pos
            report(1)
            save()
            stopped = bool(should_stop and should_stop())
        for s, e in ([] if stopped else pending.ranges()):
            p = s
            while p < e and not stopped:
                n = min(sec, e - p)
                data = None
                for _ in range(retries + 1):
                    try:
                        data = src.read_strict(p, n)
                        break
                    except OSError:
                        data = None
                if data is None:
                    bad.add(p, p + n)
                else:
                    out.seek(p - offset)
                    out.write(data)
                pending.remove(p, p + n)
                p += n
                report(2)
                save()
                stopped = bool(should_stop and should_stop())
    state["finished"] = not stopped and state["pos"] >= end and not len(pending)
    save(force=True)
    for s, e in bad.ranges():
        src.bad.add(s, e)
    return {"map": map_path, "finished": state["finished"], "copied": state["pos"] - offset,
            "total": end - offset, "pending": pending.ranges(), "bad": bad.ranges(),
            "bad_bytes": bad.total() + pending.total()}


def format_rescue(res: dict) -> str:
    L = [("완료" if res["finished"] else "중단됨 — 같은 명령을 다시 실행하면 이어서 진행합니다"),
         f"처리: {human_size(res['copied'])} / {human_size(res['total'])}",
         f"읽지 못한 양: {human_size(res['bad_bytes'])} (불량 {len(res['bad'])}구간, 보류 {len(res['pending'])}구간)",
         f"진행 기록: {res['map']}"]
    for s, e in res["bad"][:30]:
        L.append(f"  불량 0x{s:X} ~ 0x{e - 1:X} ({human_size(e - s)})")
    if len(res["bad"]) > 30:
        L.append(f"  … 외 {len(res['bad']) - 30}구간")
    return "\n".join(L)


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
                           "Select-Object DeviceID,Name,Service,HardwareID,ConfigManagerErrorCode | "
                           "ConvertTo-Json -Compress")
    for d in usb:
        m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", d.get("DeviceID") or "", re.I)
        if not m:
            continue
        vid, pid = int(m.group(1), 16), int(m.group(2), 16)
        if vid == 0:  # "알 수 없는 USB 장치 (장치 설명자 요청 실패)" 등: 연결은 됐지만 자기소개(열거)에 실패
            items.append({"kind": "usb_fail", "path": d.get("DeviceID", ""), "name": d.get("Name") or "",
                          "error": _as_int(d.get("ConfigManagerErrorCode"))})
            continue
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
                      "bridge": bridge_label(vid, pid), "driver": service,
                      "error": _as_int(d.get("ConfigManagerErrorCode"))})
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
    kinds = {"usb": "USB 브릿지", "usb_fail": "USB 인식 실패", "nvme": "NVMe", "disk": "디스크"}
    L = []
    for it in items:
        head = f"[{kinds.get(it['kind'], it['kind'])}] "
        if it["kind"] == "usb_fail":
            L.append(head + it["name"])
            L.append("    → 케이스가 연결은 됐지만 PC 에 자기소개(열거)를 못 하고 있습니다.")
            L.append("      전원·케이블·포트 문제이거나 브릿지 펌웨어가 손상됐을 수 있습니다.")
            continue
        if it["kind"] == "usb":
            L.append(head + f"{it['vid']}:{it['pid']}  {it['name']}")
            if it.get("bridge"):
                L.append(f"    칩 추정   : {it['bridge']}")
            L.append(f"    REV(bcdDevice): {it['firmware']}  ← 대부분 브릿지 펌웨어 버전")
            L.append(f"    드라이버  : {it.get('driver', '')}"
                     + (f"   속도: {it['speed']}" if it.get("speed") else ""))
            if it.get("error"):
                L.append(f"    장치 오류 : 코드 {it['error']} (장치 관리자에 느낌표로 표시됨)")
            if it.get("path"):
                label = "장치 ID   " if it["path"].upper().startswith("USB\\") else "디스크    "
                L.append(f"    {label}: {it['path']}")
        else:
            L.append(head + f"{it['path']}  {it['name']}")
            L.append(f"    펌웨어: {it.get('firmware', '')}   크기: {it.get('size', '')}"
                     + (f"   연결: {it['transport']}" if it.get("transport") else ""))
    usb_storage = [it for it in items if it["kind"] == "usb"]
    usb_fail = [it for it in items if it["kind"] == "usb_fail"]
    usb_disks = [it for it in items if it["kind"] in ("disk", "nvme") and it.get("transport", "").lower() == "usb"]
    nvme_bridges = [it for it in usb_storage if it.get("bridge", "").startswith(("Realtek", "JMicron", "ASMedia"))]
    if usb_fail:
        L += ["",
              "※ PC 가 인식하지 못한 USB 장치가 있습니다. NVMe 케이스가 목록에 없다면 이것이 케이스일 가능성이 큽니다.",
              "  (USB 3 에서 실패하면 USB 2 로 다시 시도하므로 한 장치가 두 줄로 보이기도 합니다)",
              "  케이스 칩이 시작조차 못 하는 상태입니다. 확인 순서:",
              "   1) 케이스를 뽑고 다시 scan — 사라지는 항목이 케이스",
              "   2) 다른 케이블(케이스 동봉 케이블)·메인보드 뒷면 포트로 바꿔 보기 (허브·앞면 포트 X)",
              "   3) SSD 를 빼고 케이스만 꽂아 보기 — 이때 정상 인식되면 SSD 쪽 문제, 그래도 실패면 케이스 고장"]
    elif usb_storage and not usb_disks:
        L += ["",
              "※ USB 저장장치는 연결돼 있는데 USB 디스크가 하나도 보이지 않습니다.",
              "  케이스(브릿지)는 살아 있지만 안의 SSD/디스크를 찾지 못하는 상태입니다 (Windows: '미디어 없음').",
              "  SSD 장착 상태, 케이스 규격(NVMe/SATA) 불일치, 케이스 펌웨어, 또는 SSD 고장을 의심하세요."]
    if usb_storage and not nvme_bridges and not usb_fail:
        L += ["※ 알려진 USB-NVMe 브릿지(Realtek 0BDA / JMicron 152D / ASMedia 174C)가 없습니다.",
              "  NVMe 케이스를 뺐다 꽂아 보고, 목록에서 사라지는 항목이 케이스입니다."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 드라이브 상태 (SMART / NVMe health log)
# ---------------------------------------------------------------------------

NVME_CRITICAL_BITS = ["예비 블록이 임계값 아래로 떨어짐", "온도 임계값 초과", "NVM 신뢰성 저하 (미디어/내부 오류)",
                      "읽기 전용 모드로 전환됨", "휘발성 메모리 백업 장치 고장"]


def _run_tool(cmd: list[str], timeout: int = 60) -> str:
    flags = 0x08000000 if IS_WINDOWS else 0
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout


def _as_int(v):
    if isinstance(v, dict):  # 일부 nvme-cli 버전은 {"value": n} 형태
        v = v.get("value")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_smart(log: dict) -> dict:
    """smartctl / nvme-cli 의 서로 다른 키 이름을 하나로 맞춘다."""
    def g(*keys):
        for k in keys:
            if k in log:
                return _as_int(log[k])
        return None
    temp = g("temperature")
    if temp is not None and temp > 200:  # nvme-cli 는 켈빈
        temp -= 273
    return {"critical_warning": g("critical_warning"), "temperature_c": temp,
            "available_spare": g("available_spare", "avail_spare"),
            "spare_threshold": g("available_spare_threshold", "spare_thresh"),
            "percentage_used": g("percentage_used", "percent_used"),
            "data_units_read": g("data_units_read"), "data_units_written": g("data_units_written"),
            "power_on_hours": g("power_on_hours"), "unsafe_shutdowns": g("unsafe_shutdowns"),
            "media_errors": g("media_errors"), "error_log_entries": g("num_err_log_entries")}


def read_health(path: str) -> dict:
    """smartctl → nvme-cli → (Windows) 저장소 신뢰성 카운터 순서로 상태를 읽는다.

    USB 케이스 안의 NVMe 는 smartctl 의 sntrealtek / sntjmicron / sntasmedia 방식으로
    브릿지를 통과해서 읽는다 (smartmontools 7.2 이상 권장)."""
    path = normalize_device_path(path)
    tried = []
    smartctl = shutil.which("smartctl")
    if smartctl:
        m = re.search(r"PhysicalDrive(\d+)", path, re.IGNORECASE)
        target = f"/dev/pd{m.group(1)}" if IS_WINDOWS and m else path
        for dtype in (None, "sntrealtek", "sntjmicron", "sntasmedia"):
            out = _run_tool([smartctl, "-a", "-j", target] + (["-d", dtype] if dtype else []))
            try:
                j = json.loads(out)
            except ValueError:
                j = {}
            log = j.get("nvme_smart_health_information_log")
            label = "smartctl" + (f" -d {dtype}" if dtype else "")
            if log:
                return {"source": label, "model": j.get("model_name", ""),
                        "firmware": j.get("firmware_version", ""),
                        "passed": (j.get("smart_status") or {}).get("passed"), **normalize_smart(log)}
            tried.append(label)
    else:
        tried.append("smartctl 미설치")
    if not IS_WINDOWS and shutil.which("nvme"):
        out = _run_tool(["nvme", "smart-log", path, "-o", "json"])
        try:
            return {"source": "nvme-cli", "model": "", "firmware": "", "passed": None,
                    **normalize_smart(json.loads(out))}
        except ValueError:
            tried.append("nvme-cli")
    if IS_WINDOWS:
        m = re.search(r"PhysicalDrive(\d+)", path, re.IGNORECASE)
        if m:
            try:
                rows = _powershell_json(
                    f"Get-PhysicalDisk | Where-Object DeviceId -eq '{m.group(1)}' | Get-StorageReliabilityCounter | "
                    "Select-Object Temperature,Wear,ReadErrorsUncorrected,PowerOnHours | ConvertTo-Json -Compress")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                rows = []
            if rows and any(v is not None for v in rows[0].values()):
                r = rows[0]
                return {"source": "Windows 저장소 신뢰성 카운터", "model": "", "firmware": "", "passed": None,
                        "critical_warning": None, "temperature_c": _as_int(r.get("Temperature")),
                        "available_spare": None, "spare_threshold": None,
                        "percentage_used": _as_int(r.get("Wear")), "data_units_read": None,
                        "data_units_written": None, "power_on_hours": _as_int(r.get("PowerOnHours")),
                        "unsafe_shutdowns": None, "media_errors": _as_int(r.get("ReadErrorsUncorrected")),
                        "error_log_entries": None}
            tried.append("Windows 신뢰성 카운터")
    raise OSError("드라이브 상태를 읽지 못했습니다 (시도: " + ", ".join(tried) + ").\n"
                  "smartmontools 를 설치하면 USB 케이스 안의 NVMe 도 읽을 수 있습니다:\n"
                  "  Windows: https://www.smartmontools.org 설치 후 관리자 권한으로 실행\n"
                  "  Linux  : sudo apt install smartmontools")


def health_verdicts(h: dict) -> list[str]:
    out = []
    cw = h.get("critical_warning") or 0
    for bit, text in enumerate(NVME_CRITICAL_BITS):
        if cw & (1 << bit):
            out.append(f"⚠ 치명 경고: {text}")
    if cw & 0x08:
        out.append("⚠ 드라이브가 스스로 읽기 전용이 됨 — 수명 종료 상태. 지금 바로 데이터를 복사하세요.")
    if (h.get("media_errors") or 0) > 0:
        out.append(f"⚠ 미디어 오류 {h['media_errors']}건 — 읽을 수 없는 블록이 있습니다. "
                   "탐색보다 '복구 이미지'를 먼저 만드세요.")
    if (h.get("percentage_used") or 0) >= 100:
        out.append(f"⚠ 수명 사용률 {h['percentage_used']}% — 보증 수명을 넘었습니다.")
    spare, thresh = h.get("available_spare"), h.get("spare_threshold")
    if spare is not None and thresh is not None and spare <= thresh:
        out.append(f"⚠ 예비 블록 {spare}% (임계 {thresh}%) — 곧 쓰기가 막힐 수 있습니다.")
    if (h.get("temperature_c") or 0) >= 70:
        out.append(f"⚠ 온도 {h['temperature_c']}°C — 과열. 케이스 방열(써멀패드) 확인.")
    if h.get("passed") is False:
        out.append("⚠ SMART 종합 판정: 실패")
    if not out:
        out.append("SMART 상으로는 큰 이상 없음. 인식이 끊기거나 느리면 SSD 보다는 케이스(브릿지 펌웨어)·"
                   "케이블·USB 포트 전원 문제일 가능성이 큽니다. 다른 케이블/포트, 브릿지 펌웨어 업데이트를 먼저 시도하세요.")
    return out


def format_health(h: dict, path: str = "") -> str:
    def tb(units):
        return human_size(units * 512000) if units is not None else "-"

    def v(key, unit=""):
        val = h.get(key)
        return "-" if val is None else f"{val}{unit}"
    L = [f"=== 드라이브 상태 {path} ({h.get('source', '')}) ==="]
    if h.get("model") or h.get("firmware"):
        L.append(f"모델: {h.get('model', '')}   펌웨어: {h.get('firmware', '')}")
    L += [f"치명 경고 비트 : {v('critical_warning')}",
          f"온도           : {v('temperature_c', '°C')}",
          f"예비 블록      : {v('available_spare', '%')} (임계 {v('spare_threshold', '%')})",
          f"수명 사용률    : {v('percentage_used', '%')}",
          f"읽은 양 / 쓴 양: {tb(h.get('data_units_read'))} / {tb(h.get('data_units_written'))}",
          f"사용 시간      : {v('power_on_hours', '시간')}",
          f"비정상 종료    : {v('unsafe_shutdowns', '회')}",
          f"미디어 오류    : {v('media_errors', '건')}",
          f"오류 로그      : {v('error_log_entries', '건')}",
          "", "[판정]"]
    L += [f"  {x}" for x in health_verdicts(h)]
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


def _open_for_reading(args) -> ByteSource:
    src = ByteSource(args.path, size=parse_size(args.size) if getattr(args, "size", None) else None)
    if src.size == 0:
        src.close()
        raise OSError(errno.EIO, "용량이 0 으로 보입니다 (고장 디스크일 수 있음). --size 512G 처럼 크기를 직접 지정하세요")
    src.retries = getattr(args, "retries", 0) or 0
    return src


def cmd_dump(args) -> int:
    src = _open_for_reading(args)
    try:
        offset = parse_int(args.offset)
        data = src.read(offset, parse_int(args.length))
        bad = src.bad_offsets(offset, offset + len(data))
    finally:
        src.close()
    for line in hexdump_lines(bytes(data), offset, bad):
        print(line)
    if bad:
        print(f"※ ?? = 읽기 실패 ({len(bad)} 바이트)", file=sys.stderr)
    return 0


def cmd_layout(args) -> int:
    src = _open_for_reading(args)
    try:
        print(f"{src.path}  크기 {human_size(src.size)}  논리 섹터 {src.sector}B")
        print(format_layout(parse_layout(src)))
    finally:
        src.close()
    return 0


def cmd_sigscan(args) -> int:
    src = _open_for_reading(args)
    stop = threading.Event()
    old = signal.signal(signal.SIGINT, lambda *a: stop.set())
    try:
        def progress(done, total):
            print(f"\r스캔 {human_size(done)} / {human_size(total)}", end="", file=sys.stderr)
        hits = scan_signatures(src, parse_int(args.offset), parse_size(args.length) if args.length else None,
                               parse_int(args.step), progress, stop.is_set, args.max)
    finally:
        signal.signal(signal.SIGINT, old)
        src.close()
    print(file=sys.stderr)
    for off, name in hits:
        print(f"0x{off:012X}  {name}")
    print(f"{len(hits)}개 발견" + (" (Ctrl+C 로 중단됨)" if stop.is_set() else ""), file=sys.stderr)
    return 0


def cmd_rescue(args) -> int:
    src = _open_for_reading(args)
    stop = threading.Event()
    old = signal.signal(signal.SIGINT, lambda *a: stop.set())
    try:
        def progress(p):
            print(f"\r[{p['phase']}단계] {human_size(p['done'])} / {human_size(p['total'])}  "
                  f"보류 {human_size(p['pending'])}  불량 {human_size(p['bad'])}   ", end="", file=sys.stderr)
        res = rescue_image(src, args.out, parse_int(args.offset), parse_size(args.length) if args.length else None,
                           parse_size(args.block), args.retries, progress, stop.is_set)
    finally:
        signal.signal(signal.SIGINT, old)
        src.close()
    print(file=sys.stderr)
    print(format_rescue(res))
    return 0 if res["finished"] else 3


def cmd_health(args) -> int:
    h = read_health(args.path)
    print(json.dumps(h, ensure_ascii=False, indent=2) if args.json else format_health(h, args.path))
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
               "  nvme_hexedit image PhysicalDrive2 head.bin --length 0x100000   (Windows, 관리자)\n"
               "\n고장 디스크:\n"
               "  nvme_hexedit health /dev/sda                    SMART 상태와 판정\n"
               "  nvme_hexedit rescue /dev/sda disk.img           불량 섹터 건너뛰며 이미지 (먼저 할 것!)\n"
               "  nvme_hexedit layout disk.img                    파티션/파일시스템 구조, 손상 여부\n"
               "  nvme_hexedit sigscan disk.img --length 10G      파티션 시작·사진·문서 위치 찾기\n"
               "  nvme_hexedit gui disk.img                       이미지 탐색 (못 읽은 곳은 ??)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("gui", help="GUI 실행")
    g.add_argument("path", nargs="?")
    g.add_argument("--size", help="용량 인식이 안 되는 고장 디스크를 이 크기로 열기 (예: 512G)")

    s = sub.add_parser("info", help="펌웨어 분석 (칩·VID/PID·문자열·체크섬)")
    s.add_argument("path")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_info)

    size_help = "용량 인식이 안 되는 고장 디스크를 이 크기로 열기 (예: 512G)"
    s = sub.add_parser("dump", help="헥스 덤프 출력 (파일/디스크, 읽기 실패는 ??)")
    s.add_argument("path")
    s.add_argument("--offset", default="0")
    s.add_argument("--length", default="0x100")
    s.add_argument("--size", help=size_help)
    s.add_argument("--retries", type=int, default=0, help="실패한 섹터 재시도 횟수")
    s.set_defaults(func=cmd_dump)

    s = sub.add_parser("layout", help="[고장 디스크] 파티션 테이블(MBR/GPT)·파일시스템·손상 여부 표시")
    s.add_argument("path")
    s.add_argument("--size", help=size_help)
    s.set_defaults(func=cmd_layout)

    s = sub.add_parser("sigscan", help="[고장 디스크] 파티션 부트 섹터·사진·문서 시그니처 찾기")
    s.add_argument("path")
    s.add_argument("--offset", default="0")
    s.add_argument("--length", help="생략하면 끝까지 (예: 10G)")
    s.add_argument("--step", default="512", help="검사 간격 (기본 512)")
    s.add_argument("--max", type=int, default=2000, help="최대 결과 수")
    s.add_argument("--size", help=size_help)
    s.set_defaults(func=cmd_sigscan)

    s = sub.add_parser("rescue", help="[고장 디스크] 불량 섹터를 건너뛰며 복구 이미지 만들기 (이어하기 지원)")
    s.add_argument("path", help="원본 디스크 (예: /dev/sda, PhysicalDrive2)")
    s.add_argument("out", help="저장할 이미지 파일 (진행 기록은 <파일>.rescue.json)")
    s.add_argument("--offset", default="0")
    s.add_argument("--length", help="생략하면 끝까지")
    s.add_argument("--block", default="64K", help="1단계 읽기 단위 (기본 64K)")
    s.add_argument("--retries", type=int, default=2, help="2단계 섹터 재시도 횟수 (기본 2)")
    s.add_argument("--size", help=size_help)
    s.set_defaults(func=cmd_rescue)

    s = sub.add_parser("health", help="[고장 디스크] SMART/NVMe 상태와 판정 (smartctl 권장)")
    s.add_argument("path", help="예: /dev/nvme0n1, /dev/sda, PhysicalDrive2")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_health)

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
        size = getattr(args, "size", None)
        return run_gui(getattr(args, "path", None), parse_size(size) if size else None)
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

■ 고장 난 SSD 는 '고장 디스크' 메뉴 → '고장 디스크 대처 순서' 참고
  읽기 실패한 곳은 ?? (분홍색) 로 표시되고, Ctrl+B 로 다음 실패 위치로 이동합니다.
"""

BROKEN_HELP = """고장 난 NVMe(SSD) 탐색 · 복구 순서

1. 먼저 케이스/케이블을 의심하세요
   • 다른 USB 포트(뒷면 포트), 다른 케이블, 가능하면 다른 케이스나 메인보드 M.2 슬롯에 꽂아 보세요.
   • ORICO 케이스의 인식 끊김·용량 0·속도 저하는 SSD 가 아니라 브릿지 칩(펌웨어)·전원 문제인 경우가 많습니다.

2. 드라이브 상태 (SMART) 확인
   • 미디어 오류 > 0, 치명 경고, '읽기 전용 모드' 가 보이면 SSD 자체 고장입니다.
   • USB 케이스 안의 NVMe 는 smartmontools(smartctl) 가 있어야 읽을 수 있습니다.

3. 복구 이미지부터 만드세요 (가장 중요!)
   • 고장 디스크는 읽을 때마다 상태가 나빠질 수 있습니다. 한 번에 이미지로 떠 두고, 탐색은 이미지로 하세요.
   • 1단계에서 읽히는 곳을 빠르게 복사하고, 2단계에서 실패한 곳만 섹터 단위로 재시도합니다.
   • 중간에 멈춰도 같은 출력 파일로 다시 시작하면 이어서 합니다. 이미지 저장 공간은 SSD 용량만큼 필요합니다.

4. 이미지(또는 디스크)를 열고 구조 탐색
   • 파티션 테이블(MBR/GPT)·백업 GPT·파일시스템 종류와 손상 여부를 보여줍니다.
   • 파티션 테이블이 깨졌으면 '시그니처 스캔' 으로 NTFS/exFAT/FAT32 부트 섹터, ext 슈퍼블록,
     사진(JPEG/PNG)·문서(PDF/Office)·동영상 시작 위치를 찾을 수 있습니다.
   • 읽기 실패 구간은 ?? 로 표시됩니다 (이미지라면 .rescue.json 기록을 자동으로 읽습니다).

5. 실제 파일 복구는 이미지 파일에 전용 도구(TestDisk/PhotoRec, DMDE 등)를 쓰는 것이 안전합니다.
   이 프로그램은 '어디가 살아 있고 무엇이 남아 있는지' 를 직접 확인하는 용도입니다.

※ 고장 디스크에는 절대 쓰지 마세요 (기본이 읽기 전용입니다).
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
    def __init__(self, root, initial_path: str | None = None, initial_size: int | None = None):
        self.root = root
        self.src: ByteSource | None = None
        # 디스크는 백그라운드 스레드에서 읽는다 (불량 섹터에서 수십 초 걸려도 화면이 멈추지 않게)
        self._page_key: tuple[int, int] | None = None
        self._page_raw = b""
        self._loading = False
        self._load_result = None
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
            root.after(50, lambda: self.open_path(initial_path, size=initial_size))

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
        b = tk.Menu(m, tearoff=False)
        b.add_command(label="드라이브 상태 (SMART) 확인", command=self.cmd_health)
        b.add_command(label="복구 이미지 만들기 (불량 섹터 건너뛰기)…", command=lambda: RescueWindow(self))
        b.add_separator()
        b.add_command(label="구조 탐색 (파티션·파일시스템·시그니처)", command=self.cmd_layout)
        b.add_command(label="다음 읽기 실패 위치로", accelerator="Ctrl+B", command=self.goto_next_bad)
        b.add_separator()
        b.add_command(label="고장 디스크 대처 순서", command=lambda: self.show_text("고장 디스크", BROKEN_HELP))
        m.add_cascade(label="고장 디스크", menu=b)
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
        self.text.tag_configure("bad", background="#ffcdd2", foreground="#b71c1c")
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
        r.bind("<Control-b>", lambda e: self.goto_next_bad())
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

    def _ask_size(self, path: str, reason: str) -> int | None:
        t = simpledialog.askstring(
            "용량 직접 지정", f"{path}\n{reason}\n\n고장 난 디스크는 용량을 0 이나 엉뚱한 값으로 알려주기도 합니다.\n"
                         "SSD 실제 용량을 입력하면 그 크기로 엽니다 (예: 512G, 1T, 0x7470C06000)", parent=self.root)
        if not t:
            return None
        try:
            return parse_size(t)
        except ValueError:
            messagebox.showerror("용량", f"알 수 없는 크기: {t}")
            return None

    def open_path(self, path: str, allow_device_write: bool = False, size: int | None = None):
        if not self.confirm_discard():
            return
        try:
            src = ByteSource(path, allow_device_write, size)
        except PermissionError as e:
            messagebox.showerror("열기 실패", f"{path}\n{e}{access_hint(e)}")
            return
        except OSError as e:
            if size is None and is_device_path(normalize_device_path(path)):
                size = self._ask_size(path, f"용량을 읽지 못했습니다: {e}")
                if size:
                    self.open_path(path, allow_device_write, size)
                return
            messagebox.showerror("열기 실패", f"{path}\n{e}{access_hint(e)}")
            return
        if src.size == 0 and src.is_device:
            src.close()
            size = self._ask_size(path, "용량이 0 으로 보고됩니다.")
            if size:
                self.open_path(path, allow_device_write, size)
            return
        self.close_source(force=True)
        self.src = src
        self._page_key, self._page_raw = None, b""
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
        self._page_key = None  # 디스크 내용이 바뀌었으니 화면 캐시 무효화
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

    def _page_data(self, start: int, n: int):
        """현재 화면 데이터. 디스크는 백그라운드에서 읽고, 아직이면 None."""
        src = self.src
        if not src.is_device:
            return src.read(start, n)
        if self._page_key == (start, n):
            buf = bytearray(self._page_raw)
            for k in src.patched_in_range(start, start + len(buf)):
                buf[k - start] = src.patches[k]
            return buf
        if not self._loading:
            self._loading = True
            self._load_result = None
            key = (start, n)

            def work():
                try:
                    raw = src.read_raw(*key)
                except Exception:  # 읽는 중에 장치를 닫은 경우 등
                    raw = b""
                self._load_result = (src, key, raw)
            threading.Thread(target=work, daemon=True).start()
            self.root.after(20, self._poll_page_load)
        return None

    def _poll_page_load(self):
        if self._load_result is None:
            self.root.after(30, self._poll_page_load)
            return
        src, key, raw = self._load_result
        self._loading = False
        if src is self.src:
            self._page_key, self._page_raw = key, raw
            self.render()

    def render(self):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        self._view = (0, b"")
        if not self.src:
            t.insert("1.0", "\n  파일 → 파일 열기 (Ctrl+O) 로 펌웨어 .bin 을 열거나\n"
                            "  도구 → 장치 검색 으로 케이스의 브릿지 칩과 펌웨어 버전을 확인하세요.\n"
                            "  고장 난 SSD 라면 고장 디스크 → 고장 디스크 대처 순서 를 먼저 보세요.\n\n"
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
        want = max(0, min(self.rows * 16, self.src.size - start))
        data = self._page_data(start, want)
        total = max(self.total_rows(), 1)
        self.vsb.set(self.top_row / total, min(1.0, (self.top_row + self.rows) / total))
        if data is None:
            lines = [f"{start + r:0{w}X}  " + " ".join(["··"] * 16) + "   " + "·" * 16
                     for r in range(0, want, 16)]
            t.insert("1.0", "\n".join(lines))
            t.configure(state="disabled")
            self.status.set(f"{self.src.path}  |  0x{start:X} 읽는 중… (불량 섹터면 수십 초 걸릴 수 있음)")
            return
        self._view = (start, bytes(data))
        bad = self.src.bad_offsets(start, start + len(data))
        lines = []
        for r in range(0, len(data), 16):
            chunk = data[r:r + 16]
            offs = range(start + r, start + r + len(chunk))
            lines.append(f"{start + r:0{w}X}  "
                         + " ".join("??" if o in bad else f"{b:02X}" for o, b in zip(offs, chunk)).ljust(47)
                         + "  " + "".join("?" if o in bad else (chr(b) if 32 <= b < 127 else ".")
                                          for o, b in zip(offs, chunk)))
        t.insert("1.0", "\n".join(lines))
        for r in range(len(lines)):
            t.tag_add("offs", f"{r + 1}.0", f"{r + 1}.{w}")

        def mark(off, tag, hex_tag=None):
            rel = off - start
            if 0 <= rel < len(data):
                line, i = rel // 16 + 1, rel % 16
                t.tag_add(hex_tag or tag, f"{line}.{hx + 3 * i}", f"{line}.{hx + 3 * i + 2}")
                t.tag_add(tag, f"{line}.{ax + i}", f"{line}.{ax + i + 1}")

        for off in bad:
            mark(off, "bad")
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

        self._update_inspector(bad)
        mode = "편집 가능" if self.src.writable else "읽기 전용"
        kind = "디스크" if self.src.is_device else "파일"
        bad_info = (f"  |  읽기 실패 {len(self.src.bad)}구간 {human_size(self.src.bad.total())} (Ctrl+B)"
                    if len(self.src.bad) else "")
        self.status.set(f"{self.src.path}  |  {kind} {human_size(self.src.size)}  |  {mode}  |  "
                        f"커서 0x{self.cursor:X} ({self.cursor})  |  영역 {self.area.upper()}  |  "
                        f"변경 {len(self.src.patches)} 바이트{bad_info}")

    def _update_inspector(self, bad: set[int]):
        start, data = self._view
        rel = self.cursor - start
        b = data[rel:rel + 4] if 0 <= rel < len(data) else b""
        v = self.inspect_vars
        for var in v.values():
            var.set("-")
        v["오프셋"].set(f"0x{self.cursor:X} / {self.cursor}")
        if not b:
            return
        if self.cursor in bad:
            v["u8"].set("읽기 실패 섹터")
            return
        v["u8"].set(f"{b[0]} (0x{b[0]:02X})")
        v["s8"].set(str(struct.unpack("b", b[:1])[0]))
        if len(b) >= 2:
            v["u16 LE"].set(f"{int.from_bytes(b[:2], 'little')} (0x{int.from_bytes(b[:2], 'little'):04X})")
            v["u16 BE"].set(f"{int.from_bytes(b[:2], 'big')} (0x{int.from_bytes(b[:2], 'big'):04X})")
        if len(b) == 4:
            v["u32 LE"].set(f"0x{int.from_bytes(b, 'little'):08X}")
            v["u32 BE"].set(f"0x{int.from_bytes(b, 'big'):08X}")
        v["2진수"].set(f"{b[0]:08b}")
        v["문자"].set(repr(chr(b[0])) if 32 <= b[0] < 127 else "-")

    # --- 고장 디스크 ---
    def goto_next_bad(self):
        if not self.src:
            return
        nxt = self.src.bad.next_after(self.cursor)
        if nxt is None:
            first = self.src.bad.next_after(-1)
            if first is None:
                self.status.set("알려진 읽기 실패 위치가 없습니다 (아직 읽어 본 곳에서는)")
                return
            nxt = first
        self.move_to(nxt)

    def run_bg(self, func, done, title: str = "작업", failed=None):
        """func 를 백그라운드에서 실행하고 끝나면 done(결과) 를 화면 스레드에서 호출."""
        box: dict = {}

        def work():
            try:
                box["result"] = func()
            except Exception as e:
                box["error"] = e
        th = threading.Thread(target=work, daemon=True)
        th.start()

        def poll():
            if th.is_alive():
                self.root.after(80, poll)
            elif "error" in box:
                messagebox.showerror(title, f"{box['error']}{access_hint(box['error'])}")
                if failed:
                    failed()
            else:
                done(box["result"])
        poll()

    def cmd_health(self, path: str | None = None):
        if path is None:
            default = self.src.path if self.src and self.src.is_device else ""
            path = simpledialog.askstring(
                "드라이브 상태", "장치 경로 (예: /dev/sda, /dev/nvme0n1, \\\\.\\PhysicalDrive2)",
                initialvalue=default, parent=self.root)
        if not path:
            return
        self.status.set(f"{path} 상태 읽는 중…")
        self.run_bg(lambda: format_health(read_health(path), path),
                    lambda text: self.show_text("드라이브 상태", text), "드라이브 상태")

    def cmd_layout(self):
        if not self.src:
            messagebox.showinfo("구조 탐색", "먼저 디스크나 이미지를 여세요.")
            return
        LayoutWindow(self)


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


class LayoutWindow:
    """파티션 테이블·파일시스템·읽기 실패 구간·시그니처 스캔 결과를 트리로 보여주고, 더블클릭하면 이동."""

    KIND_TAGS = {"warn": "warn", "bad": "bad"}

    def __init__(self, app: HexEditorApp):
        self.app = app
        self.src = app.src
        self.win = tk.Toplevel(app.root)
        self.win.title(f"구조 탐색 — {self.src.path}")
        self.win.geometry("1050x560")
        btns = ttk.Frame(self.win, padding=6)
        btns.pack(side="top", fill="x")
        ttk.Button(btns, text="선택 위치로 이동", command=self.jump).pack(side="left")
        ttk.Button(btns, text="새로 고침", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(btns, text="시그니처 스캔…", command=self.start_scan).pack(side="left", padx=(12, 0))
        ttk.Button(btns, text="스캔 중지", command=lambda: self.stop.set()).pack(side="left", padx=4)
        self.info = tk.StringVar()
        ttk.Label(btns, textvariable=self.info).pack(side="left", padx=12)
        frame = ttk.Frame(self.win)
        frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree = ttk.Treeview(frame, columns=("offset", "size", "detail"), show="tree headings")
        self.tree.heading("#0", text="항목")
        self.tree.column("#0", width=330)
        for col, label, width in (("offset", "오프셋", 150), ("size", "크기", 100), ("detail", "설명", 450)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=(col == "detail"))
        self.tree.tag_configure("warn", foreground="#e65100")
        self.tree.tag_configure("bad", foreground="#b71c1c")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.jump())
        self.offsets: dict[str, int] = {}
        self.stop = threading.Event()
        self.scanning = False
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def close(self):
        self.stop.set()
        self.win.destroy()

    def _insert(self, parent, label, off, length=0, detail="", tag=""):
        iid = self.tree.insert(parent, "end", text=label, open=True, tags=(tag,) if tag else (),
                               values=(f"0x{off:X}", human_size(length) if length else "", detail))
        self.offsets[iid] = off
        return iid

    def refresh(self):
        self.info.set("구조 읽는 중… (불량 섹터가 있으면 오래 걸릴 수 있음)")
        src = self.src
        self.app.run_bg(lambda: parse_layout(src), self._show, "구조 탐색")

    def _show(self, items):
        if not self.alive():
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.offsets.clear()
        bad_root = None
        for it in items:
            if it["kind"] == "bad":
                if bad_root is None:
                    bad_root = self._insert("", "읽기 실패 구간", it["offset"], tag="bad")
                self._insert(bad_root, it["label"], it["offset"], it["length"], it["detail"], "bad")
            else:
                self._insert("", it["label"], it["offset"], it["length"], it["detail"],
                             self.KIND_TAGS.get(it["kind"], ""))
        if bad_root is not None:
            n_bad = len(self.tree.get_children(bad_root))
            self.tree.item(bad_root, text=f"읽기 실패 구간 ({n_bad}개" + (", 앞 200개만" if n_bad >= 200 else "") + ")")
        warns = sum(1 for it in items if it["kind"] == "warn")
        self.info.set(f"{len(items)}개 항목" + (f", 경고 {warns}개" if warns else "")
                      + " — 더블클릭하면 그 위치로 이동")

    def jump(self):
        sel = self.tree.selection()
        if sel and sel[0] in self.offsets:
            if self.app.src is not self.src:
                messagebox.showinfo("이동", "편집기에서 다른 파일이 열려 있습니다.", parent=self.win)
                return
            self.app.move_to(self.offsets[sel[0]])

    def start_scan(self):
        if self.scanning:
            return
        default_len = "1G" if self.src.size > (1 << 30) else ""
        rng = simpledialog.askstring(
            "시그니처 스캔", "시작 오프셋, 길이 (길이를 비우면 끝까지)\n"
                          "섹터 경계마다 파티션 부트 섹터·사진·문서 시작을 찾습니다. 1GB 에 수십 초 정도 걸립니다.",
            initialvalue=f"0x{self.app.cursor - self.app.cursor % 512:X}, {default_len}", parent=self.win)
        if not rng:
            return
        try:
            off_s, _, len_s = rng.partition(",")
            start = parse_int(off_s)
            length = parse_size(len_s) if len_s.strip() else None
        except ValueError:
            messagebox.showerror("시그니처 스캔", "형식: 오프셋, 길이 (예: 0, 10G)", parent=self.win)
            return
        self.stop.clear()
        self.scanning = True
        root = self._insert("", f"시그니처 스캔 (0x{start:X} 부터)", start)
        prog = {"done": 0, "total": 1}

        def progress(done, total):
            prog.update(done=done, total=total)

        def tick():
            if self.scanning and self.alive():
                self.info.set(f"스캔 중 {human_size(prog['done'])} / {human_size(prog['total'])}")
                self.win.after(250, tick)

        def done(hits):
            self.scanning = False
            if not self.alive():
                return
            for off, name in hits:
                self._insert(root, name, off)
            self.tree.item(root, text=f"시그니처 스캔 (0x{start:X} 부터) — {len(hits)}개"
                                      + (" (중지됨)" if self.stop.is_set() else ""))
            self.info.set(f"스캔 완료: {len(hits)}개 발견" + (" (중지됨)" if self.stop.is_set() else ""))

        src = self.src
        self.app.run_bg(lambda: scan_signatures(src, start, length, 512, progress, self.stop.is_set), done,
                        "시그니처 스캔", failed=lambda: setattr(self, "scanning", False))
        tick()


class RescueWindow:
    """불량 섹터를 건너뛰며 디스크를 이미지 파일로 복사 (ddrescue 방식, 이어하기 지원)."""

    def __init__(self, app: HexEditorApp):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("복구 이미지 만들기")
        self.win.resizable(True, False)
        frm = ttk.Frame(self.win, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        default_src = app.src.path if app.src and app.src.is_device else ""
        self.vars = {}
        rows = (("src", "원본 디스크", default_src), ("out", "저장할 이미지", ""), ("offset", "시작 오프셋", "0"),
                ("length", "길이 (비우면 끝까지)", ""), ("size", "용량 직접 지정 (인식 안 될 때)", ""),
                ("retries", "실패 섹터 재시도 횟수", "2"))
        for r, (key, label, value) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=value)
            ttk.Entry(frm, textvariable=var, width=60).grid(row=r, column=1, sticky="we", padx=6)
            self.vars[key] = var
        ttk.Button(frm, text="찾아보기…", command=self.browse).grid(row=1, column=2)
        ttk.Label(frm, foreground="#666",
                  text="예: /dev/sda, /dev/nvme0n1, \\\\.\\PhysicalDrive2   (장치 검색 창에서 경로 확인)\n"
                       "이미지 파일에는 SSD 용량만큼의 빈 공간이 필요합니다. 같은 이미지로 다시 시작하면 이어서 진행합니다."
                  ).grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(4, 8))
        self.bar = ttk.Progressbar(frm, maximum=1000)
        self.bar.grid(row=len(rows) + 1, column=0, columnspan=3, sticky="we")
        self.status = tk.StringVar(value="대기 중")
        ttk.Label(frm, textvariable=self.status, justify="left").grid(row=len(rows) + 2, column=0, columnspan=3,
                                                                     sticky="w", pady=6)
        btns = ttk.Frame(frm)
        btns.grid(row=len(rows) + 3, column=0, columnspan=3, sticky="e")
        self.start_btn = ttk.Button(btns, text="시작 / 이어하기", command=self.start)
        self.start_btn.pack(side="left")
        ttk.Button(btns, text="중지", command=lambda: self.stop.set()).pack(side="left", padx=4)
        self.open_btn = ttk.Button(btns, text="이미지 열기", command=self.open_image, state="disabled")
        self.open_btn.pack(side="left")
        self.stop = threading.Event()
        self.running = False
        self.prog: dict = {}
        self.out = ""
        self.win.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        if self.running:
            if not messagebox.askyesno("복구 이미지", "진행 중입니다. 중지하고 닫을까요?\n(나중에 이어서 할 수 있습니다)",
                                       parent=self.win):
                return
            self.stop.set()
        self.win.destroy()

    def browse(self):
        out = filedialog.asksaveasfilename(parent=self.win, title="복구 이미지 저장 위치", defaultextension=".img",
                                           filetypes=[("디스크 이미지", "*.img"), ("모든 파일", "*.*")],
                                           confirmoverwrite=False)
        if out:
            self.vars["out"].set(out)

    def start(self):
        if self.running:
            return
        path = normalize_device_path(self.vars["src"].get().strip())
        out = self.vars["out"].get().strip()
        if not path or not out:
            messagebox.showerror("복구 이미지", "원본과 저장할 이미지를 입력하세요.", parent=self.win)
            return
        if os.path.abspath(out) == os.path.abspath(path):
            messagebox.showerror("복구 이미지", "원본과 같은 곳에 저장할 수 없습니다.", parent=self.win)
            return
        try:
            size = parse_size(self.vars["size"].get()) if self.vars["size"].get().strip() else None
            offset = parse_int(self.vars["offset"].get() or "0")
            length = parse_size(self.vars["length"].get()) if self.vars["length"].get().strip() else None
            retries = int(self.vars["retries"].get() or "2")
            src = ByteSource(path, size=size)
        except (ValueError, OSError) as e:
            messagebox.showerror("복구 이미지", f"{e}{access_hint(e) if isinstance(e, OSError) else ''}",
                                 parent=self.win)
            return
        if src.size == 0:
            src.close()
            messagebox.showerror("복구 이미지", "용량이 0 으로 보입니다. '용량 직접 지정' 에 SSD 용량을 입력하세요.",
                                 parent=self.win)
            return
        resume = os.path.exists(rescue_map_path(out)) and os.path.exists(out)
        self.out, self.running, self.prog = out, True, {}
        self.stop.clear()
        self.start_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.status.set("이어서 진행합니다…" if resume else "시작합니다…")

        def progress(p):
            self.prog = p

        def work():
            try:
                return rescue_image(src, out, offset, length, retries=retries, progress=progress,
                                    should_stop=self.stop.is_set)
            finally:
                src.close()

        def failed():
            self.running = False
            self.start_btn.configure(state="normal")

        self.app.run_bg(work, self.done, "복구 이미지", failed)
        self.tick()

    def tick(self):
        if not self.running:
            return
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        p = self.prog
        if p:
            self.bar["value"] = 1000 * p["done"] / max(p["total"], 1)
            phase = ("1단계: 읽히는 곳 빠르게 복사" if p["phase"] == 1 else
                     f"2단계: 실패 구간 섹터 단위 재시도 (남은 {human_size(p['pending'])})")
            self.status.set(f"{phase}\n{human_size(p['done'])} / {human_size(p['total'])}   "
                            f"건너뜀 {human_size(p['pending'])}   불량 확정 {human_size(p['bad'])}")
        self.win.after(300, self.tick)

    def done(self, res):
        self.running = False
        try:
            self.status.set(format_rescue(res))
            self.bar["value"] = 1000 * res["copied"] / max(res["total"], 1)
            self.start_btn.configure(state="normal")
            self.open_btn.configure(state="normal")
        except tk.TclError:
            pass

    def open_image(self):
        if self.out:
            self.app.open_path(self.out)


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
        ttk.Button(btns, text="SMART 상태", command=self.health_selected).pack(side="left")
        ttk.Button(btns, text="텍스트로 보기", command=self.as_text).pack(side="left", padx=4)
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
            kinds = {"usb": "USB 브릿지", "usb_fail": "USB 인식 실패", "nvme": "NVMe", "disk": "디스크"}
            for i, it in enumerate(self.items):
                vidpid = f"{it['vid']}:{it['pid']}" if it.get("vid") else ""
                self.tree.insert("", "end", iid=str(i), values=(
                    kinds.get(it["kind"], it["kind"]), it.get("path", ""), it.get("name", ""), vidpid,
                    it.get("firmware", ""), it.get("bridge", "")))
            self.info.set(f"{len(self.items)}개 발견" + ("" if self.items else
                          " — USB 케이스를 연결했는지 확인하세요"))
        poll()

    def _selected_disk(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        it = self.items[int(sel[0])]
        path = it.get("path", "").split(",")[0].strip()
        if it["kind"] in ("usb", "usb_fail") and not path.startswith(("/dev/", "\\\\.\\")):
            messagebox.showinfo("장치", "USB 브릿지 항목은 디스크 경로가 아닙니다.\n"
                                        "같은 크기의 '디스크' 항목을 선택하세요.", parent=self.win)
            return None
        return path or None

    def open_selected(self):
        path = self._selected_disk()
        if path:
            self.app.cmd_open_device(path)

    def health_selected(self):
        path = self._selected_disk()
        if path:
            self.app.cmd_health(path)

    def as_text(self):
        self.app.show_text("장치 검색 결과", format_scan(self.items))


def run_gui(initial_path: str | None = None, initial_size: int | None = None) -> int:
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
    HexEditorApp(root, initial_path, initial_size)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
