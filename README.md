# 🖥️ Windows Disk Image Copier v1.1

A Windows utility designed to clone your complete Windows installation (operating system, installed apps, user profiles, settings, and partition structure) from one drive to another (SSD or HDD).

Particularly engineered to handle **deteriorating SSDs/HDDs** that suffer from unreadable sectors or slow flash blocks.

---

## 🚀 Quick Start

1. Plug in your target SSD/HDD (via internal SATA/NVMe slot or external USB enclosure/dock).
2. Double-click [DiskImageCopier.exe](file:///c:/Users/VIRGIL/Documents/Windows%20image%20transfer/DiskImageCopier.exe) (accept the Windows UAC Administrator prompt).
3. Under **Source Drive**, select your current Windows drive (marked with `⚙️` and recognized as the active system drive).
4. Under **Destination Drive**, select the connected replacement SSD/HDD.
5. Click **▶ Start Clone** and confirm the safety prompts.

---

## 🛠️ Key Capabilities & Safety Fixes (v1.1)

- **Direct DMA Aligned I/O**: Buffers are allocated with `VirtualAlloc` (4096-byte page alignment), ensuring compatibility with Win32 unbuffered disk access (`FILE_FLAG_NO_BUFFERING`) across 4Kn and 512e drives with zero memory copy overhead.
- **Granular 4KB Bad Sector Recovery**: Unlike standard cloners that discard an entire 4MB chunk when encountering an unreadable sector, this engine switches to surgical 4KB recovery blocks upon error. It rescues 99.9% of the good data within that block and only zero-fills the specific failing sectors.
- **Disk Boundary Clamping**: Prevents read-overshoot past the physical end of the disk, protecting the secondary/backup GPT header from accidental zero-fill.
- **Volume Lock & Dismount**: Automatically unmounts and dismounts destination volumes (including partitions without drive letters) to prevent Win32 Error 5 (`Access Denied`).
- **Post-Clone Partition Cache Update**: Dispatches `IOCTL_DISK_UPDATE_PROPERTIES` to force the Windows storage driver to immediately detect the new cloned partition table.
- **Safe Window Close Intercept**: Catches `WM_DELETE_WINDOW` to prevent corrupting destination drives if the app window is accidentally closed mid-clone.
- **Non-Blocking UI**: Drive scanning runs on a background worker so the GUI remains responsive.

---

## ⚠️ Important Post-Clone Notes

> [!WARNING]
> Because this creates an exact sector clone, the destination drive shares the identical Disk Signature / GUIDs as your original drive. 
> 
> Once the clone finishes:
> 1. **Power down your PC**.
> 2. **Physically disconnect or remove the old/deteriorating SSD**.
> 3. Power on and boot directly into the new cloned drive.
> 4. If your new drive is larger than the old drive, open **Disk Management** (`diskmgmt.msc`) after booting, right-click `C:`, and click **Extend Volume** to use the remaining space.
