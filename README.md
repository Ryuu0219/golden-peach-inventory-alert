# 金桃家庫存警示推播

每日 09:00 / 18:00 自動檢查 `2026原料庫存表`，推播 🔴 警示 + 🟡 注意 品項到指定 LINE 用戶。

## 邏輯

目標庫存天數 = 到貨天數 + 安全緩衝（到貨×50%）+ 訂貨週期

## 修改

- 推播對象：`check_inventory.py` 內 `LINE_USER_IDS`
- 目標天數：`check_inventory.py` 內 `TARGET_DAYS`
- 排程：`.github/workflows/inventory-check.yml` 的 cron

## Secrets（GitHub repo 設定）

- `GSPREAD_CREDENTIALS_JSON`
- `GSPREAD_AUTHORIZED_USER_JSON`
- `LINE_CHANNEL_ACCESS_TOKEN`

## 手動觸發測試

`gh workflow run inventory-check.yml`
