from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    pg=b.new_page(viewport={"width":1200,"height":900})
    pg.goto(URL); pg.wait_for_timeout(300)
    for d in ["left","up","right","down","left"]:
        pg.evaluate("d=>window.__2048.move(d)", d); pg.wait_for_timeout(220)
    pg.wait_for_timeout(300)
    res=pg.evaluate("""() => {
      const cells=[...document.querySelectorAll('.cell')].map(e=>e.getBoundingClientRect());
      const tiles=[...document.querySelectorAll('.tile')];
      const mis=[];
      tiles.forEach(t=>{
        const v=t.querySelector('.val').textContent;
        const idx = (+getComputedStyle(t).getPropertyValue('--r'))*4 + (+getComputedStyle(t).getPropertyValue('--c'));
        const tb=$=t.getBoundingClientRect(); const cb=cells[idx];
        const dx=Math.abs(tb.x-cb.x), dy=Math.abs(tb.y-cb.y), dw=Math.abs(tb.width-cb.width);
        if (dx>0.6||dy>0.6||dw>0.8) mis.push({v,dx:+dx.toFixed(2),dy:+dy.toFixed(2),dw:+dw.toFixed(2)});
      });
      return {tiles:tiles.length, misalign:mis,
              overlap: tiles.length !== new Set(tiles.map(t=>getComputedStyle(t).getPropertyValue('--r')+','+getComputedStyle(t).getPropertyValue('--c'))).size};
    }""")
    print(res)

    # best score persistence
    pg.evaluate("window.__2048.newGame()"); pg.wait_for_timeout(200)
    pg.evaluate("(()=>{for(let i=0;i<8;i++) window.__2048.move(['left','up','right'][i%3]);})()")
    pg.wait_for_timeout(600)
    print("score now:", pg.evaluate("window.__2048.score"), "best text:", pg.inner_text("#best"))
    pg.reload(); pg.wait_for_timeout(400)
    print("after reload best:", pg.inner_text("#best"), "score:", pg.inner_text("#score"))
    pg.close(); b.close()
