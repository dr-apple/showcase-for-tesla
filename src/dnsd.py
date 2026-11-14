"""Tiny DNS server for the hotspot: every A query is answered with the iPad.

The Tesla only joins a Wi-Fi network that looks online. With no uplink, its
connectivity check would fail at DNS already; answering every name with
172.20.10.1 sends the check to this service, which replies like an online
network. Every query is logged, so we can see what the car looks up.
Only the Tesla gets this DNS server via DHCP; the iPhone keeps none.
"""
import socket
import struct
import threading

QTYPES = {1: 'A', 28: 'AAAA', 5: 'CNAME', 12: 'PTR', 15: 'MX', 16: 'TXT', 33: 'SRV', 65: 'HTTPS', 64: 'SVCB'}


def parse_query(data):
    """-> (id, flags, qname, qtype, end_of_question) or None."""
    if len(data) < 12:
        return None
    qid, flags, qd = struct.unpack('!HHH', data[:6])
    if qd < 1 or flags & 0x8000:
        return None
    i, labels = 12, []
    while i < len(data):
        n = data[i]
        if n == 0:
            i += 1
            break
        if n & 0xc0:
            return None
        labels.append(data[i + 1:i + 1 + n].decode(errors='ignore'))
        i += 1 + n
    if i + 4 > len(data):
        return None
    qtype, qclass = struct.unpack('!HH', data[i:i + 4])
    return qid, flags, '.'.join(labels), qtype, i + 4


def build_answer(data, answer_ip, ttl=30):
    q = parse_query(data)
    if not q:
        return None, None
    qid, flags, name, qtype, end = q
    question = data[12:end]
    rd = flags & 0x0100
    if qtype == 1:
        header = struct.pack('!HHHHHH', qid, 0x8080 | rd, 1, 1, 0, 0)
        answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, ttl, 4) + socket.inet_aton(answer_ip)
        return header + question + answer, (name, 'A')
    # anything else: NOERROR without records, so clients fall back to IPv4
    header = struct.pack('!HHHHHH', qid, 0x8080 | rd, 1, 0, 0, 0)
    return header + question, (name, QTYPES.get(qtype, str(qtype)))


class DNSServer:
    def __init__(self, listen_ip, answer_ip, log=print, port=53):
        self.listen_ip, self.answer_ip, self.log, self.port = listen_ip, answer_ip, log, port
        self.sock, self.running, self.seen = None, False, {}

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((self.listen_ip, self.port))
        self.sock, self.running = s, True
        threading.Thread(target=self.loop, daemon=True).start()
        self.log(f'DNS: aktiv auf {self.listen_ip}:{self.port}, alle Namen → {self.answer_ip}')

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1500)
            except OSError:
                break
            try:
                reply, info = build_answer(data, self.answer_ip)
                if reply:
                    self.sock.sendto(reply, addr)
                    key = info
                    self.seen[key] = self.seen.get(key, 0) + 1
                    if self.seen[key] <= 2:            # log each name twice at most
                        self.log(f'DNS: {addr[0]} fragt {info[0]} ({info[1]})')
            except Exception as e:
                self.log(f'DNS: Fehler: {e}')
