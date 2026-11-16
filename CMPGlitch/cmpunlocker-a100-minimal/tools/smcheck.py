#!/usr/bin/env python3
"""SM count / identity check via libcuda (no CUDA toolkit needed).

Expect SMs=128 with --sm-unlock, SMs=124 without (native A100 floorsweep).
"""
import ctypes
c = ctypes.CDLL('libcuda.so.1'); c.cuInit(0)
cnt = ctypes.c_int(); c.cuDeviceGetCount(ctypes.byref(cnt))
for i in range(cnt.value):
    d = ctypes.c_int(); c.cuDeviceGet(ctypes.byref(d), i)
    name = ctypes.create_string_buffer(128); c.cuDeviceGetName(name, 128, d)
    sm  = ctypes.c_int(); c.cuDeviceGetAttribute(ctypes.byref(sm), 16, d)   # SM count
    bus = ctypes.c_int(); c.cuDeviceGetAttribute(ctypes.byref(bus), 33, d)  # PCI bus
    dev = ctypes.c_int(); c.cuDeviceGetAttribute(ctypes.byref(dev), 34, d)  # PCI device
    print(f'cuda[{i}] {name.value.decode():28s} PCI {bus.value:02x}:{dev.value:02x}  SMs={sm.value}')
