from playwright.sync_api import sync_playwright
import os, math
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(400)
    pg.evaluate("window.__CQB.teleport(11.5, 18.5); window.__CQB.aim(-Math.PI/2, 0)")
    pg.evaluate("window.__CQB.spawnAt(11.5, 15.2, 'grunt')")
    pg.wait_for_timeout(900)
    pg.screenshot(path="f1.png")
    pg.evaluate("window.__CQB.spawnAt(11.5, 14.6, 'heavy')")
    pg.wait_for_timeout(400)
    pg.evaluate("window.__CQB.fire()")
    pg.wait_for_timeout(60)
    pg.screenshot(path="f2.png")
    b.close()
