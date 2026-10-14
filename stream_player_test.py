# EXE/stream_player_test.py
# Stream-to-Player core module.
# v2.0.7: TTML to SRT basic converter for guaranteed VLC subtitle compatibility.

import sys, os, json, struct, subprocess, threading, time, queue, importlib, traceback
import socket, re, urllib.request, urllib.error, io
from urllib.parse import urlparse, quote
import xml.etree.ElementTree as ET
import tempfile
import base64

_devnull = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None: sys.stderr = _devnull
elif hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
if sys.stdout is None: sys.stdout = _devnull

APP_NAME      = "Stream to Player"
APP_VERSION   = "1.0"
MANIFEST_NAME = "stream_player_opener"

_BASE_DIR   = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE_DIR, 'config.json')

DEFAULTS = {
    "computer_ip":           "",
    "dlna_host":             "",
    "dlna_port":             "",
    "firefox_registered":    False,
    "dlna_control_url":      "",
    "dlna_friendly_name":    "",
    "dlna_last_discovered":  0,
}

def _resource_path(filename: str) -> str:
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidate = os.path.join(meipass, filename)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(_BASE_DIR, filename)

cfg: dict = {}
_cfg_lock = threading.RLock()

def _detect_lan_ip() -> str:
    try:
        addrs = [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        for ip in addrs:
            if ip.startswith('192.168.'): return ip
        for ip in addrs:
            p = ip.split('.')
            if p[0] == '172' and 16 <= int(p[1]) <= 31: return ip
        for ip in addrs:
            if ip.startswith('10.') and not ip.startswith('127.'): return ip
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"

def load_config():
    global cfg
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            saved = json.load(f)
    except Exception:
        saved = {}
    cfg = {**DEFAULTS, **saved}
    if not cfg["computer_ip"]:
        cfg["computer_ip"] = _detect_lan_ip()

def _detect_vlc_path() -> str | None:
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\VideoLAN\VLC') as key:
                    install_dir, _ = winreg.QueryValueEx(key, 'InstallDir')
                    candidate = os.path.join(install_dir, 'vlc.exe')
                    if os.path.isfile(candidate):
                        return candidate
            except OSError:
                continue
    except ImportError:
        pass
    for path in (
        r'C:\Program Files\VideoLAN\VLC\vlc.exe',
        r'C:\Program Files (x86)\VideoLAN\VLC\vlc.exe',
    ):
        if os.path.isfile(path):
            return path
    return None

def save_config():
    with _cfg_lock:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)

def c(key):
    return cfg.get(key, DEFAULTS.get(key))

def _exe_path() -> str:
    return sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)

def register_native_host() -> tuple:
    try:
        import winreg
        manifest = {
            "name": MANIFEST_NAME,
            "description": "Stream to Player native host",
            "path": _exe_path(),
            "type": "stdio",
            "allowed_extensions": ["{a1b2c3d4-e5f6-7890-abcd-ef1234569000}"]
        }
        manifest_path = os.path.join(_BASE_DIR, f'{MANIFEST_NAME}.json')
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2)
        reg_path = rf'Software\Mozilla\NativeMessagingHosts\{MANIFEST_NAME}'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, manifest_path)
        return True, manifest_path
    except Exception as e:
        return False, str(e)

def _esc(s: str) -> str:
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def _soap(url: str, action: str, body: str, timeout: int = 8) -> bytes:
    data = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>' + body + '</s:Body></s:Envelope>').encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        'Content-Length': str(len(data)),
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _parse_dlna_desc(raw: bytes, desc_url: str) -> tuple:
    root  = ET.fromstring(raw)
    ns    = 'urn:schemas-upnp-org:device-1-0'
    fname = root.findtext(f'.//{{{ns}}}friendlyName', default='').strip()
    parsed = urlparse(desc_url)
    base   = f'{parsed.scheme}://{parsed.netloc}'
    for svc in root.findall(f'.//{{{ns}}}service'):
        if 'AVTransport' in svc.findtext(f'{{{ns}}}serviceType', ''):
            ctrl_path = svc.findtext(f'{{{ns}}}controlURL', '').strip()
            ctrl_url  = base + ctrl_path if ctrl_path.startswith('/') else f'{base}/{ctrl_path}'
            return fname, ctrl_url
    raise ValueError('AVTransport service not found in device description')

