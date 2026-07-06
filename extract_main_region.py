#!/usr/bin/env python3
"""
extract_main_region.py -- extract a single region from a Garmin .gcd firmware file.

A .gcd file is a flat sequence of records starting at byte offset 8:

    [u16 type][u16 length][length bytes of body] ...

repeated until a record of type 0xFFFF (end marker). A firmware region can be
split across several records that share the same type id; concatenating the
bodies of all records with a given type id, in file order, reconstructs the
region image.

This tool concatenates the bodies of all records whose type id matches the one
you ask for (default 0x02BD, the GPSMAP 276Cx MAIN application region) and
writes them to an output file.

    python extract_main_region.py FIRMWARE.gcd
    python extract_main_region.py FIRMWARE.gcd -o main_0x02BD.bin --region 0x02BD

Supply your OWN stock firmware .gcd. No firmware is distributed with this tool.
"""
import argparse
import struct
import sys

END_MARKER = 0xFFFF


def extract(gcd_path, region_id):
    data = open(gcd_path, "rb").read()
    off = 8  # records begin after the 8-byte file header
    out = bytearray()
    count = 0
    while off + 4 <= len(data):
        rtype, rlen = struct.unpack_from("<HH", data, off)
        off += 4
        if rtype == END_MARKER:
            break
        body = data[off:off + rlen]
        off += rlen
        if rtype == region_id:
            out += body
            count += 1
    if count == 0:
        sys.exit("No records of type 0x%04x found in %s" % (region_id, gcd_path))
    return bytes(out), count


def main():
    ap = argparse.ArgumentParser(description="Extract a region image from a Garmin .gcd")
    ap.add_argument("gcd", help="path to your stock firmware .gcd file")
    ap.add_argument("-o", "--out", default="main_0x02BD.bin", help="output image path")
    ap.add_argument("--region", default="0x02BD",
                    help="region type id (hex or decimal), default 0x02BD (276Cx MAIN)")
    args = ap.parse_args()

    region_id = int(args.region, 0)
    img, nrec = extract(args.gcd, region_id)
    with open(args.out, "wb") as f:
        f.write(img)

    print("region 0x%04x : %d record(s), %d bytes -> %s" % (region_id, nrec, len(img), args.out))
    print("sum%%256 = %d (expect 0 for a valid region image)" % (sum(img) % 256))


if __name__ == "__main__":
    main()
