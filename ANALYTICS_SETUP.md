# СделатьВычет 2.4 — аналитика

## 1. Яндекс Метрика

Создайте обычный счётчик Метрики для домена СделатьВычет.

В Timeweb → приложение → переменные добавьте:

METRIKA_COUNTER_ID=<номер счётчика>

Вебвизор в коде приложения принудительно отключён.
Счётчик загружается только после разрешения пользователя на аналитику.

В интерфейсе Метрики создайте цели типа «Целевое событие» с идентификаторами:

- upload_click
- file_selected
- analysis_started
- analysis_success
- analysis_error
- result_view
- paywall_view
- payment_click
- payment_success
- report_view
- guide_step_1
- guide_step_2
- guide_step_3
- guide_complete

Метрика получает только названия целей. Merchant names, содержимое выписки,
точные суммы расходов и analysis_id ей не передаются.

## 2. Наша first-party аналитика

По умолчанию база:
data/analytics.sqlite3

Для production лучше подключить persistent volume и указать:

ANALYTICS_DB_PATH=/persistent/analytics.sqlite3

Иначе при полном пересоздании контейнера локальная SQLite-база может пропасть.

## 3. Закрытый дашборд

Добавьте в Timeweb секрет:

ANALYTICS_DASHBOARD_TOKEN=<случайная длинная строка>

НЕ добавляйте этот токен в GitHub.

После деплоя откройте:

/analytics

Введите токен. Там будут:
- визиты
- успешные анализы
- оплаты
- выручка
- полная продуктовая воронка
- Visit → Analysis
- Result → Pay
- Visit → Pay
- разбивка по utm_source

## 4. Что хранится

Храним:
- случайный anonymous session id
- event
- UTM source/medium/campaign/content/term
- referrer hostname
- технический код банка/парсера для диагностики
- бакеты количества кандидатов и потенциального возврата
- факт платежа и фиксированную цену отчёта

Не храним в analytics:
- merchant names
- описания транзакций
- номера счетов и карт
- ФИО из выписки
- точные суммы отдельных операций
- содержимое отчёта

## 5. Событие payment_success

Считается сервером после подтверждения оплаты ЮKassa.
Поэтому это главный источник истины по покупкам, а не браузерная цель.
