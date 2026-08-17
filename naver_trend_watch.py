#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
네이버 데이터랩 검색어트렌드 자동 감시
- keywords.json 의 그룹을 '한 번에 하나씩' 조회한다 (그룹마다 0~100 스케일을 독점해야
  작은 키워드가 큰 키워드에 눌려 바닥에 깔리는 문제가 안 생긴다)
- 각 그룹에 대해 모멘텀(최근 4주 vs 직전 4주), 전년 동기 대비, 계절 봉우리 시점을 계산
- result.json (기계용) + brief.md (사람용) 생성
"""

import json, os, time, datetime as dt
from urllib import request as urlreq
from urllib.error import HTTPError

# 2026-07-31 개발자센터 신규 신청 종료 → NAVER API HUB(네이버클라우드) 방식
API = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
KEY_ID = os.environ["NCP_API_KEY_ID"]      # X-NCP-APIGW-API-KEY-ID
KEY = os.environ["NCP_API_KEY"]            # X-NCP-APIGW-API-KEY

# 40~59세 = ages 코드 7,8,9,10 (1=0~12 … 7=40~44, 8=45~49, 9=50~54, 10=55~59, 11=60~)
AGES = ["7", "8", "9", "10"]
DEVICE = "mo"          # 모바일 검색 기준
YEARS = 2              # 계절성 보려면 최소 2년
LEAD_DAYS = 5          # 봉우리 며칠 전에 발행할지 (선점 발행 룰)


def call(group):
    """검색어 그룹 하나를 주간 단위로 조회"""
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=365 * YEARS)
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "week",
        "keywordGroups": [group],
        "device": DEVICE,
        "ages": AGES,
    }
    req = urlreq.Request(
        API,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "X-NCP-APIGW-API-KEY-ID": KEY_ID,
            "X-NCP-APIGW-API-KEY": KEY,
            "Content-Type": "application/json",
        },
    )
    # 429(한도 초과)는 백오프 후 재시도
    for attempt in range(3):
        try:
            with urlreq.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))["results"][0]["data"]
        except HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def avg(rows):
    return round(sum(r["ratio"] for r in rows) / len(rows), 1) if rows else 0.0


def analyze(name, data):
    """data: [{'period':'2026-08-10','ratio':12.3}, ...] 주간 오름차순"""
    if len(data) < 60:
        return None

    recent4, prev4 = data[-4:], data[-8:-4]
    # 전년 동기: 52주 전 기준 앞뒤 4주
    yoy4 = data[-56:-52]

    m_now, m_prev, m_yoy = avg(recent4), avg(prev4), avg(yoy4)
    momentum = round(m_now / m_prev, 2) if m_prev else None
    yoy = round(m_now / m_yoy, 2) if m_yoy else None

    # 계절 봉우리: 전체 구간 최고점의 '월-일'
    peak = max(data, key=lambda r: r["ratio"])
    pd = dt.date.fromisoformat(peak["period"])

    # 올해 기준 다음 봉우리까지 남은 날
    today = dt.date.today()
    try:
        this_year = pd.replace(year=today.year)
    except ValueError:                      # 2/29 방어
        this_year = pd.replace(year=today.year, day=28)
    nxt = this_year if this_year >= today else this_year.replace(year=today.year + 1)
    d_to_peak = (nxt - today).days
    publish_on = nxt - dt.timedelta(days=LEAD_DAYS)

    return {
        "group": name,
        "recent4": m_now,
        "prev4": m_prev,
        "momentum": momentum,          # 1.0 초과 = 지금 올라오는 중
        "yoy": yoy,                    # 1.0 초과 = 작년보다 커진 시장
        "peak_mmdd": pd.strftime("%m-%d"),
        "days_to_peak": d_to_peak,
        "publish_on": publish_on.isoformat(),
    }


def main():
    groups = json.load(open("keywords.json", encoding="utf-8"))["groups"]
    out, failed = [], []

    for g in groups:
        try:
            res = analyze(g["groupName"], call(g))
            if res:
                out.append(res)
        except HTTPError as e:
            # HUB는 오류를 {"error":{...}} 형태로도 준다
            try:
                detail = json.loads(e.read().decode("utf-8"))
            except Exception:
                detail = {}
            msg = (detail.get("error") or {}).get("message") or detail.get("errorMessage") or ""
            failed.append(f"{g['groupName']}: HTTP {e.code} {msg}")
        except Exception as e:
            failed.append(f"{g['groupName']}: {e}")
        time.sleep(0.3)                # 호출 간격 확보

    # 임박한 봉우리 순으로 정렬
    out.sort(key=lambda x: x["days_to_peak"])
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    json.dump(
        {"updated": stamp, "device": DEVICE, "ages": "40~59", "items": out, "failed": failed},
        open("result.json", "w", encoding="utf-8"),
        ensure_ascii=False, indent=2,
    )

    # 사람이 읽는 요약
    hot = [x for x in out if x["momentum"] and x["momentum"] >= 1.3]
    soon = [x for x in out if 0 <= x["days_to_peak"] <= 21]

    L = [f"# 검색 수요 감시 ({stamp} / 모바일·40~59세)", ""]
    L.append("## 지금 올라오는 중 (최근 4주 ÷ 직전 4주 ≥ 1.3)")
    L += [f"- **{x['group']}** ×{x['momentum']} (전년비 {x['yoy']})" for x in hot] or ["- 없음"]
    L += ["", "## 3주 안에 봉우리 (발행 예정일)"]
    L += [f"- **{x['group']}** — {x['days_to_peak']}일 뒤 정점, **{x['publish_on']} 발행**"
          for x in soon] or ["- 없음"]
    L += ["", "## 전체 캘린더 (봉우리 순)"]
    L += [f"- {x['group']}: 매년 {x['peak_mmdd']} 전후 / 다음 발행 {x['publish_on']}" for x in out]
    if failed:
        L += ["", "## 조회 실패", *[f"- {f}" for f in failed]]

    brief = "\n".join(L)
    open("brief.md", "w", encoding="utf-8").write(brief)
    print(brief)

    # 텔레그램은 설정돼 있을 때만
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if tok and chat:
        msg = "[검색수요 감시] " + brief[:3500]
        urlreq.urlopen(
            urlreq.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                data=json.dumps({"chat_id": chat, "text": msg}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ), timeout=15)


if __name__ == "__main__":
    main()
