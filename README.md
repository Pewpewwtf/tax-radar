# СделатьВычет 2.4.1

Исправлена Яндекс Метрика:
- `analysis_success` теперь отправляется через `reachGoal`
- `payment_success` и `report_view` работают после возврата из оплаты
- `analysis_id` и другие query-параметры не передаются в Метрику
- Webvisor отключён
- clickmap отключён
- trackLinks отключён
- добавлены «Настройки аналитики» в footer
- добавлен console helper `sdelatVychetAnalyticsStatus()`

Server-side аналитика и подтверждение оплаты остаются источником истины.
