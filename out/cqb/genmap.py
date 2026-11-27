MS = 24
grid = [['#']*MS for _ in range(MS)]

def open_rect(x0, y0, x1, y1, ch=' '):
    for y in range(y0, y1+1):
        for x in range(x0, x1+1):
            grid[y][x] = ch

open_rect(1, 1, 22, 22)

# central plaza walls
for x in range(8, 16):
    grid[8][x] = 'M'
    grid[15][x] = 'M'
for y in range(9, 15):
    grid[y][8] = 'M'
    grid[y][15] = 'M'
# doorways
for (x, y) in [(11, 8), (12, 8), (11, 15), (12, 15), (8, 11), (8, 12), (15, 11), (15, 12)]:
    grid[y][x] = ' '
# inner crates
for (x, y) in [(10, 10), (10, 11), (13, 10), (13, 11)]:
    grid[y][x] = 'C'

# corner crate clusters
for cx, cy in [(3, 3), (19, 3), (3, 19), (19, 19)]:
    grid[cy][cx] = 'C'
    grid[cy][cx+1] = 'C'
    grid[cy+1][cx] = 'C'

# pillars
for (x, y) in [(11, 4), (12, 4), (11, 19), (12, 19), (4, 11), (4, 12), (19, 11), (19, 12)]:
    grid[y][x] = '#'

# extra cover blocks
for (x, y) in [(6, 7), (17, 7), (6, 16), (17, 16)]:
    grid[y][x] = 'C'

rows = [''.join(r) for r in grid]
for i, r in enumerate(rows):
    print(f'{i:2d} {r}')

# connectivity BFS from player spawn
start = (11, 21)
assert grid[start[1]][start[0]] == ' ', 'player spawn blocked'
seen = {start}
q = [start]
while q:
    x, y = q.pop()
    for dx, dy in ((1,0), (-1,0), (0,1), (0,-1)):
        nx, ny = x+dx, y+dy
        if 0 <= nx < MS and 0 <= ny < MS and (nx, ny) not in seen and grid[ny][nx] == ' ':
            seen.add((nx, ny)); q.append((nx, ny))

floors = sum(1 for y in range(MS) for x in range(MS) if grid[y][x] == ' ')
print('floor cells:', floors, 'reachable:', len(seen))
assert len(seen) == floors, f'{floors - len(seen)} floor cells unreachable'

spawns = [(4, 5), (19, 5), (4, 18), (19, 18), (11, 3), (11, 20), (5, 9), (18, 14)]
for s in spawns:
    assert s in seen, s
print('spawn points OK >>>', spawns)
print()
print('JS array:')
print('const MAP = [')
for r in rows:
    print(f'  "{r}",')
print('];')
