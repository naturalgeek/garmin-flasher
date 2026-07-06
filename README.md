# garmin-flasher

A native **Linux** tool to reflash the **MAIN firmware region** of a soft-bricked
Garmin handheld over Garmin's USB protocol, using the device's **preboot programming
interface** (`091e:0003`) — the low-level loader that comes up when the device will
no longer boot its normal firmware. **No Windows and no Garmin `Updater.exe` required.**

It is designed as a recovery tool: when a device only enumerates in loader/preboot
mode (because a previous flash left a bad MAIN region), this tool can stream a known-good
MAIN image back onto it.

---

> ## ⚠️ SCOPE / TESTED-ON — READ THIS FIRST
>
> **This tool has only been tested on the Garmin GPSMAP 276Cx (HWID 2479).**
> It **hard-codes 276Cx MAIN-region parameters** (region id, size, image hash).
> **Using it on any other Garmin model is untested and may permanently brick your
> device.** There is no model auto-detection and no safety net for other hardware.
>
> **Use entirely at your own risk.** See the disclaimer at the bottom.

---

## What it does and does not touch

- ✅ Flashes **only** the MAIN application region, Garmin region id **`0x02BD`**.
- ⛔ **Hard-refuses** the BOOT / ramloader region (`0x0008`, and the numeric aliases
  `12` / `8`). BOOT is the device's recovery escape hatch — if it is intact you can
  always get back into preboot mode. This tool will **never** write it, by design.

### Safety design

- **Read-only by default.** Running `flash_main.py` with no arguments performs a
  read-only self-test and an offline dry-run plan. **It sends no write or erase
  frames.** Only `--CONFIRM-FLASH` performs an actual write, and that flag is
  intentionally verbose and un-guessable so it cannot be triggered by accident.
- **MAIN only.** Every write path calls a guard that aborts unless the region id is
  exactly `0x02BD`, and that explicitly rejects the BOOT-class ids.
- **Image is verified before any write.** The MAIN image you supply must pass all of:
  - **length** == `18322432` bytes,
  - **additive checksum** `sum(bytes) % 256 == 0` (Garmin region checksum invariant),
  - **SHA-1 prefix** `d2d0f35f75d3…` (the known-good 276Cx MAIN image).

  If any check fails, the tool refuses to run. These values are **276Cx-specific**
  facts about the firmware, not secrets.

## How the protocol works

The 276Cx preboot loader speaks Garmin's USB protocol (GUSB) over a dedicated USB
interface:

- **Preboot interface**: `091e:0003` (vendor `0x091E`, product `0x0003`).
- **Three endpoints**:
  - bulk **OUT `0x01`** — host → device (commands and bulk data),
  - interrupt **IN `0x82`** — device → host protocol replies / ACKs,
  - bulk **IN `0x83`** — device → host bulk data.
- **GUSB packet header** (12 bytes, little-endian):

  ```
  [u8 layer][3 pad][u16 packetId][2 pad][u32 dataSize]  then dataSize payload bytes
  ```

  - **layer 0** = transport: `Start Session` (id `5`) → device replies
    `Session Started` (id `6`, payload = device unit id).
  - **layer 20** = application: product query and the flash commands.
- **Reply routing**: application-layer replies arrive as a small
  `Pid_Data_Available` (`0x02`) frame on the **interrupt IN** endpoint, which tells
  the host to then read the real packet from the **bulk IN** endpoint. The tool
  handles this two-step read automatically.
- **Flash sequence** (application layer):

  1. `0x4b` **announce** `{u16 regionId, u32 size}` — declare which region and how big,
  2. `0x4a` **status** — poll region status,
  3. `0x24` **data** — stream the image in **250-byte** chunks,
  4. `0x2d` **commit** — signal transfer complete.

## Requirements

- **Linux** and **Python 3**.
- **`pyusb`** (`pip install -r requirements.txt`) plus **libusb-1.0** installed on the
  system (e.g. `sudo apt install libusb-1.0-0`).
- A **udev rule** so a normal (non-root) user can open the device. Install it once:

  ```bash
  sudo ./install_udev.sh
  ```

  This writes `/etc/udev/rules.d/99-garmin.rules` granting access to all `091e` Garmin
  USB devices, then reloads udev. Unplug/replug the device afterward.

A quick virtualenv setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Preparing the MAIN image (you supply it)

**No firmware image ships with this tool** — Garmin firmware is copyrighted. You must
supply `main_0x02BD.bin`, the MAIN region extracted from **your own stock 276Cx
firmware `.gcd`**.

