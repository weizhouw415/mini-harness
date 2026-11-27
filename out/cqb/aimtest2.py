from playwright.sync_api import sync_playwright
import os, json, math
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(400)
    pg.evaluate("window.__CQB.teleport(11.5, 19.5)")
    pg.evaluate("window.__CQB.spawnAt(11.5, 16.5, 'grunt')")
    pg.wait_for_timeout(700)
    # 精确瞄准最近敌人
    for i in range(10):
        e = pg.evaluate("""() => {
          const c = window.__CQB; const cam = c.cam;
          const es = c.enemies.filter(e=>e.alive);
          if (!es.length) return null;
          let best=null, bd=1e9;
          for (const e of es) { const d=Math.hypot(e.x-cam.x, e.y-cam.y); if (d<bd){bd=d;best=e;} }
          return {yaw: Math.atan2(best.y-cam.y, best.x-cam.x), dist: bd, hp: best.hp};
        }""")
        if not e: break
        pg.evaluate("y => window.__CQB.aim(y, 0)", e["yaw"])
        pg.wait_for_timeout(80)
        pg.evaluate("window.__CQB.fire()")
        pg.wait_for_timeout(140)
        if e["hp"] <= 34: break
    st = pg.evaluate("""() => ({kills:window.__CQB.kills, score:window.__CQB.score,
                               acc:+window.__CQB.accuracy.toFixed(2), ammo:window.__CQB.ammo,
                               alive: window.__CQB.enemies.filter(e=>e.alive).length})""")
    print("result:", st)
    pg.screenshot(path="kill.png")
    b.close()
