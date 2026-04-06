"""
emby_doublons_dpg.pyw  —  Lecture seule  (Dear PyGui / DirectX 11)
════════════════════════════════════════════════════════════════════
Prérequis : pip install dearpygui

RÈGLE FONDAMENTALE Dear PyGui :
  Toutes les modifications UI depuis un thread secondaire passent par
  _ui_queue — la boucle principale vide cette queue à chaque frame.
  Aucun appel dpg.* direct depuis un thread.
════════════════════════════════════════════════════════════════════
"""

import dearpygui.dearpygui as dpg
import threading, queue, json, os, sys, subprocess
import re, configparser, time, csv, difflib
from pathlib import Path
import urllib.request, urllib.parse, urllib.error

# ══════════════════════════════════════════════════════════════
#  QUEUE THREAD-SAFE — seul canal vers l'UI depuis les threads
# ══════════════════════════════════════════════════════════════
_ui_queue: queue.Queue = queue.Queue()

def ui(fn):
    """Poster une lambda à exécuter dans le thread principal."""
    _ui_queue.put(fn)

# ══════════════════════════════════════════════════════════════
#  FICHIERS PERSISTANTS
# ══════════════════════════════════════════════════════════════
CONFIG_FILE  = Path(__file__).with_suffix(".ini")
SCAN_FILE    = Path(__file__).with_name(Path(__file__).stem + "_resultats.json")
IGNORED_FILE = Path(__file__).with_name(Path(__file__).stem + "_ignores.json")

def load_config():
    cfg = configparser.ConfigParser()
    cfg["emby"] = {"url":"http://localhost:8096","api_key":"","user_id":"",
                   "nas_prefix":"/volume1","nas_unc":r"\\192.168.1.x","player":""}
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg

def save_config(d):
    cfg = configparser.ConfigParser(); cfg["emby"] = d
    with open(CONFIG_FILE,"w",encoding="utf-8") as f: cfg.write(f)

def load_ignored():
    if IGNORED_FILE.exists():
        try: return set(json.loads(IGNORED_FILE.read_text("utf-8")))
        except: pass
    return set()

def save_ignored(s):
    IGNORED_FILE.write_text(json.dumps(sorted(s),ensure_ascii=False,indent=2),"utf-8")

def save_scan(dupes, multiqual, url, prefix, unc):
    mq = {k:{"items":v[0],"reason":v[1]} for k,v in multiqual.items()}
    p  = {"saved_at":time.strftime("%Y-%m-%d %H:%M:%S"),"server_url":url,
          "nas_prefix":prefix,"nas_unc":unc,"dupes":dupes,"multiqual":mq}
    SCAN_FILE.write_text(json.dumps(p,ensure_ascii=False,indent=2),"utf-8")
    return SCAN_FILE

def load_scan():
    if not SCAN_FILE.exists(): raise FileNotFoundError(str(SCAN_FILE))
    p = json.loads(SCAN_FILE.read_text("utf-8"))
    if not {"saved_at","dupes","multiqual"}.issubset(p): raise ValueError("Fichier invalide")
    mq = {k:(v["items"],v["reason"]) for k,v in p["multiqual"].items()}
    return p["dupes"],mq,{"saved_at":p.get("saved_at","?"),"server_url":p.get("server_url","?"),
                          "nas_prefix":p.get("nas_prefix",""),"nas_unc":p.get("nas_unc","")}

# ══════════════════════════════════════════════════════════════
#  CONVERSION CHEMIN
# ══════════════════════════════════════════════════════════════
def to_win(path, prefix, unc):
    if not path or not unc: return path
    base = re.sub(r'\d+$','',prefix.rstrip('/')) or "/volume"
    m = re.match(r'^'+re.escape(base)+r'\d*',path,re.I)
    if m: return unc.rstrip("\\")+path[m.end():].replace("/","\\")
    return path.replace("/","\\")

