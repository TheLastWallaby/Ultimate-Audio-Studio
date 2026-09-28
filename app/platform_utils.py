"""Platform utilities: Windows subprocess silencing, DPI, single-instance mutex, USB queries, and Drag-and-Drop."""

import ctypes
import logging
import os
import subprocess
import time
from typing import Any

from app.core.config import get_settings
from app.models import DriveInfo

logger = logging.getLogger(__name__)

_orig_popen: Any = subprocess.Popen

# Subprocess silencing: suppress flashing console windows on Windows
if os.name == "nt":
    CREATE_NO_WINDOW = 0x08000000

    class _SilentPopen(subprocess.Popen[Any]):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
            si = kwargs.get("startupinfo")
            if si is None:
                si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = si
            if kwargs.get("stdin") is None:
                kwargs["stdin"] = subprocess.DEVNULL
            super().__init__(*args, **kwargs)

    def apply_silent_popen():
        subprocess.Popen = _SilentPopen

    apply_silent_popen()
else:
    CREATE_NO_WINDOW = 0

    def apply_silent_popen():
        pass


def enable_windows_dpi():
    """Enable high-DPI scaling on Windows to avoid blurry text on high-resolution displays."""
    if os.name != "nt":
        return
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            from ctypes import windll
            windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_instance_mutex = None


def already_running():
    """Check if another instance of the application is already running via Win32 named mutex."""
    global _instance_mutex
    if os.name != "nt":
        return False
    # Use Local\ namespace so standard non-admin user accounts don't fail with ERROR_ACCESS_DENIED
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\UltimateAudioStudioSingleInstance")
    return kernel32.GetLastError() == 183


def release_instance_mutex():
    """Release and close the single-instance Win32 mutex handle so a restarted instance can start cleanly."""
    global _instance_mutex
    if os.name == "nt" and _instance_mutex:
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(_instance_mutex)
        except Exception:
            pass
        _instance_mutex = None


def clean_pyi_env():
    """Strip all PyInstaller runtime environment variables from os.environ and return a sanitized env copy."""
    for key in list(os.environ.keys()):
        if key.startswith("_PYI_") or key.startswith("_MEI") or key.startswith("PYI_"):
            os.environ.pop(key, None)
    return {
        k: v for k, v in os.environ.items()
        if not (k.startswith("_PYI_") or k.startswith("_MEI") or k.startswith("PYI_"))
    }


def launch_detached_gui(executable_path):
    """Launch a standalone GUI executable cleanly detached from the current process without hidden window flags."""
    if not os.path.exists(executable_path):
        raise FileNotFoundError(f"Executable not found: {executable_path}")

    # Strip PyInstaller runtime variables from current process environment
    # so child processes do not trigger PyInstaller's parent process security validation check
    clean_env = clean_pyi_env()

    if hasattr(os, "startfile"):
        try:
            os.startfile(executable_path)
            return True
        except Exception:
            pass

    # Fallback to unpatched Popen with detached process flags
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    _orig_popen([executable_path], env=clean_env, creationflags=creationflags, close_fds=True)
    return True


def _is_usb_bus_drive(drive_root):
    """Query Win32 storage device property to check if drive sits on a USB bus (BusTypeUsb == 7)."""
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        drive_letter = drive_root.rstrip("\\")
        h = kernel32.CreateFileW(
            f"\\\\.\\{drive_letter}",
            0,  # No access query
            1 | 2,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,  # OPEN_EXISTING
            0,
            None
        )
        if h == -1 or h == 0 or h == 0xFFFFFFFFFFFFFFFF:
            return False
        try:
            import struct
            query = (ctypes.c_int * 3)(0, 0, 0)
            out_buf = ctypes.create_string_buffer(1024)
            bytes_returned = ctypes.c_ulong()
            success = kernel32.DeviceIoControl(
                h,
                0x002D1400,  # IOCTL_STORAGE_QUERY_PROPERTY
                ctypes.byref(query),
                ctypes.sizeof(query),
                out_buf,
                ctypes.sizeof(out_buf),
                ctypes.byref(bytes_returned),
                None
            )
            if success and bytes_returned.value >= 32:
                bus_type = struct.unpack_from("<I", out_buf.raw, 28)[0]
                return bus_type == 7  # BusTypeUsb
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return False


