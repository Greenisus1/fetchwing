"""Unit tests for fetchwing: QR encoder (golden vectors + structure) and the
GitHub backend (offline, via an injected fake transport)."""
import base64, hashlib, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gh, qr
from qr_vectors import VECTORS


def matrix(rows):
    return [[c == '#' for c in row] for row in rows]


class TestQrGolden(unittest.TestCase):
    def test_vectors(self):
        for text, spec in VECTORS.items():
            got = qr.encode(text, force_mask=spec['mask'])
            want = matrix(spec['rows'])
            self.assertEqual(got, want, 'QR mismatch for %r' % text[:24])

    def test_function_patterns(self):
        m = qr.encode('STRUCTURE CHECK')
        n = len(m)
        def finder(r0, c0):
            for dr in range(7):
                for dc in range(7):
                    on = (dr in (0, 6) or dc in (0, 6)
                          or (2 <= dr <= 4 and 2 <= dc <= 4))
                    self.assertEqual(m[r0 + dr][c0 + dc], on, (r0 + dr, c0 + dc))
        finder(0, 0); finder(0, n - 7); finder(n - 7, 0)
        for i in range(8, n - 8):
            self.assertEqual(m[6][i], i % 2 == 0)
            self.assertEqual(m[i][6], i % 2 == 0)
        self.assertTrue(m[n - 8][8])  # dark module

    def test_render_text(self):
        m = qr.encode('https://github.com/login/device')
        txt = qr.render_text(m)
        lines = txt.split('\n')
        self.assertEqual(len(lines), (len(m) + 1) // 2 + 2)
        self.assertTrue(any('\u2588' in ln or '\u2580' in ln or '\u2584' in ln
                            for ln in lines))

    def test_too_long(self):
        with self.assertRaises(ValueError):
            qr.encode('x' * 214)
        qr.encode('x' * 213)  # max byte-mode payload for v10-M

    def test_version_info_present_v7plus(self):
        m7 = qr.encode('z' * 160)   # version 9
        self.assertEqual(len(m7), 53)
        m1 = qr.encode('hi')
        self.assertEqual(len(m1), 21)


class FakeHttp:
    """Scriptable transport: routes (method, path-prefix) -> handler."""
    def __init__(self):
        self.routes = []
        self.calls = []

    def add(self, method, prefix, handler):
        self.routes.append((method, prefix, handler))

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        # longest prefix wins so specific routes beat broad ones
        best = None
        for m, prefix, h in self.routes:
            if method == m and url.startswith(prefix):
                if best is None or len(prefix) > len(best[0]):
                    best = (prefix, h)
        if best:
            return best[1](method, url, headers, body)
        return 404, {}, b'{"message":"no route"}'


def J(obj):
    return 200, {}, json.dumps(obj).encode()


class TestGhAuth(unittest.TestCase):
    def test_device_code(self):
        http = FakeHttp()
        def h(method, url, headers, body):
            self.assertIn(b'client_id=cid123', body)
            self.assertIn(b'scope=repo', body)
            self.assertNotIn('Authorization', headers)
            return J({'user_code': 'ABCD-EFGH', 'device_code': 'dev',
                      'verification_uri': 'https://github.com/login/device',
                      'interval': 5})
        http.add('POST', gh.DEVICE_CODE_URL, h)
        c = gh.Client(transport=http)
        dc = c.device_code('cid123')
        self.assertEqual(dc['user_code'], 'ABCD-EFGH')

    def test_poll_pending_then_success(self):
        http = FakeHttp()
        answers = iter([J({'error': 'authorization_pending'}),
                        J({'error': 'slow_down'}),
                        J({'access_token': 'tok999'})])
        http.add('POST', gh.TOKEN_URL, lambda *a: next(answers))
        c = gh.Client(transport=http)
        self.assertIsNone(c.poll_token('cid', 'dev'))
        self.assertIsNone(c.poll_token('cid', 'dev'))
        self.assertEqual(c.poll_token('cid', 'dev'), 'tok999')

    def test_poll_expired_and_denied(self):
        for err, frag in (('expired_token', 'expired'), ('access_denied', 'declined')):
            http = FakeHttp()
            http.add('POST', gh.TOKEN_URL, lambda *a, e=err: J({'error': e}))
            c = gh.Client(transport=http)
            with self.assertRaises(gh.GhError) as ctx:
                c.poll_token('cid', 'dev')
            self.assertIn(frag, str(ctx.exception))

    def test_error_mapping(self):
        for status, frag in ((401, 'rejected'), (404, 'Not found'),
                             (422, 'refused')):
            http = FakeHttp()
            http.add('GET', gh.API + '/user',
                     lambda *a, s=status: (s, {}, b'{"message":"nope"}'))
            c = gh.Client(token='t', transport=http)
            with self.assertRaises(gh.GhError) as ctx:
                c.whoami()
            self.assertIn(frag, str(ctx.exception))


class TestGhStore(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.c = gh.Client(token='tok', transport=self.http, repo='me/drive')

    def test_list_dir_hides_chunks(self):
        man = json.dumps({'size': 10, 'parts': 2, 'sha256': 'x'}).encode()
        listing = [
            {'name': 'a.txt', 'path': 'a.txt', 'type': 'file', 'size': 3, 'sha': 's1'},
            {'name': 'big.fwmanifest', 'path': 'big.fwmanifest', 'type': 'file',
             'size': 40, 'sha': 'sm'},
            {'name': 'big.fwpart', 'path': 'big.fwpart', 'type': 'dir', 'size': 0,
             'sha': 'sp'},
            {'name': 'sub', 'path': 'sub', 'type': 'dir', 'size': 0, 'sha': 'sd'},
        ]
        self.http.add('GET', gh.API + '/repos/me/drive/contents/', lambda *a: J(listing))
        self.http.add('GET', gh.API + '/repos/me/drive/git/blobs/sm',
                      lambda *a: J({'encoding': 'base64',
                                    'content': base64.b64encode(man).decode()}))
        entries = self.c.list_dir('')
        names = [e['name'] for e in entries]
        self.assertEqual(names, ['sub', 'a.txt', 'big'])
        big = [e for e in entries if e['name'] == 'big'][0]
        self.assertTrue(big['chunked'])
        self.assertEqual(big['size'], 10)

    def test_read_small_via_contents(self):
        self.http.add('GET', gh.API + '/repos/me/drive/contents/a.txt', lambda *a: J(
            {'type': 'file', 'size': 5,
             'content': base64.b64encode(b'hello').decode()}))
        self.assertEqual(self.c.read_file('a.txt'), b'hello')

    def test_upload_small_uses_contents_put(self):
        self.http.add('GET', gh.API + '/repos/me/drive/contents/a.txt',
                      lambda *a: (404, {}, b'{"message":"nf"}'))
        def put(method, url, headers, body):
            payload = json.loads(body)
            self.assertEqual(base64.b64decode(payload['content']), b'hi there')
            self.http.put_seen = True
            return J({'content': {}})
        self.http.put_seen = False
        self.http.add('PUT', gh.API + '/repos/me/drive/contents/a.txt', put)
        out = self.c.upload('a.txt', b'hi there')
        self.assertEqual(out['kind'], 'plain')
        self.assertTrue(self.http.put_seen)
        auth = [h for m, u, h, b in self.http.calls if m == 'PUT'][0]
        self.assertEqual(auth.get('Authorization'), 'Bearer tok')

    def test_upload_medium_uses_git_api(self):
        seq = []
        self.http.add('GET', gh.API + '/repos/me/drive', lambda *a: J({'default_branch': 'main'}))
        self.http.add('GET', gh.API + '/repos/me/drive/git/ref/heads/main',
                      lambda *a: J({'object': {'sha': 'c0'}}))
        self.http.add('GET', gh.API + '/repos/me/drive/git/commits/c0',
                      lambda *a: J({'tree': {'sha': 't0'}}))
        self.http.add('POST', gh.API + '/repos/me/drive/git/blobs',
                      lambda *a: (seq.append('blob'), J({'sha': 'b1'}))[1])
        self.http.add('POST', gh.API + '/repos/me/drive/git/trees',
                      lambda *a: (seq.append('tree'), J({'sha': 't1'}))[1])
        self.http.add('POST', gh.API + '/repos/me/drive/git/commits',
                      lambda *a: (seq.append('commit'), J({'sha': 'c1'}))[1])
        self.http.add('PATCH', gh.API + '/repos/me/drive/git/refs/heads/main',
                      lambda *a: (seq.append('ref'), J({}))[1])
        out = self.c.upload('mid.bin', b'x' * 5000, contents_max=1000)
        self.assertEqual(out['kind'], 'blob')
        self.assertEqual(seq, ['blob', 'tree', 'commit', 'ref'])

    def test_chunked_upload_and_download(self):
        blobs = {}
        counter = iter(range(1000))
        self.http.add('GET', gh.API + '/repos/me/drive', lambda *a: J({'default_branch': 'main'}))
        self.http.add('GET', gh.API + '/repos/me/drive/git/ref/heads/main',
                      lambda *a: J({'object': {'sha': 'c0'}}))
        self.http.add('GET', gh.API + '/repos/me/drive/git/commits/c0',
                      lambda *a: J({'tree': {'sha': 't0'}}))
        def blob(method, url, headers, body):
            sha = 'sha%d' % next(counter)
            blobs[sha] = base64.b64decode(json.loads(body)['content'])
            return J({'sha': sha})
        self.http.add('POST', gh.API + '/repos/me/drive/git/blobs', blob)
        self.http.add('POST', gh.API + '/repos/me/drive/git/trees', lambda *a: J({'sha': 't1'}))
        self.http.add('POST', gh.API + '/repos/me/drive/git/commits', lambda *a: J({'sha': 'c1'}))
        self.http.add('PATCH', gh.API + '/repos/me/drive/git/refs/heads/main', lambda *a: J({}))
        data = bytes(range(256)) * 40   # 10240 bytes -> 3 chunks of 4096
        out = self.c.upload('big.bin', data, contents_max=1000, file_max=6000, chunk=4096)
        self.assertEqual(out['kind'], 'chunked')
        self.assertEqual(out['parts'], 3)
        man_blob = [v for v in blobs.values() if v.startswith(b'{')][0]
        man = json.loads(man_blob)
        self.assertEqual(man['parts'], 3)
        self.assertEqual(man['size'], len(data))
        self.assertEqual(man['sha256'], hashlib.sha256(data).hexdigest())
        # now download: serve manifest + parts from blobs
        part_shas = {}
        for sha, content in blobs.items():
            if not content.startswith(b'{'):
                part_shas.setdefault(len(part_shas) + 1, sha)
        def contents(method, url, headers, body):
            path = url.split('/contents/')[1]
            if path.endswith('.fwmanifest'):
                return J({'type': 'file', 'size': 9000, 'sha': 'man'})
            idx = int(path.rsplit('/', 1)[1])
            return J({'type': 'file', 'size': 99999, 'sha': part_shas[idx]})
        self.http.add('GET', gh.API + '/repos/me/drive/contents/big.bin', contents)
        self.http.add('GET', gh.API + '/repos/me/drive/git/blobs/man',
                      lambda *a: J({'encoding': 'base64',
                                    'content': base64.b64encode(man_blob).decode()}))
        def getblob(method, url, headers, body):
            sha = url.rsplit('/', 1)[1]
            return J({'encoding': 'base64',
                      'content': base64.b64encode(blobs[sha]).decode()})
        self.http.add('GET', gh.API + '/repos/me/drive/git/blobs/sha', getblob)
        got = bytearray()
        size, digest = self.c.read_chunked('big.bin', got.extend)
        self.assertEqual(bytes(got), data)
        self.assertEqual(size, len(data))

    def test_chunked_checksum_failure(self):
        man = json.dumps({'size': 4, 'parts': 1, 'sha256': 'deadbeef'}).encode()
        self.http.add('GET', gh.API + '/repos/me/drive/contents/big.bin.fwmanifest',
                      lambda *a: J({'type': 'file', 'size': 10,
                                    'content': base64.b64encode(man).decode()}))
        self.http.add('GET', gh.API + '/repos/me/drive/contents/big.bin.fwpart/000001',
                      lambda *a: J({'type': 'file', 'size': 4, 'sha': 'p1'}))
        self.http.add('GET', gh.API + '/repos/me/drive/git/blobs/p1',
                      lambda *a: J({'encoding': 'base64',
                                    'content': base64.b64encode(b'abcd').decode()}))
        with self.assertRaises(gh.GhError):
            self.c.read_chunked('big.bin', lambda b: None)

    def test_token_file_mode(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ['XDG_CONFIG_HOME'] = d
            gh.save_token('sekrit', 'me')
            self.assertEqual(gh.load_token(), {'token': 'sekrit', 'login': 'me'})
            mode = os.stat(gh.token_path()).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            gh.clear_token()
            self.assertIsNone(gh.load_token())
            del os.environ['XDG_CONFIG_HOME']

    def test_ensure_repo_creates_when_missing(self):
        calls = []
        def get(method, url, headers, body):
            return 404, {}, b'{"message":"Not Found"}'
        def post(method, url, headers, body):
            payload = json.loads(body)
            calls.append(payload)
            return J({'full_name': 'me/newrepo'})
        self.http.add('GET', gh.API + '/user', lambda *a: J({'login': 'me'}))
        self.http.add('GET', gh.API + '/repos/me/newrepo', get)
        self.http.add('POST', gh.API + '/user/repos', post)
        c = gh.Client(token='t', transport=self.http)
        self.assertEqual(c.ensure_repo('newrepo'), 'me/newrepo')
        self.assertTrue(calls[0]['private'])


if __name__ == '__main__':
    unittest.main(verbosity=1)
