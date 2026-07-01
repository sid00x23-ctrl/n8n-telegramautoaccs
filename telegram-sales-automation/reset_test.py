#!/usr/bin/env python3
"""
reset_test.py — сброс данных для повторного тестирования флоу

Что очищает:
  1. Google Sheets: Статус, Аккаунт, Запрос, Дата контакта

Использование:
  python3 reset_test.py                     # сброс всего
  python3 reset_test.py --account anasty_4  # только один аккаунт
"""

import argparse
import json
import time
import urllib.request
import urllib.error
import sys
import uuid

# ── Настройки ──────────────────────────────────────────────────────────────────
N8N_BASE       = "http://127.0.0.1:5678/api/v1"
N8N_KEY_FILE   = "/tmp/n8n_key.txt"
SHEET_ID       = "1klQ-UYUhEyQsIABQV1XgpBM2zdMmRYNA-oFo_ITZ6Ig"
GSHEETS_CRED_ID = "THxu89rT7ChaXOMp"


# ── n8n API helper ─────────────────────────────────────────────────────────────
def n8n(method, path, body=None):
    key = open(N8N_KEY_FILE).read().strip()
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    req = urllib.request.Request(
        N8N_BASE + path, data=data,
        headers={"X-N8N-API-KEY": key, "Content-Type": "application/json; charset=utf-8"},
        method=method
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


# ── Google Sheets через временный n8n-воркфлоу ────────────────────────────────
def clear_sheets(account_id=None):
    webhook_path = f"reset-{uuid.uuid4().hex[:8]}"

    # Код фильтра: какие строки сбрасывать
    if account_id:
        filter_expr = (
            f"let items = $input.all();\n"
            f"items = items.filter(i => i.json['Аккаунт'] === '{account_id}');\n"
            f"return items.filter(i => String(i.json.User_id || '').trim() !== '');"
        )
    else:
        filter_expr = (
            "let items = $input.all();\n"
            "items = items.filter(i =>\n"
            "  i.json['Статус'] || i.json['Аккаунт'] ||\n"
            "  i.json['Запрос'] || i.json['Дата контакта']\n"
            ");\n"
            "return items.filter(i => String(i.json.User_id || '').trim() !== '');"
        )

    workflow = {
        "name": "_tmp_reset_sheets",
        "nodes": [
            {
                "id": "rn1",
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [200, 300],
                "parameters": {
                    "httpMethod": "POST",
                    "path": webhook_path,
                    "responseMode": "onReceived",
                    "responseData": "noData"
                },
                "webhookId": webhook_path
            },
            {
                "id": "rn2",
                "name": "Read Sheet",
                "type": "n8n-nodes-base.googleSheets",
                "typeVersion": 4.5,
                "position": [440, 300],
                "parameters": {
                    "operation": "read",
                    "documentId": {"__rl": True, "mode": "id", "value": SHEET_ID},
                    "sheetName":  {"__rl": True, "mode": "id", "value": "0"},
                    "options": {}
                },
                "credentials": {
                    "googleSheetsOAuth2Api": {"id": GSHEETS_CRED_ID, "name": "Google Sheets"}
                }
            },
            {
                "id": "rn3",
                "name": "Filter",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [680, 300],
                "parameters": {"jsCode": filter_expr}
            },
            {
                "id": "rn4",
                "name": "Clear Fields",
                "type": "n8n-nodes-base.googleSheets",
                "typeVersion": 4.5,
                "position": [920, 300],
                "parameters": {
                    "operation": "update",
                    "documentId": {"__rl": True, "mode": "id", "value": SHEET_ID},
                    "sheetName":  {"__rl": True, "mode": "id", "value": "0"},
                    "columns": {
                        "mappingMode": "defineBelow",
                        "value": {
                            "User_id":        "={{ $json.User_id }}",
                            "Статус":         "",
                            "Аккаунт":        "",
                            "Запрос":         "",
                            "Дата контакта":  ""
                        },
                        "matchingColumns": ["User_id"],
                        "schema": []
                    },
                    "options": {}
                },
                "credentials": {
                    "googleSheetsOAuth2Api": {"id": GSHEETS_CRED_ID, "name": "Google Sheets"}
                }
            }
        ],
        "connections": {
            "Webhook":    {"main": [[{"node": "Read Sheet",   "type": "main", "index": 0}]]},
            "Read Sheet": {"main": [[{"node": "Filter",       "type": "main", "index": 0}]]},
            "Filter":     {"main": [[{"node": "Clear Fields", "type": "main", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1"}
    }

    wf_id = None
    try:
        # Создаём и активируем
        wf = n8n("POST", "/workflows", workflow)
        wf_id = wf["id"]
        n8n("POST", f"/workflows/{wf_id}/activate")
        time.sleep(1)  # ждём регистрации вебхука

        # Триггерим
        req = urllib.request.Request(
            f"http://127.0.0.1:5678/webhook/{webhook_path}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

        # Ждём завершения воркфлоу
        time.sleep(3)

    finally:
        # Деактивируем и удаляем временный воркфлоу
        if wf_id:
            try:
                n8n("POST", f"/workflows/{wf_id}/deactivate")
            except Exception:
                pass
            try:
                n8n("DELETE", f"/workflows/{wf_id}")
            except Exception:
                pass


# ── main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Сброс данных для повторного тестирования флоу")
    parser.add_argument(
        "--account",
        metavar="ACCOUNT_ID",
        help="Сбросить только этот аккаунт (напр. anasty_4). Без флага — сброс всего."
    )
    args = parser.parse_args()

    label = f"аккаунт '{args.account}'" if args.account else "все аккаунты"
    print(f"\nСброс данных: {label}\n{'─'*40}")

    print("[Google Sheets] Очищаем Статус, Аккаунт, Запрос, Дата контакта...")
    try:
        clear_sheets(args.account)
        print("[Google Sheets] Готово")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[Google Sheets] Ошибка HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[Google Sheets] Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    print("\nСброс завершён.")


if __name__ == "__main__":
    main()
