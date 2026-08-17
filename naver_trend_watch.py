#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
네이버 데이터랩 검색어트렌드 자동 감시 (v2)

v1 대비 변경점
1) 봉우리를 월별로 집계해 '연 2~3회 봉우리'까지 잡는다 (재산세 7·9월, 자동차세 1·6·12월)
2) history.jsonl 에 매 실행 결과를 누적 → 다음 주부터 '지난주 대비' 산출 가능
3) keywords.json 의 "lunar": true 그룹은 봉우리 날짜·전년비를 신뢰하지 않는다고 명시
4) peak_pct(자기 2년 최고치 대비 현재 위치) 추가 → 계절 봉우리가 없는 상시 키워드도 포착

주의: 그룹마다 API를 따로 호출하므로 ratio 는 '그룹 내부 0~100'이다.
      그룹 간 ratio 크기 비교는 성립하지 않는다. (brief.md 상단에도 명시)
"""

import json, os, time, datetime as dt
from collections import defaultdict
from urllib import request as urlreq
from urllib.error import HTTPError

API = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
KEY_ID = os.environ["NCP_API_KEY_ID"]
KEY = os.environ["NCP_API_KEY"]

AGES = ["7", "8", "9", "10"]      # 40~44, 45~49, 50~54, 55~59
DEVICE = "mo"
YEARS = 2
LEAD_DAYS = 5                     # 봉우리 며칠 전에 발행할지
PEAK_FLOOR = 0.45                 # 최대 봉우리의 45% 이상인 달만 '유효 봉우리'


def call(group):
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=365 * YEARS)
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "timeUnit": "week",
        # lunar 같은 우리쪽 메타키는 빼고 API 규격만 보낸다
        "keywordGroups": [{"groupName": group["groupName"], "keywords": group["keywords"]}],
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


def find_peaks(data):
    """월별 평균으로 후보 달을 고르고, 각 달에서 실제 최고 주의 날짜를 뽑는다"""
    by_month = defaultdict(list)
    for r in data:
        by_month[dt.date.fromisoformat(r["period"]).month].append(r)

    month_avg = {m: avg(rows) for m, rows in by_month.items()}
    top = max(month_avg.values()) or 1

    peaks = []
    for m in sorted(month_avg, key=month_avg.get, reverse=True)[:4]:
        if month_avg[m] < top * PEAK_FLOOR:
            continue
        best = max(by_month[m], key=lambda r: r["ratio"])
        peaks.append({
            "mmdd": dt.date.fromisoformat(best["period"]).strftime("%m-%d"),
            "month_avg": month_avg[m],
            "share": round(month_avg[m] / top, 2),   # 1.0 = 최대 봉우리
        })
    return sorted(peaks, key=lambda p: p["mmdd"])


def next_date(mmdd, today):
    mm, dd = int(mmdd[:2]), int(mmdd[3:])
    try:
        d = dt.date(today.year, mm, dd)
    except ValueError:
        d = dt.date(today.year, mm, 28)
    return d if d >= today else d.replace(year=today.year + 1)


def analyze(g, data, today):
    if len(data) < 60:
        return None

    m_now, m_prev = avg(data[-4:]), avg(data[-8:-4])
    m_yoy = avg(data[-56:-52])
    all_max = max(r["ratio"] for r in data) or 1

    peaks = find_peaks(data)
    lunar = bool(g.get("lunar"))

    # 가장 가까운 봉우리 기준으로 발행일 산출 (음력 그룹은 산출하지 않음)
    schedule = []
    for p in peaks:
        nd = next_date(p["mmdd"], today)
        schedule.append({
            "peak_mmdd": p["mmdd"],
            "share": p["share"],
            "days_to_peak": (nd - today).days,
            "publish_on": (nd - dt.timedelta(days=LEAD_DAYS)).isoformat(),
        })
    schedule.sort(key=lambda s: s["days_to_peak"])

    return {
        "group": g["groupName"],
        "lunar": lunar,
        "recent4": m_now,
        "prev4": m_prev,
        "momentum": round(m_now / m_prev, 2) if m_prev else None,
        "yoy": None if lunar else (round(m_now / m_yoy, 2) if m_yoy else None),
        "peak_pct": round(m_now / all_max * 100),      # 자기 2년 최고치 대비 현재 위치(%)
        "peaks": schedule,
        "next": schedule[0] if schedule else None,
    }


def load_prev():
    if not os.path.exists("history.jsonl"):
        return {}
    lines = [l for l in open("history.jsonl", encoding="utf-8").read().splitlines() if l.strip()]
    if not lines:
        return {}
    last = json.loads(lines[-1])
    return {i["group"]: i for i in last.get("items", [])}


def main():
    cfg = json.load(open("keywords.json", encoding="utf-8"))
    today = dt.date.today()
    out, failed = [], []

    for g in cfg["groups"]:
        try:
            res = analyze(g, call(g), today)
            if res:
                out.append(res)
        except HTTPError as e:
            try:
                d = json.loads(e.read().decode("utf-8"))
            except Exception:
                d = {}
            msg = (d.get("error") or {}).get("message") or d.get("errorMessage") or ""
            failed.append(f"{g['groupName']}: HTTP {e.code} {msg}")
        except Exception as e:
            failed.append(f"{g['groupName']}: {e}")
        time.sleep(0.3)

    prev = load_prev()
    for x in out:                                   # 지난 실행 대비 변화
        p = prev.get(x["group"])
        x["d_momentum"] = round(x["momentum"] - p["momentum"], 2) if p and p.get("momentum") and x["momentum"] else None
        x["d_peak_pct"] = x["peak_pct"] - p["peak_pct"] if p and p.get("peak_pct") is not None else None

    out.sort(key=lambda x: x["next"]["days_to_peak"] if x["next"] else 999)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {"updated": stamp, "device": DEVICE, "ages": "40~59",
               "items": out, "failed": failed}

    json.dump(payload, open("result.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    with open("history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    # ---------- 사람이 읽는 요약 ----------
    L = [f"# 검색 수요 감시 ({stamp} / 모바일·40~59세)", "",
         "> **해석 규칙** ①ratio는 그룹마다 따로 조회한 값이라 **그룹 간 크기 비교는 성립하지 않는다.** "
         "②검색 건수가 아니라 자기 구간 최대치를 100으로 둔 지수다. "
         "③lunar 표시된 그룹은 봉우리 날짜와 전년비를 신뢰하지 말고 실제 명절 날짜에서 역산할 것.", ""]

    due = [x for x in out if x["next"] and 0 <= x["next"]["days_to_peak"] <= 14 and not x["lunar"]]
    L += ["## 2주 안에 발행할 것"]
    L += [f"- **{x['group']}** — {x['next']['days_to_peak']}일 뒤 정점({x['next']['peak_mmdd']}), "
          f"**{x['next']['publish_on']} 발행** / 현재 {x['peak_pct']}% 수준" for x in due] or ["- 없음"]

    hot = [x for x in out if x["momentum"] and x["momentum"] >= 1.3]
    L += ["", "## 지금 올라오는 중 (최근 4주 ÷ 직전 4주 ≥ 1.3)"]
    L += [f"- **{x['group']}** ×{x['momentum']}"
          + (f" (지난주 대비 {x['d_momentum']:+})" if x["d_momentum"] is not None else "")
          + (f" / 전년비 {x['yoy']}" if x["yoy"] else " / 전년비 참고불가(lunar)")
          for x in hot] or ["- 없음"]

    high = [x for x in out if x["peak_pct"] >= 70]
    L += ["", "## 자기 최고치 근접 (계절 봉우리와 무관한 상시 수요)"]
    L += [f"- **{x['group']}** — 2년 최고치의 {x['peak_pct']}% 지점"
          + (f" ({x['d_peak_pct']:+}%p)" if x["d_peak_pct"] is not None else "")
          for x in sorted(high, key=lambda z: -z["peak_pct"])] or ["- 없음"]

    L += ["", "## 전체 캘린더 (봉우리 전부)"]
    for x in out:
        tag = " ⚠️lunar" if x["lunar"] else ""
        ps = " · ".join(f"{p['peak_mmdd']}({p['share']})" for p in x["peaks"]) or "봉우리 없음"
        L.append(f"- **{x['group']}**{tag} — {ps}"
                 + (f" / 다음 발행 {x['next']['publish_on']}" if x["next"] and not x["lunar"] else ""))

    if failed:
        L += ["", "## 조회 실패", *[f"- {f}" for f in failed]]

    brief = "\n".join(L)
    open("brief.md", "w", encoding="utf-8").write(brief)
    print(brief)

    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if tok and chat:
        urlreq.urlopen(urlreq.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": "[검색수요 감시] " + brief[:3500]}).encode("utf-8"),
            headers={"Content-Type": "application/json"}), timeout=15)


if __name__ == "__main__":
    main()
