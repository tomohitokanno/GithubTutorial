#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, async_playwright

JST = ZoneInfo("Asia/Tokyo")
PREF_URLS = {
    "東京": "https://www.carsensor.net/usedcar/tokyo/index.html",
    "千葉": "https://www.carsensor.net/usedcar/chiba/index.html",
    "埼玉": "https://www.carsensor.net/usedcar/saitama/index.html",
    "神奈川": "https://www.carsensor.net/usedcar/kanagawa/index.html",
    "茨城": "https://www.carsensor.net/usedcar/ibaraki/index.html",
}
DETAIL_RE = re.compile(r"/usedcar/detail/([A-Z0-9]+)/index\.html", re.I)


def compact(s: str | None) -> str:
    return re.sub(r"\s+", "", s or "").replace("１", "1").replace("２", "2")


def clean_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def money_man(text: str) -> float | None:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*万円", text or "")
    return float(m.group(1)) if m else None


def mileage_10k(text: str) -> float | None:
    s = compact(text).lower()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)万km", s)
    if m:
        return float(m.group(1))
    m = re.search(r"([0-9,]+)km", s)
    if m:
        return int(m.group(1).replace(",", "")) / 10000
    return None


def inspection_ym(text: str) -> tuple[int, int] | None:
    s = compact(text)
    m = re.search(r"(20\d{2})(?:\([^)]*\))?年(\d{1,2})月", s)
    return (int(m.group(1)), int(m.group(2))) if m else None


def first(patterns: list[str], text: str) -> str:
    for p in patterns:
        m = re.search(p, text, re.S)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def table_map(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for tr in soup.select("tr"):
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        vals = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in cells]
        for i in range(0, len(vals) - 1, 2):
            if vals[i] and vals[i + 1] and len(vals[i]) < 80:
                out.setdefault(vals[i], vals[i + 1])
    return out


