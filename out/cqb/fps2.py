from playwright.sync_api import sync_playwright
import os
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE, args=["--disable-frame-rate-limit","--disable-gpu-vsync"])
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.goto(URL); pg.wait_for_timeout(500)
    pg.click("#startBtn"); pg.wait_for_timeout(800)
    fps = pg.evaluate("""() => new Promise(res => {
      let n=0; const t0=performance.now();
      function tick(){ n++; if(performance.now()-t0<1500) requestAnimationFrame(tick); else res(Math.round(n/1.5)); }
      requestAnimationFrame(tick);
    })""")
    print("unthrottled fps ~", fps)
    # 渲染一帧的耗时
    ms = pg.evaluate("""() => { const t0=performance.now(); for(let i=0;i<30;i++){ /* noop */ } return 0; }""")
    b.close()
