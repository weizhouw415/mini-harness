from playwright.sync_api import sync_playwright
import os, json, math
URL = "file://" + os.path.abspath("index.html")
EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
errs=[]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    pg = b.new_page(viewport={"width":1280,"height":760})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.goto(URL); pg.wait_for_timeout(600)
    pg.click("#startBtn"); pg.wait_for_timeout(500)
    print("state:", pg.evaluate("window.__CQB.state"), "wave:", pg.evaluate("window.__CQB.wave"))

    # 1) 命中+击杀+计分
    pg.evaluate("window.__CQB.teleport(11.5,18.5)")
    pg.evaluate("window.__CQB.spawnAt(11.5,15.0,'grunt')")
    pg.wait_for_timeout(600)
    for i in range(4):
        e = pg.evaluate("""() => { const c=window.__CQB; const cam=c.cam;
            const es=c.enemies.filter(e=>e.alive); if(!es.length) return null;
            let best=null,bd=1e9;
            for(const e of es){const d=Math.hypot(e.x-cam.x,e.y-cam.y); if(d<bd){bd=d;best={x:e.x,y:e.y,hp:e.hp};}}
            return best;}""")
        if not e or e["hp"]<=0: break
        pg.evaluate("y=>window.__CQB.aim(y,0)", math.atan2(e["y"]-18.5, e["x"]-11.5))
        pg.wait_for_timeout(60); pg.evaluate("window.__CQB.fire()"); pg.wait_for_timeout(130)
    print("kill/score:", pg.evaluate("({k:window.__CQB.kills, s:window.__CQB.score})"))

    # 2) 换弹
    pg.evaluate("window.__CQB.setAmmo(3)")
    pg.evaluate("window.__CQB.fire()"); pg.wait_for_timeout(150)
    pg.evaluate("window.__CQB.fire()"); pg.wait_for_timeout(150)
    pg.evaluate("window.__CQB.fire()"); pg.wait_for_timeout(150)
    print("auto-reload started:", pg.evaluate("window.__CQB.ammo.reloading") > 0)
    pg.wait_for_timeout(2400)
    print("ammo after reload:", pg.evaluate("window.__CQB.ammo.ak"))

    # 3) 切枪
    pg.evaluate("window.__CQB.key('Digit2', true)"); pg.wait_for_timeout(120)
    pg.evaluate("window.__CQB.key('Digit2', false)")
    print("weapon:", pg.evaluate("window.__CQB.ammo.weapon"))
    pg.evaluate("window.__CQB.fire()"); pg.wait_for_timeout(120)
    print("sg mag:", pg.evaluate("window.__CQB.ammo.sg"))

    # 4) 移动+碰撞（朝墙走不应穿墙）
    pg.evaluate("window.__CQB.teleport(11.5,18.5); window.__CQB.aim(Math.PI,0)")
    pg.evaluate("window.__CQB.key('KeyW',true)")
    pg.evaluate("window.__CQB.tick(90)")
    pg.evaluate("window.__CQB.key('KeyW',false)")
    cam = pg.evaluate("window.__CQB.cam")
    print("moved west, cam:", round(cam["x"],2), round(cam["y"],2), "(x should stay >=1.26)")

    # 5) 暂停/继续
    pg.evaluate("window.__CQB.pause()"); pg.wait_for_timeout(200)
    print("paused:", pg.evaluate("window.__CQB.state"))
    pg.evaluate("window.__CQB.resume()"); pg.wait_for_timeout(200)
    print("resumed:", pg.evaluate("window.__CQB.state"))

    # 6) 受伤+死亡+重开
    pg.evaluate("window.__CQB.hurt(200)"); pg.wait_for_timeout(300)
    print("over state:", pg.evaluate("window.__CQB.state"), "hp:", pg.evaluate("window.__CQB.hp"))
    pg.screenshot(path="w_over.png")
    pg.click("#againBtn"); pg.wait_for_timeout(400)
    print("restarted:", pg.evaluate("window.__CQB.state"), "wave:", pg.evaluate("window.__CQB.wave"), "hp:", pg.evaluate("window.__CQB.hp"))

    # 7) 波次推进
    pg.evaluate("window.__CQB.killAll()")
    pg.evaluate("window.__CQB.tick(120)")
    pg.wait_for_timeout(300)
    print("wave advanced:", pg.evaluate("window.__CQB.wave"))
    print("ERRORS:", errs[:5])
    b.close()
