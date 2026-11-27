from playwright.sync_api import sync_playwright
import os, json
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(400)
    # 走廊南侧，正北敞开视线
    pg.evaluate("window.__CQB.teleport(11.5, 19.5); window.__CQB.aim(-Math.PI/2, 0)")
    pg.evaluate("window.__CQB.spawnAt(11.5, 17.0, 'grunt')")
    pg.wait_for_timeout(800)
    d = pg.evaluate("window.__CQB.spriteDebug()")
    print("nearest sprite:", json.dumps(d[-1] if d else None))
    pg.screenshot(path="t1.png")
    # 连开三枪
    for i in range(3):
        pg.evaluate("window.__CQB.fire()"); pg.wait_for_timeout(130)
    st = pg.evaluate("""() => ({hp:window.__CQB.hp, ammo:window.__CQB.ammo, kills:window.__CQB.kills,
                               acc:window.__CQB.accuracy, score:window.__CQB.score,
                               enemies: window.__CQB.enemies.filter(e=>e.alive).length})""")
    print("after 3 shots:", st)
    pg.screenshot(path="t2.png")
    b.close()
