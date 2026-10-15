"""Event-driven pseudo-terminal smoke test for fetchwing (offline demo mode)."""
import fcntl, os, pty, re, select, struct, subprocess, tempfile, termios, time

HERE = os.path.dirname(os.path.abspath(__file__))

def run(env_extra=None, cwd=None):
    m, s = pty.openpty()
    fcntl.ioctl(s, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 90, 0, 0))
    env = dict(os.environ)
    env.update(TERM='xterm-256color', LANG='C.UTF-8', PYTHONDONTWRITEBYTECODE='1',
               FETCHWING_FAKE='1')
    env.update(env_extra or {})
    p = subprocess.Popen(['python3', os.path.join(HERE, 'fetchwing.py')], stdin=s, stdout=s, stderr=s,
                         close_fds=True, env=env, cwd=cwd or HERE)
    os.close(s)
    return m, p

def read_out(m, window):
    out = b''; end = time.monotonic() + window
    while time.monotonic() < end:
        r, _, _ = select.select([m], [], [], .2)
        if not r: continue
        try: chunk = os.read(m, 65536)
        except OSError: break
        if not chunk: break
        out += chunk
    return out

def wait_out(m, tokens, timeout=15):
    end = time.monotonic() + timeout; out = b''
    while time.monotonic() < end:
        out += read_out(m, .3)
        plain = re.sub(rb'\x1b\[[0-9;?]*[A-Za-z]|\x1b.', b'', out)
        if all(t in plain for t in tokens): return plain
    raise AssertionError('tokens not found: %r\n%s' % (tokens, out[-1200:]))

def send(m, data, wait=.25):
    os.write(m, data); time.sleep(wait)

def clean(p, m):
    try: p.terminate(); p.wait(timeout=2)
    except Exception: pass
    try: os.close(m)
    except OSError: pass

def main():
    with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as work:
        upfile = os.path.join(work, 'ptupload.txt')
        open(upfile, 'w').write('hello from the pty test\n')
        env = {'XDG_CONFIG_HOME': cfg}
        m, p = run(env, cwd=work)
        try:
            # login screen
            wait_out(m, [b'FETCHWING', b'QR code', b'personal access token'])
            # device login: QR + user code
            send(m, b'd', .5)
            wait_out(m, [b'WDJB-MJHT', b'github.com/login/device', '\u2580'.encode()])
            # demo completes login on a keypress -> file browser
            send(m, b' ', .8)
            wait_out(m, [b'photos', b'notes.txt', b'demo/fetchwing-drive', b'big-backup.tar'])
            # select notes.txt (photos, big-backup.tar, bird-logo.png, notes.txt)
            for _ in range(3): send(m, b'\x1bOB', .15)
            send(m, b'd', .4)          # download
            wait_out(m, [b'Save'])
            send(m, b'\n', .6)         # default name
            wait_out(m, [b'Saved to notes.txt'])
            assert os.path.exists(os.path.join(work, 'notes.txt')), 'download missing'
            # upload
            send(m, b'u', .4)
            wait_out(m, [b'Local file to upload'])
            send(m, upfile.encode() + b'\n', .8)
            wait_out(m, [b'Uploaded ptupload.txt'])
            # new folder
            send(m, b'n', .4)
            wait_out(m, [b'New folder name'])
            send(m, b'newfolder\n', .6)
            wait_out(m, [b'Folder created'])
            # delete the file we uploaded: walk selection to ptupload.txt
            for _ in range(2): send(m, b'\x1bOB', .15)
            send(m, b'x', .4)
            wait_out(m, [b'type yes'])
            send(m, b'yes\n', .6)
            wait_out(m, [b'Deleted ptupload.txt'])
            send(m, b'q')
            end = time.monotonic() + 10
            while p.poll() is None and time.monotonic() < end: time.sleep(.1)
            assert p.poll() == 0, 'did not exit cleanly: %r' % p.poll()
        finally:
            clean(p, m)
    print('PTY PASS')

if __name__ == '__main__':
    main()
