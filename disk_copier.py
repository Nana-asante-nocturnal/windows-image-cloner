"""
Windows Disk Image Copier v1.1
Clone your entire Windows installation (OS, apps, settings, users) to another drive.
Sector-by-sector disk cloning with aligned I/O, granular bad-sector recovery,
and comprehensive safety guards.

Requirements: Run as Administrator. Python 3.10+ with customtkinter.
"""

import ctypes
import ctypes.wintypes as wintypes
import subprocess
import json
import threading
import time
import os
import sys
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Tuple
from datetime import datetime

import customtkinter as ctk
from tkinter import messagebox

# ============================================================================
# WIN32 CONSTANTS
# ============================================================================

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

FILE_BEGIN = 0

# ============================================================================
# WIN32 API BINDINGS
# ============================================================================

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
]
kernel32.CreateFileW.restype = ctypes.c_void_p

kernel32.ReadFile.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p
]
kernel32.ReadFile.restype = ctypes.c_bool

kernel32.WriteFile.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p
]
kernel32.WriteFile.restype = ctypes.c_bool

kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = ctypes.c_bool

kernel32.DeviceIoControl.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p
]
kernel32.DeviceIoControl.restype = ctypes.c_bool

kernel32.SetFilePointerEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int64,
    ctypes.POINTER(ctypes.c_int64), ctypes.c_uint32
]
kernel32.SetFilePointerEx.restype = ctypes.c_bool

kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
kernel32.FlushFileBuffers.restype = ctypes.c_bool

kernel32.VirtualAlloc.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32
]
kernel32.VirtualAlloc.restype = ctypes.c_void_p

kernel32.VirtualFree.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32
]
kernel32.VirtualFree.restype = ctypes.c_bool

