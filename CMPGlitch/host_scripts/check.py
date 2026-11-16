#!/usr/bin/env python3
"""Read-only CMP 170HX SM / HBM ceiling checker (GA100).

Based on the community "check_cmp170hx_readonly" classifier (2026-10-18),
with an added defect-flag-based ceiling estimate. The hardware read path is
unchanged: it only loads allowlisted shadow/status words through an O_RDONLY,
PROT_READ sysfs BAR0 mapping. It never runs a fuse READ command, never writes
any register, and never changes selector/control state.

Run: sudo python3 -I -B check.py --bdf 0000:01:00.0 --uuid GPU-xxxxxxxx-...
Both arguments are required. Output is JSON on stdout plus a short human
summary on stderr. Redirect stdout to a file to keep the JSON.

The ceiling estimate is a THEORETICAL maximum derived from factory defect
flags. It is not a health test: a unit without a defect flag is assumed
reusable, which the flags do not actually prove. See report["limits"].
"""

import argparse

import datetime

from dataclasses import dataclass

import json

import mmap

import os

from pathlib import Path

import platform

import re

import select

import signal

import struct

import sys

import time


VERSION = "2026-11-12.ceiling.1"
SUPPORTED_DEVICES = {0x20C2: "CMP 170HX 8GB SKU", 0x2082: "CMP 170HX 10GB SKU"}
PAGE_BYTES = 4096
TIMEOUT_SECONDS = 15.0

