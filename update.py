"""사회인야구 개인 홈페이지 데이터 업데이트 스크립트.

안양리그(alb.or.kr)와 시흥시야구소프트볼협회(shbaseball.co.kr)에서
내 팀들의 경기기록·선수기록을 모아 data.js / data.json 으로 저장한다.

사용법:  py update.py          (또는 '업데이트.bat' 더블클릭)
설정:    config.json 의 seasons(가져올 연도), teams(팀 목록)
외부 라이브러리 없이 파이썬 기본 기능만 사용한다.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

BASE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) personal-baseball-page"
ALB = "http://www.alb.or.kr"  # 이 사이트의 https 는 오래된 암호화라 파이썬이 거부함
SH = "https://www.shbaseball.co.kr"

errors = []


def log(msg):
    print(msg, flush=True)


def fetch(url, encoding="utf-8", tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode(encoding, errors="replace")
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(1.5)
    return ""


# ---------------------------------------------------------------- HTML 표 파서
class TableParser(HTMLParser):
    """페이지의 모든 <table>을 [행][셀] 텍스트 목록으로 바꾼다 (중첩 표 지원)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.stack.append({"rows": [], "row": None, "cell": None})
        elif not self.stack:
            return
        elif tag == "tr":
            self.stack[-1]["row"] = []
        elif tag in ("td", "th") and self.stack[-1]["row"] is not None:
            self.stack[-1]["cell"] = []
        elif tag == "br" and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"].append(" ")

    def handle_endtag(self, tag):
        if not self.stack:
            return
        t = self.stack[-1]
        if tag in ("td", "th") and t["cell"] is not None and t["row"] is not None:
            t["row"].append(re.sub(r"\s+", " ", "".join(t["cell"])).strip())
            t["cell"] = None
        elif tag == "tr" and t["row"] is not None:
            t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table":
            self.tables.append(self.stack.pop()["rows"])

    def handle_data(self, data):
        if self.stack and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"].append(data)


def tables_of(html):
    p = TableParser()
    p.feed(html)
    return p.tables


def find_table(tables, *must):
    """첫 행(헤더)에 must 문자열이 모두 들어있는 표를 찾는다."""
    for t in tables:
        for hi, row in enumerate(t[:3]):
            joined = "|".join(row)
            if all(m in joined for m in must):
                return row, [r for r in t[hi + 1:] if any(c for c in r)]
    return None, []


def num(v):
    v = (v or "").strip()
    if v in ("", "-"):
        return None
    try:
        return int(v) if re.fullmatch(r"-?\d+", v) else float(v)
    except ValueError:
        return v


def ip_text(v):
    """이닝 표기 통일: '4 ⅔' / '4.2' -> '4 2/3'."""
    v = (v or "").strip()
    if not v:
        return ""
    v = v.replace("⅓", " 1/3").replace("⅔", " 2/3")
    m = re.fullmatch(r"(\d+)\.([012])", v)
    if m:
        v = m.group(1) + (f" {m.group(2)}/3" if m.group(2) != "0" else "")
    return re.sub(r"\s+", " ", v).strip()


def ip_outs(v):
    v = ip_text(v)
    m = re.fullmatch(r"(\d+)(?: ([12])/3)?", v)
    if not m:
        return 0
    return int(m.group(1)) * 3 + int(m.group(2) or 0)


def add_rates(b):
    """OBP/SLG/OPS 가 없으면 계산한다."""
    ab, h = b.get("AB") or 0, b.get("H") or 0
    bb, hbp = b.get("BB") or 0, b.get("HBP") or 0
    d2, d3, hr = b.get("2B") or 0, b.get("3B") or 0, b.get("HR") or 0
    sf = b.get("SF") or 0
    if b.get("AVG") is None:
        b["AVG"] = round(h / ab, 3) if ab else 0.0
    if b.get("OBP") is None:
        den = ab + bb + hbp + sf
        b["OBP"] = round((h + bb + hbp) / den, 3) if den else 0.0
    if b.get("SLG") is None:
        tb = h + d2 + 2 * d3 + 3 * hr
        b["SLG"] = round(tb / ab, 3) if ab else 0.0
    if b.get("OPS") is None:
        b["OPS"] = round((b["OBP"] or 0) + (b["SLG"] or 0), 3)
    return b


