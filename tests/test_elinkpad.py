import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.input_injection.windows.elinkpad import ElinkPads


class PadTests(unittest.TestCase):
    def test_input_feedback_and_release_on_worker(self):
        feedback = []
        device = Mock()
        device.poll.return_value = (1, 123, 456)
        pads = ElinkPads(feedback.append, device_factory=lambda: device)
        try:
            pads.update(2, (1, 2, 3, 4, 5, 6, 7))
            deadline = time.monotonic() + 2
            while not feedback and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(feedback)
            self.assertEqual(feedback[0], {"type": "rumble", "index": 2, "left": 123, "right": 456})
            with self.assertRaisesRegex(RuntimeError, "一个手柄"):
                pads.update(3, (1,) * 7)
        finally:
            pads.close()
        device.close.assert_called_once()
        self.assertFalse(pads.thread.is_alive())
        self.assertEqual(feedback[-1]["left"], 0)

    def test_missing_device_does_not_fall_back(self):
        pads = ElinkPads(lambda _: None, device_factory=Mock(side_effect=OSError("missing device")))
        try:
            pads.update(0, (0,) * 7)
            deadline = time.monotonic() + 2
            while not pads.error and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn("missing device", pads.error)
            with self.assertRaises(RuntimeError):
                pads.update(0, (1,) * 7)
        finally:
            pads.close()


if __name__ == "__main__":
    unittest.main()