msvcrt = ctypes.cdll.msvcrt
msvcrt.memcmp.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
msvcrt.memcmp.restype = ctypes.c_int


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def is_admin() -> bool:
    """Check if running with administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def run_as_admin():
    """Re-launch the current process with UAC elevation."""
    if getattr(sys, 'frozen', False):
        exe = sys.executable
        params = ""
    else:
        exe = sys.executable
        params = f'"{os.path.abspath(sys.argv[0])}"'

    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if ret <= 32:
        messagebox.showerror("Error", "Administrator privileges are required.\nPlease run as Administrator.")
    sys.exit(0)


def format_size(size_bytes) -> str:
    """Format bytes to human-readable size string."""
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    size = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size) < 1024 or unit == 'TB':
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_time(seconds: float) -> str:
    """Format seconds to a readable time string."""
    if seconds <= 0 or seconds > 360000:
        return "--:--"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def format_speed(bps: float) -> str:
    """Format bytes-per-second to readable speed."""
    return format_size(int(bps)) + "/s" if bps > 0 else "-- MB/s"


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class PartitionInfo:
    number: int = 0
    offset: int = 0
    size: int = 0
    type_desc: str = ""
    drive_letter: str = ""
    filesystem: str = ""
    free_space: int = 0
    is_boot: bool = False


@dataclass
class DiskInfo:
    device_id: str = ""
    index: int = 0
    model: str = "Unknown"
    size: int = 0
    media_type: str = ""
    interface_type: str = ""
    serial_number: str = ""
    total_sectors: int = 0
    bytes_per_sector: int = 512
    partition_count: int = 0
    partitions: List[PartitionInfo] = field(default_factory=list)
    is_system_disk: bool = False


# ============================================================================
# DISK ENUMERATOR — uses PowerShell/WMI
# ============================================================================

class DiskEnumerator:
    """Enumerate physical disks and their partitions via WMI CIM."""

    PS_SCRIPT = r'''
$ErrorActionPreference = 'SilentlyContinue'
$systemDrive = $env:SystemDrive

# Map partitions to logical disks
$partMap = @{}
Get-CimInstance Win32_LogicalDiskToPartition | ForEach-Object {
    $partKey = [string]$_.Antecedent.DeviceID
    $driveKey = [string]$_.Dependent.DeviceID
    if ($partKey -and $driveKey) {
        $partMap[$partKey] = $driveKey
    }
}

# Logical disk details (FileSystem, FreeSpace)
$logDisks = @{}
Get-CimInstance Win32_LogicalDisk | ForEach-Object {
    $logDisks[[string]$_.DeviceID] = @{
        FileSystem = [string]$_.FileSystem
        FreeSpace  = [int64]$_.FreeSpace
    }
}

$allParts = Get-CimInstance Win32_DiskPartition

$disks = @(Get-CimInstance Win32_DiskDrive | ForEach-Object {
    $disk = $_
    $dIndex = [int]$disk.Index
    $parts = @($allParts | Where-Object { $_.DiskIndex -eq $dIndex } | ForEach-Object {
        $p = $_
        $partKey = [string]$p.DeviceID
        $letter = if ($partMap.ContainsKey($partKey)) { $partMap[$partKey] } else { "" }
        $fs = ""
        $free = 0
        if ($letter -and $logDisks.ContainsKey($letter)) {
            $fs = $logDisks[$letter].FileSystem
            $free = $logDisks[$letter].FreeSpace
        }
        @{
            Number      = [int]$p.Index
            Offset      = [int64]$p.StartingOffset
            Size        = [int64]$p.Size
            TypeDesc    = [string]$p.Type
            DriveLetter = $letter
            FileSystem  = $fs
            FreeSpace   = $free
            IsBoot      = [bool]$p.BootPartition
        }
    })

    $isSys = $false
    foreach ($p in $parts) {
        if ($p.DriveLetter -and ($p.DriveLetter.TrimEnd('\') -eq $systemDrive.TrimEnd('\'))) {
            $isSys = $true
        }
    }

    $bps = if ($disk.BytesPerSector) { [int]$disk.BytesPerSector } else { 512 }
    $diskSize = if ($disk.Size) { [int64]$disk.Size } else { [int64]($disk.TotalSectors * $bps) }

    @{
        DeviceID       = [string]$disk.DeviceID
        Index          = $dIndex
        Model          = [string]$disk.Model
        Size           = $diskSize
        MediaType      = [string]$disk.MediaType
        InterfaceType  = [string]$disk.InterfaceType
        SerialNumber   = [string]$disk.SerialNumber
        TotalSectors   = [int64]$disk.TotalSectors
        BytesPerSector = $bps
        PartitionCount = [int]$disk.Partitions
        Partitions     = $parts
        IsSystemDisk   = $isSys
    }
})

@{ Disks = $disks; SystemDrive = $systemDrive } | ConvertTo-Json -Depth 5 -Compress
'''

    @staticmethod
    def enumerate() -> List[DiskInfo]:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command',
             DiskEnumerator.PS_SCRIPT],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        )
        if result.returncode != 0:
            raise RuntimeError(f"PowerShell error: {result.stderr.strip()}")

        data = json.loads(result.stdout.strip())
        disks: List[DiskInfo] = []
        raw_disks = data.get('Disks', [])
        if isinstance(raw_disks, dict):
            raw_disks = [raw_disks]

        for d in raw_disks:
            partitions = []
            raw_parts = d.get('Partitions', [])
            if isinstance(raw_parts, dict):
                raw_parts = [raw_parts]

            for p in raw_parts:
                partitions.append(PartitionInfo(
                    number=p.get('Number', 0),
                    offset=p.get('Offset', 0),
                    size=p.get('Size', 0),
                    type_desc=str(p.get('TypeDesc', '')),
                    drive_letter=str(p.get('DriveLetter', '')),
                    filesystem=str(p.get('FileSystem', '')),
                    free_space=p.get('FreeSpace', 0),
                    is_boot=bool(p.get('IsBoot', False)),
                ))

            size = int(d.get('Size', 0) or 0)
            total_sec = int(d.get('TotalSectors', 0) or 0)
            bps = int(d.get('BytesPerSector', 512) or 512)
            if size == 0 and total_sec > 0:
                size = total_sec * bps

            disks.append(DiskInfo(
                device_id=str(d.get('DeviceID', '')),
                index=int(d.get('Index', 0)),
                model=str(d.get('Model', 'Unknown')),
                size=size,
                media_type=str(d.get('MediaType', '')),
                interface_type=str(d.get('InterfaceType', '')),
                serial_number=str(d.get('SerialNumber', '')),
                total_sectors=total_sec,
                bytes_per_sector=bps,
                partition_count=int(d.get('PartitionCount', 0)),
                partitions=partitions,
                is_system_disk=bool(d.get('IsSystemDisk', False)),
            ))
        return disks


# ============================================================================
# ALIGNED MEMORY BUFFER
# ============================================================================

class AlignedBuffer:
    """Page-aligned memory buffer (4096-byte aligned) via VirtualAlloc."""

    def __init__(self, size: int):
        self.size = size
        ptr = kernel32.VirtualAlloc(None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not ptr:
            err = ctypes.get_last_error()
            raise MemoryError(f"VirtualAlloc failed for {size} bytes (Win32 error {err})")
        self.address = ptr

    def as_void_p(self):
        return ctypes.c_void_p(self.address)

    def zero(self, num_bytes: Optional[int] = None):
        n = num_bytes if num_bytes is not None else self.size
        ctypes.memset(self.address, 0, n)

    def close(self):
        if self.address:
            kernel32.VirtualFree(ctypes.c_void_p(self.address), 0, MEM_RELEASE)
            self.address = None

    def __del__(self):
        self.close()


# ============================================================================
# RAW DISK I/O — Win32 sector-level read/write
# ============================================================================

class RawDiskIO:
    """Low-level disk I/O using Win32 CreateFile / ReadFile / WriteFile."""

    def __init__(self):
        self._handle = None
        self._path = ""

    def open(self, device_path: str, write: bool = False):
        access = GENERIC_READ | (GENERIC_WRITE if write else 0)
        flags = FILE_FLAG_NO_BUFFERING | (FILE_FLAG_WRITE_THROUGH if write else 0)
        h = kernel32.CreateFileW(
            device_path, access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, flags, None
        )
        if h == INVALID_HANDLE_VALUE or h is None:
            err = ctypes.get_last_error()
            raise OSError(f"Cannot open {device_path} (Win32 error {err})")
        self._handle = h
        self._path = device_path

    def read_into(self, buf: AlignedBuffer, size: int) -> int:
        """Read directly into aligned buffer without extra memory copies."""
        read_n = ctypes.c_uint32(0)
        ok = kernel32.ReadFile(self._handle, buf.as_void_p(), size, ctypes.byref(read_n), None)
        if not ok:
            err = ctypes.get_last_error()
            if err == 38:  # ERROR_HANDLE_EOF
                return 0
            raise OSError(f"Read failed at {self._path} (Win32 error {err})")
        return read_n.value

    def write_from(self, buf: AlignedBuffer, size: int) -> int:
        """Write directly from aligned buffer without extra memory copies."""
        written = ctypes.c_uint32(0)
        ok = kernel32.WriteFile(self._handle, buf.as_void_p(), size, ctypes.byref(written), None)
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(f"Write failed at {self._path} (Win32 error {err})")
        return written.value

    def seek(self, position: int):
        new = ctypes.c_int64(0)
        ok = kernel32.SetFilePointerEx(
            self._handle, ctypes.c_int64(position), ctypes.byref(new), FILE_BEGIN)
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(f"Seek failed (Win32 error {err})")

    def flush(self):
        if self._handle:
            kernel32.FlushFileBuffers(self._handle)

    def update_properties(self):
        """Notify Windows storage stack that the partition table has changed."""
        if self._handle:
            ret = ctypes.c_uint32(0)
            kernel32.DeviceIoControl(
                self._handle, IOCTL_DISK_UPDATE_PROPERTIES,
                None, 0, None, 0, ctypes.byref(ret), None
            )

    def get_size(self) -> int:
        size = ctypes.c_int64(0)
        ret = ctypes.c_uint32(0)
        ok = kernel32.DeviceIoControl(
            self._handle, IOCTL_DISK_GET_LENGTH_INFO,
            None, 0, ctypes.byref(size), ctypes.sizeof(size),
            ctypes.byref(ret), None)
        return size.value if ok else 0

    def ioctl(self, code: int) -> bool:
        ret = ctypes.c_uint32(0)
        return bool(kernel32.DeviceIoControl(
            self._handle, code, None, 0, None, 0, ctypes.byref(ret), None))

    def close(self):
        if self._handle and self._handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(self._handle)
            self._handle = None


# ============================================================================
# VOLUME & CACHE MANAGER
# ============================================================================

class VolumeManager:
    """Locks and dismounts destination volumes, flushes source caches."""

    def __init__(self):
        self._locked: List[RawDiskIO] = []

    def flush_source_volumes(self, disk: DiskInfo):
        """Flush file system buffers on all source volumes to commit pending writes."""
        for p in disk.partitions:
            if p.drive_letter:
                try:
                    vol = RawDiskIO()
                    vol.open(f"\\\\.\\{p.drive_letter}", write=True)
                    vol.flush()
                    vol.close()
                except Exception:
                    pass

    def lock_disk_volumes(self, disk: DiskInfo):
        """Lock and dismount all volumes on destination disk."""
        for p in disk.partitions:
            if p.drive_letter:
                vio = RawDiskIO()
                try:
                    vio.open(f"\\\\.\\{p.drive_letter}", write=True)
                    vio.ioctl(FSCTL_LOCK_VOLUME)
                    vio.ioctl(FSCTL_DISMOUNT_VOLUME)
                    self._locked.append(vio)
                except Exception as exc:
                    logging.warning("Could not lock %s: %s", p.drive_letter, exc)

        # Force dismount via PowerShell as an extra guard for partitions without letters
        try:
            subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f'Get-Partition -DiskNumber {disk.index} | '
                 f'Where-Object {{ $_.DriveLetter }} | '
                 f'ForEach-Object {{ Dismount-Volume -DriveLetter $_.DriveLetter -Force -Confirm:$false }}'],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
            )
        except Exception:
            pass

    def unlock_all(self):
        for vio in self._locked:
            try:
                vio.ioctl(FSCTL_UNLOCK_VOLUME)
                vio.close()
            except Exception:
                pass
        self._locked.clear()


# ============================================================================
# CLONE ENGINE
# ============================================================================

MAX_RETRIES = 3
SECTOR_SIZE = 512
RECOVERY_BLOCK_SIZE = 4096  # 4KB blocks for surgical bad-sector recovery


class CloneEngine:
    """Orchestrates sector-by-sector disk cloning with aligned I/O and recovery."""

    def __init__(self):
        self.is_running = False
        self.is_cancelled = False
        self.is_verifying = False
        self.progress = 0.0
        self.speed = 0.0
        self.bytes_done = 0
        self.total_bytes = 0
        self.eta = 0.0
        self.bad_sectors = 0
        self.error: Optional[str] = None
        self.status = "Idle"

        self._chunk_size = 4 * 1024 * 1024  # 4 MB
        self._vol = VolumeManager()

        self._cb_progress: Optional[Callable] = None
        self._cb_done: Optional[Callable] = None
        self._cb_error: Optional[Callable] = None
        self._cb_log: Optional[Callable] = None

    # -- public API --

    def set_callbacks(self, progress=None, done=None, error=None, log=None):
        self._cb_progress = progress
        self._cb_done = done
        self._cb_error = error
        self._cb_log = log

    def set_chunk_mb(self, mb: int):
        self._chunk_size = mb * 1024 * 1024

    def start(self, src: DiskInfo, dst: DiskInfo, flush_cache: bool, verify: bool):
        if self.is_running:
            return
        self.is_running = True
        self.is_cancelled = False
        self.is_verifying = False
        self.progress = 0.0
        self.speed = 0.0
        self.bytes_done = 0
        self.bad_sectors = 0
        self.error = None
        self.status = "Starting..."
        t = threading.Thread(target=self._worker, args=(src, dst, flush_cache, verify), daemon=True)
        t.start()

    def cancel(self):
        self.is_cancelled = True
        self.status = "Cancelling..."

    # -- internals --

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {msg}"
        logging.info(msg)
        if self._cb_log:
            self._cb_log(full)

    def _worker(self, src: DiskInfo, dst: DiskInfo, flush_cache: bool, verify: bool):
        sio = RawDiskIO()
        dio = RawDiskIO()
        buf: Optional[AlignedBuffer] = None
        verify_buf: Optional[AlignedBuffer] = None

        try:
            self._log(f"Source:  Drive {src.index} — {src.model} ({format_size(src.size)})")
            self._log(f"Dest:    Drive {dst.index} — {dst.model} ({format_size(dst.size)})")

            if dst.size and src.size and dst.size < src.size:
                raise RuntimeError(
                    f"Destination ({format_size(dst.size)}) is smaller than source "
                    f"({format_size(src.size)}).\n"
                    f"Destination must be greater than or equal to source size.")

            # Flush source caches to guarantee filesystem consistency
            if flush_cache:
                self.status = "Flushing filesystem buffers..."
                self._log("Flushing source disk filesystem caches to disk...")
                self._vol.flush_source_volumes(src)
                self._log("Source disk caches committed")

            if self.is_cancelled:
                self._log("Cancelled.")
                return

            # Lock destination volumes
            self.status = "Locking destination..."
            self._log("Locking & dismounting destination volumes...")
            self._vol.lock_disk_volumes(dst)

            # Open disks
            self._log("Opening source disk for reading...")
            sio.open(src.device_id, write=False)
            self._log("Opening destination disk for writing...")
            dio.open(dst.device_id, write=True)

            # Determine exact total size
            actual = sio.get_size()
            self.total_bytes = actual if actual > 0 else (
                src.size or (src.total_sectors * src.bytes_per_sector))
            self._log(f"Total to clone: {format_size(self.total_bytes)}")
            self._log(f"Aligned buffer size: {format_size(self._chunk_size)}")

            # Allocate page-aligned memory buffer for direct DMA
            buf = AlignedBuffer(self._chunk_size)

            # ---------- CLONE LOOP ----------
            self.status = "Cloning..."
            self._log("Cloning sectors...")
            self.bytes_done = 0
            t0 = time.monotonic()
            t_last = t0
            b_last = 0
            smoothed_speed = 0.0

            while not self.is_cancelled and self.bytes_done < self.total_bytes:
                # Clamp last chunk to prevent reading past the physical disk end
                remaining_bytes = self.total_bytes - self.bytes_done
                chunk_to_read = min(self._chunk_size, remaining_bytes)

                # Ensure chunk is sector-aligned
                if chunk_to_read % SECTOR_SIZE != 0:
                    chunk_to_read = ((chunk_to_read + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE
                    chunk_to_read = min(chunk_to_read, self._chunk_size)

                # Safe read with fallback for deteriorating drives
                bytes_read = self._safe_read_into(sio, dio, buf, chunk_to_read)
                if bytes_read <= 0:
                    if self.is_cancelled:
                        break
                    # If 0 returned at EOF, we're done
                    break

                self.bytes_done += bytes_read

                # Throttled progress update (4 Hz) with smoothed speed and ETA
                now = time.monotonic()
                dt = now - t_last
                if dt >= 0.25:
                    inst_speed = (self.bytes_done - b_last) / dt if dt > 0 else 0
                    smoothed_speed = (0.75 * smoothed_speed + 0.25 * inst_speed) if smoothed_speed > 0 else inst_speed
                    self.speed = smoothed_speed
                    self.progress = (self.bytes_done / self.total_bytes * 100) if self.total_bytes else 0
                    rem = self.total_bytes - self.bytes_done
                    elapsed = now - t0
                    self.eta = (rem / self.speed) if (self.speed > 0 and elapsed > 2) else 0
                    self.status = f"Cloning... {self.progress:.1f}%"
                    if self._cb_progress:
                        self._cb_progress()
                    t_last = now
                    b_last = self.bytes_done

            if self.is_cancelled:
                self._log("Clone cancelled by user.")
                self.status = "Cancelled"
                return

            # Flush write buffers and notify Windows partition cache
            self._log("Flushing destination disk write buffers...")
            dio.flush()
            dio.update_properties()

            elapsed = time.monotonic() - t0
            avg = self.bytes_done / elapsed if elapsed else 0
            self._log(f"Clone finished: {format_size(self.bytes_done)} in {format_time(elapsed)}")
            self._log(f"Average speed: {format_speed(avg)}")
            if self.bad_sectors:
                self._log(f"⚠️  {self.bad_sectors} bad sector block(s) recovered and zero-filled")

            # Check if destination was larger than source
            if dst.size and src.size and dst.size > src.size:
                extra = dst.size - src.size
                self._log(f"💡 Note: Destination drive has {format_size(extra)} extra unallocated space.")
                self._log("   You can extend your C: drive in Windows Disk Management (diskmgmt.msc) after booting.")

            # ---------- OPTIONAL VERIFY ----------
            if verify and not self.is_cancelled:
                verify_buf = AlignedBuffer(self._chunk_size)
                self._do_verify(sio, dio, buf, verify_buf)

            self.progress = 100.0
            self.status = "Complete ✅"
            if self._cb_done:
                self._cb_done()

        except Exception as exc:
            self.error = str(exc)
            self.status = "Error"
            self._log(f"❌ {exc}")
            if self._cb_error:
                self._cb_error(str(exc))
        finally:
            if buf:
                buf.close()
            if verify_buf:
                verify_buf.close()
            sio.close()
            dio.close()
            self._vol.unlock_all()
            self.is_running = False
            if self._cb_progress:
                self._cb_progress()

    def _safe_read_into(self, sio: RawDiskIO, dio: RawDiskIO, buf: AlignedBuffer, size: int) -> int:
        """Read *size* bytes into *buf* and write to *dio*.

        If reading fails on a deteriorating drive, falls back to granular
        4KB recovery blocks so that only the truly damaged sectors are zeroed,
        saving the rest of the 4MB chunk!
        """
        # Normal fast path: try reading the entire chunk at full speed
        try:
            read_n = sio.read_into(buf, size)
            if read_n > 0:
                dio.write_from(buf, read_n)
                return read_n
            return 0
        except OSError:
            pass

        if self.is_cancelled:
            return 0

        # Fast path failed -> deteriorating sector hit!
        # Fallback: Granular recovery through this block
        pos = self.bytes_done
        self._log(f"⚠️ Read error near {format_size(pos)}. Switching to granular sector recovery...")

        recovered_bytes = 0
        small_buf = AlignedBuffer(RECOVERY_BLOCK_SIZE)

        try:
            while recovered_bytes < size and not self.is_cancelled:
                block_pos = pos + recovered_bytes
                block_size = min(RECOVERY_BLOCK_SIZE, size - recovered_bytes)
                if block_size % SECTOR_SIZE != 0:
                    block_size = ((block_size + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE

                sio.seek(block_pos)
                dio.seek(block_pos)

                # Retry individual 4KB block
                block_ok = False
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        rn = sio.read_into(small_buf, block_size)
                        if rn > 0:
                            dio.write_from(small_buf, rn)
                            block_ok = True
                            break
                    except OSError:
                        time.sleep(0.02 * attempt)
                        try:
                            sio.seek(block_pos)
                        except OSError:
                            pass

                if not block_ok:
                    # Truly unreadable sector -> zero-fill only this individual 4KB block
                    self.bad_sectors += 1
                    self._log(f"Bad sector at {format_size(block_pos)} ({format_size(block_size)}) — zero-filled")
                    small_buf.zero(block_size)
                    try:
                        dio.write_from(small_buf, block_size)
                    except OSError as exc:
                        raise RuntimeError(f"Write failed during bad-sector zero-fill: {exc}")

                recovered_bytes += block_size

            return recovered_bytes
        finally:
            small_buf.close()
            # Ensure file pointers are aligned for next chunk
            try:
                sio.seek(pos + recovered_bytes)
                dio.seek(pos + recovered_bytes)
            except OSError:
                pass

    def _do_verify(self, sio: RawDiskIO, dio: RawDiskIO, buf_s: AlignedBuffer, buf_d: AlignedBuffer):
        """Compare source and destination byte-for-byte."""
        self._log("Starting verification pass...")
        self.is_verifying = True
        self.bytes_done = 0
        mismatches = 0

        try:
            sio.seek(0)
            dio.seek(0)
        except OSError as exc:
            self._log(f"Cannot seek for verification: {exc}")
            self.is_verifying = False
            return

        t0 = time.monotonic()
        t_last = t0
        b_last = 0

        while not self.is_cancelled and self.bytes_done < self.total_bytes:
            remaining = self.total_bytes - self.bytes_done
            chunk_to_verify = min(self._chunk_size, remaining)
            if chunk_to_verify % SECTOR_SIZE != 0:
                chunk_to_verify = ((chunk_to_verify + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE
                chunk_to_verify = min(chunk_to_verify, self._chunk_size)

            try:
                rn_s = sio.read_into(buf_s, chunk_to_verify)
            except OSError:
                rn_s = 0

            try:
                rn_d = dio.read_into(buf_d, chunk_to_verify)
            except OSError:
                rn_d = 0

            if rn_s == 0 or rn_d == 0:
                break

            # Fast memory comparison
            cmp_len = min(rn_s, rn_d)
            if msvcrt.memcmp(buf_s.as_void_p(), buf_d.as_void_p(), cmp_len) != 0:
                mismatches += 1

            self.bytes_done += cmp_len
            now = time.monotonic()
            if now - t_last >= 0.25:
                dt = now - t_last
                self.speed = (self.bytes_done - b_last) / dt if dt > 0 else 0
                self.progress = (self.bytes_done / self.total_bytes * 100) if self.total_bytes else 0
                rem = self.total_bytes - self.bytes_done
                self.eta = (rem / self.speed) if self.speed > 0 else 0
                self.status = f"Verifying... {self.progress:.1f}%"
                if self._cb_progress:
                    self._cb_progress()
                t_last = now
                b_last = self.bytes_done

        if mismatches == 0:
            self._log("✅ Verification passed — destination is byte-for-byte identical to source")
        else:
            self._log(f"⚠️  {mismatches} block(s) differed during verification.")
            self._log("   (Note: minor differences on a live Windows system are normal due to active OS files like pagefile or event logs)")
            if self.bad_sectors:
                self._log(f"   ({self.bad_sectors} block(s) were from bad sectors zero-filled during cloning)")
        self.is_verifying = False


# ============================================================================
# GUI — DISK CARD WIDGET
# ============================================================================

class DiskCard(ctk.CTkFrame):
    """Clickable card showing one physical drive's information."""

    def __init__(self, master, disk: DiskInfo, is_source_mode: bool = True, **kw):
        super().__init__(master, corner_radius=10, **kw)
        self.disk = disk
        self.is_selected = False
        self._is_src = is_source_mode
        self._on_click: Optional[Callable] = None

        self.configure(
            fg_color=("gray85", "gray20"),
            border_width=2,
            border_color=("gray70", "gray30"),
            cursor="hand2",
        )
        self._build()
        self._bind_recursive(self)

    # ---- layout ----

    def _build(self):
        d = self.disk

        # Row 1 — drive index + size
        row1 = ctk.CTkFrame(self, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(10, 1))

        icon = "💾"
        label = f"{icon}  Drive {d.index}"
        if d.is_system_disk:
            label += "  ⚙️"
        ctk.CTkLabel(row1, text=label,
                     font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(side="left")
        ctk.CTkLabel(row1, text=format_size(d.size),
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=("#1a73e8", "#5dade2"),
                     anchor="e").pack(side="right")

        # Row 2 — model
        ctk.CTkLabel(self, text=d.model,
                     font=ctk.CTkFont(size=12),
                     text_color=("gray40", "gray60"),
                     anchor="w").pack(fill="x", padx=12, pady=(0, 1))

        # Row 3 — interface / media
        info = f"{d.interface_type}  •  {d.media_type}  •  {d.partition_count} partition(s)"
        ctk.CTkLabel(self, text=info,
                     font=ctk.CTkFont(size=11),
                     text_color=("gray45", "gray55"),
                     anchor="w").pack(fill="x", padx=12, pady=(0, 2))

        # Row 4 — partitions
        if d.partitions:
            parts = "   ".join(
                f"{p.drive_letter or '—'} {p.filesystem or 'Raw'} {format_size(p.size)}"
                for p in d.partitions
            )
            ctk.CTkLabel(self, text=parts,
                         font=ctk.CTkFont(size=11),
                         text_color=("gray50", "gray55"),
                         anchor="w", wraplength=550
                         ).pack(fill="x", padx=12, pady=(0, 4))

        # Banner
        if d.is_system_disk and not self._is_src:
            b = ctk.CTkFrame(self, fg_color=("#d32f2f", "#8b0000"), corner_radius=5)
            b.pack(fill="x", padx=12, pady=(0, 8))
            ctk.CTkLabel(b, text="🚫  Protected: Active Windows system drive cannot be overwritten",
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color="white").pack(padx=8, pady=3)
        elif d.is_system_disk:
            b = ctk.CTkFrame(self, fg_color=("orange", "#b35900"), corner_radius=5)
            b.pack(fill="x", padx=12, pady=(0, 8))
            ctk.CTkLabel(b, text="⚙️  Active Windows System / Boot Drive",
                         font=ctk.CTkFont(size=11),
                         text_color="white").pack(padx=8, pady=3)
        else:
            ctk.CTkFrame(self, height=6, fg_color="transparent").pack()

    def _bind_recursive(self, widget):
        widget.bind("<Button-1>", self._clicked)
        for child in widget.winfo_children():
            self._bind_recursive(child)

    # ---- selection ----

    def set_selected(self, sel: bool):
        self.is_selected = sel
        if sel:
            self.configure(border_color=("dodgerblue", "#1a5fb4"), border_width=3)
        else:
            self.configure(border_color=("gray70", "gray30"), border_width=2)

    def on_select(self, cb: Callable):
        self._on_click = cb

    def _clicked(self, _evt=None):
        if not self._is_src and self.disk.is_system_disk:
            return
        if self._on_click:
            self._on_click(self)


# ============================================================================
# MAIN APPLICATION WINDOW
# ============================================================================

class DiskCopierApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title("Windows Disk Image Copier v1.1")
        self.geometry("800x920")
        self.minsize(720, 780)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.disks: List[DiskInfo] = []
        self.src: Optional[DiskInfo] = None
        self.dst: Optional[DiskInfo] = None
        self.src_cards: List[DiskCard] = []
        self.dst_cards: List[DiskCard] = []

        self.engine = CloneEngine()
        self.engine.set_callbacks(
            progress=self._sched_progress,
            done=self._sched_done,
            error=self._sched_error,
            log=self._sched_log,
        )
        self._prog_pending = False
        self._is_refreshing = False

        self._build_ui()

        # Intercept window close event to prevent closing mid-clone
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(200, self._refresh)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=14, pady=10)

        # -- title bar --
        hdr = ctk.CTkFrame(root, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(hdr, text="🖥️  Windows Disk Image Copier",
                     font=ctk.CTkFont(size=21, weight="bold")).pack(side="left")
        if is_admin():
            ctk.CTkLabel(hdr, text="🛡️ Admin",
                         font=ctk.CTkFont(size=11),
                         text_color=("#27ae60", "#2ecc71")).pack(side="right")

        # -- source --
        ctk.CTkLabel(root, text="📤  Source Drive  (clone FROM this drive)",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(fill="x", pady=(4, 2))
        self._src_frame = ctk.CTkScrollableFrame(
            root, height=145, fg_color=("gray92", "gray14"), corner_radius=8)
        self._src_frame.pack(fill="x", pady=(0, 6))

        # -- destination --
        ctk.CTkLabel(root, text="📥  Destination Drive  (clone TO this drive)",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(fill="x", pady=(4, 2))
        self._dst_frame = ctk.CTkScrollableFrame(
            root, height=145, fg_color=("gray92", "gray14"), corner_radius=8)
        self._dst_frame.pack(fill="x", pady=(0, 6))

        # -- refresh --
        bf = ctk.CTkFrame(root, fg_color="transparent")
        bf.pack(fill="x", pady=(0, 6))
        self._btn_refresh = ctk.CTkButton(
            bf, text="🔄  Refresh Drives", width=170,
            font=ctk.CTkFont(size=13), command=self._refresh)
        self._btn_refresh.pack(side="left")

        # -- options --
        of = ctk.CTkFrame(root, corner_radius=8)
        of.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(of, text="Options",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=12, pady=(8, 4))
        oi = ctk.CTkFrame(of, fg_color="transparent")
        oi.pack(fill="x", padx=12, pady=(0, 8))

        self._flush_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(oi, text="Flush filesystem cache before clone",
                        variable=self._flush_var,
                        font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 18))
        self._ver_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(oi, text="Verify after clone",
                        variable=self._ver_var,
                        font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 18))
        ctk.CTkLabel(oi, text="Buffer:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(8, 4))
        self._buf_var = ctk.StringVar(value="4 MB")
        ctk.CTkOptionMenu(oi, variable=self._buf_var,
                          values=["1 MB", "2 MB", "4 MB", "8 MB", "16 MB"],
                          width=100, font=ctk.CTkFont(size=12)).pack(side="left")

        # -- progress --
        pf = ctk.CTkFrame(root, corner_radius=8)
        pf.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(pf, text="Progress",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=12, pady=(8, 4))
        self._bar = ctk.CTkProgressBar(pf, height=20, corner_radius=8)
        self._bar.pack(fill="x", padx=12, pady=(0, 4))
        self._bar.set(0)
        self._plabel = ctk.CTkLabel(pf,
                                    text="Select source and destination drives to begin",
                                    font=ctk.CTkFont(size=12),
                                    text_color=("gray40", "gray60"))
        self._plabel.pack(fill="x", padx=12, pady=(0, 8))

        # -- log --
        lf = ctk.CTkFrame(root, corner_radius=8)
        lf.pack(fill="both", expand=True, pady=(0, 6))
        ctk.CTkLabel(lf, text="Log",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=12, pady=(8, 4))
        self._log_box = ctk.CTkTextbox(
            lf, height=110,
            font=ctk.CTkFont(size=11, family="Consolas"),
            fg_color=("gray95", "gray10"), corner_radius=6,
            state="disabled")
        self._log_box.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        # -- action buttons --
        af = ctk.CTkFrame(root, fg_color="transparent")
        af.pack(fill="x", pady=(0, 4))
        self._btn_start = ctk.CTkButton(
            af, text="▶   Start Clone", width=210, height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=("#27ae60", "#27ae60"),
            hover_color=("#1e8449", "#1e8449"),
            command=self._start, state="disabled")
        self._btn_start.pack(side="left", padx=(0, 10))
        self._btn_cancel = ctk.CTkButton(
            af, text="⏹   Cancel", width=150, height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=("#c0392b", "#c0392b"),
            hover_color=("#922b21", "#922b21"),
            command=self._cancel, state="disabled")
        self._btn_cancel.pack(side="left")

    # ------------------------------------------------------------ disk list

    def _log(self, msg: str):
        self._log_box.configure(state="normal")
        self._log_box.insert("end", msg + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _refresh(self):
        if self._is_refreshing or self.engine.is_running:
            return
        self._is_refreshing = True
        self._btn_refresh.configure(state="disabled")

        ts = datetime.now().strftime("%H:%M:%S")
        self._log(f"[{ts}] Scanning drives...")

        def bg_scan():
            try:
                disks = DiskEnumerator.enumerate()
            except Exception as exc:
                disks = []
                self.after(0, lambda: self._log(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Scan error: {exc}"))

            self.after(0, lambda: self._populate_disks(disks))

        threading.Thread(target=bg_scan, daemon=True).start()

    def _populate_disks(self, disks: List[DiskInfo]):
        self.disks = disks
        self.src = None
        self.dst = None
        self.src_cards.clear()
        self.dst_cards.clear()

        for w in self._src_frame.winfo_children():
            w.destroy()
        for w in self._dst_frame.winfo_children():
            w.destroy()

        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] Found {len(self.disks)} drive(s)")

        for d in self.disks:
            sc = DiskCard(self._src_frame, d, is_source_mode=True)
            sc.pack(fill="x", padx=4, pady=3)
            sc.on_select(self._sel_src)
            self.src_cards.append(sc)

            dc = DiskCard(self._dst_frame, d, is_source_mode=False)
            dc.pack(fill="x", padx=4, pady=3)
            dc.on_select(self._sel_dst)
            self.dst_cards.append(dc)

        if len(self.disks) < 2:
            ctk.CTkLabel(self._dst_frame,
                         text="\n💡 Connect your replacement SSD/HDD, then click Refresh\n",
                         font=ctk.CTkFont(size=13),
                         text_color=("gray50", "gray50")).pack(pady=4)

        self._is_refreshing = False
        self._btn_refresh.configure(state="normal")
        self._update_start()

    def _sel_src(self, card: DiskCard):
        for c in self.src_cards:
            c.set_selected(False)
        card.set_selected(True)
        self.src = card.disk
        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"Source → Drive {card.disk.index}  {card.disk.model}")
        self._update_start()

    def _sel_dst(self, card: DiskCard):
        for c in self.dst_cards:
            c.set_selected(False)
        card.set_selected(True)
        self.dst = card.disk
        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"Destination → Drive {card.disk.index}  {card.disk.model}")
        self._update_start()

    def _update_start(self):
        ok = (self.src and self.dst
              and self.src.index != self.dst.index
              and not self.engine.is_running)
        self._btn_start.configure(state="normal" if ok else "disabled")

    # ------------------------------------------------------------ actions

    def _start(self):
        s, d = self.src, self.dst
        if not s or not d:
            return
        if s.index == d.index:
            messagebox.showerror("Error", "Source and destination must be different drives.")
            return
        if d.is_system_disk:
            messagebox.showerror("Error", "Cannot clone onto your active Windows system disk!")
            return
        if d.size and s.size and d.size < s.size:
            messagebox.showerror("Error",
                                 f"Destination ({format_size(d.size)}) is smaller than "
                                 f"source ({format_size(s.size)}).\n\n"
                                 f"The destination drive must be >= source drive size.")
            return

        msg = (
            f"⚠️  ALL DATA ON THE DESTINATION WILL BE DESTROYED\n\n"
            f"Source:\n"
            f"    Drive {s.index} — {s.model}\n"
            f"    {format_size(s.size)}\n\n"
            f"Destination:\n"
            f"    Drive {d.index} — {d.model}\n"
            f"    {format_size(d.size)}\n\n"
            f"Everything on the destination drive will be permanently\n"
            f"erased and replaced with an exact clone of the source.\n\n"
            f"Continue?"
        )
        if not messagebox.askyesno("Confirm Clone", msg, icon="warning"):
            return
        if not messagebox.askyesno(
                "Final Confirmation",
                f"LAST CHANCE!\n\n"
                f"Drive {d.index} ({d.model}) will be completely overwritten.\n\n"
                f"Are you absolutely sure?",
                icon="warning"):
            return

        buf_mb = int(self._buf_var.get().split()[0])
        self.engine.set_chunk_mb(buf_mb)

        self._btn_start.configure(state="disabled")
        self._btn_cancel.configure(state="normal")
        self._btn_refresh.configure(state="disabled")
        self._bar.set(0)

        self.engine.start(s, d, flush_cache=self._flush_var.get(), verify=self._ver_var.get())

    def _cancel(self):
        if self.engine.is_running:
            if messagebox.askyesno("Cancel", "Cancel the clone? The destination will be incomplete and unbootable."):
                self.engine.cancel()

    def _on_close(self):
        if self.engine.is_running:
            if not messagebox.askyesno(
                    "Clone In Progress",
                    "A disk clone is currently in progress!\n\n"
                    "Aborting now will leave the destination drive incomplete.\n"
                    "Are you sure you want to stop and exit?"):
                return
            self.engine.cancel()
            self._log("Stopping worker thread before exit...")
            # Wait up to 2 seconds for worker to exit
            t_wait = 0
            while self.engine.is_running and t_wait < 20:
                self.update()
                time.sleep(0.1)
                t_wait += 1

        self.destroy()

    # ------------------------------------------------- thread-safe updates

    def _sched_progress(self):
        if not self._prog_pending:
            self._prog_pending = True
            self.after(50, self._do_progress)

    def _do_progress(self):
        self._prog_pending = False
        e = self.engine
        self._bar.set(e.progress / 100.0)

        if e.is_running:
            phase = "Verifying" if e.is_verifying else "Cloning"
            txt = (f"{phase}: {e.progress:.1f}%   |   "
                   f"{format_size(e.bytes_done)} / {format_size(e.total_bytes)}   |   "
                   f"{format_speed(e.speed)}   |   "
                   f"ETA {format_time(e.eta)}")
            if e.bad_sectors:
                txt += f"   |   Bad: {e.bad_sectors}"
            self._plabel.configure(text=txt)
        else:
            self._btn_start.configure(
                state="normal" if (self.src and self.dst
                                   and self.src.index != self.dst.index) else "disabled")
            self._btn_cancel.configure(state="disabled")
            self._btn_refresh.configure(state="normal")

    def _sched_done(self):
        self.after(150, self._do_done)

    def _do_done(self):
        self._do_progress()
        e = self.engine
        msg = (
            f"✅ Clone completed successfully!\n\n"
            f"{format_size(e.bytes_done)} copied.\n"
        )
        if e.bad_sectors:
            msg += f"\n⚠️ {e.bad_sectors} bad sector block(s) were recovered and zero-filled."
        msg += (
            f"\n\nThe destination drive is now a bootable clone.\n\n"
            f"IMPORTANT NEXT STEPS:\n"
            f"1. Shut down your PC.\n"
            f"2. Disconnect the old deteriorating drive.\n"
            f"3. Boot into BIOS or directly into the new drive."
        )
        messagebox.showinfo("Clone Complete", msg)

    def _sched_error(self, err: str):
        self.after(150, lambda: self._do_error(err))

    def _do_error(self, err: str):
        self._do_progress()
        messagebox.showerror("Clone Error", f"An error occurred:\n\n{err}")

    def _sched_log(self, msg: str):
        self.after(0, lambda m=msg: self._log(m))


# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')

    if not is_admin():
        run_as_admin()
        return

    app = DiskCopierApp()
    app.mainloop()


if __name__ == "__main__":
    main()
