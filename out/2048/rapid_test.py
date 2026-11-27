from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    pg=b.new_page(viewport={"width":1200,"height":900})
    errs=[]; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_timeout(300)
    pg.evaluate("window.__2048.newGame()"); pg.wait_for_timeout(250)
    # 5 rapid presses, no waiting
    for k in ["ArrowLeft","ArrowUp","ArrowLeft","ArrowDown","ArrowRight"]:
        pg.keyboard.press(k)
    pg.wait_for_timeout(1200)
    print("grid valid after burst:", pg.evaluate("""()=>{const g=window.__2048.grid;
        const cs=g.map(t=>t.r+','+t.c);
        return {n:g.length, dup:cs.length!==new Set(cs).size, pow2:g.every(t=>t.v>=2 && (t.v&(t.v-1))===0)};}"""))
    print("errs:", errs)
    # after burst, game still responds
    g1=pg.evaluate("window.__2048.grid")
    pg.keyboard.press("ArrowLeft"); pg.wait_for_timeout(350)
    print("still responsive:", g1 != pg.evaluate("window.__2048.grid"))
    # undo chain: 3 moves then 3 undos returns to start
    pg.evaluate("window.__2048.newGame()"); pg.wait_for_timeout(250)
    start=sorted(pg.evaluate("window.__2048.grid"), key=lambda t:(t['r'],t['c']))
    for k in ["ArrowLeft","ArrowDown","ArrowRight","ArrowUp"]:
        pg.keyboard.press(k); pg.wait_for_timeout(320)
    for _ in range(4):
        pg.click("#undo"); pg.wait_for_timeout(300)
    end=sorted(pg.evaluate("window.__2048.grid"), key=lambda t:(t['r'],t['c']))
    print("full undo chain restored:", start==end, "score:", pg.evaluate("window.__2048.score"))
    pg.close(); b.close()
