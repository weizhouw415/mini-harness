import json, random, asyncio
from playwright.sync_api import sync_playwright

URL = "file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
errors = []

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    pg = b.new_page(viewport={"width": 520, "height": 900})
    pg.on("console", lambda m: errors.append(("console", m.type, m.text)) if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(("pageerror", str(e))))
    pg.goto(URL)
    pg.wait_for_timeout(400)

    def state():
        return pg.evaluate("({g: window.__2048.grid, s: window.__2048.score, over: window.__2048.over})")

    # deterministic-ish random moves, validate invariants
    bad = []
    dirs = ["left", "right", "up", "down"]
    prev_score = 0
    for i in range(300):
        st = state()
        g = st["g"]
        # invariant: no duplicate coords, all in range, values powers of 2 >=2
        coords = [(t["r"], t["c"]) for t in g]
        if len(coords) != len(set(coords)): bad.append(("dup", i, g))
        for t in g:
            if not (0 <= t["r"] < 4 and 0 <= t["c"] < 4): bad.append(("range", i, g))
            if t["v"] < 2 or (t["v"] & (t["v"] - 1)) != 0: bad.append(("pow2", i, g))
        if st["over"]: break
        pg.evaluate("d => window.__2048.move(d)", random.choice(dirs))
        pg.wait_for_timeout(200)
        st2 = state()
        if st2["s"] < prev_score: bad.append(("score-decreased", i))
        prev_score = st2["s"]

    final = state()
    print("moves ok, final score:", final["s"], "tiles:", len(final["g"]), "over:", final["over"])
    print("failures:", bad[:3], "count", len(bad))
    print("console issues:", errors[:5])

    # undo test
    pg.evaluate("window.__2048.newGame()")
    pg.wait_for_timeout(300)
    before = state()
    pg.evaluate("window.__2048.move('left')")
    pg.wait_for_timeout(300)
    mid = state()
    pg.evaluate("window.__2048.undo()")
    pg.wait_for_timeout(300)
    after = state()
    print("undo restores grid:", sorted(map(str,after["g"])) == sorted(map(str,before["g"])), "score", after["s"])

    # drag test
    pg.evaluate("window.__2048.newGame()")
    pg.wait_for_timeout(300)
    box = pg.locator("#board").bounding_box()
    cx, cy = box["x"]+box["width"]/2, box["y"]+box["height"]/2
    s0 = state()
    pg.mouse.move(cx, cy); pg.mouse.down(); pg.mouse.move(cx-120, cy, steps=6); pg.mouse.up()
    pg.wait_for_timeout(300)
    print("drag moved:", state()["g"] != s0["g"])
    b.close()
