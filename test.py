import asyncio
import json
import re
import time

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sardor018.github.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/ping")
async def ping_server():
    """Простой эндпоинт для пробуждения сервера"""
    return {"status": "awake", "message": "Сервер готов к работе!"}


# Официальная таблица комиссий Uzum (лист «Комиссия на продажу»)
SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/1J9EQEJqZcF3eRS4nLLAjR0M8jcb0QabZGFpfIP_mpRU"
    "/export?format=csv&gid=1248630206"
)


class ProductRequest(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# Таблица комиссий: загрузка, разбор, кеш
# ---------------------------------------------------------------------------
CACHED_RECORDS = None
LAST_UPDATE_TIME = 0
CACHE_EXPIRE_SECONDS = 24 * 60 * 60
RETRY_AFTER_FAIL_SECONDS = 5 * 60

DASH_RE = re.compile(r"^[-–—]\s*%?$")
NO_SCHEME = "Нет в схеме"


def norm(text: str) -> str:
    """Единый вид названий: регистр, ё/е, неразрывные и двойные пробелы."""
    text = str(text).replace("\xa0", " ").replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", text).strip().lower()


def clean_pct(value: str):
    value = str(value).strip()
    if not value or DASH_RE.match(value):
        return None  # "-%" = в этой схеме категории нет
    return value


def build_records(rows):
    """
    Колонки листа: category ID | cat_code | category1_ru ... category6_ru |
    comm FBO/FBS/DBS % (до скидки) | скидки | comm FBO/FBS/DBS % (после скидки).
    Колонки ищем по заголовкам, а не по номерам.
    """
    hdr = next((i for i, r in enumerate(rows) if any(norm(c) == "category1_ru" for c in r)), None)
    if hdr is None:
        raise ValueError("в таблице не найдена строка заголовков (category1_ru)")

    head = [norm(c).replace(" ", "") for c in rows[hdr]]
    id_col = head.index("categoryid") if "categoryid" in head else 0
    cat_cols = [i for i, h in enumerate(head) if re.fullmatch(r"category\d+_ru", h)]

    def cols(name):
        return [i for i, h in enumerate(head) if h == name]

    fbo, fbs, dbs = cols("commfbo%"), cols("commfbs%"), cols("commdbs%")
    if not (cat_cols and fbo and fbs and dbs):
        raise ValueError("в таблице не найдены колонки категорий или комиссий")

    records = []
    for row in rows[hdr + 1:]:
        path = [row[i].strip() for i in cat_cols if i < len(row) and row[i].strip()]
        if not path:
            continue
        records.append({
            "id": row[id_col].strip() if id_col < len(row) else "",
            "path": path,
            "path_norm": [norm(p) for p in path],
            # последние вхождения колонок — «Комиссия после скидки» (то, что реально платит продавец)
            "FBO": clean_pct(row[fbo[-1]]),
            "FBS": clean_pct(row[fbs[-1]]),
            "DBS": clean_pct(row[dbs[-1]]),
        })
    return records


def load_records():
    """Возвращает разобранную таблицу; при сбое обновления отдаёт старый кеш."""
    global CACHED_RECORDS, LAST_UPDATE_TIME
    now = time.time()

    if CACHED_RECORDS is not None and (now - LAST_UPDATE_TIME) < CACHE_EXPIRE_SECONDS:
        return CACHED_RECORDS

    try:
        print("⏳ Загружаю свежие данные из Google Таблицы...")
        df = pd.read_csv(SHEET_CSV_URL, header=None, dtype=str).fillna("")
        rows = [[str(v).strip() for v in r] for r in df.values.tolist()]
        CACHED_RECORDS = build_records(rows)
        LAST_UPDATE_TIME = now
        print(f"✅ Загружено категорий: {len(CACHED_RECORDS)}")
    except Exception as e:
        if CACHED_RECORDS is None:
            raise
        print(f"⚠️ Ошибка обновления, использую старый кеш: {e}")
        LAST_UPDATE_TIME = now - CACHE_EXPIRE_SECONDS + RETRY_AFTER_FAIL_SECONDS

    return CACHED_RECORDS


def _result(rec, how):
    show = lambda v: v if v else NO_SCHEME
    return {
        "FBO": show(rec["FBO"]),
        "FBS": show(rec["FBS"]),
        "DBS": show(rec["DBS"]),
        "matched_category": " > ".join(rec["path"]),
        "category_id": rec["id"],
        "match": how,  # exact / parent / group / leaf
    }


def get_commission_from_sheet(category_path: list):
    """Всегда возвращает dict: {FBO, FBS, DBS, matched_category, ...} или {"error": ...}."""
    try:
        records = load_records()
    except Exception as e:
        return {"error": f"Не удалось прочитать таблицу комиссий: {e}"}

    pn = [norm(c) for c in category_path if norm(c) not in ("главная", "все категории")]
    if not pn:
        return {"error": "Пустой путь категорий"}

    # 1. Полное совпадение пути
    for r in records:
        if r["path_norm"] == pn:
            return _result(r, "exact")

    # 2. В таблице категория короче (например, в хлебных крошках в конце название товара)
    parents = [r for r in records
               if len(r["path_norm"]) < len(pn) and pn[:len(r["path_norm"])] == r["path_norm"]]
    if parents:
        return _result(max(parents, key=lambda r: len(r["path_norm"])), "parent")

    # 3. В таблице категория глубже: подходит, только если у всех подкатегорий одинаковая комиссия
    deeper = [r for r in records if r["path_norm"][:len(pn)] == pn]
    if deeper:
        triples = {(r["FBO"], r["FBS"], r["DBS"]) for r in deeper}
        if len(triples) == 1:
            return _result(deeper[0], "group")
        sample = "; ".join(" > ".join(r["path"]) for r in deeper[:3])
        return {"error": (f"У категории «{' > '.join(category_path)}» {len(deeper)} подкатегорий "
                          f"с разными комиссиями, например: {sample}")}

    # 4. По названию последней категории (затем предпоследней), лучшее совпадение по пути
    for leaf in (pn[-1], pn[-2] if len(pn) > 1 else None):
        if not leaf:
            continue
        cands = [r for r in records if r["path_norm"][-1] == leaf]
        if cands:
            best = max(cands, key=lambda r: sum(p in r["path_norm"] for p in pn))
            return _result(best, "leaf")

    return {"error": f"Категория не найдена в таблице комиссий: {' > '.join(category_path)}"}


# ---------------------------------------------------------------------------
# Парсинг страницы Uzum
# ---------------------------------------------------------------------------
def extract_breadcrumbs_jsonld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except Exception:
            continue

        blocks = data if isinstance(data, list) else [data]
        for block in blocks:
            if not isinstance(block, dict) or block.get("@type") != "BreadcrumbList":
                continue
            names = []
            for el in block.get("itemListElement", []):
                item = el.get("item")
                name = el.get("name") or (item.get("name") if isinstance(item, dict) else None)
                if name:
                    names.append(name)
            if names:
                return names
    return []


@app.post("/api/get-category")
async def get_uzum_category(request: ProductRequest):
    target_url = request.url

    if "uzum.uz/uz/" in target_url:
        target_url = target_url.replace("uzum.uz/uz/", "uzum.uz/ru/")
    elif "uzum.uz/product/" in target_url:
        target_url = target_url.replace("uzum.uz/product/", "uzum.uz/ru/product/")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1",
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(target_url, headers=headers, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=400, detail=f"Ошибка при обращении к Uzum: {e}")

    soup = BeautifulSoup(response.text, "html.parser")

    category_path = extract_breadcrumbs_jsonld(soup)

    if not category_path:
        raw_path = [el.get_text(strip=True) for el in soup.select("nav a") if el.get_text(strip=True)]
        for item in raw_path:
            if "Соглашение" in item or "Положение" in item:
                break
            if item not in ["Главная", "Все категории"] and item not in category_path:
                category_path.append(item)
    else:
        category_path = [c for c in category_path if norm(c) not in ("главная", "все категории")]

    if not category_path:
        title = soup.title.get_text(strip=True) if soup.title else None
        raise HTTPException(
            status_code=404,
            detail=(f"Не удалось найти категории. HTTP {response.status_code}, "
                    f"длина HTML: {len(response.text)}, title: {title}, "
                    f"итоговый URL: {response.url}"),
        )

    final_category = category_path[-1]

    # --- Цена ---
    price_without_card = None
    page_text = soup.get_text(separator=" ")
    match = re.search(r"Без карты Uzum\s*([\d\s\xa0]+)\s*сум", page_text, re.IGNORECASE)

    if match:
        price_without_card = int(re.sub(r"[^\d]", "", match.group(1)))
    else:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    offers = data.get("offers", {})
                    if isinstance(offers, dict) and "price" in offers:
                        price_without_card = int(float(offers["price"]))
                        break
            except Exception:
                continue

    commission_value = await asyncio.to_thread(get_commission_from_sheet, category_path)

    return {
        "url": request.url,
        "category_path": category_path,
        "final_category": final_category,
        "price_without_card": price_without_card,
        "commission": commission_value,
    }
