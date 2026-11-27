from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    for name,vw,vh in [("desktop",1200,900),("mobile",390,844)]:
        pg=b.new_page(viewport={"width":vw,"height":vh}, device_scale_factor=2)
        pg.goto(URL); pg.wait_for_timeout(500)
        # play a few moves for a nicer board
        for d in ["left","up","right","down","left","up"]:
            pg.evaluate("d=>window.__2048.move(d)", d); pg.wait_for_timeout(220)
        pg.wait_for_timeout(400)
        pg.screenshot(path=f"shot_{name}.png")
        pg.close()
    b.close()
print("ok")
