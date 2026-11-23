# buck50: Test and measurement firmware for “Blue Pill” STM32F103 development board
#
# This file is part of buck50.
#
# The buck50 program is free software: you can redistribute it
# and/or modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# The buck50 program is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# (LICENSE.txt) along with the buck50 program.  If not, see
# <https://www.gnu.org/licenses/gpl.html>

"""End-to-end tests of buck50.py against fake_buck50.py: the real
program, on a pseudo-terminal, talking the real protocol to a simulated
device."""

import os
import struct
import subprocess
import sys

import pytest

import fake_buck50
from fake_buck50 import FakeBuck50, digital_words
from hostsession import HostSession, BUCK50

DGTL_CMD = fake_buck50.DGTL_CMD
UPLD_CMD = fake_buck50.UPLD_CMD


def received(device, code):
    return [data for (cmd, data) in device.commands if cmd == code]


def parse_vcd(path):
    """{signal id: [(time, level), ...]} plus timescale line."""
    changes   = {}
    timescale = None
    time      = None
    with open(path) as file:
        for line in file:
            line = line.strip()
            if line.startswith('$timescale'):
                timescale = line
            elif line.startswith('#'):
                time = int(line[1:])
            elif line and line[0] in '01' and time is not None:
                changes.setdefault(line[1:], []).append((time, int(line[0])))
    return (timescale, changes)


def test_version_option():
    result = subprocess.run([sys.executable, '-W', 'error::SyntaxWarning',
                             BUCK50, '--version'],
                            capture_output=True, timeout=30)
    assert result.returncode == 0
    assert result.stderr == b''
    assert b'buck50.py 0.9.7' in result.stdout


def test_connects_and_reports_matching_firmware():
    with FakeBuck50() as device, HostSession(device.path) as host:
        assert "Firmware identity match: 0xea017af5" in host.startup
        assert "Firmware version  match: 0.9.3" in host.startup
        assert "FAKE0BUCK50" in host.startup
        assert "SyntaxWarning" not in host.startup
        assert "Traceback" not in host.startup


def test_older_firmware_still_accepted():
    with FakeBuck50(version=(0, 9, 2)) as device, \
         HostSession(device.path) as host:
        assert "Firmware version 0.9.2 is older than 0.9.3" in host.startup
        assert "ERROR" not in host.startup
        (output, _) = host.command("logic")
        assert "4826 samples (6.26MHz)" in output


def test_incompatible_firmware_reported():
    with FakeBuck50(version=(0, 8, 0)) as device, \
         HostSession(device.path) as host:
        assert "ERROR: Firmware version mismatch" in host.startup


def test_edge_mode_refused_on_old_firmware():
    with FakeBuck50(version=(0, 9, 2)) as device, \
         HostSession(device.path) as host:
        (output, _) = host.command("logic mode=edge")
        assert "needs firmware 0.9.3 or later" in output
        assert received(device, DGTL_CMD) == []


def test_edge_mode_sent_to_new_firmware():
    with FakeBuck50() as device, HostSession(device.path) as host:
        (output, _) = host.command("logic mode=edge")
        assert "samples (edge)" in output
        (command,) = received(device, DGTL_CMD)
        assert command[1] == 4       # firmware SamplingMode::EDGE


def test_logic_command_encoding():
    with FakeBuck50() as device, HostSession(device.path) as host:
        # one per command: rest of line after a trigger is its comment
        host.command("trigger 1=xxxxxx1x-0-1 then PB5 high")
        host.command("trigger 0=xxxxxxx1-1-0 first PB4 high")
        host.command("logic mode=uniform edges=100 code-mem=ram")
        (command,) = received(device, DGTL_CMD)
        (cmd, mode, num_triggers, ganged, dura_enabled, code_mem,
         duration, num_samples, pad) = struct.unpack('<6B3H', command[:12])
        assert (mode, num_triggers, ganged, dura_enabled, code_mem) \
               == (2, 2, 0, 0, 0)
        assert num_samples == 100
        # firmware Trigger: mask, pass, fail, bits
        assert command[12:16] == bytes([0x01, 1, 0, 0x01])
        assert command[16:20] == bytes([0x02, 0, 1, 0x02])


def test_dump_vcd_matches_samples(tmp_path):
    steps = [(720, 0x00), (720, 0x01), (1440, 0x03), (72, 0x02),
             (7200, 0x82), (720, 0x00), (9, 0xff), (900, 0x00)]
    with FakeBuck50() as device, HostSession(device.path) as host:
        device.digital = digital_words(steps)
        host.command("logic")
        (output, _) = host.command(  "dump output=file file=%s/capture "
                                     "digital-frmt=vcd viewer-vcd=other"
                                   % tmp_path                           )
        assert "logic samples: 0...7 of 8" in output
    (timescale, changes) = parse_vcd(tmp_path / 'capture.vcd')
    assert timescale == "$timescale 125 ns $end"
    # default per-tick=125 tick-units=ns: 9 CPU ticks per VCD time unit
    times  = []
    total  = 0
    for (ndx, (delta, bits)) in enumerate(steps):
        total += delta if ndx else 0
        times.append(int(round(total / 9)))
    # every signal gets its level at the first sample, then its changes
    expect = {'z': [(0, 0)]}
    last   = None
    for (time, (delta, bits)) in zip(times, steps):
        for bit in range(8):
            if last is None or (bits ^ last) & (1 << bit):
                expect.setdefault(str(bit), []).append((time,
                                                        (bits >> bit) & 1))
        last = bits
    assert changes == expect