def _fetch_desc(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'Stream-to-Player/1.0'})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read()

def _ssdp_search(st: str, lan_ip: str, timeout: float = 4.0) -> list:
    SSDP_ADDR, SSDP_PORT = '239.255.255.250', 1900
    msg = '\r\n'.join([
        'M-SEARCH * HTTP/1.1',
        f'HOST: {SSDP_ADDR}:{SSDP_PORT}',
        'MAN: "ssdp:discover"',
        'MX: 3',
        f'ST: {st}',
        '', '',
    ]).encode('utf-8')
    locations: list = []
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        if lan_ip:
            try: sock.bind((lan_ip, 0))
            except Exception: pass
        sock.settimeout(timeout)
        sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
        _deadline = time.monotonic() + timeout
        while True:
            remaining = _deadline - time.monotonic()
            if remaining <= 0: break
            sock.settimeout(max(remaining, 0.1))
            try:
                data = sock.recv(4096).decode('utf-8', errors='replace')
                m = re.search(r'LOCATION:\s*(\S+)', data, re.IGNORECASE)
                if m and m.group(1) not in locations:
                    locations.append(m.group(1))
            except socket.timeout:
                break
    except Exception as e:
        sys.stderr.write(f'[DLNA] SSDP error ({st}): {e}\n')
    finally:
        if sock:
            try: sock.close()
            except Exception: pass
    return locations

def discover_dlna() -> tuple:
    lan_ip = cfg.get('computer_ip', '')
    locations: list = []
    for st in ('urn:schemas-upnp-org:device:MediaRenderer:1', 'ssdp:all'):
        for loc in _ssdp_search(st, lan_ip, timeout=4.0):
            if loc not in locations:
                locations.append(loc)
    for loc in locations:
        try:
            fname, ctrl_url = _parse_dlna_desc(_fetch_desc(loc), loc)
            p = urlparse(loc)
            with _cfg_lock:
                cfg['dlna_control_url']   = ctrl_url
                cfg['dlna_friendly_name'] = fname
                if p.hostname: cfg['dlna_host'] = p.hostname
                if p.port:     cfg['dlna_port'] = p.port
            return True, fname or 'Device found'
        except Exception:
            continue
    host, port = cfg.get('dlna_host', ''), cfg.get('dlna_port', '')
    if host and port:
        for cpath in ['/', '/desc.xml', '/description.xml', '/device.xml', '/rootDesc.xml']:
            try:
                url = f'http://{host}:{port}{cpath}'
                fname, ctrl_url = _parse_dlna_desc(_fetch_desc(url), url)
                with _cfg_lock:
                    cfg['dlna_control_url']   = ctrl_url
                    cfg['dlna_friendly_name'] = fname
                    cfg['dlna_host'] = host
                    cfg['dlna_port'] = port
                return True, fname or 'Device found'
            except Exception:
                continue
    return False, 'DLNA MediaRenderer not found'

_DLNA_POLL_INTERVAL = 900
_dlna_soap_lock = threading.Lock()
_dlna_available = False
_dlna_available_lock = threading.Lock()

def _check_dlna_availability():
    global _dlna_available
    ctrl = cfg.get('dlna_control_url', '')
    if not ctrl:
        with _dlna_available_lock:
            _dlna_available = False
        return False
    try:
        _soap(ctrl, 'GetTransportInfo',
            '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID></u:GetTransportInfo>', timeout=3)
        with _dlna_available_lock:
            _dlna_available = True
        return True
    except Exception:
        with _dlna_available_lock:
            _dlna_available = False
        return False

