#!/usr/bin/env python3
"""Validate the committed component icons without regenerating them.

The committed Resources/logo.png and logo.svg are the source of truth. This
check only verifies that both exist, that logo.png has a PNG signature and a
valid IHDR chunk, and that logo.svg is well-formed XML with an <svg> root.
"""
import argparse
import json
from pathlib import Path
import struct
import sys
import xml.etree.ElementTree as ET
import zlib

ROOT = Path(__file__).resolve().parents[1]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def check_png(path):
    if not path.is_file():
        raise ValueError("icon is missing: " + str(path))
    data = path.read_bytes()
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("icon is not a PNG (bad signature): " + str(path))
    if len(data) < 33:
        raise ValueError("icon PNG is truncated: " + str(path))
    length, kind = struct.unpack(">I4s", data[8:16])
    if kind != b"IHDR" or length != 13:
        raise ValueError("icon PNG does not start with a valid IHDR chunk: " + str(path))
    payload = data[16:29]
    checksum = struct.unpack(">I", data[29:33])[0]
    if checksum != zlib.crc32(kind + payload) & 0xffffffff:
        raise ValueError("icon PNG IHDR checksum mismatch: " + str(path))
    width, height = struct.unpack(">II", payload[:8])
    if width == 0 or height == 0:
        raise ValueError("icon PNG has zero dimensions: " + str(path))


def check_svg(path):
    if not path.is_file():
        raise ValueError("icon is missing: " + str(path))
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise ValueError("icon SVG is not well-formed XML: " + str(path) + ": " + str(error))
    if root.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
        raise ValueError("icon SVG root element is not <svg>: " + str(path))


def check(root):
    root = Path(root)
    config = json.loads((root / "template.json").read_text())
    directory = root / "component" / config["repository"] / "Resources"
    check_png(directory / "logo.png")
    check_svg(directory / "logo.svg")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        check(args.root)
    except (ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
