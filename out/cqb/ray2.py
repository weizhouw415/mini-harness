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
    pg.wait_for_timeout(600)
    cam = pg.evaluate("window.__CQB.cam")
    print("cam:", cam)
    target = pg.evaluate("""() => { const c=window.__CQB; const es=c.enemies.filter(e=>e.alive);
        const cam=c.cam; let best=null,bd=1e9;
        for(const e of es){const d=Math.hypot(e.x-cam.x,e.y-cam.y); if(d<bd){bd=d;best={...e,d};}}
        return best;}""")
    print("nearest:", target)
    yaw = math.atan2(target["y"]-cam["y"], target["x"]-cam["x"])
    print("aim yaw:", yaw)
    print(json.dumps(pg.evaluate("y=>window.__CQB.rayTrace(y)", yaw), indent=1))
    b.close()
