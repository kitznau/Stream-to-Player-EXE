# hls_proxy.py
# HLS Reverse Proxy for DLNA — Stream-to-Player
# Version: 1.1.0
#
# Reverse-proxies HLS streams (M3U8 + segments) so DLNA TVs can play
# streams that require auth headers or aren't directly reachable from TV.
#
# Flow:
#   Browser ext  →  startHLSProxy(url)  →  native host
#   Native host  →  HLSProxy.new_session(url)  →  returns proxyUrl
#   cast-module  →  dlnaCast(proxyUrl, title)  →  TV
#   TV           →  GET /proxy?url=<master.m3u8>
#   HLSProxy     →  fetch M3U8, rewrite all segment/playlist URLs → return
#   TV           →  GET /proxy?url=<segment.ts>
#   HLSProxy     →  pipe segment bytes → TV, track stats
#
# Fixes v1.1.0:
#   #1  _speed_buf unbounded growth — trimmed inline in _track()
#   #6  _abs/_prx/_URI_RE duplicated in two classes — moved to module level
#   #7  master playlist fetched twice — _select_best_variant returns text,
#       new_session reuses it (single network request)
#   #8  _rewrite_master_for_variant was O(n²) — rewritten as single-pass O(n)
#   #11 _master_text not initialised in __init__ — fixed
#   #12 start() silently swallowed exception — now logs to stderr
#   sub  _rewrite_m3u8/_rewrite_master_for_variant: when TYPE=SUBTITLES track
#        is present, DEFAULT=YES and AUTOSELECT=YES are set so HLS-compliant
#        players enable subtitles automatically. If no subtitle tracks exist
#        this code path is never triggered.

import re
import sys
import time
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, quote, unquote, parse_qs
import urllib.request
import urllib.error


# ── Module-level helpers ───────────────────────────────────────────────────────
# Fix #6: previously duplicated verbatim in both _ProxyHandler and HLSProxy.

_URI_RE            = re.compile(r'URI="([^"]+)"')
_SUB_DEFAULT_RE    = re.compile(r'\bDEFAULT=\w+')
_SUB_AUTOSELECT_RE = re.compile(r'\bAUTOSELECT=\w+')


def _abs(url: str, base: str) -> str:
    """Resolve relative URL against base, return absolute URL."""
    if url.startswith(('http://', 'https://')):
        return url
    p = urlparse(base)
    if url.startswith('//'):
        return f'{p.scheme}:{url}'
    if url.startswith('/'):
        return f'{p.scheme}://{p.netloc}{url}'
    return base.rsplit('/', 1)[0] + '/' + url


# ── Request Handler ────────────────────────────────────────────────────────────

