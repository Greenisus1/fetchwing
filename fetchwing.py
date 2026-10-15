#!/usr/bin/env python3
"""fetchwing - a GitHub-backed cloud drive in your terminal.

Store files in a private GitHub repo: browse, upload, download, delete.
Sign in by scanning a QR code (GitHub device flow) or pasting a token.
No dependencies beyond Python 3 + curses.
"""
import curses, json, os, queue, sys, threading, time

import gh
import qr

APP = 'fetchwing'
VERSION = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      'app-version.json')))['version']
DEFAULT_REPO = 'fetchwing-drive'
LOGIN_PORT = 8917

FAKE = os.environ.get('FETCHWING_FAKE') == '1'


# ---------------------------------------------------------------- demo backend
class FakeClient:
    """Offline stand-in used by tests and the FETCHWING_FAKE=1 demo mode."""
    def __init__(self):
        self.repo = 'demo/fetchwing-drive'
        self.fs = {'': [
            {'name': 'photos', 'path': 'photos', 'type': 'dir', 'size': 0, 'sha': 'a1'},
            {'name': 'notes.txt', 'path': 'notes.txt', 'type': 'file',
             'size': 1240, 'sha': 'b2'},
            {'name': 'bird-logo.png', 'path': 'bird-logo.png', 'type': 'file',
             'size': 48213, 'sha': 'c3'},
            {'name': 'big-backup.tar', 'path': 'big-backup.tar', 'type': 'file',
             'size': 241172480, 'sha': 'd4', 'chunked': True, 'parts': 3},
        ], 'photos': [
            {'name': 'beak.jpg', 'path': 'photos/beak.jpg', 'type': 'file',
             'size': 90211, 'sha': 'e5'},
        ]}
    def whoami(self):
        return {'login': 'demo-bird'}
    def ensure_repo(self, name):
        return self.repo
    def list_dir(self, path=''):
        out = list(self.fs.get(path, []))
        out.sort(key=lambda e: (e['type'] != 'dir', e['name'].lower()))
        return out
    def read_file(self, path):
        return ('fake contents of ' + path).encode()
    def upload(self, path, content, **kw):
        parent = os.path.dirname(path)
        self.fs.setdefault(parent, [])
        self.fs[parent] = [e for e in self.fs[parent] if e['name'] != os.path.basename(path)]
        self.fs[parent].append({'name': os.path.basename(path), 'path': path,
                                'type': 'file', 'size': len(content), 'sha': 'zz'})
        return {'kind': 'plain', 'size': len(content)}
    def delete(self, entry):
        parent = os.path.dirname(entry['path'])
        self.fs[parent] = [e for e in self.fs.get(parent, [])
                           if e['name'] != entry['name']]
    def mkdir(self, path):
        parent = os.path.dirname(path)
        self.fs.setdefault(parent, []).append(
            {'name': os.path.basename(path), 'path': path, 'type': 'dir',
             'size': 0, 'sha': ''})
        self.fs.setdefault(path, [])


# ------------------------------------------------------- localhost login page
class LoginPage:
    """Tiny stdlib web page shown during device login: the same user code and
    verification link as the terminal QR, for people logging in on the Pi."""
    def __init__(self, user_code, verify_uri):
        self.user_code = user_code
        self.verify_uri = verify_uri
        self.done = False
        self.server = None
        self.thread = None
        self.port = None

    def start(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        page = self
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = ('<meta name=viewport content="width=device-width">'
                        '<body style="font-family:monospace;background:#0b1e26;color:#e8f6ff;'
                        'display:flex;min-height:90vh;align-items:center;justify-content:center">'
                        '<div style="text-align:center">'
                        '<h1>fetchwing</h1>'
                        '<p style="font-size:2em;letter-spacing:.2em">%s</p>'
                        '<p><a style="color:#5fd7ff" href="%s">%s</a></p>'
                        '<p>%s</p></div>' % (
                            page.user_code, page.verify_uri, page.verify_uri,
                            'Signed in - you can close this tab and go back to the terminal.'
                            if page.done else 'Enter this code on GitHub to connect fetchwing.'))
                data = body.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *a):
                pass
        for port in range(LOGIN_PORT, LOGIN_PORT + 10):
            try:
                self.server = HTTPServer(('127.0.0.1', port), H)
                self.port = port
                break
            except OSError:
                continue
        if self.server:
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        return self.port

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server = None


