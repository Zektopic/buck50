buck50 roadmap (Zektopic fork)
==============================

What limits buck50 today, what has been done about it, and what to do next.
Written 2026-11-17 against firmware 0.9.3 / `buck50.py` 0.9.7.

**Read this first:** no "Blue Pill" was available for any of the work so
far. Firmware 0.9.3 has been built reproducibly and its capture code runs
correctly under emulation, but it has never been flashed. Item 1 below
comes before anything else that touches firmware.


Where the limits are
--------------------

| What                         | Today                         | Set by                                                        |
|------------------------------|-------------------------------|---------------------------------------------------------------|
| Logic analyzer poll rate     | 6.26 MHz (measured, upstream) | 11 CPU cycles per poll in a hand-written loop, see README "Why so slow?" |
| Logic analyzer depth         | 4826 samples                  | 20 KB RAM, 1176 B of it used by firmware, 4 bytes per sample  |
| Oscilloscope rate            | 857 kHz                       | ADC clock 12 MHz (72 MHz / 6; chip maximum is 14 MHz), 14 ADC clocks per conversion |
| Oscilloscope depth           | 4826 dual / 9652 single       | same RAM                                                      |
| Oscilloscope trigger         | no pre-trigger samples        | sampling starts at the trigger                                |
| Host: full dump to file      | 5 ms (was 21 ms)              | excludes USB transfer, which has not been measured            |

Depth is fixed by the chip. Rates are where there is something to gain.


Done (firmware 0.9.3, buck50.py 0.9.7)
--------------------------------------

* **`logic mode=edge`**, opt-in. Estimated 9.8-11.3 MHz polling while
  inputs are quiet, against 6.26 MHz, and pulses down to about 139 ns
  always seen, against 194 ns; in exchange the first poll after a stored
  change is later (17 cycles against 12-15). Estimates, from a cost model
  fitted to upstream's measurements. See README "`logic mode=edge`".
* Existing capture loops untouched and pinned to their 0.9.2 alignment, so
  their measured timing can't drift when other code changes size.
* Two latent firmware bugs fixed (out-of-bounds read while matching the
  connect signature; unknown sampling mode jumping to the wrong code).
* `buck50.py`: bulk sample upload (4x faster host side, and robust to
  split reads), Python 3.12+ syntax warnings fixed, sane firmware version
  check, exits on end of redirected input.
* Byte-for-byte reproducible firmware build in Docker with the original
  toolchain; verified by rebuilding 0.9.2 to the published binary.
* Waveform viewers: oscilloscope captures now open in PulseView (sigrok
  session file) and GTKWave (analog VCD), logic captures display and
  convert correctly in GTKWave, and CSV keeps nanosecond resolution. All
  checked against the real programs' file readers.
* Tests: 16 end-to-end tests of `buck50.py` against a simulated device, 21
  of the export files against sigrok-cli, GTKWave's converters and gnuplot,
  72 emulated tests of the trigger/capture assembly; CI for all three plus
  a check that committed binaries match the source.
* Short README; the original became `docs/MANUAL.md`.


Next, in order
--------------

### 1. Validate 0.9.3 on a "Blue Pill" (needs hardware, nothing else)

Until this is done the 0.9.2 binaries (git commit `e77172a`) are the only
ones known to work.

1. Flash `build/buck50.bin`; connect with `buck50.py`; confirm identity,
   version 0.9.3, serial number.
2. With any slow signal on PB4-PB11, capture in each of `mode=6.26MHz`,
   `irregular`, `uniform`, `4MHz`, each with `code-mem=flash` and `ram`.
   Compare against the same captures on 0.9.2.
3. Same with `mode=edge`. Then measure it: steady square wave of a few
   hundred kHz from an independent source, `dump output=terminal`, spread
   of the "ticks" column = longest gap between polls. Expect 9-10 if the
   estimate holds, 14 for `mode=6.26MHz`. Update the README table with
   real numbers either way.
4. `oscope`, `monitor`, one bridge (`usart`), `pulse`: smoke test, since
   the C++ was recompiled even though only a few lines changed.
