import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import nvme_hexedit as nh  # noqa: E402


def usb_string(text: str) -> bytes:
    return bytes([2 + 2 * len(text), 3]) + text.encode("utf-16-le")


def fake_firmware(size: int = 0x4000) -> bytes:
    """실제 펌웨어가 아닌, 구조만 흉내 낸 테스트용 이미지 (끝 4바이트 = CRC32 LE)."""
    body = bytearray(b"\xFF" * (size - 4))
    body[0:16] = bytes(range(16))
    dev = bytes([0x12, 0x01, 0x10, 0x03, 0, 0, 0, 9]) + struct.pack("<HHH", 0x0BDA, 0x9210, 0x2001) + \
        bytes([1, 2, 3, 1])
    body[0x200:0x200 + 18] = dev
    body[0x300:0x300 + 12] = usb_string("ORICO")
    body[0x320:0x320 + 22] = usb_string("USB NVMe 1")
    body[0x400:0x400 + 20] = b"RTL9210B FW v1.23.45"
    return bytes(body) + struct.pack("<I", zlib.crc32(bytes(body)) & 0xFFFFFFFF)


class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path


class AnalysisTests(unittest.TestCase):
    def test_finds_device_descriptor(self):
        descs = nh.find_device_descriptors(fake_firmware())
        self.assertEqual(len(descs), 1)
        d = descs[0]
        self.assertEqual((d.offset, d.vid, d.pid, d.bcd_device), (0x200, 0x0BDA, 0x9210, 0x2001))
        self.assertIn("RTL9210", d.describe())

    def test_finds_string_descriptors(self):
        texts = {s.offset: s.text for s in nh.find_string_descriptors(fake_firmware())}
        self.assertEqual(texts[0x300], "ORICO")
        self.assertEqual(texts[0x320], "USB NVMe 1")

    def test_chip_and_checksum_detection(self):
        rep = nh.analyze_firmware(fake_firmware())
        self.assertTrue(any("RTL9210" in h for h in rep["chip_hints"]))
        self.assertEqual(rep["checksum_schemes"], ["crc32le-tail"])
        self.assertIn("crc32le-tail", nh.format_report(rep))

    def test_no_checksum_on_uniform_data(self):
        self.assertEqual(nh.detect_checksum_schemes(b"\x00" * 64), [])

    def test_each_checksum_scheme_roundtrips(self):
        base = bytes(range(200))
        for scheme in nh.CHECKSUM_SCHEMES:
            data = bytearray(base)
            off, fix = nh.checksum_fix(bytes(data), scheme)
            data[off:off + len(fix)] = fix
            self.assertTrue(nh.checksum_matches(bytes(data), scheme), scheme)

    def test_usb_string_patch_limits(self):
        s = nh.find_string_descriptors(fake_firmware())[0]
        off, data = nh.usb_string_patch(s, "MYBOX")
        self.assertEqual(off, 0x300)
        self.assertEqual(data, usb_string("MYBOX"))
        off, data = nh.usb_string_patch(s, "AB")
        self.assertEqual(data, usb_string("AB") + b"\x00" * 6)
        with self.assertRaises(ValueError):
            nh.usb_string_patch(s, "TOO-LONG")

    def test_diff_ranges(self):
        a = bytes(10000)
        b = bytearray(a)
        b[5] = 1
        b[9] = 1
        b[9000] = 7
        self.assertEqual(nh.diff_ranges(a, bytes(b)), [(5, 10), (9000, 9001)])

    def test_parse_helpers(self):
        self.assertEqual(nh.parse_hex_bytes("de ad:BE-ef"), b"\xde\xad\xbe\xef")
        self.assertEqual(nh.parse_int("1F0h"), 0x1F0)
        self.assertEqual(nh.parse_int("0x10"), 16)
        with self.assertRaises(ValueError):
            nh.parse_hex_bytes("ABC")


class ByteSourceTests(TmpDirTest):
    def test_patch_read_save_backup(self):
        path = self.write("fw.bin", bytes(range(256)))
        src = nh.ByteSource(path)
        src.write_bytes(0x10, b"\xAA\xBB")
        src.write_bytes(0x11, b"\x11")  # 원래 값으로 되돌리면 patch 에서 빠져야 함
        self.assertEqual(src.patches, {0x10: 0xAA})
        self.assertEqual(src.read(0x0F, 3), bytearray(b"\x0F\xAA\x11"))
        bak = src.save()
        src.close()
        with open(path, "rb") as f:
            self.assertEqual(f.read()[0x10], 0xAA)
        with open(bak, "rb") as f:
            self.assertEqual(f.read(), bytes(range(256)))

    def test_find_across_chunk_boundary(self):
        old = nh.CHUNK
        nh.CHUNK = 8
        try:
            path = self.write("f.bin", b"\x00" * 6 + b"NEEDLE" + b"\x00" * 20)
            src = nh.ByteSource(path)
            self.assertEqual(src.find(b"NEEDLE"), 6)
            self.assertEqual(src.find(b"NEEDLE", 7), -1)
            src.close()
        finally:
            nh.CHUNK = old

    def test_device_style_aligned_save(self):
        size = 4096 * 3 + 512  # 섹터(512) 배수지만 4096 배수는 아님
        data = os.urandom(size)
        path = self.write("disk.img", data)
        src = nh.ByteSource(path)
        src.is_device, src.align = True, 4096
        self.assertEqual(src.read_raw(4095, 3), data[4095:4098])
        self.assertEqual(src.read_raw(size - 2, 10), data[-2:])
        src.write_bytes(10, b"\x01\x02")
        src.write_bytes(4096 * 3 + 100, b"\xEE")
        home = os.environ.get("HOME")
        os.environ["HOME"] = self.dir
        try:
            backup_dir = src.save()
        finally:
            if home is not None:
                os.environ["HOME"] = home
        src.close()
        with open(path, "rb") as f:
            out = f.read()
        expected = bytearray(data)
        expected[10:12] = b"\x01\x02"
        expected[4096 * 3 + 100] = 0xEE
        self.assertEqual(out, bytes(expected))
        names = sorted(os.listdir(backup_dir))
        self.assertIn("0x000000000000.bin", names)
        self.assertIn("0x000000003000.bin", names)
        with open(os.path.join(backup_dir, "0x000000003000.bin"), "rb") as f:
            self.assertEqual(f.read(), data[4096 * 3:])


