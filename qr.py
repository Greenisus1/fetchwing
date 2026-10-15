"""Minimal pure-python QR encoder (byte mode, EC level M, versions 1-10).

Written from the ISO/IEC 18004 spec. No dependencies, no pip install:
this is what lets fetchwing show a scannable login QR on a bare terminal.

Public API:
    encode(text, force_mask=None) -> matrix (list[list[bool]], True = dark)
    render_text(matrix) -> str    (half-block rendering, 2 QR rows per line)
"""
# --- GF(2^8) arithmetic, primitive polynomial 0x11D ---
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]

def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]

def _rs_gen(n):
    """Generator polynomial for n EC codewords, coefficients ascending."""
    g = [1]
    for i in range(n):
        g2 = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            g2[j] ^= _mul(c, _EXP[i])
            g2[j + 1] ^= c
        g = g2
    return g

def _rs_encode(data, n):
    """Reed-Solomon remainder (n EC codewords) for data codewords."""
    g = _rs_gen(n)
    rem = [0] * n
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for j in range(n):
                rem[j] ^= _mul(g[n - 1 - j], f)
    return rem

# (total codewords, data codewords, EC codewords per block, block count)
# for EC level M, versions 1..10. Data blocks: `data % blocks` trailing
# blocks carry one extra data codeword (shorter blocks come first).
_M = {
    1: (26, 16, 10, 1),
    2: (44, 28, 16, 1),
    3: (70, 44, 26, 1),
    4: (100, 64, 18, 2),
    5: (134, 86, 24, 2),
    6: (172, 108, 16, 4),
    7: (196, 124, 18, 4),
    8: (242, 154, 22, 4),
    9: (292, 182, 22, 5),
    10: (346, 216, 26, 5),
}
# 18-bit BCH version information strings for versions 7..10
_VERSION_INFO = {7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3}

_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

def _fmt_bits(mask):
    """15-bit BCH format information for EC level M (00) + mask."""
    d = mask & 7
    b = d << 10
    g = 0x537
    for i in range(14, 9, -1):
        if b >> i & 1:
            b ^= g << (i - 10)
    return ((d << 10) | b) ^ 0x5412

