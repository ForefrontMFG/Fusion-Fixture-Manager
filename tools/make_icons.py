"""Regenerate the toolbar icons in ../resources (pure Python, no dependencies)."""
import os, struct, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, '..', 'resources')
GRAY, DARK, BLUE, GREEN = (110, 118, 128, 255), (60, 66, 74, 255), (38, 132, 214, 255), (46, 160, 90, 255)

def png(path, w, h, px):
    raw = b''.join(b'\x00' + b''.join(struct.pack('4B', *px[y][x]) for x in range(w)) for y in range(h))
    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))

def draw(rects, n):
    px = [[(0, 0, 0, 0)] * n for _ in range(n)]
    for (x0, y0, x1, y1, c) in rects:  # coords on a 16-unit grid
        for y in range(int(y0 * n / 16), int(y1 * n / 16)):
            for x in range(int(x0 * n / 16), int(x1 * n / 16)):
                px[y][x] = c
    return px

ICONS = {
    # vise body + two jaws + blue stock seated between them
    'apply': [(1, 12, 15, 15, DARK), (2, 6, 5, 12, GRAY), (11, 6, 14, 12, GRAY), (5, 4, 11, 10, BLUE)],
    # fixture block with a green plus
    'register': [(1, 9, 11, 15, GRAY), (11, 1, 13, 9, GREEN), (8, 4, 16, 6, GREEN)],
    # stacked shelf of fixtures
    'library': [(1, 2, 15, 5, GRAY), (1, 7, 15, 10, BLUE), (1, 12, 15, 15, GRAY)],
    # tray with a green arrow going up (publish to GitHub)
    'publish': [(1, 12, 15, 15, GRAY), (7, 4, 9, 11, GREEN), (4, 5, 12, 7, GREEN), (6, 3, 10, 5, GREEN)],
    # tray with a blue arrow coming down (install an update)
    'update': [(1, 12, 15, 15, GRAY), (7, 1, 9, 8, BLUE), (4, 7, 12, 9, BLUE), (6, 9, 10, 11, BLUE)],
}

for name, rects in ICONS.items():
    d = os.path.join(OUT, name)
    os.makedirs(d, exist_ok=True)
    for size, fname in ((16, '16x16.png'), (32, '32x32.png'), (32, '16x16@2x.png'), (64, '32x32@2x.png')):
        png(os.path.join(d, fname), size, size, draw(rects, size))
print('icons written to', os.path.normpath(OUT))
