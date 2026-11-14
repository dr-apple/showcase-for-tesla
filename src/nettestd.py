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
VERSION = '0.7'
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # -I leaves the script dir out
import dhcpd  # noqa: E402
import dhcpsniff  # noqa: E402
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


MODE_CHANGED = threading.Event()
IFCONFIG = tool('/var/jb/usr/sbin/ifconfig', '/var/jb/sbin/ifconfig', '/var/jb/usr/bin/ifconfig',
                '/usr/sbin/ifconfig', '/sbin/ifconfig')
LAUNCHCTL_PATH = tool('/var/jb/usr/bin/launchctl', '/var/jb/bin/launchctl', '/usr/bin/launchctl', '/bin/launchctl')
ARP_PATH = tool('/var/jb/usr/sbin/arp', '/var/jb/usr/bin/arp', '/usr/sbin/arp')
STATE = {'hotspot': None, 'alias': None, 'listening': [], 'last_error': ''}
MODES = {'lo0': 'auf lo0 (Standard, lässt den Hotspot unangetastet)',
         'bridge': 'auf der Hotspot-Schnittstelle',
         'off': 'aus (nur 172.20.10.1)'}
MODE_FILE = os.path.join(CERT_DIR, 'mode')
ARP = ARP_PATH


def read_mode():
    try:
        m = open(MODE_FILE).read().strip()
        return m if m in MODES else 'lo0'
    except OSError:
        return 'lo0'


def write_mode(m):
    try:
        with open(MODE_FILE, 'w') as f:
            f.write(m)
    except OSError as e:
        STATE['last_error'] = f'Modus nicht gespeichert: {e}'



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
            return iface, any(ALIAS_IP in v for v in addrs.values())
    return None


def alias_holder():
    try:
        for iface, ips in ipv4_addresses().items():
            if ALIAS_IP in ips:
                return iface
    except Exception:
        pass
    return None


_DHCP_CACHE = [0.0, '']


def dhcp_server_state():
    """Only checked while the hotspot runs (its DHCP server is up by then) and at
    most every 30 s, so the probe never races the hotspot's own start."""
    if time.time() - _DHCP_CACHE[0] < 30:
        return _DHCP_CACHE[1]
    _DHCP_CACHE[0], _DHCP_CACHE[1] = time.time(), _dhcp_probe()
    return _DHCP_CACHE[1]


def _dhcp_probe():
    import socket
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.bind(('0.0.0.0', 67))
        return 'KEINER – Port 67 frei, Geräte bekommen keine IP'
    except PermissionError:
        return 'unbekannt (keine Rechte)'
    except OSError as e:
        return 'läuft (Port 67 belegt)' if e.errno in (48, 98) else f'unbekannt ({e})'
    finally:
        u.close()


def arp_table():
    """{ip: mac} for complete ARP entries on the hotspot subnet."""
    if not ARP:
        return {}
    try:
        out = subprocess.run([ARP, '-an'], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    table = {}
    for ip, mac in re.findall(r'\((172\.20\.10\.\d+)\) at ([0-9a-f:]+)', out):
        mac = ':'.join(x.zfill(2) for x in mac.split(':'))
        table[ip] = mac
    return table


DHCP_FILE = os.path.join(CERT_DIR, 'dhcp')
DHCP = {'server': None, 'state': '–', 'bootpd_paused': False}
DHCP_MODES = {'replace': 'übernehmen (Tesla bekommt Router/DNS, iPhone wie bisher)',
              'auto': 'nur wenn iOS keinen DHCP-Server startet',
              'off': 'aus (nur iOS)'}
LAUNCHCTL = LAUNCHCTL_PATH


def read_dhcp_mode():
    try:
        m = open(DHCP_FILE).read().strip()
        return m if m in DHCP_MODES else 'replace'
    except OSError:
        return 'replace'


def bootpd_plist():
    """iOS's DHCP server job (com.apple.bootpd, socket-activated on port 67)."""
    import glob
    import plistlib
    for path in sorted(glob.glob('/System/Library/LaunchDaemons/*.plist')):
        if 'boot' not in os.path.basename(path).lower():
            continue
        try:
            with open(path, 'rb') as f:
                pl = plistlib.load(f)
        except Exception:
            continue
        prog = ' '.join([pl.get('Program', '')] + list(pl.get('ProgramArguments', [])))
        if 'bootpd' in prog or pl.get('Label') == 'com.apple.bootpd':
            return path, pl.get('Label', 'com.apple.bootpd')
    return None, 'com.apple.bootpd'


def port67_free(iface=None):
    """Try the same bind the DHCP server does (interface-scoped), since iOS's
    bootpd binds that way too and a plain wildcard bind can still succeed."""
    import socket
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        iface = iface or STATE.get('hotspot')
        if iface:
            u.setsockopt(socket.IPPROTO_IP, dhcpd.IP_BOUND_IF, socket.if_nametoindex(iface))
        u.bind(('0.0.0.0', 67))
        return True
    except OSError:
        return False
    finally:
        u.close()


def launchctl(*args):
    if not LAUNCHCTL:
        return 'launchctl fehlt'
    r = subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=15)
    return (r.stdout + r.stderr).strip() or f'rc={r.returncode}'


