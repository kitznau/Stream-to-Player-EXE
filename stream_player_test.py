# EXE/stream_player_test.py
# Stream-to-Player core module.
# v2.0.2: all 15 code-review fixes applied.
#
# v2.0.1: createCastPlaylist and createMasterPlaylist forward subtitle_tracks to http_server.
#
# v1.9 changes:
#
# [1] STP HOST ON/OFF TRAY TOGGLE
#     Tray menu now shows a clickable "● STP Host :8085" / "○ STP Host Off" item.
#     When STP Host Off:
#       • createMasterPlaylist — returns the original master URL directly (no session).
#         The player/TV fetches the CDN stream without going through our server.
#         Use when the TV/VLC can reach the CDN directly (no auth headers needed).
#       • createCastPlaylist HLS (use_mpeg_ts=False) — same: original video URL returned.
#       • createCastPlaylist TS (use_mpeg_ts=True) — always proxied via ffmpeg regardless
#         of the toggle, because TS muxing requires our server to run ffmpeg.
#     STP Host is always active; mode (direct/hls/ts) is selected per-domain in JS.
#
# [2] REMOVED name != 'ard' FILTER FROM _build_menu
#     The filter prevented site-specific modules from showing a tray_label() status line.
#     All loaded modules with is_active() + tray_label() now appear in the menu.
#
# v2.0.2 fixes (all from code-review):
#   #2  _hls_proxy race condition — protected by _hls_proxy_lock
#   #3  _player_procs dict accessed from 16 worker threads — protected by _player_procs_lock
#   #4  worker-pool shutdown: only 1 sentinel sent for 16 workers — now sends N
#   #5  cfg dict mutated concurrently — _cfg_lock (RLock) protects save + discovery writes
#   #9  hidden imports inside functions — moved to module level (re, importlib, traceback)
#   #10 _read_exact buf += chunk was O(n²) — replaced with b''.join() (O(n))
#   #12 open_settings silently swallowed errors — uses top-level traceback
#   #13 _kill_all_copies leaked OpenProcess handles — CloseHandle added

import sys, os, json, struct, subprocess, threading, time, queue, importlib, traceback
import socket, re, urllib.request, urllib.error, io
from urllib.parse import urlparse, quote
import xml.etree.ElementTree as ET


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
    """
    Resolve path to a bundled resource file.

    PyInstaller one-file mode extracts datas into sys._MEIPASS (a temp dir),
    NOT alongside sys.executable. So icon.ico is at:
      sys._MEIPASS/icon.ico   — in frozen (one-file) build
      _BASE_DIR/icon.ico      — in dev mode (running as .py)
      _BASE_DIR/icon.ico      — in frozen one-dir build (also works via _MEIPASS)

    Check _MEIPASS first, then fall back to _BASE_DIR so both build modes work.
    """
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

# Fix #5: RLock guards concurrent cfg mutations from discovery thread,
# pystray callbacks, and worker threads all writing cfg simultaneously.
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
    """Return VLC executable path from Windows Registry or common install dirs."""
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
    # Fix #5: hold lock while writing so discovery thread can't corrupt mid-save
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
            # Fix #5: guard concurrent cfg writes from discovery thread
            with _cfg_lock:
                cfg['dlna_control_url']   = ctrl_url
                cfg['dlna_friendly_name'] = fname
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
                return True, fname or 'Device found'
            except Exception:
                continue
    found_n = len(locations)
    hint = (f' ({found_n} UPnP device(s) found but none had AVTransport)' if found_n
            else ' (no UPnP devices found — check network/firewall)')
    return False, f'DLNA MediaRenderer not found{hint}'

_DLNA_DISCOVER_INTERVAL = 86400  # Full discovery: 24 hours
_DLNA_POLL_INTERVAL = 900  # Quick availability check: 15 minutes

# Serialise concurrent dlnaCast / dlnaStop SOAP calls
_dlna_soap_lock = threading.Lock()

# DLNA availability cache (updated by periodic poll)
_dlna_available = False
_dlna_available_lock = threading.Lock()

def _check_dlna_availability():
    """Quick check if DLNA TV is reachable (called every 15 minutes)."""
    global _dlna_available
    ctrl = cfg.get('dlna_control_url', '')
    if not ctrl:
        with _dlna_available_lock:
            _dlna_available = False
        return False
    
    try:
        # Quick SOAP GetTransportInfo to check if TV responds
        resp = _soap(ctrl, 'GetTransportInfo',
            '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID></u:GetTransportInfo>', timeout=3)
        
        # If we get a response, TV is available
        with _dlna_available_lock:
            _dlna_available = True
        return True
    except Exception:
        with _dlna_available_lock:
            _dlna_available = False
        return False

