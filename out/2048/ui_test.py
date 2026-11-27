from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)

    # --- 1. real keyboard play ---
    pg=b.new_page(viewport={"width":1200,"height":900})
    errs=[]; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_timeout(300)
    g0=pg.evaluate("window.__2048.grid")
    for key in ["ArrowLeft","ArrowUp","ArrowRight","ArrowDown"]:
        pg.keyboard.press(key); pg.wait_for_timeout(260)
    print("keyboard changed board:", g0 != pg.evaluate("window.__2048.grid"))
    # undo via U key
    pg.keyboard.press("u"); pg.wait_for_timeout(260)
    print("U undo ok:", pg.evaluate("document.querySelector('#undo').disabled") is False)
    # button undo disabled state after exhausting
    pg.keyboard.press("ArrowLeft"); pg.wait_for_timeout(250)
    pg.click("#undo"); pg.wait_for_timeout(250)
    print("undo button works, disabled now:", pg.evaluate("document.querySelector('#undo').disabled"))
    # new game button
    pg.click("#newGame"); pg.wait_for_timeout(250)
    print("new game tiles:", len(pg.evaluate("window.__2048.grid")), "score:", pg.evaluate("window.__2048.score"))

    # --- 2. tile font fit for huge values (DOM-injected, metrics only) ---
    pg.evaluate("""() => {
      const vals=[2,4,8,16,32,64,128,256,512,1024,2048,4096,8192,16384,32768,65536];
      const t=document.getElementById('tiles'); t.innerHTML='';
      vals.forEach((v,i)=>{ const r=Math.floor(i/4), c=i%4;
        const el=document.createElement('div'); el.className='tile';
        el.style.setProperty('--r',r); el.style.setProperty('--c',c);
        el.dataset.v = v<=8192? String(v):'super';
        el.dataset.digits=String(v).length;
        const s=document.createElement('span'); s.className='val'; s.textContent=v;
        el.appendChild(s); t.appendChild(el);
      });
    }""")
    pg.wait_for_timeout(200)
    over=pg.evaluate("[...document.querySelectorAll('.tile .val')].map((e,i)=>({i, over: e.scrollWidth> e.clientWidth+1, txt:e.textContent}))")
    print("tile text overflow:", [o for o in over if o["over"]])
    pg.screenshot(path="shot_huge.png")
    print("pageerrors:", errs)
    pg.close()
    b.close()
