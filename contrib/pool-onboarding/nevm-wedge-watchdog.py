#!/usr/bin/env python3
"""NEVM wedge watchdog -- kit item A3-K1 (chain-fix program 2026-10-30).

WHAT THIS IS: detection + alarm ONLY. It does not fix anything.

Seat A3's audit (wsA/03-nevm-zmq-audit.md) found the lead stall
mechanism inside the v5.0.9 daemon: fetching a NEVM template is a
synchronous `nevmblock` ZMQ request made under cs_main, on a shared
REQ socket with no receive timeout. One receive timeout can leave
that socket in an EFSM state; from then on EVERY template fetch
fails, block production stops, and -- in the deployed default --
nothing self-heals. The daemon-side fix is a daemon patch; until a
pool upgrades to a patched daemon, this watchdog is the difference
between noticing in minutes and noticing never.

THE SIGNATURE (verified in the deployed source,
src/zmq/zmqpublishnotifier.cpp:479,536 and src/node/miner.cpp:214):
the daemon log (debug.log) repeats

    Could not fetch NEVM block nevm-header-not-sent

This script tails the daemon log, counts CONSECUTIVE occurrences of
that signature, and after THRESHOLD (default 3) consecutive hits
raises the kit's mechanism-4 alarm on every channel it has:

  * a `*** ALARM ***` line to stderr (captured by systemd journal),
  * an ALARM_NEVM_WEDGE marker file (default: next to the log it
    watches, ALARM_NEVM_WEDGE.txt) naming the count and the fix,
  * an optional ALARM_CMD hook (alarm text on stdin).

It latches: it alarms once per wedge episode, then stays quiet until
the signature clears (any intervening non-signature line resets the
consecutive count; a fresh episode can alarm again).

OPERATOR GUIDANCE (also written into the marker file): the wedge
does NOT clear itself. Restart the daemon:

    systemctl restart syscoind

and confirm templates flow again (getauxblock returns a template
with "chainid": 29). Restarting is safe: the chain data is on disk;
the restart only drops the poisoned ZMQ socket state.

Usage:
    nevm-wedge-watchdog.py [--log PATH] [--threshold N]
                           [--alarm-file PATH] [--alarm-cmd CMD]
                           [--poll-secs S] [--from-start]
Defaults: log /home/shitcoin/.syscoin/debug.log, threshold 3,
alarm file <log dir>/ALARM_NEVM_WEDGE.txt, poll 2 s. Without
--from-start, watching begins at end-of-file (history is not
re-alarmed on every watchdog restart).
"""

import argparse
import os
import subprocess
import sys
import time

SIGNATURE = "Could not fetch NEVM block nevm-header-not-sent"

GUIDANCE = (
    "NEVM template fetch is wedged (signature %r seen %d times in a "
    "row). The daemon cannot build AuxPoW templates in this state "
    "and block production is stopped. This does NOT clear itself. "
    "FIX: restart the daemon (systemctl restart syscoind), then "
    "confirm getauxblock returns a template with \"chainid\": 29. "
    "Detection only -- this watchdog does not restart anything."
)


def fire_alarm(count, alarm_file, alarm_cmd, log):
    msg = GUIDANCE % (SIGNATURE, count)
    line = "*** ALARM *** NEVM_WEDGE %s" % msg
    print(line, file=sys.stderr, flush=True)
    print(line, file=sys.stdout, flush=True)
    if alarm_file:
        try:
            directory = os.path.dirname(os.path.abspath(alarm_file))
            os.makedirs(directory, exist_ok=True)
            tmp = alarm_file + ".tmp"
            with open(tmp, "w") as f:
                f.write("[%s] ALARM NEVM_WEDGE count=%d log=%s\n%s\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"),
                           count, log, msg))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, alarm_file)
            try:
                dfd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        except OSError as e:
            print("NEVM watchdog: could not write alarm file: %r" % e,
                  file=sys.stderr, flush=True)
    if alarm_cmd:
        try:
            subprocess.run(alarm_cmd, shell=True, timeout=30,
                           input=("ALARM NEVM_WEDGE %s" % msg).encode())
        except Exception as e:
            print("NEVM watchdog: alarm command failed: %r" % e,
                  file=sys.stderr, flush=True)
    return msg


def clear_alarm_marker(alarm_file):
    try:
        os.unlink(alarm_file + ".resolved")
    except OSError:
        pass
    try:
        os.replace(alarm_file, alarm_file + ".resolved")
    except OSError:
        pass


class WedgeDetector:
    """Consecutive-signature counter with a latched alarm. Pure logic
    (no I/O) so the kit harness can drive it with a fake log feed."""

    def __init__(self, threshold=3):
        self.threshold = max(1, int(threshold))
        self.consecutive = 0
        self.alarmed = False

    def feed(self, line):
        """Returns True exactly once per episode: on the line where
        the consecutive count reaches the threshold."""
        if SIGNATURE in line:
            self.consecutive += 1
            if self.consecutive >= self.threshold and not self.alarmed:
                self.alarmed = True
                return True
        else:
            if self.alarmed:
                # Episode over: the daemon produced a log line that is
                # not the wedge signature (e.g. after a restart).
                self.alarmed = False
            self.consecutive = 0
        return False


def watch(log_path, threshold, alarm_file, alarm_cmd, poll_secs,
          from_start=False):
    detector = WedgeDetector(threshold)
    fh = None
    inode = None
    while True:
        try:
            if fh is None:
                fh = open(log_path, "r", errors="replace")
                st = os.fstat(fh.fileno())
                inode = (st.st_dev, st.st_ino)
                if not from_start:
                    fh.seek(0, os.SEEK_END)
            line = fh.readline()
            if line:
                if detector.feed(line):
                    fire_alarm(detector.consecutive, alarm_file,
                               alarm_cmd, log_path)
                elif not detector.alarmed and alarm_file \
                        and os.path.exists(alarm_file) \
                        and detector.consecutive == 0:
                    clear_alarm_marker(alarm_file)
                continue
            # No new data: check for rotation/truncation, then sleep.
            try:
                st = os.stat(log_path)
                if (st.st_dev, st.st_ino) != inode \
                        or st.st_size < fh.tell():
                    fh.close()
                    fh = None
                    from_start = True  # new file: read it from the top
                    continue
            except OSError:
                fh.close()
                fh = None
            time.sleep(poll_secs)
        except OSError:
            fh = None
            time.sleep(poll_secs)


def main(argv=None):
    p = argparse.ArgumentParser(description="NEVM wedge watchdog "
                                "(detection + alarm only)")
    p.add_argument("--log", default="/home/shitcoin/.syscoin/debug.log")
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--alarm-file", default=None,
                   help="default: <log dir>/ALARM_NEVM_WEDGE.txt")
    p.add_argument("--alarm-cmd", default="")
    p.add_argument("--poll-secs", type=float, default=2.0)
    p.add_argument("--from-start", action="store_true")
    args = p.parse_args(argv)
    alarm_file = args.alarm_file or os.path.join(
        os.path.dirname(os.path.abspath(args.log)), "ALARM_NEVM_WEDGE.txt")
    print("NEVM wedge watchdog: watching %s (signature %r, threshold "
          "%d consecutive)" % (args.log, SIGNATURE, args.threshold),
          file=sys.stderr, flush=True)
    watch(args.log, args.threshold, alarm_file, args.alarm_cmd,
          args.poll_secs, from_start=args.from_start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
