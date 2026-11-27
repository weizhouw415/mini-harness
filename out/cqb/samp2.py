from playwright.sync_api import sync_playwright
import os, json
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(500)
    pg.evaluate("window.__CQB.teleport(11.5,18.5); window.__CQB.aim(-Math.PI/2,0)")
    pg.wait_for_timeout(600)
    # 直接读屏幕上真实像素
    px = pg.evaluate("""() => {
      const cv = document.getElementById('view');
      const g = cv.getContext('2d');
      const d = cv.width / 1280;
      const pts = [[640,20],[640,150],[640,300],[640,450],[640,700],[80,380],[1250,380]];
      return pts.map(([x,y]) => {
        const p = g.getImageData(Math.floor(x*d), Math.floor(y*d), 1, 1).data;
        return [x, y, p[0], p[1], p[2]];
      });
    }""")
    for r in px: print(r)
    print(json.dumps(pg.evaluate("({iw:window.__CQB.sample().iw, ih:window.__CQB.sample().ih})")))
    b.close()