def list_removable_drives():
    """Enumerate connected USB flash drives with volume label and filesystem type (e.g. FAT32, NTFS)."""
    drives = []
    if os.name != "nt":
        return drives
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetLogicalDrives.restype = ctypes.c_uint32
        kernel32.GetLogicalDrives.argtypes = []
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetVolumeInformationW.restype = ctypes.c_int
        kernel32.GetVolumeInformationW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p, ctypes.c_uint32
        ]
        bitmask = kernel32.GetLogicalDrives()
        label_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        sys_drive = get_settings().system.system_drive.upper() + "\\"
        for i in range(26):
            if bitmask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                if root.upper() == sys_drive:
                    continue
                dtype = kernel32.GetDriveTypeW(root)
                # DRIVE_REMOVABLE = 2, DRIVE_FIXED = 3 (checked via USB bus query)
                is_usb = (dtype == 2) or (dtype == 3 and _is_usb_bus_drive(root))
                if not is_usb:
                    continue
                label = "USB Drive"
                fs_type = "FAT32"
                try:
                    if kernel32.GetVolumeInformationW(root, label_buf, 261, None, None, None, fs_buf, 261):
                        label = label_buf.value or label
                        fs_type = fs_buf.value or fs_type
                except Exception:
                    pass
                drives.append(DriveInfo(root=root, display_label=f"USB Drive: {label} ({root}) [{fs_type}]", fs_type=fs_type))
    except Exception:
        pass
    return drives


_IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
_GUID_DEVINTERFACE_DISK = "{53f56307-b6bf-11d0-94f2-00a0c91efb8b}"
_DIGCF_PRESENT = 0x02
_DIGCF_DEVICEINTERFACE = 0x10
_CR_SUCCESS = 0
_PNP_VETO_OUTSTANDING_OPEN = 6
_EJECT_ATTEMPTS = 3


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("ClassGuid", _GUID),
        ("DevInst", ctypes.c_ulong),
        ("Reserved", ctypes.c_void_p),
    ]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("InterfaceClassGuid", _GUID),
        ("Flags", ctypes.c_ulong),
        ("Reserved", ctypes.c_void_p),
    ]


class _STORAGE_DEVICE_NUMBER(ctypes.Structure):
    _fields_ = [
        ("DeviceType", ctypes.c_ulong),
        ("DeviceNumber", ctypes.c_ulong),
        ("PartitionNumber", ctypes.c_ulong),
    ]


def _get_storage_device_number(device_path: str) -> tuple[int, int] | None:
    """Return (DeviceType, DeviceNumber) for a volume (\\\\.\\E:) or disk interface path."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateFileW(device_path, 0, 1 | 2, None, 3, 0, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        sdn = _STORAGE_DEVICE_NUMBER()
        returned = wintypes.DWORD()
        ok = kernel32.DeviceIoControl(
            handle, _IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
            ctypes.byref(sdn), ctypes.sizeof(sdn), ctypes.byref(returned), None,
        )
        return (int(sdn.DeviceType), int(sdn.DeviceNumber)) if ok else None
    finally:
        kernel32.CloseHandle(handle)


def _find_disk_devinst(drive_letter: str) -> int | None:
    """Find the PnP device instance of the physical disk that holds drive_letter (read-only lookup)."""
    from ctypes import wintypes

    target = _get_storage_device_number(f"\\\\.\\{drive_letter}:")
    if target is None:
        return None

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    guid = _GUID()
    if ole32.CLSIDFromString(ctypes.c_wchar_p(_GUID_DEVINTERFACE_DISK), ctypes.byref(guid)) != 0:
        return None

    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, ctypes.POINTER(_GUID), wintypes.DWORD,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA), wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_SP_DEVINFO_DATA),
    ]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

    dev_info = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if not dev_info or dev_info == wintypes.HANDLE(-1).value:
        return None
    # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize is 8 on 64-bit and 6 on 32-bit Windows.
    detail_cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
    try:
        index = 0
        while True:
            iface = _SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(dev_info, None, ctypes.byref(guid), index, ctypes.byref(iface)):
                return None
            index += 1
            required = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(dev_info, ctypes.byref(iface), None, 0, ctypes.byref(required), None)
            if required.value < 8:
                continue
            buf = ctypes.create_string_buffer(required.value)
            ctypes.c_ulong.from_buffer(buf).value = detail_cb_size
            devinfo = _SP_DEVINFO_DATA()
            devinfo.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                dev_info, ctypes.byref(iface), buf, required, None, ctypes.byref(devinfo)
            ):
                continue
            device_path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            if _get_storage_device_number(device_path) == target:
                return int(devinfo.DevInst)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(dev_info)


def _request_device_eject(devinst: int) -> tuple[bool, int, str]:
    """Ask Plug and Play to safely remove the device's parent (the USB device). Returns (ok, veto_type, veto_name)."""
    cfgmgr32 = ctypes.WinDLL("cfgmgr32")
    parent = ctypes.c_ulong()
    if cfgmgr32.CM_Get_Parent(ctypes.byref(parent), ctypes.c_ulong(devinst), 0) != _CR_SUCCESS:
        return False, 0, ""
    veto_type = ctypes.c_int(0)
    veto_name = ctypes.create_unicode_buffer(260)
    cr = cfgmgr32.CM_Request_Device_EjectW(parent, ctypes.byref(veto_type), veto_name, 260, 0)
    return (cr == _CR_SUCCESS and veto_type.value == 0), veto_type.value, veto_name.value


