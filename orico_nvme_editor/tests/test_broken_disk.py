"""고장 디스크 탐색 기능 테스트 (불량 섹터는 특정 구간 읽기에서 EIO 를 내도록 흉내 낸다)."""
import contextlib
import errno
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import uuid
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import nvme_hexedit as nh  # noqa: E402

BASIC_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"


def failing_pread(bad_ranges, calls=None):
    """bad_ranges 와 겹치는 읽기는 EIO 를 내는 ByteSource._pread 대체."""
    real = nh.ByteSource._pread

    def pread(self, start, n):
        if calls is not None:
            calls.append((start, n))
        for s, e in bad_ranges:
            if start < e and s < start + n:
                raise OSError(errno.EIO, "simulated bad sector")
        return real(self, start, n)
    return pread


def ntfs_boot() -> bytes:
    b = bytearray(512)
    b[3:11] = b"NTFS    "
    b[510:512] = b"\x55\xaa"
    return bytes(b)


def make_gpt_disk(size: int = 4 << 20, ss: int = 512) -> bytearray:
    """보호 MBR + 주/백업 GPT + 파티션 1개(NTFS) 를 가진 디스크 이미지."""
    disk = bytearray(size)
    last = size // ss - 1
    disk[446 + 4] = 0xEE
    struct.pack_into("<II", disk, 446 + 8, 1, last)
    disk[510:512] = b"\x55\xaa"
    entries = bytearray(128 * 128)
    entries[0:16] = uuid.UUID(BASIC_DATA).bytes_le
    entries[16:32] = uuid.uuid4().bytes_le
    struct.pack_into("<QQ", entries, 32, 2048, 4095)
    name = "Data".encode("utf-16-le")
    entries[56:56 + len(name)] = name

    def header(my_lba, alt_lba, ent_lba):
        h = bytearray(92)
        h[0:8] = b"EFI PART"
        struct.pack_into("<III", h, 8, 0x00010000, 92, 0)
        struct.pack_into("<QQQQ", h, 24, my_lba, alt_lba, 34, last - 33)
        h[56:72] = uuid.uuid4().bytes_le
        struct.pack_into("<QIII", h, 72, ent_lba, 128, 128, zlib.crc32(entries) & 0xFFFFFFFF)
        struct.pack_into("<I", h, 16, zlib.crc32(h) & 0xFFFFFFFF)
        return h

    disk[ss:ss + 92] = header(1, last, 2)
    disk[2 * ss:2 * ss + len(entries)] = entries
    disk[(last - 32) * ss:(last - 32) * ss + len(entries)] = entries
    disk[last * ss:last * ss + 92] = header(last, 1, last - 32)
    disk[2048 * ss:2048 * ss + 512] = ntfs_boot()
    return disk


class TmpDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = nh.main(list(argv))
        return code, out.getvalue()


class RangeSetTests(unittest.TestCase):
    def test_merge_overlap_remove(self):
        r = nh.RangeSet([(10, 20), (30, 40)])
        r.add(20, 25)            # 붙은 구간은 합쳐짐
        r.add(35, 50)
        self.assertEqual(r.ranges(), [(10, 25), (30, 50)])
        r.add(0, 100)
        self.assertEqual(r.ranges(), [(0, 100)])
        r.remove(40, 60)
        self.assertEqual(r.ranges(), [(0, 40), (60, 100)])
        self.assertEqual(r.overlaps(35, 65), [(35, 40), (60, 65)])
        self.assertEqual(r.total(), 80)
        self.assertEqual(r.next_after(5), 60)
        self.assertIsNone(r.next_after(60))

    def test_parse_size(self):
        self.assertEqual(nh.parse_size("512G"), 512 << 30)
        self.assertEqual(nh.parse_size("1.5K"), 1536)
        self.assertEqual(nh.parse_size("1TiB"), 1 << 40)
        self.assertEqual(nh.parse_size("0x200"), 512)