class _ProxyHandler(BaseHTTPRequestHandler):
    proxy: 'HLSProxy'   # injected via type() at server creation

    def log_message(self, fmt, *args):
        pass  # suppress default stdout access log

    # ── entry ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != '/proxy':
            self._plain(404, b'Not found')
            return

        params = parse_qs(parsed.query, keep_blank_values=True)
        raw = params.get('url', [None])[0]
        if not raw:
            self._plain(400, b'Missing url param')
            return

        url = unquote(raw)
        p = self.proxy
        with p._lock:
            p._stats['active']   += 1
            p._stats['requests'] += 1
        try:
            self._serve(url)
        except Exception:
            try:
                self.send_error(502, 'Proxy error')
            except Exception:
                pass
        finally:
            with p._lock:
                p._stats['active'] -= 1

    # ── helpers ────────────────────────────────────────────────────────────────

    def _plain(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _open(self, url: str):
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        req.add_header('Accept-Encoding', 'identity')   # avoid gzip — we rewrite text
        session = self.proxy._session
        if session:
            for k, v in (session.get('headers') or {}).items():
                req.add_header(k, v)
        return urllib.request.urlopen(req, timeout=20)

    def _prx(self, url: str) -> str:
        """Fix #6: delegate to HLSProxy._prx — single source of truth."""
        return self.proxy._prx(url)

    # ── M3U8 rewriting ─────────────────────────────────────────────────────────

    def _rewrite_m3u8(self, text: str, base: str) -> str:
        """
        Rewrite all URLs in M3U8 playlist to go through this proxy.
        Handles:
          - Segment lines (non-comment, non-empty)
          - Child playlist lines (#EXT-X-STREAM-INF targets)
          - URI= attributes (#EXT-X-KEY, #EXT-X-MAP, #EXT-X-MEDIA, ...)
          - Subtitle tracks (TYPE=SUBTITLES): also forces DEFAULT=YES,
            AUTOSELECT=YES so compliant players show subs automatically.
            If no subtitle tracks are present the rewrite is not applied.
        """
        out = []
        for line in text.splitlines():
            s = line.strip()

            if s.startswith('#EXT-X-MEDIA:') and 'URI=' in s:
                # Rewrite URI through proxy
                line = _URI_RE.sub(
                    lambda m: f'URI="{self._prx(_abs(m.group(1), base))}"',
                    line,
                )
                # Subtitle track present → force on DEFAULT + AUTOSELECT
                if 'TYPE=SUBTITLES' in s:
                    if 'DEFAULT=' in line:
                        line = _SUB_DEFAULT_RE.sub('DEFAULT=YES', line)
                    else:
                        line += ',DEFAULT=YES'
                    if 'AUTOSELECT=' in line:
                        line = _SUB_AUTOSELECT_RE.sub('AUTOSELECT=YES', line)
                    else:
                        line += ',AUTOSELECT=YES'
                out.append(line)
                continue

            # Other tags with URI= (#EXT-X-KEY, #EXT-X-MAP, …)
            if s.startswith('#') and 'URI=' in s:
                line = _URI_RE.sub(
                    lambda m: f'URI="{self._prx(_abs(m.group(1), base))}"',
                    line,
                )
                out.append(line)
                continue

            # Non-comment, non-empty → URL line (segment or child playlist)
            if s and not s.startswith('#'):
                out.append(self._prx(_abs(s, base)))
                continue

            out.append(line)
        return '\n'.join(out)

    # ── serving ────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_m3u8(url: str, ctype: str) -> bool:
        path = url.split('?')[0].lower()
        return (path.endswith('.m3u8') or path.endswith('.m3u')
                or 'mpegurl' in ctype.lower())

    def _serve(self, url: str):
        """
        Serve the requested URL.
        If requesting the master playlist and a best variant was auto-selected,
        return rewritten master with only that variant (audio/subtitle tracks preserved).
        """
        p = self.proxy
        with p._lock:
            session_url      = p._session.get('url') if p._session else None
            selected_variant = p._selected_variant
            master_text      = p._master_text

        if selected_variant and master_text and session_url and url == session_url:
            try:
                rewritten = p._rewrite_master_for_variant(master_text, session_url, selected_variant)
                body = rewritten.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-cache, no-store')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(body)
                self._track(len(body))
            except Exception as e:
                self._plain(502, f'Rewrite error: {e}'.encode('utf-8'))
            return

        try:
            with self._open(url) as resp:
                ctype = resp.headers.get('Content-Type', 'application/octet-stream')
                if self._is_m3u8(url, ctype):
                    self._serve_m3u8(resp, url)
                else:
                    self._serve_stream(resp, ctype)
        except urllib.error.HTTPError as e:
            self.send_error(e.code, f'Upstream HTTP {e.code}')
        except urllib.error.URLError as e:
            self.send_error(502, f'Upstream unreachable: {e.reason}')
        except Exception as e:
            self.send_error(502, f'Proxy error: {e}')

    def _serve_m3u8(self, resp, url: str):
        raw  = resp.read()
        text = raw.decode('utf-8', errors='replace')
        body = self._rewrite_m3u8(text, url).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/vnd.apple.mpegurl; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)
        self._track(len(body))

    def _serve_stream(self, resp, ctype: str):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        cl = resp.headers.get('Content-Length')
        if cl:
            self.send_header('Content-Length', cl)
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        CHUNK = 1 << 16  # 64 KB
        while True:
            buf = resp.read(CHUNK)
            if not buf:
                break
            try:
                self.wfile.write(buf)
            except (BrokenPipeError, ConnectionResetError):
                break  # TV disconnected mid-segment — not an error
            self._track(len(buf))

    def _track(self, n: int):
        p   = self.proxy
        now = time.monotonic()
        with p._lock:
            p._stats['bytes_sent'] += n
            p._speed_buf.append((now, n))
            # Fix #1: trim expired entries inline so _speed_buf stays bounded.
            # Previously only get_stats() trimmed — if stats weren't polled
            # during a long stream the buffer grew without limit.
            cutoff = now - p._SPEED_WIN
            while p._speed_buf and p._speed_buf[0][0] < cutoff:
                p._speed_buf.pop(0)