# ================================================================ 안양리그 (ALB)
def alb_q(s):
    return urllib.parse.quote(s, encoding="cp949")


ALB_BAT_HEAD = ["R.", "PLAYERS", "TEAMS", "TG", "PG", "AVG", "OBP", "SLG", "OPS", "TPA", "AB",
                "HIT", "2B", "3B", "HR", "BB", "HBP", "SFH", "SO", "RBI", "RUN", "SB", "CS",
                "ERR", "QYTD"]
ALB_PIT_HEAD = ["R.", "PLAYERS", "TEAMS", "TG", "PG", "IP", "TBF", "NP", "WIN", "LOSS", "SAVE",
                "HOLD", "HIT", "HR", "BB", "HBP", "SO", "R", "ER", "ERA", "AVG", "OBP", "QYTD"]
ALB_BAT = {"TG": None, "PG": "G", "AVG": "AVG", "OBP": "OBP", "SLG": "SLG", "OPS": "OPS",
           "TPA": "PA", "AB": "AB", "HIT": "H", "2B": "2B", "3B": "3B", "HR": "HR",
           "BB": "BB", "HBP": "HBP", "SFH": "SAC", "SO": "SO", "RBI": "RBI", "RUN": "R",
           "SB": "SB", "CS": "CS", "ERR": "E"}
ALB_PIT = {"PG": "G", "IP": "IP", "TBF": "TBF", "NP": "NP", "WIN": "W", "LOSS": "L",
           "SAVE": "SV", "HOLD": "HLD", "HIT": "H", "HR": "HR", "BB": "BB", "HBP": "HBP",
           "SO": "SO", "R": "R", "ER": "ER", "ERA": "ERA"}


def alb_roster(team):
    url = (f"{ALB}/s/teams/index.php?id=teams_players&sc={team['sc']}"
           f"&team={alb_q(team['name'])}&order=xb&gyear={datetime.now().year}")
    head, rows = find_table(tables_of(fetch(url, "cp949")), "선수명", "배번", "포지션")
    roster = {}
    if head:
        ix = {}
        for i, h in enumerate(head):
            ix.setdefault(h, i)  # '선수명' 열이 두 번 나온다: 앞의 것 사용
        for r in rows:
            if len(r) <= ix["배번"]:
                continue
            name = re.sub(r"\s*\(.*?\)", "", r[ix["선수명"]]).strip()
            roster[name] = {"no": r[ix["배번"]], "pos": r[ix["포지션"]].replace("수수", "수"),
                            "role": r[ix.get("직책", 1)]}
    return roster, url