class TolerantReadTests(TmpDir):
    def test_bad_sector_is_zero_filled_recorded_and_not_reread(self):
        data = bytes(range(256)) * 32  # 8 KiB
        src = nh.ByteSource(self.write("d.img", data))
        calls = []
        with mock.patch.object(nh.ByteSource, "_pread", failing_pread([(1024, 1536)], calls)):
            got = src.read_raw(0, len(data))
            self.assertEqual(got[:1024], data[:1024])
            self.assertEqual(got[1024:1536], bytes(512))
            self.assertEqual(got[1536:], data[1536:])
            self.assertEqual(src.bad.ranges(), [(1024, 1536)])
            calls.clear()
            src.read_raw(1000, 600)
            self.assertFalse(any(s < 1536 and 1024 < s + n for s, n in calls), calls)
        self.assertEqual(src.bad_offsets(1530, 1540), set(range(1530, 1536)))
        src.close()

    def test_device_mode_alignment_and_short_read(self):
        data = os.urandom(4096 * 4)
        src = nh.ByteSource(self.write("dev.img", data), size=4096 * 5)  # 실제보다 크게 지정 → 끝은 짧은 읽기
        src.is_device, src.align, src.sector = True, 4096, 512
        with mock.patch.object(nh.ByteSource, "_pread", failing_pread([(5000, 5100)])):
            got = src.read_raw(4500, 1000)
        self.assertEqual(got[:4608 - 4500], data[4500:4608])
        self.assertEqual(got[4608 - 4500:5120 - 4500], bytes(512))
        self.assertEqual(got[5120 - 4500:], data[5120:5500])
        tail = src.read_raw(4096 * 4 - 16, 64)
        self.assertEqual(tail[:16], data[-16:])
        self.assertEqual(src.bad.ranges(), [(4608, 5120), (4096 * 4, 4096 * 5)])
        src.close()

    def test_device_save_refuses_blocks_with_bad_sectors(self):
        src = nh.ByteSource(self.write("dev.img", bytes(8192)))
        src.is_device, src.align, src.writable = True, 4096, True
        src.bad.add(512, 1024)
        src.write_bytes(10, b"\x01")
        with self.assertRaises(OSError):
            src.save(backup=False)
        src.close()


class RescueTests(TmpDir):
    def test_rescue_skips_bad_and_resumes(self):
        data = os.urandom(1 << 20)
        path = self.write("disk.raw", data)
        out = os.path.join(self.dir, "rescue.img")
        bad = [(70000, 70100), (500000, 501000)]
        stop_after = {"n": 3}

        def should_stop():
            stop_after["n"] -= 1
            return stop_after["n"] <= 0

        with mock.patch.object(nh.ByteSource, "_pread", failing_pread(bad)):
            src = nh.ByteSource(path)
            first = nh.rescue_image(src, out, block=1 << 16, retries=1, should_stop=should_stop)
            src.close()
            self.assertFalse(first["finished"])
            src = nh.ByteSource(path)
            res = nh.rescue_image(src, out, block=1 << 16, retries=1)
            src.close()
        self.assertTrue(res["finished"])
        # 섹터 단위로 확장된 불량 구간
        self.assertEqual(res["bad"], [(69632, 70144), (499712, 501248)])
        with open(out, "rb") as f:
            img = f.read()
        expected = bytearray(data)
        for s, e in res["bad"]:
            expected[s:e] = bytes(e - s)
        self.assertEqual(img, bytes(expected))
        with open(nh.rescue_map_path(out), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["finished"])
        # 이미지를 열면 기록을 읽어 못 읽은 곳을 불량으로 표시
        img_src = nh.ByteSource(out)
        self.assertEqual(img_src.bad.ranges(), res["bad"])
        img_src.close()

    def test_unfinished_map_marks_uncopied_tail(self):
        path = self.write("disk.raw", os.urandom(1 << 18))
        out = os.path.join(self.dir, "part.img")
        src = nh.ByteSource(path)
        nh.rescue_image(src, out, block=1 << 16, should_stop=lambda: True)  # 첫 블록만 복사
        src.close()
        img = nh.ByteSource(out)
        self.assertEqual(img.bad.ranges(), [(1 << 16, 1 << 18)])
        img.close()

    def test_cli_rescue(self):
        path = self.write("disk.raw", os.urandom(1 << 17))
        out = os.path.join(self.dir, "cli.img")
        with mock.patch.object(nh.ByteSource, "_pread", failing_pread([(4096, 4097)])):
            code, text = self.run_cli("rescue", path, out, "--retries", "0")
        self.assertEqual(code, 0)
        self.assertIn("완료", text)
        self.assertIn("0x1000 ~ 0x11FF", text)


