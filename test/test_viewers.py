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

"""Do the files buck50.py writes work in the waveform viewers it claims
to support?

Tests named *_structure check the files themselves and always run.  The
rest hand the files to the real programs' own parsers and are skipped
if those aren't installed (Debian/Ubuntu: apt install sigrok-cli gtkwave
gnuplot-nox):

    sigrok-cli   same libsigrok that PulseView loads files with
    vcd2fst      GTKWave's VCD reader/converter
    gnuplot      run for real, on a text terminal

Captures come from fake_buck50.py, not hardware."""

import math
import os
import shutil
import stat
import struct
import subprocess
import zipfile

import pytest

from fake_buck50 import FakeBuck50, digital_words, uart_steps
from hostsession import HostSession

MESSAGE = b'buck50 OK\n'

needs_sigrok  = pytest.mark.skipif(not shutil.which('sigrok-cli'),
                                   reason="sigrok-cli not installed")
needs_gtkwave = pytest.mark.skipif(   not shutil.which('vcd2fst')
                                   or not shutil.which('fst2vcd'),
                                   reason="gtkwave (vcd2fst) not installed")
needs_gnuplot = pytest.mark.skipif(not shutil.which('gnuplot'),
                                   reason="gnuplot not installed")


def uart_vcd(tmp_path, viewer):
    """Capture MESSAGE as 115200 baud UART on PB4, dump VCD for viewer."""
    with FakeBuck50() as device, HostSession(device.path) as host:
        device.digital = digital_words(uart_steps(MESSAGE))
        host.command("logic")
        host.command(  "dump output=file file=%s/uart_%s digital-frmt=vcd "
                       "viewer-vcd=%s auto-digital=disabled"
                     % (tmp_path, viewer, viewer)                           )
    return tmp_path / ('uart_%s.vcd' % viewer)


def scope_words(count):
    # sine on first channel/half, ramp on second
    return [    int(2047 + 2000 * math.sin(ndx / 40.0))
            | ((ndx * 2 % 4096) << 16)
            for ndx in range(count)]


def scope_dump(tmp_path, frmt, channels, count=500):
    """Capture count words of scope_words(), dump as frmt."""
    with FakeBuck50() as device, HostSession(device.path) as host:
        device.analog = scope_words(count)
        host.command(  "oscope samples=%d slope=disabled trgr-chnl=0 "
                       "scnd-chnl=%s s/h+adc=1.5"
                     % (count * (3 - channels), '1' if channels == 2
                                                    else 'none')      )
        host.command(  "dump output=file file=%s/scope analog-frmt=%s "
                       "auto-analog=disabled" % (tmp_path, frmt)        )
    return tmp_path / ('scope.%s' % frmt)


def volts(adc):
    return 3.3 * adc / 4095


def vcd_times_and_first_values(path):
    times = []
    first = []
    for line in path.read_text().splitlines():
        if line.startswith('#'):
            times.append(int(line[1:]))
        elif len(times) == 1 and not line.startswith('$'):
            first.append(line)
    return (times, first)


# file structure
#

def test_vcd_for_pulseview_structure(tmp_path):
    text = uart_vcd(tmp_path, 'pulseview').read_text()
    # non-standard, but sigrok takes its sample rate (so memory use) from it
    assert "$timescale 125 ns $end" in text


def test_vcd_for_gtkwave_structure(tmp_path):
    (pv_times, _) = vcd_times_and_first_values(uart_vcd(tmp_path,
                                                        'pulseview'))
    path          = uart_vcd(tmp_path, 'gtkwave')
    (times, _)    = vcd_times_and_first_values(path)
    # IEEE 1364: timescale number is 1, 10, or 100
    assert "$timescale 1 ns $end" in path.read_text()
    # same instants (pulseview file has one extra, repeated, final edge)
    assert times == [125 * time for time in pv_times[:-1]]


@pytest.mark.parametrize('viewer', ['pulseview', 'gtkwave', 'other'])
def test_vcd_initial_values_structure(tmp_path, viewer):
    (times, first) = vcd_times_and_first_values(uart_vcd(tmp_path, viewer))
    assert times[0] == 0
    # every declared signal, not only the ones which are high or change
    assert sorted(first) == sorted(['0z', '10', '01', '02', '03', '04',
                                    '05', '06', '07'])


def test_csv_times_keep_resolution_structure(tmp_path):
    # edges 9 ticks (125 ns) apart, 2 seconds into a capture
    steps = [(720, 0)] + [(0xffffff - 1000, 1), (0xffffff - 1000, 0)] * 5 \
          + [(9, 1), (9, 0), (9, 1)]
    with FakeBuck50() as device, HostSession(device.path) as host:
        device.digital = digital_words(steps)
        host.command("logic")
        host.command(  "dump output=file file=%s/late digital-frmt=csv "
                       "viewer-csv=other auto-digital=disabled" % tmp_path)
    rows  = (tmp_path / 'late.csv').read_text().splitlines()[1:]
    times = [float(row.split(',')[0]) for row in rows]
    assert times[-1] > 2.0
    for (earlier, later) in zip(times[-4:], times[-3:]):
        assert later - earlier == pytest.approx(125e-9, abs=2e-9)


