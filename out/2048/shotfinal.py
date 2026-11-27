from playwright.sync_api import sync_playwright
URL="file:///Users/weizhouw/workspace/claw/mini-harness/out/2048/index.html"
EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=EXE)
    pg=b.new_page(viewport={"width":390,"height":844}, device_scale_factor=3)
    pg.goto(URL); pg.wait_for_timeout(300)
    vals=[[2,4,8,16],[32,64,128,256],[512,1024,2048,4],[2,8,32,128]]
    pg.evaluate("""(vals)=>{const t=document.getElementById('tiles');t.innerHTML='';
      vals.flat().forEach((v,i)=>{const r=Math.floor(i/4),c=i%4;
        const el=document.createElement('div'); el.className='tile';
        el.style.setProperty('--r',r); el.style.setProperty('--c',c);
        el.dataset.v=v<=8192?String(v):'super'; el.dataset.digits=String(v).length;
        const s=document.createElement('span'); s.className='val'; s.textContent=v; el.appendChild(s); t.appendChild(el);});}""", vals)
    pg.wait_for_timeout(300)
    pg.screenshot(path="shot_mobile_final.png")
    pg.close(); b.close()
print("ok")
