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
    pg.click("#startBtn"); pg.wait_for_timeout(600)
    # 清场，放一个敌人在正前方 4 格
    pg.evaluate("""() => {
      window.__CQB.teleport(11.5, 17.5);
      window.__CQB.aim(-Math.PI/2, 0);
    }""")
    pg.evaluate("window.__CQB.spawnAt(11.5, 13.2, 'grunt')")
    pg.wait_for_timeout(700)
    pg.screenshot(path="e1_front.png")
    # 更近
    pg.evaluate("window.__CQB.spawnAt(11.5, 15.5, 'heavy')")
    pg.wait_for_timeout(500)
    pg.screenshot(path="e2_near.png")
    # 开火测试
    pg.evaluate("window.__CQB.fire()")
    pg.wait_for_timeout(120)
    pg.screenshot(path="e3_shot.png")
    info = pg.evaluate("""() => {
      const c = window.__CQB;
      return { hp: c.hp, ammo: c.ammo, enemies: c.enemies, acc: c.accuracy, score: c.score };
    }""")
    print(info)
    print("errs:", errs[:4])
    b.close()