5. If two boards are to hand: `reset ganged=enabled` capture.

### 2. Faster, in rough order of value (all need hardware to measure)

* **Oscilloscope, single channel: 857 kHz to 1.71 MHz, within datasheet
  limits.** Use the STM32F1 "fast interleaved" dual-ADC mode: ADC1 and
  ADC2 sample the same pin 7 ADC clocks apart. Storage is already two
  12-bit samples per word in single-channel mode, so depth is unchanged.
  Firmware: `DUALMOD` setting, both ADCs on the trigger channel, 1.5-cycle
  sample time only. Host: one more `s/h+adc=` choice, sample order within
  the word, rate table. Check for gain/offset mismatch between the two
  ADCs showing as an alternating pattern.
* **`mode=edge` after measuring it:** tune the number of polls per loop
  pass; try a store stub that takes one full timestamped sample before
  returning, to bring the 17-cycle recovery down toward 14.
* **Trigger loop:** waiting in one state costs an estimated 11-12 cycles
  per poll (two compares, two branches). A dedicated tight loop for the
  common "stay in this state until match" case should be about 9. Matters
  for triggering on short pulses.
* **Oscilloscope overclock, opt-in and labelled as such:** ADC clock
  72 MHz / 4 = 18 MHz gives 1.29 MHz (2.57 MHz interleaved). Outside the
  14 MHz specification; accuracy degrades; some clone chips may not cope.
* **Oscilloscope pre-trigger:** run DMA in circular mode before the
  trigger and stop a set number of samples after it, so the capture shows
  what led up to the trigger. A feature more than a speed-up, but the most
  asked-for thing in any scope.
* **Upload:** measure how long a full `dump` takes over real USB first.
  `send_samples()` copies each packet twice (into `send_buf`, then into
  USB packet memory); only worth changing if the measurement says so.

Not worth doing: more sample depth (a 3-byte sample format would slow the
capture loop for 33% more samples); compiler flags (the hot loops are
hand-written assembly, the compiler doesn't see them).

### 3. Features (developable against `test/fake_buck50.py`, no hardware)

* **Scriptable operation:** `buck50.py --run "logic ...; dump ..."` and
  command files. Today input can't be piped in, because input arriving
  while waiting for the device means "abort". Unlocks automation and
  hardware-in-the-loop tests.
* **Protocol decoding:** a `decode` command which hands the capture to
  `sigrok-cli` decoders (UART, I2C, SPI) and prints the result. Doing it by
  hand on a saved VCD already works and is tested.
* **Packaging:** `pyproject.toml` and a `buck50` console command; a udev
  rule for a stable `/dev/buck50` and non-root access.
* **Report the chip:** read the MCU ID and flash size at connect.
  Clone chips (GD32, CKS32, APM32) have different instruction timing, so
  speed reports are only comparable with this.
* From upstream's own list: timing-based UART trigger, multiple SPI slave
  selects, strobed `gpio` mode (firmware; need hardware).
* **USB firmware update:** build variant linked above a DFU bootloader so
  later updates need no ST-Link (needs hardware).

### 4. Quality

* **Loopback self-test** (needs hardware, and three jumper wires): the
  `pulse` generator keeps running on PA1-PA3 during other commands; wired
  to PB4-PB6 it gives `logic` known frequencies and duty cycles to check
  against. With scriptable operation this becomes a one-command hardware
  regression test.
* Extend `fake_buck50.py` to `monitor`, `usart`, `spi`, `i2c`, `pulse`,
  `numbers`: about 5000 lines of `buck50.py` currently have no tests.
* Emulate more than the capture assembly (needs clock and USB peripheral
  models; Renode is the likely tool).
* Bring `docs/MANUAL.md`'s `help` excerpts up to date with `buck50.py`
  0.9.7 (they are the original author's, from 0.9.6).
* Logic capture as a sigrok session file too, so PulseView needs no import
  step (must expand the sparse samples to a fixed rate; mind file size).
* Offer the fixes that apply to upstream (escape sequences, signature
  read, version check) back to `thanks4opensource/buck50`.