def _start_dlna_poller():
    """Start background thread that polls DLNA availability every 30 seconds."""
    def _poll():
        while True:
            time.sleep(_DLNA_POLL_INTERVAL)
            try:
                _check_dlna_availability()
            except Exception:
                pass
    threading.Thread(target=_poll, daemon=True).start()

def is_dlna_available():
    """Return cached DLNA availability status."""
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
    """Get DLNA TransportState (PLAYING, STOPPED, TRANSITIONING)."""
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

        # Fix #9: `import re` was here — re is already imported at module level
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
    """Check if player process is running (VLC, MPC, etc.) via wmic."""
    try:
        process_name = os.path.basename(player_path).replace('.exe', '').lower()
        sys.stderr.write(f'[PlayerState] Checking: {player_path} -> looking for: {process_name}\n')

        if not process_name:
            sys.stderr.write('[PlayerState] Empty process name\n')
            return False

        result = subprocess.run(
            ['wmic', 'process', 'where', f'name like "%{process_name}%"', 'get', 'name'],
            capture_output=True, text=True, timeout=5, creationflags=0x08000000,
        )

        sys.stderr.write(f'[PlayerState] WMIC stdout: "{result.stdout.strip()}"\n')
        sys.stderr.write(f'[PlayerState] WMIC stderr: "{result.stderr.strip()}"\n')
        sys.stderr.write(f'[PlayerState] WMIC returncode: {result.returncode}\n')

        stdout_lower = result.stdout.lower()
        is_running = process_name in stdout_lower

        sys.stderr.write(f'[PlayerState] Looking for: {process_name}, Found: {is_running}\n')
        return is_running
    except Exception as e:
        sys.stderr.write(f'[PlayerState] Error: {e}\n')
        return False

def get_player_state(player_path: str) -> dict:
    """Get player state (running/stopped)."""
    try:
        if not player_path:
            return {'state': 'unknown', 'error': 'Missing playerPath'}
        return {'state': 'running' if is_player_running(player_path) else 'stopped'}
    except Exception as e:
        return {'state': 'unknown', 'error': str(e)}

def _auto_discover_dlna():
    last = cfg.get('dlna_last_discovered', 0)
    if time.time() - last < _DLNA_DISCOVER_INTERVAL and cfg.get('dlna_control_url'):
        return
    def _run():
        try:
            ok, info = discover_dlna()
            if ok:
                # Fix #5: guard concurrent write from this daemon thread
                with _cfg_lock:
                    cfg['dlna_last_discovered'] = time.time()
                save_config()
                # Update DLNA availability cache
                _check_dlna_availability()
        except Exception:
            pass
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
            # Fix #9: traceback now imported at module level
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
#  NATIVE HOST  (async stdio loop)
#
#  Design:
#    Reader thread — reads messages from stdin, dispatches each to a worker thread.
#    Worker threads — handle one message each; push encoded reply to _reply_queue.
#    Writer thread — drains _reply_queue and writes to stdout (serialised).
#
#  Each outgoing reply preserves the 'seq' field from the incoming message so
#  the browser extension can match replies to requests out-of-order.
#
# ══════════════════════════════════════════════════════════════════════════════

def _read_exact(f, n: int) -> bytes:
    # Fix #10: original used buf += chunk which is O(n²) total allocations.
    # b''.join() allocates once → O(n).
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

_player_procs: dict      = {}   # pid -> Popen, one per tab/stream
# Fix #3: _player_procs accessed from up to 16 worker threads without synchronisation
_player_procs_lock       = threading.Lock()

_hls_proxy               = None  # HLSProxy | None — lazy-initialized on first startHLSProxy
# Fix #2: two concurrent startHLSProxy messages could both see _hls_proxy is None
# and create two HLSProxy instances, leaking the first one's port binding
_hls_proxy_lock          = threading.Lock()

# ── Echo server for availability check (Windows only) ─────────────────────────

_echo_server = None  # ThreadingHTTPServer | None

def _start_echo_server(port: int = 8085):
    """Start a minimal HTTP server that responds to /echo with JSON status."""
    global _echo_server
    try:
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

        class EchoHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/echo':
                    # Check if DLNA is available (from cached poll result)
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
                pass  # suppress logging

        _echo_server = ThreadingHTTPServer(('127.0.0.1', port), EchoHandler)
        threading.Thread(target=_echo_server.serve_forever, daemon=True).start()
        sys.stderr.write(f'[BG] Echo server started on port {port}\n')
    except Exception as e:
        sys.stderr.write(f'[BG] Echo server failed: {e}\n')

