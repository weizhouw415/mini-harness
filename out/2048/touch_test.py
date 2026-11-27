from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    ctx=b.new_context(viewport={"width":390,"height":844}, has_touch=True, is_mobile=True, device_scale_factor=3)
    pg=ctx.new_page()
    pg.goto(URL); pg.wait_for_timeout(400)
    box=pg.locator("#board").bounding_box()
    cx,cy=box["x"]+box["width"]/2, box["y"]+box["height"]/2
    before=pg.evaluate("window.__2048.grid")
    # swipe left with touch events via CDP-ish touchscreen API
    pg.touchscreen.tap(cx, cy)  # sanity: tap shouldn't move
    pg.wait_for_timeout(250)
    print("tap changed board:", before != pg.evaluate("window.__2048.grid"))
    # simulate swipe using touchscreen: playwright lacks swipe; use pointer events manually
    pg.evaluate("""([cx,cy]) => {
      const b=document.getElementById('board');
      const opt=(x,y)=>({clientX:x, clientY:y, pointerId:7, pointerType:'touch', isPrimary:true, bubbles:true, cancelable:true});
      b.dispatchEvent(new PointerEvent('pointerdown', opt(cx, cy)));
      for (let i=1;i<=6;i++) b.dispatchEvent(new PointerEvent('pointermove', opt(cx - i*20, cy)));
      b.dispatchEvent(new PointerEvent('pointerup', opt(cx-120, cy)));
    }""", [cx, cy])
    pg.wait_for_timeout(400)
    print("swipe left changed board:", before != pg.evaluate("window.__2048.grid"))
    print("page scrolled:", pg.evaluate("window.scrollY"))
    ctx.close(); b.close()