A `.gcd` is a flat record stream. From file offset 8, each record is:

```
[u16 type][u16 length][length bytes of body]   ... until type == 0xFFFF (end)
```

The MAIN region can span several records that all carry type id `0x02BD`. Concatenate
their bodies, in file order, to reconstruct the region image. Pseudocode:

```
off = 8
out = b""
while off + 4 <= len(gcd):
    rtype, rlen = read_u16_le(gcd, off), read_u16_le(gcd, off+2)
    off += 4
    if rtype == 0xFFFF: break
    body = gcd[off : off+rlen]
    off += rlen
    if rtype == 0x02BD:
        out += body   # concatenate all 0x02BD bodies
write("main_0x02BD.bin", out)
```

A helper is included:

```bash
python extract_main_region.py YOUR_STOCK_FIRMWARE.gcd -o main_0x02BD.bin
```

The result must be **18322432 bytes** and satisfy `sum(bytes) % 256 == 0`. The flasher
re-verifies this (plus the SHA-1 prefix) before writing.

## Usage

1. **Enter preboot mode on the device**: power the unit off, **hold the D-pad `Up`**,
   then connect the USB cable while still holding `Up`. The preboot programming
   interface (`091e:0003`) is live for roughly **33 seconds**, so work promptly.

2. **Read-only self-test** (safe, sends no writes):

   ```bash
   python flash_main.py
   ```

   Expected output: the image passes verification, the tool opens `091e:0003`, prints
   the discovered endpoints, sends `Start Session` and receives **`Session Started`**
   (with the device unit id), then prints the product data. It also prints an offline
   dry-run plan (chunk count, announce payload). This proves comms are working. No
   data is written.

3. **Flash** (writes the MAIN region — only when the self-test succeeded):

   ```bash
   python flash_main.py --CONFIRM-FLASH
   ```

   The tool re-opens the session, sends the `0x4b` announce, streams all `0x24` data
   chunks, sends the `0x2d` commit, and reads the ACK.

   > **Note on the CRC verify step.** After committing, the tool issues a built-in
   > `GetRgnChecksum` request (`0x3a4`). The **276Cx loader does not implement this
   > command**, so the request simply **times out**. This is **benign** — it is not a
   > sign of failure. Success is judged by (a) the full data stream completing, (b) the
   > commit ACK, and (c) the device booting normally afterward.

4. **Power-cycle the device** and confirm it boots the normal firmware.

## Recovery / entry mode

If the device is soft-bricked, the preboot loader is your way in:

- Power off → **hold D-pad `Up`** → **connect USB** (keep holding `Up`).
- Confirm the interface is present:

  ```bash
  lsusb | grep 091e     # look for 091e:0003
  ```

- The window is short (~33 s). If it closes before you flash, just repeat the entry
  sequence.

## Troubleshooting

- **Device shows up as USB mass storage, not `091e:0003`.** The preboot window
  expired (or the device booted). Power off and re-enter preboot mode (hold `Up`
  while connecting USB), then run the tool immediately.
- **`Access denied` / permission errors from pyusb.** The udev rule is not installed
  or hasn't taken effect. Run `sudo ./install_udev.sh` and replug the device (or run
  the tool with `sudo` as a one-off test).
- **`pyusb unavailable` / no backend.** Install libusb-1.0 (`sudo apt install
  libusb-1.0-0`) and `pip install -r requirements.txt`.
- **`Start Session` gets no reply.** You are likely not in preboot mode, or the
  window expired. Re-enter preboot and retry. The tool refuses to flash unless the
  read-only self-test first confirms comms.
- **Image verification fails.** Your `main_0x02BD.bin` is the wrong length, wrong
  checksum, or wrong hash. Re-extract it from a known-good stock 276Cx `.gcd`.

## License

Copyright (C) 2026 the garmin-flasher contributors.

This program is free software: you can redistribute it and/or modify it under the terms
of the **GNU General Public License version 3 (GPLv3)** as published by the Free Software
Foundation. It is distributed in the hope that it will be useful, but **WITHOUT ANY
WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the full license text in [LICENSE](LICENSE).

## Disclaimer

This is an **unofficial**, community tool. It is not affiliated with, endorsed by, or
supported by Garmin. Flashing firmware over a low-level loader is inherently risky and
can render a device unusable. It has only been tested on the GPSMAP 276Cx and hard-codes
that model's parameters. No warranty of any kind — see [LICENSE](LICENSE) (GPLv3). **You
are solely responsible for anything you do to your device.**