class LayoutTests(TmpDir):
    def test_gpt_disk(self):
        src = nh.ByteSource(self.write("gpt.img", make_gpt_disk()))
        items = nh.parse_layout(src)
        labels = {it["label"]: it for it in items}
        self.assertIn("CRC 정상", labels["GPT 헤더 (주)"]["detail"])
        self.assertIn("CRC 정상", labels["GPT 파티션 항목"]["detail"])
        self.assertIn("CRC 정상", labels["GPT 헤더 (백업)"]["detail"])
        part = labels["파티션 1: Data"]
        self.assertEqual((part["offset"], part["length"]), (2048 * 512, 2048 * 512))
        self.assertIn("NTFS", part["detail"])
        self.assertIn("기본 데이터", part["detail"])
        src.close()

    def test_damaged_gpt_reported(self):
        disk = make_gpt_disk()
        disk[512 + 40] ^= 0xFF               # 주 헤더 손상
        disk[len(disk) - 512:] = bytes(512)  # 백업 헤더 소실
        src = nh.ByteSource(self.write("bad.img", disk))
        text = nh.format_layout(nh.parse_layout(src))
        self.assertIn("손상!", text)
        self.assertIn("GPT 백업 헤더 없음", text)
        src.close()

    def test_wiped_lba0_and_protective_mbr_only(self):
        src = nh.ByteSource(self.write("empty.img", bytes(1 << 16)))
        self.assertIn("파티션 테이블 없음", nh.format_layout(nh.parse_layout(src)))
        src.close()
        disk = make_gpt_disk()
        disk[512:1024] = bytes(512)  # GPT 헤더만 지움
        src = nh.ByteSource(self.write("nogpt.img", disk))
        self.assertIn("GPT 보호 MBR 인데 GPT 헤더가 없음", nh.format_layout(nh.parse_layout(src)))
        src.close()

    def test_mbr_partition_and_superfloppy(self):
        disk = bytearray(1 << 20)
        disk[446 + 4] = 0x07
        struct.pack_into("<II", disk, 446 + 8, 128, 1000)
        disk[510:512] = b"\x55\xaa"
        disk[128 * 512:129 * 512] = ntfs_boot()
        src = nh.ByteSource(self.write("mbr.img", disk))
        text = nh.format_layout(nh.parse_layout(src))
        self.assertIn("MBR 파티션 1: NTFS/exFAT", text)
        self.assertIn("파일시스템 NTFS", text)
        src.close()
        src = nh.ByteSource(self.write("vol.img", ntfs_boot() + bytes(4096)))
        self.assertIn("오프셋 0 의 파일시스템: NTFS", nh.format_layout(nh.parse_layout(src)))
        src.close()


