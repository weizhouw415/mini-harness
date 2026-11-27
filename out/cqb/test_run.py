from playwright.sync_api import sync_playwright
import os, json
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
errs = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE, args=["--allow-file-access-from-files"])
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.on("pageerror", lambda e: errs.append(("pageerror", str(e))))
    pg.on("console", lambda m: errs.append(("console", m.type, m.text)) if m.type in ("error","warning") else None)
    pg.goto(URL)
    pg.wait_for_timeout(900)
    print("loaded, state:", pg.evaluate("window.__CQB && window.__CQB.state"))
    pg.screenshot(path="01_menu.png")
    # start game
    pg.click("#startBtn")
    pg.wait_for_timeout(1200)
    print("after start state:", pg.evaluate("window.__CQB.state"))
    print("wave:", pg.evaluate("window.__CQB.wave"), "enemies:", len(pg.evaluate("window.__CQB.enemies")))
    pg.screenshot(path="02_play.png")
    print("ERR:", errs[:8])
    b.close()
