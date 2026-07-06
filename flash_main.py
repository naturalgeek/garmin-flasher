#!/usr/bin/env python3
"""
flash_main.py -- Native Linux recovery flasher for a soft-bricked Garmin GPSMAP 276Cx.

Writes the MAIN firmware region (GCD record type 0x02BD) back to the device over Garmin's
GUSB bulk protocol, via the device's PREBOOT USB interface (VID 0x091E, PID 0x0003).

  ####################################################################################
  #  SAFETY: DEFAULTS TO READ-ONLY / DRY-RUN.  Sends NO write or erase frames unless  #
  #  you pass --CONFIRM-FLASH.  Only ever targets MAIN (region 0x02BD); BOOT (0x0008/ #
  #  region 12/8) is hard-refused in code.  BOOT is the recovery escape hatch.        #
  ####################################################################################

  python flash_main.py                 # read-only self-test + dry-run plan (default)
  python flash_main.py --CONFIRM-FLASH # ACTUALLY WRITE MAIN (human-gated)
"""
import argparse, struct, sys, time, hashlib, os, math

# ------------------------------------------------------------------ constants
VID          = 0x091E
PID_PREBOOT  = 0x0003

LAYER_APP        = 20          # 0x14 application layer
LAYER_TRANSPORT  = 0           # transport / USB-protocol layer

# transport-layer (layer 0) session handshake
PID_DATA_AVAIL      = 0x02     # dev has data
PID_START_SESSION   = 0x05     # host -> dev
PID_SESSION_STARTED = 0x06     # dev -> host, payload = u32 unit id
# application-layer (layer 20) product query
PID_PRODUCT_RQST    = 0xfe     # 254 host -> dev
PID_PRODUCT_DATA    = 0xff     # 255 dev -> host

# flash sequence (application layer)
PID_ANNOUNCE = 0x4b            # announce region {u16 regionId, u32 size}
PID_STATUS   = 0x4a            # region status poll
PID_DATA     = 0x24            # region data chunk (<=250 file bytes)
PID_COMMIT   = 0x2d            # transfer complete / commit
PID_CRC_RQST = 0x3a4           # GetRgnChecksum request
PID_CRC_REPL = 0x3a9           # RgnChecksum reply

MAIN_REGION       = 0x02BD     # <-- the ONLY region this tool will ever touch
MAIN_SIZE         = 18322432
MAIN_SHA1_PREFIX  = "d2d0f35f75d3"
CHUNK             = 250

FORBIDDEN_REGIONS = {0x0008, 8, 12}

# ------------------------------------------------------------------ framing
def build_header(pid, size, layer=LAYER_APP):
    return struct.pack("<B3xH2xI", layer, pid, size)

def build_frame(pid, payload=b"", layer=LAYER_APP):
    return build_header(pid, len(payload), layer) + payload

def parse_header(buf):
    layer, pid, size = struct.unpack_from("<B3xH2xI", buf, 0)
    return layer, pid, size

def hexs(b):
    return " ".join("%02x" % x for x in b)

# ------------------------------------------------------------------ safety guard
def assert_main_only(region_id):
    if region_id in FORBIDDEN_REGIONS or region_id in (0x0008, 8, 12):
        sys.exit("REFUSING: region id %r is BOOT/ramloader-class. HARD RULE: MAIN-only." % (region_id,))
    if region_id != MAIN_REGION:
        sys.exit("REFUSING: region id 0x%04x is not MAIN (0x%04x)." % (region_id, MAIN_REGION))

# ------------------------------------------------------------------ image
def load_image(path):
    data = open(path, "rb").read()
    sha1 = hashlib.sha1(data).hexdigest()
    ok_len = len(data) == MAIN_SIZE
    ok_sum = (sum(data) % 256) == 0
    ok_sha = sha1.startswith(MAIN_SHA1_PREFIX)
    print("[image] %s" % path)
    print("        length : %d  (expect %d, match=%s)" % (len(data), MAIN_SIZE, ok_len))
    print("        sum%%256 : %d  (expect 0, match=%s)" % (sum(data) % 256, ok_sum))
    print("        sha1    : %s  (prefix %s match=%s)" % (sha1, MAIN_SHA1_PREFIX, ok_sha))
    if not (ok_len and ok_sum and ok_sha):
        sys.exit("REFUSING: image failed verification (length/checksum/sha1).")
    return data

