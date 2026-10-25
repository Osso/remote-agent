"""Six real QUIC idle outages in one native attachment, within 300 seconds.

Reuse the owned UDP proxy and real Pi component fixture. No production session
is used. Evidence remains in /var/tmp/pi-resize-fix/rust-retry/.
"""

import os
import re
import time
import unittest
from pathlib import Path

import test_rust_attach as native
from test_rust_network import NativeRustNetworkTests


class NativeRustRetryBudgetTests(NativeRustNetworkTests):
    def setUp(self):
        native.EVIDENCE = Path("/var/tmp/pi-resize-fix/rust-retry")
        native.NativeRustAttachTests.setUp(self)
        self.proxy = None
        self.original_runtime = None
        self.addCleanup(self.cleanup_network)

    def wait_acknowledged_output(self, phase, previous_count, cycle):
        output = bytearray()
        began = time.monotonic()
        try:
            while time.monotonic() - began < 12:
                output.extend(self.drain(0.1))
                self.assert_lifetime_alive()
                count = sum(event["phase"] == phase for event in self.events())
                if count > previous_count and output:
                    output.extend(self.drain(1))
                    self.assertEqual(count, previous_count + 1)
                    self.record("input-and-output-recovered", cycle=cycle, phase=phase)
                    return
            self.fail(f"outage {cycle}: no acknowledged {phase} input and output")
        finally:
            self.save_stage(f"recovery-{cycle}-{phase}", output)

    def test_six_separate_outages_do_not_exhaust_consecutive_failure_budget(self):
        began = time.monotonic()
        self.start_network()
        os.write(self.master, b"b")
        self.capture("bulk")
        self.assert_bulk(self.analyze(self.stages, "initial")[-1], "bulk")
        for cycle in range(1, 7):
            phase, key = ("revised", b"r") if cycle % 2 else ("bulk", b"b")
            previous_count = sum(event["phase"] == phase for event in self.events())
            previous_address = self.proxy.client
            self.proxy.blocked.set()
            self.record("blackhole-start", cycle=cycle, monotonic=time.monotonic())
            # The final outage also proves input queued before timeout survives.
            if cycle == 6:
                os.write(self.master, key)
            self.wait_reconnecting(f"outage-{cycle}")
            self.assert_lifetime(f"outage-{cycle}")
            self.assertEqual(
                sum(event["phase"] == phase for event in self.events()), previous_count
            )
            self.proxy.blocked.clear()
            self.record("blackhole-end", cycle=cycle, monotonic=time.monotonic())
            if cycle != 6:
                # No server output changed while offline. Hello's authorized
                # snapshot has the previous sequence and must restore the budget
                # even though duplicate display rendering is suppressed.
                deadline = time.monotonic() + 10
                while self.proxy.client == previous_address:
                    self.assert_lifetime_alive()
                    self.assertLess(time.monotonic(), deadline, "no new QUIC peer")
                    time.sleep(0.02)
                self.save_stage(f"hello-{cycle}", self.drain(1))
                os.write(self.master, key)
            self.wait_acknowledged_output(phase, previous_count, cycle)
            self.assert_lifetime(f"recovered-{cycle}")
        # Direct-PTY reference for b,r,b,r,b,r,b: 80 markers once; Pi keeps the
        # revised heading, so this is not compared with the first bulk render.
        report = self.analyze(self.stages, "six-recoveries")[-1]
        transcript = "\n".join(report["history"] + report["visible"])
        self.assertEqual(
            re.findall(r"BULK_LINE_\d{3}", transcript),
            [f"BULK_LINE_{index:03}" for index in range(80)],
        )
        self.assertNotIn("reconnecting", transcript)
        self.assertTrue(all(self.proxy.dropped), "outages must drop both directions")
        self.detach()
        status = self.cli("status", "bulk-proof")
        self.assertTrue(status["running"])
        for field in ("pid", "child_pid", "conversation"):
            self.assertEqual(status[field], self.started[field])
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 300)
        self.record("six-outage-proof", elapsed=elapsed, result="passed")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        [
            NativeRustRetryBudgetTests(
                "test_six_separate_outages_do_not_exhaust_consecutive_failure_budget"
            )
        ]
    )


if __name__ == "__main__":
    unittest.main()
