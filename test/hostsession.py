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

"""Drive an interactive buck50.py the way a person at a terminal does:
on a pseudo-terminal, typing a command each time the prompt appears.
(buck50.py treats input arriving while it waits for the device as "user
pressed <ENTER> to abort", so commands can't simply be piped in.)"""

import os
import pty
import select
import subprocess
import sys
import time

PROMPT = b'$1.50: '
BUCK50 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      '..', 'build', 'buck50.py')


class HostSession:
    def __init__(self, device_path, script=None, args=('-x',), timeout=20,
                 strict=True, env=None):
        self.timeout = timeout
        (self.master, slave) = pty.openpty()
        env = dict(os.environ, TERM='dumb', LINES='100000', COLUMNS='200',
                   **(env or {}))
        if strict:
            # e.g. invalid escape sequences, errors in future Pythons
            env['PYTHONWARNINGS'] = 'error::SyntaxWarning'
        self.proc = subprocess.Popen(
                        [sys.executable, script or BUCK50, *args, device_path],
                        stdin=slave, stdout=slave, stderr=slave,
                        env=env, close_fds=True, start_new_session=True)
        os.close(slave)
        self.startup = self.until_prompt()

    def until_prompt(self):
        return self.until(PROMPT)

    def send(self, text):
        os.write(self.master, text.encode())

    def until(self, ending):
        """Output up to and including ending (bytes) at end of output."""
        output   = b''
        deadline = time.time() + self.timeout
        while not output.endswith(ending):
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError("no prompt, got: %r" % output[-2000:])
            (readers, _, _) = select.select((self.master,), (), (), remain)
            if not readers:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                data = b''
            if not data:
                raise EOFError("buck50.py exited, got: %r" % output[-2000:])
            output += data
        return output.decode('utf-8', 'replace').replace('\r\n', '\n')

    def command(self, text):
        """Type one command, return what was printed up to next prompt
        and how long that took."""
        begin = time.time()
        os.write(self.master, text.encode() + b'\n')
        output = self.until_prompt()
        return (output, time.time() - begin)

    def close(self):
        try:
            os.write(self.master, b'quit\n')
            self.proc.wait(5)
        except Exception:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