@pytest.mark.parametrize('channels', [1, 2])
def test_sr_structure(tmp_path, channels):
    count = 500
    path  = scope_dump(tmp_path, 'sr', channels, count)
    words = scope_words(count)
    with zipfile.ZipFile(path) as srzip:
        names    = srzip.namelist()
        metadata = srzip.read('metadata').decode().splitlines()
        assert srzip.read('version') == b'2'
        datas = [srzip.read('analog-1-%d-1' % (num + 1))
                 for num in range(channels)]
    assert names[:2] == ['version', 'metadata']
    # 12 MHz ADC clock / (1.5 + 12.5) clocks per conversion
    assert "samplerate=857143"          in metadata
    assert "total analog=%d" % channels in metadata
    assert "analog1=PA0"                in metadata
    values = [list(struct.unpack('<%df' % (len(data) // 4), data))
              for data in datas]
    if channels == 2:
        assert "analog2=PA1" in metadata
        assert values[0] == pytest.approx([volts(word & 0xffff)
                                           for word in words], abs=1e-4)
        assert values[1] == pytest.approx([volts(word >> 16)
                                           for word in words], abs=1e-4)
    else:
        # one channel: two consecutive samples per word, low half first
        expect = []
        for word in words:
            expect += [volts(word & 0xffff), volts(word >> 16)]
        assert values[0] == pytest.approx(expect, abs=1e-4)


def test_analog_vcd_structure(tmp_path):
    count = 500
    path  = scope_dump(tmp_path, 'vcd', 2, count)
    lines = path.read_text().splitlines()
    assert "$timescale 1 ns $end"     in lines
    assert "$var real 64 0 PA0 $end"  in lines
    assert "$var real 64 1 PA1 $end"  in lines
    words  = scope_words(count)
    values = [float(line[1:].split()[0]) for line in lines
              if line.startswith('r') and line.endswith(' 0')]
    # (unchanged values aren't repeated)
    expect = []
    for word in words:
        value = volts(word & 0xffff)
        if not expect or abs(value - expect[-1]) > 1e-12:
            expect.append(value)
    assert values == pytest.approx(expect, abs=1e-6)
    times = [int(line[1:]) for line in lines if line.startswith('#')]
    # 14 clocks of 12 MHz per sample
    assert times[1] == 1167
    assert times[-1] == int(round(count * 14 / 12e6 * 1e9))
    # companion gtkwave save file: analog display of both signals
    gtkw = (tmp_path / 'scope.gtkw').read_text()
    assert "top.PA0" in gtkw and "top.PA1" in gtkw
    assert gtkw.count("@88022") == 2


# the viewers' own parsers
#

def sigrok(*args):
    result = subprocess.run(('sigrok-cli',) + args, capture_output=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.decode()


@needs_sigrok
@pytest.mark.parametrize('viewer', ['pulseview', 'gtkwave', 'other'])
def test_sigrok_decodes_uart_from_vcd(tmp_path, viewer):
    output = sigrok('-i', str(uart_vcd(tmp_path, viewer)), '-I', 'vcd',
                    '-P', 'uart:rx=brn:baudrate=115200',
                    '-A', 'uart=rx-data')
    decoded = bytes(int(line.split(':')[1], 16)
                    for line in output.splitlines()
                    if line.startswith('uart-1:'))
    assert decoded == MESSAGE


@needs_sigrok
@pytest.mark.parametrize('channels', [1, 2])
def test_sigrok_reads_sr(tmp_path, channels):
    count  = 500
    path   = scope_dump(tmp_path, 'sr', channels, count)
    output = sigrok('-i', str(path), '--show')
    assert "Samplerate: 857143" in output
    assert "- PA0: analog"      in output
    assert ("- PA1: analog" in output) == (channels == 2)
    assert (  "Analog sample count: %d" % (count * (3 - channels))
            in output                                             )
    # have sigrok load it and save it again with its own writer: same
    #   samples must come out
    again = tmp_path / 'again.sr'
    sigrok('-i', str(path), '-o', str(again))
    with zipfile.ZipFile(path) as ours, zipfile.ZipFile(again) as theirs:
        metadata = theirs.read('metadata').decode()
        assert "total analog=%d" % channels in metadata
        assert "analog1=PA0"                in metadata
        for num in range(1, channels + 1):
            name = 'analog-1-%d-1' % num
            assert theirs.read(name) == ours.read(name)


@needs_gtkwave
def test_gtkwave_converter_reads_digital_vcd(tmp_path):
    path = uart_vcd(tmp_path, 'gtkwave')
    (times, _) = vcd_times_and_first_values(path)
    fst = tmp_path / 'uart.fst'
    subprocess.run(['vcd2fst', str(path), str(fst)], check=True,
                   capture_output=True, timeout=120)
    back = subprocess.run(['fst2vcd', str(fst)], check=True,
                          capture_output=True, timeout=120).stdout.decode()
    lines = back.splitlines()
    # vcd2fst only understands standard timescales: wrong times otherwise
    assert [int(line[1:]) for line in lines if line.startswith('#')][-1] \
           == times[-1]
    begin = lines.index('$dumpvars')
    end   = lines.index('$end', begin)
    assert end - begin - 1 == 9          # all signals defined from time 0


@needs_gtkwave
def test_gtkwave_converter_reads_analog_vcd(tmp_path):
    path = scope_dump(tmp_path, 'vcd', 2)
    fst  = tmp_path / 'scope.fst'
    subprocess.run(['vcd2fst', str(path), str(fst)], check=True,
                   capture_output=True, timeout=120)
    back = subprocess.run(['fst2vcd', str(fst)], check=True,
                          capture_output=True, timeout=120).stdout.decode()
    assert "$var real 64" in back
    assert sum(line.startswith('r') for line in back.splitlines()) > 400


@needs_gnuplot
@pytest.mark.parametrize('kind', ['logic', 'oscope'])
def test_gnuplot_plots_csv(tmp_path, kind):
    with FakeBuck50() as device, \
         HostSession(device.path, env={'GNUTERM': 'dumb'}) as host:
        device.digital = digital_words(uart_steps(MESSAGE))
        device.analog  = scope_words(200)
        host.command("configure dump output=file file=%s/plot "
                     "digital-frmt=csv analog-frmt=csv viewer-csv=gnuplot "
                     "auto-digital=enabled auto-analog=enabled" % tmp_path)
        if kind == 'logic':
            host.command("logic")
        else:
            host.command("oscope samples=200 slope=disabled trgr-chnl=0 "
                         "scnd-chnl=1")
        # gnuplot draws, then waits at its "pause"
        host.send("dump\n")
        plot = host.until(b'for help ...  ')
        assert "Viewer command failed" not in plot
        assert "line 0:"               not in plot     # gnuplot's errors
        assert "warning"               not in plot.lower()
        assert plot.count('\n') > 20                   # an ascii-art plot
        host.send("\n")
        host.until_prompt()


# launching
#

def fake_viewers(tmp_path, names):
    """Directory of stand-in viewer programs which log how they're run."""
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'launched.log'
    for name in names:
        program = bindir / name
        program.write_text('#!/bin/sh\necho "%s $*" >> "%s"\n' % (name, log))
        program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return (str(bindir), log)


def test_found_pulseview_gets_formats_it_can_open(tmp_path):
    (bindir, log) = fake_viewers(tmp_path, ['pulseview'])
    with FakeBuck50() as device, \
         HostSession(device.path, args=(), env={'PATH': bindir}) as host:
        assert "analog-frmt=sr" in host.startup
        device.analog = scope_words(100)
        host.command("configure dump file=%s/temp" % tmp_path)
        host.command("logic")
        host.command("oscope samples=100 slope=disabled")
    assert log.read_text().splitlines() \
           == ["pulseview %s/temp.vcd" % tmp_path,
               "pulseview %s/temp.sr"  % tmp_path]
    assert zipfile.is_zipfile(tmp_path / 'temp.sr')


def test_found_gtkwave_gets_formats_it_can_open(tmp_path):
    (bindir, log) = fake_viewers(tmp_path, ['gtkwave'])
    with FakeBuck50() as device, \
         HostSession(device.path, args=(), env={'PATH': bindir}) as host:
        assert "analog-frmt=vcd" in host.startup
        device.analog = scope_words(100)
        host.command("configure dump file=%s/temp" % tmp_path)
        host.command("logic")
        digital = (tmp_path / 'temp.vcd').read_text()
        host.command("oscope samples=100 slope=disabled")
    assert log.read_text().splitlines() \
           == ["gtkwave %s/temp.vcd" % tmp_path,
               "gtkwave %s/temp.vcd %s/temp.gtkw" % (tmp_path, tmp_path)]
    assert "$timescale 1 ns $end" in digital
    assert "$var real" in (tmp_path / 'temp.vcd').read_text()


def test_viewer_that_cant_open_format_is_not_run(tmp_path):
    (bindir, log) = fake_viewers(tmp_path, ['pulseview', 'gtkwave'])
    with FakeBuck50() as device, \
         HostSession(device.path, env={'PATH': bindir}) as host:
        device.analog = scope_words(100)
        host.command("oscope samples=100 slope=disabled")
        (output, _) = host.command(
                        "dump output=file file=%s/temp analog-frmt=sr "
                        "viewer-vcd=gtkwave auto-analog=enabled" % tmp_path)
        assert 'use "analog-frmt=vcd"' in output
        (output, _) = host.command("dump analog-frmt=vcd "
                                   "viewer-vcd=pulseview")
        assert 'use "analog-frmt=sr"' in output
    assert not log.exists()
