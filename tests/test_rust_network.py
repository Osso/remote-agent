"""Bounded native QUIC migration/outage proof; no provider or model requests.

Only the two network cases are loaded. Evidence stays in
/var/tmp/pi-resize-fix/rust-network/. Requires the same binary and Pi source
variables as test_rust_attach.py. SSH/systemd are fixture shims; the CLI, QUIC,
PTY, Pi components, and terminal replay are real.
"""

import json
import os
import re
import select
import socket
import threading
import time
import unittest
from pathlib import Path

import test_rust_attach as native


class UdpForwarder:
    """Fixed client-facing socket and a replaceable server-facing NAT mapping."""

    def __init__(self, server):
        self.server = server
        self.front = self.bind_socket()
        self.back = self.bind_socket()
        self.client = None
        self.blocked = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.error = None
        self.dropped = [0, 0]
        self.forwarded = [0, 0]
        self.thread = threading.Thread(target=self.forward, name="owned-quic-proxy")

    @staticmethod
    def bind_socket():
        channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        channel.bind(("127.0.0.1", 0))
        return channel

    def rotate(self):
        with self.lock:
            previous = self.back
            self.back = self.bind_socket()
            ports = (previous.getsockname()[1], self.back.getsockname()[1])
            previous.close()
            return ports

    def forward(self):
        try:
            while not self.stopped.is_set():
                with self.lock:
                    ready, _, _ = select.select([self.front, self.back], [], [], 0.02)
                    for channel in ready:
                        packet, address = channel.recvfrom(65535)
                        direction = 0 if channel is self.front else 1
                        if direction == 0:
                            self.client = address
                        if self.blocked.is_set():
                            self.dropped[direction] += 1
                            continue
                        destination = self.server if direction == 0 else self.client
                        if destination is not None:
                            sender = self.back if direction == 0 else self.front
                            sender.sendto(packet, destination)
                            self.forwarded[direction] += 1
                self.stopped.wait(0.001)
        except Exception as error:
            self.error = error

    def close(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("owned UDP forwarder thread did not stop")
        self.front.close()
        self.back.close()
        if self.error:
            raise AssertionError(f"owned UDP forwarder failed: {self.error}")


class NativeRustNetworkTests(native.NativeRustAttachTests):
    def setUp(self):
        native.EVIDENCE = Path("/var/tmp/pi-resize-fix/rust-network")
        super().setUp()
        self.proxy = None
        self.original_runtime = None
        self.addCleanup(self.cleanup_network)

    def cleanup_network(self):
        try:
            if self.client:
                self.detach()
        finally:
            # Restore the actual server port before base cancellation/release proof.
            if self.original_runtime is not None:
                (self.state / "runtime.json").write_text(self.original_runtime)
            if self.proxy is not None:
                self.proxy.close()
                self.record("proxy-cleanup", thread_alive=self.proxy.thread.is_alive())

    @staticmethod
    def process_identity(pid):
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X"):
            raise AssertionError(f"owned process {pid} is not alive")
        return fields[19]  # /proc stat field 22, start time since boot.

    def assert_lifetime(self, phase):
        for pid, identity in self.identities.items():
            self.assertEqual(self.process_identity(pid), identity)
        runtime = json.loads((self.state / "runtime.json").read_text())
        for field in ("pid", "child_pid"):
            self.assertEqual(runtime[field], self.started[field])
        launch = json.loads((self.state / "launch.json").read_text())
        self.assertEqual(launch["conversation"], self.started["conversation"])
        self.assertEqual(
            {event["pid"] for event in self.events()}, {self.started["child_pid"]}
        )
        self.record(
            "lifetime",
            phase=phase,
            identities=self.identities,
            conversation=self.started["conversation"],
        )

    def start_network(self):
        self.started = self.cli("start", "demo", "bulk-proof")
        self.assertTrue(self.started["running"])
        self.owned.update((self.started["pid"], self.started["child_pid"]))
        self.identities = {pid: self.process_identity(pid) for pid in self.owned}
        runtime_path = self.state / "runtime.json"
        self.original_runtime = runtime_path.read_text()
        runtime = json.loads(self.original_runtime)
        server_port = int(runtime["address"].rsplit(":", 1)[1])
        self.proxy = UdpForwarder(("127.0.0.1", server_port))
        runtime["address"] = f"127.0.0.1:{self.proxy.front.getsockname()[1]}"
        runtime_path.write_text(json.dumps(runtime))
        self.front_address = self.proxy.front.getsockname()
        self.record(
            "proxy",
            server_port=server_port,
            front_port=self.proxy.front.getsockname()[1],
            back_port=self.proxy.back.getsockname()[1],
        )
        self.attach()
        self.proxy.thread.start()
        self.capture("ready")
        self.identities[self.client] = self.process_identity(self.client)
        self.assert_lifetime("attached")

    def wait_event(self, phase, timeout=3):
        deadline = time.monotonic() + timeout
        output = bytearray()
        try:
            while time.monotonic() < deadline:
                output.extend(self.drain(0.01))
                if any(event["phase"] == phase for event in self.events()):
                    self.record(
                        "observed-phase",
                        phase=phase,
                        monotonic=time.monotonic(),
                        blocked=self.proxy.blocked.is_set(),
                    )
                    return
            self.fail(f"producer did not acknowledge {phase!r}")
        finally:
            self.save_stage(phase, output)

    def save_stage(self, name, output):
        self.stages.append({"name": name, "output": output.decode(errors="replace")})
        (self.root / f"{len(self.stages):02d}-{name}.ansi").write_bytes(output)
        (self.root / "stages.json").write_text(json.dumps(self.stages))

    def schedule_bulk(self):
        os.write(self.master, b"d")
        self.wait_event("bulk-scheduled")
        self.assertFalse(any(event["phase"] == "bulk" for event in self.events()))

    def finish_network(self, phase, *, offline_detach=False):
        self.assert_lifetime(phase)
        report = self.analyze(self.stages, phase)[-1]
        self.assert_bulk(report, "revised" if phase == "recovered" else "bulk")
        self.assertEqual(sum(event["phase"] == "bulk" for event in self.events()), 1)
        self.assertTrue(all(self.proxy.forwarded))
        if offline_detach:
            self.proxy.blocked.set()
            self.wait_reconnecting("offline-detach")
            self.assert_lifetime("offline-before-detach")
            began = time.monotonic()
            client_pid = self.client
            self.detach()
            elapsed = time.monotonic() - began
            self.assertLess(elapsed, 2, "offline detach must not wait for QUIC retry")
            self.record("offline-detach", elapsed=elapsed)
            self.identities.pop(client_pid)
            self.proxy.blocked.clear()
            self.assert_lifetime("offline-after-detach")
        else:
            self.detach()
        after = self.cli("status", "bulk-proof")
        self.assertTrue(after["running"])
        for field in ("pid", "child_pid", "conversation"):
            self.assertEqual(after[field], self.started[field])
        self.record(
            "behavior",
            result="passed",
            phase=phase,
            forwarded=self.proxy.forwarded,
            dropped=self.proxy.dropped,
        )

    def test_active_quic_nat_port_migration_preserves_all_80_markers(self):
        self.start_network()
        self.schedule_bulk()
        client_address = self.proxy.client
        old, new = self.proxy.rotate()
        self.assertNotEqual(old, new)
        self.record("nat-port-change", old=old, new=new, client_address=client_address)
        self.capture("bulk")
        self.assertEqual(self.proxy.client, client_address)
        self.assertNotIn(
            "reconnecting", "".join(stage["output"] for stage in self.stages)
        )
        self.finish_network("migrated")

    def test_packet_outage_reconnects_new_nat_port_without_restarting_pi(self):
        self.start_network()
        self.schedule_bulk()
        self.proxy.blocked.set()
        began = time.monotonic()
        self.record("blackhole-start", monotonic=began)
        self.wait_event("bulk")
        self.assert_lifetime("bulk-produced-during-blackhole")
        os.write(self.master, b"r")
        output = self.wait_reconnecting("blackholed")
        self.assertNotIn(b"BULK_LINE_", output)
        self.assertTrue(all(self.proxy.dropped), "blackhole must drop both directions")
        self.assert_lifetime("reconnecting")
        # logs runs on the server host against the real local port; only the
        # attached client's ticket points through the blackholed proxy.
        runtime_path = self.state / "runtime.json"
        proxied_runtime = runtime_path.read_text()
        runtime_path.write_text(self.original_runtime)
        try:
            logs = self.cli("logs", "bulk-proof", check=False)
        finally:
            runtime_path.write_text(proxied_runtime)
        self.assertEqual(logs.returncode, 0, logs.stderr)
        self.assertEqual(
            re.findall(r"BULK_LINE_(\d{3})", logs.stdout),
            [f"{index:03}" for index in range(80)],
        )
        self.record("server-emitted-during-blackhole", marker_count=80)
        self.assertFalse(any(event["phase"] == "revised" for event in self.events()))
        old, new = self.proxy.rotate()
        self.assertNotEqual(old, new)
        self.proxy.blocked.clear()
        self.record("blackhole-end", monotonic=time.monotonic(), old=old, new=new)
        self.capture("revised")
        self.assertEqual(self.proxy.front.getsockname(), self.front_address)
        self.assertEqual(sum(event["phase"] == "revised" for event in self.events()), 1)
        self.finish_network("recovered", offline_detach=True)

    def test_reconnect_notice_does_not_scroll_rows_into_history(self):
        # Observed: each outage notice scrolled two viewport rows into native
        # scrollback; the next non-reset repaint duplicated BULK_LINE_034/035.
        self.start_network()
        os.write(self.master, b"b")
        self.capture("bulk")
        os.write(self.master, b"r")
        self.capture("revised")
        previous_peer = self.proxy.client
        self.proxy.blocked.set()
        self.wait_reconnecting("outage")
        self.proxy.blocked.clear()
        deadline = time.monotonic() + 10
        while self.proxy.client == previous_peer:
            self.assertLess(time.monotonic(), deadline, "client did not reconnect")
            time.sleep(0.02)
        self.save_stage("reconnected", self.drain(1))
        os.write(self.master, b"b")
        deadline = time.monotonic() + 12
        while sum(event["phase"] == "bulk" for event in self.events()) < 2:
            self.assertLess(time.monotonic(), deadline, "second bulk not produced")
            time.sleep(0.05)
        self.capture("bulk-after-reconnect", wait_phase=False)
        report = self.analyze(self.stages, "after-reconnect")[-1]
        transcript = "\n".join(report["history"] + report["visible"])
        self.assertEqual(
            re.findall(r"BULK_LINE_\d{3}", transcript),
            [f"BULK_LINE_{index:03}" for index in range(80)],
        )
        self.assertNotIn("reconnecting", transcript)
        self.assert_lifetime("after-reconnect")
        self.detach()

    def wait_reconnecting(self, name):
        # Real QUIC's default idle timeout is 30s. Do not replace it with a stub.
        began = time.monotonic()
        output = bytearray()
        try:
            while time.monotonic() - began < 45:
                output.extend(self.drain(0.1))
                self.assert_lifetime_alive()
                if b"reconnecting" in output:
                    self.record(
                        "client-reconnecting",
                        phase=name,
                        elapsed=time.monotonic() - began,
                    )
                    return bytes(output)
            self.fail("real native client did not enter reconnect within 45s")
        finally:
            self.save_stage(name, output)

    def assert_lifetime_alive(self):
        for pid, identity in self.identities.items():
            self.assertEqual(self.process_identity(pid), identity)


def load_tests(loader, tests, pattern):
    names = [
        name for name in NativeRustNetworkTests.__dict__ if name.startswith("test_")
    ]
    return unittest.TestSuite(NativeRustNetworkTests(name) for name in names)


if __name__ == "__main__":
    unittest.main()
