"""Patch malformed ALAC packets in an .m4a/.mp4 file in place.

Port of utils/alacfix/alacfix.go. Some ALAC encoders emit packets missing the
TYPE_END terminator; this walks the ISO BMFF container, locates ALAC tracks,
parses each packet far enough to find where the element body ends and, if the
tail does not already start with TYPE_END, overwrites those 3 bits with 111
and zero-pads the rest of the packet.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


class _Eof(Exception):
    pass


# --- bit reader ---------------------------------------------------------------


class BitReader:
    def __init__(self, buf: bytes | bytearray) -> None:
        self.buf = buf
        self.pos = 0  # bit position from MSB of buf[0]
        self.nbits = len(buf) * 8

    def left(self) -> int:
        return self.nbits - self.pos

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        if self.pos + n > self.nbits:
            raise _Eof()
        v = 0
        p = self.pos
        for _ in range(n):
            v = (v << 1) | ((self.buf[p >> 3] >> (7 - (p & 7))) & 1)
            p += 1
        self.pos = p
        return v

    def show(self, n: int) -> int:
        save = self.pos
        try:
            return self.read(n)
        finally:
            self.pos = save

    def skip(self, n: int) -> None:
        if self.pos + n > self.nbits:
            raise _Eof()
        self.pos += n

    def read_signed(self, n: int) -> int:
        v = self.read(n)
        if v & (1 << (n - 1)):
            return v - (1 << n)
        return v

    def unary09(self) -> int:
        cnt = 0
        while cnt < 9:
            if self.read(1) == 0:
                return cnt
            cnt += 1
        return 9


def av_log2(x: int) -> int:
    if x == 0:
        return 0
    r = 0
    while x > 1:
        x >>= 1
        r += 1
    return r


# --- ALAC element body scanner --------------------------------------------------


@dataclass
class AlacParams:
    max_samples_per_frame: int = 0
    sample_size: int = 0
    rice_history_mult: int = 0
    rice_initial_history: int = 0
    rice_limit: int = 0
    channels: int = 0


def decode_scalar(br: BitReader, k: int, bps: int) -> int:
    x = br.unary09()
    if x > 8:
        return br.read(bps)
    if k != 1:
        extrabits = br.show(k)
        x = (x << k) - x
        if extrabits > 1:
            x += extrabits - 1
            br.skip(k)
        else:
            br.skip(k - 1)
    return x


def rice_decompress(br: BitReader, nb_samples: int, bps: int, rhm_eff: int, p: AlacParams) -> None:
    history = p.rice_initial_history
    sign_mod = 0
    limit = p.rice_limit
    cap = nb_samples * 4 + 100
    iters = 0
    i = 0
    while i < nb_samples:
        iters += 1
        if iters > cap:
            raise ValueError("rice runaway")
        if br.left() <= 0:
            raise _Eof()
        k = av_log2((history >> 9) + 3)
        if k > limit:
            k = limit
        x = decode_scalar(br, k, bps)
        x += sign_mod
        sign_mod = 0
        if x > 0xFFFF:
            history = 0xFFFF
        else:
            history = history + x * rhm_eff - ((history * rhm_eff) >> 9)
        if history < 128 and (i + 1) < nb_samples:
            k2 = 7 - av_log2(history) + ((history + 16) >> 6)
            if k2 > limit:
                k2 = limit
            block_size = decode_scalar(br, k2, 16)
            if block_size > 0:
                if block_size >= nb_samples - i:
                    block_size = nb_samples - i - 1
                i += block_size
            if block_size <= 0xFFFF:
                sign_mod = 1
            history = 0
        i += 1


def scan_one_element(br: BitReader, p: AlacParams) -> tuple[int, bool]:
    """Returns (channels_used, is_end_tag)."""
    elem = br.read(3)
    if elem == 7:
        return 0, True
    if elem > 1 and elem != 3:
        raise ValueError(f"unsupported element tag {elem}")
    channels = 2 if elem == 1 else 1
    br.skip(4)
    br.skip(12)
    has_size = br.read(1)
    extra_bits_raw = br.read(2)
    extra_bits = extra_bits_raw << 3
    bps = p.sample_size - extra_bits + channels - 1
    if bps > 32 or bps < 1:
        raise ValueError(f"bad bps {bps}")
    not_compressed = br.read(1)
    is_compressed = not_compressed == 0
    if has_size != 0:
        output_samples = br.read(32)
    else:
        output_samples = p.max_samples_per_frame
    if output_samples == 0 or output_samples > p.max_samples_per_frame:
        raise ValueError(f"bad output_samples {output_samples}")

    if is_compressed:
        br.read(8)  # decorr_shift
        br.read(8)  # decorr_left_weight
        rhms = []
        for _ in range(channels):
            br.read(4)  # pred_type
            lpc_quant = br.read(4)
            rhm = br.read(3)
            lpc_order = br.read(5)
            if lpc_order >= p.max_samples_per_frame or lpc_quant == 0:
                raise ValueError("bad lpc")
            for _ in range(lpc_order):
                br.read_signed(16)
            rhms.append(rhm)
        if extra_bits != 0:
            need = output_samples * channels * extra_bits
            if br.left() < need:
                raise _Eof()
            br.skip(need)
        for c in range(channels):
            rhm_eff = (rhms[c] * p.rice_history_mult) // 4
            rice_decompress(br, output_samples, bps, rhm_eff, p)
    else:
        need = output_samples * channels * p.sample_size
        if br.left() < need:
            raise _Eof()
        br.skip(need)
    return channels, False


def find_body_end_bit(packet: bytes | bytearray, p: AlacParams) -> int:
    """Bit position right after the last non-END element body; -1 on failure."""
    br = BitReader(packet)
    ch_used = 0
    last_end = -1
    while br.left() >= 3:
        try:
            n_ch, is_end = scan_one_element(br, p)
        except (_Eof, ValueError):
            return -1
        if is_end:
            return br.pos
        last_end = br.pos
        ch_used += n_ch
        if ch_used >= p.channels:
            return last_end
    return last_end


# --- ISO BMFF walker -------------------------------------------------------------


@dataclass
class Atom:
    typ: str
    body_off: int
    end_off: int


def find_child(data: bytes | bytearray, start: int, end: int, typ: str) -> Atom | None:
    p = start
    while p < end - 8:
        size, atom_type = struct.unpack(">I4s", data[p : p + 8])
        atom_type = atom_type.decode("latin-1")
        hdr = 8
        if size == 1:
            if p + 16 > end:
                return None
            size = struct.unpack(">Q", data[p + 8 : p + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - p
        if size < hdr or p + size > end:
            return None
        if atom_type == typ:
            return Atom(typ=atom_type, body_off=p + hdr, end_off=p + size)
        p += size
    return None


def find_all_children(
    data: bytes | bytearray, start: int, end: int, typ: str
) -> list[Atom]:
    out: list[Atom] = []
    p = start
    while p < end - 8:
        size, atom_type = struct.unpack(">I4s", data[p : p + 8])
        atom_type = atom_type.decode("latin-1")
        hdr = 8
        if size == 1:
            if p + 16 > end:
                break
            size = struct.unpack(">Q", data[p + 8 : p + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - p
        if size < hdr or p + size > end:
            break
        if atom_type == typ:
            out.append(Atom(typ=atom_type, body_off=p + hdr, end_off=p + size))
        p += size
    return out


# --- track metadata extraction ----------------------------------------------------


@dataclass
class PacketLoc:
    offset: int
    size: int


@dataclass
class TrackData:
    track_id: int
    params: AlacParams
    locs: list[PacketLoc]


def parse_alac_magic_cookie(c: bytes | bytearray) -> AlacParams:
    """ALAC specific box payload: verflags|maxFrames|compat|sampleSize|..."""
    if len(c) < 24:
        raise ValueError("ALAC config too short")
    p = AlacParams()
    p.max_samples_per_frame = struct.unpack(">I", c[4:8])[0]
    p.sample_size = c[9]
    p.rice_history_mult = c[10]
    p.rice_initial_history = c[11]
    p.rice_limit = c[12]
    p.channels = c[13]
    return p


def extract_alac_config(data: bytes | bytearray, sample_entry: Atom) -> AlacParams:
    child_start = sample_entry.body_off + 28
    cfg = find_child(data, child_start, sample_entry.end_off, "alac")
    if cfg is not None:
        if cfg.end_off - cfg.body_off < 28:
            raise ValueError("alac config atom too small")
        return parse_alac_magic_cookie(data[cfg.body_off : cfg.body_off + 28])
    wave = find_child(data, child_start, sample_entry.end_off, "wave")
    if wave is not None:
        cfg = find_child(data, wave.body_off, wave.end_off, "alac")
        if cfg is not None:
            if cfg.end_off - cfg.body_off < 28:
                raise ValueError("alac config atom too small")
            return parse_alac_magic_cookie(data[cfg.body_off : cfg.body_off + 28])
    raise ValueError("no ALAC config inside sample entry")


def read_packet_locations(data: bytes | bytearray, stbl: Atom) -> list[PacketLoc]:
    stsz = find_child(data, stbl.body_off, stbl.end_off, "stsz")
    if stsz is None:
        raise ValueError("stsz missing")
    stsc = find_child(data, stbl.body_off, stbl.end_off, "stsc")
    if stsc is None:
        raise ValueError("stsc missing")
    stco = find_child(data, stbl.body_off, stbl.end_off, "stco")
    is64 = False
    if stco is None:
        stco = find_child(data, stbl.body_off, stbl.end_off, "co64")
        if stco is None:
            raise ValueError("stco/co64 missing")
        is64 = True

    b = stsz.body_off
    default_size = struct.unpack(">I", data[b + 4 : b + 8])[0]
    count = struct.unpack(">I", data[b + 8 : b + 12])[0]
    sizes = [default_size] * count
    if default_size == 0:
        for i in range(count):
            sizes[i] = struct.unpack(">I", data[b + 12 + 4 * i : b + 16 + 4 * i])[0]

    b = stco.body_off
    ent = struct.unpack(">I", data[b + 4 : b + 8])[0]
    chunk_off: list[int] = []
    p = b + 8
    for _ in range(ent):
        if is64:
            chunk_off.append(struct.unpack(">Q", data[p : p + 8])[0])
            p += 8
        else:
            chunk_off.append(struct.unpack(">I", data[p : p + 4])[0])
            p += 4

    b = stsc.body_off
    ent = struct.unpack(">I", data[b + 4 : b + 8])[0]
    runs: list[tuple[int, int]] = []  # (first_chunk, samples_per_chunk)
    p = b + 8
    for _ in range(ent):
        first_chunk = struct.unpack(">I", data[p : p + 4])[0]
        samples_per_chunk = struct.unpack(">I", data[p + 4 : p + 8])[0]
        runs.append((first_chunk, samples_per_chunk))
        p += 12

    samples_per_chunk = [0] * len(chunk_off)
    for i, (first_chunk, spc) in enumerate(runs):
        next_fc = runs[i + 1][0] if i + 1 < len(runs) else len(chunk_off) + 1
        c = first_chunk
        while c < next_fc and c - 1 < len(chunk_off):
            samples_per_chunk[c - 1] = spc
            c += 1
    if runs:
        for i in range(len(samples_per_chunk)):
            if samples_per_chunk[i] == 0:
                samples_per_chunk[i] = runs[-1][1]

    locs: list[PacketLoc] = []
    sample_idx = 0
    for c, chunk_start in enumerate(chunk_off):
        cur = chunk_start
        spc = samples_per_chunk[c]
        for _ in range(spc):
            if sample_idx >= len(sizes):
                break
            sz = sizes[sample_idx]
            locs.append(PacketLoc(offset=cur, size=sz))
            cur += sz
            sample_idx += 1
        if sample_idx >= len(sizes):
            break
    return locs


def find_alac_tracks(data: bytes | bytearray) -> list[TrackData]:
    if len(data) < 8:
        raise ValueError("file too small")
    moov = find_child(data, 0, len(data), "moov")
    if moov is None:
        raise ValueError("no moov atom (not an MP4/M4A?)")

    tracks: list[TrackData] = []
    for trak in find_all_children(data, moov.body_off, moov.end_off, "trak"):
        track_id = 0
        tkhd = find_child(data, trak.body_off, trak.end_off, "tkhd")
        if tkhd is not None:
            b = tkhd.body_off
            version = data[b]
            if version == 0 and tkhd.end_off - b >= 20:
                track_id = struct.unpack(">I", data[b + 12 : b + 16])[0]
            elif version == 1 and tkhd.end_off - b >= 32:
                track_id = struct.unpack(">I", data[b + 20 : b + 24])[0]

        mdia = find_child(data, trak.body_off, trak.end_off, "mdia")
        if mdia is None:
            continue
        hdlr = find_child(data, mdia.body_off, mdia.end_off, "hdlr")
        if hdlr is None:
            continue
        hb = hdlr.body_off
        if hdlr.end_off - hb < 12 or data[hb + 8 : hb + 12] != b"soun":
            continue
        minf = find_child(data, mdia.body_off, mdia.end_off, "minf")
        if minf is None:
            continue
        stbl = find_child(data, minf.body_off, minf.end_off, "stbl")
        if stbl is None:
            continue
        stsd = find_child(data, stbl.body_off, stbl.end_off, "stsd")
        if stsd is None:
            continue
        b = stsd.body_off
        if stsd.end_off - b < 8:
            continue
        entry_count = struct.unpack(">I", data[b + 4 : b + 8])[0]
        if entry_count == 0:
            continue
        entry_start = b + 8
        if entry_start + 8 > stsd.end_off:
            continue
        entry_size = struct.unpack(">I", data[entry_start : entry_start + 4])[0]
        entry_type = data[entry_start + 4 : entry_start + 8].decode("latin-1")
        if entry_size < 8 or entry_start + entry_size > stsd.end_off:
            continue
        if entry_type != "alac":
            continue
        sample_entry = Atom(typ=entry_type, body_off=entry_start + 8, end_off=entry_start + entry_size)
        params = extract_alac_config(data, sample_entry)
        locs = read_packet_locations(data, stbl)
        tracks.append(TrackData(track_id=track_id, params=params, locs=locs))
    return tracks


# --- patcher -----------------------------------------------------------------------


def patch_in_place(data: bytearray, offset: int, size: int, body_end_bit: int) -> bool:
    total_bits = size * 8
    if body_end_bit < 0 or body_end_bit + 3 > total_bits:
        return False
    for i in range(3):
        bp = body_end_bit + i
        bi = offset + (bp >> 3)
        mask = 1 << (7 - (bp & 7))
        data[bi] |= mask
    pad_start = body_end_bit + 3
    bi = offset + (pad_start >> 3)
    bit_in_byte = pad_start & 7
    if bit_in_byte != 0:
        keep = (0xFF << (8 - bit_in_byte)) & 0xFF
        data[bi] &= keep
        bi += 1
    end_byte = offset + size
    for j in range(bi, end_byte):
        data[j] = 0
    return True


def run(path: str, force: bool = False, out_path: str = "") -> None:
    data = bytearray(Path(path).read_bytes())
    dst = out_path or path
    try:
        tracks = find_alac_tracks(data)
    except (ValueError, struct.error) as exc:
        raise RuntimeError(str(exc)) from exc
    if not tracks:
        return

    patched = 0
    report: list[tuple[int, int, int, int, int]] = []

    for td in tracks:
        params = td.params
        print(
            f"Track #{td.track_id}: {len(td.locs)} packets, "
            f"max_samples_per_frame={params.max_samples_per_frame} "
            f"sample_size={params.sample_size} channels={params.channels}"
        )
        for idx, loc in enumerate(td.locs):
            pkt = bytes(data[loc.offset : loc.offset + loc.size])
            body_end = find_body_end_bit(pkt, params)
            if body_end < 0:
                continue
            if body_end == loc.size * 8:
                continue
            br = BitReader(pkt)
            try:
                br.skip(body_end)
                if br.left() >= 3 and br.show(3) == 7:
                    continue
            except _Eof:
                continue
            if patch_in_place(data, loc.offset, loc.size, body_end):
                patched += 1
                report.append((td.track_id, idx, loc.offset, loc.size, body_end))

    if patched > 0 or force:
        Path(dst).write_bytes(bytes(data))
        print(f"Patched {patched} packet(s).")
        for track_id, idx, off, size, body_end in report:
            print(
                f"  track #{track_id} packet #{idx}  "
                f"file_offset={hex(off)}  size={size}  "
                f"body_ends_at_bit={body_end}  tail_overwritten=[{body_end}..{size * 8})"
            )
