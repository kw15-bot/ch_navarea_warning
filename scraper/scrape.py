#!/usr/bin/env python3
"""
GitHub Actions で定期実行される中国海事局スクレイパー
出力: data/warnings.json
"""
import re, json, hashlib, time, logging
from datetime import datetime, timedelta
from typing import Optional
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── 対象局 ──────────────────────────────────────────────────
MSA_SOURCES = [
    {"id":"shanghai",  "name":"上海海事局",  "url":"https://www.msa.gov.cn/8dbded82f3e5413b824e51445c79726c/index.jhtml"},
    {"id":"zhejiang",  "name":"浙江海事局",  "url":"https://www.msa.gov.cn/dc8d821b39fb46908fd50924c86a7ac7/index.jhtml"},
    {"id":"fujian",    "name":"福建海事局",  "url":"https://www.msa.gov.cn/3d725583ac134dfcb74aa8c47b14a164/index.jhtml"},
    {"id":"guangdong", "name":"広東海事局",  "url":"https://www.msa.gov.cn/32fa3793394148f7b5c3ec112d2bf8af/index.jhtml"},
    {"id":"hainan",    "name":"海南海事局",  "url":"https://www.msa.gov.cn/5eb2863167464a6faaa1fca5bff0a2a9/index.jhtml"},
    {"id":"liaoning",  "name":"遼寧海事局",  "url":"https://www.msa.gov.cn/dc8d821b39fb46908fd50924c86a7ac7/index.jhtml"},
    {"id":"tianjin",   "name":"天津海事局",  "url":"https://www.msa.gov.cn/8dbded82f3e5413b824e51445c79726c/index.jhtml"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

ONE_MONTH_AGO = datetime.now() - timedelta(days=30)

# ── 座標パーサー ─────────────────────────────────────────────
def dms_to_dec(d, m, s, hemi):
    v = float(d) + float(m)/60 + float(s)/3600
    return round(-v if hemi.upper() in ("S","W","南","西") else v, 6)

COORD_RE = re.compile(
    r'(\d+)[°度]\s*(\d+)[′\'分]\s*([\d.]+)[″"秒]?\s*([NS南北])'
    r'[\s,，/]*'
    r'(\d+)[°度]\s*(\d+)[′\'分]\s*([\d.]+)[″"秒]?\s*([EW东東西])',
    re.UNICODE
)
COORD_DEC_RE = re.compile(
    r'([\d.]+)\s*°?\s*([NS南北])[\s,，]+?([\d.]+)\s*°?\s*([EW东東西])',
    re.UNICODE
)

def extract_coords(text):
    pts = []
    for m in COORD_RE.finditer(text):
        lat = dms_to_dec(m.group(1),m.group(2),m.group(3),m.group(4))
        lng = dms_to_dec(m.group(5),m.group(6),m.group(7),m.group(8))
        if 15<=lat<=42 and 105<=lng<=135:
            pts.append([lat,lng])
    if not pts:
        for m in COORD_DEC_RE.finditer(text):
            lat = float(m.group(1)) * (-1 if m.group(2).upper() in("S","南") else 1)
            lng = float(m.group(3)) * (-1 if m.group(4).upper() in("W","西") else 1)
            if 15<=lat<=42 and 105<=lng<=135:
                pts.append([round(lat,6),round(lng,6)])
    seen,uniq = set(),[]
    for c in pts:
        k = (round(c[0],3),round(c[1],3))
        if k not in seen:
            seen.add(k); uniq.append(c)
    return uniq

# ── タイプ判定 ───────────────────────────────────────────────
TYPE_KW = {
    "military": ["军事","演习","训练","射击","禁航","管控","警戒"],
    "dredging": ["疏浚","浚深","挖泥","清淤"],
    "cable":    ["海底电缆","光缆","电力电缆","敷设","埋设"],
    "platform": ["石油","钻井","平台","风电","风力","构筑物","防波堤"],
    "obstacle": ["沉船","打捞","清除","测量","调查","浮标","水雷"],
}
def classify(title, body):
    t = title + body
    for k,ws in TYPE_KW.items():
        if any(w in t for w in ws): return k
    return "hazard"

# ── 記事一覧取得 ─────────────────────────────────────────────
def fetch_list(src, sess):
    articles = []
    try:
        r = sess.get(src["url"], headers=HEADERS, timeout=20)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("ul li a, .list a, .news-list a"):
            href  = a.get("href","")
            title = a.get_text(strip=True)
            if len(title) < 5: continue
            if href.startswith("/"): href = "https://www.msa.gov.cn" + href
            elif not href.startswith("http"): continue
            articles.append({"url":href,"title":title,"source":src["name"]})
        log.info("[%s] %d件", src["name"], len(articles))
    except Exception as e:
        log.warning("[%s] 失敗: %s", src["name"], e)
    return articles

# ── 記事詳細取得 ─────────────────────────────────────────────
def fetch_detail(art, sess) -> Optional[dict]:
    try:
        r = sess.get(art["url"], headers=HEADERS, timeout=20)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        el = soup.select_one(".article-content,.content,#content,.TRS_Editor,article,main")
        body = el.get_text("\n",strip=True) if el else soup.get_text("\n",strip=True)

        # 日付
        dm = re.search(r'(\d{4})[年\-](\d{1,2})[月\-](\d{1,2})', body)
        date_str = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""
        if date_str:
            try:
                if datetime.strptime(date_str,"%Y-%m-%d") < ONE_MONTH_AGO:
                    return None
            except: pass

        coords = extract_coords(body)
        if len(coords) < 2: return None

        # 通知番号
        nm = re.search(r'[〔（(][\d年〔）]+[〕）)]\s*\d+\s*号', body)
        number = nm.group(0) if nm else ""

        # 期間
        pm = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日).*?至.*?(\d{4}年\d{1,2}月\d{1,2}日)', body)
        def cn2iso(s):
            m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日',s)
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
        start_date = cn2iso(pm.group(1)) if pm else ""
        end_date   = cn2iso(pm.group(2)) if pm else ""

        # 船舶・VHF
        vm = re.search(r'(船名|施工船)[：:]\s*([^\n。]{2,40})', body)
        vhf_m = re.search(r'VHF\s*(?:频道|CH)?\s*(\d+)', body, re.I)

        return {
            "id":        "W"+hashlib.md5(art["url"].encode()).hexdigest()[:8].upper(),
            "title":     art["title"],
            "number":    number,
            "source":    art["source"],
            "date":      date_str or datetime.now().strftime("%Y-%m-%d"),
            "startDate": start_date,
            "endDate":   end_date,
            "type":      classify(art["title"], body),
            "coords":    coords,
            "description": body[:300].replace("\n"," "),
            "vessels":   vm.group(2).strip() if vm else "",
            "vhf":       f"CH{vhf_m.group(1)}" if vhf_m else "CH16",
            "sourceUrl": art["url"],
            "fetchedAt": datetime.now().isoformat(),
        }
    except Exception as e:
        log.warning("詳細取得失敗 %s: %s", art["url"], e)
        return None

# ── メイン ───────────────────────────────────────────────────
def main():
    sess = requests.Session()
    sess.mount("https://", requests.adapters.HTTPAdapter(max_retries=2))
    results = []
    for src in MSA_SOURCES:
        articles = fetch_list(src, sess)
        for art in articles[:25]:
            detail = fetch_detail(art, sess)
            if detail:
                results.append(detail)
            time.sleep(0.8)

    out = {
        "updatedAt": datetime.now().isoformat(),
        "source":    "scraper",
        "count":     len(results),
        "warnings":  results,
    }
    with open("data/warnings.json","w",encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log.info("完了: %d件 → data/warnings.json", len(results))

if __name__ == "__main__":
    main()
