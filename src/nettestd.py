"""Showcase for Tesla – network test daemon.

While the iPad's hotspot is up (172.20.10.1), adds 100.96.0.1 to the hotspot
interface and serves a test page on both addresses, over HTTP (80) and HTTPS
(443, self-signed). Visits are listed on /log. When the hotspot goes away the
servers stop and the extra address is removed, so nothing listens at home.
"""
import collections
import html
import http.server
import json
import os
import re
import socketserver
import ssl
import subprocess
import sys
import threading
import time

ALIAS_IP = os.environ.get('ALIAS_IP', '100.96.0.1')
HOTSPOT_IP = os.environ.get('HOTSPOT_IP', '172.20.10.1')
HTTP_PORT = int(os.environ.get('HTTP_PORT', '80'))
HTTPS_PORT = int(os.environ.get('HTTPS_PORT', '443'))
CERT_DIR = os.environ.get('CERT_DIR', sys.argv[1] if len(sys.argv) > 1 else '.')
NO_ALIAS = bool(os.environ.get('NO_ALIAS'))
VERSION = '0.2'
STATUS_PORT = int(os.environ.get('STATUS_PORT', '8080'))

LOG = collections.deque(maxlen=200)


def log(line):
    entry = f"{time.strftime('%H:%M:%S')}  {line}"
    LOG.appendleft(entry)
    print(entry, flush=True)


def tool(*names):
    for n in names:
        if os.path.exists(n):
            return n
    return None


IFCONFIG = tool('/var/jb/usr/sbin/ifconfig', '/var/jb/sbin/ifconfig', '/var/jb/usr/bin/ifconfig',
                '/usr/sbin/ifconfig', '/sbin/ifconfig')
STATE = {'hotspot': None, 'alias': False, 'listening': [], 'last_error': ''}


def ipv4_addresses():
    """{interface: [IPv4, ...]} via getifaddrs, so no extra tool is needed to see the hotspot."""
    import ctypes
    import ctypes.util
    import socket

    class sockaddr(ctypes.Structure):
        _fields_ = [('sa_len', ctypes.c_uint8), ('sa_family', ctypes.c_uint8), ('sa_data', ctypes.c_uint8 * 14)]

    class ifaddrs(ctypes.Structure):
        pass
    ifaddrs._fields_ = [('ifa_next', ctypes.POINTER(ifaddrs)), ('ifa_name', ctypes.c_char_p),
                        ('ifa_flags', ctypes.c_uint), ('ifa_addr', ctypes.POINTER(sockaddr)),
                        ('ifa_netmask', ctypes.c_void_p), ('ifa_dstaddr', ctypes.c_void_p),
                        ('ifa_data', ctypes.c_void_p)]
    libc = ctypes.CDLL(ctypes.util.find_library('c') or '/usr/lib/libSystem.B.dylib', use_errno=True)
    head = ctypes.POINTER(ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), 'getifaddrs')
    out = {}
    try:
        node = head
        while node:
            ifa = node.contents
            if ifa.ifa_addr and ifa.ifa_addr.contents.sa_family == socket.AF_INET:
                raw = bytes(bytearray(ifa.ifa_addr.contents.sa_data))[2:6]   # skip port
                out.setdefault(ifa.ifa_name.decode(), []).append(socket.inet_ntoa(raw))
            node = ifa.ifa_next
    finally:
        libc.freeifaddrs(head)
    return out


def hotspot_interface():
    try:
        addrs = ipv4_addresses()
    except Exception as e:
        STATE['last_error'] = f'getifaddrs: {e}'
        return None
    for iface, ips in addrs.items():
        if HOTSPOT_IP in ips:
            return iface, ALIAS_IP in ips
    return None


def set_alias(iface, add):
    if not IFCONFIG:
        STATE['last_error'] = 'ifconfig fehlt (Paket network-cmds), 100.96.0.1 nicht möglich'
        log(STATE['last_error'])
        return False
    args = [IFCONFIG, iface, 'inet', ALIAS_IP]
    args += ['netmask', '255.255.255.0', 'alias'] if add else ['-alias']
    r = subprocess.run(args, capture_output=True, text=True)
    log(f"{'+' if add else '-'} {ALIAS_IP} auf {iface}" + (f" ({r.stderr.strip()})" if r.returncode else ''))
    if r.returncode:
        STATE['last_error'] = f'ifconfig: {r.stderr.strip()}'
    return r.returncode == 0


PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Showcase for Tesla – Test</title>
<style>body{background:#0d0f12;color:#e8eaed;font:20px/1.5 -apple-system,sans-serif;padding:24px;margin:0}
.ok{color:#3ccf7a}.bad{color:#f05252}td{padding:6px 18px 6px 0;vertical-align:top}a{color:#4c8dff}
pre{background:#000;padding:12px;border-radius:8px;font-size:14px;white-space:pre-wrap}</style></head><body>
<h1 class="ok">&#10003; Der Tesla erreicht das iPad</h1>
<table id="t"></table>
<p>Alle Adressen testen:</p><ul id="l"></ul>
<p><a href="/log">Protokoll aller Aufrufe</a> · Showcase for Tesla Netzwerktest __VERSION__</p>
<script>
const r = {url: location.href, secureContext: isSecureContext, webCodecs: 'VideoDecoder' in window,
  webSocket: 'WebSocket' in window, ua: navigator.userAgent, time: new Date().toLocaleTimeString('de-DE')};
const fmt = v => v === true ? '<span class="ok">ja</span>' : v === false ? '<span class="bad">nein</span>' : v;
const rows = [['Adresse', r.url], ['Sichere Seite', r.secureContext], ['WebCodecs', r.webCodecs], ['Uhrzeit', r.time]];
const t = document.getElementById('t');
const render = () => t.innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${fmt(v)}</td></tr>`).join('');
render();
document.getElementById('l').innerHTML = __URLS__.map(u => `<li><a href="${u}">${u}</a></li>`).join('');
fetch('/report', {method: 'POST', body: JSON.stringify(r)}).catch(() => {});
try {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.onopen = () => ws.send('ping');
  ws.onmessage = e => { rows.push(['WebSocket', e.data === 'pong']); render(); ws.close(); };
  ws.onerror = () => { rows.push(['WebSocket', false]); render(); };
} catch (e) { rows.push(['WebSocket', false]); render(); }
</script></body></html>"""


def urls():
    u = [f'http://{ALIAS_IP}/', f'http://{HOTSPOT_IP}/']
    if HAVE_TLS:
        u += [f'https://{ALIAS_IP}/', f'https://{HOTSPOT_IP}/']
    return u


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def send(self, code, body, ctype='text/html; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.server.server_address[0] == '127.0.0.1' and self.server.server_address[1] == STATUS_PORT \
                and not self.path.startswith(('/log', '/ws', '/report')):
            return self.send(200, status_page().encode())
        if self.path == '/ws' and self.headers.get('Upgrade', '').lower() == 'websocket':
            return self.websocket()
        if self.path.startswith('/log'):
            body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                    '<meta http-equiv="refresh" content="3"><title>Protokoll</title>'
                    '<body style="background:#0d0f12;color:#e8eaed;font:16px monospace;padding:16px">'
                    '<p><a style="color:#4c8dff" href="/">Zurück</a> · aktualisiert sich alle 3 s</p><pre>'
                    + html.escape('\n'.join(LOG)) + '</pre>').encode()
            return self.send(200, body)
        page = PAGE.replace('__URLS__', json.dumps(urls())).replace('__VERSION__', VERSION)
        self.send(200, page.encode())

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
        try:
            r = json.loads(data)
            log(f"{self.client_address[0]}  {r.get('url')}  sicher={r.get('secureContext')}  "
                f"WebCodecs={r.get('webCodecs')}")
        except ValueError:
            pass
        self.send(204, b'')

    def websocket(self):
        import base64
        import hashlib
        key = self.headers.get('Sec-WebSocket-Key', '')
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        self.send_response(101)
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', accept.decode())
        self.end_headers()
        self.wfile.flush()
        try:
            hdr = self.rfile.read(2)
            n = hdr[1] & 0x7f
            if n == 126:
                n = int.from_bytes(self.rfile.read(2), 'big')
            mask = self.rfile.read(4) if hdr[1] & 0x80 else b'\0\0\0\0'
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(n)))
            reply = b'pong' if payload == b'ping' else payload
            self.wfile.write(bytes([0x81, len(reply)]) + reply)
            self.wfile.flush()
            log(f"{self.client_address[0]}  WebSocket ok")
        except Exception as e:
            log(f"{self.client_address[0]}  WebSocket Fehler: {e}")
        self.close_connection = True

    def log_message(self, fmt, *args):
        if self.path not in ('/report',) and not self.path.startswith('/log'):
            log(f"{self.client_address[0]}  {fmt % args}")


def status_page():
    hs = STATE['hotspot']
    rows = [
        ('Dienst', f'läuft, Version {VERSION}, Python {sys.version.split()[0]}'),
        ('Hotspot 172.20.10.1', f'erkannt auf {hs}' if hs else 'nicht gefunden – CarPlay in Showcase starten'),
        ('Adresse 100.96.0.1', 'gesetzt' if STATE['alias'] else 'nicht gesetzt'),
        ('ifconfig', IFCONFIG or 'fehlt – in Sileo „network-cmds“ installieren'),
        ('HTTPS-Zertifikat', 'vorhanden' if HAVE_TLS else 'fehlt'),
        ('Lauscht auf', ', '.join(STATE['listening']) or '–'),
        ('Letzter Fehler', STATE['last_error'] or '–'),
    ]
    try:
        addrs = ipv4_addresses()
        rows.append(('Netzwerk', ' · '.join(f'{k}: {", ".join(v)}' for k, v in sorted(addrs.items()))))
    except Exception as e:
        rows.append(('Netzwerk', f'Fehler: {e}'))
    body = ''.join(f'<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>' for k, v in rows)
    return ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<meta http-equiv="refresh" content="3"><title>Showcase for Tesla – Status</title>'
            '<body style="background:#0d0f12;color:#e8eaed;font:18px/1.5 -apple-system,sans-serif;padding:20px">'
            '<h2>Showcase for Tesla – Netzwerktest</h2><table cellpadding="6">' + body + '</table>'
            '<p><a style="color:#4c8dff" href="/log">Protokoll</a> · aktualisiert sich alle 3 s</p>')


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


CERT = os.path.join(CERT_DIR, 'cert.pem')
KEY = os.path.join(CERT_DIR, 'key.pem')
HAVE_TLS = os.path.exists(CERT) and os.path.exists(KEY)


def start_servers(addresses, ports=None):
    servers = []
    for addr in addresses:
        for port, tls in ports or ((HTTP_PORT, False), (HTTPS_PORT, True)):
            if tls and not HAVE_TLS:
                continue
            try:
                s = Server((addr, port), Handler)
                if tls:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(CERT, KEY)
                    s.socket = ctx.wrap_socket(s.socket, server_side=True)
                threading.Thread(target=s.serve_forever, daemon=True).start()
                servers.append(s)
                STATE['listening'].append(f"{'https' if tls else 'http'}://{addr}:{port}")
                log(f"lauscht auf {'https' if tls else 'http'}://{addr}:{port}/")
            except OSError as e:
                log(f"kann {addr}:{port} nicht öffnen: {e}")
                STATE['last_error'] = f'{addr}:{port}: {e}'
    return servers


def main():
    log(f'Netzwerktest {VERSION} gestartet, HTTPS {"an" if HAVE_TLS else "aus (kein Zertifikat)"}, '
        f'ifconfig {IFCONFIG or "fehlt"}')
    status = start_servers(['127.0.0.1'], [(STATUS_PORT, False)])
    STATE['listening'] = []
    if NO_ALIAS:
        start_servers(['127.0.0.1'])
        while True:
            time.sleep(3600)
    servers, iface = [], None
    while True:
        found = hotspot_interface()
        if found and not servers:
            iface, has_alias = found
            STATE['hotspot'] = iface
            log(f'Hotspot erkannt auf {iface}')
            if not has_alias:
                has_alias = set_alias(iface, True)
                time.sleep(1)
            STATE['alias'] = has_alias
            servers = start_servers(([ALIAS_IP] if has_alias else []) + [HOTSPOT_IP])
        elif found and servers and not found[1] and STATE['alias']:
            log('100.96.0.1 ging verloren, setze neu')
            STATE['alias'] = set_alias(iface, True)
        elif not found and servers:
            log('Hotspot beendet, Server gestoppt')
            for s in servers:
                s.shutdown()
                s.server_close()
            servers = []
            STATE['listening'] = []
            if iface and STATE['alias']:
                set_alias(iface, False)
            iface, STATE['hotspot'], STATE['alias'] = None, None, False
        time.sleep(3)


if __name__ == '__main__':
    main()
