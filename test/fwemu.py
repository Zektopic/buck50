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

"""Run the buck50 logic analyzer capture code under emulation.

Loads buck50.elf into a Unicorn Cortex-M3, calls the real
trigger_and_sample_plain() assembly, and feeds it a scripted GPIOB
waveform.  Used to check the capture loops functionally (trigger state
machine, which samples get stored, sample word layout, memory limit)
without a "Blue Pill".

IMPORTANT -- what this is not: it is not cycle accurate.  Emulated time
comes from CycleModel, a per-instruction cost table fitted so that the
existing loops reproduce the cycle counts the firmware author measured on
real hardware (see buck50_asm.s: flash=11,11,11,11,11,14 etc).  Results
for code that author did not measure are estimates, not measurements.
"""

import bisect
import struct

from elftools.elf.elffile import ELFFile
from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
                     UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ)
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R5, UC_ARM_REG_SP, UC_ARM_REG_LR,
                               UC_ARM_REG_PC, UC_CPU_ARM_CORTEX_M3)

FLASH_BASE   = 0x08000000
FLASH_SIZE   = 0x10000
RAM_BASE     = 0x20000000
RAM_SIZE     = 0x5000
END_OF_RAM   = RAM_BASE + RAM_SIZE
PERIPH_BASE  = 0x40000000
PERIPH_SIZE  = 0x30000
PPB_BASE     = 0xE000E000
PPB_SIZE     = 0x1000

GPIOB_IDR    = 0x40010C08
GPIOB_BSRR   = 0x40010C10
SYSTICK_VAL  = 0xE000E018
TIM1_CR1     = 0x40012C00

MAX_TRIGGERS = 256
TRIGGERS     = END_OF_RAM - 4 * MAX_TRIGGERS

# firmware InProgress bits
TRIGGERING   = 0x0100
EXTERN_TRIG  = 0x0200
TRIGGERED    = 0x0800
SAMPLING     = 0x1000

# firmware SamplingMode codes
MODES = {'6.26MHz': 0, 'irregular': 1, 'uniform': 2, '4MHz': 3, 'edge': 4}

CODE_MEM_RAM   = 0
CODE_MEM_FLASH = 1

RETURN_MAGIC = 0x0800fff0   # never reached: capture code doesn't return


class CycleModel:
    """Per-instruction cycle costs, 72 MHz core clocks.

    Fitted to the author's hardware measurements of the flash-resident
    loops: one predicated sample = 11 cycles, taken branch = 3,
    mov/nop = 1.  The split of the 11 between the two loads is not
    observable from those measurements, hence two variants; anything
    computed for unmeasured code should be quoted as the range between
    them.
    """
    def __init__(self, ldr_apb=4, ldr_ppb=3):
        self.ldr_apb      = ldr_apb   # ldr from APB2 peripheral (GPIOB)
        self.ldr_ppb      = ldr_ppb   # ldr from private periph bus (SysTick)
        self.ldr_mem      = 2         # ldr from RAM/flash
        self.store        = 2         # executed store
        self.branch_taken = 3
        self.other        = 1         # incl. skipped conditional, "it"

    @classmethod
    def variants(cls):
        return {'apb4_ppb3': cls(4, 3), 'apb5_ppb2': cls(5, 2)}


def it_block_skipped(uc, it_addr, next_addr):
    """Number of instructions between an IT instruction at it_addr and
    next_addr, or 0 if it_addr isn't an IT or next_addr isn't the end of
    (part of) its block."""
    opcode = struct.unpack('<H', uc.mem_read(it_addr, 2))[0]
    if (opcode & 0xff00) != 0xbf00 or not (opcode & 0x000f):
        return 0
    mask  = opcode & 0xf
    limit = 4 - ((mask & -mask).bit_length() - 1)   # instructions in block
    addr  = it_addr + 2
    count = 0
    while addr < next_addr and count < limit:
        first  = struct.unpack('<H', uc.mem_read(addr, 2))[0]
        addr  += 4 if (first >> 11) in (0x1d, 0x1e, 0x1f) else 2
        count += 1
    return count if addr == next_addr else 0


def trigger_word(mask, bits, pass_state, fail_state):
    """Firmware Trigger: byte0=mask byte1=pass byte2=fail byte3=bits."""
    return mask | (pass_state << 8) | (fail_state << 16) | (bits << 24)


class Waveform:
    """PB4-PB11 levels (8 bit value) as step function of time in cycles."""
    def __init__(self, steps, initial=0):
        # steps: iterable of (cycle_time, 8-bit value), ascending time
        self.times  = [t for (t, v) in steps]
        self.values = [v for (t, v) in steps]
        self.initial = initial

    def at(self, cycle):
        ndx = bisect.bisect_right(self.times, cycle) - 1
        return self.initial if ndx < 0 else self.values[ndx]

    def edges(self):
        prev = self.initial
        for (t, v) in zip(self.times, self.values):
            if v != prev:
                yield (t, v)
            prev = v


