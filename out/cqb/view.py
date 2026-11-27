from playwright.sync_api import sync_playwright
import os, sys
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
x, y, yaw = (float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])) if len(sys.argv)>3 else (11.5,18.5,-1.5707963)
out = sys.argv[4] if len(sys.argv)>4 else "shot.png"
errs=[]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(300)
    # 清场再做纯净取景（保留一个敌人可选）
    pg.evaluate("""([x,y,yaw]) => {
      const c = window.__CQB;
      c.teleport(x,y); c.aim(yaw, 0);
    }""", [x,y,yaw])
    pg.wait_for_timeout(500)
    pg.screenshot(path=out)
    print("errs:", errs[:3])
    b.close()