# ── HLSProxy ──────────────────────────────────────────────────────────────────

class HLSProxy:
    """
    Thread-safe HLS reverse proxy for DLNA casting.

    Lifecycle:
        proxy = HLSProxy('192.168.1.100', port=8085)
        proxy.start()                              # start HTTP server once
        proxy.new_session(url)                     # reset stats per cast
        cast_url = proxy.url_for(url)              # proxy URL to give DLNA
        stats = proxy.get_stats()                  # live stats for overlay
        proxy.stop()                               # on dlnaStop
    """

    _SPEED_WIN        = 3.0   # sliding window for speed calc (seconds)
    _AUTO_SELECT_BEST = True  # automatically select best quality stream

    def __init__(self, computer_ip: str, port: int = 8085):
        self.computer_ip = computer_ip
        self.port        = port
        self._server: ThreadingHTTPServer | None = None
        self._lock       = threading.Lock()
        self._session: dict | None               = None
        self._stats      = {'bytes_sent': 0, 'requests': 0, 'active': 0}
        self._speed_buf: list[tuple[float, int]] = []
        self._t0: float                          = 0.0
        self._selected_variant: str | None       = None
        self._master_text: str | None            = None  # Fix #11: always initialised

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._server:
            return True
        try:
            Handler = type('_H', (_ProxyHandler,), {'proxy': self})
            self._server = ThreadingHTTPServer((self.computer_ip, self.port), Handler)
            self._server.daemon_threads = True
            threading.Thread(
                target=self._server.serve_forever,
                daemon=True, name='HLSProxy',
            ).start()
            return True
        except Exception as e:
            sys.stderr.write(f'[HLSProxy] start failed: {e}\n')  # Fix #12
            self._server = None
            return False

    def stop(self):
        srv, self._server = self._server, None
        if srv:
            try:
                srv.shutdown()
            except Exception:
                pass
        self._session = None

    def is_running(self) -> bool:
        return self._server is not None

    # ── URL helpers ────────────────────────────────────────────────────────────

    def _prx(self, url: str) -> str:
        """Wrap absolute URL with our proxy endpoint."""
        return f'http://{self.computer_ip}:{self.port}/proxy?url={quote(url, safe="")}'

    # ── session ────────────────────────────────────────────────────────────────

    def _select_best_variant(self, master_url: str) -> tuple[str | None, str | None, str | None]:
        """
        Fetch master playlist ONCE and select the best quality variant.
        Returns (best_variant_url, master_url, master_text).

        Fix #7: previously new_session() called this function (fetch #1) then
        fetched the master again to cache _master_text (fetch #2).  Now the
        text is returned here so the caller can reuse it without a second request.
        """
        try:
            req = urllib.request.Request(master_url)
            req.add_header('User-Agent', 'Mozilla/5.0')
            req.add_header('Accept-Encoding', 'identity')
            session = self._session
            if session:
                for k, v in (session.get('headers') or {}).items():
                    req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            sys.stderr.write(f'[HLSProxy] fetch master failed: {e}\n')
            return None, None, None

        # Fix #8 (variant scan): single-pass with manual index advance.
        # Original used nested for-loops → O(n²) for n variants.
        variants: list[dict] = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if s.startswith('#EXT-X-STREAM-INF:'):
                bandwidth, resolution = 0, ''
                bw_m  = re.search(r'BANDWIDTH=(\d+)', s)
                res_m = re.search(r'RESOLUTION=(\d+x\d+)', s)
                if bw_m:  bandwidth  = int(bw_m.group(1))
                if res_m: resolution = res_m.group(1)
                # advance to the next non-empty, non-comment line (URL)
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith('#')):
                    j += 1
                if j < len(lines):
                    variants.append({
                        'url':        _abs(lines[j].strip(), master_url),
                        'bandwidth':  bandwidth,
                        'resolution': resolution,
                    })
                i = j + 1
                continue
            i += 1

        if not variants:
            return None, None, text

        def _score(v: dict) -> tuple[int, int]:
            h = int(v['resolution'].split('x')[1]) if v['resolution'] else 0
            return (v['bandwidth'], h)

        best = max(variants, key=_score)
        return best['url'], master_url, text

    def _rewrite_master_for_variant(self, master_text: str, base_url: str, selected_variant_url: str) -> str:
        """
        Fix #8: single-pass O(n) rewrite of master playlist.

        - Keeps only the selected #EXT-X-STREAM-INF block (URL rewritten through proxy).
        - Rewrites URI= in all #EXT-X-MEDIA tags (audio/subtitle tracks).
        - Subtitle tracks (TYPE=SUBTITLES): also sets DEFAULT=YES, AUTOSELECT=YES.
          If no subtitle tracks are present this path is never taken.
        - Keeps all header / informational tags.

        Previously used enumerate() + inner for-loop → O(n²) for large master playlists.
        Now uses a while loop with manual index advance → O(n) overall.
        """
        lines        = master_text.splitlines()
        out          = []
        selected_abs = _abs(selected_variant_url, base_url)
        i = 0

        while i < len(lines):
            s = lines[i].strip()

            # ── Audio / subtitle / other media tracks — rewrite URI, keep all ──
            if s.startswith('#EXT-X-MEDIA:') and 'URI=' in s:
                rewritten = _URI_RE.sub(
                    lambda m: f'URI="{self._prx(_abs(m.group(1), base_url))}"',
                    lines[i],
                )
                # Subtitle track present → force DEFAULT + AUTOSELECT on
                if 'TYPE=SUBTITLES' in s:
                    if 'DEFAULT=' in rewritten:
                        rewritten = _SUB_DEFAULT_RE.sub('DEFAULT=YES', rewritten)
                    else:
                        rewritten += ',DEFAULT=YES'
                    if 'AUTOSELECT=' in rewritten:
                        rewritten = _SUB_AUTOSELECT_RE.sub('AUTOSELECT=YES', rewritten)
                    else:
                        rewritten += ',AUTOSELECT=YES'
                out.append(rewritten)
                i += 1
                continue

            # ── Variant stream block ──────────────────────────────────────────
            if s.startswith('#EXT-X-STREAM-INF:'):
                # advance to URL line in O(1) amortised (each line visited once)
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith('#')):
                    j += 1
                if j < len(lines):
                    abs_url = _abs(lines[j].strip(), base_url)
                    if abs_url == selected_abs:
                        out.append(lines[i])           # keep tag
                        out.append(self._prx(abs_url)) # URL rewritten through proxy
                # skip tag + URL line regardless of selection
                i = j + 1
                continue

            # ── Header and all other # lines (#EXTM3U, #EXT-X-VERSION, …) ──
            if s.startswith('#'):
                out.append(lines[i])
            i += 1

        return '\n'.join(out)

    def new_session(self, url: str, headers: dict | None = None):
        """
        Reset stats and record source URL for a new cast session.

        Fix #7: master playlist is now fetched only once inside
        _select_best_variant(); the returned text is reused as _master_text
        instead of making a second network request.
        """
        self._session          = {'url': url, 'headers': headers or {}}
        self._selected_variant = None
        self._master_text      = None

        if self._AUTO_SELECT_BEST:
            best_url, _, master_text = self._select_best_variant(url)
            if best_url:
                self._selected_variant = best_url
                self._master_text      = master_text   # reuse already-fetched text
            self._session['effective_url'] = url
        else:
            self._session['effective_url'] = url

        with self._lock:
            self._stats     = {'bytes_sent': 0, 'requests': 0, 'active': 0}
            self._speed_buf = []
            self._t0        = time.monotonic()

    def url_for(self, source_url: str) -> str:
        """Return the proxy URL that should be sent to the DLNA TV."""
        return self._prx(source_url)

    # ── stats ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        now = time.monotonic()
        with self._lock:
            # secondary trim for the case _track() wasn't called recently
            cutoff = now - self._SPEED_WIN
            self._speed_buf = [(t, b) for t, b in self._speed_buf if t >= cutoff]
            speed = (int(sum(b for _, b in self._speed_buf) / self._SPEED_WIN)
                     if self._speed_buf else 0)
            return {
                'bytes_sent':         self._stats['bytes_sent'],
                'requests':           self._stats['requests'],
                'active_connections': self._stats['active'],
                'speed_bps':          speed,
                'elapsed':            int(now - self._t0) if self._t0 else 0,
            }
