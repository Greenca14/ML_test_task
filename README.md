# Запуск

```bash
python postprocess.py data/test_predictions.jsonl answers.jsonl
```

Скрипт создаёт `answers.jsonl`: по одной строке на документ с решением `accept` или `reject`. Для принятых документов также записываются распознанные поля.

Отчёт: `REPORT.md`.