def alb_stats(team, season, comp, kind):
    page = "stand_bat" if kind == "bat" else "stand_pit"
    order = "order=avg&sort=desc" if kind == "bat" else "order=era&sort=asc"
    url = (f"{ALB}/s/{page}/index.php?id=stats&league={alb_q(comp)}&sc={team['sc']}"
           f"&gyear={season}&{order}&mode=total&team={alb_q(team['name'])}")
    html = fetch(url, "cp949")
    out = []
    # 정렬 중인 열은 헤더 모양이 달라 자동 인식이 불안정하므로 고정 열 순서를 쓴다.
    head = ALB_BAT_HEAD if kind == "bat" else ALB_PIT_HEAD
    # 이 페이지는 표 태그가 깨져 있어 선수 행(class=list2)을 직접 읽는다.
    rows = []
    for tr in re.findall(r"<tr[^>]*class=list2[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
        rows.append([re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", c))).strip()
                     for c in cells])
    mp = ALB_BAT if kind == "bat" else ALB_PIT
    for r in rows:
        if len(r) != len(head):
            if len(r) > 3 and team["name"] in r:
                errors.append(f"{team['name']} {season} {comp} {kind}: 열 개수 {len(r)} (예상 {len(head)}) - 사이트 형식 변경 확인 필요")
            continue
        d = dict(zip(head, r))
        if d.get("TEAMS", "").strip() != team["name"]:
            continue
        p = {"name": d["PLAYERS"].strip()}
        for src, dst in mp.items():
            if dst and src in d:
                p[dst] = ip_text(d[src]) if dst == "IP" else num(d[src])
        out.append(add_rates(p) if kind == "bat" else p)
    return out, url


def alb_team(team, seasons):
    log(f"[안양리그] {team['name']}")
    res = {"games": [], "stats": [], "roster": {}}
    try:
        res["roster"], res["rosterUrl"] = alb_roster(team)
    except Exception as e:  # noqa: BLE001
        errors.append(f"{team['name']} 선수명단: {e}")

    url = (f"{ALB}/s/team_stats/index.php?id=stats&sc={team['sc']}"
           f"&gyear={max(seasons)}&team={alb_q(team['name'])}")
    res["teamUrl"] = url
    res["teamLinks"] = [{"label": team.get("league", "안양리그"), "url": url}]
    head, rows = find_table(tables_of(fetch(url, "cp949")), "대회명", "경기일", "상대팀", "승부")
    if head:
        ix = {h: i for i, h in enumerate(head)}
        for r in rows:
            if len(r) < len(head):
                continue
            g = lambda k: r[ix[k]] if k in ix else ""  # noqa: E731
            season = num(g("소속연도"))
            if season not in seasons:
                continue
            y, m, dd = (g("경기일").split("/") + ["", "", ""])[:3]
            date = f"{y}-{int(m):02d}-{int(dd):02d}" if m and dd else g("경기일")
            rf, ra = num(g("득점")), num(g("실점"))
            res["games"].append({
                "season": season, "date": date, "time": "", "comp": g("대회명"),
                "opp": g("상대팀"), "home": None, "rf": rf, "ra": ra,
                "result": g("승부"), "note": "", "place": "",
                "bat": {"PA": num(g("타석")), "AB": num(g("타수")), "H": num(g("안타")),
                        "HR": num(g("홈런")), "AVG": num(g("타율")), "E": num(g("실책"))},
                "pit": {"IP": ip_text(g("이닝")), "ER": num(g("자책점")), "ERA": num(g("방어율"))},
                "link": "",
            })
    log(f"  경기 {len(res['games'])}건")

    for season in seasons:
        for comp in team.get("competitions", ["연합회장기"]):
            entry = {"season": season, "comp": comp, "batters": [], "pitchers": []}
            for kind in ("bat", "pit"):
                try:
                    players, src = alb_stats(team, season, comp, kind)
                    entry["batters" if kind == "bat" else "pitchers"] = players
                    entry["src_" + kind] = src
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{team['name']} {season} {comp} {kind}: {e}")
            if entry["batters"] or entry["pitchers"]:
                for p in entry["batters"] + entry["pitchers"]:
                    info = res["roster"].get(p["name"])
                    if info:
                        p["no"] = info["no"]
                res["stats"].append(entry)
                log(f"  {season} {comp}: 타자 {len(entry['batters'])} / 투수 {len(entry['pitchers'])}")

    res["standings"] = []
    for season in seasons:
        for comp in team.get("competitions", ["연합회장기"]):
            if comp == "플레이오프":
                continue  # 토너먼트라 순위표가 없음
            try:
                st = alb_standings(team, season, comp)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{team['name']} {season} {comp} 순위: {e}")
                continue
            if st:
                res["standings"].append(st)
                log(f"  {season} {comp} 순위: {st['myRank']} / {len(st['rows'])}팀")
    return res


def alb_standings(team, season, comp):
    """안양리그 팀순위: 대회의 모든 리그(비산·호계·평촌…) 순위표. 우리 팀 리그가 기본."""
    url = (f"{ALB}/s/stand_team/stand_team_main_new.php?sc={team['sc']}"
           f"&league={alb_q(comp)}&gyear={season}")
    groups, mine, my_group = [], None, ""
    for t in tables_of(fetch(url, "cp949")):
        rows = [r for r in t if any(c for c in r)]
        heads = [r for r in rows if len(r) >= 6 and r[0] == "R."]
        if len(heads) != 1:
            continue  # 리그 하나짜리 표만 (바깥 틀 표 제외)
        name, section, out = heads[0][1], "", []
        for r in rows:
            if len(r) == 1:
                section = r[0]
            elif len(r) >= 6 and r[0] != "R." and r[1]:
                w, d, l = num(r[3]) or 0, num(r[4]) or 0, num(r[5]) or 0
                row = {"section": section, "rank": num(r[0]), "team": r[1], "G": num(r[2]),
                       "W": w, "L": l, "D": d, "PCT": round(w / (w + l), 3) if w + l else 0.0,
                       "mine": r[1] == team["name"]}
                if row["mine"]:
                    mine, my_group = row, name
                out.append(row)
        if out and name not in [g["name"] for g in groups]:
            groups.append({"name": name, "rows": out})
    if not mine or not (mine["G"] or 0):
        return None  # 그 시즌 그 대회에 참가하지 않음
    rank = f"{mine['rank']}위" + (f" ({mine['section']})" if mine["section"] else "")
    rows = next(g["rows"] for g in groups if g["name"] == my_group)
    return {"season": season, "comp": comp, "group": my_group, "groups": groups, "rows": rows,
            "myRank": rank, "src": url, "cols": ["G", "W", "D", "L", "PCT"]}


# ================================================================ 시흥시 (SH)
SH_BAT_EN = {"AVG": "AVG", "G": "G", "PA": "PA", "AB": "AB", "H": "H", "2B": "2B", "3B": "3B",
             "HR": "HR", "RBI": "RBI", "R": "R", "SB": "SB", "BB": "BB", "HBP": "HBP",
             "SO": "SO", "C.S": "CS", "S.G": "SAC", "S.F": "SF", "D.P": "GDP",
             "SLG": "SLG", "OBP": "OBP"}
SH_PIT_KO = {"평균자책점": "ERA", "경기": "G", "승리": "W", "패배": "L", "세이브": "SV", "홀드": "HLD",
             "이닝": "IP", "피안타": "H", "피홈런": "HR", "볼넷": "BB", "사구": "HBP", "삼진": "SO",
             "실점": "R", "자책점": "ER", "상대타자수": "TBF", "투구수": "NP"}


def split_name(s):
    m = re.match(r"(.*?)\s*\((\w*)\)\s*$", s)
    return (m.group(1).strip(), m.group(2)) if m else (s.strip(), "")


def sh_players(html, kind):
    tabs = [t for t in tables_of(html) if len(t) >= 1]
    if kind == "bat":
        names_h, names = find_table(tabs, "선수명")
        # 상세표: 영문 약어가 들어있는 헤더 + 'SLG'
        head, rows = find_table(tabs, "SLG", "OBP")
    else:
        names_h, names = find_table(tabs, "선수명")
        head, rows = find_table(tabs, "평균자책점", "홀드")
        if not head:
            head, rows = find_table(tabs, "평균자책점", "이닝")
    if not head or not names_h:
        return []
    ni = names_h.index("선수명")
    keys = []
    for h in head:
        if kind == "bat":
            m = re.search(r"\b([A-Z0-9][A-Z0-9./]*) \(", h)
            keys.append(SH_BAT_EN.get(m.group(1)) if m else None)
        else:
            keys.append(SH_PIT_KO.get(h.strip()))
    out = []
    for nr, r in zip(names, rows):
        if len(nr) <= ni or len(r) < len(head) or not nr[ni]:
            continue
        name, no = split_name(nr[ni])
        p = {"name": name, "no": no}
        for k, v in zip(keys, r):
            if not k or k in p:
                continue
            if k == "HR":
                v = v.split("(")[0]
            p[k] = ip_text(v) if k == "IP" else num(v)
        out.append(add_rates(p) if kind == "bat" else p)
    return out


GAME_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)


