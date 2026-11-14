"""Passive DHCP logger for the hotspot interface (Darwin BPF, needs root).

Shows what the iPad's own DHCP server (bootpd) answers a client: offered IP,
router, DNS, lease, NAK. Reads copies of packets only; sends nothing.
"""
import fcntl
import os
import socket
import struct
import threading

BIOCGBLEN = 0x40044266
BIOCSBLEN = 0xc0044266
BIOCSETIF = 0x8020426c
BIOCIMMEDIATE = 0x80044270
BIOCSSEESENT = 0x80044277
TYPES = {1: 'DISCOVER', 2: 'OFFER', 3: 'REQUEST', 4: 'DECLINE', 5: 'ACK', 6: 'NAK', 7: 'RELEASE', 8: 'INFORM'}


def _opts(data):
    opts, i = {}, 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        n = data[i + 1]
        opts[code] = data[i + 2:i + 2 + n]
        i += 2 + n
    return opts


def _ips(b):
    return ','.join(socket.inet_ntoa(b[i:i + 4]) for i in range(0, len(b) - len(b) % 4, 4)) or '–'


def describe_dhcp(payload):
    """One readable line for a DHCP payload, or None."""
    if len(payload) < 240 or payload[236:240] != b'\x63\x82\x53\x63':
        return None
    mac = ':'.join(f'{b:02x}' for b in payload[28:34])
    yiaddr = socket.inet_ntoa(payload[16:20])
    o = _opts(payload[240:])
    t = TYPES.get(o.get(53, b'\0')[0], '?')
    parts = [f'DHCP {t}', mac]
    if 12 in o:
        parts.append(o[12].decode(errors='ignore'))
    if t in ('OFFER', 'ACK'):
        parts.append(f'IP={yiaddr}')
        parts.append(f'Router={_ips(o[3]) if 3 in o else "FEHLT"}')
        parts.append(f'DNS={_ips(o[6]) if 6 in o else "FEHLT"}')
        if 1 in o:
            parts.append(f'Maske={_ips(o[1])}')
        if 51 in o and len(o[51]) == 4:
            parts.append(f'Lease={struct.unpack("!I", o[51])[0]}s')
    elif t == 'REQUEST' and 50 in o:
        parts.append(f'möchte {_ips(o[50])}')
    if 55 in o and t in ('DISCOVER', 'REQUEST'):
        parts.append('fragt nach ' + ','.join(str(x) for x in o[55]))
    return '  '.join(parts)


def parse_frame(frame):
    """Ethernet frame -> DHCP payload (UDP 67/68) or None."""
    if len(frame) < 14 + 20 + 8:
        return None
    off = 12
    ethertype = struct.unpack('!H', frame[off:off + 2])[0]
    off += 2
    if ethertype == 0x8100:                       # VLAN tag
        ethertype = struct.unpack('!H', frame[off + 2:off + 4])[0]
        off += 4
    if ethertype != 0x0800:
        return None
    ihl = (frame[off] & 0x0f) * 4
    if frame[off + 9] != 17:
        return None
    udp = off + ihl
    sport, dport = struct.unpack('!HH', frame[udp:udp + 4])
    if {sport, dport} - {67, 68}:
        return None
    return frame[udp + 8:]


def split_bpf(buf):
    """Darwin bpf_hdr: timeval32 (8) + caplen (4) + datalen (4) + hdrlen (2), word aligned."""
    i = 0
    while i + 18 <= len(buf):
        caplen, datalen, hdrlen = struct.unpack('=IIH', buf[i + 8:i + 18])
        if hdrlen == 0:
            break
        yield buf[i + hdrlen:i + hdrlen + caplen]
        i += (hdrlen + caplen + 3) & ~3


class Sniffer:
    def __init__(self, iface, log):
        self.iface, self.log, self.fd, self.running = iface, log, None, False

    def start(self):
        for n in range(0, 64):
            try:
                self.fd = os.open(f'/dev/bpf{n}', os.O_RDONLY)
                break
            except OSError as e:
                if e.errno == 16:          # EBUSY
                    continue
                raise
        if self.fd is None:
            raise OSError('kein freies /dev/bpf')
        fcntl.ioctl(self.fd, BIOCSBLEN, struct.pack('I', 65536))
        fcntl.ioctl(self.fd, BIOCSETIF, struct.pack('16s16x', self.iface.encode()))
        fcntl.ioctl(self.fd, BIOCIMMEDIATE, struct.pack('I', 1))
        try:
            fcntl.ioctl(self.fd, BIOCSSEESENT, struct.pack('I', 1))
        except OSError:
            pass
        self.blen = struct.unpack('I', fcntl.ioctl(self.fd, BIOCGBLEN, struct.pack('I', 0)))[0]
        self.running = True
        threading.Thread(target=self.loop, daemon=True).start()
        self.log(f'DHCP-Mitschnitt aktiv auf {self.iface}')

    def stop(self):
        self.running = False
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def loop(self):
        while self.running:
            try:
                buf = os.read(self.fd, self.blen)
            except OSError:
                break
            for frame in split_bpf(buf):
                payload = parse_frame(frame)
                if payload:
                    line = describe_dhcp(payload)
                    if line:
                        self.log(line)