_WORKER_COUNT = 16  # named constant used for both spawn and shutdown

def native_host_loop():
    fin  = os.fdopen(0, 'rb', buffering=0, closefd=False)
    fout = os.fdopen(1, 'wb', buffering=0, closefd=False)

    _reply_q: queue.Queue = queue.Queue()

    # Writer thread: serialises all replies to stdout
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
                if not player or not url:
                    _reply({'success': False, 'error': 'Missing playerPath or streamUrl'}, seq)
                    return
                cmd = [player, url]
                if title:
                    if 'vlc.exe'  in player.lower(): cmd.append(f'--meta-title={title}')
                    elif 'mpc-hc' in player.lower(): cmd.extend(['/title', title])
                if 'vlc.exe' in player.lower():
                    cmd.append('--fullscreen')
                proc = subprocess.Popen(cmd, shell=False)
                pid  = proc.pid
                # Fix #3: guard dict write
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
                # Fix #3: guard dict pop
                with _player_procs_lock:
                    proc = _player_procs.pop(pid, None) if pid else None
                if proc and proc.poll() is None:
                    proc.terminate()
                    sys.stderr.write(f'[native_host] stopped pid={pid}\n')
                _reply({'success': True}, seq)

            elif action == 'getPlayerState':
                pid = msg.get('pid')
                # Fix #3: guard dict read
                with _player_procs_lock:
                    proc = _player_procs.get(pid) if pid else None
                if proc is None:
                    _reply({'state': 'stopped'}, seq)
                else:
                    _reply({'state': 'running' if proc.poll() is None else 'stopped'}, seq)

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
                # Fix #2: double-checked locking prevents two concurrent messages
                # from both seeing _hls_proxy is None and creating two instances
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
                # Extension pings on startup to ensure EXE is running
                _reply({'success': True, 'echo': 'STP Host'}, seq)

            elif action == 'detectVlc':
                path = _detect_vlc_path()
                _reply({'path': path}, seq)

            else:
                _reply({'success': False, 'error': f'Unknown action: {action}'}, seq)

        except Exception as e:
            _reply({'success': False, 'error': str(e)}, seq)

    # Bounded worker pool
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

    # Reader loop
    while True:
        try:
            msg = _get_msg(fin)
            if not msg:
                break
            _work_q.put(lambda m=msg: _handle(m))
        except Exception as e:
            sys.stderr.write(f'[native_host] reader error: {e}\n')
            break

    # Fix #4: original sent only 1 None for 16 workers — 15 threads hung forever.
    # Now every worker gets its own sentinel.
    for _ in range(_WORKER_COUNT):
        _work_q.put(None)
    # Signal writer to flush and exit
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
            # Fix #9: `import importlib as _il` was here — importlib imported at top
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
            # Fix #9: `import traceback as _tb` was here — traceback imported at top
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
            return  # another rebuild already queued
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
        # Fix #3: snapshot values under lock to avoid dict-size-change-during-iteration
        with _player_procs_lock:
            procs = list(_player_procs.values())
        player_on = any(p.poll() is None for p in procs)
        return '● Player  [playing]' if player_on else '○ Player  (idle)'

    def _proxy_status(item):
        if _hls_proxy and _hls_proxy.is_running():
            return '● Proxy  [active]'
        return '○ Proxy  (idle)'

    # All active modules with a tray_label
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
                            # Fix #13: OpenProcess returns a handle that must be closed.
                            # Original leaked the handle by passing it directly to
                            # TerminateProcess without ever calling CloseHandle.
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

    # Start echo server for availability check (Windows)
    _start_echo_server(8085)
    
    # Start DLNA poller to check TV availability every 15 minutes
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
            # Another instance owns the tray — this process is native host only.
            native_host_loop()
        else:
            if not _acquire_mutex():
                # Lost mutex race — another instance claimed the tray.
                native_host_loop()
                return
            # We are the tray owner.
            register_native_host()
            _sleep_block(True)
            _auto_discover_dlna()
            threading.Thread(target=native_host_loop, daemon=True).start()
            run_tray()   # blocks on main thread (pystray Win32 requires it)
    else:
        if not _acquire_mutex():
            return
        register_native_host()
        _sleep_block(True)
        _auto_discover_dlna()
        run_tray()

if __name__ == '__main__':
    main()
