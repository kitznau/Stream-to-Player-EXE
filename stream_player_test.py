# EXE/stream_player_test.py
# Stream-to-Player core module.
# v2.1.8: HLS → MKV export is fast AND RAM-only. Segments are downloaded in
#         parallel (10 workers) into a bounded in-RAM window and served to
#         ffmpeg by a loopback HTTP server, so no temporary copy of the video
#         is ever written to disk (only the final MKV). Memory is capped at
#         roughly window × segment size because each segment is dropped as soon
#         as ffmpeg reads it. ffmpeg's own HLS demuxer fetches segments strictly
#         sequentially, which made remuxing much slower than the browser's
#         parallel downloader. downloadMkv now shows a Windows Save-As dialog
#         (phase 'selecting') and writes to "<name>.mkv.part", renamed only on
#         success. Live/unsupported playlists still use direct ffmpeg.
# v2.1.7: HLS → MKV remux is now fast: segments are downloaded in parallel
#         (10 workers) into a temp dir, rewritten into a local .m3u8 and then
#         remuxed by ffmpeg from disk. Live/unsupported playlists still use
#         direct ffmpeg. Export was two-phase ('prefetch' → 'remuxing');
#         'stopMkv' cancels either phase.
# v2.1.6: downloadMkv no longer writes an ffmpeg .log next to the output
#         (stderr → DEVNULL). ffmpeg now receives -progress <temp file> and the
#         new 'getMkvProgress' action returns the parsed progress (percent,
#         out_time, speed, size, ETA) for the browser overlay to poll.
# v2.1.5: TTML time normalization fixed. The first cue's begin is no longer
#         subtracted blindly (shifted every cue early when the first cue was not
#         at 0). Offset is now 36000 s only for EBU-TT timelines that start at
#         10:00:00 (min begin >= 36000), otherwise 0 (ttp:timeBase="media").
#         HH:MM:SS:FF frame timestamps are parsed too (25 fps).
# v2.1.4: Subtitle temp file is now named after the TTML language code
#         (xml:lang="de-DE" -> de.srt) so VLC shows a meaningful track name.
#         Each launch gets its own temp dir (stp_sub_XXXX/de.srt) to avoid
#         name collisions between parallel players; the dir is removed when
#         the player process exits (previously stp_sub_*.srt files leaked).
# v2.1.3: Full feature-set with robust EBU-TT/TTML to SRT normalization parser.

import sys, os, json, struct, subprocess, threading, time, queue, importlib, traceback
import socket, re, urllib.request, urllib.error, io, http.server
from urllib.parse import urlparse, quote
import xml.etree.ElementTree as ET
import tempfile
import base64
import shutil

# ── stdio fix for --noconsole ────────────────────────────────────────────────
_devnull = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None: sys.stderr = _devnull
elif hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
if sys.stdout is None: sys.stdout = _devnull

# ══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════════════════
#  RESOURCE PATH (frozen exe + dev mode)
# ══════════════════════════════════════════════════════════════════════════════

def _resource_path(filename: str) -> str:
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidate = os.path.join(meipass, filename)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(_BASE_DIR, filename)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════════════════
#  FIREFOX REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

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

def _silent_restart():
    try:
        subprocess.Popen([_exe_path()], creationflags=0x00000008)
    except Exception:
        pass
    os._exit(0)

# ══════════════════════════════════════════════════════════════════════════════
#  DLNA
# ══════════════════════════════════════════════════════════════════════════════

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
    sys.stderr.write(f'[DLNA] SSDP found {len(locations)} location(s)\n')
    for loc in locations:
        try:
            fname, ctrl_url = _parse_dlna_desc(_fetch_desc(loc), loc)
            p = urlparse(loc)
            with _cfg_lock:
                cfg['dlna_control_url']   = ctrl_url
                cfg['dlna_friendly_name'] = fname
                if p.hostname: cfg['dlna_host'] = p.hostname
                if p.port:     cfg['dlna_port'] = p.port
            sys.stderr.write(f'[DLNA] DMR found: {fname} @ {ctrl_url}\n')
            return True, fname or 'Device found'
        except Exception as e:
            sys.stderr.write(f'[DLNA] Skip {loc}: {e}\n')
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
    found_n = len(locations)
    hint = (f' ({found_n} UPnP device(s) found but none had AVTransport)' if found_n
            else ' (no UPnP devices found — check network/firewall)')
    return False, f'DLNA MediaRenderer not found{hint}'

_DLNA_DISCOVER_INTERVAL = 86400
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
        try:
            _check_dlna_availability()
        except Exception:
            pass
        while True:
            time.sleep(_DLNA_POLL_INTERVAL)
            try:
                _check_dlna_availability()
            except Exception:
                pass
    threading.Thread(target=_poll, daemon=True).start()

def is_dlna_available():
    with _dlna_available_lock:
        return _dlna_available