# (name, BAR0 byte offset, accepted low-bit width). NO command/data ports.
# Topology controls are deliberately limited to the modeled GA100 unit count;
# unexpected high bits stop the survey even if the HW field is wider.
# The first read verifies the GA100 signature before any fuse/status reads.
REGISTERS = (
    ("PMC_BOOT_0", 0x00000000, 32),
    ("OPT_PCIE_DEVIDA", 0x008204D8, 16),
    ("OPT_PCIE_DEVIDB", 0x0082056C, 16),
    ("OPT_GPC_DISABLE", 0x00820350, 8),
    ("OPT_GPC_DEFECTIVE", 0x008205C4, 8),
    ("STATUS_OPT_GPC", 0x00820C1C, 8),
    ("CTRL_OPT_GPC", 0x0082081C, 8),
    ("OPT_FBP_DISABLE", 0x00820364, 12),
    ("OPT_FBP_DEFECTIVE", 0x008205CC, 12),
    ("STATUS_OPT_FBP", 0x00820D38, 12),
    ("CTRL_OPT_FBP", 0x00820938, 12),
    ("OPT_FBPA_DISABLE", 0x00820368, 24),
    ("OPT_FBPA_DEFECTIVE", 0x008205D0, 24),
    ("STATUS_OPT_FBPA", 0x00820C18, 24),
    ("CTRL_OPT_FBPA", 0x00820818, 24),
    ("OPT_FBIO_DISABLE", 0x0082036C, 24),
    ("OPT_FBIO_DEFECTIVE", 0x008205D4, 24),
    ("STATUS_OPT_FBIO", 0x00820C14, 24),
    ("CTRL_OPT_FBIO", 0x00820814, 24),
    ("EN_SW_OVERRIDE", 0x00820040, 1),
    ("DISABLE_SW_OVERRIDE_STATUS", 0x00820084, 1),
    ("OPT_SECURE_GSP_DEBUG_DIS", 0x0082074C, 1),
) + tuple(
    ("OPT_TPC_DISABLE_GPC%d" % i, 0x00820760 + 4 * i, 8)
    for i in range(8)
) + tuple(
    ("OPT_TPC_GPC%d_DEFECTIVE" % i, off, 8)
    for i, off in enumerate((
        0x008205C8, 0x00820508, 0x0082050C, 0x00820514,
        0x00820564, 0x008205AC, 0x00820B84, 0x00820B80,
    ))
) + tuple(
    ("STATUS_OPT_TPC_GPC%d" % i, 0x00820C38 + 4 * i, 8)
    for i in range(8)
) + tuple(
    ("CTRL_OPT_TPC_GPC%d" % i, 0x00820838 + 4 * i, 8)
    for i in range(8)
)
ALLOWED_OFFSETS = frozenset(off for _, off, _ in REGISTERS)
REQUIRED_BAR_BYTES = ((max(ALLOWED_OFFSETS) // PAGE_BYTES) + 1) * PAGE_BYTES


class StopProbe(RuntimeError):
    pass


def normalize_bdf(value):
    """Accept a full PCI BDF, including zero-padded eight-digit domains."""
    match = re.fullmatch(r"([0-9a-fA-F]{4}|[0-9a-fA-F]{8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])", value)
    if not match:
        raise ValueError("Use one full BDF such as 0000:01:00.0")
    domain, bus, device, function = (int(part, 16) for part in match.groups())
    if domain > 0xFFFF or device > 0x1F:
        raise ValueError("PCI domain or device number is out of range")
    return "%04x:%02x:%02x.%x" % (domain, bus, device, function)


def normalize_uuid(value):
    if not re.fullmatch(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.IGNORECASE):
        raise ValueError("Use a full physical GPU UUID: GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
    return "GPU-" + value[4:].lower()


@dataclass(frozen=True)
class Target:
    bdf: str
    gpu_uuid: str

    def __post_init__(self):
        object.__setattr__(self, "bdf", normalize_bdf(self.bdf))
        object.__setattr__(self, "gpu_uuid", normalize_uuid(self.gpu_uuid))

    @property
    def device_path(self):
        return Path("/sys/bus/pci/devices") / self.bdf

    @property
    def info_path(self):
        return Path("/proc/driver/nvidia/gpus") / self.bdf / "information"


def new_report(target):
    return {
        "script_version": VERSION,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target": {"bdf": target.bdf, "gpu_uuid": target.gpu_uuid,
                   "pci_id": None, "sku": None},
        "status": "STOPPED",
        "complete": False,
        "access_policy": {
            "path": "explicitly selected BDF's sysfs resource0 only",
            "allowed_pci_ids": ["10de:20c2", "10de:2082"],
            "automatic_device_discovery": False,
            "open": "O_RDONLY | O_CLOEXEC | O_NOFOLLOW",
            "mapping": "MAP_SHARED, PROT_READ; two pages, selected words only",
            "gpu_register_writes_in_script": 0,
            "fuse_commands_in_script": 0,
            "fallback": "none",
            "rir_read": False,
            "planned_passes": 2,
        },
        "registers": [],
        "interpretation": None,
        "ceiling_estimate": None,
        "device_identity": None,
        "limits": [
            "A mask is a recorded configuration/defect indication, not a health test.",
            "No hidden memory or inactive SM is enabled or tested.",
            "The ceiling estimate assumes every non-defect-flagged unit is reusable; factory tags do not prove this.",
            "HBM GiB uses a SKU-typical die density (4 GiB/FBPA for 8GB, 2 for 10GB); it is an assumption, not a measurement.",
            "A zero-defect-flag die still needs all 6 HBM stacks physically populated for 96 GiB; fuses cannot confirm population.",
            "The existing NVIDIA driver may act independently of this script.",
        ],
    }


def read_text(path):
    # Paths come only from a validated Target, never a user-supplied file path.
    fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "r", encoding="ascii", errors="strict") as stream:
            fd = None
            text = stream.read(16385)
        if len(text) > 16384:
            raise StopProbe("Unexpectedly large metadata: %s" % path)
        return text
    finally:
        if fd is not None:
            os.close(fd)


def preflight(target):
    device_path = target.device_path
    vendor = int(read_text(device_path / "vendor").strip(), 16)
    device = int(read_text(device_path / "device").strip(), 16)
    pci_class = int(read_text(device_path / "class").strip(), 16)
    if vendor != 0x10DE or device not in SUPPORTED_DEVICES or pci_class >> 16 != 0x03:
        raise StopProbe("Selected BDF is not a supported CMP 170HX (10de:20c2 or 10de:2082)")
    driver = Path(os.readlink(str(device_path / "driver"))).name
    if driver != "nvidia":
        raise StopProbe("The existing nvidia driver is not bound; no driver changes allowed")
    info = read_text(target.info_path)
    uuids = re.findall(r"^GPU UUID[ \t]*:[ \t]*(\S+)[ \t]*\r?$", info, re.MULTILINE)
    locations = re.findall(r"^Bus Location[ \t]*:[ \t]*(\S+)[ \t]*\r?$", info, re.MULTILINE)
    if len(uuids) != 1 or len(locations) != 1:
        raise StopProbe("Cached UUID/Bus Location missing or ambiguous; no alternate identification")
    if normalize_uuid(uuids[0]) != target.gpu_uuid or normalize_bdf(locations[0]) != target.bdf:
        raise StopProbe("Cached GPU UUID or Bus Location does not match; no alternate identification")
    enabled = int(read_text(device_path / "enable").strip(), 10)
    state = read_text(device_path / "power_state").strip()
    runtime = read_text(device_path / "power/runtime_status").strip()
    if enabled <= 0 or state != "D0" or runtime not in ("active", "unsupported"):
        raise StopProbe("GPU is not already enabled/awake; no wake, enable or resume attempted")
    fields = read_text(device_path / "resource").splitlines()[0].split()
    if len(fields) != 3:
        raise StopProbe("Malformed sysfs BAR0 resource descriptor")
    start, end, flags = (int(value, 16) for value in fields)
    if start == 0 or end < start or not flags & 0x200 or flags & 0x100:
        raise StopProbe("BAR0 is not exposed as a valid memory resource; no PCI-config fallback")
    bar_bytes = end - start + 1
    if bar_bytes < REQUIRED_BAR_BYTES:
        raise StopProbe("BAR0 is too small for the allowlisted status words")
    return {"driver": driver, "power_state": state, "runtime_status": runtime,
            "bar0_bytes": bar_bytes, "bar0_start": "0x%X" % start,
            "bar0_end": "0x%X" % end, "uuid_matched": True, "bdf_matched": True,
            "pci_device_id": device, "pci_id": "10de:%04x" % device,
            "sku": SUPPORTED_DEVICES[device]}


class ReadOnlyBar:
    """No store method; FD and VM protections both deny writing."""

    def __init__(self, path):
        self.fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.pages = {}
        try:
            size = os.fstat(self.fd).st_size
            if size < REQUIRED_BAR_BYTES:
                raise StopProbe("sysfs resource0 file is absent/truncated; no fallback")
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def read32(self, offset):
        if offset not in ALLOWED_OFFSETS or offset % 4:
            raise StopProbe("Read outside the fixed register allowlist")
        page = (offset // PAGE_BYTES) * PAGE_BYTES
        if page not in self.pages:
            region = mmap.mmap(self.fd, PAGE_BYTES, flags=mmap.MAP_SHARED,
                               prot=mmap.PROT_READ, offset=page)
            try:
                words = memoryview(region).cast("I")
                if not words.readonly or words.itemsize != 4:
                    words.release()
                    raise StopProbe("Mapping is not a read-only native uint32 view")
            except BaseException:
                region.close()
                raise
            self.pages[page] = (region, words)
        # Native aligned uint32 load from a read-only memoryview (CPython/x86-64).
        return int(self.pages[page][1][(offset - page) // 4])

    def close(self):
        for region, words in self.pages.values():
            words.release()
            region.close()
        self.pages.clear()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def validate_word(name, value, width, expected_device):
    if value == 0xFFFFFFFF or value & 0xFFFF0000 == 0xBADF0000:
        raise StopProbe("%s returned unreadable/PRI sentinel 0x%08X; no retry" % (name, value))
    if value < 0 or value >= (1 << width):
        raise StopProbe("%s has bits outside the survey's modeled width; no interpretation" % name)
    if name == "PMC_BOOT_0" and value != 0x170000A1:
        raise StopProbe("Unexpected GA100 chip signature; no more register reads")
    if name == "OPT_PCIE_DEVIDA" and (expected_device not in SUPPORTED_DEVICES or value != expected_device):
        raise StopProbe("Fuse SKU does not match the selected card's PCI device ID; no more register reads")


def bit_indices(mask, width):
    return [i for i in range(width) if mask & (1 << i)]


def interpret(values):
    result = {"classification_only_not_an_unlock_limit": True, "groups": {},
              "tpc_per_gpc": [], "verified_max_vram_gib": None,
              "verified_max_sm_count": None}
    for group, width in (("GPC", 8), ("FBP", 12), ("FBPA", 24), ("FBIO", 24)):
        disabled = values["OPT_%s_DISABLE" % group]
        defective = values["OPT_%s_DEFECTIVE" % group]
        effective = values["STATUS_OPT_%s" % group]
        result["groups"][group] = {
            "fuse_disabled_indices": bit_indices(disabled, width),
            "defect_marked_indices": bit_indices(defective, width),
            "disabled_without_defect_flag": bit_indices(disabled & ~defective, width),
            "effective_disabled_indices": bit_indices(effective, width),
            "effective_not_disabled_indices": bit_indices(~effective, width),
            "fuse_and_effective_mask_equal": disabled == effective,
            "ctrl_opt_raw": "0x%08X" % values["CTRL_OPT_%s" % group],
        }
    for gpc in range(8):
        disabled = values["OPT_TPC_DISABLE_GPC%d" % gpc]
        defective = values["OPT_TPC_GPC%d_DEFECTIVE" % gpc]
        effective = values["STATUS_OPT_TPC_GPC%d" % gpc]
        result["tpc_per_gpc"].append({
            "gpc": gpc,
            "parent_gpc_effectively_disabled": bool(values["STATUS_OPT_GPC"] & (1 << gpc)),
            "parent_gpc_defect_marked": bool(values["OPT_GPC_DEFECTIVE"] & (1 << gpc)),
            "fuse_disabled_indices": bit_indices(disabled, 8),
            "defect_marked_indices": bit_indices(defective, 8),
            "disabled_without_local_defect_flag": bit_indices(disabled & ~defective, 8),
            "effective_disabled_indices": bit_indices(effective, 8),
        })
    result["gate_readouts"] = {name: values[name] for name in (
        "EN_SW_OVERRIDE", "DISABLE_SW_OVERRIDE_STATUS", "OPT_SECURE_GSP_DEBUG_DIS")}
    return result


def _popcount(mask, width):
    return bin(mask & ((1 << width) - 1)).count("1")


def estimate_ceiling(values, device_id):
    """Theoretical SM / HBM ceiling from defect flags. Pure offline math.

    No hardware access. A unit without a defect flag is assumed reusable,
    which factory tags do not prove; this is an estimate, not a health test.
    SM = 2 x (TPCs whose parent GPC and the TPC itself are not defect-marked).
    HBM = (24 - defective FBPAs) x SKU-typical GiB/FBPA.
    """
    gpc_def = values["OPT_GPC_DEFECTIVE"] & 0xFF
    gpc_status = values["STATUS_OPT_GPC"] & 0xFF
    healthy_tpc = 0
    active_tpc = 0
    per_gpc = []
    for gpc in range(8):
        tpc_def = values["OPT_TPC_GPC%d_DEFECTIVE" % gpc] & 0xFF
        tpc_status = values["STATUS_OPT_TPC_GPC%d" % gpc] & 0xFF
        gpc_dead = bool(gpc_def & (1 << gpc))
        gpc_off = bool(gpc_status & (1 << gpc))
        healthy = 0 if gpc_dead else (8 - _popcount(tpc_def, 8))
        active = 0 if (gpc_dead or gpc_off) else (8 - _popcount(tpc_status, 8))
        healthy_tpc += healthy
        active_tpc += active
        per_gpc.append({
            "gpc": gpc,
            "defect_marked": gpc_dead,
            "defective_tpc_indices": bit_indices(tpc_def, 8),
            "healthy_tpc": healthy,
            "active_tpc": active,
        })

    fbpa_def = values["OPT_FBPA_DEFECTIVE"] & 0xFFFFFF
    fbpa_dis = values["OPT_FBPA_DISABLE"] & 0xFFFFFF
    fbpa_status = values["STATUS_OPT_FBPA"] & 0xFFFFFF
    fbp_def = values["OPT_FBP_DEFECTIVE"] & 0xFFF
    healthy_fbpa = 24 - _popcount(fbpa_def, 24)
    active_fbpa = 24 - _popcount(fbpa_status, 24)
    # SKU-typical HBM die density (assumption): 8GB SKU Hynix 16Gb ~4 GiB/FBPA,
    # 10GB SKU Samsung 8Gb ~2 GiB/FBPA.
    gib_per_fbpa = 4 if device_id == 0x20C2 else 2

    all_tpc_clean = all((values["OPT_TPC_GPC%d_DEFECTIVE" % g] & 0xFF) == 0 for g in range(8))
    return {
        "basis": "defect flags only; not a health test; non-defective != verified stable",
        "sm": {
            "healthy_tpc": healthy_tpc,
            "ceiling_sm": healthy_tpc * 2,
            "currently_active_sm": active_tpc * 2,
            "per_gpc": per_gpc,
        },
        "hbm": {
            "healthy_fbpa": healthy_fbpa,
            "defective_fbpa_indices": bit_indices(fbpa_def, 24),
            "reclaimable_fbpa_indices": bit_indices(fbpa_dis & ~fbpa_def, 24),
            "currently_active_fbpa": active_fbpa,
            "gib_per_fbpa_assumed": gib_per_fbpa,
            "ceiling_gib": healthy_fbpa * gib_per_fbpa,
            "currently_active_gib": active_fbpa * gib_per_fbpa,
            "fbp_defective_indices": bit_indices(fbp_def, 12),
        },
        "all_defect_flags_zero": (gpc_def == 0 and all_tpc_clean and fbpa_def == 0),
    }


# Known GA100 PCI device IDs (PCI ID Repository, vendor 10de). Rebrand targets
# are constants looked up here, NOT read from the card; the card only holds its
# current straps. A rebrand needs DEVID FF burns plus a compatible vBIOS.
GA100_DEVICE_IDS = {
    0x2080: "GA100 (base)",
    0x2082: "CMP 170HX 10GB",
    0x20B0: "A100 SXM4 40GB",
    0x20B1: "A100 PCIe 40GB",
    0x20B2: "A100 SXM4 80GB",
    0x20B3: "A100 SXM 64GB",
    0x20B5: "A100 PCIe 80GB",
    0x20B7: "A30 PCIe",
    0x20B8: "A100X",
    0x20BD: "A800 SXM4 40GB",
    0x20C0: "GA100 Initial DevID",
    0x20C2: "CMP 170HX 8GB",
    0x20F1: "A100 PCIe 40GB (alt)",
    0x20F3: "A800 SXM4 80GB",
    0x20F5: "A800 80GB PCIe",
    0x20F6: "A800 40GB PCIe",
}


def device_identity(values, pci_device_id):
    """Current PCI ID + both DEVID straps + known rebrand targets. Read-only."""
    devida = values["OPT_PCIE_DEVIDA"]
    devidb = values["OPT_PCIE_DEVIDB"]
    return {
        "current_pci_id": "0x%04X" % pci_device_id,
        "current_name": GA100_DEVICE_IDS.get(pci_device_id, "unknown"),
        "strap_devida": "0x%04X" % devida,
        "strap_devida_name": GA100_DEVICE_IDS.get(devida, "unknown"),
        "strap_devidb": "0x%04X" % devidb,
        "strap_devidb_name": GA100_DEVICE_IDS.get(devidb, "unknown"),
        "rebrand_targets": {
            "a100_pcie_80gb": "0x20B5",
            "a100_sxm4_80gb": "0x20B2",
            "a100_pcie_40gb": "0x20B1",
            "a800_pcie_80gb": "0x20F5",
        },
        "note": "DEVIDA/B are FF chain2 straps (offsets 37/38/39). Rebrand needs DEVID burns + a compatible signed vBIOS; targets are known GA100 IDs, not read from this card.",
    }


def collect(target, progress=lambda stage: None):
    report = new_report(target)
    bar = None
    try:
        progress("identity_and_existing_power_state")
        report["identity_check"] = preflight(target)
        identity = report["identity_check"]
        report["target"].update(pci_id=identity["pci_id"], sku=identity["sku"])
        progress("open_selected_sysfs_resource0_read_only")
        bar = ReadOnlyBar(target.device_path / "resource0")
        first = {}
        for pass_number in (1, 2):
            for index, (name, offset, width) in enumerate(REGISTERS):
                progress("pass_%d:%s@0x%08X" % (pass_number, name, offset))
                value = bar.read32(offset)
                if pass_number == 1:
                    report["registers"].append({"name": name, "offset": "0x%08X" % offset,
                                                "value": "0x%08X" % value, "second_read_matches": False})
                validate_word(name, value, width, identity["pci_device_id"])
                if pass_number == 1:
                    first[name] = value
                elif value != first[name]:
                    report["registers"][index]["second_value"] = "0x%08X" % value
                    raise StopProbe("%s changed between planned snapshots; no retry" % name)
                else:
                    report["registers"][index]["second_read_matches"] = True
        bar.close()
        bar = None
        progress("final_identity_check")
        if preflight(target) != report["identity_check"]:
            raise StopProbe("Identity or power/resource state changed during the snapshot")
        report["interpretation"] = interpret(first)
        report["ceiling_estimate"] = estimate_ceiling(first, identity["pci_device_id"])
        report["device_identity"] = device_identity(first, identity["pci_device_id"])
        report["complete"] = True
        report["status"] = "COMPLETE"
        report["matching_passes"] = 2
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        if bar is not None:
            bar.close()
    return report


def send_event(fd, event):
    payload = (json.dumps(event, separators=(",", ":")) + "\n").encode("ascii")
    while payload:
        count = os.write(fd, payload)  # private pipe only; never a device FD
        if count <= 0:
            raise RuntimeError("Result pipe closed")
        payload = payload[count:]


def supervise(target):
    """Keep a BAR0 SIGBUS/segfault or stuck child from looking like success."""
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:
        os.close(read_fd)
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            report = collect(target, lambda stage: send_event(write_fd, {"stage": stage}))
            send_event(write_fd, {"result": report})
        except BaseException:
            os._exit(3)
        finally:
            os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    buffer = b""
    last_stage = "starting_worker"
    result = None
    status = None
    failure = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([read_fd], [], [], min(0.25, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 262144:
                raise StopProbe("Unexpected worker response size")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                event = json.loads(line)
                last_stage = event.get("stage", last_stage)
                if "result" in event:
                    result = event["result"]
        else:
            failure = "Read worker exceeded 15 seconds; termination requested, no fallback"
    except (Exception, KeyboardInterrupt) as error:
        failure = "%s: %s" % (type(error).__name__, error)
    finally:
        os.close(read_fd)
        reaped, status = os.waitpid(pid, os.WNOHANG)
        # EOF can precede _exit() by a scheduling tick. Allow normal exit;
        # do not kill a successful worker just because that tick has not run.
        if not reaped and failure is None:
            grace_deadline = time.monotonic() + 0.25
            while not reaped and time.monotonic() < grace_deadline:
                time.sleep(0.01)
                reaped, status = os.waitpid(pid, os.WNOHANG)
        if not reaped:
            # Local process control only. No GPU reset or other recovery action.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            reap_deadline = time.monotonic() + 0.5
            while time.monotonic() < reap_deadline:
                reaped, status = os.waitpid(pid, os.WNOHANG)
                if reaped:
                    break
                time.sleep(0.01)
        if not reaped:
            failure = "Worker termination requested but OS has not confirmed exit; no further probing"
    if failure or result is None or not reaped or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        report = new_report(target)
        report["last_stage"] = last_stage
        report["worker_exit_confirmed"] = bool(reaped)
        report["error"] = {"type": "WorkerStopped", "message": failure or "Worker faulted or exited without a complete result"}
        if reaped and os.WIFSIGNALED(status):
            report["error"]["signal"] = os.WTERMSIG(status)
        return report
    result["last_stage"] = last_stage
    return result


def platform_error():
    if sys.platform != "linux" or sys.implementation.name != "cpython":
        return "Requires Linux CPython; no GPU access attempted"
    if platform.machine().lower() not in ("x86_64", "amd64") or sys.byteorder != "little":
        return "Requires little-endian x86-64; no GPU access attempted"
    if struct.calcsize("I") != 4 or mmap.PAGESIZE != PAGE_BYTES:
        return "Unsupported native uint32/page size; no GPU access attempted"
    if os.geteuid() != 0:
        return "Run with sudo; the script does not escalate itself"
    return None


class ExactlyOnce(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error("%s must be supplied exactly once" % option_string)
        setattr(namespace, self.dest, value)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bdf", required=True, type=normalize_bdf, action=ExactlyOnce,
                        help="One full PCI address, e.g. 0000:01:00.0 (no automatic selection)")
    parser.add_argument("--uuid", dest="gpu_uuid", required=True, type=normalize_uuid, action=ExactlyOnce,
                        help="Full physical GPU UUID; must match the existing driver's cached identity")
    return parser  # No all-devices, file-path, offset, bypass, write or identity-skip option.


def print_summary(report):
    if not report.get("complete"):
        return
    ce = report.get("ceiling_estimate")
    if not ce:
        return
    t = report["target"]
    sm, hbm = ce["sm"], ce["hbm"]
    lines = ["=== %s  %s ===" % (t.get("sku"), t.get("bdf")),
             "SM:  ceiling %d  (%d healthy TPC x2)   currently active %d"
             % (sm["ceiling_sm"], sm["healthy_tpc"], sm["currently_active_sm"])]
    defects = ["GPC%d[%s]%s" % (r["gpc"],
                                ",".join(map(str, r["defective_tpc_indices"])) or "-",
                                " whole-GPC" if r["defect_marked"] else "")
               for r in sm["per_gpc"] if r["defect_marked"] or r["defective_tpc_indices"]]
    lines.append("     defect flags: " + ("; ".join(defects) if defects else "none"))
    lines.append("HBM: ceiling %d GiB  (%d healthy FBPA x %d GiB)   active %d GiB (%d FBPA)"
                 % (hbm["ceiling_gib"], hbm["healthy_fbpa"], hbm["gib_per_fbpa_assumed"],
                    hbm["currently_active_gib"], hbm["currently_active_fbpa"]))
    lines.append("     defective FBPA (dead): %s" % (hbm["defective_fbpa_indices"] or "none"))
    lines.append("     reclaimable (disabled, not defective): %s" % (hbm["reclaimable_fbpa_indices"] or "none"))
    if ce["all_defect_flags_zero"]:
        lines.append("     all defect flags zero -> 128 SM / 96 GiB candidate (needs 6 populated stacks; unverified)")
    di = report.get("device_identity")
    if di:
        lines.append("ID:  current %s (%s) | DEVIDA %s | DEVIDB %s"
                     % (di["current_pci_id"], di["current_name"],
                        di["strap_devida"], di["strap_devidb"]))
        lines.append("     rebrand targets: A100 PCIe 80GB=0x20B5  A100 SXM4 80GB=0x20B2  A100 PCIe 40GB=0x20B1")
    lines.append("NOTE: defect-flag estimate, not a health test. Non-defective != verified stable.")
    for line in lines:
        print(line, file=sys.stderr)


def main(argv=None):
    args = build_parser().parse_args(argv)
    target = Target(args.bdf, args.gpu_uuid)
    problem = platform_error()
    if problem:
        report = new_report(target)
        report["error"] = {"type": "PreflightStopped", "message": problem}
    else:
        try:
            report = supervise(target)
        except (Exception, KeyboardInterrupt) as error:
            report = new_report(target)
            report["error"] = {"type": type(error).__name__, "message": str(error)}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    print("CMP snapshot: %s. GPU register writes: none. No fallback attempted." % report["status"], file=sys.stderr)
    print_summary(report)
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    sys.exit(main())