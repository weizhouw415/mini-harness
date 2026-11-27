from playwright.sync_api import sync_playwright
import os
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(500)
    pg.evaluate("window.__CQB.teleport(11.5,17.5); window.__CQB.aim(-Math.PI/2,0)")
    pg.evaluate("window.__CQB.spawnAt(11.5, 13.0, 'grunt')")
    pg.wait_for_timeout(700)
    print("sprite rect:", pg.evaluate("window.__CQB.spriteDebug().slice(-1)[0]"))
    print(pg.evaluate("window.__CQB.probe(300, 80, 120, 160)"))
    b.close()