# --------------------------------------------------------------------- UI kit
C_TITLE, C_BAR, C_DIR, C_FILE, C_SEL, C_OK, C_ERR, C_DIM = range(1, 9)
QR_PAIRS = {}


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE, curses.COLOR_CYAN, -1)
    curses.init_pair(C_BAR, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_DIR, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_FILE, curses.COLOR_WHITE, -1)
    curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(C_ERR, curses.COLOR_RED, -1)
    curses.init_pair(C_DIM, curses.COLOR_BLUE, -1)
    for i, (fg, bg) in enumerate(((0, 0), (0, 7), (7, 0), (7, 7))):
        curses.init_pair(20 + i, fg, bg)
        QR_PAIRS[(fg, bg)] = 20 + i


def draw_qr(win, y, x, matrix):
    """Draw a QR code, two modules per cell via the upper-half-block glyph."""
    n = len(matrix)
    for r in range(0, n, 2):
        for c in range(n):
            top = matrix[r][c]
            bot = matrix[r + 1][c] if r + 1 < n else False
            pair = QR_PAIRS[(0 if top else 7, 0 if bot else 7)]
            try:
                win.addstr(y + r // 2, x + c, '\u2580', curses.color_pair(pair))
            except curses.error:
                pass
    return (n + 1) // 2


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%.1f ' % n + unit) if unit != 'B' else '%d B' % n
        n /= 1024.0


class App:
    def __init__(self, stdscr):
        self.s = stdscr
        self.status = ('', C_DIM)
        self.events = queue.Queue()
        if FAKE:
            self.client = FakeClient()
        else:
            saved = gh.load_token()
            self.client = gh.Client(token=saved['token'] if saved else None,
                                    login=saved.get('login') if saved else None)

    # -- helpers ----------------------------------------------------------
    def set_status(self, text, color=C_DIM):
        self.status = (text, color)

    def bar(self, text, attr):
        h, w = self.s.getmaxyx()
        self.s.addstr(0, 0, text.ljust(w - 1)[:w - 1], attr)

    def prompt(self, label):
        h, w = self.s.getmaxyx()
        curses.echo()
        self.s.addstr(h - 1, 0, (label + ' ').ljust(w - 1), curses.color_pair(C_BAR))
        self.s.refresh()
        try:
            raw = self.s.getstr(h - 1, len(label) + 1, 200)
            return raw.decode('utf-8', 'replace').strip()
        except curses.error:
            return ''
        finally:
            curses.noecho()

    # -- login ------------------------------------------------------------
    def login_screen(self):
        while True:
            self.s.erase()
            h, w = self.s.getmaxyx()
            self.bar(' FETCHWING %s - your GitHub cloud drive ' % VERSION,
                     curses.color_pair(C_BAR))
            art = ['  ___ ___ ___                ',
                   ' | __| __|_ _|  fetch files   ',
                   ' | _|| _| | |   with wings    ',
                   ' |_| |___|___|               ']
            for i, line in enumerate(art):
                self.s.addstr(2 + i, 2, line, curses.color_pair(C_TITLE) | curses.A_BOLD)
            opts = []
            if gh.CLIENT_ID or FAKE:
                opts.append(('[D]', 'sign in with a QR code (GitHub device login)'))
            opts.append(('[T]', 'paste a personal access token'))
            opts.append(('[Q]', 'quit'))
            y = 8
            if not gh.CLIENT_ID and not FAKE:
                self.s.addstr(y, 4, 'QR sign-in is not configured in this build (no OAuth client id).',
                              curses.color_pair(C_DIM))
                y += 2
            for key, label in opts:
                self.s.addstr(y, 6, key, curses.color_pair(C_TITLE) | curses.A_BOLD)
                self.s.addstr(y, 12, label, curses.color_pair(C_FILE))
                y += 2
            self.footer()
            self.s.refresh()
            ch = self.s.getch()
            if ch in (ord('q'), ord('Q')):
                return False
            if ch in (ord('t'), ord('T')):
                if self.token_login():
                    return True
            if ch in (ord('d'), ord('D')) and (gh.CLIENT_ID or FAKE):
                if self.device_login():
                    return True

    def token_login(self):
        token = self.prompt('Paste a GitHub personal access token (scope: repo):')
        if not token:
            self.set_status('No token given.', C_ERR)
            return False
        if FAKE:
            gh.save_token('fake', 'demo-bird')
            self.client = FakeClient()
            return True
        self.client = gh.Client(token=token)
        try:
            login = self.client.whoami()['login']
        except gh.GhError as e:
            self.set_status(str(e), C_ERR)
            return False
        gh.save_token(token, login)
        self.client.login = login
        return True

    def device_login(self):
        client_id = 'fake-client' if FAKE else gh.CLIENT_ID
        if FAKE:
            dc = {'user_code': 'WDJB-MJHT', 'verification_uri': gh.VERIFY_URI,
                  'interval': 5, 'expires_in': 900}
        else:
            try:
                dc = self.client.device_code(client_id)
            except gh.GhError as e:
                self.set_status(str(e), C_ERR)
                return False
        page = LoginPage(dc['user_code'], dc.get('verification_uri') or gh.VERIFY_URI)
        port = page.start()
        matrix = qr.encode(dc.get('verification_uri') or gh.VERIFY_URI)
        result = {}
        stop = threading.Event()

        def poll():
            interval = max(5, int(dc.get('interval', 5)))
            deadline = time.monotonic() + int(dc.get('expires_in', 900))
            if FAKE:
                while not stop.is_set():
                    time.sleep(0.2)   # demo: waits for the user to press a key
                return
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(interval)
                try:
                    tok = self.client.poll_token(client_id, dc['device_code'])
                except gh.GhError as e:
                    result['error'] = str(e)
                    return
                if tok:
                    result['token'] = tok
                    return
            if 'token' not in result and 'error' not in result:
                result['error'] = 'The device code expired - try again.'

        t = threading.Thread(target=poll, daemon=True)
        t.start()
        try:
            while True:
                self.s.erase()
                self.bar(' FETCHWING - scan to sign in ', curses.color_pair(C_BAR))
                used = draw_qr(self.s, 2, 4, matrix)
                y = 2
                x = len(matrix) + 8
                self.s.addstr(y + 1, x, '1. Point your phone camera at the QR code', curses.color_pair(C_FILE))
                self.s.addstr(y + 2, x, '   (or open %s)' % (dc.get('verification_uri') or gh.VERIFY_URI),
                              curses.color_pair(C_FILE))
                self.s.addstr(y + 4, x, '2. Enter this code on GitHub:', curses.color_pair(C_FILE))
                self.s.addstr(y + 5, x, dc['user_code'],
                              curses.color_pair(C_TITLE) | curses.A_BOLD)
                self.s.addstr(y + 7, x, '3. Approve, and this screen continues by itself',
                              curses.color_pair(C_FILE))
                if port:
                    self.s.addstr(y + 9, x, 'On this machine: http://127.0.0.1:%d' % port,
                                  curses.color_pair(C_DIM))
                if FAKE:
                    self.s.addstr(y + 11, x, 'demo mode: press any key to finish login',
                                  curses.color_pair(C_DIM))
                self.footer('Esc cancel')
                self.s.refresh()
                self.s.timeout(300)
                ch = self.s.getch()
                if ch == 27:
                    self.set_status('Login cancelled.')
                    return False
                if FAKE and ch != -1:
                    result['token'] = 'fake'
                    return True
                if 'error' in result:
                    self.set_status(result['error'], C_ERR)
                    return False
                if 'token' in result:
                    if not FAKE:
                        login = self.client.whoami()['login']
                        gh.save_token(result['token'], login)
                    page.done = True
                    return True
        finally:
            stop.set()
            self.s.timeout(-1)
            page.stop()

    # -- browser ----------------------------------------------------------
    def footer(self, extra=''):
        h, w = self.s.getmaxyx()
        text, color = self.status
        base = extra or 'arrows move  Enter open  Bksp up  D download  U upload  N new folder  X delete  R refresh  L logout  Q quit'
        line = (' %s  %s' % (text, base))[:w - 1]
        self.s.addstr(h - 1, 0, line.ljust(w - 1), curses.color_pair(color))

    def pick_repo(self):
        name = self.prompt('Private repo to use as your drive [%s]:' % DEFAULT_REPO)
        name = name or DEFAULT_REPO
        try:
            self.client.ensure_repo(name)
        except gh.GhError as e:
            self.set_status(str(e), C_ERR)
            return False
        return True

    def browser(self):
        path = ''
        sel = 0
        entries = []
        def refresh(clear=True):
            nonlocal entries, sel
            try:
                entries = self.client.list_dir(path)
                sel = min(sel, max(0, len(entries) - 1))
                if clear:
                    self.set_status('')
            except gh.GhError as e:
                self.set_status(str(e), C_ERR)
        refresh()
        top = 0
        while True:
            self.s.erase()
            h, w = self.s.getmaxyx()
            who = getattr(self.client, 'login', None) or 'demo-bird' if FAKE else ''
            self.bar(' FETCHWING  %s  /%s  %s' % (self.client.repo or '', path, who),
                     curses.color_pair(C_BAR))
            body = h - 2
            if not entries:
                self.s.addstr(2, 4, '(empty folder - press U to upload a file)',
                              curses.color_pair(C_DIM))
            if sel < top:
                top = sel
            if sel >= top + body:
                top = sel - body + 1
            for row, e in enumerate(entries[top:top + body]):
                y = row + 1
                attr = curses.color_pair(C_SEL) if top + row == sel else (
                    curses.color_pair(C_DIR) if e['type'] == 'dir' else curses.color_pair(C_FILE))
                tag = '/' if e['type'] == 'dir' else ('*' if e.get('chunked') else ' ')
                size = '' if e['type'] == 'dir' else human(e['size'])
                line = ' %s %-44s %10s' % (tag, e['name'][:44], size)
                try:
                    self.s.addstr(y, 1, line.ljust(w - 2)[:w - 2], attr)
                except curses.error:
                    pass
            self.footer()
            self.s.refresh()
            ch = self.s.getch()
            if ch in (curses.KEY_UP, ord('k')):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord('j')):
                sel = min(len(entries) - 1, sel + 1)
            elif ch in (ord('q'), ord('Q')):
                return True
            elif ch in (ord('l'), ord('L')):
                gh.clear_token()
                return False
            elif ch in (ord('r'), ord('R')):
                refresh()
            elif ch in (10, 13, curses.KEY_RIGHT) and entries:
                e = entries[sel]
                if e['type'] == 'dir':
                    path, sel, top = e['path'], 0, 0
                    refresh()
            elif ch in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT):
                if path:
                    path = os.path.dirname(path)
                    sel = top = 0
                    refresh()
            elif ch in (ord('d'), ord('D')) and entries:
                self.do_download(entries[sel])
            elif ch in (ord('u'), ord('U')):
                self.do_upload(path)
                refresh(clear=False)
            elif ch in (ord('n'), ord('N')):
                name = self.prompt('New folder name:')
                if name:
                    try:
                        self.client.mkdir((path + '/' + name).strip('/'))
                        self.set_status('Folder created.', C_OK)
                    except gh.GhError as e:
                        self.set_status(str(e), C_ERR)
                    refresh(clear=False)
            elif ch in (ord('x'), ord('X')) and entries:
                e = entries[sel]
                if self.prompt("Delete '%s'? type yes:" % e['name']) == 'yes':
                    try:
                        self.client.delete(e)
                        self.set_status('Deleted ' + e['name'], C_OK)
                    except gh.GhError as e2:
                        self.set_status(str(e2), C_ERR)
                    refresh(clear=False)

    def do_download(self, e):
        if e['type'] == 'dir':
            return
        dest = self.prompt('Save "%s" to local path [%s]:' % (e['name'], e['name']))
        dest = dest or e['name']
        try:
            if e.get('chunked'):
                with open(dest, 'wb') as f:
                    self.client.read_chunked(e['path'], f.write)
            else:
                with open(dest, 'wb') as f:
                    f.write(self.client.read_file(e['path']))
            self.set_status('Saved to ' + dest, C_OK)
        except (gh.GhError, OSError) as ex:
            self.set_status(str(ex), C_ERR)

    def do_upload(self, path):
        src = self.prompt('Local file to upload into /%s:' % path)
        if not src:
            return
        try:
            with open(src, 'rb') as f:
                content = f.read()
        except OSError as ex:
            self.set_status(str(ex), C_ERR)
            return
        name = os.path.basename(src.rstrip('/'))
        remote = (path + '/' + name).strip('/')
        if len(content) > gh.FILE_MAX:
            parts = (len(content) + gh.CHUNK - 1) // gh.CHUNK
            self.set_status('Over GitHub\'s 100 MB limit: splitting into %d parts...' % parts)
            self.footer()
            self.s.refresh()
        try:
            info = self.client.upload(remote, content)
            if info['kind'] == 'chunked':
                self.set_status('Uploaded in %d parts (%s).' % (info['parts'], human(info['size'])), C_OK)
            else:
                self.set_status('Uploaded %s (%s).' % (name, human(info['size'])), C_OK)
        except gh.GhError as ex:
            self.set_status(str(ex), C_ERR)

    def run(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        init_colors()
        have = (not FAKE) and gh.load_token()
        while True:
            if have:
                if FAKE or self.pick_repo():
                    if self.browser():
                        return
                    have = (not FAKE) and gh.load_token()
                    continue
                have = None
            if not self.login_screen():
                return
            have = True


def main():
    if '--version' in sys.argv:
        print(VERSION)
        return
    curses.wrapper(lambda s: App(s).run())


if __name__ == '__main__':
    main()