def _start_dlna_poller():
    def _poll():
        try: _check_dlna_availability()
        except Exception: pass
        while True:
            time.sleep(_DLNA_POLL_INTERVAL)
            try: _check_dlna_availability()
            except Exception: pass
    threading.Thread(target=_poll, daemon=True).start()

def is_dlna_available():
    with _dlna_available_lock:
        return _dlna_available

def dlna_cast(cast_url: str, title: str) -> dict:
    ctrl = cfg.get('dlna_control_url', '')
    if not ctrl:
        return {'success': False, 'error': "DLNA TV not found."}
    lan_ip = cfg.get('computer_ip', '')
    if lan_ip and lan_ip not in ('127.0.0.1', 'localhost', '0.0.0.0'):
        cast_url = cast_url.replace('127.0.0.1', lan_ip).replace('localhost', lan_ip)

    url_l = cast_url.lower()
    if url_l.endswith('.m3u8'):
        mime, op, flags = 'application/vnd.apple.mpegurl', '00', '81700000000000000000000000000000'
    elif url_l.endswith('.ts'):
        mime, op, flags = 'video/mp2t', '00', '81700000000000000000000000000000'
    else:
        mime, op, flags = 'video/mp4', '01', '01700000000000000000000000000000'

    proto = f'http-get:*:{mime}:DLNA.ORG_OP={op};DLNA.ORG_CI=0;DLNA.ORG_FLAGS={flags}'
    su    = cast_url.replace('&', '&amp;')
    meta  = (
        '&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
        f'&lt;item id="0" parentID="-1" restricted="0"&gt;'
        f'&lt;dc:title&gt;{_esc(title or "Video")}&lt;/dc:title&gt;'
        '&lt;upnp:class&gt;object.item.videoItem&lt;/upnp:class&gt;'
        f'&lt;res protocolInfo="{proto}"&gt;{su}&lt;/res&gt;'
        '&lt;/item&gt;&lt;/DIDL-Lite&gt;'
    )
    try:
        if not _dlna_soap_lock.acquire(timeout=10):
            return {'success': False, 'error': 'DLNA busy'}
        try:
            try:
                _soap(ctrl, 'Stop', '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:Stop>', timeout=5)
            except Exception:
                pass
            _soap(ctrl, 'SetAVTransportURI', f'<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID><CurrentURI>{su}</CurrentURI><CurrentURIMetaData>{meta}</CurrentURIMetaData></u:SetAVTransportURI>')
            _soap(ctrl, 'Play', '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID><Speed>1</Speed></u:Play>')
        finally:
            _dlna_soap_lock.release()
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def dlna_stop() -> dict:
    try:
        ctrl = cfg.get('dlna_control_url', '')
        if not ctrl: return {'success': False, 'error': 'DLNA TV not found'}
        if not _dlna_soap_lock.acquire(timeout=5): return {'success': False, 'error': 'DLNA busy'}
        try:
            _soap(ctrl, 'Stop', '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:Stop>', timeout=5)
        finally:
            _dlna_soap_lock.release()
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def dlna_get_transport_state() -> dict:
    try:
        ctrl = cfg.get('dlna_control_url', '')
        if not ctrl: return {'success': False, 'state': 'UNKNOWN'}
        if not _dlna_soap_lock.acquire(timeout=5): return {'success': False, 'state': 'UNKNOWN'}
        try:
            resp = _soap(ctrl, 'GetTransportInfo', '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:GetTransportInfo>', timeout=5)
        finally:
            _dlna_soap_lock.release()
        match = re.search(r'<CurrentTransportState>(\w+)</CurrentTransportState>', resp.decode('utf-8'))
        state = match.group(1) if match else 'UNKNOWN'
        return {'success': True, 'state': state}
    except Exception as e:
        return {'success': False, 'state': 'UNKNOWN'}

