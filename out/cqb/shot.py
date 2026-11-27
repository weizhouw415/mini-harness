from playwright.sync_api import sync_playwright
import os
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
errs=[]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.goto(URL); pg.wait_for_timeout(700)
    pg.click("#startBtn"); pg.wait_for_timeout(500)
    # 转向朝房间中心看
    pg.evaluate("window.__CQB.aim(-Math.PI/2)")
    pg.wait_for_timeout(500)
    pg.screenshot(path="s1_center.png")
    # 靠近一些敌人看视角
    pg.evaluate("window.__CQB.teleport(11.5, 12.5)")
    pg.wait_for_timeout(400)
    pg.screenshot(path="s2_close.png")
    print("errs:", errs[:5])
    b.close()
