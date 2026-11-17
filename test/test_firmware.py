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

"""Functional tests of the logic analyzer capture assembly, run under
emulation (see fwemu.py).  They check what gets stored, not real-world
timing: emulated time comes from a cost model, not from hardware."""

import os

import pytest

pytest.importorskip('unicorn')
pytest.importorskip('elftools')

import fwemu

ELF = os.environ.get('BUCK50_ELF',
                     os.path.join(os.path.dirname(__file__),
                                  '..', 'build', 'buck50.elf'))

CLASSIC_MODES = ['6.26MHz', 'irregular', 'uniform', '4MHz']
ALL_MODES     = CLASSIC_MODES + ['edge']
CODE_MEMS     = [fwemu.CODE_MEM_FLASH, fwemu.CODE_MEM_RAM]
MODELS        = list(fwemu.CycleModel.variants().items())


@pytest.fixture(scope='module')
def firmware():
    return fwemu.Firmware(ELF)


def read_gaps(capture):
    """Cycles between consecutive GPIOB reads made by the sampling loop
    proper (identified by address, so the trigger loop and the one-off
    reads around triggering aren't included)."""
    return [b - a for (a, b) in zip(capture.sampling_reads,
                                    capture.sampling_reads[1:])]


def run(firmware, waveform, mode, **kwargs):
    if not firmware.has_mode(mode):
        pytest.skip("firmware has no \"%s\" mode" % mode)
    capture = firmware.capture(waveform, mode=mode, **kwargs)
    low  = firmware.symbols['sampling']
    high = firmware.symbols['sampling_end']
    ram  = firmware.symbols['STORAGE']
    capture.sampling_reads = [
        cycle
        for ((cycle, value), pc) in zip(capture.reads, capture.read_pcs)
        if low <= pc < high or (ram <= pc < fwemu.END_OF_RAM
                                and kwargs.get('code_mem') ==
                                    fwemu.CODE_MEM_RAM
                                and capture.in_progress & fwemu.SAMPLING
                                and cycle > capture.reads[1][0])]
    return capture


# slow enough that every mode must see every level
SLOW_STEPS = [(5000, 0x01), (9000, 0x00), (9400, 0xff), (9800, 0x0f),
              (10200, 0xf0), (10600, 0x55), (11000, 0xaa), (11400, 0x80)]


@pytest.mark.parametrize('code_mem', CODE_MEMS)
@pytest.mark.parametrize('mode', ALL_MODES)
def test_stores_exactly_the_changes(firmware, mode, code_mem):
    wave    = fwemu.Waveform(SLOW_STEPS)
    capture = run(firmware, wave, mode, code_mem=code_mem, max_cycles=20000)

    assert capture.halt == 'cycles'
    assert capture.in_progress & fwemu.SAMPLING
    assert capture.in_progress & fwemu.TRIGGERED
    assert not capture.in_progress & fwemu.TRIGGERING
    # "triggered at" sample, sync sample, then one sample per change
    assert capture.bits() == [0x00, 0x00] + [value for (t, value) in SLOW_STEPS]


@pytest.mark.parametrize('code_mem', CODE_MEMS)
@pytest.mark.parametrize('mode', ALL_MODES)
def test_timestamps_follow_the_input(firmware, mode, code_mem):
    wave    = fwemu.Waveform(SLOW_STEPS)
    capture = run(firmware, wave, mode, code_mem=code_mem, max_cycles=20000)
    times   = capture.times()[2:]
    edges   = [t for (t, value) in SLOW_STEPS]
    # elapsed time between captured changes matches the input to within
    # the two polls involved (whatever the model says a poll costs)
    slack = 2 * 25
    for ndx in range(1, len(edges)):
        want = edges[ndx] - edges[ndx - 1]
        got  = times[ndx] - times[ndx - 1]
        assert abs(got - want) <= slack, (ndx, want, got)


