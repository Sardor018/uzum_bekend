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


SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1MNUzN4jpE-E8vEVtU4ng_WmHyHEDxOCLh7iSaWzuIKQ/export?format=csv&gid=60648042"


class ProductRequest(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# Кеш таблицы комиссий
# ---------------------------------------------------------------------------
CACHED_ROWS = None          # список строк (каждая строка — список строк-ячеек)
LAST_UPDATE_TIME = 0
CACHE_EXPIRE_SECONDS = 24 * 60 * 60
RETRY_AFTER_FAIL_SECONDS = 5 * 60

PERCENT_RE = re.compile(r"^\d+(?:[.,]\d+)?\s*%?$")


def norm(text: str) -> str:
    """Приводит название к единому виду: регистр, ё/е, неразрывные и двойные пробелы."""
    text = str(text).replace("\xa0", " ").replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", text).strip().lower()


def load_sheet():
    """Возвращает строки таблицы. При ошибке обновления отдаёт старый кеш (без рекурсии)."""
    global CACHED_ROWS, LAST_UPDATE_TIME
    now = time.time()

    if CACHED_ROWS is not None and (now - LAST_UPDATE_TIME) < CACHE_EXPIRE_SECONDS:
        return CACHED_ROWS

    try:
        print("⏳ Загружаю свежие данные из Google Таблицы...")
        # header=None — чтобы первая строка не терялась как заголовок
        df = pd.read_csv(SHEET_CSV_URL, header=None, dtype=str).fillna("")
        CACHED_ROWS = [[str(v).strip() for v in row] for row in df.values.tolist()]
        LAST_UPDATE_TIME = now
    except Exception as e:
        if CACHED_ROWS is None:
            raise
        print(f"⚠️ Ошибка обновления, использую старый кеш: {e}")
        # повторная попытка обновления через несколько минут
        LAST_UPDATE_TIME = now - CACHE_EXPIRE_SECONDS + RETRY_AFTER_FAIL_SECONDS

    return CACHED_ROWS


def get_commission_from_sheet(category_path: list):
    """
    Всегда возвращает dict:
      успех  -> {"FBO", "FBS", "DBS", "matched_category"}
      ошибка -> {"error": "..."}
    Ищет сначала по конечной категории, затем по родительским.
    """
    try:
        rows = load_sheet()
    except Exception as e:
        return {"error": f"Не удалось прочитать таблицу комиссий: {e}"}

    path_norm = [norm(c) for c in category_path]

    for idx in range(len(category_path) - 1, -1, -1):
        target = path_norm[idx]
        best_cells, best_score = None, -1

        for row in rows:
            cells = [c for c in row if c]
            if len(cells) < 4:
                continue
            cells_norm = [norm(c) for c in cells]
            if target not in cells_norm:
                continue

            tail = cells[-3:]
            if not all(PERCENT_RE.match(t) for t in tail):
                continue  # в строке нет трёх процентов в конце

            # если одно название встречается в разных ветках — выбираем строку,
            # где больше совпадений с остальным путём категорий
            score = sum(1 for p in path_norm if p in cells_norm)
            if score > best_score:
                best_cells, best_score = cells, score

        if best_cells:
            return {
                "FBO": best_cells[-3],
                "FBS": best_cells[-2],
                "DBS": best_cells[-1],
                "matched_category": category_path[idx],
            }

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

    # Способ 1: JSON-LD
    category_path = extract_breadcrumbs_jsonld(soup)

    # Способ 2: HTML
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
        raise HTTPException(status_code=404, detail="Не удалось найти категории на странице.")

    final_category = category_path[-1]

    # --- Цена ---
    price_without_card = None
    page_text = soup.get_text(separator=" ")
    match = re.search(r"Без карты Uzum\s*([\d\s\xa0]+)\s*сум", page_text, re.IGNORECASE)

    if match:
        clean_price = re.sub(r"[^\d]", "", match.group(1))
        price_without_card = int(clean_price)
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

    # Комиссия (чтение таблицы блокирующее — выносим в поток)
    commission_value = await asyncio.to_thread(get_commission_from_sheet, category_path)

    return {
        "url": request.url,
        "category_path": category_path,
        "final_category": final_category,
        "price_without_card": price_without_card,
        "commission": commission_value,
    }
