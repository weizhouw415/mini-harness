from playwright.sync_api import sync_playwright
import os
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(400)
    out = pg.evaluate("""() => {
      const c = window.__CQB;
      return { cam: c.cam, state: c.state };
    }""")
    print(out)
    # 直接读内部变量：通过暴露的调试口不够，改读 canvas 像素
    px = pg.evaluate("""() => {
      const cv = document.getElementById('view');
      const g = cv.getContext('2d');
      const pts = [[640,80],[640,200],[640,380],[640,560],[640,700],[100,380],[1180,380]];
      return pts.map(([x,y]) => {
        const d = g.getImageData(x*devicePixelRatio|0, y*devicePixelRatio|0, 1, 1).data;
        return [x,y,d[0],d[1],d[2]];
      });
    }""")
    for r in px: print(r)
    b.close()
