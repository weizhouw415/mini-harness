from playwright.sync_api import sync_playwright
import os
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    errs=[]; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(400)
    pg.evaluate("window.__CQB.teleport(11.5,18.5); window.__CQB.aim(-Math.PI/2,0)")
    pg.evaluate("window.__CQB.spawnAt(11.5, 15.0, 'grunt')")
    pg.wait_for_timeout(800)
    pg.screenshot(path="v1.png")
    print("errs:", errs[:3])
    b.close()
