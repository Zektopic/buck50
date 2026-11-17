#!/usr/bin/env python3

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

"""A stand-in for a buck50 "Blue Pill", for trying and testing buck50.py
without hardware.

Speaks the firmware's USB CDC-ACM protocol over a pseudo-terminal, for
the commands needed to connect, capture ("logic", "oscope") and upload
("dump").  It does not model signals: captures return whatever samples
it was given, or a synthetic pattern.

    $ ./fake_buck50.py
    buck50.py -x /dev/pts/7       <-- run that in another terminal
"""

import os
import pty
import select
import struct
import sys
import threading
import tty

IDENTITY  = 0xea017af5
SIGNATURE = bytes([0xf2,
                   0x9e, 0xc4, 0xaa, 0xdf,
                   0xd8, 0xca, 0x8f, 0xbd,
                   0xbe, 0xa9, 0xfe, 0x83,
                   0x99, 0xd1, 0xae, 0xeb])

# Command codes, see namespace Command in buck50.cxx
IDNT_CMD =  1
VERS_CMD =  2
RSET_CMD =  3
HALT_CMD =  5
DGTL_CMD =  6
ANLG_CMD =  7
UPLD_CMD =  8
FLSH_CMD = 14
SRNO_CMD = 16
BLNK_CMD = 17
SIGN_CMD = 0xf2

HALT_MEMORY = 1

MODE_ANALOG = 0x0f

IN_PROG_TRIGGERED = 0x0800
IN_PROG_SAMPLING  = 0x1000
IN_PROG_ANALOG    = 0x0400

USB_PACKET = 64


def digital_words(steps, systick_start=0xffffff):
    """Sample words as the firmware stores them, from (ticks since
    previous sample, PB11...PB4 levels) pairs."""
    words = []
    tick  = systick_start
    for (delta, bits) in steps:
        tick = (tick - delta) & 0xffffff
        words.append(((bits & 0xff) << 24) | tick)
    return words


def uart_steps(data, ticks_per_bit=625, bit=0, lead=7200):
    """(delta ticks, levels) steps of bytes sent 8N1 on one input, idle
    high.  Default 625 ticks/bit is 115200 baud at 72 MHz.  Ends with a
    marker edge on PB11 so the last stop bit has a duration."""
    steps = [(720, 1 << bit)]
    bits  = []
    for byte in data:
        bits += [0] + [(byte >> num) & 1 for num in range(8)] + [1]
    bits += [1] * 20
    (level, run) = (1, lead)
    for value in bits:
        if value != level:
            steps.append((run, value << bit))
            (level, run) = (value, 0)
        run += ticks_per_bit
    steps.append((run, (1 << bit) | 0x80))
    return steps


def default_digital(count):
    # 8 bit counter, one count per 720 ticks (10 microseconds)
    return digital_words([(720, ndx & 0xff) for ndx in range(count)])


