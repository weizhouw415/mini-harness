from playwright.sync_api import sync_playwright
import random
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    pg=b.new_page(viewport={"width":1200,"height":900}, device_scale_factor=2)
    pg.goto(URL); pg.wait_for_timeout(300)
    dirs=["left","up","right","down"]
    for i in range(400):
        if pg.evaluate("window.__2048.over"): break
        pg.evaluate("d=>window.__2048.move(d)", random.choice(dirs))
        pg.wait_for_timeout(180)
        if i % 60 == 0:  # nudge progress toward a real 2048
            pass
    pg.wait_for_timeout(600)
    print("over:", pg.evaluate("window.__2048.over"),
          "overlay:", pg.evaluate("document.querySelector('.overlay').classList.contains('show')"),
          "title:", pg.evaluate("document.querySelector('#ovTitle').textContent"))
    pg.screenshot(path="shot_lose.png")
    pg.close(); b.close()