def sh_games(seq, seasons, base=SH):
    url = (f"{base}/teamPage/scheduleRecord/getGameRecord.hs?teamSeq={seq}"
           f"&searchStartGameDate={min(seasons)}-01-01&searchEndGameDate={max(seasons)}-12-31"
           f"&rowSize=300")
    html = fetch(url)
    leagues = re.findall(r'<option value="(\d+)" data-league-year="(\d+)"[^>]*>([^<]+)', html)
    games = []
    for tr in GAME_ROW.findall(html):
        if 'class="versus"' not in tr:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        txt = lambda s: re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()  # noqa: E731
        when = txt(tds[0])
        teams = re.findall(r'teamSeq=(\d+)"[^>]*>([^<]+)</a>', tr)
        scores = re.findall(r'<span class="score[^"]*">([^<]*)</span>', tr)
        label = re.search(r'<span class="result-label">([^<]*)</span>', tr)
        link = re.search(r'href="(/schedule/getGameRecord\.hs\?[^"]+)"', tr)
        hl = re.search(r'href="(/community/highlight/[^"]+)"', tr)
        if len(teams) < 2:
            continue
        mine = 0 if teams[0][0] == str(seq) else 1
        opp = teams[1 - mine][1].strip()
        sc = [num(s) for s in scores] + [None, None]
        rf, ra = sc[mine], sc[1 - mine]
        if rf is None or ra is None:
            result = "예정"
        else:
            result = "승" if rf > ra else "패" if rf < ra else "무"
        note = label.group(1).strip() if label else ""
        if "기권승" in note:
            result = "승"
        elif "기권패" in note:
            result = "패"
        d = when.split(" ")
        games.append({
            "season": int(d[0][:4]), "date": d[0], "time": d[1] if len(d) > 1 else "",
            "comp": txt(tds[1]), "place": txt(tds[2]), "opp": opp,
            "home": mine == 1, "rf": rf, "ra": ra, "result": result, "note": note,
            "link": base + unescape(link.group(1)) if link else "",
            "highlight": base + unescape(hl.group(1)) if hl else "",
        })
    games = [g for g in games if g["season"] in seasons]
    return games, leagues, url


