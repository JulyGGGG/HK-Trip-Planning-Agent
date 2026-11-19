"""Places supported by the first release's official-source search."""

PLACES = {
    "art-museum": {
        "category": "activities", "name": "香港藝術館", "english": "Hong Kong Museum of Art",
        "url": "https://hk.art.museum/en/web/ma/visit/opening-hours-and-admission.html",
        "station": "Tsim Sha Tsui", "area": "tst-waterfront",
        "terms": ("art", "museum", "藝術", "艺术", "文化", "hkmoa", "尖沙咀", "室内"),
    },
    "history-museum": {
        "category": "activities", "name": "香港歷史博物館", "english": "Hong Kong Museum of History",
        "url": "https://hk.history.museum/en/web/mh/plan-your-visit/admission.html",
        "station": "Tsim Sha Tsui", "area": "tst-east",
        "terms": ("history", "museum", "歷史", "历史", "博物馆", "文化", "尖沙咀", "室内"),
    },
    "science-museum": {
        "category": "activities", "name": "香港科學館", "english": "Hong Kong Science Museum",
        "url": "https://hk.science.museum/en/web/scm/visit.html",
        "station": "Tsim Sha Tsui", "area": "tst-east",
        "terms": ("science", "museum", "科学", "科學", "亲子", "家庭", "family", "室内"),
    },
    "hong-kong-park": {
        "category": "activities", "name": "香港公園", "english": "Hong Kong Park",
        "url": "https://hkp.lcsd.gov.hk/en/visit",
        "station": "Admiralty", "area": "central-park",
        "terms": ("outdoor", "nature", "park", "公园", "公園", "自然", "户外", "中環", "中环"),
    },
    "mei-ho-house": {
        "category": "lodging", "name": "YHA 美荷樓青年旅舍", "english": "YHA Mei Ho House Youth Hostel",
        "url": "https://www.yha.org.hk/en/hostel/yha-mei-ho-house-youth-hostel/",
        "station": "Sham Shui Po", "area": "ssp",
        "terms": ("mei ho", "深水埗", "美荷", "hostel", "旅舍"),
    },
    "mt-davis": {
        "category": "lodging", "name": "YHA 摩星嶺青年旅舍", "english": "YHA Jockey Club Mt. Davis Youth Hostel",
        "url": "https://www.yha.org.hk/en/hostel/jockey-club-mt-davis-youth-hostel/",
        "station": "Kennedy Town", "area": "mt-davis",
        "terms": ("mount davis", "mt. davis", "摩星", "hostel", "自然"),
    },
}

GATEWAY_POINTS = {
    "airport": ("Airport", "airport"),
    "west_kowloon": ("Austin", "west-kowloon"),
    "central": ("Hong Kong", "central-station"),
}


def matching_activity(name):
    exact = name.casefold().replace(" ", "")
    for place_id, place in PLACES.items():
        if place["category"] != "activities":
            continue
        known = {label.casefold().replace(" ", "") for label in (place_id, place["name"], place["english"])}
        if exact in known:
            return place_id
    return {
        "香港艺术馆": "art-museum", "艺术馆": "art-museum", "香港历史博物馆": "history-museum",
        "历史博物馆": "history-museum", "香港科学馆": "science-museum", "香港公园": "hong-kong-park",
    }.get(exact)
