"""Keep encoding policy stable across older and float-capable wave decoders."""

import contextlib
import csv
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest

from validator import Rules, main, validate_file


class EncodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.assets = self.directory / "audio"
        self.assets.mkdir()

    def wav(self, tag=1, bits=16, subtype=None):
        # Explicit RIFF fixtures avoid writer-version differences. All-zero samples
        # are valid silence for both signed integer PCM and IEEE float.
        width, rate = bits // 8, 48000
        fmt = struct.pack("<HHIIHH", tag, 1, rate, rate * width, width, bits)
        if tag == 0xFFFE:
            guid = struct.pack("<IHH8s", subtype, 0, 0x0010, b"\x80\x00\x00\xaa\x00\x38\x9b\x71")
            fmt += struct.pack("<HHI", 22, bits, 4) + guid
        samples = b"\0" * width * 4800
        chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
        chunks += b"data" + struct.pack("<I", len(samples)) + samples
        path = self.assets / f"SFX_Encoding_Test_{len(list(self.assets.iterdir())):02d}.wav"
        path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)
        return path

    def test_ieee_float_rejected_even_when_bit_depth_is_allowed(self):
        for depth in (32, 64):
            with self.subTest(depth=depth):
                path = self.wav(tag=3, bits=depth)
                result = validate_file(path, self.assets, Rules(bit_depths=(depth,)))
                self.assertEqual(result.status, "ERROR")
                self.assertEqual([(check.check, check.result) for check in result.checks],
                                 [("naming", "PASS"), ("format", "ERROR")])

    def test_integer_pcm_32_bit_is_still_supported_when_allowed(self):
        path = self.wav(bits=32)
        self.assertEqual(validate_file(path, self.assets, Rules(bit_depths=(32,))).status, "PASS")

    @unittest.skipIf(sys.version_info < (3, 12), "extensible PCM requires Python 3.12+")
    def test_extensible_integer_pcm_is_not_rejected_by_the_encoding_check(self):
        path = self.wav(tag=0xFFFE, bits=24, subtype=1)
        self.assertEqual(validate_file(path, self.assets, Rules()).status, "PASS")

    def test_extensible_float_is_not_mistaken_for_integer_pcm(self):
        path = self.wav(tag=0xFFFE, bits=32, subtype=3)
        result = validate_file(path, self.assets, Rules(bit_depths=(32,)))
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(next(c for c in result.checks if c.check == "format").result, "ERROR")

    def test_cli_reports_float_format_error_and_continues_with_valid_audio(self):
        floating = self.wav(tag=3, bits=32)
        self.wav()
        report = self.directory / "report.csv"
        with contextlib.redirect_stdout(io.StringIO()):
            status = main([str(self.assets), "--csv", str(report)])
        self.assertEqual(status, 1)
        with report.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        float_rows = [row for row in rows if row["file"] == floating.name]
        self.assertEqual([(row["check"], row["result"]) for row in float_rows],
                         [("naming", "PASS"), ("format", "ERROR")])
        self.assertEqual(len(rows), 8)


if __name__ == "__main__":
    unittest.main()