def sh_sources(team):
    """기본 사이트(시흥시) + config 의 extra 에 적은 같은 방식(SPOBEE) 사이트들."""
    yield {"base": SH, "teamSeq": team["teamSeq"], "league": team.get("league", "").split(" · ")[0],
           "site": "시흥시야구소프트볼협회"}
    for x in team.get("extra", []):
        yield {"base": x["base"].rstrip("/"), "teamSeq": x["teamSeq"],
               "league": x.get("league", ""), "site": x.get("site", x["base"])}


def sh_team(team, seasons):
    res = {"games": [], "stats": [], "teamLinks": []}
    for src_site in sh_sources(team):
        base, seq = src_site["base"], src_site["teamSeq"]
        log(f"[{src_site['site']}] {team['name']}")
        res["teamLinks"].append({"label": src_site["league"] or src_site["site"],
                                 "url": f"{base}/teamPage/main/getMain.hs?teamSeq={seq}"})
        try:
            games, leagues, _ = sh_games(seq, seasons, base)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{team['name']} ({src_site['site']}) 경기기록: {e}")
            continue
        res["games"] += games
        log(f"  경기 {len(games)}건")
        for lseq, year, label in leagues:
            year = int(year)
            if year not in seasons:
                continue
            comp = re.sub(r"\s*\(\d{4}\)\s*$", "", unescape(label)).strip()
            entry = {"season": year, "comp": comp, "batters": [], "pitchers": []}
            for kind, page in (("bat", "getBatterRecord"), ("pit", "getPitcherRecord")):
                src = (f"{base}/teamPage/scheduleRecord/{page}.hs?teamSeq={seq}"
                       f"&searchYear={year}&searchLowerLeagueSeq={lseq}")
                try:
                    entry["batters" if kind == "bat" else "pitchers"] = sh_players(fetch(src), kind)
                    entry["src_" + kind] = src
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{team['name']} {year} {comp} {kind}: {e}")
            if entry["batters"] or entry["pitchers"]:
                res["stats"].append(entry)
                log(f"  {year} {comp}: 타자 {len(entry['batters'])} / 투수 {len(entry['pitchers'])}")
        # 팀 순위
        for year in sorted({int(y) for _, y, _ in leagues if int(y) in seasons}):
            try:
                lmap = sh_league_map(base, seq, year)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{team['name']} {year} 리그목록: {e}")
                continue
            for lseq, y, label in leagues:
                if int(y) != year or lseq not in lmap:
                    continue
                comp = re.sub(r"\s*\(\d{4}\)\s*$", "", unescape(label)).strip()
                try:
                    st = sh_standings(base, seq, year, lmap[lseq], lseq, comp)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{team['name']} {year} {comp} 순위: {e}")
                    continue
                if st:
                    res.setdefault("standings", []).append(st)
                    log(f"  {year} {comp} 순위: {st['myRank']} / {len(st['rows'])}팀")
    res["teamUrl"] = res["teamLinks"][0]["url"]
    return res


