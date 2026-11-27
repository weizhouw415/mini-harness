from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BIG = [
 {"r":0,"c":0,"v":2},{"r":0,"c":1,"v":4},{"r":0,"c":2,"v":8},{"r":0,"c":3,"v":16},
 {"r":1,"c":0,"v":32},{"r":1,"c":1,"v":64},{"r":1,"c":2,"v":128},{"r":1,"c":3,"v":256},
 {"r":2,"c":0,"v":512},{"r":2,"c":1,"v":1024},{"r":2,"c":2,"v":2048},{"r":2,"c":3,"v":4096},
 {"r":3,"c":0,"v":8192},{"r":3,"c":1,"v":16384},{"r":3,"c":2,"v":32768},{"r":3,"c":3,"v":65536}]
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    pg=b.new_page(viewport={"width":1200,"height":900}, device_scale_factor=2)
    pg.goto(URL); pg.wait_for_timeout(400)
    pg.evaluate("g=>window.__2048._set(g, 1234)", BIG); pg.wait_for_timeout(400)
    pg.screenshot(path="shot_filled.png")
    # win overlay
    pg.evaluate("window.__2048._set([{r:0,c:0,v:2048},{r:0,c:1,v:1024}], 21000)"); pg.wait_for_timeout(200)
    pg.evaluate("window.__2048.move('left')"); pg.wait_for_timeout(500)
    pg.screenshot(path="shot_win.png")
    pg.close()

    pg=b.new_page(viewport={"width":390,"height":844}, device_scale_factor=2)
    pg.goto(URL); pg.wait_for_timeout(300)
    pg.evaluate("g=>window.__2048._set(g, 9999)", BIG); pg.wait_for_timeout(400)
    pg.screenshot(path="shot_mobile_filled.png")
    pg.close()
    b.close()
print("ok")
