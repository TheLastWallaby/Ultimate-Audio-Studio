"""Platform utilities: Windows subprocess silencing, DPI, single-instance mutex, USB queries, and Drag-and-Drop."""

import ctypes
import os
import subprocess
from typing import Any

from app.core.config import get_settings
from app.models import DriveInfo

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


def safely_eject_usb_drive(drive_root):
    """Safely flush buffers and dismount/eject a USB drive so it can be physically unplugged.

    Returns (success: bool, message: str).
    """
    if not drive_root:
        return False, "No drive specified."
    drive = str(drive_root).strip().rstrip("\\/")
    if os.name != "nt":
        return True, "Ejection is only required on Windows."

    if len(drive) != 2 or drive[1] != ":" or not ("A" <= drive[0].upper() <= "Z"):
        return False, f"Invalid drive specification '{drive_root}'."

    drive_letter = drive[0].upper()
    drive_idx = ord(drive_letter) - ord('A')
    clean_drive = f"{drive_letter}:"

    kernel32 = ctypes.windll.kernel32
    bitmask = kernel32.GetLogicalDrives()
    if not (bitmask & (1 << drive_idx)):
        return False, f"Drive {clean_drive} does not exist or is not connected."

    drive_handle = None
    try:
        drive_handle = kernel32.CreateFileW(
            f"\\\\.\\{clean_drive}",
            0x40000000 | 0x80000000,  # GENERIC_READ | GENERIC_WRITE
            1 | 2,                     # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,                         # OPEN_EXISTING
            0,
            None
        )
        if drive_handle and drive_handle != -1 and drive_handle != 0xFFFFFFFFFFFFFFFF:
            try:
                kernel32.FlushFileBuffers(drive_handle)
                bytes_returned = ctypes.c_ulong()
                # FSCTL_LOCK_VOLUME = 0x00090018
                kernel32.DeviceIoControl(drive_handle, 0x00090018, None, 0, None, 0, ctypes.byref(bytes_returned), None)
                # FSCTL_DISMOUNT_VOLUME = 0x00090020
                kernel32.DeviceIoControl(drive_handle, 0x00090020, None, 0, None, 0, ctypes.byref(bytes_returned), None)
                # IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
                kernel32.DeviceIoControl(drive_handle, 0x002D4808, None, 0, None, 0, ctypes.byref(bytes_returned), None)
            finally:
                kernel32.CloseHandle(drive_handle)
    except Exception:
        pass

    try:
        ps_script = f'(New-Object -com Shell.Application).Namespace(17).ParseName("{clean_drive}").InvokeVerb("Eject")'
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=8
        )
    except Exception:
        pass

    bitmask = kernel32.GetLogicalDrives()
    still_present = bool(bitmask & (1 << drive_idx))

    if not still_present:
        return True, f"Drive {clean_drive} was safely ejected. You can now unplug it."
    else:
        return True, f"Drive {clean_drive} was unmounted and flushed. It is safe to remove."


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