def pause_bootpd():
    path, label = bootpd_plist()
    log(f'DHCP: pausiere iOS-DHCP ({label}, {path or "Plist nicht gefunden"})')
    attempts = ([('unload', path)] if path else []) + [('bootout', f'system/{label}')]
    for args in attempts:
        out = launchctl(*args)
        DHCP['bootpd_paused'] = True
        time.sleep(0.5)
        if port67_free():
            DHCP['bootpd_paused'] = True
            log(f'DHCP: iOS-DHCP pausiert ({args[0]})')
            return True
        log(f'DHCP: {args[0]} reichte nicht: {out}')
    subprocess.run(['killall', 'bootpd'], capture_output=True)
    time.sleep(0.5)
    DHCP['bootpd_paused'] = True          # something was unloaded; resume_bootpd() restores it
    if port67_free():
        log('DHCP: iOS-DHCP pausiert (killall)')
        return True
    log('DHCP: Port 67 bleibt belegt')
    return False


def resume_bootpd():
    """Give port 67 back to iOS. With socket activation, launchd holds the port
    again as soon as the job is loaded, so a busy port means it worked."""
    if not DHCP['bootpd_paused']:
        return
    path, label = bootpd_plist()
    outs = []
    if path:
        outs.append(launchctl('load', path))
        if port67_free():
            outs.append(launchctl('bootstrap', 'system', path))
    ok = not port67_free()
    DHCP['bootpd_paused'] = not ok
    log(f"DHCP: iOS-DHCP {'wieder an' if ok else 'NICHT wieder an – Neustart des iPads behebt das'} ({' | '.join(outs)})")


def needs_gateway(mac, host):
    return mac.startswith(TESLA_OUI) or host.lower().startswith('tesla')


def start_dhcp(iface):
    mode = read_dhcp_mode()
    if mode == 'off':
        DHCP['state'] = 'aus – iOS vergibt die Adressen'
        return

    def try_start():
        srv = dhcpd.DHCPServer(iface, server_ip=HOTSPOT_IP, log=log, taken=arp_table,
                               gateway_for=needs_gateway)
        srv.start()
        return srv

    srv, err = None, None
    try:
        srv = try_start()
    except OSError as e:
        err = e
        if mode == 'replace' and e.errno in (48, 98):
            log('DHCP: Port 67 von iOS belegt')
            if pause_bootpd():
                for _ in range(6):
                    try:
                        srv = try_start()
                        break
                    except OSError as e2:
                        err = e2
                        time.sleep(0.5)
    if srv:
        DHCP['server'] = srv
        DHCP['state'] = 'eigener Server aktiv' + (' (iOS-DHCP pausiert)' if DHCP['bootpd_paused'] else '')
        return
    if err is not None and err.errno in (48, 98):
        DHCP['state'] = ('iOS vergibt die Adressen (Port 67 belegt)' if mode == 'auto'
                         else 'iOS-DHCP ließ sich nicht pausieren – iOS vergibt die Adressen')
    else:
        DHCP['state'] = f'Fehler: {err}'
    log(f'DHCP: {DHCP["state"]}')
    resume_bootpd()


def stop_dhcp():
    if DHCP['server']:
        DHCP['server'].stop()
        log('DHCP: eigener Server gestoppt')
    DHCP['server'], DHCP['state'] = None, '–'
    resume_bootpd()


TESLA_OUI = ('4c:fc:aa', '98:ed:5c', '54:f8:f0', '0c:29:8f', 'cc:88:26')


def hotspot_clients():
    if not ARP:
        return 'arp fehlt'
    table = {ip: mac for ip, mac in arp_table().items() if ip != HOTSPOT_IP}
    return ', '.join(f"{ip} ({mac}{' = Tesla' if mac.startswith(TESLA_OUI) else ''})"
                     for ip, mac in sorted(table.items())) or 'keine'


SNIFF = {'s': None}


def start_sniffer(iface):
    try:
        s = dhcpsniff.Sniffer(iface, log)
        s.start()
        SNIFF['s'] = s
    except Exception as e:
        log(f'DHCP-Mitschnitt nicht möglich: {e}')


def stop_sniffer():
    if SNIFF['s']:
        SNIFF['s'].stop()
    SNIFF['s'] = None


