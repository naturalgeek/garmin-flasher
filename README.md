# garmin-flasher

A native **Linux** tool to reflash the **MAIN firmware region** of a soft-bricked
Garmin handheld over Garmin's USB protocol, using the device's **preboot programming
interface** (`091e:0003`) — the low-level loader that comes up when the device will
no longer boot its normal firmware. **No Windows and no Garmin `Updater.exe` required.**

It is designed as a recovery tool: when a device only enumerates in loader/preboot
mode (because a previous flash left a bad MAIN region), this tool streams a known-good
MAIN image back onto it over USB.

> ✅ **Confirmed working:** used to recover a real GPSMAP 276Cx that had been
> soft-bricked by a bad MAIN write — from Linux, over USB, without touching the BOOT
> region. See "Field notes" below for the exact working sequence.

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

## Two region numbers — don't confuse them

Recovery involves **two different numbering schemes** for the same MAIN firmware. This
trips people up (it tripped us up):

| Context | MAIN identifier | Used for |
|---|---|---|
| **GCD file** record type | `0x02BD` | **Extracting** the MAIN bytes from a stock `.gcd` |
| **Preboot loader protocol** region id | **`14` (`0x000E`)** | The `0x4b` **announce** sent over USB |

So you *extract* the image using GCD record type `0x02BD`, but the flasher *announces*
it to the loader as region **14**. Announcing `0x02BD` (701) to the preboot loader is
rejected as an invalid region (it returns status `11`). The tool uses region **14**.

## What it does and does not touch

- ✅ Flashes **only** the MAIN application region — loader region id **`14` (`0x000E`)**,
  a.k.a. `fw_all.bin`.
- ⛔ **Hard-refuses** the BOOT / ramloader and low-level loader regions: `12`
  (`boot.bin` ramloader), `8` / `0x0008` (GCD BOOT record type), `5` (u-boot), `43`
  (x-loader). BOOT is the device's recovery escape hatch — if it is intact you can
  always get back into preboot mode. This tool will **never** write those, by design.

### Safety design

- **Read-only by default.** Running `flash_main.py` with no arguments performs a
  read-only self-test and an offline dry-run plan. **It sends no write or erase
  frames.** Only `--CONFIRM-FLASH` performs an actual write, and that flag is
  intentionally verbose so it cannot be triggered by accident.
- **MAIN only.** Every write path calls a guard that aborts unless the region id is
  exactly `14`, and that explicitly rejects the BOOT-class ids above.
- **Aborts before streaming if the loader rejects the region.** After the `0x4b`
  announce, the tool waits for an **erase-ready status of `0`**. If the status is
  non-zero (e.g. `11` = invalid region), it **aborts before sending any data** — so a
  wrong region never leaves the device in a half-written, hung state.
- **Image is verified before any write.** The MAIN image you supply must pass all of:
  - **length** == `18322432` bytes,
  - **additive checksum** `sum(bytes) % 256 == 0` (Garmin region checksum invariant),
  - **SHA-1 prefix** `d2d0f35f75d3…` (the known-good 276Cx stock 5.90 MAIN image).

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
- **Reply routing (important).** Application-layer replies arrive in two steps: a small
  `Pid_Data_Available` (`0x02`) frame on the **interrupt IN** endpoint, which tells the
  host to then read the real packet from the **bulk IN** endpoint. Small transport
  replies (`Session Started`) and the flash status come on the **interrupt IN**
  endpoint directly. The tool handles both, and **skips zero-length keep-alive packets**
  on the interrupt endpoint (mistaking one of those for a reply was a bug we hit — it
  causes streaming to start before the region is erased).
- **Flash sequence** (application layer):

  1. `0x4b` **announce** `{u16 regionId, u32 size}` — declare region `14` and size.
  2. `0x4a` **status** — poll; **wait for status `0`** (region erased / ready) **before
     streaming.** A non-zero status here means the region was rejected → abort.
  3. `0x24` **data** — stream the image in **250-byte** chunks. **Each packet body is
     `[u32 offset_LE][data]`** where `offset` is the running byte position in the region
     (starts at 0, increments by the chunk length). **Omitting this 4-byte offset prefix
     corrupts the staged image** — the loader reads your first 4 data bytes as the write
     address — and the commit is rejected with **status 11** ("unable to program region").
  4. `0x2d` **commit** `{u16 region}` — signal transfer complete; then poll `0x4a`
     `{u16 region}` and expect **status 0**. On a status-0 commit the **loader reboots
     itself into the new firmware** (no host reboot packet exists; it auto-restarts on a
     clean completion).

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