def _select_opts(html, name):
    m = re.search(r'<select[^>]*name="' + name + r'"[^>]*>(.*?)</select>', html, re.S)
    return re.findall(r'<option value="(\d+)"[^>]*>([^<]*)', m.group(1)) if m else []


def sh_league_map(base, seq, year):
    """하위 리그 번호 -> 상위 리그 번호 (순위 페이지는 둘 다 필요)."""
    page = f"{base}/teamPage/scheduleRecord/getBatterRecord.hs?teamSeq={seq}&searchYear={year}"
    out = {}
    for top, _ in _select_opts(fetch(page), "searchTopLeagueSeq"):
        for low, _ in _select_opts(fetch(f"{page}&searchTopLeagueSeq={top}"), "searchLowerLeagueSeq"):
            out[low] = top
    return out


SH_RANK = {"승점": "PTS", "경기": "G", "승": "W", "패": "L", "무": "D", "득점": "RS", "실점": "RA",
           "최근 10경기": "L10", "연속": "STRK"}


def sh_standings(base, seq, year, top, low, comp):
    url = (f"{base}/record/getTeamRankList.hs?leagueCategory=NORMAL&searchYear={year}"
           f"&searchTopLeagueSeq={top}&searchLowerLeagueSeq={low}")
    html = fetch(url)
    head, rows = find_table(tables_of(html), "순위", "팀명", "경기")
    if not head:
        return None
    # 표 행 순서대로 팀 번호 (data-team-seq) 를 읽어 우리 팀을 찾는다.
    body = html[html.find("팀명"):]
    seqs = re.findall(r'class="lnk-go-detail" data-team-seq="(\d+)"', body)
    keys = [SH_RANK.get(re.sub(r"\s*[▼▲]\s*", "", h).strip()) for h in head]
    out, mine = [], None
    for i, r in enumerate(rows):
        if len(r) < len(head) or not r[1]:
            continue
        row = {"section": "", "rank": num(r[0]) or len(out) + 1, "team": r[1],
               "mine": i < len(seqs) and seqs[i] == str(seq)}
        for k, v in zip(keys, r):
            if k:
                row[k] = v if k in ("L10", "STRK") else num(v)
        w, l = row.get("W") or 0, row.get("L") or 0
        row["PCT"] = round(w / (w + l), 3) if w + l else 0.0
        if row["mine"]:
            mine = row
        out.append(row)
    if not mine:
        return None
    return {"season": year, "comp": comp, "group": "", "rows": out, "myRank": f"{mine['rank']}위",
            "src": url, "cols": ["PTS", "G", "W", "D", "L", "PCT", "RS", "RA", "L10", "STRK"]}