def is_player_running(player_path: str) -> bool:
    try:
        process_name = os.path.basename(player_path).replace('.exe', '').lower()
        if not process_name: return False
        result = subprocess.run(
            ['wmic', 'process', 'where', f'name like "%{process_name}%"', 'get', 'name'],
            capture_output=True, text=True, timeout=5, creationflags=0x08000000,
        )
        return process_name in result.stdout.lower()
    except Exception:
        return False

def get_player_state(player_path: str) -> dict:
    try:
        if not player_path: return {'state': 'unknown'}
        return {'state': 'running' if is_player_running(player_path) else 'stopped'}
    except Exception as e:
        return {'state': 'unknown', 'error': str(e)}

def _sleep_block(enable: bool):
    try:
        import ctypes
        flag = (0x80000000 | 0x00000001) if enable else 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(flag)
    except Exception:
        pass

_modules: dict = {}
_mod_lock = threading.Lock()

def _load_module(name: str):
    with _mod_lock:
        if name in _modules: return _modules[name]
        try:
            mod = importlib.import_module(f'{name}_module')
            mod.init(cfg)
            mod.start_server()
            _modules[name] = mod
            return mod
        except Exception as e:
            return None

def _read_exact(f, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        chunk = f.read(remaining)
        if not chunk: break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)

def _get_msg(f):
    raw = _read_exact(f, 4)
    if len(raw) < 4: return None
    n = struct.unpack('=I', raw)[0]
    data = _read_exact(f, n)
    if len(data) < n: return None
    return json.loads(data.decode('utf-8'))

def _encode_msg(msg: dict) -> bytes:
    data = json.dumps(msg, ensure_ascii=False).encode('utf-8')
    return struct.pack('=I', len(data)) + data

_player_procs: dict = {}
_player_procs_lock = threading.Lock()
_hls_proxy = None
_hls_proxy_lock = threading.Lock()
_echo_server = None

def _start_echo_server(port: int = 8085):
    global _echo_server
    try:
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        class EchoHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/echo':
                    dlna_enabled = is_dlna_available()
                    response = {'id': 'STP Host', 'dlnaAvailable': dlna_enabled}
                    response_json = json.dumps(response)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(response_json)))
                    self.end_headers()
                    self.wfile.write(response_json.encode('utf-8'))
                else:
                    self.send_response(404)
                    self.end_headers()
            def log_message(self, fmt, *args): pass
        _echo_server = ThreadingHTTPServer(('127.0.0.1', port), EchoHandler)
        threading.Thread(target=_echo_server.serve_forever, daemon=True).start()
    except Exception:
        pass

def _convert_ttml_to_srt(xml_data: bytes) -> str:
    """Конвертує TTML/EBU-TT XML субтитри у звичайний формат .srt, який VLC розуміє ідеально."""
    try:
        root = ET.fromstring(xml_data)
        # Знімаємо префікси простір імен для безпечного пошуку
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]
        
        srt_lines = []
        index = 1
        
        # Шукаємо всі блоки <p> (текст субтитрів з таймінгами)
        for p in root.iter('p'):
            begin = p.get('begin', '00:00:00.000').replace('.', ',')
            end = p.get('end', '00:00:05.000').replace('.', ',')
            
            # Форматування таймінгів для SRT (годин:хвилин:секунд,мілісекунди)
            if len(begin.split(':')) == 2:
                begin = '00:' + begin
            if len(end.split(':')) == 2:
                end = '00:' + end
                
            # Збираємо текст з урахуванням переносу рядків усередині тегу
            text_parts = []
            if p.text: text_parts.append(p.text.strip())
            for child in p:
                if child.tag == 'span':
                    if child.text: text_parts.append(child.text.strip())
                if child.tail: text_parts.append(child.tail.strip())
            
            sub_text = ' '.join(filter(None, text_parts))
            if sub_text:
                srt_lines.append(str(index))
                srt_lines.append(f"{begin} --> {end}")
                srt_lines.append(sub_text)
                srt_lines.append("")
                index += 1
                
        return "\n".join(srt_lines)
    except Exception as e:
        sys.stderr.write(f'[native_host] TTML conversion error: {e}\n')
        return ""

