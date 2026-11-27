from playwright.sync_api import sync_playwright
import os
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(300)
    pg.evaluate("window.__CQB.teleport(11.5, 17.5); window.__CQB.aim(-Math.PI/2, 0.02)")
    pg.wait_for_timeout(400)
    # 隐藏 gun canvas 看竖线是否消失
    pg.evaluate("document.getElementById('gun').style.display='none'")
    pg.wait_for_timeout(300)
    pg.screenshot(path="d_nogun.png")
    pg.evaluate("document.getElementById('gun').style.display=''")
    # 隐藏 hud
    pg.evaluate("document.querySelector('.hud').style.display='none'")
    pg.wait_for_timeout(300)
    pg.screenshot(path="d_nohud.png")
    b.close()
print("ok")
