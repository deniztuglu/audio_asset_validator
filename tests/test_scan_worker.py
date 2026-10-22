import hashlib
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from scan_worker import ScanWorker, output_destination
from validator import Rules, scan_files, validate_file


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        for name in ("SFX_UI_Click_01.wav", "nested/SFX_UI_Click_02.wav"):
            path = self.assets / name
            path.parent.mkdir(exist_ok=True)
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\0\0" * 4800)

    def collect(self, worker):
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "worker did not finish")
        events = []
        while True:
            try:
                events.append(worker.events.get_nowait())
            except queue.Empty:
                return events

    def test_background_scan_matches_engine_and_preserves_source_bytes(self):
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in scan_files(self.assets)}
        worker = ScanWorker(self.assets, Rules())
        worker.start()
        events = self.collect(worker)
        self.assertEqual(events[0].kind, "started")
        self.assertEqual(events[-1].kind, "finished")
        self.assertEqual(events[-1].completed, 2)
        self.assertEqual([event.asset for event in events if event.kind == "asset"],
                         [validate_file(p, self.assets, Rules()) for p in scan_files(self.assets)])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).digest() for p in before})

    def test_cancel_before_scan_does_not_claim_completion(self):
        worker = ScanWorker(self.assets, Rules())
        worker.cancel()
        worker.start()
        events = self.collect(worker)
        self.assertEqual(events[-1].kind, "cancelled")
        self.assertEqual(events[-1].completed, 0)

    def test_cancel_during_file_stops_before_next_file(self):
        entered, release = threading.Event(), threading.Event()

        def slow_file(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release worker")
            return validate_file(*args)

        with patch("scan_worker.validate_file", side_effect=slow_file):
            worker = ScanWorker(self.assets, Rules())
            worker.start()
            self.assertTrue(entered.wait(3))
            worker.cancel()
            release.set()
            events = self.collect(worker)
        self.assertEqual(events[-1].kind, "cancelled")
        self.assertEqual(events[-1].completed, 1)

    def test_missing_empty_and_failed_scans_surface_errors(self):
        empty = self.root / "empty"
        empty.mkdir()
        for folder in (empty, self.root / "missing"):
            worker = ScanWorker(folder, Rules())
            worker.start()
            self.assertEqual(self.collect(worker)[-1].kind, "error")
        with patch("scan_worker.scan_files", side_effect=PermissionError("denied")):
            worker = ScanWorker(self.assets, Rules())
            worker.start()
            self.assertIn("denied", self.collect(worker)[-1].message)

    def test_output_rejects_existing_paths_and_anywhere_inside_input(self):
        existing = self.root / "existing.json"
        existing.write_text("keep", encoding="utf-8")
        for path in (existing, self.assets / "profile.json", self.assets / "nested" / "profile.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                output_destination(path, ".json", self.assets)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
        good = self.root / "report.csv"
        self.assertEqual(output_destination(good, ".csv", self.assets), good)

    def test_output_rejects_wrong_extension_missing_parent_and_symlink_into_input(self):
        for path in (self.root / "report.wav", self.root / "missing" / "report.csv"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                output_destination(path, ".csv", self.assets)
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.assets, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            output_destination(alias / "report.csv", ".csv", self.assets)


if __name__ == "__main__":
    unittest.main()
