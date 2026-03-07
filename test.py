from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
import json
import pandas as pd
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],  # <-- Именно эта строчка разрешает OPTIONS-запросы!
    allow_headers=["*"],
)

@app.get("/api/ping")
async def ping_server():
    """Простой эндпоинт для пробуждения сервера"""
    return {"status": "awake", "message": "Сервер готов к работе!"}

# Специальная ссылка для прямого скачивания листа в формате CSV
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1MNUzN4jpE-E8vEVtU4ng_WmHyHEDxOCLh7iSaWzuIKQ/export?format=csv&gid=60648042"

class ProductRequest(BaseModel):
    url: str

def get_commission_from_sheet(category_name: str):
    try:
        df = pd.read_csv(SHEET_CSV_URL)
        
        # Проходимся по всем 17 колонкам таблицы
        for col in df.columns:
            # Ищем точное совпадение категории (игнорируем регистр букв и лишние пробелы)
            match = df[df[col].astype(str).str.strip().str.lower() == category_name.strip().lower()]
            
            if not match.empty:
                # Мы нашли строку с этой категорией! Берем ее данные
                row_data = match.iloc[0]
                # Собираем все непустые ячейки из этой строки в один список
                row_values = [str(v).strip() for v in row_data.values if pd.notna(v) and str(v).strip() != '']
                # Проверяем, что в строке достаточно данных
                if len(row_values) >= 3:
                    # Берем три последних элемента списка
                    return {
                        "FBO": row_values[-3],
                        "FBS": row_values[-2],
                        "DBS": row_values[-1]
                    }
                else:
                    return {"error": "В таблице не хватает колонок с комиссиями для этой категории"}
                
        return "Категория не найдена в таблице комиссий."
        
    except Exception as e:
        return f"Ошибка при чтении таблицы: {str(e)}"


@app.post("/api/get-category")
async def get_uzum_category(request: ProductRequest):
    
    target_url = request.url
    
    # 1. Если это узбекская ссылка с сайта
    if "uzum.uz/uz/" in target_url:
        target_url = target_url.replace("uzum.uz/uz/", "uzum.uz/ru/")
    # 2. Если это ссылка из мобильного приложения (без языка)
    elif "uzum.uz/product/" in target_url:
        target_url = target_url.replace("uzum.uz/product/", "uzum.uz/ru/product/")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1"
    }

    async with httpx.AsyncClient() as client:
        try:
            # ВАЖНО: здесь теперь передаем target_url, а не request.url
            response = await client.get(target_url, headers=headers, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=400, detail=f"Ошибка при обращении к Uzum: {e}")

    # ... весь остальной код оставляем без изменений ...

    soup = BeautifulSoup(response.text, 'html.parser')
    category_path = []

    # Способ 1: Ищем микроразметку JSON-LD
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get('@type') == 'BreadcrumbList':
                elements = data.get('itemListElement', [])
                category_path = [el['item']['name'] for el in elements if 'item' in el and 'name' in el['item']]
                break
        except Exception:
            continue

    # Способ 2: Классический парсинг HTML с фильтрацией мусора
    if not category_path:
        breadcrumb_elements = soup.select('nav a')
        raw_path = [el.get_text(strip=True) for el in breadcrumb_elements if el.get_text(strip=True)]
        
        for item in raw_path:
            if "Соглашение" in item or "Положение" in item:
                break
            if item not in ["Главная", "Все категории"] and item not in category_path:
                category_path.append(item)

    if not category_path:
        raise HTTPException(status_code=404, detail="Не удалось найти категории на странице.")

    # Получаем конечную категорию
    final_category = category_path[-1]

    # --- 2. ПАРСИНГ ЦЕНЫ (НОВЫЙ БЛОК) ---
    price_without_card = None
    
    # Получаем весь текст со страницы, разделяя элементы пробелом
    page_text = soup.get_text(separator=' ')
    
    # Ищем шаблон "Без карты Uzum [любые цифры и пробелы] сум"
    match = re.search(r'Без карты Uzum\s*([\d\s\xa0]+)\s*сум', page_text, re.IGNORECASE)
    
    if match:
        # Берем найденные цифры и очищаем от пробелов (включая неразрывные), чтобы получить чистое число
        clean_price = re.sub(r'[^\d]', '', match.group(1))
        price_without_card = int(clean_price)
    else:
        # ВАЖНО: У некоторых товаров нет плашки "Без карты Uzum" (цена единая).
        # В таком случае берем базовую цену из микроразметки товара.
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get('@type') == 'Product':
                    offers = data.get('offers', {})
                    if isinstance(offers, dict) and 'price' in offers:
                        price_without_card = int(offers['price'])
                        break
            except Exception:
                continue

    # Идем в Google Таблицу и ищем комиссию для этой категории
    commission_value = get_commission_from_sheet(final_category)

    # Возвращаем полный ответ
    return {
        "url": request.url,
        "category_path": category_path,
        "final_category": final_category,
        "price_without_card": price_without_card,
        "commission": commission_value
    }