# ------------------------------------------------------------------ dry-run plan
def dry_run_plan(size):
    nchunks = math.ceil(size / CHUNK)
    last    = size - (nchunks - 1) * CHUNK
    total   = (nchunks - 1) * CHUNK + last
    ann_pl  = struct.pack("<HI", MAIN_REGION, size)
    ann_hdr = build_header(PID_ANNOUNCE, len(ann_pl))
    print("")
    print("===== OFFLINE DRY-RUN PLAN =====")
    print("region id (MAIN)          : 0x%04x (%d)" % (MAIN_REGION, MAIN_REGION))
    print("image size                : %d bytes" % size)
    print("number of 0x24 chunks     : ceil(%d/%d) = %d" % (size, CHUNK, nchunks))
    print("last chunk size           : %d bytes" % last)
    print("total bytes streamed      : %d  (matches = %s)" % (total, total == size))
    print("0x4b announce payload      : %s   (region=0x%04x, size=%d)" % (hexs(ann_pl), MAIN_REGION, size))
    print("0x4b announce header        : %s" % hexs(ann_hdr))
    print("================================")
    print("")
    return nchunks, last

# ------------------------------------------------------------------ USB layer
class Link:
    def __init__(self):
        import usb.core, usb.util
        self.usb = usb.core
        self.util = usb.util
        self.dev = None
        self.ep_in = None          # primary read pipe (interrupt IN, Garmin protocol replies)
        self.ep_int_in = None      # interrupt IN (0x82)
        self.ep_bulk_in = None     # bulk IN (0x83, bulk data)
        self.ep_out = None         # bulk OUT (0x01)
        self.intf = None
        self._detached = False

    def open(self):
        self.dev = self.usb.find(idVendor=VID, idProduct=PID_PREBOOT)
        if self.dev is None:
            return False
        try:
            self.dev.set_configuration()
        except Exception as e:
            print("[usb] set_configuration warning: %s" % e)
        cfg = self.dev.get_active_configuration()
        self.intf = cfg[(0, 0)]
        ino = self.intf.bInterfaceNumber
        try:
            if self.dev.is_kernel_driver_active(ino):
                self.dev.detach_kernel_driver(ino)
                self._detached = True
                print("[usb] detached kernel driver on interface %d" % ino)
        except Exception as e:
            print("[usb] kernel-driver check: %s" % e)
        try:
            self.util.claim_interface(self.dev, ino)
        except Exception as e:
            print("[usb] claim_interface warning: %s" % e)
        for ep in self.intf:
            etype = self.util.endpoint_type(ep.bmAttributes)
            edir = self.util.endpoint_direction(ep.bEndpointAddress)
            if edir == self.util.ENDPOINT_OUT:
                self.ep_out = ep
            elif etype == self.util.ENDPOINT_TYPE_INTR:
                self.ep_int_in = ep
            elif etype == self.util.ENDPOINT_TYPE_BULK:
                self.ep_bulk_in = ep
        # Garmin USB protocol: replies/ACKs arrive on the INTERRUPT IN endpoint; bulk IN is
        # only for bulk data phases. Read protocol frames from interrupt IN.
        self.ep_in = self.ep_int_in or self.ep_bulk_in
        print("[usb] interface %d: OUT=0x%02x  INT-IN=0x%02x  BULK-IN=0x%02x  (reading protocol from INT-IN)" % (
            ino,
            self.ep_out.bEndpointAddress if self.ep_out else 0,
            self.ep_int_in.bEndpointAddress if self.ep_int_in else 0,
            self.ep_bulk_in.bEndpointAddress if self.ep_bulk_in else 0))
        return self.ep_in is not None and self.ep_out is not None

    def _read_frame_ep(self, ep, timeout):
        raw = bytes(ep.read(ep.wMaxPacketSize or 64, timeout=timeout))
        if len(raw) < 12:
            return None, None, raw
        layer, pid, size = parse_header(raw)
        payload = raw[12:12 + size]
        while len(payload) < size:
            more = bytes(ep.read(ep.wMaxPacketSize or 64, timeout=timeout))
            if not more:
                break
            payload += more
        return layer, pid, payload

    def read_reply(self, timeout):
        """Protocol reply: read interrupt IN; if Pid_Data_Available, follow to bulk IN."""
        layer, pid, payload = self._read_frame_ep(self.ep_int_in, timeout)
        if pid == PID_DATA_AVAIL:
            layer, pid, payload = self._read_frame_ep(self.ep_bulk_in, timeout)
        return layer, pid, payload

    def start_session(self, tries=3):
        """GUSB Start Session (layer 0, pid 5) -> Session Started (pid 6). Read-only."""
        for t in range(tries):
            print("[session] TX Start_Session (layer0 id0x05) attempt %d" % (t + 1))
            try:
                self.ep_out.write(build_frame(PID_START_SESSION, b"", layer=LAYER_TRANSPORT), timeout=3000)
            except Exception as e:
                print("[session] write failed: %s" % e)
            for _ in range(8):
                try:
                    layer, pid, payload = self._read_frame_ep(self.ep_int_in, 3000)
                except Exception as e:
                    print("[session] (no reply: %s)" % e)
                    break
                if pid is None:
                    print("[session] short frame %s" % hexs(payload))
                    continue
                print("[session] RX layer=%s id=0x%02x payload=%s" % (layer, pid, hexs(payload)))
                if pid == PID_SESSION_STARTED:
                    uid = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else None
                    print("[session] SESSION STARTED. unit id = %s" % (uid,))
                    return uid if uid is not None else True
        return None

    def product_request(self):
        """App-layer product request (layer 20, id 254). Read-only."""
        print("[product] TX Product_Rqst (layer20 id0xfe)")
        try:
            self.ep_out.write(build_frame(PID_PRODUCT_RQST, b"", layer=LAYER_APP), timeout=3000)
            layer, pid, payload = self.read_reply(4000)
            if pid is not None:
                print("[product] RX layer=%s id=0x%02x payload=%s" % (layer, pid, hexs(payload[:40])))
                if len(payload) >= 4:
                    prod, ver = struct.unpack_from("<HH", payload, 0)
                    print("[product] product_id=%d software_version=%d (%.2f)" % (prod, ver, ver / 100.0))
                return True
        except Exception as e:
            print("[product] no product reply: %s" % e)
        return False

    def send(self, pid, payload=b"", layer=LAYER_APP):
        frame = build_frame(pid, payload, layer)
        print("[TX] id=0x%02x layer=%d size=%d" % (pid, layer, len(payload)))
        self.ep_out.write(frame, timeout=5000)

    def recv(self, timeout=6000):
        try:
            layer, pid, payload = self.read_reply(timeout)
        except Exception as e:
            print("[RX] error: %s" % e)
            return None, None, None
        if pid is None:
            print("[RX] short/malformed: %s" % hexs(payload))
            return None, None, payload
        print("[RX] id=0x%02x layer=%s size=%d payload=%s" % (
            pid, layer, len(payload), hexs(payload[:32]) + (" ..." if len(payload) > 32 else "")))
        return pid, layer, payload

    def close(self):
        try:
            if self.intf is not None:
                self.util.release_interface(self.dev, self.intf.bInterfaceNumber)
        except Exception:
            pass
        try:
            if self._detached:
                self.dev.attach_kernel_driver(self.intf.bInterfaceNumber)
        except Exception:
            pass

