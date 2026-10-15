"""GitHub backend for fetchwing: device-flow login, token storage, and a
private-repo file store built on the GitHub REST API.

Network goes through one injectable `transport` callable so tests can run
offline. Default transport is urllib from the standard library.

File size handling (GitHub limits):
  * <= 1 MB   : Contents API (single PUT)
  * 1-100 MB  : Git Blob + Tree + Commit API (Contents API refuses these)
  * > 100 MB  : split into <=95 MB parts stored as "<name>.fwpart/000001..."
                plus a "<name>.fwmanifest" JSON manifest; downloads reassemble
                and verify SHA-256.
"""
import base64, hashlib, json, os, stat, time, urllib.error, urllib.request

API = 'https://api.github.com'
DEVICE_CODE_URL = 'https://github.com/login/device/code'
TOKEN_URL = 'https://github.com/login/oauth/access_token'
VERIFY_URI = 'https://github.com/login/device'

# An OAuth App client id is required for device-flow login. The fallback
# "paste a personal access token" path works without it.
CLIENT_ID = 'Ov23liE4ISwOOwp2coRn'

CONTENTS_API_MAX = 1_000_000      # Contents API limit (1 MB)
FILE_MAX = 100_000_000            # GitHub hard per-file limit (100 MB)
CHUNK = 95_000_000                # chunk size for oversized files
MANIFEST_SUFFIX = '.fwmanifest'
PART_SUFFIX = '.fwpart'

UA = 'fetchwing/1.0'


class GhError(Exception):
    """Friendly-mapped GitHub failure."""