def _flush_volume(clean_drive: str) -> None:
    """Flush the volume's write cache so no buffered data is lost before removal."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(f"\\\\.\\{clean_drive}", 0x40000000 | 0x80000000, 1 | 2, None, 3, 0, None)
    if handle and handle != wintypes.HANDLE(-1).value:
        try:
            kernel32.FlushFileBuffers(handle)
        finally:
            kernel32.CloseHandle(handle)


def safely_eject_usb_drive(drive_root):
    """Flush and safely remove a USB drive through Windows Plug and Play (like 'Safely Remove Hardware').

    Only reports success once Windows has actually removed the drive, and explains when an open file
    or window blocks the removal. Returns (success: bool, message: str).
    """
    if not drive_root:
        return False, "No drive specified."
    drive = str(drive_root).strip().rstrip("\\/")
    if os.name != "nt":
        return True, "Ejection is only required on Windows."

    if len(drive) != 2 or drive[1] != ":" or not ("A" <= drive[0].upper() <= "Z"):
        return False, f"Invalid drive specification '{drive_root}'."

    drive_letter = drive[0].upper()
    drive_idx = ord(drive_letter) - ord("A")
    clean_drive = f"{drive_letter}:"

    kernel32 = ctypes.windll.kernel32
    if not (kernel32.GetLogicalDrives() & (1 << drive_idx)):
        return False, f"Drive {clean_drive} does not exist or is not connected."

    # Never ask Windows to remove an internal disk, even if called with the wrong letter.
    if not any(d.root.upper().startswith(clean_drive) for d in list_removable_drives()):
        return False, f"Drive {clean_drive} is not a removable USB drive."

    try:
        _flush_volume(clean_drive)
    except Exception as e:
        logger.warning("Flushing %s before eject failed: %s", clean_drive, e)

    veto_type, veto_name = 0, ""
    try:
        devinst = _find_disk_devinst(drive_letter)
        if devinst is None:
            return False, (
                f"Windows could not identify drive {clean_drive} for safe removal.\n"
                "Use the 'Safely Remove Hardware' icon in the taskbar instead."
            )
        for _attempt in range(_EJECT_ATTEMPTS):
            ok, veto_type, veto_name = _request_device_eject(devinst)
            if ok:
                break
            time.sleep(0.5)
    except Exception as e:
        logger.warning("Eject of %s failed: %s", clean_drive, e)

    # Trust only the result that matters: is the drive actually gone?
    for _ in range(20):
        if not (kernel32.GetLogicalDrives() & (1 << drive_idx)):
            return True, f"Drive {clean_drive} was safely ejected. You can now unplug it."
        time.sleep(0.1)

    if veto_type == _PNP_VETO_OUTSTANDING_OPEN or veto_type:
        logger.info("Eject of %s vetoed (type %s): %s", clean_drive, veto_type, veto_name)
        return False, (
            f"Drive {clean_drive} is still being used, so Windows did not eject it.\n"
            "Close any File Explorer windows or programs showing files on the drive, then try again."
        )
    return False, (
        f"Drive {clean_drive} could not be ejected.\n"
        "Close any programs using it and try again, or use the 'Safely Remove Hardware' icon in the taskbar."
    )


def get_desktop_dir():
    """Retrieve user's true Windows Desktop folder, handling OneDrive or network redirection."""
    if os.name == "nt":
        try:
            import ctypes.wintypes
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                if buf.value and os.path.isdir(buf.value):
                    return buf.value
        except Exception:
            pass
    desktop_std = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.exists(desktop_std):
        return desktop_std
    onedrive_desktop = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")
    if os.path.exists(onedrive_desktop):
        return onedrive_desktop
    return desktop_std


