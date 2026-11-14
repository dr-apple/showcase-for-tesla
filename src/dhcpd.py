"""Minimal DHCP server for the iPad hotspot (172.20.10.0/28).

On a Wi-Fi-only iPad the hotspot has no uplink, and iOS's own DHCP server
(bootpd) then answers without router and DNS. The iPhone is fine with that
(it keeps using mobile data), but a Tesla rejects such a lease ("IP address
could not be detected"). This server answers like bootpd (same mask, lease
and addresses) and adds router and DNS only for clients that need them.
"""
import socket
import struct
import threading
import time

MAGIC = b'\x63\x82\x53\x63'
IP_BOUND_IF = 25          # Darwin: bind a socket to an interface index
DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = 1, 2, 3, 4, 5, 6, 7, 8


def ip2b(ip):
    return socket.inet_aton(ip)


def b2ip(b):
    return socket.inet_ntoa(b)


def parse_options(data):
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


class DHCPServer:
    def __init__(self, iface, server_ip='172.20.10.1', first=2, last=14, netmask='255.255.255.240',
                 lease=86400, log=print, port=67, client_port=68, reply_addr='255.255.255.255',
                 taken=lambda: {}, gateway_for=lambda mac, host: True):
        self.iface, self.server_ip, self.netmask, self.lease = iface, server_ip, netmask, lease
        prefix = server_ip.rsplit('.', 1)[0]
        self.pool = [f'{prefix}.{n}' for n in range(first, last + 1)]
        self.log, self.port, self.client_port, self.reply_addr = log, port, client_port, reply_addr
        self.taken = taken            # {ip: mac} seen in the ARP table
        self.gateway_for = gateway_for  # (mac, hostname) -> include router and DNS?
        self.hosts = {}               # mac -> hostname
        self.leases = {}              # mac -> (ip, expires)
        self.sock = None
        self.running = False

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if self.iface:
            try:
                s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(self.iface))
            except OSError as e:
                self.log(f'DHCP: Schnittstelle {self.iface} nicht bindbar: {e}')
        s.bind(('0.0.0.0', self.port))      # raises EADDRINUSE if a DHCP server already runs
        self.sock = s
        self.running = True
        threading.Thread(target=self.loop, daemon=True).start()
        self.log(f'DHCP: eigener Server aktiv auf {self.iface}, vergibt {self.pool[0]}–{self.pool[-1]}')

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def summary(self):
        now = time.time()
        return ', '.join(f'{ip} ({mac})' for mac, (ip, exp) in self.leases.items() if exp > now) or 'noch keine'

    def pick(self, mac, wanted=None):
        now = time.time()
        if mac in self.leases and self.leases[mac][1] > now - 60:
            return self.leases[mac][0]
        used = {ip for m, (ip, exp) in self.leases.items() if exp > now and m != mac}
        used |= {ip for ip, m in self.taken().items() if m != mac}
        if wanted in self.pool and wanted not in used:
            return wanted
        for ip in self.pool:
            if ip not in used:
                return ip
        return None

    def loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except OSError:
                break
            try:
                self.handle(data)
            except Exception as e:      # never let one bad packet stop the server
                self.log(f'DHCP: Fehler bei Paket: {e}')

    def handle(self, data):
        if len(data) < 240 or data[0] != 1 or data[236:240] != MAGIC:
            return
        xid, flags = data[4:8], data[10:12]
        ciaddr, chaddr = data[12:16], data[28:44]
        mac = ':'.join(f'{b:02x}' for b in chaddr[:6])
        opts = parse_options(data[240:])
        mtype = opts.get(53, b'\0')[0]
        wanted = b2ip(opts[50]) if 50 in opts and len(opts[50]) == 4 else None
        host = opts.get(12, b'').decode(errors='ignore') or self.hosts.get(mac, '')
        if host:
            self.hosts[mac] = host
        gw = bool(self.gateway_for(mac, host))
        if mtype == DISCOVER:
            ip = self.pick(mac, wanted)
            if ip:
                self.leases[mac] = (ip, time.time() + 60)
                self.reply(OFFER, xid, flags, chaddr, ip, gw)
                self.log(f"DHCP: Angebot {ip} an {mac} {host}{' mit Router/DNS' if gw else ''}")
        elif mtype == REQUEST:
            ip = wanted or (b2ip(ciaddr) if ciaddr != b'\0\0\0\0' else None)
            server = b2ip(opts[54]) if 54 in opts and len(opts[54]) == 4 else None
            if server and server != self.server_ip:
                return                          # client chose another server
            if ip and ip in self.pool and self.pick(mac, ip) == ip:
                self.leases[mac] = (ip, time.time() + self.lease)
                self.reply(ACK, xid, flags, chaddr, ip, gw)
                self.log(f"DHCP: {ip} vergeben an {mac} {host}{' mit Router/DNS' if gw else ''}")
            else:
                self.reply(NAK, xid, flags, chaddr, '0.0.0.0', False)
                self.log(f'DHCP: Anfrage {ip} von {mac} abgelehnt')
        elif mtype in (RELEASE, DECLINE):
            self.leases.pop(mac, None)

    def reply(self, mtype, xid, flags, chaddr, ip, gateway):
        pkt = bytearray(240)
        pkt[0], pkt[1], pkt[2] = 2, 1, 6
        pkt[4:8], pkt[10:12] = xid, flags
        pkt[16:20] = ip2b(ip)
        pkt[20:24] = ip2b(self.server_ip)
        pkt[28:44] = chaddr
        pkt[236:240] = MAGIC
        o = bytearray([53, 1, mtype, 54, 4]) + ip2b(self.server_ip)
        if mtype != NAK:
            o += bytes([51, 4]) + struct.pack('!I', self.lease)
            o += bytes([1, 4]) + ip2b(self.netmask)
            if gateway:
                o += bytes([3, 4]) + ip2b(self.server_ip)
                o += bytes([6, 4]) + ip2b(self.server_ip)
        o += b'\xff'
        self.sock.sendto(bytes(pkt + o), (self.reply_addr, self.client_port))

    def broadcast(self):
        ip = struct.unpack('!I', ip2b(self.server_ip))[0]
        mask = struct.unpack('!I', ip2b(self.netmask))[0]
        return b2ip(struct.pack('!I', (ip & mask) | (~mask & 0xffffffff)))