The MAIN region can span several records that all carry **GCD record type id `0x02BD`**.
Concatenate their bodies, in file order, to reconstruct the region image. (Note: this
`0x02BD` is the *GCD record type*, not the loader region id — see "Two region numbers"
above.) Pseudocode:

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
   interface (`091e:0003`) is live for roughly **33 seconds**, so work promptly. (Once
   the tool is talking to it, it stays up — the window only matters for the first
   contact.)

2. **Read-only self-test** (safe, sends no writes):

   ```bash
   python flash_main.py
   ```

   Expected output: the image passes verification, the tool opens `091e:0003`, prints
   the discovered endpoints (`OUT=0x01 INT-IN=0x82 BULK-IN=0x83`), sends `Start Session`
   and receives **`Session Started`** (with the device unit id), then prints the product
   data (HWID + firmware version). It also prints an offline dry-run plan. This proves
   comms are working. No data is written.

3. **Flash** (writes the MAIN region — only when the self-test succeeded):

   ```bash
   python flash_main.py --CONFIRM-FLASH
   ```

   The tool re-opens the session, sends the `0x4b` announce for region `14`, **waits for
   the erase-ready status `0`**, streams all `0x24` data chunks (each with its offset
   prefix), sends the `0x2d` commit, and polls the final status.

   > **Commit status.** A correct flash returns commit **status `0`** — the loader
   > accepted the region and will **reboot itself** into the new firmware. A **status
   > `11`** means the staged image was rejected (almost always a malformed `0x24` data
   > frame — missing the `[u32 offset]` prefix — or a wrong region id). The follow-up
   > `GetRgnChecksum` (`0x3a4`) verify command is **not implemented by this loader** and
   > simply times out; that specific timeout is benign. Do **not** interrupt the device
   > while it writes.

4. **Device auto-reboots.** On a clean status-0 commit the loader restarts on its own —
   you'll see **"Software Loading"** and it comes up on the new firmware. **No battery
   pull needed.** (If a flash was *rejected* — status 11 — the loader may sit at
   "loading loader"; power-cycle, fix the framing/region, and retry.)

## Field notes

Recovery flow with this (fixed) tool:

1. Device bricked → only enters preboot (`091e:0003`), stable.
2. `install_udev.sh` (once) so pyusb can open the device as a normal user.
3. Enter preboot; `python flash_main.py` → `Session Started` + product data (confirms
   comms and the right device).
4. `python flash_main.py --CONFIRM-FLASH` → announce region `14` → **erase-ready
   status `0`** → stream 18 MB (~73k `0x24` packets, each `[u32 offset][250 data]`) →
   commit → **status `0`**.
5. **Device auto-reboots** → shows **"Software Loading"**, loads the firmware, boots normally.

Four bugs were fixed while developing this against a real 276Cx (each cost a flash
attempt): announcing GCD type `0x02BD` instead of loader region `14` (rejected, status
`11`); reading replies from bulk IN instead of interrupt IN (`0x82`); mistaking a
zero-length interrupt keep-alive for the erase-ready reply (streaming before erase); and
**sending `0x24` data without the `[u32 offset]` prefix** (loader scrambles the image →
commit rejected with status `11` → device hangs at "loading loader" and never
auto-reboots). All four are handled in the current code.

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
- **`ABORTING before stream: erase-ready status=11`.** The loader rejected the region
  id. This build only ever announces region `14`; if you see this, re-enter preboot and
  retry — a stale/half state can cause it. The tool deliberately aborts here instead of
  hanging the device.
- **`Access denied` / permission errors from pyusb.** The udev rule is not installed
  or hasn't taken effect. Run `sudo ./install_udev.sh` and replug the device (or run
  the tool with `sudo` as a one-off test).
- **`pyusb unavailable` / no backend.** Install libusb-1.0 (`sudo apt install
  libusb-1.0-0`) and `pip install -r requirements.txt`.
- **`Start Session` gets no reply.** You are likely not in preboot mode, or the
  window expired. Re-enter preboot and retry. The tool refuses to flash unless the
  read-only self-test first confirms comms.
- **Commit reports status `11` / CRC times out.** Benign on the 276Cx (see the note in
  Usage). Power-cycle and check whether it boots — that is the real success test.
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