class Capture:
    def __init__(self):
        self.words       = []     # raw uint32 samples as firmware stored them
        self.reads       = []     # (cycle, 8-bit value) of every GPIOB->IDR read
        self.read_pcs    = []     # pc of every GPIOB->IDR read
        self.in_progress = 0
        self.halt        = None   # 'memory', 'cycles'
        self.cycles      = 0
        self.sampling_from_read = 0  # index into reads of first sampling read
        self.max_samples = 0      # sample memory available to this run

    def bits(self):
        return [w >> 24 for w in self.words]

    def ticks(self):
        return [w & 0xffffff for w in self.words]

    def times(self):
        """Elapsed cycles of each sample since first, as host computes."""
        out, total, prev = [], 0, None
        for tick in self.ticks():
            if prev is None:
                prev = tick
            delta = prev - tick
            if tick > prev:
                delta += 0x1000000
            total += delta
            prev   = tick
            out.append(total)
        return out


class Firmware:
    def __init__(self, elf_path):
        self.elf_path = elf_path
        self.symbols  = {}
        self.segments = []
        with open(elf_path, 'rb') as file:
            elf = ELFFile(file)
            for seg in elf.iter_segments():
                if seg['p_type'] == 'PT_LOAD' and seg['p_filesz']:
                    self.segments.append((seg['p_paddr'],
                                          seg['p_vaddr'],
                                          seg.data()    ))
            for sym in elf.get_section_by_name('.symtab').iter_symbols():
                if sym.name:
                    self.symbols[sym.name] = sym['st_value']

    def has_mode(self, name):
        return name != 'edge' or 'edge' in self.symbols

    def capture(self              ,
                waveform          ,
                mode='6.26MHz'    ,
                code_mem=CODE_MEM_FLASH,
                num_samples=0xffff,
                triggers=None     ,
                model=None        ,
                max_cycles=2000000,
                systick_start=0xffffff,
                ganged=False      ,
                extern_trigger_at=None):
        """Run trigger_and_sample_plain() (or _ganged() if ganged) until
        sample memory is full (HardFault on real hardware) or max_cycles
        elapse (stands in for the duration timer / USB halt interrupts).
        extern_trigger_at: cycle at which another ganged unit pulls the
        open-drain trigger line (PB14) low."""
        model    = model or CycleModel()
        triggers = triggers or [trigger_word(0, 0, 0, 0)]  # immediate
        sym      = self.symbols
        capt     = Capture()

        uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M3)
        uc.mem_map(FLASH_BASE, FLASH_SIZE)
        uc.mem_map(RAM_BASE  , RAM_SIZE  )
        for (paddr, vaddr, data) in self.segments:
            uc.mem_write(paddr, data)
            if vaddr != paddr:
                uc.mem_write(vaddr, data)   # what startup .data copy does

        state = {'cycles'   : 0,
                 'prev_addr': None,
                 'prev_size': 0,
                 'odr'      : 0}

        def idr_now(extra):
            now  = state['cycles'] + extra
            pins = waveform.at(now) & 0xff
            # PB14/15 (ganged trigger/sync, open-drain, wire-AND between
            # units) read back what this unit drives unless another unit
            # pulls PB14 low
            ganged_lines = state['odr'] & 0xc000
            if extern_trigger_at is not None and now >= extern_trigger_at:
                ganged_lines &= ~0x4000
            return (pins << 4) | ganged_lines

        def periph_read(uc, offset, size, user):
            addr = PERIPH_BASE + offset
            if addr == GPIOB_IDR:
                # level is sampled at the end of the bus access
                value = idr_now(model.ldr_apb - 1)
                capt.reads.append((state['cycles'] + model.ldr_apb - 1,
                                   (value >> 4) & 0xff))
                capt.read_pcs.append(uc.reg_read(UC_ARM_REG_PC))
                state['load'] = model.ldr_apb
                return value
            state['load'] = model.ldr_apb
            return 0

        def periph_write(uc, offset, size, value, user):
            addr = PERIPH_BASE + offset
            if addr == GPIOB_BSRR:
                state['odr'] |=   value        & 0xffff
                state['odr'] &= ~(value >> 16) & 0xffff
            state['stored'] = True

        def ppb_read(uc, offset, size, user):
            state['load'] = model.ldr_ppb
            if PPB_BASE + offset == SYSTICK_VAL:
                return (  systick_start
                        - (state['cycles'] + model.ldr_ppb - 1)) & 0xffffff
            return 0

        def ppb_write(uc, offset, size, value, user):
            state['stored'] = True

        uc.mmio_map(PERIPH_BASE, PERIPH_SIZE,
                    periph_read, None, periph_write, None)
        uc.mmio_map(PPB_BASE   , PPB_SIZE   ,
                    ppb_read   , None, ppb_write   , None)

        def account(next_addr):
            """Charge the instruction that just finished."""
            prev = state['prev_addr']
            if prev is None:
                return
            cost = model.other
            if state.get('load'):
                cost = state['load']
            elif state.get('ram_load'):
                cost = model.ldr_mem
            elif state.get('stored'):
                cost = model.store
            if next_addr != prev + state['prev_size']:
                skipped = it_block_skipped(uc, prev, next_addr)
                if skipped:
                    # Unicorn doesn't run the code hook for instructions
                    # in an IT block whose condition failed
                    cost = model.other * (1 + skipped)
                else:
                    cost = model.branch_taken
            state['cycles'] += cost
            state['load'] = state['ram_load'] = state['stored'] = None

        def on_code(uc, address, size, user):
            account(address)
            state['prev_addr'] = address
            state['prev_size'] = size
            if state['cycles'] >= max_cycles:
                capt.halt = 'cycles'
                uc.emu_stop()

        def on_ram_write(uc, access, address, size, value, user):
            state['stored'] = True

        def on_ram_read(uc, access, address, size, value, user):
            state['ram_load'] = True

        uc.hook_add(UC_HOOK_CODE, on_code)
        uc.hook_add(UC_HOOK_MEM_WRITE, on_ram_write,
                    begin=RAM_BASE, end=END_OF_RAM - 1)
        uc.hook_add(UC_HOOK_MEM_READ , on_ram_read ,
                    begin=RAM_BASE, end=END_OF_RAM - 1)
        uc.hook_add(UC_HOOK_MEM_READ , on_ram_read ,
                    begin=FLASH_BASE, end=FLASH_BASE + FLASH_SIZE - 1)

        # what digital_sampling() in buck50.cxx does before the asm call
        uc.mem_write(sym['sampling_mode'], struct.pack('<I', MODES[mode]))
        uc.mem_write(sym['in_progress'  ], struct.pack('<H', TRIGGERING ))
        for (ndx, word) in enumerate(triggers):
            uc.mem_write(TRIGGERS + 4 * ndx, struct.pack('<I', word))

        if ganged:
            state['odr'] |= 0x4000      # gpiob->bsrr = BS14 in buck50.cxx
            entry = sym['trigger_and_sample_ganged']
        else:
            entry = sym['trigger_and_sample_plain']

        uc.reg_write(UC_ARM_REG_R0, code_mem   )
        uc.reg_write(UC_ARM_REG_R1, num_samples)
        uc.reg_write(UC_ARM_REG_SP, sym['TOP_OF_STACK'])
        uc.reg_write(UC_ARM_REG_LR, RETURN_MAGIC | 1)

        try:
            uc.emu_start(entry | 1, RETURN_MAGIC)
        except UcError as error:
            # the capture loops end on real hardware by writing one past
            # the end of RAM and taking the HardFault
            pc = uc.reg_read(UC_ARM_REG_PC)
            r2 = uc.reg_read(UC_ARM_REG_R2)
            # (base register may or may not show the post-increment of
            # the faulting store; firmware's main() clamps for the same
            # reason)
            if r2 not in (END_OF_RAM, END_OF_RAM + 4):
                raise RuntimeError(  "unexpected emulation fault %s at "
                                     "pc=0x%08x r2=0x%08x"
                                   % (error, pc, r2)                    )
            capt.halt = 'memory'

        capt.cycles = state['cycles']

        # what irq_handler_enter + main()'s longjmp_return code do
        in_prog = struct.unpack('<H', uc.mem_read(sym['in_progress'], 2))[0]
        if in_prog & TRIGGERING:
            in_prog |= uc.reg_read(UC_ARM_REG_R5) & 0xff
        samples = struct.unpack('<I', uc.mem_read(sym['samples'], 4))[0]
        if in_prog & SAMPLING:
            samples_end = uc.reg_read(UC_ARM_REG_R2)
        else:
            samples_end = struct.unpack('<I',
                                        uc.mem_read(sym['samples_end'], 4))[0]
        samples_end = min(samples_end, END_OF_RAM)
        capt.in_progress = in_prog
        capt.max_samples = (END_OF_RAM - samples) // 4
        count = (samples_end - samples) // 4
        if count:
            capt.words = list(struct.unpack('<%dI' % count,
                                            uc.mem_read(samples, 4 * count)))
        return capt