def dlna_cast(cast_url: str, title: str) -> dict:
    ctrl = cfg.get('dlna_control_url', '')
    if not ctrl:
        return {'success': False, 'error': "DLNA TV not found. Use 'Find TV' in Settings."}

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
        '&lt;item id="0" parentID="-1" restricted="0"&gt;'
        f'&lt;dc:title&gt;{_esc(title or "Video")}&lt;/dc:title&gt;'
        '&lt;upnp:class&gt;object.item.videoItem&lt;/upnp:class&gt;'
        f'&lt;res protocolInfo="{proto}"&gt;{su}&lt;/res&gt;'
        '&lt;/item&gt;&lt;/DIDL-Lite&gt;'
    )
    try:
        if not _dlna_soap_lock.acquire(timeout=10):
            return {'success': False, 'error': 'DLNA busy, try again'}
        try:
            try:
                _soap(ctrl, 'Stop',
                      '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                      '<InstanceID>0</InstanceID></u:Stop>', timeout=5)
            except Exception:
                pass
            _soap(ctrl, 'SetAVTransportURI',
                  f'<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                  f'<InstanceID>0</InstanceID>'
                  f'<CurrentURI>{su}</CurrentURI>'
                  f'<CurrentURIMetaData>{meta}</CurrentURIMetaData>'
                  f'</u:SetAVTransportURI>')
            _soap(ctrl, 'Play',
                  '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                  '<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>')
        finally:
            _dlna_soap_lock.release()
        return {'success': True}
    except urllib.error.URLError as e:
        return {'success': False, 'error': f'Network: {e.reason}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def dlna_stop() -> dict:
    try:
        ctrl = cfg.get('dlna_control_url', '')
        if not ctrl:
            return {'success': False, 'error': 'DLNA TV not found'}
        if not _dlna_soap_lock.acquire(timeout=5):
            return {'success': False, 'error': 'DLNA busy'}
        try:
            _soap(ctrl, 'Stop',
                  '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                  '<InstanceID>0</InstanceID></u:Stop>', timeout=5)
        finally:
            _dlna_soap_lock.release()
        return {'success': True}
    except urllib.error.URLError as e:
        return {'success': False, 'error': f'Network: {e.reason}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def dlna_get_transport_state() -> dict:
    try:
        ctrl = cfg.get('dlna_control_url', '')
        if not ctrl:
            return {'success': False, 'error': 'DLNA TV not found', 'state': 'UNKNOWN'}
        if not _dlna_soap_lock.acquire(timeout=5):
            return {'success': False, 'error': 'DLNA busy', 'state': 'UNKNOWN'}
        try:
            resp = _soap(ctrl, 'GetTransportInfo',
                  '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                  '<InstanceID>0</InstanceID></u:GetTransportInfo>', timeout=5)
        finally:
            _dlna_soap_lock.release()

        match = re.search(r'<CurrentTransportState>(\w+)</CurrentTransportState>', resp.decode('utf-8'))
        state = match.group(1) if match else 'UNKNOWN'
        return {'success': True, 'state': state}
    except urllib.error.URLError as e:
        return {'success': False, 'error': f'Network: {e.reason}', 'state': 'UNKNOWN'}
    except Exception as e:
        return {'success': False, 'error': str(e), 'state': 'UNKNOWN'}

# ══════════════════════════════════════════════════════════════════════════════
#  PLAYER STATE CHECK
# ══════════════════════════════════════════════════════════════════════════════

def is_player_running(player_path: str) -> bool:
    try:
        process_name = os.path.basename(player_path).replace('.exe', '').lower()
        if not process_name:
            return False
        result = subprocess.run(
            ['wmic', 'process', 'where', f'name like "%{process_name}%"', 'get', 'name'],
            capture_output=True, text=True, timeout=5, creationflags=0x08000000,
        )
        return process_name in result.stdout.lower()
    except Exception:
        return False

def get_player_state(player_path: str) -> dict:
    try:
        if not player_path:
            return {'state': 'unknown', 'error': 'Missing playerPath'}
        return {'state': 'running' if is_player_running(player_path) else 'stopped'}
    except Exception as e:
        return {'state': 'unknown', 'error': str(e)}

def _auto_discover_dlna():
    last = cfg.get('dlna_last_discovered', 0)
    has_url = bool(cfg.get('dlna_control_url'))
    is_reachable = False
    if has_url:
        is_reachable = _check_dlna_availability()
        
    if has_url and is_reachable:
        if time.time() - last < _DLNA_DISCOVER_INTERVAL:
            return

    def _run():
        try:
            sys.stderr.write('[DLNA] Discovery started...\n')
            ok, info = discover_dlna()
            if ok:
                with _cfg_lock:
                    cfg['dlna_last_discovered'] = time.time()
                save_config()
                _check_dlna_availability()
        except Exception as e:
            sys.stderr.write(f'[DLNA] Discovery thread error: {e}\n')
    threading.Thread(target=_run, daemon=True).start()

# ══════════════════════════════════════════════════════════════════════════════
#  SLEEP BLOCK
# ══════════════════════════════════════════════════════════════════════════════

_ES_CONTINUOUS      = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

def _sleep_block(enable: bool):
    try:
        import ctypes
        flag = (_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED) if enable else _ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flag)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
#  LAZY MODULE LOADER
# ══════════════════════════════════════════════════════════════════════════════

_modules: dict = {}
_mod_lock = threading.Lock()

def _load_module(name: str):
    with _mod_lock:
        if name in _modules:
            return _modules[name]
        try:
            mod = importlib.import_module(f'{name}_module')
            mod.init(cfg)
            mod.start_server()
            _modules[name] = mod
            _rebuild_tray_menu()
            return mod
        except Exception as e:
            msg = f'[loader] {name}_module: {e}\n{traceback.format_exc()}'
            sys.stderr.write(msg)
            try:
                with open(os.path.join(_BASE_DIR, 'stp_error.log'), 'a', encoding='utf-8') as lf:
                    lf.write(msg)
            except Exception:
                pass
            return None

def get_module(name: str):
    return _modules.get(name) or _load_module(name)

# ══════════════════════════════════════════════════════════════════════════════
#  SUBTITLE CONVERTER (TTML/EBU-TT to SRT with normalization & colors)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_time_to_seconds(t_str: str) -> float:
    parts = t_str.replace(',', '.').split(':')
    try:
        if len(parts) == 4:  # HH:MM:SS:FF (frames, 25 fps)
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2]) + float(parts[3]) / 25.0
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
    except Exception:
        pass
    return 0.0

def _seconds_to_srt_time(sec: float) -> str:
    if sec < 0: sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000: ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _ttml_lang_code(xml_data: bytes) -> str:
    """Primary language subtag from the root xml:lang ("de-DE" -> "de").
    Falls back to "sub" when missing or not a plain alphanumeric code."""
    try:
        head = xml_data[:4096].decode('utf-8', errors='ignore')
        m = re.search(r'\bxml:lang\s*=\s*["\']([^"\']+)["\']', head)
        if m:
            code = re.split(r'[-_]', m.group(1).strip())[0].lower()
            if re.fullmatch(r'[a-z0-9]{2,8}', code):
                return code
    except Exception:
        pass
    return 'sub'

def _convert_ttml_to_srt(xml_data: bytes) -> str:
    try:
        tree = ET.ElementTree(ET.fromstring(xml_data))
        root = tree.getroot()
        
        def local_name(tag):
            return tag.split('}')[-1] if '}' in tag else tag

        srt_lines = []
        parsed_subs = []
        min_begin_sec = float('inf')

        # Рекурсивна функція для збору чистого тексту без стилів і кольорів
        def extract_text(element):
            chunks = []
            if element.text:
                chunks.append(element.text)
            
            for child in element:
                c_name = local_name(child.tag)
                if c_name == 'br':
                    chunks.append('\n')
                else:
                    chunks.append(extract_text(child))
                
                if child.tail:
                    chunks.append(child.tail)
                        
            return "".join(chunks)

        # Шукаємо всі блоки субтитрів (<p>)
        for elem in root.iter():
            if local_name(elem.tag) == 'p':
                begin = elem.attrib.get('begin')
                end = elem.attrib.get('end')
                
                if not begin or not end:
                    continue
                    
                b_sec = _parse_time_to_seconds(begin)
                e_sec = _parse_time_to_seconds(end)
                
                if b_sec < min_begin_sec:
                    min_begin_sec = b_sec
                    
                raw_text = extract_text(elem)
                
                # Очищаємо текст від зайвих пробілів та некоректних символів
                content_clean = (raw_text
                                 .replace('&amp;', '&')
                                 .replace('&lt;', '<')
                                 .replace('&gt;', '>')
                                 .replace('&quot;', '"')
                                 .replace('&apos;', "'"))
                
                lines = [l.strip() for l in content_clean.splitlines()]
                final_text = '\n'.join(filter(None, lines))
                
                if final_text:
                    parsed_subs.append((b_sec, e_sec, final_text))
            
        # Зсув таймкоду для EBU-TT (якщо хронометраж починається з 10:00:00)
        if min_begin_sec == float('inf') or min_begin_sec < 36000.0:
            time_offset = 0.0
        else:
            time_offset = 36000.0
            
        for index, (b_sec, e_sec, final_text) in enumerate(parsed_subs, 1):
            shifted_b = b_sec - time_offset
            shifted_e = e_sec - time_offset
            if shifted_e <= shifted_b:
                shifted_e = shifted_b + 2.0
                
            begin_str = _seconds_to_srt_time(shifted_b)
            end_str = _seconds_to_srt_time(shifted_e)
            
            srt_lines.append(str(index))
            srt_lines.append(f"{begin_str} --> {end_str}")
            srt_lines.append(final_text)
            srt_lines.append("")
            
        return "\n".join(srt_lines)
    except Exception as e:
        sys.stderr.write(f'[native_host] TTML XML parsing failed: {e}\n')
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  HLS → MKV REMUX: PARALLEL DOWNLOAD, RAM-BUFFERED
#
#  ffmpeg's HLS demuxer downloads segments strictly one after another, so the
#  export was limited by network round-trips (the browser's JS downloader, with
#  6 parallel workers, felt much faster). To remove that bottleneck the EXE now
#  downloads segments in parallel (10 workers) into a bounded RAM buffer and
#  hands them to ffmpeg through a loopback-only HTTP server:
#
#      workers ──► RAM window (N segments) ──► 127.0.0.1 ──► ffmpeg
#
#  Only the final MKV touches the disk, so an export no longer needs temporary
#  space (and SSD writes) equal to the whole video. Memory stays bounded: a
#  segment is dropped as soon as ffmpeg has read it, so usage is roughly
#  window × segment size (tens of MB for typical streams).
#
#  AES-128 / fMP4 streams keep working: EXT-X-KEY / EXT-X-MAP URIs stay
#  absolute and remote, so ffmpeg fetches the keys and decrypts the segments it
#  reads from the local server. Live or unsupported playlists fall back to the
#  old direct-ffmpeg path.
# ══════════════════════════════════════════════════════════════════════════════

_MKV_JOB_TTL      = 600          # keep finished jobs pollable for 10 minutes
_mkv_jobs: dict   = {}           # job_id -> job dict
_mkv_jobs_lock    = threading.Lock()
_mkv_job_seq      = 0

_STREAM_WORKERS      = 10        # parallel segment downloads (JS downloader uses 6)
_STREAM_WINDOW       = 10        # max segments held in RAM ahead of ffmpeg
_STREAM_RAM_BUDGET   = 384 * 1024 * 1024   # shrink the window above this much RAM
_STREAM_KEEP_SERVED  = 3         # recently served segments kept for re-reads
_STREAM_MAX_SEGMENTS = 20000
_STREAM_TIMEOUT      = 20        # per-request socket timeout, seconds
_STREAM_RETRIES      = 3
_STREAM_WAIT         = 150       # how long ffmpeg may wait for one segment

class _StreamCancelled(Exception):
    pass

def _next_mkv_job_id() -> int:
    global _mkv_job_seq
    with _mkv_jobs_lock:
        _mkv_job_seq += 1
        return _mkv_job_seq

def _as_int(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None

def _find_mkv_job(jid, pid, newest_if_none: bool = False):
    """Job lookup by explicit jobId → ffmpeg pid → (optionally) newest job.

    Older extension builds poll with no id at all, so getMkvProgress keeps the
    'newest job' fallback; stopMkv must never cancel an arbitrary job."""
    with _mkv_jobs_lock:
        if jid is not None:
            job = _mkv_jobs.get(jid)
            if job is not None:
                return job
            if pid is None:
                return None            # explicit but unknown jobId → do not guess
        if pid is not None:
            return next((j for j in _mkv_jobs.values() if j.get('pid') == pid), None)
        if newest_if_none and _mkv_jobs:
            return max(_mkv_jobs.values(), key=lambda j: j['started_at'])
    return None

def _read_ffmpeg_progress(path: str) -> dict:
    """Latest key=value values from an ffmpeg -progress file.

    ffmpeg appends a small block of key=value lines every ~0.5 s and flushes
    after each block, so only the tail is read. A partial first line (when the
    seek landed mid-line) and a partial last line (block still being written)
    are dropped."""
    try:
        with open(path, 'rb') as f:
            try:
                f.seek(-65536, os.SEEK_END)
                partial_head = True
            except OSError:          # file smaller than 64 KiB
                f.seek(0)
                partial_head = False
            raw = f.read()
    except OSError:
        return {}

    complete_tail = raw.endswith(b'\n')
    lines = raw.decode('utf-8', errors='replace').replace('\r', '\n').split('\n')
    if partial_head and lines:
        lines = lines[1:]
    if not complete_tail and lines:
        lines = lines[:-1]

    data = {}
    for line in lines:
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        key = key.strip()
        if key:
            data[key] = val.strip()
    return data

def _ffmpeg_out_time(prog: dict) -> float:
    """Seconds already processed, from an ffmpeg progress dict.
    Note: ffmpeg reports both out_time_us and out_time_ms in microseconds."""
    for key in ('out_time_us', 'out_time_ms'):
        val = prog.get(key)
        if val and val != 'N/A':
            try:
                return max(0.0, int(val) / 1_000_000.0)
            except (TypeError, ValueError):
                pass
    val = prog.get('out_time', '')
    if val and val != 'N/A':
        try:
            return max(0.0, _parse_time_to_seconds(val))
        except Exception:
            pass
    return 0.0

def _ffmpeg_speed(prog: dict) -> float | None:
    raw = (prog.get('speed') or '').strip().rstrip('x')
    try:
        return float(raw)
    except ValueError:
        return None

def _fetch_text(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(8 * 1024 * 1024).decode('utf-8', errors='replace')

def _resolve_hls_media(url: str, _depth: int = 0) -> tuple:
    """(media_playlist_url, text) — follows a master playlist to its first variant."""
    text = _fetch_text(url)
    lines = [ln.strip() for ln in text.splitlines()]
    if _depth < 2 and any(ln.startswith('#EXT-X-STREAM-INF') for ln in lines):
        for i, ln in enumerate(lines):
            if not ln.startswith('#EXT-X-STREAM-INF'):
                continue
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith('#'):
                    return _resolve_hls_media(urllib.parse.urljoin(url, nxt), _depth + 1)
            break
    return url, text

def _parse_media_playlist(text: str, base_url: str) -> dict:
    """Segments (absolute URLs + optional byte ranges) and VOD metadata."""
    segments = []
    total = 0.0
    pending_dur = 0.0
    pending_range = None            # (offset|None, length)
    range_ends: dict = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXTINF:'):
            try:
                pending_dur = float(line.split(':', 1)[1].split(',', 1)[0])
            except ValueError:
                pending_dur = 0.0
            continue
        if line.startswith('#EXT-X-BYTERANGE'):
            spec = line.split(':', 1)[1].strip() if ':' in line else ''
            try:
                if '@' in spec:
                    length, off = spec.split('@', 1)
                    pending_range = (int(off), int(length))
                else:
                    pending_range = (None, int(spec))
            except ValueError:
                pending_range = None
            continue
        if line.startswith('#'):
            continue

        url = urllib.parse.urljoin(base_url, line)
        seg_range = None
        if pending_range is not None:
            off, length = pending_range
            if off is None:
                off = range_ends.get(url, 0)
            seg_range = (off, length)
            range_ends[url] = off + length
        segments.append({'url': url, 'duration': pending_dur, 'range': seg_range})
        total += pending_dur
        pending_dur = 0.0
        pending_range = None

    return {
        'segments': segments,
        'duration': total,
        'live':     '#EXT-X-ENDLIST' not in text,
        'iframe':   '#EXT-X-I-FRAMES-ONLY' in text,
    }

def _probe_hls_duration(job: dict, url: str):
    try:
        media_url, text = _resolve_hls_media(url)
        info = _parse_media_playlist(text, media_url)
        job['duration'] = info['duration'] or None
        job['live']     = info['live']
    except Exception as e:
        sys.stderr.write(f'[native_host] duration probe failed: {e}\n')

_HLS_URL_RE     = re.compile(r'\.m3u8?($|[?#])', re.I)
_MEDIA_FILE_RE  = re.compile(r'\.(mp4|m4v|webm|mkv|mov|avi|flv|3gp)($|[?#])', re.I)

def _looks_like_hls(url: str) -> bool:
    """True for playlist URLs, False for direct media files (MP4, WebM, …).

    ARD Mediathek and similar sites expose both: the adaptive player uses an
    .m3u8 playlist, while canPlayType fallback / direct links point at a plain
    .mp4. A URL with no recognisable extension is treated as a playlist —
    ffmpeg's HLS probe then decides, and a non-playlist simply falls back."""
    u = url or ''
    if _HLS_URL_RE.search(u):
        return True
    return not _MEDIA_FILE_RE.search(u)

def _probe_media_duration(job: dict, url: str, ffmpeg_path: str):
    """Best-effort duration of a direct media file (ffprobe next to ffmpeg)."""
    probe = None
    if ffmpeg_path:
        cand = os.path.join(
            os.path.dirname(ffmpeg_path),
            'ffprobe.exe' if os.name == 'nt' else 'ffprobe',
        )
        if os.path.isfile(cand):
            probe = cand
    if not probe:
        probe = shutil.which('ffprobe')
    if not probe:
        return
    try:
        res = subprocess.run(
            [probe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', url],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        lines = [ln.strip() for ln in (res.stdout or '').splitlines() if ln.strip()]
        if lines:
            dur = float(lines[0])
            if dur > 0:
                job['duration'] = dur
    except Exception as e:
        sys.stderr.write(f'[native_host] ffprobe duration failed: {e}\n')

def _playlist_for_server(text: str, base_url: str, names: list) -> str:
    """Rewrite a media playlist so segments point at the local RAM server.

    Segment lines become relative names served by _MemHlsServer. Keys
    (EXT-X-KEY) and init sections (EXT-X-MAP) keep working: their URIs are made
    absolute and stay remote — ffmpeg fetches/decrypts them as usual.
    EXT-X-BYTERANGE tags are dropped because each buffered segment already
    contains exactly the requested byte range."""
    out = []
    idx = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            if line.startswith('#EXT-X-BYTERANGE'):
                continue
            if line.startswith(('#EXT-X-KEY:', '#EXT-X-MAP:', '#EXT-X-SESSION-KEY:')):
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: 'URI="%s"' % urllib.parse.urljoin(base_url, m.group(1)),
                    line,
                )
            out.append(line)
            continue
        if idx < len(names):
            out.append(names[idx])
        idx += 1
    return '\n'.join(out) + '\n'

def _download_hls_segment(seg: dict, f, job: dict) -> int:
    """Stream one segment into an open binary file or BytesIO."""
    headers = {'User-Agent': 'Mozilla/5.0'}
    rng = seg.get('range')
    if rng:
        off, length = rng
        headers['Range'] = f'bytes={off}-{off + length - 1}'

    req = urllib.request.Request(seg['url'], headers=headers)
    written = 0
    with urllib.request.urlopen(req, timeout=_STREAM_TIMEOUT) as r:
        status = getattr(r, 'status', 200)
        skip  = rng[0] if (rng and status != 206) else 0   # server ignored Range
        limit = rng[1] if rng else None
        while True:
            if job['cancel'].is_set():
                raise _StreamCancelled()
            chunk = r.read(262144)
            if not chunk:
                break
            if skip:
                if len(chunk) <= skip:
                    skip -= len(chunk)
                    continue
                chunk = chunk[skip:]
                skip = 0
            if limit is not None:
                chunk = chunk[:max(0, limit - written)]
            if chunk:
                f.write(chunk)
                written += len(chunk)
                with job['stat_lock']:
                    job['download_bytes'] = job.get('download_bytes', 0) + len(chunk)
            if limit is not None and written >= limit:
                break

    if written == 0:
        raise IOError('empty segment')
    return written

def _download_hls_segment_retry(seg: dict, f, job: dict) -> int:
    last: Exception | None = None
    for attempt in range(_STREAM_RETRIES):
        if job['cancel'].is_set():
            raise _StreamCancelled()
        f.seek(0)
        f.truncate(0)
        try:
            return _download_hls_segment(seg, f, job)
        except _StreamCancelled:
            raise
        except Exception as e:
            last = e
            try:                                     # drop the partial body
                stale = f.tell()
                f.seek(0)
                f.truncate(0)
                with job['stat_lock']:
                    job['download_bytes'] = max(0, job.get('download_bytes', 0) - stale)
            except Exception:
                pass
            time.sleep(0.4 * (attempt + 1))
    raise last if last else IOError('download failed')

class _SegmentBuffer:
    """Parallel segment downloader with a bounded in-RAM window.

    Workers claim the next segment index in order and stay at most `window`
    segments ahead of the segment ffmpeg is consuming, so memory use is
    window × segment size instead of the whole movie. A segment is handed to
    the HTTP handler once its download finishes, and dropped from RAM shortly
    after ffmpeg has read it.
    """

    def __init__(self, segments: list, job: dict):
        self.segments = segments
        self.job      = job
        self.error    = None          # fatal download error (string)
        self._cond    = threading.Condition()
        self._data: dict = {}         # index -> bytes
        self._ready: dict = {}        # index -> threading.Event
        self._errors: dict = {}       # index -> error string
        self._next    = 0             # next index not yet claimed by a worker
        self._served  = 0             # lowest index ffmpeg has not consumed
        self._avg     = 0.0           # running average segment size, bytes
        self._closed  = False
        self._threads: list = []

    # ── helpers ───────────────────────────────────────────────────────────────
    @property
    def count(self) -> int:
        return len(self.segments)

    def _window(self) -> int:
        if self._avg <= 0:
            return _STREAM_WINDOW
        by_bytes = int(_STREAM_RAM_BUDGET // self._avg)
        return max(2, min(_STREAM_WINDOW, by_bytes))

    def _event(self, idx: int) -> threading.Event:
        ev = self._ready.get(idx)
        if ev is None:
            ev = self._ready[idx] = threading.Event()
        return ev

    def _abort(self, message: str):
        with self._cond:
            if self.error is None:
                self.error = message
            self._closed = True
            self._cond.notify_all()
        with self.job['stat_lock']:
            if not self.job.get('stream_error'):
                self.job['stream_error'] = message

    # ── worker pool ───────────────────────────────────────────────────────────
    def start(self):
        for _ in range(max(1, min(_STREAM_WORKERS, self.count))):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._threads.append(t)

    def close(self):
        with self._cond:
            if self.error is None:
                self.error = 'stopped'
            self._closed = True
            self._cond.notify_all()

    def _claim(self):
        with self._cond:
            while True:
                if self._closed or self.job['cancel'].is_set():
                    return None
                if self._next >= self.count:
                    return None
                if self._next < self._served + self._window():
                    idx = self._next
                    self._next += 1
                    return idx
                self._cond.wait(0.2)

    def _worker(self):
        while True:
            idx = self._claim()
            if idx is None:
                return
            ev = self._event(idx)
            buf = io.BytesIO()
            try:
                _download_hls_segment_retry(self.segments[idx], buf, self.job)
            except _StreamCancelled:
                with self._cond:
                    self._errors.setdefault(idx, 'cancelled')
                self._abort('cancelled')
                ev.set()
                return
            except Exception as e:
                msg = str(e) or 'download failed'
                with self._cond:
                    self._errors.setdefault(idx, msg)
                self._abort(msg)
                ev.set()
                return

            data = buf.getvalue()
            stored = False
            with self._cond:
                if not self._closed:
                    self._data[idx] = data
                    self._avg = len(data) if self._avg <= 0 else (self._avg * 0.8 + len(data) * 0.2)
                    stored = True
            ev.set()
            if stored:
                with self.job['stat_lock']:
                    self.job['buffered_bytes'] = self.job.get('buffered_bytes', 0) + len(data)
            with self.job['stat_lock']:
                self.job['segments_done'] = self.job.get('segments_done', 0) + 1

    # ── consumer side (HTTP handler) ──────────────────────────────────────────
    def get(self, idx: int, timeout: float = _STREAM_WAIT) -> bytes:
        """Bytes for segment `idx`, waiting for the download if necessary."""
        with self._cond:
            data = self._data.get(idx)
            if data is not None:
                return data
            already_passed = idx < self._next and idx < self._served
        if already_passed:
            # ffmpeg re-opened a segment that was already dropped from RAM.
            buf = io.BytesIO()
            _download_hls_segment_retry(self.segments[idx], buf, self.job)
            return buf.getvalue()

        ev = self._event(idx)
        deadline = time.time() + timeout
        while True:
            with self._cond:
                data = self._data.get(idx)
                if data is not None:
                    return data
                err = self._errors.get(idx) or self.error
                if err:
                    raise IOError(err)
            remaining = deadline - time.time()
            if remaining <= 0:
                raise IOError('timed out waiting for segment')
            ev.wait(min(0.5, remaining))

    def mark_served(self, idx: int):
        """ffmpeg finished reading segment `idx`: free RAM it no longer needs."""
        with self._cond:
            if idx + 1 > self._served:
                self._served = idx + 1
            keep_from = self._served - _STREAM_KEEP_SERVED
            freed = 0
            for k in [k for k in self._data if k < keep_from]:
                freed += len(self._data.pop(k))
                self._ready.pop(k, None)
            if freed:
                with self.job['stat_lock']:
                    self.job['buffered_bytes'] = max(
                        0, self.job.get('buffered_bytes', 0) - freed)
            self._cond.notify_all()

class _MemHlsHandler(http.server.BaseHTTPRequestHandler):
    """Serves the rewritten playlist and buffered segments on 127.0.0.1."""

    protocol_version = 'HTTP/1.0'

    def log_message(self, fmt, *args):
        pass

    def _is_mp4(self, name: str) -> bool:
        return name.lower().endswith(('.m4s', '.mp4', '.m4a'))

    def _send(self, code: int, body: bytes, ctype: str, head_only: bool = False):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self, head_only: bool = False):
        srv = self.server
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).lstrip('/')
        if path == srv.playlist_path:
            self._send(200, srv.playlist_bytes, 'application/vnd.apple.mpegurl', head_only)
            return
        idx = srv.names.get(path)
        if idx is None:
            self._send(404, b'not found', 'text/plain', head_only)
            return
        try:
            data = srv.store.get(idx)
        except Exception as e:
            sys.stderr.write(f'[native_host] segment {idx} unavailable: {e}\n')
            self._send(502, str(e).encode('utf-8', 'replace'), 'text/plain', head_only)
            return
        ctype = 'video/mp4' if self._is_mp4(path) else 'video/mp2t'
        try:
            self._send(200, data, ctype, head_only)
        finally:
            if not head_only:
                srv.store.mark_served(idx)

    def do_HEAD(self):
        self.do_GET(head_only=True)

class _MemHlsServer:
    """Loopback HTTP server exposing the RAM buffer to ffmpeg.

    ffmpeg's HLS demuxer only reads URLs/files, so the buffered segments are
    published as http://127.0.0.1:<random port>/<random token>/… . The socket
    is bound to loopback only and both the port and the path token are random,
    so other local processes cannot easily grab the stream."""

    def __init__(self, playlist_text: str, names: list, store: _SegmentBuffer, token: str):
        self.playlist_bytes = playlist_text.encode('utf-8')
        self.names = {name: i for i, name in enumerate(names)}
        self.playlist_path = f'{token}/playlist.m3u8'
        self.store = store
        self.url   = None
        self._httpd = None

    def start(self) -> str:
        class _Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                # ffmpeg closes the socket when it is terminated (cancel) or
                # stops reading early — not worth a traceback in the log.
                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                    return
                super().handle_error(request, client_address)

        self._httpd = _Server(('127.0.0.1', 0), _MemHlsHandler)
        self._httpd.playlist_bytes = self.playlist_bytes
        self._httpd.playlist_path = self.playlist_path
        self._httpd.names = self.names
        self._httpd.store = self.store
        port = self._httpd.server_address[1]
        threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={'poll_interval': 0.2},
            daemon=True,
        ).start()
        self.url = f'http://127.0.0.1:{port}/{self.playlist_path}'
        return self.url

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return

        def _shutdown():
            try: httpd.shutdown()
            except Exception: pass
            try: httpd.server_close()
            except Exception: pass

        # Bounded wait: shutdown() blocks until serve_forever() returns.
        t = threading.Thread(target=_shutdown, daemon=True)
        t.start()
        t.join(3.0)

def _start_streaming_input(playlist_url: str, job: dict):
    """Build a RAM-buffered input for ffmpeg.

    Returns (server, store) or (None, None) when this playlist cannot be
    streamed that way (live, I-frame-only, non-http segments, too long)."""
    media_url, text = _resolve_hls_media(playlist_url)
    info = _parse_media_playlist(text, media_url)
    segs = info['segments']
    job['duration'] = info['duration'] or None
    job['live']     = info['live']

    if (info['live'] or info['iframe'] or not segs
            or len(segs) > _STREAM_MAX_SEGMENTS
            or not all(s['url'].lower().startswith(('http://', 'https://')) for s in segs)):
        return None, None

    local_names = []
    token = os.urandom(8).hex()
    for i, seg in enumerate(segs):
        ext = os.path.splitext(urllib.parse.urlparse(seg['url']).path)[1].lower()
        if ext not in ('.ts', '.m4s', '.mp4', '.m4a', '.aac', '.mp3', '.ac3', '.ec3'):
            ext = '.ts'
        local_names.append(f'seg_{i:05d}{ext}')

    playlist_text = _playlist_for_server(text, media_url, local_names)
    with job['stat_lock']:
        job['segments_total'] = len(segs)
        job['segments_done']  = 0

    store  = _SegmentBuffer(segs, job)
    # The playlist is served from /<token>/, so segment URLs resolve relative
    # to it; the handler therefore sees "<token>/seg_NNNNN.ts".
    server = _MemHlsServer(playlist_text,
                           [f'{token}/{name}' for name in local_names],
                           store, token)
    url = server.start()
    store.start()
    job['store']  = store
    job['server'] = server
    sys.stderr.write(f'[native_host] streaming {len(segs)} segments via {url}\n')
    return server, store

def _normalize_output_path(path: str) -> str:
    """Absolute path that always ends in .mkv.

    The export is always Matroska (`-f matroska`), so a name typed with another
    extension ("Ard Test.mp4") would create a file whose name lies about its
    contents. Real extensions are replaced; a dot inside the title ("Mr. Robot")
    is kept and .mkv is appended instead."""
    path = os.path.abspath(os.path.expanduser((path or '').strip().strip('"')))
    root, ext = os.path.splitext(path)
    if ext and re.fullmatch(r'\.[A-Za-z0-9]{1,4}', ext):
        path = root + '.mkv'
    else:
        path += '.mkv'
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path

def _ask_save_path(suggested_path: str, title: str = 'Save video as') -> tuple:
    """Windows Save-As dialog (comdlg32.GetSaveFileNameW).

    Returns (chosen_path | None, cancelled). A user cancel is not an error:
    the export is simply dropped before any bandwidth is spent. On dialog
    failure (or a non-Windows host) it returns (None, False) and the caller
    falls back to the suggested path.
    """
    if os.name != 'nt':
        return None, False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None, False

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ('lStructSize',       wintypes.DWORD),
            ('hwndOwner',         wintypes.HWND),
            ('hInstance',         wintypes.HINSTANCE),
            ('lpstrFilter',       wintypes.LPCWSTR),
            ('lpstrCustomFilter', wintypes.LPWSTR),
            ('nMaxCustFilter',    wintypes.DWORD),
            ('nFilterIndex',      wintypes.DWORD),
            ('lpstrFile',         wintypes.LPWSTR),
            ('nMaxFile',          wintypes.DWORD),
            ('lpstrFileTitle',    wintypes.LPWSTR),
            ('nMaxFileTitle',     wintypes.DWORD),
            ('lpstrInitialDir',   wintypes.LPCWSTR),
            ('lpstrTitle',        wintypes.LPCWSTR),
            ('Flags',             wintypes.DWORD),
            ('nFileOffset',       wintypes.WORD),
            ('nFileExtension',    wintypes.WORD),
            ('lpstrDefExt',       wintypes.LPCWSTR),
            ('lCustData',         wintypes.LPARAM),
            ('lpfnHook',          ctypes.c_void_p),
            ('lpTemplateName',    wintypes.LPCWSTR),
            ('pvReserved',        ctypes.c_void_p),
            ('dwReserved',        wintypes.DWORD),
            ('FlagsEx',           wintypes.DWORD),
        ]

    OFN_OVERWRITEPROMPT = 0x00000002
    OFN_HIDEREADONLY    = 0x00000004
    OFN_NOCHANGEDIR     = 0x00000008
    OFN_PATHMUSTEXIST   = 0x00000800
    OFN_EXPLORER        = 0x00080000

    file_buf = ctypes.create_unicode_buffer(suggested_path, 32768)
    filter_buf = ctypes.create_unicode_buffer(
        'Matroska video (*.mkv)\0*.mkv\0All files (*.*)\0*.*\0\0'
    )

    ofn = OPENFILENAMEW()
    ofn.lStructSize  = ctypes.sizeof(OPENFILENAMEW)
    try:
        ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        pass
    ofn.lpstrFilter  = ctypes.cast(filter_buf, wintypes.LPCWSTR)
    ofn.nFilterIndex = 1
    ofn.lpstrFile    = ctypes.cast(file_buf, wintypes.LPWSTR)
    ofn.nMaxFile     = len(file_buf)
    ofn.lpstrTitle   = title
    ofn.lpstrDefExt  = 'mkv'
    ofn.Flags        = (OFN_EXPLORER | OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
                        | OFN_NOCHANGEDIR | OFN_HIDEREADONLY)

    ole32 = ctypes.windll.ole32
    try:
        com = ole32.CoInitializeEx(None, 0x2)      # STA, needed by the dialog
    except Exception:
        com = None
    try:
        comdlg32 = ctypes.windll.comdlg32
        comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
        comdlg32.GetSaveFileNameW.restype  = wintypes.BOOL
        if comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            path = file_buf.value.strip()
            return (path or None), False
        err = comdlg32.CommDlgExtendedError()
        if err:
            sys.stderr.write(f'[native_host] save dialog failed: 0x{err:04X}\n')
            return None, False
        return None, True                          # user pressed Cancel
    except Exception as e:
        sys.stderr.write(f'[native_host] save dialog error: {e}\n')
        return None, False
    finally:
        if com == 0:
            try: ole32.CoUninitialize()
            except Exception: pass

def _prune_mkv_jobs():
    now = time.time()
    with _mkv_jobs_lock:
        stale = [jid for jid, job in _mkv_jobs.items()
                 if job.get('finished_at') and now - job['finished_at'] > _MKV_JOB_TTL]
        for jid in stale:
            _mkv_jobs.pop(jid, None)

def _mkv_progress_snapshot(job: dict) -> dict:
    """JSON-safe progress snapshot for the 'getMkvProgress' action."""
    finished = job.get('finished_at') is not None
    rc       = job.get('returncode')
    phase    = job.get('phase', 'remuxing')
    duration = job.get('duration')
    is_live  = bool(job.get('live'))
    elapsed  = max(0.0, time.time() - job['started_at'])
    output   = job.get('output_path')

    out_time = 0.0
    size     = 0
    speed    = ''
    eta      = None
    percent  = None
    segments_done  = job.get('segments_done', 0)
    segments_total = job.get('segments_total', 0)

    if finished:
        state = 'finished' if rc == 0 else 'failed'
        phase = 'done'
        out_time = job.get('out_time', 0.0)
        try:
            size = os.path.getsize(output)
        except (OSError, TypeError):
            size = job.get('download_bytes', 0)
        percent = 100 if state == 'finished' else None

    elif phase in ('selecting', 'preparing'):
        # Waiting for the Save-As dialog: nothing is downloaded yet.
        state = 'running'

    else:
        state = 'running'
        prog  = _read_ffmpeg_progress(job['progress_path'])
        out_time = _ffmpeg_out_time(prog)
        speed_x  = _ffmpeg_speed(prog)
        try:
            size = int(prog.get('total_size') or 0)
        except (TypeError, ValueError):
            size = 0
        speed = prog.get('speed') or ''

        if duration and not is_live and duration > 0:
            ratio = max(0.0, min(1.0, out_time / duration))
            percent = max(0, min(99, int(round(ratio * 100))))
            if speed_x and speed_x > 0:
                eta = max(0.0, (duration - out_time) / speed_x)

    return {
        'success':       True,
        'state':         state,
        'phase':         phase,
        'jobId':         job['job_id'],
        'pid':           job.get('pid'),
        'outputPath':    output,
        'percent':       percent,
        'outTime':       round(out_time, 1),
        'duration':      round(duration, 1) if duration else None,
        'size':          size,
        'speed':         speed,
        'eta':           round(eta, 1) if eta is not None else None,
        'elapsed':       round(elapsed, 1),
        'live':          is_live,
        'segmentsDone':  segments_done,
        'segmentsTotal': segments_total,
        'bufferedBytes': job.get('buffered_bytes', 0),
        'downloadedBytes': job.get('download_bytes', 0),
        'error':         job.get('error'),
    }

def _stop_job_stream(job: dict):
    """Stop the segment workers and the loopback HTTP server of a job."""
    store = job.pop('store', None)
    if store is not None:
        try: store.close()
        except Exception: pass
    server = job.pop('server', None)
    if server is not None:
        try: server.stop()
        except Exception: pass

def _remove_part(job: dict):
    """Delete an unfinished .part output (never the user's chosen file)."""
    part = job.pop('part_path', None)
    if part:
        try: os.remove(part)
        except OSError: pass

def _finish_mkv_job(job: dict, rc: int, error: str | None = None):
    """Finalize a job that never reached (or never completed) ffmpeg."""
    _stop_job_stream(job)
    job['returncode'] = rc
    if error and not job.get('error'):
        job['error'] = error
    if rc == 0:
        job['error'] = None
    else:
        _remove_part(job)
    job['phase'] = 'done'
    job['finished_at'] = time.time()
    try: os.remove(job['progress_path'])
    except OSError: pass
    _prune_mkv_jobs()

def _watch_mkv(p, job: dict):
    p.wait()
    stream_error = job.get('stream_error')
    _stop_job_stream(job)
    try:
        prog = _read_ffmpeg_progress(job['progress_path'])
        job['out_time'] = _ffmpeg_out_time(prog)
    except Exception:
        pass

    rc = p.returncode
    if rc is not None and rc > 0x7FFFFFFF:   # Windows: show -2, not 4294967294
        rc -= 0x100000000

    if rc == 0:
        # ffmpeg wrote <name>.mkv.part so a failed/cancelled export never
        # leaves a half-written file under the user's chosen name.
        part = job.pop('part_path', None)
        out  = job.get('output_path')
        if part and out:
            try:
                os.replace(part, out)
            except OSError as e:
                rc = -1
                job['error'] = f'cannot save file: {e}'
    if rc != 0:
        _remove_part(job)
        if not job.get('error'):
            if stream_error == 'cancelled' or job['cancel'].is_set():
                job['error'] = 'cancelled'
            elif stream_error:
                job['error'] = f'segment download failed: {stream_error}'
            else:
                job['error'] = f'ffmpeg exited with code {rc}'

    job['returncode'] = rc
    job['phase'] = 'done'
    job['finished_at'] = time.time()          # set last: pollers key off it
    try: os.remove(job['progress_path'])
    except OSError: pass
    with _player_procs_lock:
        _player_procs.pop(p.pid, None)
    sys.stderr.write(f'[native_host] ffmpeg finished pid={p.pid} rc={rc}\n')
    _prune_mkv_jobs()

def _start_ffmpeg_remux(job: dict, input_url: str, ffmpeg_path: str):
    # -c copy: stream-copy all tracks (no re-encode). -f matroska: the output
    # is written to a *.part temp name, so the format cannot be guessed from
    # the extension. -progress <file>: machine-readable progress; stderr is
    # discarded (no .log file). The protocol whitelist lets the loopback
    # playlist reference remote AES keys / fMP4 init sections.
    out  = job.get('output_path') or job['default_output_path']
    part = out + '.part'
    job['output_path'] = out
    job['part_path']   = part
    cmd = [
        ffmpeg_path, '-hide_banner', '-loglevel', 'error', '-nostats',
        '-protocol_whitelist', 'file,crypto,data,http,https,tcp,tls',
        '-user_agent', 'Mozilla/5.0',
        '-progress', job['progress_path'],
        '-i', input_url, '-c', 'copy', '-f', 'matroska', '-y', part,
    ]
    proc = subprocess.Popen(
        cmd, shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    job['pid'] = proc.pid
    with _player_procs_lock:
        _player_procs[proc.pid] = proc
    threading.Thread(target=_watch_mkv, args=(proc, job), daemon=True).start()
    if job['cancel'].is_set():
        try: proc.terminate()
        except Exception: pass

def _run_mkv_job(job: dict, source_url: str, ffmpeg_path: str):
    # 1) Ask where to save before spending any bandwidth.
    if job.get('ask_path'):
        job['phase'] = 'selecting'
        path, cancelled = _ask_save_path(job['default_output_path'])
        if job['cancel'].is_set() or cancelled:
            _finish_mkv_job(job, -1, 'cancelled')
            return
        if path:
            job['output_path'] = _normalize_output_path(path)
        else:
            # No dialog available (headless host) → keep the suggested path.
            job['output_path'] = job['default_output_path']
    if job['cancel'].is_set():
        _finish_mkv_job(job, -1, 'cancelled')
        return

    # 2) HLS playlists are streamed through the parallel RAM buffer. Direct
    #    media files (MP4/WebM/…, e.g. the ARD Mediathek progressive links) are
    #    just remuxed by ffmpeg from the URL — no segments to pipeline.
    server = None
    if _looks_like_hls(source_url):
        job['phase'] = 'streaming'
        try:
            server, _store = _start_streaming_input(source_url, job)
        except _StreamCancelled:
            _finish_mkv_job(job, -1, 'cancelled')
            return
        except Exception as e:
            sys.stderr.write(f'[native_host] playlist setup failed: {e}\n')
    else:
        job['phase'] = 'remuxing'
        threading.Thread(target=_probe_media_duration,
                         args=(job, source_url, ffmpeg_path), daemon=True).start()

    if job['cancel'].is_set():
        _finish_mkv_job(job, -1, 'cancelled')
        return

    if server is None:
        # Live / I-frame-only / unsupported / direct file: ffmpeg reads the
        # stream itself.
        job['phase'] = 'remuxing'
        if job.get('duration') is None and _looks_like_hls(source_url):
            threading.Thread(target=_probe_hls_duration,
                             args=(job, source_url), daemon=True).start()
        input_url = source_url
    else:
        input_url = server.url

    try:
        _start_ffmpeg_remux(job, input_url, ffmpeg_path)
    except Exception as e:
        _finish_mkv_job(job, -1, f'ffmpeg start failed: {e}')

# ══════════════════════════════════════════════════════════════════════════════
#  NATIVE HOST  (async stdio loop)
# ══════════════════════════════════════════════════════════════════════════════

def _read_exact(f, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        chunk = f.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)

def _get_msg(f):
    raw = _read_exact(f, 4)
    if len(raw) < 4: return None
    n    = struct.unpack('=I', raw)[0]
    data = _read_exact(f, n)
    if len(data) < n: return None
    return json.loads(data.decode('utf-8'))

def _encode_msg(msg: dict) -> bytes:
    data = json.dumps(msg, ensure_ascii=False).encode('utf-8')
    return struct.pack('=I', len(data)) + data

_player_procs: dict      = {}
_player_procs_lock       = threading.Lock()
_hls_proxy               = None
_hls_proxy_lock          = threading.Lock()
_echo_server             = None

def _start_echo_server(port: int = 8085):
    global _echo_server
    try:
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

        class EchoHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/echo':
                    dlna_enabled = is_dlna_available()
                    response = {
                        'id': 'STP Host',
                        'dlnaAvailable': dlna_enabled
                    }
                    response_json = json.dumps(response)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(response_json)))
                    self.end_headers()
                    self.wfile.write(response_json.encode('utf-8'))
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, fmt, *args):
                pass

        _echo_server = ThreadingHTTPServer(('127.0.0.1', port), EchoHandler)
        threading.Thread(target=_echo_server.serve_forever, daemon=True).start()
        sys.stderr.write(f'[BG] Echo server started on port {port}\n')
    except Exception as e:
        sys.stderr.write(f'[BG] Echo server failed: {e}\n')

_WORKER_COUNT = 16

def native_host_loop():
    fin  = os.fdopen(0, 'rb', buffering=0, closefd=False)
    fout = os.fdopen(1, 'wb', buffering=0, closefd=False)

    _reply_q: queue.Queue = queue.Queue()

    def _writer():
        while True:
            item = _reply_q.get()
            if item is None:
                break
            try:
                fout.write(item)
                fout.flush()
            except Exception as e:
                sys.stderr.write(f'[native_host] write error: {e}\n')

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    def _reply(result: dict, seq):
        if seq is not None:
            result = {**result, 'seq': seq}
        _reply_q.put(_encode_msg(result))

    def _handle(msg: dict):
        seq    = msg.get('seq')
        action = msg.get('action', '')

        try:
            if action == 'openPlayer':
                player = msg.get('playerPath', '')
                url    = msg.get('streamUrl', '')
                title  = msg.get('title')
                slave_urls = msg.get('slaveUrls', [])
                if not player or not url:
                    _reply({'success': False, 'error': 'Missing playerPath or streamUrl'}, seq)
                    return
                
                sub_arg = None
                sub_dir = None
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
                        srt_content = _convert_ttml_to_srt(sub_data)
                        if srt_content:
                            sub_dir = tempfile.mkdtemp(prefix='stp_sub_')
                            temp_file_path = os.path.join(sub_dir, _ttml_lang_code(sub_data) + '.srt')
                            with open(temp_file_path, 'w', encoding='utf-8') as tf:
                                tf.write(srt_content)
                            sub_arg = temp_file_path
                            sys.stderr.write(f'[native_host] Successfully converted & normalized TTML to SRT: {sub_arg}\n')

                cmd = [player, url]
                if title:
                    if 'vlc.exe'  in player.lower(): cmd.append(f'--meta-title={title}')
                    elif 'mpc-hc' in player.lower(): cmd.extend(['/title', title])
                if 'vlc.exe' in player.lower():
                    cmd.append('--fullscreen')
                    if sub_arg:
                        cmd.append(f'--sub-file={sub_arg}')
                        sys.stderr.write(f'[native_host] Attached SRT file via --sub-file: {sub_arg}\n')

                proc = subprocess.Popen(cmd, shell=False)
                pid  = proc.pid
                with _player_procs_lock:
                    _player_procs[pid] = proc
                def _watch_proc(p, k, d):
                    p.wait()
                    with _player_procs_lock:
                        _player_procs.pop(k, None)
                    if d:
                        shutil.rmtree(d, ignore_errors=True)
                threading.Thread(target=_watch_proc, args=(proc, pid, sub_dir), daemon=True).start()
                _reply({'success': True, 'pid': pid}, seq)

            elif action == 'stopPlayer':
                pid = msg.get('pid')
                with _player_procs_lock:
                    proc = _player_procs.pop(pid, None) if pid else None
                if proc and proc.poll() is None:
                    proc.terminate()
                    sys.stderr.write(f'[native_host] stopped pid={pid}\n')
                _reply({'success': True}, seq)

            elif action == 'getPlayerState':
                pid = msg.get('pid')
                with _player_procs_lock:
                    proc = _player_procs.get(pid) if pid else None
                if proc is None:
                    _reply({'state': 'stopped'}, seq)
                else:
                    _reply({'state': 'running' if proc.poll() is None else 'stopped'}, seq)

            elif action == 'downloadMkv':
                # Remux any video source → MKV via ffmpeg (stream-copy, no re-encode).
                # ffmpeg searched in: PATH → EXE directory → common install locations.
                # The job thread asks the user where to save, then:
                #   • .m3u8 playlist → segments stream through RAM (10 parallel
                #     downloads) into ffmpeg;
                #   • direct media file (.mp4/.webm, e.g. ARD Mediathek's
                #     progressive links) → ffmpeg reads the URL itself.
                # Returns { success, jobId, outputPath, needsSavePath } at once;
                # progress is polled via the 'getMkvProgress' action.
                #
                # Optional 'savePath' skips the Save-As dialog (used by tests and
                # by callers that already know the destination).
                playlist_url = (msg.get('playlistUrl') or msg.get('mediaUrl')
                                or msg.get('url') or '')
                dl_title     = msg.get('title', 'video')
                quality      = msg.get('quality', '')
                save_path    = msg.get('savePath') or ''
                if not playlist_url:
                    _reply({'success': False, 'error': 'Missing playlistUrl'}, seq)
                    return

                # Locate ffmpeg binary
                ffmpeg_path = shutil.which('ffmpeg')
                if not ffmpeg_path:
                    _exe_dir = os.path.dirname(os.path.abspath(
                        sys.executable if getattr(sys, 'frozen', False) else __file__
                    ))
                    for cand in [
                        os.path.join(_exe_dir, 'ffmpeg.exe'),
                        r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
                        r'C:\ffmpeg\bin\ffmpeg.exe',
                        r'C:\ProgramData\chocolatey\bin\ffmpeg.exe',
                        r'C:\ProgramData\scoop\shims\ffmpeg.exe',
                    ]:
                        if os.path.isfile(cand):
                            ffmpeg_path = cand
                            break

                if not ffmpeg_path:
                    _reply({'success': False, 'error': 'ffmpeg not found — install ffmpeg and add it to PATH'}, seq)
                    return

                # Suggested filename (strip chars forbidden in Windows paths)
                safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', dl_title).strip('_') or 'video'
                if quality:
                    safe_title += f'_{quality}'
                downloads_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
                os.makedirs(downloads_dir, exist_ok=True)
                default_path = os.path.join(downloads_dir, f'{safe_title}.mkv')
                output_path  = _normalize_output_path(save_path) if save_path else None

                # ffmpeg appends progress blocks to this file; the EXE parses
                # them on demand. Removed when the job ends.
                fd, progress_path = tempfile.mkstemp(prefix='stp_ffmpeg_', suffix='.progress')
                os.close(fd)

                _prune_mkv_jobs()
                job = {
                    'job_id':          _next_mkv_job_id(),
                    'pid':             None,
                    'output_path':     output_path,
                    'default_output_path': default_path,
                    'part_path':       None,
                    'ask_path':        not save_path,
                    'progress_path':   progress_path,
                    'started_at':      time.time(),
                    'finished_at':     None,
                    'returncode':      None,
                    'phase':           'preparing',
                    'duration':        None,
                    'live':            False,
                    'out_time':        0.0,
                    'segments_total':  0,
                    'segments_done':   0,
                    'download_bytes':  0,
                    'buffered_bytes':  0,
                    'store':           None,
                    'server':          None,
                    'stream_error':    None,
                    'stat_lock':       threading.Lock(),
                    'cancel':          threading.Event(),
                    'error':           None,
                }
                with _mkv_jobs_lock:
                    _mkv_jobs[job['job_id']] = job

                sys.stderr.write(
                    f'[native_host] downloadMkv #{job["job_id"]}: '
                    f'{playlist_url} → {output_path or default_path}\n'
                )
                threading.Thread(
                    target=_run_mkv_job,
                    args=(job, playlist_url, ffmpeg_path),
                    daemon=True,
                ).start()
                _reply({'success': True, 'jobId': job['job_id'], 'pid': None,
                        'outputPath': output_path,
                        'suggestedOutputPath': default_path,
                        'needsSavePath': not save_path}, seq)

            elif action == 'getMkvProgress':
                # Poll one remux job.
                # { jobId | pid } → { success, state, phase, percent, outTime,
                #     duration, size, speed, eta, elapsed, live, segmentsDone,
                #     segmentsTotal, outputPath, error }
                jid = _as_int(msg.get('jobId'))
                pid = _as_int(msg.get('pid'))
                job = _find_mkv_job(jid, pid, newest_if_none=True)
                if job is None:
                    _reply({'success': False, 'state': 'unknown', 'error': 'Unknown download'}, seq)
                else:
                    _reply(_mkv_progress_snapshot(job), seq)

            elif action == 'stopMkv':
                # Cancel a remux job: aborts prefetch and/or terminates ffmpeg.
                jid = _as_int(msg.get('jobId'))
                pid = _as_int(msg.get('pid'))
                job = _find_mkv_job(jid, pid)
                if job is not None:
                    job['cancel'].set()
                    with _player_procs_lock:
                        proc = _player_procs.get(job.get('pid'))
                    if proc and proc.poll() is None:
                        proc.terminate()
                    _stop_job_stream(job)        # free the RAM buffer right away
                    sys.stderr.write(f'[native_host] stopMkv #{job["job_id"]}\n')
                _reply({'success': True}, seq)

            elif action == 'dlnaCast':
                cast_url = msg.get('castUrl', '')
                title    = msg.get('title', 'Video')
                lan_ip   = cfg.get('computer_ip', '')
                if lan_ip and lan_ip not in ('127.0.0.1', 'localhost', '0.0.0.0'):
                    cast_url = cast_url.replace('127.0.0.1', lan_ip).replace('localhost', lan_ip)
                res = dlna_cast(cast_url, title)
                if res.get('success'):
                    res['castUrl'] = cast_url
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
                            _reply({'success': False, 'error': 'Proxy failed to start — port 8085 busy?'}, seq)
                            return
                    _hls_proxy.new_session(url)
                _rebuild_tray_menu()
                _reply({'success': True, 'proxyUrl': _hls_proxy.url_for(url)}, seq)

            elif action == 'stopHLSProxy':
                if _hls_proxy:
                    _hls_proxy.stop()
                _rebuild_tray_menu()
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
            if task is None:
                break
            try:
                task()
            except Exception:
                pass
    for _ in range(_WORKER_COUNT):
        threading.Thread(target=_worker, daemon=True).start()

    while True:
        try:
            msg = _get_msg(fin)
            if not msg:
                break
            _work_q.put(lambda m=msg: _handle(m))
        except Exception as e:
            sys.stderr.write(f'[native_host] reader error: {e}\n')
            break

    for _ in range(_WORKER_COUNT):
        _work_q.put(None)
    _reply_q.put(None)

# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS WINDOW
# ══════════════════════════════════════════════════════════════════════════════

def open_settings(on_save_callback=None):
    import tkinter as tk
    from tkinter import messagebox
    import glob

    win = tk.Tk()
    win.title(f"{APP_NAME} — Settings")
    win.resizable(False, False)
    win.attributes('-topmost', True)
    try: win.iconbitmap(_resource_path('icon.ico'))
    except Exception: pass

    FONT   = ('Segoe UI', 10)
    FONT_B = ('Segoe UI', 10, 'bold')
    FONT_H = ('Segoe UI', 11, 'bold')
    BG     = '#f5f5f5'
    ACCENT = '#4a90e2'
    win.configure(bg=BG)

    def section(parent, text):
        f = tk.Frame(parent, bg=BG); f.pack(fill='x', padx=12, pady=(10,2))
        tk.Label(f, text=text, font=FONT_H, bg=BG, fg=ACCENT).pack(anchor='w')
        tk.Frame(parent, height=1, bg=ACCENT).pack(fill='x', padx=12)

    def row(parent, label, var, width=22):
        f = tk.Frame(parent, bg=BG); f.pack(fill='x', padx=12, pady=4)
        tk.Label(f, text=label, font=FONT, bg=BG, width=22, anchor='w').pack(side='left')
        tk.Entry(f, textvariable=var, font=FONT, width=width, relief='solid', bd=1).pack(side='left')

    def row_pair(parent, label, var1, var2, width1=22, width2=6):
        f = tk.Frame(parent, bg=BG); f.pack(fill='x', padx=12, pady=4)
        tk.Label(f, text=label, font=FONT, bg=BG, width=22, anchor='w').pack(side='left')
        tk.Entry(f, textvariable=var1, font=FONT, width=width1, relief='solid', bd=1).pack(side='left')
        tk.Entry(f, textvariable=var2, font=FONT, width=width2, relief='solid', bd=1).pack(side='left', padx=(4,0))

    def row_checkbox(parent, label, bool_var, hint=''):
        f = tk.Frame(parent, bg=BG); f.pack(fill='x', padx=12, pady=4)
        tk.Label(f, text=label, font=FONT, bg=BG, width=22, anchor='w').pack(side='left')
        tk.Checkbutton(f, variable=bool_var, bg=BG, activebackground=BG,
                       relief='flat', cursor='hand2').pack(side='left')
        if hint:
            tk.Label(f, text=hint, font=('Segoe UI', 9), bg=BG, fg='#999').pack(side='left', padx=(4,0))

    v_ip        = tk.StringVar(value=c('computer_ip'))
    v_dlna_host = tk.StringVar(value=c('dlna_host'))
    v_dlna_port = tk.StringVar(value=str(c('dlna_port')))

    mod_vars   = []
    _seen_keys: set = set()

    def _absorb_fields(mod):
        for field in getattr(mod, 'SETTINGS_FIELDS', []):
            if field['key'] in _seen_keys: continue
            _seen_keys.add(field['key'])
            if field.get('widget') == 'checkbox':
                true_val = field.get('true_val', 'true')
                cur_val  = cfg.get(field['key'], field.get('default', False))
                checked  = (cur_val == true_val) if isinstance(cur_val, str) else bool(cur_val)
                var = tk.BooleanVar(value=checked)
            else:
                var = tk.StringVar(value=str(cfg.get(field['key'], field.get('default', ''))))
            mod_vars.append({**field, 'var': var})

    for mod in list(_modules.values()):
        if mod: _absorb_fields(mod)
    for path in sorted(glob.glob(os.path.join(_BASE_DIR, '*_module.py'))):
        name = os.path.basename(path)[:-3]
        try:
            _absorb_fields(importlib.import_module(name))
        except Exception:
            pass

    body = tk.Frame(win, bg=BG); body.pack(fill='both', expand=True, pady=(8,0))
    section(body, "Network")
    row(body, "Computer IP (LAN)", v_ip)
    row_pair(body, "DLNA IP", v_dlna_host, v_dlna_port)
    section(body, "Servers")
    for f in mod_vars:
        if f.get('widget') == 'checkbox':
            row_checkbox(body, f['label'], f['var'], hint=f.get('hint',''))
        else:
            row(body, f['label'], f['var'], width=f.get('width', 8))

    btn_frm    = tk.Frame(win, bg=BG); btn_frm.pack(fill='x', padx=12, pady=12)
    status_var = tk.StringVar()
    tk.Label(btn_frm, textvariable=status_var, font=('Segoe UI',9), bg=BG, fg='#666').pack(side='left')

    def on_save():
        try:
            old_ports = tuple(cfg.get(f['key']) for f in mod_vars)
            cfg['computer_ip'] = v_ip.get().strip()
            cfg['dlna_host']   = v_dlna_host.get().strip()
            dlna_port_str = v_dlna_port.get().strip()
            cfg['dlna_port']   = int(dlna_port_str) if dlna_port_str else ''
            for f in mod_vars:
                if f.get('widget') == 'checkbox':
                    cfg[f['key']] = f.get('true_val', 'true') if f['var'].get() else f.get('false_val', 'false')
                else:
                    cast = f.get('cast', int)
                    cfg[f['key']] = cast(f['var'].get().strip()) if cast is str else cast(f['var'].get())
            save_config()
            new_ports = tuple(cfg.get(f['key']) for f in mod_vars if f.get('widget') != 'checkbox')
            if on_save_callback: on_save_callback()
            if old_ports != new_ports:
                status_var.set("✓ Saved. Restarting...")
                win.update()
                win.after(800, lambda: (win.destroy(), _silent_restart()))
            else:
                win.destroy()
        except ValueError:
            messagebox.showerror("Error", "Ports must be integers.", parent=win)
        except Exception:
            messagebox.showerror("Save error", traceback.format_exc(), parent=win)

    def on_detect():
        ip = _detect_lan_ip(); v_ip.set(ip); status_var.set(f"IP detected: {ip}")

    def on_find_tv():
        status_var.set("Searching for TV...")
        def _run():
            try:
                ok, info = discover_dlna()
                if ok:
                    with _cfg_lock:
                        cfg['dlna_last_discovered'] = time.time()
                    save_config()
                status_var.set(f"✓ TV: {info}" if ok else f"✗ {info}")
            except Exception as e:
                status_var.set(f"✗ {e}")
        threading.Thread(target=_run, daemon=True).start()

    tk.Button(btn_frm, text="Find TV",  font=FONT,   relief='flat', bg='#e8e8e8', cursor='hand2', padx=10, command=on_find_tv).pack(side='right', padx=(4,0))
    tk.Button(btn_frm, text="Auto IP",  font=FONT,   relief='flat', bg='#e8e8e8', cursor='hand2', padx=10, command=on_detect).pack(side='right', padx=(4,0))
    tk.Button(btn_frm, text="Save",     font=FONT_B, relief='flat', bg=ACCENT, fg='white', cursor='hand2', padx=14, command=on_save).pack(side='right')

    win.update_idletasks()
    w, h   = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")
    win.mainloop()

# ══════════════════════════════════════════════════════════════════════════════
#  TRAY
# ══════════════════════════════════════════════════════════════════════════════

_mutex_handle         = None
_tray_icon            = None
_brief_fn             = lambda msg, secs=5: None
_tray_rebuild_pending = False
_tray_rebuild_lock    = threading.Lock()

def _rebuild_tray_menu():
    global _tray_rebuild_pending
    with _tray_rebuild_lock:
        if _tray_rebuild_pending:
            return
        _tray_rebuild_pending = True
    def _do():
        global _tray_rebuild_pending
        try:
            import pystray
            if _tray_icon is None: return
            _tray_icon.menu = _build_menu(pystray)
            _tray_icon.update_menu()
        finally:
            with _tray_rebuild_lock:
                _tray_rebuild_pending = False
    threading.Thread(target=_do, daemon=True).start()

def _build_menu(pystray):
    def ff_visible(item):
        return not cfg.get('firefox_registered', False)

    def _player_status(item):
        with _player_procs_lock:
            procs = list(_player_procs.values())
        player_on = any(p.poll() is None for p in procs)
        return '● Player  [playing]' if player_on else '○ Player  (idle)'

    def _proxy_status(item):
        if _hls_proxy and _hls_proxy.is_running():
            return '● Proxy  [active]'
        return '○ Proxy  (idle)'

    mod_items = []
    for mod in list(_modules.values()):
        if mod and mod.is_active() and hasattr(mod, 'tray_label'):
            def _make_lbl(m):
                def lbl(item): return m.tray_label()
                return lbl
            mod_items.append(pystray.MenuItem(_make_lbl(mod), None, enabled=False))

    return pystray.Menu(
        pystray.MenuItem(f"{APP_NAME} v{APP_VERSION}", None, enabled=False, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_player_status, None, enabled=False),
        pystray.MenuItem(_proxy_status,  None, enabled=False),
        *mod_items,
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings...",          _on_settings_action),
        pystray.MenuItem("Register in Firefox",  _on_register_action, visible=ff_visible),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit",                 _on_quit_action),
    )

def run_tray():
    global _tray_icon, _brief_fn
    import pystray
    from PIL import Image, ImageDraw

    def _load_ico(path: str):
        try:
            return Image.open(path).convert('RGBA')
        except Exception as e:
            sys.stderr.write(f'[tray] icon load failed: {e}\n')
            raise

    ICO_RUN = _load_ico(_resource_path('icon.ico'))

    def _brief(msg, secs=5):
        if _tray_icon is None: return
        _tray_icon.title = msg
        def _r():
            time.sleep(secs)
            if _tray_icon:
                _tray_icon.title = f"{APP_NAME} v{APP_VERSION}"
        threading.Thread(target=_r, daemon=True).start()

    _brief_fn = _brief

    _tray_icon = pystray.Icon(
        name  = 'stream-to-player',
        icon  = ICO_RUN,
        title = f"{APP_NAME} v{APP_VERSION}",
        menu  = _build_menu(pystray),
    )
    _tray_icon.run()

# ══════════════════════════════════════════════════════════════════════════════
#  SINGLE INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

def _acquire_mutex() -> bool:
    global _mutex_handle
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, True, f"Global\\{APP_NAME}")
        if ctypes.windll.kernel32.GetLastError() == 183:
            return False
        _mutex_handle = h
        return True
    except Exception:
        return True