def _mask_bit(mask, r, c):
    if mask == 0: return (r + c) % 2 == 0
    if mask == 1: return r % 2 == 0
    if mask == 2: return c % 3 == 0
    if mask == 3: return (r + c) % 3 == 0
    if mask == 4: return (r // 2 + c // 3) % 2 == 0
    if mask == 5: return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6: return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0

def _build_base(v, out):
    """Return (matrix, func) with function patterns drawn and data placed
    (unmasked). matrix cells: bool; func cells: True where no data lives."""
    size = 17 + 4 * v
    m = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def finder(r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r0 + dr, c0 + dc
                if 0 <= rr < size and 0 <= cc < size:
                    on = (0 <= dr <= 6 and 0 <= dc <= 6) and (
                        dr in (0, 6) or dc in (0, 6)
                        or (2 <= dr <= 4 and 2 <= dc <= 4))
                    m[rr][cc] = on
                    func[rr][cc] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # alignment patterns: skip the three combos overlapping a finder
    al = _ALIGN[v]
    for r in al:
        for c in al:
            if ((r <= 8 and c <= 8) or (r <= 8 and c >= size - 9)
                    or (r >= size - 9 and c <= 8)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1
                    func[r + dr][c + dc] = True

    # timing patterns (only where nothing else lives)
    for i in range(8, size - 8):
        for rr, cc in ((6, i), (i, 6)):
            if not func[rr][cc]:
                m[rr][cc] = (i % 2 == 0)
                func[rr][cc] = True

    # dark module
    m[size - 8][8] = True
    func[size - 8][8] = True

    # version information (v7+): two 3x6 blocks, ISO/IEC 18004 fig. 27
    if v >= 7:
        vi = _VERSION_INFO[v]
        for i in range(6):
            b1 = bool(vi >> (i * 3) & 1)
            b2 = bool(vi >> (i * 3 + 1) & 1)
            b3 = bool(vi >> (i * 3 + 2) & 1)
            cells = ((size - 11, i), (size - 10, i), (size - 9, i),
                     (i, size - 11), (i, size - 10), (i, size - 9))
            for (rr, cc), bit in zip(cells, (b1, b2, b3, b1, b2, b3)):
                m[rr][cc] = bit
                func[rr][cc] = True

    # reserve format-info areas (values written after masking)
    for i in range(9):
        func[8][i] = True
        func[i][8] = True
    for i in range(8):
        func[8][size - 1 - i] = True
        func[size - 1 - i][8] = True

    # zigzag data placement, right to left in column pairs
    bits = []
    for byte in out:
        for i in range(7, -1, -1):
            bits.append(byte >> i & 1)
    i = 0
    col = size - 1
    up = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not func[r][c]:
                    m[r][c] = bool(bits[i]) if i < len(bits) else False
                    i += 1
        up = not up
        col -= 2
    return m, func

def _write_format(m, mask):
    size = len(m)
    f = _fmt_bits(mask)
    bit = lambda i: bool(f >> (14 - i) & 1)  # bit 0 = MSB of the 15-bit string
    # top-left copy
    for i in range(6):
        m[8][i] = bit(i)
    m[8][7] = bit(6)
    m[8][8] = bit(7)
    m[7][8] = bit(8)
    for i in range(9, 15):
        m[14 - i][8] = bit(i)
    # split copy: column 8 bottom (bits 0..6), row 8 right (bits 7..14)
    for i in range(7):
        m[size - 1 - i][8] = bit(i)
    for i in range(7, 15):
        m[8][size - 15 + i] = bit(i)

def _penalty(m):
    size = len(m)
    pen = 0
    # rule 1: runs of 5+ in rows and columns
    for line in ([m[r][:] for r in range(size)]
                 + [[m[r][c] for r in range(size)] for c in range(size)]):
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    pen += run - 2
                run = 1
        if run >= 5:
            pen += run - 2
    # rule 2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            if (m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]):
                pen += 3
    # rule 3: finder-like 11-cell patterns
    pats = ((1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0),
            (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1))
    for line in ([[int(x) for x in m[r]] for r in range(size)]
                 + [[int(m[r][c]) for r in range(size)] for c in range(size)]):
        for i in range(size - 10):
            w = tuple(line[i:i + 11])
            if w == pats[0] or w == pats[1]:
                pen += 40
    # rule 4: dark-module proportion
    dark = sum(sum(row) for row in m)
    pct = dark * 100.0 / (size * size)
    pen += int(abs(pct - 50) // 5) * 10
    return pen

def encode(text, force_mask=None):
    """Encode text as a QR matrix (byte mode, EC level M, versions 1-10).
    Returns list of rows of bools (True = dark). Raises ValueError if too long."""
    data = text.encode('utf-8')
    v = None
    for cand in range(1, 11):
        total, dlen, ecnb, nb = _M[cand]
        cci = 8 if cand <= 9 else 16
        if 4 + cci + 8 * len(data) <= dlen * 8:
            v = cand
            break
    if v is None:
        raise ValueError('text too long for QR version 10 (EC level M)')
    total, dlen, ecnb, nb = _M[v]

    bits = []
    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append(val >> i & 1)
    put(0b0100, 4)                       # byte mode
    put(len(data), 8 if v <= 9 else 16)  # character count
    for byte in data:
        put(byte, 8)
    cap = dlen * 8
    put(0, min(4, cap - len(bits)))      # terminator
    # pad to codeword boundary. When already aligned, append a full zero
    # codeword: mirrors segno's field-proven output (pads are inert filler -
    # decoders stop at the terminator - so compatibility wins here).
    put(0, 8 - (len(bits) % 8))
    pads = (0xEC, 0x11)
    pi = 0
    while len(bits) < cap:
        put(pads[pi % 2], 8)
        pi += 1
    code = [sum(bits[i * 8 + j] << (7 - j) for j in range(8))
            for i in range(dlen)]

    # split into blocks (shorter first), compute EC, interleave
    per, extra = divmod(dlen, nb)
    blocks = []
    i = 0
    for b in range(nb):
        n = per + (1 if b >= nb - extra else 0)
        blocks.append(code[i:i + n])
        i += n
    ecs = [_rs_encode(b, ecnb) for b in blocks]
    out = []
    for j in range(per + 1):
        for b in blocks:
            if j < len(b):
                out.append(b[j])
    for j in range(ecnb):
        for e in ecs:
            out.append(e[j])

    base, func = _build_base(v, out)
    size = len(base)

    masks = range(8) if force_mask is None else (force_mask,)
    best = None
    for mask in masks:
        cand = [row[:] for row in base]
        for r in range(size):
            for c in range(size):
                if not func[r][c] and _mask_bit(mask, r, c):
                    cand[r][c] = not cand[r][c]
        _write_format(cand, mask)
        if force_mask is not None:
            return cand
        p = _penalty(cand)
        if best is None or p < best[0]:
            best = (p, mask, cand)
    return best[2]

def render_text(matrix):
    """Half-block rendering: two QR rows per text line, 2-cell quiet zone."""
    n = len(matrix)
    pad = 2
    out = []
    for r in range(0, n, 2):
        line = ' ' * pad
        for c in range(n):
            top = matrix[r][c]
            bot = matrix[r + 1][c] if r + 1 < n else False
            line += ('\u2588' if top and bot else '\u2580' if top
                     else '\u2584' if bot else ' ')
        out.append(line + ' ' * pad)
    out.insert(0, ' ' * (n + 2 * pad))
    out.append(' ' * (n + 2 * pad))
    return '\n'.join(out)
