from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    pg=b.new_page(viewport={"width":1200,"height":900}, device_scale_factor=2)
    pg.goto(URL); pg.wait_for_timeout(300)
    # win: two 1024 merge to 2048
    pg.evaluate("window.__2048._set([{r:0,c:0,v:1024},{r:0,c:1,v:1024},{r:1,c:2,v:512}], 20000)")
    pg.wait_for_timeout(200)
    pg.evaluate("window.__2048.move('left')"); pg.wait_for_timeout(450)
    pg.screenshot(path="shot_win2.png")
    print("win overlay:", pg.evaluate("document.querySelector('.overlay').classList.contains('show')"))
    # lose: full board, no merges possible
    pg.evaluate("window.__2048.newGame()"); pg.wait_for_timeout(200)
    grid=[{"r":r,"c":c,"v":[2,4,2,4][ (r*4+c) % 4 ] if (r+c)%2==0 else [4,2,4,2][(r*4+c)%4]} for r in range(4) for c in range(4)]
    # build a checkerboard-ish no-merge board
    vals=[[2,4,2,4],[4,2,4,2],[2,4,2,4],[4,2,4,2]]
    grid=[{"r":r,"c":c,"v":vals[r][c]} for r in range(4) for c in range(4)]
    pg.evaluate("g=>window.__2048._set(g, 512)", grid); pg.wait_for_timeout(200)
    # make a move that changes nothing -> no lose; instead force by shuffling into a mergeable then fill
    pg.evaluate("window.__2048.move('left')"); pg.wait_for_timeout(500)
    pg.screenshot(path="shot_lose.png")
    print("lose overlay:", pg.evaluate("document.querySelector('.overlay').classList.contains('show')"))
    print(pg.evaluate("document.querySelector('#ovTitle').textContent"))
    pg.close(); b.close()