# ================================================================ 공개용 사이트 (GitHub Pages)
def build_site(out, js):
    """docs/ 폴더에 공개용 페이지를 만든다. GitHub Pages 가 이 폴더를 그대로 보여준다."""
    docs = BASE / "docs"
    docs.mkdir(exist_ok=True)
    page = (BASE / "index.html").read_text(encoding="utf-8")
    names = " · ".join(t["name"] for t in out["teams"])
    title = out.get("owner") or "나의 사회인야구"
    desc = f"{names} 경기기록·선수기록 (업데이트 {out['updated']})"
    esc = lambda s: s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")  # noqa: E731
    page = page.replace('<meta charset="utf-8">\n', "", 1)
    head = ("<!doctype html>\n<html lang=\"ko\">\n<head>\n<meta charset=\"utf-8\">\n"
            f'<meta property="og:type" content="website">\n'
            f'<meta property="og:title" content="{esc(title)}">\n'
            f'<meta property="og:description" content="{esc(desc)}">\n'
            f'<meta name="description" content="{esc(desc)}">\n')
    # index.html 의 <style> 이 끝나는 곳까지를 head 로, 나머지를 body 로 감싼다.
    # 데이터가 바뀔 때마다 주소를 바꿔, 브라우저에 남은 옛 기록 대신 새 기록을 읽게 한다.
    ver = re.sub(r"\D", "", out["updated"])
    page = page.replace('<script src="data.js"></script>', f'<script src="data.js?v={ver}"></script>')
    page = page.replace('fetch("data.json")', f'fetch("data.json?v={ver}")')
    cut = page.index("</style>") + len("</style>")
    html = head + page[:cut] + "\n</head>\n<body>\n" + page[cut:] + "\n</body>\n</html>\n"
    (docs / "index.html").write_text(html, encoding="utf-8")
    (docs / "data.js").write_text("window.BASEBALL_DATA = " + js + ";\n", encoding="utf-8")
    (docs / "data.json").write_text(js, encoding="utf-8")
    (docs / ".nojekyll").write_text("", encoding="utf-8")


# ================================================================ 메인
def main():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    seasons = [int(s) for s in cfg.get("seasons", [datetime.now().year])]
    out = {"owner": cfg.get("owner", ""), "myName": cfg.get("myName", ""), "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "seasons": sorted(seasons, reverse=True), "teams": [], "errors": errors}
    for t in cfg["teams"]:
        base = {k: t.get(k) for k in ("id", "name", "site", "league", "division")}
        try:
            data = alb_team(t, seasons) if t["site"] == "alb" else sh_team(t, seasons)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t['name']}: {e}")
            data = {"games": [], "stats": []}
        data["games"].sort(key=lambda g: (g["date"], g.get("time", "")), reverse=True)
        out["teams"].append({**base, **data})

    js = json.dumps(out, ensure_ascii=False, indent=1)
    (BASE / "data.json").write_text(js, encoding="utf-8")
    (BASE / "data.js").write_text("window.BASEBALL_DATA = " + js + ";\n", encoding="utf-8")
    build_site(out, js)
    log(f"\n완료: {out['updated']}  → data.js, docs/ 저장")
    if errors:
        log("주의 - 일부 항목을 가져오지 못했습니다:")
        for e in errors:
            log("  - " + e)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
