from playwright.sync_api import sync_playwright
import os, time
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
errs=[]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.goto(URL); pg.wait_for_timeout(700)
    pg.screenshot(path="final_menu.png")
    pg.click("#startBtn"); pg.wait_for_timeout(1500)
    # 真实操作：走两步 + 开两枪 + 换弹 + 暂停 + 继续
    pg.keyboard.down("w"); pg.wait_for_timeout(500); pg.keyboard.up("w")
    pg.mouse.move(640,380); pg.mouse.down(); pg.wait_for_timeout(120); pg.mouse.up()
    pg.wait_for_timeout(300)
    pg.keyboard.press("KeyR"); pg.wait_for_timeout(300)
    pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    paused = pg.evaluate("document.getElementById('pauseScreen').classList.contains('hide')")
    pg.click("#resumeBtn"); pg.wait_for_timeout(400)
    pg.screenshot(path="final_play.png")
    # 帧率粗测
    fps = pg.evaluate("""() => new Promise(res => {
      let n = 0; const t0 = performance.now();
      function tick(){ n++; if (performance.now() - t0 < 1000) requestAnimationFrame(tick); else res(n); }
      requestAnimationFrame(tick);
    })""")
    print("fps ~", fps)
    print("pause overlay was visible:", not paused)
    print("ERRORS:", errs[:4])
    b.close()