class CliTests(TmpDirTest):
    def run_cli(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = nh.main(list(argv))
        return code, buf.getvalue()

    def test_info_and_dump(self):
        path = self.write("fw.bin", fake_firmware())
        code, out = self.run_cli("info", path)
        self.assertEqual(code, 0)
        self.assertIn("0BDA:9210", out)
        code, out = self.run_cli("dump", path, "--offset", "0x200", "--length", "16")
        self.assertTrue(out.startswith("00000200  12 01 10 03"))

    def test_patch_usb_id_with_checksum(self):
        path = self.write("fw.bin", fake_firmware())
        out_path = os.path.join(self.dir, "new.bin")
        code, out = self.run_cli("patch", path, "--usb-id", "1234:5678", "--usb-string", "0x300=MYBOX",
                                 "--fix-checksum", "auto", "-o", out_path)
        self.assertEqual(code, 0, out)
        with open(out_path, "rb") as f:
            new = f.read()
        d = nh.find_device_descriptors(new)[0]
        self.assertEqual((d.vid, d.pid, d.bcd_device), (0x1234, 0x5678, 0x2001))
        self.assertTrue(nh.checksum_matches(new, "crc32le-tail"))
        self.assertIn("MYBOX", [s.text for s in nh.find_string_descriptors(new)])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), fake_firmware())  # 원본은 그대로

    def test_patch_in_place_set_and_diff(self):
        orig = fake_firmware()
        path = self.write("fw.bin", orig)
        code, _ = self.run_cli("patch", path, "--set", "0x10=DEADBEEF", "--in-place")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(path + ".bak"))
        code, out = self.run_cli("diff", path + ".bak", path)
        self.assertEqual(code, 1)
        self.assertIn("0x10 ~ 0x13", out)

    def test_image_export(self):
        path = self.write("fw.bin", fake_firmware())
        out_path = os.path.join(self.dir, "part.bin")
        code, _ = self.run_cli("image", path, out_path, "--offset", "0x200", "--length", "18")
        self.assertEqual(code, 0)
        with open(out_path, "rb") as f:
            self.assertEqual(f.read(), fake_firmware()[0x200:0x212])

    def test_scan_runs(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux sysfs 전용")
        code, _ = self.run_cli("scan")
        self.assertEqual(code, 0)


class WindowsScanTests(unittest.TestCase):
    """사용자가 보고한 Windows 결과(Initio 13FD:0840 + 내장 HDD)를 재현."""

    def fake_ps(self, cmd):
        if "Win32_PnPEntity" in cmd:
            return [{"DeviceID": "USB\\VID_13FD&PID_0840\\000000000000000000000000", "Name": "USB 대용량 저장 장치",
                     "Service": "USBSTOR", "HardwareID": ["USB\\VID_13FD&PID_0840&REV_0114"],
                     "ConfigManagerErrorCode": 0},
                    {"DeviceID": "USB\\VID_0000&PID_0002\\5&1A2B3C&0&3",
                     "Name": "알 수 없는 USB 장치(장치 설명자 요청 실패)", "Service": None, "HardwareID": None,
                     "ConfigManagerErrorCode": 43},
                    {"DeviceID": "USB\\VID_046D&PID_C52B\\5&2", "Name": "USB 수신기", "Service": "usbccgp",
                     "HardwareID": [], "ConfigManagerErrorCode": 0}]
        return [{"DeviceId": "0", "FriendlyName": "WDC WD20EZBX-00AYRA0", "BusType": 11,
                 "FirmwareVersion": "01.01A01", "Size": 2000398934016}]

    def test_scan_windows_diagnostics(self):
        with mock.patch.object(nh, "_powershell_json", self.fake_ps):
            items = nh.scan_windows()
        self.assertEqual([it["kind"] for it in items], ["usb", "usb_fail", "disk"])
        text = nh.format_scan(items)
        self.assertIn("Initio 칩 — USB↔SATA 브릿지 (NVMe 케이스 칩 아님)", text)
        self.assertIn("장치 ID   : USB\\VID_13FD", text)
        self.assertIn("[USB 인식 실패] 알 수 없는 USB 장치(장치 설명자 요청 실패)", text)
        self.assertIn("PC 가 인식하지 못한 USB 장치가 있습니다", text)
        self.assertIn("SSD 를 빼고 케이스만 꽂아 보기", text)
        self.assertNotIn("케이스(브릿지)는 살아 있지만", text)  # 인식 실패가 있으면 오해를 부르는 안내는 생략
        self.assertIn("연결: SATA", text)

    def test_scan_hints_without_enumeration_failure(self):
        def ps(cmd):
            rows = self.fake_ps(cmd)
            return [r for r in rows if "VID_0000" not in r.get("DeviceID", "")]
        with mock.patch.object(nh, "_powershell_json", ps):
            text = nh.format_scan(nh.scan_windows())
        self.assertIn("USB 디스크가 하나도 보이지 않습니다", text)
        self.assertIn("알려진 USB-NVMe 브릿지", text)
        self.assertNotIn("인식하지 못한", text)


if __name__ == "__main__":
    unittest.main()
