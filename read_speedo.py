#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 nvidia-speedo-linux contributors
# RM ABI definitions: Copyright (c) 1993-2026 NVIDIA CORPORATION & AFFILIATES
"""Read NVIDIA's cached Speedo and VFE range. No GPU register writes.

Public RM ABI: NVIDIA/open-gpu-kernel-modules, nvos.h/cl0080/ctrl0000gpu.h.
Private GET mapping: Skyworks-Neo/nvapi-rs, sys/src/gpu/clock.rs.
VFIELD_ID_S_PARAM=7: NVIDIA nvgpu include/nvgpu/bios.h.
The private record layout is configurable in layout.json; verified on 610.43.02.
"""
import argparse
import csv
import ctypes as C
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

U32, U64, U16 = C.c_uint32, C.c_uint64, C.c_uint16


class Alloc(C.Structure):
    _fields_ = [(k, U32) for k in ('root', 'parent', 'handle', 'cls')] + [
        ('params', U64), ('size', U32), ('status', U32)]


class Control(C.Structure):
    _fields_ = [(k, U32) for k in ('client', 'object', 'command', 'flags')] + [
        ('params', U64), ('size', U32), ('status', U32)]


class Device(C.Structure):
    _fields_ = [(k, U32) for k in ('id', 'share', 'target_client', 'target_device', 'flags')] + [
        ('va_size', U64), ('va_start', U64), ('va_limit', U64), ('va_mode', U32)]


class GpuInfo(C.Structure):
    _fields_ = [(k, U32) for k in ('id', 'flags', 'device', 'subdevice',
                                 'sli', 'board', 'instance', 'numa')]


class PciInfo(C.Structure):
    _fields_ = [('id', U32), ('domain', U32), ('bus', U16), ('slot', U16)]


def ioctl(fd, number, obj):
    # Linux _IOWR('F', number, sizeof(obj)); the public NVIDIA ABI uses this.
    request = (3 << 30) | (C.sizeof(obj) << 16) | (ord('F') << 8) | number
    data = bytearray(C.string_at(C.addressof(obj), C.sizeof(obj)))
    fcntl.ioctl(fd, request, data, True)
    C.memmove(C.addressof(obj), bytes(data), len(data))


class RM:
    def __init__(self):
        self.fd = os.open('/dev/nvidiactl', os.O_RDWR | os.O_CLOEXEC)
        self.root = 0
        self.root = self.allocate(0, 0, 0x41)

    def allocate(self, parent, handle, cls, params=None):
        a = Alloc(self.root, parent, handle, cls,
                  C.addressof(params) if params is not None else 0,
                  C.sizeof(params) if params is not None else 0, 0)
        ioctl(self.fd, 0x2b, a)
        if a.status:
            raise RuntimeError(f'RM_ALLOC class={cls:#x}: {a.status:#x}')
        return a.handle

    def get(self, obj, command, params):
        a = Control(self.root, obj, command, 0, C.addressof(params), C.sizeof(params), 0)
        ioctl(self.fd, 0x2a, a)
        if a.status:
            raise RuntimeError(f'RM GET {command:#x}: {a.status:#x}')

    def close(self):
        if self.root:
            free = (U32 * 4)(self.root, self.root, self.root, 0)
            ioctl(self.fd, 0x29, free)
        os.close(self.fd)


def method_size(blob, command):
    """Read the GSP method table, rather than guessing an ioctl buffer size."""
    token = struct.pack('<I', command)
    candidates = set()
    start = 0
    while True:
        pos = blob.find(token, start)
        if pos < 0:
            break
        start = pos + 1
        if pos % 8 or pos + 32 > len(blob):
            continue
        cmd, size, owner, function, flags = struct.unpack_from('<IIQQQ', blob, pos)
        # Method descriptors are 32 bytes, with two aligned function pointers.
        if 0 < size <= 1 << 20 and owner and function and not owner % 4 and not function % 2 and flags:
            candidates.add(size)
    if len(candidates) != 1:
        raise RuntimeError(f'No unambiguous GSP descriptor for {command:#x}: {sorted(candidates)}')
    return candidates.pop()


def firmware_sizes(driver, layout, firmware=None):
    paths = [firmware] if firmware else sorted(Path('/lib/firmware/nvidia', driver).glob('gsp_*.bin'))
    results = set()
    for p in paths:
        blob = Path(p).read_bytes()
        try:
            results.add(tuple(method_size(blob, int(layout[k]['command'], 0))
                              for k in ('info', 'control')))
        except RuntimeError:
            continue
    if len(results) != 1:
        raise RuntimeError('GSP layouts differ or are unavailable; provide --firmware and a matching --layout')
    return results.pop()


