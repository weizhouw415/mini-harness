from playwright.sync_api import sync_playwright
import os
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(400)
    pg.keyboard.press("Digit2"); pg.wait_for_timeout(200)
    print("after press 2:", pg.evaluate("window.__CQB.ammo.weapon"))
    pg.keyboard.press("Digit1"); pg.wait_for_timeout(200)
    print("after press 1:", pg.evaluate("window.__CQB.ammo.weapon"))
    pg.keyboard.press("KeyR"); pg.wait_for_timeout(200)
    print("reload via R:", pg.evaluate("window.__CQB.ammo.reloading"))
    b.close()