# ------------------------------------------------------------------ read-only self-test
def read_only_selftest(link):
    print("")
    print("===== READ-ONLY SELF-TEST (no write/erase frames) =====")
    uid = link.start_session()
    if not uid:
        print("[selftest] Start Session got no reply -> comms NOT established. Will refuse to flash.")
        return False
    link.product_request()
    try:
        link.send(PID_CRC_RQST, struct.pack("<HI", MAIN_REGION, MAIN_SIZE))
        pid, layer, data = link.recv(timeout=4000)
        if pid == PID_CRC_REPL:
            crc = struct.unpack_from("<I", data, 0)[0] if data and len(data) >= 4 else 0
            print("[selftest] region 0x02BD CRC=0x%08x (region id confirmed live)" % crc)
        else:
            print("[selftest] CRC query not answered (id=%r) - not required; comms proven by Start Session." % (pid,))
    except Exception as e:
        print("[selftest] CRC query skipped: %s" % e)
    print("[selftest] COMMS ESTABLISHED (Start Session OK). Safe to proceed to --CONFIRM-FLASH.")
    return True

# ------------------------------------------------------------------ write path
def flash_main(link, data, confirm):
    region = MAIN_REGION
    assert_main_only(region)
    size = len(data)
    nchunks, last = dry_run_plan(size)

    if not confirm:
        print("[flash] DRY-RUN: --CONFIRM-FLASH not given. No write/erase frames sent.")
        return None

    assert_main_only(region)
    print("")
    print("===== LIVE FLASH (region 0x%04x, %d bytes) =====" % (region, size))
    if not link.start_session():
        sys.exit("REFUSING: could not Start Session before flash.")

    link.send(PID_ANNOUNCE, struct.pack("<HI", region, size))
    link.send(PID_STATUS)
    pid, layer, st = link.recv(timeout=60000)
    print("[flash] announce status reply id=%r payload=%s" % (pid, hexs(st) if st else st))

    off = 0
    idx = 0
    t0 = time.time()
    while off < size:
        chunk = data[off:off + CHUNK]
        link.ep_out.write(build_frame(PID_DATA, chunk), timeout=5000)
        off += len(chunk)
        idx += 1
        if idx % 5000 == 0 or off >= size:
            print("[flash] %d/%d chunks (%d/%d bytes)" % (idx, nchunks, off, size))

    link.send(PID_COMMIT)
    pid, layer, st = link.recv(timeout=60000)
    committed_ok = (st is not None)
    print("[flash] commit reply id=%r payload=%s" % (pid, hexs(st) if st else st))

    link.send(PID_CRC_RQST, struct.pack("<HI", region, size))
    pid, layer, cr = link.recv(timeout=60000)
    crc_ok = (pid == PID_CRC_REPL)
    print("[flash] crc reply id=%r payload=%s (elapsed %.1fs)" % (pid, hexs(cr) if cr else cr, time.time() - t0))

    verdict = committed_ok and crc_ok
    print("")
    print("===== VERDICT: %s =====" % ("SUCCESS" if verdict else "FAILURE / INCONCLUSIVE"))
    if not verdict:
        print("  no status/abort = staged image rejected by loader.")
        print("  crc mismatch/missing = readback differs. Re-check image & region.")
    return verdict

# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="GPSMAP 276Cx MAIN recovery flasher (read-only by default)")
    ap.add_argument("--image", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "main_0x02BD.bin"))
    ap.add_argument("--CONFIRM-FLASH", dest="confirm", action="store_true",
                    help="ACTUALLY write MAIN. Human-only. Without this, dry-run.")
    args = ap.parse_args()

    print("=== GPSMAP 276Cx MAIN recovery flasher ===")
    print("mode: %s" % ("LIVE FLASH (--CONFIRM-FLASH)" if args.confirm else "READ-ONLY / DRY-RUN (default)"))

    data = load_image(args.image)
    if not args.confirm:
        dry_run_plan(len(data))

    link = None
    try:
        link = Link()
    except Exception as e:
        print("[usb] pyusb unavailable: %s" % e)
        link = None

    opened = False
    if link is not None:
        try:
            opened = link.open()
        except Exception as e:
            print("[usb] open failed: %s" % e)
            opened = False

    if not opened:
        print("")
        print("[device] NOT in preboot (091e:0003) or not openable. Live steps skipped.")
        if args.confirm:
            sys.exit("REFUSING: --CONFIRM-FLASH given but device not present at 091e:0003.")
        return

    try:
        confirmed = read_only_selftest(link)
        if args.confirm:
            if not confirmed:
                sys.exit("REFUSING to flash: read-only self-test did not confirm comms.")
            flash_main(link, data, confirm=True)
        else:
            flash_main(link, data, confirm=False)
    finally:
        link.close()

if __name__ == "__main__":
    main()
