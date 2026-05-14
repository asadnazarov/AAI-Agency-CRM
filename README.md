# AI Sales CRM

Минималистичный CRM с канбаном для продажи AI Sales Manager.

## Быстрый старт

```bash
npm install
npm run dev
```

## Деплой на GitHub Pages

### 1. Создай репозиторий на GitHub
Зайди на github.com → New repository → назови `ai-sales-crm` → Create

### 2. Обнови vite.config.js
Поменяй `base` на имя твоего репо:
```js
base: '/ai-sales-crm/',   // замени если назвал по-другому
```

### 3. Обнови package.json
В поле `homepage` и скрипте `deploy`:
```json
"homepage": "https://ТВОЙ_USERNAME.github.io/ai-sales-crm"
```

### 4. Запушь код и задеплой
```bash
git init
git add .
git commit -m "init crm"
git branch -M main
git remote add origin https://github.com/ТВОЙ_USERNAME/ai-sales-crm.git
git push -u origin main

npm run deploy
```

### 5. GitHub Pages настройки
Зайди в репо → Settings → Pages → Source: `gh-pages` branch → Save

Через ~1 минуту сайт будет на `https://ТВОЙ_USERNAME.github.io/ai-sales-crm`

---

## Google Sheets интеграция

Таблица должна быть открыта для просмотра (Файл → Настройки доступа → Все у кого есть ссылка → Просмотр).

- **Новые лиды** (gid=214734999) — лиды от AI-агента
- **В работе** (gid=1447471635) — текущие лиды

Нажми кнопку **"Новые лиды"** или **"В работе"** в шапке чтобы импортировать.

---

## Groq API

Используется модель `moonshotai/kimi-k2-instruct` для анализа воронки.
Ключ вшит в `src/constants.js` — если нужно поменять, правь там.