def test_dump_csv_matches_samples(tmp_path):
    steps = [(720, 0x00), (720, 0x01), (1440, 0x03), (72, 0x82)]
    with FakeBuck50() as device, HostSession(device.path) as host:
        device.digital = digital_words(steps)
        host.command("logic")
        host.command(  "dump output=file file=%s/capture digital-frmt=csv "
                       "viewer-csv=other auto-digital=disabled"
                     % tmp_path                                           )
    lines = (tmp_path / 'capture.csv').read_text().splitlines()
    assert lines[0] == "time,brn,red,org,yel,grn,blu,vio,gry"
    rows = [line.split(',') for line in lines[1:]]
    assert [row[1:] for row in rows] == [list('00000000'),
                                         list('10000000'),
                                         list('11000000'),
                                         list('01000001')]
    seconds = [float(row[0]) for row in rows]
    assert seconds == pytest.approx([0, 720 / 72e6, 2160 / 72e6, 2232 / 72e6],
                                    rel=1e-5)


def test_dump_to_terminal_one_line_per_sample():
    steps = [(720, value) for value in (0x00, 0x01, 0x03, 0x02, 0x80)]
    with FakeBuck50() as device, HostSession(device.path) as host:
        device.digital = digital_words(steps)
        host.command("logic")
        (output, _) = host.command("dump output=terminal")
    rows = [line.split() for line in output.splitlines()
            if line[:5].strip().isdigit()]
    assert [(int(row[0]), row[1], row[2]) for row in rows] \
           == [(0, '00', '00000000'),
               (1, '01', '00000001'),
               (2, '03', '00000011'),
               (3, '02', '00000010'),
               (4, '80', '10000000')]
    # ticks since previous sample
    assert [int(row[-2]) for row in rows[1:]] == [720] * 4


def test_dump_range_request():
    with FakeBuck50() as device, HostSession(device.path) as host:
        host.command("logic")
        (output, _) = host.command("dump output=terminal begin=100 count=50")
        assert "logic samples: 100...149 of 4826" in output
        (command,) = received(device, UPLD_CMD)
        (cmd, pad, first, count, pad, pad) = struct.unpack('<2B2H2B', command)
        assert (first, count) == (100, 50)


def test_large_upload_arrives_complete(tmp_path):
    # far more than fits in one tty buffer or one read()
    count = 60000
    with FakeBuck50(memory=count) as device, HostSession(device.path) as host:
        device.digital = digital_words([(72, ndx & 0xff)
                                        for ndx in range(count)])
        host.command("logic")
        (output, _) = host.command(  "dump output=file file=%s/big "
                                     "digital-frmt=csv viewer-csv=other "
                                     "auto-digital=disabled"
                                   % tmp_path                            )
        assert "logic samples: 0...59999 of 60000" in output
    lines = (tmp_path / 'big.csv').read_text().splitlines()
    assert len(lines) == count + 1
    for ndx in (1, 2, 255, 256, 257, 40000, count - 1):
        bits = ''.join(lines[ndx + 1].split(',')[1:])
        assert int(bits[::-1], 2) == ndx & 0xff, ndx


def test_truncated_upload_reported():
    class Truncating(FakeBuck50):
        def _send(self, data):
            # drop the second half of sample data
            if len(data) > 1000:
                data = data[:len(data) // 2]
            super()._send(data)
        HANDLERS = dict(FakeBuck50.HANDLERS)
    Truncating.HANDLERS[UPLD_CMD] = Truncating._upload

    with Truncating() as device, HostSession(device.path) as host:
        host.command("logic")
        (output, seconds) = host.command("dump output=terminal")
        assert "Upload of 4826 samples interrupted/failed @ #2414" in output
        assert "Traceback" not in output


def test_oscope_one_channel_csv(tmp_path):
    with FakeBuck50() as device, HostSession(device.path) as host:
        # two 12 bit samples per word, low half first
        device.analog = [0x0fff0000, 0x08000400]
        (output, _) = host.command("oscope samples=4 slope=disabled "
                                   "scnd-chnl=none")
        assert "4 samples, 1 channel" in output
        host.command(  "dump output=file file=%s/scope analog-frmt=csv "
                       "viewer-csv=other auto-analog=disabled" % tmp_path)
    lines = (tmp_path / 'scope.csv').read_text().splitlines()
    assert lines[0] == "time,PA0"
    volts = [float(line.split(',')[1]) for line in lines[1:]]
    assert volts == pytest.approx([0.0, 3.3, 3.3 * 0x400 / 0xfff,
                                   3.3 * 0x800 / 0xfff], rel=1e-3)


def test_oscope_two_channel_csv(tmp_path):
    with FakeBuck50() as device, HostSession(device.path) as host:
        device.analog = [0x0fff0000, 0x00000fff, 0x08000800]
        (output, _) = host.command("oscope samples=3 slope=disabled "
                                   "trgr-chnl=0 scnd-chnl=1")
        assert "3 samples, 2 channels" in output
        host.command(  "dump output=file file=%s/scope analog-frmt=csv "
                       "viewer-csv=other auto-analog=disabled" % tmp_path)
    lines = (tmp_path / 'scope.csv').read_text().splitlines()
    assert lines[0] == "time,PA0,PA1"
    rows = [[float(field) for field in line.split(',')] for line in lines[1:]]
    assert [row[1] for row in rows] == pytest.approx([0.0, 3.3, 1.65],
                                                     rel=1e-3)
    assert [row[2] for row in rows] == pytest.approx([3.3, 0.0, 1.65],
                                                     rel=1e-3)


def test_help_mentions_edge_mode():
    with FakeBuck50() as device, HostSession(device.path) as host:
        (output, _) = host.command("help logic mode=")
        assert '"edge"' in output
        assert "NOT measured on hardware" in output
        assert "Traceback" not in output
