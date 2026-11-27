from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
issues=[]
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    for vw,vh in [(390,844),(360,640),(1200,900),(1440,780)]:
        pg=b.new_page(viewport={"width":vw,"height":vh}, device_scale_factor=2)
        errs=[]
        pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
        pg.on("console", lambda m, errs=errs: errs.append(m.text) if m.type=="error" else None)
        pg.goto(URL); pg.wait_for_timeout(400)
        # no horizontal scroll & board in viewport
        r=pg.evaluate("""()=>{const b=document.querySelector('.board').getBoundingClientRect();
          return {sx:document.documentElement.scrollWidth, vw:innerWidth,
                  top:b.top, bottom:b.bottom, vh:innerHeight,
                  tileInk: [...document.querySelectorAll('.tile .val')].map(e=>e.scrollWidth>e.clientWidth+1)};}""")
        bad_ink = any(r["tileInk"])
        fits = r["top"] >= 0 and r["bottom"] <= r["vh"] + 0.5
        print(f"{vw}x{vh}: hscroll={r['sx']>r['vw']} boardFits={fits} tileOverflow={bad_ink} errs={errs}")
        if r["sx"]>r["vw"] or not fits or bad_ink or errs: issues.append((vw,vh))
        pg.close()
    b.close()
print("ISSUES:", issues)