def _tray_already_running() -> bool:
    try:
        import ctypes
        mutex = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, f"Global\\{APP_NAME}")
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)
            return True
        return False
    except Exception:
        return False

# ══════════════════════════════════════════════════════════════════════════════
#  TRAY ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _on_settings_action(icon, item):
    threading.Thread(
        target=lambda: open_settings(on_save_callback=lambda: _brief_fn("✓ Saved")),
        daemon=True
    ).start()

def _on_register_action(icon, item):
    ok, info = register_native_host()
    if ok:
        with _cfg_lock:
            cfg['firefox_registered'] = True
        save_config()
        _brief_fn("✓ Firefox: registered successfully")
        icon.update_menu()
    else:
        _brief_fn(f"✗ Error: {info[:70]}")

def _kill_all_copies():
    try:
        if getattr(sys, 'frozen', False):
            exe_name = os.path.basename(sys.executable)
            subprocess.Popen(
                ['taskkill', '/F', '/IM', exe_name],
                creationflags=0x08000000,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            our_script = os.path.abspath(__file__)
            try:
                import ctypes
                TH32CS_SNAPPROCESS = 0x00000002
                class PROCESSENTRY32(ctypes.Structure):
                    _fields_ = [
                        ('dwSize',              ctypes.c_ulong),
                        ('cntUsage',            ctypes.c_ulong),
                        ('th32ProcessID',       ctypes.c_ulong),
                        ('th32DefaultHeapID',   ctypes.POINTER(ctypes.c_ulong)),
                        ('th32ModuleID',        ctypes.c_ulong),
                        ('cntThreads',          ctypes.c_ulong),
                        ('th32ParentProcessID', ctypes.c_ulong),
                        ('pcPriClassBase',      ctypes.c_long),
                        ('dwFlags',             ctypes.c_ulong),
                        ('szExeFile',           ctypes.c_char * 260),
                    ]
                current_pid = os.getpid()
                snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
                entry = PROCESSENTRY32(); entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
                pids_to_kill = []
                if ctypes.windll.kernel32.Process32First(snap, ctypes.byref(entry)):
                    while True:
                        exe = entry.szExeFile.decode('utf-8', errors='replace').lower()
                        if 'python' in exe and entry.th32ProcessID != current_pid:
                            pids_to_kill.append(entry.th32ProcessID)
                        if not ctypes.windll.kernel32.Process32Next(snap, ctypes.byref(entry)):
                            break
                ctypes.windll.kernel32.CloseHandle(snap)
                for pid in pids_to_kill:
                    try:
                        result = subprocess.run(
                            ['wmic', 'process', 'where', f'ProcessId={pid}', 'get', 'CommandLine', '/value'],
                            capture_output=True, text=True, timeout=2, creationflags=0x08000000,
                        )
                        if our_script.lower() in result.stdout.lower() or \
                           os.path.basename(__file__).lower() in result.stdout.lower():
                            h = ctypes.windll.kernel32.OpenProcess(1, False, pid)
                            if h:
                                ctypes.windll.kernel32.TerminateProcess(h, 0)
                                ctypes.windll.kernel32.CloseHandle(h)
                    except Exception:
                        pass
            except Exception:
                import signal
                subprocess.Popen(['pkill', '-f', os.path.basename(__file__)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    finally:
        os._exit(0)

def _on_quit_action(icon, item):
    _sleep_block(False)
    icon.stop()
    _kill_all_copies()

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

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
            return ctypes.windll.kernel32.GetFileType(
                ctypes.windll.kernel32.GetStdHandle(-10)
            ) == 3
        except Exception:
            return (sys.stdin is not None) and (not sys.stdin.isatty())

    is_native = _is_pipe()

    if is_native:
        if _tray_already_running():
            native_host_loop()
        else:
            if not _acquire_mutex():
                native_host_loop()
                return
            register_native_host()
            _sleep_block(True)
            _auto_discover_dlna()
            threading.Thread(target=native_host_loop, daemon=True).start()
            run_tray()
    else:
        if not _acquire_mutex():
            return
        register_native_host()
        _sleep_block(True)
        _auto_discover_dlna()
        run_tray()

if __name__ == '__main__':
    main()