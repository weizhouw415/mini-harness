from playwright.sync_api import sync_playwright
import os, math
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
    # 站在走廊南端朝北看整个中央广场
    pg.evaluate("window.__CQB.teleport(11.5, 20.5); window.__CQB.aim(-Math.PI/2)")
    pg.wait_for_timeout(600)
    pg.screenshot(path="v_north.png")
    # 朝中央房间里的敌人看
    pg.evaluate("window.__CQB.teleport(11.5, 17.5); window.__CQB.aim(-Math.PI/2, 0.02)")
    pg.wait_for_timeout(600)
    pg.screenshot(path="v_plaza.png")
    # 贴近视角看墙纹理
    pg.evaluate("window.__CQB.teleport(11.5, 17.5); window.__CQB.aim(0, 0)")
    pg.wait_for_timeout(600)
    pg.screenshot(path="v_wall.png")
    print("errs:", errs[:4])
    b.close()
