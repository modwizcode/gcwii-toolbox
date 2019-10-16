#!/usr/bin/python3
""" libstfu/core.py
Everything about this is a mistake
"""

from __future__ import print_function
from unicorn import *
from unicorn.arm_const import *
from unicorn.unicorn_const import *
from capstone import *

from libstfu.io import *
from libstfu.hollywood_defs import *
from libstfu.util import *

import sys
from struct import pack, unpack
import ctypes
import time


# These are the memory regions backing SRAM and BROM.
# In order to implement mirroring we need to pass pointers to mem_map_ptr().
# FIXME: make this less shitty

_srama_buf = bytearray(b'\x00' * 0x10000)
_srama_type = ctypes.c_ubyte * 0x10000
_sramb_buf = bytearray(b'\x00' * 0x10000)
_sramb_type = ctypes.c_ubyte * 0x10000
_brom_buf = bytearray(b'\x00' * 0x20000)
_brom_type = ctypes.c_ubyte * 0x20000
_sram_a = _srama_type.from_buffer(_srama_buf)
_sram_b = _sramb_type.from_buffer(_sramb_buf)
_brom = _brom_type.from_buffer(_brom_buf)


class Starlet(object):
    """ Object wrapping a Starlet emulator, implemented with Unicorn.
    We do not expect this to be exceptionally performant. However, for some
    particular [mostly simple] use-cases, it's convienient to have something
    like this implemented in Python.
    """

    def __init__(self):

        self.running = False        # Is emulation running?
        self.booted = False         # Have we already entered via boot vector?
        self.boot_vector = 0        # Entrypoint used to boot the system

        self.use_boot0 = False      # Should we do a full boot?
        self.code_loaded = False    # Has user code been loaded?
        self.codelen = None         # Length of user code

        self.brom_mapped = True         # Current BROM mapping state
        self.brom_mapped_next = False   # Changed by I/O

        self.sram_mirror = False        # Current SRAM mirror state
        self.sram_mirror_next = False   # Changed by I/O

        self.time_started = 0           # Walltime when emulation started
        self.uptime_limit = None        # User-defined uptime limit
        self.why = None                 # Reason for execution halt

        # These are used to co-ordinate the SRAM mirror toggle 
        self.schedule_mirror_done = False
        self.schedule_mirror_hook = None

        # For co-ordinating the BROM map toggle
        self.schedule_brom_map_done = False
        self.schedule_brom_map_hook = False

        self.symbols = {}           # Dictionary of symbols
        self.last_block_size = 0    # Size of the previous basic block
        self.block_count = 0        # The number of basic blocks executed

        # Capstone/Unicorn objects
        # FIXME: It's not clear how to disassemble ARM/THUMB?
        self.dis_arm = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_BIG_ENDIAN)
        self.dis_thumb = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
        self.mu = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_BIG_ENDIAN)

        self.mu.parent = self       # Unicorn ref to this object
        self.io = StarletIO(self)   # I/O device container
        self.__init_mmu()           # Configure memory mappings in Unicorn
        self.__init_hook()          # Initialize required hooks


    """ -----------------------------------------------------------------------
    Top-level functions for initializing/destroying emulator state.
    These are called when constructing a new Starlet() object.
    """

    def __init_mmu(self):
        """ Create all relevant memory mappings.
        In order to implement mirrors, we need to actually allocate some
        bytearray() to serve as the backing and pass a pointer to Unicorn.

        Protip: Do not attempt to alter the SRAM/BROM mappings inside of
        a UC_MEM_{READ,WRITE} hook. It would be conceptually the easiest 
        approach, however, the current nasty code is a hack around the fact
        that we apparently can't accomplish that without horrific death inside
        of libunicorn.so (no idea what that's about, but I'd rather hack
        right around it instead of deal with it).

        FIXME: There is some weird thing happening with mappings on ffff0000.
        When I set them to len=0x10000, we hang in libunicorn.so on unmapping.
        Apparently things work with len=0xf800. WHY?
        """

        # FIXME: investigate this a bit more
        # Initial SRAM/BROM mappings
        self.mu.mem_map_ptr(0xffff0000, 0x00010000, UC_PROT_ALL, _brom)
        self.mu.mem_map_ptr(0xfffe0000, 0x00010000, UC_PROT_ALL, _sram_a)

        self.mu.mem_map_ptr(0xfff00000, 0x00010000, UC_PROT_ALL, _sram_a)
        self.mu.mem_map_ptr(0x0d400000, 0x00010000, UC_PROT_ALL, _sram_a)

        self.mu.mem_map_ptr(0x0d410000, 0x00010000, UC_PROT_ALL, _sram_b)
        self.mu.mem_map_ptr(0xfff10000, 0x00010000, UC_PROT_ALL, _sram_b)

        # Hollywood I/O register spaces
        self.mu.mem_map(0x0d010000, 0x00001000) # NAND interface
        self.mu.mem_map(0x0d020000, 0x00001000) # AES interface
        self.mu.mem_map(0x0d030000, 0x00001000) # SHA interface
        self.mu.mem_map(0x0d040000, 0x00000400) # ECHI
        self.mu.mem_map(0x0d050000, 0x00000400) # OHCI0
        self.mu.mem_map(0x0d060000, 0x00000400) # OHCI1

        self.mu.mem_map(0x0d800000, 0x00000400) # Hollywood registers
        self.mu.mem_map(0x0d806000, 0x00000400) # EXI registers
        self.mu.mem_map(0x0d8b0000, 0x00008000) # Memory controller interface?

        # Two banks of actual RAM
        self.mu.mem_map(0x00000000, 0x01800000) # MEM1
        self.mu.mem_map(0x10000000, 0x04000000) # MEM2

    def brom_enabled_mirror_enable(self):
        """ When the BROM is mapped, enable the SRAM mirror """
        self.mu.mem_unmap(0xffff0000, 0x00010000) # brom
        self.mu.mem_unmap(0xfffe0000, 0x00010000) # sram_a
        self.mu.mem_unmap(0xfff00000, 0x00010000) # sram_a
        self.mu.mem_unmap(0x0d400000, 0x00010000) # sram_a
        self.mu.mem_unmap(0xfff10000, 0x00010000) # sram_b
        self.mu.mem_unmap(0x0d410000, 0x00010000) # sram_b

        self.mu.mem_map_ptr(0xfff00000,0x00020000,UC_PROT_ALL, _brom)
        self.mu.mem_map_ptr(0x0d400000,0x00020000,UC_PROT_ALL, _brom)
        self.mu.mem_map_ptr(0xffff0000,0x00010000,UC_PROT_ALL, _sram_a)
        self.mu.mem_map_ptr(0xfffe0000,0x00010000,UC_PROT_ALL, _brom)

    def mirror_enabled_brom_disable(self):
        """ When the SRAM mirror is enabled, unmap the BROM """
        uc.mem_unmap(0xfff00000, 0x00020000) # brom
        uc.mem_unmap(0x0d400000, 0x00020000) # brom
        uc.mem_unmap(0xffff0000, 0x00010000) # sram_a
        uc.mem_unmap(0xfffe0000, 0x00010000) # brom

        uc.mem_map_ptr(0xfff00000,0x00010000,UC_PROT_ALL, _sram_b)
        uc.mem_map_ptr(0x0d400000,0x00010000,UC_PROT_ALL, _sram_b)
        uc.mem_map_ptr(0xfff10000,0x00010000,UC_PROT_ALL, _sram_a)
        uc.mem_map_ptr(0x0d410000,0x00010000,UC_PROT_ALL, _sram_a)
        uc.mem_map_ptr(0xfffe0000,0x00010000,UC_PROT_ALL, _sram_b)
        uc.mem_map_ptr(0xffff0000,0x00010000,UC_PROT_ALL, _sram_a)




    def __init_hook(self):
        """ Initialize a set of default hooks necessary for emulation.
        Bin all non-device-specific MMIOs into a generic Hollywood device like
        `self.io.hlwd` or something similar.
        """
        self.mmio_hook_list = []

        self.__register_mmio_device("NAND",    0x0d010000, 0x20, self.io.nand)
        self.__register_mmio_device("AES",     0x0d020000, 0x20, self.io.aes)
        self.__register_mmio_device("SHA1",    0x0d030000, 0x20, self.io.sha)

        self.__register_mmio_device("ECHI",    0x0d040000,0x100, self.io.ehci)
        self.__register_mmio_device("OHCI0",   0x0d050000,0x200, self.io.ohci0)
        self.__register_mmio_device("OHCI1",   0x0d060000,0x200, self.io.ohci1)

        self.__register_mmio_device("IPC",     0x0d800000, 0x0c, self.io.ipc)
        self.__register_mmio_device("HW",      0x0d800010, 0x1c, self.io.hlwd)
        self.__register_mmio_device("INTR",    0x0d800030, 0x2c, self.io.intc)
        self.__register_mmio_device("HW",      0x0d800060, 0x5c, self.io.hlwd)
        self.__register_mmio_device("PPCGPIO", 0x0d8000c0, 0x18, self.io.gpio)
        self.__register_mmio_device("ARMGPIO", 0x0d8000dc, 0x20, self.io.gpio)
        self.__register_mmio_device("AHB",     0x0d800100, 0x4c, self.io.ahb)
        self.__register_mmio_device("HW",      0x0d800150, 0x98, self.io.hlwd)
        self.__register_mmio_device("EFUSE",   0x0d8001ec, 0x04, self.io.otp)
        self.__register_mmio_device("HW",      0x0d8001f4, 0x2c, self.io.hlwd)

        self.__register_mmio_device("AHB",     0x0d8b4000, 0x40, self.io.ahb)
        self.__register_mmio_device("AHB",     0x0d8b4228, 0x02, self.io.ahb)

        # Error handling hooks
        self.mu.hook_add(UC_HOOK_MEM_UNMAPPED, self.__get_err_unmapped_func())

        # Interrupt handling hook?
        self.mu.hook_add(UC_HOOK_INTR, self.__get_intr_function())

    def enable_block_hook(self):
        self.blocks_reached = []
        self.mu.hook_add(UC_HOOK_BLOCK, self.__get_basic_block_func())

    def __get_intr_function(self):
        def intr_func(uc, intno, user_data):
            starlet = uc.parent
            #log("Got interrupt {:08x}", intno)
            self.why = { "type": "interrupt", "intno": intno }
            starlet.mu.emu_stop()
        return intr_func


    def __get_mmio_func(self, mmio_name, io_device):
        """ Generate an MMIO-handler specific to the provided I/O device """
        def mmio_func(uc, access, addr, size, value, user_data):
            starlet = uc.parent
            io_device.on_access(access, addr, size, value)
        return mmio_func

    def __register_mmio_device(self, name, addr, size, io_device=None):
        """ Register an MMIO handler specific to some I/O device """
        base = addr
        tail = base + size

        #log("Registering MMIO {:08x}-{:08x} for {}", base, tail, name)
        if (io_device == None): io_device = self.io.dummy
        idx = self.mu.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE,
            self.__get_mmio_func(name, io_device), begin=base, end=tail)
        self.mmio_hook_list.append(idx)

    def __get_basic_block_func(self):
        """ Generate a Unicorn UC_HOOK_BLOCK handler """
        def basic_block_hook(uc, addr, size, user_data):
            starlet = uc.parent
            if (addr not in starlet.blocks_reached): 
                starlet.blocks_reached.append(addr)
        return basic_block_hook


    """ -----------------------------------------------------------------------
    Functions for directly mutating the machine state, writing into memory,
    controlling the flow of execution, etc.
    """

    def boot(self, halt=None, resume=None, timeout=0, user_until=None):
        """ Start the system at the boot vector """
        if (self.use_boot0):
            self.boot_vector = 0xffff0000
            until = 0x00000000
        else:
            if (self.code_loaded != True):
                warn("No binary/entrypoint specified")
                warn("Try loading a binary, or attaching NAND and boot ROM")
                return None
            else:
                until = halt if (halt != None) else 0x00000000

        if (user_until): until = user_until
        self.booted = True
        self.time_started = time.time()
        self.__do_mainloop(self.boot_vector, until)

    def halt(self):
        """ Halt emulation """
        self.mu.emu_stop()
        warn("Emulator got halt request")
        self.running = False

    def __do_mainloop(self, entrypt, until, timeout=0, count=0):
        """ This is the main emulation loop  """
        self.running = True
        _running = False
        self.mu.reg_write(UC_ARM_REG_PC, entrypt)
        self.main_ctx = None
        self.why = None
        self.last_pc = 0
        self.last_pc_timer = 0
        while True:
            try:

                if (self.main_ctx): self.mu.context_restore(self.main_ctx)

                # Need to account for THUMB here because the PC doesn't
                # necessarily encode this (idk if this is a Unicorn bug)
                pc = self.get_pc()
                if ((self.mu.reg_read(UC_ARM_REG_CPSR) & 0x20) != 0):
                    pc |= 1

                # Start executing; 
                self.mu.emu_start(pc, until, timeout=timeout, 
                        count=self.io._SRV_US)
                self.main_ctx = self.mu.context_save()
                pc = self.get_pc()

                # Detect looping halt branches
                instr = self.read32(pc)
                if (instr == 0xeafffffe):
                    warn("Halt loop detected at PC={:08x}", pc)
                    break

                # Handle breaks
                if (self.why): 
                    if (self.why['type'] == 'interrupt'):
                        self.dump_state()
                        break
                    if (self.why['type'] == 'sigint'):
                        self.dump_state()
                        break
                    if (self.why['type'] == 'breakpoint'):
                        break

                if (self.uptime_limit):
                    if ((time.time() - self.time_started) > self.uptime_limit):
                        self.why = {'type':  'time_limit'}
                        break

                self.last_pc_timer = self.io.timer
                self.last_pc = pc

                # Do an I/O update; check other important platform things
                self.io.update()
                if (self.sram_mirror != self.sram_mirror_next):
                    if (self.brom_mapped == True) and \
                            (self.sram_mirror_next == True):
                        self.brom_enabled_mirror_enable()

                if (self.brom_mapped != self.brom_mapped_next):
                    if (self.sram_mirror == True) and \
                            (self.brom_mapped_next == False):
                        self.mirror_enabled_brom_disable()

            except UcError as e:
                warn("ERROR: {}", e)
                self.dump_state()
                pc = self.get_pc()
                x = self.mu.mem_read(pc-0x10, 0x20)
                log("Stopped at pc={:08x}, here's memory at pc-0x10:", pc)
                hexdump_idt(x, 1)
                self.dump_state()
                self.running = False
                self.mu.emu_stop()
                break


    def clear_breakpoint(self, bp_idx): return self.mu.hook_del(bp_idx)
    def add_breakpoint(self, addr):
        """ Add a breakpoint hook at some address """
        return self.mu.hook_add(UC_HOOK_CODE, self.__get_breakpoint_func(),
                begin=addr, end=addr)

    def add_breakpoint_patch(self, addr, my_patch_func):
        """ Add a breakpoint hook at some address; pass a function to run"""
        return self.mu.hook_add(UC_HOOK_CODE, 
            self.__get_breakpoint_patch_func(my_patch_func), 
            begin=addr, end=addr)

    def __get_breakpoint_func(self):
        """ Generate a breakpoint-like hook which halts emulation """
        def breakpoint_hook(uc, addr, size, user_data):
            starlet = uc.parent
            #log("Hit breakpoint at pc={:08x}", addr)
            self.why = { "type": "breakpoint" }
            self.mu.emu_stop()
        return breakpoint_hook

    def __get_breakpoint_patch_func(self, my_func):
        """ This isn't actually a breakpoint. Calls my_func(starlet).
        """
        def breakpoint_hook(uc, addr, size, user_data):
            starlet = uc.parent
            my_func(starlet)
        return breakpoint_hook


    def add_code_logrange(self, addr_base, addr_tail, detail=False):
        """ Add a hook for logging on some range of code """
        if (detail == True):
            self.mu.hook_add(UC_HOOK_CODE, self.__get_code_logrange_detail_func(),
                    begin=addr_base, end=addr_tail)
        else:
            self.mu.hook_add(UC_HOOK_CODE, self.__get_code_logrange_func(),
                    begin=addr_base, end=addr_tail)

    def add_mem_logrange(self, addr_base, addr_tail):
        """ Add a hook for logging on some range of code """
        self.mu.hook_add(UC_HOOK_MEM_READ|UC_HOOK_MEM_WRITE, 
            self.__get_mem_logrange_func(), begin=addr_base, end=addr_tail)


    def read32(self, addr): return up32(self.mu.mem_read(addr, 4))
    def read16(self, addr): return up16(self.mu.mem_read(addr, 2))
    def write32(self, addr, val): self.mu.mem_write(addr, pack(">L", val))
    def write16(self, addr, val): self.mu.mem_write(addr, pack(">H", val))
    def dma_write(self, addr, data): self.mu.mem_write(addr, bytes(data))
    def dma_read(self, addr, size): return self.mu.mem_read(addr, size)
    def get_pc(self): return self.mu.reg_read(UC_ARM_REG_PC)
    def get_lr(self): return self.mu.reg_read(UC_ARM_REG_LR)

    """ -----------------------------------------------------------------------
    Functions for attaching devices and/or importing some other kinds
    of data into the platform/emulator.
    """

    def load_boot0(self, filename):
        """ Load the boot ROM into memory """
        with open(filename, "rb") as f: data = f.read()
        self.mu.mem_write(0xffff0000, data)
        self.use_boot0 = True

    def load_nand_file(self, filename):
        """ Attach a NAND dump to the NANDInterface. This reads the entire
        NAND dump into memory at once """
        with open(filename, "rb") as f: self.io.nand.data = f.read()
        #log("Imported NAND from {} ({:08x})", filename, len(self.io.nand.data))

    def load_nand_data(self, buf):
        """ Attach a NAND dump from a bytearray """
        self.io.nand.data = bytearray(buf)

    def load_code_file(self, filename, addr, entry=None):
        """ Load some code into memory at the specified address """
        assert self.running == False
        with open(filename, "rb") as f: ARM_CODE = f.read()
        self.codelen = len(ARM_CODE)
        self.mu.mem_write(addr, ARM_CODE)
        self.boot_vector = addr if (entry == None) else entry
        self.code_loaded = True

    def load_code_buf(self, buf, addr, entry=None):
        assert self.running == False
        self.codelen = len(buf)
        self.mu.mem_write(addr, buf)
        self.boot_vector = addr if (entry == None) else entry
        self.code_loaded = True

    def load_otp(self, filename):
        """ Attach an OTP memory dump from some file """
        with open(filename, "rb") as f: OTP_DATA = f.read()
        self.io.otp.data = OTP_DATA
        #log("Loaded {:08x} bytes from {} to OTP", len(OTP_DATA), filename)

    def load_symbols(self, filename):
        """ Load a CSV file with symbols into memory. Expects a file with some
        lines with [at least something like]: "address","symbol_name" """

        # Don't load symbols while running
        assert self.running == False

        with open(filename, "rb") as f:
            for line in f.readlines():
                l = line.decode('utf-8').replace('"', "")
                x = l.split(',')
                addr = int(x[0], 16)
                name = x[1]
                self.symbols[addr] = name
        #log("Imported {} symbols from {}", len(self.symbols), filename)


    """ -----------------------------------------------------------------------
    Functions for logging, manipulating and managing symbols, disassembly, etc.
    """

    def find_symbol(self, addr):
        """ Given some address, find the lowest, closest symbol """
        syms = [ addr for addr in self.symbols ]
        syms.sort()
        target = min(range(len(syms)), key=lambda x: abs(syms[x] - addr))
        if (syms[target] > addr):
            target = target - 1
        func_addr = syms[target]
        return self.symbols[func_addr]

    def get_symbol(self, addr):
        """ Given an address, return the matching symbol """
        if (self.symbols): return self.symbols.get(addr)
        else: return None

    def __get_locinfo(self, addr):
        """ Format string things """
        if (self.symbols):
            sym = self.find_symbol(addr)
            return "in {} ({:08x})".format(sym, addr) if sym else "@ pc={:08x}"\
                    .format(addr)
        else:
            return "@ pc={:08x}".format(addr)

    def disas(self, addr, size):
        """ Disassemble some amount of bytes at address """
        data = self.mu.mem_read(addr, size)

        # FIXME: how to deal with ARM/THUMB
        instrs = self.dis_thumb.disasm(data, addr, count=size)

        log("Disassembly request at {:08x}", addr)
        for instr in instrs:
            ad = instr.address
            ib = hexlify(instr.bytes).decode('utf-8')
            mn = instr.mnemonic
            op = instr.op_str
            print("\t{:08x}: \t{}\t{}\t{}".format(ad, ib, mn, op))

    def __get_code_logrange_func(self):
        """ Generate a hook for logging on some range of code """
        def logrange_hook(uc, addr, size, user_data):
            starlet = uc.parent
            log("TRACE: {}", self.__get_locinfo(addr))
        return logrange_hook

    def __get_code_logrange_detail_func(self):
        def logrange_hook(uc, addr, size, user_data):
            starlet = uc.parent
            log("TRACE: {}", self.__get_locinfo(addr))
            starlet.dump_state()
        return logrange_hook


    def __get_mem_logrange_func(self):
        """ Generate an MMIO-handler specific to the provided I/O device """
        def mem_logrange(uc, access, addr, size, value, user_data):
            starlet = uc.parent
            pc = starlet.get_pc()
            acc = "write" if access == UC_MEM_WRITE else "read"
            log("TRACE: {} {} memaddr={:08x},val={:08x}",
                    acc, self.__get_locinfo(pc), addr, value)
        return mem_logrange



    def __get_err_unmapped_func(self):
        """ Generate a handler for un-mapped memory accesses """
        def hook_unmapped(uc, access, addr, size, value, user_data):
            starlet = uc.parent
            pc = starlet.get_pc()
            acc_type = "write" if access == UC_MEM_WRITE_UNMAPPED else "read"
            warn("MMU error: pc={:08x} Unmapped {} {:08x} on {:08x}", pc,
                    acc_type, value, addr)
            starlet.disas(pc, 0x20)
            return False
        return hook_unmapped

    def dump_state(self, silent=False):
        """ Quick hack for dumping some machine state """
        pc = self.get_pc()
        lr = self.mu.reg_read(UC_ARM_REG_LR)
        sp = self.mu.reg_read(UC_ARM_REG_SP)
        r0 = self.mu.reg_read(UC_ARM_REG_R0)
        r1 = self.mu.reg_read(UC_ARM_REG_R1)
        r2 = self.mu.reg_read(UC_ARM_REG_R2)
        r3 = self.mu.reg_read(UC_ARM_REG_R3)
        r4 = self.mu.reg_read(UC_ARM_REG_R4)
        r5 = self.mu.reg_read(UC_ARM_REG_R5)
        r6 = self.mu.reg_read(UC_ARM_REG_R6)
        r7 = self.mu.reg_read(UC_ARM_REG_R7)
        r8 = self.mu.reg_read(UC_ARM_REG_R8)
        r9 = self.mu.reg_read(UC_ARM_REG_R9)
        r10 = self.mu.reg_read(UC_ARM_REG_R10)
        r11 = self.mu.reg_read(UC_ARM_REG_R11)
        r12 = self.mu.reg_read(UC_ARM_REG_R12)
        cpsr = self.mu.reg_read(UC_ARM_REG_CPSR)
        spsr = self.mu.reg_read(UC_ARM_REG_SPSR)
        apsr = self.mu.reg_read(UC_ARM_REG_APSR)
        c1_c0_2 = self.mu.reg_read(UC_ARM_REG_C1_C0_2)

        fmt = """pc={:08x} lr={:08x} sp={:08x} CPSR={:08x} SPSR={:08x} IPSR={:08x}\
            \nr0={:08x}  r1={:08x}   r2={:08x}   r3={:08x} r4={:08x}  r5={:08x}\
            \nr6={:08x}  r7={:08x}   r8={:08x}   r9={:08x} r10={:08x} r11={:08x}\
            \nr12={:08x} """

        ctx = {
                'pc': pc,
                'lr': lr,
                'sp': sp,
                'cpsr': cpsr,
                'spsr': spsr,
                'apsr': apsr,
                'r0': r0,
                'r1': r1,
                'r2': r2,
                'r3': r3,
                'r4': r4,
                'r5': r5,
                'r6': r6,
                'r7': r7,
                'r8': r8,
                'r9': r9,
                'r10': r10,
                'r11': r11,
                'r12': r12,
        }
 
        if (silent == False):
            log(fmt, pc, lr, sp, cpsr, spsr, apsr, 
                    r0,r1,r2,r3,r4,r5,r6,r7,r8,r9,r10,r11,r12,)
                
        return ctx


