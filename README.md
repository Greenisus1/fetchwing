# fetchwing

A fullscreen cloud drive in your terminal, backed by a private GitHub repo. Sign in by scanning a QR code with your phone, then browse, upload, download, and delete files from any Linux or Raspberry Pi terminal. No dependencies, no pip install: just Python 3 and the standard curses library.

fetchwing is an original app. It talks to GitHub's official REST API; it is not affiliated with GitHub.

## Run

```
python3 fetchwing.py
```

## Signing in

Two ways in:

- **QR code (GitHub device login).** The terminal shows a real, scannable QR code (drawn in block characters by fetchwing's own pure-Python QR encoder) plus a short code. Point your phone camera at it, approve on GitHub, and the app continues by itself. If you are on the machine itself, a tiny local page at `http://127.0.0.1:8917` shows the same code and link. The app ships with the fetchwing OAuth client id (public, not a secret), so this works out of the box.
- **Personal access token.** Create a token at GitHub -> Settings -> Developer settings -> Personal access tokens with the `repo` scope and paste it in.

Your token is stored only on your machine at `~/.config/fetchwing/token.json` with permissions `600` (owner read/write only, inside a `700` directory). It never goes into any repo, log, or screen. Press `L` in the browser to log out and delete it.

## Your drive

On first run fetchwing creates (or reuses) a **private** repo named `fetchwing-drive` under your account and uses it as the drive. Only you can see it.

- **Arrows** move, **Enter** opens a folder, **Backspace** goes up.
- **D** downloads the selected file, **U** uploads a local file, **N** makes a folder, **X** deletes (asks you to type `yes`), **R** refreshes.
- Chunked files (over 100 MB, see below) show a `*` marker and their full logical size.

## File sizes and GitHub's limits

GitHub is a code host, not a bulk storage service - fetchwing is for documents, saves, configs, photos, and other everyday files, not a media archive.

- Files up to 1 MB upload through the Contents API.
- Files from 1 MB to 100 MB upload through the Git blob API (the Contents API refuses them).
- Files over GitHub's hard 100 MB per-file limit are split into 95 MB parts with a small manifest; downloads reassemble the parts and verify a SHA-256 checksum before you get the file.

Please be a good citizen: avoid hammering the API with huge repeated uploads, and keep an eye on GitHub's storage guidance (repos work best under a few GB).

## Demo mode

`FETCHWING_FAKE=1 python3 fetchwing.py` runs entirely offline against a fake drive - handy for a test drive with no account.

## Development

```
python3 test_fetchwing.py   # unit tests: QR encoder golden vectors + GitHub backend (offline fake transport)
python3 test_pty.py         # event-driven pseudo-terminal smoke test
```

The QR encoder (`qr.py`) is written from the ISO/IEC 18004 spec (byte mode, error-correction level M, versions 1-10) and its output was verified cell-for-cell against a reference encoder across 232 payload/mask combinations during development.

Status: unit tests and terminal (PTY) checks pass on x86-64 Linux. It has NOT yet run on a real Raspberry Pi; the first launch on your Pi is the first real test there. Report anything odd and it will be fixed.

## License

MIT - see LICENSE.