@pytest.mark.parametrize('code_mem', CODE_MEMS)
@pytest.mark.parametrize('mode', ALL_MODES)
def test_stops_when_sample_memory_full(firmware, mode, code_mem):
    # toggles forever, far slower than any mode polls
    steps   = [(1000 + 200 * n, n & 0xff) for n in range(1, 6000)]
    capture = run(firmware, fwemu.Waveform(steps), mode,
                  code_mem=code_mem, max_cycles=3000000)
    assert capture.halt == 'memory'
    assert len(capture.words) == capture.max_samples
    if code_mem == fwemu.CODE_MEM_FLASH:
        ram_free = fwemu.END_OF_RAM - firmware.symbols['STORAGE']
        assert capture.max_samples == ram_free // 4
    else:
        # running from RAM costs the sample memory the code is copied into
        assert 0 < capture.max_samples < (  fwemu.END_OF_RAM
                                          - firmware.symbols['STORAGE']) // 4
    bits = capture.bits()
    assert all(a != b for (a, b) in zip(bits[1:], bits[2:]))


@pytest.mark.parametrize('code_mem', CODE_MEMS)
@pytest.mark.parametrize('mode', ALL_MODES)
def test_honors_requested_number_of_samples(firmware, mode, code_mem):
    steps   = [(1000 + 200 * n, n & 0xff) for n in range(1, 400)]
    capture = run(firmware, fwemu.Waveform(steps), mode, code_mem=code_mem,
                  num_samples=100, max_cycles=400000)
    assert capture.halt == 'memory'
    assert len(capture.words) == 100


@pytest.mark.parametrize('mode', ALL_MODES)
def test_waits_for_trigger(firmware, mode):
    # state 0: wait for PB4 high, then state 1: wait for PB5 high, then go
    triggers = [fwemu.trigger_word(0x01, 0x01, 1, 0),
                fwemu.trigger_word(0x02, 0x02, 0, 1)]
    wave = fwemu.Waveform([(3000, 0x04), (4000, 0x05), (5000, 0x04),
                           (6000, 0x06), (7000, 0x00), (7500, 0x10)])
    capture = run(firmware, wave, mode, triggers=triggers, max_cycles=12000)
    assert capture.in_progress & fwemu.TRIGGERED
    assert capture.in_progress & 0xff == 1     # state number triggered at
    # nothing before PB5 rose is recorded
    assert capture.bits() == [0x06, 0x06, 0x00, 0x10]


@pytest.mark.parametrize('mode', ALL_MODES)
def test_never_triggered_gives_no_samples(firmware, mode):
    triggers = [fwemu.trigger_word(0x80, 0x80, 0, 0)]   # PB11 high
    wave     = fwemu.Waveform([(3000, 0x01), (4000, 0x7f)])
    capture  = run(firmware, wave, mode, triggers=triggers, max_cycles=8000)
    assert capture.halt == 'cycles'
    assert capture.in_progress & fwemu.TRIGGERING
    assert not capture.in_progress & fwemu.SAMPLING
    assert capture.words == []


@pytest.mark.parametrize('code_mem', CODE_MEMS)
@pytest.mark.parametrize('mode', ALL_MODES)
def test_ganged_triggers_on_own_condition(firmware, mode, code_mem):
    triggers = [fwemu.trigger_word(0x01, 0x01, 0, 0)]   # PB4 high
    wave     = fwemu.Waveform([(4000, 0x01), (6000, 0x03), (8000, 0x02)])
    capture  = run(firmware, wave, mode, code_mem=code_mem, ganged=True,
                   triggers=triggers, max_cycles=12000)
    assert capture.in_progress & fwemu.TRIGGERED
    assert not capture.in_progress & fwemu.EXTERN_TRIG
    assert capture.bits() == [0x01, 0x01, 0x03, 0x02]


@pytest.mark.parametrize('mode', ALL_MODES)
def test_ganged_triggers_when_other_unit_does(firmware, mode):
    triggers = [fwemu.trigger_word(0x80, 0x80, 0, 0)]   # PB11 high: never
    wave     = fwemu.Waveform([(6000, 0x03), (8000, 0x02)])
    capture  = run(firmware, wave, mode, ganged=True, triggers=triggers,
                   extern_trigger_at=4000, max_cycles=12000)
    assert capture.in_progress & fwemu.TRIGGERED
    assert capture.in_progress & fwemu.EXTERN_TRIG
    assert capture.bits() == [0x00, 0x00, 0x03, 0x02]


