"""Native resize/replay proof. Run after rebuilding REMOTE_AGENT_RUST_BIN.

Uses only owned local fixture homes, PTYs, QUIC, and real Pi components.
"""

import fcntl
import json
import os
import select
import shutil
import struct
import termios
import unittest
from pathlib import Path

import test_rust_attach as fixture


class NativeRustResizeTests(fixture.NativeRustAttachTests):
    def setUp(self):
        fixture.EVIDENCE = Path("/var/tmp/pi-resize-fix/rust-resize")
        super().setUp()
        self.peer = None
        self.geometry = (165, 46)
        self.addCleanup(self.detach_peer)

    def drain(self, duration):
        output = super().drain(duration)
        if self.peer:
            master = self.peer[0]
            while select.select([master], [], [], 0)[0]:
                try:
                    if not os.read(master, 65536):
                        break
                except OSError:
                    break
        return output

    def capture(self, phase, *, wait_phase=True):
        try:
            super().capture(phase, wait_phase=wait_phase)
        finally:
            if self.stages:
                self.stages[-1].update(columns=self.geometry[0], rows=self.geometry[1])
                (self.root / "stages.json").write_text(json.dumps(self.stages))

    def resize(self, columns, rows):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.geometry = (columns, rows)
        self.record("physical-resize", columns=columns, rows=rows)

    def attach_peer(self):
        home = self.root / "peer-home"
        home.mkdir()
        shutil.copytree(self.root / ".config", home / ".config")
        (home / ".local").mkdir()
        (home / ".local/bin").symlink_to(self.root / ".local/bin", target_is_directory=True)
        original = (self.master, self.client, self.original_termios, self.env)
        try:
            self.env = {
                **self.env,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_STATE_HOME": str(home / ".local/state"),
            }
            self.attach()
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 80, 0, 0))
            self.peer = (self.master, self.client, self.original_termios)
            self.capture_peer_startup()
        finally:
            self.master, self.client, self.original_termios, self.env = original

    def capture_peer_startup(self):
        # The peer's first replay confirms transport startup before it pins the PTY.
        output = bytearray()
        for _ in range(100):
            output.extend(super().drain(0.05))
            if b"\x1b[3J" in output:
                output.extend(super().drain(0.3))
                (self.root / "peer-startup.ansi").write_bytes(output)
                return
        self.fail("second native client did not receive its initial snapshot")

    def detach_peer(self):
        if not self.peer:
            return
        original = (self.master, self.client, self.original_termios)
        self.master, self.client, self.original_termios = self.peer
        try:
            self.detach()
        finally:
            self.peer = None
            self.master, self.client, self.original_termios = original

    def child_size(self, child_pid):
        fd = os.open(f"/proc/{child_pid}/fd/0", os.O_RDONLY | os.O_NOCTTY)
        try:
            rows, columns, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))
            return columns, rows
        finally:
            os.close(fd)

    def exercise_resize(self, pinned):
        started = self.cli("start", "demo", "bulk-proof")
        self.owned.update((started["pid"], started["child_pid"]))
        self.attach()
        self.capture("ready")
        if pinned:
            self.attach_peer()
            self.capture("pinned", wait_phase=False)
            self.assertEqual(self.child_size(started["child_pid"]), (80, 20))
        os.write(self.master, b"b")
        self.capture("bulk")
        self.resize(80, 20)
        os.write(self.master, b"r")
        self.capture("revised")
        self.assertEqual(self.child_size(started["child_pid"]), (80, 20))
        self.resize(165, 46)
        os.write(self.master, b"w")
        self.capture("widgets")
        os.write(self.master, b"p")
        self.capture("partial")
        self.assertEqual(self.child_size(started["child_pid"]), (80, 20) if pinned else (165, 46))
        reports = self.analyze(self.stages, "pinned" if pinned else "resize")
        for report, stage in zip(reports, self.stages, strict=True):
            self.assertEqual(len(report["visible"]), stage["rows"])
            if report["name"] in ("bulk", "revised", "widgets", "partial"):
                self.assert_bulk(report, report["name"])
        status = self.cli("status", "bulk-proof")
        for field in ("pid", "child_pid", "conversation"):
            self.assertEqual(status[field], started[field])
        self.assertEqual({event["pid"] for event in self.events()}, {started["child_pid"]})
        self.detach_peer()
        self.detach()
        self.record("resize-behavior", result="passed", pinned=pinned)

    def test_resize_bulk_revised_widgets(self):
        self.exercise_resize(pinned=False)

    def test_physical_growth_with_second_client_pinning_pty_minimum(self):
        self.exercise_resize(pinned=True)


def load_tests(loader, tests, pattern):
    # Inherit fixture helpers, not the parent's independently owned test cases.
    return unittest.TestSuite(
        NativeRustResizeTests(name)
        for name in (
            "test_resize_bulk_revised_widgets",
            "test_physical_growth_with_second_client_pinning_pty_minimum",
        )
    )


if __name__ == "__main__":
    unittest.main()