# ══════════════════════════════════════════════════════════════
#  API EMBY (GET uniquement)
# ══════════════════════════════════════════════════════════════
def emby_get(base, key, path, params=None):
    p = dict(params or {}); p["api_key"] = key
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(p)}"
    req = urllib.request.Request(url, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def fetch_movies(base, key, uid, cb, parent_ids=None):
    """
    parent_ids : liste d'IDs de médiathèques à scanner.
    Si None ou vide → scan global (comportement original).
    """
    base_params = {"Recursive":"true","IncludeItemTypes":"Movie",
                   "Fields":"ProviderIds,Path,MediaSources,ProductionYear,DateCreated,MediaStreams",
                   "Limit":500}
    if uid: base_params["UserId"] = uid

    # Liste des scopes à parcourir
    scopes = parent_ids if parent_ids else [None]
    all_items, seen_ids = [], set()

    for pid in scopes:
        params = dict(base_params); params["StartIndex"] = 0
        if pid: params["ParentId"] = pid
        page = 0
        while True:
            data  = emby_get(base,key,"/Items",dict(params))
            items = data.get("Items",[])
            for it in items:
                if it.get("Id") not in seen_ids:
                    seen_ids.add(it.get("Id")); all_items.append(it)
            total_scope = data.get("TotalRecordCount",0)
            page += 1; cb(len(all_items), len(all_items)+max(0,total_scope-len(items)), page)
            if len(items)<params["Limit"] or not items: break
            params["StartIndex"] += len(items)

    return all_items

# ══════════════════════════════════════════════════════════════
#  ANALYSE MÉTADONNÉES
# ══════════════════════════════════════════════════════════════
_RES_RE   = re.compile(r'\b(4K|UHD|2160p?|1080p?|720p?|480p?)\b',re.I)
_CODEC_RE = re.compile(r'\b(AV1|HEVC|[Hx]\.?265|[Hx]\.?264|VP9|AVC)\b',re.I)
_HDR_RE   = re.compile(r'\b(HDR10?\+?|Dolby[\. ]?Vision|DV|HLG)\b',re.I)
# Critères séparés — chacun contrôlable par checkbox
_3D_RE      = re.compile(r'\b(3D|SBS|OU|half[.\s_-]?SBS|full[.\s_-]?SBS|MVC)\b', re.I)
_REMASTER_RE= re.compile(r'\b(remastered|remasterise)\b', re.I)
_BONUS_RE   = re.compile(r'\b(bonus|extras?|featurette|behind[.\s_-]?the[.\s_-]?scenes|making[.\s_-]?of|deleted[.\s_-]?scenes|interviews?)\b', re.I)
_CUT_RE     = re.compile(
    r'\b(extended|version[.\s_-]?longue|long[.\s_-]?cut|'
    r'director[s]?[.\s_-]?cut|theatrical[.\s_-]?cut|theatrical|'
    r'unrated|uncensored|final[.\s_-]?cut|redux|special[.\s_-]?edition)\b', re.I)
_VER_KW     = re.compile(   # union pour la rétrocompatibilité
    r'\b(extended|version[.\s_-]?longue|long[.\s_-]?cut|'
    r'director[s]?[.\s_-]?cut|theatrical[.\s_-]?cut|theatrical|'
    r'unrated|uncensored|final[.\s_-]?cut|redux|remastered|'
    r'special[.\s_-]?edition|3D|SBS|OU|MVC|bonus|extras?|featurette)\b', re.I)

def _nc(c):
    c=(c or "").upper()
    if c in ("HEVC","H265","H.265","X265"): return "HEVC"
    if c in ("H264","H.264","X264","AVC"):  return "H264"
    if c in ("AV1",): return "AV1"
    if c in ("VP9",): return "VP9"
    return c or "?"

def get_all_sources(movie):
    srcs = movie.get("MediaSources",[])
    if srcs: return [s.get("Path","") for s in srcs if s.get("Path")]
    p = movie.get("Path",""); return [p] if p else []

def get_api_size(item):
    return (item.get("MediaSources") or [{}])[0].get("Size",0) or 0

def get_rich_metadata(movie):
    md = {"width":0,"height":0,"res_label":"?","res_tier":0,
          "vcodec":"?","acodec":"?","channels":"?","hdr":False,
          "bitrate_kbps":0,"duration_s":0,"size_bytes":0,
          "date_added":movie.get("DateCreated","")[:10],
          "audio_tracks":[]}   # liste de dicts {codec, channels, lang, title}
    srcs = movie.get("MediaSources",[]); src = srcs[0] if srcs else {}
    md["size_bytes"]   = src.get("Size",0) or 0
    md["bitrate_kbps"] = (src.get("Bitrate",0) or 0)//1000
    ticks = src.get("RunTimeTicks",0) or movie.get("RunTimeTicks",0) or 0
    md["duration_s"]   = ticks//10_000_000
    streams = src.get("MediaStreams",[]) or movie.get("MediaStreams",[])
    video = next((s for s in streams if s.get("Type")=="Video"),None)

    # Toutes les pistes audio
    audio_streams = [s for s in streams if s.get("Type")=="Audio"]
    for a in audio_streams:
        lang  = (a.get("Language") or "").strip().upper() or "?"
        title = (a.get("DisplayTitle") or a.get("Title") or "").strip()
        codec = (a.get("Codec") or "?").upper()
        ch    = str(a.get("Channels","?"))
        md["audio_tracks"].append({"lang":lang,"codec":codec,
                                   "channels":ch,"title":title})

    # Résumé piste principale (compatibilité)
    if audio_streams:
        a = audio_streams[0]
        md["acodec"]   = (a.get("Codec") or "?").upper()
        md["channels"] = str(a.get("Channels","?"))

    if video:
        w,h = video.get("Width",0) or 0, video.get("Height",0) or 0
        mx = max(w,h)
        tier = 4 if mx>=2160 else 3 if mx>=1080 else 2 if mx>=720 else 1 if mx>0 else 0
        md["width"]=w; md["height"]=h; md["res_tier"]=tier
        md["vcodec"] = _nc(video.get("Codec",""))
        md["hdr"]    = video.get("VideoRange","").upper() not in ("SDR","")
        lbl = {4:"4K/UHD",3:"1080p",2:"720p",1:"SD",0:"?"}
        md["res_label"] = f"{lbl[tier]} ({w}x{h})" if w else lbl[tier]
    if md["vcodec"]=="?":
        paths=get_all_sources(movie); fname=Path(paths[0]).name if paths else ""
        mc=_CODEC_RE.search(fname)
        if mc: md["vcodec"]=_nc(mc.group(0))
    return md

def get_quality_signature(movie):
    md = get_rich_metadata(movie)
    return {"res_tier":md["res_tier"],"codec":md["vcodec"],
            "hdr":md["hdr"],"res_label":md["res_label"],"codec_raw":md["vcodec"]}

def fmt_size(b):
    if not b: return "?"
    for u in ["o","Ko","Mo","Go","To"]:
        if b<1024: return f"{b:.1f}{u}"
        b/=1024
    return f"{b:.1f}Po"

def fmt_duration(s):
    if not s: return "?"
    h,r=divmod(int(s),3600); m,sc=divmod(r,60)
    return f"{h}h{m:02d}m{sc:02d}s" if h else f"{m}m{sc:02d}s"

def confidence_score(key):
    if key.startswith("imdb:"):  return 100,"IMDB",(46,204,113)
    if key.startswith("tmdb:"):  return  85,"TMDB",(240,160,0)
    if key.startswith("title:"): return  60,"Titre",(220,120,0)
    if key.startswith("fuzzy:"): return  40,"Fuzzy",(200,60,60)
    return 50,"?",(136,136,136)

# ══════════════════════════════════════════════════════════════
#  DÉTECTION DOUBLONS
# ══════════════════════════════════════════════════════════════
def normalize_title(t):
    t=t.lower(); t=re.sub(r"[^\w\s]","",t); t=re.sub(r"\s+"," ",t).strip()
    return re.sub(r"^(le|la|les|the|a|an|l|un|une)\s+","",t)

def _ver_tag(movie):
    paths=get_all_sources(movie)
    fname=Path(paths[0]).name if paths else movie.get("Name","")
    m=_VER_KW.search(fname); return m.group(0).lower() if m else ""

def _fname(movie):
    paths=get_all_sources(movie)
    return Path(paths[0]).name if paths else movie.get("Name","")

def is_intentional(items, criteria=None):
    """
    criteria : dict des critères actifs (None = utilise G["criteria"]).
    Clés : resolution, hdr, av1, 3d, remaster, cut
    """
    c = criteria if criteria is not None else G.get("criteria",{})
    sigs=[get_quality_signature(m) for m in items]
    tiers={s["res_tier"] for s in sigs if s["res_tier"]>0}
    codecs={s["codec"] for s in sigs if s["codec"] not in ("?","")}
    hdrs={s["hdr"] for s in sigs}
    reasons=[]

    if c.get("resolution",True) and len(tiers)>1:
        reasons.append("resolutions diff. ("+"/".join(sorted({s["res_label"] for s in sigs}))+")")

    if c.get("hdr",True) and len(hdrs)>1:
        reasons.append("HDR vs SDR")

    if c.get("av1",True) and "AV1" in codecs and len(codecs)>1:
        reasons.append("AV1 vs autre codec")

    if c.get("3d",True):
        for m in items:
            if _3D_RE.search(_fname(m)):
                reasons.append("3D/SBS detecte"); break

    if c.get("remaster",True):
        for m in items:
            if _REMASTER_RE.search(_fname(m)):
                reasons.append("Remastered detecte"); break

    if c.get("cut",True):
        tags=set()
        for m in items:
            mr=_CUT_RE.search(_fname(m))
            if mr: tags.add(mr.group(0).lower())
        if tags: reasons.append("cuts ("+"/".join(sorted(tags))+")")

    if c.get("bonus",True):
        for m in items:
            if _BONUS_RE.search(_fname(m)):
                reasons.append("bonus/extras detecte"); break

    return bool(reasons)," + ".join(reasons)

def find_fuzzy_dupes(movies):
    cands=[m for m in movies
           if not m.get("ProviderIds",{}).get("Imdb","").strip()
           and not m.get("ProviderIds",{}).get("Tmdb","").strip()]
    if len(cands)>200: cands=cands[:200]  # cap O(n²)
    groups,used={},set()
    for i,a in enumerate(cands):
        if i in used: continue
        ta=normalize_title(a.get("Name","")); ya=a.get("ProductionYear",0) or 0
        grp=[a]
        for j,b in enumerate(cands):
            if j<=i or j in used: continue
            tb=normalize_title(b.get("Name","")); yb=b.get("ProductionYear",0) or 0
            if abs(ya-yb)>1: continue
            if difflib.SequenceMatcher(None,ta,tb).ratio()>=0.82 and ta!=tb:
                grp.append(b); used.add(j)
        if len(grp)>1: used.add(i); groups[f"fuzzy:{ta}:{ya}"]=grp
    return groups

def find_duplicates(movies, step_cb):
    groups={}; total=len(movies)
    for idx,m in enumerate(movies):
        pids=m.get("ProviderIds",{})
        imdb=pids.get("Imdb","").strip(); tmdb=pids.get("Tmdb","").strip()
        key=(f"imdb:{imdb}" if imdb else f"tmdb:{tmdb}" if tmdb
             else f"title:{normalize_title(m.get('Name',''))}:{m.get('ProductionYear','')}")
        groups.setdefault(key,[]).append(m)
        if idx%20==0 or idx==total-1: step_cb(idx+1,total,m.get("Name",""))
    for k,v in find_fuzzy_dupes(movies).items():
        if k not in groups: groups[k]=v
    real,mq={},{}
    for k,v in groups.items():
        if len(v)<=1: continue
        intl,reason=is_intentional(v)
        if intl: mq[k]=(v,reason)
        else:    real[k]=v
    return real,mq

def compute_stats(dupes):
    """Retourne (nb_groupes, nb_fichiers, gain_min, gain_max).
    gain_min : si on supprime seulement le plus petit fichier de chaque groupe.
    gain_max : si on supprime tous les fichiers sauf le plus petit de chaque groupe.
    """
    ng, nf, gain_min, gain_max = 0, 0, 0, 0
    for items in dupes.values():
        ng += 1
        sizes = sorted([get_api_size(it) for it in items], reverse=True)
        nf += len(items)
        if len(sizes) > 1:
            gain_min += sizes[-1]          # on supprime seulement le plus petit
            gain_max += sum(sizes[:-1])    # on garde seulement le plus petit
    return ng, nf, gain_min, gain_max

# ══════════════════════════════════════════════════════════════
#  EXPORT
# ══════════════════════════════════════════════════════════════
def export_csv(dupes, multiqual, fp, prefix, unc):
    with open(fp,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f,delimiter=";")
        w.writerow(["Type","#","Titre","Année","Score","Fichier","Dossier","Taille","Qualité"])
        for g_idx,(key,items) in enumerate(dupes.items(),1):
            sc,sl,_=confidence_score(key); first=items[0]
            for item in items:
                for p in get_all_sources(item):
                    wp=to_win(p,prefix,unc) or p; sig=get_quality_signature(item)
                    w.writerow(["Doublon",g_idx,first.get("Name","?"),
                                first.get("ProductionYear",""),sc,
                                Path(p).name,str(Path(wp).parent),
                                fmt_size(get_api_size(item)),
                                f"{sig['res_label']} {sig['codec_raw']}"])

def export_html(dupes, multiqual, fp, prefix, unc):
    ng, nf, gain_min, gain_max = compute_stats(dupes)
    nq  = len(multiqual)
    ts  = time.strftime("%d/%m/%Y à %H:%M")

    # ── Données pour graphiques ──────────────────────────────────────────────

    # Répartition par score de confiance
    score_counts = {"IMDB (100%)":0, "TMDB (85%)":0, "Titre (60%)":0, "Fuzzy (40%)":0}
    for key in dupes:
        sc,sl,_ = confidence_score(key)
        if sc==100:   score_counts["IMDB (100%)"]+=1
        elif sc==85:  score_counts["TMDB (85%)"]+=1
        elif sc==60:  score_counts["Titre (60%)"]+=1
        else:         score_counts["Fuzzy (40%)"]+=1

    # Top 10 doublons par espace gaspillé
    top10 = []
    for key, items in dupes.items():
        sizes = sorted([get_api_size(it) for it in items], reverse=True)
        wasted = sum(sizes[1:]) if len(sizes)>1 else 0
        if wasted > 0:
            first = items[0]
            top10.append({
                "title": f"{first.get('Name','?')} ({first.get('ProductionYear','')})",
                "wasted": wasted,
                "files": len(items),
                "sc": confidence_score(key)[1]
            })
    top10 = sorted(top10, key=lambda x: -x["wasted"])[:10]

    # Répartition par résolution
    res_counts = {"4K/UHD":0, "1080p":0, "720p":0, "SD":0, "?":0}
    for items in dupes.values():
        for item in items:
            sig = get_quality_signature(item)
            tier = sig["res_tier"]
            k = {4:"4K/UHD",3:"1080p",2:"720p",1:"SD"}.get(tier,"?")
            res_counts[k] += 1

    # Répartition codec
    codec_counts = {}
    for items in dupes.values():
        for item in items:
            sig = get_quality_signature(item)
            c = sig["codec_raw"] or "?"
            codec_counts[c] = codec_counts.get(c,0)+1

    # Tableau détaillé
    rows_html = ""
    for g_idx,(key,items) in enumerate(dupes.items(),1):
        sc,sl,_ = confidence_score(key)
        first   = items[0]
        title   = f"{first.get('Name','?')} ({first.get('ProductionYear','')})"
        sizes   = sorted([get_api_size(it) for it in items],reverse=True)
        wasted  = sum(sizes[1:]) if len(sizes)>1 else 0
        badge_col = {"IMDB":"#2ecc71","TMDB":"#f39c12","Titre":"#e67e22","Fuzzy":"#e74c3c"}.get(sl,"#888")
        rows_html += (f'<tr class="grp"><td>{g_idx}</td>'
                      f'<td colspan="4"><b>{title}</b>'
                      f'  <span class="badge" style="background:{badge_col}">{sl} {sc}%</span>'
                      f'  <span class="wasted">Gaspille : {fmt_size(wasted)}</span></td></tr>\n')
        for item in items:
            for p in get_all_sources(item):
                wp  = to_win(p,prefix,unc) or p
                sig = get_quality_signature(item)
                sz  = fmt_size(get_api_size(item))
                rows_html += (f'<tr><td></td><td>{Path(p).name}</td>'
                              f'<td>{sig["res_label"]} {sig["codec_raw"]}</td>'
                              f'<td style="color:#aaa;font-size:.8em">{str(Path(wp).parent)}</td>'
                              f'<td style="text-align:right">{sz}</td></tr>\n')

    rows_mq = ""
    for g_idx,(key,payload) in enumerate(multiqual.items(),1):
        items,reason = payload
        first = items[0]
        title = f"{first.get('Name','?')} ({first.get('ProductionYear','')})"
        rows_mq += (f'<tr class="grp-mq"><td>{g_idx}</td>'
                    f'<td colspan="4"><b>{title}</b>'
                    f'  <span style="color:#aaa;font-size:.85em">{reason}</span></td></tr>\n')

    # ── JSON pour Chart.js ──────────────────────────────────────────────────
    import json as _json
    sc_labels = _json.dumps(list(score_counts.keys()))
    sc_data   = _json.dumps(list(score_counts.values()))
    sc_colors = _json.dumps(["#2ecc71","#f39c12","#e67e22","#e74c3c"])

    res_labels = _json.dumps([k for k,v in res_counts.items() if v>0])
    res_data   = _json.dumps([v for v in res_counts.values() if v>0])
    res_colors = _json.dumps(["#e94560","#0f3460","#f39c12","#2ecc71","#888"])

    cod_labels = _json.dumps(list(codec_counts.keys()))
    cod_data   = _json.dumps(list(codec_counts.values()))

    top_labels = _json.dumps([t["title"][:35]+"…" if len(t["title"])>35
                               else t["title"] for t in top10])
    top_data   = _json.dumps([round(t["wasted"]/1e9,2) for t in top10])

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Emby Duplicate Finder — Rapport {ts}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',sans-serif;background:#12121e;color:#e0e0e0;padding:0}}
.header{{background:linear-gradient(135deg,#1a1a3e,#0f3460);padding:28px 40px;
         border-bottom:3px solid #e94560}}
.header h1{{font-size:2em;color:#e94560;margin-bottom:4px}}
.header .sub{{color:#8888aa;font-size:.95em}}
.content{{padding:28px 40px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
           gap:16px;margin-bottom:32px}}
.kpi{{background:#16213e;border-radius:12px;padding:20px;text-align:center;
      border:1px solid #0f3460;transition:transform .2s}}
.kpi:hover{{transform:translateY(-3px)}}
.kpi .val{{font-size:2.2em;font-weight:700;line-height:1.1}}
.kpi .lbl{{color:#8888aa;font-size:.85em;margin-top:4px}}
.kpi.red .val{{color:#e94560}}
.kpi.orange .val{{color:#f39c12}}
.kpi.green .val{{color:#2ecc71}}
.kpi.blue .val{{color:#88ccff}}
.kpi.gray .val{{color:#aaa}}
.charts{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:32px}}
.chart-box{{background:#16213e;border-radius:12px;padding:20px;border:1px solid #0f3460}}
.chart-box h3{{color:#88ccff;margin-bottom:16px;font-size:1em;text-transform:uppercase;
               letter-spacing:.05em}}
.chart-box.wide{{grid-column:1/-1}}
canvas{{max-height:280px}}
h2{{color:#e94560;margin:32px 0 12px;font-size:1.1em;text-transform:uppercase;
    letter-spacing:.08em;border-bottom:1px solid #0f3460;padding-bottom:8px}}
table{{width:100%;border-collapse:collapse;font-size:.875em;margin-bottom:32px}}
th{{background:#0f3460;padding:10px 12px;text-align:left;color:#88ccff;
    font-weight:600;position:sticky;top:0}}
td{{padding:7px 12px;border-bottom:1px solid #1e2a3a}}
tr:hover td{{background:#1e2a40}}
tr.grp td{{background:#2a1a1a;font-weight:600;padding:10px 12px}}
tr.grp-mq td{{background:#0f1f30;padding:10px 12px}}
.badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.78em;
        font-weight:700;color:#000;margin-left:8px;vertical-align:middle}}
.wasted{{color:#e94560;font-size:.82em;margin-left:12px;font-weight:400}}
.footer{{background:#0a0a18;text-align:center;padding:16px;color:#445;font-size:.8em;
         border-top:1px solid #1e2a3a}}
</style>
</head>
<body>

<div class="header">
  <h1>🎬 Emby Duplicate Finder</h1>
  <div class="sub">Rapport généré le {ts} &nbsp;·&nbsp; By Popov2026 &copy; 2026</div>
</div>

<div class="content">

<!-- KPIs -->
<div class="kpi-grid">
  <div class="kpi red">
    <div class="val">{ng}</div>
    <div class="lbl">Groupes de doublons</div>
  </div>
  <div class="kpi orange">
    <div class="val">{nf}</div>
    <div class="lbl">Fichiers concernés</div>
  </div>
  <div class="kpi green">
    <div class="val">{fmt_size(gain_min)}</div>
    <div class="lbl">Gain minimum<br><small>(supprimer les plus petits)</small></div>
  </div>
  <div class="kpi green">
    <div class="val">{fmt_size(gain_max)}</div>
    <div class="lbl">Gain maximum<br><small>(ne garder que les plus petits)</small></div>
  </div>
  <div class="kpi blue">
    <div class="val">{nq}</div>
    <div class="lbl">Versions intentionnelles</div>
  </div>
</div>

<!-- Graphiques -->
<div class="charts">
  <div class="chart-box">
    <h3>Répartition par score de confiance</h3>
    <canvas id="chartScore"></canvas>
  </div>
  <div class="chart-box">
    <h3>Répartition par résolution</h3>
    <canvas id="chartRes"></canvas>
  </div>
  <div class="chart-box">
    <h3>Répartition par codec vidéo</h3>
    <canvas id="chartCodec"></canvas>
  </div>
  <div class="chart-box">
    <h3>Top 10 — Espace gaspillé par groupe (Go)</h3>
    <canvas id="chartTop"></canvas>
  </div>
</div>

<!-- Tableau doublons -->
<h2>⚠ Vrais doublons — {ng} groupe(s)</h2>
<table>
  <tr><th>#</th><th>Fichier</th><th>Qualité</th><th>Dossier</th><th>Taille</th></tr>
  {rows_html}
</table>

<!-- Tableau intentionnels -->
<h2>ℹ Versions intentionnelles — {nq} groupe(s)</h2>
<table>
  <tr><th>#</th><th colspan="4">Titre — Raison</th></tr>
  {rows_mq}
</table>

</div>

<div class="footer">
  Emby Duplicate Finder &nbsp;·&nbsp; By Popov2026 &copy; 2026 &nbsp;·&nbsp;
  Rapport du {ts}
</div>

<script>
const DARK = '#16213e', GRID = '#1e2a3a', TEXT = '#8888aa';
const defaults = {{
  plugins:{{legend:{{labels:{{color:'#e0e0e0',font:{{size:12}}}}}}}},
  scales:{{
    x:{{ticks:{{color:TEXT}},grid:{{color:GRID}}}},
    y:{{ticks:{{color:TEXT}},grid:{{color:GRID}}}}
  }}
}};

// Score confiance
new Chart(document.getElementById('chartScore'),{{
  type:'doughnut',
  data:{{labels:{sc_labels},datasets:[{{data:{sc_data},backgroundColor:{sc_colors},
    borderColor:'#12121e',borderWidth:2}}]}},
  options:{{plugins:{{legend:{{labels:{{color:'#e0e0e0'}}}}}}}}
}});

// Résolution
new Chart(document.getElementById('chartRes'),{{
  type:'pie',
  data:{{labels:{res_labels},datasets:[{{data:{res_data},backgroundColor:{res_colors},
    borderColor:'#12121e',borderWidth:2}}]}},
  options:{{plugins:{{legend:{{labels:{{color:'#e0e0e0'}}}}}}}}
}});

// Codec
new Chart(document.getElementById('chartCodec'),{{
  type:'bar',
  data:{{labels:{cod_labels},datasets:[{{label:'Fichiers',data:{cod_data},
    backgroundColor:'#0f3460',borderColor:'#e94560',borderWidth:1}}]}},
  options:{{...defaults,plugins:{{legend:{{display:false}}}}}}
}});

// Top 10
new Chart(document.getElementById('chartTop'),{{
  type:'bar',
  data:{{labels:{top_labels},datasets:[{{label:'Go gaspillés',data:{top_data},
    backgroundColor:'#e94560cc',borderColor:'#e94560',borderWidth:1}}]}},
  options:{{...defaults,indexAxis:'y',plugins:{{legend:{{display:false}}}}}}
}});
</script>

</body>
</html>"""
    Path(fp).write_text(html, encoding="utf-8")

# ══════════════════════════════════════════════════════════════
#  OUVERTURE FICHIERS
# ══════════════════════════════════════════════════════════════
def get_player():
    """Lit toujours le champ lecteur depuis l'UI — jamais de valeur figée."""
    try:
        return dpg.get_value("inp_player").strip()
    except Exception:
        return G.get("player","")


def open_file(path, player=""):
    if not path: return
    try:
        if player and Path(player).exists():
            subprocess.Popen([player, path])
        elif sys.platform == "win32":
            # os.startfile gère mieux les chemins UNC que cmd /c start
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        ui(lambda e=e, p=path: modal_err(
            "Erreur ouverture",
            f"Chemin tente :\n{p}\n\n"
            f"Erreur : {e}\n\n"
            f"Lecteur configure : {player or '(defaut systeme)'}"))


def open_folder(path):
    if not path: return
    try:
        if sys.platform == "win32":
            # /select, et le chemin DOIVENT etre colles en un seul argument
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
    except Exception as e:
        ui(lambda e=e, p=path: modal_err(
            "Erreur dossier",
            f"Chemin tente :\n{p}\n\nErreur : {e}"))


def tip(text, wrap=320):
    """Ajoute un tooltip sur le dernier widget créé."""
    with dpg.tooltip(dpg.last_item()):
        dpg.add_text(text, wrap=wrap)

# ══════════════════════════════════════════════════════════════
#  ÉTAT GLOBAL
# ══════════════════════════════════════════════════════════════
CFG = load_config()
G = {
    "dupes":{}, "multiqual":{}, "ignored":load_ignored(),
    "nas_prefix": CFG["emby"].get("nas_prefix","/volume1"),
    "nas_unc":    CFG["emby"].get("nas_unc",""),
    "player":     CFG["emby"].get("player",""),
    "filter":"",  "sort":"title_asc",  "min_score": 0,
    # Critères d'exclusion (True = actif = ce critère rend le groupe intentionnel)
    "criteria": {"resolution":True,"hdr":True,"av1":True,
                 "3d":True,"remaster":True,"cut":True,"bonus":True},
    # Médiathèques disponibles et sélectionnées
    "libraries": [],        # [{"id":..,"name":..,"type":..}]
    "lib_selected": set(),  # ids sélectionnés pour le scan
}
_mid = 0
_render_timer = 0.0   # debounce pour slider/filtre
_RENDER_DELAY = 0.30  # secondes d'attente apres dernier changement

# ══════════════════════════════════════════════════════════════
#  MODALES  (toujours appelées depuis le thread principal)
# ══════════════════════════════════════════════════════════════
def modal_err(title, msg):
    global _mid; _mid+=1; tag=f"_err{_mid}"
    with dpg.window(label=title,tag=tag,modal=True,width=520,height=200,
                    pos=[160,200],no_resize=True):
        dpg.add_text(msg,wrap=500)
        dpg.add_separator()
        dpg.add_button(label="OK",width=-1,callback=lambda s,a,u,_t=tag:dpg.delete_item(_t))

def modal_info(title, msg):
    global _mid; _mid+=1; tag=f"_inf{_mid}"
    with dpg.window(label=title,tag=tag,modal=True,width=520,height=220,
                    pos=[160,200],no_resize=True):
        dpg.add_text(msg,wrap=500)
        dpg.add_separator()
        dpg.add_button(label="OK",width=-1,callback=lambda s,a,u,_t=tag:dpg.delete_item(_t))

# ══════════════════════════════════════════════════════════════
#  FILTRE / TRI
# ══════════════════════════════════════════════════════════════
def apply_filter_sort():
    ft=G["filter"].lower().strip(); sk=G["sort"]; mn=G["min_score"]
    ign=G["ignored"]; d=G["dupes"]; m=G["multiqual"]

    def match(key,payload):
        items=payload[0] if isinstance(payload,tuple) else payload
        title=(items[0].get("Name","") if items else "").lower()
        sc,_,_=confidence_score(key)
        if sc < mn: return False                          # seuil de confiance
        return not ft or ft in title or ft in key.lower()

    d2={k:v for k,v in d.items() if match(k,v) and k not in ign}
    m2={k:v for k,v in m.items() if match(k,v)}

    def skey(kv,is_mq=False):
        key,payload=kv
        items=payload[0] if is_mq else payload
        title=(items[0].get("Name","") if items else "").lower()
        sc,_,_=confidence_score(key)
        sz=sum(get_api_size(it) for it in items)
        if sk=="title_asc":  return title
        if sk=="title_desc": return tuple(-ord(c) for c in title[:60])
        if sk=="size":       return -sz
        if sk=="conf_desc":  return -sc
        if sk=="conf_asc":   return sc
        return title

    return (dict(sorted(d2.items(),key=lambda kv:skey(kv,False))),
            dict(sorted(m2.items(),key=lambda kv:skey(kv,True))))

# ══════════════════════════════════════════════════════════════
#  RENDU RÉSULTATS  (thread principal uniquement)
# ══════════════════════════════════════════════════════════════
def render_results():
    dpg.delete_item("results_area",children_only=True)
    d,m = apply_filter_sort()
    ng=len(d); nf=sum(len(v) for v in d.values()); nq=len(m); ign=len(G["ignored"])

    try: dpg.configure_item("lbl_ignored",default_value=f"{ign} ignoré(s)")
    except: pass

    if not d and not m:
        dpg.add_text("Aucun doublon détecté.",parent="results_area",color=(46,204,113))
        return

    # Stats
    _,_,gain_min,gain_max=compute_stats(d)
    with dpg.group(parent="results_area",horizontal=True):
        for val,lbl,col in [(str(ng),"groupes",(233,69,96)),
                             (str(nf),"fichiers",(230,126,34)),
                             (f"{fmt_size(gain_min)} ~ {fmt_size(gain_max)}","recuperable",(46,204,113)),
                             (str(nq),"intentionnels",(136,136,170)),
                             (str(ign),"ignores",(100,100,120))]:
            with dpg.group():
                dpg.add_text(val,color=col)
                dpg.add_text(lbl,color=(136,136,170))
            dpg.add_spacer(width=18)
    dpg.add_separator(parent="results_area")
    dpg.add_spacer(height=6,parent="results_area")

    if d:
        dpg.add_text(f"  VRAIS DOUBLONS  -  {ng} groupe(s)  -  {nf} fichiers",
                     parent="results_area",color=(233,69,96))
        dpg.add_spacer(height=4,parent="results_area")
        _render_table(d,False)
    else:
        dpg.add_text("Aucun vrai doublon.",parent="results_area",color=(46,204,113))

    if m:
        dpg.add_spacer(height=14,parent="results_area")
        dpg.add_text(f"  VERSIONS INTENTIONNELLES  -  {nq} groupe(s)",
                     parent="results_area",color=(136,136,170))
        dpg.add_spacer(height=4,parent="results_area")
        _render_table(m,True)


def _render_table(groups, is_mq):
    prefix=G["nas_prefix"]; unc=G["nas_unc"]; player=G["player"]
    hdr_col = (80,20,20) if not is_mq else (15,40,70)   # fond bandeau
    ttl_col = (255,160,60) if not is_mq else (140,180,255)  # couleur titre

    for g_idx,(key,payload) in enumerate(groups.items()):
        items=payload[0] if is_mq else payload
        reason=payload[1] if is_mq else ""
        if not items: continue
        first=items[0]
        title=first.get("Name","?"); year=first.get("ProductionYear","")
        imdb=first.get("ProviderIds",{}).get("Imdb","")
        sc,sl,sc_col=confidence_score(key)
        all_wp=[to_win(p,prefix,unc) or p for it in items for p in get_all_sources(it)]

        # ── Séparateur visuel entre groupes ──────────────────
        dpg.add_spacer(height=10, parent="results_area")

        # ── Bandeau titre du groupe ───────────────────────────
        with dpg.group(parent="results_area"):

            # Ligne titre principale
            with dpg.group(horizontal=True):
                # Badge score coloré
                dpg.add_text(f"[{sc}%]", color=sc_col)

                # Titre en grand, coloré
                dpg.add_text(f"  >> {title}",color=ttl_col)
                if year:
                    dpg.add_text(f"({year})", color=(180,180,180))
                if imdb:
                    dpg.add_text(f"[IMDB:{imdb}]", color=(100,200,100))
                dpg.add_text(
                    f"-  {len(items)} fichiers" + (f"  .  {reason}" if reason else ""),
                    color=(180,180,180))

                # Boutons alignés à droite
                dpg.add_spacer(width=20)
                dpg.add_text(f"[{sl} {sc}%]", color=sc_col)
                dpg.add_spacer(width=10)
                dpg.add_button(label="Ouvrir tout", width=80,
                    user_data=all_wp,
                    callback=lambda s,a,u: [open_file(p,get_player()) for p in u])
                tip("Ouvre tous les fichiers du groupe simultanement.\n\n"
                    "ATTENTION : votre lecteur video doit supporter\n"
                    "plusieurs instances simultanées (sessions multiples).\n"
                    "VLC : Preferences > Interface > decocher 'Une seule instance'.\n"
                    "MPC-HC : Options > Lecteur > 'Permettre plusieurs instances'.", wrap=360)
                dpg.add_button(label="Comparer", width=70,
                    user_data=items,
                    callback=lambda s,a,u: compare_popup(u))
                tip("Affiche les metadonnees des fichiers cote a cote\n(resolution, codec, bitrate, pistes audio...)\nLes differences sont surlignees en jaune.")
                dpg.add_button(label="Ignorer", width=60,
                    user_data=(key,title),
                    callback=lambda s,a,u: do_ignore(u[0],u[1]))
                tip("Marque ce groupe comme faux positif.\nIl n'apparaitra plus dans les resultats.\nRecuperez-le via 'Reinitialiser' les ignores.")

            dpg.add_separator()

            # ── Sous-tableau fichiers ─────────────────────────
            with dpg.table(header_row=True, row_background=True,
                           borders_innerH=True, borders_outerH=True,
                           borders_innerV=True, borders_outerV=True,
                           policy=dpg.mvTable_SizingStretchProp):
                dpg.add_table_column(label="Fichier",    width_stretch=True, init_width_or_weight=0.34)
                dpg.add_table_column(label="Qualité",    width_fixed=True,   init_width_or_weight=130)
                dpg.add_table_column(label="Dossier NAS",width_stretch=True, init_width_or_weight=0.40)
                dpg.add_table_column(label="Taille",     width_fixed=True,   init_width_or_weight=68)
                dpg.add_table_column(label="Actions",    width_fixed=True,   init_width_or_weight=115)

                for item in items:
                    sig=get_quality_signature(item)
                    qual=(f"{sig['res_label']} {sig['codec_raw']}"
                          + (" HDR" if sig["hdr"] else ""))
                    sz=fmt_size(get_api_size(item))
                    for path in get_all_sources(item):
                        wp=to_win(path,prefix,unc) or path
                        fname=Path(path).name if path else "—"
                        folder=str(Path(wp).parent) if wp else "—"
                        fd=folder if len(folder)<=55 else "…"+folder[-52:]
                        with dpg.table_row():
                            dpg.add_button(label=f"  {fname}", width=-1,
                                user_data=(wp,player),
                                callback=lambda s,a,u: open_file(u[0],u[1]))
                            dpg.add_text(qual, color=(170,221,255))
                            dpg.add_text(fd)
                            dpg.add_text(sz)
                            with dpg.group(horizontal=True):
                                dpg.add_button(label="Lire", width=40,
                                    user_data=wp,
                                    callback=lambda s,a,u: open_file(u,get_player()))
                                tip("Ouvre ce fichier avec le lecteur video configure.")
                                dpg.add_button(label="Dossier", width=60,
                                    user_data=wp,
                                    callback=lambda s,a,u: open_folder(u))
                                tip("Ouvre l'explorateur Windows sur le dossier de ce fichier.")


# ══════════════════════════════════════════════════════════════
#  POPUP COMPARAISON
# ══════════════════════════════════════════════════════════════
def compare_popup(items):
    global _mid; _mid+=1; tag=f"_cmp{_mid}"
    first=items[0]
    prefix=G["nas_prefix"]; unc=G["nas_unc"]; player=G["player"]
    lx=[get_all_sources(it)[0] if get_all_sources(it) else "" for it in items]
    wp=[to_win(p,prefix,unc) or p for p in lx]
    metas=[get_rich_metadata(it) for it in items]
    w=min(430*len(items),1400)
    title_str=f"{first.get('Name','?')} ({first.get('ProductionYear','')})"

    with dpg.window(label=f"Comparaison — {title_str}",tag=tag,
                    modal=True,width=w,height=640,pos=[40,60]):

        def fmt_audio_tracks(md):
            tracks = md.get("audio_tracks",[])
            if not tracks: return "?"
            parts = []
            for t in tracks:
                lang  = t["lang"] if t["lang"] != "?" else "?"
                codec = t["codec"]
                ch    = t["channels"]
                label = f"{lang} {codec} {ch}ch"
                if t["title"]: label += f" ({t['title'][:20]})"
                parts.append(label)
            return " | ".join(parts)

        FIELDS=[
            ("Fichier",      lambda md,lp,wpp: Path(wpp).name if wpp else "—"),
            ("Resolution",   lambda md,lp,wpp: md["res_label"]),
            ("Codec video",  lambda md,lp,wpp: md["vcodec"]),
            ("HDR",          lambda md,lp,wpp: "Oui" if md["hdr"] else "Non"),
            ("Codec audio",  lambda md,lp,wpp: md["acodec"]),
            ("Canaux",       lambda md,lp,wpp: md["channels"]),
            ("Pistes audio", lambda md,lp,wpp: fmt_audio_tracks(md)),
            ("Bitrate",      lambda md,lp,wpp: f"{md['bitrate_kbps']} kbps" if md["bitrate_kbps"] else "?"),
            ("Duree",        lambda md,lp,wpp: fmt_duration(md["duration_s"])),
            ("Taille",       lambda md,lp,wpp: fmt_size(md["size_bytes"])),
            ("Ajoute le",    lambda md,lp,wpp: md["date_added"] or "?"),
            ("Dossier",      lambda md,lp,wpp: str(Path(wpp).parent) if wpp else "—"),
        ]
        with dpg.table(header_row=True,borders_outerH=True,
                       borders_innerV=True,borders_innerH=True,
                       policy=dpg.mvTable_SizingStretchProp):
            dpg.add_table_column(label="Champ",width_fixed=True,init_width_or_weight=100)
            for ci in range(len(items)):
                dpg.add_table_column(label=f"Fichier #{ci+1}",width_stretch=True)
            for fname,fn in FIELDS:
                vals=[fn(md,lp,wpp) for md,lp,wpp in zip(metas,lx,wp)]
                diff=len(set(vals))>1
                with dpg.table_row():
                    dpg.add_text(fname,color=(136,136,170))
                    for v in vals:
                        dpg.add_text(v,color=(255,220,100) if diff else (224,224,224))
        dpg.add_separator()
        with dpg.group(horizontal=True):
            for ci,wpp in enumerate(wp):
                dpg.add_button(label=f"Lire #{ci+1}",width=90,
                    user_data=wpp,
                    callback=lambda s,a,u: open_file(u,get_player()))
                tip("Ouvre ce fichier avec le lecteur video configure.")
                dpg.add_button(label=f"Dossier #{ci+1}",width=90,
                    user_data=wpp,
                    callback=lambda s,a,u: open_folder(u))
                dpg.add_spacer(width=8)
        dpg.add_separator()
        dpg.add_button(label="Fermer",width=-1,
            user_data=tag,
            callback=lambda s,a,u: dpg.delete_item(u))

# ══════════════════════════════════════════════════════════════
#  ACTIONS
# ══════════════════════════════════════════════════════════════
def _set_criterion(key, val):
    G["criteria"][key] = val

def do_ignore(key, title):
    G["ignored"].add(key); save_ignored(G["ignored"]); render_results()

def do_reset_ignored():
    G["ignored"].clear(); save_ignored(G["ignored"]); render_results()

def _schedule_render():
    """Demande un render dans _RENDER_DELAY secondes (debounce)."""
    global _render_timer
    _render_timer = time.time() + _RENDER_DELAY

def on_filter(s,v,u):
    G["filter"]=v; _schedule_render()

def on_score_threshold(s,v,u):
    global _render_timer
    G["min_score"]=v
    # Mettre à jour le label IMMÉDIATEMENT (pas besoin de render complet)
    try: dpg.configure_item("lbl_score_val", default_value=f"{v}%")
    except: pass
    _schedule_render()

def on_sort(s,v,u):
    G["sort"]={"Titre A>Z":"title_asc","Titre Z>A":"title_desc",
               "Taille":"size","Confiance v":"conf_desc",
               "Confiance ^":"conf_asc"}.get(v,"title_asc")
    render_results()

def browse_player():
    try:
        import tkinter as tk; from tkinter import filedialog
        r=tk.Tk(); r.withdraw()
        fp=filedialog.askopenfilename(title="Lecteur video",
            filetypes=[("Executables","*.exe"),("Tous","*.*")],
            initialdir=r"C:\Program Files")
        r.destroy()
        if fp:
            dpg.set_value("inp_player",fp); G["player"]=fp
    except: pass

def do_export():
    global _mid; _mid+=1; tag=f"_exp{_mid}"
    with dpg.window(label="Exporter",tag=tag,modal=True,
                    width=340,height=190,pos=[200,200],no_resize=True):
        dpg.add_text("Format :")
        dpg.add_radio_button(("HTML (navigateur)","CSV (tableur)"),tag="exp_fmt_rb")
        dpg.add_separator()
        def go():
            fmt_str = dpg.get_value("exp_fmt_rb")   # retourne la chaîne choisie
            dpg.delete_item(tag)
            is_html = "HTML" in fmt_str
            ext     = "html" if is_html else "csv"
            ts      = time.strftime("%Y%m%d_%H%M")
            defname = f"emby_doublons_{ts}.{ext}"
            try:
                import tkinter as tk; from tkinter import filedialog
                r=tk.Tk(); r.withdraw()
                fp=filedialog.asksaveasfilename(
                    title=f"Exporter en {ext.upper()}",
                    initialfile=defname,
                    defaultextension=f".{ext}",
                    filetypes=[(f"Fichier {ext.upper()}",f"*.{ext}"),("Tous","*.*")])
                r.destroy()
                if not fp: return
                prefix=G["nas_prefix"]; unc=G["nas_unc"]
                if is_html:
                    export_html(G["dupes"],G["multiqual"],fp,prefix,unc)
                    import webbrowser; webbrowser.open(Path(fp).as_uri())
                else:
                    export_csv(G["dupes"],G["multiqual"],fp,prefix,unc)
                modal_info("Export OK",f"Fichier:\n{fp}")
            except Exception as e: modal_err("Erreur export",str(e))
        dpg.add_button(label="Exporter",width=-1,callback=lambda s,a,u:go())

def do_save():
    try:
        p=save_scan(G["dupes"],G["multiqual"],
                    dpg.get_value("inp_url").strip(),
                    G["nas_prefix"],G["nas_unc"])
        dpg.configure_item("lbl_scan_info",default_value=f"Sauvegarde {time.strftime('%d/%m/%Y %H:%M')}")
        modal_info("Sauvegarde",f"Fichier:\n{p}")
    except Exception as e: modal_err("Erreur",str(e))

def do_load():
    try:
        dupes,mq,meta=load_scan()
        G["dupes"]=dupes; G["multiqual"]=mq
        if meta["nas_prefix"]:
            G["nas_prefix"]=meta["nas_prefix"]
            dpg.set_value("inp_prefix",meta["nas_prefix"])
        if meta["nas_unc"]:
            G["nas_unc"]=meta["nas_unc"]
            dpg.set_value("inp_unc",meta["nas_unc"])
        dpg.configure_item("lbl_scan_info",default_value=f"Charge {meta['saved_at']}")
        dpg.configure_item("btn_save",enabled=True)
        render_results()
    except FileNotFoundError: modal_err("Aucun scan",f"Fichier attendu:\n{SCAN_FILE}")
    except Exception as e:    modal_err("Erreur",str(e))

# ══════════════════════════════════════════════════════════════
#  SCAN (thread → queue → UI)
# ══════════════════════════════════════════════════════════════
def _get_params():
    return {
        "url":    dpg.get_value("inp_url").strip().rstrip("/"),
        "key":    dpg.get_value("inp_key").strip(),
        "uid":    dpg.get_value("inp_uid").strip(),
        "prefix": dpg.get_value("inp_prefix").strip(),
        "unc":    dpg.get_value("inp_unc").strip(),
        "player": dpg.get_value("inp_player").strip(),
    }

def do_connect():
    """Vérifie la connexion et charge les médiathèques disponibles."""
    p = _get_params()
    if not p["url"] or not p["key"]:
        modal_err("Parametres manquants","L'URL et la cle API sont obligatoires.")
        return

    def thread():
        try:
            # Test connexion
            emby_get(p["url"],p["key"],"/System/Info/Public")
            # Récupérer les médiathèques
            libs_raw = emby_get(p["url"],p["key"],"/Library/VirtualFolders")
            libs = []
            for lib in libs_raw:
                lib_id   = lib.get("ItemId","") or lib.get("Id","")
                lib_name = lib.get("Name","?")
                lib_type = lib.get("CollectionType","")
                if lib_id:
                    libs.append({"id":lib_id,"name":lib_name,"type":lib_type})

            def on_done(libs=libs):
                G["libraries"] = libs
                # Tout sélectionner par défaut
                G["lib_selected"] = {lib["id"] for lib in libs}
                _rebuild_library_panel(libs)
                dpg.configure_item("btn_scan",enabled=True)
                dpg.configure_item("lbl_scan_info",
                    default_value=f"{len(libs)} mediatheque(s) trouvee(s)")
            ui(on_done)

        except urllib.error.HTTPError as e:
            msg = "Cle API invalide (401)." if e.code==401 else f"HTTP {e.code}: {e.reason}"
            ui(lambda m=msg: modal_err("Erreur connexion",m))
        except Exception as e:
            ui(lambda m=str(e): modal_err("Erreur connexion",m))

    threading.Thread(target=thread,daemon=True).start()


def _rebuild_library_panel(libs):
    """Affiche les mediatheques en grille de checkboxes cliquables."""
    dpg.delete_item("lib_panel", children_only=True)

    if not libs:
        dpg.add_text("Aucune mediatheque trouvee.", parent="lib_panel",
                     color=(200,80,80))
        return

    # Boutons tout cocher / tout décocher
    with dpg.group(horizontal=True, parent="lib_panel"):
        dpg.add_text("Mediatheques :", color=(136,136,170))
        dpg.add_spacer(width=8)
        dpg.add_button(label="Tout cocher", width=90,
            user_data=libs,
            callback=lambda s,a,u: _select_all_libs(u, True))
        dpg.add_button(label="Tout decocher", width=100,
            user_data=libs,
            callback=lambda s,a,u: _select_all_libs(u, False))
    dpg.add_spacer(height=4, parent="lib_panel")

    # Grille 4 colonnes dans un tableau DPG
    COLS = 4
    with dpg.table(parent="lib_panel", header_row=False,
                   policy=dpg.mvTable_SizingStretchSame):
        for _ in range(COLS):
            dpg.add_table_column()
        for i in range(0, len(libs), COLS):
            with dpg.table_row():
                batch = libs[i:i+COLS]
                for lib in batch:
                    icon = {"movies":"Films","tvshows":"Series","music":"Musique",
                            "books":"Livres","photos":"Photos"}.get(lib["type"],
                            lib["type"] or "?")
                    dpg.add_checkbox(
                        label=f"{lib['name']}  [{icon}]",
                        tag=f"chk_lib_{lib['id']}",
                        default_value=True,
                        user_data=lib["id"],
                        callback=lambda s,v,u: _toggle_lib(u, v))
                # Remplir cellules vides si rang incomplet
                for _ in range(COLS - len(batch)):
                    dpg.add_text("")


def _select_all_libs(libs, checked):
    """Coche ou décoche toutes les mediatheques."""
    for lib in libs:
        try: dpg.set_value(f"chk_lib_{lib['id']}", checked)
        except Exception: pass
        _toggle_lib(lib["id"], checked)


def _toggle_lib(lib_id, checked):
    if checked: G["lib_selected"].add(lib_id)
    else:       G["lib_selected"].discard(lib_id)



def start_scan():
    p = _get_params()
    if not p["url"] or not p["key"]:
        modal_err("Parametres manquants","L'URL et la cle API sont obligatoires.")
        return

    G["nas_prefix"]=p["prefix"]; G["nas_unc"]=p["unc"]; G["player"]=p["player"]
    save_config({"url":p["url"],"api_key":p["key"],"user_id":p["uid"],
                 "nas_prefix":p["prefix"],"nas_unc":p["unc"],"player":p["player"]})

    # Médiathèques sélectionnées (None = toutes si aucune sélection)
    parent_ids = list(G["lib_selected"]) if G["lib_selected"] else None

    dpg.configure_item("btn_scan",enabled=False,label="Scan...")
    dpg.configure_item("scan_popup",show=True)
    dpg.set_value("scan_step","Connexion au serveur...")
    dpg.set_value("scan_pb",0.0)

    def thread():
        def set_step(msg,pct):
            ui(lambda m=msg,pp=pct:(
                dpg.set_value("scan_step",m),
                dpg.set_value("scan_pb",pp)))

        try:
            set_step(f"Connexion a {p['url']}...",0.02)
            emby_get(p["url"],p["key"],"/System/Info/Public")

            scope_msg = (f"{len(parent_ids)} mediatheque(s)"
                         if parent_ids else "toutes les mediatheques")
            set_step(f"OK — recuperation films ({scope_msg})...",0.05)
            t0=time.time()

            def on_page(fetched,total,page):
                pct=0.05+0.55*(fetched/max(total,1))
                el=time.time()-t0; rate=fetched/el if el>0 else 0
                eta=(total-fetched)/rate if rate>0 else 0
                msg=(f"Page {page} — {fetched} films ({rate:.0f}/s)"
                     +(f" ~{eta:.0f}s" if eta>2 else ""))
                set_step(msg,pct)

            movies=fetch_movies(p["url"],p["key"],p["uid"],on_page,parent_ids)
            set_step(f"{len(movies)} films — analyse...",0.62)

            def on_step(idx,total,title):
                set_step(f"{idx}/{total} — {title}",0.62+0.35*(idx/max(total,1)))

            dupes,multiqual=find_duplicates(movies,on_step)
            set_step("Sauvegarde...",0.98)

            try:
                save_scan(dupes,multiqual,p["url"],p["prefix"],p["unc"])
                saved_msg=f"Sauvegarde {time.strftime('%d/%m/%Y %H:%M')}"
            except Exception as e:
                saved_msg=f"Erreur sauvegarde: {e}"

            def finish(d=dupes,mq=multiqual,sm=saved_msg):
                G["dupes"]=d; G["multiqual"]=mq
                dpg.set_value("scan_pb",1.0)
                dpg.configure_item("scan_popup",show=False)
                dpg.configure_item("btn_scan",enabled=True,label="Scanner")
                dpg.configure_item("btn_save",enabled=True)
                dpg.configure_item("lbl_scan_info",default_value=sm)
                render_results()
            ui(finish)

        except urllib.error.HTTPError as e:
            msg="Cle API invalide (401)." if e.code==401 else f"HTTP {e.code}: {e.reason}"
            ui(lambda m=msg: (modal_err("Erreur API",m),
                dpg.configure_item("scan_popup",show=False),
                dpg.configure_item("btn_scan",enabled=True,label="Scanner")))
        except Exception as e:
            msg=str(e)
            ui(lambda m=msg: (modal_err("Erreur scan",m),
                dpg.configure_item("scan_popup",show=False),
                dpg.configure_item("btn_scan",enabled=True,label="Scanner")))

    threading.Thread(target=thread,daemon=True).start()

# ══════════════════════════════════════════════════════════════
#  THEME
# ══════════════════════════════════════════════════════════════
def setup_theme():
    with dpg.theme() as t:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg,       (26,26,46))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg,         (22,33,62))
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg,         (22,33,62))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg,         (13,27,42))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered,  (20,40,60))
            dpg.add_theme_color(dpg.mvThemeCol_Button,          (15,52,96))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,   (26,80,144))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,    (233,69,96))
            dpg.add_theme_color(dpg.mvThemeCol_Header,          (15,52,96))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,   (26,80,144))
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg,   (15,52,96))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg,      (30,42,58))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt,   (25,34,50))
            dpg.add_theme_color(dpg.mvThemeCol_Text,            (224,224,224))
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled,    (136,136,170))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBg,         (15,52,96))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive,   (15,52,96))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg,     (13,27,42))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab,   (15,52,96))
            dpg.add_theme_color(dpg.mvThemeCol_Border,          (15,52,96))
            dpg.add_theme_color(dpg.mvThemeCol_Separator,       (15,52,96))
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,   4)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding,   6)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding,    6,4)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,     6,4)
    dpg.bind_theme(t)

# ══════════════════════════════════════════════════════════════
#  CONSTRUCTION UI
# ══════════════════════════════════════════════════════════════
def build_ui():
    # Popup scan
    with dpg.window(label="Scan en cours",tag="scan_popup",modal=True,
                    show=False,width=580,height=160,pos=[200,220],
                    no_close=True,no_resize=True):
        dpg.add_text("...",tag="scan_step",wrap=560)
        dpg.add_progress_bar(tag="scan_pb",default_value=0.0,width=-1,height=22)

    # Fenetre principale
    with dpg.window(tag="main_win",no_title_bar=True,no_move=True,
                    no_resize=True,no_scrollbar=True,no_scroll_with_mouse=True):

        # Titre + copyright
        with dpg.group(horizontal=True):
            dpg.add_text("Emby Duplicate Finder",color=(233,69,96))
            dpg.add_text("- lecture seule (DirectX 11)",color=(136,136,170))
            dpg.add_spacer(width=20)
            dpg.add_text("By Popov2026",color=(180,130,255))
            dpg.add_text("(c) 2026",color=(120,90,180))
        dpg.add_separator()

        # Ligne 1 : connexion
        with dpg.group(horizontal=True):
            dpg.add_text("URL")
            dpg.add_input_text(tag="inp_url",default_value=CFG["emby"]["url"],width=210)
            dpg.add_spacer(width=6)
            dpg.add_text("Cle API")
            dpg.add_input_text(tag="inp_key",default_value=CFG["emby"]["api_key"],
                               password=True,width=230)
            dpg.add_spacer(width=6)
            dpg.add_text("User ID")
            dpg.add_input_text(tag="inp_uid",default_value=CFG["emby"].get("user_id",""),
                               width=110,hint="optionnel")
        dpg.add_spacer(height=3)

        # Ligne 2 : NAS + lecteur
        with dpg.group(horizontal=True):
            dpg.add_text("Prefixe")
            dpg.add_input_text(tag="inp_prefix",
                               default_value=CFG["emby"].get("nas_prefix","/volume1"),width=110)
            dpg.add_text("->",color=(233,69,96))
            dpg.add_input_text(tag="inp_unc",
                               default_value=CFG["emby"].get("nas_unc",""),
                               width=200,hint=r"\\192.168.1.x")
            dpg.add_spacer(width=10)
            dpg.add_text("Lecteur video")
            dpg.add_input_text(tag="inp_player",
                               default_value=CFG["emby"].get("player",""),
                               width=200,hint="C:\\...\\vlc.exe")
            tip("Chemin vers votre lecteur video (VLC, MPC-HC...).\nModification prise en compte IMMEDIATEMENT sans relancer le script.")
            dpg.add_button(label="...",callback=browse_player,width=24)
            tip("Parcourir pour choisir le lecteur video.")
        dpg.add_spacer(height=3)

        # Ligne 3 : boutons principaux
        with dpg.group(horizontal=True):
            dpg.add_button(label="Connecter",tag="btn_connect",
                           callback=lambda s,a,u:do_connect(),width=100)
            tip("Verifie la connexion au serveur Emby\net charge la liste des mediatheques disponibles.\n\nIMPORTANT : si vous avez supprime des doublons manuellement,\nrafraichissez d'abord les mediatheques dans Emby avant de rescanner\n(Tableau de bord > Mediatheques > Analyser les mediatheques),\nsinon les fichiers supprimes apparaitront encore dans les resultats.", wrap=380)
            dpg.add_button(label="Scanner",tag="btn_scan",callback=start_scan,
                           width=100,enabled=False)
            tip("Lance le scan des mediatheques selectionnees.\nUtilisez Connecter d'abord pour choisir les mediatheques,\nou Charger scan pour reafficher un scan precedent sans reconnecter.")
            dpg.add_button(label="Sauvegarder",tag="btn_save",callback=do_save,
                           width=110,enabled=False)
            tip("Sauvegarde les resultats du scan dans un fichier JSON local.")
            dpg.add_button(label="Charger scan",callback=do_load,width=110)
            tip("Recharge les resultats du dernier scan sans reconnecter Emby.\nFonctionne hors ligne.")
            dpg.add_button(label="Exporter",callback=do_export,width=90)
            tip("Exporte le rapport des doublons en HTML ou CSV.")
            dpg.add_spacer(width=10)
            dpg.add_text("",tag="lbl_scan_info",color=(136,136,170))
        dpg.add_spacer(height=4)

        # Ligne 4 : médiathèques (remplie dynamiquement après Connecter)
        with dpg.child_window(tag="lib_panel",height=110,border=True,autosize_x=True):
            dpg.add_text("Cliquez sur Connecter pour charger les mediatheques.",
                         color=(136,136,170))
        dpg.add_spacer(height=4)

        # Ligne 5 : critères d'exclusion
        with dpg.collapsing_header(label="Criteres — versions intentionnelles (cocher = ignorer ce type de doublon)",
                                   default_open=True):
            dpg.add_spacer(height=3)
            with dpg.group(horizontal=True):
                CRITERIA = [
                    ("resolution","Resolution differente (4K/HD/SD)","resolution"),
                    ("hdr",       "HDR vs SDR",                      "hdr"),
                    ("av1",       "Codec AV1",                       "av1"),
                    ("3d",        "Film 3D / SBS / MVC",             "3d"),
                    ("remaster",  "Remastered",                      "remaster"),
                    ("cut",       "Version longue / Extended / Director's Cut","cut"),
                    ("bonus",     "Bonus / Extras / Featurette",               "bonus"),
                ]
                for tag_suffix, label, key in CRITERIA:
                    _tips_map = {
                        "resolution":"Coche = 4K et 1080p du meme film ne sont PAS des doublons.",
                        "hdr":       "Coche = version HDR et SDR ne sont PAS des doublons.",
                        "av1":       "Coche = fichier AV1 et H264/HEVC ne sont PAS des doublons.",
                        "3d":        "Coche = version 3D/SBS et version 2D ne sont PAS des doublons.",
                        "remaster":  "Coche = Remastered et original ne sont PAS des doublons.",
                        "cut":       "Coche = Extended, Director's Cut, Version Longue etc. ne sont PAS des doublons.",
                        "bonus":     "Coche = fichier Bonus/Extras/Featurette ne sont PAS des doublons.",
                    }
                    dpg.add_checkbox(
                        label=label, tag=f"chk_{tag_suffix}",
                        default_value=G["criteria"].get(key,True),
                        user_data=key,
                        callback=lambda s,v,u: _set_criterion(u,v))
                    tip(_tips_map.get(key,""), wrap=280)
                    dpg.add_spacer(width=14)
            dpg.add_spacer(height=3)
        dpg.add_spacer(height=3)

        # Ligne 6 : filtre + tri + seuil + ignorés
        with dpg.group(horizontal=True):
            dpg.add_text("Filtre :")
            dpg.add_input_text(tag="inp_filter",width=200,hint="Titre...",
                               callback=on_filter,on_enter=False)
            dpg.add_spacer(width=10)
            dpg.add_text("Trier :")
            dpg.add_combo(("Titre A>Z","Titre Z>A","Taille","Confiance v","Confiance ^"),
                          tag="cb_sort",default_value="Titre A>Z",
                          callback=on_sort,width=140)
            dpg.add_spacer(width=14)
            dpg.add_text("Seuil min :")
            dpg.add_slider_int(tag="sld_score",default_value=0,min_value=0,max_value=100,
                               width=140,callback=on_score_threshold)
            tip("Masque les groupes dont le score de confiance\nest inferieur a ce seuil.\n\n"
                "IMDB = 100%  (certain)\n"
                "TMDB = 85%   (probable)\n"
                "Titre+annee = 60%  (possible)\n"
                "Similarite = 40%  (incertain)\n\n"
                "Ex: seuil 85 = affiche seulement IMDB et TMDB.", wrap=300)
            dpg.add_text("0%",tag="lbl_score_val",color=(136,136,170))
            dpg.add_spacer(width=14)
            dpg.add_text("Ignores :")
            dpg.add_text(f"{len(G['ignored'])} ignore(s)",
                         tag="lbl_ignored",color=(230,126,34))
            dpg.add_spacer(width=4)
            dpg.add_button(label="Reinitialiser",callback=do_reset_ignored,width=100)

        dpg.add_separator()
        dpg.add_child_window(tag="results_area",border=False,autosize_x=True,height=-1)


# ══════════════════════════════════════════════════════════════
#  POINT D'ENTREE
# ══════════════════════════════════════════════════════════════
def main():
    dpg.create_context()
    setup_theme()

    dpg.create_viewport(title="Emby Duplicate Finder - DirectX 11",
                        width=1300,height=900,min_width=1024,min_height=640)
    build_ui()

    # Theme rouge pour barre de progression
    with dpg.theme() as pb_th:
        with dpg.theme_component(dpg.mvProgressBar):
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram,(233,69,96))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg,(40,20,30))
    dpg.bind_item_theme("scan_pb",pb_th)

    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main_win",True)

    def on_resize():
        vw=dpg.get_viewport_client_width()
        vh=dpg.get_viewport_client_height()
        dpg.set_item_width("main_win",vw)
        dpg.set_item_height("main_win",vh)
    dpg.set_viewport_resize_callback(on_resize)

    # ── Boucle principale ─────────────────────────────────────
    while dpg.is_dearpygui_running():
        # Vider la queue — toutes les modifs UI des threads s'exécutent ici
        while not _ui_queue.empty():
            try:
                _ui_queue.get_nowait()()
            except Exception:
                pass
        # Debounce : render_results() seulement si le timer a expiré
        global _render_timer
        if _render_timer > 0 and time.time() >= _render_timer:
            _render_timer = 0.0
            render_results()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    main()