def set_alias(iface, add):
    """/32 alias: the Tesla reaches it through its gateway 172.20.10.1, and the
    hotspot's own subnet (and its DHCP server) stays untouched."""
    if not IFCONFIG:
        STATE['last_error'] = 'ifconfig fehlt (Paket network-cmds), 100.96.0.1 nicht möglich'
        log(STATE['last_error'])
        return False
    args = [IFCONFIG, iface, 'inet', ALIAS_IP]
    args += ['netmask', '255.255.255.255', 'alias'] if add else ['-alias']
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
        if self.path in ('/mode', '/dhcp') and self.server.server_address == ('127.0.0.1', STATUS_PORT):
            m = data.decode(errors='ignore').partition('m=')[2].strip()
            if self.path == '/mode' and m in MODES:
                write_mode(m)
                log(f'Modus gewechselt: {m}')
                MODE_CHANGED.set()
            elif self.path == '/dhcp' and m in DHCP_MODES:
                try:
                    with open(DHCP_FILE, 'w') as f:
                        f.write(m)
                except OSError as e:
                    STATE['last_error'] = f'DHCP-Modus nicht gespeichert: {e}'
                log(f'DHCP-Modus: {m}')
                MODE_CHANGED.set()
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
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
        if self.server.server_address == ('127.0.0.1', STATUS_PORT):
            return                                   # the iPad's own status page
        if self.path not in ('/report',) and not self.path.startswith('/log'):
            log(f"{self.client_address[0]}  {fmt % args}")


def status_page():
    hs = STATE['hotspot']
    rows = [
        ('Dienst', f'läuft, Version {VERSION}, Python {sys.version.split()[0]}'),
        ('Hotspot 172.20.10.1', f'erkannt auf {hs}' if hs else 'nicht gefunden – CarPlay in Showcase starten'),
        ('Adresse 100.96.0.1', f"gesetzt auf {STATE['alias']}" if STATE['alias'] else 'nicht gesetzt'),
        ('Modus', MODES[read_mode()]),
        ('DHCP', (DHCP['state'] + (f" – vergeben: {DHCP['server'].summary()}" if DHCP['server'] else ''))
                 if hs else '– (Hotspot aus)'),
        ('DHCP-Modus', DHCP_MODES[read_dhcp_mode()]),
        ('Geräte im Hotspot', hotspot_clients() if hs else '–'),
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
            '<p>100.96.0.1 setzen: ' + ''.join(
                f'<form method="post" action="/mode" style="display:inline">'
                f'<button name="m" value="{m}" style="font-size:18px;margin:4px;padding:10px 14px">{m}</button></form>'
                for m in MODES) + '</p><p>Eigener DHCP-Server: ' + ''.join(
                f'<form method="post" action="/dhcp" style="display:inline">'
                f'<button name="m" value="{m}" style="font-size:18px;margin:4px;padding:10px 14px">{m}</button></form>'
                for m in DHCP_MODES) + '</p>'
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
    import signal

    def shutdown(signum, frame):
        log(f'beendet (Signal {signum}), räume auf')
        stop_dhcp()
        for iface, ips in ipv4_addresses().items():
            if ALIAS_IP in ips:
                set_alias(iface, False)
        os._exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log(f'Netzwerktest {VERSION} gestartet, HTTPS {"an" if HAVE_TLS else "aus (kein Zertifikat)"}, '
        f'ifconfig {IFCONFIG or "fehlt"}')
    status = start_servers(['127.0.0.1'], [(STATUS_PORT, False)])
    STATE['listening'] = []
    if NO_ALIAS:
        start_servers(['127.0.0.1'])
        while True:
            time.sleep(3600)
    servers, iface = [], None

    def stop_all():
        nonlocal servers
        stop_dhcp()
        stop_sniffer()
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        servers = []
        STATE['listening'] = []
        if STATE['alias']:
            set_alias(STATE['alias'], False)
        STATE['alias'] = None

    def start_all(hotspot_iface):
        nonlocal servers
        STATE['last_error'] = ''
        mode = read_mode()
        holder = alias_holder()
        if holder and (mode == 'off' or holder != ('lo0' if mode == 'lo0' else hotspot_iface)):
            set_alias(holder, False)          # left over from another mode or version
            holder = None
        if mode != 'off' and not holder:
            target = 'lo0' if mode == 'lo0' else hotspot_iface
            if set_alias(target, True):
                holder = target
            time.sleep(1)
        STATE['alias'] = holder
        start_sniffer(hotspot_iface)
        start_dhcp(hotspot_iface)
        servers = start_servers(([ALIAS_IP] if holder else []) + [HOTSPOT_IP])

    while True:
        found = hotspot_interface()
        if MODE_CHANGED.is_set():
            MODE_CHANGED.clear()
            if servers:
                stop_all()
        if found and not servers:
            iface = found[0]
            STATE['hotspot'] = iface
            log(f'Hotspot erkannt auf {iface}, Modus {read_mode()}')
            start_all(iface)
        elif not found and servers:
            log('Hotspot beendet, Server gestoppt')
            stop_all()
            iface, STATE['hotspot'] = None, None
        elif not found and not servers:
            holder = alias_holder()
            if holder:
                set_alias(holder, False)
        MODE_CHANGED.wait(3)

if __name__ == '__main__':
    main()
