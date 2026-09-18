"""Explicit input/rumble exercise for an isolated driver test OS, not normal CI."""
import argparse
import time

from elink.core.elinkpad import Device
from elink.core.input import XInput


def wait_for(predicate, label, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError(label)
        time.sleep(0.01)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", type=int, choices=range(4), required=True,
                        help="Known ElinkPad XInput slot in the isolated test OS")
    parser.add_argument("--exercise-input", action="store_true", required=True,
                        help="Submit synthetic controller state and rumble to the selected test slot")
    args = parser.parse_args()
    device = Device()
    xinput = XInput()
    zero = [0] * 7
    try:
        expected = [0x1000, 255, 128, -32768, 32767, -1, 1]
        device.update(expected)
        wait_for(lambda: xinput.read(args.slot) == expected, "XInput state mismatch; check slot and driver binding", 0.35)
        xinput.rumble(args.slot, 0x8080, 0xFFFF)
        wait_for(lambda: device.poll()[1:] == (0x8080, 0xFFFF), "Rumble feedback mismatch", 0.35)
        try:
            second = Device()
        except OSError as exc:
            if exc.winerror != 32:
                raise
        else:
            second.close()
            raise RuntimeError("Second file unexpectedly acquired an owned device")
        device.update(expected)
        time.sleep(0.65)
        assert xinput.read(args.slot) == zero, "Timeout did not neutralize input"
        assert device.poll()[1:] == (0, 0), "Timeout did not neutralize rumble"
        device.update(expected)
    finally:
        device.close()
    wait_for(lambda: xinput.read(args.slot) == zero, "File release did not neutralize input")
    replacement = Device()
    replacement.close()
    print("PASS: XInput state, rumble, competing lease, timeout, release and reacquire.")
    print("Still required: helper/process kill, unplug, Windows versions, security settings and real games.")


if __name__ == "__main__":
    main()