class FakeBuck50:
    def __init__(self                    ,
                 version=(0, 9, 3)       ,
                 serial=b'FAKE0BUCK50'   ,
                 memory=4826             ):
        self.version   = tuple(version)
        self.serial    = serial
        self.memory    = memory      # sample memory, 32 bit words
        self.digital   = None        # words to "capture", None = synthetic
        self.analog    = None        #   "    "      "
        self.commands  = []          # (code, raw bytes) received
        self.sent      = 0           # bytes sent to host
        # state as firmware's globals
        self.samples        = []
        self.sampling_mode  = 0xff
        self.num_adc_chans  = 0
        self.adc_chan_ndxs  = 0
        self.adc_samp_rate  = 0
        self.num_adc_words  = 0
        (self.master, self.slave) = pty.openpty()
        tty.setraw(self.master)
        tty.setraw(self.slave )
        self.path     = os.ttyname(self.slave)
        self._buffer  = b''
        self._stop    = os.pipe()
        self._thread  = None

    # threading
    #
    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        os.write(self._stop[1], b'x')
        if self._thread:
            self._thread.join(5)
        for fd in (self.master, self.slave) + self._stop:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *unused):
        self.stop()

    # I/O
    #
    def _need(self, count):
        """Block until count bytes buffered.  False if stopped."""
        while len(self._buffer) < count:
            (readers, _, _) = select.select((self.master, self._stop[0]),
                                            (), ())
            if self._stop[0] in readers:
                return False
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return False
            if not data:
                return False
            self._buffer += data
        return True

    def _take(self, count):
        (data, self._buffer) = (self._buffer[:count], self._buffer[count:])
        return data

    def _send(self, data):
        # in USB packet sized pieces, as firmware does
        for ndx in range(0, len(data), USB_PACKET):
            os.write(self.master, data[ndx:ndx + USB_PACKET])
        self.sent += len(data)

    # protocol
    #
    def run(self):
        while self._need(1):
            code = self._buffer[0]
            handler = self.HANDLERS.get(code)
            if handler is None:
                self._take(1)       # as firmware: flush and ignore
                continue
            if handler(self) is False:
                return

    def _fixed(self, length):
        if not self._need(length):
            return None
        data = self._take(length)
        self.commands.append((data[0], data))
        return data

    def _signature(self):
        # firmware matches byte by byte; host pads to 20
        if not self._need(20):
            return False
        data = self._take(20)
        self.commands.append((SIGN_CMD, data))
        if data[:len(SIGNATURE)] == SIGNATURE:
            self._send(struct.pack('<I', IDENTITY))

    def _identity(self):
        if self._fixed(4) is None: return False
        self._send(struct.pack('<I', IDENTITY))

    def _version(self):
        if self._fixed(4) is None: return False
        self._send(bytes(self.version))

    def _serial(self):
        if self._fixed(4) is None: return False
        self._send(bytes([len(self.serial)]) + self.serial)

    def _ignore4(self):
        if self._fixed(4) is None: return False

    def _digital(self):
        head = self._fixed(12)
        if head is None: return False
        (cmd, mode, num_triggers, ganged, dura_enabled, code_mem,
         duration, num_samples, pad) = struct.unpack('<6B3H', head)
        if not self._need(4 * num_triggers): return False
        triggers = self._take(4 * num_triggers)
        self.commands[-1] = (cmd, head + triggers)
        num_samples = max(num_samples, 3)
        limit       = min(num_samples, self.memory)
        words       = self.digital
        if words is None:
            words = default_digital(limit)
        self.samples       = list(words[:limit])
        self.sampling_mode = mode
        self._send(struct.pack('<BBHH'                             ,
                               mode                                ,
                               HALT_MEMORY                         ,
                               IN_PROG_TRIGGERED | IN_PROG_SAMPLING,
                               len(self.samples)                   ))

    def _analog(self):
        head = self._fixed(12)
        if head is None: return False
        (cmd, trigger_chan, second_chan, slope, rate, ganged,
         num_words, level_lo, level_hi) = struct.unpack('<6B3H', head)
        num_chans = 1 if second_chan > 7 else 2
        num_words = min(num_words, self.memory)
        words     = self.analog
        if words is None:
            # sawtooths
            words = [    ((ndx * 37) & 0xfff)
                     | (((ndx * 53) & 0xfff) << 16)
                     for ndx in range(num_words)]
        self.samples       = list(words[:num_words])
        self.sampling_mode = MODE_ANALOG
        self.num_adc_chans = num_chans
        self.adc_chan_ndxs = (trigger_chan & 0xf) | ((second_chan & 0xf) << 4)
        self.adc_samp_rate = rate
        self.num_adc_words = len(self.samples)
        self._send(struct.pack('<4B2H'           ,
                               HALT_MEMORY       ,
                               num_chans         ,
                               self.adc_chan_ndxs,
                               rate              ,
                               self.num_adc_words,
                                 IN_PROG_ANALOG
                               | IN_PROG_TRIGGERED
                               | IN_PROG_SAMPLING))

    def _upload(self):
        head = self._fixed(8)
        if head is None: return False
        (cmd, pad, first, count, pad, pad) = struct.unpack('<2B2H2B', head)
        if self.sampling_mode == MODE_ANALOG and self.num_adc_chans == 1:
            first >>= 1
            count >>= 1
        first = min(first, len(self.samples))
        last  = min(first + count, len(self.samples))
        words = self.samples[first:last]
        self._send(struct.pack('<4H4BH'          ,
                               first             ,
                               len(words)        ,
                               len(self.samples) ,
                               self.memory       ,
                               self.sampling_mode,
                               self.num_adc_chans,
                               self.adc_chan_ndxs,
                               self.adc_samp_rate,
                               self.num_adc_words))
        if words:
            self._send(struct.pack('<%dI' % len(words), *words))

    HANDLERS = {
        SIGN_CMD : _signature,
        IDNT_CMD : _identity ,
        VERS_CMD : _version  ,
        SRNO_CMD : _serial   ,
        HALT_CMD : _ignore4  ,
        RSET_CMD : _ignore4  ,
        FLSH_CMD : _ignore4  ,
        BLNK_CMD : _ignore4  ,
        DGTL_CMD : _digital  ,
        ANLG_CMD : _analog   ,
        UPLD_CMD : _upload   ,
    }


if __name__ == '__main__':
    device = FakeBuck50()
    sys.stdout.write(  "Fake buck50 device on %s -- run:\n"
                       "    buck50.py -x %s\n"
                       "<CTRL-C> here to exit ...\n"
                     % (device.path, device.path)          )
    try:
        device.run()
    except KeyboardInterrupt:
        pass
