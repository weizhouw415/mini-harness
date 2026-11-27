from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    pg=b.new_page(viewport={"width":1200,"height":900})
    pg.goto(URL); pg.wait_for_timeout(300)
    box=pg.locator("#board").bounding_box()
    cx,cy=box["x"]+box["width"]/2, box["y"]+box["height"]/2
    seen={}
    for attempt in range(6):
        for name,(dx,dy) in {"left":(-110,0),"right":(110,0),"up":(0,-110),"down":(0,110)}.items():
            g0=pg.evaluate("window.__2048.grid")
            pg.mouse.move(cx,cy); pg.mouse.down()
            pg.mouse.move(cx+dx*0.5, cy+dy*0.5, steps=4)
            pg.mouse.move(cx+dx, cy+dy, steps=4)
            pg.mouse.up(); pg.wait_for_timeout(320)
            changed = g0 != pg.evaluate("window.__2048.grid")
            seen.setdefault(name, False)
            seen[name] = seen[name] or changed
            pg.evaluate("window.__2048.newGame()"); pg.wait_for_timeout(250)
    print("drag directions that produced moves:", seen)
    # exact direction correctness: drag right must not shift tiles leftwards
    pg.evaluate("window.__2048.newGame()"); pg.wait_for_timeout(250)
    for _ in range(3):
        g0=pg.evaluate("window.__2048.grid")
        pg.mouse.move(cx,cy); pg.mouse.down(); pg.mouse.move(cx+120,cy,steps=6); pg.mouse.up()
        pg.wait_for_timeout(320)
        g1=pg.evaluate("window.__2048.grid")
        print("right drag: max_c before/after:", max(t['c'] for t in g0), max(t['c'] for t in g1))
        pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(280)
    pg.close(); b.close()
