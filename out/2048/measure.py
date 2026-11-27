from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    for vw,vh in [(390,844),(1200,900),(1440,780),(360,640),(768,1024)]:
        pg=b.new_page(viewport={"width":vw,"height":vh})
        pg.goto(URL); pg.wait_for_timeout(250)
        info=pg.evaluate("""() => {
          const r = (s)=>{const e=document.querySelector(s); const b=e.getBoundingClientRect();
            return {x:+b.x.toFixed(1), y:+b.y.toFixed(1), w:+b.width.toFixed(1), h:+b.height.toFixed(1), r:+b.right.toFixed(1), b:+b.bottom.toFixed(1)};};
          return {vw: innerWidth, vh: innerHeight, docW: document.documentElement.scrollWidth,
                  docH: document.documentElement.scrollHeight, bodyOverflowY: document.documentElement.scrollHeight>innerHeight,
                  app:r('.app'), board:r('.board'), header:r('header'), footer:r('footer')};
        }""")
        print(vw,vh, info)
        pg.close()
    b.close()