def _default_transport(method, url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _b64(data):
    return base64.b64encode(data).decode('ascii')


def _unb64(text):
    return base64.b64decode(text.encode('ascii'))


class Client:
    def __init__(self, token=None, transport=None, repo=None, login=None):
        self.token = token
        self.repo = repo
        self.login = login
        self._transport = transport or _default_transport
        self.user_cache = None

    # -- low level ------------------------------------------------------
    def _request(self, method, path, payload=None, token=None, form=None):
        url = path if path.startswith('http') else API + path
        headers = {'User-Agent': UA, 'Accept': 'application/vnd.github+json'}
        tok = token if token is not None else self.token
        if tok:
            headers['Authorization'] = 'Bearer ' + tok
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        status, _hdrs, raw = self._transport(method, url, headers, body)
        if status in (204,):
            return {}
        try:
            data = json.loads(raw.decode('utf-8')) if raw else {}
        except ValueError:
            raise GhError('GitHub returned unreadable data (status %d)' % status)
        if status >= 400:
            msg = data.get('message', '') if isinstance(data, dict) else ''
            if status == 401:
                raise GhError('GitHub rejected the token - sign in again.')
            if status == 404:
                raise GhError('Not found: ' + path)
            if status == 403 and 'rate limit' in msg.lower():
                raise GhError('GitHub rate limit hit - wait a bit and retry.')
            if status == 422:
                raise GhError('GitHub refused the request: ' + msg)
            raise GhError('GitHub error %d: %s' % (status, msg or 'unknown'))
        return data

    # -- auth -----------------------------------------------------------
    def device_code(self, client_id=CLIENT_ID):
        return self._request('POST', DEVICE_CODE_URL,
                             form={'client_id': client_id, 'scope': 'repo'},
                             token=None)

    def poll_token(self, client_id, device_code):
        """One poll. Returns token string, or None while pending."""
        data = self._request('POST', TOKEN_URL, form={
            'client_id': client_id, 'device_code': device_code,
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'},
            token=None)
        if data.get('access_token'):
            return data['access_token']
        err = data.get('error')
        if err in ('authorization_pending', 'slow_down'):
            return None
        if err == 'expired_token':
            raise GhError('The device code expired - start login again.')
        if err == 'access_denied':
            raise GhError('Login was declined on GitHub.')
        raise GhError('Login failed: ' + (err or 'unknown'))

    def whoami(self):
        if not self.user_cache:
            data = self._request('GET', '/user')
            self.user_cache = data
        return self.user_cache

    # -- repo -----------------------------------------------------------
    def ensure_repo(self, name):
        """Return 'owner/name', creating a private repo if needed."""
        owner = self.whoami()['login']
        try:
            self._request('GET', '/repos/%s/%s' % (owner, name))
        except GhError as e:
            if 'Not found' not in str(e):
                raise
            self._request('POST', '/user/repos', payload={
                'name': name, 'private': True, 'auto_init': True,
                'description': 'fetchwing cloud drive'})
        self.repo = '%s/%s' % (owner, name)
        return self.repo

    def default_branch(self):
        return self._request('GET', '/repos/' + self.repo)['default_branch']

    # -- listing --------------------------------------------------------
    def list_dir(self, path=''):
        """List a directory. Returns entries: {name, path, type, size, sha}.
        Chunked files appear once as their logical name (type 'file',
        chunked=True, size from manifest)."""
        data = self._request('GET', '/repos/%s/contents/%s' % (self.repo, path))
        if isinstance(data, dict):  # a file, not a dir
            raise GhError('Not a folder: ' + (path or '/'))
        out = []
        manifests = {}
        for it in data:
            name = it['name']
            if name.endswith(MANIFEST_SUFFIX):
                manifests[name[:-len(MANIFEST_SUFFIX)]] = it
                continue
            if name.endswith(PART_SUFFIX):
                continue
            out.append({'name': name, 'path': it['path'], 'type': it['type'],
                        'size': it.get('size', 0), 'sha': it.get('sha', '')})
        for logical, man in manifests.items():
            info = {'parts': 0, 'size': 0}
            try:
                meta = json.loads(self._blob_text(man['sha']))
                info = meta
            except Exception:
                pass
            out.append({'name': logical, 'path': man['path'][:-len(MANIFEST_SUFFIX)],
                        'type': 'file', 'size': info.get('size', 0),
                        'sha': man.get('sha', ''), 'chunked': True,
                        'parts': info.get('parts', 0)})
        out.sort(key=lambda e: (e['type'] != 'dir', e['name'].lower()))
        return out

    # -- download -------------------------------------------------------
    def _blob_text(self, sha):
        return self._blob_bytes(sha).decode('utf-8')

    def _blob_bytes(self, sha):
        data = self._request('GET', '/repos/%s/git/blobs/%s' % (self.repo, sha))
        if data.get('encoding') != 'base64':
            raise GhError('Unexpected blob encoding for ' + sha)
        return _unb64(data['content'])

    def read_file(self, path):
        """Read a (possibly chunked) file. Returns bytes."""
        data = self._request('GET', '/repos/%s/contents/%s' % (self.repo, path))
        if isinstance(data, list):
            raise GhError('That is a folder, not a file: ' + path)
        if data.get('type') != 'file':
            raise GhError('Not a file: ' + path)
        if data.get('size', 0) <= CONTENTS_API_MAX and data.get('content'):
            return _unb64(data['content'])
        return self._blob_bytes(data['sha'])

    def read_chunked(self, logical_path, dest_write):
        """Download a chunked file. dest_write(chunk_bytes) called in order.
        Returns (size, sha256_hex)."""
        man = json.loads(self.read_file(logical_path + MANIFEST_SUFFIX))
        h = hashlib.sha256()
        total = 0
        for i in range(man['parts']):
            part_path = '%s%s/%06d' % (logical_path, PART_SUFFIX, i + 1)
            blob = self._request('GET', '/repos/%s/contents/%s' % (self.repo, part_path))
            chunk = self._blob_bytes(blob['sha'])
            h.update(chunk)
            total += len(chunk)
            dest_write(chunk)
        if total != man['size'] or h.hexdigest() != man['sha256']:
            raise GhError('Chunked download failed its checksum - file dropped.')
        return total, man['sha256']

    # -- upload ---------------------------------------------------------
    def _put_contents(self, path, content, message):
        payload = {'message': message, 'content': _b64(content)}
        try:
            cur = self._request('GET', '/repos/%s/contents/%s' % (self.repo, path))
            if isinstance(cur, dict) and cur.get('sha'):
                payload['sha'] = cur['sha']
        except GhError:
            pass
        self._request('PUT', '/repos/%s/contents/%s' % (self.repo, path), payload)

    def _git_write(self, entries, message):
        """entries: [(path, bytes_or_None)] - None deletes. One commit."""
        branch = self.default_branch()
        ref = self._request('GET', '/repos/%s/git/ref/heads/%s' % (self.repo, branch))
        commit_sha = ref['object']['sha']
        commit = self._request('GET', '/repos/%s/git/commits/%s' % (self.repo, commit_sha))
        tree = []
        for path, content in entries:
            if content is None:
                tree.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': None})
            else:
                blob = self._request('POST', '/repos/%s/git/blobs' % self.repo,
                                     {'content': _b64(content), 'encoding': 'base64'})
                tree.append({'path': path, 'mode': '100644', 'type': 'blob',
                             'sha': blob['sha']})
        new_tree = self._request('POST', '/repos/%s/git/trees' % self.repo,
                                 {'base_tree': commit['tree']['sha'], 'tree': tree})
        new_commit = self._request('POST', '/repos/%s/git/commits' % self.repo, {
            'message': message, 'tree': new_tree['sha'], 'parents': [commit_sha]})
        self._request('PATCH', '/repos/%s/git/refs/heads/%s' % (self.repo, branch),
                      {'sha': new_commit['sha']})

    def upload(self, path, content, contents_max=CONTENTS_API_MAX,
               file_max=FILE_MAX, chunk=CHUNK):
        """Upload bytes to path, splitting over GitHub's 100 MB limit."""
        n = len(content)
        if n <= contents_max:
            self._put_contents(path, content, 'fetchwing: upload ' + path)
            return {'kind': 'plain', 'size': n}
        if n <= file_max:
            self._git_write([(path, content)], 'fetchwing: upload ' + path)
            return {'kind': 'blob', 'size': n}
        parts = (n + chunk - 1) // chunk
        h = hashlib.sha256(content)
        entries = []
        for i in range(parts):
            entries.append(('%s%s/%06d' % (path, PART_SUFFIX, i + 1),
                            content[i * chunk:(i + 1) * chunk]))
        manifest = json.dumps({'version': 1, 'size': n, 'parts': parts,
                               'part_size': chunk, 'sha256': h.hexdigest()},
                              sort_keys=True).encode('utf-8')
        entries.append((path + MANIFEST_SUFFIX, manifest))
        self._git_write(entries, 'fetchwing: upload %s (%d parts)' % (path, parts))
        return {'kind': 'chunked', 'size': n, 'parts': parts}

    def delete(self, entry):
        """Delete a file (chunked or plain) or leave dirs alone."""
        if entry.get('chunked'):
            man = json.loads(self.read_file(entry['path'] + MANIFEST_SUFFIX))
            targets = ['%s%s/%06d' % (entry['path'], PART_SUFFIX, i + 1)
                       for i in range(man['parts'])]
            targets.append(entry['path'] + MANIFEST_SUFFIX)
            self._git_write([(p, None) for p in targets],
                            'fetchwing: delete ' + entry['path'])
        else:
            sha = entry.get('sha') or self._request(
                'GET', '/repos/%s/contents/%s' % (self.repo, entry['path']))['sha']
            self._request('DELETE', '/repos/%s/contents/%s' % (self.repo, entry['path']),
                          {'message': 'fetchwing: delete ' + entry['path'], 'sha': sha})

    def mkdir(self, path):
        self._put_contents(path.rstrip('/') + '/.fwkeep', b'',
                           'fetchwing: new folder ' + path)


# -- token storage -------------------------------------------------------

def config_dir():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'fetchwing')


def token_path():
    return os.path.join(config_dir(), 'token.json')


def save_token(token, login):
    os.makedirs(config_dir(), exist_ok=True)
    os.chmod(config_dir(), 0o700)
    path = token_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'token': token, 'login': login}, f)
    os.chmod(path, 0o600)


def load_token():
    try:
        with open(token_path()) as f:
            data = json.load(f)
        if data.get('token'):
            return data
    except (OSError, ValueError):
        pass
    return None


def clear_token():
    try:
        os.remove(token_path())
    except OSError:
        pass
