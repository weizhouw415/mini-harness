from playwright.sync_api import sync_playwright
import os, json
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(500)
    pg.evaluate("window.__CQB.teleport(11.5,17.5); window.__CQB.aim(-Math.PI/2,0)")
    # 清掉所有敌人，只留一个在正前方
    pg.evaluate("""() => {
      const c = window.__CQB;
      c.enemies; // 读
      for (const e of c.enemies) {}
    }""")
    pg.evaluate("window.__CQB.spawnAt(11.5, 13.0, 'grunt')")
    pg.wait_for_timeout(900)
    rect = pg.evaluate("window.__CQB.spriteDebug()")
    print(json.dumps(rect, indent=1))
    pg.screenshot(path="aim_enemy.png")
    b.close()