def decode(info, control, layout):
    capacity = struct.unpack_from('<I', info)[0]
    if not 0 < capacity <= 255:
        raise RuntimeError(f'Unsupported VFE capacity: {capacity}')
    mask_words = (capacity + 31) // 32
    header = 4 + mask_words * 4 + 4
    strides = [(len(b) - header) // capacity for b in (info, control)]
    if strides != [layout['info']['stride'], layout['control']['stride']]:
        raise RuntimeError(f'Unknown private VFE record layout: {strides}')
    if any((len(b) - header) % capacity for b in (info, control)):
        raise RuntimeError('Malformed VFE record array')
    out = []
    for i in range(capacity):
        word = struct.unpack_from('<I', info, 4 + 4 * (i // 32))[0]
        if not word & (1 << (i % 32)):
            continue
        record = info[header + strides[0]*i:header + strides[0]*(i+1)]
        limits = control[header + strides[1]*i:header + strides[1]*(i+1)]
        if record[0] != layout['fuse_type'] or record[layout['vfield_id_offset']] != layout['speedo_vfield_id']:
            continue
        value = struct.unpack_from('<I', record, layout['cached_integer_offset'])[0]
        minimum, maximum = struct.unpack_from('<ff', limits, layout['range_offset'])
        if not all(map(math.isfinite, (minimum, maximum))) or maximum <= minimum:
            raise RuntimeError('Malformed VFE range')
        low, high = record[layout['low_bit_offset']], record[layout['high_bit_offset']]
        address = struct.unpack_from('<I', record, layout['register_offset'])[0]
        out.append({'vfe_index': i, 'speedo': value, 'register': f'0x{address:08X}',
                    'bits': [low, high], 'range_min': minimum, 'range_max': maximum,
                    'range_normalized_percent': 100*(value-minimum)/(maximum-minimum),
                    'info_record_hex': record.hex(), 'control_record_hex': limits.hex()})
    if len(out) != 1:
        raise RuntimeError(f'Expected one Speedo VFIELD definition, found {len(out)}')
    return out[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--dump-dir', type=Path)
    parser.add_argument('--layout', type=Path, default=Path(__file__).with_name('layout.json'))
    parser.add_argument('--firmware', type=Path)
    args = parser.parse_args()
    layout = json.loads(args.layout.read_text())
    driver_text = Path('/proc/driver/nvidia/version').read_text()
    driver = re.search(r'\b\d{3}\.\d+\.\d+\b', driver_text).group()
    sizes = firmware_sizes(driver, layout, args.firmware)
    names = {}
    try:
        text = subprocess.check_output(['nvidia-smi', '--query-gpu=pci.bus_id,name',
                                        '--format=csv,noheader'], text=True, timeout=15)
        for address, name in csv.reader(text.splitlines()):
            domain, bus, slot = address.strip().split(':')
            names[f'{int(domain,16):04x}:{bus.lower()}:{slot.lower()}'] = name.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    report = {'recorded_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'driver_version': driver, 'method': 'NVIDIA RM cached VFE GET_INFO and GET_CONTROL',
              'gpu_register_writes': False, 'cards': []}
    rm = RM()
    try:
        ids = (U32 * 32)(*([0xffffffff] * 32))
        rm.get(rm.root, 0x201, ids)
        attached = list(ids).index(0xffffffff) if 0xffffffff in ids else len(ids)
        if not attached:
            raise RuntimeError('No attached NVIDIA GPUs')
        attach = (U32 * attached)(*list(ids)[:attached])
        ioctl(rm.fd, 212, attach)
        for i, gpu_id in enumerate(list(ids)[:attached]):
            pci = PciInfo(gpu_id, 0, 0, 0)
            gi = GpuInfo()
            gi.id = gpu_id
            rm.get(rm.root, 0x21b, pci)
            rm.get(rm.root, 0x205, gi)
            bdf = f'{pci.domain:04x}:{pci.bus:02x}:{pci.slot:02x}.0'
            card = {'pci_address': bdf}
            gpu_fd = None
            try:
                details = {}
                for line in Path('/proc/driver/nvidia/gpus', bdf, 'information').read_text().splitlines():
                    if ':' in line:
                        key, value = line.split(':', 1)
                        details[key.strip()] = value.strip()
                card['name'] = names.get(bdf, details.get('Model', ''))
                pci_path = Path('/sys/bus/pci/devices', bdf)
                card['pci_device_id'] = ':'.join((pci_path/k).read_text().strip().removeprefix('0x')
                                               for k in ('vendor', 'device'))
                gpu_fd = os.open(f"/dev/nvidia{int(details['Device Minor'])}", os.O_RDWR | os.O_CLOEXEC)
                dev, sub = 0x100 + i*2, 0x101 + i*2
                dp = Device()
                dp.id, dp.share = gi.device, rm.root
                rm.allocate(rm.root, dev, 0x80, dp)
                rm.allocate(dev, sub, 0x2080, U32(gi.subdevice))
                info = C.create_string_buffer(sizes[0])
                rm.get(sub, int(layout['info']['command'], 0), info)
                ctl = C.create_string_buffer(sizes[1])
                capacity = struct.unpack_from('<I', info.raw)[0]
                if not 0 < capacity <= 255:
                    raise RuntimeError(f'Unsupported VFE capacity: {capacity}')
                mask_size = 4 + ((capacity + 31)//32)*4
                C.memmove(ctl, info, mask_size)
                rm.get(sub, int(layout['control']['command'], 0), ctl)
                card.update(decode(info.raw, ctl.raw, layout))
                card['linux_confirmed'] = True
                if args.dump_dir:
                    args.dump_dir.mkdir(parents=True, exist_ok=True)
                    for name, data in [('info', info.raw), ('control', ctl.raw)]:
                        (args.dump_dir/f'{bdf}-{name}.bin').write_bytes(data)
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                card.update({'linux_confirmed': False, 'error': str(exc)})
            finally:
                if gpu_fd is not None:
                    os.close(gpu_fd)
            report['cards'].append(card)
    finally:
        rm.close()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for card in report['cards']:
            if card['linux_confirmed']:
                print(f"{card['pci_address']} {card['name']}: Speedo={card['speedo']} "
                      f"range=[{card['range_min']:.2f},{card['range_max']:.2f}] "
                      f"normalized={card['range_normalized_percent']:.2f}%")
            else:
                print(f"{card['pci_address']}: {card['error']}", file=sys.stderr)
    return 0 if all(c['linux_confirmed'] for c in report['cards']) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except PermissionError as exc:
        print(f'Permission denied: {exc}. Try running with sudo.', file=sys.stderr)
        sys.exit(1)
