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

"""buck50.py run non-interactively (--command, --script, piped stdin),
and finding the device without being told where it is."""

import importlib.util
import os
import pty
import subprocess
import sys
import time

import pytest

from fake_buck50 import FakeBuck50, DGTL_CMD, digital_words
from hostsession import BUCK50


def run(device_path, *args, stdin=subprocess.DEVNULL, input=None,
        timeout=60):
    return subprocess.run([sys.executable, '-W', 'error::SyntaxWarning',
                           BUCK50, '-x', '-q', *args, device_path],
                          stdin=None if input is not None else stdin,
                          input=input, capture_output=True, text=True,
                          timeout=timeout)


def received(device, code):
    return [data for (cmd, data) in device.commands if cmd == code]


STEPS = [(720, 0x00), (720, 0x01), (1440, 0x03), (72, 0x02)]

DUMP_CSV = ("dump output=file file=%s/capture digital-frmt=csv "
            "viewer-csv=other auto-digital=disabled")


def csv_bits(path):
    rows = path.read_text().splitlines()[1:]
    return [''.join(row.split(',')[1:]) for row in rows]


def test_commands_option(tmp_path):
    with FakeBuck50() as device:
        device.digital = digital_words(STEPS)
        result = run(device.path, '-c', 'logic edges=100',
                                  '-c', DUMP_CSV % tmp_path)
    assert result.returncode == 0, result.stderr
    assert "$1.50: logic edges=100" in result.stdout
    assert "4 samples (6.26MHz)"    in result.stdout
    assert csv_bits(tmp_path / 'capture.csv') == ['00000000', '10000000',
                                                  '11000000', '01000000']


def test_piped_commands(tmp_path):
    with FakeBuck50() as device:
        device.digital = digital_words(STEPS)
        result = run(device.path,
                     input="# a comment\n\nlogic\n%s\n" % (DUMP_CSV
                                                            % tmp_path))
    assert result.returncode == 0, result.stderr
    assert len(csv_bits(tmp_path / 'capture.csv')) == 4


def test_script_file_after_commands(tmp_path):
    script = tmp_path / 'capture.b50cmds'
    script.write_text("logic   # capture\n" + DUMP_CSV % tmp_path + "\n")
    with FakeBuck50() as device:
        device.digital = digital_words(STEPS)
        result = run(device.path, '-c', 'logic mode=uniform',
                     '-s', str(script))
        (first, second) = received(device, DGTL_CMD)
    assert result.returncode == 0, result.stderr
    assert first[1] == 2 and second[1] == 2      # mode=uniform kept
    assert (tmp_path / 'capture.csv').exists()


def test_error_stops_script():
    with FakeBuck50() as device:
        result = run(device.path, '-c', 'logic mode=bogus', '-c', 'logic')
        assert received(device, DGTL_CMD) == []
    assert result.returncode == 1
    assert "Stopping: error in --command #1" in result.stderr


def test_unknown_command_stops_script():
    with FakeBuck50() as device:
        result = run(device.path, '-c', 'frobnicate', '-c', 'logic')
        assert received(device, DGTL_CMD) == []
    assert result.returncode == 1


def test_keep_going():
    with FakeBuck50() as device:
        result = run(device.path, '-k', '-c', 'logic mode=bogus',
                                        '-c', 'logic')
        assert len(received(device, DGTL_CMD)) == 1
    assert result.returncode == 1          # but still reports the failure


def test_existing_file_kept_unless_yes(tmp_path):
    target = tmp_path / 'capture.csv'
    target.write_text("precious\n")
    with FakeBuck50() as device:
        device.digital = digital_words(STEPS)
        kept = run(device.path, '-c', 'logic', '-c', DUMP_CSV % tmp_path)
        assert target.read_text() == "precious\n"
        overwritten = run(device.path, '-y', '-c', 'logic',
                          '-c', DUMP_CSV % tmp_path)
    assert "keeping (non-interactive, see --yes)" in kept.stdout
    assert kept.returncode == 1            # "Not overwriting" is an error
    assert overwritten.returncode == 0, overwritten.stderr
    assert len(csv_bits(target)) == 4


def test_long_terminal_dump_does_not_page():
    with FakeBuck50() as device:
        result = run(device.path, '-c', 'logic', '-c', 'dump output=terminal')
    assert result.returncode == 0, result.stderr
    assert "logic samples: 0...4825 of 4826" in result.stdout
    assert "<ENTER> to continue" not in result.stdout
    assert result.stdout.count('\n') > 4826


def test_unresponsive_device_gives_up():
    (master, slave) = pty.openpty()      # nothing ever answers
    try:
        begin  = time.time()
        result = run(os.ttyname(slave), '-c', 'logic', timeout=60)
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == 1
    assert "No response from buck50 device" in result.stderr
    assert time.time() - begin < 30


# device discovery
#

@pytest.fixture(scope='module')
def buck50():
    spec   = importlib.util.spec_from_file_location('buck50_module', BUCK50)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_sysfs(root, ports):
    """ports: {n: (idVendor, idProduct, serial)} -> root/class/tty"""
    tty = root / 'class' / 'tty'
    tty.mkdir(parents=True)
    (tty / 'ttyS0').mkdir()               # not ACM, ignored
    for (num, (vendor, product, serial)) in ports.items():
        usb = root / 'devices' / ('1-%d' % num)
        interface = usb / ('1-%d:1.0' % num)
        interface.mkdir(parents=True)
        (usb / 'idVendor' ).write_text(vendor  + '\n')
        (usb / 'idProduct').write_text(product + '\n')
        if serial:
            (usb / 'serial').write_text(serial + '\n')
        (tty / ('ttyACM%d' % num)).mkdir()
        os.symlink(interface, tty / ('ttyACM%d' % num) / 'device')
    return str(tty)


def test_find_devices(buck50, tmp_path):
    sysfs = fake_sysfs(tmp_path, {10: ('0483', '5740', 'CCCC'),
                                   2: ('0483', '5740', 'BBBB'),
                                   0: ('1a86', '7523', 'XXXX'),  # CH340
                                   1: ('0483', '5740', None    )})
    assert buck50.find_devices(sysfs) == [('/dev/ttyACM1' , ''    ),
                                          ('/dev/ttyACM2' , 'BBBB'),
                                          ('/dev/ttyACM10', 'CCCC')]
    assert buck50.find_devices(str(tmp_path / 'missing')) == []


def test_choose_device(buck50, capsys):
    devices = [('/dev/ttyACM1', 'AAAA'), ('/dev/ttyACM2', 'BBBB')]
    assert buck50.choose_device(None   , True, []     ) == '/dev/ttyACM0'
    assert buck50.choose_device(None   , True, devices) == '/dev/ttyACM1'
    assert buck50.choose_device('BBBB' , True, devices) == '/dev/ttyACM2'
    assert buck50.choose_device('ACM5' , True, devices) == '/dev/ttyACM5'
    assert buck50.choose_device('ttyACM5', True, []   ) == '/dev/ttyACM5'
    assert buck50.choose_device('/dev/buck50-AAAA', True, devices) \
           == '/dev/buck50-AAAA'
    buck50.choose_device(None, False, devices)
    assert "Found 2 possible buck50 devices" in capsys.readouterr().out
