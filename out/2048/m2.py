from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    pg=b.new_page(viewport={"width":390,"height":844}, device_scale_factor=3)
    pg.goto(URL); pg.wait_for_timeout(400)
    r=pg.evaluate("""()=>{
      const a=document.querySelector('.app').getBoundingClientRect();
      const bd=document.querySelector('.board').getBoundingClientRect();
      const body=document.body.getBoundingClientRect();
      return {vh:innerHeight, bodyTop:body.top, bodyH:body.height,
              appTop:a.top, appH:a.height, boardTop:bd.top,
              cs:getComputedStyle(document.body).display,
              ai:getComputedStyle(document.body).alignItems,
              ih:getComputedStyle(document.documentElement).height};
    }""")
    print(r)
    pg.close(); b.close()