def test_unmodified_modes_match_measured_cycle_counts(firmware):
    """The cost model is only worth anything if it reproduces what the
    author measured on hardware for the flash-resident loops."""
    measured = {'6.26MHz'  : [11, 11, 11, 11, 11, 14],
                'irregular': [11, 14],
                'uniform'  : [15],
                '4MHz'     : [18]}
    for (name, model) in MODELS:
        for (mode, pattern) in measured.items():
            capture = run(firmware, fwemu.Waveform([]), mode,
                          model=model, max_cycles=3000)
            gaps = read_gaps(capture)
            assert len(gaps) > 3 * len(pattern)
            assert sorted(set(gaps)) == sorted(set(pattern)), (name, mode)
            window = gaps[:len(pattern)]
            assert sorted(window) == sorted(pattern), (name, mode, window)


@pytest.mark.parametrize('name,model', MODELS)
def test_edge_mode_polls_faster_when_idle(firmware, name, model):
    """Model estimate, not a hardware measurement."""
    idle = {}
    for mode in ('6.26MHz', 'edge'):
        capture    = run(firmware, fwemu.Waveform([]), mode,
                         model=model, max_cycles=6000)
        gaps       = read_gaps(capture)
        idle[mode] = (sum(gaps) / len(gaps), max(gaps))
    (classic_avg, classic_max) = idle['6.26MHz']
    (edge_avg   , edge_max   ) = idle['edge']
    assert edge_avg < 0.7 * classic_avg
    assert edge_max < classic_max


@pytest.mark.parametrize('name,model', MODELS)
def test_edge_mode_sees_pulse_too_short_for_other_modes(firmware, name, model):
    """Model estimate, not a hardware measurement.  A pulse narrower than
    the 6.26MHz mode's shortest poll interval is missed at some phases; edge
    mode, polling more often while idle, records it at every phase."""
    width   = 10
    classic = 0
    edge    = 0
    phases  = range(0, 69)       # one full pass of the 6.26MHz loop
    for phase in phases:
        wave = fwemu.Waveform([(5000 + phase        , 0x01),
                               (5000 + phase + width, 0x00)])
        for mode in ('6.26MHz', 'edge'):
            capture = run(firmware, wave, mode, model=model, max_cycles=7000)
            seen    = capture.bits()[2:] == [0x01, 0x00]
            if mode == 'edge':
                edge    += seen
            else:
                classic += seen
    assert edge == len(phases)
    assert classic < len(phases)


@pytest.mark.parametrize('name,model', MODELS)
def test_tick_spread_of_square_wave_gives_longest_poll_gap(firmware, name,
                                                           model):
    """The hardware measurement procedure in README.md ("logic mode=edge"):
    the spread of the time between captured edges of a steady square wave
    is the longest gap between polls.  Must recover the measured 14 cycles
    of the 6.26MHz mode for the procedure to be worth recommending."""
    half   = 1009       # CPU cycles, prime so every loop phase gets visited
    steps  = [(3000 + half * n, n & 1) for n in range(1, 400)]
    spread = {}
    for mode in ('6.26MHz', 'edge'):
        capture = run(firmware, fwemu.Waveform(steps), mode, model=model,
                      max_cycles=3000 + half * 400)
        times   = capture.times()[2:]
        deltas  = [b - a for (a, b) in zip(times, times[1:])]
        assert len(deltas) > 300
        spread[mode] = max(deltas) - min(deltas)
        gaps = read_gaps(run(firmware, fwemu.Waveform([]), mode, model=model,
                             max_cycles=6000))
        assert spread[mode] == max(gaps), (mode, spread[mode], max(gaps))
    assert spread['6.26MHz'] == 14
    assert spread['edge'] < spread['6.26MHz']