def find_windows_media_player():
    """Return the path of Windows Media Player Legacy (used to burn audio CDs), or None if not installed."""
    if os.name != "nt":
        return None
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            candidate = os.path.join(base, "Windows Media Player", "wmplayer.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


class Win32DragDropHandler:
    """Handles native Windows WM_DROPFILES messages and WM_DEVICECHANGE USB events without external C-extensions."""

    def __init__(self, root_window, callback, is_shutting_down_fn=None, device_change_callback=None):
        self.root = root_window
        self.callback = callback
        self.is_shutting_down_fn = is_shutting_down_fn
        self.device_change_callback = device_change_callback
        self._old_wndproc = None
        self._drop_target_hwnd = None
        self._drop_wndproc_c = None

    def setup(self):
        if os.name != "nt":
            return
        try:
            from ctypes import wintypes
            WM_DROPFILES = 0x0233
            WM_DEVICECHANGE = 0x0219
            GWLP_WNDPROC = -4

            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32

            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            parent_hwnd = user32.GetParent(hwnd)
            target_hwnd = parent_hwnd if parent_hwnd else hwnd

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

            def py_wndproc(h_wnd, msg, wparam, lparam):
                if msg == WM_DEVICECHANGE:
                    if self.device_change_callback and not (self.is_shutting_down_fn and self.is_shutting_down_fn()):
                        try:
                            self.root.after(600, self.device_change_callback)
                        except Exception:
                            pass
                    return user32.CallWindowProcW(self._old_wndproc, h_wnd, msg, wparam, lparam)

                if msg == WM_DROPFILES:
                    h_drop = wparam
                    try:
                        count = shell32.DragQueryFileW(h_drop, 0xFFFFFFFF, None, 0)
                        dropped_files = []
                        for i in range(count):
                            buf = ctypes.create_unicode_buffer(512)
                            shell32.DragQueryFileW(h_drop, i, buf, 512)
                            if buf.value:
                                dropped_files.append(buf.value)
                        shell32.DragFinish(h_drop)
                        if dropped_files:
                            if self.is_shutting_down_fn and self.is_shutting_down_fn():
                                return 0
                            self.root.after(0, self.callback, dropped_files)
                    except Exception:
                        pass
                    return 0
                return user32.CallWindowProcW(self._old_wndproc, h_wnd, msg, wparam, lparam)

            self._drop_wndproc_c = WNDPROC(py_wndproc)
            shell32.DragAcceptFiles(target_hwnd, True)

            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

            self._drop_target_hwnd = target_hwnd
            self._old_wndproc = user32.SetWindowLongPtrW(
                target_hwnd, GWLP_WNDPROC, ctypes.cast(self._drop_wndproc_c, ctypes.c_void_p).value
            )
        except Exception:
            pass

    def teardown(self):
        if os.name != "nt" or not self._old_wndproc or not self._drop_target_hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            GWLP_WNDPROC = -4
            user32.SetWindowLongPtrW(self._drop_target_hwnd, GWLP_WNDPROC, self._old_wndproc)
            self._old_wndproc = None
            self._drop_target_hwnd = None
        except Exception:
            pass