class SignatureScanTests(TmpDir):
    def test_finds_aligned_signatures_only(self):
        old = nh.CHUNK
        nh.CHUNK = 4096  # 청크 경계에 걸친 시그니처도 확인
        try:
            disk = bytearray(1 << 15)
            disk[0x1000:0x1004] = b"\xff\xd8\xff\xe0"
            disk[0x2000:0x2008] = b"\x89PNG\r\n\x1a\n"
            disk[0x2601:0x2605] = b"\xff\xd8\xff\xe0"     # 섹터 경계 아님 → 무시
            disk[0x3000:0x3200] = ntfs_boot()
            disk[0x3E00:0x3E05] = b"%PDF-"
            disk[0x4000 + 4:0x4000 + 8] = b"ftyp"
            disk[0x5000:0x5200] = ntfs_boot()[:510] + b"\x00\x00"  # 부트 시그니처 없음 → 무시
            src = nh.ByteSource(self.write("sig.img", disk))
            hits = dict(nh.scan_signatures(src))
            src.close()
        finally:
            nh.CHUNK = old
        self.assertEqual(hits, {0x1000: "JPEG 사진", 0x2000: "PNG 이미지", 0x3000: "NTFS 부트 섹터 (파티션 시작)",
                                0x3E00: "PDF 문서", 0x4000: "MP4 / MOV 동영상"})

    def test_cli_layout_sigscan_dump(self):
        path = self.write("gpt.img", make_gpt_disk())
        code, text = self.run_cli("layout", path)
        self.assertEqual(code, 0)
        self.assertIn("파티션 1: Data", text)
        code, text = self.run_cli("sigscan", path)
        self.assertIn("0x000000100000  NTFS 부트 섹터", text)
        self.assertIn("GPT 헤더", text)
        with mock.patch.object(nh.ByteSource, "_pread", failing_pread([(512, 1024)])):
            code, text = self.run_cli("dump", path, "--offset", "0x1F0", "--length", "0x20")
        self.assertIn("000001F0  00 00 00 00 00 00 00 00 00 00 00 00 00 00 55 AA", text)
        self.assertIn("00000200  ?? ?? ??", text)

    def test_zero_size_device_needs_size(self):
        path = self.write("empty.img", b"")
        code, _ = self.run_cli("dump", path)
        self.assertEqual(code, 1)
        path = self.write("small.img", bytes(1024))
        code, text = self.run_cli("dump", path, "--size", "2K", "--offset", "0x3F0", "--length", "0x20")
        self.assertEqual(code, 0)
        self.assertIn("00000400  ?? ??", text)


class HealthTests(unittest.TestCase):
    SMARTCTL_JSON = {
        "model_name": "Samsung SSD 980 1TB", "firmware_version": "2B4QFXO7", "smart_status": {"passed": False},
        "nvme_smart_health_information_log": {
            "critical_warning": 0x08 | 0x04, "temperature": 45, "available_spare": 3, "available_spare_threshold": 10,
            "percentage_used": 104, "data_units_read": 2000000, "data_units_written": 1000000,
            "power_on_hours": 9000, "unsafe_shutdowns": 80, "media_errors": 12, "num_err_log_entries": 30}}

    def test_smartctl_through_usb_bridge(self):
        calls = []

        def fake_run(cmd, timeout=60):
            calls.append(cmd)
            return json.dumps(self.SMARTCTL_JSON) if "sntrealtek" in cmd else json.dumps({"device": {}})

        with mock.patch.object(nh.shutil, "which", lambda name: "/usr/sbin/smartctl" if name == "smartctl" else None), \
                mock.patch.object(nh, "_run_tool", fake_run), mock.patch.object(nh, "IS_WINDOWS", False):
            h = nh.read_health("/dev/sda")
        self.assertEqual(h["source"], "smartctl -d sntrealtek")
        self.assertEqual(len(calls), 2)
        text = nh.format_health(h, "/dev/sda")
        for needle in ("읽기 전용 모드", "미디어 오류 12건", "수명 사용률 104%", "예비 블록 3%", "SMART 종합 판정: 실패",
                       "Samsung SSD 980"):
            self.assertIn(needle, text)

    def test_nvme_cli_kelvin_and_healthy_verdict(self):
        h = nh.normalize_smart({"critical_warning": 0, "temperature": 318, "avail_spare": 100, "spare_thresh": 10,
                                "percent_used": 3, "media_errors": 0})
        self.assertEqual(h["temperature_c"], 45)
        self.assertEqual(h["percentage_used"], 3)
        verdicts = nh.health_verdicts(h)
        self.assertEqual(len(verdicts), 1)
        self.assertIn("케이스", verdicts[0])

    def test_no_tools_gives_install_hint(self):
        with mock.patch.object(nh.shutil, "which", lambda name: None), mock.patch.object(nh, "IS_WINDOWS", False):
            with self.assertRaises(OSError) as ctx:
                nh.read_health("/dev/sda")
        self.assertIn("smartmontools", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
