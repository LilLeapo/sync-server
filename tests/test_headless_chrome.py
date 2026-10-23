"""headless_chrome：管道传输、超时与清理（tests/fake_chrome.py 当 Chrome），以及一条要真 Chrome 的冒烟测试。"""
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import headless_chrome  # noqa: E402
from server.headless_chrome import (PNG_MAGIC, PROFILE_PREFIX, HeadlessChrome, RenderError,  # noqa: E402
                                    find_chrome, sweep_profiles)

FAKE = os.path.join(ROOT, "tests", "fake_chrome.py")
HTML = '<!doctype html><div id="card">x</div>'


def profiles() -> set:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), PROFILE_PREFIX + "*")))


def profile_of(run: dict) -> str:
    return next(a.split("=", 1)[1] for a in run["argv"] if a.startswith("--user-data-dir="))


def group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestFakeChrome(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.binary = os.path.join(cls._dir.name, "chrome")
        with open(cls.binary, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        os.chmod(cls.binary, 0o755)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def setUp(self):
        self.log = os.path.join(self._dir.name, self.id() + ".log")
        env = mock.patch.dict(os.environ, {"FAKE_CHROME_MODE": "ok", "FAKE_CHROME_LOG": self.log})
        env.start()
        self.addCleanup(env.stop)

    def runs(self) -> list[dict]:
        merged: dict[int, dict] = {}
        with open(self.log, encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                merged.setdefault(row["pid"], {}).update(row)
        os.remove(self.log)
        return sorted(merged.values(), key=lambda r: r["start"])

    def fail_with(self, mode: str, **kwargs) -> str:
        os.environ["FAKE_CHROME_MODE"] = mode
        with self.assertRaises(RenderError) as cm:
            HeadlessChrome(self.binary, **kwargs)(HTML, "card")
        return str(cm.exception)

    def test_ok_returns_png_and_leaves_nothing_behind(self):
        png = HeadlessChrome(self.binary)(HTML, "card")
        self.assertTrue(png.startswith(PNG_MAGIC))
        [run] = self.runs()
        self.assertIn("end", run, "fake chrome exits on Browser.close")
        self.assertFalse(os.path.exists(profile_of(run)), "profile removed")
        self.assertFalse(group_alive(run["pid"]), "process group reaped")

    def test_argv_has_pipe_resolver_block_profile_and_sandbox_only_for_root(self):
        HeadlessChrome(self.binary, euid=lambda: 501)(HTML, "card")
        HeadlessChrome(self.binary, euid=lambda: 0)(HTML, "card")
        user, root = [r["argv"] for r in self.runs()]
        for argv in (user, root):
            self.assertIn("--remote-debugging-pipe", argv)
            self.assertIn("--host-resolver-rules=MAP * ~NOTFOUND", argv)
            self.assertIn("--headless=new", argv)
            self.assertTrue(profile_of({"argv": argv}).startswith(os.path.join(tempfile.gettempdir(), PROFILE_PREFIX)))
            self.assertEqual(argv[-1], "about:blank")
        self.assertNotIn("--no-sandbox", user)
        self.assertIn("--no-sandbox", root)

    def test_hang_times_out_kills_group_and_removes_profile(self):
        started = time.monotonic()
        message = self.fail_with("hang", timeout_s=1)
        self.assertLess(time.monotonic() - started, 1.6, "one deadline for the whole call")
        self.assertIn("超过 1 秒", message)
        [run] = self.runs()
        self.assertFalse(group_alive(run["pid"]), "hung chrome killed by process group")
        self.assertFalse(os.path.exists(profile_of(run)), "profile removed after timeout")

    def test_crash_message_carries_stderr_tail(self):
        self.assertIn("libnss3.so", self.fail_with("crash"))

    def test_cdp_error_and_non_png_are_render_errors(self):
        self.assertIn("fake failure", self.fail_with("cdp_error"))
        self.assertIn("PNG", self.fail_with("notpng"))

    def test_interleaved_events_are_skipped(self):
        os.environ["FAKE_CHROME_MODE"] = "events"
        self.assertTrue(HeadlessChrome(self.binary)(HTML, "card").startswith(PNG_MAGIC))

    def test_max_parallel_bounds_concurrent_chromes(self):
        os.environ["FAKE_CHROME_DELAY"] = "0.3"

        def overlapped(max_parallel):
            render = HeadlessChrome(self.binary, max_parallel=max_parallel)
            threads = [threading.Thread(target=render, args=(HTML, "card")) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            first, second = self.runs()
            return second["start"] < first["end"]

        self.assertFalse(overlapped(1), "max_parallel=1 runs one chrome at a time")
        self.assertTrue(overlapped(2), "max_parallel=2 lets two chromes overlap")

    def test_renders_when_pipe_fds_come_out_low(self):
        code = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {ROOT!r})
            r, w = os.pipe()
            assert max(r, w) < 10, ("expected low fds in a fresh process", r, w)
            os.close(r)
            os.close(w)
            from server.headless_chrome import HeadlessChrome
            sys.stdout.write(HeadlessChrome({self.binary!r})("<div id=card></div>", "card")[1:4].decode())
        """)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual((out.returncode, out.stdout), (0, "PNG"), out.stderr)

    def test_find_chrome_rules(self):
        self.assertEqual(find_chrome(self.binary), self.binary)
        self.assertIsNone(find_chrome("/nonexistent/chrome", which=lambda n: "/usr/bin/" + n),
                          "a wrong configured path does not fall back")
        plain = os.path.join(self._dir.name, "not-executable")
        open(plain, "w").close()
        self.assertIsNone(find_chrome(plain))
        with mock.patch.object(headless_chrome, "MAC_APPS", ()):
            self.assertEqual(find_chrome(which={"chromium": "/a", "google-chrome": "/b"}.get), "/a")
            self.assertEqual(find_chrome(which={"google-chrome-stable": "/c"}.get), "/c")
            self.assertIsNone(find_chrome(which=lambda n: None))
        with mock.patch.object(headless_chrome, "MAC_APPS", ("/nonexistent/Chrome", self.binary)):
            self.assertEqual(find_chrome(which=lambda n: "/usr/bin/" + n), self.binary)

    def test_sweep_removes_only_stale_profiles(self):
        old, fresh = tempfile.mkdtemp(prefix=PROFILE_PREFIX), tempfile.mkdtemp(prefix=PROFILE_PREFIX)
        other = tempfile.mkdtemp(prefix="not-a-card-profile-")
        for path in (old, fresh, other):
            self.addCleanup(shutil.rmtree, path, True)
        past = time.time() - 7200
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        self.assertGreaterEqual(sweep_profiles(3600), 1)
        self.assertEqual([os.path.isdir(p) for p in (old, fresh, other)], [False, True, True])


@unittest.skipUnless(os.environ.get("CARD_CHROME_TESTS") and find_chrome(), "需要 CARD_CHROME_TESTS=1 且本机有 Chrome")
class TestRealChrome(unittest.TestCase):
    def test_renders_element_at_twice_its_css_width(self):
        html = ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
                '<style>body{margin:0}#card{box-sizing:border-box;width:500px;padding:20px;font-size:16px}</style>'
                '</head><body><div id="card">电解铜 110,650 <img src="http://example.com/x.png"></div></body></html>')
        before = profiles()
        started = time.monotonic()
        png = HeadlessChrome(find_chrome())(html, "card")
        elapsed = time.monotonic() - started
        self.assertTrue(png.startswith(PNG_MAGIC))
        self.assertEqual(struct.unpack(">I", png[16:20])[0], 1000, "scale 2 makes a 500px card 1000px wide")
        self.assertLess(elapsed, 3, "blocked remote image does not delay the render")
        self.assertEqual(profiles(), before, "no card-chrome-* dir left")


if __name__ == "__main__":
    unittest.main()