def rgb(s: str) -> tuple[int, int, int] | None:
    m = re.search(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", s or "")
    return tuple(int(m.group(i)) for i in range(1, 4)) if m else None


def orange(s: str) -> bool:
    x = rgb(s)
    if not x:
        return False
    r, g, b = x
    return r >= 140 and r > g + 25 and g >= b - 10 and r - min(x) >= 45


async def choose_select(page: Page, context: str, labels: list[str], *, prefer_last=False, exclude: list[str] | None = None) -> dict[str, Any]:
    return await page.evaluate(
        r"""
        ({context, labels, preferLast, exclude}) => {
          const c=s=>(s||'').replace(/\s+/g,'');
          const wanted=labels.map(c); const cand=[];
          [...document.querySelectorAll('select')].forEach((sel, idx)=>{
            const opts=[...sel.options].filter(o=>wanted.includes(c(o.textContent)));
            if(!opts.length) return;
            let n=sel.parentElement, best=null;
            for(let d=0;n&&d<10;d++,n=n.parentElement){
              const t=c(n.innerText||n.textContent);
              if(!t.includes(c(context))) continue;
              if((exclude||[]).some(x=>t.includes(c(x)))) continue;
              const score=10000-Math.min(t.length,10000)-d*10;
              if(!best||score>best.score) best={score,text:t};
            }
            if(best) cand.push({sel,idx,opt:opts[0],best});
          });
          if(!cand.length) return {ok:false, context, reason:'not_found'};
          cand.sort((a,b)=>b.best.score-a.best.score||a.idx-b.idx);
          const top=cand[0].best.score; const same=cand.filter(x=>x.best.score===top);
          const x=preferLast?same[same.length-1]:same[0];
          x.sel.value=x.opt.value;
          x.sel.dispatchEvent(new Event('input',{bubbles:true}));
          x.sel.dispatchEvent(new Event('change',{bubbles:true}));
          return {ok:true, context, name:x.sel.name, label:c(x.opt.textContent), value:x.opt.value};
        }
        """,
        {"context": context, "labels": labels, "preferLast": prefer_last, "exclude": exclude or []},
    )


async def choose_checkbox(page: Page, label: str) -> dict[str, Any]:
    return await page.evaluate(
        r"""
        ({label}) => {
          const c=s=>(s||'').replace(/\s+/g,''); const target=c(label); const found=[];
          for(const lab of document.querySelectorAll('label')){
            if(c(lab.innerText||lab.textContent)!==target) continue;
            let i=lab.htmlFor?document.getElementById(lab.htmlFor):lab.querySelector('input');
            if(!i&&lab.parentElement) i=lab.parentElement.querySelector('input[type=checkbox],input[type=radio]');
            if(i) found.push(i);
          }
          if(!found.length){
            for(const e of document.querySelectorAll('span,div,li,dt,dd')){
              if(c(e.innerText||e.textContent)!==target) continue;
              const i=e.parentElement?.querySelector('input[type=checkbox],input[type=radio]'); if(i) found.push(i);
            }
          }
          const i=found.find(x=>x.offsetParent!==null)||found[0];
          if(!i) return {ok:false,label,reason:'not_found'};
          if(!i.checked){i.click();}
          return {ok:true,label,name:i.name,value:i.value,checked:i.checked};
        }
        """,
        {"label": label},
    )


async def configure(page: Page, url: str) -> dict[str, Any]:
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(1500)
    steps = [
        await choose_select(page, "走行距離", ["5万Km", "5万km"], prefer_last=True),
        await choose_select(page, "価格", ["100万円"], prefer_last=True, exclude=["ローン月々支払い価格", "ローン頭金"]),
        await choose_select(page, "排気量", ["1000", "1000(1.0L)"], prefer_last=False),
        await choose_select(page, "車検残", ["1年以上", "１年以上"], prefer_last=False),
        await choose_checkbox(page, "ガソリン"),
        await choose_checkbox(page, "スマートキー"),
        await choose_checkbox(page, "バックカメラ"),
    ]
    if not all(x.get("ok") for x in steps):
        return {"ok": False, "url": page.url, "steps": steps}
    before = page.url
    submit = await page.evaluate(
        r"""
        () => { const c=s=>(s||'').replace(/\s+/g,'');
          const els=[...document.querySelectorAll('button,input[type=submit],a')].filter(e=>c(e.innerText||e.value||e.textContent)==='検索する'&&e.offsetParent!==null);
          if(!els.length) return {ok:false};
          const scored=els.map((e,i)=>{const f=e.closest('form');let s=0;if(f){const t=c(f.innerText||f.textContent);if(t.includes('スマートキー'))s+=10;if(t.includes('バックカメラ'))s+=10;if([...f.querySelectorAll('input')].some(x=>x.checked))s+=10;}return {e,i,s};}).sort((a,b)=>b.s-a.s||a.i-b.i);
          const x=scored[0].e; const f=x.closest('form'); if(f&&f.requestSubmit&&x.tagName!=='A')f.requestSubmit(x);else x.click(); return {ok:true}; }
        """
    )
    if not submit.get("ok"):
        return {"ok": False, "url": page.url, "steps": steps, "submit": submit}
    try:
        await page.wait_for_url(lambda u: u != before, timeout=45000)
    except Exception:
        pass
    await page.wait_for_load_state("domcontentloaded", timeout=90000)
    await page.wait_for_timeout(1800)
    body = await page.locator("body").inner_text()
    counts = [int(x.replace(",", "")) for x in re.findall(r"([0-9][0-9,]*)台", body)]
    return {"ok": True, "url": page.url, "steps": steps, "count": min(counts) if counts else None}


async def result_urls(page: Page, max_pages=100) -> tuple[list[str], list[dict[str, Any]]]:
    seen: set[str] = set(); out: list[str] = []; pages=[]
    for n in range(1, max_pages + 1):
        await page.wait_for_load_state("domcontentloaded", timeout=90000)
        await page.wait_for_timeout(700)
        hrefs = await page.locator('a[href*="/usedcar/detail/"]').evaluate_all("es=>es.map(e=>e.href)")
        added=0
        for h in hrefs:
            u=clean_url(h)
            if DETAIL_RE.search(u) and u not in seen:
                seen.add(u); out.append(u); added+=1
        pages.append({"page":n,"url":page.url,"links":len(hrefs),"new":added})
        nxt = await page.evaluate(r"""()=>{const c=s=>(s||'').replace(/\s+/g,'');return [...document.querySelectorAll('a')].find(a=>c(a.innerText||a.textContent)==='次へ'&&a.offsetParent!==null&&a.href)?.href||null}""")
        if not nxt or clean_url(nxt)==clean_url(page.url): break
        await page.goto(nxt, wait_until="domcontentloaded", timeout=90000)
    return out,pages


async def equipment_rows(page: Page) -> list[dict[str, Any]]:
    return await page.evaluate(
        r"""()=>{const root=document.querySelector('#sec-soubi')||[...document.querySelectorAll('h2,h3')].find(x=>(x.innerText||'').includes('装備仕様'))?.parentElement;if(!root)return[];const c=s=>(s||'').replace(/\s+/g,' ').trim();const a=[];for(const e of root.querySelectorAll('li,dd,dt,span,a,p')){const t=c(e.innerText||e.textContent);if(!t||t.length>90)continue;const s=getComputedStyle(e);a.push({text:t,color:s.color,className:String(e.className||''),icon:[...e.querySelectorAll('i,span')].map(x=>String(x.className||'')).join(' '),checked:!!e.querySelector('input:checked')});}return a;}"""
    )


def active_set(rows: list[dict[str, Any]]) -> set[str]:
    out=set()
    for r in rows:
        cls=(r.get("className","")+" "+r.get("icon","")).lower()
        if any(x in cls for x in ["disabled","equip-off","is-off","ico_no","icon-no"]): continue
        yes=any(x in cls for x in ["active","checked","enabled","equip-on","is-on","ico_yes","icon-yes"])
        if yes or r.get("checked") or orange(r.get("color","")):
            out.add(compact(r.get("text","")))
    return out


def has(active: set[str], words: list[str]) -> bool:
    return any(any(compact(w) in x for w in words) for x in active)


async def parse_detail(ctx: BrowserContext, url: str, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        p=await ctx.new_page()
        try:
            await p.goto(url, wait_until="domcontentloaded", timeout=90000)
            await p.wait_for_timeout(1000)
            body=await p.locator("body").inner_text(); html=await p.content(); mp=table_map(html)
            h1=(await p.locator("h1").first.inner_text()).strip() if await p.locator("h1").count() else ""
            ended=any(x in body for x in ["掲載期間終了","掲載を終了","Sold Out","販売終了"])
            inquiry=any(x in body for x in ["在庫確認・見積依頼をする","無料在庫確認・見積依頼","来店予約をする"])
            rows=await equipment_rows(p); active=active_set(rows); tn=compact(h1)
            bp_txt=first([r"車両本体価格(?:（税込）)?\s*([0-9.]+\s*万円)"],body)
            total_txt=first([r"支払総額(?:（税込）)?\s*([0-9.]+\s*万円)"],body)
            year=first([r"年式\s*(20\d{2}(?:\([^)]*\))?)"],body)
            mile=first([r"走行距離\s*([0-9.]+\s*万km)",r"走行距離\s*([0-9,]+\s*km)"],body)
            insp=mp.get("車検","") or first([r"車検有無\s*((?:20\d{2}).{0,20}?年\s*\d{1,2}\s*月)",r"車検\s*((?:20\d{2}).{0,20}?年\s*\d{1,2}\s*月)"],body)
            region=first([r"地域\s*([^\n]+)"],body)
            address=first([r"住所：\s*([^\n]+)"],body)
            dealer=first([r"販売店名：\s*([^\n]+)"],body)
            repair=first([r"修復歴\s*(あり|なし|無し|無)"],body)
            maintenance=first([r"法定整備\s*([^\n]+)"],body)
            warranty=first([r"保証\s*([^\n]+)"],body)
            disp_txt=mp.get("排気量",""); m=re.search(r"([0-9,]+)\s*cc",disp_txt,re.I); disp=int(m.group(1).replace(",","")) if m else None
            engine=mp.get("エンジン種別","")
            smart="スマートキー" in tn or has(active,["スマートキー"])
            back=any(x in tn for x in ["バックカメラ","リアカメラ","Bカメラ"]) or has(active,["バックカメラ","リアカメラ"])
            if "バック" in next((v for k,v in mp.items() if compact(k).startswith("カメラ")),""): back=True
            iy=inspection_ym(insp); reasons=[]
            if ended: reasons.append("listing_ended")
            if not inquiry: reasons.append("inquiry_unavailable")
            if money_man(bp_txt) is None or money_man(bp_txt)>100: reasons.append("body_price")
            if mileage_10k(mile) is None or mileage_10k(mile)>5: reasons.append("mileage")
            if disp is None or disp<1000: reasons.append("displacement")
            if compact(engine)!="ガソリン": reasons.append("engine")
            if not smart: reasons.append("smart_key")
            if not back: reasons.append("back_camera")
            if iy is None or iy[0]*12+iy[1] < 2027*12+8: reasons.append("inspection")
            if not any(x in region+address for x in PREF_URLS): reasons.append("region")
            eq=lambda ws: has(active,ws)
            camera="全周囲" if eq(["全周囲カメラ"]) else ("バック" if back else ("フロント" if eq(["フロントカメラ"]) else "✕"))
            tv="フルセグ" if eq(["フルセグ"]) else ("ワンセグ" if eq(["ワンセグ"]) else ("TV" if eq(["テレビ","TV"]) else "✕"))
            video="DVD" if eq(["DVD"]) else ("ブルーレイ" if eq(["ブルーレイ","Blu-ray"]) else "✕")
            audio=[]
            for label,words in [("CD",["CD"]),("Bluetooth",["Bluetooth"]),("USB",["USB"]),("ミュージックサーバー",["ミュージックサーバー"]),("SD",["SD"])]:
                if eq(words): audio.append(label)
            headlights="LED" if eq(["LEDヘッドライト","LEDライト"]) else ("HID" if eq(["ディスチャージ","HID"]) else "ハロゲン")
            return {
                "id": (DETAIL_RE.search(url).group(1) if DETAIL_RE.search(url) else ""),"url":clean_url(p.url),"title":h1,
                "live":not ended and inquiry,"eligible":not reasons,"reasons":reasons,
                "body_price_man":money_man(bp_txt),"total_price_man":money_man(total_txt),"year":year,"mileage_text":mile,"mileage_10k":mileage_10k(mile),
                "inspection_text":insp,"inspection_ym":list(iy) if iy else None,"repair_history":repair,"maintenance":maintenance,"warranty":warranty,
                "region":region,"address":address,"dealer":dealer,"body_type":mp.get("ボディタイプ",""),"color":mp.get("色",""),"displacement_cc":disp,
                "engine":engine,"drive":mp.get("駆動方式",""),"mission":mp.get("ミッション",""),"capacity":mp.get("乗車定員",""),"doors":mp.get("ドア数",""),
                "smoking":mp.get("禁煙車",""),"recycle":mp.get("リサイクル料",""),"smart_key":smart,"back_camera":back,
                "cruise":"◯" if eq(["クルーズコントロール"]) else "✕","parking_assist":"◯" if eq(["パーキングアシスト"]) else "✕",
                "obstacle":"◯" if eq(["障害物センサー","クリアランスソナー"]) else "✕","camera":camera,"around_camera":"◯" if eq(["全周囲カメラ"]) else "✕",
                "collision_brake":"◯" if eq(["衝突被害軽減ブレーキ"]) else "✕","tv":tv,"video":video,"audio":"/".join(audio) if audio else "✕",
                "navigation":"◯" if eq(["カーナビ","メモリーナビ","HDDナビ","SDナビ"]) or "ナビ" in tn else "✕",
                "music_player":"◯" if eq(["Bluetooth","USB","ミュージックプレイヤー接続"]) else "✕","etc":"◯" if eq(["ETC"]) or "ETC" in h1 else "✕",
                "drive_recorder":"◯" if eq(["ドライブレコーダー","ドラレコ"]) or "ドラレコ" in h1 else "✕","display_audio":"◯" if eq(["ディスプレイオーディオ"]) else "✕",
                "leather":"◯" if eq(["本革シート","レザーシート"]) else "✕","headlights":headlights,
                "active_equipment":sorted(active),"table_mapping":mp,
            }
        except Exception as e:
            return {"url":url,"live":False,"eligible":False,"reasons":["fetch_error"],"error":f"{type(e).__name__}: {e}"}
        finally:
            await p.close()


async def main() -> None:
    cfg=json.loads(Path("config.json").read_text(encoding="utf-8")); prefs=cfg["prefectures"]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        ctx=await browser.new_context(locale="ja-JP",timezone_id="Asia/Tokyo",viewport={"width":1440,"height":1000},user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
        ctx.set_default_timeout(45000); search=await ctx.new_page(); urls=[]; seen=set(); summaries=[]; fatal=False
        for pref in prefs:
            setup=await configure(search,PREF_URLS[pref])
            if not setup.get("ok") or (setup.get("count") is not None and setup["count"]>500):
                fatal=True; summaries.append({"prefecture":pref,"setup":setup,"pages":[]}); continue
            us,pages=await result_urls(search)
            new=[]
            for u in us:
                if u not in seen: seen.add(u);urls.append(u);new.append(u)
            summaries.append({"prefecture":pref,"setup":setup,"pages":pages,"url_count":len(new)})
        details=[]
        if not fatal:
            sem=asyncio.Semaphore(int(cfg.get("detail_concurrency",3)))
            for i in range(0,len(urls),25):
                details.extend(await asyncio.gather(*(parse_detail(ctx,u,sem) for u in urls[i:i+25])))
                await asyncio.sleep(0.5)
        await search.close();await ctx.close();await browser.close()
    eligible=[x for x in details if x.get("eligible")]
    payload={"scanned_at":datetime.now(JST).isoformat(),"fatal":fatal,"criteria":cfg,"searches":summaries,"result_url_count":len(urls),"detail_count":len(details),"eligible_count":len(eligible),"eligible":eligible,"rejected":[x for x in details if not x.get("eligible")]}
    Path("output").mkdir(exist_ok=True);Path("output/result.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:payload[k] for k in ["scanned_at","fatal","result_url_count","detail_count","eligible_count"]},ensure_ascii=False))
    if fatal: raise SystemExit(2)

if __name__=="__main__": asyncio.run(main())