_WORKER_COUNT = 16

def native_host_loop():
    fin = os.fdopen(0, 'rb', buffering=0, closefd=False)
    fout = os.fdopen(1, 'wb', buffering=0, closefd=False)
    _reply_q: queue.Queue = queue.Queue()

    def _writer():
        while True:
            item = _reply_q.get()
            if item is None: break
            try:
                fout.write(item)
                fout.flush()
            except Exception: pass

    threading.Thread(target=_writer, daemon=True).start()

    def _reply(result: dict, seq):
        if seq is not None: result = {**result, 'seq': seq}
        _reply_q.put(_encode_msg(result))

    def _handle(msg: dict):
        seq = msg.get('seq')
        action = msg.get('action', '')
        try:
            if action == 'openPlayer':
                player = msg.get('playerPath', '')
                url = msg.get('streamUrl', '')
                title = msg.get('title')
                slave_urls = msg.get('slaveUrls', [])
                if not player or not url:
                    _reply({'success': False, 'error': 'Missing playerPath or streamUrl'}, seq)
                    return
                
                sub_arg = None
                if 'vlc.exe' in player.lower() and slave_urls and slave_urls[0]:
                    raw_sub = urllib.parse.unquote(slave_urls[0])
                    sub_data = None
                    if raw_sub.startswith('http://') or raw_sub.startswith('https://'):
                        try:
                            req = urllib.request.Request(raw_sub, headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(req, timeout=3) as resp:
                                sub_data = resp.read()
                        except Exception as e:
                            sys.stderr.write(f'[native_host] Subtitle download failed: {e}\n')
                    elif raw_sub.startswith('data:'):
                        try:
                            header, encoded = raw_sub.split(',', 1)
                            if ';base64' in header:
                                sub_data = base64.b64decode(encoded)
                            else:
                                sub_data = urllib.parse.unquote(encoded).encode('utf-8')
                        except Exception as e:
                            sys.stderr.write(f'[native_host] Data-URI sub parse failed: {e}\n')
                    
                    if sub_data:
                        # Конвертуємо TTML у чистий SRT формат і зберігаємо як .srt файл
                        srt_content = _convert_ttml_to_srt(sub_data)
                        if srt_content:
                            fd, temp_file_path = tempfile.mkstemp(suffix='.srt', prefix='stp_sub_')
                            os.close(fd)
                            with open(temp_file_path, 'w', encoding='utf-8') as tf:
                                tf.write(srt_content)
                            sub_arg = temp_file_path
                            sys.stderr.write(f'[native_host] Converted and saved SRT subtitles to: {sub_arg}\n')

                cmd = [player, url]
                if title and 'vlc.exe' in player.lower():
                    cmd.append(f'--meta-title={title}')
                
                if 'vlc.exe' in player.lower():
                    cmd.append('--fullscreen')
                    if sub_arg:
                        cmd.append(f'--sub-file={sub_arg}')
                        sys.stderr.write(f'[native_host] Attached SRT subtitle via --sub-file: {sub_arg}\n')

                proc = subprocess.Popen(cmd, shell=False)
                pid = proc.pid
                with _player_procs_lock:
                    _player_procs[pid] = proc
                def _watch_proc(p, k):
                    p.wait()
                    with _player_procs_lock:
                        _player_procs.pop(k, None)
                threading.Thread(target=_watch_proc, args=(proc, pid), daemon=True).start()
                _reply({'success': True, 'pid': pid}, seq)

            elif action == 'stopPlayer':
                pid = msg.get('pid')
                with _player_procs_lock:
                    proc = _player_procs.pop(pid, None) if pid else None
                if proc and proc.poll() is None:
                    proc.terminate()
                _reply({'success': True}, seq)

            elif action == 'getPlayerState':
                pid = msg.get('pid')
                with _player_procs_lock:
                    proc = _player_procs.get(pid) if pid else None
                if proc is None:
                    _reply({'state': 'stopped'}, seq)
                else:
                    _reply({'state': 'running' if proc.poll() is None else 'stopped'}, seq)

            elif action == 'dlnaCast':
                cast_url = msg.get('castUrl', '')
                title = msg.get('title', 'Video')
                lan_ip = cfg.get('computer_ip', '')
                if lan_ip and lan_ip not in ('127.0.0.1', 'localhost', '0.0.0.0'):
                    cast_url = cast_url.replace('127.0.0.1', lan_ip).replace('localhost', lan_ip)
                res = dlna_cast(cast_url, title)
                if res.get('success'): res['castUrl'] = cast_url
                _reply(res, seq)

            elif action == 'dlnaStop':
                _reply(dlna_stop(), seq)

            elif action == 'dlnaGetTransportState':
                _reply(dlna_get_transport_state(), seq)

            elif action == 'startHLSProxy':
                global _hls_proxy
                url = msg.get('url', '')
                if not url:
                    _reply({'success': False, 'error': 'Missing url'}, seq)
                    return
                with _hls_proxy_lock:
                    if _hls_proxy is None:
                        try:
                            from hls_proxy import HLSProxy
                            lan_ip = cfg.get('computer_ip') or '127.0.0.1'
                            _hls_proxy = HLSProxy(computer_ip=lan_ip, port=8085)
                        except ImportError as ie:
                            _reply({'success': False, 'error': f'hls_proxy not found: {ie}'}, seq)
                            return
                    if not _hls_proxy.is_running():
                        if not _hls_proxy.start():
                            _reply({'success': False, 'error': 'Proxy failed'}, seq)
                            return
                    _hls_proxy.new_session(url)
                _reply({'success': True, 'proxyUrl': _hls_proxy.url_for(url)}, seq)

            elif action == 'stopHLSProxy':
                if _hls_proxy: _hls_proxy.stop()
                _reply({'success': True}, seq)

            elif action == 'getProxyStats':
                if _hls_proxy and _hls_proxy.is_running():
                    _reply({'success': True, **_hls_proxy.get_stats()}, seq)
                else:
                    _reply({'success': False, 'error': 'STP Host not running'}, seq)

            elif action == 'pingNativeHost':
                _reply({'success': True, 'echo': 'STP Host'}, seq)

            elif action == 'detectVlc':
                path = _detect_vlc_path()
                _reply({'path': path}, seq)

            else:
                _reply({'success': False, 'error': f'Unknown action: {action}'}, seq)
        except Exception as e:
            _reply({'success': False, 'error': str(e)}, seq)

    _work_q: queue.Queue = queue.Queue()
    def _worker():
        while True:
            task = _work_q.get()
            if task is None: break
            try: task()
            except Exception: pass
    for _ in range(_WORKER_COUNT):
        threading.Thread(target=_worker, daemon=True).start()

    while True:
        try:
            msg = _get_msg(fin)
            if not msg: break
            _work_q.put(lambda m=msg: _handle(m))
        except Exception:
            break

    for _ in range(_WORKER_COUNT): _work_q.put(None)
    _reply_q.put(None)

def main():
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass and meipass not in sys.path:
        sys.path.insert(0, meipass)

    load_config()
    _start_echo_server(8085)
    _start_dlna_poller()

    def _is_pipe() -> bool:
        try:
            import ctypes
            return ctypes.windll.kernel32.GetFileType(ctypes.windll.kernel32.GetStdHandle(-10)) == 3
        except Exception:
            return (sys.stdin is not None) and (not sys.stdin.isatty())

    is_native = _is_pipe()
    if is_native:
        native_host_loop()
    else:
        _sleep_block(True)
        native_host_loop()

if __name__ == '__main__':
    main()